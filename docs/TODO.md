# 开发 TODO 清单

本清单基于当前 `docs/plans/` 三份正式文档整理，目标是把方案转成可直接执行的开发任务。

状态约定：

- `[ ]` 未开始
- `[~]` 进行中
- `[x]` 已完成

---

## P0：基础骨架与最小闭环

### 1. 项目结构

- [x] 创建 `core/regime/`
- [x] 创建 `core/selector/`
- [x] 创建 `core/indicators/`
- [x] 创建 `core/detectors/`
- [x] 创建 `core/position/`
- [x] 创建 `core/risk/`
- [x] 创建 `core/router/`
- [x] 创建 `backtest/engine/`
- [x] 创建 `backtest/strategies/`
- [x] 创建 `backtest/reports/`
- [x] 创建 `tests/core/`
- [x] 创建 `tests/backtest/`
- [x] 创建 `tests/integration/`

### 2. 配置基线

- [x] 更新 `quant-python/signal_system/config/config.yaml`
- [x] 增加 `regime` 配置
- [x] 增加 `manual_overrides` 配置
- [x] 增加 `position` 配置
- [x] 增加 `risk` 配置
- [x] 增加 `t_trading` 配置
- [x] 增加 `backtest` 配置
- [x] 校验配置默认值与优先级

### 3. 市场状态引擎

- [x] 实现 `core/regime/market_regime_engine.py`
- [x] 实现 `core/regime/regime_override.py`
- [x] 编写 `tests/core/regime/test_market_regime_engine.py`
- [x] 支持输出 `bull / bear / range`
- [x] 支持人工覆盖
- [x] 支持输出判断原因和分数

### 4. 持仓与仓位

- [x] 实现 `core/position/position.py`
- [x] 实现 `core/position/position_manager.py`
- [x] 编写 `tests/core/position/test_position.py`
- [x] 编写 `tests/core/position/test_position_manager.py`
- [x] 支持基本仓 / 机动仓
- [x] 支持 `2~4` 只股票约束
- [x] 支持单票仓位上限

### 5. 指标与检测器

- [x] 实现 `core/indicators/divergence.py`
- [x] 编写 `tests/core/indicators/test_divergence.py`
- [x] 实现 `core/detectors/bear_trap.py`
- [x] 编写 `tests/core/detectors/test_bear_trap.py`
- [x] 确认面积法背离逻辑
- [x] 确认空头陷阱触发条件

### 6. 选股器

- [x] 实现 `core/selector/stock_selector.py`
- [x] 编写 `tests/core/selector/test_stock_selector.py`
- [x] 接入基本面过滤
- [x] 接入换手率过滤
- [x] 接入量价结构过滤
- [x] 输出候选池和过滤原因

### 7. 趋势信号闭环

- [x] 实现趋势策略信号逻辑
- [x] 将趋势策略接入现有 `strategy_engine`
- [x] 生成买入 / 卖出 / 加仓 / 减仓信号
- [x] 补充信号解释字段

### 8. 通知闭环

- [x] 复用 `notification/notifier.py`
- [x] 设计统一通知消息结构
- [x] 推送市场状态
- [x] 推送候选池
- [x] 推送高优先级交易信号

### 9. 集成闭环

- [x] 打通“盘后选股 -> 盘中扫描 -> 通知推送”
- [x] 编写基础集成测试
- [x] 能在不接券商的前提下独立运行

---

## P1：回测与做 T

### 10. 做 T 策略

- [x] 实现 `core/position/t_trading.py`
- [x] 编写 `tests/core/position/test_t_trading.py`
- [x] 支持正 T
- [x] 支持反 T
- [x] 支持震荡 T
- [x] 与 `PositionManager` 联动

### 11. 风险控制

- [x] 实现 `core/risk/risk_manager.py`
- [x] 编写 `tests/core/risk/test_risk_manager.py`
- [x] 支持止损
- [x] 支持止盈
- [x] 支持组合回撤约束
- [x] 支持禁止新增仓位开关

### 12. 回测引擎

- [x] 实现 `backtest/engine/bt_engine.py`
- [x] 编写 `tests/backtest/test_bt_engine.py`
- [x] 接入 `Backtesting.py`
- [x] 统一数据准备逻辑
- [x] 支持策略参数输入
- [x] 支持结果落盘

### 13. 趋势策略回测

