import os
import sys
import tempfile
import time
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

    def _daily_frame(self, periods: int = 40, source: str = "stub") -> pd.DataFrame:
        frame = pd.DataFrame(
            {
                "datetime": pd.bdate_range("2025-01-02", periods=periods),
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.2,
                "volume": 1000.0,
                "amount": 100000.0,
                "is_closed": True,
            }
        )
        frame.attrs.update({"source": source, "timeframe": "1d", "adjust": "none"})
        return frame

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

    def test_display_name_map_includes_etfs_and_dedupes_blank_names(self):
        """展示名表 = 股票 + ETF；空名剔除、同码以股票优先。

        ETF 单独取数（`_fetch_eastmoney_etf_list`），**不**并进 `get_stock_list()` ——
        后者是可交易股票 universe，喂给 scan；混入基金会让策略在基金上选股。
        """
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None
            client._cached = lambda *args, **kwargs: None
            client._save_cache = lambda *args, **kwargs: None
            client.get_stock_list = lambda: pd.DataFrame(
                [{"code": "600750", "name": "华润江中"}, {"code": "300006", "name": "莱美药业"}]
            )
            client._fetch_eastmoney_etf_list = lambda: pd.DataFrame(
                [
                    {"code": "517520", "name": "黄金ETF"},
                    {"code": "588000", "name": "科创50ETF"},
                    {"code": "600750", "name": "不应覆盖股票名"},
                    {"code": "999999", "name": ""},
                ]
            )
            result = client.get_display_name_map()
            names = dict(zip(result["code"], result["name"]))
            self.assertEqual("黄金ETF", names["517520"])
            self.assertEqual("科创50ETF", names["588000"])
            self.assertEqual("华润江中", names["600750"])  # 股票优先，ETF 表不得覆盖
            self.assertNotIn("999999", names)  # 空名剔除
            # 2 只股票 + 2 只 ETF = 4（重复的 600750 被去重、空名的 999999 被剔除）
            self.assertEqual(4, len(result))

    def test_display_name_map_degrades_to_stocks_when_etf_fetch_fails(self):
        """ETF 取数失败只降级名字展示，绝不打断调用方（否则报告生成会因展示数据失败）。

        且降级结果必须写**单独的短 TTL 缓存键** —— 与正常结果共用 24h 缓存会让
        一次网络抖动钉住一整天、网络恢复也不自愈（本项目踩过同类故障）。
        """
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None
            client._cached = lambda *args, **kwargs: None
            saved = {}
            client._save_cache = lambda key, frame: saved.update({key: frame})
            client.get_stock_list = lambda: pd.DataFrame([{"code": "600750", "name": "华润江中"}])

            def boom():
                raise RuntimeError("eastmoney unavailable")

            client._fetch_eastmoney_etf_list = boom
            result = client.get_display_name_map()
            self.assertEqual(["600750"], result["code"].tolist())
            keys = list(saved)
            self.assertEqual(1, len(keys))
            self.assertTrue(keys[0].endswith("_degraded"), f"降级应写独立缓存键，实际 {keys[0]}")
            self.assertNotEqual("display_names_v1_120_1_1", keys[0], "降级不得占用正常缓存键")

    def test_display_name_map_degrades_for_non_eastmoney_provider(self):
        """akshare/tushare provider 下不含 ETF 是既定行为，不产生降级缓存键。"""
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient(
                {"market_data": {"cache_dir": directory, "provider": "akshare"}}
            )
            client._throttle = lambda: None
            client._cached = lambda *args, **kwargs: None
            saved = {}
            client._save_cache = lambda key, frame: saved.update({key: frame})
            client.get_stock_list = lambda: pd.DataFrame([{"code": "600750", "name": "华润江中"}])
            client._fetch_eastmoney_etf_list = lambda: (_ for _ in ()).throw(
                AssertionError("非东财 provider 不该去抓 ETF")
            )
            result = client.get_display_name_map()
            self.assertEqual(["600750"], result["code"].tolist())
            self.assertEqual(1, len(saved))
            self.assertFalse(list(saved)[0].endswith("_degraded"))

    def test_display_name_map_retries_after_degraded_cache_expires(self):
        """降级缓存的 TTL 必须短于正常缓存 —— 用真实 _cached 验证窗口。"""
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            normal = f"display_names_v1_{client.min_listing_trade_days}_{int(client.exclude_st)}_{int(client.exclude_delisting)}"
            client._save_cache(normal, pd.DataFrame([{"code": "600750", "name": "华润江中"}]))
            client._save_cache(f"{normal}_degraded", pd.DataFrame([{"code": "600750", "name": "华润江中"}]))
            # 用 mtime 制造"45 分钟前"——已超出降级 TTL(10min)，远小于正常 TTL(24h)
            old = time.time() - 45 * 60
            os.utime(client._cache_path(f"{normal}_degraded"), (old, old))
            os.utime(client._cache_path(normal), (old, old))
            self.assertIsNone(client._cached(f"{normal}_degraded", 10 * 60))
            self.assertIsNotNone(client._cached(normal, 24 * 3600))

    def test_etf_list_paginates_until_total(self):
        """东财 clist 的 pz 硬上限是 100/页，必须按 total 翻页取满。"""
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None
            pages = {}

            def fake_http(url, params):
                page = params["pn"]
                pages[page] = pages.get(page, 0) + 1
                start = (page - 1) * 100
                batch = [
                    {"f12": f"51{i:04d}", "f14": f"ETF{i}"} for i in range(start, min(start + 100, 250))
                ]
                return {"data": {"total": 250, "diff": batch}}

            client._http_json = fake_http
            result = client._fetch_eastmoney_etf_list()
            self.assertEqual(250, len(result))
            self.assertEqual([1, 2, 3], sorted(pages), "应恰好翻 3 页（250 条 / 100 每页）")

    def test_etf_list_rejects_truncated_pages(self):
        """服务端报 total 却只给一页时必须抛错 —— 否则残缺名单会被写进 24h 缓存。"""
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None
            client._http_json = lambda *args, **kwargs: {
                "data": {"total": 1200, "diff": [{"f12": "517520", "f14": "黄金ETF"}]}
            }
            with self.assertRaises(RuntimeError):
                client._fetch_eastmoney_etf_list()

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

    def test_bars_degrade_through_backup_sources_in_order(self):
        """腾讯 K 线故障时按 东方财富 → 新浪 顺序接管，单源故障不再中断扫描。"""
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient(
                {"market_data": {"cache_dir": directory, "provider": "auto"}}
            )
            client._throttle = lambda: None
            calls = []

            def unavailable(name):
                def fetch(*_args, **_kwargs):
                    calls.append(name)
                    raise ConnectionError(f"{name} down")

                return fetch

            client._fetch_tencent = unavailable("tencent")
            client._fetch_eastmoney = unavailable("eastmoney")
            client._fetch_sina = lambda *_args, **_kwargs: self._daily_frame(source="sina")

            frame = client.get_bars("000001.SZ", "1d", limit=30)

            self.assertEqual(["tencent", "eastmoney"], calls)
            self.assertEqual("sina", frame.attrs["source"])
            self.assertEqual(30, len(frame))

    def test_bars_raise_when_every_source_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None
            client._fetch_tencent = MagicMock(side_effect=ConnectionError("tencent down"))
            client._fetch_eastmoney = MagicMock(side_effect=ConnectionError("eastmoney down"))
            client._fetch_sina = MagicMock(side_effect=ConnectionError("sina down"))

            with self.assertRaises(RuntimeError) as raised:
                client.get_bars("000001.SZ", "1d", limit=30)

            message = str(raised.exception)
            for source in ("tencent", "eastmoney", "sina"):
                self.assertIn(source, message)

    def test_bars_return_empty_frame_when_all_sources_report_no_data(self):
        """全源无数据（新股、退市等）不算故障：保持空 DataFrame 语义而不抛异常。"""
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None
            client._fetch_tencent = MagicMock(return_value=self._daily_frame(periods=0))
            client._fetch_eastmoney = MagicMock(return_value=self._daily_frame(periods=0))
            client._fetch_sina = MagicMock(return_value=self._daily_frame(periods=0))

            frame = client.get_bars("000001.SZ", "1d", limit=30)

            self.assertTrue(frame.empty)
            self.assertEqual(1, client._fetch_tencent.call_count)

    def test_sina_source_normalizes_share_volume_to_lots(self):
        """新浪成交量单位是股，腾讯/东财是手：降级到新浪时必须折算，否则量能失真 100 倍。"""
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            client._throttle = lambda: None
            ak = MagicMock()
            ak.stock_zh_a_daily.return_value = pd.DataFrame(
                {
                    "date": pd.bdate_range("2025-01-02", periods=3),
                    "open": [10.0, 10.1, 10.2],
                    "high": [10.5, 10.6, 10.7],
                    "low": [9.5, 9.6, 9.7],
                    "close": [10.2, 10.3, 10.4],
                    "volume": [5_000_000.0, 6_000_000.0, 7_000_000.0],
                    "amount": [51_000_000.0, 61_800_000.0, 72_800_000.0],
                }
            )
            client._get_akshare = lambda: ak

            frame = client._fetch_sina("600036.SH", "1d", limit=3)

            self.assertEqual("sina", frame.attrs["source"])
            self.assertEqual([50_000.0, 60_000.0, 70_000.0], list(frame["volume"]))
            self.assertEqual("sh600036", ak.stock_zh_a_daily.call_args.kwargs["symbol"])

    def test_bar_fallback_providers_config_replaces_default_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient(
                {
                    "market_data": {
                        "cache_dir": directory,
                        "provider": "auto",
                        "fallback_providers": ["akshare"],
                    }
                }
            )
            client._throttle = lambda: None
            client._fetch_tencent = MagicMock(side_effect=ConnectionError("tencent down"))
            client._fetch_eastmoney = MagicMock(return_value=self._daily_frame(source="eastmoney"))
            client._fetch_akshare = MagicMock(return_value=self._daily_frame(source="akshare"))

            frame = client.get_bars("000001.SZ", "1d", limit=30)

            self.assertEqual("akshare", frame.attrs["source"])
            self.assertEqual(0, client._fetch_eastmoney.call_count)

    def test_trade_calendar_falls_back_to_sina_when_tencent_unavailable(self):
        """腾讯日历源故障时新浪日历接管，避免退化成工作日近似。"""
        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient(
                {"market_data": {"cache_dir": directory, "provider": "auto"}}
            )
            client._throttle = lambda: None
            client._http_json = MagicMock(side_effect=ConnectionError("tencent down"))
            ak = MagicMock()
            ak.tool_trade_date_hist_sina.return_value = pd.DataFrame(
                {"trade_date": pd.to_datetime(["2026-09-16", "2026-09-17"])}
            )
            client._get_akshare = lambda: ak

            dates = client.get_trade_dates()

            self.assertIn(date(2026, 9, 16), dates)
            self.assertEqual(1, ak.tool_trade_date_hist_sina.call_count)

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

        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            with patch.object(client, "_http_json_once", new=fake_once):
                with patch("data.market_data.time.sleep"):
                    result = client._http_json(https_url, {})

        self.assertEqual(payload, result)
        # HTTPS 重试两次失败后降级 HTTP 并成功
        self.assertEqual([https_url, https_url, http_url], calls)

    def test_http_json_does_not_retry_non_push2his_endpoints(self):
        payload = {"ok": True}
        calls = []

        def fake_once(url: str, params):
            calls.append(url)
            return payload

        with tempfile.TemporaryDirectory() as directory:
            client = MarketDataClient({"market_data": {"cache_dir": directory}})
            with patch.object(client, "_http_json_once", new=fake_once):
                result = client._http_json(
                    "https://vip.stock.finance.sina.com.cn/api", {}
                )

        self.assertEqual(payload, result)
        self.assertEqual(1, len(calls))

    def test_disable_system_proxy_builds_direct_opener(self):
        """系统代理会把腾讯/东财行情改写成 501/断连；配置为 true 时必须直连。"""
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:9",
                    "HTTPS_PROXY": "http://127.0.0.1:9",
                    "http_proxy": "http://127.0.0.1:9",
                    "https_proxy": "http://127.0.0.1:9",
                },
            ):
                client = MarketDataClient(
                    {
                        "market_data": {"cache_dir": directory},
                        "data": {"disable_system_proxy": True},
                    }
                )
        proxy_handlers = [
            handler
            for handler in client._http_opener.handlers
            if handler.__class__.__name__ == "ProxyHandler"
        ]
        # ProxyHandler({}) 不注册任何 http_request 方法，因此直连 opener
        # 里不会出现 ProxyHandler；只要没有它，环境变量代理就不会生效。
        self.assertEqual([], proxy_handlers)

    def test_market_data_proxy_env_builds_proxy_opener(self):
        """海外服务器可只给行情链路指定代理，不影响模型/Web 的全局代理。"""
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {"MARKET_DATA_PROXY": "http://proxy.example:7890"},
            ):
                client = MarketDataClient(
                    {
                        "market_data": {"cache_dir": directory},
                        "data": {"disable_system_proxy": True},
                    }
                )
        proxy_handlers = [
            handler
            for handler in client._http_opener.handlers
            if handler.__class__.__name__ == "ProxyHandler"
        ]
        self.assertEqual(1, len(proxy_handlers))
        self.assertEqual(
            {
                "http": "http://proxy.example:7890",
                "https": "http://proxy.example:7890",
            },
            proxy_handlers[0].proxies,
        )


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

        clock = patch("data.market_data.now_shanghai", new=fake_now)
        clock.start()
        self.addCleanup(clock.stop)
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
            self._set_today("2026-09-17")
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


class StrictCalendarTests(unittest.TestCase):
    _client = TradeCalendarTodayTests._client
    _set_today = TradeCalendarTodayTests._set_today

    def test_strict_calendar_does_not_invent_a_holiday_session(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(directory)
            self._set_today("2026-10-05")
            frame = pd.DataFrame({"trade_date": pd.to_datetime(["2026-09-30", "2026-10-09"])})
            client._save_cache("trade_calendar", frame)
            self.assertNotIn(date(2026, 10, 5), client.get_trade_dates(strict=True))

    def test_strict_calendar_refreshes_stale_history_without_weekday_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(directory)
            self._set_today("2026-10-05")
            frame = pd.DataFrame({"trade_date": pd.to_datetime(["2026-09-30"])})
            client._save_cache("trade_calendar", frame)
            with patch.object(client, "_fetch_trade_calendar", return_value=frame):
                self.assertNotIn(date(2026, 10, 5), client.get_trade_dates(strict=True))


if __name__ == "__main__":
    unittest.main()
