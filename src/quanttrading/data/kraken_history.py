"""Long-history Kraken BTC/USD OHLCV for paper CSVs.

Kraken's public REST OHLC endpoint returns at most ~720 bars per call
(~120 days of 4h). Multi-year history comes from Kraken's official
downloadable OHLCVT archives:

https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data

Preferred source file for 4h bars is ``XBTUSD_240.csv`` (240-minute
candles). Finer intervals (``XBTUSD_60.csv``, ``XBTUSD_1.csv``, …) can be
resampled to 4h UTC. Output matches ``quanttrading paper --data``.
"""

from __future__ import annotations

import hashlib
import shutil
import urllib.request
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quanttrading.data.ohlcv import save_csv
from quanttrading.market import Bar

# Official full-history parts (through 30 June 2026 per Kraken support).
KRAKEN_OHLCVT_BASE = "https://assets.kraken.com/marketing/institutions"
KRAKEN_OHLCVT_FULL_STEM = "Kraken_OHLCVT_Full_2026Q2.zip"
KRAKEN_OHLCVT_PARTS = tuple(f"{KRAKEN_OHLCVT_FULL_STEM}.part{i:02d}" for i in range(5))
KRAKEN_OHLCVT_ZIP_SHA256 = "fc81b54cba6e12af3e9422dde9416179e6ef76af4831d48d839fbdb43018eaa4"

# Pair + interval naming inside the archive (no header row).
DEFAULT_PAIR_PREFIX = "XBTUSD"
INTERVAL_MINUTES_4H = 240
BAR_DELTA_4H = timedelta(hours=4)

_DEFAULT_SYMBOL = "BTC/USD"


@dataclass(frozen=True)
class HistorySpan:
    """Inclusive bar span after conversion."""

    n_bars: int
    start: datetime
    end: datetime

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86_400.0

    @property
    def years(self) -> float:
        return self.days / 365.25


def kraken_part_urls() -> list[str]:
    return [f"{KRAKEN_OHLCVT_BASE}/{name}" for name in KRAKEN_OHLCVT_PARTS]


def download_file(url: str, dest: Path, *, chunk_size: int = 1 << 20) -> Path:
    """Download ``url`` to ``dest`` (resumes via temp file rewrite)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as out:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(dest)
    return dest


def download_kraken_ohlcvt_parts(dest_dir: Path, *, force: bool = False) -> list[Path]:
    """Download the five official full-history ZIP parts into ``dest_dir``."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, url in zip(KRAKEN_OHLCVT_PARTS, kraken_part_urls(), strict=True):
        path = dest_dir / name
        if path.exists() and path.stat().st_size > 0 and not force:
            paths.append(path)
            continue
        download_file(url, path)
        paths.append(path)
    return paths


def assemble_kraken_zip(parts: Iterable[Path], zip_path: Path) -> Path:
    """Concatenate ZIP parts into one archive (``cat part* > full.zip``)."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zip_path.open("wb") as out:
        for part in parts:
            with Path(part).open("rb") as fh:
                shutil.copyfileobj(fh, out, length=1 << 20)
    return zip_path


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_kraken_zip(zip_path: Path, *, expected: str = KRAKEN_OHLCVT_ZIP_SHA256) -> None:
    got = sha256_file(zip_path)
    if got != expected:
        raise ValueError(f"Kraken ZIP sha256 mismatch: got {got}, expected {expected}")


def extract_member(zip_path: Path, member_suffix: str, dest_dir: Path) -> Path:
    """Extract the first ZIP member whose name ends with ``member_suffix``."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        matches = [name for name in zf.namelist() if name.replace("\\", "/").endswith(member_suffix)]
        if not matches:
            raise FileNotFoundError(f"{member_suffix} not found in {zip_path}")
        member = matches[0]
        target = dest_dir / Path(member).name
        with zf.open(member) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out, length=1 << 20)
    return target


