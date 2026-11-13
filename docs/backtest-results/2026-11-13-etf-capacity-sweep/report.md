# ETF execution stress test (V4/V5)

- Period: 2020-01-02 → 2026-10-30
- Capital levels: 250,000, 500,000, 1,000,000, 2,000,000, 5,000,000 CNY
- Signals and rebalance dates are identical across scenarios; only execution changes.
- Premium/discount and empirical spread proxy use fetched historical data; partial fill remains **assumption-based**.

| Strategy | Capital | Scenario | CAGR | Sharpe | Max DD | Trades | Turnover | ADV caps | Partial fills | Unfilled notional |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| benchmark_etf_v5.1_40_35_10 | 250,000 | baseline | 9.87% | 1.20 | -10.19% | 539 | 72.1x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 250,000 | adv_1pct | 9.87% | 1.20 | -10.19% | 539 | 72.1x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 250,000 | adv_5pct | 9.87% | 1.20 | -10.19% | 539 | 72.1x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 250,000 | adv_10pct | 9.87% | 1.20 | -10.19% | 539 | 72.1x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 250,000 | spread_5bp | 9.43% | 1.15 | -10.41% | 531 | 71.2x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 250,000 | spread_20bp | 8.14% | 1.00 | -11.09% | 531 | 68.2x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 500,000 | baseline | 9.92% | 1.20 | -10.20% | 573 | 73.0x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 500,000 | adv_1pct | 9.92% | 1.20 | -10.20% | 573 | 73.0x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 500,000 | adv_5pct | 9.92% | 1.20 | -10.20% | 573 | 73.0x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 500,000 | adv_10pct | 9.92% | 1.20 | -10.20% | 573 | 73.0x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 500,000 | spread_5bp | 9.47% | 1.15 | -10.43% | 573 | 71.9x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 500,000 | spread_20bp | 8.15% | 1.00 | -11.11% | 579 | 68.9x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 1,000,000 | baseline | 9.92% | 1.20 | -10.21% | 605 | 73.4x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 1,000,000 | adv_1pct | 9.92% | 1.20 | -10.21% | 608 | 73.4x | 1 | 1 | 31,981 |
| benchmark_etf_v5.1_40_35_10 | 1,000,000 | adv_5pct | 9.92% | 1.20 | -10.21% | 605 | 73.4x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 1,000,000 | adv_10pct | 9.92% | 1.20 | -10.21% | 605 | 73.4x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 1,000,000 | spread_5bp | 9.48% | 1.15 | -10.44% | 603 | 72.3x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 1,000,000 | spread_20bp | 8.15% | 1.00 | -11.13% | 607 | 69.2x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 2,000,000 | baseline | 9.93% | 1.20 | -10.21% | 625 | 73.6x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 2,000,000 | adv_1pct | 9.86% | 1.20 | -10.35% | 627 | 72.8x | 11 | 11 | 2,112,408 |
| benchmark_etf_v5.1_40_35_10 | 2,000,000 | adv_5pct | 9.93% | 1.20 | -10.21% | 625 | 73.6x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 2,000,000 | adv_10pct | 9.93% | 1.20 | -10.21% | 625 | 73.6x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 2,000,000 | spread_5bp | 9.49% | 1.15 | -10.44% | 625 | 72.5x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 2,000,000 | spread_20bp | 8.16% | 1.00 | -11.13% | 624 | 69.4x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 5,000,000 | baseline | 9.93% | 1.20 | -10.22% | 666 | 73.8x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 5,000,000 | adv_1pct | 9.08% | 1.12 | -11.45% | 688 | 66.5x | 63 | 63 | 39,237,322 |
| benchmark_etf_v5.1_40_35_10 | 5,000,000 | adv_5pct | 9.93% | 1.20 | -10.22% | 665 | 73.8x | 1 | 1 | 149,244 |
| benchmark_etf_v5.1_40_35_10 | 5,000,000 | adv_10pct | 9.93% | 1.20 | -10.22% | 666 | 73.8x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 5,000,000 | spread_5bp | 9.48% | 1.15 | -10.45% | 668 | 72.7x | 0 | 0 | 0 |
| benchmark_etf_v5.1_40_35_10 | 5,000,000 | spread_20bp | 8.16% | 1.00 | -11.14% | 660 | 69.6x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 250,000 | baseline | 9.21% | 1.15 | -12.48% | 125 | 13.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 250,000 | adv_1pct | 9.21% | 1.15 | -12.48% | 125 | 13.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 250,000 | adv_5pct | 9.21% | 1.15 | -12.48% | 125 | 13.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 250,000 | adv_10pct | 9.21% | 1.15 | -12.48% | 125 | 13.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 250,000 | spread_5bp | 9.12% | 1.14 | -12.61% | 122 | 13.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 250,000 | spread_20bp | 8.88% | 1.11 | -12.76% | 121 | 13.0x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 500,000 | baseline | 9.25% | 1.16 | -12.52% | 143 | 13.7x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 500,000 | adv_1pct | 9.25% | 1.16 | -12.52% | 143 | 13.7x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 500,000 | adv_5pct | 9.25% | 1.16 | -12.52% | 143 | 13.7x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 500,000 | adv_10pct | 9.25% | 1.16 | -12.52% | 143 | 13.7x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 500,000 | spread_5bp | 9.17% | 1.15 | -12.59% | 143 | 13.6x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 500,000 | spread_20bp | 8.92% | 1.12 | -12.76% | 141 | 13.5x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 1,000,000 | baseline | 9.27% | 1.16 | -12.53% | 164 | 14.0x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 1,000,000 | adv_1pct | 9.21% | 1.15 | -12.53% | 160 | 13.8x | 2 | 2 | 221,224 |
| tsmom_12_1_3asset_vt10 | 1,000,000 | adv_5pct | 9.27% | 1.16 | -12.53% | 164 | 14.0x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 1,000,000 | adv_10pct | 9.27% | 1.16 | -12.53% | 164 | 14.0x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 1,000,000 | spread_5bp | 9.18% | 1.15 | -12.59% | 160 | 13.9x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 1,000,000 | spread_20bp | 8.92% | 1.12 | -12.77% | 157 | 13.8x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 2,000,000 | baseline | 9.27% | 1.16 | -12.53% | 175 | 14.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 2,000,000 | adv_1pct | 9.15% | 1.14 | -12.53% | 173 | 13.8x | 7 | 7 | 1,069,828 |
| tsmom_12_1_3asset_vt10 | 2,000,000 | adv_5pct | 9.27% | 1.16 | -12.53% | 175 | 14.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 2,000,000 | adv_10pct | 9.27% | 1.16 | -12.53% | 175 | 14.2x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 2,000,000 | spread_5bp | 9.18% | 1.15 | -12.59% | 170 | 14.1x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 2,000,000 | spread_20bp | 8.92% | 1.12 | -12.77% | 170 | 13.9x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 5,000,000 | baseline | 9.27% | 1.16 | -12.53% | 187 | 14.3x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 5,000,000 | adv_1pct | 9.04% | 1.14 | -12.53% | 191 | 13.7x | 12 | 12 | 13,101,994 |
| tsmom_12_1_3asset_vt10 | 5,000,000 | adv_5pct | 9.22% | 1.15 | -12.53% | 187 | 14.1x | 2 | 2 | 1,126,864 |
| tsmom_12_1_3asset_vt10 | 5,000,000 | adv_10pct | 9.27% | 1.16 | -12.53% | 187 | 14.3x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 5,000,000 | spread_5bp | 9.18% | 1.15 | -12.59% | 186 | 14.3x | 0 | 0 | 0 |
| tsmom_12_1_3asset_vt10 | 5,000,000 | spread_20bp | 8.92% | 1.12 | -12.78% | 185 | 14.1x | 0 | 0 | 0 |

