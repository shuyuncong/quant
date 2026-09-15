"""年线(MA250)趋势候选指标与回踩信号 —— 研究展示池专用。

信号定义移植自已回测的 yearline_pullback 入场规则, 全部只读信号日收盘
及以前的数据, 无前视偏差:

  1. MA250[t] > MA250[t-20]                   年线上行
  2. MA60[t] > MA120[t] > MA250[t]            中长期均线多头排列
  3. close[t-1] > MA250[t-1]                  前一交易日收盘已在年线上方
  4. MA250[t]*(1-0.5%) <= low[t] <= MA250[t]*(1+2%)   当日最低进入年线回踩区间
  5. close[t] >= MA250[t]                     当日收盘不破年线
  6. close[t] >= open[t]                      当日收阳(或收平)

信号在收盘确认, 入场参考为下一交易日开盘 (仅记录字段, 不下单)。

动态止损建议 (research_only 展示字段, 不修改生产固定 8% 止损):
    stop_suggestion = clip(2 * ATR14[t] / close[t], 5%, 8%)

"放量站上年线"突破路线此前研究筛选未通过, 本模块不实现该路线。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

POOL_TYPE_YEARLINE = "yearline_pullback"
PULLBACK_ZONE_LOW = -0.005  # 回踩区间下界: 年线下方 0.5%
PULLBACK_ZONE_HIGH = 0.02  # 回踩区间上界: 年线上方 2%
STOP_FLOOR = 0.05
STOP_CEIL = 0.08
MIN_BARS = 270  # MA250 + 20 日斜率 + 少量余量


def add_yearline_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """在日线框上追加年线指标列 (因果, 逐行只用当日及以前数据)。

    期望输入列: datetime/open/high/low/close/volume。
    """
    out = df.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    out["ma60"] = close.rolling(60).mean()
    out["ma120"] = close.rolling(120).mean()
    out["ma250"] = close.rolling(250).mean()
    out["ma250_prev"] = out["ma250"].shift(1)
    out["ma250_20ago"] = out["ma250"].shift(20)
    out["ma250_slope"] = out["ma250"] - out["ma250_20ago"]
    prev_close = close.shift(1)
    prev_ma250 = out["ma250"].shift(1)
    out["above250_prev"] = prev_close > prev_ma250
    out["ma_align"] = (out["ma60"] > out["ma120"]) & (out["ma120"] > out["ma250"])
    out["ma250_up"] = out["ma250"] > out["ma250_20ago"]
    low = pd.to_numeric(out["low"], errors="coerce")
    out["low_in_zone"] = (
        (low >= out["ma250"] * (1 + PULLBACK_ZONE_LOW))
        & (low <= out["ma250"] * (1 + PULLBACK_ZONE_HIGH))
    )
    out["close_hold"] = close >= out["ma250"]
    out["close_ge_open"] = close >= pd.to_numeric(out["open"], errors="coerce")
    # Wilder ATR14
    high = pd.to_numeric(out["high"], errors="coerce")
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14 = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    out["atr14"] = atr14
    out["atr14_pct"] = atr14 / close * 100.0
    volume = pd.to_numeric(out["volume"], errors="coerce")
    vol_ma5 = volume.rolling(5).mean()
    out["vol_ma5"] = vol_ma5
    out["volume_ratio"] = volume / vol_ma5
    return out


def pullback_signal_at(df: pd.DataFrame, i: int) -> Optional[dict[str, Any]]:
    """评估第 i 根 K 线的 yearline_pullback 信号; 命中返回候选字典, 否则 None。

    只读 <=i 的数据, 因果无前视。调用方需保证 df 已过 add_yearline_indicators。
    """
    if i < 0:
        i = len(df) - 1
    if i < MIN_BARS - 1:
        return None
    row = df.iloc[i]
    fields = [
        "ma250", "ma250_slope", "ma60", "ma120", "atr14", "atr14_pct",
        "volume_ratio", "ma_align", "ma250_up", "above250_prev",
        "low_in_zone", "close_hold", "close_ge_open",
    ]
    for field in fields:
        value = row.get(field)
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
    if not (
        bool(row["ma250_up"])
        and bool(row["ma_align"])
        and bool(row["above250_prev"])
        and bool(row["low_in_zone"])
        and bool(row["close_hold"])
        and bool(row["close_ge_open"])
    ):
        return None
    close = float(row["close"])
    ma250 = float(row["ma250"])
    atr14 = float(row["atr14"])
    stop_suggestion = float(
        np.clip(2.0 * atr14 / close, STOP_FLOOR, STOP_CEIL)
    )
    slope_abs = float(row["ma250_slope"])
    ma250_20ago = float(row["ma250_20ago"])
    slope_pct = slope_abs / ma250_20ago * 100.0 if ma250_20ago else 0.0
    return {
        "signal_type": POOL_TYPE_YEARLINE,
        "signal_date": str(pd.Timestamp(row["datetime"]).date()),
        "entry_reference": "next_day_open",
        "close": round(close, 3),
        "ma60": round(float(row["ma60"]), 3),
        "ma120": round(float(row["ma120"]), 3),
        "ma250": round(ma250, 3),
        "ma250_slope": round(slope_abs, 3),
        "ma250_slope_pct": round(slope_pct, 4),
        "atr14_pct": round(float(row["atr14_pct"]), 3),
        "volume_ratio": round(float(row["volume_ratio"]), 3),
        "low_vs_ma250_pct": round(
            (float(row["low"]) - ma250) / ma250 * 100.0, 4
        ),
        "close_vs_ma250_pct": round((close - ma250) / ma250 * 100.0, 4),
        "stop_suggestion_pct": round(stop_suggestion * 100.0, 2),
        "research_only": True,
    }


def last_bar_pullback_candidate(
    symbol: str,
    name: str,
    daily: pd.DataFrame,
    score: int = 100,
) -> Optional[dict[str, Any]]:
    """对最近一根已收盘日线评估年线回踩信号, 拼出候选记录; 未命中返回 None。

    daily 期望为市场客户端返回的日线框 (含 datetime/open/high/low/close/volume)。
    """
    df = add_yearline_indicators(daily)
    signal = pullback_signal_at(df, -1)
    if signal is None:
        return None
    candidate = {
        "symbol": symbol,
        "name": name,
        "score": score,
        "pool_type": POOL_TYPE_YEARLINE,
    }
    candidate.update(signal)
    return candidate
