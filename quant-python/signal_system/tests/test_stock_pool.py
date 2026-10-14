from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from strategy.stock_pool import evaluate_stock_pool, filter_buy_events  # noqa: E402
from monitor.service import SignalMonitor  # noqa: E402


def pool_config(**overrides):
    values = {
        "enabled": True,
        "min_market_cap": 50,
        "max_market_cap": 3000,
        "amount_window": 20,
        "min_avg_amount": 1,
        "turnover_window": 20,
        "min_avg_turnover_rate": 0.5,
        "max_avg_turnover_rate": 8,
        "min_listing_trade_days": 120,
        "exclude_st": True,
        "exclude_delisting": True,
        "missing_data_policy": "reject",
        "volume_unit_shares": 100,
    }
    values.update(overrides)
    return {"stock_pool": values}


def history(periods=140, *, market_cap=100.0, amount=2.0, turnover=2.0):
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2025-01-02", periods=periods),
            "close": [10.0] * periods,
            "volume": [200_000.0] * periods,
            "amount": [amount * 100_000_000] * periods,
            "turnover_rate": [turnover] * periods,
            "circulating_market_cap": [market_cap] * periods,
            "is_closed": [True] * periods,
        }
    )


class StockPoolTests(unittest.TestCase):
    def test_accepts_candidate_at_inclusive_thresholds(self):
        frame = history(market_cap=50, amount=1, turnover=0.5)
        result = evaluate_stock_pool(
            frame,
            frame.iloc[-1]["datetime"].date(),
            pool_config(),
            name="样本股份",
        )
        self.assertTrue(result["passed"])
        self.assertEqual([], result["reasons"])
        self.assertEqual(50.0, result["metrics"]["market_cap"])
        self.assertEqual(1.0, result["metrics"]["avg_amount"])
        self.assertEqual(0.5, result["metrics"]["avg_turnover_rate"])

    def test_rejects_each_out_of_range_metric(self):
        cases = (
            (history(market_cap=49), "stock_pool_market_cap_below_min"),
            (history(market_cap=3001), "stock_pool_market_cap_above_max"),
            (history(amount=0.9), "stock_pool_avg_amount_below_min"),
            (history(turnover=0.4), "stock_pool_turnover_below_min"),
            (history(turnover=8.1), "stock_pool_turnover_above_max"),
            (history(periods=119), "stock_pool_listing_days_below_min"),
        )
        for frame, reason in cases:
            with self.subTest(reason=reason):
                result = evaluate_stock_pool(
                    frame,
                    frame.iloc[-1]["datetime"].date(),
                    pool_config(),
                )
                self.assertFalse(result["passed"])
                self.assertIn(reason, result["reasons"])

    def test_listing_gate_rejection_does_not_report_unneeded_metric_data_failures(self):
        frame = history(periods=19).drop(
            columns=["amount", "turnover_rate", "circulating_market_cap"]
        )

        result = evaluate_stock_pool(
            frame,
            frame.iloc[-1]["datetime"].date(),
            pool_config(),
        )

        self.assertEqual(["stock_pool_listing_days_below_min"], result["reasons"])

    def test_rejects_st_and_delisting_names(self):
        frame = history()
        for name, reason in (
            ("*ST样本", "stock_pool_special_treatment"),
            ("样本退", "stock_pool_delisting_risk"),
        ):
            with self.subTest(name=name):
                result = evaluate_stock_pool(
                    frame,
                    frame.iloc[-1]["datetime"].date(),
                    pool_config(),
                    name=name,
                )
                self.assertIn(reason, result["reasons"])

    def test_missing_policy_can_reject_or_allow(self):
        frame = history().drop(columns=["turnover_rate", "circulating_market_cap"])
        strict = evaluate_stock_pool(
            frame,
            frame.iloc[-1]["datetime"].date(),
            pool_config(),
        )
        loose = evaluate_stock_pool(
            frame,
            frame.iloc[-1]["datetime"].date(),
            pool_config(missing_data_policy="allow"),
        )
        self.assertFalse(strict["passed"])
        self.assertIn("stock_pool_market_cap_missing", strict["reasons"])
        self.assertIn("stock_pool_turnover_missing", strict["reasons"])
        self.assertTrue(loose["passed"])
        self.assertIn("stock_pool_market_cap_missing", loose["warnings"])

        empty = evaluate_stock_pool(
            pd.DataFrame(),
            date(2025, 1, 2),
            pool_config(missing_data_policy="allow"),
        )
        self.assertTrue(empty["passed"])
        self.assertIn("stock_pool_listing_days_missing", empty["warnings"])

    def test_filter_uses_each_event_day_without_lookahead(self):
        frame = history()
        early_day = frame.iloc[119]["datetime"].date()
        late_day = frame.iloc[-1]["datetime"].date()
        frame.loc[frame.index >= 120, "circulating_market_cap"] = 4000.0
        events = {
            "buy": [
                {"day": early_day.isoformat(), "signal_type": "buy_1"},
                {"day": late_day.isoformat(), "signal_type": "buy_1"},
            ],
            "sell": [],
        }
        filtered, skipped, details = filter_buy_events(frame, events, pool_config())
        self.assertEqual([early_day.isoformat()], [item["day"] for item in filtered["buy"]])
        self.assertEqual(1, skipped["stock_pool_market_cap_above_max"])
        self.assertEqual(late_day.isoformat(), details[0]["day"])

    def test_stale_metric_history_is_not_used_for_a_later_signal(self):
        frame = history()
        later_day = frame.iloc[-1]["datetime"].date() + pd.Timedelta(days=1)
        strict = evaluate_stock_pool(frame, later_day, pool_config())
        loose = evaluate_stock_pool(
            frame,
            later_day,
            pool_config(missing_data_policy="allow"),
        )
        self.assertFalse(strict["passed"])
        self.assertIn("stock_pool_history_stale", strict["reasons"])
        self.assertTrue(loose["passed"])
        self.assertIsNone(loose["metrics"]["market_cap"])

    def test_disabled_filter_keeps_events_without_history(self):
        events = {"buy": [{"day": date(2025, 1, 2).isoformat()}], "sell": []}
        filtered, skipped, details = filter_buy_events(
            pd.DataFrame(),
            events,
            {"stock_pool": {"enabled": False}},
        )
        self.assertEqual(events, filtered)
        self.assertEqual({}, dict(skipped))
        self.assertEqual([], details)

    def test_live_scan_reports_stock_pool_rejections_without_database(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = object.__new__(SignalMonitor)
            monitor.config = {
                **pool_config(),
                "scan": {"universe_mode": "all_a"},
                "entry_filters": {"position_gate_enabled": False},
            }
            monitor.market = MagicMock()
            monitor.market.refresh_daily_histories_from_snapshot.return_value = 0
            monitor.market.latest_expected_trade_date.return_value = date(2025, 7, 16)
            monitor.market.get_stock_list.return_value = pd.DataFrame(
                [
                    {"code": "000001", "name": "通过股份"},
                    {"code": "000002", "name": "过滤股份"},
                ]
            )
            daily = history(periods=140)
            monitor.market.get_bars.return_value = daily
            monitor.market.get_stock_pool_history.side_effect = (
                lambda symbol, **_: history(market_cap=100 if symbol == "000001" else 40)
            )
            monitor.analyzer = MagicMock()
            monitor.analyzer.analyze.side_effect = lambda *_: {
                    "event_objects": [],
                    "timeframes": {
                        "1d": {
                            "latest_time": "2025-07-16",
                            "latest_price": 10.0,
                            "buy_score": 70,
                            "indicators": {
                                "golden_cross_entry_ready": True,
                                "golden_cross_entry_zone": "above",
                                "golden_cross_zone": "above",
                                "golden_cross_state": "confirmed_pullback",
                                "dif": 1.2,
                                "dea": 0.8,
                                "golden_cross_zone_label": "0轴上方金叉",
                                "above_ma_long": True,
                                "ma_long_up": True,
                            },
                            "chan": {"fresh_signals": []},
                        }
                    },
                }
            monitor.store = MagicMock()
            monitor.store.get_state.return_value = "true"
            monitor.notifier = MagicMock()
            monitor.notifier.active_channels.return_value = []
            monitor.output_dir = Path(directory)
            monitor.watchlist = []
            monitor.max_scan_symbols = 500
            monitor.min_daily_bars = 120
            monitor.candidate_ttl = 5
            monitor.candidate_limit = 100
            monitor.push_candidate_pool = False
            monitor._name_map = None
            monitor._market_entry_context = lambda: {"allows_entries": True}

            report = monitor.scan_zero_axis(notify=False)

            self.assertEqual(1, report["candidate_count"])
            self.assertEqual("000001", report["candidates"][0]["symbol"])
            self.assertEqual(1, report["stock_pool"]["rejected_candidates"])
            self.assertEqual(
                1,
                report["stock_pool"]["rejections"]["stock_pool_market_cap_below_min"],
            )
            self.assertEqual(100.0, report["candidates"][0]["stock_pool_metrics"]["market_cap"])

    def test_live_scan_observes_above_cross_in_range_regime(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = object.__new__(SignalMonitor)
            monitor.config = {
                **pool_config(enabled=False),
                "scan": {"universe_mode": "all_a"},
                "entry_filters": {"position_gate_enabled": False},
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {
                            "macd_golden_cross_pullback_confirmed_above": "enabled",
                        },
                        "by_regime": {
                            "range": {
                                "macd_golden_cross_pullback_confirmed_above": "observe_only",
                            }
                        },
                    }
                },
            }
            monitor.market = MagicMock()
            monitor.market.refresh_daily_histories_from_snapshot.return_value = 0
            monitor.market.latest_expected_trade_date.return_value = date(2025, 7, 16)
            monitor.market.get_stock_list.return_value = pd.DataFrame(
                [{"code": "000001", "name": "观察样本"}]
            )
            monitor.market.get_bars.return_value = history(periods=140)
            monitor.analyzer = MagicMock()
            monitor.analyzer.analyze.side_effect = lambda *_: {
                "event_objects": [],
                "timeframes": {
                    "1d": {
                        "latest_time": "2025-07-16",
                        "latest_price": 10.0,
                        "buy_score": 70,
                        "indicators": {
                            "golden_cross_entry_ready": True,
                            "golden_cross_entry_zone": "above",
                            "golden_cross_zone": "above",
                            "golden_cross_state": "confirmed_pullback",
                            "dif": 1.2,
                            "dea": 0.8,
                        },
                        "chan": {"fresh_signals": []},
                    }
                },
            }
            monitor.store = MagicMock()
            monitor.store.get_state.return_value = "true"
            monitor.notifier = MagicMock()
            monitor.notifier.active_channels.return_value = []
            monitor.output_dir = Path(directory)
            monitor.watchlist = []
            monitor.max_scan_symbols = 500
            monitor.min_daily_bars = 120
            monitor.candidate_ttl = 5
            monitor.candidate_limit = 100
            monitor.push_candidate_pool = False
            monitor._name_map = None
            monitor._market_entry_context = lambda: {
                "allows_entries": True,
                "regime": "range",
            }

            report = monitor.scan_zero_axis(notify=False)

            self.assertEqual(0, report["candidate_count"])
            self.assertEqual(1, len(report["observed_candidates"]))
            self.assertEqual("observe_only", report["observed_candidates"][0]["execution_mode"])
            self.assertEqual("range", report["observed_candidates"][0]["regime"])

    def test_live_scan_keeps_gate_closed_candidates_in_pool_as_observe_only(self):
        """市场闸门关闭（熊市）时，enabled 信号不再被静默丢弃，也不仅进观察列表：
        候选降级 execution_mode=observe_only 后继续进入候选池，指标池照常产出，
        开仓语义不变（执行模式仍为观察）。"""
        with tempfile.TemporaryDirectory() as directory:
            monitor = object.__new__(SignalMonitor)
            monitor.config = {
                **pool_config(enabled=False),
                "scan": {"universe_mode": "all_a"},
                "entry_filters": {"position_gate_enabled": False},
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {
                            "macd_golden_cross_pullback_confirmed_near": "enabled",
                        },
                    }
                },
            }
            monitor.market = MagicMock()
            monitor.market.refresh_daily_histories_from_snapshot.return_value = 0
            monitor.market.latest_expected_trade_date.return_value = date(2025, 7, 16)
            monitor.market.get_stock_list.return_value = pd.DataFrame(
                [{"code": "000001", "name": "熊市零轴金叉"}]
            )
            monitor.market.get_bars.return_value = history(periods=140)
            monitor.analyzer = MagicMock()
            monitor.analyzer.analyze.side_effect = lambda *_: {
                "event_objects": [],
                "timeframes": {
                    "1d": {
                        "latest_time": "2025-07-16",
                        "latest_price": 10.0,
                        "buy_score": 70,
                        "indicators": {
                            "golden_cross_entry_ready": True,
                            "golden_cross_entry_zone": "near",
                            "golden_cross_zone": "near",
                            "golden_cross_state": "confirmed_pullback",
                            "dif": 1.2,
                            "dea": 0.8,
                        },
                        "chan": {"fresh_signals": []},
                    }
                },
            }
            monitor.store = MagicMock()
            monitor.store.get_state.return_value = "true"
            monitor.notifier = MagicMock()
            monitor.notifier.active_channels.return_value = []
            monitor.output_dir = Path(directory)
            monitor.watchlist = []
            monitor.max_scan_symbols = 500
            monitor.min_daily_bars = 120
            monitor.candidate_ttl = 5
            monitor.candidate_limit = 100
            monitor.push_candidate_pool = False
            monitor._name_map = None
            monitor._market_entry_context = lambda: {
                "allows_entries": False,
                "regime": "bear",
            }

            report = monitor.scan_zero_axis(notify=False)

            self.assertEqual(1, report["candidate_count"])
            self.assertEqual(0, report["observed_count"])
            self.assertEqual([], report["observed_candidates"])
            entry = report["candidates"][0]
            self.assertEqual("observe_only", entry["execution_mode"])
            self.assertEqual("market_gate_closed", entry["observe_reason"])
            self.assertTrue(entry["entry_gate_blocked"])
            self.assertEqual("bear", entry["market_context"]["regime"])
            self.assertEqual("near", entry["golden_cross_zone"])

    def test_live_scan_keeps_position_gate_blocked_candidates_in_pool_as_observe_only(self):
        """仓位闸门未满足（个股不在上行长期均线上）时同样降级：
        进入候选池但 execution_mode=observe_only，并标注 observe_reason。"""
        with tempfile.TemporaryDirectory() as directory:
            monitor = object.__new__(SignalMonitor)
            monitor.config = {
                **pool_config(enabled=False),
                "scan": {"universe_mode": "all_a"},
                "entry_filters": {"position_gate_enabled": True},
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {
                            "macd_golden_cross_pullback_confirmed_above": "enabled",
                        },
                    }
                },
            }
            monitor.market = MagicMock()
            monitor.market.refresh_daily_histories_from_snapshot.return_value = 0
            monitor.market.latest_expected_trade_date.return_value = date(2025, 7, 16)
            monitor.market.get_stock_list.return_value = pd.DataFrame(
                [{"code": "000001", "name": "仓位不足样本"}]
            )
            monitor.market.get_bars.return_value = history(periods=140)
            monitor.analyzer = MagicMock()
            monitor.analyzer.analyze.side_effect = lambda *_: {
                "event_objects": [],
                "timeframes": {
                    "1d": {
                        "latest_time": "2025-07-16",
                        "latest_price": 10.0,
                        "buy_score": 70,
                        "indicators": {
                            "golden_cross_entry_ready": True,
                            "golden_cross_entry_zone": "above",
                            "golden_cross_zone": "above",
                            "golden_cross_state": "confirmed_pullback",
                            "dif": 1.2,
                            "dea": 0.8,
                            "above_ma_long": False,
                            "ma_long_up": False,
                        },
                        "chan": {"fresh_signals": []},
                    }
                },
            }
            monitor.store = MagicMock()
            monitor.store.get_state.return_value = "true"
            monitor.notifier = MagicMock()
            monitor.notifier.active_channels.return_value = []
            monitor.output_dir = Path(directory)
            monitor.watchlist = []
            monitor.max_scan_symbols = 500
            monitor.min_daily_bars = 120
            monitor.candidate_ttl = 5
            monitor.candidate_limit = 100
            monitor.push_candidate_pool = False
            monitor._name_map = None
            monitor._market_entry_context = lambda: {
                "allows_entries": True,
                "regime": "bull",
            }

            report = monitor.scan_zero_axis(notify=False)

            self.assertEqual(1, report["candidate_count"])
            self.assertEqual(0, report["observed_count"])
            entry = report["candidates"][0]
            self.assertEqual("observe_only", entry["execution_mode"])
            self.assertEqual("position_gate_not_ready", entry["observe_reason"])
            self.assertTrue(entry["entry_gate_blocked"])

    def test_live_scan_self_heals_stale_stock_pool_history(self):
        """股票池历史停在上一交易日（收盘后首扫缓存 24h）时：
        scan 失效当日缓存并重拉一次，源回补后候选正常入池，
        而不是被 stock_pool_history_stale 拒绝。"""
        with tempfile.TemporaryDirectory() as directory:
            monitor = object.__new__(SignalMonitor)
            monitor.config = {
                **pool_config(),
                "scan": {"universe_mode": "all_a"},
                "entry_filters": {"position_gate_enabled": False},
            }
            monitor.market = MagicMock()
            monitor.market.refresh_daily_histories_from_snapshot.return_value = 0
            monitor.market.latest_expected_trade_date.return_value = date(2025, 7, 16)
            monitor.market.get_stock_list.return_value = pd.DataFrame(
                [{"code": "000001", "name": "自愈样本"}]
            )
            monitor.market.get_bars.return_value = history(periods=140)
            stale = history(periods=139)  # 停在 2025-07-15 < as_of 2025-07-16
            fresh = history(periods=140)
            monitor.market.get_stock_pool_history.side_effect = [stale, fresh]
            call_log = []
            monitor.market.invalidate_stock_pool_history.side_effect = (
                lambda *args, **kwargs: call_log.append((args, kwargs)) or True
            )
            monitor.analyzer = MagicMock()
            monitor.analyzer.analyze.side_effect = lambda *_: {
                "event_objects": [],
                "timeframes": {
                    "1d": {
                        "latest_time": "2025-07-16",
                        "latest_price": 10.0,
                        "buy_score": 70,
                        "indicators": {
                            "golden_cross_entry_ready": True,
                            "golden_cross_entry_zone": "above",
                            "golden_cross_zone": "above",
                            "golden_cross_state": "confirmed_pullback",
                            "dif": 1.2,
                            "dea": 0.8,
                            "golden_cross_zone_label": "0轴上方金叉",
                            "above_ma_long": True,
                            "ma_long_up": True,
                        },
                        "chan": {"fresh_signals": []},
                    }
                },
            }
            monitor.store = MagicMock()
            monitor.store.get_state.return_value = "true"
            monitor.notifier = MagicMock()
            monitor.notifier.active_channels.return_value = []
            monitor.output_dir = Path(directory)
            monitor.watchlist = []
            monitor.max_scan_symbols = 500
            monitor.min_daily_bars = 120
            monitor.candidate_ttl = 5
            monitor.candidate_limit = 100
            monitor.push_candidate_pool = False
            monitor._name_map = None
            monitor._market_entry_context = lambda: {
                "allows_entries": True,
                "regime": "bull",
            }

            report = monitor.scan_zero_axis(notify=False)

            self.assertEqual(1, len(call_log))
            self.assertEqual(0, report["stock_pool"]["rejected_candidates"])
            self.assertEqual(1, report["candidate_count"])
            self.assertEqual("000001", report["candidates"][0]["symbol"])
            self.assertEqual(
                "2025-07-16",
                report["candidates"][0]["stock_pool_metrics"]["as_of"],
            )


