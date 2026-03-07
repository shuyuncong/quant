# 量化交易系统实施计划 (最终版 v2.0)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 构建混合架构的量化交易系统,实现"程序执行已有模式+给操作信号+不必盯盘+允许主观择时"的核心诉求。

**Architecture:** 自写策略内核(选股/市场状态/仓位/做T/风控) + 复用成熟组件(数据获取/回测框架/通知) + 不做券商接入

**Tech Stack:** Python 3.9+, Pandas, Numpy, TA-Lib, Backtesting.py, Tushare Pro, PyYAML

**参考文档:**
- `doc/设计文档/设计修正方案.md` - 核心类设计
- `doc/设计文档/设计方案审查报告.md` - Critical问题修正
- `doc/量化交易/quantTrading.md` - 知识星球交易理念
- `doc/设计文档/第一阶段-Python核心引擎-详细设计.md` - 详细接口

**关键调整 (基于Codex分析):**
1. ✅ 新增MarketRegimeEngine - 支持人工覆盖市场状态
2. ✅ 仓位策略调整为2-4只+单票不超40%
3. ✅ 明确资金周转率KPI定义
4. ✅ 补充A股成本模型
5. ✅ 明确V1/V2/V3阶段边界

---

## 核心原则

1. **先信号,后增强** - V1先做能用的,V2补全多市场,V3优化评估
2. **先复用,后扩展** - 数据/回测/通知用现成的,策略内核自己写
3. **先验证规则,再优化参数** - 不追求过拟合
4. **代码围绕策略组织,不围绕券商组织** - 保持独立性
5. **允许主观择时** - 可人工覆盖市场状态,但默认自动运行

---

## 系统架构

```
quant/
├── core/                      # 策略内核(自己写)
│   ├── regime/                # 市场状态引擎
│   │   ├── market_regime_engine.py
│   │   └── regime_override.py
│   ├── selector/              # 选股器
│   │   └── stock_selector.py
│   ├── indicators/            # 技术指标
│   │   ├── divergence.py      # 面积法背离
│   │   └── macd.py
│   ├── detectors/             # 信号检测器
│   │   ├── bear_trap.py       # 空头陷阱
│   │   └── volume_price.py    # 量价关系
│   ├── position/              # 仓位管理
│   │   ├── position.py
│   │   ├── position_manager.py
│   │   └── t_trading.py       # 做T策略
│   ├── risk/                  # 风险控制
│   │   └── risk_manager.py
│   └── router/                # 策略路由
│       └── strategy_router.py
│
├── backtest/                  # 回测系统(复用Backtesting.py)
│   ├── engine/
│   │   ├── bt_engine.py
│   │   └── china_cost_model.py  # A股成本模型
│   ├── strategies/
│   │   ├── trend_following_bt.py
│   │   ├── mean_reversion_bt.py
│   │   └── breakout_bt.py
│   └── reports/
│       └── performance_report.py
│
├── quant-python/signal_system/  # 实时信号系统(复用现有)
│   ├── data/                  # 数据获取(Tushare)
│   ├── notification/          # 通知推送
│   ├── strategy/              # 策略引擎
│   └── main.py
│
└── tests/
    ├── core/
    ├── backtest/
    └── integration/
```

---

## Phase 0: 市场状态引擎 (新增,优先级最高)

### Task 0.1: MarketRegimeEngine市场状态引擎

**Files:**
- Create: `core/regime/__init__.py`
- Create: `core/regime/market_regime_engine.py`
- Create: `core/regime/regime_override.py`
- Create: `tests/core/regime/test_market_regime_engine.py`

**参考:** quantTrading.md - "允许一定主观来择时"

**Step 1: 编写MarketRegimeEngine测试**

