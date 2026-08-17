import os
import sys
import tempfile
import unittest

import pandas as pd


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from models import SignalEvent, TimeframeReport
from storage.signal_store import SignalStore
from strategy.multi_timeframe import MultiTimeframeAnalyzer


class ScoringAndStorageTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = MultiTimeframeAnalyzer({})

    def test_buy_and_sell_have_independent_reachable_scores(self):
        higher_buy = TimeframeReport(
            timeframe="1d",
            status="ok",
            indicators={"dif_rising": True, "dif": 0.1},
        )
        buy = TimeframeReport(
            timeframe="60m",
            status="ok",
            indicators={
                "zero_axis_golden_cross": True,
                "above_ma60": True,
                "ma60_up": True,
                "volume_ratio": 1.2,
            },
            chan={"fresh_signals": [{"signal_type": "buy_3", "side": "buy"}]},
        )
        buy_score, _ = self.analyzer._score(buy, higher_buy, "buy")
        self.assertGreaterEqual(buy_score, 60)

        higher_sell = TimeframeReport(
            timeframe="1d",
            status="ok",
            indicators={"dif_falling": True, "dif": -0.1},
        )
        sell = TimeframeReport(
            timeframe="60m",
            status="ok",
            indicators={
                "zero_axis_death_cross": True,
                "below_ma60": True,
                "ma60_down": True,
                "volume_ratio": 1.2,
                "price_change": -0.1,
            },
            chan={"fresh_signals": [{"signal_type": "sell_3", "side": "sell"}]},
        )
        sell_score, _ = self.analyzer._score(sell, higher_sell, "sell")
        self.assertGreaterEqual(sell_score, 60)

    def test_event_dedup_and_per_channel_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SignalStore(os.path.join(directory, "signals.db"))
            event = SignalEvent(
                symbol="000001",
                name="平安银行",
                timeframe="5m",
                signal_type="buy_3",
                side="buy",
                price=10,
                structure_time="2025-01-01T10:00:00",
                confirmed_at="2025-01-01T10:01:00",
                score=70,
            )
            self.assertTrue(store.enqueue_event(event, ["wechat", "webhook"]))
            self.assertFalse(store.enqueue_event(event, ["wechat", "webhook"]))
            self.assertFalse(store.enqueue_event(event, ["email"]))
            claimed = store.claim_deliveries()
            self.assertEqual(3, len(claimed))
            wechat = next(item for item in claimed if item["channel"] == "wechat")
            self.assertTrue(
                store.mark_delivered(event.event_id, "wechat", wechat["claim_token"])
            )
            self.assertFalse(
                store.mark_failed(
                    event.event_id,
                    "wechat",
                    0,
                    "late worker",
                    "stale-claim-token",
                )
            )
            self.assertEqual([], store.pending_deliveries())

    def test_fresh_chan_point_creates_event_below_score_threshold(self):
        report = TimeframeReport(
            timeframe="5m",
            status="ok",
            latest_time="2025-01-01T10:05:00",
            latest_price=10.0,
            indicators={},
            chan={
                "fresh_signals": [
                    {
                        "signal_type": "buy_1",
                        "side": "buy",
                        "structure_time": "2025-01-01T10:00:00",
                        "confirmed_at": "2025-01-01T10:05:00",
                    }
                ]
            },
        )
        event = self.analyzer._event("000001.SZ", "平安银行", report, "buy", 20, ["缠论buy_1 +20"])
        self.assertIsNotNone(event)
        self.assertFalse(event.evidence["strong_signal"])
        self.assertEqual("structure", event.evidence["signal_level"])

    def test_each_fresh_chan_point_creates_a_stable_event(self):
        report = TimeframeReport(
            timeframe="15m",
            status="ok",
            latest_time="2025-01-01T10:15:00",
            latest_price=10.0,
            indicators={"golden_cross": True, "golden_cross_zone": "above"},
            chan={
                "fresh_signals": [
                    {
                        "signal_type": "buy_1",
                        "side": "buy",
                        "structure_time": "2025-01-01T09:45:00",
                        "confirmed_at": "2025-01-01T10:00:00",
                    },
                    {
                        "signal_type": "buy_2",
                        "side": "buy",
                        "structure_time": "2025-01-01T10:00:00",
                        "confirmed_at": "2025-01-01T10:15:00",
                    },
                ]
            },
        )
        events = self.analyzer._events("000001.SZ", "平安银行", report, "buy", 80, [])
        self.assertEqual(["buy_1", "buy_2"], [event.signal_type for event in events])
        self.assertEqual(
            ["2025-01-01T10:00:00", "2025-01-01T10:15:00"],
            [event.confirmed_at for event in events],
        )
        self.assertTrue(all("macd_golden_cross_above" in event.evidence["components"] for event in events))

    def test_fifth_delivery_failure_becomes_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SignalStore(os.path.join(directory, "signals.db"))
            event = SignalEvent(
                symbol="000001.SZ",
                name="平安银行",
                timeframe="5m",
                signal_type="buy_1",
                side="buy",
                price=10,
                structure_time="2025-01-01T10:00:00",
                confirmed_at="2025-01-01T10:01:00",
                score=20,
            )
            store.enqueue_event(event, ["webhook"])
            claimed = store.claim_deliveries()[0]
            self.assertTrue(
                store.mark_failed(
                    event.event_id,
                    "webhook",
                    4,
                    "still failing",
                    claimed["claim_token"],
                )
            )
            summary = store.outbox_summary()
            self.assertEqual(0, summary["pending"])
            self.assertEqual(1, summary["failed"])

    def test_insufficient_timeframe_keeps_source_metadata(self):
        frame = pd.DataFrame(
            {
                "datetime": pd.date_range("2025-01-01", periods=10, freq="min"),
                "open": [10.0] * 10,
                "high": [10.1] * 10,
                "low": [9.9] * 10,
                "close": [10.0] * 10,
                "volume": [100.0] * 10,
                "is_closed": [True] * 10,
            }
        )
        frame.attrs.update(
            requested_bars=300,
            history_complete=False,
            source_mode="direct",
            source_warning="行情历史不足: 仅获取 10/300 根",
        )

        report, _ = self.analyzer._base_report("5m", frame)

        self.assertEqual("insufficient_data", report.status)
        self.assertEqual(10, report.indicators["bar_count"])
        self.assertEqual(300, report.indicators["requested_bar_count"])
        self.assertFalse(report.indicators["history_complete"])
        self.assertEqual("行情历史不足: 仅获取 10/300 根", report.indicators["source_warning"])


if __name__ == "__main__":
    unittest.main()
