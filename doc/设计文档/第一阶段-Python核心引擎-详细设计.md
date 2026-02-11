# 量化交易信号系统 - 第一阶段详细设计

**文档版本**: v1.0
**编写日期**: 2026-02-11
**系统阶段**: 第一阶段 - Python 核心引擎详细设计

---

## 1. 文档说明

本文档是《第一阶段-Python核心引擎-概要设计》的详细设计文档，包含：
- 详细的类设计和接口定义
- 核心算法的实现细节
- 数据结构设计
- 关键流程的时序图
- 异常处理机制

---

## 2. 数据层详细设计

### 2.1 DataFetcher 类设计

#### 2.1.1 类图

```python
class DataFetcher:
    """数据获取器 - 负责从 Tushare 获取数据并缓存"""

    # 属性
    - tushare_token: str
    - use_cache: bool
    - cache_dir: str
    - pro: TushareProAPI
    - _cache: Dict[str, Any]

    # 方法
    + __init__(token, use_cache, cache_dir)
    + get_stock_list(exchange, list_status) -> DataFrame
    + get_daily_data(ts_code, start_date, end_date) -> DataFrame
    + get_minute_data(ts_code, freq, start_date, end_date) -> DataFrame
    + get_financial_data(ts_code, period) -> Dict
    + get_daily_basic(ts_code, trade_date) -> Dict
    + get_index_daily(ts_code, start_date, end_date) -> DataFrame
    - _get_cache_key(method, params) -> str
    - _load_cache(cache_key, max_age_hours) -> Any
    - _save_cache(cache_key, data) -> None
    - _get_latest_trade_date() -> str
```

#### 2.1.2 核心方法实现

**get_minute_data() 方法**

```python
def get_minute_data(self, ts_code: str, freq: str,
                    start_date: str = None, end_date: str = None,
                    period: int = 100) -> pd.DataFrame:
    """
    获取分钟级行情数据

    Args:
        ts_code: 股票代码
        freq: 频率 ('5min', '15min', '30min', '60min')
        start_date: 开始日期 (YYYYMMDD)
        end_date: 结束日期 (YYYYMMDD)
        period: 获取周期数（如果不指定日期）

    Returns:
        DataFrame: 分钟K线数据

    Raises:
        DataSourceError: 数据源异常
        NetworkError: 网络异常
    """
    # 1. 参数处理
    if start_date is None:
        start_date = self._calculate_start_date(freq, period)
    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d %H:%M:%S')

    # 2. 检查缓存
    cache_key = f"minute_{ts_code}_{freq}_{start_date}_{end_date}"
    cached_data = self._load_cache(cache_key, max_age_hours=1)
    if cached_data is not None:
        logger.debug(f"从缓存加载分钟数据: {ts_code} {freq}")
        return cached_data

    # 3. 调用 Tushare API
    try:
        df = self.pro.stk_mins(
            ts_code=ts_code,
            freq=freq,
            start_date=start_date,
            end_date=end_date
        )

        if df.empty:
            logger.warning(f"未获取到数据: {ts_code} {freq}")
            return pd.DataFrame()

        # 4. 数据处理
        df = df.sort_values('trade_time').reset_index(drop=True)
        df['trade_time'] = pd.to_datetime(df['trade_time'])

        # 5. 保存缓存
        self._save_cache(cache_key, df)

        logger.info(f"获取分钟数据成功: {ts_code} {freq}, {len(df)} 条")
        return df

    except Exception as e:
        logger.error(f"获取分钟数据失败: {ts_code} {freq}, {e}")
        raise DataSourceError(f"获取分钟数据失败: {e}")
```

**_calculate_start_date() 辅助方法**

```python
def _calculate_start_date(self, freq: str, period: int) -> str:
    """
    根据频率和周期数计算开始日期

    Args:
        freq: 频率
        period: 周期数

    Returns:
        str: 开始日期 (YYYYMMDD)
    """
    freq_to_minutes = {
        '5min': 5,
        '15min': 15,
        '30min': 30,
        '60min': 60
    }

    minutes = freq_to_minutes.get(freq, 60)
    # 每天交易时间 4 小时 = 240 分钟
    bars_per_day = 240 // minutes
    # 计算需要的交易日数
    days_needed = (period // bars_per_day) + 5  # 多加5天缓冲

    start_date = datetime.now() - timedelta(days=days_needed * 2)
    return start_date.strftime('%Y%m%d')
```

### 2.2 DataCache 类设计

#### 2.2.1 类图

```python
class DataCache:
    """数据缓存管理器 - 使用 LRU 策略"""

    # 属性
    - cache_dir: str
    - max_size: int
    - _cache_index: Dict[str, CacheEntry]

    # 方法
    + __init__(cache_dir, max_size)
    + get(key, max_age_hours) -> Any
    + set(key, value, ttl_hours) -> None
    + clear_expired() -> int
    + get_cache_stats() -> Dict
    - _load_from_disk(key) -> Any
    - _save_to_disk(key, value) -> None
    - _evict_lru() -> None
```

#### 2.2.2 缓存策略

**缓存键设计**

```python
# 格式: {data_type}_{params_hash}_{date}
# 示例:
"daily_000001.SZ_20260101_20260211"
"minute_5min_000001.SZ_20260211"
"financial_000001.SZ_20231231"
```

**缓存过期策略**

```python
CACHE_TTL = {
    'stock_list': 24,      # 24小时
    'daily': 24,           # 24小时
    'minute': 1,           # 1小时
    'financial': 168,      # 7天
    'index': 24            # 24小时
}
```

---

## 3. 策略层详细设计

### 3.1 策略基类设计

#### 3.1.1 BaseStrategy 抽象类

```python
from abc import ABC, abstractmethod
from typing import List, Dict, Any
import pandas as pd

class BaseStrategy(ABC):
    """策略基类 - 所有策略必须继承此类"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化策略

        Args:
            config: 策略配置参数
        """
        self.config = config
        self.name = self.__class__.__name__
        self.enabled = config.get('enabled', True)
        self.weight = config.get('weight', 1.0)

    @abstractmethod
    def select_stocks(self, stock_list: pd.DataFrame) -> List[Dict]:
        """
        选股逻辑

        Args:
            stock_list: 股票列表

        Returns:
            List[Dict]: 通过筛选的股票列表
        """
        pass

    @abstractmethod
    def generate_signals(self, stock_data: pd.DataFrame,
                        timeframe: str) -> List[Dict]:
        """
        生成交易信号

        Args:
            stock_data: 股票数据
            timeframe: 时间周期 ('5min', '30min', '60min', '120min', '1d')

        Returns:
            List[Dict]: 信号列表
        """
        pass

    @abstractmethod
    def score_signal(self, signal: Dict) -> int:
        """
        信号评分

        Args:
            signal: 信号字典

        Returns:
            int: 评分 (0-100)
        """
        pass

    def validate_signal(self, signal: Dict) -> bool:
        """
        信号验证

        Args:
            signal: 信号字典

        Returns:
            bool: 是否有效
        """
        required_fields = ['ts_code', 'signal_type', 'price', 'timeframe']
        return all(field in signal for field in required_fields)
```