```python
# tests/core/regime/test_market_regime_engine.py
import pytest
import pandas as pd
from core.regime.market_regime_engine import MarketRegimeEngine

def test_detect_bull_market():
    """测试牛市识别"""
    # 构造牛市数据: MA250向上 + 价格>MA250 + MACD>0
    dates = pd.date_range('2026-01-01', periods=100, freq='D')
    df = pd.DataFrame({
        'close': [3000 + i*10 for i in range(100)],
        'ma250': [2900 + i*8 for i in range(100)],
        'volume': [200000000] * 100
    }, index=dates)

    engine = MarketRegimeEngine()
    regime, score, reason = engine.detect_regime(df)

    assert regime == 'bull'
    assert score > 0.7
    assert 'MA250向上' in reason

def test_detect_bear_market():
    """测试熊市识别"""
    dates = pd.date_range('2026-01-01', periods=100, freq='D')
    df = pd.DataFrame({
        'close': [3000 - i*10 for i in range(100)],
        'ma250': [3100 - i*8 for i in range(100)],
        'volume': [150000000] * 100
    }, index=dates)

    engine = MarketRegimeEngine()
    regime, score, reason = engine.detect_regime(df)

    assert regime == 'bear'
    assert score > 0.7

def test_detect_range_market():
    """测试震荡市识别"""
    dates = pd.date_range('2026-01-01', periods=100, freq='D')
    df = pd.DataFrame({
        'close': [3000 + (i % 20 - 10) * 5 for i in range(100)],
        'ma250': [3000] * 100,
        'volume': [180000000] * 100
    }, index=dates)

    engine = MarketRegimeEngine()
    regime, score, reason = engine.detect_regime(df)

    assert regime == 'range'

def test_manual_override():
    """测试人工覆盖市场状态"""
    engine = MarketRegimeEngine()

    # 设置人工覆盖
    engine.set_override('bull', '主观判断市场即将转强')

    # 即使数据显示熊市,也应返回覆盖的状态
    dates = pd.date_range('2026-01-01', periods=100, freq='D')
    df = pd.DataFrame({
        'close': [3000 - i*10 for i in range(100)],
        'ma250': [3100 - i*8 for i in range(100)],
        'volume': [150000000] * 100
    }, index=dates)

    regime, score, reason = engine.detect_regime(df)

    assert regime == 'bull'
    assert '人工覆盖' in reason

def test_clear_override():
    """测试清除人工覆盖"""
    engine = MarketRegimeEngine()

    engine.set_override('bull', '测试覆盖')
    engine.clear_override()

    assert engine.get_override() is None
```

**Step 2: 运行测试验证失败**

```bash
pytest tests/core/regime/test_market_regime_engine.py -v
```

Expected: FAIL

**Step 3: 实现MarketRegimeEngine类**

