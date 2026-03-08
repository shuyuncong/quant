"""Position entity with base and mobile inventory separation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class Position:
    """Represents one symbol position split into base and mobile tranches."""

    ts_code: str
    name: str = ""
    base_shares: int = 0
    base_cost: float = 0.0
    mobile_shares: int = 0
    mobile_cost: float = 0.0
    current_price: float = 0.0

    @property
    def total_shares(self) -> int:
        return self.base_shares + self.mobile_shares

    @property
    def base_market_value(self) -> float:
        return self.base_shares * self.current_price

    @property
    def mobile_market_value(self) -> float:
        return self.mobile_shares * self.current_price

    @property
    def market_value(self) -> float:
        return self.total_shares * self.current_price

    @property
    def total_cost(self) -> float:
        return self.base_shares * self.base_cost + self.mobile_shares * self.mobile_cost

    @property
    def average_cost(self) -> float:
        if self.total_shares == 0:
            return 0.0
        return self.total_cost / self.total_shares

    @property
    def profit_loss(self) -> float:
        return self.market_value - self.total_cost

    @property
    def profit_rate(self) -> float:
        if self.total_cost == 0:
            return 0.0
        return self.profit_loss / self.total_cost

    def update_price(self, current_price: float) -> None:
        self.current_price = current_price

    def add_base(self, shares: int, price: float) -> None:
        self.base_cost = self._recalculate_cost(self.base_shares, self.base_cost, shares, price)
        self.base_shares += shares

    def add_mobile(self, shares: int, price: float) -> None:
        self.mobile_cost = self._recalculate_cost(self.mobile_shares, self.mobile_cost, shares, price)
        self.mobile_shares += shares

    def snapshot(self) -> Dict:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "base_shares": self.base_shares,
            "base_cost": round(self.base_cost, 4),
            "mobile_shares": self.mobile_shares,
            "mobile_cost": round(self.mobile_cost, 4),
            "current_price": round(self.current_price, 4),
            "market_value": round(self.market_value, 4),
            "profit_loss": round(self.profit_loss, 4),
            "profit_rate": round(self.profit_rate, 6),
        }

    @staticmethod
    def _recalculate_cost(existing_shares: int, existing_cost: float, new_shares: int, new_price: float) -> float:
        total_shares = existing_shares + new_shares
        if total_shares == 0:
            return 0.0
        total_cost = existing_shares * existing_cost + new_shares * new_price
        return total_cost / total_shares
