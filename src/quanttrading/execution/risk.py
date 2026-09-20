from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from quanttrading.config import Settings
from quanttrading.signals import RiskUtilization

HaltReason = Literal["max_drawdown", "daily_circuit_breaker", "per_trade_limit", "halted"]


class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_trade_pct: float = Field(gt=0, le=1)
    daily_dd_pct: float = Field(gt=0, le=1)
    total_dd_pct: float = Field(gt=0, le=1)

    @classmethod
    def from_settings(cls, settings: Settings) -> RiskLimits:
        return cls(
            per_trade_pct=settings.per_trade_pct,
            daily_dd_pct=settings.daily_dd_pct,
            total_dd_pct=settings.total_dd_pct,
        )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None


@dataclass
class RiskGate:
    """Hard kill-switch. Lives in the execution layer, not the strategy."""

    limits: RiskLimits
    starting_equity: float
    peak_equity: float
    day_start_equity: float
    day: date | None = None
    halted: bool = False
    halt_reason: str | None = None
    daily_halt_date: date | None = None
    last_open_notional: float = 0.0

    @classmethod
    def from_equity(cls, equity: float, limits: RiskLimits) -> RiskGate:
        return cls(
            limits=limits,
            starting_equity=equity,
            peak_equity=equity,
            day_start_equity=equity,
        )

    def snapshot(self) -> dict[str, str | float | int | None]:
        return {
            "starting_equity": self.starting_equity,
            "peak_equity": self.peak_equity,
            "day_start_equity": self.day_start_equity,
            "day": self.day.isoformat() if self.day else None,
            "halted": int(self.halted),
            "halt_reason": self.halt_reason,
            "daily_halt_date": self.daily_halt_date.isoformat() if self.daily_halt_date else None,
        }

    @classmethod
    def restore(cls, payload: dict[str, str | float | int | None], limits: RiskLimits) -> RiskGate:
        day_raw = payload.get("day")
        halt_day_raw = payload.get("daily_halt_date")
        return cls(
            limits=limits,
            starting_equity=float(payload["starting_equity"]),
            peak_equity=float(payload["peak_equity"]),
            day_start_equity=float(payload["day_start_equity"]),
            day=date.fromisoformat(str(day_raw)) if day_raw else None,
            halted=bool(int(payload.get("halted") or 0)),
            halt_reason=str(payload["halt_reason"]) if payload.get("halt_reason") else None,
            daily_halt_date=date.fromisoformat(str(halt_day_raw)) if halt_day_raw else None,
        )

    def on_mark(self, ts: datetime, equity: float) -> None:
        day = ts.astimezone(timezone.utc).date()
        if self.day is None or day != self.day:
            self.day = day
            self.day_start_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        dd = self._total_dd(equity)
        if dd >= self.limits.total_dd_pct:
            self.halted = True
            self.halt_reason = "max_drawdown"
        daily_loss = self.day_start_equity - equity
        if daily_loss >= self.day_start_equity * self.limits.daily_dd_pct:
            self.daily_halt_date = day

    def evaluate(self, *, intent: str, notional: float, equity: float) -> RiskDecision:
        if self.halted:
            if intent == "open":
                return RiskDecision(False, self.halt_reason or "halted")
            return RiskDecision(True, None)

        if intent == "open":
            if self.day is not None and self.daily_halt_date == self.day:
                return RiskDecision(False, "daily_circuit_breaker")
            cap = equity * self.limits.per_trade_pct
            if notional > cap + 1e-9:
                return RiskDecision(False, "per_trade_limit")
            self.last_open_notional = notional
        return RiskDecision(True, None)

    def utilization(self, equity: float) -> RiskUtilization:
        per_trade = 0.0
        cap = equity * self.limits.per_trade_pct
        if cap > 0 and self.last_open_notional > 0:
            per_trade = min(self.last_open_notional / cap, 1.0)
        daily = 0.0
        daily_budget = self.day_start_equity * self.limits.daily_dd_pct
        if daily_budget > 0:
            daily = min(max((self.day_start_equity - equity) / daily_budget, 0.0), 1.0)
        total_budget = self.peak_equity * self.limits.total_dd_pct
        total = 0.0
        if total_budget > 0:
            total = min(max((self.peak_equity - equity) / total_budget, 0.0), 1.0)
        return RiskUtilization(per_trade_pct=per_trade, daily_dd_pct=daily, total_dd_pct=total)

    def _total_dd(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max((self.peak_equity - equity) / self.peak_equity, 0.0)