```python
# core/regime/market_regime_engine.py
"""
MarketRegimeEngine - 市场状态引擎

功能:
1. 自动判断市场状态(bull/bear/range)
2. 支持人工覆盖市场状态
3. 输出判断依据和置信度

参考: doc/量化交易/quantTrading.md - 允许主观择时
"""

import pandas as pd
import numpy as np
from typing import Tuple, Optional, Dict
from datetime import datetime
import talib

class MarketRegimeEngine:
    """
    市场状态引擎

    判断逻辑:
    - 牛市: MA250向上 + 价格>MA250 + MACD>0 + 成交量>均值
    - 熊市: MA250向下 + 价格<MA250 + MACD<0
    - 震荡: 其他情况
    """

    def __init__(self, config: dict = None):
        self.config = config or {}

        # 判断阈值
        self.bull_score_threshold = self.config.get('bull_score_threshold', 0.7)
        self.bear_score_threshold = self.config.get('bear_score_threshold', 0.7)
        self.range_score_threshold = self.config.get('range_score_threshold', 0.6)

        # 人工覆盖状态
        self._override_regime: Optional[str] = None
        self._override_reason: Optional[str] = None
        self._override_time: Optional[datetime] = None

    def detect_regime(self, df: pd.DataFrame) -> Tuple[str, float, str]:
        """
        检测市场状态

        Args:
            df: 包含close, ma250, volume的DataFrame

        Returns:
            (regime, score, reason)
            - regime: 'bull' / 'bear' / 'range'
            - score: 置信度 0-1
            - reason: 判断依据
        """
        # 1. 检查人工覆盖
        if self._override_regime is not None:
            reason = f"人工覆盖: {self._override_reason} (设置于{self._override_time.strftime('%Y-%m-%d %H:%M')})"
            return self._override_regime, 1.0, reason

        # 2. 自动判断
        return self._auto_detect(df)

    def _auto_detect(self, df: pd.DataFrame) -> Tuple[str, float, str]:
        """自动检测市场状态"""
        reasons = []
        bull_score = 0.0
        bear_score = 0.0

        # 指标1: MA250趋势
        if len(df) >= 10:
            ma250_slope = (df['ma250'].iloc[-1] - df['ma250'].iloc[-10]) / df['ma250'].iloc[-10]

            if ma250_slope > 0.02:  # 上涨>2%
                bull_score += 0.3
                reasons.append(f"MA250向上({ma250_slope*100:.1f}%)")
            elif ma250_slope < -0.02:  # 下跌>2%
                bear_score += 0.3
                reasons.append(f"MA250向下({ma250_slope*100:.1f}%)")

        # 指标2: 价格相对MA250位置
        if 'ma250' in df.columns:
            price_vs_ma = (df['close'].iloc[-1] - df['ma250'].iloc[-1]) / df['ma250'].iloc[-1]

            if price_vs_ma > 0.05:  # 价格高于MA250 5%以上
                bull_score += 0.3
                reasons.append(f"价格>MA250({price_vs_ma*100:.1f}%)")
            elif price_vs_ma < -0.05:  # 价格低于MA250 5%以上
                bear_score += 0.3
                reasons.append(f"价格<MA250({price_vs_ma*100:.1f}%)")

        # 指标3: MACD
        macd, signal, hist = talib.MACD(df['close'].values, 12, 26, 9)

        if len(macd) > 0 and not np.isnan(macd[-1]):
            if macd[-1] > 0:
                bull_score += 0.2
                reasons.append(f"MACD>0({macd[-1]:.2f})")
            else:
                bear_score += 0.2
                reasons.append(f"MACD<0({macd[-1]:.2f})")

        # 指标4: 成交量
        if len(df) >= 20:
            recent_volume = df['volume'].iloc[-5:].mean()
            avg_volume = df['volume'].iloc[-20:].mean()

            if recent_volume > avg_volume * 1.2:
                bull_score += 0.2
                reasons.append("成交量放大")

        # 判断最终状态
        if bull_score >= self.bull_score_threshold:
            return 'bull', bull_score, ' + '.join(reasons)
        elif bear_score >= self.bear_score_threshold:
            return 'bear', bear_score, ' + '.join(reasons)
        else:
            return 'range', max(bull_score, bear_score), '震荡市: ' + ' + '.join(reasons)

    def set_override(self, regime: str, reason: str):
        """
        设置人工覆盖

        Args:
            regime: 'bull' / 'bear' / 'range'
            reason: 覆盖原因
        """
        if regime not in ['bull', 'bear', 'range']:
            raise ValueError(f"Invalid regime: {regime}")

        self._override_regime = regime
        self._override_reason = reason
        self._override_time = datetime.now()

        print(f"✅ 市场状态已人工覆盖为: {regime}")
        print(f"   原因: {reason}")
        print(f"   时间: {self._override_time.strftime('%Y-%m-%d %H:%M:%S')}")

    def clear_override(self):
        """清除人工覆盖,恢复自动判断"""
        if self._override_regime is not None:
            print(f"✅ 已清除人工覆盖(原状态: {self._override_regime})")
            self._override_regime = None
            self._override_reason = None
            self._override_time = None
        else:
            print("ℹ️  当前无人工覆盖")

    def get_override(self) -> Optional[Dict]:
        """获取当前人工覆盖状态"""
        if self._override_regime is None:
            return None

        return {
            'regime': self._override_regime,
            'reason': self._override_reason,
            'time': self._override_time
        }

    def get_status(self, df: pd.DataFrame) -> Dict:
        """
        获取完整状态信息

        Returns:
            {
                'regime': 当前状态,
                'score': 置信度,
                'reason': 判断依据,
                'is_override': 是否人工覆盖,
                'auto_regime': 自动判断结果(如果有覆盖)
            }
        """
        current_regime, score, reason = self.detect_regime(df)

        result = {
            'regime': current_regime,
            'score': score,
            'reason': reason,
            'is_override': self._override_regime is not None
        }

        # 如果有人工覆盖,同时返回自动判断结果
        if self._override_regime is not None:
            auto_regime, auto_score, auto_reason = self._auto_detect(df)
            result['auto_regime'] = auto_regime
            result['auto_score'] = auto_score
            result['auto_reason'] = auto_reason

        return result
```

