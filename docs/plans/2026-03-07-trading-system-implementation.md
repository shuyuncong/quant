# 量化交易系统实施计划（V1 / V2）

**文档定位**：基于 `2026-03-07-implementation-plan-aligned.md` 的落地实施计划  
**阶段目标**：先做“可用的信号系统”，再做“完整的多市场策略系统”

---

## 1. 实施原则

本实施计划遵循以下原则：

1. **先信号，后增强**
2. **先复用，后扩展**
3. **先验证规则有效，再优化参数**
4. **代码围绕策略内核组织，不围绕券商组织**
5. **所有新增模块都要同时考虑实时信号和回测复用**

---

## 2. 目标产物

完成 V1 / V2 后，系统应具备：

- 盘后自动选股
- 市场状态自动判断
- 手动覆盖市场状态
- 多策略信号输出
- 基本仓 + 机动仓建议
- 做 T 信号建议
- 回测与报告输出
- 企业微信/邮件通知

---

## 3. 目录规划

建议按以下结构推进：

```text
quant-python/
└── signal_system/
    ├── data/
    ├── notification/
    ├── strategy/
    ├── service/
    └── main.py

core/
├── regime/
├── selector/
├── indicators/
├── detectors/
├── position/
├── risk/
└── router/

backtest/
├── engine/
├── strategies/
└── reports/

tests/
├── core/
├── backtest/
└── integration/
```

---

## 4. V1：最小可用版本

## 4.1 V1 目标

先做一套你能真正每天使用的系统：

- 收盘后给你候选股
- 盘中给你买卖/加减仓信号
- 不需要盯盘
- 不需要接券商
- 能回测趋势策略是否有效

## 4.2 V1 范围

### 模块 A：配置与基础设施

- 统一配置文件
- 调度入口
- 输出目录规范
- 日志与错误处理

### 模块 B：市场状态引擎

新增：

- `MarketRegimeEngine`
- `RegimeOverride`

能力要求：

- 自动判断 `bull / bear / range`
- 支持人工强制覆盖
- 输出判断原因

### 模块 C：选股器

新增：

- `StockSelector`

规则先聚焦你已有模式中的核心共识：

- 年线方向
- 基本面过滤
- 换手率
- 成交量结构
- 量价关系

### 模块 D：信号检测

新增或增强：

- 面积法 MACD 背离
- 空头陷阱检测
- 趋势买点 / 卖点

### 模块 E：仓位与风险

新增：

- `Position`
- `PositionManager`
- `RiskManager`

V1 默认参数：

- `target_stocks = 3`
- `base_position_per_stock = 0.25`
- `mobile_cash_ratio = 0.25`
- `max_stocks = 4`
- `stop_loss_pct = 0.08`

### 模块 F：做 T

新增：

- `TTradingStrategy`

V1 先实现基础规则：

- 牛市：正 T
- 熊市：反 T
- 震荡：机动仓高抛低吸

### 模块 G：通知

复用：

- 企业微信
- 邮件

通知必须包含：

- 市场状态
- 股票
- 信号类型
- 建议动作
- 仓位建议
- 风险提示

### 模块 H：回测

新增：

- `BacktestEngine`
- `TrendFollowingStrategy`

V1 只要求完成：

- 趋势策略回测
- 参数可配置
- 结果落盘

---

## 5. V1 交付清单

## 5.1 功能交付

- 自动选股
- 趋势策略实时信号
- 基础仓位管理
- 做 T 基础规则
- 通知推送
- 趋势策略回测

## 5.2 文档交付

- 配置说明
- 运行说明
- 回测说明
- 参数说明

## 5.3 测试交付

- 指标单元测试
- 检测器单元测试
- 仓位管理单元测试
- 回测引擎单元测试
- 从选股到通知的集成测试

---

## 6. V2：补齐多市场能力

## 6.1 V2 目标

让系统不只适用于上涨行情，还能覆盖下跌和震荡。

## 6.2 V2 新增范围

### 模块 I：震荡策略

新增：

- `MeanReversionStrategy`

要求：

- 适配 `range`
- 强调高抛低吸
- 适配机动仓频繁调节

### 模块 J：下跌策略

新增：

- `DefensiveStrategy`

要求：

- 强调反 T
- 限制抄底
- 默认降低总仓位

### 模块 K：突破策略

新增：

- `BreakoutStrategy`

要求：

- 适配震荡末期 / 趋势启动
- 对成交量放大敏感

### 模块 L：策略路由

新增：

- `StrategyRouter`

规则：

- `bull` → 趋势优先
- `bear` → 防守优先
- `range` → 均值回归优先

### 模块 M：回测增强

回测新增以下输出：

- 按市场状态拆分结果
- 交易次数
- 平均持仓周期
- 资金周转率
- 不同策略横向比较

---

## 7. V1 / V2 实施顺序

建议按以下顺序开发：

1. 配置与目录基线
2. `MarketRegimeEngine`
3. `Position` / `PositionManager`
4. `RiskManager`
5. 背离检测 / 空头陷阱
6. `StockSelector`
7. 趋势策略信号
8. `TTradingStrategy`
9. 通知整合
10. `BacktestEngine`
11. 趋势策略回测
12. 震荡策略
13. 下跌策略
14. 突破策略
15. `StrategyRouter`
16. 集成验证

---

## 8. 核心配置建议

建议统一成以下配置分组：

```yaml
data:
selector:
regime:
strategy:
position:
risk:
t_trading:
backtest:
notify:
manual_overrides:
```

重点新增：

```yaml
regime:
  mode: auto
  bull_score_threshold: 0.7
  bear_score_threshold: 0.7
  range_score_threshold: 0.6

manual_overrides:
  regime_override: auto
  disable_new_positions: false
  max_total_exposure: 1.0

position:
  min_stocks: 2
  target_stocks: 3
  max_stocks: 4
  base_position_per_stock: 0.25
  mobile_cash_ratio: 0.25
  max_position_per_stock: 0.40

risk:
  stop_loss_pct: 0.08
  max_portfolio_drawdown_pct: 0.20
  max_single_day_drawdown_pct: 0.02
```

---

## 9. 运行节奏设计

## 9.1 盘后任务

- 更新日线与基本面数据
- 运行市场状态判断
- 运行选股
- 生成候选池
- 推送次日重点观察列表

## 9.2 盘中任务

- 扫描候选池分钟数据
- 生成买卖/加减仓/做 T 信号
- 推送高优先级消息

## 9.3 周期任务

- 每周回测复核
- 每月策略复盘
- 每月参数再评估

---

## 10. V1 / V2 验收标准

## 10.1 V1 验收

- 能自动完成盘后选股
- 能自动判断市场状态
- 能对候选股推送趋势信号
- 能给出基本仓/机动仓建议
- 能完成趋势策略回测

## 10.2 V2 验收

- 能按 `bull / bear / range` 切换策略
- 能覆盖上涨、下跌、震荡三种市场
- 能输出做 T 相关信号
- 能输出资金周转率与市场状态分组报表

---

## 11. 当前阶段不纳入实施的内容

以下内容统一延后，不进入 V1 / V2：

- 券商接口
- 自动交易
- Web 前后端
- 多用户权限
- 复杂数据库持久化

---

## 12. 文档之间的关系

- 本文档负责 **V1 / V2 的功能实施**
- `2026-03-07-implementation-plan-aligned.md` 负责 **总方案基线**
- `2026-03-07-trading-system-implementation-part2.md` 负责 **V3 和增强项**
