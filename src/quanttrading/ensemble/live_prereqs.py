"""Live prerequisites for ma_ensemble_v1 (docs/ma_ensemble_v1.md, 执行/风控), paper-testable.

No exchange calls here: callers feed quotes in and act on the returned decisions.

(a) Limit-order reprice: ``step_limit_order`` is intraday-callable (e.g. every 5-15 min).
    Rule: place passive limit at best price; if unfilled 2h after placement, re-price to
    the current best and re-place ONCE; if still unfilled at/after 23:00 UTC of the
    execution day, fall back to market for the remaining quantity.
(b) Cost guard: ``CostGuard`` measures realized cost per side (fee + slippage vs the
    decision reference price) in bps; ``consecutive_limit`` consecutive fills above
    60 bps -> paused (manual review required to resume).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone

REPRICE_AFTER = timedelta(hours=2)
MAX_REPRICES = 1  # doc: "改价重挂一次"
MARKET_CUTOFF_UTC = time(23, 0)
COST_LIMIT_BPS = 60.0
CONSECUTIVE_BREACHES = 3  # doc says "连续明显高于" without N; 3 chosen, configurable


@dataclass(frozen=True)
class Quote:
    ts: datetime  # tz-aware UTC
    bid: float
    ask: float
    last: float | None = None  # last trade price (used for touch detection)


@dataclass
class LimitOrder:
    side: str  # "buy" | "sell"
    qty: float
    exec_day: str  # UTC date YYYY-MM-DD the order belongs to
    limit_px: float
    placed_at: datetime
    reprices: int = 0
    status: str = "open"  # open | filled | cancelled
    fill_px: float | None = None
    fill_type: str | None = None  # limit | limit_repriced | market_fallback
    filled_at: datetime | None = None
    events: list[str] = field(default_factory=list)


def passive_price(side: str, q: Quote) -> float:
    """Maker price: join the best bid for buys, best ask for sells."""
    return q.bid if side == "buy" else q.ask


def place_limit_order(side: str, qty: float, q: Quote) -> LimitOrder:
    if side not in ("buy", "sell"):
        raise ValueError(side)
    o = LimitOrder(side, qty, q.ts.date().isoformat(), passive_price(side, q), q.ts)
    o.events.append(f"{q.ts.isoformat()} place {side} {qty} @ {o.limit_px}")
    return o


def _touched(o: LimitOrder, q: Quote) -> bool:
    """Paper fill model: buy fills if the market traded/offered at or below the limit."""
    if o.side == "buy":
        return q.ask <= o.limit_px or (q.last is not None and q.last < o.limit_px)
    return q.bid >= o.limit_px or (q.last is not None and q.last > o.limit_px)


def cutoff_for(exec_day: str) -> datetime:
    return datetime.combine(datetime.fromisoformat(exec_day).date(), MARKET_CUTOFF_UTC, tzinfo=timezone.utc)


def step_limit_order(o: LimitOrder, q: Quote, market_slippage: float = 0.0) -> str:
    """Advance the order with one intraday quote. Returns the action taken:
    'none' | 'filled' | 'repriced' | 'market_fallback' | 'noop_closed'."""
    if o.status != "open":
        return "noop_closed"
    if q.ts.tzinfo is None:
        raise ValueError("quote ts must be tz-aware UTC")
    if _touched(o, q):
        o.status, o.fill_px, o.filled_at = "filled", o.limit_px, q.ts
        o.fill_type = "limit_repriced" if o.reprices else "limit"
        o.events.append(f"{q.ts.isoformat()} filled @ {o.limit_px} ({o.fill_type})")
        return "filled"
    if q.ts >= cutoff_for(o.exec_day):
        px = q.ask * (1 + market_slippage) if o.side == "buy" else q.bid * (1 - market_slippage)
        o.status, o.fill_px, o.filled_at, o.fill_type = "filled", px, q.ts, "market_fallback"
        o.events.append(f"{q.ts.isoformat()} 23:00 UTC cutoff -> market @ {px}")
        return "market_fallback"
    if o.reprices < MAX_REPRICES and q.ts - o.placed_at >= REPRICE_AFTER:
        old = o.limit_px
        o.limit_px, o.placed_at = passive_price(o.side, q), q.ts
        o.reprices += 1
        o.events.append(f"{q.ts.isoformat()} unfilled 2h: cancel/replace {old} -> {o.limit_px}")
        return "repriced"
    return "none"


def realized_cost_bps(side: str, qty: float, fill_px: float, fee: float, ref_px: float) -> float:
    """Fee + adverse slippage vs the reference (decision) price, as bps of reference notional."""
    notional = qty * ref_px
    if notional <= 0:
        raise ValueError("notional must be > 0")
    slip = (fill_px - ref_px) * qty if side == "buy" else (ref_px - fill_px) * qty
    return (fee + slip) / notional * 1e4


@dataclass
class CostGuard:
    limit_bps: float = COST_LIMIT_BPS
    consecutive_limit: int = CONSECUTIVE_BREACHES
    consecutive: int = 0
    paused: bool = False
    history: list[float] = field(default_factory=list)

    def record(self, cost_bps: float) -> bool:
        """Record one fill's realized cost; returns True if this record triggered the pause."""
        self.history.append(round(cost_bps, 4))
        self.consecutive = self.consecutive + 1 if cost_bps > self.limit_bps else 0
        if not self.paused and self.consecutive >= self.consecutive_limit:
            self.paused = True
            return True
        return False

    def resume(self) -> None:
        """Manual review done (人工复核)."""
        self.paused, self.consecutive = False, 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CostGuard":
        return cls(**d)
