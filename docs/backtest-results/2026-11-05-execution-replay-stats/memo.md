# 执行压力、纸面账本 replay、bootstrap 与拥挤度复盘

- 日期：2026-11-05
- 状态：研究/审计产物，**不改变生产规则，不接券商，不下单**
- 数据：本地 ETF qfq 缓存；ETF 池为当前上市标的
- 口径：40% 核心 + 卫星 ≤35% + 回填 35%，10% 波动目标；基准周度，TSMOM 月度

## 1. V2 纸面账本 replay：与 canonical 回测逐点一致

用 `plan_paper`/`fill_paper` 的同一套计划与成交逻辑回放 2019-01-02 → 2026-10-30：

| 指标 | Paper replay | Canonical backtest | 差异 |
|---|---:|---:|---:|
| 总收益 | 108.28% | 108.28% | 0.00pp |
| CAGR | 9.83% | 9.83% | 0.00pp |
| Sharpe | 1.21 | 1.21 | 0.00 |
| 最大回撤 | −10.19% | −10.19% | 0.00pp |
| 最终权益 | 520,704.99 | 520,704.93 | **0.06 CNY** |
| 订单数 | 615 | 615 | 0 |
| 年换手 | 87.91x | 87.91x | 0 |

- 1,897 个对齐交易日；最大/平均相对权益差 **0.0000%**，跟踪误差 **0.0000%**。
- 差异仅来自纸面账本把现金/价格四舍五入到分，最终权益差 6 分钱。
- 结论：**账本 replay 与 canonical 回测同源**；但这是历史回放，不是实盘 track record，也不证明未来收益。

## 2. V4/V5 执行压力：1% ADV 在当前资金规模不绑定，价差是主要成本

同一信号、同一区间，只改执行：

### 250,000 CNY

| 策略 | 场景 | CAGR | Sharpe | 最大回撤 | 换手 | 1% ADV 截断 | 部分成交 | 未成交名义额 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| ETF 基准 | baseline | 9.87% | 1.20 | −10.19% | 72.1x | 0 | 0 | 0 |
| ETF 基准 | **1% ADV** | 9.87% | 1.20 | −10.19% | 72.1x | **0** | 0 | 0 |
| ETF 基准 | +5bp 价差 | 9.43% | 1.15 | −10.41% | 71.2x | 0 | 0 | 0 |
| ETF 基准 | +10bp 价差 | 9.00% | 1.10 | −10.64% | 70.1x | 0 | 0 | 0 |
| ETF 基准 | +20bp 价差 | 8.14% | 1.00 | −11.09% | 68.2x | 0 | 0 | 0 |
| ETF 基准 | **经验 spread 代理** | **8.32%** | **1.02** | −10.88% | 68.5x | 0 | 0 | 0 |
| ETF 基准 | 折溢价 10bp | 9.00% | 1.10 | −10.64% | 70.1x | 0 | 0 | 0 |
| ETF 基准 | 50% 部分成交 | 8.49% | 1.07 | −9.60% | 41.0x | 0 | 4,762 | 14,028,669 |
| ETF 基准 | 组合压力 | 7.45% | 0.95 | −9.70% | 39.8x | 0 | 4,747 | 13,483,816 |
| TSMOM 3 资产 + 10% vol | baseline | 9.21% | 1.15 | −12.48% | 13.2x | 0 | 0 | 0 |
| TSMOM 3 资产 + 10% vol | **1% ADV** | 9.21% | 1.15 | −12.48% | 13.2x | **0** | 0 | 0 |
| TSMOM 3 资产 + 10% vol | **经验 spread 代理** | **9.07%** | **1.14** | −12.67% | 13.1x | 0 | 0 | 0 |
| TSMOM 3 资产 + 10% vol | 组合压力 | 8.16% | 1.07 | −12.94% | 7.2x | 0 | 214 | 2,349,316 |

### 500,000 CNY

