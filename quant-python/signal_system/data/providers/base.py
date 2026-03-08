"""Base interfaces for market data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional

import pandas as pd


class BaseMarketDataProvider(ABC):
    """Abstract market data provider."""

    name = "base"

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}

    @abstractmethod
    def get_stock_list(self, exchange: str = "", list_status: str = "L") -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def get_daily_data(
        self,
        ts_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def get_index_daily(
        self,
        ts_code: str = "000001.SH",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def get_daily_basic(self, ts_code: str, trade_date: Optional[str] = None) -> Optional[Dict]:
        raise NotImplementedError

    @abstractmethod
    def get_financial_data(self, ts_code: str, period: Optional[str] = None) -> Optional[Dict]:
        raise NotImplementedError

    def get_trade_calendar(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        return pd.DataFrame()

    def get_latest_trade_date(self) -> Optional[str]:
        return None

    def get_minute_data(
        self,
        ts_code: str,
        interval: str = "5m",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        count: Optional[int] = None,
    ) -> pd.DataFrame:
        return pd.DataFrame()
