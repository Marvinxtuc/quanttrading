# ETH long-history OOS — ma_ensemble_v1 (research only)

- Data: Bitstamp ETHUSD daily (UTC), 2017-08-16 → 2026-11-07, no gaps. Kraken full archive isn't available locally; cross-check vs Kraken XETHZUSD daily over the last 721 days: close median diff 0.013%, max 0.15%.
- Same framework and criteria as BTC (`reports/low_turnover_oos/run.py`): expanding walk-forward + holdout from 2024-01-01; signal at close, fill next open; 30/40/60 bps + 5 bps slippage; no tuning. Folds differ from BTC because ETH history is shorter (wf_0 starts 2019-11-09).
- Added `basket` = ma_ensemble_v1: thirds on SMA150/200/250 with the 60% total-drawdown hard stop (all to cash, stays flat).

## Holdout 2024-01-01 → 2026-11-07, 60 bps

| Strategy | Return | B&H | MaxDD | B&H DD | PF | a | b | c | Result |
|---|---:|---:|---:|---:|---:|---|---|---|---|
| Basket | +22.8% | -2.6% | 40.8% | 67.6% | 1.87 | ✅ | ❌ (>35%) | ❌ (wf_2 -14.2%) | **FAIL** |
| SMA150 | +35.6% | -2.6% | 47.8% | 67.6% | 2.15 | ✅ | ❌ | ❌ | **FAIL** |
| SMA200 | +27.7% | -2.6% | 41.4% | 67.6% | 2.11 | ✅ | ❌ | ❌ | **FAIL** |
| SMA250 | +5.0% | -2.6% | 41.6% | 67.6% | 1.30 | ✅ | ❌ | ❌ | **FAIL** |

30/40 bps and the holdout cut at 2026-06-30 (BTC-comparable) give the same verdict: every variant FAILs. Details in `reports/eth_oos/results*.json`.

## Notable
- Every strategy loses in wf_2 (2021-10 → 2022-09): basket -14.2%, SMA150 -5.3%, SMA200 -15.5%, SMA250 -21.8% (60 bps).
- The 60% hard stop fired at the bottom in wf_0 (2020 crash) and wf_1 (2021 crash): basket wf_0 -42.5% vs sleeves +6.6% to +33.1%. On ETH the stop locks in losses instead of protecting.
