from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        ts = self.ts
        if ts.tzinfo is None:
            object.__setattr__(self, "ts", ts.replace(tzinfo=timezone.utc))
        else:
            object.__setattr__(self, "ts", ts.astimezone(timezone.utc))
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC bounds are inconsistent")
        if self.low <= 0 or self.close <= 0:
            raise ValueError("prices must be positive")
