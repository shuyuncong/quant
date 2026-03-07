# 量化交易系统实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 构建混合架构的量化交易系统,集成Backtesting.py回测引擎和自定义核心策略模块,实现基本仓+机动仓管理、正T/反T策略、面积法背离检测等核心功能。

**Architecture:** 保留现有的实时信号系统(data/strategy/notification层),新增独立的core模块(仓位管理、技术指标、信号检测器)供两系统共享,新增backtest模块封装Backtesting.py进行历史回测验证。

**Tech Stack:** Python 3.9+, Pandas, Numpy, TA-Lib, Backtesting.py, Tushare Pro, PyYAML

---

## Phase 1: 核心模块基础设施

### Task 1: 项目结构重组

**Files:**
- Create: `core/__init__.py`
- Create: `core/position/__init__.py`
- Create: `core/indicators/__init__.py`
- Create: `core/detectors/__init__.py`
- Create: `backtest/__init__.py`
- Create: `backtest/engine/__init__.py`
- Create: `backtest/strategies/__init__.py`

**Step 1: 创建目录结构**

```bash
mkdir -p core/position core/indicators core/detectors
mkdir -p backtest/engine backtest/strategies
mkdir -p tests/core/position tests/core/indicators tests/core/detectors
mkdir -p tests/backtest
```

**Step 2: 创建__init__.py文件**

在每个新目录下创建空的`__init__.py`文件。

**Step 3: 验证导入**

```python
# 测试导入
import core
import core.position
import core.indicators
import core.detectors
import backtest
```

**Step 4: Commit**

```bash
git add core/ backtest/ tests/
git commit -m "feat: 创建核心模块和回测模块目录结构"
```

---

### Task 2: Position数据结构

**Files:**
- Create: `core/position/position.py`
- Create: `tests/core/position/test_position.py`

**Step 1: 编写Position数据类测试**

```python
# tests/core/position/test_position.py
import pytest
from decimal import Decimal
from datetime import date
from core.position.position import Position

def test_position_creation():
    """测试持仓创建"""
    pos = Position(
        ts_code="000001.SZ",
        stock_name="平安银行",
        base_shares=1000,
        base_cost=Decimal("12.50"),
        mobile_shares=0,
        mobile_cost=Decimal("0"),
        buy_date=date(2026, 3, 1)
    )

    assert pos.ts_code == "000001.SZ"
    assert pos.total_shares == 1000
    assert pos.total_cost == Decimal("12.50")
    assert pos.total_amount == Decimal("12500.00")

def test_position_update_price():
    """测试更新价格和盈亏计算"""
    pos = Position(
        ts_code="000001.SZ",
        stock_name="平安银行",
        base_shares=1000,
        base_cost=Decimal("12.50"),
        mobile_shares=0,
        mobile_cost=Decimal("0"),
        buy_date=date(2026, 3, 1)
    )

    pos.update_price(Decimal("13.75"))

    assert pos.current_price == Decimal("13.75")
    assert pos.market_value == Decimal("13750.00")
    assert pos.profit_loss == Decimal("1250.00")
    assert pos.profit_rate == Decimal("0.10")

def test_position_add_mobile():
    """测试添加机动仓"""
    pos = Position(
        ts_code="000001.SZ",
        stock_name="平安银行",
        base_shares=1000,
        base_cost=Decimal("12.50"),
        mobile_shares=0,
        mobile_cost=Decimal("0"),
        buy_date=date(2026, 3, 1)
    )

    pos.add_mobile_shares(500, Decimal("12.00"))

    assert pos.mobile_shares == 500
    assert pos.total_shares == 1500
    assert pos.mobile_cost == Decimal("12.00")
```

**Step 2: 运行测试验证失败**

```bash
pytest tests/core/position/test_position.py -v
```

Expected: FAIL with "ModuleNotFoundError: No module named 'core.position.position'"

**Step 3: 实现Position类**

