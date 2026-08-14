"""MACD and zero-axis cross calculations without TA-Lib."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def calculate_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    values = pd.to_numeric(close, errors="coerce").astype(float)
    ema_fast = values.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = values.ewm(span=slow, adjust=False, min_periods=slow).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = (dif - dea) * 2.0
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist}, index=close.index)


def analyze_macd(
    frame: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    zero_axis_tolerance: float = 0.005,
    ma_period: int = 60,
    volume_period: int = 20,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if frame.empty or len(frame) < slow + signal + 2:
        return frame.copy(), {"status": "insufficient_data"}

    result = frame.copy()
    macd = calculate_macd(result["close"], fast=fast, slow=slow, signal=signal)
    result = result.join(macd)
    result["ma60"] = result["close"].rolling(ma_period, min_periods=ma_period).mean()
    result["avg_volume"] = result["volume"].rolling(
        volume_period, min_periods=max(3, volume_period // 2)
    ).mean()
    result["volume_ratio"] = result["volume"] / result["avg_volume"].replace(0, np.nan)

    current = result.iloc[-1]
    previous = result.iloc[-2]
    if pd.isna(current["dif"]) or pd.isna(current["dea"]):
        return result, {"status": "insufficient_data"}

    close = max(abs(float(current["close"])), 1e-12)
    zero_distance = max(abs(float(current["dif"])), abs(float(current["dea"]))) / close
    golden_cross = bool(current["dif"] > current["dea"] and previous["dif"] <= previous["dea"])
    death_cross = bool(current["dif"] < current["dea"] and previous["dif"] >= previous["dea"])
    near_zero = bool(zero_distance <= zero_axis_tolerance)

    ma_current = current["ma60"]
    ma_previous = result["ma60"].iloc[-6] if len(result) >= 6 else np.nan
    ma_up = bool(pd.notna(ma_current) and pd.notna(ma_previous) and ma_current > ma_previous)
    ma_down = bool(pd.notna(ma_current) and pd.notna(ma_previous) and ma_current < ma_previous)

    summary = {
        "status": "ok",
        "dif": float(current["dif"]),
        "dea": float(current["dea"]),
        "hist": float(current["hist"]),
        "dif_rising": bool(current["dif"] > previous["dif"]),
        "dif_falling": bool(current["dif"] < previous["dif"]),
        "zero_distance": float(zero_distance),
        "near_zero": near_zero,
        "golden_cross": golden_cross,
        "death_cross": death_cross,
        "zero_axis_golden_cross": bool(golden_cross and near_zero),
        "zero_axis_death_cross": bool(death_cross and near_zero),
        "ma60": None if pd.isna(ma_current) else float(ma_current),
        "ma60_up": ma_up,
        "ma60_down": ma_down,
        "above_ma60": bool(pd.notna(ma_current) and current["close"] > ma_current),
        "below_ma60": bool(pd.notna(ma_current) and current["close"] < ma_current),
        "volume_ratio": 0.0
        if pd.isna(current["volume_ratio"])
        else float(current["volume_ratio"]),
        "price_change": float(current["close"] - previous["close"]),
    }
    return result, summary

