import os
import sys
import tempfile
import unittest


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
            claimed = store.claim_deliveries()
            self.assertEqual(2, len(claimed))
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


if __name__ == "__main__":
    unittest.main()
