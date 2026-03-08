## Baseline Sample

This directory holds a fixed-input baseline for backtest and report regression checks.

Input:
- `sample_price_data.csv`

Generator:
- `generate_baseline.py`

Generated outputs:
- `trend_following_baseline_result.json`
- `trend_following_baseline_report.json`
- `trend_following_baseline_report_trades.csv`
- `trend_following_baseline_report_signals.json`
- `trend_following_baseline_report_positions.json`
- `trend_following_parameter_scan.json`

Run:

```bash
python quant-python/backtest/baselines/generate_baseline.py
```
