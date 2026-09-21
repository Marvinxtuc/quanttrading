from __future__ import annotations

from datetime import timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quanttrading.backtest.runner import run_bars
from quanttrading.cli import app
from quanttrading.config import Settings
from quanttrading.execution.base import ExecutionReport
from quanttrading.execution.paper import PaperBroker
from quanttrading.market import Bar
from quanttrading.signals import Signal
from quanttrading.strategy import (
    DEFAULT_MIN_VOL,
    DEFAULT_STRATEGY_ID,
    DEFAULT_VOL_WINDOW,
    SMACrossover,
    default_strategy,
    realized_vol,
)
from tests.helpers import bars_from_closes, make_context

UTC = timezone.utc

# fast=2, slow=4 needs 5 closes before a crossover can fire.
WARMUP = [10.0, 10.0, 10.0, 10.0, 10.0]
GOLDEN_CROSS = WARMUP + [20.0]
GOLDEN_CROSS_LOW_VOL = WARMUP + [10.001]
# After the jump, stay elevated then drop so fast crosses back below slow.
DEATH_CROSS = GOLDEN_CROSS + [20.0, 20.0, 1.0]


def short_sma(**overrides: object) -> SMACrossover:
    """Unit-test SMA (fast=2, slow=4) with a vol window that fits short bars."""
    params: dict = dict(fast=2, slow=4, vol_window=3, min_vol=0.0005)
    params.update(overrides)
    return SMACrossover(**params)


def test_default_factory_matches_sma() -> None:
    strategy = default_strategy()
    assert isinstance(strategy, SMACrossover)
    assert strategy.strategy_id == DEFAULT_STRATEGY_ID
    assert strategy.strategy_id == "sma_cross_v2"
    assert strategy.fast == 10
    assert strategy.slow == 30
    assert strategy.vol_window == DEFAULT_VOL_WINDOW
    assert strategy.min_vol == DEFAULT_MIN_VOL


def test_venue_defaults_are_kraken_btcusd() -> None:
    settings = Settings()
    assert settings.exchange_id == "kraken"
    assert settings.default_symbol == "BTC/USD"
    assert settings.per_trade_pct == 0.01
    assert settings.daily_dd_pct == 0.03
    assert settings.total_dd_pct == 0.20


def test_warmup_emits_nothing() -> None:
    strategy = short_sma()
    ctx = make_context()
    signals = [strategy.on_bar(bar, ctx) for bar in bars_from_closes(WARMUP)]
    assert signals == [None] * len(WARMUP)


def test_golden_cross_low_vol_skips_open() -> None:
    strategy = short_sma()
    ctx = make_context()
    bars = bars_from_closes(GOLDEN_CROSS_LOW_VOL)
    signals = [strategy.on_bar(bar, ctx) for bar in bars]
    assert all(s is None for s in signals)
    vol = realized_vol(GOLDEN_CROSS_LOW_VOL, 3)
    assert vol is not None
    assert vol < strategy.min_vol


def test_golden_cross_sufficient_vol_emits_valid_v01_open() -> None:
    strategy = short_sma(max_slippage_bps=5.0)
    ctx = make_context(equity=2000.0, per_trade_pct=0.01)
    bars = bars_from_closes(GOLDEN_CROSS, symbol="BTC/USD")
    signals = [strategy.on_bar(bar, ctx) for bar in bars]
    assert all(s is None for s in signals[:-1])
    signal = signals[-1]
    assert signal is not None

    vol = realized_vol(GOLDEN_CROSS, 3)
    assert vol is not None and vol >= strategy.min_vol

    assert signal.strategy_id == DEFAULT_STRATEGY_ID
    assert signal.ts == bars[-1].ts.astimezone(UTC)
    assert signal.symbol == "BTC/USD"
    assert signal.side == "buy"
    assert signal.intent == "open"
    assert signal.qty == 20.0
    assert signal.qty_unit == "quote"
    assert signal.order_type == "market"
    assert signal.limit_price is None
    assert signal.strength == 1.0
    assert signal.max_slippage_bps == 5.0
    assert signal.risk.per_trade_pct == 0.0
    assert signal.risk.daily_dd_pct == 0.0
    assert signal.risk.total_dd_pct == 0.0
    assert signal.client_order_id == "sma_cross_v2-BTCUSD-20240101T050000Z-open-buy"
    assert signal.meta is not None
    assert signal.meta["fast"] == 2
    assert signal.meta["slow"] == 4
    assert signal.meta["vol_window"] == 3
    assert signal.meta["min_vol"] == 0.0005
    assert signal.meta["vol_filter"] == "min"
    assert signal.meta["close"] == 20.0
    assert signal.meta["fast_sma"] > signal.meta["slow_sma"]
    assert signal.meta["vol_ok"] is True
    assert signal.meta["realized_vol"] == pytest.approx(vol)

    restored = Signal.model_validate_json(signal.model_dump_json())
    assert restored.strategy_id == signal.strategy_id
    assert restored.client_order_id == signal.client_order_id
    assert restored.model_dump(mode="json")["ts"].endswith("Z")
    assert restored.meta is not None
    assert restored.meta["vol_window"] == 3
    assert restored.meta["min_vol"] == 0.0005