### 3.2 趋势跟踪策略实现

#### 3.2.1 TrendFollowingStrategy 类

```python
class TrendFollowingStrategy(BaseStrategy):
    """趋势跟踪策略"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.ma_period = config.get('ma_period', 250)
        self.macd_fast = config.get('macd_fast', 12)
        self.macd_slow = config.get('macd_slow', 26)
        self.macd_signal = config.get('macd_signal', 9)

    def select_stocks(self, stock_list: pd.DataFrame) -> List[Dict]:
        """
        选股逻辑：
        1. 基本面：ROE > 10%, 负债率 < 50%
        2. 技术面：年线向上
        3. 成交量：换手率 1-5%
        """
        selected = []

        for _, stock in stock_list.iterrows():
            # 基本面筛选
            if not self._check_fundamental(stock):
                continue

            # 技术面筛选
            if not self._check_technical(stock):
                continue

            # 成交量筛选
            if not self._check_volume(stock):
                continue

            selected.append(stock.to_dict())

        return selected

    def generate_signals(self, stock_data: pd.DataFrame,
                        timeframe: str) -> List[Dict]:
        """
        生成信号逻辑：
        1. 计算技术指标
        2. 识别信号模式
        3. 生成信号
        """
        signals = []

        # 计算指标
        indicators = self._calculate_indicators(stock_data)

        # 买入信号检测
        buy_signal = self._detect_buy_signal(indicators, timeframe)
        if buy_signal:
            signals.append(buy_signal)

        # 卖出信号检测
        sell_signal = self._detect_sell_signal(indicators, timeframe)
        if sell_signal:
            signals.append(sell_signal)

        return signals
```

#### 3.2.2 核心方法实现

**_calculate_indicators() 方法**

```python
def _calculate_indicators(self, df: pd.DataFrame) -> Dict:
    """
    计算技术指标

    Args:
        df: OHLCV 数据

    Returns:
        Dict: 指标字典
    """
    close = df['close'].values

    # 计算均线
    ma250 = talib.SMA(close, timeperiod=self.ma_period)

    # 计算 MACD
    macd, signal, hist = talib.MACD(
        close,
        fastperiod=self.macd_fast,
        slowperiod=self.macd_slow,
        signalperiod=self.macd_signal
    )

    # 计算 RSI
    rsi = talib.RSI(close, timeperiod=14)

    # 计算成交量均线
    volume = df['volume'].values
    vol_ma = talib.SMA(volume, timeperiod=30)

    return {
        'close': close,
        'ma250': ma250,
        'macd': macd,
        'signal': signal,
        'hist': hist,
        'rsi': rsi,
        'volume': volume,
        'vol_ma': vol_ma
    }
```

**_detect_buy_signal() 方法**

```python
def _detect_buy_signal(self, indicators: Dict, timeframe: str) -> Dict:
    """
    检测买入信号

    买入条件：
    1. 年线向上
    2. 价格接近年线（5%以内）
    3. MACD 底背离或金叉
    4. 放量（量比 > 1.5）

    Args:
        indicators: 技术指标
        timeframe: 时间周期

    Returns:
        Dict: 买入信号，如果没有则返回 None
    """
    close = indicators['close']
    ma250 = indicators['ma250']
    macd = indicators['macd']
    signal = indicators['signal']
    hist = indicators['hist']
    volume = indicators['volume']
    vol_ma = indicators['vol_ma']

    # 检查数据完整性
    if len(close) < 60 or np.isnan(ma250[-1]):
        return None

    # 条件1: 年线向上
    ma_slope = (ma250[-1] - ma250[-20]) / 20
    if ma_slope <= 0:
        return None

    # 条件2: 价格接近年线
    current_price = close[-1]
    distance = abs(current_price - ma250[-1]) / ma250[-1]
    if distance > 0.05:
        return None

    # 条件3: MACD 金叉
    macd_cross = macd[-1] > signal[-1] and macd[-2] <= signal[-2]

    # 条件4: 底背离检测
    divergence = self._detect_bullish_divergence(close, hist)

    # 条件5: 放量
    volume_burst = volume[-1] > vol_ma[-1] * 1.5

    # 至少满足 MACD 金叉或底背离
    if not (macd_cross or divergence):
        return None

    # 生成信号
    reasons = []
    if ma_slope > 0:
        reasons.append('年线向上')
    if distance < 0.05:
        reasons.append('回调至年线')
    if macd_cross:
        reasons.append('MACD金叉')
    if divergence:
        reasons.append('底背离')
    if volume_burst:
        reasons.append('放量')

    return {
        'signal_type': 'BUY',
        'price': current_price,
        'timeframe': timeframe,
        'reasons': reasons,
        'indicators': {
            'ma250': ma250[-1],
            'macd': macd[-1],
            'rsi': indicators['rsi'][-1]
        }
    }
```

**_detect_bullish_divergence() 方法**

```python
def _detect_bullish_divergence(self, price: np.ndarray,
                               indicator: np.ndarray,
                               lookback: int = 60) -> bool:
    """
    检测底背离

    Args:
        price: 价格序列
        indicator: 指标序列（如 MACD hist）
        lookback: 回看周期

    Returns:
        bool: 是否存在底背离
    """
    if len(price) < lookback:
        return False

    recent_price = price[-lookback:]
    recent_indicator = indicator[-lookback:]

    # 找到价格和指标的低点
    from scipy.signal import find_peaks

    # 价格低点
    price_troughs, _ = find_peaks(-recent_price, distance=10)

    # 指标低点
    indicator_troughs, _ = find_peaks(-recent_indicator, distance=10)

    # 需要至少两个低点
    if len(price_troughs) < 2 or len(indicator_troughs) < 2:
        return False

    # 检查最近两个低点
    last_price_trough = price_troughs[-1]
    prev_price_trough = price_troughs[-2]
    last_ind_trough = indicator_troughs[-1]
    prev_ind_trough = indicator_troughs[-2]

    # 底背离：价格创新低，指标不创新低
    price_lower = recent_price[last_price_trough] < recent_price[prev_price_trough]
    indicator_higher = recent_indicator[last_ind_trough] > recent_indicator[prev_ind_trough]

    return price_lower and indicator_higher
```

