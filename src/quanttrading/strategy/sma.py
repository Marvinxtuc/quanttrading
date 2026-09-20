from __future__ import annotations

from collections import deque
from datetime import timezone
from statistics import fmean
from typing import Any

from quanttrading.market import Bar
from quanttrading.signals import Intent, QtyUnit, RiskUtilization, Side, Signal
from quanttrading.strategy.base import Strategy, StrategyContext

DEFAULT_STRATEGY_ID = "sma_cross_v1"
DEFAULT_FAST = 10
DEFAULT_SLOW = 30
DEFAULT_SLIPPAGE_BPS = 5.0


class SMACrossover(Strategy):
    """Default paper strategy: long-only fast/slow SMA crossover.

    Intended as the smoke-test / first paper strategy on a single symbol
    (CLI default: BTC/USDT). Deterministic given the same bar sequence and
    ``StrategyContext``. At most one Signal per bar. Risk hard-stops (1% /
    3% / 20%) live in the execution layer; this strategy only sizes opens at
    ``equity * per_trade_pct``.

    Rules
    -----
    * Warm-up: no crossover signal until ``slow + 1`` closes are available
      (current and previous SMA). Halt-flatten does not wait for warm-up.
    * Fast SMA crosses above slow SMA while flat → market buy, intent=open,
      qty in quote = equity * per_trade_pct.
    * Fast SMA crosses below slow SMA while long → market sell, intent=close,
      qty in base = current position.
    * If ``ctx.halted`` and long → flatten (close). Otherwise no signal.
    """

    def __init__(
        self,
        fast: int = DEFAULT_FAST,
        slow: int = DEFAULT_SLOW,
        *,
        strategy_id: str = DEFAULT_STRATEGY_ID,
        max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    ) -> None:
        if fast < 2 or slow <= fast:
            raise ValueError("require 2 <= fast < slow")
        if max_slippage_bps < 0:
            raise ValueError("max_slippage_bps must be >= 0")
        self.fast = fast
        self.slow = slow
        self.strategy_id = strategy_id
        self.max_slippage_bps = max_slippage_bps
        self._closes: deque[float] = deque(maxlen=slow + 1)

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
        extra = {
            "fast_sma": fast_now,
            "slow_sma": slow_now,
            "fast_sma_prev": fast_prev,
            "slow_sma_prev": slow_prev,
        }

        if crossed_up and not long:
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
        meta: dict[str, Any] = {"fast": self.fast, "slow": self.slow, "close": bar.close}
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
    max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> SMACrossover:
    """Factory for the default paper/backtest strategy."""
    return SMACrossover(fast=fast, slow=slow, max_slippage_bps=max_slippage_bps)
