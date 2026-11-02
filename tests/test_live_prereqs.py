from datetime import datetime, timedelta, timezone

import pytest

from quanttrading.ensemble.live_prereqs import (
    CostGuard, Quote, place_limit_order, realized_cost_bps, step_limit_order,
)
from quanttrading.ensemble.ma_ensemble import DailyBar, Ledger, run_step

T0 = datetime(2026, 11, 2, 0, 10, tzinfo=timezone.utc)


def q(minutes, bid, ask, last=None):
    return Quote(T0 + timedelta(minutes=minutes), bid, ask, last)


def test_limit_fills_without_reprice():
    o = place_limit_order("buy", 0.001, q(0, 100, 101))
    assert o.limit_px == 100
    assert step_limit_order(o, q(30, 100.5, 101)) == "none"
    assert step_limit_order(o, q(60, 99.5, 100)) == "filled"
    assert (o.fill_px, o.fill_type) == (100, "limit")
    assert step_limit_order(o, q(70, 90, 91)) == "noop_closed"


def test_reprice_after_2h_once_then_fill():
    o = place_limit_order("buy", 0.001, q(0, 100, 101))
    assert step_limit_order(o, q(119, 102, 103)) == "none"  # <2h
    assert step_limit_order(o, q(120, 102, 103)) == "repriced"
    assert o.limit_px == 102 and o.reprices == 1
    # only one reprice even after another 2h+
    assert step_limit_order(o, q(400, 104, 105)) == "none"
    assert step_limit_order(o, q(500, 101, 102)) == "filled"
    assert o.fill_type == "limit_repriced" and o.fill_px == 102


def test_sell_market_fallback_at_2300_utc():
    o = place_limit_order("sell", 0.002, q(0, 100, 101))
    assert o.limit_px == 101
    assert step_limit_order(o, q(130, 98, 99)) == "repriced"
    assert o.limit_px == 99
    cutoff_min = (23 * 60) - 10
    assert step_limit_order(o, q(cutoff_min - 1, 95, 96)) == "none"
    assert step_limit_order(o, q(cutoff_min, 95, 96), market_slippage=0.001) == "market_fallback"
    assert o.fill_type == "market_fallback" and o.fill_px == pytest.approx(95 * 0.999)


def test_last_trade_through_limit_counts_as_fill():
    o = place_limit_order("sell", 0.001, q(0, 100, 101))
    assert step_limit_order(o, q(5, 100, 101, last=101.5)) == "filled"


def test_naive_ts_rejected():
    o = place_limit_order("buy", 0.001, q(0, 100, 101))
    with pytest.raises(ValueError):
        step_limit_order(o, Quote(datetime(2026, 11, 2, 1), 100, 101))


def test_realized_cost_bps():
    # 40 bps fee + 5 bps adverse slip on buy
    assert realized_cost_bps("buy", 1, 100.05, 100.05 * 0.004, 100) == pytest.approx(45.02, abs=0.01)
    # sell above ref = favourable slippage reduces cost
    assert realized_cost_bps("sell", 1, 100.10, 0.40, 100) == pytest.approx(30.0)


def test_cost_guard_consecutive_breaches():
    g = CostGuard()
    assert not g.record(70) and not g.record(80)
    assert not g.record(50)  # resets streak
    assert not g.record(61) and not g.record(65)
    assert g.record(90) and g.paused
    assert not g.record(10) and g.paused  # stays paused until manual resume
    g.resume()
    assert not g.paused and g.consecutive == 0
    assert CostGuard.from_dict(g.to_dict()) == g


def _bars(closes, last_day):
    from datetime import date
    d = date.fromisoformat(last_day)
    return [DailyBar((d - timedelta(days=len(closes) - 1 - i)).isoformat(), c, c * 1.01, c * 0.99, c)
            for i, c in enumerate(closes)]


def test_paused_cost_guard_blocks_trading_in_run_step(tmp_path):
    ledger = Ledger(tmp_path / "s.sqlite")
    g = CostGuard(); g.paused = True
    ledger.save_cost_guard(g); ledger.db.commit()
    bars = _bars([100 + i for i in range(300)], "2026-01-01")
    s = run_step(bars, ledger, now=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc))
    assert s["trades_today"] == [] and s["cost_guard"]["paused"]
    assert any("cost guard paused" in n for n in s["notes"])
    assert s["reconciliation"]["mismatches"] == 0 and s["orders_sent"] == 0


def test_run_step_records_costs_below_limit(tmp_path):
    ledger = Ledger(tmp_path / "s.sqlite")
    bars = _bars([100 + i for i in range(300)], "2026-01-01")
    s = run_step(bars, ledger, now=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc))
    assert all(t["cost_bps"] == pytest.approx(45.0, abs=0.1) for t in s["trades_today"])
    assert ledger.cost_guard().history and not ledger.cost_guard().paused
