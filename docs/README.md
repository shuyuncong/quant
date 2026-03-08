# Docs 导航

本目录保存当前量化交易系统的正式设计与实施文档。

---

## 文档结构

- 总方案基线：`docs/plans/2026-03-07-implementation-plan-aligned.md`
  - 定义系统目标、边界、架构、模块分工、配置设计和验收标准
- V1 / V2 实施计划：`docs/plans/2026-03-07-trading-system-implementation.md`
  - 定义最小可用版本到多市场覆盖版本的实施范围、顺序和验收标准
- V3 / 评估 / 二期预留：`docs/plans/2026-03-07-trading-system-implementation-part2.md`
  - 定义评估体系、参数优化、A 股现实约束增强和第二阶段接口预留
- 开发 TODO 清单：`docs/TODO.md`
  - 按开发顺序拆分的可执行任务列表

---

## 推荐阅读顺序

1. `docs/plans/2026-03-07-implementation-plan-aligned.md`
2. `docs/plans/2026-03-07-trading-system-implementation.md`
3. `docs/plans/2026-03-07-trading-system-implementation-part2.md`
4. `docs/TODO.md`

---

## 当前方案摘要

当前方案采用：

- 自写策略内核
  - 市场状态判断
  - 选股
  - 信号生成
  - 仓位管理
  - 风险控制
  - 策略路由
- 复用通用基础设施
  - 数据获取
  - 回测框架
  - 调度
  - 通知
- 明确不做
  - 券商接入
  - 自动下单
  - 高频交易
  - Web 可视化系统

---

## 当前开发优先级

- P0：跑通市场状态、选股、趋势信号、仓位与通知
- P1：补齐做 T、回测、风险控制
- P2：扩展震荡 / 下跌 / 突破策略
- P3：完善评估体系、参数优化和二期接口预留

---

## 使用方式

- 想了解“系统到底做什么”：看总方案基线
- 想开始开发：看 V1 / V2 实施计划
- 想做评估和增强：看 V3 文档
- 想直接开工：看 `docs/TODO.md`
