import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, date
from unittest.mock import MagicMock, call

import pandas as pd


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.service import SignalMonitor
from models import SignalEvent


class MonitorTimeTests(unittest.TestCase):
    def _monitor(self, directory):
        config = {
            "market_data": {"cache_dir": os.path.join(directory, "cache")},
            "runtime": {
                "database_path": os.path.join(directory, "signals.db"),
                "output_dir": os.path.join(directory, "output"),
            },
            "monitor": {"watchlist": []},
        }
        return SignalMonitor(config)

    def test_sessions_and_holiday_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.market.get_trade_dates = lambda: {date(2025, 1, 2)}
            self.assertTrue(monitor.is_trading_session(datetime(2025, 1, 2, 10, 0)))
            self.assertFalse(monitor.is_trading_session(datetime(2025, 1, 2, 12, 0)))
            self.assertFalse(monitor.is_trading_session(datetime(2025, 1, 3, 10, 0)))

    def test_full_market_bootstrap_does_not_complete_on_fetch_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.config["scan"] = {"universe_mode": "all_a"}
            monitor.max_scan_symbols = 3
            monitor.market.refresh_daily_histories_from_snapshot = lambda: 0
            monitor.market.get_stock_list = lambda: pd.DataFrame(
                [{"code": "000001", "name": "A"}, {"code": "000002", "name": "B"}]
            )
            monitor.market.latest_expected_trade_date = lambda: date(2025, 1, 2)

            def fail(*args, **kwargs):
                raise RuntimeError("network failed")

            monitor.market.get_bars = fail
            report = monitor.scan_zero_axis(notify=False)
            self.assertEqual(0.0, report["coverage"])
            self.assertFalse(report["completed_round"])
            self.assertNotEqual("true", monitor.store.get_state("daily_bootstrap_complete"))

    def test_stale_signal_is_not_current(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.market.latest_expected_trade_date = lambda: date(2025, 1, 2)
            event = SignalEvent(
                symbol="000001",
                name="A",
                timeframe="1d",
                signal_type="buy_1",
                side="buy",
                price=10,
                structure_time="2024-12-01T00:00:00",
                confirmed_at="2024-12-02T00:00:00",
                score=70,
            )
            self.assertFalse(monitor._event_is_current(event))

    def test_bootstrap_success_is_intersected_with_current_universe(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.config["scan"] = {"universe_mode": "all_a"}
            monitor.max_scan_symbols = 2
            monitor.store.set_state(
                "daily_bootstrap_success", '["000001", "999999"]'
            )
            monitor.market.refresh_daily_histories_from_snapshot = lambda: 0
            monitor.market.get_stock_list = lambda: pd.DataFrame(
                [{"code": "000001", "name": "A"}, {"code": "000002", "name": "B"}]
            )
            monitor.market.latest_expected_trade_date = lambda: date(2025, 1, 2)
            monitor.market.daily_history_is_usable = lambda symbol, *args, **kwargs: symbol == "000001"

            def fail(*args, **kwargs):
                raise RuntimeError("network failed")

            monitor.market.get_bars = fail
            report = monitor.scan_zero_axis(notify=False)
            self.assertEqual(0.5, report["coverage"])
            self.assertFalse(report["completed_round"])

    def test_bootstrap_success_is_revalidated_against_local_history(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.config["scan"] = {"universe_mode": "all_a"}
            monitor.max_scan_symbols = 2
            monitor.store.set_state("daily_bootstrap_success", '["000001"]')
            monitor.market.refresh_daily_histories_from_snapshot = lambda: 0
            monitor.market.get_stock_list = lambda: pd.DataFrame(
                [{"code": "000001", "name": "A"}, {"code": "000002", "name": "B"}]
            )
            monitor.market.latest_expected_trade_date = lambda: date(2025, 1, 2)
            monitor.market.daily_history_is_usable = lambda *args, **kwargs: False
            monitor.market.get_bars = MagicMock(side_effect=RuntimeError("network failed"))
            report = monitor.scan_zero_axis(notify=False)
            self.assertEqual(0.0, report["coverage"])
            self.assertFalse(report["completed_round"])

    def test_recent_listing_is_excluded_from_bootstrap_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.config["scan"] = {"universe_mode": "all_a"}
            monitor.max_scan_symbols = 2
            monitor.market.refresh_daily_histories_from_snapshot = lambda: 0
            monitor.market.get_stock_list = lambda: pd.DataFrame(
                [{"code": "000001", "name": "成熟股票"}, {"code": "000002", "name": "新股"}]
            )
            monitor.market.latest_expected_trade_date = lambda: date(2025, 1, 2)
            monitor.market.daily_history_is_usable = lambda *args, **kwargs: False

            def bars(symbol, *args, **kwargs):
                count = 120 if symbol == "000001" else 20
                return pd.DataFrame(
                    {
                        "datetime": pd.bdate_range(end="2025-01-02", periods=count),
                        "is_closed": True,
                    }
                )

            monitor.market.get_bars = bars
            monitor.analyzer.analyze = lambda *args, **kwargs: {
                "event_objects": [],
                "timeframes": {"1d": {"indicators": {}}},
            }
            report = monitor.scan_zero_axis(notify=False)
            self.assertEqual(1.0, report["coverage"])
            self.assertTrue(report["completed_round"])
            self.assertEqual(1, report["ineligible_symbols"])

    def test_bootstrap_completes_when_remaining_errors_are_stale_data(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.config["scan"] = {"universe_mode": "all_a"}
            monitor.max_scan_symbols = 2
            monitor.market.refresh_daily_histories_from_snapshot = lambda: 0
            monitor.market.get_stock_list = lambda: pd.DataFrame(
                [{"code": "000001", "name": "A"}, {"code": "000002", "name": "B"}]
            )
            monitor.market.latest_expected_trade_date = lambda: date(2025, 1, 2)

            def stale(*args, **kwargs):
                raise ValueError("日线已过期: latest=2024-12-31, expected=2025-01-02")

            monitor.market.get_bars = stale
            report = monitor.scan_zero_axis(notify=False)
            self.assertTrue(report["completed_round"])
            self.assertEqual(1.0, report["coverage"])
            self.assertEqual(2, report["ineligible_symbols"])
            self.assertEqual("true", monitor.store.get_state("daily_bootstrap_complete"))
            deferred = json.loads(monitor.store.get_state("daily_bootstrap_deferred"))
            self.assertEqual({"000001": "2025-01-02", "000002": "2025-01-02"}, deferred)

    def test_daily_scan_excludes_stale_data_errors_after_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.config["scan"] = {"universe_mode": "all_a"}
            monitor.store.set_state("daily_bootstrap_complete", "true")
            monitor.market.refresh_daily_histories_from_snapshot = lambda: 0
            monitor.market.get_stock_list = lambda: pd.DataFrame(
                [{"code": "000001", "name": "A"}, {"code": "000002", "name": "B"}]
            )
            monitor.market.latest_expected_trade_date = lambda: date(2025, 1, 2)

            def bars(symbol, *args, **kwargs):
                if symbol == "000002":
                    raise ValueError("日线已过期: latest=2024-12-31, expected=2025-01-02")
                return pd.DataFrame(
                    {
                        "datetime": pd.bdate_range(end="2025-01-02", periods=130),
                        "is_closed": True,
                    }
                )

            monitor.market.get_bars = bars
            monitor.analyzer.analyze = lambda *args, **kwargs: {
                "event_objects": [],
                "timeframes": {"1d": {"indicators": {}}},
            }
            report = monitor.scan_zero_axis(notify=False)
            self.assertEqual(1.0, report["coverage"])
            self.assertTrue(report["completed_round"])
            self.assertEqual(1, report["ineligible_symbols"])

    def test_daily_scan_retries_transient_errors_after_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.config["scan"] = {"universe_mode": "all_a"}
            monitor.store.set_state("daily_bootstrap_complete", "true")
            monitor.market.refresh_daily_histories_from_snapshot = lambda: 0
            monitor.market.get_stock_list = lambda: pd.DataFrame(
                [{"code": "000001", "name": "A"}, {"code": "000002", "name": "B"}]
            )
            monitor.market.latest_expected_trade_date = lambda: date(2025, 1, 2)

            def bars(symbol, *args, **kwargs):
                if symbol == "000002":
                    raise RuntimeError("network failed")
                return pd.DataFrame(
                    {
                        "datetime": pd.bdate_range(end="2025-01-02", periods=130),
                        "is_closed": True,
                    }
                )

            monitor.market.get_bars = bars
            monitor.analyzer.analyze = lambda *args, **kwargs: {
                "event_objects": [],
                "timeframes": {"1d": {"indicators": {}}},
            }
            report = monitor.scan_zero_axis(notify=False)
            self.assertEqual(0.5, report["coverage"])
            self.assertFalse(report["completed_round"])

    def test_dispatch_claims_only_the_delivery_being_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            delivery = {
                "event_id": "event-1",
                "channel": "webhook",
                "attempts": 0,
                "claim_token": "claim-1",
                "payload": {"event_id": "event-1"},
            }
            monitor.store.claim_deliveries = MagicMock(side_effect=[[delivery], []])
            monitor.store.mark_delivered = MagicMock(return_value=True)
            monitor.notifier.send = MagicMock(return_value=(True, "ok"))
            summary = monitor.dispatch_outbox()
            self.assertEqual({"delivered": 1, "failed": 0}, summary)
            self.assertEqual(
                [call(limit=1), call(limit=1)],
                monitor.store.claim_deliveries.call_args_list,
            )

    def test_monitor_cycle_uses_persistent_round_robin_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.max_monitor_symbols = 2
            monitor.monitoring_symbols = lambda: (
                ["000001.SZ", "000002.SZ", "000003.SZ"],
                {},
            )
            monitor.analyze_symbols = MagicMock(return_value={"mode": "analyze"})
            monitor.run_monitor_cycle(notify=False)
            monitor.run_monitor_cycle(notify=False)
            self.assertEqual(
                ["000001.SZ", "000002.SZ"],
                monitor.analyze_symbols.call_args_list[0].args[0],
            )
            self.assertEqual(
                ["000003.SZ", "000001.SZ"],
                monitor.analyze_symbols.call_args_list[1].args[0],
            )
            self.assertEqual(
                1,
                monitor.analyze_symbols.call_args_list[1].kwargs["report_meta"]["monitor_next_cursor"],
            )
            self.assertEqual(
                3,
                monitor.analyze_symbols.call_args_list[0].kwargs["report_meta"]["monitor_pool_size"],
            )

    def test_candidate_event_carries_zone_confirmations_and_risk(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            event = monitor._candidate_event(
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "price": 10.5,
                    "score": 350,
                    "confirmed_at": "2025-01-02T15:00:00",
                    "golden_cross_zone": "above",
                    "golden_cross_zone_label": "0轴上方金叉",
                    "confirmation_items": ["成交量温和放大"],
                }
            )
            self.assertEqual("macd_golden_cross_above", event.signal_type)
            self.assertEqual("candidate", event.evidence["notification_kind"])
            self.assertIn("优先级最高", event.evidence["risk_text"])

    def test_candidate_notification_is_covered_by_trade_event_for_same_cross(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            candidate = monitor._candidate_event(
                {
                    "symbol": "000001.SZ",
                    "confirmed_at": "2025-01-02T15:00:00",
                    "golden_cross_zone": "above",
                }
            )
            trade = SignalEvent(
                symbol="000001.SZ",
                name="平安银行",
                timeframe="1d",
                signal_type="buy_2",
                side="buy",
                price=10.0,
                structure_time="2025-01-02T15:00:00",
                confirmed_at="2025-01-02T15:00:00",
                score=80,
                evidence={"components": ["buy_2", "macd_golden_cross_above"]},
            )

            self.assertTrue(monitor._candidate_is_covered_by_trade_event(candidate, [trade]))

    def test_ai_analysis_notification_uses_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._monitor(directory)
            monitor.notifier.active_channels = MagicMock(return_value=["webhook"])
            monitor.store.enqueue_event = MagicMock(return_value=True)
            monitor.dispatch_outbox = MagicMock(return_value={"delivered": 1, "failed": 0})
            result = monitor.notify_ai_analysis(
                "AI自动解读 #1",
                "这是解读内容",
                "output/analysis.json",
                "2025-01-02T15:00:00",
            )
            self.assertEqual(1, result["enqueued"])
            event = monitor.store.enqueue_event.call_args.args[0]
            self.assertEqual("ai_analysis", event.signal_type)
            self.assertEqual("这是解读内容", event.evidence["content"])


if __name__ == "__main__":
    unittest.main()
