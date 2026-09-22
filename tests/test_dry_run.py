from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quanttrading.backtest.runner import run_bars
from quanttrading.cli import app
from quanttrading.config import Settings
from quanttrading.data.ohlcv import generate_sample_bars, save_csv
from quanttrading.execution.dry_run import DryRunBroker, build_would_be_order, summarize_dry_run
from quanttrading.execution.live import LiveBroker
from quanttrading.execution.market_meta import (
    DECIMAL_PLACES,
    SIGNIFICANT_DIGITS,
    TICK_SIZE,
    CcxtPublicMarketMetadata,
    MarketConstraints,
    StaticMarketMetadata,
    step_from_precision,
)
from quanttrading.execution.paper import PaperBroker
from quanttrading.execution.store import SQLiteStore
from quanttrading.strategy import build_strategy
from quanttrading.strategy.sma import SMACrossover
from tests.helpers import make_bar, make_signal

SETTINGS = Settings(
    paper_equity=2000,
    per_trade_pct=0.01,
    daily_dd_pct=0.03,
    total_dd_pct=0.20,
    max_slippage_bps=5.0,
)
TRIAL = Settings(
    paper_equity=2000,
    per_trade_pct=0.005,
    daily_dd_pct=0.03,
    total_dd_pct=0.20,
    max_slippage_bps=5.0,
)


class _ExplodingMetadata:
    def for_symbol(self, symbol: str) -> MarketConstraints:
        raise AssertionError(f"market metadata consulted for {symbol}")


def test_per_trade_notional_rejected_without_market_lookup() -> None:
    broker = DryRunBroker(TRIAL, store_path=":memory:", market=_ExplodingMetadata())
    bar = make_bar(close=100.0)
    report = broker.submit(make_signal(qty=15.0, qty_unit="quote", client_order_id="too-big"), bar)
    assert report.status == "rejected"
    assert report.reason == "per_trade_limit"
    assert broker.position_qty(bar.symbol) == 0.0
    assert broker.orders == []
    row = broker.store.fills()[0]
    assert row["status"] == "rejected"
    assert row["side"] == "buy"
    assert row["intent"] == "open"
    assert row["client_order_id"] == "too-big"


def test_daily_circuit_breaker_rejects_opens() -> None:
    broker = DryRunBroker(SETTINGS, store_path=":memory:")
    bar = make_bar(close=100.0)
    broker.mark_to_market(bar)
    broker.cash = 2000.0 - 60.0
    broker.mark_to_market(bar)
    report = broker.submit(make_signal(qty=20.0, client_order_id="daily"), bar)
    assert report.status == "rejected"
    assert report.reason == "daily_circuit_breaker"
    assert [row["status"] for row in broker.store.fills()] == ["rejected"]


def test_max_drawdown_halts_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "dry_run.sqlite"
    bar = make_bar(close=100.0)
    broker = DryRunBroker(SETTINGS, store_path=path)
    broker.mark_to_market(bar)
    broker.cash = 1600.0
    broker.mark_to_market(bar)
    assert broker.risk.halted
    assert broker.risk.halt_reason == "max_drawdown"
    first = broker.submit(make_signal(qty=10.0, client_order_id="halted-open"), bar)
    assert first.status == "rejected"
    assert first.reason == "max_drawdown"
    broker.store.close()

    restarted = DryRunBroker(SETTINGS, store_path=path)
    second = restarted.submit(make_signal(qty=10.0, client_order_id="halted-again"), bar)
    assert restarted.risk.halted
    assert second.status == "rejected"
    assert second.reason == "max_drawdown"


def test_min_qty_marks_size_invalid_and_does_not_open() -> None:
    market = StaticMarketMetadata(MarketConstraints(min_qty=1.0))
    broker = DryRunBroker(TRIAL, store_path=":memory:", market=market)
    bar = make_bar(close=100.0)
    report = broker.submit(make_signal(qty=10.0, qty_unit="quote", client_order_id="tiny"), bar)
    assert report.status == "size_invalid"
    assert report.reason == "below_min_qty"
    assert broker.position_qty(bar.symbol) == 0.0
    assert broker.orders == []
    row = broker.store.fills()[0]
    assert row["status"] == "size_invalid"
    assert row["order_type"] == "market"
    assert row["client_order_id"] == "tiny"


