import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
SIGNAL_SYSTEM_ROOT = ROOT / "signal_system"
for candidate in (str(ROOT), str(SIGNAL_SYSTEM_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from data.data_fetcher import DataFetcher
from data.market_data_service import MarketDataService


class StubDailyProvider:
    def __init__(self):
        self.calls = []

    def get_stock_list(self, exchange="", list_status="L"):
        self.calls.append(("get_stock_list", exchange, list_status))
        return pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行"}])

    def get_daily_data(self, ts_code, start_date=None, end_date=None, period=250):
        self.calls.append(("get_daily_data", ts_code, start_date, end_date, period))
        return pd.DataFrame(
            [{"trade_date": "20260306", "datetime": pd.Timestamp("2026-03-06"), "close": 12.3}]
        )

    def get_index_daily(self, ts_code="000001.SH", start_date=None, end_date=None, period=250):
        self.calls.append(("get_index_daily", ts_code, start_date, end_date, period))
        return pd.DataFrame([{"trade_date": "20260306", "close": 3300.0}])

    def get_daily_basic(self, ts_code, trade_date=None):
        self.calls.append(("get_daily_basic", ts_code, trade_date))
        return {"turnover_rate": 2.1, "pe": 8.5}

    def get_financial_data(self, ts_code, period=None):
        self.calls.append(("get_financial_data", ts_code, period))
        return {"roe": 12.5, "debt_to_assets": 41.0}

    def get_trade_calendar(self, start_date=None, end_date=None):
        self.calls.append(("get_trade_calendar", start_date, end_date))
        return pd.DataFrame([{"trade_date": "20260305"}, {"trade_date": "20260306"}])

    def get_latest_trade_date(self):
        self.calls.append(("get_latest_trade_date",))
        return "20260306"


class StubMinuteProvider:
    def __init__(self):
        self.calls = []

    def get_minute_data(self, ts_code, interval="5m", start_date=None, end_date=None, count=None):
        self.calls.append(("get_minute_data", ts_code, interval, start_date, end_date, count))
        return pd.DataFrame([{"datetime": pd.Timestamp("2026-03-06 10:00:00"), "close": 12.4}])


class MarketDataServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = {
            "data_source": {"use_cache": True, "cache_dir": self.temp_dir.name},
            "data": {
                "provider": "akshare",
                "minute_provider": "pytdx",
                "cache_dir": self.temp_dir.name,
                "daily_cache_hours": 24,
                "minute_cache_hours": 6,
                "fundamentals_cache_hours": 168,
            },
        }
        self.daily_provider = StubDailyProvider()
        self.minute_provider = StubMinuteProvider()
        self.service = MarketDataService(
            config=self.config,
            providers={"akshare": self.daily_provider, "pytdx": self.minute_provider},
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_routes_daily_requests_to_daily_provider(self):
        stock_list = self.service.get_stock_list()
        financial = self.service.get_financial_data("000001.SZ")
        basic = self.service.get_daily_basic("000001.SZ")

        self.assertEqual(stock_list.iloc[0]["ts_code"], "000001.SZ")
        self.assertEqual(financial["roe"], 12.5)
        self.assertEqual(basic["pe"], 8.5)
        self.assertEqual(self.daily_provider.calls[0][0], "get_stock_list")

    def test_routes_minute_requests_to_minute_provider(self):
        minute_df = self.service.get_minute_data("000001.SZ", interval="5m", count=20)

        self.assertEqual(len(minute_df), 1)
        self.assertEqual(self.minute_provider.calls[0][0], "get_minute_data")
        self.assertEqual(self.minute_provider.calls[0][2], "5m")

    def test_latest_trade_date_comes_from_trade_calendar(self):
        latest_trade_date = self.service.get_latest_trade_date()
        self.assertEqual(latest_trade_date, "20260306")

    def test_data_fetcher_remains_compatible_facade(self):
        fetcher = DataFetcher(
            config=self.config,
            providers={"akshare": self.daily_provider, "pytdx": self.minute_provider},
        )

        stock_list = fetcher.get_stock_list()
        minute_df = fetcher.get_minute_data("000001.SZ", count=10)

        self.assertEqual(stock_list.iloc[0]["name"], "平安银行")
        self.assertEqual(len(minute_df), 1)


if __name__ == "__main__":
    unittest.main()
