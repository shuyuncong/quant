"""Strategy routing and conflict resolution."""

from __future__ import annotations

from typing import Dict, List, Optional

from core.position.position_manager import PositionManager

from .strategies import BreakoutStrategy, DefensiveStrategy, MeanReversionStrategy, TrendFollowingStrategy


class StrategyRouter:
    """按市场状态选择可用策略，并解决同一股票的信号冲突。

    设计约束:
    - bull: 优先做趋势和突破
    - range: 允许趋势、均值回归、突破并行尝试
    - bear: 只允许防守型动作
    """

    ACTION_PRIORITY = {
        "SELL": 4,
        "REDUCE": 3,
        "ADD": 2,
        "BUY": 1,
    }

    def __init__(self, config: Optional[Dict] = None, position_manager: Optional[PositionManager] = None):
        self.config = config or {}
        self.position_manager = position_manager or PositionManager(config=self.config)
        self.trend_following = TrendFollowingStrategy(
            position_manager=self.position_manager,
            config=self.config,
        )
        self.mean_reversion = MeanReversionStrategy()
        self.defensive = DefensiveStrategy()
        self.breakout = BreakoutStrategy()

    def route_signals(
        self,
        market_status: str,
        candidate_pool: List[Dict],
        positions: Optional[List[Dict]] = None,
        primary_signals: Optional[List[Dict]] = None,
        portfolio_risk=None,
    ) -> List[Dict]:
        """为候选池生成最终交易信号。"""
        positions = positions or []
        primary_signals = primary_signals or []
        position_lookup = {position["ts_code"]: position for position in positions}
        routed_signals = list(primary_signals)

        for stock in candidate_pool:
            current_position = position_lookup.get(stock["ts_code"])
            if market_status == "range":
                # 震荡市需要多策略并行尝试，最后再靠优先级和分数选优。
                trend_signal = self.trend_following.generate(
                    stock,
                    market_status,
                    current_position=current_position,
                    portfolio_risk=portfolio_risk,
                )
                if trend_signal:
                    routed_signals.append(trend_signal)
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
                # bull 市里，明显突破 setup 直接优先跑 breakout，
                # 这样可以避免同一只票先算一遍趋势、再算一遍突破。
                if self._is_breakout_setup_candidate(stock):
                    breakout_signal = self.breakout.generate(stock, current_position)
                    if breakout_signal:
                        routed_signals.append(breakout_signal)
                        continue

                trend_signal = self.trend_following.generate(
                    stock,
                    market_status,
                    current_position=current_position,
                    portfolio_risk=portfolio_risk,
                )
                if trend_signal:
                    routed_signals.append(trend_signal)

        return self.resolve_conflicts(routed_signals)

    @staticmethod
    def _is_breakout_setup_candidate(stock: Dict) -> bool:
        """判断是否属于值得优先尝试 breakout 的候选。"""
        return bool(stock.get("close_above_recent_high")) and float(stock.get("volume_ratio", 0.0)) >= 1.6

    def resolve_conflicts(self, signals: List[Dict]) -> List[Dict]:
        """同一股票只保留一条最终信号。

        规则:
        - 先比较动作优先级: SELL > REDUCE > ADD > BUY
        - 动作相同再比较 score
        """
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