| 策略 | 场景 | CAGR | Sharpe | 最大回撤 | 1% ADV 截断 | 未成交名义额 |
|---|---|---:|---:|---:|---:|---:|
| ETF 基准 | baseline | 9.92% | 1.20 | −10.20% | 0 | 0 |
| ETF 基准 | 1% ADV | 9.92% | 1.20 | −10.20% | **0** | 0 |
| ETF 基准 | 经验 spread 代理 | **8.34%** | **1.03** | −10.90% | 0 | 0 |
| ETF 基准 | 组合压力 | 7.49% | 0.96 | −9.69% | 0 | 24,135,205 |
| TSMOM 3 资产 + 10% vol | baseline | 9.25% | 1.16 | −12.52% | 0 | 0 |
| TSMOM 3 资产 + 10% vol | 1% ADV | 9.25% | 1.16 | −12.52% | 0 | 0 |
| TSMOM 3 资产 + 10% vol | 经验 spread 代理 | **9.10%** | **1.14** | −12.72% | 0 | 0 |
| TSMOM 3 资产 + 10% vol | 组合压力 | 8.26% | 1.07 | −12.82% | **1** | 4,398,057 |

- **1% ADV 在 250k/500k 基本不绑定**：基准 0 笔截断；TSMOM 只在 500k 组合压力下出现 1 笔（2020-03-02 国债 ETF）。这不是说 V4 不重要，而是说当前资金规模下它不是第一约束；资金放大或流动性收缩时才需要。
- **价差是主要成本**：基准 +10bp 价差 ≈ −0.87pp CAGR / −0.10 Sharpe；+20bp ≈ −1.73pp / −0.20 Sharpe。
- **经验 spread 代理**：用日线高低价 Corwin-Schultz 估计的 half-spread 逐日套用，基准 250k/500k 分别降至 **8.32%/1.02** 和 **8.34%/1.03**；TSMOM 只降至 **9.07%/1.14** 和 **9.10%/1.14**。代理 P50 约 5.7bp，但 P95 约 93bp，基准高换手放大尾部成本。该场景叠加在 5bp 基础滑点上，可能双重计算，应视为偏保守上界，不是净成本校准。
- **V5 溢价/折价已可用历史 NAV 验证**：本池均值 +0.013%、P95 +0.284%、|值|>1% 仅 0.37%；不是主要成本，也不能在市场价之上再加一次。详见 [`microstructure_report.md`](microstructure_report.md)。
- **盘口/部分成交仍不可用**：高低价代理不是 quote；逐笔/队列/部分成交没有数据，50% fill-ratio 仍是假设。
- **V4 PIT 退市/清盘池仍不完整**：SSE 历史规模表可用（2020–2025 并集 640 个代码，66 个不在当前列表，含退市/合并/改名）；`510070`/`510110` 样例的行情和 NAV 可补齐。SZSE 历史列表不可用；当前 47 只都是当前上市标的，完整 PIT 尚未接入回测，幸存者偏差未消除。

## 3. Bootstrap + 多重检验：1.14 vs 1.20 不是显著差异

- 区间：2020-01-03 → 2026-10-30；1,652 个交易日；20 个固定变体。
- 方法：stationary block bootstrap，10,000 次，block=20 天。

| 策略 | Sharpe | 95% CI | Bootstrap SE |
|---|---:|---:|---:|
| ETF 基准 | 1.198 | 0.505 – 1.903 | 0.357 |
| TSMOM 3 资产 + 10% vol | 1.136 | 0.469 – 1.872 | 0.361 |

- 配对差异（基准 − TSMOM）：**+0.062**；95% CI **−0.426 – +0.501**；双侧 p = **0.807**；P(diff>0)=0.597。
- Deflated Sharpe（20 次试验）：基准 **0.86**，TSMOM **0.82**。
- Reality check（20 个变体的最大均值）：p = **0.177**；不能拒绝“最好变体只是数据挖掘”的原假设。
- 结论：**基准 1.20 与 TSMOM 1.14 的差距在统计上不可区分**；不要用这一点 Sharpe 差去选策略。基准的优势主要是回撤更低、规则更完整，而不是 Sharpe 显著更高。
- 边界：block bootstrap 仍假设分段平稳；DSR 假设试验独立，20 个变体高度相关时会偏保守；p 值只用于研究，不是生产门槛。

