"""MACD and zero-axis cross calculations without TA-Lib."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def classify_zero_axis_zone(
    dif: float,
    dea: float,
    close: float,
    zero_axis_tolerance: float,
) -> str:
    normalized_distance = max(abs(dif), abs(dea)) / max(abs(close), 1e-12)
    if normalized_distance <= zero_axis_tolerance or dif * dea <= 0:
        return "near"
    if dif > 0 and dea > 0:
        return "above"
    return "below"


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
    moderate_volume_min: float = 1.0,
    moderate_volume_max: float = 2.0,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if frame.empty or len(frame) < slow + signal + 2:
        return frame.copy(), {"status": "insufficient_data"}

    result = frame.copy()
    macd = calculate_macd(result["close"], fast=fast, slow=slow, signal=signal)
    result = result.join(macd)
    result["ma5"] = result["close"].rolling(5, min_periods=5).mean()
    result["ma10"] = result["close"].rolling(10, min_periods=10).mean()
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
    golden_cross_zone = classify_zero_axis_zone(
        float(current["dif"]),
        float(current["dea"]),
        float(current["close"]),
        zero_axis_tolerance,
    )

    volume_ratio = 0.0 if pd.isna(current["volume_ratio"]) else float(current["volume_ratio"])
    moderate_volume = bool(moderate_volume_min <= volume_ratio <= moderate_volume_max)
    current_short_ma = max(float(current["ma5"]), float(current["ma10"]))
    previous_short_ma = max(float(previous["ma5"]), float(previous["ma10"]))
    price_breakout = bool(
        pd.notna(current["ma5"])
        and pd.notna(current["ma10"])
        and pd.notna(previous["ma5"])
        and pd.notna(previous["ma10"])
        and float(current["close"]) > current_short_ma
        and float(previous["close"]) <= previous_short_ma
    )
    hist_expanding = bool(
        len(result) >= 3
        and pd.notna(result["hist"].iloc[-3])
        and 0 < float(result["hist"].iloc[-3]) < float(result["hist"].iloc[-2]) < float(current["hist"])
    )
    confirmations = [
        label
        for enabled, label in (
            (moderate_volume, "成交量温和放大"),
            (price_breakout, "价格突破MA5/MA10"),
            (hist_expanding, "MACD红柱连续放大"),
        )
        if enabled
    ]
    if not golden_cross:
        golden_cross_quality = "none"
    elif golden_cross_zone == "above" and len(confirmations) >= 2:
        golden_cross_quality = "strong"
    elif golden_cross_zone in {"above", "near"} and len(confirmations) >= 1:
        golden_cross_quality = "confirmed"
    else:
        golden_cross_quality = "weak"

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
        "golden_cross_zone": golden_cross_zone,
        "golden_cross_zone_label": {
            "above": "0轴上方金叉",
            "near": "0轴附近金叉",
            "below": "0轴下方金叉",
        }[golden_cross_zone],
        "golden_cross_risk": {"above": "low", "near": "medium", "below": "high"}[
            golden_cross_zone
        ],
        "zero_axis_golden_cross": bool(golden_cross and golden_cross_zone == "near"),
        "above_zero_golden_cross": bool(golden_cross and golden_cross_zone == "above"),
        "below_zero_golden_cross": bool(golden_cross and golden_cross_zone == "below"),
        "zero_axis_death_cross": bool(death_cross and near_zero),
        "ma5": None if pd.isna(current["ma5"]) else float(current["ma5"]),
        "ma10": None if pd.isna(current["ma10"]) else float(current["ma10"]),
        "ma60": None if pd.isna(ma_current) else float(ma_current),
        "ma60_up": ma_up,
        "ma60_down": ma_down,
        "above_ma60": bool(pd.notna(ma_current) and current["close"] > ma_current),
        "below_ma60": bool(pd.notna(ma_current) and current["close"] < ma_current),
        "volume_ratio": volume_ratio,
        "moderate_volume": moderate_volume,
        "price_breakout_ma5_ma10": price_breakout,
        "hist_expanding": hist_expanding,
        "confirmation_items": confirmations,
        "confirmation_count": len(confirmations),
        "golden_cross_quality": golden_cross_quality,
        "price_change": float(current["close"] - previous["close"]),
    }
    return result, summary

