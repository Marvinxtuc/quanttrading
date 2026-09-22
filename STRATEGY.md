# Paper strategies (4h)

`quanttrading live` stays on `sma_cross_v2` and the 1h default. The strategies below are paper and dry-run only. They do not change those defaults and do not raise the 1% / 3% / 20% execution gates.

The numbers in this file are a first version. They are not fitted and not verified on a backtest. This file does not report PnL.

**ADX band [18, 25):** neither `range_reversion_v2` nor `trend_breakout_v1` takes a new entry. Range needs ADX14 `< 18`. Trend needs ADX14 `>= 25`. That middle band is intentionally quiet for both.

---

# trend_breakout_v1

## Market

| Item | Value |
| --- | --- |
| Venue | Kraken spot |
| Symbol | `BTC/USD` |
| Side | Long only, no leverage, no scale-in |
| Bars | 4h. The strategy does not resample; each CSV row is one bar |

Signal on the **closed** 4h bar. The earliest fill is the **next** bar's open. Nothing in the decision reads the fill bar's high, low, or close.

`Signal.ts` and `meta.exec_ts` are the fill bar. `meta.signal_ts` is the closed bar that formed the decision. `meta.fill_on` is `open`. The paper and dry-run brokers fill that open (with the usual slippage) and reject the order when `exec_ts` is not the submitted bar or when `signal_ts` is not strictly earlier.

## Indicators

Computed on closed bars only.

| Indicator | Length | Method |
| --- | --- | --- |
| EMA fast | 50 | SMA seed, then `2 / (period + 1)` |
| EMA slow | 200 | same |
| ATR | 14 | Wilder. First value is the average of the first 14 true ranges (`period + 1` bars) |
| ADX | 14 | Wilder DX, then Wilder smooth. First value needs `2 * period` bars |

Warm-up is the slow EMA (200 closes) together with a 55-bar high behind the signal bar. ADX is ready before that.

## Parameters (first version, unverified)

| Parameter | Default | Role |
| --- | --- | --- |
| `ema_fast` | 50 | Trend direction |
| `ema_slow` | 200 | Trend direction |
| `atr_period` | 14 | Stop distance and breakout band |
| `adx_period` | 14 | Trend strength |
| `adx_min` | 25 | ADX must be at least this |
| `breakout_lookback` | 55 | Prior highs, excluding the signal bar |
| `breakout_atr` | 0.1 | Close must clear that high by this many ATRs |
| `max_chase_atr` | 1.0 | Close may not extend past that high by more than this many ATRs |
| `stop_atr` (`k`) | 2.0 | Initial stop and trail. `--stop-atr` |
| `assumed_round_trip_cost_pct` | **0.003** | Cost filter. `--round-trip-cost`. See below |
| `trend_break_lookback` | 10 | Prior lows, excluding the signal bar |
| `follow_through_bars` | 30 | 30 bars of 4h is 5 days |
| `cooldown_bars` | 3 | Closed bars after an exit fill with no new entry |
| slippage | settings, default 5 bps | Same market slippage as the other strategies |

### Cost default

`0.003` is 30 bps round trip, the midpoint of the brief's example band **0.002–0.004**. It is an assumption for the filter, not a measured Kraken fee schedule. The filter uses the signal-bar close as `entry_price` (the next open is not known yet):

```
(k * ATR14) / close >= 2 * assumed_round_trip_cost_pct
```

With the defaults that is `2 * ATR14 / close >= 0.006`.

## Entry

All of these must hold on the closed bar. Otherwise there is no order.

1. `close > EMA200` and `EMA50 > EMA200`
2. `ADX14 >= 25`
3. `close > max(high of the prior 55 bars) + 0.1 * ATR14`
4. `(close - that 55-high) <= 1.0 * ATR14`
5. Cost filter above
6. Flat: no open BTC position and no buy already waiting for the next bar

One open at a time. No add-on after a loser, and no second buy while a buy is pending. Size is `equity * per_trade_pct` in quote USD. The execution layer still rejects opens outside 1% notional, the 3% daily loss breaker, and the 20% drawdown halt.

`planned_stop_risk_fraction` on the signal meta is the equity fraction that would be lost if the locked stop fills at that quote size. It does not change the size.

## Exits

Decided on a closed bar, filled on the next open. No fixed percent take-profit.

| Exit | Rule |
| --- | --- |
| Initial stop | Fill open minus `k * ATR14` from the **signal** bar. Locked. A later, larger ATR cannot move the stop down |
| Trail | After the bar is checked against the stop already in force, `stop = max(stop, close - k * ATR14)`. The new level is used starting the next bar |
| Stop touch | This bar's low is at or below the stop that was already active. The fill is still the next open, not the stop price |
| Trend break | `close < min(low of the prior 10 bars)` |
| No follow-through | After 30 closed bars in the trade, if no close has reached `entry + initial_stop_distance`, exit. The entry bar counts. The target is one stop-distance above the fill, not a take-profit |
| Halt | The execution halt flag uses the same next-bar flatten |

If several fire on one bar, the order is halt, stop, trend break, then no follow-through.

## Cooldown

After the exit **fill**, the next 3 closed bars cannot form a new entry. The earliest new entry decision is the bar after those three, and that order fills one bar later.

## Kraken 720-bar limit

Kraken's public OHLC endpoint returns at most 720 candles per request. `quanttrading fetch --limit` larger than 720 is truncated. 720 bars of 4h is about 120 days, which covers the 200-bar EMA warm-up. A longer replay needs a CSV you already fetched or saved.

## Run paper on 4h data

