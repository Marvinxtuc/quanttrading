from __future__ import annotations

from collections import deque
from datetime import timezone
from statistics import fmean, pstdev
from typing import Any

from quanttrading.market import Bar
from quanttrading.signals import Intent, QtyUnit, RiskUtilization, Side, Signal
from quanttrading.strategy.base import Strategy, StrategyContext

DEFAULT_STRATEGY_ID = "sma_cross_v2"
DEFAULT_FAST = 10
DEFAULT_SLOW = 30
DEFAULT_VOL_WINDOW = 20
DEFAULT_MIN_VOL = 0.0005
DEFAULT_SLIPPAGE_BPS = 5.0

# Paper-only 15m and 5m variants. Same 10/30 cross as v2; relaxed open filter.
# ``quanttrading live`` stays on ``sma_cross_v2``.
HF_VOL_WINDOW = 16
HF_MIN_VOL = 0.0002
SMA_CROSS_15M_STRATEGY_ID = "sma_cross_15m_v1"
SMA_CROSS_5M_STRATEGY_ID = "sma_cross_5m_v1"


def realized_vol(closes: list[float], window: int) -> float | None:
    """Population std of simple close-to-close returns over ``window`` returns.

    Needs ``window + 1`` closes. Returns ``None`` until that warm-up is met.
    """
    if window < 2 or len(closes) < window + 1:
        return None
    sample = closes[-(window + 1) :]
    returns = [sample[i] / sample[i - 1] - 1.0 for i in range(1, len(sample))]
    return pstdev(returns)


