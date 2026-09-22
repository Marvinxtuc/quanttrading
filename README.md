# quanttrading

Local crypto trading MVP: public market data → strategy signals → backtest, paper, dry-run, or an explicit Kraken live order.

This is **not** a multi-exchange router. `quanttrading paper` and `quanttrading dry-run` never load API keys and never send orders. `quanttrading live` is the only command that places Kraken spot orders, and only for `sma_cross_v2`. It reads trade-only keys from the environment or `.env` and writes a separate live SQLite book. The status page stays read-only: it can show a paper, dry-run, or live book and it cannot trade.

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

Copy `.env.example` to `.env` if you want to override defaults. Do not put secrets in the repo. `.gitignore` excludes `.env` (and `.env.*` except `.env.example`), `state/`, and SQLite files. Live keys, when you use them, are `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` in that gitignored file or in the process environment.

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

## Status dashboard

Local read-only page for a paper or dry-run SQLite book. It has no order buttons, no APIs that change trading state, and it does not load API keys. The process binds to loopback only (`127.0.0.1` by default) and refuses any other host.

```bash
quanttrading paper --data data/btcusd_1h.csv --state state/paper.sqlite
quanttrading status-ui --state state/paper.sqlite --host 127.0.0.1 --port 8787 --refresh-sec 10
```

Open http://127.0.0.1:8787 . The page polls `GET /api/status` every `--refresh-sec` seconds (allowed range 5–15). Point `--state` at `state/live.sqlite` to read a live book. The page still has no order buttons and does not load `KRAKEN_API_KEY` or `KRAKEN_API_SECRET`. Overview mode is `paper`, `dry-run`, or `live`.

The page shows:

- **Overview** — strategy id, mode (`paper` or `dry-run`), symbol, equity, cash, unrealized PnL, day PnL %, rolling PnL %, max drawdown %, halt (yes/no and reason)
- **Position** — symbol, side, qty, average price, mark price, percent of equity
- **Fills** — time, side, qty, price, `client_order_id`, and whether the risk gate rejected the order
- **Market heartbeat** — last bar time and lag versus now. Lag is red when it is greater than twice the bar timeframe

Figures come from the SQLite book (`meta`, `positions`, `fills`, `equity`). The server opens that file read-only and does not create it. Strategy id is taken from the latest `client_order_id` when it matches `{strategy}-{symbol}-{ts}-{intent}-{side}`. Mode defaults to `paper`. With one open position, mark price is the last marked close implied by equity and cash. Max drawdown uses the equity curve and the persisted peak. A missing database shows an error on the page and is picked up on a later refresh.

Optional heartbeat JSON, for a paper or dry-run process that is keeping a live clock. The default path is the state file with a `.heartbeat.json` suffix (`state/paper.sqlite` → `state/paper.heartbeat.json`). Pass `--heartbeat` to use another file.

```json
{
  "strategy_id": "sma_cross_v2",
  "mode": "dry-run",
  "symbol": "BTC/USD",
  "timeframe": "1h",
  "last_bar_ts": "2026-09-22T01:00:00Z"
}
```

Only those fields are read. Any other key, including secrets, is ignored. Values in the heartbeat override the book for strategy, mode, and last bar time. `--timeframe` (for example `1h` or `15m`) sets the stale threshold. Without a heartbeat, the last equity timestamp is the bar time and the timeframe defaults to `1h`.

`quanttrading status-ui --host 0.0.0.0` is rejected.

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

`quanttrading dry-run` replays the same bars → strategy → `ExecutionBackend.submit` path as paper, then records the order that **would** be sent. It does not place exchange orders and does not read API keys. There is no `--live` flag on `paper` or `dry-run`. A bare `LiveBroker()` still raises and does not send.

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

## Live (Kraken, `sma_cross_v2` only)

`quanttrading live` places real Kraken spot orders. Nothing else does. `mean_reversion_v1` is refused. The venue must be Kraken.

