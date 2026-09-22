from __future__ import annotations

from dataclasses import dataclass

from quanttrading.market import Bar, utc_iso
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


def market_anchor_price(signal: Signal, bar: Bar) -> float:
    """Price a market order is sized and filled against.

    Existing strategies fill the signal bar's close. A next-bar order sets
    ``meta['fill_on'] == 'open'`` so the fill uses that bar's open and does
    not read its close.
    """
    meta = signal.meta or {}
    if meta.get("fill_on") == "open":
        return bar.open
    return bar.close


def execution_timing_error(signal: Signal, bar: Bar) -> str | None:
    """Reject a next-bar order that is not tied to this bar's open.

    Signals without ``signal_ts`` / ``exec_ts`` are unchanged. When those
    fields are present, ``exec_ts`` must be this bar, ``signal_ts`` must be
    strictly earlier, and ``fill_on`` must be ``open``.
    """
    meta = signal.meta or {}
    if "signal_ts" not in meta and "exec_ts" not in meta and meta.get("fill_on") != "open":
        return None
    signal_ts = meta.get("signal_ts")
    exec_ts = meta.get("exec_ts")
    if not isinstance(signal_ts, str) or not isinstance(exec_ts, str):
        return "signal_timing"
    if signal_ts >= exec_ts:
        return "lookahead_ts"
    if meta.get("fill_on") != "open":
        return "fill_on"
    if exec_ts != utc_iso(bar.ts):
        return "exec_ts_mismatch"
    return None


def estimated_notional(signal: Signal, bar: Bar) -> float:
    if signal.qty_unit == "quote":
        return signal.qty
    price = signal.limit_price if signal.order_type == "limit" and signal.limit_price else market_anchor_price(signal, bar)
    return signal.qty * price


def simulate_fill(signal: Signal, bar: Bar) -> Fill | None:
    """IOC-style: market fills this bar with slippage; limit fills only if touched."""
    side = "sell" if signal.side == "flat" else signal.side

    if signal.order_type == "market":
        anchor = market_anchor_price(signal, bar)
        slip = signal.max_slippage_bps / 10_000.0
        price = anchor * (1.0 + slip) if side == "buy" else anchor * (1.0 - slip)
        qty_base = to_base(signal.qty, signal.qty_unit, anchor)
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
