# 量化交易系统实施计划 (与现有设计对齐版)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有设计文档基础上,实施混合架构量化交易系统,完全兼容第一阶段Python核心引擎设计,并为第二阶段Java+Vue可视化系统预留接口。

**Architecture:** 严格遵循"设计修正方案"中的Critical修正要求,实现基本仓+机动仓管理、正T/反T策略、面积法背离检测,同时保持与现有signal_system代码的兼容性。

**Tech Stack:** Python 3.9+, Pandas, Numpy, TA-Lib, Backtesting.py, Tushare Pro, PyYAML

**参考文档:**
- `doc/设计文档/第一阶段-Python核心引擎-概要设计.md`
- `doc/设计文档/第一阶段-Python核心引擎-详细设计.md`
- `doc/设计文档/设计修正方案.md`
- `doc/设计文档/设计方案审查报告.md`
- `doc/量化交易/quantTrading.md`

---

## 设计对齐说明

### 与审查报告的对齐

根据`设计方案审查报告.md`,本实施计划重点解决以下Critical问题:

1. ✅ **问题1: 仓位管理严重偏离** → Task 2-3实现PositionManager
2. ✅ **问题2: 完全缺失做T策略** → Task 6实现TTradingStrategy
3. ✅ **问题3: 背离检测算法不精确** → Task 4实现面积法
4. ✅ **问题6: 缺少空头陷阱识别** → Task 5实现BearTrapDetector

### 与修正方案的对齐

严格按照`设计修正方案.md`中的类设计和算法实现:
- Position类设计 (第2节)
- PositionManager类设计 (第2.1节)
- TTradingStrategy类设计 (第2.2节)
- BearTrapDetector类设计 (第2.3节)
- 背离检测算法修正 (第3.1节)
- 参数配置修正 (第4节)

### 与现有代码的兼容

保持与`quant-python/signal_system/`现有代码的完全兼容:
- 复用data/data_fetcher.py
- 复用notification/notifier.py
- 复用strategy/indicators.py (增强背离检测)
- 扩展strategy/strategy_engine.py (集成核心模块)

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

**参考:** 设计修正方案 第5节 - 修正后的系统架构

**Step 1: 创建目录结构**

```bash
cd D:/development/github/quant
mkdir -p core/position core/indicators core/detectors
mkdir -p backtest/engine backtest/strategies
mkdir -p tests/core/position tests/core/indicators tests/core/detectors
mkdir -p tests/backtest
```

**Step 2: 创建__init__.py文件**

```bash
touch core/__init__.py
touch core/position/__init__.py
touch core/indicators/__init__.py
touch core/detectors/__init__.py
touch backtest/__init__.py
touch backtest/engine/__init__.py
touch backtest/strategies/__init__.py
```

**Step 3: 验证导入**

```python
import core
import core.position
import core.indicators
import core.detectors
import backtest
print("✅ 目录结构创建成功")
```

**Step 4: Commit**

```bash
git add core/ backtest/ tests/
git commit -m "feat: 创建核心模块和回测模块目录结构

- 新增core模块(position/indicators/detectors)
- 新增backtest模块(engine/strategies)
- 符合设计修正方案第5节架构

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Position持仓数据结构

**Files:**
- Create: `core/position/position.py`
- Create: `tests/core/position/test_position.py`

**参考:** 设计修正方案 第2.1.2节 - 核心数据结构

**Step 1: 编写Position数据类测试**

```python
# tests/core/position/test_position.py
import pytest
from decimal import Decimal
from datetime import date
from core.position.position import Position

def test_position_creation():
    """测试持仓创建 - 符合设计修正方案Position类设计"""
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
    """测试添加机动仓 - 符合知识星球做T理念"""
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

def test_position_reduce_mobile():
    """测试减少机动仓"""
    pos = Position(
        ts_code="000001.SZ",
        stock_name="平安银行",
        base_shares=1000,
        base_cost=Decimal("12.50"),
        mobile_shares=500,
        mobile_cost=Decimal("12.00"),
        buy_date=date(2026, 3, 1)
    )

    pos.update_price(Decimal("13.00"))
    sell_amount = pos.reduce_mobile_shares(500)

    assert pos.mobile_shares == 0
    assert sell_amount == Decimal("6500.00")  # 500 * 13.00
```

**Step 2: 运行测试验证失败**

```bash
pytest tests/core/position/test_position.py -v
```

Expected: FAIL with "ModuleNotFoundError"

**Step 3: 实现Position类 (严格按照设计修正方案)**

```python
# core/position/position.py
"""
Position持仓数据结构

参考: doc/设计文档/设计修正方案.md 第2.1.2节
实现: 区分基本仓位和机动仓位,支持做T操作
"""

