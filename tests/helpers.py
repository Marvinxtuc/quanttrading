from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from quanttrading.market import Bar
from quanttrading.signals import RiskUtilization, Signal

UTC = timezone.utc


def make_bar(
    *,
    close: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    open: float | None = None,
    ts: datetime | None = None,
    symbol: str = "BTC/USDT",
) -> Bar:
    o = close if open is None else open
    return Bar(
        ts=ts or datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
        symbol=symbol,
        open=o,
        high=high if high is not None else max(o, close) * 1.01,
        low=low if low is not None else min(o, close) * 0.99,
        close=close,
        volume=10.0,
    )


def make_signal(**overrides: object) -> Signal:
    payload: dict = dict(
        strategy_id="test",
        ts=datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
        symbol="BTC/USDT",
        side="buy",
        intent="open",
        qty=20.0,
        qty_unit="quote",
        order_type="market",
        limit_price=None,
        strength=1.0,
        max_slippage_bps=10.0,
        risk=RiskUtilization(per_trade_pct=0.0, daily_dd_pct=0.0, total_dd_pct=0.0),
        client_order_id=uuid4().hex,
        meta=None,
    )
    payload.update(overrides)
    return Signal.model_validate(payload)
