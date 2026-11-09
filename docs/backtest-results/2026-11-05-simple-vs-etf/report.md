# Simple strategies vs canonical ETF benchmark

- Period: 2020-01-02 → 2026-10-30
- Costs: commission 3bp (min 5 CNY), slippage 5bp, lot 100, no stamp duty
- Benchmark: canonical ETF 40/35 + backfill, 10% vol, weekly

| Variant | Rebalance | CAGR | Sharpe | Max DD | Trade win | Closed rounds | Daily win | Exposure | Turnover |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `benchmark_etf_v5.1_40_35_10` | W | 9.87% | 1.20 | -10.19% | 52.94% | 68 | 54.66% | 0.63 | 72.1x |
| `benchmark_etf_v5.1_40_35_10_M` | M | 7.38% | 0.91 | -11.50% | 50.0% | 32 | 53.63% | 0.63 | 29.9x |
| `benchmark_etf_v5.1_40_35_10_D` | D | 7.09% | 0.89 | -11.29% | 42.11% | 190 | 53.33% | 0.63 | 170.7x |
| `tsmom_12_1_3asset` | M | 10.31% | 1.06 | -13.70% | 83.33% | 6 | 55.87% | 0.98 | 16.0x |
| `tsmom_12_1_3asset_W` | W | 8.46% | 0.87 | -13.53% | 43.75% | 16 | 55.99% | 0.99 | 34.9x |
| `tsmom_12_1_3asset_D` | D | 7.46% | 0.77 | -15.92% | 50.0% | 30 | 55.45% | 0.99 | 61.7x |
| `tsmom_12_1_3asset_vt10` | M | 9.26% | 1.14 | -12.51% | 83.33% | 6 | 55.69% | 0.89 | 13.5x |
| `tsmom_12_1_3asset_vt10_W` | W | 7.89% | 0.98 | -13.36% | 43.75% | 16 | 55.87% | 0.90 | 30.2x |
| `tsmom_12_1_3asset_vt10_D` | D | 7.09% | 0.89 | -14.65% | 50.0% | 30 | 55.39% | 0.90 | 53.3x |
| `tsmom_12_1_all` | M | -0.28% | 0.08 | -44.89% | 37.21% | 172 | 50.12% | 0.98 | 43.0x |
| `tsmom_12_1_all_vt10` | M | 1.03% | 0.15 | -33.23% | 38.95% | 172 | 49.76% | 0.57 | 25.5x |
| `tsmom_ma200_3asset` | M | 6.53% | 0.66 | -16.28% | 35.71% | 14 | 55.45% | 0.98 | 24.2x |
| `tsmom_ma200_all` | M | 2.15% | 0.21 | -40.78% | 29.07% | 258 | 50.18% | 0.98 | 51.7x |
| `dual_momentum_gem_vt10` | M | 5.47% | 0.71 | -10.63% | 66.67% | 6 | 55.99% | 0.74 | 15.2x |
| `dual_momentum_top3_vt10` | M | 2.40% | 0.29 | -27.60% | 41.43% | 70 | 51.09% | 0.42 | 28.9x |
| `reversal_5d_top10` | W | -1.30% | 0.01 | -39.15% | 51.93% | 1244 | 29.9% | 0.55 | 259.3x |
| `reversal_5d_top20` | W | 0.49% | 0.11 | -31.98% | 52.73% | 1392 | 29.6% | 0.55 | 153.8x |
| `reversal_5d_top10_no_filter` | W | -1.95% | 0.03 | -49.01% | 51.3% | 2193 | 50.36% | 0.99 | 499.9x |
| `lowvol_60d_top10` | M | 5.39% | 0.45 | -27.48% | 50.7% | 142 | 49.64% | 0.99 | 39.0x |
| `lowvol_60d_top20` | M | 4.34% | 0.34 | -34.09% | 47.55% | 204 | 48.79% | 0.99 | 30.3x |

Notes:
- `trade_win` is per closed round; `closed_rounds` is the sample size.
- `daily_win` is the share of positive days; it is not a trade win rate.
- The benchmark uses the canonical 40/35 + backfill 10% weekly rule set.
- TSMOM/dual momentum use 12−1 momentum; Faber uses price > MA200.
- Reversal uses the worst 5-day return; low-vol uses the lowest 60-day vol.
- No parameter sweep: these are fixed, pre-registered variants.
