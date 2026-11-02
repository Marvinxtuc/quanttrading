"""Signal-flip injection of the sell (and re-buy) path, end to end through run_step.

Never touches state/paper_sma_basket.*: works on synthetic ledgers and on a temp COPY.
"""
import json
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from quanttrading.ensemble.ma_ensemble import COST_PER_SIDE, DailyBar, Ledger, run_step

REAL_STATE = Path(__file__).resolve().parents[1] / "state" / "paper_sma_basket.sqlite"


def bars_ending(closes, last_day):
    d_last = date.fromisoformat(last_day)
    n = len(closes)
    return [DailyBar((d_last - timedelta(days=n - 1 - i)).isoformat(), c, c * 1.01, c * 0.99, c) for i, c in enumerate(closes)]


def at(day):
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(minutes=5)


def check_fills(summary, side, price):
    for t in summary["trades_today"]:
        assert t["side"] == side
        assert t["fill_type"] == "limit"
        exp_px = price * (0.9995 if side == "sell" else 1.0005)
        assert t["price"] == pytest.approx(exp_px, rel=1e-6)
        assert t["fee"] == pytest.approx(t["qty_btc"] * t["price"] * COST_PER_SIDE, abs=1e-4)
    assert summary["reconciliation"]["mismatches"] == 0
    assert summary["orders_sent"] == 0


def run_flip_cycle(ledger, base, start_last_day):
    """Day0 flat base (state set up by caller); day1 crash below all SMAs -> sell;
    day2 rebound above all SMAs -> buy. Returns the two summaries."""
    hist = [base] * 300
    d1 = (date.fromisoformat(start_last_day) + timedelta(days=1)).isoformat()
    d2 = (date.fromisoformat(start_last_day) + timedelta(days=2)).isoformat()
    d3 = (date.fromisoformat(start_last_day) + timedelta(days=3)).isoformat()
    low, high = base * 0.85, base * 1.10
    # signal bar d1 closes at `low`; exec bar d2 opens at `low`
    s_sell = run_step(bars_ending(hist + [low, low], d2), ledger, now=at(d2))
    s_buy = run_step(bars_ending(hist + [low, high, high], d3), ledger, now=at(d3))
    return s_sell, s_buy, low, high


def test_flip_long_cash_long_synthetic(tmp_path):
    ledger = Ledger(tmp_path / "s.sqlite")
    base = 80000.0
    hist = [base * (0.7 + 0.3 * i / 299) for i in range(300)]  # uptrend: all long
    s0 = run_step(bars_ending(hist + [base], "2026-01-01"), ledger, now=at("2026-01-01"))
    assert {t["side"] for t in s0["trades_today"]} == {"buy"} and len(s0["trades_today"]) == 3
    btc_before = {k: v["btc"] for k, v in ledger.sleeves().items()}

    s_sell, s_buy, low, high = run_flip_cycle(ledger, base, "2026-01-01")
    assert {t["sleeve"] for t in s_sell["trades_today"]} == {"A", "B", "C"}
    check_fills(s_sell, "sell", low)
    for name, row in s_sell["sleeves"].items():
        assert row["signal"] == "CASH" and row["btc"] == 0
        t = next(t for t in s_sell["trades_today"] if t["sleeve"] == name)
        assert t["qty_btc"] == pytest.approx(btc_before[name], abs=1e-8)
    assert s_buy["sleeves"]["A"]["signal"] == "LONG"
    assert {t["sleeve"] for t in s_buy["trades_today"]} == {"A", "B", "C"}
    check_fills(s_buy, "buy", high)
    for row in s_buy["sleeves"].values():
        assert row["cash"] >= -1e-9 and row["cash"] < 0.01  # fully invested, never negative
    # ledger replay == state, cost guard untouched at 45 bps (<60)
    assert ledger.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 9
    assert s_buy["cost_guard"]["paused"] is False


@pytest.mark.skipif(not REAL_STATE.exists(), reason="paper state not present")
def test_flip_on_copy_of_paper_state(tmp_path):
    copy = tmp_path / "copy.sqlite"
    shutil.copyfile(REAL_STATE, copy)
    before_hash = REAL_STATE.read_bytes()
    ledger = Ledger(copy)
    last_done = ledger.db.execute("SELECT MAX(signal_day) FROM runs").fetchone()[0]
    sl = ledger.sleeves()
    base = 80000.0
    held = {k: v["btc"] for k, v in sl.items()}
    s_sell, s_buy, low, high = run_flip_cycle(ledger, base, last_done)
    for name, row in s_sell["sleeves"].items():
        assert row["btc"] == 0 and row["signal"] == "CASH"
        if held[name] > 0:
            t = next(t for t in s_sell["trades_today"] if t["sleeve"] == name)
            exp_cash = sl[name]["cash"] + held[name] * t["price"] * (1 - COST_PER_SIDE)
            assert row["cash"] == pytest.approx(exp_cash, abs=1e-3)
    check_fills(s_sell, "sell", low)
    check_fills(s_buy, "buy", high)
    assert REAL_STATE.read_bytes() == before_hash  # real state untouched
