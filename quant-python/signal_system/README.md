# A股缠论多周期信号监控

这个程序自动获取 A 股行情，分析分时、1/5/15/30/60/120 分钟和日线，识别缠论中枢与一二三类买卖点，把 MACD 金叉按 0 轴上方、附近、下方分级，并通过 Bark、企业微信、邮件或通用 HTTP Webhook 推送新信号。

> 信号用于研究和提醒，不自动下单，也不构成投资建议。“0轴金叉 + 缠论共振”只是筛选排序规则；项目已提供包含默认手续费、滑点和样本外参数扫描的真实行情验收，但当前固定样本和交易次数不足，不能声称已经提高真实胜率。

## 核心能力

- 默认直接读取腾讯日线/分钟线，并用新浪/东方财富获取全市场列表与收盘快照，无额外行情 SDK；另保留 AkShare、东方财富 K 线和 Tushare 适配。
- 行情源故障自动降级：K 线 腾讯→东方财富→新浪，列表/快照 东方财富→新浪，指数 东方财富→腾讯，交易日历 腾讯↔新浪；末级新浪 K 线走 AkShare 的新浪接口，单接口抖动不再中断整轮扫描。
- 1m、5m、15m、30m、60m、120m、1d 多周期分析。
- 当日 1 分钟分时摘要：涨跌幅、成交量、成交额和 VWAP；没有成交额时明确标为典型价格近似值。
- 工程化缠论：包含处理、严格分型、笔、中枢、一/二/三类买卖点、MACD 面积背驰。
- MACD 金叉按 0 轴上方、附近、下方分级，附近阈值按收盘价归一化，并结合温和放量、突破 MA5/MA10、红柱放大确认。
- 大周期趋势、MA60、量比和缠论共振的可解释买卖双评分。
- SQLite 事件去重与事务 outbox；每个通知通道独立记录状态，worker 每次只领取当前要发送的一条，失败指数退避。
- 交易日、午休和收盘调度；120 分钟线不会跨越午休。

## 安装

建议 Python 3.11 或更高版本：

```powershell
cd D:\development\github\quant\quant-python\signal_system
python -m pip install -r requirements.txt
```

核心算法不再依赖 TA-Lib 和 SciPy。默认 `provider: auto` 使用腾讯公开 K 线接口，主源故障时按 `market_data.fallback_providers`（缺省 东方财富→新浪）自动切换，显式空列表可关闭降级；股票列表/快照在东方财富不可用时自动降级新浪财经，K 线末级新浪源需要 `requirements-akshare.txt`。如果要改用 AkShare 或 Tushare，再安装对应可选依赖：

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

### 三策略独立止损（2026-10-07）

Web 入口：**系统配置 → 策略配置 → 三策略规则与退出参数**。止损、固定止盈均按百分比输入：填写 `6` 表示 6% 止损，填写 `30` 表示 30% 止盈；保存到配置/API 时仍为 `0.06`、`0.30`，不改变引擎参数口径。

- 保留 `risk.stop_loss_pct` 全局默认；三个策略可分别取消“继承全局止损”并填写独立值。
- 生效优先级为：策略独立值 → 全局值 → 代码兜底 8%。策略值缺失或为 `null` 均继承；默认三个策略全部继承，不会自动应用回测中的 10%／3%／6%。
- 页面显示“当前生效”及来源；未保存的更改显示“生效预览”。允许 0.1%～99%，不能用 0 或空白禁用止损。
- 重新勾选“继承”并保存，会通过现有 `PUT /api/config/strategies` 将对应的扁平配置键保存为 `null`，明确覆盖 YAML 中可能存在的独立值，而不是删除设置后让 YAML 值重新生效。
- “保存止损与退出规则”和“保存入场与股票池参数”分别只保存本组；各组显示未保存项数，“放弃修改”恢复该组最近一次成功读取/保存的值，不写入配置。把数字改回原值会清除对应的未保存状态。
- 入场参数中的背驰判定比例和零轴邻近容差也按百分比输入：`90` 对应内部 `0.9`，`0.5` 对应内部 `0.005`。换手率原本就以百分数存储，仍保持 `0.5` = 0.5%；量比倍数、评分和窗口根数不转换。
- 参数预设只填入草稿，点击本组保存后才生效。保存失败保留输入及未保存状态；加载配置前不能保存，保存过程中本组输入不可编辑。
- 页面有任一组草稿时，链接离开、刷新/关闭会提醒。支持 Navigation API 且允许取消遍历的浏览器还会拦截同文档后退/前进；不支持时不伪造历史记录，请先保存或放弃修改。

