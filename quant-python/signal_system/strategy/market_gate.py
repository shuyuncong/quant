"""Shared market-gate configuration and trend calculations."""

from __future__ import annotations

from typing import Any

import pandas as pd


VALID_FAST_GATE_MODES = {
    "none",
    "ma10_latch",
    "macd_death_latch",
    "any_latch",
}


def normalize_fast_gate_mode(value: Any) -> str:
    """Return one supported fast-gate mode, falling back to disabled."""
    mode = str(value or "none").lower().strip()
    return mode if mode in VALID_FAST_GATE_MODES else "none"


def resolve_market_gate_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the effective market-gate settings used by live and backtest."""
    entry_filters = config.get("entry_filters", {})
    macd_config = config.get("signal_strategy", {}).get("macd", {})
    trend_fast = max(int(entry_filters.get("trend_fast_ma", 20)), 2)
    trend_slow = max(
        int(entry_filters.get("trend_slow_ma", 60)),
        trend_fast + 1,
    )
    return {
        "trend_gate_enabled": bool(entry_filters.get("trend_gate_enabled", False)),
        "trend_fast_ma": trend_fast,
        "trend_slow_ma": trend_slow,
        "fast_gate_mode": normalize_fast_gate_mode(
            entry_filters.get("fast_gate_mode", "none")
        ),
        "macd": {
            "fast": max(int(macd_config.get("fast", 12)), 1),
            "slow": max(int(macd_config.get("slow", 26)), 2),
            "signal": max(int(macd_config.get("signal", 9)), 1),
        },
    }


def calculate_trend_gate(
    close: pd.Series,
    enabled: bool,
    fast_period: int,
    slow_period: int,
) -> pd.Series:
    """Return the same causal moving-average trend state for every bar."""
    values = pd.to_numeric(close, errors="coerce").astype(float)
    if not enabled:
        return pd.Series(True, index=values.index, dtype=bool)
    fast = values.rolling(fast_period, min_periods=fast_period).mean()
    slow = values.rolling(slow_period, min_periods=slow_period).mean()
    return (fast > slow).fillna(False).astype(bool)


def calculate_strict_regime(
    close: pd.Series,
    *,
    fast_period: int = 10,
    slow_period: int = 20,
) -> pd.Series:
    """Classify each closed bar as ``bull``, ``range`` or ``bear``.

    The rule is intentionally causal and shared by live scans and backtests:

    * bull: close > MA(slow), MA(slow) rising, and MA(fast) > MA(slow)
    * bear: close < MA(slow) and MA(slow) falling
    * otherwise: range

    Bars without enough history remain ``range`` so callers can separately
    apply their own insufficient-history gate.
    """
    values = pd.to_numeric(close, errors="coerce").astype(float)
    fast_period = max(int(fast_period), 2)
    slow_period = max(int(slow_period), fast_period + 1)
    ma_fast = values.rolling(fast_period, min_periods=fast_period).mean()
    ma_slow = values.rolling(slow_period, min_periods=slow_period).mean()
    ma_slow_prev = ma_slow.shift(1)

    regimes = pd.Series("range", index=values.index, dtype="object")
    valid = ma_fast.notna() & ma_slow.notna() & ma_slow_prev.notna() & values.notna()
    bull = valid & (values > ma_slow) & (ma_slow > ma_slow_prev) & (ma_fast > ma_slow)
    bear = valid & (values < ma_slow) & (ma_slow < ma_slow_prev)
    regimes.loc[bull] = "bull"
    regimes.loc[bear] = "bear"
    return regimes
