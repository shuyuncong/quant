# 量化交易系统实施计划 (续)

## Phase 3: 回测引擎集成 (续)

### Task 8: 回测引擎封装

**Files:**
- Create: `backtest/engine/bt_engine.py`
- Create: `tests/backtest/test_bt_engine.py`

**Step 1: 编写回测引擎测试**

```python
# tests/backtest/test_bt_engine.py
import pytest
import pandas as pd
from backtest.engine.bt_engine import BacktestEngine

def test_prepare_data():
    engine = BacktestEngine()
    df = pd.DataFrame({
        'date': pd.date_range('2026-01-01', periods=100),
        'open': [10.0] * 100,
        'high': [10.5] * 100,
        'low': [9.5] * 100,
        'close': [10.0] * 100,
        'volume': [1000000] * 100
    })

    prepared = engine.prepare_data(df)
    assert 'Open' in prepared.columns
    assert prepared.index.name == 'date'
```

**Step 2: 运行测试验证失败**

```bash
pytest tests/backtest/test_bt_engine.py -v
```

**Step 3: 实现BacktestEngine类**

```python
# backtest/engine/bt_engine.py
import pandas as pd
from backtesting import Backtest, Strategy
from typing import Type, Dict, Any

class BacktestEngine:
    """回测引擎封装"""

    def __init__(self, cash: float = 100000, commission: float = 0.001):
        self.cash = cash
        self.commission = commission

    def prepare_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """准备回测数据"""
        data = df.copy()
        column_mapping = {
            'open': 'Open',
            'high': 'High',
            'low': 'Low',
            'close': 'Close',
            'volume': 'Volume'
        }
        data = data.rename(columns=column_mapping)

        if 'date' in data.columns:
            data['date'] = pd.to_datetime(data['date'])
            data = data.set_index('date')

        return data

    def run(self, data: pd.DataFrame, strategy_class: Type[Strategy],
            strategy_params: Dict[str, Any] = None) -> Dict:
        """运行回测"""
        prepared_data = self.prepare_data(data)

        bt = Backtest(
            prepared_data,
            strategy_class,
            cash=self.cash,
            commission=self.commission
        )

        if strategy_params:
            stats = bt.run(**strategy_params)
        else:
            stats = bt.run()

        return {'stats': stats, 'backtest': bt}
```

**Step 4: 运行测试验证通过**

```bash
pytest tests/backtest/test_bt_engine.py -v
```

**Step 5: Commit**

```bash
git add backtest/engine/bt_engine.py tests/backtest/test_bt_engine.py
git commit -m "feat(backtest): 实现回测引擎封装"
```

---

### Task 9: 趋势跟踪回测策略

**Files:**
- Create: `backtest/strategies/trend_following_bt.py`

**Step 1: 实现策略**

```python
# backtest/strategies/trend_following_bt.py
from backtesting import Strategy
from backtesting.lib import crossover
import talib

class TrendFollowingStrategy(Strategy):
    """趋势跟踪策略"""

    ma_period = 250
    stop_loss_pct = 0.08
    stop_profit_pct = 0.30

    def init(self):
        self.ma250 = self.I(talib.SMA, self.data.Close, self.ma_period)
        macd, signal, hist = talib.MACD(self.data.Close, 12, 26, 9)
        self.macd = self.I(lambda: macd)
        self.macd_signal = self.I(lambda: signal)

    def next(self):
        price = self.data.Close[-1]
        ma250 = self.ma250[-1]

        if not self.position:
            if len(self.ma250) > 10:
                ma_uptrend = self.ma250[-1] > self.ma250[-10]
                near_ma = abs(price - ma250) / ma250 < 0.05
                macd_cross = crossover(self.macd, self.macd_signal)

                if ma_uptrend and near_ma and macd_cross:
                    self.buy(size=0.25)
        else:
            if price < self.position.entry_price * (1 - self.stop_loss_pct):
                self.position.close()
            elif price > self.position.entry_price * (1 + self.stop_profit_pct):
                self.position.close()
            elif crossover(self.macd_signal, self.macd):
                self.position.close()
```