### 3.3 信号引擎设计

#### 3.3.1 SignalEngine 类

```python
class SignalEngine:
    """信号引擎 - 负责信号生成和管理"""

    def __init__(self, config: Dict, data_fetcher: DataFetcher):
        self.config = config
        self.data_fetcher = data_fetcher
        self.strategies = []
        self.signal_history = []

    def register_strategy(self, strategy: BaseStrategy):
        """注册策略"""
        self.strategies.append(strategy)
        logger.info(f"注册策略: {strategy.name}")

    def scan_minute_signals(self, stock_pool: List[str],
                           timeframes: List[str]) -> List[Dict]:
        """
        扫描分钟级信号

        Args:
            stock_pool: 候选股票池
            timeframes: 时间周期列表 ['5min', '30min', '60min', '120min']

        Returns:
            List[Dict]: 信号列表
        """
        all_signals = []

        for ts_code in stock_pool:
            for timeframe in timeframes:
                # 获取分钟数据
                df = self.data_fetcher.get_minute_data(
                    ts_code,
                    freq=timeframe,
                    period=100
                )

                if df.empty:
                    continue

                # 每个策略生成信号
                for strategy in self.strategies:
                    if not strategy.enabled:
                        continue

                    signals = strategy.generate_signals(df, timeframe)

                    for signal in signals:
                        signal['ts_code'] = ts_code
                        signal['strategy'] = strategy.name
                        signal['weight'] = strategy.weight

                        # 评分
                        score = strategy.score_signal(signal)
                        signal['score'] = score

                        # 验证
                        if strategy.validate_signal(signal):
                            all_signals.append(signal)

        # 去重和排序
        filtered_signals = self._filter_signals(all_signals)

        return filtered_signals

    def _filter_signals(self, signals: List[Dict]) -> List[Dict]:
        """
        信号过滤和去重

        规则：
        1. 同一股票同一类型，只保留评分最高的
        2. 按评分排序
        3. 限制数量（前10个）
        """
        # 按股票代码和信号类型分组
        signal_map = {}

        for signal in signals:
            key = f"{signal['ts_code']}_{signal['signal_type']}"

            if key not in signal_map:
                signal_map[key] = signal
            else:
                # 保留评分更高的
                if signal['score'] > signal_map[key]['score']:
                    signal_map[key] = signal

        # 转换为列表并排序
        filtered = list(signal_map.values())
        filtered.sort(key=lambda x: x['score'], reverse=True)

        return filtered[:10]
```

---

## 4. 分钟级监控详细设计

### 4.1 监控架构

```
┌─────────────────────────────────────────┐
│  定时任务调度器 (Scheduler)              │
│  - 每 N 分钟触发一次                     │
└──────────────┬──────────────────────────┘
               │
               ↓
┌──────────────────────────────────────────┐
│  分钟级监控器 (MinuteMonitor)            │
│  1. 读取候选股票池                       │
│  2. 获取最新分钟数据                     │
│  3. 调用信号引擎                         │
│  4. 推送重要信号                         │
└──────────────┬──────────────────────────┘
               │
               ↓
┌──────────────────────────────────────────┐
│  信号引擎 (SignalEngine)                 │
│  - 多策略并行计算                        │
│  - 信号评分和过滤                        │
└──────────────────────────────────────────┘
```

### 4.2 MinuteMonitor 类设计

```python
class MinuteMonitor:
    """分钟级监控器"""

    def __init__(self, config: Dict, signal_engine: SignalEngine,
                 notifier: Notifier):
        self.config = config
        self.signal_engine = signal_engine
        self.notifier = notifier
        self.stock_pool = []
        self.last_signals = {}

    def load_stock_pool(self):
        """加载候选股票池（来自日线选股）"""
        pool_file = 'cache/stock_pool_latest.pkl'

        if not os.path.exists(pool_file):
            logger.warning("股票池文件不存在")
            return

        with open(pool_file, 'rb') as f:
            self.stock_pool = pickle.load(f)

        logger.info(f"加载股票池: {len(self.stock_pool)} 只")

    def run_scan(self, timeframes: List[str] = None):
        """
        执行扫描

        Args:
            timeframes: 时间周期，默认 ['30min', '60min']
        """
        if not self.stock_pool:
            self.load_stock_pool()

        if not self.stock_pool:
            logger.warning("股票池为空，跳过扫描")
            return

        if timeframes is None:
            timeframes = ['30min', '60min']

        logger.info(f"开始分钟级扫描: {len(self.stock_pool)} 只股票")

        # 扫描信号
        signals = self.signal_engine.scan_minute_signals(
            self.stock_pool,
            timeframes
        )

        # 过滤新信号（避免重复推送）
        new_signals = self._filter_new_signals(signals)

        if new_signals:
            logger.info(f"发现新信号: {len(new_signals)} 个")

            # 推送通知
            self._notify_signals(new_signals)

            # 更新历史
            self._update_signal_history(new_signals)

    def _filter_new_signals(self, signals: List[Dict]) -> List[Dict]:
        """过滤新信号（避免重复推送）"""
        new_signals = []

        for signal in signals:
            key = f"{signal['ts_code']}_{signal['signal_type']}_{signal['timeframe']}"

            # 检查是否在最近推送过
            if key in self.last_signals:
                last_time = self.last_signals[key]
                # 如果距离上次推送不到30分钟，跳过
                if (datetime.now() - last_time).seconds < 1800:
                    continue

            new_signals.append(signal)
            self.last_signals[key] = datetime.now()

        return new_signals

    def _notify_signals(self, signals: List[Dict]):
        """推送信号通知"""
        if not signals:
            return

        # 格式化消息
        content = self._format_signal_message(signals)

        # 推送
        self.notifier.send_wechat(content, msg_type='markdown')

    def _format_signal_message(self, signals: List[Dict]) -> str:
        """格式化信号消息"""
        content = f"# 🔔 分钟级交易信号\n\n"
        content += f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"

        for i, signal in enumerate(signals[:5], 1):
            signal_type_emoji = '🟢' if signal['signal_type'] == 'BUY' else '🔴'

            content += f"### {i}. {signal_type_emoji} {signal.get('stock_name', signal['ts_code'])}\n\n"
            content += f"- **代码**: {signal['ts_code']}\n"
            content += f"- **类型**: {signal['signal_type']}\n"
            content += f"- **价格**: ¥{signal['price']:.2f}\n"
            content += f"- **周期**: {signal['timeframe']}\n"
            content += f"- **评分**: {signal['score']}\n"
            content += f"- **原因**: {', '.join(signal['reasons'])}\n\n"

        return content
```

