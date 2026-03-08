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