**Step 2: Commit**

```bash
git add backtest/strategies/trend_following_bt.py
git commit -m "feat(backtest): 实现趋势跟踪回测策略"
```

---

## Phase 4: 系统集成

### Task 10: 配置文件更新

**Files:**
- Modify: `quant-python/signal_system/config/config.yaml`

**Step 1: 添加配置**

```yaml
# 仓位管理配置
position:
  total_capital: 100000
  stock_count: 3
  base_position_per_stock: 0.25
  mobile_cash_ratio: 0.25

# 做T策略配置
t_trading:
  enabled: true
  positive_t:
    buy_confidence_threshold: 75
    sell_confidence_threshold: 70
  negative_t:
    sell_confidence_threshold: 70
    buy_confidence_threshold: 75

# 回测配置
backtest:
  initial_cash: 100000
  commission: 0.001
  start_date: "2023-01-01"
  end_date: "2025-12-31"
```

**Step 2: Commit**

```bash
git add quant-python/signal_system/config/config.yaml
git commit -m "feat(config): 添加仓位管理和回测配置"
```

---

### Task 11: 创建回测主程序

**Files:**
- Create: `backtest/backtest_main.py`

**Step 1: 编写主程序**

```python
# backtest/backtest_main.py
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from backtest.engine.bt_engine import BacktestEngine
from backtest.strategies.trend_following_bt import TrendFollowingStrategy

def main():
    print("=" * 60)
    print("量化交易系统 - 回测模块")
    print("=" * 60)

    # TODO: 从配置文件读取参数
    # TODO: 获取历史数据
    # TODO: 运行回测
    # TODO: 输出结果

    print("\n回测完成!")

if __name__ == "__main__":
    main()
```

**Step 2: Commit**

```bash
git add backtest/backtest_main.py
git commit -m "feat(backtest): 创建回测主程序框架"
```

---

## Phase 5: 与现有设计文档对齐

### Task 12: 对齐数据层设计

**参考文档:** `doc/设计文档/第一阶段-Python核心引擎-详细设计.md`

**Files:**
- Review: `quant-python/signal_system/data/data_fetcher.py`
- Ensure: 实现符合详细设计文档中的DataFetcher类设计

**Step 1: 检查现有实现**

阅读现有data_fetcher.py,对比设计文档中的接口定义。

**Step 2: 补充缺失方法**

如果缺少设计文档中定义的方法,补充实现:
- `get_financial_data()`
- `get_daily_basic()`
- `get_index_daily()`

**Step 3: Commit**

```bash
git add quant-python/signal_system/data/data_fetcher.py
git commit -m "feat(data): 对齐DataFetcher与设计文档"
```

---

### Task 13: 对齐策略层设计

**参考文档:** `doc/设计文档/设计修正方案.md`

**Files:**
- Review: `quant-python/signal_system/strategy/strategy_engine.py`
- Integrate: 核心模块到现有策略引擎

**Step 1: 导入核心模块**

```python
from core.position.position_manager import PositionManager
from core.position.t_trading import TTradingStrategy
from core.indicators.divergence import DivergenceDetector
from core.detectors.bear_trap import BearTrapDetector
```

**Step 2: 修改StrategyEngine**

在StrategyEngine.__init__中添加:

```python
def __init__(self, config):
    self.config = config

    # 初始化仓位管理器
    from decimal import Decimal
    total_capital = Decimal(str(config.get('position', {}).get('total_capital', 100000)))
    self.position_manager = PositionManager(total_capital)

    # 初始化做T策略
    self.t_trading = TTradingStrategy(
        self.position_manager,
        config.get('t_trading', {})
    )

    # 初始化检测器
    self.divergence_detector = DivergenceDetector()
    self.bear_trap_detector = BearTrapDetector()
```