### 4.3 定时任务设计

```python
import schedule
import time

class TaskScheduler:
    """任务调度器"""

    def __init__(self, config: Dict):
        self.config = config
        self.jobs = []

    def add_daily_job(self, func, time_str: str):
        """添加每日任务"""
        job = schedule.every().day.at(time_str).do(func)
        self.jobs.append(job)
        logger.info(f"添加每日任务: {func.__name__} at {time_str}")

    def add_minute_job(self, func, interval: int):
        """添加分钟任务"""
        job = schedule.every(interval).minutes.do(func)
        self.jobs.append(job)
        logger.info(f"添加分钟任务: {func.__name__} every {interval} min")

    def run(self):
        """运行调度器"""
        logger.info("任务调度器启动")

        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("任务调度器停止")
                break
            except Exception as e:
                logger.error(f"任务执行异常: {e}", exc_info=True)
                time.sleep(60)
```

**使用示例**

```python
# 创建调度器
scheduler = TaskScheduler(config)

# 添加日线选股任务（每天15:30）
scheduler.add_daily_job(
    lambda: stock_selector.run_daily_selection(),
    "15:30"
)

# 添加分钟级监控任务（每30分钟）
scheduler.add_minute_job(
    lambda: minute_monitor.run_scan(['30min', '60min']),
    30
)

# 启动
scheduler.run()
```

---

## 5. 数据流设计

### 5.1 日线选股数据流

```
[开始] 每日 15:30
    ↓
[获取] 全市场股票列表
    ↓
[筛选] 基本面过滤
    ├─ 获取财务数据
    ├─ 计算 ROE、负债率
    └─ 过滤不符合条件的
    ↓
[筛选] 成交量过滤
    ├─ 获取日线数据
    ├─ 计算换手率
    └─ 过滤不符合条件的
    ↓
[筛选] 技术面过滤
    ├─ 计算年线
    ├─ 计算 MACD
    └─ 过滤不符合条件的
    ↓
[保存] 候选股票池
    ├─ 保存到缓存文件
    └─ 记录选股日志
    ↓
[推送] 选股结果通知
    ↓
[结束]
```

### 5.2 分钟级信号数据流

```
[开始] 每 N 分钟
    ↓
[加载] 候选股票池
    ↓
[循环] 遍历每只股票
    ├─ [获取] 分钟K线数据
    │   ├─ 检查缓存
    │   └─ 调用 Tushare API
    ├─ [计算] 技术指标
    │   ├─ MACD
    │   ├─ 均线
    │   └─ RSI
    ├─ [识别] 信号模式
    │   ├─ 买入信号
    │   └─ 卖出信号
    └─ [评分] 信号质量
    ↓
[过滤] 去重和排序
    ├─ 同股票同类型去重
    ├─ 按评分排序
    └─ 取前 N 个
    ↓
[检查] 是否为新信号
    ├─ 对比历史记录
    └─ 过滤重复信号
    ↓
[推送] 新信号通知
    ↓
[保存] 信号历史
    ↓
[结束]
```

---

## 6. 时序图

### 6.1 日线选股时序图

```
用户      主程序      选股引擎    数据获取器    Tushare    通知服务
 │          │           │           │           │          │
 │  启动    │           │           │           │          │
 │─────────>│           │           │           │          │
 │          │  定时触发 │           │           │          │
 │          │──────────>│           │           │          │
 │          │           │  获取列表 │           │          │
 │          │           │──────────>│           │          │
 │          │           │           │  API调用  │          │
 │          │           │           │──────────>│          │
 │          │           │           │<──────────│          │
 │          │           │<──────────│           │          │
 │          │           │  基本面筛选│          │          │
 │          │           │───────────│           │          │
 │          │           │  成交量筛选│          │          │
 │          │           │───────────│           │          │
 │          │           │  技术面筛选│          │          │
 │          │           │───────────│           │          │
 │          │           │  保存结果 │           │          │
 │          │           │───────────│           │          │
 │          │<──────────│           │           │          │
 │          │  推送通知 │           │           │          │
 │          │──────────────────────────────────>│          │
 │<─────────────────────────────────────────────│          │
```

### 6.2 分钟级信号时序图

```
定时器    监控器    信号引擎    策略    数据获取器    通知服务
 │         │         │         │         │            │
 │  触发   │         │         │         │            │
 │────────>│         │         │         │            │
 │         │  加载池 │         │         │            │
 │         │────────>│         │         │            │
 │         │  扫描   │         │         │            │
 │         │────────>│         │         │            │
 │         │         │  生成信号│        │            │
 │         │         │────────>│         │            │
 │         │         │         │  获取数据│           │
 │         │         │         │────────>│            │
 │         │         │         │<────────│            │
 │         │         │         │  计算指标│           │
 │         │         │         │─────────│            │
 │         │         │         │  识别信号│           │
 │         │         │         │─────────│            │
 │         │         │<────────│         │            │
 │         │         │  过滤排序│        │            │
 │         │         │─────────│         │            │
 │         │<────────│         │         │            │
 │         │  推送   │         │         │            │
 │         │────────────────────────────────────────>│
```

---

## 7. 异常处理设计

### 7.1 异常类层次

```python
class QuantSystemError(Exception):
    """系统基础异常"""
    pass

class DataSourceError(QuantSystemError):
    """数据源异常"""
    pass

class NetworkError(QuantSystemError):
    """网络异常"""
    pass

class CacheError(QuantSystemError):
    """缓存异常"""
    pass

class StrategyError(QuantSystemError):
    """策略异常"""
    pass

class SignalError(QuantSystemError):
    """信号异常"""
    pass
```

### 7.2 异常处理策略

```python
def safe_execute(func, *args, **kwargs):
    """
    安全执行函数，带重试和降级

    Args:
        func: 要执行的函数
        *args, **kwargs: 函数参数

    Returns:
        函数返回值或 None
    """
    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)

        except NetworkError as e:
            logger.warning(f"网络异常，重试 {attempt + 1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
            else:
                logger.error(f"网络异常，重试失败: {e}")
                return None

        except DataSourceError as e:
            logger.error(f"数据源异常: {e}")
            # 尝试备用数据源
            return try_backup_source(*args, **kwargs)

        except Exception as e:
            logger.error(f"未知异常: {e}", exc_info=True)
            return None
```