配置/API 内部仍使用比例小数，例如以下配置保持现有全局行为：

```yaml
risk:
  stop_loss_pct: 0.08
  strategy_stop_loss_pct:
    macd_zero_axis: null
    yearline_pullback: null
    macd_divergence: null
```

对明确归属的策略设置数值（例如 `yearline_pullback: 0.06`）才启用独立覆盖。单股和全市场三策略回测均使用同一个止损解析入口，各策略的模拟持仓有明确归属。成本止损之外的固定止盈、趋势退出和超时规则保持不变。

**实际持仓边界**：Web 持仓台账在 `/assets?tab=holdings` 的编辑表单里可选「归属策略」（`quant.holdings.strategy_id`，v5 迁移新增；空串 = 未归属）。只有持仓明确带受支持的 `strategy_id` 时才使用该归属策略的止损与分批计划；缺失、空串或未知归属回到全局。归属是**这一笔持仓**的属性：止损归属只看 `holding.strategy_id`，不会借用当天被评估策略的口径，也不会取三个止损中的最小值，更不会根据当天命中的信号猜归属。未持仓时，各策略报告展示各自的假设止损。存量持仓在迁移时一律置为空串（维持原全局口径），需要人工在表单里指定归属。

任务提交时冻结包含上述字段的 `risk` 快照；后来修改全局或独立值，不修改已有任务的规则和已完成报告。分析结果的 `stop_loss` 字段含 `pct`、`source`（`strategy`／`global`／`default`）及实际应用的覆盖策略 `strategy_id`。策略版本为 `2026-10-07.1`；恢复未完成回测时，旧版本信号缓存会失效并按原冻结规则重算，冻结行情继续复用，已完成报告不改写。

这是配置能力，不是参数优化或投产建议。独立阈值仍需在不重叠时期验证，不应把单一半年样本的收益最高档直接设为默认。

### 分批止盈（2026-10-10）

Web 入口：**系统配置 → 策略配置 → 分批止盈（分批减仓）**。默认关闭：不配置 `risk.exit_plan`
时，止盈仍然是 `risk.stop_profit_pct` 一次性全清，历史回测口径不变。

**按策略分开配置（双样本结论）**：`risk.strategy_exit_plan.<id>` 支持三种取值 —— `null` 继承全局计划、
`{}` 该策略单独关闭分批、对象为该策略自己的计划。两段半年（2024H1 选参数 / 2024H2 样本外）方向一致：

| 策略 | 止损（研究口径） | 减仓方式 | 两段收益（全清 → 分批） | 最大回撤 |
|---|---|---|---|---|
| 日线零轴金叉 | 10% | 半仓 @+15% + 尾仓止损上移到成本价 | +2.57% → +13.12% / −8.78% → +13.10% | 16.3%/21.4% → 11.6%/12.5% |
| 零轴＋底背离 | 6% | 阶梯 1/3 @+15% / 1/3 @+30% / 1/3 @+45% | −4.20% → +0.18% / +3.66% → +16.54% | 12.3%/12.7% → 9.7%/8.1% |
| 年线趋势 | 3% | **保持一次性全清**（8 种分批方案两段全输） | +2.37% / +14.79% | — |

止损档位沿用上一轮止损回测的较好档，属研究口径、不是已生效建议。

