# ma_ensemble_v1 basket: 60% stop variants (research only, 60 bps + 5 bps slip)

Variants: **none** = no stop; **hard** = total DD ≥ 60% → all cash for the rest of the window; **reentry** = DD ≥ 60% → all cash, each sleeve re-buys only after a fresh close cross above its own SMA, peak resets. Same folds as the BTC/ETH OOS. Stop liquidation approximated at that day's close.

## BTC (Kraken XBTUSD, to 2026-06-30)

| Fold | B&H | none | hard | reentry | Stop fired |
|---|---:|---:|---:|---:|---|
| wf_0 2017-05→2018-11 | +138.6% | +273.0% (DD 65.4%) | +319.0% (DD 61.1%) | +254.9% (DD 67.1%) | 2018-02-05 |
| wf_1 2018-11→2020-06 | +126.9% | +53.7% | same | same | never |
| wf_2 2020-06→2021-12 | +396.0% | +184.7% | same | same | never |
| wf_3 2021-12→2023-12 | -14.2% | +55.0% | same | same | never |
| holdout 2024-01→2026-06 | +37.7% | +30.9% (DD 33.7%) | same | same | never |

## ETH (Bitstamp ETHUSD, to 2026-11-07), for comparison

| Fold | none | hard | reentry | Stop fired |
|---|---:|---:|---:|---|
| wf_0 2019-11→2020-10 | +17.0% | -42.5% | +17.1% | 2020-03-12 |
| wf_1 2020-10→2021-10 | +562.9% | +297.6% | +426.0% | 2021-07-16 |
| wf_2 / wf_3 / holdout | never fired | | | |