def test_death_cross_emits_close_while_long() -> None:
    strategy = short_sma()
    bars = bars_from_closes(DEATH_CROSS)
    ctx = make_context(position_qty=0.0)
    open_signal = None
    close_signal = None
    for bar in bars:
        signal = strategy.on_bar(bar, ctx)
        if signal is None:
            continue
        if signal.intent == "open":
            open_signal = signal
            ctx = make_context(position_qty=0.05, equity=2000.0)
        elif signal.intent == "close":
            close_signal = signal
    assert open_signal is not None
    assert close_signal is not None
    assert close_signal.side == "sell"
    assert close_signal.intent == "close"
    assert close_signal.qty_unit == "base"
    assert close_signal.qty == 0.05
    assert close_signal.order_type == "market"
    assert close_signal.client_order_id.endswith("-close-sell")
    assert close_signal.meta is not None
    assert close_signal.meta["vol_window"] == 3
    assert close_signal.meta["min_vol"] == 0.0005


def test_death_cross_close_ignores_vol_filter() -> None:
    """Closes stay unrestricted so exits still work in a low-vol (or filtered) regime."""
    strategy = short_sma(min_vol=10.0)
    bars = bars_from_closes(DEATH_CROSS)
    ctx = make_context(position_qty=0.05)
    close_signal = None
    for bar in bars:
        signal = strategy.on_bar(bar, ctx)
        if signal is None:
            continue
        assert signal.intent != "open"
        if signal.intent == "close":
            close_signal = signal
    assert close_signal is not None
    assert close_signal.side == "sell"
    assert close_signal.qty == 0.05
    assert close_signal.qty_unit == "base"


def test_no_duplicate_open_while_already_long() -> None:
    strategy = short_sma()
    ctx = make_context(position_qty=0.1)
    signals = [strategy.on_bar(bar, ctx) for bar in bars_from_closes(GOLDEN_CROSS)]
    assert all(s is None for s in signals)


def test_halt_flattens_without_warmup() -> None:
    strategy = short_sma()
    bar = bars_from_closes([100.0])[0]
    ctx = make_context(position_qty=0.25, halted=True)
    signal = strategy.on_bar(bar, ctx)
    assert signal is not None
    assert signal.side == "sell"
    assert signal.intent == "close"
    assert signal.qty == 0.25
    assert signal.qty_unit == "base"


def test_halt_flat_is_silent() -> None:
    strategy = short_sma()
    bar = bars_from_closes([100.0])[0]
    assert strategy.on_bar(bar, make_context(halted=True, position_qty=0.0)) is None


def test_client_order_id_is_deterministic() -> None:
    a = short_sma()
    b = short_sma()
    bars = bars_from_closes(GOLDEN_CROSS)
    ctx = make_context()
    sig_a = [a.on_bar(bar, ctx) for bar in bars][-1]
    sig_b = [b.on_bar(bar, ctx) for bar in bars][-1]
    assert sig_a is not None and sig_b is not None
    assert sig_a.client_order_id == sig_b.client_order_id


