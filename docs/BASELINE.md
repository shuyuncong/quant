# Baseline Regression

固定输入基线样例在 `quant-python/backtest/baselines/`。

包含内容：
- 固定输入数据：`sample_price_data.csv`
- 生成脚本：`generate_baseline.py`
- 回测结果与报告输出：`trend_following_baseline_*.json/csv`
- 参数扫描输出：`trend_following_parameter_scan.json`

运行方式：

```bash
python quant-python/backtest/baselines/generate_baseline.py
```

用途：
- 回归比对回测指标是否漂移
- 回归比对报告结构是否变化
- 回归比对参数扫描输出是否稳定