def parse_kraken_ohlcvt_row(
    row: str,
    *,
    symbol: str = _DEFAULT_SYMBOL,
) -> Bar | None:
    """Parse one Kraken OHLCVT line: ``timestamp,open,high,low,close,volume,trades``.

    ``timestamp`` is Unix seconds (UTC). Empty lines are skipped.
    """
    line = row.strip()
    if not line or line.startswith("#") or line.lower().startswith("timestamp"):
        return None
    parts = line.split(",")
    if len(parts) < 6:
        raise ValueError(f"expected at least 6 CSV fields, got {len(parts)}: {line[:80]!r}")
    ts = datetime.fromtimestamp(int(float(parts[0])), tz=timezone.utc)
    return Bar(
        ts=ts,
        symbol=symbol,
        open=float(parts[1]),
        high=float(parts[2]),
        low=float(parts[3]),
        close=float(parts[4]),
        volume=float(parts[5]),
    )


def load_kraken_ohlcvt_csv(path: Path, *, symbol: str = _DEFAULT_SYMBOL) -> list[Bar]:
    """Load a Kraken OHLCVT CSV (no header) into ``Bar`` rows."""
    bars: list[Bar] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            bar = parse_kraken_ohlcvt_row(line, symbol=symbol)
            if bar is not None:
                bars.append(bar)
    if not bars:
        raise ValueError(f"no bars in {path}")
    bars.sort(key=lambda b: b.ts)
    return bars


def _floor_4h_utc(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc)
    hour = (ts.hour // 4) * 4
    return ts.replace(hour=hour, minute=0, second=0, microsecond=0)


def resample_ohlcv_to_4h(bars: Iterable[Bar]) -> list[Bar]:
    """Aggregate finer bars into contiguous 4h UTC candles.

    Open = first open, high = max high, low = min low, close = last close,
    volume = sum. Empty buckets are omitted (Kraken OHLCVT also omits quiet
    intervals). Output is sorted by timestamp.
    """
    buckets: dict[datetime, list[Bar]] = {}
    for bar in bars:
        key = _floor_4h_utc(bar.ts)
        buckets.setdefault(key, []).append(bar)
    out: list[Bar] = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda b: b.ts)
        out.append(
            Bar(
                ts=key,
                symbol=group[0].symbol,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum(b.volume for b in group),
            )
        )
    return out


