# ETF execution stress test (V4/V5)

- Period: 2020-01-02 → 2026-10-30
- Initial capital: 250,000 CNY
- Signals and rebalance dates are identical across scenarios; only execution changes.
- Premium/discount and empirical spread proxy use fetched historical data; partial fill remains **assumption-based**.

| Strategy | Scenario | CAGR | Sharpe | Max DD | Trades | Turnover | 1% ADV caps | Partial fills | Unfilled notional |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| benchmark_etf_v5.1_40_35_10 | baseline | 9.87% | 1.20 | -10.19% | 536 | 72.1x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | adv_1pct | 9.87% | 1.20 | -10.19% | 536 | 72.1x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | spread_5bp | 9.43% | 1.15 | -10.41% | 526 | 71.2x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | spread_10bp | 9.00% | 1.10 | -10.64% | 523 | 70.1x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | spread_20bp | 8.14% | 1.00 | -11.09% | 532 | 68.2x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | premium_discount_10bp | 9.00% | 1.10 | -10.64% | 523 | 70.1x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | partial_50pct | 8.49% | 1.07 | -9.60% | 911 | 41.0x | 0 | 4762 | 14,028,668 |
| benchmark_etf_v5.1_40_35_10 | combined_stress | 7.45% | 0.95 | -9.70% | 902 | 39.8x | 0 | 4747 | 13,483,816 |
| benchmark_etf_v5.1_40_35_10 | empirical_spread_proxy | 8.32% | 1.02 | -10.88% | 530 | 68.5x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | baseline | 9.21% | 1.15 | -12.48% | 125 | 13.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | adv_1pct | 9.21% | 1.15 | -12.48% | 125 | 13.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | spread_5bp | 9.12% | 1.14 | -12.61% | 122 | 13.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | spread_10bp | 9.04% | 1.13 | -12.64% | 124 | 13.1x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | spread_20bp | 8.88% | 1.11 | -12.76% | 121 | 13.0x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | premium_discount_10bp | 9.04% | 1.13 | -12.64% | 124 | 13.1x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | partial_50pct | 8.37% | 1.09 | -12.76% | 153 | 7.4x | 0 | 212 | 2,348,138 |
| tsmom_12_1_3asset_vt10 | combined_stress | 8.16% | 1.07 | -12.94% | 151 | 7.2x | 0 | 214 | 2,349,316 |
| tsmom_12_1_3asset_vt10 | empirical_spread_proxy | 9.07% | 1.14 | -12.67% | 123 | 13.1x | 0 | 0 | 0 |

## Data gaps

- **PIT delisted/liquidated ETF universe:** SSE historical scale is available; SZSE historical list is not; current 47 are current-listed. Survivorship bias remains.
- **ETF premium/discount:** historical NAV/price data is available separately; it is a diagnostic, not a constant cost on top of market prices.
- **Bid-ask spread:** no quote data; `empirical_spread_proxy` uses a Corwin-Schultz high-low estimator, which is not a quote; extra-slippage scenarios remain assumptions.
- **Partial fill:** no order-book/queue data; the 50% fill-ratio scenario is an assumption.

## Reading the result

- `adv_1pct` shows whether the 1% ADV cap binds at the tested capital. At 250k/500k on this liquid pool it may not bind; that is a finding, not a failure.
- `capped_orders` counts orders reduced by the ADV cap; `unfilled_notional` is the notional not executed because of ADV/partial-fill caps.
- Capped residuals are not carried forward; the next weekly rebalance corrects the position. This is a conservative proxy for partial fills.
- Results are in-sample research, not live trading evidence.