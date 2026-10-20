from datetime import datetime, timedelta, timezone

from quanttrading.ensemble.ma_ensemble import (
    DailyBar, Ledger, MIN_ORDER_BTC, run_step, sma, target_position, simulate_fill,
)


def make_bars(closes, start="2025-01-01"):
    d0 = datetime.fromisoformat(start)
    return [DailyBar((d0 + timedelta(days=i)).date().isoformat(), c, c * 1.01, c * 0.99, c) for i, c in enumerate(closes)]


def now_after(bars):
    last = datetime.fromisoformat(bars[-1].day).replace(tzinfo=timezone.utc)
    return last + timedelta(minutes=5)  # last bar = in-progress "today"


def test_sma_and_signal():
    assert sma([1, 2, 3], 3) == 2
    assert sma([1, 2], 3) is None
    assert target_position(11, 10) == 1
    assert target_position(10, 10) == 0  # close <= SMA → cash
    assert target_position(5, None) == 0


def test_limit_then_market_fallback():
    bar = DailyBar("2025-01-01", 100, 101, 99, 100)
    assert simulate_fill("buy", 99.5, bar) == (99.5, "limit")
    px, kind = simulate_fill("buy", 98.0, bar)
    assert kind == "market_fallback" and px > 100


def test_initial_entry_and_idempotent(tmp_path):
    bars = make_bars([100 + i for i in range(300)])  # uptrend: all sleeves long
    ledger = Ledger(tmp_path / "s.sqlite")
    now = now_after(bars)
    s1 = run_step(bars, ledger, now=now, log_path=tmp_path / "log")
    assert len(s1["trades_today"]) == 3
    assert all(t["reason"] == "initial_entry" for t in s1["trades_today"])
    assert s1["reconciliation"]["mismatches"] == 0 and s1["orders_sent"] == 0
    s2 = run_step(bars, ledger, now=now + timedelta(hours=1), log_path=tmp_path / "log")
    assert s2["already_processed"] and s2["trades_today"] == []
    assert ledger.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 3


def test_flip_trades_only_changed_sleeve(tmp_path):
    closes = [100 + i for i in range(300)]
    ledger = Ledger(tmp_path / "s.sqlite")
    run_step(make_bars(closes), ledger, now=now_after(make_bars(closes)))
    # completed close 315: below SMA150 (~324) but above SMA200 (~300) and SMA250 (~275)
    closes2 = closes + [315.0, 315.0]
    bars2 = make_bars(closes2)
    s = run_step(bars2, ledger, now=now_after(bars2))
    sides = {t["sleeve"]: t["side"] for t in s["trades_today"]}
    assert sides == {"A": "sell"}, s
    assert s["reconciliation"]["mismatches"] == 0


def test_hard_stop_on_drawdown(tmp_path):
    closes = [100 + i for i in range(300)]
    ledger = Ledger(tmp_path / "s.sqlite")
    run_step(make_bars(closes), ledger, now=now_after(make_bars(closes)))
    crash = closes + [80.0, 80.0]  # ~80% drop from ~399
    bars = make_bars(crash)
    s = run_step(bars, ledger, now=now_after(bars))
    assert s["halted"] is True
    assert all(r["btc"] == 0 for r in s["sleeves"].values())


def test_min_order_skip(tmp_path):
    closes = [1e7 + i for i in range(300)]  # absurd price → qty below min
    bars = make_bars(closes)
    s = run_step(bars, Ledger(tmp_path / "s.sqlite"), now=now_after(bars))
    assert s["trades_today"] == [] and any("below Kraken min" in n for n in s["notes"])
    assert MIN_ORDER_BTC == 0.00005
