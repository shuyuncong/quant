# Fundamental + momentum research study

- Version: `fundamental_momentum_study.v1`
- Sample: 2018-01-31 → 2026-08-31
- Universe: 300 symbols
- Target: Sharpe > 1.5 and trade win rate > 60%
- Costs: commission 3bp (min 5 CNY), stamp tax 10bp on sells, slippage 5bp, lot 100

## Factor IC (monthly, eligible universe)

| Factor | IC 1M | ICIR 1M | Positive days | IC 3M |
|---|---:|---:|---:|---:|
| `mom_12_1` | -0.0032 | -0.018 | 48.5% | -0.018 |
| `mom_6_1` | -0.0247 | -0.14 | 46.5% | -0.0156 |
| `mom_3_1` | -0.023 | -0.137 | 40.6% | -0.0105 |
| `mom_1` | -0.0462 | -0.343 | 37.6% | -0.0423 |
| `vol_60` | -0.0655 | -0.315 | 35.6% | -0.0967 |
| `earnings_yield` | 0.0182 | 0.1 | 53.5% | 0.0154 |
| `roe` | 0.0246 | 0.138 | 55.4% | 0.0108 |
| `debt_ratio` | -0.011 | -0.102 | 46.5% | -0.0099 |
| `roe_yoy` | 0.0169 | 0.136 | 59.4% | 0.0093 |
| `earnings_surprise_75` | 0.0516 | 0.381 | 69.4% | 0.0414 |
| `earnings_surprise_90` | 0.0456 | 0.326 | 66.7% | 0.0368 |
| `earnings_surprise_120` | 0.0279 | 0.204 | 62.2% | 0.0145 |
| `quality` | 0.0268 | 0.219 | 55.4% | 0.0172 |
| `value` | 0.0182 | 0.1 | 53.5% | 0.0154 |
| `momentum` | -0.0032 | -0.018 | 48.5% | -0.018 |
| `qvm_lv` | 0.0363 | 0.201 | 60.4% | 0.037 |

## Long-only portfolios

| Variant | CAGR | Sharpe | Max DD | Monthly win | Trade win | Trades | Exposure |
|---|---:|---:|---:|---:|---:|---:|---:|
| `qvm_lv_top20` | 11.79% | 0.625 | -33.51% | 57.1% | 51.1% | 595 | 97.1% |
| `qvm_lv_top30` | 13.81% | 0.714 | -32.54% | 55.2% | 50.3% | 763 | 97.0% |
| `qvm_lv_top50` | 13.21% | 0.692 | -32.04% | 56.2% | 51.6% | 1062 | 95.6% |
| `quality_top20` | 6.76% | 0.405 | -34.02% | 49.5% | 47.1% | 223 | 96.2% |
| `value_top20` | 6.8% | 0.42 | -35.21% | 47.6% | 54.2% | 253 | 98.4% |
| `earnings_surprise_75_top20` | 5.62% | 0.394 | -41.49% | 36.2% | 49.2% | 484 | 51.8% |
| `earnings_surprise_90_top20` | 7.89% | 0.501 | -43.43% | 38.1% | 50.5% | 493 | 55.1% |
| `qvm_lv_top20_ma200` | 13.75% | 0.861 | -29.19% | 41.9% | 55.2% | 431 | 55.7% |
| `qvm_lv_top30_ma200` | 14.53% | 0.924 | -20.71% | 39.0% | 53.6% | 560 | 55.3% |
| `qvm_lv_top50_ma200` | 14.55% | 0.94 | -17.74% | 39.0% | 54.2% | 825 | 54.7% |
| `qvm_lv_top30_ma60` | 6.36% | 0.498 | -21.06% | 39.0% | 46.6% | 760 | 45.6% |

## Long-short research spread (not executable in A-share retail)

| Variant | Active months | Gross annual | Gross Sharpe (full) | Gross win (full) | Net annual | Net Sharpe (full) |
|---|---:|---:|---:|---:|---:|---:|
| `earnings_surprise_75_q5` | 49/104 | 8.72% | 0.955 | 28.8% | 7.87% | 0.869 |
| `earnings_surprise_75_q10` | 49/104 | 10.64% | 0.915 | 32.7% | 9.79% | 0.847 |
| `earnings_surprise_90_q5` | 54/104 | 8.36% | 0.836 | 31.7% | 7.43% | 0.746 |
| `earnings_surprise_90_q10` | 54/104 | 12.2% | 0.999 | 35.6% | 11.26% | 0.928 |
| `earnings_surprise_120_q10` | 75/104 | 11.43% | 0.777 | 44.2% | 10.14% | 0.69 |

## Verdict

- Best long-only: Sharpe 0.94, trade win 54.2%, max drawdown -17.74%.
- Target met by a long-only variant: no.
- The long-short earnings-surprise spread is a research diagnostic only. It requires shorting, borrow availability and financing costs that are not available to the retail constraints of this repository.
- No parameter was tuned to force the target. Production decision: none.
