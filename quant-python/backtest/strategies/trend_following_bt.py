"""Trend-following backtest strategy signal generator."""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd


class TrendFollowingBacktestStrategy:
    """Generates basic trend-following buy/sell signals from price data."""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        strategy_cfg = self.config.get("strategy", {}).get("technical", {})
        risk_cfg = self.config.get("risk", {})
        self.ma_period = strategy_cfg.get("ma_period", 20)
        self.stop_loss_pct = risk_cfg.get("stop_loss_pct", 0.08)
        self.stop_profit_pct = risk_cfg.get("stop_profit_pct", 0.30)

    def generate_signals(self, price_data: pd.DataFrame) -> List[Dict]:
        if price_data.empty:
            return []

        frame = price_data.copy().reset_index(drop=True)
        if "datetime" not in frame.columns:
            frame["datetime"] = pd.to_datetime(frame.index)
        else:
            frame["datetime"] = pd.to_datetime(frame["datetime"])

        frame["ma_long"] = frame["close"].rolling(self.ma_period).mean()
        frame["volume_ma"] = frame["volume"].rolling(5).mean() if "volume" in frame.columns else 1.0

        signals: List[Dict] = []
        in_position = False
        entry_price = 0.0

        for index in range(1, len(frame)):
            current = frame.iloc[index]
            previous = frame.iloc[index - 1]
            if pd.isna(current["ma_long"]) or pd.isna(previous["ma_long"]):
                continue

            if not in_position:
                crossed_up = previous["close"] <= previous["ma_long"] and current["close"] > current["ma_long"]
                volume_confirmed = current.get("volume", current.get("vol", 0)) >= current.get("volume_ma", 0)
                if crossed_up and volume_confirmed:
                    signals.append({
                        "datetime": current["datetime"].isoformat(),
                        "action": "BUY",
                        "position_pct": 0.95,
                        "reason": "突破长期均线",
                    })
                    in_position = True
                    entry_price = float(current["close"])
                continue

            profit_pct = (float(current["close"]) - entry_price) / entry_price if entry_price else 0.0
            crossed_down = previous["close"] >= previous["ma_long"] and current["close"] < current["ma_long"]
            should_exit = crossed_down or profit_pct <= -self.stop_loss_pct or profit_pct >= self.stop_profit_pct
            if should_exit:
                signals.append({
                    "datetime": current["datetime"].isoformat(),
                    "action": "SELL",
                    "reason": "跌破长期均线/止损止盈",
                })
                in_position = False
                entry_price = 0.0

        return signals