**Step 4: 运行测试验证通过**

```bash
pytest tests/core/regime/test_market_regime_engine.py -v
```

Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add core/regime/ tests/core/regime/
git commit -m "feat(regime): 实现MarketRegimeEngine市场状态引擎

- 自动判断bull/bear/range
- 支持人工覆盖市场状态
- 输出判断依据和置信度
- 解决Codex指出的主观择时缺失问题

参考: doc/量化交易/quantTrading.md
Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 0.2: 配置文件更新

**Files:**
- Modify: `quant-python/signal_system/config/config.yaml`

**Step 1: 添加市场状态和人工覆盖配置**

```yaml
# 市场状态引擎配置
regime:
  mode: auto  # auto: 自动判断, manual: 完全人工
  bull_score_threshold: 0.7
  bear_score_threshold: 0.7
  range_score_threshold: 0.6

# 人工覆盖配置
manual_overrides:
  regime_override: null  # 可设置为bull/bear/range强制覆盖
  override_reason: ""    # 覆盖原因
  # 注意: 设置后需要手动清除,否则一直生效

# 仓位管理配置(调整为2-4只+单票不超40%)
position:
  min_stocks: 2          # 最少持仓数量
  target_stocks: 3       # 目标持仓数量
  max_stocks: 4          # 最多持仓数量
  base_position_per_stock: 0.25  # 每只基本仓位25%
  mobile_cash_ratio: 0.25        # 机动资金25%
  max_position_per_stock: 0.40   # 单票最大仓位40%

# 风险控制配置
risk:
  stop_loss_pct: 0.08              # 止损8%
  stop_profit_pct: 0.30            # 止盈30%
  max_portfolio_drawdown_pct: 0.20 # 最大组合回撤20%
  max_single_day_drawdown_pct: 0.02  # 单日最大回撤2%

# 回测配置(补充A股成本模型)
backtest:
  initial_cash: 100000
  commission: 0.0003     # 万三佣金
  stamp_tax: 0.001       # 千一印花税(仅卖出)
  slippage: 0.001        # 滑点0.1%
  min_shares: 100        # 最小100股
  enable_t_plus_1: true  # 启用T+1限制
  start_date: "2023-01-01"
  end_date: "2025-12-31"

# KPI指标定义
kpi:
  # 资金周转率 = 一段时间内累计成交金额 / 平均账户净值
  target_win_rate: 0.55           # 目标胜率55%
  target_profit_loss_ratio: 2.0   # 目标盈亏比2:1
  target_max_drawdown: 0.20       # 目标最大回撤20%
  target_annual_return: 0.15      # 目标年化收益15%
  # 资金周转率: 回测时自动计算,不设目标值
```

**Step 2: Commit**