**下跌区间核验（2026-10-12）**：原定的 2018/2022 熊市区间**本地数据不支持**（`cache/daily_history`
5008 只起点中位 2023-05-18，0 只覆盖 2018/2022；且 270 根预热把可用起点推到 2024-06 中位），
改用唯一可跑的下跌区间 2024Q4（4739 只、61 日、指数 −3.95%）复核后，结论出现分歧：
年线趋势与零轴＋底背离方向不变（前者仍分批有害、后者仍偏阶梯），但**日线零轴金叉的
"半仓+尾仓保本"在该区间变成最差方案**（−21.65% vs 基线 −20.18%）。三个区间结论互相矛盾，
故**默认保持关闭、本轮不构成投产依据**；详见
[2026-10-12 核验](../../docs/2026-10-12-三策略止盈分批减仓熊市区间核验.md)。
Web 入口为「策略配置 → 按策略覆盖分批计划」（继承 / 关闭 / 自定义三选一）。

配置结构（`config.yaml` 或 `PUT /api/config/strategies`）：

```yaml
risk:
  exit_plan:                      # 全局计划；null = 关闭分批减仓
    first_gain: 0.15              # 首档涨幅 (相对成本价)
    initial_fraction: 0.5         # 首档卖出原始仓位的比例
    tiers:                        # 后续档位, gain 必须严格递增
      - {gain: 0.30, fraction: 0.5}
    protect_tail: breakeven       # none | breakeven (首档成交后尾仓止损上移到成本价)
    trailing_activation_gain: 0.15  # 可选: 移动止盈激活涨幅
    trailing_pct: 0.08              # 可选: 从最高收盘价回撤该比例清仓
  strategy_exit_plan:             # 策略级覆盖: null 继承全局, {} 该策略单独关闭
    macd_zero_axis: null
    yearline_pullback: null
    macd_divergence: null
```

- 解析入口为 `strategy/registry.resolve_exit_plan`（与 `resolve_stop_loss` 同层，分析和回测共用），
  非法结构直接 `raise`，不会静默降级成"没有止盈"或"停止止损"。
- **计划生效即接管止盈侧**：`risk.stop_profit_pct` 对该策略不再产生离场条件；止损、持仓超时、
  顶背离/跌破年线仍然生效，尾仓由这些规则继续管理。
- 回测/回放口径：档位在日线最高价触及当日成交，成交价取档位价与开盘价的较优者；同一交易日
  最多执行一档；每档按整手（北交所 1 股）截断，剩余不足一手一次性卖出，不留碎股；
  `protect_tail: breakeven` 在首档成交后把剩余仓位止损上移到成本价；移动止盈按**收盘价**
  激活、激活当根不清仓（避免用当根最高价做当根决策的前视）。
  **同日优先级为 止损 → 尾仓保护 → 下一档止盈**：日线无法还原盘中路径，引擎固定按"先取不利
  结果"处理（与既有"止损先于止盈"一致）；尾仓保护清仓后，未执行的档位自然作废。
- 分析报告分两层口径，避免"整仓卖出"和"分批减仓"互相污染：
  - **回测回放层**（`evaluate_exits`）：计划档位始终是信息行（`met` 恒为 `null`），
    由回放引擎自己维护档位进度，规则层不得声明触发——否则尾仓会被一次性卖光。
  - **实盘/每日提醒层**（`evaluate_strategies`）：按当日**日线最高价**判定下一档是否触发，
    触发时该行 `met = true` 且 `trigger_basis = 日线最高价`，并挂载 `exit_plan_advice`
    （下一档触发价、建议股数、原始仓位比例、已执行档位、保本止损位、移动止盈激活价与回撤位）。
    提醒文案与 AI 解读直接引用这些数字，不再自行估仓位。
  - 首档成交后（持仓 `exit_levels_taken > 0`）`exit_parameters` 把尾仓止损上移到成本价，
    止损来源标记为 `plan_breakeven`（`stop_loss_pct = 0`）。
  - **进度来源与缺口**：持仓台账还没有 `exit_levels_taken` 字段，所以实盘由**成交记录**反推
    （`resolve_plan_progress` → `plan_progress_from_trades`：累计卖出 ÷ 累计买入 = 已执行比例，
    对照档位比例得出已执行档位，原始股数 = 累计买入）。建议与止损两处共用同一口径，
    不会出现"建议说保本、止损行还印 8%"的分叉。成交记录不完整（快照 `history_complete` 为假）
    时文案标明"按已知成交估算"；卖出多于已知买入（期初持仓无买入记录）时返回未知，
    按 0 档处理并写明"按当前仓位视为原始仓位"。
  - 回测/回放不受影响：replay 传入的是合成持仓（只有价格/数量/建仓日，没有成交记录），
    档位进度由引擎内部 `levels_taken` 维护，规则层不会替它推进止损。
