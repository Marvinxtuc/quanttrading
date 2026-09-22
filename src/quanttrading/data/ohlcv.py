from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quanttrading.market import Bar

_CSV_FIELDS = ("timestamp", "symbol", "open", "high", "low", "close", "volume")


def fetch_ohlcv(
    *,
    exchange_id: str = "kraken",
    symbol: str = "BTC/USD",
    timeframe: str = "1h",
    limit: int = 500,
    since_ms: int | None = None,
) -> list[Bar]:
    """Public ccxt OHLCV only — no API keys, no private endpoints.

    Kraken's OHLC endpoint returns at most 720 bars per call. ``limit`` above
    that is still sent, and Kraken truncates the response.
    """
    import ccxt

    exchange_cls = getattr(ccxt, exchange_id, None)
    if exchange_cls is None:
        raise ValueError(f"unknown ccxt exchange id: {exchange_id}")
    exchange = exchange_cls({"enableRateLimit": True, "timeout": 20_000})
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)
    except Exception as exc:
        raise RuntimeError(
            f"public OHLCV fetch failed for {exchange_id} {symbol}: {exc}. "
            "Default venue is Kraken public OHLCV (BTC/USD). "
            "Retry with --exchange kraken --symbol BTC/USD "
            "(or another public ccxt id) "
            "or generate offline bars: quanttrading sample-data"
        ) from exc
    bars: list[Bar] = []
    for ts_ms, o, h, l, c, v in raw:
        bars.append(
            Bar(
                ts=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
                symbol=symbol,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
            )
        )
    return bars


def save_csv(bars: list[Bar], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for bar in bars:
            writer.writerow(
                {
                    "timestamp": bar.ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "symbol": bar.symbol,
                    "open": f"{bar.open:.8f}",
                    "high": f"{bar.high:.8f}",
                    "low": f"{bar.low:.8f}",
                    "close": f"{bar.close:.8f}",
                    "volume": f"{bar.volume:.8f}",
                }
            )


def load_csv(path: Path, default_symbol: str = "BTC/USD") -> list[Bar]:
    bars: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            bars.append(
                Bar(
                    ts=ts,
                    symbol=row.get("symbol") or default_symbol,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
    if not bars:
        raise ValueError(f"no bars in {path}")
    return bars


def generate_sample_bars(
    *,
    symbol: str = "BTC/USD",
    n: int = 480,
    start: datetime | None = None,
    seed: int = 42,
    start_price: float = 42000.0,
) -> list[Bar]:
    """Deterministic synthetic BTC-like hourly bars with regime flips so SMA crosses fire."""
    rng = random.Random(seed)
    ts = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = start_price
    bars: list[Bar] = []
    for i in range(n):
        regime = 1.0 if (i // 80) % 2 == 0 else -1.0
        ret = regime * 0.0018 + rng.gauss(0.0, 0.0012)
        open_px = price
        close_px = max(1.0, open_px * (1.0 + ret))
        wick = abs(rng.gauss(0.0, 0.0005))
        high = max(open_px, close_px) * (1.0 + wick)
        low = min(open_px, close_px) * (1.0 - wick)
        volume = 80.0 + rng.random() * 40.0
        bars.append(
            Bar(
                ts=ts,
                symbol=symbol,
                open=open_px,
                high=high,
                low=low,
                close=close_px,
                volume=volume,
            )
        )
        price = close_px
        ts += timedelta(hours=1)
    return bars


def bundled_sample_path() -> Path:
    return Path(__file__).resolve().parent.parent / "samples" / "btcusdt_1h.csv"