def test_dry_run_ok_records_would_be_order() -> None:
    market = StaticMarketMetadata(MarketConstraints(min_qty=0.01, amount_step=0.01, price_tick=0.5))
    broker = DryRunBroker(TRIAL, store_path=":memory:", market=market)
    bar = make_bar(close=100.0)
    report = broker.submit(
        make_signal(qty=10.0, qty_unit="quote", max_slippage_bps=0, client_order_id="ok-1"),
        bar,
    )
    assert report.status == "dry_run_ok"
    assert report.qty_base is not None and abs(report.qty_base - 0.1) < 1e-12
    assert report.fill_price == 100.0
    assert len(broker.orders) == 1
    order = broker.orders[0]
    assert order.symbol == "BTC/USD"
    assert order.side == "buy"
    assert order.order_type == "market"
    assert order.limit_price is None
    assert order.client_order_id == "ok-1"
    assert abs(order.qty - 0.1) < 1e-12
    row = broker.store.fills()[0]
    assert row["status"] == "dry_run_ok"
    assert row["side"] == "buy"
    assert row["intent"] == "open"
    summary = summarize_dry_run(broker, strategy_id="sma_cross_v2")
    assert summary["orders_sent"] == 0
    assert summary["n_signals"] == 1
    assert summary["n_would_send"] == 1
    assert summary["n_risk_rejected"] == 0
    assert summary["n_size_invalid"] == 0
    assert summary["sample_client_order_ids"] == ["ok-1"]
    assert summary["sample_orders"] == [
        {
            "client_order_id": "ok-1",
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": report.qty_base,
            "order_type": "market",
            "limit_price": None,
        }
    ]
    assert summary["paper_reconcile"] is None


def test_last_price_rechecks_min_cost_without_resizing() -> None:
    bar = make_bar(close=100.0)
    market = StaticMarketMetadata(MarketConstraints(min_cost=5.0, last_price=10.0))
    order, reason = build_would_be_order(
        make_signal(qty=10.0, qty_unit="quote", client_order_id="last"),
        bar,
        market.for_symbol("BTC/USD"),
    )
    assert reason == "below_min_cost_at_last"
    assert abs(order.qty - 0.1) < 1e-12
    assert order.reference_price == 100.0

    broker = DryRunBroker(TRIAL, store_path=":memory:", market=market)
    report = broker.submit(make_signal(qty=10.0, qty_unit="quote"), bar)
    assert report.status == "size_invalid"
    assert report.reason == "below_min_cost_at_last"
    assert broker.position_qty(bar.symbol) == 0.0


def test_limit_tick_rounds_and_min_cost_rejects() -> None:
    bar = make_bar(close=100.0, high=101.0, low=99.0)
    constraints = MarketConstraints(price_tick=0.1, min_cost=50.0)
    order, reason = build_would_be_order(
        make_signal(
            order_type="limit",
            limit_price=100.04,
            qty=10.0,
            qty_unit="quote",
            client_order_id="lim",
        ),
        bar,
        constraints,
    )
    assert reason == "below_min_cost"
    assert order.order_type == "limit"
    assert order.limit_price == pytest.approx(100.0)


def test_submit_does_not_use_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry-run unit test opened a network connection")

    monkeypatch.setattr(socket, "create_connection", explode)
    broker = DryRunBroker(
        TRIAL,
        store_path=":memory:",
        market=StaticMarketMetadata(MarketConstraints(min_qty=0.01)),
    )
    report = broker.submit(make_signal(qty=10.0, qty_unit="quote"), make_bar(close=100.0))
    assert report.status == "dry_run_ok"


def test_matches_paper_ids_sides_and_equity(tmp_path: Path) -> None:
    bars = generate_sample_bars(n=240)
    paper_path = tmp_path / "paper.sqlite"
    paper = PaperBroker(TRIAL, store_path=paper_path)
    dry = DryRunBroker(TRIAL, store_path=tmp_path / "dry.sqlite", market=StaticMarketMetadata())
    run_bars(bars, SMACrossover(max_slippage_bps=TRIAL.max_slippage_bps), paper)
    run_bars(bars, SMACrossover(max_slippage_bps=TRIAL.max_slippage_bps), dry)
    paper.store.close()

    summary = summarize_dry_run(dry, paper_state=paper_path, strategy_id="sma_cross_v2")
    assert summary["n_would_send"] >= 1
    assert summary["n_risk_rejected"] == 0
    assert summary["n_size_invalid"] == 0
    reconcile = summary["paper_reconcile"]
    assert isinstance(reconcile, dict)
    assert reconcile["n_mismatches"] == 0
    assert reconcile["side_counts"]["dry_run"] == reconcile["side_counts"]["paper"]
    assert reconcile["intent_counts"]["dry_run"] == reconcile["intent_counts"]["paper"]
    assert abs(paper.equity() - dry.equity()) < 1e-6
    snapshot = summary["equity_path"]
    assert isinstance(snapshot, dict)
    assert snapshot["n_points"] >= 1
    assert snapshot["start_equity"] == pytest.approx(2000.0, abs=1e-3)


