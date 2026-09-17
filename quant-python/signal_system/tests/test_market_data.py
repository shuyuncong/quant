import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, call, patch

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

    def test_stock_pool_history_parses_turnover_and_causal_market_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient(
                {
                    "market_data": {
                        "cache_dir": directory,
                        "volume_unit_shares": 100,
                    }
                }
            )
            client._throttle = lambda: None
            client._http_json = lambda *args, **kwargs: {
                "data": {
                    "klines": [
                        "2025-01-02,9.80,10.00,10.10,9.70,200000,200000000,4.00,2.00,0.20,2.00"
                    ]
                }
            }
            frame = client.get_stock_pool_history(
                "000001",
                limit=300,
                end=date(2025, 1, 2),
            )
            self.assertEqual(2.0, frame.iloc[0]["turnover_rate"])
            self.assertEqual(100.0, frame.iloc[0]["circulating_market_cap"])
            self.assertEqual(200_000_000.0, frame.iloc[0]["amount"])
            self.assertEqual(date(2025, 1, 2), frame.iloc[0]["datetime"].date())

    def test_stock_pool_history_falls_back_to_akshare_sina(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None
            client._http_json = MagicMock(side_effect=RuntimeError("eastmoney unavailable"))
            ak = MagicMock()
            ak.stock_zh_a_daily.return_value = pd.DataFrame(
                [
                    {
                        "date": date(2025, 1, 2),
                        "close": 10.0,
                        "volume": 20_000_000.0,
                        "amount": 200_000_000.0,
                        "outstanding_share": 1_000_000_000.0,
                        "turnover": 0.02,
                    }
                ]
            )
            client._get_akshare = lambda: ak
            frame = client.get_stock_pool_history(
                "000001",
                limit=300,
                end=date(2025, 1, 2),
            )
            self.assertEqual(2.0, frame.iloc[0]["turnover_rate"])
            self.assertEqual(100.0, frame.iloc[0]["circulating_market_cap"])

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

    def test_multi_timeframe_prefers_direct_target_periods(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})

            def bars(_symbol, timeframe, limit=300):
                size = limit
                frame = self._minute_frame().iloc[: min(size, 240)].copy()
                if size > len(frame):
                    frame = pd.concat([frame] * ((size // len(frame)) + 1), ignore_index=True).iloc[:size]
                return frame.reset_index(drop=True)

            client.get_bars = MagicMock(side_effect=bars)
            result, errors = client.get_multi_timeframe_bars(
                "000001.SZ", ["1m", "5m", "15m"], limit=300
            )
            self.assertEqual({}, errors)
            self.assertEqual(300, len(result["5m"]))
            self.assertEqual(300, len(result["15m"]))
            self.assertTrue(result["15m"].attrs["history_complete"])
            self.assertEqual("direct", result["15m"].attrs["source_mode"])
            requested = [call.args[1] for call in client.get_bars.call_args_list]
            self.assertEqual(["1m", "5m", "15m"], requested)

    def test_multi_timeframe_marks_partial_direct_history_with_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client.get_bars = MagicMock(return_value=self._minute_frame().iloc[:120].copy())

            result, errors = client.get_multi_timeframe_bars(
                "000001.SZ", ["5m"], limit=300
            )

            self.assertEqual({}, errors)
            self.assertFalse(result["5m"].attrs["history_complete"])
            self.assertEqual(
                "行情历史不足: 仅获取 120/300 根",
                result["5m"].attrs["source_warning"],
            )

    def test_multi_timeframe_resamples_one_minute_only_as_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            minute = pd.concat([self._minute_frame()] * 3, ignore_index=True)

            def bars(_symbol, timeframe, limit=300):
                if timeframe == "5m":
                    raise RuntimeError("5m provider unavailable")
                if timeframe == "1m":
                    return minute.tail(limit).reset_index(drop=True)
                raise AssertionError(timeframe)

            client.get_bars = MagicMock(side_effect=bars)
            result, errors = client.get_multi_timeframe_bars("000001.SZ", ["5m"], limit=300)
            self.assertNotIn("5m", errors)
            self.assertGreaterEqual(len(result["5m"]), 40)
            self.assertFalse(result["5m"].attrs["history_complete"])
            self.assertEqual("resampled_1m_fallback", result["5m"].attrs["source_mode"])

    def test_fallback_refetches_longer_one_minute_history_after_short_initial_load(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            sessions = [
                self._minute_frame().assign(
                    datetime=lambda frame, offset=offset: frame["datetime"] + pd.Timedelta(days=offset)
                )
                for offset in range(5)
            ]
            minute = pd.concat(sessions, ignore_index=True)

            def bars(_symbol, timeframe, limit=300):
                if timeframe == "15m":
                    raise RuntimeError("15m provider unavailable")
                if timeframe == "1m":
                    return minute.tail(limit).reset_index(drop=True)
                raise AssertionError(timeframe)

            client.get_bars = MagicMock(side_effect=bars)
            result, errors = client.get_multi_timeframe_bars(
                "000001.SZ", ["1m", "15m"], limit=300
            )

            self.assertEqual({}, errors)
            self.assertGreaterEqual(len(result["15m"]), 40)
            self.assertEqual(["1m", "15m", "1m"], [call.args[1] for call in client.get_bars.call_args_list])
            self.assertEqual([300, 300, 600], [call.kwargs["limit"] for call in client.get_bars.call_args_list])

    def test_index_bars_falls_back_to_tencent_when_eastmoney_unavailable(self):
        """东财 push2his 端点故障时（Remote end closed），腾讯 ifzq 备用源接管，
        市场闸门不回退到 regime=unknown。"""
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None

            def broken(_url, *_args, **_kwargs):
                raise ConnectionError("Remote end closed connection without response")

            client._http_json = broken
            tencent = pd.DataFrame(
                {
                    "datetime": pd.bdate_range("2025-01-02", periods=30),
                    "open": [10.0] * 30,
                    "high": [10.5] * 30,
                    "low": [9.5] * 30,
                    "close": [10.2] * 30,
                    "volume": [1000.0] * 30,
                    "amount": [100000.0] * 30,
                    "is_closed": [True] * 30,
                }
            )
            client._fetch_index_bars_tencent = lambda *_args, **_kwargs: tencent

            frame = client.get_index_bars("000001.SH", limit=300)

            self.assertEqual(30, len(frame))
            self.assertEqual(date(2025, 1, 2), frame["datetime"].iloc[0].date())
            self.assertEqual(10.2, frame["close"].iloc[-1])

    def test_index_bars_raises_when_all_sources_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None
            client._http_json = MagicMock(side_effect=RuntimeError("no eastmoney"))
            client._fetch_index_bars_tencent = lambda *_args, **_kwargs: None
            with self.assertRaises(RuntimeError):
                client.get_index_bars("000001.SH")

    def test_http_json_falls_back_from_https_to_http_on_push2his(self):
        """东财 push2his 间歇性故障（HTTPS 502/断连而 HTTP 可用）时，
        _http_json 自动降级 HTTP 重试，不再把失败抛给降级链。"""
        https_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        http_url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        payload = {"data": {"klines": ["2026-09-14,1,2,3,4,5,6,7,8,9,10"]}}
        calls = []

        def fake_once(url: str, params):
            calls.append(url)
            if url.startswith("https://"):
                raise ConnectionError("Remote end closed connection without response")
            return payload

        with patch.object(MarketDataClient, "_http_json_once", new=fake_once):
            with patch("data.market_data.time.sleep"):
                result = MarketDataClient._http_json(https_url, {})

        self.assertEqual(payload, result)
        # HTTPS 重试两次失败后降级 HTTP 并成功
        self.assertEqual([https_url, https_url, http_url], calls)

    def test_http_json_does_not_retry_non_push2his_endpoints(self):
        payload = {"ok": True}
        calls = []

        def fake_once(url: str, params):
            calls.append(url)
            return payload

        with patch.object(MarketDataClient, "_http_json_once", new=fake_once):
            result = MarketDataClient._http_json(
                "https://vip.stock.finance.sina.com.cn/api", {}
            )

        self.assertEqual(payload, result)
        self.assertEqual(1, len(calls))


class TradeCalendarTodayTests(unittest.TestCase):
    """get_trade_dates 的当日兜底：腾讯日 K 当日 bar 收盘前不存在，
    凌晨回源缺当日会导致整天 is_trading_day=false（2026-09 线上故障根因）。"""

    def _client(self, cache_dir):
        return MarketDataClient(
            {"market_data": {"cache_dir": cache_dir, "provider": "auto"}}
        )

    def _set_today(self, iso):
        today = date.fromisoformat(iso)

        def fake_now():
            class X:
                @staticmethod
                def date():
                    return today

            return X()

        with patch("data.market_data.now_shanghai", new=fake_now):
            return today

    def test_weekday_missing_today_is_added(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(directory)
            today = self._set_today("2026-09-17")  # 周四
            dates = {date(2026, 9, 15), date(2026, 9, 16)}  # 昨日收盘数据
            result = client._with_today_if_stale(dates)
            self.assertIn(today, result)
            self.assertEqual(result, {date(2026, 9, 15), date(2026, 9, 16), today})

    def test_already_contains_today_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(directory)
            today = self._set_today("2026-09-17")
            dates = {date(2026, 9, 16), today}
            result = client._with_today_if_stale(dates)
            self.assertEqual(result, dates)

    def test_weekend_not_added(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(directory)
            today = self._set_today("2026-09-19")  # 周六
            dates = {date(2026, 9, 17), date(2026, 9, 18)}
            result = client._with_today_if_stale(dates)
            self.assertNotIn(today, result)

    def test_future_dated_calendar_unchanged(self):
        # akshare 全表含未来交易日：不应被兜底改动
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(directory)
            today = self._set_today("2026-09-17")
            dates = {date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)}
            result = client._with_today_if_stale(dates)
            self.assertEqual(result, dates)

    def test_empty_dates_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(directory)
            self._set_today("2026-09-17")
            self.assertEqual(client._with_today_if_stale(set()), set())

    def test_cache_path_missing_today_also_gets_today(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(directory)
            self._set_today("2026-09-17")
            frame = pd.DataFrame({"trade_date": pd.to_datetime(["2026-09-15", "2026-09-16"])})
            client._save_cache("trade_calendar", frame)
            result = client.get_trade_dates()
            self.assertIn(date(2026, 9, 17), result)


if __name__ == "__main__":
    unittest.main()
