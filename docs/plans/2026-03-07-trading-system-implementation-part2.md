# 量化交易系统实施计划（V3 / 评估 / 二期预留）

**文档定位**：承接 V1 / V2，定义增强方向、评估框架与第二阶段接口预留  
**本阶段核心目标**：让系统从“能用”走向“更稳、更可评估、更易扩展”

---

## 1. V3 总目标

V3 不再以“多写几个模块”为目标，而是聚焦三件事：

1. **把策略表现评估清楚**
2. **把多策略协同起来**
3. **把第二阶段可视化/管理系统的接口留稳**

---

## 2. V3 范围

## 2.1 多策略组合

新增能力：

- 多策略并行评分
- 按市场状态分配主策略 / 辅策略
- 信号冲突时的优先级裁决

建议的策略组合规则：

- `bull`
  - 主：趋势跟踪
  - 辅：突破
- `bear`
  - 主：防守 / 反 T
  - 辅：超跌反弹观察
- `range`
  - 主：均值回归
  - 辅：震荡做 T

## 2.2 参数优化

参数优化必须遵守以下边界：

- 只优化有限核心参数
- 不追求极端最优
- 必须做样本外验证
- 避免过拟合

优先优化的参数：

- 趋势判定阈值
- 背离确认窗口
- 止损 / 止盈比例
- 做 T 触发阈值
- 仓位调整步长

## 2.3 评估报表

新增统一报表输出：

- 总体绩效
- 分市场状态绩效
- 分策略绩效
- 分持仓周期绩效
- 分信号类型绩效

---

## 3. 评估体系

## 3.1 核心指标

本项目固定使用以下指标评估：

- 胜率
- 盈亏比
- 最大回撤
- 年化收益率
- Sharpe / Calmar（如可用）
- 交易次数
- 平均持仓周期
- 资金周转率
- 信号命中率

## 3.2 必须增加的拆分维度

仅看整体结果不够，V3 必须拆分：

- `bull / bear / range`
- 大盘强势 / 弱势
- 大盘放量 / 缩量
- 短周期与长周期信号
- 基本仓信号与机动仓信号

## 3.3 资金周转率定义

为避免文档中“提高资金周转率”停留在口号层面，统一定义为：

```text
资金周转率 = 一段时间内累计成交金额 / 平均账户净值
```

同时观察：

- 周转率是否提高
- 周转率提高后盈亏比是否下降
- 周转率提高后回撤是否放大

也就是说，**周转率只能和胜率、盈亏比、回撤一起看**。

---

## 4. A 股现实约束增强

V1 / V2 的回测先跑通，V3 必须进一步贴近 A 股实际：

- 手续费
- 印花税
- 滑点
- 100 股整手
- T+1
- 涨跌停导致的成交限制

建议分两步处理：

1. **近似模型**
   - 先用于策略比较
2. **保守模型**
   - 用于上线前压力测试

---

## 5. 主观择时机制固化

V3 必须把“允许主观择时”变成正式能力，而不是临时改代码。

## 5.1 需要固化的控制项

- `regime_override`
- `disable_new_positions`
- `only_reduce_positions`
- `max_total_exposure`
- `watchlist_only`

## 5.2 使用原则

- 默认程序自动运行
- 只有当你明确判断市场风格变化时，才使用人工覆盖
- 所有人工覆盖都记录原因、时间和影响范围

## 5.3 回测处理方式

对于人工覆盖，回测层至少支持两种模式：

- `auto_only`
- `manual_assisted`

这样可以分辨：

- 纯程序表现
- 程序 + 人工择时表现

---

## 6. 第二阶段接口预留

本项目当前不做 Java/Vue，但要保证未来能平滑接入。

## 6.1 需要稳定的数据对象

至少统一以下结构：

- `SignalRecord`
- `PositionSnapshot`
- `BacktestRun`
- `BacktestTrade`
- `StrategyConfigSnapshot`
- `RegimeDecisionRecord`

建议字段如下：

- `SignalRecord`
  - `signal_id`
  - `ts_code`
  - `stock_name`
  - `signal_time`
  - `regime`
  - `strategy_name`
  - `signal_type`
  - `action`
  - `suggested_position_change`
  - `stop_loss_price`
  - `stop_profit_price`
  - `reason`
  - `risk_flags`
