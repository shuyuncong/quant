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


def _golden_cross_indices(dif: pd.Series, dea: pd.Series) -> list[int]:
    """Return confirmed golden-cross bar indexes without look-ahead."""
    previous_dif = dif.shift(1)
    previous_dea = dea.shift(1)
    return [
        index
        for index in range(1, len(dif))
        if pd.notna(dif.iloc[index])
        and pd.notna(dea.iloc[index])
        and pd.notna(previous_dif.iloc[index])
        and pd.notna(previous_dea.iloc[index])
        and dif.iloc[index] > dea.iloc[index]
        and previous_dif.iloc[index] <= previous_dea.iloc[index]
    ]


def _pullback_confirmation(
    frame: pd.DataFrame,
    cross_index: int,
    confirmation_bars: int,
) -> dict[str, Any]:
    """Classify the latest state after one golden cross.

    A valid confirmation must first retest the cross candle body, keep the
    cross candle low intact, and then close back above the cross close. This
    deliberately leaves the raw cross visible while delaying the entry event.
    """
    latest_index = len(frame) - 1
    cross = frame.iloc[cross_index]
    cross_close = float(cross["close"])
    cross_low = float(cross.get("low", cross_close))
    age = latest_index - cross_index
    state: dict[str, Any] = {
        "cross_index": cross_index,
        "cross_bars_ago": age,
        "cross_price": cross_close,
        "cross_low": cross_low,
        "pullback_touched": False,
        "pullback_confirmed": False,
    }
    if age <= 0:
        state["state"] = "pending_pullback"
        return state
    if age > confirmation_bars:
        state["state"] = "expired"
        return state

    post_cross = frame.iloc[cross_index + 1 : latest_index + 1]
    lows = pd.to_numeric(post_cross.get("low", post_cross["close"]), errors="coerce")
    lows = lows.fillna(pd.to_numeric(post_cross["close"], errors="coerce"))
    if not lows.empty and float(lows.min()) < cross_low:
        state["state"] = "invalidated"
        return state

    touched = bool((lows <= cross_close).any())
    state["pullback_touched"] = touched
    latest_close = float(frame["close"].iloc[-1])
    previous_close = float(frame["close"].iloc[-2])
    recovered = latest_close > cross_close and latest_close > previous_close
    if touched and recovered:
        state["pullback_confirmed"] = True
        state["state"] = "confirmed_pullback"
    else:
        state["state"] = "pending_pullback"
    return state


def _first_pullback_confirmation_index(
    frame: pd.DataFrame,
    cross_index: int,
    confirmation_bars: int,
) -> int | None:
    """Return the first bar that confirms the pullback for one cross."""
    max_index = min(
        len(frame) - 1,
        cross_index + max(int(confirmation_bars), 1),
    )
    for confirmation_index in range(cross_index + 1, max_index + 1):
        state = _pullback_confirmation(
            frame.iloc[: confirmation_index + 1],
            cross_index,
            confirmation_bars,
        )
        if state["pullback_confirmed"]:
            return confirmation_index
        if state["state"] == "invalidated":
            return None
    return None


