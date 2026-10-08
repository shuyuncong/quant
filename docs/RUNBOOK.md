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

### 界面指标与操作约定

- `/assets?tab=holdings` 的持仓账面金额为登记成本金额合计，不是实时市值；剩余额度按设定总资金减账面占用估算，不代表券商可用现金。首次加载前显示 `—`，不把未读取的数据当成零。
- `/backtest` 同时列出实际区间收益与年化折算值；未提供实际收益时保持“不可计算”，不拿年化值代替。最大回撤按非负跌幅幅度展示；收益负值只显示一个负号，舍入为零不显示负零。
- 对比卡片只使用有交易记录且对应指标有效的账户；纯现金未交易账户保留真实零值但不参与排名，仅持仓未平仓的账户不参加胜率排名。全亏损时仅标注“本样本亏损最少”，最低回撤不等同低波动或最佳风控。
- 策略配置页按两组独立保存、计算未保存项数和放弃修改。背驰判定比例 `90` / 零轴邻近容差 `0.5` 分别保存为 `0.9` / `0.005`；换手率仍使用原有百分数，避免二次除以 100。预设不自动保存。
- 任一组有草稿时会提示离开；浏览器后退/前进的同文档拦截依赖 Navigation API 和可取消事件。旧浏览器仅有链接导航及刷新/关闭提醒，不通过插入历史记录或拦截 `history` 制造虚假的返回行为。
- 手机导航复用模态 Dialog：打开后聚焦关闭按钮，Tab/Shift+Tab 留在导航内，Escape/遮罩关闭后返回触发按钮；切换为桌面宽度或选中页面时关闭导航，避免隐藏弹层仍锁住页面。
