# 数据源方案：AKShare + pytdx

## 1. 目标

本方案用于替换当前 `signal_system/data/data_fetcher.py` 中的 Tushare 直连模式，建立一套可扩展的数据源抽象层。

本阶段选择免费方案：

- 主数据源：`AKShare`
- 分钟级补充源：`pytdx`

暂不将 `Tushare` 作为主路径，也不围绕积分能力设计主流程。

---

## 2. 选择结论

### 2.1 为什么选 AKShare

AKShare 作为主数据源，适合当前阶段，原因是：

- 免费可用，不受 Tushare 积分门槛限制
- 文档完整，接口覆盖面广，适合快速对接和排错
- 能覆盖当前系统的核心日频需求：
  - 股票列表
  - 个股日线
  - 指数日线
  - 部分基础面与行情辅助字段
- 后续即使切到 Tushare，也可以保留为 fallback provider

### 2.2 为什么配 pytdx

pytdx 不适合作为整个系统的唯一主数据源，但非常适合提供分钟级行情：

- 支持 1/5/15/30/60 分钟数据
- 更适合盘中信号、做 T、分钟级回测或实时扫描
- 可作为日频系统向盘中系统演进时的自然补充

### 2.3 为什么现在不选 Tushare

当前你已经确认 Tushare 有 Token，但权限受积分限制。对现阶段来说，它不是一个稳定可依赖的主路径。

因此当前阶段的原则是：

- 不让“积分不足”阻塞系统演进
- 先完成免费数据源抽象
- 以后再把 Tushare 接成可选 provider

---

## 3. 数据分层设计

建议把数据层拆成 4 层：

### 3.1 Provider 接口层

定义统一协议，例如：

- `get_stock_list()`
- `get_daily_data(ts_code, start_date=None, end_date=None, period=250)`
- `get_index_daily(ts_code='000001.SH', start_date=None, end_date=None, period=250)`
- `get_daily_basic(ts_code, trade_date=None)`
- `get_financial_data(ts_code, period=None)`
- `get_minute_data(ts_code, interval='5m', start_date=None, end_date=None, count=None)`
- `get_latest_trade_date()`
- `get_trade_calendar(start_date=None, end_date=None)`

这一层不关心具体是 AKShare 还是 pytdx。

### 3.2 Adapter 实现层

按数据源分别实现：

- `AkshareDailyProvider`
- `PytdxMinuteProvider`

后续可扩展：

- `TushareProvider`
- `BaoStockProvider`

### 3.3 Orchestrator 层

新增统一入口，例如：

- `MarketDataService`

职责：

- 按数据类型路由到正确 provider
- 负责 fallback
- 负责缓存
- 负责字段标准化

### 3.4 兼容层

保留现有 `DataFetcher` 类名，内部改为委托 `MarketDataService`，这样尽量不动上层 `StrategyEngine`。

目标是：

- 上层扫描逻辑先不改
- 先替换数据底座

---

## 4. 数据源职责划分

### 4.1 AKShare 负责

AKShare 负责所有日频主链路数据：

- 股票列表
- 个股日线
- 指数日线
- 日频基础行情补充
- 能从公开源稳定拿到的基础面辅助字段

适用模块：

- `signal_system/strategy/strategy_engine.py`
- `core/regime/market_regime_engine.py`
- `core/selector/stock_selector.py`
- `backtest/` 日线回测

### 4.2 pytdx 负责

pytdx 只负责分钟级或更实时的行情：

- 1 分钟线
- 5 分钟线
- 15/30/60 分钟线
- 盘中补充行情

适用模块：

- `core/position/t_trading.py`
- 未来盘中扫描
- 未来分钟级回测

### 4.3 交易日历由 AKShare 负责

当前系统里 `get_latest_trade_date()` 仍然是 Tushare `trade_cal` 思路。

切换到免费方案后，交易日历统一由 AKShare 提供，原因是：

- 日频主链路已经在 AKShare
- `daily_basic`、日线和指数日线都需要同一套交易日判断
- 不应该让 `get_latest_trade_date()` 成为最后一个残留的 Tushare 依赖

因此第一阶段要补两个统一入口：

- `get_trade_calendar(start_date=None, end_date=None)`
- `get_latest_trade_date()`

其中：

- `get_trade_calendar()` 返回标准化交易日列表
- `get_latest_trade_date()` 基于交易日历推导，不再直接调用某个 provider 的私有逻辑

### 4.4 当前阶段不做的事

本轮不做：

- 全量实时 tick
- 盘口逐笔
- 高频撮合
- Level2 级别特征

---

## 5. 字段标准化

必须先统一字段，再允许上层调用。

### 5.1 股票代码标准

系统内部统一使用：

- `600000.SH`
- `000001.SZ`

因此需要在 provider 层做代码转换：

- AKShare 常见代码可能是 `600000`
- pytdx 常用是 `market + code`

建议新增统一工具：

- `normalize_ts_code()`
- `split_ts_code()`
- `to_akshare_symbol()`
- `to_pytdx_params()`

### 5.2 日线字段标准

统一输出 DataFrame 列：

- `trade_date`
- `open`
- `high`
- `low`
- `close`
- `vol`
- `amount`
- `pct_chg`
- `turnover_rate`（拿得到就填）

### 5.3 指数字段标准

至少统一：

- `trade_date`
- `open`
- `high`
- `low`
- `close`
- `vol`

### 5.4 daily_basic 标准

统一输出 dict：

- `turnover_rate`
- `volume_ratio`
- `pe`
- `pb`
- `total_mv`
- `circ_mv`

