from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from quanttrading.backtest.runner import run_backtest, run_bars
from quanttrading.config import Settings, load_settings
from quanttrading.data.ohlcv import bundled_sample_path, fetch_ohlcv, generate_sample_bars, load_csv, save_csv
from quanttrading.execution.credentials import KrakenCredentialsError, load_kraken_credentials
from quanttrading.execution.dry_run import DryRunBroker, summarize_dry_run
from quanttrading.execution.live import (
    LIVE_PER_TRADE_DEFAULT,
    LIVE_STRATEGY_ID,
    LiveBroker,
    assert_live_per_trade_pct,
    kraken_trade_client,
    run_live_latest,
    summarize_live,
)
from quanttrading.execution.market_meta import CcxtPublicMarketMetadata, MarketMetadata, StaticMarketMetadata
from quanttrading.execution.paper import PaperBroker
from quanttrading.market import Bar
from quanttrading.status.server import assert_loopback, serve_status
from quanttrading.status.snapshot import timeframe_seconds
from quanttrading.strategy import (
    DEFAULT_ENTRY_Z,
    DEFAULT_EXIT_Z,
    DEFAULT_LOOKBACK,
    DEFAULT_MIN_VOL,
    DEFAULT_STRATEGY_ID,
    DEFAULT_VOL_WINDOW,
    HF_MIN_VOL,
    HF_VOL_WINDOW,
    DEFAULT_ROUND_TRIP_COST,
    DEFAULT_STOP_ATR,
    MEAN_REVERSION_STRATEGY_ID,
    SMA_CROSS_15M_STRATEGY_ID,
    SMA_CROSS_5M_STRATEGY_ID,
    TREND_BREAKOUT_STRATEGY_ID,
    Strategy,
    build_strategy,
)

_PAPER_STRATEGY_HELP = (
    f"{DEFAULT_STRATEGY_ID} (default), {SMA_CROSS_15M_STRATEGY_ID}, "
    f"{SMA_CROSS_5M_STRATEGY_ID}, {MEAN_REVERSION_STRATEGY_ID}, "
    f"or {TREND_BREAKOUT_STRATEGY_ID} (paper/dry-run, 4h bars)"
)
_SMA_FAST_HELP = "Fast SMA window (SMA cross strategies)."
_SMA_SLOW_HELP = "Slow SMA window (SMA cross strategies)."
_SMA_VOL_WINDOW_HELP = (
    "Realized-vol window for SMA cross strategies. "
    f"Default {DEFAULT_VOL_WINDOW} ({DEFAULT_STRATEGY_ID}) or "
    f"{HF_VOL_WINDOW} ({SMA_CROSS_15M_STRATEGY_ID}, {SMA_CROSS_5M_STRATEGY_ID})."
)
_SMA_MIN_VOL_HELP = (
    "Min realized vol to open for SMA cross strategies. "
    f"Default {DEFAULT_MIN_VOL:g} ({DEFAULT_STRATEGY_ID}) or "
    f"{HF_MIN_VOL:g} ({SMA_CROSS_15M_STRATEGY_ID}, {SMA_CROSS_5M_STRATEGY_ID})."
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Local crypto trading MVP. paper and dry-run never send orders. "
        "live sends Kraken orders for sma_cross_v2 only."
    ),
)


def _print_metrics(metrics: dict) -> None:
    typer.echo(json.dumps(metrics, indent=2, default=str))


def _load_bars(data: Path | None, symbol: str | None) -> tuple[list[Bar], Settings]:
    settings = load_settings()
    csv_path = data or bundled_sample_path()
    if not csv_path.exists():
        raise typer.BadParameter(f"no data at {csv_path}; run: quanttrading fetch  or  quanttrading sample-data")
    bars = load_csv(csv_path, default_symbol=symbol or settings.default_symbol)
    return bars, settings


