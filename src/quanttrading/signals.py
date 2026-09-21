from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

Side = Literal["buy", "sell", "flat"]
Intent = Literal["open", "close", "reduce"]
QtyUnit = Literal["quote", "base", "contracts"]
OrderType = Literal["market", "limit"]


class RiskUtilization(BaseModel):
    """Current utilization of the three hard risk budgets, each in [0, 1]."""

    model_config = ConfigDict(extra="forbid")

    per_trade_pct: float = Field(ge=0, le=1)
    daily_dd_pct: float = Field(ge=0, le=1)
    total_dd_pct: float = Field(ge=0, le=1)


class Signal(BaseModel):
    """Signal contract v0.1 — JSON-serializable, shared by backtest and paper."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1)
    ts: datetime
    symbol: str = Field(min_length=1)
    side: Side
    intent: Intent
    qty: float = Field(ge=0)
    qty_unit: QtyUnit
    order_type: OrderType
    limit_price: float | None = Field(default=None, gt=0)
    strength: float = Field(ge=0, le=1)
    max_slippage_bps: float = Field(ge=0)
    risk: RiskUtilization
    client_order_id: str = Field(min_length=1)
    meta: dict[str, Any] | None = None

    @field_validator("ts")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_serializer("ts")
    def _ser_ts(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @model_validator(mode="after")
    def _check_order_and_side(self) -> Signal:
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required when order_type is limit")
        if self.side == "flat" and self.intent not in ("close", "reduce"):
            raise ValueError("side=flat requires intent close or reduce")
        if self.intent == "open" and self.qty <= 0:
            raise ValueError("open intent requires qty > 0")
        return self
