"""Market data orchestration layer with provider routing and cache handling."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import os
import pickle
from typing import Any, Dict, Iterable, Optional

import pandas as pd

from .providers import AkshareDailyProvider, PytdxMinuteProvider

logger = logging.getLogger(__name__)


class MarketDataService:
    """Routes data requests to the appropriate provider and applies cache policy."""

    def __init__(self, config: Optional[Dict] = None, providers: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        data_config = self.config.get("data", {})
        data_source_config = self.config.get("data_source", {})

        self.daily_provider_name = str(data_config.get("provider", "akshare"))
        self.minute_provider_name = str(data_config.get("minute_provider", "pytdx"))
        self.fallback_provider_name = str(data_config.get("fallback_provider", "") or "")
        self.minute_fallback_provider_name = str(data_config.get("minute_fallback_provider", "") or "")
        self.use_cache = bool(data_source_config.get("use_cache", True))
        self.cache_dir = str(data_config.get("cache_dir") or data_source_config.get("cache_dir") or "./cache")
        self.daily_cache_hours = int(data_config.get("daily_cache_hours", 24))
        self.minute_cache_hours = int(data_config.get("minute_cache_hours", 6))
        self.fundamentals_cache_hours = int(data_config.get("fundamentals_cache_hours", 168))

        if self.use_cache:
            os.makedirs(self.cache_dir, exist_ok=True)

        self.providers = providers or {
            "akshare": AkshareDailyProvider(config=self.config),
            "pytdx": PytdxMinuteProvider(config=self.config),
        }

    def get_stock_list(self, exchange: str = "", list_status: str = "L") -> pd.DataFrame:
        return self._get_dataframe_with_cache(
            provider_name=self.daily_provider_name,
            method_name="get_stock_list",
            cache_key=f"{self.daily_provider_name}_stock_list_{exchange}_{list_status}",
            cache_hours=24,
            default=pd.DataFrame(),
            exchange=exchange,
            list_status=list_status,
        )

    def get_daily_data(
        self,
        ts_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ) -> pd.DataFrame:
        start_date, end_date = self._resolve_date_range(start_date=start_date, end_date=end_date, period=period)
        return self._get_dataframe_with_cache(
            provider_name=self.daily_provider_name,
            method_name="get_daily_data",
            cache_key=f"{self.daily_provider_name}_daily_{ts_code}_{start_date}_{end_date}",
            cache_hours=self.daily_cache_hours,
            default=pd.DataFrame(),
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            period=period,
        )

    def get_index_daily(
        self,
        ts_code: str = "000001.SH",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ) -> pd.DataFrame:
        start_date, end_date = self._resolve_date_range(start_date=start_date, end_date=end_date, period=period)
        return self._get_dataframe_with_cache(
            provider_name=self.daily_provider_name,
            method_name="get_index_daily",
            cache_key=f"{self.daily_provider_name}_index_daily_{ts_code}_{start_date}_{end_date}",
            cache_hours=self.daily_cache_hours,
            default=pd.DataFrame(),
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            period=period,
        )

    def get_daily_basic(self, ts_code: str, trade_date: Optional[str] = None) -> Optional[Dict]:
        trade_date = trade_date or self.get_latest_trade_date()
        return self._get_object_with_cache(
            provider_name=self.daily_provider_name,
            method_name="get_daily_basic",
            cache_key=f"{self.daily_provider_name}_daily_basic_{ts_code}_{trade_date}",
            cache_hours=self.daily_cache_hours,
            default=None,
            ts_code=ts_code,
            trade_date=trade_date,
        )

    def get_financial_data(self, ts_code: str, period: Optional[str] = None) -> Optional[Dict]:
        report_period = period or self.get_latest_report_period()
        return self._get_object_with_cache(
            provider_name=self.daily_provider_name,
            method_name="get_financial_data",
            cache_key=f"{self.daily_provider_name}_financial_{ts_code}_{report_period}",
            cache_hours=self.fundamentals_cache_hours,
            default=None,
            ts_code=ts_code,
            period=report_period,
        )

    def get_trade_calendar(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        start_date = start_date or (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        end_date = end_date or datetime.now().strftime("%Y%m%d")
        return self._get_dataframe_with_cache(
            provider_name=self.daily_provider_name,
            method_name="get_trade_calendar",
            cache_key=f"{self.daily_provider_name}_trade_calendar_{start_date}_{end_date}",
            cache_hours=24,
            default=pd.DataFrame(),
            start_date=start_date,
            end_date=end_date,
        )

    def get_latest_trade_date(self) -> str:
        calendar = self.get_trade_calendar(
            start_date=(datetime.now() - timedelta(days=14)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
        )
        if calendar.empty or "trade_date" not in calendar.columns:
            provider = self._get_provider(self.daily_provider_name)
            if provider is None:
                return datetime.now().strftime("%Y%m%d")
            latest = provider.get_latest_trade_date()
            return latest or datetime.now().strftime("%Y%m%d")
        return str(calendar.iloc[-1]["trade_date"])

    def get_minute_data(
        self,
        ts_code: str,
        interval: str = "5m",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        count: Optional[int] = None,
    ) -> pd.DataFrame:
        cache_suffix = f"{start_date or 'latest'}_{end_date or count or 'default'}"
        return self._get_dataframe_with_cache(
            provider_name=self.minute_provider_name,
            method_name="get_minute_data",
            cache_key=f"{self.minute_provider_name}_minute_{ts_code}_{interval}_{cache_suffix}",
            cache_hours=self.minute_cache_hours,
            default=pd.DataFrame(),
            ts_code=ts_code,
            interval=interval,
            start_date=start_date,
            end_date=end_date,
            count=count,
        )

    @staticmethod
    def get_latest_report_period(now: Optional[datetime] = None) -> str:
        now = now or datetime.now()
        year = now.year
        month = now.month

        if month >= 10:
            return f"{year}0930"
        if month >= 7:
            return f"{year}0630"
        if month >= 4:
            return f"{year}0331"
        return f"{year - 1}0930"

    def _get_dataframe_with_cache(
        self,
        provider_name: str,
        method_name: str,
        cache_key: str,
        cache_hours: int,
        default: pd.DataFrame,
        **kwargs,
    ) -> pd.DataFrame:
        cached = self._load_cache(cache_key, cache_hours)
        if isinstance(cached, pd.DataFrame):
            return cached

        frame = self._call_provider(provider_name, method_name, default=default, **kwargs)
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            self._save_cache(cache_key, frame)
        return frame if isinstance(frame, pd.DataFrame) else default

    def _get_object_with_cache(
        self,
        provider_name: str,
        method_name: str,
        cache_key: str,
        cache_hours: int,
        default,
        **kwargs,
    ):
        cached = self._load_cache(cache_key, cache_hours)
        if cached is not None:
            return cached

        result = self._call_provider(provider_name, method_name, default=default, **kwargs)
        if result is not None:
            self._save_cache(cache_key, result)
        return result

    def _call_provider(self, provider_name: str, method_name: str, default, **kwargs):
        provider = self._get_provider(provider_name)
        if provider is None:
            logger.error("Unknown data provider: %s", provider_name)
            return default

        try:
            return getattr(provider, method_name)(**kwargs)
        except Exception as exc:
            logger.error(
                "Data provider call failed: provider=%s method=%s kwargs=%s error=%s",
                provider_name,
                method_name,
                kwargs,
                exc,
            )
            return default

    def _get_provider(self, provider_name: str):
        return self.providers.get(provider_name)

    def _get_cache_path(self, cache_key: str) -> str:
        return os.path.join(self.cache_dir, f"{cache_key}.pkl")

    def _load_cache(self, cache_key: str, max_age_hours: int):
        if not self.use_cache:
            return None

        cache_path = self._get_cache_path(cache_key)
        if not os.path.exists(cache_path):
            return None

        file_age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_path))
        if file_age > timedelta(hours=max_age_hours):
            return None

        try:
            with open(cache_path, "rb") as handle:
                return pickle.load(handle)
        except Exception as exc:
            logger.warning("Failed to load cache %s: %s", cache_key, exc)
            return None

    def _save_cache(self, cache_key: str, data) -> None:
        if not self.use_cache:
            return

        cache_path = self._get_cache_path(cache_key)
        try:
            with open(cache_path, "wb") as handle:
                pickle.dump(data, handle)
        except Exception as exc:
            logger.warning("Failed to save cache %s: %s", cache_key, exc)

    @staticmethod
    def _resolve_date_range(
        start_date: Optional[str],
        end_date: Optional[str],
        period: int,
    ) -> tuple[str, str]:
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=period * 2)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        return start_date, end_date
