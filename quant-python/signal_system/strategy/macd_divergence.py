"""MACD 底背离 + 零轴金叉 + 放量 + 年线以上 —— 独立研究展示池。

本模块与生产 MACD 池(macd_zero_axis)、年线池(yearline_pullback)完全解耦:

- 独立 pool_type = "macd_divergence";
- 独立筛选函数, 不调用 analyze_macd / find_golden_cross_entries, 也不进入
  盘中监控、候选投票与下单链路;
- 不推送通知, 只写候选表并可由页面展示。

四个条件全部只读信号日收盘及以前的数据, 无前视偏差:

  1. 零轴金叉  DIF[t] > DEA[t] 且 DIF[t-1] <= DEA[t-1], 且金叉落在 0 轴附近或上方
     (zone != "below"; "near" 含 DIF/DEA 横跨 0 轴的情形)。

  2. 日线底背离  比较最近两段已完成的 *负* MACD 柱区间:
     后一段价格更低(创新低) 且 负柱绝对面积更小。
     未完成的当前区间不参与比较, 避免部分区间偏置。
     (口径与 macd_divergence_audit._completed_cycles 的因果定义一致。)

  3. 放量  当日成交量 >= 前 20 日均量 × volume_ratio_min。
     均值取 shift(1) 的过去 20 日, 不含当日, 避免自身稀释。
     与生产池的"温和放量 [1.0, 2.0]"不同: 这里只设下限, 不设上限。

  4. 年线以上  收盘 > MA250 且 MA250 上行(MA250[t] > MA250[t-lookback])。

信号在收盘确认, 入场参考为下一交易日开盘(仅记录字段, 不下单。
回测口径: signal_day 收盘确认 -> 次日开盘成交)。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

POOL_TYPE_DIVERGENCE = "macd_divergence"
SIGNAL_TYPE = "macd_divergence_bottom"

MIN_BARS = 270  # MA250 + 20 日斜率 + 余量
DEFAULT_MACD = {"fast": 12, "slow": 26, "signal": 9}
DEFAULT_ZERO_AXIS_TOLERANCE = 0.005
DEFAULT_VOLUME_WINDOW = 20
DEFAULT_MIN_VOLUME_RATIO = 1.5
DEFAULT_LONG_MA_PERIOD = 250
DEFAULT_LONG_MA_SLOPE_WINDOW = 20
DEFAULT_MIN_MACD_SEGMENT_BARS = 2


def resolve_divergence_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the macd_divergence pool settings with explicit defaults.

    Missing block => pool disabled, so the new strategy never runs implicitly
    on an existing deployment's config.
    """
    raw = config.get("macd_divergence")
    if not isinstance(raw, dict):
        return {
            "enabled": False,
            "fast": DEFAULT_MACD["fast"],
            "slow": DEFAULT_MACD["slow"],
            "signal": DEFAULT_MACD["signal"],
            "zero_axis_tolerance": DEFAULT_ZERO_AXIS_TOLERANCE,
            "volume_window": DEFAULT_VOLUME_WINDOW,
            "min_volume_ratio": DEFAULT_MIN_VOLUME_RATIO,
            "long_ma_period": DEFAULT_LONG_MA_PERIOD,
            "long_ma_slope_window": DEFAULT_LONG_MA_SLOPE_WINDOW,
            "min_macd_segment_bars": DEFAULT_MIN_MACD_SEGMENT_BARS,
        }
    macd = raw.get("macd") if isinstance(raw.get("macd"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "fast": int(macd.get("fast", DEFAULT_MACD["fast"])),
        "slow": int(macd.get("slow", DEFAULT_MACD["slow"])),
        "signal": int(macd.get("signal", DEFAULT_MACD["signal"])),
        "zero_axis_tolerance": float(
            raw.get("zero_axis_tolerance", DEFAULT_ZERO_AXIS_TOLERANCE)
        ),
        "volume_window": max(int(raw.get("volume_window", DEFAULT_VOLUME_WINDOW)), 1),
        "min_volume_ratio": float(
            raw.get("min_volume_ratio", DEFAULT_MIN_VOLUME_RATIO)
        ),
        "long_ma_period": max(int(raw.get("long_ma_period", DEFAULT_LONG_MA_PERIOD)), 1),
        "long_ma_slope_window": max(
            int(raw.get("long_ma_slope_window", DEFAULT_LONG_MA_SLOPE_WINDOW)), 1
        ),
        "min_macd_segment_bars": max(
            int(raw.get("min_macd_segment_bars", DEFAULT_MIN_MACD_SEGMENT_BARS)), 1
        ),
    }


def prepare_divergence_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize daily bars to the closed-bar convention used by this pool."""
    if df is None or df.empty or "datetime" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    if "is_closed" in out.columns:
        out = out[out["is_closed"].fillna(False).astype(bool)]
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])
    required = ("open", "high", "low", "close", "volume")
    if not set(required).issubset(out.columns):
        return pd.DataFrame()
    for column in required:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    return (
        out.dropna(subset=list(required))
        .sort_values("datetime", kind="mergesort")
        .drop_duplicates("datetime", keep="last")
        .reset_index(drop=True)
    )


def _calculate_macd(
    close: pd.Series,
    fast: int,
    slow: int,
    signal: int,
) -> pd.DataFrame:
    """Same EMA/EWM definition as strategy.macd.calculate_macd, kept local.

    Duplicated on purpose: this pool must not depend on the production MACD
    module, so a production change cannot silently alter this experiment.
    """
    values = pd.to_numeric(close, errors="coerce").astype(float)
    ema_fast = values.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = values.ewm(span=slow, adjust=False, min_periods=slow).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = (dif - dea) * 2.0
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist}, index=close.index)


def _zero_axis_zone(dif: float, dea: float, close: float, tolerance: float) -> str:
    """Classify the zone; identical semantics to the production classifier."""
    normalized_distance = max(abs(dif), abs(dea)) / max(abs(close), 1e-12)
    if normalized_distance <= tolerance or dif * dea <= 0:
        return "near"
    if dif > 0 and dea > 0:
        return "above"
    return "below"


def top_divergence_flags(
    hist: pd.Series,
    close: pd.Series,
) -> tuple[np.ndarray, dict[int, dict[str, float]]]:
    """正柱周期完成当根的顶背离标记 (与回测 _build_trend_exit_flags 同一套算法)。

    返回 (flags, details): flags[i] 为 True 表示第 i 根"正柱周期刚走完",
    且该周期最高价高于上一个正柱周期、正柱面积更小。
    details[i] 给出该次触发的两组对比数值, 供提醒/页面展示。

    回测与实盘共用这一个实现: 两套"顶背离"定义不一致时, 页面提示的卖点
    和回测里验证过的卖点会是两回事。
    """
    histogram = pd.to_numeric(hist, errors="coerce").fillna(0.0).to_numpy(dtype=float)
    close_values = pd.to_numeric(close, errors="coerce").to_numpy(dtype=float)
    flags = np.zeros(len(histogram), dtype=bool)
    details: dict[int, dict[str, float]] = {}
    if len(histogram) < 3:
        return flags, details
    positive = np.isfinite(histogram) & (histogram > 0)
    changes = np.flatnonzero(np.diff(positive.astype(np.int8)) != 0) + 1
    bounds = np.concatenate(([0], changes, [len(positive)]))
    cycles: list[dict[str, float]] = []
    for run_start, run_stop in zip(bounds[:-1], bounds[1:]):
        if not positive[run_start]:
            continue
        segment_hist = histogram[run_start:run_stop]
        segment_close = close_values[run_start:run_stop]
        area = float(segment_hist.sum())
        if area <= 0 or not np.isfinite(segment_close).any():
            continue
        cycles.append({"trigger": float(run_stop), "area": area,
                       "high": float(np.nanmax(segment_close))})
    for position in range(1, len(cycles)):
        latest, prior = cycles[position], cycles[position - 1]
        trigger = int(latest["trigger"])
        if trigger >= len(flags):
            break
        if latest["high"] > prior["high"] and latest["area"] < prior["area"]:
            flags[trigger] = True
            details[trigger] = {
                "prior_high": prior["high"], "latest_high": latest["high"],
                "prior_area": prior["area"], "latest_area": latest["area"],
            }
    return flags, details


def _completed_negative_cycles(
    hist: pd.Series,
    low: pd.Series,
    signal_index: int,
    min_segment_bars: int,
) -> list[dict[str, Any]]:
    """Return completed negative-histogram cycles strictly before signal_index.

    A cycle still active on the signal bar is incomplete and is excluded, so
    the comparison never uses a partial segment.
    """
    if signal_index <= 0:
        return []
    histogram = pd.to_numeric(hist.iloc[: signal_index + 1], errors="coerce")
    prices = pd.to_numeric(low.iloc[: signal_index + 1], errors="coerce")
    cycles: list[dict[str, Any]] = []
    start: int | None = None
    for index, value in enumerate(histogram):
        active = pd.notna(value) and float(value) < 0
        if active and start is None:
            start = index
        if not active and start is not None:
            end = index - 1
            if end - start + 1 >= min_segment_bars:
                segment_hist = histogram.iloc[start : end + 1]
                segment_prices = prices.iloc[start : end + 1].dropna()
                if not segment_prices.empty:
                    cycles.append(
                        {
                            "start": start,
                            "end": end,
                            "area": float(segment_hist.abs().sum()),
                            "extreme": float(segment_prices.min()),
                        }
                    )
            start = None
    return [cycle for cycle in cycles if int(cycle["end"]) < signal_index]


def bottom_divergence_at(
    hist: pd.Series,
    low: pd.Series,
    signal_index: int,
    min_segment_bars: int,
) -> dict[str, Any]:
    """Evaluate the causal bottom-divergence condition at one bar index.

    True when the latest two completed negative cycles show a lower price low
    with a smaller absolute negative area.
    """
    cycles = _completed_negative_cycles(hist, low, signal_index, min_segment_bars)
    if len(cycles) < 2:
        return {"available": False, "bottom_divergence": False}
    prior, latest = cycles[-2], cycles[-1]
    if prior["area"] <= 0 or prior["extreme"] == 0:
        return {"available": False, "bottom_divergence": False}
    area_ratio = latest["area"] / prior["area"]
    price_change_pct = (latest["extreme"] / prior["extreme"] - 1.0) * 100.0
    return {
        "available": True,
        "bottom_divergence": bool(price_change_pct < 0 and area_ratio < 1.0),
        "area_ratio": round(float(area_ratio), 4),
        "price_new_low_pct": round(float(price_change_pct), 4),
        "prior_area": round(float(prior["area"]), 6),
        "latest_area": round(float(latest["area"]), 6),
        "latest_cycle_low": round(float(latest["extreme"]), 3),
        "prior_cycle_low": round(float(prior["extreme"]), 3),
        "wait_bars": int(signal_index - int(latest["end"])),
    }


def add_divergence_indicators(
    df: pd.DataFrame,
    config: dict[str, Any],
) -> pd.DataFrame:
    """Append causal indicator columns for this pool.

    Every column is a rolling/shifted function of current and prior bars only.
    """
    out = prepare_divergence_bars(df)
    if out.empty:
        return out
    fast = int(config["fast"])
    slow = int(config["slow"])
    signal = int(config["signal"])
    macd = _calculate_macd(out["close"], fast=fast, slow=slow, signal=signal)
    out["dif"] = macd["dif"]
    out["dea"] = macd["dea"]
    out["hist"] = macd["hist"]
    out["ma_long"] = out["close"].rolling(
        int(config["long_ma_period"]), min_periods=int(config["long_ma_period"])
    ).mean()
    out["ma_long_prev"] = out["ma_long"].shift(int(config["long_ma_slope_window"]))
    earlier = out["volume"].shift(1)
    out["avg_volume_prev"] = earlier.rolling(
        int(config["volume_window"]), min_periods=int(config["volume_window"])
    ).mean()
    out["volume_ratio"] = out["volume"] / out["avg_volume_prev"].replace(0, np.nan)
    prev_dif = out["dif"].shift(1)
    prev_dea = out["dea"].shift(1)
    out["golden_cross"] = (
        (out["dif"] > out["dea"])
        & (prev_dif <= prev_dea)
        & out["dif"].notna()
        & out["dea"].notna()
        & prev_dif.notna()
        & prev_dea.notna()
    )
    return out


def evaluate_conditions(
    df: pd.DataFrame,
    index: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Return the four condition gates at one bar, for both signal and funnel.

    Independent booleans (not short-circuited) so a scan report can show which
    condition filtered a symbol out. Reads only data up to ``index``.
    """
    result: dict[str, Any] = {
        "available": False,
        "golden_cross": False,
        "zero_axis_ok": False,
        "volume_ok": False,
        "above_yearline": False,
        "divergence_ok": False,
        "all": False,
        "zone": None,
        "volume_ratio": None,
        "divergence": None,
    }
    if index < 0:
        index = len(df) - 1
    if index < MIN_BARS - 1 or index >= len(df):
        return result
    row = df.iloc[index]
    scalar_fields = ("dif", "dea", "hist", "ma_long", "ma_long_prev", "volume_ratio")
    for field in scalar_fields:
        value = row.get(field)
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            return result
    result["available"] = True
    close = float(row["close"])
    dif = float(row["dif"])
    dea = float(row["dea"])
    zone = _zero_axis_zone(dif, dea, close, float(config["zero_axis_tolerance"]))
    result["zone"] = zone
    result["golden_cross"] = bool(row["golden_cross"])
    result["zero_axis_ok"] = zone != "below"
    volume_ratio = float(row["volume_ratio"])
    result["volume_ratio"] = volume_ratio
    result["volume_ok"] = volume_ratio >= float(config["min_volume_ratio"])
    ma_long = float(row["ma_long"])
    ma_long_prev = float(row["ma_long_prev"])
    result["above_yearline"] = bool(close > ma_long and ma_long > ma_long_prev)
    divergence = bottom_divergence_at(
        df["hist"],
        df["low"] if "low" in df.columns else df["close"],
        index,
        int(config["min_macd_segment_bars"]),
    )
    result["divergence"] = divergence
    result["divergence_ok"] = bool(divergence["bottom_divergence"])
    result["all"] = bool(
        result["golden_cross"]
        and result["zero_axis_ok"]
        and result["volume_ok"]
        and result["above_yearline"]
        and result["divergence_ok"]
    )
    return result


def divergence_signal_at(
    df: pd.DataFrame,
    index: int,
    config: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Evaluate the four-condition signal at one bar; None when not satisfied.

    Caller must pass a frame already processed by add_divergence_indicators.
    Only data up to ``index`` is read.
    """
    if index < 0:
        index = len(df) - 1
    if index < MIN_BARS - 1 or index >= len(df):
        return None
    conditions = evaluate_conditions(df, index, config)
    if not conditions["all"]:
        return None
    row = df.iloc[index]
    close = float(row["close"])
    dif = float(row["dif"])
    dea = float(row["dea"])
    zone = str(conditions["zone"])
    volume_ratio = float(conditions["volume_ratio"])
    divergence = conditions["divergence"]
    ma_long = float(row["ma_long"])
    ma_long_prev = float(row["ma_long_prev"])
    slope_pct = (ma_long / ma_long_prev - 1.0) * 100.0 if ma_long_prev else 0.0
    return {
        "signal_type": SIGNAL_TYPE,
        "signal_date": str(pd.Timestamp(row["datetime"]).date()),
        "entry_reference": "next_day_open",
        "close": round(close, 3),
        "dif": round(dif, 6),
        "dea": round(dea, 6),
        "hist": round(float(row["hist"]), 6),
        "zero_axis_zone": zone,
        "zero_axis_zone_label": {
            "above": "0轴上方金叉",
            "near": "0轴附近金叉",
        }[zone],
        "ma_long": round(ma_long, 3),
        "ma_long_slope_pct": round(slope_pct, 4),
        "close_vs_ma_long_pct": round((close / ma_long - 1.0) * 100.0, 4),
        "volume_ratio": round(volume_ratio, 3),
        "volume_ratio_threshold": round(float(config["min_volume_ratio"]), 3),
        "divergence_area_ratio": divergence["area_ratio"],
        "divergence_price_new_low_pct": divergence["price_new_low_pct"],
        "divergence_prior_low": divergence["prior_cycle_low"],
        "divergence_latest_low": divergence["latest_cycle_low"],
        "conditions": [
            f"{'0轴上方' if zone == 'above' else '0轴附近'}金叉",
            f"底背离(面积比 {divergence['area_ratio']:.2f})",
            f"放量 {volume_ratio:.2f}x",
            "年线上方且年线上行",
        ],
        "research_only": True,
    }


def last_bar_divergence_candidate(
    symbol: str,
    name: str,
    daily: pd.DataFrame,
    config: dict[str, Any],
    score: int = 100,
    frame: pd.DataFrame | None = None,
) -> Optional[dict[str, Any]]:
    """Evaluate the four conditions on the latest closed daily bar.

    ``frame`` accepts an already-processed indicator frame (as returned by
    add_divergence_indicators) so a scanning loop can compute indicators once
    and reuse them for both the funnel and the candidate.
    """
    df = frame if frame is not None else add_divergence_indicators(daily, config)
    if df.empty or len(df) < MIN_BARS:
        return None
    signal = divergence_signal_at(df, len(df) - 1, config)
    if signal is None:
        return None
    candidate = {
        "symbol": symbol,
        "name": name,
        "score": score,
        "pool_type": POOL_TYPE_DIVERGENCE,
    }
    candidate.update(signal)
    return candidate
