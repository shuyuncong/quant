# 量化交易系统最终方案（对齐版）

**状态**：已定稿  
**适用范围**：当前仓库 `docs/` 方案基线  
**目标读者**：有股票交易经验、希望把已有交易模式程序化执行的个人开发者

---

## 1. 方案结论

本项目采用 **“自写策略内核 + 复用通用基础设施” 的混合架构**：

- **自己实现**：市场状态判断、选股规则、交易信号、仓位管理、风险控制、策略路由
- **复用现成能力**：行情/基本面数据获取、回测框架、任务调度、消息通知
- **明确不做**：券商接入、自动下单、订单回报处理、实盘 OMS

这是当前最适合本项目的路线，因为它同时满足：

- 保留主观择时空间
- 能把已有经验规则固化成程序
- 不需要实时盯盘
- 可以先回测，再做信号推送
- 不被第三方平台或券商 API 强绑定

---

## 2. 用户需求摘要

结合 `doc/量化交易/quantTrading.md` 与当前讨论，系统需要满足以下需求：

1. 将已有交易模式程序化，而不是从零发明新因子体系
2. 覆盖 **选股、仓位、交易、资金管理、回测**
3. 输出 **操作信号**，不要求自动执行
4. 支持 **上涨 / 下跌 / 震荡** 多策略
5. 允许 **一定主观择时**
6. 重点优化 **胜率、盈亏比、资金周转率**
7. 先做可落地的个人使用系统，而不是做机构级交易平台

---

## 3. 与 `doc/` 设计文档的对齐结论

## 3.1 与第一阶段原始设计的关系

与 `doc/设计文档/第一阶段-Python核心引擎-概要设计.md` 的关系如下：

- **保持兼容**：自动选股、分钟级信号、通知推送、无需盯盘
- **在修正基础上扩展**：仓位管理、做 T、回测能力
- **不破坏现有思路**：仍以 Python 核心引擎为中心

因此，本方案不是严格停留在“最初第一阶段范围”，而是采用：

**第一阶段信号系统 + 审查报告修正项 + 回测能力前置**

## 3.2 与审查报告/修正方案的关系

完全吸收以下修正结论：

- 仓位管理必须支持 **基本仓 + 机动仓**
- 必须增加 **正 T / 反 T / 震荡 T**
- 背离检测改为 **面积法**
- 增加 **空头陷阱识别**
- 配置参数按修正方案重新整理

## 3.3 与第二阶段设计的关系

本方案 **只为第二阶段预留接口，不直接实现 Java/Vue 系统**：

- 预留 Position / Signal / BacktestReport 的结构映射
- 预留文件共享或 REST API 的接口形态
- 当前阶段不启动 Java 后端与 Vue 前端开发

---

## 4. 最终系统边界

## 4.1 本阶段要做

- 盘后选股
- 市场状态判断
- 多策略信号生成
- 仓位与风险建议
- 做 T 信号建议
- 历史回测
- 参数配置
- 通知推送
- 信号与结果落盘

## 4.2 本阶段不做

- 券商 API 接入
- 自动下单
- 持仓自动同步到券商
- 订单簿、成交回报、撤单管理
- 高频交易
- 复杂 Web 可视化系统

---

## 5. 核心设计原则

1. **信号优先，不做自动执行**
2. **策略逻辑与回测逻辑共享**
3. **市场状态先行，策略后选**
4. **主观择时可控接入，而不是完全手工化**
5. **优先验证可用性，再追求功能完整**
6. **A 股场景优先，避免照搬海外量化框架默认假设**

---

## 6. 总体架构

```text
行情/基本面数据
    ↓
Data Layer（复用）
    ↓
Market Regime Engine（自写）
    ↓
Stock Selector（自写）
    ↓
Strategy Router（自写）
    ↓
Signal Engine（自写 + 集成现有 signal_system）
    ↓
Position / Risk / T Trading（自写）
    ↓
Notifier（复用）
    ↓
用户手动执行

同一套策略规则
    ↓
Backtest Engine（复用框架 + 自写封装）
    ↓
绩效评估 / 参数比较 / 报告输出
```

---

## 7. 模块划分

## 7.1 复用模块

- `quant-python/signal_system/data/`
  - 数据获取
  - 缓存
  - 数据清洗
- `quant-python/signal_system/notification/`
  - 企业微信 / 邮件通知
