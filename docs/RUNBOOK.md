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
- `data/positions.yaml`，如果你要让系统检查已有持仓

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

## 6. 常见问题

如果日常扫描启动失败，先检查：
- `config/config.yaml` 是否有有效的 Tushare Token
- 当前 Python 环境是否安装了 `PyYAML`
- 通知配置是否关闭或可用

如果基线回测失败，先检查：
- 是否在仓库根目录执行
- 当前环境是否安装了 `pandas`
- 基线输入文件 `sample_price_data.csv` 是否存在

## 7. 数据源网络自检

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

## 8. 真实数据日扫验收

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