class SMACrossover(Strategy):
    """Default paper strategy: long-only fast/slow SMA crossover + vol filter.

    Intended as the smoke-test / first paper strategy on a single symbol
    (CLI default: Kraken BTC/USD). Deterministic given the same bar sequence
    and ``StrategyContext``. At most one Signal per bar. Risk hard-stops
    (1% / 3% / 20%) live in the execution layer; this strategy only sizes
    opens at ``equity * per_trade_pct``.

    Rules
    -----
    * Warm-up: no crossover signal until ``slow + 1`` closes are available
      (current and previous SMA). Halt-flatten does not wait for warm-up.
    * Fast SMA crosses above slow SMA while flat → market buy, intent=open,
      qty in quote = equity * per_trade_pct, **only if** realized vol is
      known and ``>= min_vol``. Opens in dead/chop markets are skipped.
    * Fast SMA crosses below slow SMA while long → market sell, intent=close,
      qty in base = current position. Closes are **not** vol-filtered so
      exits still fire in quiet regimes.
    * If ``ctx.halted`` and long → flatten (close). Otherwise no signal.

    Volatility proxy
    ----------------
    ``realized_vol`` = population std of simple close-to-close returns over
    ``vol_window`` returns (needs ``vol_window + 1`` closes). Primary rule:
    skip **opens** when vol is missing or ``vol < min_vol``. No high-vol cap.
    """

    def __init__(
        self,
        fast: int = DEFAULT_FAST,
        slow: int = DEFAULT_SLOW,
        *,
        vol_window: int = DEFAULT_VOL_WINDOW,
        min_vol: float = DEFAULT_MIN_VOL,
        strategy_id: str = DEFAULT_STRATEGY_ID,
        max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    ) -> None:
        if fast < 2 or slow <= fast:
            raise ValueError("require 2 <= fast < slow")
        if vol_window < 2:
            raise ValueError("vol_window must be >= 2")
        if min_vol < 0:
            raise ValueError("min_vol must be >= 0")
        if max_slippage_bps < 0:
            raise ValueError("max_slippage_bps must be >= 0")
        self.fast = fast
        self.slow = slow
        self.vol_window = vol_window
        self.min_vol = min_vol
        self.strategy_id = strategy_id
        self.max_slippage_bps = max_slippage_bps
        self._closes: deque[float] = deque(maxlen=max(slow, vol_window) + 1)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        self._closes.append(bar.close)
        long = ctx.position_qty > 1e-12

        if ctx.halted:
            if long:
                return self._signal(bar, ctx, side="sell", intent="close", qty=abs(ctx.position_qty), qty_unit="base")
            return None

        if len(self._closes) < self.slow + 1:
            return None

        closes = list(self._closes)
        fast_now = fmean(closes[-self.fast :])
        slow_now = fmean(closes[-self.slow :])
        fast_prev = fmean(closes[-self.fast - 1 : -1])
        slow_prev = fmean(closes[-self.slow - 1 : -1])
        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        vol = realized_vol(closes, self.vol_window)
        vol_ok = vol is not None and vol >= self.min_vol
        extra = {
            "fast_sma": fast_now,
            "slow_sma": slow_now,
            "fast_sma_prev": fast_prev,
            "slow_sma_prev": slow_prev,
            "realized_vol": vol,
            "vol_ok": vol_ok,
        }

        if crossed_up and not long:
            if not vol_ok:
                return None
            qty_quote = max(ctx.equity * ctx.per_trade_pct, 0.0)
            if qty_quote <= 0:
                return None
            return self._signal(
                bar,
                ctx,
                side="buy",
                intent="open",
                qty=qty_quote,
                qty_unit="quote",
                extra=extra,
            )
        if crossed_down and long:
            return self._signal(
                bar,
                ctx,
                side="sell",
                intent="close",
                qty=abs(ctx.position_qty),
                qty_unit="base",
                extra=extra,
            )
        return None

    def _signal(
        self,
        bar: Bar,
        ctx: StrategyContext,
        *,
        side: Side,
        intent: Intent,
        qty: float,
        qty_unit: QtyUnit,
        extra: dict[str, Any] | None = None,
    ) -> Signal:
        meta: dict[str, Any] = {
            "fast": self.fast,
            "slow": self.slow,
            "vol_window": self.vol_window,
            "min_vol": self.min_vol,
            "vol_filter": "min",
            "close": bar.close,
        }
        if extra:
            meta.update(extra)
        return Signal(
            strategy_id=self.strategy_id,
            ts=bar.ts.astimezone(timezone.utc),
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


DefaultStrategy = SMACrossover


def default_strategy(
    *,
    fast: int = DEFAULT_FAST,
    slow: int = DEFAULT_SLOW,
    vol_window: int = DEFAULT_VOL_WINDOW,
    min_vol: float = DEFAULT_MIN_VOL,
    max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> SMACrossover:
    """Factory for the default paper/backtest strategy (SMA cross + min-vol filter)."""
    return SMACrossover(
        fast=fast,
        slow=slow,
        vol_window=vol_window,
        min_vol=min_vol,
        max_slippage_bps=max_slippage_bps,
    )


def _hf_sma(
    strategy_id: str,
    *,
    fast: int,
    slow: int,
    vol_window: int,
    min_vol: float,
    max_slippage_bps: float,
) -> SMACrossover:
    return SMACrossover(
        fast=fast,
        slow=slow,
        vol_window=vol_window,
        min_vol=min_vol,
        strategy_id=strategy_id,
        max_slippage_bps=max_slippage_bps,
    )


def sma_cross_15m_strategy(
    *,
    fast: int = DEFAULT_FAST,
    slow: int = DEFAULT_SLOW,
    vol_window: int = HF_VOL_WINDOW,
    min_vol: float = HF_MIN_VOL,
    max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> SMACrossover:
    """15m paper variant: SMA 10/30 cross, ``vol_window=16``, ``min_vol=0.0002``.

    Same Signal → submit rules as ``sma_cross_v2``. Opens still need realized
    vol ``>= min_vol``; closes are not filtered. Not enabled for live orders.
    """
    return _hf_sma(
        SMA_CROSS_15M_STRATEGY_ID,
        fast=fast,
        slow=slow,
        vol_window=vol_window,
        min_vol=min_vol,
        max_slippage_bps=max_slippage_bps,
    )


def sma_cross_5m_strategy(
    *,
    fast: int = DEFAULT_FAST,
    slow: int = DEFAULT_SLOW,
    vol_window: int = HF_VOL_WINDOW,
    min_vol: float = HF_MIN_VOL,
    max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> SMACrossover:
    """5m paper variant: SMA 10/30 cross, ``vol_window=16``, ``min_vol=0.0002``.

    Same Signal → submit rules as ``sma_cross_v2``. Opens still need realized
    vol ``>= min_vol``; closes are not filtered. Not enabled for live orders.
    """
    return _hf_sma(
        SMA_CROSS_5M_STRATEGY_ID,
        fast=fast,
        slow=slow,
        vol_window=vol_window,
        min_vol=min_vol,
        max_slippage_bps=max_slippage_bps,
    )