- [x] 实现 `backtest/strategies/trend_following_bt.py`
- [x] 跑通趋势策略历史回测
- [x] 输出胜率、盈亏比、回撤、年化
- [x] 输出交易次数和持仓周期

### 14. A 股成本模型基础版

- [x] 实现 `backtest/engine/china_cost_model.py`
- [x] 支持手续费
- [x] 支持印花税
- [x] 支持滑点
- [x] 支持整手交易
- [x] 预留 `T+1` 和涨跌停模型入口

---

## P2：多市场与策略路由

### 15. 震荡策略

- [x] 实现 `MeanReversionStrategy`
- [x] 编写对应测试
- [x] 适配 `range`

### 16. 下跌策略

- [x] 实现 `DefensiveStrategy`
- [x] 编写对应测试
- [x] 适配 `bear`

### 17. 突破策略

- [x] 实现 `BreakoutStrategy`
- [x] 编写对应测试
- [x] 适配趋势启动场景

### 18. 策略路由

- [x] 实现 `core/router/strategy_router.py`
- [x] 根据 `regime` 选择主策略
- [x] 处理信号冲突优先级
- [x] 支持策略评分

### 19. 多策略回测

- [x] 支持不同策略独立回测
- [x] 支持不同市场状态分组回测
- [x] 支持策略横向比较

---

## P3：评估体系与二期预留

### 20. 评估指标体系

- [x] 实现统一绩效指标计算
- [x] 增加资金周转率计算
- [x] 增加信号命中率计算
- [x] 增加分市场状态统计

### 21. 参数优化

- [x] 支持有限核心参数扫描
- [x] 区分样本内 / 样本外
- [x] 输出参数对比结果
- [x] 控制过拟合风险

### 22. 标准数据对象

- [x] 定义 `SignalRecord`
- [x] 定义 `PositionSnapshot`
- [x] 定义 `BacktestRun`
- [x] 定义 `BacktestTrade`
- [x] 定义 `StrategyConfigSnapshot`
- [x] 定义 `RegimeDecisionRecord`

### 23. 回测报告输出

- [x] 实现 `backtest/reports/performance_report.py`
- [x] 输出 JSON 报告
- [x] 输出 CSV 交易流水
- [x] 输出按市场状态拆分结果

### 24. 第二阶段接口预留

- [x] 统一信号落盘格式
- [x] 统一持仓快照格式
- [x] 统一回测结果格式
- [x] 预留文件共享接口
- [x] 预留 REST API 映射结构

---

## P4：因子审计与回测研究路线（2026-08-31）

本阶段只做研究和回测，不自动改变生产策略。每次实验只改变一个变量，候选层验证通过后才进入组合层。

### 25. 已完成审计与生产结论

- [x] raw ROE 审计：Q1-Q5 反向、非单调，不上线
- [x] report-period-normalized ROE 审计：弱正向但 IC≈0.11，不上线
- [x] 行业中性负债率审计：方向跨窗口不稳定，不上线
- [x] log 市值全池审计：train/val/test 方向不一致，不上线
- [x] SL8 取消止损反事实审计：val 改善但 test 和尾部风险恶化，继续保留 SL8
- [x] 真实 Q1/Q3 公告日和 PIT 数据链路修复
- [x] 审计产物记录候选池、行情、配置、随机种子和输出 hash
- [x] 生产基线冻结：`P0 / SL8 / TP30 / C4`，基本面默认关闭

### 26. 当前实验：弱市 `macd_near` 降级

- [x] 实现 `signal_system/macd_near_regime_audit.py`
- [x] 固定候选、固定行情、固定 SL8/TP30/T+1/费用/滑点/退出逻辑
- [x] 只移除 `range/bear + macd_golden_cross_pullback_confirmed_near`
- [x] 输出候选层和组合层双报告
- [x] 当前探索性回测：val 移除 18 笔，组合收益 `28.57% → 31.15%`
- [x] 当前结论登记为 `insufficient_sample_exploratory`，不改变生产
- [x] 在候选生成前隔离真正未查看的时间外 holdout（v2 final：全 A 5007，只含预冻结信息，未生成/查看候选收益）
- [ ] 使用 2026-09 之后新窗口重新验证 near 降级
- [ ] holdout 至少满足：30 个唯一候选、10 只股票、10 个信号日
- [x] v2 双阶段 seal：初始 freeze seal + 一次性 candidate seal，候选 hash 不允许为空
- [x] 候选生成强制 `--local-data-only`，禁止行情或股票池历史联网回填
- [x] 全池本地缓存已补齐并通过复核：NONE 5007/5007、QFQ 5007/5007、缺失 0，指数更新至 2026-09-01；生产库未连接
- [x] 新增只读 `holdout_readiness_status.py`：自动检查 freeze、全池覆盖、Holdout 交易日、候选二阶段 seal 和结果成熟度，不生成或修改任何产物
- [ ] 当前 Holdout 状态：`waiting_for_signal_days`，2026-09-01 起仅 1/10 个完整交易日；达到 10 日后才允许探测一次性候选生成
- [ ] 样本门首次满足后一次性封存候选；封存后停止生成器并等待至少 40 个后续指数交易日，正式审计仍须 `baseline_replay_complete=true`

