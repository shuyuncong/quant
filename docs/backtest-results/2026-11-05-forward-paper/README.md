# ETF forward paper ledger (500k)

- 启动：2026-11-05
- 初始资金：500,000 CNY
- 状态目录：`quant-python/signal_system/state/forward_paper_500k/`
- 规则：canonical 40% 核心 + 卫星 ≤35% + 回填 35%，10% 波动目标，周度最后交易日信号、下一交易日开盘成交
- 约束：**只做纸面账本，不接券商，不下单，不改参数**

## 当前状态（2026-11-09 更新）

已初始化并按 2026-11-06 收盘信号生成首个计划；计划等待下一交易日开盘成交：

| 项 | 值 |
|---|---:|
| `as_of` | 2026-11-06 |
| 现金 | 500,000 |
| 权益 | 500,000 |
| 持仓 | 空 |
| 待成交计划 | `511260` 61% + `518880` 14% |
| NAV 点数 | 2 |
| `fill_error` | `没有待成交的纸面计划`（首次 cycle 尚无旧计划） |

首次 cycle 的 `sync` 已把 47/47 只 ETF 更新到 2026-11-06（腾讯源，17s），`schedule.is_week_end == true`，计划状态为 `planned`。下一次运行同一 cycle 会在行情包含 2026-11-09 开盘价后成交；在此之前不要手工改价或强行成交。

## 运行方式

```powershell
# 初始化（只做一次；不会生成计划）
.\scripts\run_forward_paper.ps1 -Init

# 每日/每周收盘后推进：先同步行情，再成交/计划/盯市
.\scripts\run_forward_paper.ps1 -Sync

# 周五建议加 -Calendar：用交易所日历判断本周最后交易日
.\scripts\run_forward_paper.ps1 -Sync -Calendar

# 只查看，不改变状态
python quant-python/signal_system/forward_paper.py --action status
```

如果 `-Sync` 因网络/代理失败，先单独修数据同步，不要用旧行情或手工价格继续；`cycle` 会明确报错。

建议用 Windows Task Scheduler 在每个交易日收盘后（例如 16:30）运行一次 `run_forward_paper.ps1 -Sync`；脚本本身幂等，重复运行不会重复生成计划。

## 首次 cycle 检查（2026-11-09 补跑）

本次 JSON 结果：

- `ledger.as_of == "2026-11-06"`：行情确实更新到周五；
- `schedule.is_week_end == true`：本周最后交易日判断正确；
- `pending_plan.status == "planned"`：计划已生成，但尚未成交；
- `fill_error == "没有待成交的纸面计划"`：首次 cycle 没有旧计划可成交，符合预期。

如果 `as_of` 还是 11-05，说明行情未更新：**修同步，不要手工改价或强行 plan**。

下一交易日（2026-11-09）再运行同一命令，检查：

- `fill_error == null`；
- `last_plan.status == "filled"`；
- `orders` 有成交记录，且 `fill_date` 是 11-09；
- 现金 + 持仓市值 = 权益，整手和最低佣金没有异常。

## 每周节奏

1. **周五收盘后**：同步到最新交易日；若 `week_end_status` 判定为本周最后交易日，生成下周计划。
2. **下一交易日开盘**：`fill_paper` 按开盘价成交；卖出先于买入，整手 100，佣金 3bp（最低 5 元），滑点 5bp。
3. **每个交易日**：`mark_paper_to_market` 更新净值；不因盘中波动改计划。
4. **周末**：记录 `tracking_error_pct`、成交滑点、现金/整手、未成交原因、信号日/成交日。

## 预注册的复盘指标

6–12 个月后统一看，不中途按结果调参：

- 纸面账本 vs canonical 回测的跟踪误差；
- 实际成交价 vs 信号日收盘/次日开盘的偏差；
- 现金、整手、最低佣金造成的拖累；
- 未成交/跳过订单的原因；
- 与 `execution_stress` 的 5/10/20bp 价差情景对比。

建议的“可接受”门槛（研究用，不是生产 SLA）：

- 跟踪误差 ≤ 2%/年；
- 无法解释的现金/整手差异 = 0；
- 成交滑点落在已测压力区间内；
- 没有“无记录”的订单。

## 边界

- 纸面账本不是实盘 track record，不证明未来收益。
- V5 的折溢价/盘口/部分成交仍没有真实数据；纸面账本只记录实际可观察的成交假设。
- V4 的 PIT 退市池仍未关闭；当前池是当前上市 ETF，幸存者偏差未消除。
- 生产动作：none。