def _build_strategy(
    settings: Settings,
    strategy_id: str,
    *,
    fast: int,
    slow: int,
    vol_window: int | None,
    min_vol: float | None,
    lookback: int,
    entry_z: float,
    exit_z: float,
    stop_atr: float = DEFAULT_STOP_ATR,
    round_trip_cost: float = DEFAULT_ROUND_TRIP_COST,
) -> Strategy:
    try:
        return build_strategy(
            strategy_id,
            fast=fast,
            slow=slow,
            vol_window=vol_window,
            min_vol=min_vol,
            lookback=lookback,
            entry_z=entry_z,
            exit_z=exit_z,
            max_slippage_bps=settings.max_slippage_bps,
            stop_atr=stop_atr,
            round_trip_cost=round_trip_cost,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def fetch(
    symbol: Optional[str] = typer.Option(None, help="ccxt symbol, e.g. BTC/USD"),
    timeframe: Optional[str] = typer.Option(None),
    exchange: Optional[str] = typer.Option(None, help="ccxt exchange id (public OHLCV only)"),
    limit: int = typer.Option(
        500,
        min=1,
        max=1000,
        help="Bars to request. Kraken public OHLC returns at most 720 per call.",
    ),
    out: Path = typer.Option(Path("data/btcusd_1h.csv")),
) -> None:
    """Fetch public OHLCV via ccxt (no API keys).

    Kraken's public OHLC endpoint caps one response at 720 bars. A 4h request
    at that cap is about 120 days. Longer paper history has to come from a CSV
    you already have.
    """
    settings = load_settings()
    try:
        bars = fetch_ohlcv(
            exchange_id=exchange or settings.exchange_id,
            symbol=symbol or settings.default_symbol,
            timeframe=timeframe or settings.default_timeframe,
            limit=limit,
        )
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    save_csv(bars, out)
    typer.echo(f"wrote {len(bars)} bars → {out}")


@app.command("sample-data")
def sample_data(
    out: Path = typer.Option(Path("data/btcusd_1h.csv")),
    n: int = typer.Option(480, min=50),
    symbol: str = typer.Option("BTC/USD"),
) -> None:
    """Write bundled-style synthetic BTC/USD hourly bars (offline, deterministic)."""
    bars = generate_sample_bars(symbol=symbol, n=n)
    save_csv(bars, out)
    typer.echo(f"wrote {len(bars)} synthetic bars → {out}")


@app.command()
def paper(
    data: Optional[Path] = typer.Option(None, help="OHLCV CSV. Defaults to bundled sample."),
    state: Optional[Path] = typer.Option(None, help="SQLite state path"),
    symbol: Optional[str] = typer.Option(None),
    strategy: str = typer.Option(
        DEFAULT_STRATEGY_ID,
        help=_PAPER_STRATEGY_HELP,
    ),
    fast: int = typer.Option(10, min=2, help=_SMA_FAST_HELP),
    slow: int = typer.Option(30, min=3, help=_SMA_SLOW_HELP),
    vol_window: Optional[int] = typer.Option(None, min=2, show_default=False, help=_SMA_VOL_WINDOW_HELP),
    min_vol: Optional[float] = typer.Option(None, min=0.0, show_default=False, help=_SMA_MIN_VOL_HELP),
    lookback: int = typer.Option(DEFAULT_LOOKBACK, min=2, help="Close lookback (mean_reversion_v1)."),
    entry_z: float = typer.Option(DEFAULT_ENTRY_Z, help="Open when z <= -entry_z (mean_reversion_v1)."),
    exit_z: float = typer.Option(DEFAULT_EXIT_Z, help="Flatten when z >= exit_z (mean_reversion_v1)."),
    stop_atr: float = typer.Option(
        DEFAULT_STOP_ATR,
        help="Initial and trailing ATR multiple (trend_breakout_v1). Default 2. The stop never moves down.",
    ),
    round_trip_cost: float = typer.Option(
        DEFAULT_ROUND_TRIP_COST,
        min=0.0,
        help=(
            "Assumed round-trip cost fraction for the trend_breakout_v1 cost filter. "
            "Default 0.003 (30 bps), inside the unverified 0.002–0.004 band. Not a Kraken fee quote."
        ),
    ),
) -> None:
    """Replay bars through a strategy, the paper broker, and execution risk gates."""
    bars, settings = _load_bars(data, symbol)
    store_path = state or settings.state_path
    broker = PaperBroker(settings, store_path=store_path)
    chosen = _build_strategy(
        settings,
        strategy,
        fast=fast,
        slow=slow,
        vol_window=vol_window,
        min_vol=min_vol,
        lookback=lookback,
        entry_z=entry_z,
        exit_z=exit_z,
        stop_atr=stop_atr,
        round_trip_cost=round_trip_cost,
    )
    result = run_bars(bars, chosen, broker)
    typer.echo(f"paper loop: {len(bars)} bars  strategy={chosen.strategy_id}  state={store_path}")
    _print_metrics(result.metrics)


def _market_metadata(public_markets: bool, exchange_id: str) -> MarketMetadata:
    if not public_markets:
        return StaticMarketMetadata()
    return CcxtPublicMarketMetadata(exchange_id)


@app.command("dry-run")
def dry_run(
    data: Optional[Path] = typer.Option(None, help="OHLCV CSV. Defaults to bundled sample."),
    state: Optional[Path] = typer.Option(None, help="SQLite dry-run state. Default state/dry_run.sqlite."),
    paper_state: Optional[Path] = typer.Option(
        None,
        "--paper-state",
        help="Optional paper SQLite to compare client_order_id / side / intent.",
    ),
    symbol: Optional[str] = typer.Option(None),
    strategy: str = typer.Option(
        DEFAULT_STRATEGY_ID,
        help=_PAPER_STRATEGY_HELP,
    ),
    per_trade_pct: float = typer.Option(
        0.005,
        min=1e-6,
        max=1.0,
        help="Trial fraction of equity per open. Default 0.5% (0.005), not the paper 1%.",
    ),
    public_markets: bool = typer.Option(
        False,
        "--public-markets",
        help="Validate size with public ccxt min qty / tick / last. No API keys.",
    ),
    exchange: Optional[str] = typer.Option(
        None,
        help="ccxt id for --public-markets only. Public metadata; keys are refused.",
    ),
    fast: int = typer.Option(10, min=2, help=_SMA_FAST_HELP),
    slow: int = typer.Option(30, min=3, help=_SMA_SLOW_HELP),
    vol_window: Optional[int] = typer.Option(None, min=2, show_default=False, help=_SMA_VOL_WINDOW_HELP),
    min_vol: Optional[float] = typer.Option(None, min=0.0, show_default=False, help=_SMA_MIN_VOL_HELP),
    lookback: int = typer.Option(DEFAULT_LOOKBACK, min=2, help="Close lookback (mean_reversion_v1)."),
    entry_z: float = typer.Option(DEFAULT_ENTRY_Z, help="Open when z <= -entry_z (mean_reversion_v1)."),
    exit_z: float = typer.Option(DEFAULT_EXIT_Z, help="Flatten when z >= exit_z (mean_reversion_v1)."),
    stop_atr: float = typer.Option(
        DEFAULT_STOP_ATR,
        help="Initial and trailing ATR multiple (trend_breakout_v1).",
    ),
    round_trip_cost: float = typer.Option(
        DEFAULT_ROUND_TRIP_COST,
        min=0.0,
        help="Assumed round-trip cost fraction for trend_breakout_v1. Default 0.003.",
    ),
) -> None:
    """Replay signals through risk gates and record would-be orders. Sends nothing."""
    bars, settings = _load_bars(data, symbol)
    settings = settings.model_copy(update={"per_trade_pct": per_trade_pct})
    store_path = state or settings.dry_run_state_path
    if paper_state is not None and not paper_state.exists():
        raise typer.BadParameter(f"paper state not found: {paper_state}")
    try:
        market = _market_metadata(public_markets, exchange or settings.exchange_id)
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    broker = DryRunBroker(settings, store_path=store_path, market=market)
    if public_markets and bars:
        try:
            broker.market_constraints[bars[0].symbol] = market.for_symbol(bars[0].symbol)
        except RuntimeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    chosen = _build_strategy(
        settings,
        strategy,
        fast=fast,
        slow=slow,
        vol_window=vol_window,
        min_vol=min_vol,
        lookback=lookback,
        entry_z=entry_z,
        exit_z=exit_z,
        stop_atr=stop_atr,
        round_trip_cost=round_trip_cost,
    )
    run_bars(bars, chosen, broker)
    try:
        summary = summarize_dry_run(broker, paper_state=paper_state, strategy_id=chosen.strategy_id)
    except (FileNotFoundError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    reconcile = summary.get("paper_reconcile")
    mismatch_note = ""
    if isinstance(reconcile, dict):
        mismatch_note = f"  paper_mismatches={reconcile['n_mismatches']}"
    typer.echo(
        f"dry-run: {len(bars)} bars  strategy={chosen.strategy_id}  "
        f"per_trade_pct={settings.per_trade_pct:g}  "
        f"signals={summary['n_signals']}  would_send={summary['n_would_send']}  "
        f"risk_rejected={summary['n_risk_rejected']}  size_invalid={summary['n_size_invalid']}"
        f"{mismatch_note}  orders_sent=0  state={store_path}"
    )
    _print_metrics(summary)


@app.command()
def live(
    data: Optional[Path] = typer.Option(
        None,
        help="OHLCV CSV. Required unless --fetch. The bundled sample is never traded.",
    ),
    fetch: bool = typer.Option(
        False,
        "--fetch",
        help="Load public Kraken OHLCV, then send at most the latest bar.",
    ),
    state: Optional[Path] = typer.Option(None, help="SQLite live state. Default state/live.sqlite."),
    symbol: Optional[str] = typer.Option(None),
    timeframe: Optional[str] = typer.Option(None, help="Used by --fetch. Default from settings (1h)."),
    limit: int = typer.Option(200, min=10, max=1000, help="Bars to load with --fetch."),
    strategy: str = typer.Option(
        LIVE_STRATEGY_ID,
        help=(
            "sma_cross_v2 only. 15m/5m paper variants, mean_reversion_v1, "
            "and trend_breakout_v1 are refused."
        ),
    ),
    per_trade_pct: float = typer.Option(
        LIVE_PER_TRADE_DEFAULT,
        help="Trial fraction of equity per open. Live allows 0.002–0.005 (0.2%–0.5%). Default 0.005.",
    ),
    exchange: Optional[str] = typer.Option(None, help="Must be kraken."),
    fast: int = typer.Option(10, min=2, help="Fast SMA window."),
    slow: int = typer.Option(30, min=3, help="Slow SMA window."),
    vol_window: int = typer.Option(DEFAULT_VOL_WINDOW, min=2, help="Realized-vol window."),
    min_vol: float = typer.Option(DEFAULT_MIN_VOL, min=0.0, help="Min realized vol to open."),
) -> None:
    """Send Kraken market orders for sma_cross_v2. Requires KRAKEN_API_KEY and KRAKEN_API_SECRET.

    Keys are read from the environment or .env (gitignored). Missing or placeholder
    values refuse to start. The key must be trade-only: this client cannot withdraw.
    Only the latest bar can send an order. Earlier bars warm the strategy and are
    not replayed onto the account. Fills and rejects go to a separate live SQLite file.
    """
    if strategy != LIVE_STRATEGY_ID:
        raise typer.BadParameter(f"live orders are enabled for {LIVE_STRATEGY_ID} only")
    try:
        assert_live_per_trade_pct(per_trade_pct)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    settings = load_settings()
    venue = (exchange or settings.exchange_id).strip().lower()
    if venue != "kraken":
        raise typer.BadParameter("live orders are Kraken only")
    if fetch and data is not None:
        raise typer.BadParameter("pass either --data or --fetch, not both")
    if not fetch and data is None:
        raise typer.BadParameter(
            "live requires --data or --fetch; it does not trade the bundled sample"
        )
    try:
        api_key, api_secret = load_kraken_credentials()
    except KrakenCredentialsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    settings = settings.model_copy(update={"per_trade_pct": per_trade_pct})
    bar_symbol = symbol or settings.default_symbol
    bar_timeframe = timeframe or settings.default_timeframe
    if fetch:
        try:
            bars = fetch_ohlcv(
                exchange_id=venue,
                symbol=bar_symbol,
                timeframe=bar_timeframe,
                limit=limit,
            )
        except RuntimeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    else:
        assert data is not None
        if not data.exists():
            raise typer.BadParameter(f"no data at {data}")
        try:
            bars = load_csv(data, default_symbol=bar_symbol)
        except (OSError, ValueError, KeyError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    if not bars:
        typer.echo("live requires at least one bar", err=True)
        raise typer.Exit(code=1)
    symbols = {bar.symbol for bar in bars}
    if len(symbols) != 1:
        raise typer.BadParameter("live requires a single symbol")
    trading_symbol = bars[0].symbol

    try:
        market = CcxtPublicMarketMetadata(venue)
        market.for_symbol(trading_symbol)
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    try:
        client = kraken_trade_client(api_key, api_secret)
        broker = LiveBroker(
            settings,
            store_path=state or settings.live_state_path,
            enable_trading=True,
            client=client,
            market=market,
            symbol=trading_symbol,
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    broker.store.set_meta("timeframe", bar_timeframe)
    broker.store.commit()
    chosen = _build_strategy(
        settings,
        strategy,
        fast=fast,
        slow=slow,
        vol_window=vol_window,
        min_vol=min_vol,
        lookback=DEFAULT_LOOKBACK,
        entry_z=DEFAULT_ENTRY_Z,
        exit_z=DEFAULT_EXIT_Z,
    )
    try:
        report = run_live_latest(bars, chosen, broker)
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    summary = summarize_live(
        broker,
        strategy_id=chosen.strategy_id,
        n_bars=len(bars),
        last_bar_ts=bars[-1].ts.isoformat().replace("+00:00", "Z"),
        report=report,
    )
    store_path = state or settings.live_state_path
    typer.echo(
        f"live: {len(bars)} bars  strategy={chosen.strategy_id}  "
        f"per_trade_pct={settings.per_trade_pct:g}  "
        f"orders_sent={summary['orders_sent']}  state={store_path}"
    )
    _print_metrics(summary)
    broker.store.close()


@app.command()
def backtest(
    data: Optional[Path] = typer.Option(None),
    symbol: Optional[str] = typer.Option(None),
    strategy: str = typer.Option(
        DEFAULT_STRATEGY_ID,
        help=_PAPER_STRATEGY_HELP,
    ),
    fast: int = typer.Option(10, min=2, help=_SMA_FAST_HELP),
    slow: int = typer.Option(30, min=3, help=_SMA_SLOW_HELP),
    vol_window: Optional[int] = typer.Option(None, min=2, show_default=False, help=_SMA_VOL_WINDOW_HELP),
    min_vol: Optional[float] = typer.Option(None, min=0.0, show_default=False, help=_SMA_MIN_VOL_HELP),
    lookback: int = typer.Option(DEFAULT_LOOKBACK, min=2, help="Close lookback (mean_reversion_v1)."),
    entry_z: float = typer.Option(DEFAULT_ENTRY_Z, help="Open when z <= -entry_z (mean_reversion_v1)."),
    exit_z: float = typer.Option(DEFAULT_EXIT_Z, help="Flatten when z >= exit_z (mean_reversion_v1)."),
    stop_atr: float = typer.Option(DEFAULT_STOP_ATR, help="Initial and trailing ATR multiple (trend_breakout_v1)."),
    round_trip_cost: float = typer.Option(
        DEFAULT_ROUND_TRIP_COST,
        min=0.0,
        help="Assumed round-trip cost fraction for trend_breakout_v1. Default 0.003.",
    ),
) -> None:
    """Historical bars + pluggable strategy, same signal → execution interface as paper."""
    bars, settings = _load_bars(data, symbol)
    chosen = _build_strategy(
        settings,
        strategy,
        fast=fast,
        slow=slow,
        vol_window=vol_window,
        min_vol=min_vol,
        lookback=lookback,
        entry_z=entry_z,
        exit_z=exit_z,
        stop_atr=stop_atr,
        round_trip_cost=round_trip_cost,
    )
    result = run_backtest(bars, chosen, settings)
    typer.echo(f"backtest: {len(bars)} bars  strategy={chosen.strategy_id}")
    _print_metrics(result.metrics)


@app.command("status-ui")
def status_ui(
    state: Path = typer.Option(
        Path("state/paper.sqlite"),
        "--state",
        help="Paper, dry-run, or live SQLite book. Read-only.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Loopback bind address. Non-loopback hosts are refused."),
    port: int = typer.Option(8787, "--port", min=1, max=65535),
    refresh_sec: int = typer.Option(10, "--refresh-sec", min=5, max=15, help="Browser refresh interval, 5–15 seconds."),
    heartbeat: Optional[Path] = typer.Option(
        None,
        "--heartbeat",
        help="Optional JSON heartbeat. Default: <state stem>.heartbeat.json when that file exists.",
    ),
    timeframe: Optional[str] = typer.Option(
        None,
        "--timeframe",
        help="Bar size for the stale threshold (2×). Example: 1h, 15m. Default: heartbeat file, else 1h.",
    ),
) -> None:
    """Serve a local read-only status page. Does not place orders, write state, or load API keys."""
    try:
        assert_loopback(host)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if timeframe is not None:
        try:
            timeframe_seconds(timeframe)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if heartbeat is not None and not heartbeat.is_file():
        typer.echo(f"heartbeat file not found: {heartbeat}", err=True)
        raise typer.Exit(code=1)
    try:
        serve_status(
            state=state,
            host=host,
            port=port,
            refresh_sec=refresh_sec,
            heartbeat=heartbeat,
            timeframe=timeframe,
        )
    except OSError as exc:
        typer.echo(f"could not bind {host}:{port}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def main() -> None:
    app()


if __name__ == "__main__":
    main()