产物：

- `quant-python/signal_system/macd_near_regime_audit.py`
- `quant-python/signal_system/freeze_holdout.py`
- `quant-python/signal_system/generate_holdout_candidates.py`
- `quant-python/signal_system/holdout_integrity.py`
- `quant-python/signal_system/holdout_readiness_status.py`
- `D:\tmp\macd_near_regime_audit_final\macd_near_regime_audit.json`
- `D:\tmp\holdout_full_a_v2_final\holdout_freeze.json`
- `D:\tmp\holdout_full_a_v2_final\holdout_freeze.seal`

### 27. 第一优先级：入场与市场环境因子

按以下顺序逐个审计：

- [ ] 市场状态 × 信号类型：`bull/range/bear × near/above/buy_1`
- [x] MACD 金叉质量候选层审计器已实现：主因子固定为归一化 DIF-DEA 间距；柱体斜率、确认次数、确认等待天数仅作诊断，不得事后择优
- [x] MACD 金叉质量正式回测：gap_strength 在 train/val/test 的方向弱反或不稳定，所有 high-low cluster bootstrap CI 跨 0，`candidate_gate.pass=false`；不进入 P5a/组合层，生产维持 P0
- [x] MACD 细分因子暂停：面积背离与金叉间距均被候选层拒绝，不反向重测、不从柱体斜率/确认次数/等待天数中事后择优
- [x] MACD 面积背离审计器已实现并验证：完整柱体周期、底/顶背离、面积收缩比、价格新低/新高幅度、确认等待天数、候选层闸门与组合层保护
- [x] MACD 面积背离候选层回测：去重后 train/val/test 为 16/312/45，底背离样本 2/24/6，全部不足；方向跨窗口不一致，`candidate_gate.pass=false`，组合层未运行，生产维持 P0
- [x] 候选导出唯一性关卡：`candidate_integrity.py` 只移除同 ID 且内容完全相同的行；同 ID 内容冲突立即失败；三套 canonical 候选和 hash manifest 已生成
- [ ] 根源合并连续再金叉：待 near Holdout 完成或单独重新冻结后，修改已封印的 `strategy/macd.py`/`backtest_winrate.py`；此前研究只能读取 canonical 候选
- [ ] 零轴位置：金叉距离零轴的标准化距离
- [x] 趋势结构候选层审计器已实现：主因子固定为 `MA250` 的 20 交易日标准化斜率；MA20/MA60 斜率、价格距均线和多头排列仅作诊断
- [x] 趋势结构正式回测：MA250 斜率的 f40 high-low 三窗弱正但 CI 全部跨 0，train/val 的 trade PnL 反向，`candidate_gate.pass=false`；不进入组合层，生产维持 P0
- [x] 趋势结构细分暂停：不反向测试低斜率，不改用 MA20/MA60、均线距离或多头排列事后择优，不扫描其他回看周期
- [x] 量价关系候选层审计器已实现：唯一主假设固定为“信号日上涨且成交量不低于前 20 交易日均量”；前 20 日基准严格排除信号日；四象限、价格位置和连续量价因子仅作诊断
- [x] 量价关系正式候选层回测：train/val/test 样本与覆盖均充足，但“上涨且放量”在 train 为负、val 混合、test 为正，跨分片方向不一致且关键 CI 多数跨 0；`candidate_gate.pass=false`
- [x] 量价数据可用性已核验：本地 QFQ 缓存 `amount` 全为 0，`amount_ratio_20` 与 `turnover_value_proxy` 必须登记为 unavailable，不得将 0 当有效成交额或真实换手率
- [x] 量价主因子已拒绝：不反向测试“缩量更好”，不从四象限或诊断因子中事后择优；成交额/真实换手率因本地数据不可用仍登记为未验证，生产维持 P0
- [x] 短期过热候选层审计器已实现：唯一主因子固定为截至信号日收盘的 `pre_entry_20d_return`，同日较冷组减较热组；5 日涨幅、距 20 日高点和信号日跳空仅作诊断
- [x] 短期过热正式候选层回测：样本与覆盖充足，train/val 点估计支持“越冷越好”，但 test future_40d 反转且所有关键 CI 跨 0；`candidate_gate.pass=false`
- [x] 短期过热主因子已拒绝：不反向测试“越热越好”，不扫描其他回看周期，不从 5 日涨幅、距高点或跳空中事后择优，生产维持 P0
- [x] 中期动量候选层审计器已实现：唯一主因子固定为 `pre_entry_60d_return` 越高越好；20 日收益、20/60 对数日均加速度和 60 日上涨比例仅作诊断
- [x] 中期动量正式候选层回测：train 方向与假设相反，val/test 混合，全部关键 CI 跨 0，`candidate_gate.pass=false`
- [x] 中期动量因子已拒绝：不反向重测、不扫描其他周期、不进入组合层，生产维持 P0；暂停继续扩展入场技术因子