- **按策略计划需要持仓带归属**：`quant.holdings.strategy_id`（v5 迁移新增，Web 表单可选「归属策略」）
  现在会随持仓快照进到 `stop_owner`，`strategy_exit_plan.<id>` 的分策略计划在线上按归属生效；
  空串/缺失/未知归属一律按 **全局** `risk.exit_plan` / `risk.stop_loss_pct` 处理。
  持仓没有 `opened_on` 时由最早一笔买入成交补（`monitor._holding_with_trade_history`）。
  端到端回归见 `tests/test_exit_notification.py::HoldingAttributionTests`。
- 单变量对照实验：`scale_out_take_profit_study.py`（2024H1 冻结样本，7 方案 × 3 策略，
  含样本外 `--period oos` 通道与引擎回归 `--parity-grid`）。结果见
  [2026-10-10 归档](../docs/2026-10-10-三策略止盈分批减仓回测归档.md)：日线零轴金叉与
  零轴＋底背离在两个半年里都优于一次性全清（"先卖一半 + 尾仓保本止损"最稳），
  年线趋势两个区间都不如全清，移动止盈单独使用方向反复。
  **这是研究结论，不是默认参数授权。**
- 策略版本 `2026-10-11.1`（`2026-10-10.1` 之后新增 `exit_plan_advice` 与实盘层档位 `met`）；
  回测口径未变，计划缺省路径仍与旧引擎逐字段一致（30 组止损网格重跑核验通过）。

## 使用

### 分析指定股票

```powershell
python main.py analyze --symbols 000001.SZ 600036.SH --no-notify
```

去掉 `--no-notify` 后，新鲜且达到阈值的信号会进入 outbox 并推送。结果同时保存到 `output/analysis_*.json`。

### 扫描 MACD 金叉并按 0 轴位置分级

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

### 独立研究池：零轴金叉 + 底背离 + 放量 + 年线以上

`macd_divergence` 池与生产 MACD 池、年线池完全解耦：独立配置段、独立
`pool_type`、独立 TTL/容量、不进入监控与下单链路、不推送通知。四个条件**同时**
成立才入池（全部只读信号日收盘及以前数据）：

1. **零轴金叉**：`DIF > DEA` 且前一日 `DIF <= DEA`，且金叉不位于 0 轴下方。
2. **日线底背离**：最近两段*已完成*的负 MACD 柱区间中，后一段价格创新低且绝对面积收缩。
   未完成的当前区间不参与比较，避免部分区间偏置。
3. **放量**：当日量 ≥ 前 20 日均量 × `min_volume_ratio`（默认 1.5）。与生产池的
   “温和放量 [1.0, 2.0]” 不同：这里只设下限，不设上限。
4. **年线以上**：收盘 > MA250 且 MA250 上行（对比 20 个交易日前）。

信号收盘确认，入场参考为下一交易日开盘（仅记录字段，不下单）。数据口径为**前复权**：
背离与年线比较跨越一年以上，未复权价格会在除权日制造假新低。

```yaml
macd_divergence:
  enabled: true
  universe_mode: "watchlist"     # 可选；缺省继承 scan.universe_mode
  macd: { fast: 12, slow: 26, signal: 9 }
  zero_axis_tolerance: 0.005     # 0轴“附近”阈值（按收盘价归一化）
  volume_window: 20
  min_volume_ratio: 1.5          # 放量下限，不设上限
  long_ma_period: 250
  long_ma_slope_window: 20
  min_macd_segment_bars: 2       # 过滤单根柱噪声区间
```

