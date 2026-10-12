# 运行手册

本文档只回答三件事：
- 怎么跑日常扫描
- 怎么跑固定输入基线回测
- 怎么看回测和报告输出
- 怎么开线上三策略回测（含独立 worker）

## 1. 环境准备

在仓库根目录执行：

```bash
cd quant-python/signal_system
pip install -r requirements.txt
```

如果只是跑固定输入基线回测，不依赖 Tushare，也不需要通知配置。

## 2. 日常扫描

工作目录：

```bash
cd quant-python/signal_system
```

只跑扫描，不发通知：

```bash
python main.py --no-notify
```

测试通知链路：

```bash
python main.py --test-notify
```

正常执行扫描并发通知：

```bash
python main.py
```

主要输入：
- `config/config.yaml`
- `state/positions.yaml`，如果你要让系统检查已有持仓

主要输出：
- 控制台摘要
- `logs/`
- `output/signals_*.yaml`

## 3. 固定输入基线回测

工作目录可以直接在仓库根目录。

执行：

```bash
python quant-python/backtest/baselines/generate_baseline.py
```

固定输入数据：
- `quant-python/backtest/baselines/sample_price_data.csv`

生成的基线产物：
- `quant-python/backtest/baselines/trend_following_baseline_result.json`
- `quant-python/backtest/baselines/trend_following_baseline_result_report.json`
- `quant-python/backtest/baselines/trend_following_baseline_result_report_trades.csv`
- `quant-python/backtest/baselines/trend_following_baseline_result_report_signals.json`
- `quant-python/backtest/baselines/trend_following_baseline_result_report_positions.json`
- `quant-python/backtest/baselines/trend_following_parameter_scan.json`
- `quant-python/backtest/baselines/baseline_manifest.json`

用途：
- 对比本次改动前后回测指标是否漂移
- 对比报告结构是否变化
- 对比参数扫描结果是否失稳

## 4. 如何看输出

先看：
- `trend_following_baseline_result.json`
  - 回测主结果
- `trend_following_baseline_result_report.json`
  - 标准化报告

重点字段：
- `summary` / `metrics`
  - `annual_return`
  - `max_drawdown`
  - `win_rate`
  - `profit_loss_ratio`
  - `turnover_rate`
  - `signal_hit_rate`
- `regime_breakdown`
  - 按市场状态拆分的表现
- `trades`
  - 标准化交易记录
- `signals`
  - 标准化信号记录
- `positions`
  - 标准化持仓快照

## 5. 回归建议

每次改这几类代码后，至少跑一次基线：
- `quant-python/signal_system/strategy/`
- `quant-python/core/`
- `quant-python/backtest/`

推荐最小回归命令：

```bash
python quant-python/backtest/baselines/generate_baseline.py
python quant-python/tests/backtest/test_bt_engine.py
python quant-python/tests/backtest/test_parameter_scan.py
python quant-python/tests/integration/test_daily_scan_flow.py
```

## 6. 配置化策略实验流程

知识库策略按“四层”落地：基本面、成交量、技术分析负责选股/入场；市场环境、仓位、做 T 和风险控制负责执行。实验时不要直接改生产配置，先复制一份研究配置：

```powershell
Copy-Item quant-python/signal_system/config/config.yaml `
  quant-python/signal_system/config/config.research.yaml
```

研究配置只修改一个层或一组相关参数，并保留：

- `strategy.framework.version` 与 `strategy.framework.profile`
- 各层 enabled 开关
- 数据窗口、复权、手续费、滑点、T+1 和涨跌停模型
- 基本面历史快照路径及缺失数据策略

命令行 `--fundamental-data` 的相对路径按当前工作目录解析；配置文件中的
`backtest.fundamental.data_path` 相对路径按配置文件所在目录解析。

使用独立配置运行回测：

```powershell
python quant-python/signal_system/backtest_winrate.py `
  --config quant-python/signal_system/config/config.research.yaml `
  --start 2025-01-01 --end 2025-12-31 `
  --mode both `
  --out bt_exec/research_p0.json
