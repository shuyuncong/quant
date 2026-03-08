"""Compatibility facade for the market data service layer."""

from __future__ import annotations

from copy import deepcopy
import logging
from typing import Dict, Optional

from .market_data_service import MarketDataService

logger = logging.getLogger(__name__)


class DataFetcher:
    """Backward-compatible data access facade.

    Existing upper layers still call `DataFetcher`, but the actual implementation
    is now delegated to `MarketDataService` and provider adapters.
    """

    def __init__(
        self,
        tushare_token: Optional[str] = None,
        use_cache: bool = True,
        cache_dir: str = "./cache",
        config: Optional[Dict] = None,
        providers: Optional[Dict] = None,
    ):
        self.config = self._build_runtime_config(
            tushare_token=tushare_token,
            use_cache=use_cache,
            cache_dir=cache_dir,
            config=config,
        )
        self.market_data = MarketDataService(config=self.config, providers=providers)
        self.tushare_token = tushare_token
        self.use_cache = bool(self.config.get("data_source", {}).get("use_cache", use_cache))
        self.cache_dir = str(self.config.get("data", {}).get("cache_dir", cache_dir))

    def get_stock_list(self, exchange: str = "", list_status: str = "L"):
        """Get stock list through the configured daily provider."""
        return self.market_data.get_stock_list(exchange=exchange, list_status=list_status)

    def get_daily_data(
        self,
        ts_code,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ):
        """Get daily OHLCV data through the configured daily provider."""
        return self.market_data.get_daily_data(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            period=period,
        )

    def get_financial_data(self, ts_code, period: Optional[str] = None):
        """Get financial data used by stock selection."""
        return self.market_data.get_financial_data(ts_code=ts_code, period=period)

    def get_daily_basic(self, ts_code, trade_date: Optional[str] = None):
        """Get daily basic data such as turnover rate, volume ratio and valuation."""
        return self.market_data.get_daily_basic(ts_code=ts_code, trade_date=trade_date)

    def get_index_daily(
        self,
        ts_code: str = "000001.SH",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ):
        """Get index daily data through the configured daily provider."""
        return self.market_data.get_index_daily(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            period=period,
        )

    def get_trade_calendar(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ):
        """Get normalized trade calendar."""
        return self.market_data.get_trade_calendar(start_date=start_date, end_date=end_date)

    def get_latest_trade_date(self) -> str:
        """Get latest trade date through the configured calendar provider."""
        return self.market_data.get_latest_trade_date()

    def get_minute_data(
        self,
        ts_code,
        interval: str = "5m",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        count: Optional[int] = None,
    ):
        """Get minute-level data through the configured minute provider."""
        return self.market_data.get_minute_data(
            ts_code=ts_code,
            interval=interval,
            start_date=start_date,
            end_date=end_date,
            count=count,
        )

    def _get_latest_report_period(self) -> str:
        """Preserve the legacy helper name for compatibility."""
        return self.market_data.get_latest_report_period()

    def _get_latest_trade_date(self) -> str:
        """Preserve the legacy helper name for compatibility."""
        return self.get_latest_trade_date()

    @staticmethod
    def _build_runtime_config(
        tushare_token: Optional[str],
        use_cache: bool,
        cache_dir: str,
        config: Optional[Dict],
    ) -> Dict:
        runtime = deepcopy(config or {})
        data_source = runtime.setdefault("data_source", {})
        data = runtime.setdefault("data", {})

        if tushare_token is not None:
            data_source["tushare_token"] = tushare_token
        else:
            data_source.setdefault("tushare_token", "")

        # Constructor arguments should win over file config so ad-hoc validation
        # can explicitly disable cache or redirect cache output.
        data_source["use_cache"] = use_cache
        data_source["cache_dir"] = cache_dir or data_source.get("cache_dir") or "./cache"

        data.setdefault("provider", "akshare")
        data.setdefault("minute_provider", "pytdx")
        data.setdefault("fallback_provider", "")
        data.setdefault("minute_fallback_provider", "")
        data["cache_dir"] = data_source["cache_dir"]
        data.setdefault("daily_cache_hours", 24)
        data.setdefault("minute_cache_hours", 6)
        data.setdefault("fundamentals_cache_hours", 168)
        data.setdefault("disable_system_proxy", True)
        data.setdefault("pytdx_host", "180.153.18.170")
        data.setdefault("pytdx_port", 7709)
        data.setdefault(
            "pytdx_hosts",
            [
                "180.153.18.170:7709",
                "119.147.212.81:7709",
                "114.80.63.12:7709",
            ],
        )

        return runtime
