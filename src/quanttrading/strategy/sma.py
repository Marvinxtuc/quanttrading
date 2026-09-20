from __future__ import annotations

from collections import deque
from datetime import timezone
from statistics import fmean
from uuid import uuid4

from quanttrading.market import Bar
from quanttrading.signals import RiskUtilization, Signal
from quanttrading.strategy.base import Strategy, StrategyContext


class SMACrossover(Strategy):
    """Long-only fast/slow SMA crossover stub so the pipeline runs end-to-end."""

    def __init__(self, fast: int = 10, slow: int = 30, strategy_id: str = "sma_cross_v1") -> None:
        if fast < 2 or slow <= fast:
            raise ValueError("require 2 <= fast < slow")
        self.fast = fast
        self.slow = slow
        self.strategy_id = strategy_id
        self._closes: deque[float] = deque(maxlen=slow + 1)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        self._closes.append(bar.close)
        if len(self._closes) < self.slow + 1:
            return None
        if ctx.halted:
            if ctx.position_qty > 0:
                return self._signal(bar, ctx, side="sell", intent="close", qty_quote=0.0)
            return None

        closes = list(self._closes)
        fast_now = fmean(closes[-self.fast :])
        slow_now = fmean(closes[-self.slow :])
        fast_prev = fmean(closes[-self.fast - 1 : -1])
        slow_prev = fmean(closes[-self.slow - 1 : -1])
        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        long = ctx.position_qty > 1e-12
        quote_qty = max(ctx.equity * ctx.per_trade_pct, 0.0)

        if crossed_up and not long:
            return self._signal(bar, ctx, side="buy", intent="open", qty_quote=quote_qty)
        if crossed_down and long:
            return self._signal(bar, ctx, side="sell", intent="close", qty_quote=0.0)
        return None

    def _signal(
        self,
        bar: Bar,
        ctx: StrategyContext,
        *,
        side: str,
        intent: str,
        qty_quote: float,
    ) -> Signal:
        if intent == "close":
            qty = abs(ctx.position_qty)
            qty_unit: str = "base"
        else:
            qty = qty_quote
            qty_unit = "quote"
        return Signal(
            strategy_id=self.strategy_id,
            ts=bar.ts.astimezone(timezone.utc),
            symbol=bar.symbol,
            side=side,  # type: ignore[arg-type]
            intent=intent,  # type: ignore[arg-type]
            qty=qty,
            qty_unit=qty_unit,  # type: ignore[arg-type]
            order_type="market",
            limit_price=None,
            strength=1.0,
            max_slippage_bps=5.0,
            risk=RiskUtilization(
                per_trade_pct=ctx.per_trade_util,
                daily_dd_pct=ctx.daily_dd_util,
                total_dd_pct=ctx.total_dd_util,
            ),
            client_order_id=uuid4().hex,
            meta={"fast": self.fast, "slow": self.slow, "close": bar.close},
        )
