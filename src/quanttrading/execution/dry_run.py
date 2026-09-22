from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from quanttrading.config import Settings
from quanttrading.execution.base import ExecutionReport
from quanttrading.execution.fills import Fill, estimated_notional, execution_timing_error, market_anchor_price, to_base
from quanttrading.execution.market_meta import MarketConstraints, MarketMetadata, StaticMarketMetadata
from quanttrading.execution.paper import PaperBroker, fill_price_market
from quanttrading.market import Bar
from quanttrading.signals import Signal


@dataclass(frozen=True, slots=True)
class WouldBeOrder:
    """Order that dry-run would hand to an exchange. Nothing is sent."""

    symbol: str
    side: str
    qty: float
    client_order_id: str
    order_type: str
    limit_price: float | None
    notional: float
    reference_price: float


def floor_to_step(qty: float, step: float) -> float:
    if step <= 0 or qty <= 0:
        return qty if qty > 0 else 0.0
    steps = math.floor((qty / step) + 1e-9)
    return steps * step


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return math.floor((price / tick) + 0.5 + 1e-12) * tick


def build_would_be_order(
    signal: Signal,
    bar: Bar,
    constraints: MarketConstraints | None = None,
) -> tuple[WouldBeOrder, str | None]:
    """Build the order a live broker would send, plus a size-invalid reason.

    Base qty uses the bar (close for market, open when ``meta['fill_on']`` is
    ``open``, ticked limit for limit) so the hypothetical book matches paper.
    ``last_price`` does not resize the order.
    When both ``last_price`` and ``min_cost`` are set, the same base qty must
    also clear ``min_cost`` at that public last.
    """
    constraints = constraints or MarketConstraints()
    side = "sell" if signal.side == "flat" else signal.side
    if signal.order_type == "limit" and signal.limit_price is not None:
        price = round_to_tick(float(signal.limit_price), constraints.price_tick)
        limit_price: float | None = price
    else:
        price = market_anchor_price(signal, bar)
        limit_price = None

    if price <= 0:
        order = WouldBeOrder(
            symbol=signal.symbol,
            side=side,
            qty=0.0,
            client_order_id=signal.client_order_id,
            order_type=signal.order_type,
            limit_price=limit_price,
            notional=0.0,
            reference_price=price,
        )
        return order, "invalid_price"

    raw_qty = to_base(signal.qty, signal.qty_unit, price)
    qty = floor_to_step(raw_qty, constraints.amount_step)
    notional = qty * price
    order = WouldBeOrder(
        symbol=signal.symbol,
        side=side,
        qty=qty,
        client_order_id=signal.client_order_id,
        order_type=signal.order_type,
        limit_price=limit_price,
        notional=notional,
        reference_price=price,
    )
    if raw_qty > 1e-16 and qty <= 1e-16 and constraints.amount_step > 0:
        return order, "below_step"
    if qty <= 1e-16:
        return order, "zero_qty"
    if constraints.min_qty > 0 and qty + 1e-12 < constraints.min_qty:
        return order, "below_min_qty"
    if constraints.min_cost > 0 and notional + 1e-9 < constraints.min_cost:
        return order, "below_min_cost"
    last = constraints.last_price
    if last is not None and last > 0 and constraints.min_cost > 0 and (qty * last) + 1e-9 < constraints.min_cost:
        return order, "below_min_cost_at_last"
    return order, None


def _hypothetical_fill(signal: Signal, bar: Bar, order: WouldBeOrder) -> Fill | None:
    """Tape fill for the hypothetical book. Not an exchange acknowledgement."""
    side = order.side
    if signal.order_type == "market":
        price = fill_price_market(market_anchor_price(signal, bar), side, signal.max_slippage_bps)
    else:
        limit = order.limit_price
        if limit is None:
            return None
        if side == "buy" and bar.low <= limit:
            price = limit
        elif side == "sell" and bar.high >= limit:
            price = limit
        else:
            return None
    if order.qty <= 0:
        return None
    return Fill(
        client_order_id=signal.client_order_id,
        ts=bar.ts.isoformat().replace("+00:00", "Z"),
        symbol=signal.symbol,
        side=side,
        intent=signal.intent,
        qty_base=order.qty,
        price=price,
        notional=order.qty * price,
        slippage_bps=signal.max_slippage_bps if signal.order_type == "market" else 0.0,
    )


