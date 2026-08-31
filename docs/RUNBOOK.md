# 运行手册

本文档只回答三件事：
- 怎么跑日常扫描
- 怎么跑固定输入基线回测
- 怎么看回测和报告输出

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

完整需求、设计、测试和 UAT 用例见：

- `docs/ai-dev-workflow/strategy-framework/requirements.md`
- `docs/ai-dev-workflow/strategy-framework/overview-design.md`
- `docs/ai-dev-workflow/strategy-framework/test-plan.md`
- `docs/ai-dev-workflow/strategy-framework/uat-cases.md`

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