四条件是「且」关系，命中率天然很低。扫描报告因此附带 `condition_funnel`
（各条件独立的通过数与比例）与 `volume_ratio_distribution`（量比中位数/分位数/
达阈值只数），网页端候选池的「零轴+底背离」页签会把它们显示成一行漏斗，用来解释
“为什么候选很少”。库内扫描用前复权口径，与生产实时口径互不影响。

回测（独立脚本，不改生产配置，复用 `backtest_winrate` 的成交/风控语义）：

```powershell
# 四条件 AND（默认放量 ≥1.5）+ 放量/背离消融 + 生产信号对照
python backtest_macd_divergence.py --limit 0 --start 2023-05-01 `
  --arms v1,vol:1.0,vol:1.2,no_div,baseline --out divergence_backtest.json
```

`--arms` 可选 `v1`（默认阈值）、`vol:<倍数>`（替换放量下限）、`no_div`（去掉底背离
要求）、`baseline`（生产 `macd_golden_cross_pullback_confirmed_*` 信号）。两条臂都
不加市场闸门与股票池过滤，差异只来自信号本身；成交语义（次日开盘买入、T+1、涨跌停、
佣金/印花税/滑点）与既有回测一致。

### 卖出规则（v1b，2026-10-04 定）

零轴+底背离池的卖出改用 **v1b**：四条规则任一触发即卖出。

本节记录 2026-10-04 基线规则及历史实验数字；线上三策略分析/回测的成本止损现可按上文独立覆盖，历史实验结果不随配置改写。

| 规则 | 触发条件 | 成交 |
|---|---|---|
| 止损 | 盘中触及买入价 −8% | 当根 K 线（跳空则按开盘价） |
| 日线 MACD 顶背离 | 最近两段已完成正柱区间：价格更高高点 + 面积收缩 | 收盘确认，次日开盘 |
| 跌破年线 | 收盘 < MA250 | 收盘确认，次日开盘 |
| 持仓超时 | 持仓满 40 根 K 线 | 该根 K 线开盘 |

关键在于**取消了固定 +30% 止盈**：让盈利单跟到趋势走坏为止，这是 v1b 单笔均值
（+2.77%）显著高于旧版（+1.43%）的主要原因。

配置（`backtest.exit_rules`）：

```yaml
backtest:
  exit_rules:
    mode: "divergence_trend"      # fixed = 旧行为(固定止盈+超时)
    top_divergence: true
    below_yearline: true
    ma_long_period: 250
    apply_to_signal_types:        # 作用范围: 只改研究池, 不动其它策略
      - "macd_divergence_bottom"