**Step 3: Commit**

```bash
git add quant-python/signal_system/strategy/strategy_engine.py
git commit -m "feat(strategy): 集成核心模块到策略引擎"
```

---

### Task 14: 对齐参数配置

**参考文档:** `doc/设计文档/设计修正方案.md` 第4节

**Files:**
- Modify: `quant-python/signal_system/config/config.yaml`

**Step 1: 更新参数**

确保config.yaml包含设计修正方案中的所有参数:

```yaml
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
  turnover_rate_max: 0.03  # 修正: 从0.05改为0.03
  accumulation_turnover_min: 0.05
  accumulation_turnover_max: 0.10
```

**Step 2: Commit**

```bash
git add quant-python/signal_system/config/config.yaml
git commit -m "feat(config): 对齐参数配置与设计修正方案"
```

---

## Phase 6: 文档和测试

### Task 15: 更新README

**Files:**
- Modify: `README.md`

**Step 1: 添加新功能说明**

在README.md中添加:

```markdown
## 新增功能

### 核心模块

- **仓位管理**: 基本仓(75%) + 机动仓(25%)模式
- **做T策略**: 正T(牛市) / 反T(熊市) / 震荡T
- **面积法背离检测**: 更准确的MACD背离识别
- **空头陷阱检测**: 年线附近的假突破识别

### 回测系统

- 基于Backtesting.py的轻量级回测引擎
- 支持多种策略回测
- 参数优化功能
- 可视化回测报告

## 使用方法

### 实时信号系统

```bash
cd quant-python/signal_system
python main.py
```

### 回测系统

```bash
cd backtest
python backtest_main.py
```
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: 更新README添加新功能说明"
```

---

### Task 16: 编写集成测试

**Files:**
- Create: `tests/integration/test_full_workflow.py`

**Step 1: 编写测试**

```python
# tests/integration/test_full_workflow.py
import pytest
from decimal import Decimal
import pandas as pd
from core.position.position_manager import PositionManager
from core.position.t_trading import TTradingStrategy
from core.indicators.divergence import DivergenceDetector

def test_full_workflow():
    """测试完整工作流"""
    # 1. 初始化
    pm = PositionManager(total_capital=Decimal("100000"))

    # 2. 开仓
    position = pm.open_base_position("000001.SZ", "测试股票", Decimal("12.50"))
    assert position.base_shares == 2000

    # 3. 做T分析
    t_strategy = TTradingStrategy(pm)
    indicators = {
        'has_bullish_divergence': True,
        'is_near_support': True,
        'volume_increasing': True
    }
    result = t_strategy.analyze_t_opportunity("000001.SZ", "bull", indicators)
    assert result['has_opportunity'] is True

    print("✅ 集成测试通过!")
```

**Step 2: 运行测试**

```bash
pytest tests/integration/test_full_workflow.py -v
```

**Step 3: Commit**

```bash
git add tests/integration/test_full_workflow.py
git commit -m "test: 添加完整工作流集成测试"
```

---

## 总结

### 已完成模块

1. ✅ Position持仓数据结构
2. ✅ PositionManager仓位管理器
3. ✅ DivergenceDetector面积法背离检测
4. ✅ BearTrapDetector空头陷阱检测
5. ✅ TTradingStrategy做T策略
6. ✅ BacktestEngine回测引擎
7. ✅ TrendFollowingStrategy回测策略
8. ✅ 配置文件更新
9. ✅ 与现有设计对齐
10. ✅ 文档和测试

### 架构优势

- **模块化**: 核心模块独立,可复用
- **兼容性**: 完全兼容现有实时信号系统
- **灵活性**: 回测和实时系统共享策略逻辑
- **可扩展**: 易于添加新策略和指标

### 下一步工作

1. 实现更多回测策略(均值回归、突破)
2. 参数优化和性能调优
3. 添加更多技术指标
4. 实盘对接准备
5. 监控告警系统

---

**实施计划已完成!**
