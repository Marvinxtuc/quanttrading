from __future__ import annotations

from dataclasses import dataclass

from quanttrading.market import Bar
from quanttrading.signals import Signal


@dataclass(frozen=True, slots=True)
class StrategyContext:
    equity: float
    position_qty: float
    per_trade_pct: float
    per_trade_util: float
    daily_dd_util: float
    total_dd_util: float
    halted: bool


class Strategy:
    """Pluggable strategy: one bar in, optional signal out."""

    strategy_id: str

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        raise NotImplementedError
