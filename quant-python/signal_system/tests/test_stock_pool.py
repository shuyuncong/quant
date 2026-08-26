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


if __name__ == "__main__":
    unittest.main()
