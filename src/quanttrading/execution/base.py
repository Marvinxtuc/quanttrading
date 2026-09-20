from __future__ import annotations

from typing import Protocol

from quanttrading.market import Bar
from quanttrading.signals import Signal


class ExecutionReport:
    __slots__ = ("status", "reason", "fill_price", "qty_base", "notional", "client_order_id", "equity")

    def __init__(
        self,
        *,
        status: str,
        client_order_id: str,
        reason: str | None = None,
        fill_price: float | None = None,
        qty_base: float | None = None,
        notional: float | None = None,
        equity: float | None = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.fill_price = fill_price
        self.qty_base = qty_base
        self.notional = notional
        self.client_order_id = client_order_id
        self.equity = equity

    def __repr__(self) -> str:
        return (
            f"ExecutionReport(status={self.status!r}, reason={self.reason!r}, "
            f"fill_price={self.fill_price}, qty_base={self.qty_base}, equity={self.equity})"
        )


class ExecutionBackend(Protocol):
    """Shared signal → execution interface. Paper and live differ only behind this."""

    def submit(self, signal: Signal, bar: Bar) -> ExecutionReport: ...

    def mark_to_market(self, bar: Bar) -> float: ...

    def equity(self) -> float: ...

    def position_qty(self, symbol: str) -> float: ...