```python
# core/position/position.py
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date, datetime

@dataclass
class Position:
    """持仓信息"""
    ts_code: str
    stock_name: str

    # 基本仓位
    base_shares: int
    base_cost: Decimal

    # 机动仓位
    mobile_shares: int
    mobile_cost: Decimal

    # 买入日期
    buy_date: date

    # 当前价格和市值(动态更新)
    current_price: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    profit_loss: Decimal = Decimal("0")
    profit_rate: Decimal = Decimal("0")

    def __post_init__(self):
        """初始化计算字段"""
        self.update_totals()

    @property
    def total_shares(self) -> int:
        """总股数"""
        return self.base_shares + self.mobile_shares

    @property
    def total_cost(self) -> Decimal:
        """平均成本"""
        if self.total_shares == 0:
            return Decimal("0")
        total_amount = (self.base_shares * self.base_cost +
                       self.mobile_shares * self.mobile_cost)
        return total_amount / self.total_shares

    @property
    def base_amount(self) -> Decimal:
        """基本仓位金额"""
        return self.base_shares * self.base_cost

    @property
    def mobile_amount(self) -> Decimal:
        """机动仓位金额"""
        return self.mobile_shares * self.mobile_cost

    @property
    def total_amount(self) -> Decimal:
        """总金额"""
        return self.base_amount + self.mobile_amount

    @property
    def holding_days(self) -> int:
        """持仓天数"""
        return (date.today() - self.buy_date).days

    def update_price(self, price: Decimal):
        """更新当前价格和盈亏"""
        self.current_price = price
        self.market_value = price * self.total_shares
        self.profit_loss = self.market_value - self.total_amount
        if self.total_amount > 0:
            self.profit_rate = self.profit_loss / self.total_amount

    def add_mobile_shares(self, shares: int, cost: Decimal):
        """添加机动仓位"""
        if self.mobile_shares == 0:
            self.mobile_cost = cost
        else:
            # 加权平均成本
            total_amount = self.mobile_amount + shares * cost
            self.mobile_cost = total_amount / (self.mobile_shares + shares)

        self.mobile_shares += shares
        self.update_totals()

    def reduce_mobile_shares(self, shares: int) -> Decimal:
        """减少机动仓位,返回卖出金额"""
        if shares > self.mobile_shares:
            raise ValueError(f"减仓数量({shares})超过机动仓位({self.mobile_shares})")

        sell_amount = shares * self.current_price
        self.mobile_shares -= shares

        if self.mobile_shares == 0:
            self.mobile_cost = Decimal("0")

        self.update_totals()
        return sell_amount

    def reduce_base_shares(self, shares: int) -> Decimal:
        """减少基本仓位,返回卖出金额"""
        if shares > self.base_shares:
            raise ValueError(f"减仓数量({shares})超过基本仓位({self.base_shares})")

        sell_amount = shares * self.current_price
        self.base_shares -= shares

        if self.base_shares == 0:
            self.base_cost = Decimal("0")

        self.update_totals()
        return sell_amount

    def update_totals(self):
        """更新总计字段"""
        if self.current_price > 0:
            self.update_price(self.current_price)
```

**Step 4: 运行测试验证通过**

```bash
pytest tests/core/position/test_position.py -v
```

Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add core/position/position.py tests/core/position/test_position.py
git commit -m "feat(position): 实现Position持仓数据结构"
```

---

### Task 3: PositionManager仓位管理器

**Files:**
- Create: `core/position/position_manager.py`
- Create: `tests/core/position/test_position_manager.py`

**Step 1: 编写PositionManager测试**

```python
# tests/core/position/test_position_manager.py
import pytest
from decimal import Decimal
from core.position.position_manager import PositionManager

def test_position_manager_init():
    """测试仓位管理器初始化"""
    pm = PositionManager(total_capital=Decimal("100000"))

    assert pm.total_capital == Decimal("100000")
    assert pm.mobile_cash_ratio == Decimal("0.25")
    assert pm.base_position_ratio == Decimal("0.25")
    assert pm.max_stocks == 3
    assert len(pm.positions) == 0

def test_can_open_position():
    """测试是否可以开仓"""
    pm = PositionManager(total_capital=Decimal("100000"))

    # 第一只股票可以开仓
    can_open, reason = pm.can_open_position("000001.SZ")
    assert can_open is True

    # 开3只后不能再开
    pm.open_base_position("000001.SZ", "平安银行", Decimal("12.50"))
    pm.open_base_position("600036.SH", "招商银行", Decimal("35.80"))
    pm.open_base_position("601318.SH", "中国平安", Decimal("45.20"))

    can_open, reason = pm.can_open_position("000002.SZ")
    assert can_open is False
    assert "最大持仓数量" in reason

def test_calculate_position_size():
    """测试计算仓位大小"""
    pm = PositionManager(total_capital=Decimal("100000"))

    # 基本仓位 = 100000 * 0.25 = 25000
    # 价格12.50, 股数 = 25000 / 12.50 / 100 * 100 = 2000
    shares = pm.calculate_position_size(Decimal("12.50"), "base")
    assert shares == 2000