from dataclasses import dataclass
from decimal import Decimal
from datetime import date

@dataclass
class Position:
    """
    持仓信息

    核心设计:
    - 区分基本仓位(base)和机动仓位(mobile)
    - 基本仓位用于长期持有
    - 机动仓位用于做T操作
    """
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
        """总股数 = 基本仓 + 机动仓"""
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
        """
        添加机动仓位

        用于: 正T买入、反T买回
        """
        if self.mobile_shares == 0:
            self.mobile_cost = cost
        else:
            # 加权平均成本
            total_amount = self.mobile_amount + shares * cost
            self.mobile_cost = total_amount / (self.mobile_shares + shares)

        self.mobile_shares += shares
        self.update_totals()

    def reduce_mobile_shares(self, shares: int) -> Decimal:
        """
        减少机动仓位

        用于: 正T卖出
        Returns: 卖出金额
        """
        if shares > self.mobile_shares:
            raise ValueError(f"减仓数量({shares})超过机动仓位({self.mobile_shares})")

        sell_amount = shares * self.current_price
        self.mobile_shares -= shares

        if self.mobile_shares == 0:
            self.mobile_cost = Decimal("0")

        self.update_totals()
        return sell_amount

    def reduce_base_shares(self, shares: int) -> Decimal:
        """
        减少基本仓位

        用于: 反T卖出、止损止盈
        Returns: 卖出金额
        """
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

Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add core/position/position.py tests/core/position/test_position.py
git commit -m "feat(position): 实现Position持仓数据结构

- 区分基本仓位和机动仓位
- 支持做T操作(add_mobile/reduce_mobile/reduce_base)
- 符合设计修正方案第2.1.2节
- 解决审查报告问题1(仓位管理偏离)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: PositionManager仓位管理器

**Files:**
- Create: `core/position/position_manager.py`
- Create: `tests/core/position/test_position_manager.py`

**参考:**
- 设计修正方案 第2.1节
- 审查报告 问题1修正建议

**Step 1: 编写PositionManager测试**

```python
# tests/core/position/test_position_manager.py
import pytest
from decimal import Decimal
from core.position.position_manager import PositionManager

def test_position_manager_init():
    """测试仓位管理器初始化 - 符合3只+25%机动模式"""
    pm = PositionManager(total_capital=Decimal("100000"))

    assert pm.total_capital == Decimal("100000")
    assert pm.mobile_cash_ratio == Decimal("0.25")
    assert pm.base_position_ratio == Decimal("0.25")
    assert pm.max_stocks == 3  # 知识星球标准: 3只股票
    assert len(pm.positions) == 0

def test_can_open_position():
    """测试是否可以开仓 - 最多3只股票限制"""
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
    """测试计算仓位大小 - 每只25%"""
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
    """测试获取可用机动资金 - 25%机动资金"""
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

**Step 3: 实现PositionManager类(第一部分)**

继续下一个回复...

---
```python
# core/position/position_manager.py
"""
PositionManager仓位管理器

参考: doc/设计文档/设计修正方案.md 第2.1节
实现: 知识星球"3只股票+25%机动资金"模式
"""

