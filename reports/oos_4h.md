# Rolling out-of-sample report (paper only)

- Bars: **26810** 4h candles from `2013-10-06T20:00:00Z` to `2026-06-30T20:00:00Z`
- Span: **4650.0** days (**12.731** years)
- Paper equity: 2000 USD; gates 1% / 3% / 20%

## Caveats

- Live sma_cross_v2 is unchanged; this report is paper-only.
- Round-trip cost is the strategy entry filter and a labeled fee estimate; the paper broker does not debit commissions from equity.
- Buy-and-hold baseline sizes 1% of starting paper equity once at each fold's test open.
- Cash baseline is flat (0 PnL).

## Results by strategy

### `trend_breakout_v1`

Fold count (including holdout): **5**

| Fold | Kind | Test start | Test end | Bars |
| --- | --- | --- | --- | ---: |
| `wf_0` | walk_forward | `2017-09-06T16:00:00Z` | `2019-02-26T08:00:00Z` | 3217 |
| `wf_1` | walk_forward | `2019-02-26T12:00:00Z` | `2020-08-15T12:00:00Z` | 3217 |
| `wf_2` | walk_forward | `2020-08-15T16:00:00Z` | `2022-02-02T16:00:00Z` | 3217 |
| `wf_3` | walk_forward | `2022-02-02T20:00:00Z` | `2024-01-18T20:00:00Z` | 4291 |
| `holdout` | holdout | `2024-01-19T00:00:00Z` | `2026-06-30T20:00:00Z` | 5362 |

#### Cost stress `round_trip=0.003`

| Metric | Value |
| --- | --- |
| Fold count | 5 |
| Full round-trip trade count | 106 |
| After-cost EV / RT (trade-weighted, **fee estimate**) | 0.209532 |
| Profit factor | 1.879726 |
| Max drawdown (worst fold) | 0.003698 |
| Fee share estimate (fees / gross profit) | 0.105368 |
| Sum PnL (slippage path, no fee debit) | 28.663754 |
| Buy-and-hold 1% sleeve PnL (sum of folds) | 92.496684 |
| Cash baseline PnL | 0.0 |

Per-fold detail:

| Fold | RTs | After-cost EV/RT | PF | Max DD | Fee share | BH 1% PnL | Cash |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `wf_0` | 15 | 0.474504 | 2.708743 | 0.001121 | 0.071171 | -3.837919 | 0.0 |
| `wf_1` | 22 | 0.696143 | 4.419777 | 0.001386 | 0.062093 | 42.605574 | 0.0 |
| `wf_2` | 27 | -0.043546 | 1.039651 | 0.003698 | 0.133474 | 43.410854 | 0.0 |
| `wf_3` | 13 | 0.218077 | 1.973129 | 0.001538 | 0.107883 | 1.94925 | 0.0 |
| `holdout` | 29 | -0.064883 | 0.985256 | 0.001683 | 0.242169 | 8.368925 | 0.0 |

#### Cost stress `round_trip=0.006`

| Metric | Value |
| --- | --- |
| Fold count | 5 |
| Full round-trip trade count | 106 |
| After-cost EV / RT (trade-weighted, **fee estimate**) | 0.147053 |
| Profit factor | 1.874596 |
| Max drawdown (worst fold) | 0.003698 |
| Fee share estimate (fees / gross profit) | 0.211311 |
| Sum PnL (slippage path, no fee debit) | 28.492506 |
| Buy-and-hold 1% sleeve PnL (sum of folds) | 92.496684 |
| Cash baseline PnL | 0.0 |

Per-fold detail:

| Fold | RTs | After-cost EV/RT | PF | Max DD | Fee share | BH 1% PnL | Cash |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `wf_0` | 15 | 0.414113 | 2.708743 | 0.001121 | 0.142342 | -3.837919 | 0.0 |
| `wf_1` | 22 | 0.635328 | 4.419777 | 0.001386 | 0.124187 | 42.605574 | 0.0 |
| `wf_2` | 27 | -0.104499 | 1.039651 | 0.003698 | 0.266948 | 43.410854 | 0.0 |
| `wf_3` | 13 | 0.144091 | 1.928161 | 0.001537 | 0.220799 | 1.94925 | 0.0 |
| `holdout` | 29 | -0.125965 | 0.985256 | 0.001683 | 0.484337 | 8.368925 | 0.0 |

