"""China A-share trading cost model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Optional


@dataclass(frozen=True)
class TradeCostBreakdown:
    """Trade cost details."""

    side: str
    shares: int
    price: float
    fill_price: float
    notional: float
    commission: float
    stamp_tax: float
    slippage_cost: float
    cash_flow: float

    def to_dict(self) -> Dict:
        return {
            "side": self.side,
            "shares": self.shares,
            "price": self.price,
            "fill_price": self.fill_price,
            "notional": self.notional,
            "commission": self.commission,
            "stamp_tax": self.stamp_tax,
            "slippage_cost": self.slippage_cost,
            "cash_flow": self.cash_flow,
        }


class ChinaCostModel:
    """Models commission, stamp tax, slippage, lot size and T+1 constraints."""

    def __init__(
        self,
        commission_pct: float = 0.0003,
        stamp_tax_pct: float = 0.001,
        slippage_pct: float = 0.0005,
        lot_size: int = 100,
        t_plus_one: bool = True,
        price_limit_model: str = "conservative",
    ):
        self.commission_pct = commission_pct
        self.stamp_tax_pct = stamp_tax_pct
        self.slippage_pct = slippage_pct
        self.lot_size = lot_size
        self.t_plus_one = t_plus_one
        self.price_limit_model = price_limit_model

    def normalize_shares(self, shares: int) -> int:
        if shares <= 0:
            return 0
        return shares - (shares % self.lot_size)

    def estimate_trade(self, price: float, shares: int, side: str) -> TradeCostBreakdown:
        normalized_shares = self.normalize_shares(shares)
        fill_price = self.apply_slippage(price, side)
        notional = fill_price * normalized_shares
        commission = notional * self.commission_pct
        stamp_tax = notional * self.stamp_tax_pct if side.upper() == "SELL" else 0.0
        slippage_cost = abs(fill_price - price) * normalized_shares

        if side.upper() == "BUY":
            cash_flow = -(notional + commission + stamp_tax)
        else:
            cash_flow = notional - commission - stamp_tax

        return TradeCostBreakdown(
            side=side.upper(),
            shares=normalized_shares,
            price=price,
            fill_price=fill_price,
            notional=notional,
            commission=commission,
            stamp_tax=stamp_tax,
            slippage_cost=slippage_cost,
            cash_flow=cash_flow,
        )

    def apply_slippage(self, price: float, side: str) -> float:
        multiplier = 1 + self.slippage_pct if side.upper() == "BUY" else 1 - self.slippage_pct
        return price * multiplier

    def can_sell(self, entry_dt: Optional[datetime], exit_dt: datetime) -> bool:
        if not self.t_plus_one or entry_dt is None:
            return True
        return self._to_date(exit_dt) > self._to_date(entry_dt)

    def validate_price_limit(self, price_change_pct: Optional[float]) -> bool:
        if price_change_pct is None:
            return True
        if self.price_limit_model == "conservative":
            return abs(price_change_pct) < 0.098
        return abs(price_change_pct) < 0.198

    @staticmethod
    def _to_date(value: datetime) -> date:
        return value.date() if isinstance(value, datetime) else value