def test_size_filter_is_reported_as_a_paper_mismatch(tmp_path: Path) -> None:
    bars = generate_sample_bars(n=240)
    paper_path = tmp_path / "paper.sqlite"
    paper = PaperBroker(TRIAL, store_path=paper_path)
    run_bars(bars, SMACrossover(), paper)
    paper.store.close()
    dry = DryRunBroker(
        TRIAL,
        store_path=":memory:",
        market=StaticMarketMetadata(MarketConstraints(min_qty=1_000_000)),
    )
    run_bars(bars, SMACrossover(), dry)
    summary = summarize_dry_run(dry, paper_state=paper_path)
    assert summary["n_would_send"] == 0
    assert summary["n_size_invalid"] >= 1
    reconcile = summary["paper_reconcile"]
    assert isinstance(reconcile, dict)
    assert reconcile["n_mismatches"] >= 1
    fields = {item["field"] for item in reconcile["mismatches"]}  # type: ignore[index]
    assert "client_order_id" in fields


def test_risk_reject_ids_match_paper(tmp_path: Path) -> None:
    bar = make_bar(close=100.0)
    signal = make_signal(qty=50.0, qty_unit="quote", client_order_id="too-big")
    paper_path = tmp_path / "paper.sqlite"
    paper = PaperBroker(SETTINGS, store_path=paper_path)
    dry = DryRunBroker(SETTINGS, store_path=":memory:")
    assert paper.submit(signal, bar).status == "rejected"
    assert dry.submit(signal, bar).status == "rejected"
    paper.store.close()
    summary = summarize_dry_run(dry, paper_state=paper_path)
    reconcile = summary["paper_reconcile"]
    assert isinstance(reconcile, dict)
    assert reconcile["n_mismatches"] == 0
    assert reconcile["n_dry_run_rejected"] == 1
    assert reconcile["n_paper_rejected"] == 1


def test_mean_reversion_remains_selectable() -> None:
    broker = DryRunBroker(TRIAL, store_path=":memory:")
    run_bars(generate_sample_bars(n=80), build_strategy("mean_reversion_v1"), broker)
    summary = summarize_dry_run(broker, strategy_id="mean_reversion_v1")
    assert summary["strategy_id"] == "mean_reversion_v1"
    assert summary["orders_sent"] == 0
    assert summary["n_risk_rejected"] == 0


def test_live_broker_still_cannot_place_orders() -> None:
    live = LiveBroker()
    with pytest.raises(RuntimeError, match="No exchange API keys"):
        live.submit(make_signal(), make_bar())
    with pytest.raises(RuntimeError, match="disabled"):
        live.equity()


