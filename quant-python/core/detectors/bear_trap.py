"""Bear trap detector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Union

import pandas as pd


@dataclass(frozen=True)
class BearTrapResult:
    """Structured bear trap detection result."""

    is_bear_trap: bool
    reason: str
    details: Dict


class BearTrapDetector:
    """Detects false breakdowns below long term moving average."""

    def __init__(self, ma_period: int = 250, break_days: int = 5, recovery_days: int = 5, slope_window: int = 20):
        self.ma_period = ma_period
        self.break_days = break_days
        self.recovery_days = recovery_days
        self.slope_window = slope_window

    def detect(self, df: pd.DataFrame, divergence: Union[bool, Dict]) -> BearTrapResult:
        if df is None or df.empty or "close" not in df.columns:
            return BearTrapResult(False, "缺少价格数据", {})

        working_df = df.copy().reset_index(drop=True)
        if "ma_long" not in working_df.columns:
            working_df["ma_long"] = working_df["close"].rolling(self.ma_period).mean()

        if working_df["ma_long"].isna().all():
            return BearTrapResult(False, "长期均线数据不足", {})

        reasons = []

        if not self._check_ma_uptrend(working_df):
            return BearTrapResult(False, "年线未向上", {})
        reasons.append("年线向上")

        if not self._check_recent_break_below_ma(working_df):
            return BearTrapResult(False, "未发现短期跌破年线", {})
        reasons.append("短期跌破年线")

        if not self._check_quick_recovery(working_df):
            return BearTrapResult(False, "未快速收回", {})
        reasons.append("快速收回")

        if not self._check_bullish_divergence(divergence):
            return BearTrapResult(False, "无底背离", {})
        reasons.append("出现底背离")

        return BearTrapResult(
            True,
            " + ".join(reasons),
            {
                "latest_close": float(working_df["close"].iloc[-1]),
                "latest_ma_long": float(working_df["ma_long"].iloc[-1]),
            },
        )

    def _check_ma_uptrend(self, df: pd.DataFrame) -> bool:
        valid_ma = df["ma_long"].dropna()
        if len(valid_ma) < self.slope_window:
            return False
        recent_ma = valid_ma.iloc[-self.slope_window:]
        slope = (recent_ma.iloc[-1] - recent_ma.iloc[0]) / max(abs(recent_ma.iloc[0]), 1.0)
        return slope > 0

    def _check_recent_break_below_ma(self, df: pd.DataFrame) -> bool:
        recent = df.tail(self.break_days)
        return bool((recent["close"] < recent["ma_long"]).any())

    def _check_quick_recovery(self, df: pd.DataFrame) -> bool:
        recent = df.tail(self.recovery_days)
        return bool(recent["close"].iloc[-1] > recent["ma_long"].iloc[-1])

    @staticmethod
    def _check_bullish_divergence(divergence: Union[bool, Dict]) -> bool:
        if isinstance(divergence, bool):
            return divergence
        return bool(divergence.get("is_divergence"))
