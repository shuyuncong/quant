"""Strategy routing and conflict resolution."""

from __future__ import annotations

from typing import Dict, List, Optional

from .strategies import BreakoutStrategy, DefensiveStrategy, MeanReversionStrategy


class StrategyRouter:
    """Routes signals according to market regime and resolves conflicts."""

    ACTION_PRIORITY = {
        "SELL": 4,
        "REDUCE": 3,
        "ADD": 2,
        "BUY": 1,
    }

    def __init__(self):
        self.mean_reversion = MeanReversionStrategy()
        self.defensive = DefensiveStrategy()
        self.breakout = BreakoutStrategy()

    def route_signals(
        self,
        market_status: str,
        candidate_pool: List[Dict],
        positions: Optional[List[Dict]] = None,
        primary_signals: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        positions = positions or []
        primary_signals = primary_signals or []
        position_lookup = {position["ts_code"]: position for position in positions}
        routed_signals = list(primary_signals)

        for stock in candidate_pool:
            current_position = position_lookup.get(stock["ts_code"])
            if market_status == "range":
                signal = self.mean_reversion.generate(stock, current_position)
                if signal:
                    routed_signals.append(signal)
                breakout_signal = self.breakout.generate(stock, current_position)
                if breakout_signal:
                    breakout_signal["score"] -= 5
                    routed_signals.append(breakout_signal)
            elif market_status == "bear":
                signal = self.defensive.generate(stock, current_position)
                if signal:
                    routed_signals.append(signal)
            else:
                breakout_signal = self.breakout.generate(stock, current_position)
                if breakout_signal:
                    routed_signals.append(breakout_signal)

        return self.resolve_conflicts(routed_signals)

    def resolve_conflicts(self, signals: List[Dict]) -> List[Dict]:
        resolved: Dict[str, Dict] = {}
        for signal in signals:
            ts_code = signal["ts_code"]
            current = resolved.get(ts_code)
            if current is None:
                resolved[ts_code] = signal
                continue

            current_priority = self.ACTION_PRIORITY.get(current.get("signal_type"), 0)
            incoming_priority = self.ACTION_PRIORITY.get(signal.get("signal_type"), 0)
            if incoming_priority > current_priority:
                resolved[ts_code] = signal
            elif incoming_priority == current_priority and signal.get("score", 0) > current.get("score", 0):
                resolved[ts_code] = signal

        return sorted(resolved.values(), key=lambda item: item.get("score", 0), reverse=True)
