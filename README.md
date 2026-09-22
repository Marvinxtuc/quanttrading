# quanttrading

Local crypto **paper-trading** MVP: public market data → strategy signals → backtest or paper execution → hard risk kill-switch.

This is **not** a broker platform. There is no dashboard, no multi-exchange router, and no live order placement. `LiveBroker` is a stub that raises: it never loads API keys and never sends orders. `quanttrading dry-run` is the pre-live check for `sma_cross_v2` — same risk gates and a would-be order record, still with no keys and no private exchange calls. The paper path never loads API keys.

## Baselines (locked in config)

| Item | Default |
| --- | --- |
| Market | Crypto via [ccxt](https://github.com/ccxt/ccxt), **Kraken** public OHLCV (`BTC/USD`) |
| Paper wallet | **2000 USD** (`PAPER_EQUITY`) |
| Per-trade cap | **1%** of equity (20 USD at start) |
| Daily loss breaker | **3%** of day-start equity (60 USD at start) |
| Max total drawdown | **20%** from peak (400 USD at start) → persist halt, reject new **open** orders |

Risk limits are enforced in the **execution** layer, not the strategy. Backtest and paper share the same `Signal → ExecutionBackend.submit` interface; only the store (in-memory vs SQLite) differs.

## Install

Python 3.11+. Prefer `uv`, or pip + `pyproject.toml`.

```bash
# uv
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# pip
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Commands are `quanttrading` (console script) or `python -m quanttrading`.

Copy `.env.example` to `.env` if you want to override defaults. Do not put secrets in the repo; `.gitignore` excludes `.env`, `state/`, and SQLite files.

## Fetch sample data

Public OHLCV only (no keys):

```bash
quanttrading fetch --symbol BTC/USD --timeframe 1h --out data/btcusd_1h.csv
```

Default venue is Kraken (`BTC/USD`). Override `--exchange` / `--symbol` if needed, or generate bars offline:

```bash
quanttrading fetch --exchange kraken --symbol BTC/USD --timeframe 1h --out data/btcusd_1h.csv
quanttrading sample-data --out data/btcusd_1h.csv
```

A deterministic synthetic sample is also bundled at `src/quanttrading/samples/btcusdt_1h.csv`, so `quanttrading paper` runs without network.

## Run the paper loop

Default strategy is `sma_cross_v2`. `--strategy mean_reversion_v1` runs the short-window mean-reversion comparison on the same Kraken `BTC/USD` bars, paper broker, and risk gates (per-trade 1%, daily loss breaker 3%, max drawdown 20%).

```bash
# sma_cross_v2 (default — omitting --strategy is the same)
quanttrading paper
quanttrading paper --strategy sma_cross_v2 --data data/btcusd_1h.csv --state state/paper.sqlite

# mean_reversion_v1
quanttrading paper --strategy mean_reversion_v1
quanttrading paper --strategy mean_reversion_v1 --data data/btcusd_1h.csv --state state/paper_mr.sqlite --lookback 20 --entry-z 1.5 --exit-z 0
```

Same pipeline as a historical backtest (in-memory store):

```bash
quanttrading backtest --data data/btcusd_1h.csv
quanttrading backtest --strategy mean_reversion_v1 --data data/btcusd_1h.csv
```

Both print metrics: trade days, Sharpe (crypto, 365), max drawdown, win rate, profit factor, halt state.

### Comparing `sma_cross_v2` and `mean_reversion_v1`

Compare the two on the **same bars** using **fills** (`n_fills`), **drawdown** (`max_dd`), and **invalid or filtered signals** — not short-window Sharpe. The printed `sharpe` is not a ranking metric here: mean reversion uses a 10–20 bar lookback, and Sharpe on that horizon is noise.

- **Fills / drawdown:** `n_fills` and `max_dd` in the paper or backtest JSON. Use a separate `--state` file per strategy so the SQLite books do not mix.
- **Filtered signals:** `sma_cross_v2` drops new opens when realized vol is missing or below `min_vol` (no Signal is emitted; closes are not filtered). `mean_reversion_v1` does not use that vol filter. It emits no open when flat and the close is not displaced (`z > -entry_z`).
- **Invalid signals:** the execution layer still rejects opens that break the 1% / 3% / 20% caps. Those land in the state SQLite `fills` table with `status=rejected` and a reason (`per_trade_limit`, `daily_circuit_breaker`, `max_drawdown`). Flatten/close stays allowed.

### Default strategy (`sma_cross_v2`)

Long-only fast/slow SMA crossover (defaults 10 / 30) plus a **min-vol filter** on whatever symbol the feed provides. Market orders only. An open is sized at `equity * PER_TRADE_PCT` in quote USD; a close flattens the full base position.

**Volatility rule (opens only):** realized vol is the population std of simple close-to-close returns over `vol_window` (default 20). New **open** signals fire only when that vol is known and `>= min_vol` (default `0.0005`). Dead/chop crosses are skipped. **Closes are not filtered**, so exits and halt-flatten still work in quiet markets. `vol_window`, `min_vol`, `realized_vol`, and `vol_ok` are stored on each Signal `meta` (and the strategy id is `sma_cross_v2`) so paper runs are auditable.

`client_order_id` is deterministic from strategy id, symbol, bar timestamp, intent, and side so paper replays are stable. Risk kill-switches stay in the execution layer.

`--fast`, `--slow`, `--vol-window`, and `--min-vol` apply to `sma_cross_v2` only.

### Comparison strategy (`mean_reversion_v1`)

Long-only mean reversion on a short close window (default lookback **20**). Market orders only, same sizing path as `sma_cross_v2`: an open is `equity * PER_TRADE_PCT` in quote USD; a close flattens the full base position.

**Rule:** z-score of the latest close versus the lookback SMA (population std). While flat, z `<= -entry_z` (default **1.5**) → market buy, intent=open. While long, z `>= exit_z` (default **0**, back at the mean) → market sell, intent=close. Prices above the mean do not open a short. No second open while already long.

The SMA min-vol filter is not applied. That filter skips quiet *trend* opens; this strategy's entry is the displacement itself.

`lookback`, `entry_z`, `exit_z`, `threshold` (`-entry_z`), `mean`, `std`, `z`, and `close` are stored on each Signal `meta` (strategy id `mean_reversion_v1`) so paper runs are auditable.

`--lookback`, `--entry-z`, and `--exit-z` apply to `mean_reversion_v1` only.

## Dry-run (pre-live, no orders)

`quanttrading dry-run` replays the same bars → strategy → `ExecutionBackend.submit` path as paper, then records the order that **would** be sent. It does not place exchange orders and does not read API keys. `LiveBroker.submit` still raises. There is no `--live` command.

Default strategy is `sma_cross_v2`. `mean_reversion_v1` stays selectable. Trial size defaults to **0.5%** of equity (`--per-trade-pct 0.005`), not the paper 1%. Use `0.002`–`0.005` for a 0.2%–0.5% trial. Pass `--per-trade-pct 0.01` when you want the dry-run book to match a paper run that used the config default.

```bash
quanttrading dry-run
quanttrading dry-run --strategy sma_cross_v2 --data data/btcusd_1h.csv \
  --state state/dry_run.sqlite --per-trade-pct 0.005 \
  --paper-state state/paper.sqlite
```

Use a **separate** `--state` file from paper (`state/dry_run.sqlite` by default). Statuses in that SQLite file are `dry_run_ok`, `rejected` (risk), or `size_invalid`. `orders_sent` in the summary is always 0.

The printed summary includes signal count, would-send count, risk-rejected count, size-invalid count, a sample of would-be orders (`client_order_id`, symbol, side, qty, market/limit), and an equity-path snapshot (hypothetical local book only). `--paper-state` compares that file to the dry-run book and lists mismatches in `client_order_id`, side counts, and intent counts (would-send vs paper `filled`, plus risk-rejected ids). Limit orders the bar does not touch are stored as `unfilled` and are not would-send.

Offline (the default) sizes market orders from the bar close, same quote→base math as paper, and does not dial the network. `--public-markets` loads **public** ccxt metadata only (Kraken `BTC/USD` unless `--exchange` is set): min qty, amount step, price tick, min cost, and last price. A non-empty API key is refused. Private endpoints are never called. The public last does not resize the order; when the venue also has a min cost, the bar-sized base qty must clear that min cost at the public last or the row is `size_invalid`.

## Tests

```bash
python -m pytest
```

Coverage: signal contract validation, `sma_cross_v2` and `mean_reversion_v1` → Signal → submit, risk halt / reject, paper fill math, dry-run risk reject / size check (no network).

## Layout

```
src/quanttrading/
  signals.py           # Signal contract v0.1 (pydantic, JSON-serializable)
  data/ohlcv.py        # ccxt public fetch, CSV, synthetic sample
  strategy/sma.py      # default strategy: SMA crossover + min-vol filter (`sma_cross_v2`)
  strategy/mean_reversion.py  # comparison: short-window mean reversion (`mean_reversion_v1`)
  execution/base.py    # shared ExecutionBackend protocol
  execution/paper.py   # sim fills, positions, equity, SQLite
  execution/dry_run.py # would-be orders, same risk gates, no exchange IO
  execution/market_meta.py  # public min qty / tick / last, or an offline stub
  execution/risk.py    # per-trade / daily / total-DD kill-switch
  execution/live.py    # stub — raises, never sends orders
  backtest/runner.py   # bars → strategy → submit
  cli.py
```

### Signal contract v0.1

`strategy_id`, `ts` (UTC ISO), `symbol`, `side` (`buy|sell|flat`), `intent` (`open|close|reduce`), `qty`, `qty_unit` (`quote|base|contracts`), `order_type` (`market|limit`), `limit_price` (required if limit), `strength` 0–1, `max_slippage_bps`, `risk` utilization `{per_trade_pct, daily_dd_pct, total_dd_pct}`, `client_order_id`, optional `meta`.

### Paper fills

- **Market**: fill at `close * (1 ± slippage_bps / 10000)` (buy up, sell down).
- **Limit**: fill at the limit if the bar touches it (`low <= limit` for buys, `high >= limit` for sells); otherwise unfilled (IOC).

### Risk halt

- Open notional (quote) above `equity * PER_TRADE_PCT` → reject `per_trade_limit`.
- Day P&L ≤ `-DAILY_DD_PCT` from day-start equity → reject further opens that UTC day (`daily_circuit_breaker`).
- Drawdown from peak ≥ `TOTAL_DD_PCT` → sticky halt in SQLite (`max_drawdown`); new opens stay rejected after restart. Flatten/close is still allowed.

Live trading is out of scope: `LiveBroker.submit` raises and does not accept keys. Dry-run is the pre-live reconcile only; it cannot place orders either.