优先保留具备跨窗口稳定性的因子，最多进入两个正交因子后再做组合验证。

### 28. 第二优先级：风险与可执行性因子

- [x] ATR20 百分比正式候选层审计：train 两个主结果均为负，跨 split 不一致，关键 CI 多数跨 0，`candidate_gate.pass=false`；下行半偏差仅诊断，生产维持 P0
- [ ] 成交额、换手率及流动性稳定度
- [x] 低 Beta60 风险候选层审计器已实现：全 canonical 同日 low-high 为主；原 range/bear-only 方案因 train/test 先天样本不足在实现前调整；regime、correlation、downside beta、残差波动仅诊断
- [x] 低 Beta60 正式候选层回测：train/val/test 主结果方向不一致，全部 bootstrap CI 跨 0，`candidate_gate.pass=false`；不进入组合层，生产维持 P0
- [x] 跳空风险正式候选层审计：唯一主因子固定为 60 日下行开盘 gap 半偏差，同日低风险组减高风险组；跌停开盘代理、大跌 gap 频率、最差 gap、none 口径和涨停后追高 gap 仅作诊断。train 两个主结果均为负，test trade PnL 为负，所有主 CI 跨 0，`candidate_gate.pass=false`；不反向测试、不扫描阈值/周期、不进入组合层，生产维持 P0
- [x] Overnight Variance Share60 风险候选层正式回测：隔夜平方收益占“隔夜+盘中平方收益”代理的比例；train/val/test 方向混杂，全部关键 bootstrap CI 跨 0，`candidate_gate.pass=false`；不进入组合层，生产维持 P0
- [ ] 行业集中度和组合相关性
- [ ] 同日候选拥挤度、行业信号密度

这些因子优先用于风险降级或过滤，不默认作为收益排序因子。

### 29. 第三优先级：基本面因子

只有在真实公告日 PIT 和历史行业分类可用时才审计：

- [ ] 现金流质量：经营现金流/净利润
- [ ] 资本回报：ROIC、ROA
- [ ] 盈利稳定性：毛利率、营业利润率及其变化
- [ ] 成长：营收同比、利润同比、增长加速度
- [ ] 估值：盈利收益率、PB、自由现金流收益率
- [ ] 财务风险：利息保障倍数、行业中性负债率复核
- [ ] 亏损标记：负 PE 单独处理，不与盈利股混排

ROE、统一负债率和 log 市值在没有新数据范围或新方法前不重复堆叠。

### 30. 第四优先级：退出与仓位模型