```

`apply_to_signal_types` 是刻意的隔离设计：`mode` 一旦生效会作用于所有走
`simulate_single_trade` 的策略，限定信号类型后，生产 MACD 池、缠论买点、年线池
的回测口径与历史研究结论都不受影响。

**5007 只 / 2023-05-01~2026-10-04 / 4 仓位（官方口径：引擎实现，逐笔可复现）：**

| 口径 | 单笔均值 | PF | 胜率 | 平均持仓 | 组合收益区间(4 种排序) | 最大回撤 |
|---|---:|---:|---:|---:|---|---:|
| 旧 fixed 8%/30%/40 天 | +1.43% | 1.26 | 37.0% | 29.4 天 | +47.4% ~ +93.0%（中位 +64.1%） | 12.8% ~ 17.0% |
| **v1b（现行）** | **+2.81%** | **1.56** | 33.6% | 28.2 天 | **+55.5% ~ +97.0%**（中位 +78.0%） | 21.0% ~ 22.8% |

v1b 的收益来源发生了结构性变化（`div_exit_engine.json`）：

| 退出原因 | 笔数 | 占比 | 单笔均值 | 合计贡献 |
|---|---:|---:|---:|---:|
| 止损 −8% | 240 | 42.9% | −9.16% | −2197 pp |
| 顶背离 | 138 | 24.7% | +6.00% | +828 pp |
| **持仓超时 40 天** | 134 | 24.0% | **+23.90%** | **+3202 pp** |
| 跌破年线 | 47 | 8.4% | −5.61% | −264 pp |

取消 +30% 固定止盈后，**40 天超时成了主要利润来源**（单笔 +23.90%），
因为它让盈利单一直跟到趋势走完；旧版里只有 17.4% 的单子能吃到 +30% 止盈。

组合层数字仍受「4 仓位 + 先到先得」影响极大（区间来自 4 种排序），但 v1b 在
**每一种排序下都优于旧版**，代价是最大回撤从 12.8%~17.0% 升到 21.0%~22.8%。

完整的 12 方案对照（含 MA20 放宽、移动止盈梯度、规则堆叠反例）与结论见
[`docs/exit-rules-study.md`](docs/exit-rules-study.md)。

### 持仓台账与交易闸门

**本系统不下单**：它只扫描、通知，持仓由使用者按券商实际成交录入。为让
`position.max_stocks`、`position.max_position_per_stock`、`risk.max_single_day_drawdown_pct`
真正生效，并限制每日交易次数，信号侧加了闸门（`trading/positions.py`）。

```yaml
trading_limits:
  enabled: true
  account_equity: 100000          # 券商实际权益; 留空则依赖权益的规则自动跳过
  max_new_positions_per_day: 2    # 每日最多开新仓数
  max_trades_per_day: 5           # 每日最多成交笔数 (买+卖)
  max_trades_per_symbol_per_day_per_side: 1   # 每只股票每日每方向最多一次
  max_new_position_pct: 0.25      # 单笔开仓占权益上限
  enforce_max_stocks: true
  enforce_single_position_cap: true
  enforce_single_day_drawdown: true
```

闸门规则（任一不满足即拦截信号）：

| 规则 | 依据 | 说明 |
|---|---|---|
| `per_symbol_per_day` | `max_trades_per_symbol_per_day_per_side` | 同股同日同向最多一次；**买卖各自计数** |
| `max_trades_per_day` | `max_trades_per_day` | 当日买+卖总笔数 |
| `max_new_positions_per_day` | `max_new_positions_per_day` | 只算开新仓；对已持仓加仓不算新仓 |
| `max_stocks` | `position.max_stocks` | 当前持仓只数 |
| `max_position_per_stock` | `position.max_position_per_stock` | 加仓后市值占权益比例 |
| `single_day_drawdown` | `risk.max_single_day_drawdown_pct` | 当日首次调用建立基准，之后逐次比较 |

要点：

- **被拦下的信号不会静默消失**：写入 `trade_gate_rejection` 表，并在 analysis 报告的
  `trade_gate_rejections` / `trade_gate_rejection_count` 里可见。
- **卖出不受回撤/仓位规则限制**：下跌日恰恰是最需要卖出的日子，卖出只受"同日同股同向一次"约束。
- **权益未知时不误判**：`account_equity` 留空则跳过所有依赖权益的规则，只保留笔数与持仓只数规则。

台账维护（`web_bridge.py positions`）：

```powershell
# 查看持仓、当日成交与拦截记录
echo '{"action":"list"}' | python web_bridge.py positions
# 回填一笔成交（自动更新持仓数量与成本）
echo '{"action":"trade","symbol":"600036.SH","side":"buy","quantity":1000,"price":35.2}' | python web_bridge.py positions
# 直接维护持仓
echo '{"action":"upsert","symbol":"600036.SH","quantity":1000,"avg_cost":35.2,"name":"招商银行"}' | python web_bridge.py positions
# 试探某笔拟成交是否放行
echo '{"action":"check","symbol":"000001.SZ","side":"buy","quantity":2000,"price":11.0}' | python web_bridge.py positions
```

### 持仓退出提醒（有持仓时才会推送）

**买点靠扫描推送；卖点以前只在网页上显示一个徽章，不会主动提醒你。**
现在只要有持仓、且退出条件触发，就会推一条「策略退出提醒」到已启用的通道
（微信 / 邮件 / Bark / Webhook），不再需要你自己去翻页面。

触发条件就是 141 行那套 v1b 规则（`mode: divergence_trend`）：**持仓成本止损、**
**已确认日线顶背离、跌破年线、持仓超时**，以及 `fixed` 模式下的固定止盈。

一个例子：

```
# 🔴 策略退出提醒