- `PositionSnapshot`
  - `snapshot_time`
  - `ts_code`
  - `base_shares`
  - `base_cost`
  - `mobile_shares`
  - `mobile_cost`
  - `current_price`
  - `market_value`
  - `profit_loss`
  - `profit_rate`
- `BacktestRun`
  - `run_id`
  - `strategy_name`
  - `regime_scope`
  - `start_date`
  - `end_date`
  - `config_snapshot`
  - `metrics`
- `BacktestTrade`
  - `run_id`
  - `ts_code`
  - `entry_time`
  - `exit_time`
  - `side`
  - `entry_price`
  - `exit_price`
  - `shares`
  - `holding_days`
  - `pnl`
  - `pnl_ratio`
- `StrategyConfigSnapshot`
  - `strategy_name`
  - `version`
  - `params`
  - `created_at`
- `RegimeDecisionRecord`
  - `decision_time`
  - `auto_regime`
  - `manual_override`
  - `final_regime`
  - `score`
  - `reason`

## 6.2 推荐落盘格式

当前阶段建议优先使用：

- `YAML`：适合配置和信号
- `JSON`：适合报表和结构化结果
- `CSV`：适合交易流水和分析导出

## 6.3 与二期系统的接口策略

当前确定的接口优先级：

1. **文件共享**
2. **REST API**
3. **消息队列**

原因：

- 当前项目以个人使用为主
- 文件共享最轻
- 未来如果做 Web，再升级到 API

## 6.4 回测报告输出样例

建议标准回测报告至少输出以下结构：

```json
{
  "run_id": "bt_20260307_001",
  "strategy_name": "trend_following",
  "regime_scope": "bull",
  "period": {
    "start": "2024-01-01",
    "end": "2026-01-01"
  },
  "metrics": {
    "win_rate": 0.58,
    "profit_loss_ratio": 2.15,
    "max_drawdown": 0.17,
    "annual_return": 0.19,
    "turnover_rate": 3.4,
    "trade_count": 42,
    "avg_holding_days": 11.6
  },
  "cost_model": {
    "commission_pct": 0.0003,
    "stamp_tax_pct": 0.001,
    "slippage_pct": 0.0005,
    "lot_size": 100,
    "t_plus_one": true
  },
  "regime_breakdown": {
    "bull": {"win_rate": 0.63, "trade_count": 24},
    "range": {"win_rate": 0.55, "trade_count": 12},
    "bear": {"win_rate": 0.33, "trade_count": 6}
  }
}
```

该样例的目标是统一报表结构，不是固定具体字段值。

---

## 7. V3 交付清单

## 7.1 功能交付

- 多策略路由与组合
- 参数扫描
- 样本内 / 样本外验证
- 分市场状态报表
- 人工择时覆盖体系
- A 股现实约束增强

## 7.2 文档交付

- 策略评估说明
- 参数优化说明
- 人工覆盖使用说明
- 二期接口说明

## 7.3 数据交付

- 标准化信号记录
- 标准化持仓快照
- 标准化回测结果

---

## 8. 风险与控制

## 8.1 主要风险

- 参数过拟合
- 回测结果与实盘脱节
- 市场状态识别滞后
- 做 T 增加交易频率后反而降低盈亏比
- 人工覆盖过多导致系统失去一致性

## 8.2 控制原则

- 参数数量控制在少量核心变量
- 所有增强都以样本外结果为准
- 回测报告必须输出回撤与周转率
- 人工覆盖只允许少数几个入口

---

## 9. 里程碑定义

## M1：可评估

- 能输出完整核心指标
- 能拆分市场状态
- 能回看每笔交易原因

## M2：可优化

- 能做有限参数扫描
- 能比较不同策略在不同市场的表现

## M3：可扩展

- 能稳定输出标准化文件
- 能平滑接入未来的可视化系统

---

## 10. 最终边界再次确认

V3 完成后，项目仍然保持以下边界不变：

- 不接券商
- 不做自动下单
- 不做机构级交易平台
- 不做高频

项目定位仍然是：

**面向个人交易者的低频/中低频、信号驱动、可回测、可人工择时的量化辅助系统。**

---

## 11. 文档关系

- `2026-03-07-implementation-plan-aligned.md`
  - 最终方案基线
- `2026-03-07-trading-system-implementation.md`
  - V1 / V2 实施
- `2026-03-07-trading-system-implementation-part2.md`
  - V3、评估、二期接口预留

至此，`docs/` 目录方案统一为同一条主线，不再包含“接券商/自动执行”的歧义。
