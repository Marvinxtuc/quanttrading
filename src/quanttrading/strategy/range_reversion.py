"""Paper-only long range reversion on closed 4h bars.

The decision is made when a 4h bar closes. The order is emitted on the next
bar and the broker fills that bar's open. ``signal_ts`` is the closed bar.
``exec_ts`` is the fill bar. Live trading does not select this strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import fmean, pstdev
from typing import Any, Literal

from quanttrading.market import Bar, utc_iso
from quanttrading.signals import Intent, QtyUnit, RiskUtilization, Side, Signal
from quanttrading.strategy.base import Strategy, StrategyContext
from quanttrading.strategy.indicators import ema, prior_window, wilder_atr_adx
from quanttrading.strategy.trend_breakout import planned_stop_risk_fraction

RANGE_REVERSION_STRATEGY_ID = "range_reversion_v2"

# First-version defaults from research brief v0. Not fitted and not verified
# against a backtest. See STRATEGY.md.
DEFAULT_EMA_FAST = 50
DEFAULT_EMA_SLOW = 200
DEFAULT_ATR_PERIOD = 14
DEFAULT_ADX_PERIOD = 14
# Entries require ADX below this. trend_breakout_v1 requires ADX >= 25.
# The band [18, 25) takes no new entries from either strategy.
DEFAULT_ADX_MAX_ENTRY = 18.0
DEFAULT_ADX_TREND_EXIT = 25.0
DEFAULT_EMA_COMPRESSION = 0.01
DEFAULT_Z_LOOKBACK = 48
DEFAULT_ENTRY_Z = 2.0
DEFAULT_STOP_ATR = 1.0
DEFAULT_STOP_Z = 1.0
DEFAULT_ROUND_TRIP_COST = 0.003
DEFAULT_MIN_RR_AFTER_COST = 1.0
DEFAULT_MAX_HOLD_BARS = 18
DEFAULT_COOLDOWN_BARS = 6
DEFAULT_SLIPPAGE_BPS = 5.0
ASSUMED_TIMEFRAME = "4h"
ASSUMED_BAR_DELTA = timedelta(hours=4)

_EPS = 1e-12


def population_z(
    closes: list[float],
    lookback: int,
) -> tuple[float, float, float, float] | None:
    """Mean and population std of the prior ``lookback`` closes (excluding last).

    Returns ``(mean, std, z_prev, z_curr)`` where both z values use that same
    window. ``None`` until the window exists. ``None`` when std is 0 (skip).
    """
    window = prior_window(closes, lookback)
    if window is None or len(closes) < 2:
        return None
    mean = fmean(window)
    std = pstdev(window)
    if std <= _EPS:
        return None
    z_prev = (closes[-2] - mean) / std
    z_curr = (closes[-1] - mean) / std
    return mean, std, z_prev, z_curr


def stop_distance(atr: float, std: float, stop_atr: float, stop_z: float) -> float:
    """Locked stop distance: ``max(stop_atr * ATR, stop_z * std)``."""
    return max(stop_atr * atr, stop_z * std)


def evaluate_env_gate(
    *,
    adx: float,
    ema_fast: float,
    ema_slow: float,
    adx_max_entry: float,
    ema_compression: float,
) -> str | None:
    """Return a reject reason, or ``None`` when the range environment holds."""
    if ema_slow <= _EPS:
        return "ema"
    if adx >= adx_max_entry:
        return "adx"
    if abs(ema_fast / ema_slow - 1.0) >= ema_compression:
        return "compression"
    return None


def evaluate_entry(
    *,
    close: float,
    mean: float,
    std: float,
    z_prev: float,
    z_curr: float,
    atr: float,
    adx: float,
    ema_fast: float,
    ema_slow: float,
    adx_max_entry: float,
    ema_compression: float,
    entry_z: float,
    stop_atr: float,
    stop_z: float,
    round_trip_cost: float,
    min_rr_after_cost: float,
) -> str | None:
    """Return a reject reason, or ``None`` when every entry rule holds.

    ``close`` is the signal bar. Mean/std are from the prior ``z_lookback``
    closes excluding this bar. The cost / RR check uses this close as the
    entry proxy because the next open is not known yet. Target is the locked
    mean. Stop distance is ``max(stop_atr * atr, stop_z * std)``.
    """
    env = evaluate_env_gate(
        adx=adx,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        adx_max_entry=adx_max_entry,
        ema_compression=ema_compression,
    )
    if env is not None:
        return env
    if atr <= 0.0 or close <= 0.0 or std <= _EPS:
        return "atr"
    if not (z_prev < -entry_z and z_curr >= -entry_z and close < mean):
        return "z_recovery"
    distance = stop_distance(atr, std, stop_atr, stop_z)
    if distance <= 0.0:
        return "stop"
    upside = (mean - close) / close
    downside = distance / close
    cost = round_trip_cost
    if upside <= cost:
        return "cost"
    if (upside - cost) / (downside + cost) < min_rr_after_cost:
        return "rr"
    return None


def has_bar_gap(timestamps: list[datetime], lookback: int, expected: timedelta) -> bool:
    """True when any consecutive pair in the last ``lookback`` steps is not ``expected`` apart."""
    if lookback < 1 or len(timestamps) < lookback + 1:
        return False
    window = timestamps[-(lookback + 1) :]
    for earlier, later in zip(window, window[1:]):
        if later - earlier != expected:
            return True
    return False


@dataclass
class _Pending:
    kind: Literal["entry", "exit"]
    signal_ts: datetime
    reason: str
    atr: float
    close: float
    mean: float
    std: float
    extra: dict[str, Any]


class RangeReversion(Strategy):
    """Long-only 4h range reversion. At most one Signal per bar. Market orders only.

    Callers pass closed 4h bars in order. The strategy does not resample and
    does not know the clock; a 1h CSV would be treated as if each row were 4h.

    Timing
    ------
    A setup on closed bar T is stored. The Signal is returned on bar T+1.
    ``ts`` and ``meta['exec_ts']`` are T+1. ``meta['signal_ts']`` is T.
    ``meta['fill_on']`` is ``open``. The broker fills T+1's open and rejects
    the same Signal if it is submitted on any other bar.

    Environment (all must hold to accept an entry)
    ----------------------------------------------
    1. ADX14 < 18
    2. abs(EMA50 / EMA200 - 1) < 0.01
    3. Indicators warmed; skip when std == 0 or a 4h gap sits in the z window

    ADX in [18, 25) takes no new entries here. ``trend_breakout_v1`` also takes
    none there (it needs ADX >= 25). That band is intentionally quiet.

    Entry (flat only, no pending buy)
    ---------------------------------
    Mean and population std from the prior 48 closed closes excluding the
    signal bar. ``z_prev`` and ``z_curr`` use that same window.
    Trigger: z_prev < -2 AND z_curr >= -2 AND close < mean.
    Then the cost / RR filter must pass (signal close as entry proxy).

    Exit (decided on a closed bar, filled on the next open)
    -------------------------------------------------------
    * Target: bar high reaches the mean locked at the signal (not chased).
    * Stop: bar low reaches the locked initial stop (no add, no trail).
      Distance is ``max(1.0 * ATR14, 1.0 * std)`` from the signal bar.
    * Max hold: 18 closed bars in the trade without the target.
    * Regime: ADX14 >= 25 AND close < EMA50.
    * Halt: the execution flag schedules the same next-bar flatten.

    Cooldown is ``cooldown_bars`` closed bars after the exit fill.
    """

    def __init__(
        self,
        *,
        ema_fast: int = DEFAULT_EMA_FAST,
        ema_slow: int = DEFAULT_EMA_SLOW,
        atr_period: int = DEFAULT_ATR_PERIOD,
        adx_period: int = DEFAULT_ADX_PERIOD,
        adx_max_entry: float = DEFAULT_ADX_MAX_ENTRY,
        adx_trend_exit: float = DEFAULT_ADX_TREND_EXIT,
        ema_compression: float = DEFAULT_EMA_COMPRESSION,
        z_lookback: int = DEFAULT_Z_LOOKBACK,
        entry_z: float = DEFAULT_ENTRY_Z,
        stop_atr: float = DEFAULT_STOP_ATR,
        stop_z: float = DEFAULT_STOP_Z,
        round_trip_cost: float = DEFAULT_ROUND_TRIP_COST,
        min_rr_after_cost: float = DEFAULT_MIN_RR_AFTER_COST,
        max_hold_bars: int = DEFAULT_MAX_HOLD_BARS,
        cooldown_bars: int = DEFAULT_COOLDOWN_BARS,
        strategy_id: str = RANGE_REVERSION_STRATEGY_ID,
        max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        bar_delta: timedelta = ASSUMED_BAR_DELTA,
    ) -> None:
        if ema_fast < 2 or ema_slow <= ema_fast:
            raise ValueError("require 2 <= ema_fast < ema_slow")
        if atr_period < 1 or adx_period < 1:
            raise ValueError("atr_period and adx_period must be >= 1")
        if adx_max_entry < 0.0 or adx_trend_exit <= adx_max_entry:
            raise ValueError("require 0 <= adx_max_entry < adx_trend_exit")
        if ema_compression <= 0.0:
            raise ValueError("ema_compression must be > 0")
        if z_lookback < 2:
            raise ValueError("z_lookback must be >= 2")
        if entry_z <= 0.0:
            raise ValueError("entry_z must be > 0")
        if stop_atr <= 0.0:
            raise ValueError("stop_atr must be > 0")
        if stop_z <= 0.0:
            raise ValueError("stop_z must be > 0")
        if round_trip_cost < 0.0:
            raise ValueError("round_trip_cost must be >= 0")
        if min_rr_after_cost <= 0.0:
            raise ValueError("min_rr_after_cost must be > 0")
        if max_hold_bars < 1:
            raise ValueError("max_hold_bars must be >= 1")
        if cooldown_bars < 0:
            raise ValueError("cooldown_bars must be >= 0")
        if max_slippage_bps < 0.0:
            raise ValueError("max_slippage_bps must be >= 0")
        if bar_delta <= timedelta(0):
            raise ValueError("bar_delta must be > 0")
        if not strategy_id:
            raise ValueError("strategy_id must be non-empty")
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.adx_max_entry = adx_max_entry
        self.adx_trend_exit = adx_trend_exit
        self.ema_compression = ema_compression
        self.z_lookback = z_lookback
        self.entry_z = entry_z
        self.stop_atr = stop_atr
        self.stop_z = stop_z
        self.round_trip_cost = round_trip_cost
        self.min_rr_after_cost = min_rr_after_cost
        self.max_hold_bars = max_hold_bars
        self.cooldown_bars = cooldown_bars
        self.strategy_id = strategy_id
        self.max_slippage_bps = max_slippage_bps
        self.bar_delta = bar_delta
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._timestamps: list[datetime] = []
        self._pending: _Pending | None = None
        self._in_trade = False
        self._entry_price: float | None = None
        self._target: float | None = None
        self._stop: float | None = None
        self._initial_stop_distance: float | None = None
        self._bars_held = 0
        self._cooldown_left = 0

    @property
    def stop(self) -> float | None:
        """Locked long stop. ``None`` when flat. Does not trail."""
        return self._stop

    @property
    def target(self) -> float | None:
        """Locked mean target from the signal bar. ``None`` when flat."""
        return self._target

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        if self._in_trade and ctx.position_qty <= _EPS:
            self._reset_trade()
            if self._pending is not None and self._pending.kind == "exit":
                self._pending = None

        emitted: Signal | None = None
        exited_now = False
        if self._pending is not None:
            pending = self._pending
            self._pending = None
            if pending.kind == "entry":
                emitted = self._emit_entry(bar, ctx, pending)
            else:
                emitted = self._emit_exit(bar, ctx, pending)
                exited_now = True

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._timestamps.append(bar.ts)

        if self._in_trade:
            self._bars_held += 1
            reason = self._exit_reason(bar, ctx)
            if reason is not None:
                self._arm_exit(bar, reason)
        elif not exited_now:
            if self._cooldown_left > 0:
                self._cooldown_left -= 1
            else:
                self._maybe_arm_entry(bar, ctx)
        return emitted

    def _emit_entry(self, bar: Bar, ctx: StrategyContext, pending: _Pending) -> Signal | None:
        if ctx.halted or ctx.position_qty > _EPS:
            return None
        qty = max(ctx.equity * ctx.per_trade_pct, 0.0)
        if qty <= 0.0 or pending.atr <= 0.0 or pending.std <= _EPS:
            return None
        entry = _slipped(bar.open, "buy", self.max_slippage_bps)
        distance = stop_distance(pending.atr, pending.std, self.stop_atr, self.stop_z)
        self._in_trade = True
        self._entry_price = entry
        self._target = pending.mean
        self._initial_stop_distance = distance
        self._stop = entry - distance
        self._bars_held = 0
        extra = dict(pending.extra)
        extra.update(
            {
                "entry_price": entry,
                "target": self._target,
                "initial_stop_distance": distance,
                "stop": self._stop,
                "planned_stop_risk_fraction": planned_stop_risk_fraction(
                    qty, entry, distance, ctx.equity
                ),
            }
        )
        return self._signal(
            bar,
            ctx,
            pending=pending,
            side="buy",
            intent="open",
            qty=qty,
            qty_unit="quote",
            extra=extra,
        )

    def _emit_exit(self, bar: Bar, ctx: StrategyContext, pending: _Pending) -> Signal | None:
        qty = abs(ctx.position_qty)
        if qty <= _EPS:
            self._reset_trade()
            return None
        signal = self._signal(
            bar,
            ctx,
            pending=pending,
            side="sell",
            intent="close",
            qty=qty,
            qty_unit="base",
            extra=dict(pending.extra),
        )
        self._reset_trade()
        self._cooldown_left = self.cooldown_bars
        return signal

    def _maybe_arm_entry(self, bar: Bar, ctx: StrategyContext) -> None:
        if ctx.halted or ctx.position_qty > _EPS or self._in_trade or self._pending is not None:
            return
        if has_bar_gap(self._timestamps, self.z_lookback, self.bar_delta):
            return
        snap = self._snapshot()
        if snap is None:
            return
        block = evaluate_entry(
            close=bar.close,
            mean=snap["mean"],
            std=snap["std"],
            z_prev=snap["z_prev"],
            z_curr=snap["z_curr"],
            atr=snap["atr"],
            adx=snap["adx"],
            ema_fast=snap["ema_fast"],
            ema_slow=snap["ema_slow"],
            adx_max_entry=self.adx_max_entry,
            ema_compression=self.ema_compression,
            entry_z=self.entry_z,
            stop_atr=self.stop_atr,
            stop_z=self.stop_z,
            round_trip_cost=self.round_trip_cost,
            min_rr_after_cost=self.min_rr_after_cost,
        )
        if block is not None:
            return
        self._pending = _Pending(
            kind="entry",
            signal_ts=bar.ts,
            reason="z_recovery",
            atr=snap["atr"],
            close=bar.close,
            mean=snap["mean"],
            std=snap["std"],
            extra=snap,
        )

    def _arm_exit(self, bar: Bar, reason: str) -> None:
        atr, adx = wilder_atr_adx(self._highs, self._lows, self._closes, self.atr_period)
        if self.atr_period != self.adx_period:
            atr, _ignored = wilder_atr_adx(self._highs, self._lows, self._closes, self.atr_period)
            _ignored_atr, adx = wilder_atr_adx(self._highs, self._lows, self._closes, self.adx_period)
        fast = ema(self._closes, self.ema_fast)
        self._pending = _Pending(
            kind="exit",
            signal_ts=bar.ts,
            reason=reason,
            atr=atr or 0.0,
            close=bar.close,
            mean=self._target or 0.0,
            std=0.0,
            extra={
                "reason": reason,
                "stop": self._stop,
                "target": self._target,
                "entry_price": self._entry_price,
                "initial_stop_distance": self._initial_stop_distance,
                "bars_held": self._bars_held,
                "atr": atr,
                "adx": adx,
                "ema_fast": fast,
            },
        )

    def _exit_reason(self, bar: Bar, ctx: StrategyContext) -> str | None:
        if ctx.halted:
            return "halt"
        # Stop before target when the same bar spans both (path unknown).
        if self._stop is not None and bar.low <= self._stop:
            return "stop"
        if self._target is not None and bar.high >= self._target:
            return "target"
        if self._bars_held >= self.max_hold_bars:
            return "max_hold"
        _atr, adx = wilder_atr_adx(self._highs, self._lows, self._closes, self.adx_period)
        fast = ema(self._closes, self.ema_fast)
        if adx is not None and fast is not None and adx >= self.adx_trend_exit and bar.close < fast:
            return "regime"
        return None

    def _snapshot(self) -> dict[str, float] | None:
        fast = ema(self._closes, self.ema_fast)
        slow = ema(self._closes, self.ema_slow)
        atr, adx = wilder_atr_adx(self._highs, self._lows, self._closes, self.adx_period)
        if self.atr_period != self.adx_period:
            atr, _ignored = wilder_atr_adx(self._highs, self._lows, self._closes, self.atr_period)
        zstats = population_z(self._closes, self.z_lookback)
        if fast is None or slow is None or atr is None or adx is None or zstats is None:
            return None
        mean, std, z_prev, z_curr = zstats
        close = self._closes[-1]
        distance = stop_distance(atr, std, self.stop_atr, self.stop_z)
        upside = (mean - close) / close if close > 0.0 else 0.0
        downside = distance / close if close > 0.0 else 0.0
        cost = self.round_trip_cost
        rr = (upside - cost) / (downside + cost) if (downside + cost) > 0.0 else 0.0
        return {
            "ema_fast": fast,
            "ema_slow": slow,
            "atr": atr,
            "adx": adx,
            "mean": mean,
            "std": std,
            "z_prev": z_prev,
            "z_curr": z_curr,
            "close": close,
            "target": mean,
            "initial_stop_distance": distance,
            "upside": upside,
            "downside": downside,
            "rr_after_cost": rr,
            "assumed_round_trip_cost_pct": self.round_trip_cost,
            "ema_compression": abs(fast / slow - 1.0) if slow > _EPS else 0.0,
            "stop_z": self.stop_z,
        }

    def _signal(
        self,
        bar: Bar,
        ctx: StrategyContext,
        *,
        pending: _Pending,
        side: Side,
        intent: Intent,
        qty: float,
        qty_unit: QtyUnit,
        extra: dict[str, Any],
    ) -> Signal:
        meta: dict[str, Any] = {
            "signal_ts": utc_iso(pending.signal_ts),
            "exec_ts": utc_iso(bar.ts),
            "fill_on": "open",
            "timeframe": ASSUMED_TIMEFRAME,
            "reason": pending.reason,
            "signal_close": pending.close,
            "ema_fast_period": self.ema_fast,
            "ema_slow_period": self.ema_slow,
            "atr_period": self.atr_period,
            "adx_period": self.adx_period,
            "adx_max_entry": self.adx_max_entry,
            "adx_trend_exit": self.adx_trend_exit,
            "ema_compression_max": self.ema_compression,
            "z_lookback": self.z_lookback,
            "entry_z": self.entry_z,
            "stop_atr": self.stop_atr,
            "stop_z": self.stop_z,
            "assumed_round_trip_cost_pct": self.round_trip_cost,
            "min_rr_after_cost": self.min_rr_after_cost,
            "max_hold_bars": self.max_hold_bars,
            "cooldown_bars": self.cooldown_bars,
        }
        meta.update(extra)
        return Signal(
            strategy_id=self.strategy_id,
            ts=bar.ts,
            symbol=bar.symbol,
            side=side,
            intent=intent,
            qty=qty,
            qty_unit=qty_unit,
            order_type="market",
            limit_price=None,
            strength=1.0,
            max_slippage_bps=self.max_slippage_bps,
            risk=RiskUtilization(
                per_trade_pct=ctx.per_trade_util,
                daily_dd_pct=ctx.daily_dd_util,
                total_dd_pct=ctx.total_dd_util,
            ),
            client_order_id=self._client_order_id(bar, intent, side),
            meta=meta,
        )

    def _client_order_id(self, bar: Bar, intent: str, side: str) -> str:
        ts = bar.ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sym = bar.symbol.replace("/", "").replace(":", "")
        return f"{self.strategy_id}-{sym}-{ts}-{intent}-{side}"

    def _reset_trade(self) -> None:
        self._in_trade = False
        self._entry_price = None
        self._target = None
        self._stop = None
        self._initial_stop_distance = None
        self._bars_held = 0


def _slipped(price: float, side: str, slippage_bps: float) -> float:
    slip = slippage_bps / 10_000.0
    if side == "buy":
        return price * (1.0 + slip)
    return price * (1.0 - slip)


def range_reversion_strategy(
    *,
    stop_atr: float = DEFAULT_STOP_ATR,
    round_trip_cost: float = DEFAULT_ROUND_TRIP_COST,
    max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> RangeReversion:
    """Factory for the paper/dry-run 4h range reversion. Not used by ``quanttrading live``."""
    return RangeReversion(
        stop_atr=stop_atr,
        round_trip_cost=round_trip_cost,
        max_slippage_bps=max_slippage_bps,
    )