## 4. 季度拥挤度/生命周期复盘：只记录可观测指标

- 覆盖 2019Q1 → 2026Q4，32 个季度；指标：`n_trend`、`n_liquid`、median ADV20、60 日平均两两相关、12−1 动量离散度、沪深300 回撤/动量。
- 固定触发条件：回撤 ≤−10%、平均相关 ≥0.65、动量离散度 ≤0.10、趋势候选 ≤2、可交易标的 ≤10、沪深300 12−1 ≤0；**另有“拥挤度数据不可用”常驻标记**。
- 最新 2026Q4（2026-10-30）：gate=False，`n_trend=0`，`n_liquid=35`，median ADV20=319.4M，平均相关 0.373，动量离散度 0.251，沪深300 回撤 −20.2%，12−1 动量 −0.002，市场触发 3 项。
- **不可用数据**：ETF 资金流、融券/空头、持仓/定位；这些字段明确为 `None`，没有伪造拥挤度分数。折溢价已单独用历史 NAV 补算，见 `microstructure_report.md`。
- 用法：触发项是**复盘提示**，不是卖出信号；需要人工回答“为什么相关/宽度/流动性变了”。

## 5. 结论

1. 纸面账本 replay 已与 canonical 回测对齐，V2 的“回测 ≠ 账本”问题在当前规则下关闭；但它仍不是实盘验证。
2. V4 的逐单 1% ADV 已实现；在 250k/500k 下不绑定，资金放大后再测。PIT 退市池只完成数据可行性（SSE 历史可补、SZSE 不可用），尚未接入回测，幸存者偏差未消除。
3. V5 的溢价/折价已用历史 NAV 验证（很小）；盘口仍无真实 quote，经验高低价 spread 代理把基准压到约 8.3%/1.02，把 TSMOM 压到约 9.1%/1.14。部分成交仍是假设。
4. 基准 vs TSMOM 的 Sharpe 差异不显著；不要用 1.14 vs 1.20 做选择。但执行成本敏感性差异明显：基准高换手、卫星多，受 spread 影响更大。
5. 拥挤度复盘已落地为可观测季度表，数据缺口明确标注；不生成交易信号。

## 6. 前向纸面账本已启动

- 500,000 CNY，状态目录 `quant-python/signal_system/state/forward_paper_500k/`。
- 2026-11-05 初始化，空仓；首个计划等待 2026-11-06 周五收盘后生成，下一交易日开盘纸面成交。
- 运行：`python quant-python/signal_system/forward_paper.py --action cycle --sync`。
- 记录：跟踪误差、成交滑点、现金/整手、未成交原因；6–12 个月后再评估，不中途调参。
- 详见 [`2026-11-05-forward-paper/README.md`](../2026-11-05-forward-paper/README.md)。

## 产物

- `execution_stress_250k.json` / `execution_stress_250k.md`
- `execution_stress_500k.json` / `execution_stress_500k.md`
- `microstructure_summary.json` / `microstructure_report.md`
- `paper_replay.json`
- `bootstrap.json` / `bootstrap_report.md`
- `crowding.json` / `crowding_quarters.csv` / `crowding_report.md`

脚本：

- `quant-python/signal_system/etf_execution_stress.py`
- `quant-python/signal_system/etf_microstructure.py`
- `quant-python/signal_system/forward_paper.py`
- `quant-python/signal_system/etf_rotation/paper_replay.py`
- `quant-python/signal_system/strategy_statistics.py`
- `quant-python/signal_system/etf_crowding_review.py`

测试：

- `tests/test_etf_execution_stress.py`
- `tests/test_etf_microstructure.py`
- `tests/test_etf_paper_replay.py`
- `tests/test_strategy_statistics.py`
- `tests/test_etf_crowding_review.py`
- `tests/test_etf_runtime_parity.py`（含 `initialize_paper(plan=False)`）
