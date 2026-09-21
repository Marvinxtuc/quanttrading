from __future__ import annotations

from datetime import timezone
from pathlib import Path
from statistics import fmean, pstdev

import pytest
from typer.testing import CliRunner

from quanttrading.backtest.runner import run_bars
from quanttrading.cli import app
from quanttrading.config import Settings
from quanttrading.execution.paper import PaperBroker
from quanttrading.signals import Signal
from quanttrading.strategy import (
    DEFAULT_ENTRY_Z,
    DEFAULT_EXIT_Z,
    DEFAULT_LOOKBACK,
    DEFAULT_STRATEGY_ID,
    MEAN_REVERSION_STRATEGY_ID,
    MeanReversion,
    build_strategy,
    close_zscore,
    mean_reversion_strategy,
)
from tests.helpers import bars_from_closes, make_context

UTC = timezone.utc

# lookback=5. Last close is 2 std below the window mean → open.
DIP = [100.0, 100.0, 100.0, 100.0, 90.0]
# Still below the mean after the dip → hold while long.
HOLD = DIP + [91.0]
# Back through the mean → flatten.
REVERT = HOLD + [100.0]
# Mild variation around the mean; last z is about -0.7, inside ±1.5.
NOT_DISPLACED = [10.0, 12.0, 8.0, 11.0, 9.0]
# Upside spike. Long-only: do not open.
SPIKE = [100.0, 100.0, 100.0, 100.0, 120.0]


def _z(closes: list[float], lookback: int) -> float:
    window = closes[-lookback:]
    sigma = pstdev(window)
    assert sigma > 0
    return (window[-1] - fmean(window)) / sigma


def short_mr(**overrides: object) -> MeanReversion:
    params: dict = dict(lookback=5, entry_z=1.0, exit_z=0.0)
    params.update(overrides)
    return MeanReversion(**params)


def test_factory_defaults() -> None:
    strategy = mean_reversion_strategy()
    assert isinstance(strategy, MeanReversion)
    assert strategy.strategy_id == MEAN_REVERSION_STRATEGY_ID
    assert strategy.strategy_id == "mean_reversion_v1"
    assert strategy.lookback == DEFAULT_LOOKBACK == 20
    assert strategy.entry_z == DEFAULT_ENTRY_Z == 1.5
    assert strategy.exit_z == DEFAULT_EXIT_Z == 0.0


def test_build_strategy_keeps_sma_default() -> None:
    strategy = build_strategy()
    assert strategy.strategy_id == DEFAULT_STRATEGY_ID
    assert strategy.strategy_id == "sma_cross_v2"
    other = build_strategy("mean_reversion_v1", lookback=12, entry_z=2.0, exit_z=-0.25)
    assert isinstance(other, MeanReversion)
    assert other.lookback == 12
    assert other.entry_z == 2.0
    assert other.exit_z == -0.25


def test_unknown_strategy_rejected() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategy("sma_cross")


def test_close_zscore_warmup_and_flat_window() -> None:
    assert close_zscore([100.0, 90.0], lookback=5) is None
    stats = close_zscore([100.0, 100.0, 100.0, 100.0, 100.0], lookback=5)
    assert stats is not None
    mean, sigma, z = stats
    assert mean == 100.0
    assert sigma == 0.0
    assert z == 0.0


def test_warmup_emits_nothing() -> None:
    strategy = short_mr()
    ctx = make_context()
    signals = [strategy.on_bar(bar, ctx) for bar in bars_from_closes(DIP[:-1])]
    assert signals == [None] * (len(DIP) - 1)


def test_below_mean_entry_emits_valid_v01_open() -> None:
    strategy = short_mr(max_slippage_bps=5.0)
    ctx = make_context(equity=2000.0, per_trade_pct=0.01)
    bars = bars_from_closes(DIP, symbol="BTC/USD")
    signals = [strategy.on_bar(bar, ctx) for bar in bars]
    assert all(s is None for s in signals[:-1])
    signal = signals[-1]
    assert signal is not None

    z = _z(DIP, 5)
    assert z <= -strategy.entry_z

    assert signal.strategy_id == "mean_reversion_v1"
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
    assert signal.client_order_id == "mean_reversion_v1-BTCUSD-20240101T040000Z-open-buy"
    assert signal.meta is not None
    assert signal.meta["lookback"] == 5
    assert signal.meta["entry_z"] == 1.0
    assert signal.meta["exit_z"] == 0.0
    assert signal.meta["threshold"] == -1.0
    assert signal.meta["close"] == 90.0
    assert signal.meta["z"] == pytest.approx(z)
    assert signal.meta["z"] <= signal.meta["threshold"]
    assert signal.meta["mean"] == pytest.approx(fmean(DIP))
    assert signal.meta["std"] == pytest.approx(pstdev(DIP))

    restored = Signal.model_validate_json(signal.model_dump_json())
    assert restored.strategy_id == signal.strategy_id
    assert restored.side == "buy"
    assert restored.intent == "open"
    assert restored.qty == 20.0
    assert restored.qty_unit == "quote"
    assert restored.order_type == "market"
    assert restored.client_order_id == signal.client_order_id
    assert restored.model_dump(mode="json")["ts"].endswith("Z")
    assert restored.meta is not None
    assert restored.meta["lookback"] == 5
    assert restored.meta["z"] == pytest.approx(z)
    assert restored.meta["threshold"] == -1.0


