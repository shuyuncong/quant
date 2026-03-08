"""T-trading strategy helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .position_manager import PositionManager


@dataclass(frozen=True)
class TSignal:
    """Structured T-trading signal."""

    ts_code: str
    signal_type: str
    action: str
    reason: str
    confidence: int
    suggested_position_change: float
    suggested_amount: int = 0

    def to_dict(self) -> Dict:
        return {
            "ts_code": self.ts_code,
            "signal_type": self.signal_type,
            "action": self.action,
            "reason": self.reason,
            "confidence": self.confidence,
            "score": self.confidence,
            "suggested_position_change": self.suggested_position_change,
            "suggested_amount": self.suggested_amount,
        }


class TTradingStrategy:
    """Implements positive, negative and range T-trading suggestions."""

    def __init__(self, position_manager: PositionManager, config: Optional[Dict] = None):
        self.position_manager = position_manager
        self.config = config or {}
        self.t_config = self.config.get("t_trading", {})

    def analyze_t_opportunity(self, position: Dict, market_trend: str, indicators: Dict) -> Optional[Dict]:
        if not self.t_config.get("enabled", True):
            return None

        if market_trend == "bull":
            signal = self._analyze_positive_t(position, indicators)
        elif market_trend == "bear":
            signal = self._analyze_negative_t(position, indicators)
        else:
            signal = self._analyze_range_t(position, indicators)

        return signal.to_dict() if signal else None

    def _analyze_positive_t(self, position: Dict, indicators: Dict) -> Optional[TSignal]:
        mobile_ratio = min(
            self.position_manager.mobile_exposure_ratio(),
            self.t_config.get("positive_t_step_pct", 0.05),
        )

        if indicators.get("divergence") == "bullish" and indicators.get("near_ma250") and indicators.get("volume_ratio", 0) >= 1.0:
            return TSignal(
                ts_code=position["ts_code"],
                signal_type="positive_t_buy",
                action="加机动仓",
                reason="底背离 + 年线附近企稳，正T加机动仓",
                confidence=78,
                suggested_position_change=round(mobile_ratio, 4),
                suggested_amount=position.get("mobile_shares", 0),
            )

        if indicators.get("divergence") == "bearish" and position.get("mobile_shares", 0) > 0:
            return TSignal(
                ts_code=position["ts_code"],
                signal_type="positive_t_sell",
                action="减机动仓",
                reason="顶背离出现，正T先减机动仓",
                confidence=76,
                suggested_position_change=round(-mobile_ratio, 4),
                suggested_amount=position.get("mobile_shares", 0),
            )

        return None

    def _analyze_negative_t(self, position: Dict, indicators: Dict) -> Optional[TSignal]:
        base_reduce_ratio = min(
            self.t_config.get("negative_t_step_pct", 0.05),
            0.30,
        )

        if indicators.get("divergence") == "bearish" and position.get("base_shares", 0) > 0:
            return TSignal(
                ts_code=position["ts_code"],
                signal_type="negative_t_sell",
                action="减基本仓",
                reason="下跌趋势中的顶背离，反T先减基本仓",
                confidence=74,
                suggested_position_change=round(-base_reduce_ratio, 4),
                suggested_amount=int(position.get("base_shares", 0) * 0.3),
            )

        if indicators.get("divergence") == "bullish" and indicators.get("macd_golden_cross"):
            return TSignal(
                ts_code=position["ts_code"],
                signal_type="negative_t_buy",
                action="买回基本仓",
                reason="快速下跌后企稳 + 底背离，反T买回基本仓",
                confidence=72,
                suggested_position_change=round(base_reduce_ratio, 4),
                suggested_amount=int(position.get("base_shares", 0) * 0.3),
            )

        return None

    def _analyze_range_t(self, position: Dict, indicators: Dict) -> Optional[TSignal]:
        range_ratio = min(
            self.position_manager.mobile_exposure_ratio() or 0.05,
            self.t_config.get("range_t_step_pct", 0.05),
        )

        if indicators.get("divergence") == "bullish" and indicators.get("price_change_pct", 0) <= 0:
            return TSignal(
                ts_code=position["ts_code"],
                signal_type="range_t_buy",
                action="低吸机动仓",
                reason="震荡市底背离，低吸机动仓",
                confidence=70,
                suggested_position_change=round(range_ratio, 4),
                suggested_amount=position.get("mobile_shares", 0),
            )

        if indicators.get("divergence") == "bearish" and position.get("mobile_shares", 0) > 0:
            return TSignal(
                ts_code=position["ts_code"],
                signal_type="range_t_sell",
                action="高抛机动仓",
                reason="震荡市顶背离，高抛机动仓",
                confidence=70,
                suggested_position_change=round(-range_ratio, 4),
                suggested_amount=position.get("mobile_shares", 0),
            )

        return None
