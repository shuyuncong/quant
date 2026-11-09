# Benchmark vs TSMOM: bootstrap and multiple-testing

- Period: 2020-01-03 → 2026-10-30 (1652 days)
- Variants in the comparison: 20
- Bootstrap: stationary block, 10,000 resamples, block 20

## Sharpe confidence intervals

| Strategy | Sharpe | 95% CI low | 95% CI high | Bootstrap SE |
|---|---:|---:|---:|---:|
| ETF benchmark | 1.198 | 0.505 | 1.903 | 0.357 |
| TSMOM 3 asset + 10% vol | 1.136 | 0.469 | 1.872 | 0.361 |

## Paired difference (benchmark − TSMOM)

- Sharpe difference: **0.062**
- 95% CI: [-0.426, 0.501]
- P(diff > 0): 0.597; two-sided bootstrap p-value: 0.807

## Multiple testing

- Deflated Sharpe — benchmark: **0.860** (expected max daily SR 0.0484, trials 20)
- Deflated Sharpe — TSMOM: **0.820** (expected max daily SR 0.0484, trials 20)
- Reality check (max mean across 20 variants): p = **0.177**; best = `tsmom_12_1_3asset`

## Reading

- Overlapping CIs do not prove equality; the paired difference is the direct test.
- The deflated Sharpe is a multiple-testing adjustment, not a guarantee; it assumes independent trials and is conservative when variants are correlated.
- The reality check p-value is for the best mean return across all variants, not for the benchmark/TSMOM pair.
- Research-only; no production change.