"""Position sizing and portfolio constraints."""

from __future__ import annotations

import math
from typing import Dict, Optional

from .position import Position


class PositionManager:
    """Enforces portfolio structure rules from config."""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        position_config = self.config.get("position", {})
        self.min_stocks = position_config.get("min_stocks", 2)
        self.target_stocks = position_config.get("target_stocks", 3)
        self.max_stocks = position_config.get("max_stocks", 4)
        self.base_position_per_stock = position_config.get("base_position_per_stock", 0.25)
        self.mobile_cash_ratio = position_config.get("mobile_cash_ratio", 0.25)
        self.max_position_per_stock = position_config.get("max_position_per_stock", 0.40)
        self.lot_size = self.config.get("backtest", {}).get("lot_size", 100)
        self.override_config = self.config.get("manual_overrides", {})

    def validate_symbol_count(self, symbol_count: int) -> bool:
        return self.min_stocks <= symbol_count <= self.max_stocks

    def can_open_new_position(self, current_symbol_count: int) -> bool:
        if self.override_config.get("disable_new_positions", False):
            return False
        return current_symbol_count < self.max_stocks

    def base_exposure_ratio(self) -> float:
        diversified_base = (1.0 - self.mobile_cash_ratio) / max(self.target_stocks, 1)
        return min(self.base_position_per_stock, diversified_base, self.max_position_per_stock)

    def mobile_exposure_ratio(self) -> float:
        available = max(self.max_position_per_stock - self.base_exposure_ratio(), 0.0)
        return min(self.mobile_cash_ratio, available)

    def total_single_stock_limit(self) -> float:
        return min(self.base_exposure_ratio() + self.mobile_exposure_ratio(), self.max_position_per_stock)

    def remaining_total_exposure(self, active_exposure: float) -> float:
        max_total_exposure = self.override_config.get("max_total_exposure", 1.0)
        return max(max_total_exposure - active_exposure, 0.0)

    def recommend_position(self, total_capital: float, current_price: float) -> Dict[str, float]:
        base_ratio = self.base_exposure_ratio()
        mobile_ratio = self.mobile_exposure_ratio()
        total_ratio = self.total_single_stock_limit()

        base_budget = total_capital * base_ratio
        mobile_budget = total_capital * mobile_ratio

        base_shares = self._round_lot(base_budget, current_price)
        mobile_shares = self._round_lot(mobile_budget, current_price)

        return {
            "base_ratio": round(base_ratio, 4),
            "mobile_ratio": round(mobile_ratio, 4),
            "total_ratio": round(total_ratio, 4),
            "base_budget": round(base_budget, 2),
            "mobile_budget": round(mobile_budget, 2),
            "base_shares": base_shares,
            "mobile_shares": mobile_shares,
            "total_shares": base_shares + mobile_shares,
        }

    def build_position(self, ts_code: str, name: str, total_capital: float, current_price: float) -> Position:
        recommendation = self.recommend_position(total_capital, current_price)
        position = Position(
            ts_code=ts_code,
            name=name,
            current_price=current_price,
        )
        if recommendation["base_shares"] > 0:
            position.add_base(recommendation["base_shares"], current_price)
        if recommendation["mobile_shares"] > 0:
            position.add_mobile(recommendation["mobile_shares"], current_price)
        return position

    def validate_single_position(self, exposure_ratio: float) -> bool:
        return exposure_ratio <= self.max_position_per_stock

    def _round_lot(self, budget: float, price: float) -> int:
        if budget <= 0 or price <= 0:
            return 0
        raw_shares = math.floor(budget / price)
        return raw_shares - (raw_shares % self.lot_size)