### 7.3 关键点异常处理

**数据获取异常处理**

```python
def get_data_with_fallback(ts_code, data_type):
    """带降级的数据获取"""
    try:
        # 主数据源
        return tushare_fetcher.get_data(ts_code, data_type)
    except DataSourceError:
        logger.warning("Tushare 失败，尝试 AkShare")
        try:
            # 备用数据源
            return akshare_fetcher.get_data(ts_code, data_type)
        except Exception as e:
            logger.error(f"所有数据源失败: {e}")
            return None
```

**信号生成异常处理**

```python
def generate_signals_safe(stock_data, strategy):
    """安全的信号生成"""
    try:
        signals = strategy.generate_signals(stock_data)
        return signals
    except StrategyError as e:
        logger.error(f"策略执行失败: {e}")
        return []
    except Exception as e:
        logger.error(f"信号生成异常: {e}", exc_info=True)
        return []
```

---

## 8. 配置管理设计

### 8.1 配置文件结构

```yaml
# config.yaml
system:
  name: "量化交易信号系统"
  version: "1.0.0"
  log_level: "INFO"

data_source:
  primary: "tushare"
  tushare:
    token: "${TUSHARE_TOKEN}"  # 环境变量
    timeout: 30
    retry: 3
  backup: "akshare"
  cache:
    enabled: true
    dir: "./cache"
    ttl:
      daily: 24
      minute: 1
      financial: 168

strategy:
  enabled_strategies:
    - name: "trend_following"
      enabled: true
      weight: 0.6
      params:
        ma_period: 250
        stop_loss: 0.08
    - name: "mean_reversion"
      enabled: true
      weight: 0.4
      params:
        rsi_period: 14
        oversold: 30

  fundamental:
    min_roe: 10
    max_debt_ratio: 50
    max_pe: 30
    min_market_cap: 50
    max_market_cap: 500

  technical:
    ma_period: 250
    macd_fast: 12
    macd_slow: 26
    macd_signal: 9

  volume:
    min_turnover_rate: 1
    max_turnover_rate: 5

schedule:
  daily_selection:
    time: "15:30"
    enabled: true

  minute_monitor:
    interval: 30  # 分钟
    timeframes: ["30min", "60min"]
    enabled: true

notification:
  wechat:
    enabled: true
    webhook: "${WECHAT_WEBHOOK}"
  email:
    enabled: false
```

### 8.2 配置加载器

```python
import yaml
import os
from typing import Any

class ConfigLoader:
    """配置加载器"""

    @staticmethod
    def load(config_path: str) -> Dict[str, Any]:
        """
        加载配置文件

        支持环境变量替换: ${VAR_NAME}
        """
        with open(config_path, 'r', encoding='utf-8') as f:
            config_str = f.read()

        # 替换环境变量
        config_str = ConfigLoader._replace_env_vars(config_str)

        # 解析 YAML
        config = yaml.safe_load(config_str)

        return config

    @staticmethod
    def _replace_env_vars(config_str: str) -> str:
        """替换环境变量"""
        import re

        pattern = r'\$\{([^}]+)\}'

        def replacer(match):
            var_name = match.group(1)
            return os.getenv(var_name, match.group(0))

        return re.sub(pattern, replacer, config_str)
```

---

## 9. 性能优化设计

### 9.1 并发处理

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

class ParallelProcessor:
    """并行处理器"""

    def __init__(self, max_workers: int = 5):
        self.max_workers = max_workers

    def process_stocks(self, stock_list: List[str],
                      process_func: callable) -> List[Any]:
        """
        并行处理股票列表

        Args:
            stock_list: 股票列表
            process_func: 处理函数

        Returns:
            List[Any]: 处理结果
        """
        results = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交任务
            future_to_stock = {
                executor.submit(process_func, stock): stock
                for stock in stock_list
            }

            # 收集结果
            for future in as_completed(future_to_stock):
                stock = future_to_stock[future]
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except Exception as e:
                    logger.error(f"处理 {stock} 失败: {e}")

        return results
```

### 9.2 缓存优化

```python
from functools import lru_cache

class CachedDataFetcher(DataFetcher):
    """带内存缓存的数据获取器"""

    @lru_cache(maxsize=1000)
    def get_stock_info(self, ts_code: str) -> Dict:
        """获取股票信息（内存缓存）"""
        return super().get_stock_info(ts_code)

    def clear_cache(self):
        """清理缓存"""
        self.get_stock_info.cache_clear()
```

---

## 10. 测试设计

### 10.1 单元测试

```python
import unittest
from unittest.mock import Mock, patch

class TestDataFetcher(unittest.TestCase):
    """数据获取器测试"""

    def setUp(self):
        self.fetcher = DataFetcher(
            token="test_token",
            use_cache=False
        )

    def test_get_daily_data(self):
        """测试获取日线数据"""
        df = self.fetcher.get_daily_data(
            "000001.SZ",
            start_date="20260101",
            end_date="20260131"
        )

        self.assertIsNotNone(df)
        self.assertFalse(df.empty)
        self.assertIn('close', df.columns)

    @patch('tushare.pro_api')
    def test_get_data_with_error(self, mock_api):
        """测试数据获取异常"""
        mock_api.return_value.daily.side_effect = Exception("API Error")

        with self.assertRaises(DataSourceError):
            self.fetcher.get_daily_data("000001.SZ")
```

### 10.2 集成测试

```python
class TestSignalGeneration(unittest.TestCase):
    """信号生成集成测试"""

    def setUp(self):
        self.config = load_config('config/test_config.yaml')
        self.fetcher = DataFetcher(self.config)
        self.engine = SignalEngine(self.config, self.fetcher)

        # 注册策略
        strategy = TrendFollowingStrategy(self.config)
        self.engine.register_strategy(strategy)

    def test_full_signal_flow(self):
        """测试完整信号流程"""
        # 1. 选股
        stock_pool = ['000001.SZ', '600036.SH']

        # 2. 生成信号
        signals = self.engine.scan_minute_signals(
            stock_pool,
            ['30min']
        )

        # 3. 验证
        self.assertIsInstance(signals, list)
        for signal in signals:
            self.assertIn('ts_code', signal)
            self.assertIn('signal_type', signal)
            self.assertIn('score', signal)
