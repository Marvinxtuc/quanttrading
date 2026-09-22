from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quanttrading.config import Settings
from quanttrading.execution.base import ExecutionReport
from quanttrading.execution.fills import Fill, estimated_notional, execution_timing_error, simulate_fill
from quanttrading.execution.risk import RiskGate, RiskLimits
from quanttrading.execution.store import SQLiteStore
from quanttrading.market import Bar
from quanttrading.signals import Signal


@dataclass
class Position:
    qty: float = 0.0
    avg_price: float = 0.0


class PaperBroker:
    """Simulated fills, positions, equity, and persisted risk halt."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store_path: Path | str | None = ":memory:",
        starting_equity: float | None = None,
    ) -> None:
        self.settings = settings or Settings()
        equity = float(starting_equity if starting_equity is not None else self.settings.paper_equity)
        self.store = SQLiteStore(store_path)
        self.marks: dict[str, float] = {}
        self.realized_pnls: list[float] = []
        restored_cash = self.store.load_cash()
        if restored_cash is None:
            self.cash = equity
            self.positions: dict[str, Position] = {}
            self.risk = RiskGate.from_equity(equity, RiskLimits.from_settings(self.settings))
            self.store.save_cash(self.cash)
            self.store.save_risk_payload(self.risk.snapshot())
            self.store.commit()
        else:
            self.cash = restored_cash
            self.positions = {
                symbol: Position(qty=qty, avg_price=avg) for symbol, (qty, avg) in self.store.load_positions().items()
            }
            payload = self.store.load_risk_payload()
            limits = RiskLimits.from_settings(self.settings)
            self.risk = RiskGate.restore(payload, limits) if payload else RiskGate.from_equity(equity, limits)

    def equity(self) -> float:
        total = self.cash
        for symbol, pos in self.positions.items():
            mark = self.marks.get(symbol, pos.avg_price)
            total += pos.qty * mark
        return total

    def position_qty(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return 0.0 if pos is None else pos.qty

    def mark_to_market(self, bar: Bar) -> float:
        self.marks[bar.symbol] = bar.close
        eq = self.equity()
        self.risk.on_mark(bar.ts, eq)
        ts = bar.ts.isoformat().replace("+00:00", "Z")
        self.store.record_equity(ts, eq, self.cash)
        self.store.save_cash(self.cash)
        self.store.save_risk_payload(self.risk.snapshot())
        self.store.commit()
        return eq

    def submit(self, signal: Signal, bar: Bar) -> ExecutionReport:
        signal = Signal.model_validate(signal.model_dump())
        self.marks[bar.symbol] = bar.close
        self.risk.on_mark(bar.ts, self.equity())

        timing = execution_timing_error(signal, bar)
        if timing is not None:
            self.store.record_reject(
                signal.client_order_id,
                bar.ts.isoformat().replace("+00:00", "Z"),
                signal.symbol,
                timing,
            )
            self.store.commit()
            return ExecutionReport(
                status="rejected",
                client_order_id=signal.client_order_id,
                reason=timing,
                equity=self.equity(),
            )

        working = self._prepare(signal)
        notional = estimated_notional(working, bar)
        decision = self.risk.evaluate(intent=working.intent, notional=notional, equity=self.equity())
        if not decision.allowed:
            self.store.record_reject(
                working.client_order_id,
                bar.ts.isoformat().replace("+00:00", "Z"),
                working.symbol,
                decision.reason or "rejected",
            )
            self.store.save_risk_payload(self.risk.snapshot())
            self.store.commit()
            return ExecutionReport(
                status="rejected",
                client_order_id=working.client_order_id,
                reason=decision.reason,
                equity=self.equity(),
            )

        fill = simulate_fill(working, bar)
        if fill is None:
            return ExecutionReport(
                status="unfilled",
                client_order_id=working.client_order_id,
                reason="not_touchable",
                equity=self.equity(),
            )

        self._apply_fill(fill)
        self.store.record_fill(fill)
        eq = self.mark_to_market(bar)
        return ExecutionReport(
            status="filled",
            client_order_id=working.client_order_id,
            fill_price=fill.price,
            qty_base=fill.qty_base,
            notional=fill.notional,
            equity=eq,
        )

    def _prepare(self, signal: Signal) -> Signal:
        pos = self.position_qty(signal.symbol)
        if signal.intent not in ("close", "reduce") and signal.side != "flat":
            return signal
        if abs(pos) < 1e-16:
            return signal.model_copy(update={"qty": 0, "qty_unit": "base", "side": "flat"})
        mark = self.marks.get(signal.symbol) or 0.0
        if signal.intent == "reduce" and signal.qty > 0 and signal.side != "flat":
            if signal.qty_unit == "quote" and mark > 0:
                requested = signal.qty / mark
            else:
                requested = signal.qty
            close_qty = min(requested, abs(pos))
        else:
            close_qty = abs(pos)
        side = "sell" if pos > 0 else "buy"
        intent = "close" if close_qty >= abs(pos) - 1e-12 else "reduce"
        return signal.model_copy(update={"side": side, "qty": close_qty, "qty_unit": "base", "intent": intent})

    def _apply_fill(self, fill: Fill) -> None:
        pos = self.positions.get(fill.symbol) or Position()
        signed = fill.qty_base if fill.side == "buy" else -fill.qty_base
        if fill.side == "buy":
            self.cash -= fill.qty_base * fill.price
        else:
            self.cash += fill.qty_base * fill.price

        new_qty = pos.qty + signed
        if abs(pos.qty) > 1e-16 and pos.qty * signed < 0:
            closed = min(abs(pos.qty), abs(signed))
            direction = 1.0 if pos.qty > 0 else -1.0
            self.realized_pnls.append((fill.price - pos.avg_price) * closed * direction)

        if abs(new_qty) < 1e-12:
            pos = Position(0.0, 0.0)
        elif pos.qty == 0 or (pos.qty > 0 and signed > 0) or (pos.qty < 0 and signed < 0):
            avg = (abs(pos.qty) * pos.avg_price + fill.qty_base * fill.price) / (abs(pos.qty) + fill.qty_base)
            pos = Position(new_qty, avg)
        elif abs(signed) <= abs(pos.qty) + 1e-12:
            pos = Position(new_qty, pos.avg_price)
        else:
            remainder = abs(signed) - abs(pos.qty)
            pos = Position(remainder if signed > 0 else -remainder, fill.price)

        self.positions[fill.symbol] = pos
        self.store.save_position(fill.symbol, pos.qty, pos.avg_price)
        self.store.save_cash(self.cash)


def fill_price_market(close: float, side: str, slippage_bps: float) -> float:
    slip = slippage_bps / 10_000.0
    if side == "buy":
        return close * (1.0 + slip)
    return close * (1.0 - slip)


__all__ = ["PaperBroker", "Position", "fill_price_market"]
