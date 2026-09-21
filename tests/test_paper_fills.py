from quanttrading.backtest.runner import run_backtest
from quanttrading.config import Settings
from quanttrading.data.ohlcv import generate_sample_bars
from quanttrading.execution.fills import simulate_fill
from quanttrading.execution.paper import PaperBroker, fill_price_market
from quanttrading.strategy.sma import SMACrossover
from tests.helpers import make_bar, make_signal


def test_market_buy_sell_slippage() -> None:
    bar = make_bar(close=100.0, high=101.0, low=99.0)
    buy = simulate_fill(make_signal(side="buy", qty=1.0, qty_unit="base", max_slippage_bps=10), bar)
    sell = simulate_fill(
        make_signal(side="sell", intent="close", qty=1.0, qty_unit="base", max_slippage_bps=10),
        bar,
    )
    assert buy is not None and sell is not None
    assert buy.price == fill_price_market(100.0, "buy", 10)
    assert sell.price == fill_price_market(100.0, "sell", 10)
    assert abs(buy.price - 100.1) < 1e-12
    assert abs(sell.price - 99.9) < 1e-12
    assert abs(buy.notional - buy.qty_base * buy.price) < 1e-12


def test_limit_fill_only_if_touchable() -> None:
    bar = make_bar(close=100.0, high=101.0, low=99.0)
    missed = simulate_fill(
        make_signal(order_type="limit", limit_price=98.0, qty=1.0, qty_unit="base"),
        bar,
    )
    hit = simulate_fill(
        make_signal(order_type="limit", limit_price=99.5, qty=1.0, qty_unit="base"),
        bar,
    )
    sell_miss = simulate_fill(
        make_signal(
            side="sell",
            intent="close",
            order_type="limit",
            limit_price=102.0,
            qty=1.0,
            qty_unit="base",
        ),
        bar,
    )
    sell_hit = simulate_fill(
        make_signal(
            side="sell",
            intent="close",
            order_type="limit",
            limit_price=100.5,
            qty=1.0,
            qty_unit="base",
        ),
        bar,
    )
    assert missed is None
    assert sell_miss is None
    assert hit is not None and hit.price == 99.5
    assert sell_hit is not None and sell_hit.price == 100.5


def test_paper_broker_applies_market_fill_to_cash_and_position() -> None:
    settings = Settings(paper_equity=2000, per_trade_pct=0.01)
    broker = PaperBroker(settings, store_path=":memory:")
    bar = make_bar(close=100.0)
    report = broker.submit(
        make_signal(qty=20.0, qty_unit="quote", max_slippage_bps=10, order_type="market"),
        bar,
    )
    assert report.status == "filled"
    qty_base = 20.0 / 100.0
    fill_px = 100.0 * (1.0 + 10 / 10_000.0)
    assert report.fill_price == fill_px
    assert abs(report.qty_base - qty_base) < 1e-12
    assert abs(broker.position_qty(bar.symbol) - qty_base) < 1e-12
    assert abs(broker.cash - (2000.0 - qty_base * fill_px)) < 1e-9
    expected_eq = broker.cash + qty_base * bar.close
    assert abs(broker.equity() - expected_eq) < 1e-9


def test_sma_pipeline_emits_fills() -> None:
    result = run_backtest(generate_sample_bars(n=240), SMACrossover())
    assert result.metrics["n_fills"] >= 1
    assert "sharpe" in result.metrics
    assert "max_dd" in result.metrics
    assert result.metrics["halted"] in (0, 1)
