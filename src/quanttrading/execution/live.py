from __future__ import annotations

from quanttrading.execution.base import ExecutionReport
from quanttrading.market import Bar
from quanttrading.signals import Signal


class LiveBroker:
    """Live execution is an interface stub only. No keys, no orders.

    ``submit`` always raises. There is no mode that places exchange orders.
    Pre-live reconcile is ``DryRunBroker`` (public market metadata only).
    """

    def submit(self, signal: Signal, bar: Bar | None = None) -> ExecutionReport:
        raise RuntimeError(
            "Live execution is disabled in this MVP. Use PaperBroker. "
            "No exchange API keys are loaded by design."
        )

    def mark_to_market(self, bar: Bar) -> float:
        raise RuntimeError("Live execution is disabled in this MVP.")

    def equity(self) -> float:
        raise RuntimeError("Live execution is disabled in this MVP.")

    def position_qty(self, symbol: str) -> float:
        raise RuntimeError("Live execution is disabled in this MVP.")
