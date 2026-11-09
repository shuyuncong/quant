# ETF execution stress test (V4/V5)

- Period: 2020-01-02 → 2026-10-30
- Initial capital: 500,000 CNY
- Signals and rebalance dates are identical across scenarios; only execution changes.
- Premium/discount and empirical spread proxy use fetched historical data; partial fill remains **assumption-based**.

| Strategy | Scenario | CAGR | Sharpe | Max DD | Trades | Turnover | 1% ADV caps | Partial fills | Unfilled notional |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| benchmark_etf_v5.1_40_35_10 | baseline | 9.92% | 1.20 | -10.20% | 573 | 73.0x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | adv_1pct | 9.92% | 1.20 | -10.20% | 573 | 73.0x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | spread_5bp | 9.48% | 1.15 | -10.43% | 571 | 72.0x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | spread_10bp | 9.03% | 1.10 | -10.66% | 575 | 70.9x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | spread_20bp | 8.15% | 1.00 | -11.11% | 583 | 68.9x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | premium_discount_10bp | 9.03% | 1.10 | -10.66% | 575 | 70.9x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | partial_50pct | 8.56% | 1.08 | -9.60% | 1004 | 42.3x | 0 | 4773 | 24,984,664 |
| benchmark_etf_v5.1_40_35_10 | combined_stress | 7.49% | 0.96 | -9.69% | 1001 | 40.8x | 0 | 4759 | 24,135,205 |
| benchmark_etf_v5.1_40_35_10 | empirical_spread_proxy | 8.34% | 1.03 | -10.90% | 567 | 69.3x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | baseline | 9.25% | 1.16 | -12.52% | 143 | 13.7x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | adv_1pct | 9.25% | 1.16 | -12.52% | 143 | 13.7x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | spread_5bp | 9.17% | 1.15 | -12.59% | 143 | 13.6x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | spread_10bp | 9.09% | 1.14 | -12.65% | 144 | 13.7x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | spread_20bp | 8.92% | 1.12 | -12.76% | 141 | 13.5x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | premium_discount_10bp | 9.09% | 1.14 | -12.65% | 144 | 13.7x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | partial_50pct | 8.49% | 1.10 | -12.70% | 177 | 7.9x | 0 | 221 | 4,421,368 |
| tsmom_12_1_3asset_vt10 | combined_stress | 8.26% | 1.07 | -12.82% | 175 | 7.8x | 1 | 223 | 4,398,057 |
| tsmom_12_1_3asset_vt10 | empirical_spread_proxy | 9.10% | 1.14 | -12.72% | 143 | 13.7x | 0 | 0 | 0 |

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