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
    assert "eth=" in format_summary(s)
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