def ensure_4h_bars(bars: list[Bar], *, source_interval_minutes: int | None = None) -> list[Bar]:
    """Return 4h bars, resampling when the source interval is finer than 4h.

    When ``source_interval_minutes`` is ``240`` (or ``None`` and median step
    is already ~4h), bars are returned as-is after a sort. Gaps are kept.
    """
    if not bars:
        raise ValueError("no bars to normalize")
    ordered = sorted(bars, key=lambda b: b.ts)
    if source_interval_minutes is not None:
        if source_interval_minutes > INTERVAL_MINUTES_4H:
            raise ValueError(
                f"cannot build 4h bars from coarser interval {source_interval_minutes}m"
            )
        if source_interval_minutes < INTERVAL_MINUTES_4H:
            return resample_ohlcv_to_4h(ordered)
        return ordered
    if len(ordered) < 2:
        return ordered
    deltas = [(ordered[i].ts - ordered[i - 1].ts).total_seconds() for i in range(1, min(50, len(ordered)))]
    median = sorted(deltas)[len(deltas) // 2]
    if median < BAR_DELTA_4H.total_seconds() * 0.75:
        return resample_ohlcv_to_4h(ordered)
    return ordered


def span_of(bars: list[Bar]) -> HistorySpan:
    if not bars:
        raise ValueError("empty bar list")
    return HistorySpan(n_bars=len(bars), start=bars[0].ts, end=bars[-1].ts)


def write_paper_csv(bars: list[Bar], path: Path) -> HistorySpan:
    """Write bars in the ``quanttrading paper --data`` schema."""
    save_csv(bars, path)
    return span_of(bars)


def build_btcusd_4h_from_kraken_csv(
    source: Path,
    out: Path,
    *,
    symbol: str = _DEFAULT_SYMBOL,
    source_interval_minutes: int | None = None,
) -> HistorySpan:
    """Convert a Kraken ``XBTUSD_*.csv`` into a paper-compatible 4h CSV."""
    raw = load_kraken_ohlcvt_csv(source, symbol=symbol)
    bars = ensure_4h_bars(raw, source_interval_minutes=source_interval_minutes)
    return write_paper_csv(bars, out)


def iter_candidate_members(pair_prefix: str = DEFAULT_PAIR_PREFIX) -> Iterator[tuple[str, int]]:
    """Preferred archive members: native 4h first, then finer for resample."""
    yield f"{pair_prefix}_240.csv", 240
    yield f"{pair_prefix}_60.csv", 60
    yield f"{pair_prefix}_15.csv", 15
    yield f"{pair_prefix}_5.csv", 5
    yield f"{pair_prefix}_1.csv", 1


def extract_best_xbtusd(
    zip_path: Path,
    dest_dir: Path,
    *,
    pair_prefix: str = DEFAULT_PAIR_PREFIX,
) -> tuple[Path, int]:
    """Extract the finest preferred XBTUSD interval available from the ZIP."""
    with zipfile.ZipFile(zip_path) as zf:
        names = {Path(n.replace("\\", "/")).name: n for n in zf.namelist()}
    for member, minutes in iter_candidate_members(pair_prefix):
        if member in names:
            path = extract_member(zip_path, member, dest_dir)
            return path, minutes
    raise FileNotFoundError(f"no {pair_prefix}_*.csv candle file found in {zip_path}")


def fetch_kraken_btcusd_4h(
    *,
    out: Path,
    work_dir: Path,
    verify_checksum: bool = True,
    keep_zip: bool = False,
    source_csv: Path | None = None,
    pair_prefix: str = DEFAULT_PAIR_PREFIX,
) -> HistorySpan:
    """Build a multi-year paper 4h CSV from official Kraken OHLCVT data.

    If ``source_csv`` is already a local Kraken OHLCVT file, conversion runs
    offline. Otherwise the five full-history parts are downloaded, assembled,
    and the best ``XBTUSD_*`` member is extracted.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    if source_csv is not None:
        minutes = _infer_minutes_from_name(source_csv.name)
        return build_btcusd_4h_from_kraken_csv(
            source_csv,
            out,
            source_interval_minutes=minutes,
        )

    parts_dir = work_dir / "parts"
    parts = download_kraken_ohlcvt_parts(parts_dir)
    zip_path = work_dir / KRAKEN_OHLCVT_FULL_STEM
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        assemble_kraken_zip(parts, zip_path)
    if verify_checksum:
        verify_kraken_zip(zip_path)

    raw_dir = work_dir / "raw"
    source, minutes = extract_best_xbtusd(zip_path, raw_dir, pair_prefix=pair_prefix)
    span = build_btcusd_4h_from_kraken_csv(
        source,
        out,
        source_interval_minutes=minutes,
    )
    if not keep_zip:
        zip_path.unlink(missing_ok=True)
        for part in parts:
            part.unlink(missing_ok=True)
    return span


def _infer_minutes_from_name(name: str) -> int | None:
    stem = Path(name).stem
    if "_" not in stem:
        return None
    suffix = stem.rsplit("_", 1)[-1]
    if suffix.isdigit():
        return int(suffix)
    return None


__all__ = [
    "BAR_DELTA_4H",
    "DEFAULT_PAIR_PREFIX",
    "HistorySpan",
    "INTERVAL_MINUTES_4H",
    "KRAKEN_OHLCVT_BASE",
    "KRAKEN_OHLCVT_FULL_STEM",
    "KRAKEN_OHLCVT_PARTS",
    "KRAKEN_OHLCVT_ZIP_SHA256",
    "assemble_kraken_zip",
    "build_btcusd_4h_from_kraken_csv",
    "download_kraken_ohlcvt_parts",
    "ensure_4h_bars",
    "extract_best_xbtusd",
    "extract_member",
    "fetch_kraken_btcusd_4h",
    "kraken_part_urls",
    "load_kraken_ohlcvt_csv",
    "parse_kraken_ohlcvt_row",
    "resample_ohlcv_to_4h",
    "sha256_file",
    "span_of",
    "verify_kraken_zip",
    "write_paper_csv",
]