class DryRunBroker(PaperBroker):
    """Paper risk and bookkeeping with no exchange IO.

    Risk gates match paper (per-trade, daily loss, total drawdown). A passing
    order is recorded as ``dry_run_ok`` and applied only to the local book so
    later signals stay aligned with paper. Nothing calls a private endpoint.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store_path: Path | str | None = ":memory:",
        starting_equity: float | None = None,
        market: MarketMetadata | None = None,
    ) -> None:
        super().__init__(settings, store_path=store_path, starting_equity=starting_equity)
        self.market: MarketMetadata = market if market is not None else StaticMarketMetadata()
        self.orders: list[WouldBeOrder] = []
        self.market_constraints: dict[str, MarketConstraints] = {}

    def submit(self, signal: Signal, bar: Bar) -> ExecutionReport:
        signal = Signal.model_validate(signal.model_dump())
        self.marks[bar.symbol] = bar.close
        self.risk.on_mark(bar.ts, self.equity())
        timing = execution_timing_error(signal, bar)
        ts = bar.ts.isoformat().replace("+00:00", "Z")
        if timing is not None:
            self._record(
                signal,
                ts,
                status="rejected",
                reason=timing,
                qty_base=0.0,
                price=market_anchor_price(signal, bar),
                notional=0.0,
                order_type=signal.order_type,
                limit_price=signal.limit_price,
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
            qty_base = _estimate_base(working, bar)
            self._record(
                working,
                ts,
                status="rejected",
                reason=decision.reason,
                qty_base=qty_base,
                price=market_anchor_price(working, bar),
                notional=notional,
                order_type=working.order_type,
                limit_price=working.limit_price,
            )
            self.store.save_risk_payload(self.risk.snapshot())
            self.store.commit()
            return ExecutionReport(
                status="rejected",
                client_order_id=working.client_order_id,
                reason=decision.reason,
                qty_base=qty_base,
                notional=notional,
                equity=self.equity(),
            )

        constraints = self.market.for_symbol(working.symbol)
        self.market_constraints[working.symbol] = constraints
        order, size_reason = build_would_be_order(working, bar, constraints)
        if size_reason is not None:
            self._record(
                working,
                ts,
                status="size_invalid",
                reason=size_reason,
                qty_base=order.qty,
                price=order.reference_price,
                notional=order.notional,
                order_type=order.order_type,
                limit_price=order.limit_price,
            )
            self.store.commit()
            return ExecutionReport(
                status="size_invalid",
                client_order_id=working.client_order_id,
                reason=size_reason,
                qty_base=order.qty,
                notional=order.notional,
                equity=self.equity(),
            )

        fill = _hypothetical_fill(working, bar, order)
        if fill is None:
            self._record(
                working,
                ts,
                status="unfilled",
                reason="not_touchable",
                qty_base=order.qty,
                price=order.reference_price,
                notional=order.notional,
                order_type=order.order_type,
                limit_price=order.limit_price,
            )
            self.store.commit()
            return ExecutionReport(
                status="unfilled",
                client_order_id=working.client_order_id,
                reason="not_touchable",
                qty_base=order.qty,
                notional=order.notional,
                equity=self.equity(),
            )

        self.orders.append(order)
        self._apply_fill(fill)
        self._record(
            working,
            ts,
            status="dry_run_ok",
            reason=None,
            qty_base=fill.qty_base,
            price=fill.price,
            notional=fill.notional,
            order_type=order.order_type,
            limit_price=order.limit_price,
            side=fill.side,
        )
        eq = self.mark_to_market(bar)
        return ExecutionReport(
            status="dry_run_ok",
            client_order_id=working.client_order_id,
            fill_price=fill.price,
            qty_base=fill.qty_base,
            notional=fill.notional,
            equity=eq,
        )

    def _record(
        self,
        signal: Signal,
        ts: str,
        *,
        status: str,
        reason: str | None,
        qty_base: float,
        price: float,
        notional: float,
        order_type: str | None,
        limit_price: float | None,
        side: str | None = None,
    ) -> None:
        self.store.record_event(
            client_order_id=signal.client_order_id,
            ts=ts,
            symbol=signal.symbol,
            side=side or signal.side,
            intent=signal.intent,
            qty_base=qty_base,
            price=price,
            notional=notional,
            slippage_bps=signal.max_slippage_bps if signal.order_type == "market" else 0.0,
            status=status,
            reason=reason,
            order_type=order_type,
            limit_price=limit_price,
        )


def _estimate_base(signal: Signal, bar: Bar) -> float:
    price = signal.limit_price if signal.order_type == "limit" and signal.limit_price else market_anchor_price(signal, bar)
    if price <= 0 or signal.qty <= 0:
        return 0.0
    try:
        return to_base(signal.qty, signal.qty_unit, price)
    except ValueError:
        return 0.0


def _count_by(rows: list[sqlite3.Row], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row[key])
        counts[name] = counts.get(name, 0) + 1
    return counts


def _reason_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = str(row["reason"] or "")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def equity_snapshot(curve: list[tuple[str, float]]) -> dict[str, object]:
    if not curve:
        return {
            "n_points": 0,
            "start_equity": None,
            "end_equity": None,
            "min_equity": None,
            "max_equity": None,
            "head": [],
            "tail": [],
        }
    equities = [eq for _, eq in curve]
    head = [{"ts": ts, "equity": round(eq, 4)} for ts, eq in curve[:1]]
    tail = [{"ts": ts, "equity": round(eq, 4)} for ts, eq in curve[-3:]]
    return {
        "n_points": len(curve),
        "start_equity": round(curve[0][1], 4),
        "end_equity": round(curve[-1][1], 4),
        "min_equity": round(min(equities), 4),
        "max_equity": round(max(equities), 4),
        "head": head,
        "tail": tail,
    }


def _read_paper_fills(path: Path) -> list[sqlite3.Row]:
    resolved = path.resolve()
    uri = "file:" + quote(resolved.as_posix()) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM fills ORDER BY id ASC"))
    finally:
        conn.close()


def reconcile_rows(
    dry_rows: list[sqlite3.Row],
    paper_rows: list[sqlite3.Row],
    *,
    paper_state: str,
) -> dict[str, object]:
    """Compare dry-run would-send / rejects with a paper book's fills."""
    dry_ok = [row for row in dry_rows if row["status"] == "dry_run_ok"]
    paper_ok = [row for row in paper_rows if row["status"] == "filled"]
    dry_rej = [row for row in dry_rows if row["status"] == "rejected"]
    paper_rej = [row for row in paper_rows if row["status"] == "rejected"]

    dry_ids = [str(row["client_order_id"]) for row in dry_ok]
    paper_ids = [str(row["client_order_id"]) for row in paper_ok]
    dry_rej_ids = [str(row["client_order_id"]) for row in dry_rej]
    paper_rej_ids = [str(row["client_order_id"]) for row in paper_rej]
    dry_sides = _count_by(dry_ok, "side")
    paper_sides = _count_by(paper_ok, "side")
    dry_intents = _count_by(dry_ok, "intent")
    paper_intents = _count_by(paper_ok, "intent")

    mismatches: list[dict[str, object]] = []
    if dry_ids != paper_ids:
        dry_set = set(dry_ids)
        paper_set = set(paper_ids)
        mismatches.append(
            {
                "field": "client_order_id",
                "only_dry_run": [order_id for order_id in dry_ids if order_id not in paper_set],
                "only_paper": [order_id for order_id in paper_ids if order_id not in dry_set],
            }
        )
    elif any(d["side"] != p["side"] or d["intent"] != p["intent"] for d, p in zip(dry_ok, paper_ok, strict=True)):
        bad = []
        for dry_row, paper_row in zip(dry_ok, paper_ok, strict=True):
            if dry_row["side"] != paper_row["side"] or dry_row["intent"] != paper_row["intent"]:
                bad.append(
                    {
                        "client_order_id": str(dry_row["client_order_id"]),
                        "dry_run": {"side": dry_row["side"], "intent": dry_row["intent"]},
                        "paper": {"side": paper_row["side"], "intent": paper_row["intent"]},
                    }
                )
        mismatches.append({"field": "client_order_id_attributes", "rows": bad})
    if dry_sides != paper_sides:
        mismatches.append({"field": "side", "dry_run": dry_sides, "paper": paper_sides})
    if dry_intents != paper_intents:
        mismatches.append({"field": "intent", "dry_run": dry_intents, "paper": paper_intents})
    if dry_rej_ids != paper_rej_ids:
        dry_rej_set = set(dry_rej_ids)
        paper_rej_set = set(paper_rej_ids)
        mismatches.append(
            {
                "field": "rejected_client_order_id",
                "only_dry_run": [order_id for order_id in dry_rej_ids if order_id not in paper_rej_set],
                "only_paper": [order_id for order_id in paper_rej_ids if order_id not in dry_rej_set],
            }
        )

    return {
        "paper_state": paper_state,
        "n_mismatches": len(mismatches),
        "mismatches": mismatches,
        "side_counts": {"dry_run": dry_sides, "paper": paper_sides},
        "intent_counts": {"dry_run": dry_intents, "paper": paper_intents},
        "n_dry_run_ok": len(dry_ok),
        "n_paper_filled": len(paper_ok),
        "n_dry_run_rejected": len(dry_rej),
        "n_paper_rejected": len(paper_rej),
    }