def test_open_base_position():
    """测试开基本仓位"""
    pm = PositionManager(total_capital=Decimal("100000"))

    position = pm.open_base_position("000001.SZ", "平安银行", Decimal("12.50"))

    assert position.ts_code == "000001.SZ"
    assert position.base_shares == 2000
    assert position.mobile_shares == 0
    assert "000001.SZ" in pm.positions

def test_get_available_mobile_cash():
    """测试获取可用机动资金"""
    pm = PositionManager(total_capital=Decimal("100000"))

    # 初始机动资金 = 100000 * 0.25 = 25000
    assert pm.get_available_mobile_cash() == Decimal("25000")

    # 开一个基本仓后,机动资金不变
    pm.open_base_position("000001.SZ", "平安银行", Decimal("12.50"))
    assert pm.get_available_mobile_cash() == Decimal("25000")
```

**Step 2: 运行测试验证失败**

```bash
pytest tests/core/position/test_position_manager.py -v
```

Expected: FAIL

**Step 3: 实现PositionManager类(第一部分)**

```python
# core/position/position_manager.py
from decimal import Decimal
from typing import Dict, Tuple, Optional
from datetime import date
from .position import Position

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
        """
        初始化仓位管理器

        Args:
            total_capital: 总资金
        """
        self.total_capital = total_capital
        self.positions: Dict[str, Position] = {}

        # 仓位配置
        self.mobile_cash_ratio = Decimal("0.25")  # 机动资金比例
        self.base_position_ratio = Decimal("0.25")  # 单只基本仓位比例
        self.max_stocks = 3  # 最大持仓数量
        self.max_stocks_temp = 4  # 临时最大持仓数量

        # 资金统计
        self._used_base_capital = Decimal("0")  # 已用基本仓位资金
        self._used_mobile_capital = Decimal("0")  # 已用机动资金

    def can_open_position(self, ts_code: str) -> Tuple[bool, str]:
        """
        检查是否可以开新仓位

        Returns:
            (是否可以, 原因说明)
        """
        # 1. 检查是否已持有
        if ts_code in self.positions:
            return False, "已持有该股票"

        # 2. 检查持仓数量限制
        active_positions = len([p for p in self.positions.values()
                               if p.total_shares > 0])

        if active_positions >= self.max_stocks:
            return False, f"已达到最大持仓数量({self.max_stocks}只)"

        # 3. 检查可用资金
        available_cash = self.get_available_base_cash()
        required_cash = self.total_capital * self.base_position_ratio

        if available_cash < required_cash:
            return False, f"可用资金不足(需要{required_cash}, 可用{available_cash})"

        return True, "可以开仓"

    def calculate_position_size(self, price: Decimal,
                               position_type: str = 'base') -> int:
        """
        计算应该买入的股数

        Args:
            price: 当前价格
            position_type: 'base'(基本仓) 或 'mobile'(机动仓)

        Returns:
            应买入股数(手数的整数倍)
        """
        if position_type == 'base':
            # 基本仓位 = 总资金 * 25%
            amount = self.total_capital * self.base_position_ratio
        else:
            # 机动仓位 = 可用机动资金
            amount = self.get_available_mobile_cash()

        # 计算股数(向下取整到100的倍数)
        shares = int(amount / price / 100) * 100

        return shares

    def open_base_position(self, ts_code: str, stock_name: str,
                          price: Decimal) -> Position:
        """
        开基本仓位

        Args:
            ts_code: 股票代码
            stock_name: 股票名称
            price: 买入价格

        Returns:
            Position对象
        """
        can_open, reason = self.can_open_position(ts_code)
        if not can_open:
            raise ValueError(f"无法开仓: {reason}")

        # 计算买入股数
        shares = self.calculate_position_size(price, 'base')

        # 创建持仓
        position = Position(
            ts_code=ts_code,
            stock_name=stock_name,
            base_shares=shares,
            base_cost=price,
            mobile_shares=0,
            mobile_cost=Decimal("0"),
            buy_date=date.today()
        )

        position.update_price(price)

        # 更新资金统计
        self._used_base_capital += position.base_amount

        # 保存持仓
        self.positions[ts_code] = position

        return position

    def get_available_base_cash(self) -> Decimal:
        """获取可用基本仓位资金"""
        total_base_cash = self.total_capital * Decimal("0.75")  # 75%用于基本仓
        return total_base_cash - self._used_base_capital

    def get_available_mobile_cash(self) -> Decimal:
        """获取可用机动资金"""
        total_mobile_cash = self.total_capital * self.mobile_cash_ratio
        return total_mobile_cash - self._used_mobile_capital
