# 小市值策略研究归档（2026-11-06）

## 一句话结论

**这条线暂停，不投产，不买数据救它。**

从“5 年 30 倍”开始，经过日线近似、PIT 全 A、退市股、成本压力、质量过滤、shell-free 优化，最好的可复现结果是：

> **shell-free 优化 v2：4.50% CAGR / Sharpe 0.386 / 最大回撤 −24.74%**  
> 同期 ETF 基准：**9.87% / 1.200 / −10.19%**

**风险调整后仍远不如 ETF；30 倍不是可复现的因子收益。**

## 研究链路

| 日期 | 实验 | 关键结果 | 归档 |
|---|---|---|---|
| 2026-11-05 | 原文策略的日线近似 | 价格类核心最好 14.15% / 0.60 / −48.83%；未复现 30 倍/17% | [`2026-11-05-smallcap-strategy`](../2026-11-05-smallcap-strategy/memo.md) |
| 2026-11-05 | 当前中证 1000 快照质量 A/B | 无过滤 31.34% / 1.385 / −30.15%；宽松 27.44%、严格 11.75%，质量过滤无效 | [`2026-11-05-quality-filter`](../2026-11-05-quality-filter/memo.md) |
| 2026-11-06 | 2025–2026 数据扩展 | 4,815/4,817 只 Sina 日线；2025 年报 PIT | [`2026-11-05-股票PIT数据层`](../../2026-11-05-股票PIT数据层.md) |
| 2026-11-06 | PIT 全 A + 237 只退市股，严格 50 | **−2.03% / −0.014 / −45.45%** | [`2026-11-06-pit-smallcap`](../2026-11-06-pit-smallcap/memo.md) |
| 2026-11-06 | PIT 全 A 版质量 baseline（100→30 月度） | **6.44% / 0.399 / −48.52% / 盈亏比 0.899** | [`2026-11-06-pit-quality-baseline`](../2026-11-06-pit-quality-baseline/memo.md) |
| 2026-11-06 | 成本与稳健性矩阵 | 5bp −2.03%；10bp −4.79%；20bp −9.98%；换排名带/去行业 cap/池 150/动态 3–6 都没有可投结果 | [`2026-11-06-smallcap-robustness`](../2026-11-06-smallcap-robustness/memo.md) |
| 2026-11-06 | 优化 v1：50 只、行业 cap、月度、10bp、1% ADV、10% vol target | 1.05% / 0.167 / −30.44%；换手降到 8.4x，但仍不可投 | [`2026-11-06-smallcap-optimized`](../2026-11-06-smallcap-optimized/memo.md) |
| 2026-11-06 | 优化 v2：按 Liu–Stambaugh–Yuan 剔除最小 30% 壳价值区 | **4.50% / 0.386 / −24.74%** | [`2026-11-06-smallcap-shellfree`](../2026-11-06-smallcap-shellfree/memo.md) |
| 2026-11-06 | 三策略网页 | 质量 baseline / ETF / 小市值基线，2020–2026 | [`2026-11-06-three-strategies`](../2026-11-06-three-strategies/index.html) |
| 2026-11-06 | JQData 试用探针 | 只覆盖 2025-07-29→2026-08-05；无法补 2020–2024 | [`2026-11-06-JQData试用探针`](../../2026-11-06-JQData试用探针.md) |

## 为什么停

1. **壳价值污染**  
   Liu–Stambaugh–Yuan：A 股最小 30% 市值约 30% 是反向并购壳价值；83% 的壳来自这一段。原策略“买最小 50”正好踩中。2024 年退市/IPO 改革后壳价值压缩，回撤放大。

2. **幸存者偏差**  
   当前中证 1000 快照回填 2020–2026，31% 的 baseline 在 PIT 全 A + 退市后只剩 6.44%；严格 50 甚至 −2.03%。

3. **成本吃掉收益**  
   5bp 已经负；10bp −4.79%；20bp −9.98%。微盘换手 50x+，成本是第一杀手。

4. **质量过滤没有增量**  
   当前快照上宽松 27.44%、严格 11.75%，都低于无过滤 31.34%；文献也表明盈利/低波异象大多被 size/value 解释。

5. **shell-free 也只到个位数**  
   剔除最小 30% 后 4.50% / 0.386 / −24.74%；文献 EP+ME 约 4.17% / 0.17 / 38.35%，同一量级。**现实 size/value 不是 30 倍。**

## 文献锚点

- Liu, Stambaugh, Yuan (2019), *Size and Value in China*: https://www.nber.org/papers/w24458
- He (2025), *Mitigating Shell Value Contamination in China’s A-Share Market*: https://www.atlantis-press.com/article/126015009.pdf
- Man Group, *The Impact of Volatility Targeting*: https://www.man.com/insights/the-impact-of-volatility-targeting
- 2024 小微盘回撤报道: https://www.yicai.com/news/102074491.html

## 什么情况下再研究

只有同时满足，才重新开启：

- [ ] 拿到**历史 PIT 中证 1000 成分**（含纳入/剔除日期），不再用当前快照；
- [ ] 拿到**完整退市股日线**（当前 237/367，仍有缺口）；
- [ ] 拿到**全 A PIT 财务**：EP、ROE、扣非、经营现金流、商誉、负债、审计意见、质押、立案；
- [ ] 用 **EP + ME**（不是纯 size）做预注册复现；
- [ ] 扣 **10bp 滑点 + 1% ADV** 后，Sharpe > 0.8、最大回撤 < 25%；
- [ ] 2025–2026 作为时间 holdout，不参与调参；
- [ ] 通过后跑 6–12 个月纸面账本，再谈实盘。

## 不要做

- ❌ 不再调小市值排名带、止损阈值、行业 cap、band、vol target；
- ❌ 不再用当前中证 1000 快照回填历史；
- ❌ 不再加 MACD/动量/微盘一致性/放量换手；文献和中国数据都不支持动量；
- ❌ 不为这条线买 Wind/JQData 正式版；除非先证明 PIT 版有正期望。

## 代码与数据

- `quant-python/signal_system/smallcap_strategy_study.py`
- `quant-python/signal_system/quality_filter_study.py`
- `quant-python/signal_system/smallcap_robustness_study.py`
- `quant-python/signal_system/smallcap_optimized_study.py`
- `quant-python/signal_system/pit_quality_baseline.py`
- `quant-python/signal_system/stock_pit_data.py`
- `quant-python/signal_system/jqdata_probe.py`
- 测试：`tests/test_smallcap_strategy_study.py`、`tests/test_quality_filter_study.py`、`tests/test_stock_pit_data.py`
- 本地数据：`quant-python/signal_system/cache/stock_pit/`（gitignored）
- 研究归档：`docs/backtest-results/2026-11-06-smallcap-research-archive/`