```

推荐验证顺序：

1. 用生产配置复现 P0 基线。
2. 只打开一个附加层，或只改变一组参数。
3. 同一窗口、同一股票池、同一成本和持仓限制下比较 signal 与 portfolio。
4. 训练窗口选择候选，验证窗口复测；再用多个滚动窗口检查稳定性。
5. 报告总体、bull/range/bear、信号类型、持仓周期、交易数、覆盖率和 P10/P50/P90。
6. 未达到验收门槛前不修改生产 `config.yaml`；关闭研究开关即可回滚。

## 7. 常见问题

如果日常扫描启动失败，先检查：
- `config/config.yaml` 是否有有效的 Tushare Token
- 当前 Python 环境是否安装了 `PyYAML`
- 通知配置是否关闭或可用

如果基线回测失败，先检查：
- 是否在仓库根目录执行
- 当前环境是否安装了 `pandas`
- 基线输入文件 `sample_price_data.csv` 是否存在

## 8. 数据源网络自检

如果你切到 `AKShare + pytdx` 方案，先跑一次网络自检：

```bash
python quant-python/signal_system/data/network_diagnostic.py
```

如需保存 JSON 结果：

```bash
python quant-python/signal_system/data/network_diagnostic.py --output quant-python/output/network_diagnostic.json
```

它会检查：
- 当前代理环境变量
- 东财 HTTP 链路是否可用
- `pytdx` 是否可导入
- 常见 TDX 行情主机 `7709` 端口是否可连

## 9. 真实数据日扫验收

如果你要验证“当前真实数据链路 + selector + router”是否还能跑出候选池和买点，执行：

```bash
python quant-python/signal_system/acceptance/run_daily_scan_acceptance.py
```

默认行为：
- 使用固定的 40 只真实样本股
- 复用当前正式配置
- 要求至少满足：
  - `candidate_pool_count >= 1`
  - `buy_signals_count >= 1`

可选参数：

```bash
python quant-python/signal_system/acceptance/run_daily_scan_acceptance.py --no-cache
python quant-python/signal_system/acceptance/run_daily_scan_acceptance.py --output quant-python/output/daily_scan_acceptance.json
```

如果脚本退出码为 `0`，表示这条真实验收链路通过；如果退出码非 `0`，优先检查：
- 数据源是否还能拿到真实行情
- 当前 selector 默认阈值是否被改得过严
- `StrategyEngine` 是否还保留了 `watchlist_only` 的约束入口
濡傛灉瑕佽窇鏇村ぇ鏍锋湰鎴栦竴娆℃€绘敹澶氫釜鏍锋湰缁勶紝鍙互鐢?`--group`锛?
```bash
python quant-python/signal_system/acceptance/run_daily_scan_acceptance.py --group expanded_60
python quant-python/signal_system/acceptance/run_daily_scan_acceptance.py --group quality_midcap_20
python quant-python/signal_system/acceptance/run_daily_scan_acceptance.py --group all
```

## 4. 线上三策略回测（quant-backtest worker）

页面 `/backtest` 的“个股回测 / 全市场回测”由独立的 `quant-backtest` 容器消费：

- **提交**：`POST /api/backtests` 只做校验并写入 `quant.jobs`（kind=backtest），立即返回 202；不拉行情、不等待回放。
- **开关**：`BACKTEST_ENABLED=1` 才允许新任务与重试；未开启时接口返回 503“线上回测未启用（BACKTEST_ENABLED=1）”，历史报告仍可查看。改环境后需重启 `quant-web` 与 `quant-backtest`。
- **执行**：worker 用 `pg_try_advisory_lock(920005)` 保证同一时刻只有一个消费者；一次一项按 id 升序执行，不依赖 web 的调度器或交易日历。中断（重启/信号）会保留 running，下一任 worker 自动恢复同一任务与冻结输入。
- **资源**：默认 `cpus: ${BACKTEST_CPUS:-1.0}`、`mem_limit: ${BACKTEST_MEMORY_LIMIT:-2g}`；Python 侧固定 `OMP/OPENBLAS/MKL_NUM_THREADS=1`。全市场任务内存不足时任务会失败但输入检查点在 `output/backtests/<uuid>/inputs` 保留，调大上限后重试即可（不得靠缩减股票范围“完成”任务）。
- **本地运行**（开发调试）：在 `quant-python/web` 执行 `npm run backtest:worker`，需要 `BACKTEST_ENABLED=1`；非 production 环境会先校验 `DATABASE_URL` 指向本机库，避免误连生产。
- **耗时**：串行节流抓取自建行情源时约 2~8 秒/只，全市场（当前在市约 5500 只）准备阶段需要数小时；进度写在 `output/backtests/<uuid>/progress.json` 的 `stage/processed/total/excluded`，`inputs/prepared.json` 每 25 只落盘一次，重启不会重复抓取已完成的股票。
- **资源实测**：全市场 5572 只（纳入 4852、58 个交易日）完整回放峰值 RSS 约 **0.7 GiB**，磁盘冻结输入约 162 MiB/任务。回放是单线程，增核不缩短单个任务；区间更长/持仓更多时内存上升，故线上建议 `BACKTEST_MEMORY_LIMIT=3g`、`BACKTEST_CPUS=4.0`（4 核机器留突发余量）。
- **可复现性**：同一冻结输入重跑（含中断恢复后）产出完全一致的样本、指标与成交明细（已用全市场任务实测：4852 只纳入、三策略 11/12/13 笔交易、五指标逐项相同）。
- **报告口径**：报告含 `universe`（总数=纳入+排除）、`effective_start/effective_end`（请求区间内的真实交易日）、每策略 `metrics/equity_curve/trades/rejected/warnings`，以及只含规则（不含密钥）的 `config_snapshot`。旧版本地回测任务会被标记失败并提示新建，不会被静默重跑。
- **鉴权**：回测是计算与行情抓取入口，公网部署必须置于 Nginx Basic Auth、来源 IP 限制等之后；页面本身没有登录。

### 按策略配置止损

- 在 `/settings/strategies` 设置全局默认及三策略独立值。止损、止盈 UI 均使用百分比（`6` = 6% 止损、`30` = 30% 止盈），API/配置仍为小数（`0.06`、`0.30`）；默认三个策略均继承现有全局值，无需 SQL 迁移或回填设置。
- 覆盖键为 `risk.strategy_stop_loss_pct.macd_zero_axis`、`risk.strategy_stop_loss_pct.yearline_pullback`、`risk.strategy_stop_loss_pct.macd_divergence`。通过 `PUT /api/config/strategies` 写入数值启用覆盖，写入 `null` 恢复继承并屏蔽 YAML 的独立值。合法比例为 `0.001`～`0.99`；非法更新返回 422，不应把清除操作改为写 0。
- 策略独立值优先于全局值，缺失/空覆盖继承全局，未配置全局时以 8% 兜底。实际持仓未明确归属策略时仍用全局；当前持仓页面没有归属编辑项，不自动改动已有持仓。详见 [三策略独立止损说明](../quant-python/signal_system/README.md#三策略独立止损2026-10-07)。
- 新任务冻结提交时的规则；已有任务不读取后来修改的值，已完成报告保持不变。策略版本 `2026-10-07.1` 会使旧信号检查点失效；恢复中的任务可能重算信号，但复用原冻结行情和参数，不需要重新抓取已有输入。

### 分批止盈（分批减仓）

- 在 `/settings/strategies` 的“分批止盈（分批减仓）”面板启用：填写首档涨幅、首档减仓比例、可选后续档位，以及“首档后尾仓止损上移到成本价”。默认关闭（`risk.exit_plan = null`），关闭时行为与旧的固定止盈一次性全清完全一致。
- 覆盖键为 `risk.exit_plan`（全局）与 `risk.strategy_exit_plan.<strategy>`（策略级，`null` 继承全局、`{}` 表示该策略单独关闭分批）。合法结构由 Python 端 `strategy/registry.resolve_exit_plan` 校验：档位涨幅严格递增、各档比例之和不超过 1、`protect_tail` 仅为 `none`/`breakeven`、移动止盈的两个参数必须成对提供。UI 只能编辑阶梯 + 保本止损；带 `trailing_*` 的计划需经 API/`config.yaml` 修改，表单会按只读处理避免误删字段。
- 计划生效后**接管止盈侧**：`risk.stop_profit_pct` 对该策略不再产生离场条件；止损、持仓超时、顶背离/跌破年线照旧，尾仓继续由这些规则管理。
- 回测执行口径：档位在日线最高价触及当日成交（成交价取档位价与开盘价较优者），同一交易日最多减一档，比例按整手截断、最后一档吸收零头；保本止损在首档成交后把剩余仓位的止损上移到成本价；移动止盈按收盘价激活、激活当根不清仓。同日优先级为 **止损 → 尾仓保护 → 下一档止盈**。详见 [分批止盈说明](../quant-python/signal_system/README.md#分批止盈2026-10-10) 与 [2024H1 归档](2026-10-10-三策略止盈分批减仓回测归档.md)。
- 2024H1 与 2024H2 两个半年样本的一致结论：日线零轴金叉与零轴＋底背离分批更优（"先卖一半 + 尾仓保本止损"最稳），年线趋势两个区间都不如一次性全清，"移动止盈单独使用"方向反复。**默认仍为关闭**，启用属于人工决策。
- **下跌区间核验（2026-10-12）：原定 2018/2022 无法执行，且第三个区间推翻了上一条的部分结论。**
  本地 `cache/daily_history`（5008 只）起点中位数 2023-05-18、最早 2022-12-26，**0 只覆盖 2018/2022**；
  三策略还需 270 根预热（第 270 根中位 2024-06-27），故 2023H2 及更早区间连一只可跑标的都没有。
  唯一可跑且下跌的区间是 **2024Q4**（4739 只、61 个交易日、上证 −3.95%，权重远小于真熊市）。
  用它复核（`--period oos --start 2024-10-01 --end 2024-12-31`）：
  - 年线趋势：基线 +2.37% 是唯一正收益，四个分批方案全负 → **第三个区间支持"年线不适配分批"**；
  - 零轴＋底背离：阶梯 +0.45%/−0.48% vs 基线 −6.04%，胜率 20%→33%~36% → 方向不变；
  - 日线零轴金叉：**"半仓+尾仓保本"变成五个方案里最差**（−21.65%，比基线 −20.18% 更低且回撤 20.2%→21.8%），
    与 H1/H2 的"最稳"结论**直接冲突**。
  三个区间结论互相矛盾（尤其移动止盈在三个区间分别是巨亏/最优/最优），
  **默认参数保持关闭，本轮不构成任何投产依据**；在补齐 2018 前后日线之前，
  不应再在同一数据范围内重复回测。详见 [2026-10-12 核验](2026-10-12-三策略止盈分批减仓熊市区间核验.md)。
- 若要按研究结论分开配置（「策略配置 → 按策略覆盖分批计划」，三选一）：
  - 日线零轴金叉 → 自定义：首档 +15% 卖 50%，勾选"首档后尾仓止损上移到成本价"；
  - 零轴＋底背离 → 自定义：+15% / +30% / +45% 各卖 1/3；
  - 年线趋势 → 关闭分批（写入空计划 `{}`），保持一次性全清。
  对应的覆盖键：`risk.strategy_exit_plan.macd_zero_axis` / `.macd_divergence` / `.yearline_pullback`；`null` 表示继承全局计划。
  > ⚠️ **该推荐已作废（2026-10-12 重分析）**：上面三条来自 2024H1/H2 两个区间，
  > 但下跌区间（2024Q4）的核验把「日线零轴金叉 → 半仓+保本止损」这条**机制性推翻**
  > —— 保本止损把尾仓止损抬到成本价，在下跌区间等于"在反弹起点被扫出"，
  > 该方案 Q4 是八个方案里最差，回撤也更差。详见
  > [2026-10-12 日线零轴金叉减仓方案重分析](2026-10-12-日线零轴金叉减仓方案重分析.md)。
  > 本文保留这三条仅为记录最初的研究结论，**不应照着配置**；在补齐 2018 前后数据并取得
  > 三区间一致结论前，三个策略都保持「未归属 / 关闭分批」的默认状态。
  > 三区间都不曾垫底的只有阶梯减仓（`ladder_a`/`ladder_b`），但改善幅度仅 0.7~1.2pp，
  > 不足以支撑改默认。
- **每日提醒与 AI 解读已带上分批口径**（2026-10-11）：`evaluate_strategies` 在实盘层按当日**日线最高价**判定下一档，触发时该条件 `met = true`、`trigger_basis = 日线最高价`，结果对象带 `exit_plan_advice`（下一档触发价、建议股数、原始仓位比例、已执行/剩余档位、保本止损位、移动止盈激活价与回撤位）。推送 evidence 字段为 `exit_plan_advice`，`/workflow` 报告表在"实际数值"列直接显示引擎算好的执行摘要。回测回放层（`evaluate_exits`）仍保持信息行（`met = null`），两者不可混用。
- **AI 解读只能照抄引擎数字**：`INTERPRET_SYSTEM_PROMPT` 与两阶段提示词的 `synthesis` 阶段都写明——建议减仓时必须引用 `exit_plan_advice.next` 的触发价与建议股数、说明占原始仓位比例、`breakeven_active` 时说明尾仓止损已上移到成本价；不得自行估算比例或股数，也不得建议卖得比该档更多；没有 advice 时不得编造档位。解读输入（`buildInterpretationContext`）已保留 `strategies` 字段，否则模型看不到这些数字。
- **保本止损与档位进度来自成交记录**：持仓台账目前没有 `exit_levels_taken` 字段，实盘进度由 `quant.holding_trades` 反推——累计卖出 ÷ 累计买入 = 已执行比例，对照计划档位比例得出已执行到第几档，原始股数取累计买入（`resolve_plan_progress`）。建议与止损共用这一口径，不会出现"建议说保本已生效、止损行还印 8%"的分叉；首档执行后止损来源变为 `plan_breakeven`。
- **进度是估算时必须说明**：成交记录每股只保留最近 50 笔（快照 `history_complete` 恒为假），所以文案统一写"按已知成交估算"；卖出多于已知买入（期初持仓没有买入记录）时判定为未知，退回"按当前仓位视为原始仓位"并按 0 档处理，不猜。
- **回测口径不受影响**：replay 传入合成持仓（无成交记录），档位进度由引擎内部维护，`--parity-grid` 30 组仍为零差异。
- **持仓归属策略（2026-10-12 已落地）**：`quant.holdings.strategy_id`（schema v5，`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`）在 `/assets?tab=holdings` 的编辑表单里可选「归属策略」，选项为三个策略 id 或「未归属」。升级需跑 `npm run db:setup`（把 `db/schema.sql` 整段幂等重放并把 `schema_meta` 升到 5；`src/lib/db.ts` 的 `SCHEMA_VERSION` 必须同步为 5，否则 `validateSchema` 会拒绝启动）。存量持仓列默认空串 = 未归属，**行为与升级前完全一致**，需要人工逐个指定归属。
  - 归属是**这一笔持仓**的属性：`strategy.registry.stop_owner` 只看 `holding.strategy_id`；指定后该持仓的止损走 `risk.strategy_stop_loss_pct.<id>`、止盈走 `risk.strategy_exit_plan.<id>`（含分批计划），且**所有**被评估策略看同一笔持仓时都用归属策略的口径，不会各自换回自己的。空串/缺失/未知归属一律回到全局。
  - 「未归属」是显式选择，不会根据当天命中的信号猜归属；未知 id 由 `PUT /api/holdings/<symbol>` 直接返回 422，不静默降级。
  - 归属只在**编辑既有持仓**时下发：新增持仓时表单不给该字段，避免一次点击就把止损口径从全局换成策略级。
  - 端到端回归：`tests/test_exit_notification.py::HoldingAttributionTests`（归属生效 / 未归属不借口径两条）。

### 界面指标与操作约定

- `/assets?tab=holdings` 的持仓账面金额为登记成本金额合计，不是实时市值；剩余额度按设定总资金减账面占用估算，不代表券商可用现金。首次加载前显示 `—`，不把未读取的数据当成零。
- `/backtest` 同时列出实际区间收益与年化折算值；未提供实际收益时保持“不可计算”，不拿年化值代替。最大回撤按非负跌幅幅度展示；收益负值只显示一个负号，舍入为零不显示负零。
- 对比卡片只使用有交易记录且对应指标有效的账户；纯现金未交易账户保留真实零值但不参与排名，仅持仓未平仓的账户不参加胜率排名。全亏损时仅标注“本样本亏损最少”，最低回撤不等同低波动或最佳风控。
- 策略配置页按两组独立保存、计算未保存项数和放弃修改。背驰判定比例 `90` / 零轴邻近容差 `0.5` 分别保存为 `0.9` / `0.005`；换手率仍使用原有百分数，避免二次除以 100。预设不自动保存。
- 任一组有草稿时会提示离开；浏览器后退/前进的同文档拦截依赖 Navigation API 和可取消事件。旧浏览器仅有链接导航及刷新/关闭提醒，不通过插入历史记录或拦截 `history` 制造虚假的返回行为。
- 手机导航复用模态 Dialog：打开后聚焦关闭按钮，Tab/Shift+Tab 留在导航内，Escape/遮罩关闭后返回触发按钮；切换为桌面宽度或选中页面时关闭导航，避免隐藏弹层仍锁住页面。
