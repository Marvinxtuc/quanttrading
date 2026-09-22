from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Business baselines. Load from env / `.env`; never from secrets files."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    paper_equity: float = Field(default=2000.0, gt=0)
    exchange_id: str = "kraken"
    default_symbol: str = "BTC/USD"
    default_timeframe: str = "1h"
    per_trade_pct: float = Field(default=0.01, gt=0, le=1)
    daily_dd_pct: float = Field(default=0.03, gt=0, le=1)
    total_dd_pct: float = Field(default=0.20, gt=0, le=1)
    max_slippage_bps: float = Field(default=5.0, ge=0)
    state_path: Path = Path("state/paper.sqlite")
    dry_run_state_path: Path = Path("state/dry_run.sqlite")
    live_state_path: Path = Path("state/live.sqlite")

    @property
    def per_trade_usdt(self) -> float:
        return self.paper_equity * self.per_trade_pct

    @property
    def daily_loss_usdt(self) -> float:
        return self.paper_equity * self.daily_dd_pct

    @property
    def max_drawdown_usdt(self) -> float:
        return self.paper_equity * self.total_dd_pct


def load_settings() -> Settings:
    return Settings()
