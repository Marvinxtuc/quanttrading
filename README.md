# quanttrading

Local crypto **paper-trading** MVP: public market data → strategy signals → backtest or paper execution → hard risk kill-switch.

This is **not** a broker platform. There is no dashboard, no multi-exchange router, and no live order placement. Live execution exists only as an interface stub. The paper path never loads API keys.

## Baselines (locked in config)

| Item | Default |
| --- | --- |
| Market | Crypto via [ccxt](https://github.com/ccxt/ccxt), Binance public OHLCV (`BTC/USDT` spot, or `BTC/USDT:USDT` USDT-M) |
| Paper wallet | **2000 USDT** (`PAPER_EQUITY`) |
| Per-trade cap | **1%** of equity (20 USDT at start) |
| Daily loss breaker | **3%** of day-start equity (60 USDT at start) |
| Max total drawdown | **20%** from peak (400 USDT at start) → persist halt, reject new **open** orders |

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
quanttrading fetch --symbol BTC/USDT --timeframe 1h --out data/btcusdt_1h.csv
```

Default venue is Binance. If that host returns HTTP 451 (geo restriction), use another public ccxt exchange or the offline generator:

```bash
quanttrading fetch --exchange kraken --symbol BTC/USDT --timeframe 1h --out data/btcusdt_1h.csv
quanttrading sample-data --out data/btcusdt_1h.csv
```

A deterministic synthetic sample is also bundled at `src/quanttrading/samples/btcusdt_1h.csv`, so `quanttrading paper` runs without network.

## Run the paper loop

One command replays BTC/USDT bars through the default SMA crossover, paper broker, and risk gates:

```bash
quanttrading paper
# or with fetched/synthetic CSV + persisted SQLite state
quanttrading paper --data data/btcusdt_1h.csv --state state/paper.sqlite
```

Same pipeline as a historical backtest (in-memory store):

```bash
quanttrading backtest --data data/btcusdt_1h.csv
```

Both print metrics: trade days, Sharpe (crypto, 365), max drawdown, win rate, profit factor, halt state.

### Default strategy (`sma_cross_v1`)

Long-only fast/slow SMA crossover (defaults 10 / 30) on whatever symbol the feed provides. Market orders only. An open is sized at `equity * PER_TRADE_PCT` in quote USDT; a close flattens the full base position. `client_order_id` is deterministic from strategy id, symbol, bar timestamp, intent, and side so paper replays are stable. Risk kill-switches stay in the execution layer.

## Tests

```bash
python -m pytest
```

Coverage: signal contract validation, default SMA strategy → Signal → submit, risk halt / reject, paper fill math.

## Layout

```
src/quanttrading/
  signals.py           # Signal contract v0.1 (pydantic, JSON-serializable)
  data/ohlcv.py        # ccxt public fetch, CSV, synthetic sample
  strategy/sma.py      # default strategy: long-only SMA crossover
  execution/base.py    # shared ExecutionBackend protocol
  execution/paper.py   # sim fills, positions, equity, SQLite
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

Live trading is out of scope: `LiveBroker.submit` raises and does not accept keys.