def test_reversion_to_mean_emits_close() -> None:
    strategy = short_mr()
    bars = bars_from_closes(REVERT)
    ctx = make_context(position_qty=0.0)
    open_signal = None
    close_signal = None
    for i, bar in enumerate(bars):
        signal = strategy.on_bar(bar, ctx)
        if i == len(DIP):
            assert signal is None
            held_z = _z(HOLD, 5)
            assert held_z < strategy.exit_z
        if signal is None:
            continue
        if signal.intent == "open":
            open_signal = signal
            ctx = make_context(position_qty=0.05, equity=2000.0)
        elif signal.intent == "close":
            close_signal = signal
    assert open_signal is not None
    assert close_signal is not None
    z = _z(REVERT, 5)
    assert z >= strategy.exit_z
    assert close_signal.side == "sell"
    assert close_signal.intent == "close"
    assert close_signal.qty_unit == "base"
    assert close_signal.qty == 0.05
    assert close_signal.order_type == "market"
    assert close_signal.limit_price is None
    assert close_signal.client_order_id == "mean_reversion_v1-BTCUSD-20240101T060000Z-close-sell"
    assert close_signal.meta is not None
    assert close_signal.meta["lookback"] == 5
    assert close_signal.meta["z"] == pytest.approx(z)
    assert close_signal.meta["z"] >= close_signal.meta["exit_z"]
    assert close_signal.meta["threshold"] == -1.0

    restored = Signal.model_validate_json(close_signal.model_dump_json())
    assert restored.intent == "close"
    assert restored.side == "sell"
    assert restored.qty_unit == "base"


def test_no_open_when_flat_and_not_displaced() -> None:
    ctx = make_context(position_qty=0.0)

    flat_strategy = short_mr(entry_z=1.5)
    flat = [flat_strategy.on_bar(bar, ctx) for bar in bars_from_closes([100.0] * 8)]
    assert flat == [None] * 8

    mild_strategy = short_mr(entry_z=1.5)
    mild = [mild_strategy.on_bar(bar, ctx) for bar in bars_from_closes(NOT_DISPLACED)]
    assert mild == [None] * len(NOT_DISPLACED)
    z = _z(NOT_DISPLACED, 5)
    assert z > -mild_strategy.entry_z

    spike = short_mr()
    signals = [spike.on_bar(bar, ctx) for bar in bars_from_closes(SPIKE)]
    assert signals == [None] * len(SPIKE)
    assert _z(SPIKE, 5) > 0


def test_no_second_open_while_already_long() -> None:
    strategy = short_mr()
    ctx = make_context(position_qty=0.1)
    signals = [strategy.on_bar(bar, ctx) for bar in bars_from_closes(DIP)]
    assert all(s is None for s in signals)


def test_halt_flattens_without_warmup() -> None:
    strategy = short_mr()
    bar = bars_from_closes([100.0])[0]
    signal = strategy.on_bar(bar, make_context(position_qty=0.25, halted=True))
    assert signal is not None
    assert signal.side == "sell"
    assert signal.intent == "close"
    assert signal.qty == 0.25
    assert signal.qty_unit == "base"
    assert signal.meta is not None
    assert signal.meta["lookback"] == 5
    assert "z" not in signal.meta


def test_halt_flat_is_silent() -> None:
    strategy = short_mr()
    bar = bars_from_closes([100.0])[0]
    assert strategy.on_bar(bar, make_context(halted=True, position_qty=0.0)) is None


def test_invalid_params_rejected() -> None:
    with pytest.raises(ValueError, match="lookback"):
        MeanReversion(lookback=1)
    with pytest.raises(ValueError, match="entry_z"):
        MeanReversion(entry_z=0)
    with pytest.raises(ValueError, match="exit_z"):
        MeanReversion(entry_z=1.0, exit_z=-1.0)
    with pytest.raises(ValueError, match="slippage"):
        MeanReversion(max_slippage_bps=-1)


def test_run_bars_open_then_close_on_paper_path() -> None:
    settings = Settings(paper_equity=2000, per_trade_pct=0.01)
    broker = PaperBroker(settings, store_path=":memory:")
    result = run_bars(bars_from_closes(REVERT), short_mr(), broker)
    assert result.metrics["n_fills"] == 2
    assert result.metrics["n_closed_trades"] == 1
    assert result.metrics["halted"] == 0
    assert broker.position_qty("BTC/USD") == 0
    fills = [row for row in broker.store.fills() if row["status"] == "filled"]
    assert [(row["intent"], row["side"]) for row in fills] == [("open", "buy"), ("close", "sell")]
    assert fills[0]["client_order_id"].startswith("mean_reversion_v1-")


def test_paper_cli_default_stays_sma(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["paper", "--state", str(tmp_path / "paper.sqlite")])
    assert result.exit_code == 0, result.output
    assert "strategy=sma_cross_v2" in result.output
    assert "mean_reversion_v1" not in result.output


def test_paper_cli_selects_mean_reversion(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "paper",
            "--strategy",
            "mean_reversion_v1",
            "--state",
            str(tmp_path / "paper_mr.sqlite"),
            "--lookback",
            "20",
            "--entry-z",
            "1.5",
            "--exit-z",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "strategy=mean_reversion_v1" in result.output
    assert '"n_fills"' in result.output
    assert '"max_dd"' in result.output


def test_paper_cli_rejects_unknown_strategy(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["paper", "--strategy", "grid", "--state", str(tmp_path / "paper.sqlite")],
    )
    assert result.exit_code != 0
    assert "unknown strategy" in result.output