```

---

## 11. 部署设计

### 11.1 目录结构

```
signal_system/
├── config/
│   ├── config.yaml
│   └── config.prod.yaml
├── data/
│   └── data_fetcher.py
├── strategy/
│   ├── base_strategy.py
│   ├── trend_following.py
│   └── indicators.py
├── signals/
│   └── signal_engine.py
├── notification/
│   └── notifier.py
├── utils/
│   ├── helpers.py
│   └── logger.py
├── cache/
├── logs/
├── output/
├── main.py
├── requirements.txt
└── README.md
```

### 11.2 启动脚本

```bash
#!/bin/bash
# start.sh

# 激活虚拟环境
source venv/bin/activate

# 设置环境变量
export TUSHARE_TOKEN="your_token"
export WECHAT_WEBHOOK="your_webhook"

# 启动系统
nohup python main.py > logs/system.log 2>&1 &

echo "系统已启动，PID: $!"
```

---

## 12. 仓位管理模块设计 (新增)

> **重要**: 本模块是根据设计审查报告新增的核心模块，实现知识星球的"3只+25%机动"仓位管理理念

### 12.1 PositionManager 类

#### 12.1.1 类设计

```python
class PositionManager:
    """
    仓位管理器 - 实现知识星球的仓位管理理念

    核心原则:
    - 同时持有3只股票
    - 每只股票25%基本仓位
    - 保留25%机动资金用于做T
    - 最多允许临时持有4只(超配情况)
    """

    def __init__(self, total_capital: Decimal):
        self.total_capital = total_capital
        self.positions: Dict[str, Position] = {}
        self.mobile_cash_ratio = Decimal('0.25')
        self.base_position_ratio = Decimal('0.25')
        self.max_stocks = 3
        self.max_stocks_temp = 4

    def can_open_position(self, ts_code: str) -> Tuple[bool, str]:
        """检查是否可以开新仓位"""

    def calculate_position_size(self, ts_code: str,
                               position_type: str = 'base') -> int:
        """计算应该买入的股数"""

    def open_base_position(self, ts_code: str, price: Decimal) -> Position:
        """开基本仓位"""

    def add_mobile_position(self, ts_code: str, price: Decimal,
                           amount: int) -> bool:
        """增加机动仓位"""

    def reduce_mobile_position(self, ts_code: str, price: Decimal,
                              amount: int) -> bool:
        """减少机动仓位"""

    def reduce_base_position(self, ts_code: str, price: Decimal,
                            ratio: float) -> bool:
        """减少基本仓位"""

    def close_position(self, ts_code: str, price: Decimal) -> Position:
        """平仓"""

    def get_available_mobile_cash(self) -> Decimal:
        """获取可用机动资金"""

    def get_position_summary(self) -> Dict:
        """获取持仓汇总"""
```

#### 12.1.2 Position 数据结构

```python
@dataclass
class Position:
    """持仓信息"""
    ts_code: str
    stock_name: str

    # 基本仓位
    base_shares: int
    base_cost: Decimal
    base_amount: Decimal

    # 机动仓位
    mobile_shares: int
    mobile_cost: Decimal
    mobile_amount: Decimal

    # 统计信息
    total_shares: int
    total_cost: Decimal
    total_amount: Decimal
    current_price: Decimal
    market_value: Decimal
    profit_loss: Decimal
    profit_rate: Decimal

    buy_date: date
    holding_days: int

    def update_price(self, price: Decimal):
        """更新当前价格和盈亏"""
        self.current_price = price
        self.market_value = price * self.total_shares
        self.profit_loss = self.market_value - self.total_amount
        self.profit_rate = self.profit_loss / self.total_amount
```

#### 12.1.3 核心方法实现

```python
def can_open_position(self, ts_code: str) -> Tuple[bool, str]:
    """
    检查是否可以开新仓位

    Returns:
        (是否可以, 原因说明)
    """
    if ts_code in self.positions:
        return False, "已持有该股票"

    active_positions = len([p for p in self.positions.values()
                           if p.total_shares > 0])

    if active_positions >= self.max_stocks:
        return False, f"已达到最大持仓数量({self.max_stocks}只)"

    available_cash = self.get_available_cash()
    required_cash = self.total_capital * self.base_position_ratio

    if available_cash < required_cash:
        return False, f"可用资金不足"

    return True, "可以开仓"

def calculate_position_size(self, ts_code: str,
                           position_type: str = 'base') -> int:
    """
    计算应该买入的股数

    Args:
        ts_code: 股票代码
        position_type: 'base'(基本仓) 或 'mobile'(机动仓)

    Returns:
        应买入股数(手数的整数倍)
    """
    if position_type == 'base':
        amount = self.total_capital * self.base_position_ratio
    else:
        amount = self.get_available_mobile_cash()

    current_price = self._get_current_price(ts_code)
    shares = int(amount / current_price / 100) * 100

    return shares

def get_available_mobile_cash(self) -> Decimal:
    """
    获取可用机动资金

    Returns:
        可用机动资金 = 总机动资金 - 已使用机动资金
    """
    total_mobile = self.total_capital * self.mobile_cash_ratio
    used_mobile = sum(p.mobile_amount for p in self.positions.values())
    return total_mobile - used_mobile
```

---

## 13. 做T策略模块设计 (新增)

> **重要**: 本模块是根据设计审查报告新增的核心模块，实现知识星球的做T理念

### 13.1 TTradingStrategy 类

#### 13.1.1 类设计

```python
class TTradingStrategy:
    """
    做T策略 - 实现知识星球的做T理念

    核心原则:
    - 上涨趋势: 正T(底背离加机动仓, 顶背离减机动仓)
    - 下跌趋势: 反T(顶背离减基本仓, 回落买回)
    - 震荡市: 机动资金高抛低吸
    """

    def __init__(self, position_manager: PositionManager, config: Dict):
        self.position_manager = position_manager
        self.config = config

    def analyze_t_opportunity(self, ts_code: str, market_trend: str,
                             timeframe: str = '30min') -> Dict:
        """分析做T机会"""

    def execute_positive_t(self, ts_code: str, signal_type: str) -> bool:
        """执行正T"""

    def execute_negative_t(self, ts_code: str, signal_type: str) -> bool:
        """执行反T"""

    def execute_range_t(self, ts_code: str, signal_type: str) -> bool:
        """执行震荡市做T"""