def _constraints_payload(constraints: MarketConstraints) -> dict[str, float | None]:
    return {
        "min_qty": constraints.min_qty,
        "min_cost": constraints.min_cost,
        "amount_step": constraints.amount_step,
        "price_tick": constraints.price_tick,
        "last_price": constraints.last_price,
    }


def summarize_dry_run(
    broker: DryRunBroker,
    *,
    paper_state: Path | str | None = None,
    strategy_id: str | None = None,
    sample_size: int = 5,
) -> dict[str, object]:
    rows = list(broker.store.fills())
    would_send = [row for row in rows if row["status"] == "dry_run_ok"]
    rejected = [row for row in rows if row["status"] == "rejected"]
    size_invalid = [row for row in rows if row["status"] == "size_invalid"]
    unfilled = [row for row in rows if row["status"] == "unfilled"]
    sample_rows = would_send[:sample_size]
    sample = [str(row["client_order_id"]) for row in sample_rows]
    sample_orders = [
        {
            "client_order_id": str(row["client_order_id"]),
            "symbol": str(row["symbol"]),
            "side": str(row["side"]),
            "qty": row["qty_base"],
            "order_type": row["order_type"],
            "limit_price": row["limit_price"],
        }
        for row in sample_rows
    ]
    summary: dict[str, object] = {
        "mode": "dry_run",
        "strategy_id": strategy_id,
        "per_trade_pct": broker.risk.limits.per_trade_pct,
        "orders_sent": 0,
        "n_signals": len(rows),
        "n_would_send": len(would_send),
        "n_risk_rejected": len(rejected),
        "n_size_invalid": len(size_invalid),
        "n_unfilled": len(unfilled),
        "risk_reject_reasons": _reason_counts(rejected),
        "size_invalid_reasons": _reason_counts(size_invalid),
        "sample_client_order_ids": sample,
        "sample_orders": sample_orders,
        "equity_path": equity_snapshot(broker.store.equity_curve()),
        "market": {symbol: _constraints_payload(item) for symbol, item in broker.market_constraints.items()},
        "halted": int(broker.risk.halted),
        "halt_reason": broker.risk.halt_reason or "",
    }
    if paper_state is not None:
        path = Path(paper_state)
        if not path.exists():
            raise FileNotFoundError(f"paper state not found: {path}")
        try:
            paper_rows = _read_paper_fills(path)
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not read paper state {path}: {exc}") from exc
        summary["paper_reconcile"] = reconcile_rows(rows, paper_rows, paper_state=str(path))
    else:
        summary["paper_reconcile"] = None
    return summary
