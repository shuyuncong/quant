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
- [ ] 在候选生成前隔离真正未查看的时间外 holdout
- [ ] 使用 2026-09 之后新窗口重新验证 near 降级
- [ ] holdout 至少满足：30 个唯一候选、10 只股票、10 个信号日

产物：

- `quant-python/signal_system/macd_near_regime_audit.py`
- `D:\tmp\macd_near_regime_audit_final\macd_near_regime_audit.json`

### 27. 第一优先级：入场与市场环境因子

按以下顺序逐个审计：

- [ ] 市场状态 × 信号类型：`bull/range/bear × near/above/buy_1`
- [ ] MACD 金叉质量：DIF-DEA 间距、柱体斜率、确认次数、确认等待天数
- [ ] 零轴位置：金叉距离零轴的标准化距离
- [ ] 趋势结构：MA20/MA60/MA250 距离与均线斜率
- [ ] 短期过热：入场前 5/20 日涨幅、距离近期高点、跳空幅度
- [ ] 中期动量：20/60 日收益和动量加速度

优先保留具备跨窗口稳定性的因子，最多进入两个正交因子后再做组合验证。

### 28. 第二优先级：风险与可执行性因子

- [ ] ATR 百分比和下行波动率
- [ ] 成交额、换手率及流动性稳定度
- [ ] 个股 Beta × 市场状态
- [ ] 跳空风险、跌停频率、涨停后追高风险
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

- [ ] MFE 后利润保护或跟踪止盈
- [ ] ATR 跟踪止损/止盈
- [ ] timeout 固定退出 vs MA-break 条件退出
- [ ] 零轴死叉 1 日确认 vs 2 日确认
- [ ] 分段止盈和回撤保护

不得重新开展“直接取消 SL8”或无证据放宽止损实验；所有退出实验必须保持单变量和尾部风险闸门。

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
- [ ] 新 holdout 在候选生成前完成隔离并保存 manifest
- [ ] 任何候选因子上线前先进入 `observe_only`，再做人工复核

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
