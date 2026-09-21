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
from quanttrading.strategy import DEFAULT_STRATEGY_ID, SMACrossover, default_strategy
from tests.helpers import bars_from_closes, make_context

UTC = timezone.utc

# fast=2, slow=4 needs 5 closes before a crossover can fire.
WARMUP = [10.0, 10.0, 10.0, 10.0, 10.0]
GOLDEN_CROSS = WARMUP + [20.0]
# After the jump, stay elevated then drop so fast crosses back below slow.
DEATH_CROSS = GOLDEN_CROSS + [20.0, 20.0, 1.0]


def test_default_factory_matches_sma() -> None:
    strategy = default_strategy()
    assert isinstance(strategy, SMACrossover)
    assert strategy.strategy_id == DEFAULT_STRATEGY_ID
    assert strategy.fast == 10
    assert strategy.slow == 30


def test_warmup_emits_nothing() -> None:
    strategy = SMACrossover(fast=2, slow=4)
    ctx = make_context()
    signals = [strategy.on_bar(bar, ctx) for bar in bars_from_closes(WARMUP)]
    assert signals == [None] * len(WARMUP)


def test_golden_cross_emits_valid_v01_open() -> None:
    strategy = SMACrossover(fast=2, slow=4, max_slippage_bps=5.0)
    ctx = make_context(equity=2000.0, per_trade_pct=0.01)
    bars = bars_from_closes(GOLDEN_CROSS, symbol="BTC/USDT")
    signals = [strategy.on_bar(bar, ctx) for bar in bars]
    assert all(s is None for s in signals[:-1])
    signal = signals[-1]
    assert signal is not None

    assert signal.strategy_id == DEFAULT_STRATEGY_ID
    assert signal.ts == bars[-1].ts.astimezone(UTC)
    assert signal.symbol == "BTC/USDT"
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
    assert signal.client_order_id == "sma_cross_v1-BTCUSDT-20240101T050000Z-open-buy"
    assert signal.meta is not None
    assert signal.meta["fast"] == 2
    assert signal.meta["slow"] == 4
    assert signal.meta["close"] == 20.0
    assert signal.meta["fast_sma"] > signal.meta["slow_sma"]

    restored = Signal.model_validate_json(signal.model_dump_json())
    assert restored.strategy_id == signal.strategy_id
    assert restored.client_order_id == signal.client_order_id
    assert restored.model_dump(mode="json")["ts"].endswith("Z")


def test_death_cross_emits_close_while_long() -> None:
    strategy = SMACrossover(fast=2, slow=4)
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


def test_no_duplicate_open_while_already_long() -> None:
    strategy = SMACrossover(fast=2, slow=4)
    ctx = make_context(position_qty=0.1)
    signals = [strategy.on_bar(bar, ctx) for bar in bars_from_closes(GOLDEN_CROSS)]
    assert all(s is None for s in signals)


def test_halt_flattens_without_warmup() -> None:
    strategy = SMACrossover(fast=2, slow=4)
    bar = bars_from_closes([100.0])[0]
    ctx = make_context(position_qty=0.25, halted=True)
    signal = strategy.on_bar(bar, ctx)
    assert signal is not None
    assert signal.side == "sell"
    assert signal.intent == "close"
    assert signal.qty == 0.25
    assert signal.qty_unit == "base"


def test_halt_flat_is_silent() -> None:
    strategy = SMACrossover(fast=2, slow=4)
    bar = bars_from_closes([100.0])[0]
    assert strategy.on_bar(bar, make_context(halted=True, position_qty=0.0)) is None


def test_client_order_id_is_deterministic() -> None:
    a = SMACrossover(fast=2, slow=4)
    b = SMACrossover(fast=2, slow=4)
    bars = bars_from_closes(GOLDEN_CROSS)
    ctx = make_context()
    sig_a = [a.on_bar(bar, ctx) for bar in bars][-1]
    sig_b = [b.on_bar(bar, ctx) for bar in bars][-1]
    assert sig_a is not None and sig_b is not None
    assert sig_a.client_order_id == sig_b.client_order_id


def test_strategy_signal_submits_into_paper_broker() -> None:
    strategy = SMACrossover(fast=2, slow=4)
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
    strategy = SMACrossover(fast=2, slow=4)
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


def test_paper_cli_replays_default_strategy(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["paper", "--state", str(tmp_path / "paper.sqlite")])
    assert result.exit_code == 0, result.output
    assert DEFAULT_STRATEGY_ID in result.output
    assert '"n_fills"' in result.output


def test_run_bars_default_strategy_open_then_close() -> None:
    settings = Settings(paper_equity=2000, per_trade_pct=0.01)
    broker = PaperBroker(settings, store_path=":memory:")
    result = run_bars(bars_from_closes(DEATH_CROSS), SMACrossover(fast=2, slow=4), broker)
    assert result.metrics["n_fills"] == 2
    assert result.metrics["n_closed_trades"] == 1
    assert result.metrics["halted"] == 0
    assert broker.position_qty("BTC/USDT") == 0
    intents = [row["intent"] for row in broker.store.fills() if row["status"] == "filled"]
    assert intents == ["open", "close"]
