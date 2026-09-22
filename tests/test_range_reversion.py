"""Paper-only range_reversion_v2: env gate, z recovery, cost/RR, exits, timing, cooldown."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import fmean, pstdev

import pytest
from typer.testing import CliRunner

from quanttrading.backtest.runner import run_bars
from quanttrading.cli import app
from quanttrading.config import Settings
from quanttrading.data.ohlcv import save_csv
from quanttrading.execution.dry_run import DryRunBroker
from quanttrading.execution.paper import PaperBroker
from quanttrading.market import Bar, utc_iso
from quanttrading.strategy import (
    DEFAULT_STRATEGY_ID,
    RANGE_DEFAULT_STOP_ATR,
    RANGE_REVERSION_STRATEGY_ID,
    build_strategy,
)
from quanttrading.strategy.range_reversion import (
    DEFAULT_ADX_MAX_ENTRY,
    DEFAULT_ADX_TREND_EXIT,
    DEFAULT_COOLDOWN_BARS,
    DEFAULT_EMA_COMPRESSION,
    DEFAULT_ENTRY_Z,
    DEFAULT_MAX_HOLD_BARS,
    DEFAULT_MIN_RR_AFTER_COST,
    DEFAULT_ROUND_TRIP_COST,
    DEFAULT_Z_LOOKBACK,
    RangeReversion,
    evaluate_entry,
    evaluate_env_gate,
    has_bar_gap,
    population_z,
    stop_distance,
)
from tests.helpers import make_context

UTC = timezone.utc


def _entry(**overrides: float) -> str | None:
    payload = dict(
        close=97.0,
        mean=100.0,
        std=1.5,
        z_prev=-2.5,
        z_curr=-1.5,
        atr=1.0,
        adx=10.0,
        ema_fast=100.0,
        ema_slow=100.5,
        adx_max_entry=18.0,
        ema_compression=0.01,
        entry_z=2.0,
        stop_atr=1.0,
        stop_z=1.0,
        round_trip_cost=0.003,
        min_rr_after_cost=1.0,
    )
    payload.update(overrides)
    return evaluate_entry(**payload)


def _short(**overrides: object) -> RangeReversion:
    params: dict = dict(
        ema_fast=3,
        ema_slow=8,
        atr_period=3,
        adx_period=3,
        adx_max_entry=100.0,
        adx_trend_exit=101.0,
        ema_compression=1.0,
        z_lookback=5,
        entry_z=2.0,
        stop_atr=1.0,
        stop_z=1.0,
        # Path fixtures use a sharp dip that widens ATR; RR is unit-tested separately.
        round_trip_cost=0.003,
        min_rr_after_cost=0.01,
        max_hold_bars=18,
        cooldown_bars=6,
        max_slippage_bps=0.0,
    )
    params.update(overrides)
    return RangeReversion(**params)


def _bar(ts: datetime, close: float, *, high: float | None = None, low: float | None = None) -> Bar:
    opened = close
    return Bar(
        ts=ts,
        symbol="BTC/USD",
        open=opened,
        high=high if high is not None else close + 0.1,
        low=low if low is not None else close - 0.1,
        close=close,
        volume=1.0,
    )


def _recovery_bars() -> list[Bar]:
    """Warm-up, dip below -2z, recovery below the mean, then one exec bar."""
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    closes = [98.0] * 12 + [98.0, 98.0, 98.0, 98.0, 98.0, 80.0, 90.0, 90.0]
    bars: list[Bar] = []
    for close in closes:
        if close == 80.0:
            bars.append(_bar(ts, close, high=81.0, low=79.0))
        else:
            bars.append(_bar(ts, close))
        ts += timedelta(hours=4)
    return bars


def _drive(strategy: RangeReversion, bars: list[Bar], *, position_qty: float = 0.0) -> list:
    ctx = make_context(position_qty=position_qty)
    signals = []
    for bar in bars:
        signal = strategy.on_bar(bar, ctx)
        signals.append(signal)
        if signal is not None and signal.intent == "open":
            ctx = make_context(position_qty=1.0)
        elif signal is not None and signal.intent == "close":
            ctx = make_context(position_qty=0.0)
    return signals


def test_defaults_match_brief_and_live_default_stays_sma() -> None:
    strategy = build_strategy("range_reversion_v2")
    assert isinstance(strategy, RangeReversion)
    assert strategy.strategy_id == RANGE_REVERSION_STRATEGY_ID == "range_reversion_v2"
    assert strategy.ema_fast == 50
    assert strategy.ema_slow == 200
    assert strategy.atr_period == 14
    assert strategy.adx_period == 14
    assert strategy.adx_max_entry == DEFAULT_ADX_MAX_ENTRY == 18.0
    assert strategy.adx_trend_exit == DEFAULT_ADX_TREND_EXIT == 25.0
    assert strategy.ema_compression == DEFAULT_EMA_COMPRESSION == 0.01
    assert strategy.z_lookback == DEFAULT_Z_LOOKBACK == 48
    assert strategy.entry_z == DEFAULT_ENTRY_Z == 2.0
    assert strategy.stop_atr == RANGE_DEFAULT_STOP_ATR == 1.0
    assert strategy.stop_z == 1.0
    assert strategy.round_trip_cost == DEFAULT_ROUND_TRIP_COST == 0.003
    assert strategy.min_rr_after_cost == DEFAULT_MIN_RR_AFTER_COST == 1.0
    assert strategy.max_hold_bars == DEFAULT_MAX_HOLD_BARS == 18
    assert strategy.cooldown_bars == DEFAULT_COOLDOWN_BARS == 6
    assert build_strategy().strategy_id == DEFAULT_STRATEGY_ID == "sma_cross_v2"
    assert build_strategy("trend_breakout_v1").stop_atr == 2.0


def test_env_gate_requires_quiet_adx_and_compressed_emas() -> None:
    assert (
        evaluate_env_gate(
            adx=10.0,
            ema_fast=100.0,
            ema_slow=100.5,
            adx_max_entry=18.0,
            ema_compression=0.01,
        )
        is None
    )
    assert (
        evaluate_env_gate(
            adx=18.0,
            ema_fast=100.0,
            ema_slow=100.5,
            adx_max_entry=18.0,
            ema_compression=0.01,
        )
        == "adx"
    )
    assert (
        evaluate_env_gate(
            adx=20.0,
            ema_fast=100.0,
            ema_slow=100.5,
            adx_max_entry=18.0,
            ema_compression=0.01,
        )
        == "adx"
    )
    assert (
        evaluate_env_gate(
            adx=10.0,
            ema_fast=102.0,
            ema_slow=100.0,
            adx_max_entry=18.0,
            ema_compression=0.01,
        )
        == "compression"
    )


def test_entry_combo_requires_env_z_recovery_and_rr() -> None:
    assert _entry() is None
    assert _entry(adx=18.0) == "adx"
    assert _entry(ema_fast=102.0, ema_slow=100.0) == "compression"
    assert _entry(z_prev=-1.5) == "z_recovery"
    assert _entry(z_curr=-2.1) == "z_recovery"
    assert _entry(close=100.0) == "z_recovery"
    # Upside too small versus cost.
    assert _entry(close=99.8, mean=100.0, atr=0.01, std=0.01, z_prev=-2.5, z_curr=-1.0) == "cost"
    # RR after cost below 1.0: large stop relative to upside.
    assert _entry(close=99.0, mean=100.0, atr=10.0, std=0.5, z_prev=-2.5, z_curr=-1.0) == "rr"


def test_population_z_excludes_signal_bar_and_skips_zero_std() -> None:
    closes = [100.0, 101.0, 99.0, 100.5, 98.0, 97.0]
    stats = population_z(closes, 4)
    assert stats is not None
    mean, std, z_prev, z_curr = stats
    window = closes[-5:-1]
    assert mean == pytest.approx(fmean(window))
    assert std == pytest.approx(pstdev(window))
    assert z_prev == pytest.approx((closes[-2] - mean) / std)
    assert z_curr == pytest.approx((closes[-1] - mean) / std)
    assert population_z([10.0] * 6, 4) is None


def test_stop_distance_uses_max_of_atr_and_z_band() -> None:
    assert stop_distance(2.0, 1.0, 1.0, 1.0) == pytest.approx(2.0)
    assert stop_distance(0.5, 2.0, 1.0, 1.0) == pytest.approx(2.0)


def test_z_recovery_entry_emits_next_bar_open() -> None:
    bars = _recovery_bars()
    signals = _drive(_short(), bars)
    entry_i = next(i for i, signal in enumerate(signals) if signal is not None and signal.intent == "open")
    signal = signals[entry_i]
    assert entry_i > 0
    assert signals[entry_i - 1] is None
    assert signal.strategy_id == "range_reversion_v2"
    assert signal.side == "buy" and signal.intent == "open"
    assert signal.meta["reason"] == "z_recovery"
    assert signal.meta["fill_on"] == "open"
    assert signal.meta["timeframe"] == "4h"
    assert signal.meta["signal_ts"] == utc_iso(bars[entry_i - 1].ts)
    assert signal.meta["exec_ts"] == utc_iso(bars[entry_i].ts)
    assert signal.meta["signal_ts"] < signal.meta["exec_ts"]
    assert signal.meta["z_prev"] < -2.0
    assert signal.meta["z_curr"] >= -2.0
    assert signal.meta["close"] < signal.meta["mean"]
    assert signal.meta["target"] == pytest.approx(signal.meta["mean"])
    assert signal.meta["upside"] > 0.0


def test_cost_rr_filter_blocks_a_setup_the_path_would_take() -> None:
    bars = _recovery_bars()
    allowed = _drive(_short(), bars)
    assert any(signal is not None and signal.intent == "open" for signal in allowed)
    blocked = _drive(_short(min_rr_after_cost=50.0), bars)
    assert blocked == [None] * len(bars)


def test_open_position_blocks_a_new_buy() -> None:
    signals = _drive(_short(), _recovery_bars(), position_qty=0.25)
    assert all(signal is None for signal in signals)


def test_gap_in_z_window_skips_entry() -> None:
    assert has_bar_gap(
        [
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 1, 4, tzinfo=UTC),
            datetime(2024, 1, 1, 12, tzinfo=UTC),
        ],
        2,
        timedelta(hours=4),
    )
    bars = _recovery_bars()
    # Break the 4h spacing inside the z-window just before the recovery.
    broken = list(bars)
    idx = len(broken) - 2
    broken[idx] = Bar(
        ts=broken[idx].ts + timedelta(hours=1),
        symbol=broken[idx].symbol,
        open=broken[idx].open,
        high=broken[idx].high,
        low=broken[idx].low,
        close=broken[idx].close,
        volume=1.0,
    )
    assert _drive(_short(), broken) == [None] * len(broken)


def test_target_and_stop_exits_on_next_bar() -> None:
    strategy = _short(max_hold_bars=50)
    bars = _recovery_bars()
    ctx = make_context()
    entry_i: int | None = None
    for i, bar in enumerate(bars):
        signal = strategy.on_bar(bar, ctx)
        if signal is not None and signal.intent == "open":
            entry_i = i
            ctx = make_context(position_qty=1.0)
            break
    assert entry_i is not None
    assert strategy.target is not None and strategy.stop is not None
    locked_target = strategy.target
    locked_stop = strategy.stop

    ts = bars[-1].ts + timedelta(hours=4)
    hit_target = _bar(ts, locked_target - 1.0, high=locked_target + 0.5, low=locked_stop + 1.0)
    assert strategy.on_bar(hit_target, ctx) is None
    exit_bar = _bar(ts + timedelta(hours=4), locked_target)
    exit_signal = strategy.on_bar(exit_bar, ctx)
    assert exit_signal is not None
    assert exit_signal.intent == "close"
    assert exit_signal.meta["reason"] == "target"
    assert exit_signal.meta["fill_on"] == "open"

    strategy2 = _short(max_hold_bars=50)
    ctx2 = make_context()
    for bar in bars:
        signal = strategy2.on_bar(bar, ctx2)
        if signal is not None and signal.intent == "open":
            ctx2 = make_context(position_qty=1.0)
            break
    assert strategy2.stop is not None
    ts2 = bars[-1].ts + timedelta(hours=4)
    hit_stop = _bar(ts2, strategy2.stop + 1.0, high=strategy2.stop + 2.0, low=strategy2.stop - 0.5)
    assert strategy2.on_bar(hit_stop, ctx2) is None
    stop_exit = strategy2.on_bar(_bar(ts2 + timedelta(hours=4), strategy2.stop + 1.0), ctx2)
    assert stop_exit is not None
    assert stop_exit.meta["reason"] == "stop"


def test_max_hold_exits_without_target() -> None:
    bars = _recovery_bars()
    ts = bars[-1].ts + timedelta(hours=4)
    for _ in range(3):
        bars.append(_bar(ts, 90.0, high=90.1, low=89.9))
        ts += timedelta(hours=4)
    signals = _drive(_short(max_hold_bars=1), bars)
    first_close = next(signal for signal in signals if signal is not None and signal.intent == "close")
    assert first_close.meta["reason"] == "max_hold"
    assert first_close.meta["bars_held"] == 1
    assert first_close.meta["signal_ts"] < first_close.meta["exec_ts"]


def test_regime_exit_when_adx_trends_and_close_below_ema() -> None:
    strategy = _short(max_hold_bars=50)
    bars = _recovery_bars()
    ctx = make_context()
    for bar in bars:
        signal = strategy.on_bar(bar, ctx)
        if signal is not None and signal.intent == "open":
            ctx = make_context(position_qty=1.0)
            break
    assert strategy.stop is not None
    strategy.adx_trend_exit = 0.0
    ts = bars[-1].ts + timedelta(hours=4)
    # Stay above the stop so regime (not stop) is the exit reason.
    weak = _bar(ts, strategy.stop + 2.0, high=strategy.stop + 2.1, low=strategy.stop + 1.5)
    assert strategy.on_bar(weak, ctx) is None
    exit_signal = strategy.on_bar(_bar(ts + timedelta(hours=4), strategy.stop + 2.0), ctx)
    assert exit_signal is not None
    assert exit_signal.meta["reason"] == "regime"


def test_cooldown_waits_six_closed_bars_after_the_exit_fill() -> None:
    strategy = _short(cooldown_bars=6, max_hold_bars=50)
    bars = _recovery_bars()
    ctx = make_context()
    signals = []
    entry_i: int | None = None
    for bar in bars:
        signal = strategy.on_bar(bar, ctx)
        signals.append(signal)
        if signal is not None and signal.intent == "open":
            entry_i = len(signals) - 1
            ctx = make_context(position_qty=1.0)

    assert entry_i is not None
    assert strategy.stop is not None and strategy.target is not None
    locked_stop = strategy.stop
    locked_target = strategy.target
    ts = bars[-1].ts

    # Bar after entry: stay inside the band (no exit yet).
    ts += timedelta(hours=4)
    mid = (locked_stop + locked_target) / 2.0
    signals.append(strategy.on_bar(_bar(ts, mid, high=mid + 0.1, low=mid - 0.1), ctx))
    assert signals[-1] is None

    # Next bar: poke the stop (decision). Fill on the following open.
    ts += timedelta(hours=4)
    signals.append(
        strategy.on_bar(_bar(ts, mid, high=mid + 0.1, low=locked_stop - 0.5), ctx)
    )
    assert signals[-1] is None
    ts += timedelta(hours=4)
    exit_signal = strategy.on_bar(_bar(ts, mid, high=mid + 0.1, low=mid - 0.1), ctx)
    signals.append(exit_signal)
    assert exit_signal is not None and exit_signal.intent == "close"
    assert exit_signal.meta["reason"] == "stop"
    ctx = make_context(position_qty=0.0)
    close_i = len(signals) - 1

    # Cooldown bars must not open; then feed recoveries until a second entry appears.
    for _ in range(strategy.cooldown_bars):
        ts += timedelta(hours=4)
        signals.append(strategy.on_bar(_bar(ts, 98.0), ctx))
        assert signals[-1] is None or signals[-1].intent != "open"

    second_open = None
    for _ in range(30):
        for close in [98.0, 98.0, 98.0, 98.0, 98.0, 80.0, 90.0, 90.0]:
            ts += timedelta(hours=4)
            bar = _bar(ts, close, high=81.0, low=79.0) if close == 80.0 else _bar(ts, close)
            signal = strategy.on_bar(bar, ctx)
            signals.append(signal)
            if signal is not None and signal.intent == "open":
                second_open = len(signals) - 1
                break
        if second_open is not None:
            break

    assert second_open is not None
    assert second_open >= close_i + strategy.cooldown_bars + 2
    assert close_i == entry_i + 3


def test_no_lookahead_paper_rejects_wrong_exec_bar() -> None:
    bars = _recovery_bars()
    preview = _drive(_short(), bars)
    entry_i = next(i for i, signal in enumerate(preview) if signal is not None and signal.intent == "open")
    signal = preview[entry_i]

    opened = bars[entry_i].open + 2.5
    exec_bar = bars[entry_i]
    gapped = list(bars)
    gapped[entry_i] = Bar(
        ts=exec_bar.ts,
        symbol=exec_bar.symbol,
        open=opened,
        high=max(opened, exec_bar.close) + 0.1,
        low=min(opened, exec_bar.close) - 0.05,
        close=exec_bar.close,
        volume=1.0,
    )
    settings = Settings(paper_equity=2000, per_trade_pct=0.01, max_slippage_bps=0)
    broker = PaperBroker(settings, store_path=":memory:")
    run_bars(gapped, _short(), broker)
    fills = [row for row in broker.store.fills() if row["status"] == "filled"]
    assert fills[0]["price"] == pytest.approx(opened)
    assert fills[0]["price"] != pytest.approx(signal.meta["signal_close"])
    assert fills[0]["ts"] == utc_iso(exec_bar.ts)

    wrong = PaperBroker(settings, store_path=":memory:")
    rejected = wrong.submit(signal, bars[entry_i - 1])
    assert rejected.status == "rejected"
    assert rejected.reason == "exec_ts_mismatch"

    lookahead = signal.model_copy(
        update={"meta": {**signal.meta, "signal_ts": signal.meta["exec_ts"]}}
    )
    later = PaperBroker(settings, store_path=":memory:")
    blocked = later.submit(lookahead, bars[entry_i])
    assert blocked.status == "rejected"
    assert blocked.reason == "lookahead_ts"


def test_paper_and_dry_run_cli_select_range_reversion(tmp_path) -> None:
    csv_path = tmp_path / "btcusd_4h.csv"
    save_csv(_recovery_bars(), csv_path)
    paper = CliRunner().invoke(
        app,
        [
            "paper",
            "--strategy",
            "range_reversion_v2",
            "--data",
            str(csv_path),
            "--state",
            str(tmp_path / "paper_range.sqlite"),
            "--round-trip-cost",
            "0.003",
        ],
    )
    assert paper.exit_code == 0, paper.output
    assert "strategy=range_reversion_v2" in paper.output

    dry = CliRunner().invoke(
        app,
        [
            "dry-run",
            "--strategy",
            "range_reversion_v2",
            "--data",
            str(csv_path),
            "--state",
            str(tmp_path / "dry_range.sqlite"),
            "--per-trade-pct",
            "0.005",
        ],
    )
    assert dry.exit_code == 0, dry.output
    assert "strategy=range_reversion_v2" in dry.output
    assert "orders_sent=0" in dry.output
