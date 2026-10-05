"""回测 macd_divergence 研究池策略 (独立, 不修改生产配置与生产回测脚本)。

信号源: strategy/macd_divergence.py 的四条件 AND (零轴金叉 + 底背离 + 放量 + 年线以上)。
成交/风控: 复用 backtest_winrate 的同一套执行语义 (次日开盘买入、T+1、涨跌停、
佣金/印花税/滑点、固定止损止盈、持仓超时), 因此结果与既有回测可比。

用法:
    python backtest_macd_divergence.py --limit 300 --start 2023-01-01
    python backtest_macd_divergence.py --min-volume-ratio 1.0 --out report_v1.json

输出: 逐笔信号、组合资金曲线、与逐条件放宽的对比 (volume/背离 是否加门槛)。
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(BASE_DIR.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import backtest_winrate as bt  # noqa: E402
from backtest_winrate import (  # noqa: E402
    _bar_price_limits,
    _build_trade,
    _execution_values,
    next_bar_index,
)
from strategy.macd_divergence import (  # noqa: E402
    MIN_BARS,
    _calculate_macd,
    add_divergence_indicators,
    prepare_divergence_bars,
    resolve_divergence_config,
)
from utils.helpers import load_config  # noqa: E402


def _completed_cycles(hist: np.ndarray, low: np.ndarray, min_bars: int) -> list[tuple[int, float, float]]:
    """Completed negative histogram runs as (end_index, abs_area, lowest_low)."""
    negative = np.isfinite(hist) & (hist < 0)
    if not negative.any():
        return []
    changes = np.flatnonzero(np.diff(negative.astype(np.int8)) != 0) + 1
    bounds = np.concatenate(([0], changes, [len(negative)]))
    cycles: list[tuple[int, float, float]] = []
    for start, stop in zip(bounds[:-1], bounds[1:]):
        if not negative[start]:
            continue
        if stop - start < min_bars:
            continue
        segment = hist[start:stop]
        prices = low[start:stop]
        cycles.append((stop - 1, float(np.abs(segment).sum()), float(np.nanmin(prices))))
    return cycles


def _divergence_flags(
    hist: np.ndarray,
    low: np.ndarray,
    min_bars: int,
) -> np.ndarray:
    """Per-bar boolean: two latest completed negative cycles show bottom divergence.

    Fast equivalent of strategy.macd_divergence.bottom_divergence_at, verified
    against it in tests. Only cycles closed strictly before the bar count.
    """
    cycles = _completed_cycles(hist, low, min_bars)
    flags = np.zeros(len(hist), dtype=bool)
    if len(cycles) < 2:
        return flags
    index = 2
    for position in range(1, len(cycles)):
        end_prev, area_prev, low_prev = cycles[position - 1]
        end_cur, area_cur, low_cur = cycles[position]
        if area_prev <= 0 or low_prev == 0:
            continue
        divergent = bool(low_cur < low_prev and area_cur < area_prev)
        start = max(end_cur + 1, index)
        if start >= len(flags):
            break
        if divergent:
            flags[start:] = True
        index = start
        if not divergent:
            flags[start:] = False
    return flags


def build_entries(
    symbol: str,
    closed: pd.DataFrame,
    settings: dict[str, Any],
    min_volume_ratio: float | None = None,
) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Return (buy records, per-bar all-four-condition mask)."""
    frame = add_divergence_indicators(closed, settings)
    if frame.empty or len(frame) < MIN_BARS:
        return [], np.zeros(0, dtype=bool)
    dif = pd.to_numeric(frame["dif"], errors="coerce").to_numpy(dtype=float)
    dea = pd.to_numeric(frame["dea"], errors="coerce").to_numpy(dtype=float)
    hist = pd.to_numeric(frame["hist"], errors="coerce").to_numpy(dtype=float)
    low = pd.to_numeric(frame["low"], errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    ma_long = pd.to_numeric(frame["ma_long"], errors="coerce").to_numpy(dtype=float)
    ma_long_prev = pd.to_numeric(frame["ma_long_prev"], errors="coerce").to_numpy(dtype=float)
    volume_ratio = pd.to_numeric(frame["volume_ratio"], errors="coerce").to_numpy(dtype=float)
    cross = frame["golden_cross"].to_numpy(dtype=bool)
    tolerance = float(settings["zero_axis_tolerance"])
    threshold = float(
        settings["min_volume_ratio"] if min_volume_ratio is None else min_volume_ratio
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        distance = np.maximum(np.abs(dif), np.abs(dea)) / np.maximum(np.abs(close), 1e-12)
        near = distance <= tolerance
        zone_ok = near | (dif * dea <= 0) | ((dif > 0) & (dea > 0))
    volume_ok = volume_ratio >= threshold
    above = (close > ma_long) & (ma_long > ma_long_prev)
    divergence = _divergence_flags(hist, low, int(settings["min_macd_segment_bars"]))
    mask = (
        np.nan_to_num(cross, nan=False).astype(bool)
        & np.nan_to_num(zone_ok, nan=False).astype(bool)
        & np.nan_to_num(volume_ok, nan=False).astype(bool)
        & np.nan_to_num(above, nan=False).astype(bool)
        & divergence
    )
    valid = np.arange(len(frame)) >= (MIN_BARS - 1)
    mask &= valid
    buys: list[dict[str, Any]] = []
    dates = pd.to_datetime(frame["datetime"])
    for index in np.flatnonzero(mask):
        index = int(index)
        zone = "above" if (dif[index] > 0 and dea[index] > 0) else "near"
        buys.append(
            {
                "symbol": symbol,
                "day": str(dates.iloc[index].date()),
                "signal_type": "macd_divergence_bottom",
                "side": "buy",
                "price": float(close[index]),
                "confirmed_at": str(dates.iloc[index]),
                "zone": zone,
                "volume_ratio": float(volume_ratio[index]),
                "area_ratio": None,
            }
        )
    return buys, mask


def build_production_entries(
    symbol: str,
    closed: pd.DataFrame,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Production MACD pullback-confirmed entries, for a head-to-head baseline.

    Delegates to backtest_winrate.find_signals so the baseline uses the exact
    production signal definition (no re-implementation drift).
    """
    found = bt.find_signals(closed, config)
    buys: list[dict[str, Any]] = []
    for record in found.get("buy", []):
        if not str(record.get("signal_type", "")).startswith(
            "macd_golden_cross_pullback_confirmed_"
        ):
            continue
        buys.append({"symbol": symbol, **record})
    return buys


def build_yearline_entries(
    symbol: str,
    closed: pd.DataFrame,
    vol_mult: float = 1.5,
    tolerance: float = 0.025,
    cooldown: int = 20,
) -> list[dict[str, Any]]:
    """Yearline-pool entries, mirrored from yearline_trend_strategy_backtest.

    Covers both production entry types (A_breakout: volume close above the
    yearline; B_pullback: touch-and-hold of the yearline). Indicators are the
    same causal definitions, so the comparison runs on shared execution.
    """
    import yearline_trend_strategy_backtest as yl

    frame = yl.add_indicators(closed)
    if len(frame) < yl.MA_YEAR + yl.MA_SLOPE_LOOKBACK + 5:
        return []
    signals = yl.generate_signals(
        symbol,
        "",
        frame,
        vol_mult=vol_mult,
        tol=tolerance,
        cooldown=cooldown,
    )
    return [
        {
            "symbol": symbol,
            "day": str(pd.Timestamp(signal.entry_date).date()),
            "signal_type": f"yearline_{signal.entry_type}",
            "side": "buy",
            "price": float(signal.entry_price),
            "confirmed_at": str(pd.Timestamp(signal.entry_date)),
            "volume_ratio": float(signal.vol_ratio),
        }
        for signal in signals
    ]


def top_divergence_flags(
    hist: pd.Series,
    close: pd.Series,
    min_segment_bars: int,
) -> np.ndarray:
    """Per-bar MACD top-divergence *trigger* flags (single bar per cycle).

    Mirror of backtest_winrate._top_divergence_risk: a top divergence exists when
    the latest completed positive-histogram cycle makes a higher price high with a
    smaller positive area than the prior cycle. The flag is raised only on the bar
    where that cycle completes (hist turns non-positive), so a divergence formed
    before entry can never trigger an exit later -- no stale signal, no lookahead.
    """
    histogram = pd.to_numeric(hist, errors="coerce").to_numpy(dtype=float)
    closes = pd.to_numeric(close, errors="coerce").to_numpy(dtype=float)
    flags = np.zeros(len(histogram), dtype=bool)
    if len(histogram) < 3:
        return flags
    positive = np.isfinite(histogram) & (histogram > 0)
    changes = np.flatnonzero(np.diff(positive.astype(np.int8)) != 0) + 1
    bounds = np.concatenate(([0], changes, [len(positive)]))
    cycles: list[tuple[int, float, float]] = []
    for run_start, run_stop in zip(bounds[:-1], bounds[1:]):
        if not positive[run_start] or run_stop - run_start < min_segment_bars:
            continue
        segment_hist = histogram[run_start:run_stop]
        segment_close = closes[run_start:run_stop]
        area = float(segment_hist.sum())
        if area <= 0 or not np.isfinite(segment_close).any():
            continue
        # Trigger bar: first bar after the positive run (where it becomes known).
        cycles.append((run_stop, area, float(np.nanmax(segment_close))))
    for position in range(1, len(cycles)):
        trigger, area, high = cycles[position]
        _, prior_area, prior_high = cycles[position - 1]
        if trigger >= len(flags):
            break
        flags[trigger] = bool(high > prior_high and area < prior_area)
    return flags


def build_exit_levels(closed: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    """Causal daily exit levels: MA20, MA250 (yearline) and MACD DIF/DEA."""
    frame = prepare_divergence_bars(closed)
    if frame.empty:
        return {}
    macd_config = (config.get("macd_divergence") or {}).get("macd") or {}
    macd = _calculate_macd(
        frame["close"],
        int(macd_config.get("fast", 12)),
        int(macd_config.get("slow", 26)),
        int(macd_config.get("signal", 9)),
    )
    return {
        "dates": [item.date() for item in pd.to_datetime(frame["datetime"])],
        "close": pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float),
        "open": pd.to_numeric(frame["open"], errors="coerce").to_numpy(dtype=float),
        "low": pd.to_numeric(frame["low"], errors="coerce").to_numpy(dtype=float),
        "high": pd.to_numeric(frame["high"], errors="coerce").to_numpy(dtype=float),
        "ma20": frame["close"].rolling(20, min_periods=20).mean().to_numpy(dtype=float),
        "ma250": frame["close"].rolling(250, min_periods=250).mean().to_numpy(dtype=float),
        "dif": macd["dif"].to_numpy(dtype=float),
        "dea": macd["dea"].to_numpy(dtype=float),
    }


# 命名退出方案: 每条都是一组规则 + 参数, 便于一次性对比。
EXIT_RULE_SETS: dict[str, dict[str, Any]] = {
    # 旧基线: 固定止损止盈 + 40 根超时 (由 backtest_winrate 实现, 不走本表)
    "prod_8_30_40": {"mode": "production"},
    # 用户给出的初版: 顶背离 / 跌破MA20 / 跌破年线 / 止损, 无超时
    "v0_all_no_timeout": {
        "rules": ("stop", "top_divergence", "ma20", "below_yearline"),
    },
    # 变体 1a: 顶背离 + 止损 + 40天超时 (去掉 MA20/年线)
    "v1_div_stop_timeout": {
        "rules": ("stop", "top_divergence", "timeout"),
    },
    # 变体 1b: 顶背离 + 止损 + 40天超时 + 年线
    "v1b_div_stop_timeout_ma250": {
        "rules": ("stop", "top_divergence", "timeout", "below_yearline"),
    },
    # 变体 2: 顶背离 + 止损 + 移动止盈 (无超时)
    "v2_div_stop_trailing": {
        "rules": ("stop", "top_divergence", "trailing"),
    },
    # 变体 2b: 顶背离 + 止损 + 移动止盈 + 40天超时
    "v2b_div_stop_trail_timeout": {
        "rules": ("stop", "top_divergence", "trailing", "timeout"),
    },
    # 变体 3a: MA20 放宽为"连续 3 天站不上"
    "v3_ma20_3days": {
        "rules": ("stop", "top_divergence", "ma20_3days", "timeout"),
    },
    # 变体 3b: MA20 放宽为"跌破且 MACD 死叉"
    "v3b_ma20_death_cross": {
        "rules": ("stop", "top_divergence", "ma20_death_cross", "timeout"),
    },
    # 变体 4: 移动止盈梯度 (涨 15% 后回撤 8%)
    "v4_trailing_15_8": {
        "rules": ("stop", "trailing", "timeout"),
        "trail_arm_pct": 0.15,
        "trail_gap_pct": 0.08,
    },
    # 变体 4b: 移动止盈更紧 (涨 10% 后回撤 5%)
    "v4b_trailing_10_5": {
        "rules": ("stop", "trailing", "timeout"),
        "trail_arm_pct": 0.10,
        "trail_gap_pct": 0.05,
    },
    # 变体 4c: 移动止盈更松 (涨 20% 后回撤 10%)
    "v4c_trailing_20_10": {
        "rules": ("stop", "trailing", "timeout"),
        "trail_arm_pct": 0.20,
        "trail_gap_pct": 0.10,
    },
    # 组合最优候选: 顶背离 + 移动止盈 + 放宽MA20 + 45天超时
    "v5_combo": {
        "rules": ("stop", "top_divergence", "trailing", "ma20_death_cross", "timeout"),
        "trail_arm_pct": 0.15,
        "trail_gap_pct": 0.08,
    },
}


EXIT_RULE_CHOICES = (
    "stop",            # -8% 硬止损 (盘中触发, 止损优先)
    "ma20",            # 收盘跌破 MA20 即离场
    "ma20_3days",      # 连续 3 根收盘在 MA20 下方才离场 (放宽)
    "ma20_death_cross",# 收盘跌破 MA20 且 MACD 处于死叉状态 (放宽)
    "below_yearline",  # 收盘跌破年线
    "top_divergence",  # 日线 MACD 顶背离 (面积收缩 + 更高高点)
    "timeout",         # 持仓满 max_holding_bars 根 K 线
    "trailing",        # 浮盈超过 trail_arm_pct 后, 回撤 trail_gap_pct 卖出
)


def simulate_with_rules(
    symbol: str,
    closed: pd.DataFrame,
    buy: dict[str, Any],
    config: dict[str, Any],
    settings: dict[str, Any],
    costs: dict[str, Any],
    divergence_flags: np.ndarray,
    rules: tuple[str, ...],
    trail_arm_pct: float = 0.15,
    trail_gap_pct: float = 0.08,
    ma20_confirm_bars: int = 3,
) -> tuple[dict[str, Any] | None, str | None]:
    """Long-only trade under a composable, causal exit rule set.

    ``rules`` lists the enabled exits; the first rule that fires on a bar wins,
    except the stop loss which is intrabar and always evaluated first. Price
    exits signal on the close and fill at the next open. Every rule reads only
    bars up to the one being evaluated.
    """
    del settings  # exit rules do not depend on the entry settings
    if not rules:
        raise ValueError("at least one exit rule is required")
    unknown = [rule for rule in rules if rule not in EXIT_RULE_CHOICES]
    if unknown:
        raise ValueError(f"unknown exit rules: {unknown}")
    execution = _execution_values(costs)
    levels = build_exit_levels(closed, config)
    if not levels:
        return None, "no_bars"
    dates: list[date] = levels["dates"]
    signal_day = date.fromisoformat(str(buy["day"]))
    entry_idx = next_bar_index(closed, dates, signal_day)
    if entry_idx is None:
        return None, "missing_entry_bar"
    entry_price = float(levels["open"][entry_idx])
    if not np.isfinite(entry_price) or entry_price <= 0:
        return None, "invalid_entry_price"
    limits = _bar_price_limits(symbol, closed, entry_idx, execution)
    if limits is not None and entry_price >= limits[0] - 0.0001:
        return None, "entry_limit_up"

    stop_loss_pct = float(execution["stop_loss_pct"] or 0.0)
    stop_price = entry_price * (1.0 - stop_loss_pct) if stop_loss_pct > 0 else None
    timeout_bars = int(execution["max_holding_bars"]) if "timeout" in rules else None
    dif_series = levels["dif"]
    dea_series = levels["dea"]
    below_ma20_run = 0
    peak_close = entry_price
    reason: str | None = None
    exit_idx = entry_idx
    exit_price = entry_price
    exit_session = "open"

    for index in range(entry_idx + 1, len(dates)):
        bar_open = float(levels["open"][index])
        bar_close = float(levels["close"][index])
        bar_low = float(levels["low"][index])
        held_bars = index - entry_idx

        # 0) scheduled timeout: the engine sells at this bar's open without
        #    consulting the bar's low, so mirror that ordering exactly.
        if timeout_bars is not None and held_bars == timeout_bars:
            reason = "timeout"
            exit_idx = index
            exit_price = bar_open if np.isfinite(bar_open) else bar_close
            exit_session = "open"
            break

        # 1) intrabar hard stop first
        if "stop" in rules and stop_price is not None and np.isfinite(bar_low) and bar_low <= stop_price:
            reason = "stop_loss"
            exit_idx = index
            exit_price = min(bar_open, stop_price) if np.isfinite(bar_open) else stop_price
            exit_session = "open"
            break

        # 2) trailing profit lock, evaluated on the close
        if "trailing" in rules and np.isfinite(bar_close):
            peak_close = max(peak_close, bar_close)
            if peak_close >= entry_price * (1.0 + trail_arm_pct):
                trail_price = peak_close * (1.0 - trail_gap_pct)
                if bar_close <= trail_price:
                    reason = "trailing_stop"

        # 3) MA / divergence / timeout exits
        if reason is None:
            ma20 = float(levels["ma20"][index])
            ma250 = float(levels["ma250"][index])
            if "ma20" in rules and np.isfinite(ma20) and bar_close < ma20:
                reason = "below_ma20"
            elif "ma20_3days" in rules and np.isfinite(ma20):
                below_ma20_run = below_ma20_run + 1 if bar_close < ma20 else 0
                if below_ma20_run >= ma20_confirm_bars:
                    reason = "below_ma20_3days"
            elif (
                "ma20_death_cross" in rules
                and np.isfinite(ma20)
                and bar_close < ma20
                and np.isfinite(dif_series[index])
                and np.isfinite(dea_series[index])
                # death-cross *state*, not the crossing bar: the cross usually
                # happens days away from the MA20 break, so requiring the exact
                # bar would leave the rule inert.
                and float(dif_series[index]) < float(dea_series[index])
            ):
                reason = "below_ma20_death_cross"
            elif "below_yearline" in rules and np.isfinite(ma250) and bar_close < ma250:
                reason = "below_yearline"
            elif (
                "top_divergence" in rules
                and index < len(divergence_flags)
                and bool(divergence_flags[index])
            ):
                reason = "top_divergence"

        if reason is not None:
            # Signal on the close -> fill at the next open; without a next
            # session fall back to this close.
            if index + 1 < len(dates):
                exit_idx = index + 1
                exit_price = float(levels["open"][exit_idx])
                exit_session = "open"
            else:
                exit_idx = index
                exit_price = bar_close
                exit_session = "close"
            break
    else:
        last = len(dates) - 1
        reason = "open_at_end"
        exit_idx = last
        exit_price = float(levels["close"][last])
        exit_session = "close"

    if reason is None:
        return None, "no_exit_resolved"
    if not np.isfinite(exit_price) or exit_price <= 0:
        return None, "invalid_exit_price"
    trade = _build_trade(
        symbol,
        closed,
        dates,
        buy,
        entry_idx,
        exit_idx,
        float(exit_price),
        reason,
        exit_idx,
        exit_session,
        execution,
        None,
        0,
    )
    if buy.get("zone"):
        trade["entry_zone"] = buy["zone"]
    trade["holding_bars"] = exit_idx - entry_idx
    return trade, None


def simulate_with_exit_rules(
    symbol: str,
    closed: pd.DataFrame,
    buy: dict[str, Any],
    config: dict[str, Any],
    settings: dict[str, Any],
    costs: dict[str, Any],
    divergence_flags: np.ndarray,
) -> tuple[dict[str, Any] | None, str | None]:
    """Backwards-compatible wrapper: the original full rule set, no timeout."""
    return simulate_with_rules(
        symbol,
        closed,
        buy,
        config,
        settings,
        costs,
        divergence_flags,
        ("stop", "top_divergence", "ma20", "below_yearline"),
    )


# 组合层年化 / 夏普。引擎的 summary 只给累计收益与最大回撤,
# 而"资金在不同时间被占用"的组合必须按日频权益序列算, 否则不可比。
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.0


def portfolio_risk_metrics(equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
    """Annualized return / Sharpe / exposure from the daily equity curve.

    Wall-clock calendar days, not session count: equal-span comparisons between
    strategies that trade on the same dataset therefore use the same span.
    """
    points = sorted(
        ((str(point["day"]), float(point["equity"])) for point in equity_curve),
        key=lambda item: item[0],
    )
    if len(points) < 2:
        return {
            "annualized_return_pct": None,
            "annualized_volatility_pct": None,
            "sharpe_ratio": None,
            "span_days": None,
        }
    first_day = date.fromisoformat(points[0][0])
    last_day = date.fromisoformat(points[-1][0])
    span_days = (last_day - first_day).days
    initial = points[0][1]
    final = points[-1][1]
    if initial <= 0 or span_days <= 0:
        return {
            "annualized_return_pct": None,
            "annualized_volatility_pct": None,
            "sharpe_ratio": None,
            "span_days": span_days or None,
        }
    annualized = (final / initial) ** (365.25 / span_days) - 1.0
    returns = [
        points[index][1] / points[index - 1][1] - 1.0
        for index in range(1, len(points))
        if points[index - 1][1] > 0
    ]
    if len(returns) > 1:
        daily_std = float(np.std(returns, ddof=1))
        mean_daily = float(np.mean(returns)) - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR
        volatility = daily_std * (TRADING_DAYS_PER_YEAR ** 0.5)
        sharpe = mean_daily / daily_std * (TRADING_DAYS_PER_YEAR ** 0.5) if daily_std else None
    else:
        volatility = 0.0
        sharpe = None
    return {
        "annualized_return_pct": round(annualized * 100.0, 2),
        "annualized_volatility_pct": round(volatility * 100.0, 2),
        "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
        "span_days": span_days,
    }


# 买入信号 → run_arm 的 signal_name。交叉臂用它把"买入策略"和"卖出规则"解耦:
# 同一个买入信号可以分别配 fixed / v1b 两套卖出规则, 差异只来自卖出规则。
CROSS_SIGNALS: dict[str, str] = {
    "baseline": "production_macd_pullback",   # 日线零轴金叉+回落确认 (实盘推送)
    "yearline": "yearline_all",               # 年线趋势 (A突破 + B回踩)
    "diverge": "divergence_v1",               # 零轴+底背离 四条件 AND
}


def run_arm(
    args: argparse.Namespace,
    signal_name: str,
    settings: dict[str, Any] | None,
    config: dict[str, Any],
    costs: dict[str, Any],
    portfolio_config: dict[str, Any],
    paths: list[Path],
    start: date,
    end: date,
    exit_mode: str = "production",
    rules: tuple[str, ...] = ("stop", "top_divergence", "ma20", "below_yearline"),
    rule_params: dict[str, Any] | None = None,
    engine_exit_mode: str = "divergence_trend",
    engine_scope: tuple[str, ...] = ("macd_divergence_bottom",),
) -> dict[str, Any]:
    """Run one arm (divergence variant or the production baseline).

    exit_mode="production" keeps the shipped exits (8/30%, 40-bar timeout) so
    old and new numbers stay comparable; exit_mode="rules" swaps in the research
    exits (top divergence / MA20 / MA250 / stop, no timeout).
    """
    # 资金容量与入场抽样从 CLI 读取, 这样所有臂共用同一套扫描参数。
    entry_step = max(int(getattr(args, "entry_step", 1) or 1), 1)
    entry_offset = max(int(getattr(args, "entry_offset", 0) or 0), 0)
    # 全局候选计数器: 抽样必须按**全部候选**取模, 不能按单只股票取模——
    # 否则 1 个信号的股票在 offset>0 时会被整只丢掉, 样本量塌陷。
    seen_candidates = 0
    rule_params = dict(rule_params or {})
    # The legacy arm must keep the pre-v1b behaviour even though the config now
    # selects divergence_trend by default; otherwise it silently duplicates v1b.
    if exit_mode == "production":
        costs = {**costs, "exit_mode": "fixed", "exit_rules_scope": []}
    trades: list[dict[str, Any]] = []
    rejections: Counter = Counter()
    stats = {
        "symbols_with_history": 0,
        "symbols_evaluated": 0,
        "signal_count": 0,
        "insufficient_history": 0,
    }
    for path in paths:
        symbol = path.name.split("_")[0]
        try:
            raw = pd.read_pickle(path)
        except Exception:
            continue
        closed = bt.prepare_closed_bars(raw)
        if closed is None or len(closed) < MIN_BARS:
            stats["insufficient_history"] += 1
            continue
        stats["symbols_with_history"] += 1
        if signal_name == "production_macd_pullback":
            buys = build_production_entries(symbol, closed, config)
        elif signal_name.startswith("yearline_"):
            buys = build_yearline_entries(symbol, closed)
        else:
            assert settings is not None
            buys, _ = build_entries(symbol, closed, settings)
        dates = [item.date() for item in pd.to_datetime(closed["datetime"])]
        window = [b for b in buys if start <= date.fromisoformat(str(b["day"])) <= end]
        # 入场抽样: 超订策略 (18033 个信号 / 4 个仓位) 的组合结果由"抽到哪 0.5%"决定。
        # 用全局计数器取模, 保证每份样本恰好占 1/step 且互不重叠; 若按单只股票取模,
        # "只有 1 个信号"的股票在 offset>0 时会被整只丢掉, 样本量会塌陷。
        candidate_count = len(window)
        if entry_step > 1:
            window = [
                buy
                for index, buy in enumerate(window, start=seen_candidates)
                if index % entry_step == entry_offset
            ]
        seen_candidates += candidate_count
        if not window:
            continue
        stats["symbols_evaluated"] += 1
        stats["signal_count"] += len(window)
        flags = None
        if exit_mode == "rules":
            frame = add_divergence_indicators(closed, settings or {})
            # min_segment_bars=1 gives exact parity with the shipped
            # backtest_winrate._top_divergence_risk detector (verified).
            flags = top_divergence_flags(frame["hist"], frame["close"], 1)
        arm_costs = costs
        if exit_mode == "engine":
            arm_costs = copy.deepcopy(costs)
            arm_costs["exit_mode"] = engine_exit_mode
            arm_costs["exit_rules_scope"] = list(engine_scope)
            if engine_exit_mode == "divergence_trend":
                arm_costs["exit_rules_config"] = dict(costs.get("exit_rules_config") or {})
        for buy in window:
            if exit_mode == "engine":
                trade, reason = bt.simulate_single_trade(
                    symbol, closed, dates, buy, {}, arm_costs
                )
            elif exit_mode == "rules":
                trade, reason = simulate_with_rules(
                    symbol, closed, buy, config, settings or {}, costs, flags, rules, **rule_params
                )
            else:
                trade, reason = bt.simulate_single_trade(
                    symbol, closed, dates, buy, {}, costs
                )
            if trade is None:
                rejections[reason or "unknown"] += 1
                continue
            trades.append(trade)
    by_reason: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        by_reason[str(trade.get("exit_reason") or "unknown")].append(
            float(trade.get("pnl_pct") or 0.0)
        )
    arm: dict[str, Any] = {
        "signal": signal_name,
        "stats": stats,
        "execution_rejections": dict(rejections),
        "trade_count": len(trades),
        "entry_sampling": {"step": entry_step, "offset": entry_offset},
        "exit_reasons": {
            reason: {
                "count": len(values),
                "share_pct": round(100.0 * len(values) / max(len(trades), 1), 2),
                "avg_pnl_pct": round(float(np.mean(values)), 2),
                "total_pnl_pp": round(float(np.sum(values)), 1),
            }
            for reason, values in sorted(
                by_reason.items(), key=lambda item: -len(item[1])
            )
        },
    }
    if trades:
        arm["single_trade_summary"] = bt.summarize(trades)
        # 组合层受"先到先得"排序影响极大, 因此报多排序区间而非单点。
        tie_breaks = getattr(args, "tie_break_list", None) or [
            str(portfolio_config.get("tie_break", "symbol_asc"))
        ]
        variants: dict[str, Any] = {}
        # 仓位上限扫描: 一组 trades 只算一次(贵), 组合层反复跑(便宜)。
        # 每档单笔仓位 = 1/仓位上限, 也就是"满仓时正好用完全部资金";
        # 这样各档位之间比较的是**能同时容纳多少个信号**, 而不是下注金额。
        # A 股 100 股一手, 高仓位档位需要更大本金, 故配合 --initial-cash 使用。
        raw_levels = str(getattr(args, "slots_list", "") or "").strip()
        if not raw_levels:
            # 兼容旧的 --slots int 形式 (既有审计脚本仍在用)。
            legacy_slots = int(getattr(args, "slots", 0) or 0)
            raw_levels = str(legacy_slots) if legacy_slots > 0 else ""
        slot_levels = (
            [int(item) for item in raw_levels.split(",") if item.strip()]
            if raw_levels
            else [int(portfolio_config.get("max_positions", 4))]
        )
        cash_per_slot = float(getattr(args, "cash_per_slot", 0.0) or 0.0)
        fixed_pct = float(getattr(args, "position_pct", 0.0) or 0.0)
        absolute_cash = float(getattr(args, "initial_cash", 0.0) or 0.0)
        for level in slot_levels:
            slot_count = max(int(level), 1)
            for tie_break in tie_breaks:
                variant_config = copy.deepcopy(portfolio_config)
                variant_config["tie_break"] = tie_break
                variant_config["max_positions"] = slot_count
                # 默认按 1/仓位上限 等权; --position-pct 可固定为 config 口径
                # (如 25%), 用来观察"仓位占用不匹配时第 5 个仓位买不进"的真实效果。
                variant_config["position_size_pct"] = (
                    fixed_pct if fixed_pct > 0 else 1.0 / slot_count
                )
                if absolute_cash > 0:
                    variant_config["initial_cash"] = absolute_cash
                if cash_per_slot > 0:
                    # 单笔金额固定 = cash_per_slot, 总本金 = 单笔 × 仓位上限。
                    # 这样各档比较的是"能同时装多少个 2.5 万的仓位", 而不是下注金额;
                    # 若不随档位放大本金, 高仓位档位会被"买不起一手"挡掉, 看起来像容量不足。
                    variant_config["initial_cash"] = cash_per_slot * slot_count
                result = bt.run_portfolio(
                    copy.deepcopy(trades), costs, variant_config
                )
                summary = dict(result["summary"])
                summary.update(portfolio_risk_metrics(result["equity_curve"]))
                # 占用率: 平均同时持仓数 / 仓位上限。它区分"持仓少是因为没信号"
                # 与"是因为没资金"——这是判断资金瓶颈的直接证据。
                average_positions = result["attribution"].get("average_positions")
                summary["average_positions"] = average_positions
                summary["occupancy_pct"] = round(
                    float(average_positions or 0.0) / max(slot_count, 1) * 100.0,
                    2,
                )
                variants[f"slots{level if level >= 0 else 'inf'}:{tie_break}"] = {
                    "summary": summary,
                    "rejection_reasons": result["rejection_reasons"],
                }
        arm["portfolio_variants"] = variants
        primary = f"slots{slot_levels[0] if slot_levels[0] >= 0 else 'inf'}:{tie_breaks[0]}"
        arm["portfolio_summary"] = variants[primary]["summary"]
        arm["portfolio_rejections"] = variants[primary]["rejection_reasons"]
    return arm


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(str(Path(args.config).expanduser().resolve()))
    if config is None:
        raise RuntimeError("unable to load config")
    settings = resolve_divergence_config(config)
    costs = bt._resolve_execution_config(config)
    position = config.get("position", {}) or {}
    portfolio_config = {
        "initial_cash": float(costs.get("initial_cash", 100000.0)),
        "max_positions": int(position.get("max_stocks", 4)),
        "position_size_pct": float(position.get("base_position_per_stock", 0.25)),
        "lot_size": int(costs.get("lot_size", 100)),
        "signal_priority": costs.get("signal_priority"),
        "score_mode": "P0",
        "tie_break": "symbol_asc",
        "seed": int(args.seed),
    }

    paths = sorted((BASE_DIR / "cache" / "daily_history").glob("*_qfq.pkl"))
    if args.symbols:
        wanted = {item.strip().zfill(6) for item in args.symbols.split(",") if item.strip()}
        paths = [p for p in paths if p.name.split("_")[0] in wanted]
    if args.limit:
        paths = paths[: args.limit]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    # Arms: the four-condition AND plus threshold sweeps and the production
    # signal on identical data/execution, so differences come from the signal.
    # Selectable so the expensive production (chan) arm can run in parallel.
    arm_specs: list[tuple[str, dict[str, Any] | None]] = []
    for token in [
        item.strip()
        for item in args.arms.replace("+", ",").split(",")
        if item.strip()
    ]:
        if token in EXIT_RULE_SETS:
            spec = EXIT_RULE_SETS[token]
            if spec.get("mode") == "production":
                arm_specs.append((token, ("production", None, {})))
            else:
                arm_specs.append(
                    (
                        token,
                        (
                            "rules",
                            tuple(spec["rules"]),
                            {
                                key: value
                                for key, value in spec.items()
                                if key != "rules"
                            },
                        ),
                    )
                )
        elif token in {"eng_v1b", "eng_fixed"}:
            arm_specs.append((token, {"__engine__": token}))
        elif token == "v1":
            arm_specs.append(("divergence_v1", copy.deepcopy(settings)))
        elif token.startswith("vol:"):
            ratio = float(token.split(":", 1)[1])
            variant = copy.deepcopy(settings)
            variant["min_volume_ratio"] = ratio
            arm_specs.append((f"divergence_vol_{ratio}", variant))
        elif token == "no_div":
            variant = copy.deepcopy(settings)
            variant["_skip_divergence"] = True
            arm_specs.append(("divergence_no_bottom_divergence", variant))
        elif token == "baseline":
            arm_specs.append(("production_macd_pullback", None))
        elif token == "yearline":
            arm_specs.append(("yearline_all", None))
        elif "x" in token and token.split("x", 1)[0].strip() in CROSS_SIGNALS:
            # 买入×卖出 交叉臂: "baseline x v1b" / "yearline x fixed"。
            # 口径与已提交的 eng_* 臂完全一致, 只是把卖出规则换成通配作用域,
            # 这样"同一买入信号换卖出规则"的差异只来自卖出规则本身。
            buy_token, _, exit_token = token.partition("x")
            buy_token, exit_token = buy_token.strip(), exit_token.strip()
            if exit_token not in {"fixed", "v1b"}:
                raise SystemExit(f"cross arm exit must be fixed|v1b, got {exit_token!r}")
            signal_name = CROSS_SIGNALS[buy_token]
            arm_specs.append(
                (
                    f"{buy_token}__{exit_token}",
                    {
                        "__engine__": "cross",
                        "__buy__": signal_name,
                        "__exit__": exit_token,
                        "__buy_token__": buy_token,
                    },
                )
            )
        else:
            raise SystemExit(f"unknown arm: {token}")
    if args.min_volume_ratio is not None:
        for _, spec in arm_specs:
            if isinstance(spec, dict):
                spec["min_volume_ratio"] = float(args.min_volume_ratio)

    arms: dict[str, Any] = {}
    for name, arm_settings in arm_specs:
        if isinstance(arm_settings, dict) and arm_settings.get("__engine__") == "cross":
            exit_token = arm_settings["__exit__"]
            arms[name] = run_arm(
                args,
                arm_settings["__buy__"],
                copy.deepcopy(settings),
                config,
                costs,
                portfolio_config,
                paths,
                start,
                end,
                exit_mode="engine",
                engine_exit_mode="divergence_trend" if exit_token == "v1b" else "fixed",
                # 通配作用域: 让 v1b 卖出规则作用到这条买入信号的全部 signal_type
                # (年线池有 yearline_A_breakout / yearline_B_pullback 两种类型)。
                engine_scope=("*",) if exit_token == "v1b" else (),
            )
            arms[name]["buy_token"] = arm_settings["__buy_token__"]
            arms[name]["exit_token"] = exit_token
            continue
        # Engine-path arms: run the shipped simulator with the config's exits.
        if isinstance(arm_settings, dict) and "__engine__" in arm_settings:
            arms[name] = run_arm(
                args,
                name,
                copy.deepcopy(settings),
                config,
                costs,
                portfolio_config,
                paths,
                start,
                end,
                exit_mode="engine",
                engine_exit_mode=(
                    "divergence_trend" if arm_settings["__engine__"] == "eng_v1b" else "fixed"
                ),
                engine_scope=(
                    ("macd_divergence_bottom",)
                    if arm_settings["__engine__"] == "eng_v1b"
                    else ()
                ),
            )
            continue
        # Named exit-rule sets arrive as ("rules"|"production", rules, params).
        if isinstance(arm_settings, tuple):
            arm_exit_mode, arm_rules, arm_rule_params = arm_settings
            arms[name] = run_arm(
                args,
                name,
                copy.deepcopy(settings),
                config,
                costs,
                portfolio_config,
                paths,
                start,
                end,
                exit_mode=arm_exit_mode,
                rules=arm_rules or ("stop", "top_divergence", "ma20", "below_yearline"),
                rule_params=arm_rule_params,
            )
            continue
        if arm_settings is not None and arm_settings.pop("_skip_divergence", False):
            arms[name] = _run_skip_divergence_arm(
                args, name, arm_settings, costs, portfolio_config, paths, start, end
            )
            continue
        arms[name] = run_arm(
            args,
            name,
            arm_settings,
            config,
            costs,
            portfolio_config,
            paths,
            start,
            end,
            exit_mode=args.exit_mode,
        )

    result = {
        "window": {"start": args.start, "end": args.end},
        "universe": {
            "history_dir": str(BASE_DIR / "cache" / "daily_history"),
            "symbols_considered": len(paths),
            "adjust": "qfq",
        },
        "execution": {
            "stop_loss_pct": costs.get("stop_loss_pct"),
            "take_profit_pct": costs.get("take_profit_pct"),
            "max_holding_bars": costs.get("chan_zero_axis", {}).get("max_holding_bars"),
            "commission_pct": costs.get("commission_pct"),
            "stamp_tax_pct": costs.get("stamp_tax_pct"),
            "slippage_pct": costs.get("slippage_pct"),
            "t_plus_one": costs.get("t_plus_one"),
            "price_limit_model": costs.get("price_limit_model"),
            "portfolio": portfolio_config,
        },
        "exit_mode": args.exit_mode,
        "gates_applied": {
            "market_gate": False,
            "stock_pool": False,
            "note": "两条臂都不加市场/股票池闸门, 差异只来自信号本身",
        },
        "arms": arms,
    }
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = BASE_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        result["output_file"] = str(out_path)
    return result


def _run_skip_divergence_arm(
    args: argparse.Namespace,
    name: str,
    settings: dict[str, Any],
    costs: dict[str, Any],
    portfolio_config: dict[str, Any],
    paths: list[Path],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Ablation: same three gates, bottom-divergence requirement removed."""
    trades: list[dict[str, Any]] = []
    rejections: Counter = Counter()
    stats = {"symbols_with_history": 0, "symbols_evaluated": 0, "signal_count": 0}
    for path in paths:
        symbol = path.name.split("_")[0]
        try:
            raw = pd.read_pickle(path)
        except Exception:
            continue
        closed = bt.prepare_closed_bars(raw)
        if closed is None or len(closed) < MIN_BARS:
            continue
        stats["symbols_with_history"] += 1
        buys = _build_entries_no_divergence(symbol, closed, settings)
        dates = [item.date() for item in pd.to_datetime(closed["datetime"])]
        window = [b for b in buys if start <= date.fromisoformat(str(b["day"])) <= end]
        if not window:
            continue
        stats["symbols_evaluated"] += 1
        stats["signal_count"] += len(window)
        for buy in window:
            trade, reason = bt.simulate_single_trade(
                symbol, closed, dates, buy, {}, costs
            )
            if trade is None:
                rejections[reason or "unknown"] += 1
                continue
            trades.append(trade)
    arm: dict[str, Any] = {
        "signal": name,
        "stats": stats,
        "execution_rejections": dict(rejections),
        "trade_count": len(trades),
    }
    if trades:
        arm["single_trade_summary"] = bt.summarize(trades)
        portfolio_result = bt.run_portfolio(
            copy.deepcopy(trades), costs, copy.deepcopy(portfolio_config)
        )
        arm["portfolio_summary"] = portfolio_result["summary"]
        arm["portfolio_rejections"] = portfolio_result["rejection_reasons"]
    del args
    return arm


def _build_entries_no_divergence(
    symbol: str,
    closed: pd.DataFrame,
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Golden cross + volume + yearline, without the divergence requirement."""
    frame = add_divergence_indicators(closed, settings)
    if frame.empty or len(frame) < MIN_BARS:
        return []
    dif = pd.to_numeric(frame["dif"], errors="coerce").to_numpy(dtype=float)
    dea = pd.to_numeric(frame["dea"], errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    ma_long = pd.to_numeric(frame["ma_long"], errors="coerce").to_numpy(dtype=float)
    ma_long_prev = pd.to_numeric(frame["ma_long_prev"], errors="coerce").to_numpy(dtype=float)
    volume_ratio = pd.to_numeric(frame["volume_ratio"], errors="coerce").to_numpy(dtype=float)
    tolerance = float(settings["zero_axis_tolerance"])
    with np.errstate(invalid="ignore", divide="ignore"):
        distance = np.maximum(np.abs(dif), np.abs(dea)) / np.maximum(np.abs(close), 1e-12)
        zone_ok = (distance <= tolerance) | (dif * dea <= 0) | ((dif > 0) & (dea > 0))
    mask = (
        np.nan_to_num(frame["golden_cross"].to_numpy(dtype=bool), nan=False).astype(bool)
        & np.nan_to_num(zone_ok, nan=False).astype(bool)
        & np.nan_to_num(volume_ratio >= float(settings["min_volume_ratio"]), nan=False).astype(bool)
        & np.nan_to_num((close > ma_long) & (ma_long > ma_long_prev), nan=False).astype(bool)
        & (np.arange(len(frame)) >= (MIN_BARS - 1))
    )
    dates = pd.to_datetime(frame["datetime"])
    return [
        {
            "symbol": symbol,
            "day": str(dates.iloc[int(index)].date()),
            "signal_type": "macd_divergence_no_divergence",
            "side": "buy",
            "price": float(close[int(index)]),
            "confirmed_at": str(dates.iloc[int(index)]),
        }
        for index in np.flatnonzero(mask)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest the macd_divergence research pool")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--symbols", type=str, default=None, help="comma separated codes")
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--min-volume-ratio", type=float, default=None)
    parser.add_argument(
        "--arms",
        default="v1",
        help="comma list: v1,vol:1.0,vol:1.2,vol:2.0,no_div,baseline",
    )
    parser.add_argument("--mode", choices=("signal", "portfolio"), default="portfolio")
    parser.add_argument(
        "--exit-mode",
        choices=("production", "rules"),
        default="production",
        help="production = 固定止损止盈 + 40 根超时; rules = 顶背离/MA20/MA250/止损",
    )
    parser.add_argument(
        "--tie-breaks",
        default="symbol_asc",
        help="comma list of portfolio tie-breaks; range exposes ordering luck",
    )
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument(
        "--slots-list",
        default="",
        help="仓位上限列表, 如 4,10,20,50; 空 = 用 config 的 max_stocks",
    )
    parser.add_argument(
        "--cash-per-slot",
        type=float,
        default=0.0,
        help="单笔金额 (0 = 用 config); 总本金 = 单笔 × 仓位档位, 使各档位可比较",
    )
    parser.add_argument(
        "--position-pct",
        type=float,
        default=0.0,
        help="固定单笔仓位比例 (0 = 按 1/仓位档位等权); 用于复现 config 的 25%% 口径",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=0.0,
        help="覆盖初始资金 (0 = 用 config); 与 --position-pct 配合模拟固定本金",
    )
    parser.add_argument(
        "--entry-step",
        type=int,
        default=1,
        help="入场抽样步长 (>1 时只保留 1/step 的信号); 用于区分超订策略的真实表现与抽样运气",
    )
    parser.add_argument("--entry-offset", type=int, default=0, help="抽样起始偏移")
    parser.add_argument("--out", default="divergence_backtest_report.json")
    args = parser.parse_args()
    args.tie_break_list = [
        item.strip() for item in args.tie_breaks.split(",") if item.strip()
    ]
    result = run(args)
    compact = {
        "window": result["window"],
        "universe": result["universe"],
        "execution": result["execution"],
        "arms": {
            name: {
                "signal_count": arm["stats"]["signal_count"],
                "trade_count": arm["trade_count"],
                "win_rate": arm.get("single_trade_summary", {}).get("win_rate"),
                "avg_pnl_pct": arm.get("single_trade_summary", {}).get("avg_pnl_pct"),
                "profit_factor": arm.get("single_trade_summary", {}).get("profit_factor"),
                "portfolio": {
                    key: arm.get("portfolio_summary", {}).get(key)
                    for key in (
                        "count",
                        "win_rate",
                        "avg_pnl_pct",
                        "profit_factor",
                        "total_return_pct",
                        "max_drawdown_pct",
                        "final_equity",
                        "avg_holding_days",
                    )
                },
            }
            for name, arm in result["arms"].items()
        },
        "output_file": result.get("output_file"),
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
