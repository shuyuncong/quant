# 实施计划

1. 定义含 `amount` 的 `Bar`、`SignalEvent`、`AnalysisReport` 数据合同及配置 schema，统一路径、时区、复权、`is_closed` 和错误状态。
2. 重构行情层：增加 AkShare/Tushare 自动选择、可续跑全市场历史回填、批量日线增量、复权能力校验、节流/退避和按交易会话对齐的周期重采样，同时保留旧日线接口兼容性。
3. 重写 MACD 为 pandas 实现，增加归一化 0 轴金叉/死叉、MA60、量比和趋势过滤。
4. 实现确定性的包含处理、确认分型、笔、中枢和一二三类买卖点，并返回 `structure_time/confirmed_at/evidence`。
5. 实现多周期分析器和固定评分合同，处理周期缺失、冲突信号与新鲜窗口。
6. 增加 SQLite 表 `signal_event/outbox_delivery/candidate/run_state`，用唯一键、事务和每通道状态实现至少一次投递。
7. 扩展通知模块，增加 `quant.signal.v1` 通用 Webhook、认证头、幂等键和有限重试。
8. 实现监控服务与 CLI：`analyze`、`scan`、`monitor`、`test-notify`；接入交易日历、补跑和单轮预算。
9. 更新配置、依赖、README、快速启动与风险说明。
10. 运行 `unittest`、`compileall`、离线 CLI smoke test、逐根回放一致性测试和代码审查。

## 兼容与回滚

- 原 `DataFetcher` 和旧 `StrategyEngine` 文件保留，新的 CLI 默认走新监控链路；必要时可继续导入旧类。
- SQLite 和缓存均是可删除的运行产物，不修改用户持仓数据。
- 网络提供方为延迟导入，未安装 AkShare 时核心算法测试仍能运行。