def find_golden_cross_entries(
    frame: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    zero_axis_tolerance: float = 0.005,
    confirmation_bars: int = 5,
    allowed_zones: tuple[str, ...] = ("above", "near"),
) -> list[dict[str, Any]]:
    """Find pullback-confirmed golden-cross entries in a closed-bar frame."""
    if frame.empty or "close" not in frame.columns:
        return []
    macd = calculate_macd(frame["close"], fast=fast, slow=slow, signal=signal)
    entries: list[dict[str, Any]] = []
    for cross_index in _golden_cross_indices(macd["dif"], macd["dea"]):
        zone = classify_zero_axis_zone(
            float(macd["dif"].iloc[cross_index]),
            float(macd["dea"].iloc[cross_index]),
            float(frame["close"].iloc[cross_index]),
            zero_axis_tolerance,
        )
        if zone not in allowed_zones:
            continue
        max_index = min(len(frame) - 1, cross_index + max(int(confirmation_bars), 1))
        for confirmation_index in range(cross_index + 1, max_index + 1):
            state = _pullback_confirmation(
                frame.iloc[: confirmation_index + 1],
                cross_index,
                confirmation_bars,
            )
            confirmation_zone = classify_zero_axis_zone(
                float(macd["dif"].iloc[confirmation_index]),
                float(macd["dea"].iloc[confirmation_index]),
                float(frame["close"].iloc[confirmation_index]),
                zero_axis_tolerance,
            )
            confirmation_bullish = bool(
                macd["dif"].iloc[confirmation_index] > macd["dea"].iloc[confirmation_index]
            )
            if state["pullback_confirmed"] and confirmation_bullish and confirmation_zone in allowed_zones:
                entries.append(
                    {
                        "cross_index": cross_index,
                        "confirmation_index": confirmation_index,
                        "zone": zone,
                        "cross_price": float(frame["close"].iloc[cross_index]),
                        "confirmation_price": float(frame["close"].iloc[confirmation_index]),
                        "confirmation_bars": confirmation_index - cross_index,
                    }
                )
                break
            if state["pullback_confirmed"]:
                break
            if state["state"] == "invalidated":
                break
    return entries


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
    pullback_confirmation_bars: int = 5,
    long_ma_period: int | None = None,
    position_lookback: int = 20,
    max_long_ma_distance: float = 0.35,
    max_recent_return: float = 0.30,
    high_position_volume_ratio: float = 3.0,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if frame.empty or len(frame) < slow + signal + 2:
        return frame.copy(), {"status": "insufficient_data"}

    result = frame.copy()
    macd = calculate_macd(result["close"], fast=fast, slow=slow, signal=signal)
    result = result.join(macd)
    result["ma5"] = result["close"].rolling(5, min_periods=5).mean()
    result["ma10"] = result["close"].rolling(10, min_periods=10).mean()
    result["ma20"] = result["close"].rolling(20, min_periods=20).mean()
    result["ma60"] = result["close"].rolling(ma_period, min_periods=ma_period).mean()
    if long_ma_period:
        result["ma_long"] = result["close"].rolling(
            long_ma_period, min_periods=long_ma_period
        ).mean()
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

    cross_indices = _golden_cross_indices(result["dif"], result["dea"])
    latest_cross_index = cross_indices[-1] if cross_indices else None
    if latest_cross_index is None:
        cross_state = {"state": "none", "cross_bars_ago": None, "pullback_confirmed": False}
        latest_cross_zone = None
        first_confirmation_index = None
    else:
        first_confirmation_index = _first_pullback_confirmation_index(
            result,
            latest_cross_index,
            max(int(pullback_confirmation_bars), 1),
        )
        if first_confirmation_index is None:
            cross_state = _pullback_confirmation(
                result,
                latest_cross_index,
                max(int(pullback_confirmation_bars), 1),
            )
        else:
            cross_state = _pullback_confirmation(
                result.iloc[: first_confirmation_index + 1],
                latest_cross_index,
                max(int(pullback_confirmation_bars), 1),
            )
            cross_state["confirmation_index"] = first_confirmation_index
            cross_state["confirmation_bars"] = first_confirmation_index - latest_cross_index
        latest_cross_zone = classify_zero_axis_zone(
            float(result["dif"].iloc[latest_cross_index]),
            float(result["dea"].iloc[latest_cross_index]),
            float(result["close"].iloc[latest_cross_index]),
            zero_axis_tolerance,
        )
    pullback_confirmed = bool(cross_state.get("pullback_confirmed"))
    confirmation_is_latest = bool(
        first_confirmation_index is not None
        and first_confirmation_index == len(result) - 1
    )
    # A pullback cannot revive a setup that has already crossed back down or
    # moved into the weak zero-axis zone before the entry is taken.
    current_bullish = bool(current["dif"] > current["dea"])
    entry_ready = bool(
        pullback_confirmed
        and confirmation_is_latest
        and latest_cross_zone in {"above", "near"}
        and current_bullish
        and golden_cross_zone in {"above", "near"}
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
    quality_zone = golden_cross_zone if golden_cross else latest_cross_zone
    quality_active = bool(golden_cross or entry_ready)
    if not quality_active or quality_zone is None:
        golden_cross_quality = "none"
    elif quality_zone == "above" and len(confirmations) >= 2:
        golden_cross_quality = "strong"
    elif quality_zone in {"above", "near"} and len(confirmations) >= 1:
        golden_cross_quality = "confirmed"
    else:
        golden_cross_quality = "weak"

    ma_current = current["ma60"]
    ma_previous = result["ma60"].iloc[-6] if len(result) >= 6 else np.nan
    ma_up = bool(pd.notna(ma_current) and pd.notna(ma_previous) and ma_current > ma_previous)
    ma_down = bool(pd.notna(ma_current) and pd.notna(ma_previous) and ma_current < ma_previous)

    ma_long_current = np.nan
    ma_long_previous = np.nan
    if long_ma_period and "ma_long" in result:
        ma_long_current = result["ma_long"].iloc[-1]
        previous_index = max(0, len(result) - 6)
        ma_long_previous = result["ma_long"].iloc[previous_index]
    ma_long_up = bool(pd.notna(ma_long_current) and pd.notna(ma_long_previous) and ma_long_current > ma_long_previous)
    ma_long_down = bool(pd.notna(ma_long_current) and pd.notna(ma_long_previous) and ma_long_current < ma_long_previous)
    above_ma_long = bool(pd.notna(ma_long_current) and current["close"] > ma_long_current)
    distance_to_ma_long = (
        float(abs(current["close"] - ma_long_current) / ma_long_current)
        if pd.notna(ma_long_current) and ma_long_current > 0
        else None
    )
    lookback_index = max(0, len(result) - max(int(position_lookback), 1) - 1)
    base_close = float(result["close"].iloc[lookback_index])
    recent_return = float(current["close"] / base_close - 1.0) if base_close > 0 else None
    high_position = bool(
        above_ma_long
        and (
            (distance_to_ma_long is not None and distance_to_ma_long >= max_long_ma_distance)
            or (recent_return is not None and recent_return >= max_recent_return)
        )
    )
    high_volume_risk = bool(high_position and volume_ratio >= high_position_volume_ratio and current["close"] > previous["close"])
    position_risk_flags = []
    if high_position:
        position_risk_flags.append("高位偏离长期均线或近期涨幅过大")
    if high_volume_risk:
        position_risk_flags.append("高位放量上涨")

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
        "golden_cross_state": cross_state.get("state", "none"),
        "golden_cross_entry_ready": entry_ready,
        "golden_cross_entry_zone": latest_cross_zone,
        "golden_cross_confirmation_bars": cross_state.get("cross_bars_ago"),
        "golden_cross_pullback_touched": bool(cross_state.get("pullback_touched", False)),
        "golden_cross_cross_time": (
            None
            if latest_cross_index is None or "datetime" not in result.columns
            else pd.Timestamp(result["datetime"].iloc[latest_cross_index]).isoformat()
        ),
        "golden_cross_cross_price": cross_state.get("cross_price"),
        "golden_cross_cross_low": cross_state.get("cross_low"),
        "golden_cross_first_confirmation_time": (
            None
            if first_confirmation_index is None or "datetime" not in result.columns
            else pd.Timestamp(result["datetime"].iloc[first_confirmation_index]).isoformat()
        ),
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
        "ma20": None if pd.isna(current["ma20"]) else float(current["ma20"]),
        "ma60": None if pd.isna(ma_current) else float(ma_current),
        "ma60_up": ma_up,
        "ma60_down": ma_down,
        "above_ma60": bool(pd.notna(ma_current) and current["close"] > ma_current),
        "below_ma60": bool(pd.notna(ma_current) and current["close"] < ma_current),
        "ma20_above_ma60": bool(
            pd.notna(current["ma20"])
            and pd.notna(ma_current)
            and float(current["ma20"]) > float(ma_current)
        ),
        "volume_ratio": volume_ratio,
        "moderate_volume": moderate_volume,
        "price_breakout_ma5_ma10": price_breakout,
        "hist_expanding": hist_expanding,
        "confirmation_items": confirmations,
        "confirmation_count": len(confirmations),
        "golden_cross_quality": golden_cross_quality,
        "price_change": float(current["close"] - previous["close"]),
        "ma_long": None if pd.isna(ma_long_current) else float(ma_long_current),
        "ma_long_up": ma_long_up,
        "ma_long_down": ma_long_down,
        "above_ma_long": above_ma_long,
        "distance_to_ma_long": distance_to_ma_long,
        "recent_return": recent_return,
        "high_position_risk": high_position,
        "high_volume_risk": high_volume_risk,
        "position_risk_flags": position_risk_flags,
    }
    return result, summary

