# A股缠论多周期信号监控

这个程序自动获取 A 股行情，分析分时、1/5/15/30/60/120 分钟和日线，识别缠论中枢与一二三类买卖点，筛选 MACD 0 轴附近金叉/死叉，并通过企业微信、邮件或通用 HTTP Webhook 推送新信号。

> 信号用于研究和提醒，不自动下单，也不构成投资建议。“0轴金叉 + 缠论共振”只是筛选排序规则；没有经过包含手续费、滑点和样本外数据的回测，不能声称已经提高真实胜率。

## 核心能力

- 默认直接读取腾讯日线/分钟线，并用新浪/东方财富获取全市场列表与收盘快照，无额外行情 SDK；另保留 AkShare、东方财富 K 线和 Tushare 适配。
- 1m、5m、15m、30m、60m、120m、1d 多周期分析。
- 当日 1 分钟分时摘要：涨跌幅、成交量、成交额和 VWAP；没有成交额时明确标为典型价格近似值。
- 工程化缠论：包含处理、严格分型、笔、中枢、一/二/三类买卖点、MACD 面积背驰。
- MACD 0 轴附近金叉/死叉，阈值按收盘价归一化，便于不同价格股票比较。
- 大周期趋势、MA60、量比和缠论共振的可解释买卖双评分。
- SQLite 事件去重与事务 outbox；每个通知通道独立记录状态，worker 每次只领取当前要发送的一条，失败指数退避。
- 交易日、午休和收盘调度；120 分钟线不会跨越午休。

## 安装

建议 Python 3.11 或更高版本：

```powershell
cd D:\development\github\quant\quant-python\signal_system
python -m pip install -r requirements.txt
```

核心算法不再依赖 TA-Lib 和 SciPy。默认 `provider: auto` 使用腾讯公开 K 线接口，股票列表/快照在东方财富不可用时自动降级新浪财经。如果要改用 AkShare 或 Tushare，再安装对应可选依赖：

```powershell
python -m pip install -r requirements-akshare.txt
python -m pip install -r requirements-tushare.txt
```

## 配置

编辑 [config/config.yaml](config/config.yaml)。先把自选股改成你的股票：

```yaml
monitor:
  watchlist:
    - "000001.SZ"
    - "600036.SH"
```

密钥建议使用环境变量，不要写进 Git：

```powershell
$env:TUSHARE_TOKEN = "你的Token"
$env:WECHAT_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
$env:SIGNAL_WEBHOOK_URL = "https://your-service.example.com/stock-signal"
$env:SIGNAL_WEBHOOK_AUTH = "Bearer your-token"
```

启用对应通道：

```yaml
notification:
  wechat:
    enabled: true
  webhook:
    enabled: true
```

## 使用

### 分析指定股票

```powershell
python main.py analyze --symbols 000001.SZ 600036.SH --no-notify
```

去掉 `--no-notify` 后，新鲜且达到阈值的信号会进入 outbox 并推送。结果同时保存到 `output/analysis_*.json`。

### 扫描 0 轴附近金叉

默认只扫描自选股：

```powershell
python main.py scan --no-notify
```

全市场模式：

```yaml
scan:
  universe_mode: "all_a"
```

```powershell
python main.py scan
```

首次全市场运行要逐只回填日线历史，默认每轮最多 500 只，并在结果中返回 `coverage`。程序持久化成功股票集合，但每轮仍会重新验证本地历史是否存在、至少 120 根且更新到最近应有交易日；股票列表提供上市日期时会提前排除上市不足 `market_data.min_listing_trade_days` 的股票，降级数据源缺少上市日期时则根据实际历史长度暂缓并在后续交易日复查。只有全部符合条件的活跃股票成功后才标记回填完成；历史保存在 `cache/daily_history/`。之后使用全市场收盘快照做日线增量，零价或非法 OHLC（包括新浪常见的零值停牌表示）不会写成新鲜日线。免费数据源不适合在一分钟内对数千只股票抓七个周期，因此全市场只做日线低频筛选，分钟级监控只处理自选股与候选池。

### 常驻监控

```powershell
python main.py monitor
```

程序只在 A 股交易日的 `09:30-11:30`、`13:00-15:00` 执行分钟监控，并在配置的 `daily_scan_time` 执行或补跑日线扫描。先做一次 smoke test：

```powershell
python main.py monitor --once --no-notify
```

Windows 任务计划程序可把“启动程序”设为 `powershell.exe`，参数设为：

```text
-ExecutionPolicy Bypass -File D:\development\github\quant\quant-python\signal_system\scripts\run_monitor.ps1
```

常驻任务意外退出后，建议让任务计划程序自动重启。

### 测试通知

```powershell
python main.py test-notify
```

## 通用 Webhook 合同

请求使用 `POST application/json`，并发送 `Idempotency-Key: <event_id>`。schema 当前为 `quant.signal.v1`：

```json
{
  "schema": "quant.signal.v1",
  "event_id": "e2c4...",
  "symbol": "000001",
  "name": "平安银行",
  "timeframe": "30m",
  "signal_type": "buy_3+zero_axis_golden_cross",
  "side": "buy",
  "price": 10.52,
  "structure_time": "2026-08-14T14:30:00",
  "confirmed_at": "2026-08-14T15:00:00",
  "score": 80,
  "evidence": {},
  "risk_notice": "量化信号仅供研究，不构成投资建议；请独立判断并控制风险。"
}
```

投递语义是“至少一次”。如果接收端按 `event_id` 或 `Idempotency-Key` 去重，可避免网络超时重试造成重复处理。

## 缠论口径

缠论不同流派的包含、成笔和中枢画法并不完全一致。本项目使用一套可回放、可测试的工程规则：

1. 只处理已收盘 K 线。
2. 包含方向由最近非包含 K 线高低点同向移动确定。
3. 顶底分型采用严格比较，分型在右侧 K 线完成后初步确认。
4. 笔连接交替分型，默认端点至少相隔 4 根处理后 K 线；最后一笔保持 provisional，只有下一笔被接受后前一笔才锁定并可发信号，防止同类新极值重绘。
5. 三笔共同重叠形成中枢，核心 `ZD/ZG` 在中枢扩展时保持不变。
6. 一类点使用同向笔创新高/低且 MACD 柱面积衰减；二类点检查一类点后的回试；三类点检查离开中枢后的不回中枢回抽。

完整定义见 [总体设计](../../docs/ai-dev-workflow/chan-signal-monitor/overview-design.md)。算法输出同时带 `structure_time` 和 `confirmed_at`，推送与去重以确认时间为准。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
```

测试覆盖 MACD 0 轴交叉、午休对齐的 120 分钟聚合、包含/分型、一二三类买卖点、防重绘回放、买卖双评分、SQLite 去重、分通道投递和交易时段判断。

## 运行数据

- `cache/`：行情缓存和日线历史。
- `data/signal_monitor.db`：事件、outbox、候选池和任务状态。
- `output/`：每次分析/扫描的 JSON 报告。
- `logs/signal_monitor.log`：运行日志。

这些目录已加入 `.gitignore`。删除缓存可强制重拉行情；删除数据库会丢失去重、候选池和调度状态，可能导致旧信号再次被视为新事件。