## Capacity view (benchmark)

CAGR by participation cap; Δ is versus the same-capital baseline.

| Capital | Baseline CAGR | 1% ADV | Δ 1% | 5% ADV | Δ 5% | 10% ADV | Δ 10% | 1% ADV capped orders |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 250,000 | 9.87% | 9.87% | +0.00pp | 9.87% | +0.00pp | 9.87% | +0.00pp | 0 |
| 500,000 | 9.92% | 9.92% | +0.00pp | 9.92% | +0.00pp | 9.92% | +0.00pp | 0 |
| 1,000,000 | 9.92% | 9.92% | +0.00pp | 9.92% | +0.00pp | 9.92% | +0.00pp | 1 |
| 2,000,000 | 9.93% | 9.86% | -0.07pp | 9.93% | +0.00pp | 9.93% | +0.00pp | 11 |
| 5,000,000 | 9.93% | 9.08% | -0.85pp | 9.93% | +0.00pp | 9.93% | +0.00pp | 63 |

- A cap that binds produces `capped_orders > 0`; zero means the 1% ADV limit was not reached at that capital.
- This is a capacity **proxy**: no order-book depth, queue position, or intraday volume profile.

## Data gaps

- **PIT delisted/liquidated ETF universe:** a PIT builder was added separately, but this execution run still uses the current-listed panel. Survivorship bias remains for this table.
- **ETF premium/discount:** historical NAV/price data is available separately; it is a diagnostic, not a constant cost on top of market prices.
- **Bid-ask spread:** no quote data; `empirical_spread_proxy` uses a Corwin-Schultz high-low estimator, which is not a quote; extra-slippage scenarios remain assumptions.
- **Partial fill:** no order-book/queue data; the 50% fill-ratio scenario is an assumption.

## Reading the result

- `adv_1pct` shows whether the 1% ADV cap binds at the tested capital. At 250k/500k on this liquid pool it may not bind; that is a finding, not a failure.
- `capped_orders` counts orders reduced by the ADV cap; `unfilled_notional` is the notional not executed because of ADV/partial-fill caps.
- Capped residuals are not carried forward; the next weekly rebalance corrects the position. This is a conservative proxy for partial fills.
- Results are in-sample research, not live trading evidence.