- [x] MFE 后利润保护或跟踪止盈（2026-10-10：`risk.exit_plan` 支持保本止损/移动止盈；回测见归档）
- [ ] ATR 跟踪止损/止盈
- [ ] timeout 固定退出 vs MA-break 条件退出
- [ ] 零轴死叉 1 日确认 vs 2 日确认
- [x] **持仓归属策略字段（2026-10-12 已完成）**：`quant.holdings` 新增 `strategy_id`（schema v5，
  幂等 `ADD COLUMN IF NOT EXISTS`，存量行默认空串 = 未归属，行为不变）；`/assets?tab=holdings`
  编辑表单新增「归属策略」选择，`PUT /api/holdings/<symbol>` 接受并校验该字段（未知 id → 422），
  归属只在编辑既有持仓时下发。`portfolioSnapshot` 已 `SELECT *`，字段随 `portfolio_context`
  进到 `stop_owner`，`risk.strategy_exit_plan.<id>` 与 `risk.strategy_stop_loss_pct.<id>`
  在线上按归属生效。回归：`HoldingAttributionTests`（Python，归属生效 / 未归属不借口径）
  + `holding strategy ownership` / `默认未归属，指定归属后能读回`（web，TS）。
  升级需 `npm run db:setup` 且 `SCHEMA_VERSION` 已同步为 5。
- [x] 分段止盈和回撤保护（2026-10-10：分批减仓 + 尾仓保护引擎与配置已落地，24 组 2024H1 对照 + 2024H2 样本外复核方向一致；**未更改默认参数**）
- [x] 每日卖出信号与 AI 解读带上分批口径（2026-10-11：`strategy/registry.exit_plan_advice` 给出下一档触发价/建议股数/保本止损位，
  `evaluate_strategies` 在实盘层按当日日线最高价补 `met`，推送 evidence 带 `exit_plan_advice`，`/results` 条件表直接显示执行摘要，
  AI 提示词要求照抄引擎数字；回测回放层保持 `met = null` 不受影响。档位进度改由成交记录反推
  （台账无 `exit_levels_taken`），样本不全时文案标明"按已知成交估算"；30 组 parity 仍零差异）
- [x] **下跌区间核验（2026-10-12，原定 2018/2022 熊市）**：**原定区间无法执行** ——
  本地 `cache/daily_history`（5008 只）起点中位数 2023-05-18、最早 2022-12-26，0 只覆盖 2018/2022；
  且三策略需 270 根预热，第 270 根中位在 2024-06-27，2023H2 及更早无任何可跑标的。
  改用唯一可跑且下跌的 2024Q4（4739 只、61 个交易日、指数 −3.95%）：年线趋势下跌区间仍是分批有害
  （基线 +2.37% vs 阶梯 −2.14%/−3.90%，第三个区间支持"年线不适配分批"）；零轴＋底背离仍偏阶梯
  （+0.45%/−0.48% vs 基线 −6.04%）；但**日线零轴金叉的"半仓+尾仓保本"在下跌区间变成最差**
  （−21.65%，比基线 −20.18% 还低且回撤更大），与 H1/H2 的"最稳"结论冲突。
  归档 [2026-10-12 核验](../docs/2026-10-12-三策略止盈分批减仓熊市区间核验.md)。
  **默认参数保持关闭，本轮未产生投产依据**；补数据前不再重复同一数据范围内的回测。
- [x] **日线零轴金叉减仓方案重分析（2026-10-12）**：三区间全方案对照后，
  **"半仓@+15% + 尾仓保本止损"推荐作废**。机制已定位：保本止损把尾仓止损抬到成本价，
  下跌区间等于"在反弹起点被扫出"（`002302`：ladder_b 尾仓留到 +5.2%，hybrid_b 在成本价被扫出）。
  equity 对账显示差额主要来自**期末未平仓位的浮亏**（−80.9k vs 基线 −54.8k），而非已平仓 PnL
  （仅差 652 元）——分批腾出的现金在单边下跌里没有更好的去处。该方案 Q4 盈亏比 0.75（基线 0.55）、
  回撤反而更大（20.18%→21.78%）。`trailing_a` 三区间 −11.36%/+14.60%/−6.28%，形态依赖、不可作默认。
  三区间都不曾垫底的只有阶梯减仓（改善 0.7~1.2pp，不足以改默认）。
  归档 [2026-10-12 重分析](../docs/2026-10-12-日线零轴金叉减仓方案重分析.md)。
- [ ] **（新增阻塞）补 2018/2022 个股日线**：要真正回答熊市问题须拉取 2018 前后日线进
  `cache/daily_history`；MA250 预热会再吃掉前 270 个交易日，只补 2022 仍覆盖不到 2022 下跌段。

不得重新开展“直接取消 SL8”或无证据放宽止损实验；所有退出实验必须保持单变量和尾部风险闸门。
分批止盈属同一单变量闸门：档位/比例的任何默认变更都必须先在非重叠区间复核，
且不得只凭单一半年样本的收益最高档直接投产。已知差异：年线趋势在两个区间都不适合分批减仓。

