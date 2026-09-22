"""Live Kraken execution with a mocked client. No network."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quanttrading.cli import app
from quanttrading.config import Settings
from quanttrading.data.ohlcv import save_csv
from quanttrading.execution.credentials import KrakenCredentialsError, load_kraken_credentials, looks_like_placeholder
from quanttrading.execution.live import (
    LIVE_STRATEGY_ID,
    ExchangeCallError,
    LiveBroker,
    TradeOnlyClient,
    kraken_trade_client,
    kraken_userref,
    parse_spot_balance,
    run_live_latest,
    summarize_live,
)
from quanttrading.execution.market_meta import MarketConstraints, StaticMarketMetadata
from quanttrading.execution.paper import PaperBroker
from quanttrading.status.snapshot import build_status
from quanttrading.strategy.sma import SMACrossover
from tests.helpers import bars_from_closes, make_bar, make_signal

ROOT = Path(__file__).resolve().parents[1]
RUNNER = CliRunner()
KEY = "kValidKey0123456789abcdef"
SECRET = "sValidSecret0123456789abcd"
TRIAL = Settings(
    paper_equity=2000,
    per_trade_pct=0.005,
    daily_dd_pct=0.03,
    total_dd_pct=0.20,
    max_slippage_bps=5.0,
)
PAPER_PCT = Settings(
    paper_equity=2000,
    per_trade_pct=0.01,
    daily_dd_pct=0.03,
    total_dd_pct=0.20,
    max_slippage_bps=5.0,
)
WARMUP = [10.0, 10.0, 10.0, 10.0, 10.0]
GOLDEN_CROSS = WARMUP + [20.0]
DEATH_CROSS = GOLDEN_CROSS + [20.0, 20.0, 1.0]


class FakeKraken:
    def __init__(
        self,
        *,
        usd: float = 2000.0,
        btc: float = 0.0,
        average: float = 100.0,
        fail: BaseException | None = None,
        filled: float | None = None,
        follow_up: dict | None = None,
    ) -> None:
        self.usd = usd
        self.btc = btc
        self.average = average
        self.fail = fail
        self.filled = filled
        self.follow_up = follow_up
        self.calls: list[tuple] = []

    def fetch_balance(self) -> dict:
        self.calls.append(("fetch_balance",))
        return {
            "USD": {"free": self.usd, "used": 0.0, "total": self.usd},
            "BTC": {"free": self.btc, "used": 0.0, "total": self.btc},
        }

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, type, side, amount, price, params))
        if self.fail is not None:
            raise self.fail
        filled = amount if self.filled is None else self.filled
        return {
            "id": "OID-1",
            "symbol": symbol,
            "type": type,
            "side": side,
            "amount": amount,
            "filled": filled,
            "average": self.average if filled else None,
            "status": "closed" if filled else "open",
        }

    def fetch_order(self, order_id, symbol=None, params=None):
        self.calls.append(("fetch_order", order_id, symbol))
        if self.follow_up is None:
            raise AssertionError("fetch_order was not expected")
        payload = dict(self.follow_up)
        payload.setdefault("id", order_id)
        return payload


def _orders(fake: FakeKraken) -> list[tuple]:
    return [call for call in fake.calls if call[0] == "create_order"]


def _broker(
    fake: FakeKraken,
    *,
    store_path: str | Path = ":memory:",
    settings: Settings = TRIAL,
    market: StaticMarketMetadata | None = None,
    symbol: str = "BTC/USD",
    enable_trading: bool = True,
) -> LiveBroker:
    return LiveBroker(
        settings,
        store_path=store_path,
        enable_trading=enable_trading,
        client=fake,
        market=market,
        symbol=symbol,
    )


def _short_sma() -> SMACrossover:
    return SMACrossover(fast=2, slow=4, vol_window=3, min_vol=0.0005, max_slippage_bps=5.0)


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("live test opened a network connection")

    monkeypatch.setattr(socket, "create_connection", explode)


def test_unconfigured_broker_never_sends() -> None:
    live = LiveBroker()
    with pytest.raises(RuntimeError, match="No exchange API keys"):
        live.submit(make_signal(), make_bar())
    with pytest.raises(RuntimeError, match="disabled"):
        live.equity()
    fake = FakeKraken()
    disabled = _broker(fake, enable_trading=False)
    with pytest.raises(RuntimeError, match="No exchange API keys"):
        disabled.submit(make_signal(qty=10, qty_unit="quote"), make_bar(close=100))
    assert fake.calls == []


def test_paper_pct_is_refused_before_any_call() -> None:
    fake = FakeKraken()
    with pytest.raises(ValueError, match="0.2%"):
        _broker(fake, settings=PAPER_PCT)
    assert fake.calls == []


def test_risk_reject_does_not_send(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_network(monkeypatch)
    fake = FakeKraken()
    broker = _broker(fake)
    bar = make_bar(close=100.0)
    report = broker.submit(make_signal(qty=15.0, qty_unit="quote", client_order_id="too-big"), bar)
    assert report.status == "rejected"
    assert report.reason == "per_trade_limit"
    assert _orders(fake) == []
    assert broker.orders_sent == 0
    assert broker.position_qty(bar.symbol) == 0.0
    row = broker.store.fills()[0]
    assert row["status"] == "rejected"
    assert row["intent"] == "open"
    assert row["client_order_id"] == "too-big"


def test_daily_breaker_rejects_open_without_sending() -> None:
    fake = FakeKraken()
    broker = _broker(fake)
    bar = make_bar(close=100.0)
    broker.mark_to_market(bar)
    broker.cash = 2000.0 - 60.0
    broker.mark_to_market(bar)
    report = broker.submit(make_signal(qty=5.0, qty_unit="quote", client_order_id="daily"), bar)
    assert report.status == "rejected"
    assert report.reason == "daily_circuit_breaker"
    assert _orders(fake) == []


def test_happy_path_builds_market_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_network(monkeypatch)
    market = StaticMarketMetadata(MarketConstraints(min_qty=0.0001, amount_step=0.0001, price_tick=0.1))
    fake = FakeKraken(average=100.0)
    broker = _broker(fake, market=market)
    bar = make_bar(close=100.0)
    report = broker.submit(
        make_signal(qty=10.0, qty_unit="quote", max_slippage_bps=5, client_order_id="ok-1"),
        bar,
    )
    assert report.status == "filled"
    assert report.qty_base == pytest.approx(0.1)
    assert report.fill_price == pytest.approx(100.0)
    assert broker.orders_sent == 1
    assert broker.position_qty("BTC/USD") == pytest.approx(0.1)
    assert len(_orders(fake)) == 1
    _, symbol, order_type, side, amount, price, params = _orders(fake)[0]
    assert symbol == "BTC/USD"
    assert order_type == "market"
    assert side == "buy"
    assert amount == pytest.approx(0.1)
    assert price is None
    assert params == {"userref": kraken_userref("ok-1")}
    assert "ok-1" not in params.values()
    assert isinstance(params["userref"], int)
    assert 1 <= params["userref"] < 2**31
    row = broker.store.fills()[0]
    assert row["status"] == "filled"
    assert row["exchange_order_id"] == "OID-1"
    assert row["order_type"] == "market"
    summary = summarize_live(broker, strategy_id="sma_cross_v2", n_bars=1, last_bar_ts="t", report=report)
    assert summary["mode"] == "live"
    assert summary["orders_sent"] == 1
    assert summary["n_risk_rejected"] == 0
    assert summary["state_mode"] == "live"
    assert summary["last_bar_only"] is True


def test_qty_is_floored_to_step_before_send() -> None:
    market = StaticMarketMetadata(MarketConstraints(min_qty=0.01, amount_step=0.03, price_tick=0.5))
    fake = FakeKraken()
    broker = _broker(fake, market=market)
    report = broker.submit(make_signal(qty=10.0, qty_unit="quote", client_order_id="step"), make_bar(close=100.0))
    assert report.status == "filled"
    assert _orders(fake)[0][4] == pytest.approx(0.09)


def test_min_qty_does_not_send() -> None:
    market = StaticMarketMetadata(MarketConstraints(min_qty=1.0))
    fake = FakeKraken()
    broker = _broker(fake, market=market)
    report = broker.submit(make_signal(qty=10.0, qty_unit="quote", client_order_id="tiny"), make_bar(close=100.0))
    assert report.status == "size_invalid"
    assert report.reason == "below_min_qty"
    assert _orders(fake) == []
    assert broker.position_qty("BTC/USD") == 0.0


def test_halt_rejects_open_and_still_sends_close(tmp_path: Path) -> None:
    path = tmp_path / "live.sqlite"
    market = StaticMarketMetadata(MarketConstraints(last_price=100.0, min_qty=0.0001, amount_step=0.0001))
    fake = FakeKraken(usd=1900.0, btc=0.5, average=100.0)
    broker = _broker(fake, store_path=path, market=market)
    bar = make_bar(close=100.0)
    broker.mark_to_market(bar)
    broker.cash = 1500.0
    broker.mark_to_market(bar)
    assert broker.risk.halted
    assert broker.risk.halt_reason == "max_drawdown"
    opened = broker.submit(make_signal(qty=5.0, qty_unit="quote", client_order_id="halt-open"), bar)
    assert opened.status == "rejected"
    assert opened.reason == "max_drawdown"
    closed = broker.submit(
        make_signal(side="sell", intent="close", qty=0.5, qty_unit="base", client_order_id="halt-close"),
        bar,
    )
    assert closed.status == "filled"
    assert _orders(fake)[0][3] == "sell"
    assert _orders(fake)[0][4] == pytest.approx(0.5)
    broker.store.close()

    restarted = _broker(FakeKraken(usd=1900.0, btc=0.0), store_path=path, market=market)
    assert restarted.risk.halted
    again = restarted.submit(make_signal(qty=5.0, qty_unit="quote", client_order_id="halt-open-2"), bar)
    assert again.status == "rejected"
    assert again.reason == "max_drawdown"
    assert _orders(restarted.client) == []  # type: ignore[arg-type]


def test_duplicate_client_order_id_does_not_resend() -> None:
    fake = FakeKraken()
    broker = _broker(fake)
    bar = make_bar(close=100.0)
    signal = make_signal(qty=10.0, qty_unit="quote", client_order_id="once")
    assert broker.submit(signal, bar).status == "filled"
    second = broker.submit(signal, bar)
    assert second.status == "duplicate"
    assert second.reason == "filled"
    assert len(_orders(fake)) == 1
    assert len(broker.store.fills()) == 1


def test_kraken_ack_is_resolved_with_fetch_order() -> None:
    class AckThenFill(FakeKraken):
        def create_order(self, symbol, type, side, amount, price=None, params=None):
            self.calls.append(("create_order", symbol, type, side, amount, price, params))
            self.amount = amount
            return {"id": "TXID-9", "filled": 0, "average": None, "status": "open"}

        def fetch_order(self, order_id, symbol=None, params=None):
            self.calls.append(("fetch_order", order_id, symbol))
            return {
                "id": order_id,
                "filled": self.amount,
                "average": 100.0,
                "status": "closed",
                "symbol": symbol,
            }

    fake = AckThenFill()
    broker = _broker(fake)
    report = broker.submit(make_signal(qty=10.0, qty_unit="quote", client_order_id="ack"), make_bar(close=100.0))
    assert report.status == "filled"
    assert report.qty_base == pytest.approx(0.1)
    assert broker.position_qty("BTC/USD") == pytest.approx(0.1)
    assert broker.store.fills()[0]["exchange_order_id"] == "TXID-9"
    assert any(call[0] == "fetch_order" for call in fake.calls)


def test_submitted_when_the_ack_has_no_fill() -> None:
    fake = FakeKraken(filled=0.0)
    fake.follow_up = None
    fake.fetch_order = None  # type: ignore[method-assign]
    broker = _broker(fake)
    report = broker.submit(make_signal(qty=10.0, qty_unit="quote", client_order_id="resting"), make_bar(close=100.0))
    assert report.status == "submitted"
    assert broker.orders_sent == 1
    assert broker.position_qty("BTC/USD") == 0.0
    assert broker.store.fills()[0]["exchange_order_id"] == "OID-1"
    assert broker.store.fills()[0]["status"] == "submitted"


def test_exchange_error_is_stored_and_not_retried() -> None:
    fake = FakeKraken(fail=RuntimeError("insufficient funds"))
    broker = _broker(fake)
    signal = make_signal(qty=10.0, qty_unit="quote", client_order_id="nope")
    bar = make_bar(close=100.0)
    report = broker.submit(signal, bar)
    assert report.status == "exchange_rejected"
    assert report.reason is not None and "insufficient funds" in report.reason
    assert broker.orders_sent == 0
    assert broker.position_qty("BTC/USD") == 0.0
    again = broker.submit(signal, bar)
    assert again.status == "duplicate"
    assert len(_orders(fake)) == 1


def test_trade_client_redacts_secrets_and_blocks_withdraw() -> None:
    secret = "SECRETVALUE0123456789abcd"

    class Raw:
        def fetch_balance(self) -> dict:
            return {
                "USD": {"free": 2000.0, "used": 0.0, "total": 2000.0},
                "BTC": {"free": 0.0, "used": 0.0, "total": 0.0},
            }

        def create_order(self, *args: object, **kwargs: object) -> dict:
            raise RuntimeError(f"denied for {secret}")

        def withdraw(self, *args: object, **kwargs: object) -> dict:
            return {"sent": True}

    client = TradeOnlyClient(Raw(), secrets=(secret,))
    with pytest.raises(RuntimeError, match="withdraw"):
        client.withdraw("USD", 1.0)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="withdraw"):
        client.privatePostWithdraw()  # type: ignore[attr-defined]
    broker = _broker(client)  # type: ignore[arg-type]
    report = broker.submit(make_signal(qty=10.0, qty_unit="quote", client_order_id="leak"), make_bar(close=100.0))
    assert report.status == "exchange_rejected"
    assert isinstance(report.reason, str)
    assert secret not in report.reason
    assert secret not in " ".join(str(row["reason"]) for row in broker.store.fills())
    assert "[redacted]" in report.reason


def test_balance_error_redacts_the_secret() -> None:
    secret = "SECRETVALUE0123456789abcd"

    class Raw:
        def fetch_balance(self) -> dict:
            raise RuntimeError(f"bad {secret}")

    client = TradeOnlyClient(Raw(), secrets=(secret,))
    with pytest.raises(RuntimeError) as exc:
        _broker(client)  # type: ignore[arg-type]
    assert secret not in str(exc.value)


def test_live_book_is_separate_and_status_is_read_only(tmp_path: Path) -> None:
    paper_path = tmp_path / "paper.sqlite"
    paper = PaperBroker(TRIAL, store_path=paper_path)
    paper.store.close()
    before = paper_path.read_bytes()
    live_path = tmp_path / "live.sqlite"
    fake = FakeKraken()
    broker = _broker(fake, store_path=live_path)
    broker.submit(make_signal(qty=10.0, qty_unit="quote", client_order_id="book"), make_bar(close=100.0))
    broker.store.close()
    assert paper_path.read_bytes() == before
    snap = build_status(live_path, now=make_bar().ts)
    assert snap["overview"]["mode"] == "live"
    assert snap["read_only"] is True
    assert "KEY" not in json.dumps(snap)
    assert snap["fills"][0]["status"] == "filled"
    assert snap["fills"][0]["rejected_by_gate"] is False


def test_latest_bar_only_sends_the_current_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_network(monkeypatch)
    market = StaticMarketMetadata(MarketConstraints(min_qty=1e-8, amount_step=1e-8, last_price=20.0))
    fake = FakeKraken(average=20.0)
    broker = _broker(fake, market=market)
    report = run_live_latest(bars_from_closes(GOLDEN_CROSS), _short_sma(), broker)
    assert report is not None
    assert report.status == "filled"
    assert len(_orders(fake)) == 1
    assert _orders(fake)[0][2] == "market"
    assert _orders(fake)[0][3] == "buy"
    assert _orders(fake)[0][4] == pytest.approx(2000 * 0.005 / 20.0)

    historical = FakeKraken(average=20.0)
    quiet = _broker(historical, market=market)
    assert run_live_latest(bars_from_closes(DEATH_CROSS), _short_sma(), quiet) is None
    assert _orders(historical) == []


def test_halted_long_flattens_on_the_latest_bar_only() -> None:
    market = StaticMarketMetadata(MarketConstraints(last_price=20.0, min_qty=1e-8, amount_step=1e-8))
    fake = FakeKraken(usd=1990.0, btc=0.5, average=20.0)
    broker = _broker(fake, market=market)
    broker.risk.halted = True
    broker.risk.halt_reason = "max_drawdown"
    report = run_live_latest(bars_from_closes(GOLDEN_CROSS), _short_sma(), broker)
    assert report is not None
    assert report.status == "filled"
    assert len(_orders(fake)) == 1
    assert _orders(fake)[0][3] == "sell"


def test_synced_long_does_not_open_again_on_the_same_cross() -> None:
    market = StaticMarketMetadata(MarketConstraints(last_price=20.0, min_qty=1e-8, amount_step=1e-8))
    fake = FakeKraken(usd=1990.0, btc=0.5, average=20.0)
    broker = _broker(fake, market=market)
    assert broker.position_qty("BTC/USD") == pytest.approx(0.5)
    assert run_live_latest(bars_from_closes(GOLDEN_CROSS), _short_sma(), broker) is None
    assert _orders(fake) == []


def test_non_default_strategies_cannot_run_live() -> None:
    from quanttrading.strategy import build_strategy

    for strategy_id in (
        "mean_reversion_v1",
        "sma_cross_15m_v1",
        "sma_cross_5m_v1",
        "trend_breakout_v1",
        "range_reversion_v2",
    ):
        fake = FakeKraken()
        broker = _broker(fake)
        with pytest.raises(ValueError, match="sma_cross_v2"):
            run_live_latest(bars_from_closes(GOLDEN_CROSS), build_strategy(strategy_id), broker)
        assert _orders(fake) == []


def test_userref_is_stable_and_parse_balance_prefers_unified_codes() -> None:
    assert kraken_userref("ok-1") == kraken_userref("ok-1")
    assert kraken_userref("ok-1") != kraken_userref("ok-2")
    quote, base = parse_spot_balance(
        {"USD": {"total": 10}, "ZUSD": {"total": 999}, "BTC": {"total": 0.2}, "XXBT": {"total": 9}},
        "BTC/USD",
    )
    assert quote == 10
    assert base == 0.2
    quote, base = parse_spot_balance({"total": {"ZUSD": 4.0, "XXBT": 0.1}}, "BTC/USD")
    assert quote == 4.0
    assert base == 0.1


def test_credentials_reject_placeholders_without_echoing_them(tmp_path: Path) -> None:
    assert looks_like_placeholder("")
    assert looks_like_placeholder("changeme")
    assert looks_like_placeholder("placeholder-key-but-SECRETVALUE0123456789")
    assert not looks_like_placeholder(KEY)
    with pytest.raises(KrakenCredentialsError) as exc:
        load_kraken_credentials(
            {"KRAKEN_API_KEY": "changeme", "KRAKEN_API_SECRET": "placeholder-SECRETVALUE0123456789abcd"},
            env_file=None,
        )
    assert "SECRETVALUE" not in str(exc.value)
    assert "changeme" not in str(exc.value)
    assert "KRAKEN_API_KEY" in str(exc.value)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KRAKEN_API_KEY=fromfileKey0123456789abcd\nKRAKEN_API_SECRET=fromfileSecret0123456789abcd\n",
        encoding="utf-8",
    )
    loaded = load_kraken_credentials({}, env_file=env_file)
    assert loaded == ("fromfileKey0123456789abcd", "fromfileSecret0123456789abcd")
    overridden = load_kraken_credentials(
        {"KRAKEN_API_KEY": KEY, "KRAKEN_API_SECRET": SECRET},
        env_file=env_file,
    )
    assert overridden == (KEY, SECRET)


def test_kraken_factory_wraps_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_network(monkeypatch)

    class FakeExchange:
        def __init__(self, config: dict) -> None:
            self.config = config

        def withdraw(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("withdraw should be blocked before the raw client")

    import ccxt

    monkeypatch.setattr(ccxt, "kraken", FakeExchange)
    client = kraken_trade_client(KEY, SECRET)
    assert isinstance(client, TradeOnlyClient)
    assert client._raw.config["apiKey"] == KEY  # type: ignore[attr-defined]
    assert client._raw.config["enableRateLimit"] is True  # type: ignore[attr-defined]
    assert "withdraw" not in client._raw.config  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="withdraw"):
        client.withdraw("USD", 1)  # type: ignore[attr-defined]


def test_secrets_are_gitignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignore
    assert ".env.*" in ignore
    assert "!.env.example" in ignore
    assert "state/" in ignore
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "KRAKEN_API_KEY=" in example
    assert "KRAKEN_API_SECRET=" in example
    for line in example.splitlines():
        if line.startswith("KRAKEN_API_KEY=") or line.startswith("KRAKEN_API_SECRET="):
            assert line.split("=", 1)[1].strip() == ""
    assert subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT).returncode != 0
    assert subprocess.run(["git", "check-ignore", "-q", "state/live.sqlite"], cwd=ROOT).returncode == 0
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    assert ".env" not in tracked
    assert ".env.example" in tracked


def _patch_live_io(monkeypatch: pytest.MonkeyPatch, fake: FakeKraken) -> None:
    _no_network(monkeypatch)

    class Meta:
        def __init__(self, exchange_id: str = "kraken") -> None:
            assert exchange_id == "kraken"

        def for_symbol(self, symbol: str) -> MarketConstraints:
            return MarketConstraints(min_qty=1e-8, amount_step=1e-8, price_tick=0.01, last_price=20.0)

    monkeypatch.setattr("quanttrading.cli.CcxtPublicMarketMetadata", Meta)
    monkeypatch.setattr("quanttrading.cli.kraken_trade_client", lambda api_key, api_secret: fake)


def test_live_cli_refuses_missing_and_placeholder_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _no_network(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)

    def forbid_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("trade client built without keys")

    def forbid_meta(*args: object, **kwargs: object) -> None:
        raise AssertionError("market metadata loaded without keys")

    monkeypatch.setattr("quanttrading.cli.kraken_trade_client", forbid_client)
    monkeypatch.setattr("quanttrading.cli.CcxtPublicMarketMetadata", forbid_meta)
    csv_path = tmp_path / "bars.csv"
    save_csv(bars_from_closes(GOLDEN_CROSS), csv_path)
    state = tmp_path / "live.sqlite"
    missing = RUNNER.invoke(app, ["live", "--data", str(csv_path), "--state", str(state)])
    assert missing.exit_code != 0
    assert "KRAKEN_API_KEY" in missing.output
    assert "KRAKEN_API_SECRET" in missing.output
    assert not state.exists()

    monkeypatch.setenv("KRAKEN_API_KEY", "changeme")
    monkeypatch.setenv("KRAKEN_API_SECRET", "placeholder-SECRETVALUE0123456789abcd")
    placeholder = RUNNER.invoke(app, ["live", "--data", str(csv_path), "--state", str(state)])
    assert placeholder.exit_code != 0
    assert "SECRETVALUE" not in placeholder.output
    assert "placeholder" not in placeholder.output.lower() or "placeholders" in placeholder.output.lower()
    assert not state.exists()


def test_live_cli_submits_latest_bar_and_keeps_trial_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRAKEN_API_KEY", KEY)
    monkeypatch.setenv("KRAKEN_API_SECRET", SECRET)
    monkeypatch.setenv("PER_TRADE_PCT", "0.01")
    fake = FakeKraken(average=20.0)
    _patch_live_io(monkeypatch, fake)
    csv_path = tmp_path / "bars.csv"
    save_csv(bars_from_closes(GOLDEN_CROSS), csv_path)
    state = tmp_path / "live.sqlite"
    result = RUNNER.invoke(
        app,
        [
            "live",
            "--data",
            str(csv_path),
            "--state",
            str(state),
            "--strategy",
            "sma_cross_v2",
            "--fast",
            "2",
            "--slow",
            "4",
            "--vol-window",
            "3",
            "--min-vol",
            "0.0005",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "orders_sent=1" in result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["mode"] == "live"
    assert payload["strategy_id"] == LIVE_STRATEGY_ID
    assert payload["per_trade_pct"] == 0.005
    assert payload["orders_sent"] == 1
    assert payload["last_bar_only"] is True
    assert payload["n_filled"] == 1
    assert len(_orders(fake)) == 1
    assert _orders(fake)[0][4] == pytest.approx(0.5)
    assert not (tmp_path / "state" / "paper.sqlite").exists()

    second = FakeKraken(average=20.0)
    monkeypatch.setattr("quanttrading.cli.kraken_trade_client", lambda api_key, api_secret: second)
    again = RUNNER.invoke(
        app,
        [
            "live",
            "--data",
            str(csv_path),
            "--state",
            str(state),
            "--fast",
            "2",
            "--slow",
            "4",
            "--vol-window",
            "3",
            "--min-vol",
            "0.0005",
            "--per-trade-pct",
            "0.002",
        ],
    )
    assert again.exit_code == 0, again.output
    assert _orders(second) == []
    body = json.loads(again.output[again.output.index("{") :])
    assert body["orders_sent"] == 0
    assert body["last_decision"]["status"] == "duplicate"


def test_live_cli_refuses_other_strategies_sizes_and_venues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    csv_path = tmp_path / "bars.csv"
    save_csv(bars_from_closes([100.0] * 5), csv_path)
    state = tmp_path / "live.sqlite"
    for strategy_id in (
        "mean_reversion_v1",
        "sma_cross_15m_v1",
        "sma_cross_5m_v1",
        "trend_breakout_v1",
        "range_reversion_v2",
    ):
        other = RUNNER.invoke(
            app,
            ["live", "--strategy", strategy_id, "--data", str(csv_path), "--state", str(state)],
        )
        assert other.exit_code != 0
        assert "sma_cross_v2" in other.output

    wide = RUNNER.invoke(
        app,
        ["live", "--per-trade-pct", "0.01", "--data", str(csv_path), "--state", str(state)],
    )
    assert wide.exit_code != 0
    narrow = RUNNER.invoke(
        app,
        ["live", "--per-trade-pct", "0.001", "--data", str(csv_path), "--state", str(state)],
    )
    assert narrow.exit_code != 0

    both = RUNNER.invoke(app, ["live", "--data", str(csv_path), "--fetch", "--state", str(state)])
    assert both.exit_code != 0
    neither = RUNNER.invoke(app, ["live", "--state", str(state)])
    assert neither.exit_code != 0
    assert "bundled sample" in neither.output

    venue = RUNNER.invoke(
        app,
        ["live", "--exchange", "binance", "--data", str(csv_path), "--state", str(state)],
    )
    assert venue.exit_code != 0
    assert "Kraken" in venue.output

    paper_flag = RUNNER.invoke(app, ["paper", "--live"])
    assert paper_flag.exit_code != 0
    help_text = RUNNER.invoke(app, ["paper", "--help"])
    assert help_text.exit_code == 0
    assert "--live" not in help_text.output
    live_help = RUNNER.invoke(app, ["live", "--help"])
    assert "KRAKEN_API_KEY" in live_help.output
    assert "KRAKEN_API_SECRET" in live_help.output


def test_dry_run_and_paper_do_not_build_a_trade_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _no_network(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRAKEN_API_KEY", KEY)
    monkeypatch.setenv("KRAKEN_API_SECRET", SECRET)

    def forbid(*args: object, **kwargs: object) -> None:
        raise AssertionError("paper/dry-run constructed a Kraken trade client")

    monkeypatch.setattr("quanttrading.cli.kraken_trade_client", forbid)
    csv_path = tmp_path / "bars.csv"
    save_csv(bars_from_closes(GOLDEN_CROSS), csv_path)
    paper = RUNNER.invoke(
        app,
        ["paper", "--data", str(csv_path), "--state", str(tmp_path / "paper.sqlite"), "--strategy", "sma_cross_v2"],
    )
    assert paper.exit_code == 0, paper.output
    dry = RUNNER.invoke(
        app,
        [
            "dry-run",
            "--data",
            str(csv_path),
            "--state",
            str(tmp_path / "dry.sqlite"),
            "--strategy",
            "sma_cross_v2",
            "--per-trade-pct",
            "0.005",
        ],
    )
    assert dry.exit_code == 0, dry.output
    assert "orders_sent=0" in dry.output


def test_fetch_flag_uses_injected_public_bars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRAKEN_API_KEY", KEY)
    monkeypatch.setenv("KRAKEN_API_SECRET", SECRET)
    fake = FakeKraken(average=20.0)
    _patch_live_io(monkeypatch, fake)
    seen: dict = {}

    def fake_fetch(**kwargs: object) -> list:
        seen.update(kwargs)
        return bars_from_closes(GOLDEN_CROSS)

    monkeypatch.setattr("quanttrading.cli.fetch_ohlcv", fake_fetch)
    state = tmp_path / "live.sqlite"
    result = RUNNER.invoke(
        app,
        [
            "live",
            "--fetch",
            "--state",
            str(state),
            "--fast",
            "2",
            "--slow",
            "4",
            "--vol-window",
            "3",
            "--min-vol",
            "0.0005",
            "--per-trade-pct",
            "0.002",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["exchange_id"] == "kraken"
    assert seen["symbol"] == "BTC/USD"
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["per_trade_pct"] == 0.002
    assert payload["orders_sent"] == 1
    assert _orders(fake)[0][4] == pytest.approx(2000 * 0.002 / 20.0)
