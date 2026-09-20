from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from quanttrading.backtest.runner import run_backtest, run_bars
from quanttrading.config import load_settings
from quanttrading.data.ohlcv import bundled_sample_path, fetch_ohlcv, generate_sample_bars, load_csv, save_csv
from quanttrading.execution.paper import PaperBroker
from quanttrading.strategy.sma import SMACrossover

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Local crypto paper-trading MVP.")


def _print_metrics(metrics: dict) -> None:
    typer.echo(json.dumps(metrics, indent=2, default=str))


@app.command()
def fetch(
    symbol: Optional[str] = typer.Option(None, help="ccxt symbol, e.g. BTC/USDT or BTC/USDT:USDT"),
    timeframe: Optional[str] = typer.Option(None),
    exchange: Optional[str] = typer.Option(None, help="ccxt exchange id (public OHLCV only)"),
    limit: int = typer.Option(500, min=1, max=1000),
    out: Path = typer.Option(Path("data/btcusdt_1h.csv")),
) -> None:
    """Fetch public OHLCV via ccxt (no API keys)."""
    settings = load_settings()
    bars = fetch_ohlcv(
        exchange_id=exchange or settings.exchange_id,
        symbol=symbol or settings.default_symbol,
        timeframe=timeframe or settings.default_timeframe,
        limit=limit,
    )
    save_csv(bars, out)
    typer.echo(f"wrote {len(bars)} bars → {out}")


@app.command("sample-data")
def sample_data(
    out: Path = typer.Option(Path("data/btcusdt_1h.csv")),
    n: int = typer.Option(480, min=50),
    symbol: str = typer.Option("BTC/USDT"),
) -> None:
    """Write bundled-style synthetic BTC/USDT hourly bars (offline, deterministic)."""
    bars = generate_sample_bars(symbol=symbol, n=n)
    save_csv(bars, out)
    typer.echo(f"wrote {len(bars)} synthetic bars → {out}")


@app.command()
def paper(
    data: Optional[Path] = typer.Option(None, help="OHLCV CSV. Defaults to bundled sample."),
    state: Optional[Path] = typer.Option(None, help="SQLite state path"),
    symbol: Optional[str] = typer.Option(None),
    fast: int = typer.Option(10, min=2),
    slow: int = typer.Option(30, min=3),
) -> None:
    """Replay bars through SMA stub + paper broker + risk gates."""
    settings = load_settings()
    csv_path = data or bundled_sample_path()
    if not csv_path.exists():
        raise typer.BadParameter(f"no data at {csv_path}; run: quanttrading fetch  or  quanttrading sample-data")
    bars = load_csv(csv_path, default_symbol=symbol or settings.default_symbol)
    store_path = state or settings.state_path
    broker = PaperBroker(settings, store_path=store_path)
    strategy = SMACrossover(fast=fast, slow=slow)
    result = run_bars(bars, strategy, broker)
    typer.echo(f"paper loop: {len(bars)} bars  state={store_path}")
    _print_metrics(result.metrics)


@app.command()
def backtest(
    data: Optional[Path] = typer.Option(None),
    symbol: Optional[str] = typer.Option(None),
    fast: int = typer.Option(10, min=2),
    slow: int = typer.Option(30, min=3),
) -> None:
    """Historical bars + pluggable strategy, same signal → execution interface as paper."""
    settings = load_settings()
    csv_path = data or bundled_sample_path()
    if not csv_path.exists():
        raise typer.BadParameter(f"no data at {csv_path}")
    bars = load_csv(csv_path, default_symbol=symbol or settings.default_symbol)
    result = run_backtest(bars, SMACrossover(fast=fast, slow=slow), settings)
    typer.echo(f"backtest: {len(bars)} bars  strategy=sma_cross_v1")
    _print_metrics(result.metrics)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