```bash
git add quant-python/signal_system/config/config.yaml
git commit -m "feat(config): 更新配置文件

- 新增regime市场状态引擎配置
- 新增manual_overrides人工覆盖配置
- 调整position为2-4只+单票不超40%
- 补充A股成本模型(佣金/印花税/滑点/T+1)
- 明确KPI指标定义(含资金周转率)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Phase 1: 核心模块基础设施

### Task 1: 项目结构重组

(保持原Task 1内容不变)

### Task 2: Position持仓数据结构

(保持原Task 2内容不变)

### Task 3: PositionManager仓位管理器

**Files:**
- Create: `core/position/position_manager.py`
- Create: `tests/core/position/test_position_manager.py`

**调整:** 支持2-4只+单票不超40%

```python
# core/position/position_manager.py (关键调整)
class PositionManager:
    """
    仓位管理器

    调整后的核心原则:
    - 持有2-4只股票(灵活)
    - 每只股票25%基本仓位
    - 保留25%机动资金用于做T
    - 单票最大仓位不超40%
    """

    def __init__(self, total_capital: Decimal, config: dict = None):
        self.total_capital = total_capital
        self.positions: Dict[str, Position] = {}

        # 仓位配置 - 调整为灵活模式
        config = config or {}
        self.min_stocks = config.get('min_stocks', 2)
        self.target_stocks = config.get('target_stocks', 3)
        self.max_stocks = config.get('max_stocks', 4)
        self.base_position_ratio = Decimal(str(config.get('base_position_per_stock', 0.25)))
        self.mobile_cash_ratio = Decimal(str(config.get('mobile_cash_ratio', 0.25)))
        self.max_position_per_stock = Decimal(str(config.get('max_position_per_stock', 0.40)))

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

    def check_position_limit(self, ts_code: str, additional_amount: Decimal) -> Tuple[bool, str]:
        """
        检查是否超过单票最大仓位限制

        Args:
            ts_code: 股票代码
            additional_amount: 拟增加的金额

        Returns:
            (是否允许, 原因)
        """
        if ts_code not in self.positions:
            # 新开仓,检查是否超过40%
            if additional_amount / self.total_capital > self.max_position_per_stock:
                return False, f"单票仓位不能超过{self.max_position_per_stock*100}%"
            return True, "可以开仓"

        position = self.positions[ts_code]
        current_ratio = position.total_amount / self.total_capital
        new_ratio = (position.total_amount + additional_amount) / self.total_capital

        if new_ratio > self.max_position_per_stock:
            return False, f"加仓后单票仓位({new_ratio*100:.1f}%)将超过限制({self.max_position_per_stock*100}%)"

        return True, "可以加仓"
```

(其余方法保持不变)

---

## 实施计划总结

### V1阶段 (4-6周) - 最小可用版本

**目标:** 能每天使用的信号系统

**交付:**
- ✅ Task 0.1-0.2: MarketRegimeEngine + 人工覆盖
- ✅ Task 1: 项目结构
- ✅ Task 2-3: Position + PositionManager(2-4只+40%限制)
- ✅ Task 4: MACD背离检测(面积法)
- ✅ Task 5: 空头陷阱检测
- ✅ Task 6: 做T策略(正T/反T)
- ✅ Task 7-9: Backtesting.py + 趋势策略回测
- ✅ Task 10: 集成到signal_system
- ✅ Task 11: 通知推送

### V2阶段 (4-6周) - 多市场覆盖

**目标:** 覆盖上涨/下跌/震荡三种市场

**交付:**
- 震荡策略(MeanReversionStrategy)
- 下跌策略(DefensiveStrategy)
- 突破策略(BreakoutStrategy)
- 策略路由(StrategyRouter)
- 多策略回测对比

### V3阶段 (4-6周) - 优化和评估

**目标:** 可评估、可优化、可扩展

**交付:**
- 参数优化(有限核心参数)
- 资金周转率KPI计算
- A股成本模型(佣金/印花税/滑点/T+1/涨跌停)
- 样本外验证
- 分市场状态报表
- 为第二阶段预留接口

---

## 关键KPI定义

### 资金周转率

```python
资金周转率 = 一段时间内累计成交金额 / 平均账户净值

# 示例计算
total_trade_amount = sum(buy_amount + sell_amount for all trades)
avg_account_value = mean(daily_account_value)
turnover_rate = total_trade_amount / avg_account_value
```

**注意:** 周转率必须和胜率、盈亏比、回撤一起看,不能单独优化

### 核心指标体系

| 指标 | 目标值 | 说明 |
|------|--------|------|
| 胜率 | >55% | 盈利交易占比 |
| 盈亏比 | >2:1 | 平均盈利/平均亏损 |
| 最大回撤 | <20% | 最大资金回撤 |
| 年化收益率 | >15% | 年化收益 |
| 资金周转率 | 监控 | 不设目标,观察与其他指标关系 |
| 交易次数 | 监控 | 过多可能降低盈亏比 |
| 平均持仓周期 | 监控 | 与策略类型相关 |

---

## 明确不做的内容

1. ❌ 券商接口对接
2. ❌ 自动下单执行
3. ❌ Java/Vue可视化系统(V1/V2/V3阶段)
4. ❌ 高频交易
5. ❌ 机构级交易平台

**项目定位:** 面向个人交易者的低频/中低频、信号驱动、可回测、可人工择时的量化辅助系统

---

**完整实施计划已更新!**

**下一步: 开始执行Task 0.1 - 实现MarketRegimeEngine**