def test_public_metadata_parser_uses_no_private_calls() -> None:
    class FakeExchange:
        def __init__(self) -> None:
            self.apiKey = ""
            self.secret = ""
            self.password = ""
            self.precisionMode = TICK_SIZE
            self.calls: list[object] = []

        def load_markets(self) -> dict[str, object]:
            self.calls.append("load_markets")
            return {}

        def market(self, symbol: str) -> dict[str, object]:
            self.calls.append(("market", symbol))
            return {
                "limits": {"amount": {"min": 0.5}, "cost": {"min": 1}},
                "precision": {"amount": 0.1, "price": 0.01},
            }

        def fetch_ticker(self, symbol: str) -> dict[str, float]:
            self.calls.append(("fetch_ticker", symbol))
            return {"last": 100.0}

        def create_order(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("private create_order")

        def fetch_balance(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("private fetch_balance")

    fake = FakeExchange()
    meta = CcxtPublicMarketMetadata("kraken", exchange=fake)
    constraints = meta.for_symbol("BTC/USD")
    assert constraints.min_qty == 0.5
    assert constraints.min_cost == 1.0
    assert constraints.amount_step == 0.1
    assert constraints.price_tick == 0.01
    assert constraints.last_price == 100.0
    assert "create_order" not in fake.calls
    assert fake.calls == ["load_markets", ("market", "BTC/USD"), ("fetch_ticker", "BTC/USD")]

    broker = DryRunBroker(TRIAL, store_path=":memory:", market=meta)
    report = broker.submit(make_signal(qty=10.0, qty_unit="quote", client_order_id="venue"), make_bar(close=100.0))
    assert report.status == "size_invalid"
    assert report.reason == "below_min_qty"
    assert all(call != "create_order" for call in fake.calls)


def test_public_metadata_refuses_api_keys() -> None:
    class Keyed:
        apiKey = "secret"
        secret = "also-secret"
        password = ""

    with pytest.raises(RuntimeError, match="refuses exchange credentials"):
        CcxtPublicMarketMetadata("kraken", exchange=Keyed())


def test_ticker_failure_leaves_last_price_empty() -> None:
    class FakeExchange:
        apiKey = ""
        secret = ""
        password = ""
        precisionMode = DECIMAL_PLACES

        def load_markets(self) -> dict[str, object]:
            return {}

        def market(self, symbol: str) -> dict[str, object]:
            return {
                "limits": {"amount": {"min": None}, "cost": {"min": None}},
                "precision": {"amount": 4, "price": 2},
            }

        def fetch_ticker(self, symbol: str) -> dict[str, float]:
            raise OSError("public ticker down")

    constraints = CcxtPublicMarketMetadata("kraken", exchange=FakeExchange()).for_symbol("BTC/USD")
    assert constraints.last_price is None
    assert constraints.amount_step == pytest.approx(1e-4)
    assert constraints.price_tick == pytest.approx(1e-2)


def test_step_from_precision_modes() -> None:
    assert step_from_precision(0.0001, TICK_SIZE) == pytest.approx(0.0001)
    assert step_from_precision(8, DECIMAL_PLACES) == pytest.approx(1e-8)
    assert step_from_precision(5, SIGNIFICANT_DIGITS) == 0.0
    assert step_from_precision(None, TICK_SIZE) == 0.0


def test_precision_constants_match_ccxt() -> None:
    import ccxt

    assert ccxt.DECIMAL_PLACES == DECIMAL_PLACES
    assert ccxt.SIGNIFICANT_DIGITS == SIGNIFICANT_DIGITS
    assert ccxt.TICK_SIZE == TICK_SIZE


def test_store_migrates_order_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY,
            client_order_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            intent TEXT NOT NULL,
            qty_base REAL NOT NULL,
            price REAL NOT NULL,
            notional REAL NOT NULL,
            slippage_bps REAL NOT NULL,
            status TEXT NOT NULL,
            reason TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    store = SQLiteStore(path)
    store.record_event(
        client_order_id="mig",
        ts="2024-06-01T12:00:00Z",
        symbol="BTC/USD",
        side="buy",
        intent="open",
        qty_base=0.1,
        price=100.0,
        notional=10.0,
        slippage_bps=0.0,
        status="dry_run_ok",
        reason=None,
        order_type="market",
        limit_price=None,
    )
    store.commit()
    row = store.fills()[0]
    assert row["order_type"] == "market"
    assert row["status"] == "dry_run_ok"
    store.close()


def test_cli_dry_run_default_trial_and_reconcile(tmp_path: Path) -> None:
    csv_path = tmp_path / "bars.csv"
    save_csv(generate_sample_bars(n=240), csv_path)
    paper_path = tmp_path / "paper.sqlite"
    dry_path = tmp_path / "dry.sqlite"
    runner = CliRunner()
    paper = runner.invoke(
        app,
        [
            "paper",
            "--data",
            str(csv_path),
            "--state",
            str(paper_path),
            "--strategy",
            "sma_cross_v2",
        ],
    )
    assert paper.exit_code == 0, paper.output
    result = runner.invoke(
        app,
        [
            "dry-run",
            "--data",
            str(csv_path),
            "--state",
            str(dry_path),
            "--paper-state",
            str(paper_path),
            "--strategy",
            "sma_cross_v2",
            "--per-trade-pct",
            "0.01",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "orders_sent=0" in result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["mode"] == "dry_run"
    assert payload["strategy_id"] == "sma_cross_v2"
    assert payload["per_trade_pct"] == 0.01
    assert payload["orders_sent"] == 0
    assert payload["n_would_send"] >= 1
    assert payload["paper_reconcile"]["n_mismatches"] == 0
    assert payload["sample_client_order_ids"]
    assert "equity_path" in payload

    default_trial = runner.invoke(
        app,
        ["dry-run", "--data", str(csv_path), "--state", str(tmp_path / "trial.sqlite")],
    )
    assert default_trial.exit_code == 0, default_trial.output
    trial_payload = json.loads(default_trial.output[default_trial.output.index("{") :])
    assert trial_payload["strategy_id"] == "sma_cross_v2"
    assert trial_payload["per_trade_pct"] == 0.005
    assert trial_payload["orders_sent"] == 0