拿不到的字段允许为 `None`，但 key 要稳定存在。

### 5.5 financial_data 标准

统一输出 dict：

- `roe`
- `debt_to_assets`
- `current_ratio`
- `quick_ratio`

如果 AKShare 某些源拿不到全部字段：

- 先保证 `roe` 和 `debt_to_assets`
- 缺字段返回 `None`

### 5.6 financial_data 降级规则

财务字段是这轮改造里最容易“数据不齐但流程又不能直接崩”的部分，因此必须先定义降级策略。

第一阶段按下面的规则执行：

- `roe`、`debt_to_assets` 是 selector 最低依赖字段
- `current_ratio`、`quick_ratio` 允许为空
- 如果 `roe` 或 `debt_to_assets` 缺失：
  - 该股票不进入 `fundamental` 通过名单
  - 记录明确失败原因
  - 不做隐式放宽

这样做的原因是：

- 当前选股逻辑已经依赖基本面阈值
- 如果对缺字段股票做“默认放行”，会让回测和实盘候选池失真
- 先保持保守过滤，比放宽后引入脏样本更稳

后续如果要放宽，需要单独加配置，例如：

- `selector.allow_missing_financial_fields`
- `selector.financial_fallback_mode`

---

## 6. 缓存策略

数据源切换后，缓存不能再直接按“provider 无关的 pickle”粗放保存。

建议缓存键改成：

- `{provider}_{method}_{symbol}_{date_range}`

例如：

- `akshare_daily_600276.SH_20240101_20260301`
- `pytdx_minute_600276.SH_5m_20260301_20260308`

缓存建议：

- 股票列表：24 小时
- 日线：6 小时
- 指数日线：6 小时
- daily_basic：1 个交易日
- financial_data：7 天
- 分钟线：15 分钟到 1 小时

---

## 7. 回退与失败策略

### 7.1 本阶段建议

先不做复杂多级 fallback，只做固定职责路由：

- 日频调用失败：AKShare 抛错并返回空数据
- 分钟级调用失败：pytdx 抛错并返回空数据

原因：

- 当前主目标是先把 provider 层抽象出来
- 过早引入多级 fallback 会让调试复杂度上升

第一阶段应明确：

- `MarketDataService` 负责“职责路由”，不是“自动容灾切换”
- fallback 接口可以预留，但默认关闭
- 所有失败都要写入日志，并保留 provider 名称、方法名、symbol 和时间范围

### 7.2 第二阶段再做

后续可以加：

- `AKShare -> Tushare`
- `AKShare -> BaoStock`
- `pytdx -> AKShare 分钟接口`

但不放进当前第一轮改造。

### 7.3 第一阶段的失败返回约定

为避免上层代码在 provider 切换时出现不一致行为，第一阶段统一约定：

- DataFrame 型接口失败时返回空 `DataFrame`
- dict 型接口失败时返回 `None`
- 列表型接口失败时返回空列表

并且：

- 不吞异常上下文
- 日志至少包含 `provider / method / symbol / error`

---

## 8. 对现有代码的影响

### 8.1 直接受影响文件

- `quant-python/signal_system/data/data_fetcher.py`
- `quant-python/signal_system/config/config.yaml`

### 8.2 新增目录建议

- `quant-python/signal_system/data/providers/`
- `quant-python/signal_system/data/providers/akshare_provider.py`
- `quant-python/signal_system/data/providers/pytdx_provider.py`
- `quant-python/signal_system/data/market_data_service.py`
- `quant-python/signal_system/data/symbols.py`

### 8.3 上层模块尽量不动

本阶段原则：

- `StrategyEngine` 不直接感知 AKShare / pytdx
- `MarketRegimeEngine` 不直接感知 AKShare / pytdx
- 上层仍然通过 `DataFetcher` 风格接口访问

---

## 9. 分阶段落地顺序

### P1. 建立抽象，不改业务逻辑

- 定义 provider 接口
- 新建 `AkshareDailyProvider`
- 新建 `PytdxMinuteProvider`
- 新建 `MarketDataService`
- 让 `DataFetcher` 变成兼容层

验收标准：

- 现有单测和集成测试继续可跑
- 上层调用签名不变

### P2. 打通 AKShare 日频主链路

- 接通股票列表
- 接通个股日线
- 接通指数日线
- 接通 daily_basic
- 接通 financial_data

验收标准：

- `run_daily_scan()` 可在不依赖 Tushare 的前提下运行
- 真实数据验收脚本可直接走 AKShare

### P3. 打通 pytdx 分钟级

- 接入 5 分钟线
- 统一分钟线字段
- 为做 T 与盘中扫描预留入口

验收标准：

- 新增分钟线读取测试
- 可拉取单只股票最近一段 5 分钟数据

### P4. 文档与基线

- 补运行说明
- 补 provider 对照表
- 补分钟线样例输出

---

## 10. 本轮改造边界

本轮只做数据源架构改造，不做这些事：

- 不重写策略逻辑
- 不重写回测逻辑
- 不新增实时交易接口
- 不接券商
- 不做复杂多源容灾

---

## 11. 最终建议

当前项目下一阶段采用：

- `AKShare` 作为日频主数据源
- `pytdx` 作为分钟级补充数据源
- `DataFetcher -> MarketDataService -> Provider Adapter` 三层结构改造

这样做的优点是：

- 免费可运行
- 不被 Tushare 积分卡住
- 对现有上层逻辑侵入最小
- 后续如果拿到 Tushare 权限，可以平滑新增 provider，而不是再次重写数据层
