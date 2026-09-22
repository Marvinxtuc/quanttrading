"""Paper-only long breakout on closed 4h bars.

The decision is made when a 4h bar closes. The order is emitted on the next
bar and the broker fills that bar's open. ``signal_ts`` is the closed bar.
``exec_ts`` is the fill bar. Live trading does not select this strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from quanttrading.market import Bar, utc_iso
from quanttrading.signals import Intent, QtyUnit, RiskUtilization, Side, Signal
from quanttrading.strategy.base import Strategy, StrategyContext
from quanttrading.strategy.indicators import ema, prior_window, wilder_atr_adx

TREND_BREAKOUT_STRATEGY_ID = "trend_breakout_v1"

# First-version defaults from research brief v0. Not fitted and not verified
# against a backtest. See STRATEGY.md.
DEFAULT_EMA_FAST = 50
DEFAULT_EMA_SLOW = 200
DEFAULT_ATR_PERIOD = 14
DEFAULT_ADX_PERIOD = 14
DEFAULT_ADX_MIN = 25.0
DEFAULT_BREAKOUT_LOOKBACK = 55
DEFAULT_BREAKOUT_ATR = 0.1
DEFAULT_MAX_CHASE_ATR = 1.0
DEFAULT_STOP_ATR = 2.0
# Midpoint of the brief's example round-trip cost band (0.002–0.004).
# This is an assumption, not a measured Kraken fee.
DEFAULT_ROUND_TRIP_COST = 0.003
DEFAULT_TREND_BREAK_LOOKBACK = 10
DEFAULT_FOLLOW_THROUGH_BARS = 30
DEFAULT_COOLDOWN_BARS = 3
DEFAULT_SLIPPAGE_BPS = 5.0
ASSUMED_TIMEFRAME = "4h"

_EPS = 1e-12


def planned_stop_risk_fraction(
    notional_quote: float,
    entry_price: float,
    stop_distance: float,
    equity: float,
) -> float:
    """Equity fraction lost if the locked stop fills, given a quote-sized open.

    Informational only. Opens are still sized at ``equity * per_trade_pct`` and
    the execution layer still enforces the 1% / 3% / 20% gates. This does not
    raise size.
    """
    if entry_price <= 0.0 or equity <= 0.0 or notional_quote <= 0.0 or stop_distance <= 0.0:
        return 0.0
    qty_base = notional_quote / entry_price
    return (qty_base * stop_distance) / equity


def ratchet_stop(stop: float, close: float, atr: float, k: float) -> float:
    """Long trailing stop. The level never moves down when ATR widens."""
    if atr <= 0.0 or k <= 0.0:
        return stop
    candidate = close - k * atr
    if candidate > stop:
        return candidate
    return stop


def evaluate_entry(
    *,
    close: float,
    ema_fast: float,
    ema_slow: float,
    adx: float,
    atr: float,
    prior_high: float,
    adx_min: float,
    breakout_atr: float,
    max_chase_atr: float,
    stop_atr: float,
    round_trip_cost: float,
) -> str | None:
    """Return a reject reason, or ``None`` when every entry rule holds.

    ``close`` is the signal bar. ``prior_high`` is the max high of the previous
    ``breakout`` bars, excluding this bar. The cost check uses this close as
    ``entry_price`` because the next open is not known yet. Stop distance is
    ``stop_atr * atr`` of this same closed bar.
    """
    if atr <= 0.0 or close <= 0.0:
        return "atr"
    if not (close > ema_slow and ema_fast > ema_slow):
        return "trend"
    if adx < adx_min:
        return "adx"
    extension = close - prior_high
    if not (close > prior_high + breakout_atr * atr):
        return "breakout"
    if extension > max_chase_atr * atr:
        return "chase"
    stop_distance = stop_atr * atr
    if stop_distance / close < 2.0 * round_trip_cost:
        return "cost"
    return None


@dataclass
class _Pending:
    kind: Literal["entry", "exit"]
    signal_ts: datetime
    reason: str
    atr: float
    close: float
    extra: dict[str, Any]


class TrendBreakout(Strategy):
    """Long-only 4h breakout. At most one Signal per bar. Market orders only.

    Callers pass closed 4h bars in order. The strategy does not resample and
    does not know the clock; a 1h CSV would be treated as if each row were 4h.

    Timing
    ------
    A setup on closed bar T is stored. The Signal is returned on bar T+1.
    ``ts`` and ``meta['exec_ts']`` are T+1. ``meta['signal_ts']`` is T.
    ``meta['fill_on']`` is ``open``. The broker fills T+1's open and rejects
    the same Signal if it is submitted on any other bar.

    Entry (all must hold on bar T, while flat, with no pending buy)
    ----------------------------------------------------------------
    1. close > EMA200 and EMA50 > EMA200
    2. ADX14 >= 25
    3. close > max(high of the prior 55 bars) + 0.1 * ATR14
    4. (close - that high) <= 1.0 * ATR14
    5. (k * ATR14) / close >= 2 * assumed round-trip cost
    6. no open position and no buy already waiting for the next bar

    Exit (decided on a closed bar, filled on the next open)
    -------------------------------------------------------
    * Stop: bar low touches the active stop. The initial stop is the fill
      open minus k * ATR14 from the signal bar. It is locked: later ATR
      growth cannot lower it. The trail is ``max(stop, close - k * ATR)``
      and the new level is used starting the next bar.
    * Trend break: close < min(low of the prior 10 bars).
    * No follow-through: after 30 closed bars in the trade, exit if no close
      has reached entry + the initial stop distance. No fixed take-profit
      and no adding to a loser.
    * Halt: the execution flag schedules the same next-bar flatten.

    Cooldown is ``cooldown_bars`` closed bars after the exit fill. Those bars
    cannot form a new entry. The earliest new entry decision is the next bar.
    """

    def __init__(
        self,
        *,
        ema_fast: int = DEFAULT_EMA_FAST,
        ema_slow: int = DEFAULT_EMA_SLOW,
        atr_period: int = DEFAULT_ATR_PERIOD,
        adx_period: int = DEFAULT_ADX_PERIOD,
        adx_min: float = DEFAULT_ADX_MIN,
        breakout_lookback: int = DEFAULT_BREAKOUT_LOOKBACK,
        breakout_atr: float = DEFAULT_BREAKOUT_ATR,
        max_chase_atr: float = DEFAULT_MAX_CHASE_ATR,
        stop_atr: float = DEFAULT_STOP_ATR,
        round_trip_cost: float = DEFAULT_ROUND_TRIP_COST,
        trend_break_lookback: int = DEFAULT_TREND_BREAK_LOOKBACK,
        follow_through_bars: int = DEFAULT_FOLLOW_THROUGH_BARS,
        cooldown_bars: int = DEFAULT_COOLDOWN_BARS,
        strategy_id: str = TREND_BREAKOUT_STRATEGY_ID,
        max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    ) -> None:
        if ema_fast < 2 or ema_slow <= ema_fast:
            raise ValueError("require 2 <= ema_fast < ema_slow")
        if atr_period < 1 or adx_period < 1:
            raise ValueError("atr_period and adx_period must be >= 1")
        if adx_min < 0.0:
            raise ValueError("adx_min must be >= 0")
        if breakout_lookback < 1 or trend_break_lookback < 1:
            raise ValueError("lookbacks must be >= 1")
        if breakout_atr < 0.0 or max_chase_atr <= breakout_atr:
            raise ValueError("require 0 <= breakout_atr < max_chase_atr")
        if stop_atr <= 0.0:
            raise ValueError("stop_atr must be > 0")
        if round_trip_cost < 0.0:
            raise ValueError("round_trip_cost must be >= 0")
        if follow_through_bars < 1:
            raise ValueError("follow_through_bars must be >= 1")
        if cooldown_bars < 0:
            raise ValueError("cooldown_bars must be >= 0")
        if max_slippage_bps < 0.0:
            raise ValueError("max_slippage_bps must be >= 0")
        if not strategy_id:
            raise ValueError("strategy_id must be non-empty")
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.adx_min = adx_min
        self.breakout_lookback = breakout_lookback
        self.breakout_atr = breakout_atr
        self.max_chase_atr = max_chase_atr
        self.stop_atr = stop_atr
        self.round_trip_cost = round_trip_cost
        self.trend_break_lookback = trend_break_lookback
        self.follow_through_bars = follow_through_bars
        self.cooldown_bars = cooldown_bars
        self.strategy_id = strategy_id
        self.max_slippage_bps = max_slippage_bps
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._pending: _Pending | None = None
        self._in_trade = False
        self._entry_price: float | None = None
        self._initial_stop_distance: float | None = None
        self._stop: float | None = None
        self._bars_held = 0
        self._max_close: float | None = None
        self._cooldown_left = 0

    @property
    def stop(self) -> float | None:
        """Active long stop. ``None`` when flat. Never moves down while set."""
        return self._stop

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

        if self._in_trade:
            self._bars_held += 1
            self._max_close = bar.close if self._max_close is None else max(self._max_close, bar.close)
            reason = self._exit_reason(bar, ctx)
            if reason is not None:
                self._arm_exit(bar, reason)
            else:
                self._trail(bar)
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
        if qty <= 0.0 or pending.atr <= 0.0:
            return None
        entry = _slipped(bar.open, "buy", self.max_slippage_bps)
        distance = self.stop_atr * pending.atr
        self._in_trade = True
        self._entry_price = entry
        self._initial_stop_distance = distance
        self._stop = entry - distance
        self._bars_held = 0
        self._max_close = None
        extra = dict(pending.extra)
        extra.update(
            {
                "entry_price": entry,
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
        snap = self._snapshot()
        if snap is None:
            return
        block = evaluate_entry(
            close=bar.close,
            ema_fast=snap["ema_fast"],
            ema_slow=snap["ema_slow"],
            adx=snap["adx"],
            atr=snap["atr"],
            prior_high=snap["prior_high"],
            adx_min=self.adx_min,
            breakout_atr=self.breakout_atr,
            max_chase_atr=self.max_chase_atr,
            stop_atr=self.stop_atr,
            round_trip_cost=self.round_trip_cost,
        )
        if block is not None:
            return
        self._pending = _Pending(
            kind="entry",
            signal_ts=bar.ts,
            reason="breakout",
            atr=snap["atr"],
            close=bar.close,
            extra=snap,
        )

    def _arm_exit(self, bar: Bar, reason: str) -> None:
        atr, _adx = wilder_atr_adx(self._highs, self._lows, self._closes, self.atr_period)
        self._pending = _Pending(
            kind="exit",
            signal_ts=bar.ts,
            reason=reason,
            atr=atr or 0.0,
            close=bar.close,
            extra={
                "reason": reason,
                "stop": self._stop,
                "entry_price": self._entry_price,
                "initial_stop_distance": self._initial_stop_distance,
                "bars_held": self._bars_held,
                "max_close": self._max_close,
                "atr": atr,
            },
        )

    def _exit_reason(self, bar: Bar, ctx: StrategyContext) -> str | None:
        if ctx.halted:
            return "halt"
        if self._stop is not None and bar.low <= self._stop:
            return "stop"
        window = prior_window(self._lows, self.trend_break_lookback)
        if window is not None and bar.close < min(window):
            return "trend_break"
        if (
            self._entry_price is not None
            and self._initial_stop_distance is not None
            and self._max_close is not None
            and self._bars_held >= self.follow_through_bars
            and self._max_close < self._entry_price + self._initial_stop_distance
        ):
            return "no_follow_through"
        return None

    def _trail(self, bar: Bar) -> None:
        if self._stop is None:
            return
        atr, _adx = wilder_atr_adx(self._highs, self._lows, self._closes, self.atr_period)
        if atr is None:
            return
        self._stop = ratchet_stop(self._stop, bar.close, atr, self.stop_atr)

    def _snapshot(self) -> dict[str, float] | None:
        fast = ema(self._closes, self.ema_fast)
        slow = ema(self._closes, self.ema_slow)
        atr, adx = wilder_atr_adx(self._highs, self._lows, self._closes, self.adx_period)
        # ATR period and ADX period are the same knob in the brief (14). If a
        # caller splits them, the stop still uses ``atr_period``.
        if self.atr_period != self.adx_period:
            atr, _ignored = wilder_atr_adx(self._highs, self._lows, self._closes, self.atr_period)
        highs = prior_window(self._highs, self.breakout_lookback)
        if fast is None or slow is None or atr is None or adx is None or highs is None:
            return None
        close = self._closes[-1]
        prior_high = max(highs)
        return {
            "ema_fast": fast,
            "ema_slow": slow,
            "atr": atr,
            "adx": adx,
            "prior_high": prior_high,
            "close": close,
            "extension": close - prior_high,
            "initial_stop_distance": self.stop_atr * atr,
            "cost_ratio": (self.stop_atr * atr) / close,
            "assumed_round_trip_cost_pct": self.round_trip_cost,
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
            "adx_min": self.adx_min,
            "breakout_lookback": self.breakout_lookback,
            "breakout_atr": self.breakout_atr,
            "max_chase_atr": self.max_chase_atr,
            "stop_atr": self.stop_atr,
            "assumed_round_trip_cost_pct": self.round_trip_cost,
            "trend_break_lookback": self.trend_break_lookback,
            "follow_through_bars": self.follow_through_bars,
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
        self._initial_stop_distance = None
        self._stop = None
        self._bars_held = 0
        self._max_close = None


def _slipped(price: float, side: str, slippage_bps: float) -> float:
    slip = slippage_bps / 10_000.0
    if side == "buy":
        return price * (1.0 + slip)
    return price * (1.0 - slip)


def trend_breakout_strategy(
    *,
    stop_atr: float = DEFAULT_STOP_ATR,
    round_trip_cost: float = DEFAULT_ROUND_TRIP_COST,
    max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> TrendBreakout:
    """Factory for the paper/dry-run 4h breakout. Not used by ``quanttrading live``."""
    return TrendBreakout(
        stop_atr=stop_atr,
        round_trip_cost=round_trip_cost,
        max_slippage_bps=max_slippage_bps,
    )