```

#### 13.1.2 TSignal 数据结构

```python
@dataclass
class TSignal:
    """做T信号"""
    ts_code: str
    signal_type: str  # 'positive_t_buy', 'positive_t_sell',
                      # 'negative_t_sell', 'negative_t_buy'
    timeframe: str
    price: Decimal
    amount: int
    reason: str
    confidence: int
    indicators: Dict
    timestamp: datetime
```

#### 13.1.3 核心方法实现

```python
def analyze_t_opportunity(self, ts_code: str, market_trend: str,
                         timeframe: str = '30min') -> Dict:
    """
    分析做T机会

    Args:
        ts_code: 股票代码
        market_trend: 市场趋势 ('bull', 'bear', 'range')
        timeframe: 时间周期

    Returns:
        {
            'has_opportunity': bool,
            'signal_type': str,
            'reason': str,
            'confidence': int,
            'suggested_amount': int
        }
    """
    if ts_code not in self.position_manager.positions:
        return {'has_opportunity': False, 'reason': '未持仓'}

    position = self.position_manager.positions[ts_code]
    indicators = self._calculate_indicators(ts_code, timeframe)

    if market_trend == 'bull':
        return self._analyze_positive_t(position, indicators)
    elif market_trend == 'bear':
        return self._analyze_negative_t(position, indicators)
    else:
        return self._analyze_range_t(position, indicators)

def _analyze_positive_t(self, position: Position,
                       indicators: Dict) -> Dict:
    """
    分析正T机会(上涨趋势)

    正T逻辑:
    - 买入时机: 底背离 + 回调至支撑位 + 放量
    - 卖出时机: 顶背离 + 短期冲高 + 缩量
    - 操作对象: 机动仓位
    """
    result = {
        'has_opportunity': False,
        'signal_type': None,
        'reason': '',
        'confidence': 0,
        'suggested_amount': 0
    }

    # 检查买入机会(加机动仓)
    if self._has_bullish_divergence(indicators):
        if self._is_near_support(indicators):
            if self._is_volume_increasing(indicators):
                available_cash = self.position_manager.get_available_mobile_cash()
                if available_cash > 0:
                    result['has_opportunity'] = True
                    result['signal_type'] = 'positive_t_buy'
                    result['reason'] = '底背离+支撑位+放量,加机动仓'
                    result['confidence'] = 80
                    result['suggested_amount'] = int(available_cash * 0.5)

    # 检查卖出机会(减机动仓)
    elif self._has_bearish_divergence(indicators):
        if position.mobile_shares > 0:
            if self._is_volume_decreasing(indicators):
                result['has_opportunity'] = True
                result['signal_type'] = 'positive_t_sell'
                result['reason'] = '顶背离+缩量,减机动仓'
                result['confidence'] = 75
                result['suggested_amount'] = position.mobile_shares

    return result

def _analyze_negative_t(self, position: Position,
                       indicators: Dict) -> Dict:
    """
    分析反T机会(下跌趋势)

    反T逻辑:
    - 卖出时机: 顶背离 + 反弹至阻力位
    - 买入时机: 快速下跌后企稳 + 底背离
    - 操作对象: 部分基本仓位
    """
    result = {
        'has_opportunity': False,
        'signal_type': None,
        'reason': '',
        'confidence': 0,
        'suggested_amount': 0
    }

    # 检查卖出机会(减基本仓)
    if self._has_bearish_divergence(indicators):
        if self._is_near_resistance(indicators):
            if position.base_shares > 0:
                result['has_opportunity'] = True
                result['signal_type'] = 'negative_t_sell'
                result['reason'] = '顶背离+阻力位,减部分基本仓'
                result['confidence'] = 70
                result['suggested_amount'] = int(position.base_shares * 0.3)

    # 检查买入机会(买回基本仓)
    elif self._has_bullish_divergence(indicators):
        if self._is_stabilizing(indicators):
            target_base = self.position_manager.calculate_position_size(
                position.ts_code, 'base'
            )
            shortage = target_base - position.base_shares

            if shortage > 0:
                result['has_opportunity'] = True
                result['signal_type'] = 'negative_t_buy'
                result['reason'] = '底背离+企稳,买回基本仓'
                result['confidence'] = 75
                result['suggested_amount'] = shortage

    return result
```

---

## 14. 背离检测算法修正 (Critical)

> **重要**: 原算法只比较峰值，现修正为计算MACD柱子面积

### 14.1 修正后的算法

```python
def detect_divergence(self, price: pd.Series, macd_hist: pd.Series,
                     divergence_type: str = 'bullish') -> Tuple[bool, Dict]:
    """
    检测背离 - 使用面积计算法

    Args:
        price: 价格序列
        macd_hist: MACD柱子序列
        divergence_type: 'bullish'(底背离) 或 'bearish'(顶背离)

    Returns:
        (是否背离, 详细信息)
    """
    # 1. 找出所有波段
    segments = self._find_macd_segments(macd_hist, divergence_type)

    if len(segments) < 2:
        return False, {}

    # 2. 取最近两个波段
    prev_segment = segments[-2]
    last_segment = segments[-1]

    # 3. 计算MACD柱子面积
    prev_area = self._calculate_macd_area(
        macd_hist,
        prev_segment['start'],
        prev_segment['end']
    )
    last_area = self._calculate_macd_area(
        macd_hist,
        last_segment['start'],
        last_segment['end']
    )

    # 4. 比较价格和面积
    if divergence_type == 'bullish':
        # 底背离: 价格新低 + MACD面积缩小
        price_lower = price[last_segment['trough']] < price[prev_segment['trough']]
        area_smaller = last_area < prev_area
        is_divergence = price_lower and area_smaller
    else:
        # 顶背离: 价格新高 + MACD面积缩小
        price_higher = price[last_segment['peak']] > price[prev_segment['peak']]
        area_smaller = last_area < prev_area
        is_divergence = price_higher and area_smaller

    detail = {
        'prev_area': prev_area,
        'last_area': last_area,
        'area_ratio': last_area / prev_area if prev_area > 0 else 0,
        'prev_segment': prev_segment,
        'last_segment': last_segment
    }

    return is_divergence, detail

def _calculate_macd_area(self, hist: pd.Series,
                        start_idx: int, end_idx: int) -> float:
    """
    计算MACD柱子面积

    面积 = Σ|hist[i]| for i in [start_idx, end_idx]
    """
    area = 0.0
    for i in range(start_idx, end_idx + 1):
        area += abs(hist.iloc[i])
    return area