```

**Step 4: 运行测试验证通过**

```bash
pytest tests/core/position/test_position_manager.py -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add core/position/position_manager.py tests/core/position/test_position_manager.py
git commit -m "feat(position): 实现PositionManager仓位管理器基础功能"
```

---

## Phase 2: 技术指标模块

### Task 4: MACD背离检测(面积法)

**Files:**
- Create: `core/indicators/divergence.py`
- Create: `tests/core/indicators/test_divergence.py`

**Step 1: 编写背离检测测试**

```python
# tests/core/indicators/test_divergence.py
import pytest
import pandas as pd
import numpy as np
from decimal import Decimal
from core.indicators.divergence import DivergenceDetector

def test_find_macd_segments_bullish():
    """测试找出绿柱波段"""
    # 构造测试数据: 两个绿柱波段
    hist = pd.Series([0.1, 0.05, -0.1, -0.2, -0.15, 0.05, -0.05, -0.1, -0.05, 0.1])

    detector = DivergenceDetector()
    segments = detector._find_macd_segments(hist, 'bullish')

    assert len(segments) == 2
    assert segments[0]['start'] == 2
    assert segments[0]['end'] == 4
    assert segments[1]['start'] == 6
    assert segments[1]['end'] == 8

def test_calculate_macd_area():
    """测试计算MACD面积"""
    hist = pd.Series([-0.1, -0.2, -0.15, -0.05])

    detector = DivergenceDetector()
    area = detector._calculate_macd_area(hist, 0, 3)

    # 面积 = |−0.1| + |−0.2| + |−0.15| + |−0.05| = 0.5
    assert abs(area - 0.5) < 0.001

def test_detect_bullish_divergence():
    """测试检测底背离"""
    # 构造底背离数据: 价格新低,MACD面积缩小
    price = pd.Series([10.0, 9.5, 9.0, 9.2, 9.5, 9.0, 8.5, 8.8, 9.0])
    macd_hist = pd.Series([0.1, 0.05, -0.1, -0.2, -0.15, 0.05, -0.05, -0.08, -0.03])

    detector = DivergenceDetector()
    is_divergence, detail = detector.detect_divergence(price, macd_hist, 'bullish')

    assert is_divergence is True
    assert detail['area_ratio'] < 1.0  # 面积缩小
```

**Step 2: 运行测试验证失败**

```bash
pytest tests/core/indicators/test_divergence.py -v
```

Expected: FAIL

**Step 3: 实现DivergenceDetector类**

```python
# core/indicators/divergence.py
import pandas as pd
from typing import Tuple, Dict, List

