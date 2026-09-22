"""Paper SMA variants for 15m and 5m bars. Live default stays sma_cross_v2."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from quanttrading.backtest.runner import run_bars
from quanttrading.cli import _build_strategy, app
from quanttrading.config import Settings
from quanttrading.execution.paper import PaperBroker
from quanttrading.strategy import (
    DEFAULT_MIN_VOL,
    DEFAULT_STRATEGY_ID,
    DEFAULT_VOL_WINDOW,
    HF_MIN_VOL,
    HF_VOL_WINDOW,
    SMA_CROSS_15M_STRATEGY_ID,
    SMA_CROSS_5M_STRATEGY_ID,
    SMACrossover,
    build_strategy,
    default_strategy,
    realized_vol,
)
from tests.helpers import bars_from_closes, make_context

# 30 flat closes, then one jump. slow=30 needs 31 closes before a cross.
# Vol window 16 sees fifteen zero returns and one jump return.
_BELOW_MIN = 0.0007  # realized vol ≈ 0.000169, under 0.0002
_RELAXED = 0.0009  # realized vol ≈ 0.000218, in [0.0002, 0.0005)
_AT_MIN = 0.0002 * 16 / (15**0.5)  # realized vol == 0.0002
# After the relaxed open, 17 identical down-steps push the jump out of the
# vol window (std 0) and cross fast back below slow.
_QUIET_STEPS = 17
_QUIET_STEP = 0.00005

HF_IDS = (SMA_CROSS_15M_STRATEGY_ID, SMA_CROSS_5M_STRATEGY_ID)


def _jump(ret: float) -> list[float]:
    return [100.0] * 30 + [100.0 * (1.0 + ret)]


def _open_then_quiet_close() -> list[float]:
    closes = _jump(_RELAXED)
    price = closes[-1]
    for _ in range(_QUIET_STEPS):
        price *= 1.0 - _QUIET_STEP
        closes.append(price)
    return closes


def _signals(strategy: SMACrossover, closes: list[float], *, position_qty: float = 0.0):
    ctx = make_context(equity=2000.0, per_trade_pct=0.01, position_qty=position_qty)
    out = []
    for bar in bars_from_closes(closes, symbol="BTC/USD"):
        signal = strategy.on_bar(bar, ctx)
        out.append(signal)
        if signal is not None and signal.intent == "open":
            ctx = make_context(equity=2000.0, per_trade_pct=0.01, position_qty=0.05)
    return out


@pytest.mark.parametrize("strategy_id", HF_IDS)
def test_factory_defaults_are_relaxed_and_not_the_live_default(strategy_id: str) -> None:
    strategy = build_strategy(strategy_id)
    assert isinstance(strategy, SMACrossover)
    assert strategy.strategy_id == strategy_id
    assert strategy.strategy_id != DEFAULT_STRATEGY_ID
    assert strategy.fast == 10
    assert strategy.slow == 30
    assert strategy.vol_window == HF_VOL_WINDOW == 16
    assert strategy.min_vol == HF_MIN_VOL == 0.0002
    default = default_strategy()
    assert default.strategy_id == "sma_cross_v2"
    assert default.vol_window == DEFAULT_VOL_WINDOW == 20
    assert default.min_vol == DEFAULT_MIN_VOL == 0.0005


def test_build_strategy_keeps_v2_when_vol_args_omitted() -> None:
    strategy = build_strategy()
    assert strategy.strategy_id == "sma_cross_v2"
    assert strategy.vol_window == 20
    assert strategy.min_vol == 0.0005


@pytest.mark.parametrize("strategy_id", HF_IDS)
def test_explicit_vol_override(strategy_id: str) -> None:
    strategy = build_strategy(strategy_id, vol_window=12, min_vol=0.001)
    assert isinstance(strategy, SMACrossover)
    assert strategy.vol_window == 12
    assert strategy.min_vol == 0.001
    assert strategy.fast == 10
    assert strategy.slow == 30


@pytest.mark.parametrize("strategy_id", HF_IDS)
def test_open_skipped_when_vol_below_0_0002(strategy_id: str) -> None:
    closes = _jump(_BELOW_MIN)
    vol = realized_vol(closes, HF_VOL_WINDOW)
    assert vol is not None
    assert vol < 0.0002
    strategy = build_strategy(strategy_id)
    signals = _signals(strategy, closes)
    assert all(signal is None for signal in signals)


@pytest.mark.parametrize("strategy_id", HF_IDS)
def test_open_at_relaxed_threshold_and_v2_still_skips(strategy_id: str) -> None:
    closes = _jump(_RELAXED)
    vol = realized_vol(closes, HF_VOL_WINDOW)
    assert vol is not None
    assert vol >= 0.0002
    assert vol < DEFAULT_MIN_VOL

    strategy = build_strategy(strategy_id)
    signals = _signals(strategy, closes)
    assert all(signal is None for signal in signals[:-1])
    signal = signals[-1]
    assert signal is not None
    assert signal.strategy_id == strategy_id
    assert signal.side == "buy"
    assert signal.intent == "open"
    assert signal.qty == 20.0
    assert signal.qty_unit == "quote"
    assert signal.order_type == "market"
    assert signal.limit_price is None
    assert signal.client_order_id.startswith(f"{strategy_id}-BTCUSD-")
    assert signal.client_order_id.endswith("-open-buy")
    assert signal.meta is not None
    assert signal.meta["fast"] == 10
    assert signal.meta["slow"] == 30
    assert signal.meta["vol_window"] == 16
    assert signal.meta["min_vol"] == 0.0002
    assert signal.meta["vol_filter"] == "min"
    assert signal.meta["vol_ok"] is True
    assert signal.meta["realized_vol"] == pytest.approx(vol)

    v2 = build_strategy(DEFAULT_STRATEGY_ID)
    assert all(item is None for item in _signals(v2, closes))


@pytest.mark.parametrize("strategy_id", HF_IDS)
def test_open_when_vol_equals_0_0002(strategy_id: str) -> None:
    closes = _jump(_AT_MIN)
    vol = realized_vol(closes, HF_VOL_WINDOW)
    assert vol is not None
    assert vol == pytest.approx(0.0002)
    signal = _signals(build_strategy(strategy_id), closes)[-1]
    assert signal is not None
    assert signal.intent == "open"
    assert signal.meta is not None
    assert signal.meta["vol_ok"] is True


@pytest.mark.parametrize("strategy_id", HF_IDS)
def test_close_unrestricted_when_vol_below_0_0002(strategy_id: str) -> None:
    closes = _jump(-_BELOW_MIN)
    vol = realized_vol(closes, HF_VOL_WINDOW)
    assert vol is not None
    assert vol < 0.0002
    strategy = build_strategy(strategy_id)
    signals = _signals(strategy, closes, position_qty=0.05)
    close_signals = [signal for signal in signals if signal is not None]
    assert len(close_signals) == 1
    signal = close_signals[0]
    assert signal.side == "sell"
    assert signal.intent == "close"
    assert signal.qty == 0.05
    assert signal.qty_unit == "base"
    assert signal.order_type == "market"
    assert signal.client_order_id.endswith("-close-sell")
    assert signal.meta is not None
    assert signal.meta["min_vol"] == 0.0002
    assert signal.meta["vol_ok"] is False
    assert signal.meta["realized_vol"] == pytest.approx(vol)
    assert all(item is None or item.intent != "open" for item in signals)


@pytest.mark.parametrize("strategy_id", HF_IDS)
def test_signal_submits_open_then_low_vol_close(strategy_id: str) -> None:
    settings = Settings(paper_equity=2000, per_trade_pct=0.01, daily_dd_pct=0.03, total_dd_pct=0.20)
    assert settings.per_trade_pct == 0.01
    assert settings.daily_dd_pct == 0.03
    assert settings.total_dd_pct == 0.20
    broker = PaperBroker(settings, store_path=":memory:")
    closes = _open_then_quiet_close()
    close_vol = realized_vol(closes, HF_VOL_WINDOW)
    assert close_vol is not None
    assert close_vol < 0.0002
    result = run_bars(bars_from_closes(closes), build_strategy(strategy_id), broker)
    assert result.metrics["n_fills"] == 2
    assert result.metrics["n_closed_trades"] == 1
    assert result.metrics["halted"] == 0
    assert broker.position_qty("BTC/USD") == 0
    fills = [row for row in broker.store.fills() if row["status"] == "filled"]
    assert [(row["intent"], row["side"]) for row in fills] == [("open", "buy"), ("close", "sell")]
    assert fills[0]["client_order_id"].startswith(f"{strategy_id}-")
    assert fills[1]["client_order_id"].endswith("-close-sell")
    assert fills[0]["notional"] == pytest.approx(20.0, rel=0.02)


def test_cli_presets_and_default_stay_on_v2() -> None:
    settings = Settings()
    common = dict(fast=10, slow=30, lookback=20, entry_z=1.5, exit_z=0.0, vol_window=None, min_vol=None)
    default = _build_strategy(settings, DEFAULT_STRATEGY_ID, **common)
    assert default.strategy_id == "sma_cross_v2"
    assert default.vol_window == 20
    assert default.min_vol == 0.0005
    for strategy_id in HF_IDS:
        chosen = _build_strategy(settings, strategy_id, **common)
        assert isinstance(chosen, SMACrossover)
        assert chosen.strategy_id == strategy_id
        assert chosen.vol_window == 16
        assert chosen.min_vol == 0.0002


def test_paper_cli_selects_hf_strategies_without_changing_default(tmp_path: Path) -> None:
    runner = CliRunner()
    default = runner.invoke(app, ["paper", "--state", str(tmp_path / "paper.sqlite")])
    assert default.exit_code == 0, default.output
    assert "strategy=sma_cross_v2" in default.output
    assert "sma_cross_15m_v1" not in default.output
    assert "sma_cross_5m_v1" not in default.output

    for strategy_id, name in (
        (SMA_CROSS_15M_STRATEGY_ID, "paper_15m.sqlite"),
        (SMA_CROSS_5M_STRATEGY_ID, "paper_5m.sqlite"),
    ):
        result = runner.invoke(
            app,
            ["paper", "--strategy", strategy_id, "--state", str(tmp_path / name)],
        )
        assert result.exit_code == 0, result.output
        assert f"strategy={strategy_id}" in result.output
        assert '"n_fills"' in result.output
        assert '"max_dd"' in result.output

    help_text = runner.invoke(app, ["paper", "--help"])
    assert help_text.exit_code == 0
    assert "sma_cross_15m_v1" in help_text.output
    assert "sma_cross_5m_v1" in help_text.output
    assert "0.0002" in help_text.output
