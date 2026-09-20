from __future__ import annotations

from dataclasses import dataclass

from quanttrading.backtest.metrics import metrics_from_broker
from quanttrading.config import Settings
from quanttrading.execution.paper import PaperBroker
from quanttrading.market import Bar
from quanttrading.strategy.base import Strategy, StrategyContext


@dataclass
class LoopResult:
    broker: PaperBroker
    metrics: dict[str, float | int | str | None]


def _context(broker: PaperBroker, symbol: str) -> StrategyContext:
    util = broker.risk.utilization(broker.equity())
    return StrategyContext(
        equity=broker.equity(),
        position_qty=broker.position_qty(symbol),
        per_trade_pct=broker.risk.limits.per_trade_pct,
        per_trade_util=util.per_trade_pct,
        daily_dd_util=util.daily_dd_pct,
        total_dd_util=util.total_dd_pct,
        halted=broker.risk.halted,
    )


def run_bars(
    bars: list[Bar],
    strategy: Strategy,
    broker: PaperBroker,
) -> LoopResult:
    """Shared bar loop: strategy → Signal → ExecutionBackend.submit."""
    for bar in bars:
        broker.mark_to_market(bar)
        ctx = _context(broker, bar.symbol)
        signal = strategy.on_bar(bar, ctx)
        if signal is not None:
            util = broker.risk.utilization(broker.equity())
            signal = signal.model_copy(update={"risk": util})
            broker.submit(signal, bar)
        broker.mark_to_market(bar)
    return LoopResult(broker=broker, metrics=metrics_from_broker(broker))


def run_backtest(
    bars: list[Bar],
    strategy: Strategy,
    settings: Settings | None = None,
) -> LoopResult:
    settings = settings or Settings()
    broker = PaperBroker(settings, store_path=":memory:")
    return run_bars(bars, strategy, broker)