Trial size is **0.2%–0.5%** of exchange equity (`--per-trade-pct` `0.002`–`0.005`). The default is **0.005** (0.5%). Paper's 1% is rejected here even if `PER_TRADE_PCT=0.01` is set for the paper book. The same execution risk gates apply: per-trade notional, daily loss breaker (3%), and total drawdown (20%). A halt rejects new opens. Closes and halt-flatten are still sent.

Each invocation syncs USD and BTC balances, warms the strategy on the supplied bars, and **submits only the latest bar**. Older signals are not replayed onto the account. The same `client_order_id` is not sent twice. Market orders are sized from the bar close, floored to the public amount step, and checked against public min qty / min cost / tick before `create_order`. Kraken's string `cl_ord_id` cannot hold our signal id, so the id stays in SQLite and the order carries a 31-bit `userref` derived from it. The Kraken txid is stored on the fill as `exchange_order_id`. `max_slippage_bps` is not a Kraken market-order parameter; the fill price is the venue average.

State defaults to `state/live.sqlite`, separate from `state/paper.sqlite` and `state/dry_run.sqlite`.

Keys: `KRAKEN_API_KEY` and `KRAKEN_API_SECRET`, from the environment or `.env`. Startup refuses when either value is missing, blank, or looks like a placeholder. Create the Kraken key with query funds and create/modify orders. Leave withdraw disabled. The client raises if a withdraw method is called. Keys are never written to the status page, the SQLite book, or this repo.

```bash
# 1. Paper, then dry-run. orders_sent stays 0.
quanttrading paper --strategy sma_cross_v2 --data data/btcusd_1h.csv --state state/paper.sqlite
quanttrading dry-run --strategy sma_cross_v2 --per-trade-pct 0.005 \
  --data data/btcusd_1h.csv --state state/dry_run.sqlite --paper-state state/paper.sqlite

# 2. Live. Sends at most one order, for the latest bar.
quanttrading live --strategy sma_cross_v2 --per-trade-pct 0.005 \
  --data data/btcusd_1h.csv --state state/live.sqlite

# Or load public candles, then act on the latest bar only.
quanttrading live --strategy sma_cross_v2 --per-trade-pct 0.005 \
  --fetch --state state/live.sqlite

# 3. Read-only status. No keys, no orders.
quanttrading status-ui --state state/live.sqlite --host 127.0.0.1 --port 8787
```

`quanttrading live` does not trade the bundled sample. Pass `--data` or `--fetch`. An order below Kraken's minimum is stored as `size_invalid` and is not sent.

### Manual checklist

1. `python -m pytest` (mocked exchange, no network).
2. Copy `.env.example` to `.env`. `git check-ignore .env` should print `.env`. `.env.example` stays tracked and contains empty key lines.
3. On Kraken, create an API key with query funds and create/modify orders. Leave **Withdraw funds** unchecked.
4. Set `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` in `.env` or the environment. Do not commit them.
5. Run the paper command, then dry-run, and confirm the dry-run line contains `orders_sent=0`.
6. Run `quanttrading live` with `--per-trade-pct` between `0.002` and `0.005` and `--state state/live.sqlite`. Confirm the summary `mode` is `live` and `orders_sent` is 0 or 1, not a replay of the whole file.
7. Open the status page on that live file. Confirm it shows mode `live` and has no way to send an order.
8. After a halt (`max_drawdown`), the next live run must reject a new open and may still send a close.

## Tests

```bash
python -m pytest
```

Coverage: signal contract validation, `sma_cross_v2` and `mean_reversion_v1` → Signal → submit, risk halt / reject, paper fill math, dry-run risk reject / size check (no network), live risk reject and mocked order submit (no network), read-only status page.

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
  execution/live.py    # Kraken orders for sma_cross_v2; armed only by `quanttrading live`
  execution/credentials.py  # KRAKEN_API_KEY / KRAKEN_API_SECRET, placeholder refusal
  backtest/runner.py   # bars → strategy → submit
  status/snapshot.py   # read-only status from SQLite + optional heartbeat
  status/server.py     # loopback status page
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

`quanttrading live` is the only path that places orders. `paper`, `dry-run`, and `status-ui` do not. An unconfigured `LiveBroker()` raises and does not load keys.
