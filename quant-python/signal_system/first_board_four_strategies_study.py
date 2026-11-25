"""四个聚宽首板/连板策略的日线机械复现（研究脚本，不接生产）。

来源（docs/学习/量化基础~策略源码.7z:聚宽策略/聚宽2025年精选）：
1. 1首板低开策略.txt（wywy1995, post 44901）
2. 7追首板涨停 过去两年年化304%.txt（子匀, post 48680）
3. 2连板龙头策略.txt（wywy1995, post 44926）
4. 13首板高开-低开-弱转强混合策略（今年收益1138.61）.txt（天山灵兔, post 49499）

这是**机械复现**，不是原策略的精确复制。日线数据无法还原：
- 集合竞价成交量（策略 7/13 的 auction_volume / prev_volume >= 3%）
- 11:25/11:28/14:50 的盘中价格（本脚本用收盘价近似卖出）
- 历史 ST 名单（用板块涨跌停幅度推断，主板 ST 的 5% 涨停不会被识别）
- 总市值（用 amount / turnover_rate 的流通市值近似，70-520 亿窗口）

其余规则（首板/连板、开盘涨幅、相对位置、成交额/换手/市值、左压、
10 仓位、T+1、涨停不买/跌停不卖、100 股整手）按源码实现。

成本：佣金 3bp/边（最低 5 元）、卖出印花税 10bp、滑点 10bp/边。
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from open_gap_momentum_study import PANEL_CACHE, load_study_panels

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = (
    BASE_DIR.parent.parent
    / "docs"
    / "backtest-results"
    / "2026-11-14-first-board-four-strategies"
)
PS = 10
LOT = 100
COMMISSION = 0.0003
STAMP_TAX = 0.0010
SLIPPAGE = 0.0010
MIN_COMMISSION = 5.0
INITIAL_CAPITAL = 1_000_000.0


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------


@dataclass
class Bundle:
    dates: pd.DatetimeIndex
    symbols: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    amount: np.ndarray
    turnover: np.ndarray
    rate: np.ndarray
    limit_up: np.ndarray
    touched: np.ndarray
    one_word: np.ndarray
    limit_down: np.ndarray
    consecutive: np.ndarray
    open_ratio: np.ndarray
    rp60: np.ndarray
    amount_prev: np.ndarray
    volume_prev: np.ndarray
    close_prev: np.ndarray
    high_prev: np.ndarray
    open_prev: np.ndarray
    float_mv_prev: np.ndarray
    vol5: np.ndarray
    avg_price_increase: np.ndarray
    avg_price_increase_1_1: np.ndarray
    three_day_ret: np.ndarray
    open_close_prev: np.ndarray
    current_ratio_ws: np.ndarray
    ma5_prev4: np.ndarray
    high_limit_price: np.ndarray
    age50: np.ndarray
    age250: np.ndarray
    universe1: np.ndarray
    universe2: np.ndarray
    universe7: np.ndarray
    universe13: np.ndarray


def _normalize_delisted_volume(panels: dict[str, pd.DataFrame]) -> None:
    """Eastmoney 退市日线的成交量单位是手；统一换成股。"""

    baostock = {
        path.name.split("_")[0]
        for path in (BASE_DIR / "cache" / "baostock_history_20261026").glob("*_qfq.pkl")
    }
    delisted = {
        path.stem
        for path in (BASE_DIR / "cache" / "stock_pit" / "prices_delisted").glob("*.csv")
        if path.name != "prices_index.csv"
    }
    delisted_only = delisted - baostock
    volume = panels["volume"]
    for symbol in delisted_only:
        if symbol in volume.columns:
            volume[symbol] = volume[symbol] * 100.0


def load_bundle() -> Bundle:
    panels = load_study_panels(PANEL_CACHE)
    _normalize_delisted_volume(panels)
    return bundle_from_panels(panels)


def bundle_from_panels(panels: dict[str, pd.DataFrame]) -> Bundle:
    """日线面板（open/high/low/close/volume/amount/turnover_rate）→ 信号特征。"""

    close_frame = panels["close"].astype("float64")
    dates = close_frame.index
    symbols = np.array([str(value) for value in close_frame.columns])

    close = close_frame.to_numpy(dtype="float64")
    open_ = panels["open"].to_numpy(dtype="float64")
    high = panels["high"].to_numpy(dtype="float64")
    low = panels["low"].to_numpy(dtype="float64")
    volume = panels["volume"].to_numpy(dtype="float64")
    amount = panels["amount"].to_numpy(dtype="float64")
    turnover = panels["turnover_rate"].to_numpy(dtype="float64")
    n_dates, n_symbols = close.shape

    prev_close = np.full_like(close, np.nan)
    prev_close[1:] = close[:-1]
    ret = close / prev_close - 1.0
    high_ret = high / prev_close - 1.0
    low_ret = low / prev_close - 1.0
    open_ret = open_ / prev_close - 1.0

    rate = np.full(close.shape, 0.10, dtype="float64")
    for column, symbol in enumerate(symbols):
        if symbol.startswith(("688", "689")):
            rate[:, column] = 0.20
        elif symbol.startswith(("300", "301", "302")):
            rate[:, column] = 0.10
            rate[dates >= pd.Timestamp("2020-08-24"), column] = 0.20

    limit_up = (ret >= rate - 0.005) & (high >= close - 0.005)
    limit_up[0] = False
    touched = (high_ret >= rate - 0.005) & (ret < rate - 0.005)
    one_word = (open_ret >= rate - 0.005) & (low_ret >= rate - 0.005)
    limit_down = ret <= -rate + 0.005

    consecutive = np.zeros(close.shape, dtype="int16")
    for index in range(1, n_dates):
        consecutive[index] = np.where(
            limit_up[index], consecutive[index - 1] + 1, 0
        )

    open_ratio = open_ / prev_close
    high60 = (
        pd.DataFrame(high, index=dates).rolling(60, min_periods=60).max().shift(1)
    ).to_numpy(dtype="float64")
    low60 = (
        pd.DataFrame(low, index=dates).rolling(60, min_periods=60).min().shift(1)
    ).to_numpy(dtype="float64")
    close_prev = prev_close
    rp60 = np.where(
        (high60 > low60) & np.isfinite(high60) & np.isfinite(low60),
        (close_prev - low60) / (high60 - low60),
        np.nan,
    )

    amount_prev = np.full_like(amount, np.nan)
    amount_prev[1:] = amount[:-1]
    volume_prev = np.full_like(volume, np.nan)
    volume_prev[1:] = volume[:-1]
    open_prev = np.full_like(open_, np.nan)
    open_prev[1:] = open_[:-1]
    high_prev = np.full_like(high, np.nan)
    high_prev[1:] = high[:-1]

    with np.errstate(divide="ignore", invalid="ignore"):
        float_mv = amount / (turnover / 100.0)
    float_mv[~np.isfinite(float_mv)] = np.nan
    float_mv_prev = np.full_like(float_mv, np.nan)
    float_mv_prev[1:] = float_mv[:-1]

    turnover_frame = pd.DataFrame(turnover, index=dates)
    vol5 = (
        turnover_frame.rolling(5, min_periods=5).mean().shift(1)
    ).to_numpy(dtype="float64")

    with np.errstate(divide="ignore", invalid="ignore"):
        avg_price_increase = amount_prev / volume_prev / close_prev - 1.0
        avg_price_increase_1_1 = amount_prev / volume_prev / close_prev * 1.1 - 1.0
    avg_price_increase[~np.isfinite(avg_price_increase)] = np.nan
    avg_price_increase_1_1[~np.isfinite(avg_price_increase_1_1)] = np.nan

    close_prev_frame = pd.DataFrame(close_prev, index=dates)
    three_day_ret = (
        close_prev_frame / close_prev_frame.shift(3) - 1.0
    ).to_numpy(dtype="float64")
    open_close_prev = np.where(
        np.isfinite(open_prev) & (open_prev > 0),
        close_prev / open_prev - 1.0,
        np.nan,
    )
    current_ratio_ws = open_ratio * 1.1 / (1.0 + rate)
    ma5_prev4 = (
        pd.DataFrame(close, index=dates).shift(1).rolling(4, min_periods=4).mean()
    ).to_numpy(dtype="float64")
    high_limit_price = prev_close * (1.0 + rate)

    valid_close = np.isfinite(close)
    first_idx = valid_close.argmax(axis=0)
    first_dates = dates.values[first_idx]
    age_days = (
        dates.values[:, None] - first_dates[None, :]
    ).astype("timedelta64[D]").astype("int32")
    age50 = age_days >= 50
    age250 = age_days >= 250

    prefix = np.array([symbol[:3] for symbol in symbols])
    main = np.isin(prefix, ["000", "001", "002", "003", "600", "601", "603", "605"])
    chinext = np.isin(prefix, ["300", "301", "302"])
    star = np.isin(prefix, ["688", "689"])
    bj = np.array([symbol.startswith(("4", "8", "92")) for symbol in symbols])
    b_share = np.isin(prefix, ["200", "900"])
    base = ~bj & ~b_share

    return Bundle(
        dates=dates,
        symbols=symbols,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
        turnover=turnover,
        rate=rate,
        limit_up=limit_up,
        touched=touched,
        one_word=one_word,
        limit_down=limit_down,
        consecutive=consecutive,
        open_ratio=open_ratio,
        rp60=rp60,
        amount_prev=amount_prev,
        volume_prev=volume_prev,
        close_prev=close_prev,
        high_prev=high_prev,
        open_prev=open_prev,
        float_mv_prev=float_mv_prev,
        vol5=vol5,
        avg_price_increase=avg_price_increase,
        avg_price_increase_1_1=avg_price_increase_1_1,
        three_day_ret=three_day_ret,
        open_close_prev=open_close_prev,
        current_ratio_ws=current_ratio_ws,
        ma5_prev4=ma5_prev4,
        high_limit_price=high_limit_price,
        age50=age50,
        age250=age250,
        universe1=base & ~star,
        universe2=base & ~star,
        universe7=base & ~star & ~chinext,
        universe13=base & ~star,
    )


# ---------------------------------------------------------------------------
# 信号
# ---------------------------------------------------------------------------


def _valid_buy(bundle: Bundle, index: int) -> np.ndarray:
    if index <= 0:
        return np.zeros(len(bundle.symbols), dtype=bool)
    return (
        np.isfinite(bundle.open[index])
        & (bundle.open[index] > 0)
        & np.isfinite(bundle.close[index - 1])
        & (bundle.close[index - 1] > 0)
        & np.isfinite(bundle.volume[index - 1])
        & (bundle.volume[index - 1] > 0)
    )


def _left_pressure_ok(bundle: Bundle, index: int, column: int) -> bool:
    if index < 3:
        return False
    prev_high = bundle.high[index - 1, column]
    if not math.isfinite(prev_high):
        return False

    zyts0 = 100
    max_offset = min(101, index)
    for offset in range(3, max_offset + 1):
        value = bundle.high[index - offset, column]
        if math.isfinite(value) and value >= prev_high:
            zyts0 = offset - 1
            break
    zyts = zyts0 + 5
    start = max(0, index - min(zyts, 101))
    window = bundle.volume[start:index, column]
    if window.size < 2:
        return False
    previous = window[:-1]
    if not np.all(np.isfinite(previous)) or not math.isfinite(window[-1]):
        return False
    return bool(float(window[-1]) > float(np.max(previous)) * 0.9)


def build_signals(bundle: Bundle) -> dict[str, dict[int, np.ndarray]]:
    signals: dict[str, dict[int, np.ndarray]] = {
        "s1_first_board_low_open": {},
        "s2_consecutive_leader": {},
        "s7_chase_first_board": {},
        "s13_mixed": {},
    }
    for index in range(3, len(bundle.dates)):
        valid = _valid_buy(bundle, index)
        if not valid.any():
            continue

        # 1. 首板低开：昨日首板（非连板），60日相对位置<=0.5，低开3-4%。
        candidates = np.flatnonzero(
            bundle.universe1
            & bundle.limit_up[index - 1]
            & (bundle.consecutive[index - 1] < 2)
            & (bundle.rp60[index] <= 0.5)
            & (bundle.open_ratio[index] >= 0.96)
            & (bundle.open_ratio[index] <= 0.97)
            & bundle.age250[index]
            & valid
        )
        if candidates.size:
            signals["s1_first_board_low_open"][index] = candidates[
                np.argsort(bundle.symbols[candidates])
            ]

        # 2. 连板龙头：最高连板，VOL5 升序，最多10个仓位。
        candidates = np.flatnonzero(
            bundle.universe2
            & (bundle.consecutive[index - 1] >= 2)
            & bundle.age50[index]
            & valid
        )
        if candidates.size:
            counts = bundle.consecutive[index - 1, candidates].astype("int64")
            highest = int(counts.max())
            candidates = candidates[counts == highest]
            scores = bundle.vol5[index, candidates]
            order = np.lexsort((bundle.symbols[candidates], scores))
            signals["s2_consecutive_leader"][index] = candidates[order]

        # 7. 追首板涨停：主板首板，高开0-6%，成交额7-19亿，市值70-520亿，左压。
        candidates = np.flatnonzero(
            bundle.universe7
            & bundle.limit_up[index - 1]
            & ~bundle.limit_up[index - 2]
            & ~bundle.limit_up[index - 3]
            & bundle.age50[index]
            & valid
            & (bundle.amount_prev[index] >= 700_000_000.0)
            & (bundle.amount_prev[index] <= 1_900_000_000.0)
            & (bundle.avg_price_increase[index] >= -0.04)
            & (bundle.float_mv_prev[index] >= 7_000_000_000.0)
            & (bundle.float_mv_prev[index] <= 52_000_000_000.0)
            & (bundle.open_ratio[index] > 1.0)
            & (bundle.open_ratio[index] < 1.0602)
        )
        if candidates.size:
            candidates = np.array(
                [
                    column
                    for column in candidates
                    if _left_pressure_ok(bundle, index, column)
                ],
                dtype="int64",
            )
        if candidates.size:
            signals["s7_chase_first_board"][index] = candidates[
                np.argsort(bundle.symbols[candidates])
            ]

        # 13. 首板高开 + 首板低开 + 弱转强。
        first = (
            bundle.universe13
            & bundle.limit_up[index - 1]
            & ~bundle.limit_up[index - 2]
            & bundle.age50[index]
            & valid
        )
        low = (
            first
            & (bundle.rp60[index] <= 0.5)
            & (bundle.amount_prev[index] >= 100_000_000.0)
            & (bundle.open_ratio[index] >= 0.955)
            & (bundle.open_ratio[index] <= 0.97)
        )
        high = (
            first
            & ~low
            & (bundle.avg_price_increase_1_1[index] >= 0.07)
            & (bundle.amount_prev[index] >= 550_000_000.0)
            & (bundle.amount_prev[index] <= 2_000_000_000.0)
            & (bundle.float_mv_prev[index] >= 7_000_000_000.0)
            & (bundle.float_mv_prev[index] <= 52_000_000_000.0)
            & (bundle.open_ratio[index] > 1.0)
            & (bundle.open_ratio[index] < 1.06)
        )
        weak = (
            bundle.universe13
            & bundle.touched[index - 1]
            & ~bundle.limit_up[index - 2]
            & bundle.age50[index]
            & valid
            & (bundle.three_day_ret[index] <= 0.28)
            & (bundle.open_close_prev[index] >= -0.05)
            & (bundle.avg_price_increase[index] >= -0.04)
            & (bundle.amount_prev[index] >= 300_000_000.0)
            & (bundle.amount_prev[index] <= 1_900_000_000.0)
            & (bundle.float_mv_prev[index] >= 7_000_000_000.0)
            & (bundle.float_mv_prev[index] <= 52_000_000_000.0)
            & (bundle.current_ratio_ws[index] > 0.98)
            & (bundle.current_ratio_ws[index] < 1.09)
        )

        high_columns = np.flatnonzero(high)
        if high_columns.size:
            high_columns = np.array(
                [
                    column
                    for column in high_columns
                    if _left_pressure_ok(bundle, index, column)
                ],
                dtype="int64",
            )
        weak_columns = np.flatnonzero(weak)
        if weak_columns.size:
            weak_columns = np.array(
                [
                    column
                    for column in weak_columns
                    if _left_pressure_ok(bundle, index, column)
                ],
                dtype="int64",
            )
        ordered = [
            *high_columns[np.argsort(bundle.symbols[high_columns])],
            *np.flatnonzero(low)[np.argsort(bundle.symbols[np.flatnonzero(low)])],
            *weak_columns[np.argsort(bundle.symbols[weak_columns])],
        ]
        if ordered:
            signals["s13_mixed"][index] = np.array(ordered, dtype="int64")
    return signals


def signal_summary(
    bundle: Bundle, signals: dict[str, dict[int, np.ndarray]]
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    years = bundle.dates.year
    for name, by_date in signals.items():
        rows = []
        for index, columns in by_date.items():
            rows.extend(
                [
                    {
                        "date": bundle.dates[index],
                        "symbol": str(bundle.symbols[column]),
                        "year": int(years[index]),
                    }
                    for column in columns
                ]
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            summary[name] = {"signals": 0, "days": 0, "by_year": {}}
            continue
        summary[name] = {
            "signals": int(len(frame)),
            "days": int(frame["date"].nunique()),
            "avg_per_day": round(float(len(frame) / frame["date"].nunique()), 2),
            "by_year": {
                str(int(year)): int(count)
                for year, count in frame.groupby("year").size().items()
            },
        }
    return summary


# ---------------------------------------------------------------------------
# 组合模拟
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    strategy: str
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: int
    gross_entry: float
    entry_fee: float
    gross_exit: float
    exit_fee: float
    net_pnl: float
    net_return: float
    hold_days: int
    exit_reason: str


@dataclass
class Position:
    symbol: str
    column: int
    qty: int
    entry_index: int
    entry_date: pd.Timestamp
    entry_price: float
    entry_cost: float
    last_price: float
    stale_days: int = 0

    @property
    def avg_cost(self) -> float:
        return self.entry_cost / self.qty


def _fee(gross: float, sell: bool = False) -> float:
    value = max(gross * COMMISSION, MIN_COMMISSION)
    if sell:
        value += gross * STAMP_TAX
    return value


def _buy_fill(
    bundle: Bundle, index: int, column: int, strategy: str
) -> float | None:
    """开盘成交价；返回 None 表示放弃（一字板/开盘涨停）。"""

    price = float(bundle.open[index, column])
    previous = float(bundle.close_prev[index, column])
    if not math.isfinite(price) or price <= 0 or not math.isfinite(previous) or previous <= 0:
        return None
    rate = float(bundle.rate[index, column])
    open_return = price / previous - 1.0
    at_limit_up = open_return >= rate - 0.005

    if strategy == "s2_consecutive_leader":
        if at_limit_up:
            low_return = float(bundle.low[index, column]) / previous - 1.0
            if low_return >= rate - 0.005:
                return None
            # 源码用 LimitOrderStyle(day_open) 排板；按涨停价成交。
            return float(bundle.high_limit_price[index, column])
        return price * (1.0 + SLIPPAGE)

    if at_limit_up:
        return None
    return price * (1.0 + SLIPPAGE)


class StrategyPortfolio:
    def __init__(self, strategy: str, max_weight: float = 1.0):
        self.strategy = strategy
        self.max_weight = max_weight
        self.cash = INITIAL_CAPITAL
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.buy_notional = 0.0
        # 源码：策略2固定10仓；其余按当日候选数平分可用资金。
        self.max_positions = 10 if strategy == "s2_consecutive_leader" else 50

    def mark_price(self, bundle: Bundle, index: int, position: Position) -> float:
        """收盘盯市价：当天有成交用收盘价，否则沿用 ``last_price``。"""

        close = float(bundle.close[index, position.column])
        volume = float(bundle.volume[index, position.column])
        if math.isfinite(close) and math.isfinite(volume) and volume > 0:
            return close
        return position.last_price

    def _equity(self, bundle: Bundle, index: int, mark: str = "close") -> float:
        value = self.cash
        for position in self.positions.values():
            price = (
                self.mark_price(bundle, index, position)
                if mark == "close"
                else position.last_price
            )
            value += position.qty * price
        return value

    def buy_signals(
        self, bundle: Bundle, index: int, columns: np.ndarray
    ) -> None:
        if columns.size == 0:
            return
        strategy = self.strategy

        if strategy == "s1_first_board_low_open":
            # 源码：只在空仓时买；total_value / 当日候选数。
            if self.positions:
                return
            equity = self._equity(bundle, index, mark="last")
            target = min(equity / len(columns), equity * self.max_weight)
            for column in columns:
                self._buy_one(bundle, index, int(column), target)
            return

        if strategy == "s2_consecutive_leader":
            slots = self.max_positions - len(self.positions)
            if slots <= 0:
                return
            equity = self._equity(bundle, index, mark="last")
            target = min(equity / PS, equity * self.max_weight)
            for column in columns[:slots]:
                self._buy_one(bundle, index, int(column), target)
            return

        # 策略7/13：available_cash / 当日候选数，买全部候选；再受单票上限约束。
        target = min(
            self.cash / len(columns),
            self._equity(bundle, index, mark="last") * self.max_weight,
        )
        for column in columns:
            self._buy_one(bundle, index, int(column), target)

    def _buy_one(
        self, bundle: Bundle, index: int, column: int, target_cash: float
    ) -> None:
        symbol = str(bundle.symbols[column])
        if symbol in self.positions or len(self.positions) >= self.max_positions:
            return
        fill = _buy_fill(bundle, index, column, self.strategy)
        if fill is None or not math.isfinite(fill) or fill <= 0:
            return
        budget = min(target_cash, self.cash)
        qty = int(budget / fill // LOT) * LOT
        while qty >= LOT:
            gross = fill * qty
            cost = gross + _fee(gross)
            if cost <= self.cash:
                break
            qty -= LOT
        if qty < LOT:
            return
        gross = fill * qty
        fee = _fee(gross)
        cost = gross + fee
        if cost > self.cash:
            return
        self.cash -= cost
        self.buy_notional += gross
        self.positions[symbol] = Position(
            symbol=symbol,
            column=column,
            qty=qty,
            entry_index=index,
            entry_date=bundle.dates[index],
            entry_price=fill,
            entry_cost=cost,
            last_price=fill,
        )

    def sell_positions(self, bundle: Bundle, index: int) -> None:
        for position in list(self.positions.values()):
            if index <= position.entry_index:
                continue
            close = float(bundle.close[index, position.column])
            volume = float(bundle.volume[index, position.column])
            tradable = math.isfinite(close) and math.isfinite(volume) and volume > 0
            if not tradable:
                position.stale_days += 1
                if position.stale_days >= 60:
                    self._exit(bundle, index, position, position.last_price, "stale")
                continue
            position.stale_days = 0
            position.last_price = close

            limit_up = bool(bundle.limit_up[index, position.column])
            limit_down = bool(bundle.limit_down[index, position.column])
            if limit_down:
                continue
            if limit_up and self.strategy != "s13_mixed":
                continue
            if self._should_sell(bundle, index, position, close):
                self._exit(bundle, index, position, close, "signal")

    def _should_sell(
        self, bundle: Bundle, index: int, position: Position, close: float
    ) -> bool:
        strategy = self.strategy
        if strategy in ("s1_first_board_low_open", "s7_chase_first_board"):
            # 源码 11:28/14:50 均会清仓；日线用当天的收盘价近似。
            return True
        if strategy == "s2_consecutive_leader":
            # 源码：T+1 有盈利即卖；否则到 T+2 卖；涨停不卖。
            return (index >= position.entry_index + 2) or (close > position.avg_cost)
        if strategy == "s13_mixed":
            # 源码：止盈（高于成本且未涨停）或跌破 MA5 止损。
            previous_four = float(bundle.ma5_prev4[index, position.column])
            if not math.isfinite(previous_four):
                return True
            ma5 = (previous_four * 4.0 + close) / 5.0
            take_profit = close > position.avg_cost and not bool(
                bundle.limit_up[index, position.column]
            )
            stop_loss = close < ma5
            return take_profit or stop_loss
        return True

    def _exit(
        self,
        bundle: Bundle,
        index: int,
        position: Position,
        raw_price: float,
        reason: str,
    ) -> None:
        price = float(raw_price)
        if not math.isfinite(price) or price <= 0:
            price = position.entry_price
        price *= 1.0 - SLIPPAGE
        gross = price * position.qty
        fee = _fee(gross, sell=True)
        proceeds = gross - fee
        self.cash += proceeds
        entry_gross = position.entry_price * position.qty
        self.trades.append(
            Trade(
                strategy=self.strategy,
                symbol=position.symbol,
                entry_date=position.entry_date,
                exit_date=bundle.dates[index],
                entry_price=position.entry_price,
                exit_price=price,
                qty=position.qty,
                gross_entry=entry_gross,
                entry_fee=position.entry_cost - entry_gross,
                gross_exit=gross,
                exit_fee=fee,
                net_pnl=proceeds - position.entry_cost,
                net_return=(proceeds - position.entry_cost) / position.entry_cost,
                hold_days=index - position.entry_index,
                exit_reason=reason,
            )
        )
        del self.positions[position.symbol]


def _benchmark(
    bundle: Bundle, start_index: int, end_index: int
) -> pd.Series:
    dates = bundle.dates[start_index : end_index + 1]
    values = np.ones(len(dates), dtype="float64")
    universe = bundle.universe2
    for offset, index in enumerate(range(start_index + 1, end_index + 1), start=1):
        previous = bundle.close_prev[index]
        current = bundle.close[index]
        with np.errstate(divide="ignore", invalid="ignore"):
            returns = current / previous - 1.0
        mask = (
            universe
            & np.isfinite(previous)
            & (previous > 0)
            & np.isfinite(current)
            & np.isfinite(returns)
            & np.isfinite(bundle.volume[index])
            & (bundle.volume[index] > 0)
        )
        values[offset] = (
            values[offset - 1] * (1.0 + float(np.mean(returns[mask])))
            if mask.any()
            else values[offset - 1]
        )
    return pd.Series(values * INITIAL_CAPITAL, index=dates)


def _annualized(equity: pd.Series) -> float:
    periods = len(equity) - 1
    if periods <= 0:
        return 0.0
    return float((equity.iloc[-1] / equity.iloc[0]) ** (252.0 / periods) - 1.0)


def _drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1.0).min())


def _yearly(equity: pd.Series) -> dict[str, float]:
    result: dict[str, float] = {}
    previous = float(equity.iloc[0])
    for year, group in equity.groupby(equity.index.year):
        end = float(group.iloc[-1])
        result[str(int(year))] = end / previous - 1.0
        previous = end
    return result


def _metrics(
    strategy: str,
    portfolio: StrategyPortfolio,
    equity: pd.Series,
    exposure: pd.Series,
    benchmark: pd.Series,
) -> dict[str, Any]:
    returns = equity.pct_change().dropna()
    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    annualized = _annualized(equity)
    volatility = float(returns.std(ddof=1) * math.sqrt(252.0)) if len(returns) > 1 else 0.0
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else None
    )
    drawdown = _drawdown(equity)
    calmar = annualized / abs(drawdown) if drawdown < 0 else None

    pnls = np.array([trade.net_pnl for trade in portfolio.trades], dtype="float64")
    trade_returns = np.array(
        [trade.net_return for trade in portfolio.trades], dtype="float64"
    )
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    profit_factor = (
        float(wins.sum() / abs(losses.sum())) if losses.size and wins.size else None
    )
    win_rate = float((pnls > 0).mean()) if pnls.size else None
    average_trade = float(trade_returns.mean()) if trade_returns.size else None
    average_hold = (
        float(np.mean([trade.hold_days for trade in portfolio.trades]))
        if portfolio.trades
        else None
    )

    bench_total = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1.0)
    bench_returns = benchmark.pct_change().dropna()
    bench_sharpe = (
        float(
            bench_returns.mean() / bench_returns.std(ddof=1) * math.sqrt(252.0)
        )
        if len(bench_returns) > 1 and bench_returns.std(ddof=1) > 0
        else None
    )

    return {
        "strategy": strategy,
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
        "trading_days": int(len(equity)),
        "signals_traded": int(len(portfolio.trades)),
        "total_return": total,
        "annualized_return": annualized,
        "volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "calmar": calmar,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "average_trade_return": average_trade,
        "average_hold_days": average_hold,
        "average_exposure": float(exposure.mean()),
        "final_equity": float(equity.iloc[-1]),
        "buy_notional": float(portfolio.buy_notional),
        "yearly_returns": _yearly(equity),
        "benchmark_total_return": bench_total,
        "benchmark_annualized_return": _annualized(benchmark),
        "benchmark_max_drawdown": _drawdown(benchmark),
        "benchmark_sharpe": bench_sharpe,
        "benchmark_yearly_returns": _yearly(benchmark),
    }


def simulate(
    bundle: Bundle,
    signals: dict[str, dict[int, np.ndarray]],
    strategy: str,
    start_date: str,
    end_date: str,
    min_price: float = 0.0,
    max_weight: float = 1.0,
) -> tuple[StrategyPortfolio, pd.Series, pd.Series, pd.Series]:
    start_index = max(3, int(bundle.dates.searchsorted(pd.Timestamp(start_date))))
    end_index = int(
        bundle.dates.searchsorted(pd.Timestamp(end_date), side="right") - 1
    )
    if end_index <= start_index:
        raise ValueError("end date must be after start date")

    portfolio = StrategyPortfolio(strategy, max_weight=max_weight)
    dates: list[pd.Timestamp] = []
    equities: list[float] = []
    exposures: list[float] = []
    by_date = signals.get(strategy, {})

    for index in range(start_index, end_index + 1):
        if index < end_index:
            columns = by_date.get(index)
            if columns is not None and len(columns):
                if min_price > 0:
                    columns = columns[
                        bundle.close_prev[index, columns] >= min_price
                    ]
                if len(columns):
                    portfolio.buy_signals(bundle, index, columns)
        portfolio.sell_positions(bundle, index)
        equity = portfolio._equity(bundle, index, mark="close")
        dates.append(bundle.dates[index])
        equities.append(equity)
        exposures.append((equity - portfolio.cash) / equity if equity > 0 else 0.0)

    if portfolio.positions:
        for position in list(portfolio.positions.values()):
            raw = portfolio.mark_price(bundle, end_index, position)
            portfolio._exit(bundle, end_index, position, raw, "end_of_sample")
        equities[-1] = portfolio.cash
        exposures[-1] = 0.0

    equity_series = pd.Series(equities, index=pd.DatetimeIndex(dates))
    exposure_series = pd.Series(exposures, index=pd.DatetimeIndex(dates))
    benchmark = _benchmark(bundle, start_index, end_index)
    return portfolio, equity_series, exposure_series, benchmark


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _clean_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_json(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return None if not math.isfinite(number) else number
    if isinstance(value, pd.Timestamp):
        return str(value.date())
    return value


def _pct(value: float | None, digits: int = 1) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value) * 100:.{digits}f}%"


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value):.{digits}f}"


def _write_memo(
    out: Path,
    args: argparse.Namespace,
    signal_stats: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
) -> None:
    names = {
        "s1_first_board_low_open": "1 首板低开",
        "s2_consecutive_leader": "2 连板龙头",
        "s7_chase_first_board": "7 追首板涨停",
        "s13_mixed": "13 高开/低开/弱转强",
    }
    lines = [
        "# 四个聚宽首板/连板策略：日线机械复现",
        "",
        f"- 区间：{args.start} .. {args.end}",
        "- 数据：本地 qfq 日线，含退市股；2018-01 起有完整历史，2019-01 起回测。",
        "- 成本：佣金 3bp/边（最低 5 元）、卖出印花税 10bp、滑点 10bp/边。",
        "- 组合：每个策略独立 100 万；按源码仓位规则（策略1/7/13 当日候选平分可用资金，策略2 固定 10 仓）。",
        "",
        "## 信号数",
        "",
        "| 策略 | 信号 | 有信号交易日 | 日均 |",
        "|---|---:|---:|---:|",
    ]
    if getattr(args, "min_price", 0.0) > 0:
        lines.insert(
            5,
            f"- 敏感性过滤：买入前收盘价 < {args.min_price:.2f} 元的候选剔除"
            "（ST/退市风险代理，非源码规则）。",
        )
    if getattr(args, "max_weight", 1.0) < 1.0:
        lines.insert(
            5,
            f"- 单票上限：每只股票最多占当前总资产的 "
            f"{args.max_weight * 100:.2f}%；源码为当日候选等分、单票可满仓。",
        )
    for key in metrics:
        name = names[key]
        stat = signal_stats.get(key, {})
        lines.append(
            f"| {name} | {stat.get('signals', 0)} | {stat.get('days', 0)} | "
            f"{stat.get('avg_per_day', 0)} |"
        )

    lines += [
        "",
        "## 绩效",
        "",
        "| 策略 | 交易 | 总收益 | 年化 | 最大回撤 | Sharpe | 胜率 | 盈亏比 | 平均持有 | 平均仓位 | 期末权益 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, item in metrics.items():
        name = names[key]
        lines.append(
            f"| {name} | {item['signals_traded']} | {_pct(item['total_return'])} | "
            f"{_pct(item['annualized_return'])} | {_pct(item['max_drawdown'])} | "
            f"{_fmt(item['sharpe'])} | {_pct(item['win_rate'])} | "
            f"{_fmt(item['profit_factor'])} | "
            f"{_fmt(item['average_hold_days'], 1)} | "
            f"{_pct(item['average_exposure'])} | "
            f"{item['final_equity'] / 10000:.1f}万 |"
        )

    bench = next(iter(metrics.values()))
    lines += [
        "",
        f"基准（等权主板+创业板，日再平衡，无成本）：总收益 "
        f"{_pct(bench['benchmark_total_return'])}，年化 "
        f"{_pct(bench['benchmark_annualized_return'])}，最大回撤 "
        f"{_pct(bench['benchmark_max_drawdown'])}，Sharpe "
        f"{_fmt(bench['benchmark_sharpe'])}。",
        "",
        "## 年度收益",
        "",
        "| 年份 | "
        + " | ".join(names[key] for key in metrics)
        + " | 基准 |",
        "|---|" + "---:|" * len(metrics) + "---:|",
    ]
    years = sorted(
        {
            year
            for item in metrics.values()
            for year in item["yearly_returns"]
        }
    )
    for year in years:
        row = [f"| {year} "]
        for key in metrics:
            row.append(f"| {_pct(metrics[key]['yearly_returns'].get(year))} ")
        row.append(f"| {_pct(bench['benchmark_yearly_returns'].get(year))} |")
        lines.append("".join(row))

    ranked = sorted(
        metrics.values(), key=lambda item: item["annualized_return"], reverse=True
    )
    best_return = ranked[0]
    best_drawdown = min(
        metrics.values(), key=lambda item: abs(item["max_drawdown"])
    )
    most_trades = max(metrics.values(), key=lambda item: item["signals_traded"])
    lines += [
        "",
        "## 结论（机械口径）",
        "",
        f"- 年化最高：{names[best_return['strategy']]}（{_pct(best_return['annualized_return'])}，"
        f"最大回撤 {_pct(best_return['max_drawdown'])}）。",
        f"- 回撤最小：{names[best_drawdown['strategy']]}（{_pct(best_drawdown['max_drawdown'])}）。",
        f"- 交易最多：{names[most_trades['strategy']]}（{most_trades['signals_traded']} 笔）。",
        "- 源码标题收益（如 304%、1138.61%）不可直接比较：本复现缺少集合竞价量、"
        "盘中 11:25/14:50 价格、历史 ST 名单和真实总市值。",
    ]
    s7 = metrics.get("s7_chase_first_board")
    if s7:
        y23 = s7["yearly_returns"].get("2023")
        y24 = s7["yearly_returns"].get("2024")
        if y23 is not None and y24 is not None:
            two_year = (1.0 + y23) * (1.0 + y24) - 1.0
            lines.append(
                f"- 策略7 在 2023-2024 复现为 {_pct(y23)} / {_pct(y24)}"
                f"（两年复合 {_pct(two_year)}），方向与源码标题一致；后续年份需单独看。"
            )
    s13 = metrics.get("s13_mixed")
    if s13:
        y25 = s13["yearly_returns"].get("2025")
        signal_count = signal_stats.get("s13_mixed", {}).get("signals", 0)
        lines.append(
            f"- 策略13 2025 复现为 {_pct(y25)}；信号数 {signal_count} 远高于源码"
            "（缺集合竞价量过滤），不能据此否定源码。"
        )
    lines += [
        "",
        "## 关键口径与偏差",
        "",
        "- **集合竞价量缺失**：策略 7/13 的 `auction_volume / prev_volume >= 3%` 无法计算，"
        "已省略；这会增加信号数，尤其是小盘/低流动性股票。",
        "- **盘中卖点**：源码在 11:25/11:28/14:50 卖出；本脚本统一用收盘价，"
        "无法复现盘中止盈/止损的成交价。策略2 的 T+1 盈利即卖尤其依赖 14:50 价格；"
        "收盘价近似会把盘中冲高回落误判为未盈利，从而拖到 T+2，放大亏损。",
        "- **涨停/跌停**：开盘涨停不买（策略2 非一字板按涨停价排板）；收盘涨停不卖；"
        "收盘跌停不卖。qfq 价格用 0.5% 容差识别涨跌停。",
        "- **T+1**：买入当日不可卖；策略2 的 T+1 盈利即卖、T+2 强制卖按收盘价近似。",
        "- **ST/退市**：无历史 ST 标记；退市股保留在面板中，停牌超过 60 个交易日按最后价强平。",
        "- **市值**：用 `成交额 / 换手率` 近似流通市值；源码用 `circulating_market_cap` 和 `market_cap` 混合过滤。",
        "- **左压**：按源码的 101 日窗口和 `volume[-1] > 0.9 * max(volume[:-1])` 复现；"
        "成交量单位差异已把东财退市股从手换算为股。",
        "- **期末**：最后一个交易日不做新开仓，剩余持仓按收盘价强平并计成本。",
        "",
    ]
    (out / "memo.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--signals-only", action="store_true")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument(
        "--min-price",
        type=float,
        default=0.0,
        help="可选的 ST/退市风险敏感性过滤：买入前收盘价不低于该值",
    )
    parser.add_argument(
        "--max-weight",
        type=float,
        default=1.0,
        help="单票最大仓位占当前总资产比例，默认 1.0；例如 1/3 用 0.3333",
    )
    parser.add_argument(
        "--strategies",
        default="s1_first_board_low_open,s2_consecutive_leader,"
        "s7_chase_first_board,s13_mixed",
    )
    args = parser.parse_args()
    if not 0 < args.max_weight <= 1.0:
        raise ValueError("--max-weight must be in (0, 1]")

    print("loading bundle ...", flush=True)
    bundle = load_bundle()
    print(
        f"{len(bundle.dates)} dates x {len(bundle.symbols)} symbols "
        f"({bundle.dates.min().date()} .. {bundle.dates.max().date()})",
        flush=True,
    )
    print("building signals ...", flush=True)
    signals = build_signals(bundle)
    signal_stats = signal_summary(bundle, signals)
    for name, values in signal_stats.items():
        print(name, values, flush=True)

    if args.signals_only:
        return

    selected = [value.strip() for value in args.strategies.split(",") if value.strip()]
    unknown = [value for value in selected if value not in signals]
    if unknown:
        raise ValueError(f"unknown strategies: {unknown}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict[str, Any]] = {}
    all_trades: list[Trade] = []
    curve_frames: list[pd.DataFrame] = []
    benchmark: pd.Series | None = None

    for strategy in selected:
        print(f"simulating {strategy} ...", flush=True)
        portfolio, equity, exposure, bench = simulate(
            bundle,
            signals,
            strategy,
            args.start,
            args.end,
            args.min_price,
            args.max_weight,
        )
        if benchmark is None:
            benchmark = bench
        results[strategy] = _metrics(
            strategy, portfolio, equity, exposure, bench
        )
        all_trades.extend(portfolio.trades)
        curve_frames.append(
            pd.DataFrame(
                {
                    "date": equity.index,
                    "strategy": strategy,
                    "equity": equity.to_numpy(),
                    "benchmark": bench.to_numpy(),
                }
            )
        )
        print(
            f"  {strategy}: trades={len(portfolio.trades)} "
            f"total={_pct(results[strategy]['total_return'])} "
            f"ann={_pct(results[strategy]['annualized_return'])} "
            f"mdd={_pct(results[strategy]['max_drawdown'])}",
            flush=True,
        )

    if benchmark is None:
        raise RuntimeError("no strategies selected")

    pd.concat(curve_frames, ignore_index=True).to_csv(
        out / "curves.csv", index=False
    )
    trade_rows = [asdict(trade) for trade in all_trades]
    if trade_rows:
        pd.DataFrame(trade_rows).to_csv(out / "trades.csv", index=False)
    else:
        pd.DataFrame(
            columns=list(Trade.__dataclass_fields__.keys())
        ).to_csv(out / "trades.csv", index=False)
    flat_metrics = []
    for key, item in results.items():
        flat_metrics.append(
            {
                name: value
                for name, value in item.items()
                if name not in ("yearly_returns", "benchmark_yearly_returns")
            }
        )
    pd.DataFrame(flat_metrics).to_csv(out / "metrics.csv", index=False)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "data": {
            "dates": int(len(bundle.dates)),
            "symbols": int(len(bundle.symbols)),
            "start": str(bundle.dates.min().date()),
            "end": str(bundle.dates.max().date()),
        },
        "signals": signal_stats,
        "strategies": results,
        "benchmark": {
            "description": "等权主板+创业板，日再平衡，无成本",
            "total_return": results[selected[0]]["benchmark_total_return"],
            "annualized_return": results[selected[0]][
                "benchmark_annualized_return"
            ],
            "max_drawdown": results[selected[0]]["benchmark_max_drawdown"],
            "sharpe": results[selected[0]]["benchmark_sharpe"],
            "yearly_returns": results[selected[0]]["benchmark_yearly_returns"],
        },
        "caveats": [
            "集合竞价成交量缺失，策略7/13的竞价量过滤未执行",
            "盘中11:25/11:28/14:50卖点用收盘价近似",
            "无历史ST名单；退市股停牌60个交易日后按最后价强平",
            "流通市值用成交额/换手率近似",
            "期末强平，最后一日不新开仓",
        ],
    }
    (out / "summary.json").write_text(
        json.dumps(_clean_json(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_memo(out, args, signal_stats, results)
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
