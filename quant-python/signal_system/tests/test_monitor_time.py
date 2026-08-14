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


if __name__ == "__main__":
    unittest.main()
