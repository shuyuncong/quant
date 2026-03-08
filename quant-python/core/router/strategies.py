"""Market-specific strategy implementations."""

from __future__ import annotations

from typing import Dict, Optional

from core.position.position_manager import PositionManager


class TrendFollowingStrategy:
    """趋势跟随策略。

    目标是抓住“长期趋势未坏、短期回踩后重新启动”的买点。
    """

    name = "trend_following"

    def __init__(self, position_manager: Optional[PositionManager] = None, config: Optional[Dict] = None):
        self.config = config or {}
        self.position_manager = position_manager or PositionManager(config=self.config)

    def generate(
        self,
        stock: Dict,
        market_status: str,
        current_position: Optional[Dict] = None,
        portfolio_risk=None,
    ) -> Optional[Dict]:
        """根据单只股票上下文生成趋势型 BUY / ADD 信号。"""
        if market_status == "bear":
            return None
        if current_position is None and portfolio_risk and not portfolio_risk.allowed:
            return None
        if current_position is not None and self.config.get("manual_overrides", {}).get("only_reduce_positions", False):
            return None

        signals = []
        # selector 给的是基础分，这里再叠加趋势确认信号。
        score = int(stock.get("selection_score", 0))

        if stock.get("ma250_slope", 0) > 0:
            signals.append("年线向上")
            score += 15

        if stock.get("near_ma250") and stock.get("is_above_ma250"):
            signals.append("回调至年线附近")
            score += 12

        if stock.get("divergence") == "bullish":
            signals.append("底背离")
            score += 18

        if stock.get("bear_trap"):
            signals.append("空头陷阱回收")
            score += 15

        if stock.get("macd_golden_cross"):
            signals.append("MACD金叉")
            score += 10

        if stock.get("volume_ratio", 0) > 1.5:
            signals.append("放量")
            score += 8

        if stock.get("price_change_pct", 0) < 0 and stock.get("volume_ratio", 0) <= 1.2:
            signals.append("缩量下跌")
            score += 6

        threshold = 75 if market_status == "bull" else 85
        if score < threshold:
            return None

        # 当前无仓位时是开仓，有仓位时才允许加仓。
        if current_position is None:
            signal_type = "BUY"
            action = "买入"
            suggested_ratio = self.position_manager.base_exposure_ratio()
            reason = "趋势策略买入点"
        else:
            signal_type = "ADD"
            action = "加仓"
            suggested_ratio = self.position_manager.mobile_exposure_ratio()
            reason = "上涨趋势中的回调加仓"

        if suggested_ratio <= 0:
            return None

        return {
            "ts_code": stock["ts_code"],
            "name": stock["name"],
            "price": stock["current_price"],
            "signal_type": signal_type,
            "action": action,
            "strategy_name": self.name,
            "market_status": market_status,
            "signals": signals,
            "score": score,
            "roe": stock.get("roe", 0),
            "pe": stock.get("pe", 0),
            "market_cap": stock.get("market_cap", 0),
            "reason": reason,
            "explanation": f"{action}依据: " + " + ".join(signals),
            "suggested_position_change": round(suggested_ratio, 4),
            "risk_flags": [],
        }


class MeanReversionStrategy:
    """震荡市均值回归策略。"""

    name = "mean_reversion"

    def generate(self, stock: Dict, current_position: Optional[Dict] = None) -> Optional[Dict]:
        """在区间震荡中寻找回踩后的反弹机会。"""
        score = int(stock.get("selection_score", 0))
        reasons = []

        if stock.get("divergence") == "bullish":
            reasons.append("底背离")
            score += 16
        if stock.get("near_ma250"):
            reasons.append("贴近均线")
            score += 12
        if stock.get("price_change_pct", 0) <= 0:
            reasons.append("回落后企稳")
            score += 8
        if stock.get("volume_ratio", 0) >= 1.0:
            reasons.append("量能可接受")
            score += 5

        if score < 70 or "底背离" not in reasons:
            return None

        signal_type = "BUY" if current_position is None else "ADD"
        action = "低吸买入" if current_position is None else "波段加仓"
        return {
            "ts_code": stock["ts_code"],
            "name": stock["name"],
            "price": stock["current_price"],
            "signal_type": signal_type,
            "action": action,
            "strategy_name": self.name,
            "score": score,
            "reason": "震荡市均值回归机会",
            "signals": reasons,
            "explanation": action + "依据: " + " + ".join(reasons),
            "suggested_position_change": 0.15 if current_position is None else 0.10,
            "risk_flags": [],
        }