class MarketGateObservabilityScanTests(unittest.TestCase):
    """scan_zero_axis 日报必须同时给出指数层 blocker 分布与个股层被拦信号数。"""

    def _scan_monitor(self, directory: str, *, gate_open: bool, symbols: list[dict]):
        monitor = object.__new__(SignalMonitor)
        monitor.config = {
            **pool_config(enabled=False),
            "scan": {"universe_mode": "watchlist"},
            "entry_filters": {"market_gate_enabled": True, "position_gate_enabled": True},
        }
        monitor.market = MagicMock()
        monitor.market.refresh_daily_histories_from_snapshot.return_value = 0
        monitor.market.latest_expected_trade_date.return_value = date(2025, 7, 16)
        monitor.market.get_bars.return_value = history(periods=140)
        monitor.analyzer = MagicMock()
        monitor.analyzer.analyze.side_effect = lambda *_: {
            "event_objects": [],
            "timeframes": {
                "1d": {
                    "latest_time": "2025-07-16",
                    "latest_price": 10.0,
                    "buy_score": 70,
                    "indicators": {
                        "golden_cross_entry_ready": True,
                        "golden_cross_entry_zone": "above",
                        "golden_cross_zone": "above",
                        "golden_cross_state": "confirmed_pullback",
                        "dif": 1.2,
                        "dea": 0.8,
                        "above_ma_long": True,
                        "ma_long_up": True,
                    },
                    "chan": {"fresh_signals": []},
                }
            },
        }
        monitor.store = MagicMock()
        # run_state 用真实的内存 KV 语义：set 之后再 get 必须看得到，
        # 否则"观测写没写进去"这件事根本测不出来。
        state: dict[str, str] = {}
        monitor.store.get_state.side_effect = lambda key, default=None: state.get(key, default)
        monitor.store.set_state.side_effect = lambda key, value: state.__setitem__(key, value)
        monitor.notifier = MagicMock()
        monitor.notifier.active_channels.return_value = []
        monitor.output_dir = Path(directory)
        monitor.watchlist = [row["code"] for row in symbols]
        monitor.max_scan_symbols = 500
        monitor.min_daily_bars = 120
        monitor.candidate_ttl = 5
        monitor.candidate_limit = 100
        monitor.push_candidate_pool = False
        monitor._name_map = None
        monitor.market.get_stock_list.return_value = pd.DataFrame(symbols)
        monitor._market_entry_context = lambda: {
            "enabled": True,
            "allows_entries": gate_open,
            "blocked": not gate_open,
            "blocked_by": [] if gate_open else ["ma_long_down", "below_ma_long"],
            "regime": "bull" if gate_open else "bear",
            "index_last_day": "2025-07-16",
        }
        return monitor

    def test_blocked_signals_are_counted_and_gate_history_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._scan_monitor(
                directory, gate_open=False, symbols=[{"code": "000001", "name": "甲"}]
            )

            report = monitor.scan_zero_axis(notify=False)

            signals = report["market_gate_signals"]
            self.assertEqual(1, signals["signals_screened"])
            self.assertEqual(1, signals["blocked_by_market_gate"])
            self.assertEqual(1, signals["candidates_published"])
            self.assertEqual(1, signals["this_run"]["candidates_entry_gate_blocked"])
            self.assertEqual("2025-07-16", signals["day"])
            # 指数层：当日 blocker 与逐日分布都落进报告。
            gate = report["market_gate"]
            self.assertEqual("2025-07-16", gate["today"]["date"])
            self.assertFalse(gate["today"]["allows_entries"])
            self.assertEqual(
                ["ma_long_down", "below_ma_long"], gate["today"]["active_blockers"]
            )
            self.assertEqual(1, gate["days_recorded"])
            self.assertEqual("1/1", gate["hits"]["ma_long_down"]["display"])
            # 观测写进了 run_state（web_bridge 侧可读）。
            written = {
                call.args[0]: call.args[1] for call in monitor.store.set_state.call_args_list
            }
            self.assertIn("market_gate_history", written)
            self.assertIn("2025-07-16", written["market_gate_history"])

    def test_open_gate_reports_zero_blocked_signals_without_hiding_the_state(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._scan_monitor(
                directory, gate_open=True, symbols=[{"code": "000001", "name": "甲"}]
            )

            report = monitor.scan_zero_axis(notify=False)

            signals = report["market_gate_signals"]
            self.assertEqual(1, signals["signals_screened"])
            self.assertEqual(0, signals["blocked_by_market_gate"])
            gate = report["market_gate"]
            self.assertTrue(gate["today"]["allows_entries"])
            self.assertEqual([], gate["today"]["active_blockers"])
            self.assertEqual(1, gate["days_recorded"])
            self.assertEqual("0/1", gate["hits"]["ma_long_down"]["display"])
            lines = SignalMonitor._market_gate_summary_lines(gate, signals)
            self.assertIn("闸门：开", "\n".join(lines))

    def test_empty_scan_still_reports_the_gate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._scan_monitor(directory, gate_open=False, symbols=[])

            report = monitor.scan_zero_axis(notify=False)

            signals = report["market_gate_signals"]
            self.assertEqual(0, signals["signals_screened"])
            self.assertEqual(0, signals["blocked_by_market_gate"])
            # 无信号也必须能看出闸门是开是关，否则数据不可解读。
            lines = SignalMonitor._market_gate_summary_lines(report["market_gate"], signals)
            self.assertIn("闸门：关", "\n".join(lines))
            self.assertEqual(1, report["market_gate"]["days_recorded"])

    def test_scan_summary_push_text_carries_the_gate_block(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._scan_monitor(
                directory, gate_open=False, symbols=[{"code": "000001", "name": "甲"}]
            )
            monitor.push_candidate_pool = True
            monitor.notifier.active_channels.return_value = ["webhook"]
            enqueued = []
            monitor.store.enqueue_event = lambda event, channels: enqueued.append(event) or True
            monitor.dispatch_outbox = MagicMock(return_value={"delivered": 1, "failed": 0})

            monitor.scan_zero_axis(notify=True)

            self.assertEqual(1, len(enqueued))
            evidence = enqueued[0].evidence
            self.assertIn("闸门：关", evidence["content"])
            self.assertIn("ma_long_down", evidence["content"])
            self.assertIn("因闸门被拦下的信号：1 只", evidence["content"])
            self.assertEqual(1, evidence["market_gate_signals"]["blocked_by_market_gate"])
            self.assertEqual(
                ["ma_long_down", "below_ma_long"],
                evidence["market_gate"]["today"]["active_blockers"],
            )


if __name__ == "__main__":
    unittest.main()
