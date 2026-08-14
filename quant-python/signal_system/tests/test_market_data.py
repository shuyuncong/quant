import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from data.market_data import MarketDataClient, resample_session_bars, standardize_bars


class MarketDataTests(unittest.TestCase):
    def _minute_frame(self):
        stamps = list(pd.date_range("2025-01-02 09:31", "2025-01-02 11:30", freq="min"))
        stamps += list(pd.date_range("2025-01-02 13:01", "2025-01-02 15:00", freq="min"))
        rows = []
        for index, stamp in enumerate(stamps):
            price = 10 + index / 1000
            rows.append(
                {
                    "datetime": stamp,
                    "open": price,
                    "high": price + 0.1,
                    "low": price - 0.1,
                    "close": price + 0.02,
                    "volume": 10,
                    "amount": 10000,
                    "is_closed": True,
                }
            )
        return pd.DataFrame(rows)

    def test_120_minute_bars_do_not_cross_lunch(self):
        frame = self._minute_frame()
        result = resample_session_bars(frame, 120, source_minutes=1)
        self.assertEqual(2, len(result))
        self.assertEqual("11:30:00", result["datetime"].iloc[0].time().isoformat())
        self.assertEqual("15:00:00", result["datetime"].iloc[1].time().isoformat())
        self.assertEqual(1200, result["volume"].iloc[0])
        self.assertEqual(frame["open"].iloc[0], result["open"].iloc[0])
        self.assertEqual(frame["close"].iloc[119], result["close"].iloc[0])

    def test_60_to_120_resample(self):
        one_minute = self._minute_frame()
        sixty = resample_session_bars(one_minute, 60, source_minutes=1)
        result = resample_session_bars(sixty, 120, source_minutes=60)
        self.assertEqual(4, len(sixty))
        self.assertEqual(2, len(result))
        self.assertEqual(sixty["volume"].iloc[:2].sum(), result["volume"].iloc[0])

    def test_unclosed_daily_bar_is_filtered_by_caller_contract(self):
        raw = pd.DataFrame(
            [{"日期": "2025-01-02", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10.5, "成交量": 1}]
        )
        before_close = standardize_bars(
            raw,
            "1d",
            "fixture",
            "qfq",
            now=pd.Timestamp("2025-01-02 14:00").to_pydatetime(),
        )
        self.assertFalse(bool(before_close["is_closed"].iloc[0]))

    def test_stock_list_filters_recent_listings_when_date_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient(
                {
                    "market_data": {
                        "cache_dir": directory,
                        "min_listing_trade_days": 120,
                    }
                }
            )
            trade_dates = set(pd.bdate_range("2025-01-02", periods=150).date)
            expected = max(trade_dates)
            cutoff = sorted(trade_dates)[-120]
            client._throttle = lambda: None
            client._latest_expected_trade_date = lambda: expected
            client.get_trade_dates = lambda: trade_dates
            client._http_json = lambda *args, **kwargs: {
                "data": {
                    "diff": [
                        {
                            "f12": "000001",
                            "f14": "成熟股票",
                            "f26": int((cutoff - pd.Timedelta(days=1)).strftime("%Y%m%d")),
                        },
                        {
                            "f12": "000002",
                            "f14": "新股",
                            "f26": int((cutoff + pd.Timedelta(days=1)).strftime("%Y%m%d")),
                        },
                    ]
                }
            }
            result = client.get_stock_list()
            self.assertEqual(["000001"], result["code"].tolist())

    def test_suspended_zero_snapshot_is_not_written_as_fresh_daily_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            history = pd.DataFrame(
                [
                    {
                        "datetime": pd.Timestamp("2025-01-01"),
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.8,
                        "close": 10.2,
                        "volume": 100.0,
                        "amount": 1000.0,
                        "is_closed": True,
                    }
                ]
            )
            client.save_daily_history("000001", history)
            client._throttle = lambda: None
            client._latest_expected_trade_date = lambda: date(2025, 1, 2)
            client._http_json = MagicMock(side_effect=RuntimeError("eastmoney unavailable"))
            client._fetch_sina_stock_snapshot = lambda: [
                {
                    "symbol": "sz000001",
                    "code": "000001",
                    "name": "停牌股票",
                    "open": "0.000",
                    "high": "0.000",
                    "low": "0.000",
                    "trade": "0.000",
                    "volume": 0,
                    "amount": 0,
                }
            ]
            with patch(
                "data.market_data.now_shanghai",
                return_value=datetime(2025, 1, 2, 15, 30),
            ):
                updated = client.refresh_daily_histories_from_snapshot()
            self.assertEqual(0, updated)
            saved = client.load_daily_history("000001")
            self.assertEqual([date(2025, 1, 1)], pd.to_datetime(saved["datetime"]).dt.date.tolist())

    def test_tushare_accepts_explicit_none_adjustment(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient(
                {
                    "market_data": {
                        "provider": "tushare",
                        "adjust": "none",
                        "cache_dir": directory,
                        "tushare_token": "test-token",
                    }
                }
            )
            pro = MagicMock()
            pro.daily.return_value = pd.DataFrame(
                [
                    {
                        "trade_date": "20250102",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.8,
                        "close": 10.2,
                        "vol": 100.0,
                        "amount": 1000.0,
                    }
                ]
            )
            client._get_tushare = lambda: pro
            client._throttle = lambda: None
            frame = client._fetch_tushare_daily("000001", 300)
            self.assertEqual("none", frame.attrs["adjust"])
            self.assertEqual(1, len(frame))

    def test_daily_history_validation_rejects_structural_damage_and_staleness(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            damaged = pd.DataFrame(
                {
                    "datetime": pd.bdate_range(end="2025-01-02", periods=120),
                    "is_closed": True,
                }
            )
            client.save_daily_history("000001", damaged)
            self.assertFalse(
                client.daily_history_is_usable("000001", date(2025, 1, 2), min_bars=120)
            )

            valid = damaged.assign(
                open=10.0,
                high=10.5,
                low=9.5,
                close=10.2,
                volume=100.0,
                amount=1000.0,
            )
            client.save_daily_history("000001", valid)
            self.assertTrue(
                client.daily_history_is_usable("000001", date(2025, 1, 2), min_bars=120)
            )
            self.assertFalse(
                client.daily_history_is_usable("000001", date(2025, 1, 3), min_bars=120)
            )


if __name__ == "__main__":
    unittest.main()