class DefensiveStrategy:
    """熊市防守策略。

    核心目标不是进攻收益，而是限制回撤和控制试错仓位。
    """

    name = "defensive"

    def generate(self, stock: Dict, current_position: Optional[Dict] = None) -> Optional[Dict]:
        """在熊市环境下生成 REDUCE 或小仓位 BUY。"""
        score = 50
        reasons = []

        if stock.get("bear_trap"):
            reasons.append("空头陷阱")
            score += 15
        if stock.get("divergence") == "bullish":
            reasons.append("底背离")
            score += 10
        if stock.get("ma250_slope", 0) < 0:
            reasons.append("长期趋势偏弱")
            score += 5
        if stock.get("volume_ratio", 0) > 1.5:
            reasons.append("放量波动")
            score += 5

        if current_position is not None:
            if stock.get("divergence") == "bearish" or not stock.get("is_above_ma250", True):
                return {
                    "ts_code": stock["ts_code"],
                    "name": stock["name"],
                    "price": stock["current_price"],
                    "signal_type": "REDUCE",
                    "action": "防守减仓",
                    "strategy_name": self.name,
                    "score": max(score, 78),
                    "reason": "熊市环境优先防守",
                    "signals": reasons + ["降低风险暴露"],
                    "explanation": "防守减仓依据: " + " + ".join(reasons + ["降低风险暴露"]),
                    "suggested_position_change": -0.20,
                    "risk_flags": ["bear_market_defense"],
                }
            return None

        if score < 72 or "空头陷阱" not in reasons:
            return None

        return {
            "ts_code": stock["ts_code"],
            "name": stock["name"],
            "price": stock["current_price"],
            "signal_type": "BUY",
            "action": "防守试仓",
            "strategy_name": self.name,
            "score": score,
            "reason": "熊市下仅允许防守性试仓",
            "signals": reasons,
            "explanation": "防守试仓依据: " + " + ".join(reasons),
            "suggested_position_change": 0.10,
            "risk_flags": ["small_probe_only"],
        }


class BreakoutStrategy:
    """突破策略。

    关注“价格站上近期高点 + 量能确认”的启动形态。
    """

    name = "breakout"

    def generate(self, stock: Dict, current_position: Optional[Dict] = None) -> Optional[Dict]:
        """当突破条件成立时生成 BUY / ADD。"""
        score = int(stock.get("selection_score", 0))
        reasons = []

        if stock.get("close_above_recent_high"):
            reasons.append("突破近期高点")
            score += 18
        if stock.get("volume_ratio", 0) >= 1.6:
            reasons.append("放量确认")
            score += 12
        if stock.get("ma250_slope", 0) > 0:
            reasons.append("长期趋势支持")
            score += 8

        if score < 78 or "突破近期高点" not in reasons or "放量确认" not in reasons:
            return None

        signal_type = "BUY" if current_position is None else "ADD"
        action = "突破买入" if current_position is None else "突破加仓"
        ratio = 0.20 if current_position is None else 0.10
        return {
            "ts_code": stock["ts_code"],
            "name": stock["name"],
            "price": stock["current_price"],
            "signal_type": signal_type,
            "action": action,
            "strategy_name": self.name,
            "score": score,
            "reason": "突破策略信号成立",
            "signals": reasons,
            "explanation": action + "依据: " + " + ".join(reasons),
            "suggested_position_change": ratio,
            "risk_flags": [],
        }
