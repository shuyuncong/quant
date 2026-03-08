"""Risk management helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class RiskDecision:
    """Structured risk evaluation result."""

    allowed: bool
    action: str
    reasons: List[str]
    risk_flags: List[str]
    suggested_position_change: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "reasons": self.reasons,
            "risk_flags": self.risk_flags,
            "suggested_position_change": self.suggested_position_change,
        }


class RiskManager:
    """Evaluates position and portfolio level risk constraints."""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.risk_config = self.config.get("risk", {})
        self.override_config = self.config.get("manual_overrides", {})
        self.position_config = self.config.get("position", {})

    def evaluate_position(
        self,
        profit_pct: float,
        tech_result: Dict,
        market_status: str = "range",
        holding_days: Optional[int] = None,
        volatility_pct: Optional[float] = None,
    ) -> RiskDecision:
        reasons: List[str] = []
        flags: List[str] = []
        action = "HOLD"
        suggested_position_change = 0.0

        stop_loss_pct, stop_profit_pct = self._resolve_dynamic_thresholds(
            holding_days=holding_days,
            volatility_pct=volatility_pct,
        )

        if profit_pct <= -stop_loss_pct:
            reasons.append(
                f"触发止损阈值({profit_pct * 100:.2f}%, 阈值 {stop_loss_pct * 100:.2f}%)"
            )
            flags.append("stop_loss")

        if profit_pct >= stop_profit_pct:
            reasons.append(
                f"触发止盈阈值({profit_pct * 100:.2f}%, 阈值 {stop_profit_pct * 100:.2f}%)"
            )
            flags.append("take_profit")

        if not tech_result.get("is_above_ma250", True) and tech_result.get("ma250_slope", 0) < 0:
            reasons.append("跌破长期均线且趋势转弱")
            flags.append("trend_breakdown")

        if market_status == "bear" and profit_pct < 0:
            reasons.append("熊市环境下亏损持仓承压")
            flags.append("bear_market_pressure")

        if reasons:
            if "stop_loss" in flags or "trend_breakdown" in flags:
                action = "SELL"
                suggested_position_change = -1.0
            else:
                action = "REDUCE"
                suggested_position_change = -0.25

        return RiskDecision(
            allowed=action == "HOLD",
            action=action,
            reasons=reasons,
            risk_flags=flags,
            suggested_position_change=suggested_position_change,
        )

    def evaluate_portfolio(self, portfolio_stats: Optional[Dict] = None) -> RiskDecision:
        portfolio_stats = portfolio_stats or {}
        reasons: List[str] = []
        flags: List[str] = []

        if self.override_config.get("disable_new_positions", False):
            reasons.append("人工开关禁止新增仓位")
            flags.append("manual_disable_new_positions")

        portfolio_drawdown = portfolio_stats.get("portfolio_drawdown_pct", 0.0)
        single_day_drawdown = portfolio_stats.get("single_day_drawdown_pct", 0.0)
        current_exposure = portfolio_stats.get("current_exposure_pct", 0.0)

        if portfolio_drawdown > self.risk_config.get("max_portfolio_drawdown_pct", 0.20):
            reasons.append("组合回撤超过阈值")
            flags.append("portfolio_drawdown_limit")

        if single_day_drawdown > self.risk_config.get("max_single_day_drawdown_pct", 0.02):
            reasons.append("单日回撤超过阈值")
            flags.append("single_day_drawdown_limit")

        if current_exposure > self.override_config.get("max_total_exposure", 1.0):
            reasons.append("总仓位超过人工上限")
            flags.append("manual_exposure_limit")

        if self.override_config.get("only_reduce_positions", False):
            reasons.append("当前仅允许减仓，不允许新增")
            flags.append("only_reduce_positions")

        allow_new_position_when_drawdown_exceeded = self.risk_config.get(
            "allow_new_position_when_drawdown_exceeded",
            False,
        )
        disallowed_by_drawdown = (
            ("portfolio_drawdown_limit" in flags or "single_day_drawdown_limit" in flags)
            and not allow_new_position_when_drawdown_exceeded
        )

        allowed = not (
            flags
            and (
                disallowed_by_drawdown
                or "manual_disable_new_positions" in flags
                or "only_reduce_positions" in flags
                or "manual_exposure_limit" in flags
            )
        )
        action = "ALLOW_NEW_POSITION" if allowed else "BLOCK_NEW_POSITION"

        return RiskDecision(
            allowed=allowed,
            action=action,
            reasons=reasons,
            risk_flags=flags,
            suggested_position_change=0.0,
        )

    def _resolve_dynamic_thresholds(
        self,
        holding_days: Optional[int] = None,
        volatility_pct: Optional[float] = None,
    ) -> tuple[float, float]:
        stop_loss_pct = float(self.risk_config.get("stop_loss_pct", 0.08))
        stop_profit_pct = float(self.risk_config.get("stop_profit_pct", 0.30))

        if holding_days is not None:
            threshold_days = int(self.risk_config.get("long_holding_days_threshold", 40))
            if holding_days >= threshold_days:
                stop_loss_pct *= float(self.risk_config.get("long_holding_stop_loss_multiplier", 1.25))
                stop_profit_pct *= float(self.risk_config.get("long_holding_stop_profit_multiplier", 1.10))

        if volatility_pct is not None:
            volatility_threshold = float(self.risk_config.get("high_volatility_threshold_pct", 0.35))
            if volatility_pct >= volatility_threshold:
                stop_loss_pct *= float(self.risk_config.get("high_volatility_stop_loss_multiplier", 1.20))
                stop_profit_pct *= float(self.risk_config.get("high_volatility_stop_profit_multiplier", 1.10))

        return stop_loss_pct, stop_profit_pct