```bash
quanttrading fetch --exchange kraken --symbol BTC/USD --timeframe 4h --limit 720 --out data/btcusd_4h.csv
quanttrading paper --strategy trend_breakout_v1 --data data/btcusd_4h.csv --state state/paper_trend.sqlite
quanttrading dry-run --strategy trend_breakout_v1 --data data/btcusd_4h.csv \
  --state state/dry_run_trend.sqlite --per-trade-pct 0.005
```

Use a different `--state` file from `sma_cross_v2`. A quiet or short file can print `n_fills` of 0 until warm-up plus a breakout; that is still a valid paper run. `--stop-atr` and `--round-trip-cost` override the two defaults above. The other parameters stay at the table until the constructor is called from code.

---

# range_reversion_v2

Independent paper experiment. Not live. Does not change `sma_cross_v2` or `trend_breakout_v1`.

## Market

Same as trend: Kraken spot `BTC/USD`, long only, no leverage, **4h** bars, no resample.

Signal on the **closed** 4h bar. Fill on the **next** bar open. `meta.signal_ts` / `meta.exec_ts` / `meta.fill_on=open` match `trend_breakout_v1`.

## Indicators

| Indicator | Length | Method |
| --- | --- | --- |
| EMA fast | 50 | SMA seed, then `2 / (period + 1)` |
| EMA slow | 200 | same |
| ATR | 14 | Wilder |
| ADX | 14 | Wilder |
| Mean / population std | 48 | Prior closed closes **excluding** the signal bar |

Warm-up is the slow EMA (200 closes). The z-window needs 48 prior closes. Skip when population std is 0 or when any consecutive timestamps in that z-window are not exactly 4h apart (data gap).

## Parameters (first version, unverified)

| Parameter | Default | Role |
| --- | --- | --- |
| `ema_fast` | 50 | Compression and regime exit |
| `ema_slow` | 200 | Compression gate |
| `atr_period` | 14 | Stop distance (with z-band) |
| `adx_period` | 14 | Environment and regime exit |
| `adx_max_entry` | 18 | ADX must be **below** this to enter |
| `adx_trend_exit` | 25 | With close `< EMA50`, exit |
| `ema_compression` | 0.01 | `abs(EMA50 / EMA200 - 1)` must be below this |
| `z_lookback` | 48 | Prior closes for mean/std, excluding signal bar |
| `entry_z` | 2.0 | Recovery threshold |
| `stop_atr` | **1.0** | ATR leg of stop. `--stop-atr` (omit for this default) |
| `stop_z` | **1.0** | Std leg of stop (`max(stop_atr * ATR, stop_z * std)`) |
| `assumed_round_trip_cost_pct` | **0.003** | Cost / RR filter. `--round-trip-cost` |
| `min_rr_after_cost` | 1.0 | `(U - C) / (D + C)` must be at least this |
| `max_hold_bars` | 18 | 18 × 4h = 72h |
| `cooldown_bars` | 6 | 6 × 4h = 24h after exit fill |
| slippage | settings, default 5 bps | Same market slippage; not added again in the RR filter |

### Environment gate

All must hold on the signal bar or there is no entry:

1. `ADX14 < 18`
2. `abs(EMA50 / EMA200 - 1) < 0.01`
3. Indicators warmed; std `> 0`; no 4h gap in the z-window

### Entry signal

Mean and population std from the prior **48** closed closes **excluding** the signal bar. Both `z_prev` and `z_curr` use that same window:

```
z = (close - mean) / std
```

Trigger (flat only, no pending buy):

- previous bar `z < -2`
- current bar `z >= -2`
- current close still `< mean` (recovery back into the band, still below the mean)

### Pre-trade filters (reject if fail)

- **Target:** locked at the signal-bar mean. Later means are not chased.
- **Stop distance:** `max(1.0 * ATR14, 1.0 * std)` from the signal bar. Applied to the fill open: `stop = entry - distance`. Locked; no trail; no add. (`stop_z = 1.0` is half the entry band so a recovery toward the mean can still clear the RR filter; using `2.0 * std` would make `(U - C) / (D + C) >= 1` unreachable near the `-2` band after cost.)
- Price basis for the filter is the **signal-bar close** (next open unknown). Slippage is not double-counted here; the broker still applies the usual market slippage on the fill.
- `U = (target - close) / close`, `D = stop_distance / close`, `C = 0.003`
- Require `U > C` and `(U - C) / (D + C) >= 1.0`

Size is still `equity * per_trade_pct` in quote. The 1% / 3% / 20% gates stay in the execution layer.

## Exits

Decided on a closed bar, filled on the next open.

| Exit | Rule |
| --- | --- |
| Target | Bar high reaches the locked mean |
| Stop | Bar low reaches the locked initial stop |
| Max hold | 18 closed bars in the trade without the target (entry bar counts) |
| Regime | `ADX14 >= 25` and `close < EMA50` |
| Halt | Execution halt flag → same next-bar flatten |

If several fire on one bar, the order is halt, stop, target, max hold, then regime. Stop is checked before target when the same bar spans both (path unknown).

## Cooldown

After the exit **fill**, the next 6 closed bars cannot form a new entry.

## Kraken 720-bar limit

Same as trend: public OHLC caps at 720 bars (~120 days of 4h), which covers the 200-bar EMA warm-up. Longer history is out of scope here.

## Run paper on 4h data

```bash
quanttrading fetch --exchange kraken --symbol BTC/USD --timeframe 4h --limit 720 --out data/btcusd_4h.csv
quanttrading paper --strategy range_reversion_v2 --data data/btcusd_4h.csv --state state/paper_range.sqlite
quanttrading dry-run --strategy range_reversion_v2 --data data/btcusd_4h.csv \
  --state state/dry_run_range.sqlite --per-trade-pct 0.005
```

Use a different `--state` file from `sma_cross_v2` and from `trend_breakout_v1`. A quiet or short file can print `n_fills` of 0 until warm-up plus a recovery; that is still a valid paper run.