class DivergenceDetector:
    """
    背离检测器 - 使用面积计算法

    改进点:
    - 从峰值比较改为面积计算
    - 更准确地反映MACD柱子的能量变化
    """

    def detect_divergence(self, price: pd.Series,
                         macd_hist: pd.Series,
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
            price_lower = price.iloc[last_segment['trough']] < price.iloc[prev_segment['trough']]
            area_smaller = last_area < prev_area

            is_divergence = price_lower and area_smaller

        else:
            # 顶背离: 价格新高 + MACD面积缩小
            price_higher = price.iloc[last_segment['peak']] > price.iloc[prev_segment['peak']]
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
                        # 波段结束
                        segment_end = i - 1
                        segment_data = hist.iloc[segment_start:segment_end+1]
                        trough_idx = segment_data.idxmin()
                        segments.append({
                            'start': segment_start,
                            'end': segment_end,
                            'trough': hist.index.get_loc(trough_idx)
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
                        segment_data = hist.iloc[segment_start:segment_end+1]
                        peak_idx = segment_data.idxmax()
                        segments.append({
                            'start': segment_start,
                            'end': segment_end,
                            'peak': hist.index.get_loc(peak_idx)
                        })
                        in_segment = False

        # 处理最后一个波段(如果还在波段中)
        if in_segment:
            segment_end = len(hist) - 1
            segment_data = hist.iloc[segment_start:segment_end+1]
            if segment_type == 'bullish':
                trough_idx = segment_data.idxmin()
                segments.append({
                    'start': segment_start,
                    'end': segment_end,
                    'trough': hist.index.get_loc(trough_idx)
                })
            else:
                peak_idx = segment_data.idxmax()
                segments.append({
                    'start': segment_start,
                    'end': segment_end,
                    'peak': hist.index.get_loc(peak_idx)
                })

        return segments
```

**Step 4: 运行测试验证通过**

```bash
pytest tests/core/indicators/test_divergence.py -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add core/indicators/divergence.py tests/core/indicators/test_divergence.py
git commit -m "feat(indicators): 实现面积法MACD背离检测"
```

---

### Task 5: 空头陷阱检测器

**Files:**
- Create: `core/detectors/bear_trap.py`
- Create: `tests/core/detectors/test_bear_trap.py`

**Step 1: 编写空头陷阱检测测试**

```python
# tests/core/detectors/test_bear_trap.py
import pytest
import pandas as pd
from core.detectors.bear_trap import BearTrapDetector

def test_detect_bear_trap():
    """测试完整的空头陷阱检测"""
    dates = pd.date_range('2026-01-01', periods=20, freq='D')
    df = pd.DataFrame({
        'close': [10.0] * 10 + [9.8, 9.7, 9.6, 9.8, 10.0, 10.1, 10.2, 10.3, 10.4, 10.5],
        'ma250': [9.5 + i*0.05 for i in range(20)]
    }, index=dates)

    indicators = {'has_bullish_divergence': True}
    detector = BearTrapDetector()
    is_trap, reason = detector.detect(df, indicators)

    assert is_trap is True
```

**Step 2: 运行测试验证失败**

```bash
pytest tests/core/detectors/test_bear_trap.py -v
```

**Step 3: 实现BearTrapDetector类**

```python
# core/detectors/bear_trap.py
import pandas as pd
from typing import Tuple

class BearTrapDetector:
    """空头陷阱识别器"""

    def detect(self, df: pd.DataFrame, indicators: dict) -> Tuple[bool, str]:
        reasons = []
        if not self._check_ma250_uptrend(df):
            return False, "年线未向上"
        reasons.append("年线向上")

        if not self._check_recent_break_below_ma(df, days=5):
            return False, "未发现短期跌破年线"
        reasons.append("短期跌破年线")

        if not self._check_quick_recovery(df, days=5):
            return False, "未快速收回"
        reasons.append("快速收回")

        if not indicators.get('has_bullish_divergence', False):
            return False, "无底背离"
        reasons.append("出现底背离")

        return True, " + ".join(reasons)

    def _check_ma250_uptrend(self, df: pd.DataFrame) -> bool:
        if 'ma250' not in df.columns or len(df) < 10:
            return False
        recent_ma = df['ma250'].tail(10)
        return recent_ma.iloc[-1] > recent_ma.iloc[0]

    def _check_recent_break_below_ma(self, df: pd.DataFrame, days: int = 5) -> bool:
        if len(df) < days:
            return False
        recent = df.tail(days)
        return (recent['close'] < recent['ma250']).any()

    def _check_quick_recovery(self, df: pd.DataFrame, days: int = 5) -> bool:
        if len(df) < days:
            return False
        recent = df.tail(days)
        current_above = recent['close'].iloc[-1] > recent['ma250'].iloc[-1]
        had_break = (recent['close'] < recent['ma250']).any()
        return current_above and had_break
```

**Step 4: 运行测试验证通过**

```bash
pytest tests/core/detectors/test_bear_trap.py -v
```

**Step 5: Commit**

```bash
git add core/detectors/bear_trap.py tests/core/detectors/test_bear_trap.py
git commit -m "feat(detectors): 实现空头陷阱检测器"
```

---

### Task 6: 做T策略模块

**Files:**
- Create: `core/position/t_trading.py`
- Create: `tests/core/position/test_t_trading.py`

**Step 1: 编写做T策略测试**

```python
# tests/core/position/test_t_trading.py
import pytest
from decimal import Decimal
from core.position.t_trading import TTradingStrategy
from core.position.position_manager import PositionManager

def test_analyze_positive_t_buy():
    pm = PositionManager(total_capital=Decimal("100000"))
    pm.open_base_position("000001.SZ", "平安银行", Decimal("12.50"))
    strategy = TTradingStrategy(pm)

    indicators = {
        'has_bullish_divergence': True,
        'is_near_support': True,
        'volume_increasing': True
    }

    result = strategy._analyze_positive_t(pm.positions["000001.SZ"], indicators)
    assert result['has_opportunity'] is True
    assert result['signal_type'] == 'positive_t_buy'
```

**Step 2: 运行测试验证失败**

```bash
pytest tests/core/position/test_t_trading.py -v
```

**Step 3: 实现TTradingStrategy类**

继续下一部分...

---