> **贵州茅台 (600519)**

- 策略：年线趋势、日线零轴金叉、零轴＋底背离
- 现价：1258.620
- 成本价：1887.930
- 浮动盈亏：-33.33%
- 触发条件：持仓成本止损：成本价 × 0.9200 = 1736.896（实际 1258.62）；
            跌破年线：收盘价 < MA250 (1344.849)（实际 1258.62）
- 建仓日：2026-08-19
- 确认时间：2026-10-05
```

要点：

- **没有持仓不会提醒**：`sell_conditions` 照常计算（作为建仓后的参考），
  但没有持仓就没有提醒——不会让你去卖一只没持有的股票。
- **一天最多一条**：同一天同一只股票只推一次（按分析所属交易日去重，落在
  `run_state` 表，键 `exit_notify:<symbol>:*`）。
- **条件持续期间每交易日一条**：止损是"状态"不是"事件"，只要还亏着就每天提醒，
  直到你卖出或清仓——避免周一的提醒被漏掉后后面就再也不提。
- **不把"没法判断"当"没事"**：`account_equity` / 成本价缺失时，该条件记为
  **未知**而不是"未触发"；成本未知的提醒会显式写「成本价：未知」，
  而不是印一个会让人误读的 `+0.00%`。
- **一条消息合并多策略**：三个策略常常同时标出同一条规则（例如都报成本止损），
  此时只推一条、`触发条件`里去重，标题列全部命中的策略名，不挑一个"背锅"。
- **频率很低，不会变成噪音**：提醒条数 ≈ 退出笔数。窗口内 830 个交易日里，
  底背离池共 559 个信号（不限仓位时的上界 ≈ 0.67 条/交易日）；
  实际 4 仓组合只有 88 笔真正成交 ≈ **0.11 条/交易日，约每 9~10 个交易日一条**，
  5 仓 0.13 条/交易日。超时是兜底退出条件，触发当天通常已经因止损/顶背离/
  破年线先退了。

```powershell
# 关掉卖点推送（买点推送不受影响）
# config.yaml -> notification.push_trade_signal: false
```

⚠️ **注意通知通道**：`notification.push_trade_signal: true` 只表示"允许推送
交易信号"，真正发得出去还要至少有一个通道处于 `enabled: true`。仓库默认
`wechat/webhook/email/bark` 全是关闭的，此时提醒只会进本地 outbox 表
（`event_count` 会增加），不会真的到你手机上。

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

算法输出同时带 `structure_time` 和 `confirmed_at`，推送与去重以确认时间为准。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
```

测试覆盖 MACD 0 轴交叉、午休对齐的 120 分钟聚合、包含/分型、一二三类买卖点、防重绘回放、买卖双评分、SQLite 去重、分通道投递和交易时段判断。

## 运行数据

- `cache/`：行情缓存和日线历史。
- `state/signal_monitor.db`：事件、outbox、候选池和任务状态。
- `output/`：每次分析/扫描的 JSON 报告。
- `logs/signal_monitor.log`：运行日志。

这些目录已加入 `.gitignore`。`state/` 是信号引擎运行时状态目录（SQLite 数据库），Docker 部署时挂载独立命名卷持久化；`data/` 目录只存放 Python 源码包、禁止被卷覆盖。删除缓存可强制重拉行情；删除数据库会丢失去重、候选池和调度状态，可能导致旧信号再次被视为新事件。