from decimal import Decimal
from typing import Dict, Tuple
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
        self.total_capital = total_capital
        self.positions: Dict[str, Position] = {}

        # 仓位配置 - 符合审查报告修正建议
        self.mobile_cash_ratio = Decimal("0.25")  # 机动资金比例
        self.base_position_ratio = Decimal("0.25")  # 单只基本仓位比例
        self.max_stocks = 3  # 最大持仓数量
        self.max_stocks_temp = 4  # 临时最大持仓数量

        # 资金统计
        self._used_base_capital = Decimal("0")
        self._used_mobile_capital = Decimal("0")

    def can_open_position(self, ts_code: str) -> Tuple[bool, str]:
        """检查是否可以开新仓位"""
        if ts_code in self.positions:
            return False, "已持有该股票"

        active_positions = len([p for p in self.positions.values()
                               if p.total_shares > 0])

        if active_positions >= self.max_stocks:
            return False, f"已达到最大持仓数量({self.max_stocks}只)"

        available_cash = self.get_available_base_cash()
        required_cash = self.total_capital * self.base_position_ratio

        if available_cash < required_cash:
            return False, f"可用资金不足"

        return True, "可以开仓"

    def calculate_position_size(self, price: Decimal,
                               position_type: str = 'base') -> int:
        """计算应该买入的股数"""
        if position_type == 'base':
            amount = self.total_capital * self.base_position_ratio
        else:
            amount = self.get_available_mobile_cash()

        # 计算股数(向下取整到100的倍数)
        shares = int(amount / price / 100) * 100
        return shares

    def open_base_position(self, ts_code: str, stock_name: str,
                          price: Decimal) -> Position:
        """开基本仓位"""
        can_open, reason = self.can_open_position(ts_code)
        if not can_open:
            raise ValueError(f"无法开仓: {reason}")

        shares = self.calculate_position_size(price, 'base')

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
        self._used_base_capital += position.base_amount
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

    def add_mobile_position(self, ts_code: str, shares: int, price: Decimal) -> bool:
        """添加机动仓位 - 用于做T"""
        if ts_code not in self.positions:
            return False

        position = self.positions[ts_code]
        position.add_mobile_shares(shares, price)

        self._used_mobile_capital += shares * price
        return True

    def reduce_mobile_position(self, ts_code: str, shares: int) -> Decimal:
        """减少机动仓位 - 用于做T"""
        if ts_code not in self.positions:
            raise ValueError(f"未持有{ts_code}")

        position = self.positions[ts_code]
        sell_amount = position.reduce_mobile_shares(shares)

        self._used_mobile_capital -= shares * position.mobile_cost
        return sell_amount
```

**Step 4: 运行测试验证通过**

```bash
pytest tests/core/position/test_position_manager.py -v
```

**Step 5: Commit**

```bash
git add core/position/position_manager.py tests/core/position/test_position_manager.py
git commit -m "feat(position): 实现PositionManager仓位管理器

- 实现3只股票+25%机动资金模式
- 强制持仓数量限制
- 区分基本仓位和机动资金
- 符合设计修正方案第2.1节
- 解决审查报告Critical问题1

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## 实施计划总结

### 已规划的任务

**Phase 1: 核心模块** (Task 1-6)
- ✅ Task 1: 项目结构重组
- ✅ Task 2: Position持仓数据结构
- ✅ Task 3: PositionManager仓位管理器
- 📋 Task 4: MACD背离检测(面积法)
- 📋 Task 5: 空头陷阱检测器
- 📋 Task 6: 做T策略模块

**Phase 2: 回测引擎** (Task 7-9)
- 📋 Task 7: 安装Backtesting.py
- 📋 Task 8: 回测引擎封装
- 📋 Task 9: 趋势跟踪回测策略

**Phase 3: 系统集成** (Task 10-14)
- 📋 Task 10: 配置文件更新(对齐修正方案参数)
- 📋 Task 11: 集成到现有strategy_engine
- 📋 Task 12: 对齐DataFetcher与详细设计
- 📋 Task 13: 创建回测主程序
- 📋 Task 14: 为第二阶段预留接口

**Phase 4: 测试和文档** (Task 15-16)
- 📋 Task 15: 集成测试
- 📋 Task 16: 更新README

### 与现有设计的完全对齐

| 设计文档 | 对齐内容 | 实施任务 |
|---------|---------|---------|
| 审查报告-问题1 | 仓位管理3只+25%机动 | Task 2-3 |
| 审查报告-问题2 | 做T策略 | Task 6 |
| 审查报告-问题3 | 背离算法面积法 | Task 4 |
| 审查报告-问题6 | 空头陷阱识别 | Task 5 |
| 修正方案-第2节 | 核心类设计 | Task 2-6 |
| 修正方案-第3节 | 算法修正 | Task 4 |
| 修正方案-第4节 | 参数配置 | Task 10 |
| 详细设计-数据层 | DataFetcher接口 | Task 12 |
| 第二阶段设计 | 数据库表结构预留 | Task 14 |

### 关键设计决策

1. **完全兼容现有代码**: 不破坏signal_system现有功能
2. **严格遵循修正方案**: 所有Critical问题必须解决
3. **为第二阶段预留接口**: Position类可直接映射到t_position表
4. **TDD驱动开发**: 每个任务都有完整测试
5. **小步提交**: 每个任务独立commit,便于回滚

---

**完整实施计划请参考:**
- 本文档 (对齐版概要)
- `2026-03-07-trading-system-implementation.md` (详细Task 1-4)
- `2026-03-07-trading-system-implementation-part2.md` (详细Task 5-16)

**执行选项:**

1. **Subagent-Driven (当前会话)** - 逐任务派发,实时审查
2. **Parallel Session (新会话)** - 批量执行,设置检查点

**你想选择哪种方式?**

