# 回测中心

打开 `overview.html` 是总览页；`index.html` 是按策略类别分页签的对比浏览器。
每个对比页包含 2020-01-02 至 2026-10-30、初始 20 万的净值曲线、核心指标、年度收益和策略说明。
`catalog/` 下是每个历史结果目录的独立详情页；“全部目录”页签可以搜索。

## 结构

- `overview.html`：总览（卡片、总曲线、区间指标、年度收益）。
- `index.html`：分类对比浏览器（按策略类别/对比回测分页签）。
- `<comparison-id>/`：一个对比回测的独立页面、`data.json`、`curves.csv`、`metrics.csv`、`annual.csv`。
- `catalog/<result-dir>/index.html`：历史目录详情页，链接原始 memo/report/summary/curves。
- `data/manifest.json`：总览嵌入数据的副本，便于检查。

## 重新生成

```bash
cd /home/ubuntu/development/quant
quant-python/.venv/bin/python scripts/build_backtest_hub.py
```

标准化口径：每个策略曲线在 2020-01-02 归一化到 20 万；年化按 252 个交易日折算；
股票策略的胜率/盈亏比为交易口径，ETF/基准为日频口径。2026 为年初至今。