- 调度能力
  - Windows 任务计划 / APScheduler
- 回测底座
  - `Backtesting.py`

## 7.2 自写模块

- `core/regime/`
  - `MarketRegimeEngine`
  - 判断 `bull / bear / range`
  - 支持人工覆盖
- `core/selector/`
  - `StockSelector`
  - 固化“三条腿原则”
- `core/indicators/`
  - 背离、趋势、波动、量价结构
- `core/detectors/`
  - 空头陷阱、平台突破、异常放量等
- `core/position/`
  - `Position`
  - `PositionManager`
  - `TTradingStrategy`
- `core/risk/`
  - `RiskManager`
  - 止损、止盈、回撤、仓位约束
- `core/router/`
  - `StrategyRouter`
  - 不同市场状态下切换策略
- `backtest/`
  - `BacktestEngine`
  - 结果评估
  - 参数扫描
  - 报告落盘

---

## 8. 市场状态与主观择时设计

这是当前方案与原始文档相比最关键的补充。

## 8.1 市场状态定义

- `bull`：单边上涨、趋势强化
- `bear`：单边下跌、反弹脆弱
- `range`：横盘震荡、适合波段与做 T

## 8.2 判定来源

市场状态由两层共同决定：

1. **程序判定**
   - 指数均线方向
   - 涨跌家数
   - 成交额
   - 波动率
   - 趋势强度
2. **人工覆盖**
   - `auto`
   - `force_bull`
   - `force_bear`
   - `force_range`

## 8.3 为什么必须这样设计

因为你的策略并不是纯客观因子交易，明确允许主观择时；因此系统必须提供：

- 程序默认判断
- 人工可控修正
- 修正行为可回溯

---

## 9. 仓位与资金管理设计

本项目采用 **“默认刚性 + 配置可放宽”** 的设计：

## 9.1 默认模式

- 同时持有 `3` 只股票
- 每只股票 `25%` 基本仓
- `25%` 机动资金
- 允许临时超配到 `4` 只

这是对齐审查报告后的默认标准模式。

## 9.2 可配置放宽

为兼容 `quantTrading.md` 的更灵活思路，配置层允许：

- `min_stocks = 2`
- `target_stocks = 3`
- `max_stocks = 4`
- `max_position_per_stock = 0.40`

也就是说：

- **默认执行** 用 3 只 + 25% 机动
- **配置层** 允许过渡到 2~4 只、单票不超 40%

这样既兼容审查修正规范，也兼容你真实交易习惯。

---

## 10. 策略设计

## 10.1 策略路由

系统不是一条策略打天下，而是先判断市场，再路由策略：

- `bull` → 趋势策略 + 正 T
- `bear` → 防守策略 + 反 T
- `range` → 波段策略 + 震荡 T

## 10.2 首批实现策略

### V1 必做

- 趋势跟踪策略
- 正 T / 反 T 基础规则
- 空头陷阱检测
- 面积法背离检测

### V2 增强

- 震荡均值回归策略
- 平台突破策略
- 多策略评分与排序

### V3 优化

- 策略组合
- 参数分层优化
- 不同市场状态下的权重切换

---

## 11. 回测设计

## 11.1 回测定位

回测不是独立系统，而是与实时信号共享同一套策略规则。

## 11.2 回测必须纳入的指标

除了收益率，本项目固定追踪：

- 胜率
- 盈亏比
- 最大回撤
- 年化收益率
- 交易次数
- 平均持仓周期
- 资金周转率
- 不同市场状态下的表现拆分

## 11.3 A 股现实约束

回测封装层必须预留以下成本/约束：

- 手续费
- 印花税
- 滑点
- 最小交易单位（100 股）
- T+1 影响
- 涨跌停导致的无法成交场景（先作为近似模型）

---

## 12. 通知与执行设计

## 12.1 通知内容

系统不只推送“买/卖”，还要推送：

- 市场状态
- 触发策略
- 信号方向
- 建议仓位变化
- 止损位 / 止盈位
- 触发原因
- 风险提示

## 12.2 执行方式

本阶段统一采用：

**程序出信号 → 用户手动确认并执行**

这是最终确定的边界，不接券商。

---

## 13. 配置设计

配置文件至少包含以下部分：

- `data`
- `selector`
- `regime`
- `strategy`
- `position`
- `risk`
- `t_trading`
- `backtest`
- `notify`
- `manual_overrides`

