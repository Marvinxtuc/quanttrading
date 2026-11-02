from datetime import datetime, timedelta, timezone

import pytest

from quanttrading.ensemble.ma_ensemble import MIN_ORDER_ETH, PAIRS, Ledger, format_summary, resolve_pair, run_once, run_step
from tests.test_ma_ensemble import make_bars, now_after


def test_resolve_pair():
    assert resolve_pair("ETHUSD") == resolve_pair("XETHZUSD") == resolve_pair("eth/usd") == "ETHUSD"
    assert resolve_pair("XBTUSD") == "XBTUSD"
    with pytest.raises(ValueError):
        resolve_pair("DOGEUSD")
    assert "XETHZUSD" in PAIRS["ETHUSD"][1] and MIN_ORDER_ETH == 0.002


def test_eth_run_once_uses_separate_state(tmp_path):
    bars = make_bars([2000 + i for i in range(300)])
    s = run_once(tmp_path, fetch=lambda: bars, now=now_after(bars), pair="ETHUSD")
    assert (tmp_path / "paper_eth_basket.sqlite").exists() and (tmp_path / "paper_eth_basket.log").exists()
    assert not (tmp_path / "paper_sma_basket.sqlite").exists()
    assert s["asset"] == "ETH" and s["min_order"] == 0.002 and s["orders_sent"] == 0
    assert len(s["trades_today"]) == 3 and abs(s["total_equity"] - 200) < 2
    assert " ETH" in format_summary(s)
    again = run_once(tmp_path, fetch=lambda: bars, now=now_after(bars), pair="ETHUSD")
    assert again["already_processed"]


def test_btc_default_unchanged(tmp_path):
    bars = make_bars([100 + i for i in range(300)])
    s = run_once(tmp_path, fetch=lambda: bars, now=now_after(bars))
    assert (tmp_path / "paper_sma_basket.sqlite").exists() and s["asset"] == "BTC"
    assert not (tmp_path / "paper_eth_basket.sqlite").exists()


def test_eth_min_order_skip(tmp_path):
    # price so high that 66.67 USD buys < 0.002 ETH -> skipped
    bars = make_bars([40000 + i for i in range(300)])
    ledger = Ledger(tmp_path / "e.sqlite")
    s = run_step(bars, ledger, now=now_after(bars), min_order=MIN_ORDER_ETH, asset="ETH")
    assert s["trades_today"] == [] and len(s["notes"]) == 3
    assert all("ETH below Kraken min 0.002" in n for n in s["notes"])


def test_eth_log_uses_qty_not_btc(tmp_path):
    import json
    bars = make_bars([2000 + i for i in range(300)])
    run_once(tmp_path, fetch=lambda: bars, now=now_after(bars), pair="ETHUSD")
    rec = json.loads((tmp_path / "paper_eth_basket.log").read_text().splitlines()[-1])
    for r in rec["sleeves"].values():
        assert r["asset"] == "ETH" and "qty" in r and "btc" not in r
    for t in rec["trades_today"]:
        assert t["asset"] == "ETH" and "qty" in t and "qty_btc" not in t


def test_btc_log_keeps_legacy_keys(tmp_path):
    bars = make_bars([100 + i for i in range(300)])
    s = run_once(tmp_path, fetch=lambda: bars, now=now_after(bars))
    for r in s["sleeves"].values():
        assert r["btc"] == r["qty"] and r["asset"] == "BTC"
    for t in s["trades_today"]:
        assert t["qty_btc"] == t["qty"]


def test_rerun_shows_recorded_trades(tmp_path):
    bars = make_bars([2000 + i for i in range(300)])
    first = run_once(tmp_path, fetch=lambda: bars, now=now_after(bars), pair="ETHUSD")
    again = run_once(tmp_path, fetch=lambda: bars, now=now_after(bars), pair="ETHUSD")
    assert again["trades_today"] == [] and len(again["trades_recorded_earlier"]) == 3
    out = format_summary(again)
    assert "recorded earlier" in out and "ALREADY PROCESSED" in out and first["run_ts"] in out


def test_legacy_summary_without_qty_formats():
    from quanttrading.ensemble.ma_ensemble import normalize_summary
    old = {"strategy_id": "x", "signal_day": "d", "signal_close": 1.0, "exec_day": "e", "exec_price": 1.0,
           "sleeves": {"a": {"sma_n": 150, "sma": 1, "signal": "LONG", "btc": 0.5, "cash": 0, "equity": 1}},
           "total_equity": 1, "peak_equity": 1, "drawdown": 0, "halted": False,
           "trades_today": [{"sleeve": "a", "side": "buy", "qty_btc": 0.5, "price": 1, "fill_type": "limit"}],
           "notes": [], "reconciliation": {"mismatches": 0}, "orders_sent": 0}
    n = normalize_summary(old)
    assert n["sleeves"]["a"]["qty"] == 0.5 and n["trades_today"][0]["qty"] == 0.5
    assert "qty=0.50000000 BTC" in format_summary(old)
