from __future__ import annotations

from dataclasses import dataclass

from quanttrading.market import Bar
from quanttrading.signals import Signal


@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: str
    ts: str
    symbol: str
    side: str
    intent: str
    qty_base: float
    price: float
    notional: float
    slippage_bps: float


def to_base(qty: float, qty_unit: str, price: float) -> float:
    if price <= 0:
        raise ValueError("price must be positive")
    if qty_unit == "quote":
        return qty / price
    return qty  # base and linear contracts


def estimated_notional(signal: Signal, bar: Bar) -> float:
    if signal.qty_unit == "quote":
        return signal.qty
    price = signal.limit_price if signal.order_type == "limit" and signal.limit_price else bar.close
    return signal.qty * price


def simulate_fill(signal: Signal, bar: Bar) -> Fill | None:
    """IOC-style: market fills this bar with slippage; limit fills only if touched."""
    side = "sell" if signal.side == "flat" else signal.side

    if signal.order_type == "market":
        slip = signal.max_slippage_bps / 10_000.0
        price = bar.close * (1.0 + slip) if side == "buy" else bar.close * (1.0 - slip)
        qty_base = to_base(signal.qty, signal.qty_unit, bar.close)
        if qty_base <= 0:
            return None
        return Fill(
            client_order_id=signal.client_order_id,
            ts=bar.ts.isoformat().replace("+00:00", "Z"),
            symbol=signal.symbol,
            side=side,
            intent=signal.intent,
            qty_base=qty_base,
            price=price,
            notional=qty_base * price,
            slippage_bps=signal.max_slippage_bps,
        )

    limit = signal.limit_price
    if limit is None:
        return None
    if side == "buy" and bar.low <= limit:
        price = limit
        qty_base = to_base(signal.qty, signal.qty_unit, limit)
    elif side == "sell" and bar.high >= limit:
        price = limit
        qty_base = to_base(signal.qty, signal.qty_unit, limit)
    else:
        return None
    if qty_base <= 0:
        return None
    return Fill(
        client_order_id=signal.client_order_id,
        ts=bar.ts.isoformat().replace("+00:00", "Z"),
        symbol=signal.symbol,
        side=side,
        intent=signal.intent,
        qty_base=qty_base,
        price=price,
        notional=qty_base * price,
        slippage_bps=0.0,
    )