其中 `manual_overrides` 至少支持：

- `regime_override`
- `disable_new_positions`
- `max_total_exposure`

## 13.1 `config.yaml` 最小完整示例

```yaml
data:
  provider: tushare
  fallback_provider: akshare
  cache_dir: data/cache
  daily_cache_hours: 24
  minute_cache_hours: 6
  fundamentals_cache_hours: 168

selector:
  roe_min: 0.10
  debt_ratio_max: 0.50
  pe_excellent_max: 17
  pe_acceptable_max: 30
  market_cap_min: 5000000000
  market_cap_max: 50000000000
  turnover_rate_min: 0.01
  turnover_rate_max: 0.03
  volume_ratio_min: 1.5

regime:
  mode: auto
  index_code: 000001.SH
  ma_short: 20
  ma_long: 250
  bull_score_threshold: 0.70
  bear_score_threshold: 0.70
  range_score_threshold: 0.60

strategy:
  enabled:
    trend_following: true
    mean_reversion: false
    breakout: false
  minute_frames: [5, 30, 60]
  candidate_pool_size: 30

position:
  min_stocks: 2
  target_stocks: 3
  max_stocks: 4
  base_position_per_stock: 0.25
  mobile_cash_ratio: 0.25
  max_position_per_stock: 0.40

risk:
  stop_loss_pct: 0.08
  stop_profit_pct: 0.30
  max_portfolio_drawdown_pct: 0.20
  max_single_day_drawdown_pct: 0.02
  allow_new_position_when_drawdown_exceeded: false

t_trading:
  enabled: true
  positive_t_step_pct: 0.05
  negative_t_step_pct: 0.05
  range_t_step_pct: 0.05

backtest:
  initial_cash: 100000
  commission_pct: 0.0003
  stamp_tax_pct: 0.001
  slippage_pct: 0.0005
  lot_size: 100
  t_plus_one: true
  price_limit_model: conservative

notify:
  channels: [wecom, email]
  push_market_regime: true
  push_candidate_pool: true
  push_trade_signal: true

manual_overrides:
  regime_override: auto
  disable_new_positions: false
  max_total_exposure: 1.0
```

## 13.2 配置优先级

统一采用以下优先级：

1. `manual_overrides`
2. 运行时参数
3. `config.yaml`
4. 系统默认值

这样可以保证“人工覆盖”不会被静态配置误伤。

---

## 14. 分阶段路线图

## V1：先跑通

- 盘后选股
- 市场状态判断
- 趋势策略信号
- 基本仓 / 机动仓
- 通知推送
- 基础回测

## V2：补齐核心能力

- 震荡策略
- 下跌策略
- 做 T 联动
- 风险控制完善
- A 股回测成本模型

## V3：做强

- 多策略组合
- 参数优化
- 资金周转率优化
- 分市场状态报表
- 为第二阶段可视化预留更稳定接口

---

## 15. 验收标准

当以下条件成立时，认为当前方案落地成功：

1. 能按日自动输出候选股票与市场状态
2. 能按分钟级别对候选股生成买卖/加减仓/做 T 信号
3. 用户无需实时盯盘，只需接收通知并手动执行
4. 回测可输出胜率、盈亏比、回撤、周转率等核心指标
5. 同一套策略逻辑可同时用于实时信号与回测
6. 不依赖券商接口也能完整使用

---

## 16. 最终决策清单

- 采用混合架构：**确定**
- 保留现有 `signal_system`：**确定**
- 引入 `Backtesting.py` 作为回测底座：**确定**
- 新增 `MarketRegimeEngine` 和人工覆盖层：**确定**
- 新增 `PositionManager` / `RiskManager` / `StrategyRouter`：**确定**
- 不接券商、不自动下单：**确定**
- 第二阶段 Java/Vue 只预留接口：**确定**

---

## 17. 本目录文档分工

- `2026-03-07-implementation-plan-aligned.md`
  - 最终方案总览
- `2026-03-07-trading-system-implementation.md`
  - V1/V2 实施计划
- `2026-03-07-trading-system-implementation-part2.md`
  - V3、评估体系、二期接口预留

该文档为当前 `docs/` 目录的主基线。

## 18. 文档清理说明

当前 `docs/plans/` 只保留上述三份正式方案文档。

以下内容不再作为正式方案来源：

- 临时草稿
- 重复的“最终版”计划文件
- 过程性问题记录