### `range_reversion_v2`

Fold count (including holdout): **5**

| Fold | Kind | Test start | Test end | Bars |
| --- | --- | --- | --- | ---: |
| `wf_0` | walk_forward | `2017-09-06T16:00:00Z` | `2019-02-26T08:00:00Z` | 3217 |
| `wf_1` | walk_forward | `2019-02-26T12:00:00Z` | `2020-08-15T12:00:00Z` | 3217 |
| `wf_2` | walk_forward | `2020-08-15T16:00:00Z` | `2022-02-02T16:00:00Z` | 3217 |
| `wf_3` | walk_forward | `2022-02-02T20:00:00Z` | `2024-01-18T20:00:00Z` | 4291 |
| `holdout` | holdout | `2024-01-19T00:00:00Z` | `2026-06-30T20:00:00Z` | 5362 |

#### Cost stress `round_trip=0.003`

> No completed round trips under default parameters on this history. That is a real (quiet) outcome, not a missing run.

| Metric | Value |
| --- | --- |
| Fold count | 5 |
| Full round-trip trade count | 0 |
| After-cost EV / RT (trade-weighted, **fee estimate**) | None |
| Profit factor | 0.0 |
| Max drawdown (worst fold) | 0.0 |
| Fee share estimate (fees / gross profit) | None |
| Sum PnL (slippage path, no fee debit) | 0 |
| Buy-and-hold 1% sleeve PnL (sum of folds) | 92.496684 |
| Cash baseline PnL | 0.0 |

Per-fold detail:

| Fold | RTs | After-cost EV/RT | PF | Max DD | Fee share | BH 1% PnL | Cash |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `wf_0` | 0 | None | 0.0 | 0.0 | None | -3.837919 | 0.0 |
| `wf_1` | 0 | None | 0.0 | 0.0 | None | 42.605574 | 0.0 |
| `wf_2` | 0 | None | 0.0 | 0.0 | None | 43.410854 | 0.0 |
| `wf_3` | 0 | None | 0.0 | 0.0 | None | 1.94925 | 0.0 |
| `holdout` | 0 | None | 0.0 | 0.0 | None | 8.368925 | 0.0 |

#### Cost stress `round_trip=0.006`

> No completed round trips under default parameters on this history. That is a real (quiet) outcome, not a missing run.

| Metric | Value |
| --- | --- |
| Fold count | 5 |
| Full round-trip trade count | 0 |
| After-cost EV / RT (trade-weighted, **fee estimate**) | None |
| Profit factor | 0.0 |
| Max drawdown (worst fold) | 0.0 |
| Fee share estimate (fees / gross profit) | None |
| Sum PnL (slippage path, no fee debit) | 0 |
| Buy-and-hold 1% sleeve PnL (sum of folds) | 92.496684 |
| Cash baseline PnL | 0.0 |

Per-fold detail:

| Fold | RTs | After-cost EV/RT | PF | Max DD | Fee share | BH 1% PnL | Cash |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `wf_0` | 0 | None | 0.0 | 0.0 | None | -3.837919 | 0.0 |
| `wf_1` | 0 | None | 0.0 | 0.0 | None | 42.605574 | 0.0 |
| `wf_2` | 0 | None | 0.0 | 0.0 | None | 43.410854 | 0.0 |
| `wf_3` | 0 | None | 0.0 | 0.0 | None | 1.94925 | 0.0 |
| `holdout` | 0 | None | 0.0 | 0.0 | None | 8.368925 | 0.0 |

## How to regenerate

```bash
quanttrading fetch-history --out data/btcusd_4h_long.csv
quanttrading oos-report --data data/btcusd_4h_long.csv --out reports/oos_4h.md
```