def test_strategy_signal_submits_into_paper_broker() -> None:
    strategy = short_sma()
    settings = Settings(paper_equity=2000, per_trade_pct=0.01, max_slippage_bps=5.0)
    broker = PaperBroker(settings, store_path=":memory:")
    bars = bars_from_closes(GOLDEN_CROSS)
    submitted: list[Signal] = []
    reports: list[ExecutionReport] = []

    for bar in bars:
        broker.mark_to_market(bar)
        ctx = make_context(
            equity=broker.equity(),
            position_qty=broker.position_qty(bar.symbol),
            per_trade_pct=broker.risk.limits.per_trade_pct,
            per_trade_util=broker.risk.utilization(broker.equity()).per_trade_pct,
            daily_dd_util=broker.risk.utilization(broker.equity()).daily_dd_pct,
            total_dd_util=broker.risk.utilization(broker.equity()).total_dd_pct,
            halted=broker.risk.halted,
        )
        signal = strategy.on_bar(bar, ctx)
        if signal is None:
            continue
        submitted.append(signal)
        reports.append(broker.submit(signal, bar))

    assert len(submitted) == 1
    assert submitted[0].intent == "open"
    assert submitted[0].side == "buy"
    assert reports[0].status == "filled"
    assert reports[0].client_order_id == submitted[0].client_order_id
    assert broker.position_qty(bars[-1].symbol) > 0
    fills = [row for row in broker.store.fills() if row["status"] == "filled"]
    assert len(fills) == 1
    assert fills[0]["intent"] == "open"
    assert fills[0]["side"] == "buy"


class _RecordingBackend:
    """Minimal ExecutionBackend stand-in so tests pin the Signal → submit contract."""

    def __init__(self) -> None:
        self.submitted: list[tuple[Signal, Bar]] = []

    def submit(self, signal: Signal, bar: Bar) -> ExecutionReport:
        self.submitted.append((signal, bar))
        return ExecutionReport(status="accepted", client_order_id=signal.client_order_id, equity=2000.0)

    def mark_to_market(self, bar: Bar) -> float:
        return 2000.0

    def equity(self) -> float:
        return 2000.0

    def position_qty(self, symbol: str) -> float:
        return 0.0


def test_strategy_calls_shared_submit_interface() -> None:
    strategy = short_sma()
    backend = _RecordingBackend()
    ctx = make_context()
    bars = bars_from_closes(GOLDEN_CROSS)
    for bar in bars:
        signal = strategy.on_bar(bar, ctx)
        if signal is not None:
            backend.submit(signal, bar)
    assert len(backend.submitted) == 1
    signal, bar = backend.submitted[0]
    assert signal.symbol == bar.symbol
    assert signal.intent == "open"
    Signal.model_validate(signal.model_dump())


def test_invalid_windows_rejected() -> None:
    with pytest.raises(ValueError, match="fast"):
        SMACrossover(fast=5, slow=5)
    with pytest.raises(ValueError, match="slippage"):
        SMACrossover(fast=2, slow=4, max_slippage_bps=-1)
    with pytest.raises(ValueError, match="vol_window"):
        SMACrossover(fast=2, slow=4, vol_window=1)
    with pytest.raises(ValueError, match="min_vol"):
        SMACrossover(fast=2, slow=4, min_vol=-0.1)


def test_realized_vol_none_until_warmup() -> None:
    assert realized_vol([1.0, 1.1], window=3) is None
    assert realized_vol([10.0, 10.0, 10.0, 20.0], window=3) is not None


def test_paper_cli_replays_default_strategy(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["paper", "--state", str(tmp_path / "paper.sqlite")])
    assert result.exit_code == 0, result.output
    assert DEFAULT_STRATEGY_ID in result.output
    assert '"n_fills"' in result.output


def test_run_bars_default_strategy_open_then_close() -> None:
    settings = Settings(paper_equity=2000, per_trade_pct=0.01)
    broker = PaperBroker(settings, store_path=":memory:")
    result = run_bars(bars_from_closes(DEATH_CROSS), short_sma(), broker)
    assert result.metrics["n_fills"] == 2
    assert result.metrics["n_closed_trades"] == 1
    assert result.metrics["halted"] == 0
    assert broker.position_qty("BTC/USD") == 0
    intents = [row["intent"] for row in broker.store.fills() if row["status"] == "filled"]
    assert intents == ["open", "close"]