def _find_macd_segments(self, hist: pd.Series,
                       segment_type: str) -> List[Dict]:
    """
    找出MACD柱子的波段

    Args:
        hist: MACD柱子序列
        segment_type: 'bullish'(绿柱波段) 或 'bearish'(红柱波段)

    Returns:
        波段列表, 每个波段包含 {start, end, peak/trough}
    """
    segments = []
    in_segment = False
    segment_start = None

    for i in range(len(hist)):
        if segment_type == 'bullish':
            # 寻找绿柱波段(hist < 0)
            if hist.iloc[i] < 0:
                if not in_segment:
                    segment_start = i
                    in_segment = True
            else:
                if in_segment:
                    segment_end = i - 1
                    trough_idx = hist.iloc[segment_start:segment_end+1].idxmin()
                    segments.append({
                        'start': segment_start,
                        'end': segment_end,
                        'trough': trough_idx
                    })
                    in_segment = False
        else:
            # 寻找红柱波段(hist > 0)
            if hist.iloc[i] > 0:
                if not in_segment:
                    segment_start = i
                    in_segment = True
            else:
                if in_segment:
                    segment_end = i - 1
                    peak_idx = hist.iloc[segment_start:segment_end+1].idxmax()
                    segments.append({
                        'start': segment_start,
                        'end': segment_end,
                        'peak': peak_idx
                    })
                    in_segment = False

    return segments
```

---

## 15. 空头陷阱识别模块 (新增)

### 15.1 BearTrapDetector 类

```python
class BearTrapDetector:
    """
    空头陷阱识别器

    识别条件:
    1. 年线向上
    2. 短期跌破年线
    3. 快速收回(3-5天内)
    4. 出现底背离
    """

    def detect(self, df: pd.DataFrame, indicators: Dict) -> Tuple[bool, str]:
        """
        检测空头陷阱

        Returns:
            (是否是空头陷阱, 详细说明)
        """
        reasons = []

        # 1. 检查年线趋势
        if not self._check_ma250_uptrend(df):
            return False, "年线未向上"
        reasons.append("年线向上")

        # 2. 检查是否短期跌破年线
        if not self._check_recent_break_below_ma(df, days=5):
            return False, "未发现短期跌破年线"
        reasons.append("短期跌破年线")

        # 3. 检查是否快速收回
        if not self._check_quick_recovery(df, days=5):
            return False, "未快速收回"
        reasons.append("快速收回")

        # 4. 检查底背离
        if not self._check_bullish_divergence(indicators):
            return False, "无底背离"
        reasons.append("出现底背离")

        return True, " + ".join(reasons)

    def _check_ma250_uptrend(self, df: pd.DataFrame) -> bool:
        """检查年线是否向上"""
        ma250 = df['ma250'].tail(20)
        slope = (ma250.iloc[-1] - ma250.iloc[0]) / len(ma250)
        return slope > 0

    def _check_recent_break_below_ma(self, df: pd.DataFrame,
                                    days: int = 5) -> bool:
        """检查最近是否跌破年线"""
        recent = df.tail(days)
        return any(recent['close'] < recent['ma250'])

    def _check_quick_recovery(self, df: pd.DataFrame,
                             days: int = 5) -> bool:
        """检查是否快速收回"""
        recent = df.tail(days)
        return recent['close'].iloc[-1] > recent['ma250'].iloc[-1]
```

---

## 16. 配置参数修正

### 16.1 修正后的 config.yaml

```yaml
# 仓位管理配置
position:
  stock_count: 3
  base_position_per_stock: 0.25
  mobile_cash_ratio: 0.25
  max_stocks_temp: 4

# 基本面筛选配置
fundamental:
  roe_min: 0.10
  debt_ratio_max: 0.50
  pe_excellent: 17
  pe_acceptable: 30
  market_cap_min: 5000000000
  market_cap_max: 50000000000

# 成交量筛选配置
volume:
  turnover_rate_min: 0.01
  turnover_rate_max: 0.03
  accumulation_turnover_min: 0.05
  accumulation_turnover_max: 0.10

# 做T策略配置
t_trading:
  enabled: true
  positive_t:
    buy_confidence_threshold: 75
    sell_confidence_threshold: 70
    mobile_ratio: 0.5
  negative_t:
    sell_confidence_threshold: 70
    buy_confidence_threshold: 75
    base_reduce_ratio: 0.3
```

---

## 17. 修正后的项目结构

```
signal_system/
├── data/
│   ├── data_fetcher.py
│   ├── data_cache.py
│   └── data_cleaner.py
├── strategy/
│   ├── base_strategy.py
│   ├── stock_selector.py
│   ├── signal_engine.py
│   ├── indicators.py (修正背离算法)
│   ├── trend_following_strategy.py
│   ├── bear_trap_detector.py (新增)
│   └── volume_price_analyzer.py
├── position/ (新增模块)
│   ├── __init__.py
│   ├── position_manager.py (新增)
│   ├── position.py (新增)
│   └── t_trading_strategy.py (新增)
├── service/
│   ├── notifier.py
│   ├── logger.py
│   └── storage.py
├── config/
│   └── config.yaml (修正参数)
├── tests/
├── cache/
├── logs/
├── output/
├── main.py
├── requirements.txt
└── README.md
```

---

## 18. 总结

本详细设计文档涵盖了第一阶段 Python 核心引擎的所有关键设计：

1. **数据层**: 完整的数据获取和缓存机制
2. **策略层**: 模块化的策略框架和具体实现
3. **信号层**: 分钟级信号监控和生成
4. **仓位管理层** (新增): 3只+25%机动的仓位管理
5. **做T策略层** (新增): 正T、反T、震荡市做T
6. **服务层**: 通知推送和日志管理
7. **异常处理**: 完善的异常处理和降级机制
8. **性能优化**: 并发处理和缓存优化
9. **算法修正**: 背离检测改为面积计算
10. **测试**: 单元测试和集成测试
11. **部署**: 完整的部署方案

### 18.1 与审查报告的对应

✅ **Critical问题已全部修正**:
1. ✅ 新增 PositionManager 类 - 实现3只+25%机动
2. ✅ 新增 TTradingStrategy 类 - 实现正T和反T
3. ✅ 修正背离检测算法 - 改为面积计算

✅ **Warning问题已全部修正**:
4. ✅ 新增 BearTrapDetector 类 - 空头陷阱识别
5. ✅ 修正配置参数 - PE分级、换手率范围

**下一步**: 基于本详细设计编写实施计划

---

**文档版本**: v2.0 (已根据审查报告修正)
**文档状态**: ✅ 已完成
**审核状态**: 符合知识星球理念
**下一步**: 编写实施计划

