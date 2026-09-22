"""Paper-only trend_breakout_v1: entry gates, cost, stop lock, timing, cooldown."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    DEFAULT_ROUND_TRIP_COST,
    DEFAULT_STOP_ATR,
    DEFAULT_STRATEGY_ID,
    TREND_BREAKOUT_STRATEGY_ID,
    build_strategy,
)
from quanttrading.strategy.indicators import ema, prior_window, wilder_atr_adx
from quanttrading.strategy.trend_breakout import (
    DEFAULT_ADX_MIN,
    DEFAULT_BREAKOUT_ATR,
    DEFAULT_BREAKOUT_LOOKBACK,
    DEFAULT_COOLDOWN_BARS,
    DEFAULT_EMA_FAST,
    DEFAULT_EMA_SLOW,
    DEFAULT_FOLLOW_THROUGH_BARS,
    DEFAULT_MAX_CHASE_ATR,
    TrendBreakout,
    evaluate_entry,
    planned_stop_risk_fraction,
    ratchet_stop,
)
from tests.helpers import make_context

UTC = timezone.utc


def _entry(**overrides: float) -> str | None:
    payload = dict(
        close=100.0,
        ema_fast=101.0,
        ema_slow=90.0,
        adx=30.0,
        atr=2.0,
        prior_high=98.0,
        adx_min=25.0,
        breakout_atr=0.1,
        max_chase_atr=1.0,
        stop_atr=2.0,
        round_trip_cost=0.003,
    )
    payload.update(overrides)
    return evaluate_entry(**payload)


def _short(**overrides: object) -> TrendBreakout:
    params: dict = dict(
        ema_fast=3,
        ema_slow=8,
        atr_period=3,
        adx_period=3,
        adx_min=15.0,
        breakout_lookback=5,
        trend_break_lookback=3,
        follow_through_bars=30,
        cooldown_bars=3,
        max_slippage_bps=0.0,
    )
    params.update(overrides)
    return TrendBreakout(**params)


def _rising(n: int = 40, *, start: float = 100.0, step: float = 1.0) -> list[Bar]:
    price = start
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[Bar] = []
    for _ in range(n):
        opened = price
        close = price + step
        bars.append(
            Bar(
                ts=ts,
                symbol="BTC/USD",
                open=opened,
                high=close + 0.1,
                low=opened - 0.05,
                close=close,
                volume=1.0,
            )
        )
        price = close
        ts += timedelta(hours=4)
    return bars


def _drive(strategy: TrendBreakout, bars: list[Bar], *, position_qty: float = 0.0) -> list:
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
    strategy = build_strategy("trend_breakout_v1")
    assert isinstance(strategy, TrendBreakout)
    assert strategy.strategy_id == TREND_BREAKOUT_STRATEGY_ID == "trend_breakout_v1"
    assert strategy.ema_fast == DEFAULT_EMA_FAST == 50
    assert strategy.ema_slow == DEFAULT_EMA_SLOW == 200
    assert strategy.atr_period == 14
    assert strategy.adx_period == 14
    assert strategy.adx_min == DEFAULT_ADX_MIN == 25.0
    assert strategy.breakout_lookback == DEFAULT_BREAKOUT_LOOKBACK == 55
    assert strategy.breakout_atr == DEFAULT_BREAKOUT_ATR == 0.1
    assert strategy.max_chase_atr == DEFAULT_MAX_CHASE_ATR == 1.0
    assert strategy.stop_atr == DEFAULT_STOP_ATR == 2.0
    assert strategy.round_trip_cost == DEFAULT_ROUND_TRIP_COST == 0.003
    assert strategy.trend_break_lookback == 10
    assert strategy.follow_through_bars == DEFAULT_FOLLOW_THROUGH_BARS == 30
    assert strategy.cooldown_bars == DEFAULT_COOLDOWN_BARS == 3
    assert build_strategy().strategy_id == DEFAULT_STRATEGY_ID == "sma_cross_v2"
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategy("range_reversion_v2")


def test_entry_combo_requires_every_gate() -> None:
    assert _entry() is None
    assert _entry(close=90.0) == "trend"
    assert _entry(ema_fast=90.0) == "trend"
    assert _entry(adx=24.9) == "adx"
    assert _entry(close=98.2) == "breakout"
    assert _entry(close=101.0, prior_high=98.0) == "chase"
    # Distance is inside the chase band, and still too small versus 2x cost.
    assert _entry(atr=0.2, prior_high=99.9) == "cost"
    assert _entry(atr=0.3, prior_high=99.85) is None


def test_cost_filter_blocks_a_setup_the_price_path_would_take() -> None:
    bars = _rising(20)
    allowed = _drive(_short(), bars)
    assert any(signal is not None and signal.intent == "open" for signal in allowed)
    blocked = _drive(_short(round_trip_cost=0.05), bars)
    assert blocked == [None] * len(bars)


def test_open_position_blocks_a_new_buy() -> None:
    signals = _drive(_short(), _rising(20), position_qty=0.25)
    assert signals == [None] * 20


def test_breakout_signal_records_every_passing_gate() -> None:
    signals = _drive(_short(), _rising(12))
    signal = next(item for item in signals if item is not None)
    meta = signal.meta
    assert signal.strategy_id == "trend_breakout_v1"
    assert signal.side == "buy" and signal.intent == "open"
    assert meta["reason"] == "breakout"
    assert meta["close"] > meta["ema_slow"]
    assert meta["ema_fast"] > meta["ema_slow"]
    assert meta["adx"] >= meta["adx_min"]
    assert meta["close"] > meta["prior_high"] + meta["breakout_atr"] * meta["atr"]
    assert meta["extension"] <= meta["max_chase_atr"] * meta["atr"]
    assert meta["cost_ratio"] >= 2.0 * meta["assumed_round_trip_cost_pct"]
    assert meta["assumed_round_trip_cost_pct"] == 0.003
    assert meta["fill_on"] == "open"
    assert meta["timeframe"] == "4h"


def test_stop_never_moves_down_when_atr_widens() -> None:
    assert ratchet_stop(90.0, 100.0, 20.0, 2.0) == 90.0
    assert ratchet_stop(90.0, 110.0, 5.0, 2.0) == 100.0

    strategy = _short()
    price = 100.0
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    ctx = make_context()
    locked: float | None = None
    initial: float | None = None
    for i in range(12):
        opened = price
        close = price + 1.0
        bar = Bar(
            ts=ts,
            symbol="BTC/USD",
            open=opened,
            high=close + 0.1,
            low=opened - 0.05,
            close=close,
            volume=1.0,
        )
        signal = strategy.on_bar(bar, ctx)
        price = close
        ts += timedelta(hours=4)
        if signal is not None and signal.intent == "open":
            initial = float(signal.meta["stop"])
            locked = strategy.stop
            ctx = make_context(position_qty=1.0)
            assert locked is not None and initial is not None
            assert locked >= initial
            fat_open = price
            fat_close = price + 0.2
            fat_low = locked + 0.2
            fat = Bar(
                ts=ts,
                symbol="BTC/USD",
                open=fat_open,
                high=fat_low + 40.0,
                low=fat_low,
                close=fat_close,
                volume=1.0,
            )
            assert strategy.on_bar(fat, ctx) is None
            assert strategy.stop == pytest.approx(locked)
            assert strategy.stop >= initial
            return
    raise AssertionError("expected an entry before the widened-ATR bar")


def test_signal_and_fill_are_separated_by_one_bar() -> None:
    bars = _rising(12)
    preview = _drive(_short(), bars)
    entry_i = next(i for i, signal in enumerate(preview) if signal is not None and signal.intent == "open")
    signal = preview[entry_i]
    assert entry_i > 0
    assert signal.meta["signal_ts"] == utc_iso(bars[entry_i - 1].ts)
    assert signal.meta["exec_ts"] == utc_iso(bars[entry_i].ts)
    assert signal.meta["signal_ts"] < signal.meta["exec_ts"]
    assert signal.ts == bars[entry_i].ts
    assert signal.meta["signal_close"] == pytest.approx(bars[entry_i - 1].close)

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

    dry = DryRunBroker(settings, store_path=":memory:")
    run_bars(gapped, _short(), dry)
    assert dry.orders[0].reference_price == pytest.approx(opened)
    assert dry.orders[0].reference_price != pytest.approx(bars[entry_i - 1].close)

    wrong = PaperBroker(settings, store_path=":memory:")
    rejected = wrong.submit(signal, bars[entry_i - 1])
    assert rejected.status == "rejected"
    assert rejected.reason == "exec_ts_mismatch"
    assert wrong.position_qty("BTC/USD") == 0.0

    lookahead = signal.model_copy(
        update={"meta": {**signal.meta, "signal_ts": signal.meta["exec_ts"]}}
    )
    later = PaperBroker(settings, store_path=":memory:")
    blocked = later.submit(lookahead, bars[entry_i])
    assert blocked.status == "rejected"
    assert blocked.reason == "lookahead_ts"


def test_cooldown_waits_three_closed_bars_after_the_exit_fill() -> None:
    strategy = _short()
    price = 100.0
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    ctx = make_context()
    signals = []
    entry_i: int | None = None
    for i in range(40):
        opened = price
        close = price + 1.0
        high = close + 0.1
        low = opened - 0.05
        if entry_i is not None and i == entry_i + 2:
            assert strategy.stop is not None
            low = strategy.stop - 0.5
            high = max(high, close + 0.1)
        bar = Bar(ts=ts, symbol="BTC/USD", open=opened, high=high, low=low, close=close, volume=1.0)
        signal = strategy.on_bar(bar, ctx)
        signals.append(signal)
        if signal is not None and signal.intent == "open":
            if entry_i is None:
                entry_i = i
            ctx = make_context(position_qty=1.0)
        elif signal is not None and signal.intent == "close":
            ctx = make_context(position_qty=0.0)
        price = close
        ts += timedelta(hours=4)

    opens = [i for i, signal in enumerate(signals) if signal is not None and signal.intent == "open"]
    closes = [i for i, signal in enumerate(signals) if signal is not None and signal.intent == "close"]
    assert opens[0] == entry_i
    assert closes[0] == opens[0] + 3
    assert signals[closes[0]].meta["reason"] == "stop"
    quiet_until = closes[0] + strategy.cooldown_bars + 1
    assert all(signals[i] is None for i in range(closes[0] + 1, quiet_until))
    assert opens[1] == closes[0] + strategy.cooldown_bars + 2
    assert signals[opens[1]].meta["signal_ts"] == utc_iso(
        datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=4 * (opens[1] - 1))
    )


def test_no_follow_through_exits_on_the_next_bar() -> None:
    signals = _drive(_short(follow_through_bars=1), _rising(16))
    first_close = next(signal for signal in signals if signal is not None and signal.intent == "close")
    assert first_close.meta["reason"] == "no_follow_through"
    assert first_close.meta["bars_held"] == 1
    assert first_close.meta["max_close"] < first_close.meta["entry_price"] + first_close.meta["initial_stop_distance"]
    assert first_close.meta["signal_ts"] < first_close.meta["exec_ts"]


def test_indicators_match_hand_values() -> None:
    assert ema([1.0, 2.0, 3.0, 4.0], 3) == pytest.approx(3.0)
    assert ema([5.0, 5.0], 3) is None
    highs = [10.0, 12.0, 11.0, 13.0]
    lows = [10.0, 9.0, 10.0, 10.0]
    closes = [10.0, 11.0, 10.5, 12.0]
    atr, adx = wilder_atr_adx(highs, lows, closes, 2)
    assert atr == pytest.approx(2.5)
    assert adx is not None
    assert prior_window([1.0, 2.0, 3.0, 4.0], 2) == [2.0, 3.0]
    assert planned_stop_risk_fraction(20.0, 100.0, 4.0, 2000.0) == pytest.approx(0.0004)


def test_paper_and_dry_run_cli_select_trend_breakout(tmp_path) -> None:
    csv_path = tmp_path / "btcusd_4h.csv"
    save_csv(_rising(12), csv_path)
    paper = CliRunner().invoke(
        app,
        [
            "paper",
            "--strategy",
            "trend_breakout_v1",
            "--data",
            str(csv_path),
            "--state",
            str(tmp_path / "paper_trend.sqlite"),
            "--stop-atr",
            "2",
            "--round-trip-cost",
            "0.003",
        ],
    )
    assert paper.exit_code == 0, paper.output
    assert "strategy=trend_breakout_v1" in paper.output

    dry = CliRunner().invoke(
        app,
        [
            "dry-run",
            "--strategy",
            "trend_breakout_v1",
            "--data",
            str(csv_path),
            "--state",
            str(tmp_path / "dry_trend.sqlite"),
            "--per-trade-pct",
            "0.005",
        ],
    )
    assert dry.exit_code == 0, dry.output
    assert "strategy=trend_breakout_v1" in dry.output
    assert "orders_sent=0" in dry.output