### 31. 统一因子验收闸门

- [ ] 候选覆盖率不低于 90%
- [ ] 受影响样本至少 30 个唯一候选、10 只股票、10 个信号日
- [ ] train、val、新 holdout 方向一致
- [ ] Q1-Q5 或分组结果不能只在单一窗口成立
- [ ] IC、正 IC 日比例和组合收益不能接近随机或方向反复
- [ ] 组合层收益改善不能伴随明显最大回撤恶化
- [ ] 使用按股票或时间分组的 cluster bootstrap
- [ ] holdout 只能查看一次，不能事后改规则
- [ ] 单因子通过后才允许做双因子组合

### 32. 研究与生产隔离

- [x] 回测、测试只使用本地数据和本地 PostgreSQL
- [x] 禁止连接生产 PostgreSQL 执行回测或验证
- [x] 研究参数使用运行期覆盖或研究副本，不写生产 `config.yaml`
- [x] 新 holdout 在候选生成前完成隔离并保存 manifest（`holdout_freeze.v2`）
- [ ] 任何候选因子上线前先进入 `observe_only`，再做人工复核

---

### 33. 市场闸门核验（2026-10-13）

- [x] **核验结论**：闸门在实盘生效（`market_gate_enabled: true`、fail-closed，关闭时候选降级
  `observe_only` 不静默丢弃），但**没有可证明的入场择时优势**：日级 63 个（季度×策略×horizon）
  配对中开放日更优 28 / 关闭日更优 35，中位 Δ −0.29pp、均值 −2.00pp。
  且在两次大级别下跌起点是**开着的**（2026-06 开放期间 6/17 的 78 只信号 20 日中位 −27.7%；
  2025Q1 Δ −12.09pp）。原"信号池化"口径因两组落在不同日历时段、且未剔除样本末端而得出
  相反的表象，已废弃。归档 [2026-10-13 市场闸门核验](../docs/2026-10-13-市场闸门核验.md)。
- [x] **记录闸门严格度**：1200 个交易日只开放 210 天（17.5%）；2021、2022 全年 0 天；
  2024H1 全年 0 天（根因 `ma_long_up` 在 117/117 天全为 False，同期 strict bull 有 41 天）。
- [x] **作用范围澄清（2026-10-13）**：闸门**只约束日线零轴金叉**。三个扫描入口中只有
  `scan_zero_axis`（`service.py:838`）引用 `allows_entries`；`scan_yearline`（`:1315`）与
  `scan_macd_divergence`（`:1541`）**0 处引用**，且 docstring 明确它们是独立研究展示池、
  不进监控/不下单/不推送，后者写明"不受执行策略与市场闸门影响"。
  另记两套回测的口径分裂：旧回测对**所有**买点套闸门，新回测（三策略对照）完全不读闸门。
- [ ] **单独评估 `ma_long_up`（MA250 斜率）**：它是把 2024H1 整个半年锁死的一票否决条件。
  可考虑改为斜率均值阈值或降级为加权项。**必须先做独立对照再改，禁止顺手调参。**
- [ ] **`position.mobile_cash_ratio`（预留 25% 机动现金）二选一**：实现它，或从配置与文档删除。
  现状是死配置——全仓库只有写入无读取（`utils/helpers.py:354` 抄进 `reserve_cash_ratio` 后无人使用），
  实测平均部署率 94.1%、61 天里 55 天满 4 仓，**文档与行为不一致比没有该功能更糟**。
- [ ] **给闸门加可观测性（仅对日线零轴金叉有意义）**：把 `blocked_by` 逐日分布与
  "因闸门关闭而被拦下的信号数"纳入日报，使"闸门是否过严"可由运行数据回答，
  而非每次临时写脚本。年线趋势与零轴＋底背离不在闸门覆盖内，它们要的是
  "是否纳入生产链路"的决策。

---

## 验收里程碑

### M1：能跑

- [x] 自动盘后选股
- [x] 自动市场状态判断
- [x] 自动信号推送

### M2：能回测

- [x] 趋势策略回测可运行
- [x] 回测输出关键指标
- [x] 成本模型基础版可用

### M3：能扩展

- [x] 支持多市场策略
- [x] 支持多策略路由
- [x] 支持统一报告输出

### M4：能复盘

- [x] 有标准化信号记录
- [x] 有标准化持仓记录
- [x] 有标准化回测记录
