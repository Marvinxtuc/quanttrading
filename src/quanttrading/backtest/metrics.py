from __future__ import annotations

from datetime import datetime, timezone
from math import sqrt

from quanttrading.execution.paper import PaperBroker


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def max_drawdown(equities: list[float]) -> float:
    peak = equities[0] if equities else 0.0
    max_dd = 0.0
    for value in equities:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return max_dd


def sharpe_ratio(daily_returns: list[float], periods_per_year: int = 365) -> float:
    if len(daily_returns) < 2:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    std = sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * sqrt(periods_per_year)


def metrics_from_broker(broker: PaperBroker) -> dict[str, float | int | str | None]:
    curve = broker.store.equity_curve()
    equities = [eq for _, eq in curve]
    by_day: dict[str, float] = {}
    for ts, eq in curve:
        day = _parse_ts(ts).date().isoformat()
        by_day[day] = eq
    days = sorted(by_day)
    daily_returns: list[float] = []
    for i in range(1, len(days)):
        prev = by_day[days[i - 1]]
        if prev:
            daily_returns.append((by_day[days[i]] - prev) / prev)

    pnls = list(broker.realized_pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor: float | None = round(gross_profit / gross_loss, 4)
    elif gross_profit > 0:
        profit_factor = None
    else:
        profit_factor = 0.0
    win_rate = (len(wins) / len(pnls)) if pnls else 0.0

    fill_days: set[str] = set()
    for row in broker.store.fills():
        if row["status"] == "filled":
            fill_days.add(_parse_ts(str(row["ts"])).date().isoformat())

    start = equities[0] if equities else broker.settings.paper_equity
    end = equities[-1] if equities else broker.equity()
    return {
        "trade_days": len(fill_days),
        "n_fills": sum(1 for row in broker.store.fills() if row["status"] == "filled"),
        "n_closed_trades": len(pnls),
        "sharpe": round(sharpe_ratio(daily_returns), 4),
        "max_dd": round(max_drawdown(equities), 6),
        "win_rate": round(win_rate, 4),
        "profit_factor": profit_factor,
        "start_equity": round(start, 4),
        "end_equity": round(end, 4),
        "return_pct": round((end / start - 1.0) * 100.0, 4) if start else 0.0,
        "halted": int(broker.risk.halted),
        "halt_reason": broker.risk.halt_reason or "",
    }
