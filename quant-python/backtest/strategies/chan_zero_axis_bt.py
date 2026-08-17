"""Backtest strategy for Chan buy points confirmed by MACD zero-axis golden crosses."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from signal_system.strategy.chan import analyze_chan
from signal_system.strategy.macd import calculate_macd, classify_zero_axis_zone


class ChanZeroAxisBacktestStrategy:
    """Enter on a Chan buy point when a qualified golden cross occurred recently."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        strategy = self.config.get("signal_strategy", {})
        macd = strategy.get("macd", {})
        chan = strategy.get("chan", {})
        backtest = self.config.get("backtest", {}).get("chan_zero_axis", {})
        risk = self.config.get("risk", {})
        self.fast = int(macd.get("fast", 12))
        self.slow = int(macd.get("slow", 26))
        self.signal = int(macd.get("signal", 9))
        self.zero_axis_tolerance = float(macd.get("zero_axis_tolerance", 0.005))
        self.moderate_volume_min = float(macd.get("moderate_volume_min", 1.0))
        self.moderate_volume_max = float(macd.get("moderate_volume_max", 2.0))
        self.min_bi_bars = int(chan.get("min_bi_bars", 4))
        self.divergence_ratio = float(chan.get("divergence_ratio", 0.9))
        self.allowed_zones = set(backtest.get("allowed_zones", ["above", "near"]))
        self.min_confirmations = int(backtest.get("min_confirmations", 2))
        self.cross_window_bars = int(backtest.get("cross_window_bars", 5))
        self.max_holding_bars = int(backtest.get("max_holding_bars", 40))
        self.stop_loss_pct = float(risk.get("stop_loss_pct", 0.08))
        self.stop_profit_pct = float(risk.get("stop_profit_pct", 0.30))

    def _enrich(self, price_data: pd.DataFrame) -> pd.DataFrame:
        frame = price_data.copy().reset_index(drop=True)
        frame["datetime"] = pd.to_datetime(frame.get("datetime", frame.index))
        if "volume" not in frame.columns:
            frame["volume"] = 0.0
        if "is_closed" not in frame.columns:
            frame["is_closed"] = True
        macd = calculate_macd(frame["close"], self.fast, self.slow, self.signal)
        frame = frame.join(macd)
        frame["ma5"] = frame["close"].rolling(5, min_periods=5).mean()
        frame["ma10"] = frame["close"].rolling(10, min_periods=10).mean()
        frame["volume_ma20"] = frame["volume"].rolling(20, min_periods=10).mean()
        frame["volume_ratio"] = frame["volume"] / frame["volume_ma20"].replace(0, np.nan)
        return frame

    def _qualified_crosses(self, frame: pd.DataFrame) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for index in range(2, len(frame)):
            current = frame.iloc[index]
            previous = frame.iloc[index - 1]
            if pd.isna(current["dif"]) or pd.isna(previous["dea"]):
                continue
            if not (current["dif"] > current["dea"] and previous["dif"] <= previous["dea"]):
                continue
            zone = classify_zero_axis_zone(
                float(current["dif"]),
                float(current["dea"]),
                float(current["close"]),
                self.zero_axis_tolerance,
            )
            volume_ratio = float(current["volume_ratio"]) if pd.notna(current["volume_ratio"]) else 0.0
            moderate_volume = self.moderate_volume_min <= volume_ratio <= self.moderate_volume_max
            price_breakout = bool(
                pd.notna(current["ma5"])
                and pd.notna(current["ma10"])
                and pd.notna(previous["ma5"])
                and pd.notna(previous["ma10"])
                and current["close"] > max(current["ma5"], current["ma10"])
                and previous["close"] <= max(previous["ma5"], previous["ma10"])
            )
            hist_expanding = bool(
                0 < frame["hist"].iloc[index - 2] < frame["hist"].iloc[index - 1] < current["hist"]
            )
            confirmations = sum((moderate_volume, price_breakout, hist_expanding))
            if zone in self.allowed_zones and confirmations >= self.min_confirmations:
                result[index] = {"zone": zone, "confirmations": confirmations}
        return result

    def generate_signals(self, price_data: pd.DataFrame) -> List[Dict[str, Any]]:
        if price_data.empty:
            return []
        frame = self._enrich(price_data)
        chan = analyze_chan(
            frame,
            min_bi_bars=self.min_bi_bars,
            divergence_ratio=self.divergence_ratio,
        )
        signal_rows: dict[pd.Timestamp, list[dict[str, Any]]] = {}
        for signal in chan.get("signals", []):
            timestamp = pd.Timestamp(signal["confirmed_at"])
            signal_rows.setdefault(timestamp, []).append(signal)
        qualified_crosses = self._qualified_crosses(frame)
        signals: List[Dict[str, Any]] = []
        in_position = False
        entry_price = 0.0
        entry_index = -1

        for index, current in frame.iterrows():
            current_time = pd.Timestamp(current["datetime"])
            chan_signals = signal_rows.get(current_time, [])
            buy_points = [item for item in chan_signals if item.get("side") == "buy"]
            sell_points = [item for item in chan_signals if item.get("side") == "sell"]
            recent_cross = next(
                (
                    qualified_crosses[cross_index]
                    for cross_index in range(index, max(-1, index - self.cross_window_bars), -1)
                    if cross_index in qualified_crosses
                ),
                None,
            )
            if not in_position and buy_points and recent_cross:
                signals.append(
                    {
                        "datetime": current_time.isoformat(),
                        "action": "BUY",
                        "position_pct": 0.95,
                        "reason": (
                            f"缠论{buy_points[0]['signal_type']} + MACD {recent_cross['zone']}金叉"
                            f"（{recent_cross['confirmations']}项确认）"
                        ),
                    }
                )
                in_position = True
                entry_price = float(current["close"])
                entry_index = index
                continue
            if not in_position:
                continue

            profit_pct = (float(current["close"]) - entry_price) / entry_price if entry_price else 0.0
            death_cross = bool(
                index > 0
                and pd.notna(current["dea"])
                and current["dif"] < current["dea"]
                and frame["dif"].iloc[index - 1] >= frame["dea"].iloc[index - 1]
            )
            exit_reason = ""
            if sell_points:
                exit_reason = f"缠论{sell_points[0]['signal_type']}"
            elif death_cross:
                exit_reason = "MACD死叉"
            elif profit_pct <= -self.stop_loss_pct:
                exit_reason = "止损"
            elif profit_pct >= self.stop_profit_pct:
                exit_reason = "止盈"
            elif index - entry_index >= self.max_holding_bars:
                exit_reason = "达到最长持有期"
            if exit_reason:
                signals.append(
                    {
                        "datetime": current_time.isoformat(),
                        "action": "SELL",
                        "reason": exit_reason,
                    }
                )
                in_position = False
                entry_price = 0.0
                entry_index = -1
        return signals
