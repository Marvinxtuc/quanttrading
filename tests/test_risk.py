from datetime import datetime, timedelta, timezone
from pathlib import Path

from quanttrading.config import Settings
from quanttrading.execution.paper import PaperBroker
from tests.helpers import make_bar, make_signal

SETTINGS = Settings(
    paper_equity=2000,
    per_trade_pct=0.01,
    daily_dd_pct=0.03,
    total_dd_pct=0.20,
)


def test_per_trade_notional_rejected() -> None:
    broker = PaperBroker(SETTINGS, store_path=":memory:")
    bar = make_bar(close=100.0)
    report = broker.submit(make_signal(qty=50.0, qty_unit="quote"), bar)
    assert report.status == "rejected"
    assert report.reason == "per_trade_limit"
    assert broker.position_qty(bar.symbol) == 0


def test_daily_circuit_breaker_rejects_opens() -> None:
    broker = PaperBroker(SETTINGS, store_path=":memory:")
    bar = make_bar(close=100.0)
    broker.mark_to_market(bar)
    broker.cash = 2000.0 - 60.0  # 3% of 2000
    broker.mark_to_market(bar)
    report = broker.submit(make_signal(qty=20.0), bar)
    assert report.status == "rejected"
    assert report.reason == "daily_circuit_breaker"


def test_max_drawdown_halts_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite"
    ts = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    bar = make_bar(close=100.0, ts=ts)
    broker = PaperBroker(SETTINGS, store_path=path)
    broker.mark_to_market(bar)
    broker.cash = 1600.0  # 20% off peak 2000
    broker.mark_to_market(bar)
    assert broker.risk.halted
    assert broker.risk.halt_reason == "max_drawdown"
    first = broker.submit(make_signal(qty=10.0), bar)
    assert first.status == "rejected"
    assert first.reason == "max_drawdown"
    broker.store.close()

    restarted = PaperBroker(SETTINGS, store_path=path)
    later = make_bar(close=100.0, ts=ts + timedelta(days=2))
    restarted.marks[bar.symbol] = 100.0
    second = restarted.submit(make_signal(qty=10.0), later)
    assert restarted.risk.halted
    assert second.status == "rejected"
    assert second.reason == "max_drawdown"
