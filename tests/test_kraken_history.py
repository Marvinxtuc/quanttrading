"""Kraken OHLCVT → paper 4h CSV conversion (offline)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from quanttrading.data.kraken_history import (
    build_btcusd_4h_from_kraken_csv,
    ensure_4h_bars,
    load_kraken_ohlcvt_csv,
    parse_kraken_ohlcvt_row,
    resample_ohlcv_to_4h,
    span_of,
)
from quanttrading.data.ohlcv import load_csv
from quanttrading.market import Bar


def _kraken_line(ts: int, o: float, h: float, l: float, c: float, v: float, trades: int = 1) -> str:
    return f"{ts},{o},{h},{l},{c},{v},{trades}"


def test_parse_kraken_ohlcvt_row() -> None:
    bar = parse_kraken_ohlcvt_row(_kraken_line(1_700_000_000, 10, 12, 9, 11, 3.5, 7))
    assert bar is not None
    assert bar.open == 10.0
    assert bar.high == 12.0
    assert bar.low == 9.0
    assert bar.close == 11.0
    assert bar.volume == 3.5
    assert bar.symbol == "BTC/USD"
    assert bar.ts == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)


def test_resample_1h_to_4h_utc() -> None:
    start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    bars: list[Bar] = []
    price = 100.0
    for i in range(8):
        ts = start.replace(hour=i)
        bars.append(
            Bar(
                ts=ts,
                symbol="BTC/USD",
                open=price,
                high=price + 1,
                low=price - 1,
                close=price + 0.5,
                volume=1.0,
            )
        )
        price += 1.0
    out = resample_ohlcv_to_4h(bars)
    assert len(out) == 2
    assert out[0].ts.hour == 0
    assert out[0].open == 100.0
    assert out[0].close == 103.5
    assert out[0].volume == 4.0
    assert out[1].ts.hour == 4


def test_build_paper_csv_from_kraken_file(tmp_path: Path) -> None:
    raw = tmp_path / "XBTUSD_60.csv"
    # Two 4h buckets of 1h bars.
    lines = []
    base = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())
    px = 60_000.0
    for i in range(8):
        lines.append(_kraken_line(base + i * 3600, px, px + 10, px - 10, px + 5, 2.0, 3))
        px += 20
    raw.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "btcusd_4h.csv"
    span = build_btcusd_4h_from_kraken_csv(raw, out, source_interval_minutes=60)
    assert span.n_bars == 2
    paper = load_csv(out)
    assert paper[0].symbol == "BTC/USD"
    assert len(paper) == 2
    assert paper[0].open == 60_000.0


def test_ensure_4h_passthrough_when_already_4h() -> None:
    bars = [
        Bar(
            ts=datetime(2024, 1, 1, i * 4, tzinfo=timezone.utc),
            symbol="BTC/USD",
            open=1.0,
            high=1.1,
            low=0.9,
            close=1.05,
            volume=1.0,
        )
        for i in range(5)
    ]
    out = ensure_4h_bars(bars, source_interval_minutes=240)
    assert len(out) == 5
    assert span_of(out).n_bars == 5


def test_load_kraken_skips_blank_and_header(tmp_path: Path) -> None:
    path = tmp_path / "XBTUSD_240.csv"
    path.write_text(
        "\n".join(
            [
                "",
                "timestamp,open,high,low,close,volume,trades",
                _kraken_line(1_700_000_000, 1, 2, 0.5, 1.5, 9, 1),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bars = load_kraken_ohlcvt_csv(path)
    assert len(bars) == 1
