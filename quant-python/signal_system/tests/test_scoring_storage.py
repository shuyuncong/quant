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
                "golden_cross_entry_ready": True,
                "golden_cross_entry_zone": "near",
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

    def test_buy_2_observe_policy_keeps_non_actionable_watch_event(self):
        analyzer = MultiTimeframeAnalyzer(
            {
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {"buy_2": "observe_only"},
                    }
                }
            }
        )
        report = TimeframeReport(
            timeframe="1d",
            status="ok",
            latest_time="2025-01-01T15:00:00",
            latest_price=10.0,
            indicators={},
            chan={
                "fresh_signals": [
                    {
                        "signal_type": "buy_2",
                        "side": "buy",
                        "structure_time": "2025-01-01T14:30:00",
                        "confirmed_at": "2025-01-01T15:00:00",
                    }
                ]
            },
        )

        events = analyzer._events("000001.SZ", "平安银行", report, "buy", 25, [])

        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("buy_2", event.signal_type)
        self.assertFalse(event.evidence["actionable"])
        self.assertEqual("watch", event.evidence["signal_level"])
        self.assertEqual("observe_only", event.evidence["execution_mode"])

    def test_disabled_buy_signal_does_not_create_event(self):
        analyzer = MultiTimeframeAnalyzer(
            {
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {"buy_2": "disabled"},
                    }
                }
            }
        )
        report = TimeframeReport(
            timeframe="1d",
            status="ok",
            latest_time="2025-01-01T15:00:00",
            latest_price=10.0,
            indicators={},
            chan={
                "fresh_signals": [
                    {
                        "signal_type": "buy_2",
                        "side": "buy",
                        "structure_time": "2025-01-01T14:30:00",
                        "confirmed_at": "2025-01-01T15:00:00",
                    }
                ]
            },
        )

        self.assertEqual(
            [],
            analyzer._events("000001.SZ", "平安银行", report, "buy", 25, []),
        )

    def test_enabled_macd_event_executes_alongside_observed_buy_2(self):
        analyzer = MultiTimeframeAnalyzer(
            {
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {"buy_2": "observe_only"},
                    }
                }
            }
        )
        report = TimeframeReport(
            timeframe="1d",
            status="ok",
            latest_time="2025-01-03T15:00:00",
            latest_price=10.3,
            indicators={
                "golden_cross_entry_ready": True,
                "golden_cross_entry_zone": "above",
            },
            chan={
                "fresh_signals": [
                    {
                        "signal_type": "buy_2",
                        "side": "buy",
                        "structure_time": "2025-01-03T14:30:00",
                        "confirmed_at": "2025-01-03T15:00:00",
                    }
                ]
            },
        )

        events = analyzer._events("000001.SZ", "平安银行", report, "buy", 70, [])

        self.assertEqual(2, len(events))
        buy_2_event = next(event for event in events if event.signal_type == "buy_2")
        macd_event = next(
            event
            for event in events
            if event.signal_type == "macd_golden_cross_pullback_confirmed_above"
        )
        self.assertFalse(buy_2_event.evidence["actionable"])
        self.assertEqual("observe_only", buy_2_event.evidence["execution_mode"])
        self.assertTrue(macd_event.evidence["actionable"])
        self.assertEqual("enabled", macd_event.evidence["execution_mode"])

    def test_raw_golden_cross_and_pullback_confirmation_are_separate_events(self):
        cross_time = "2025-01-01T10:00:00"
        raw_report = TimeframeReport(
            timeframe="1d",
            status="ok",
            latest_time=cross_time,
            latest_price=10.0,
            indicators={
                "golden_cross": True,
                "golden_cross_state": "pending_pullback",
                "golden_cross_zone": "above",
                "golden_cross_zone_label": "0轴上方金叉",
                "golden_cross_cross_time": cross_time,
            },
        )
        raw_events = self.analyzer._events(
            "000001.SZ", "平安银行", raw_report, "buy", 25, []
        )
        self.assertEqual(1, len(raw_events))
        watch = raw_events[0]
        self.assertEqual("macd_golden_cross_detected_above", watch.signal_type)
        self.assertEqual("watch", watch.evidence["signal_level"])
        self.assertFalse(watch.evidence["actionable"])

        confirmed_report = TimeframeReport(
            timeframe="1d",
            status="ok",
            latest_time="2025-01-03T10:00:00",
            latest_price=10.3,
            indicators={
                "golden_cross": False,
                "golden_cross_entry_ready": True,
                "golden_cross_entry_zone": "above",
                "golden_cross_cross_time": cross_time,
            },
        )
        confirmed_events = self.analyzer._events(
            "000001.SZ", "平安银行", confirmed_report, "buy", 40, []
        )
        self.assertEqual(1, len(confirmed_events))
        confirmation = confirmed_events[0]
        self.assertEqual(
            "macd_golden_cross_pullback_confirmed_above",
            confirmation.signal_type,
        )
        self.assertTrue(confirmation.evidence["actionable"])
        self.assertEqual("confirmation", confirmation.evidence["signal_level"])
        self.assertEqual(
            watch.evidence["setup_id"],
            confirmation.evidence["setup_id"],
        )
        self.assertNotEqual(watch.event_id, confirmation.event_id)

    def test_below_zero_raw_golden_cross_does_not_create_watch_event(self):
        report = TimeframeReport(
            timeframe="1d",
            status="ok",
            latest_time="2025-01-01T10:00:00",
            latest_price=10.0,
            indicators={
                "golden_cross": True,
                "golden_cross_zone": "below",
            },
        )
        self.assertEqual(
            [],
            self.analyzer._events(
                "000001.SZ", "平安银行", report, "buy", 30, []
            ),
        )

    def test_each_fresh_chan_point_creates_a_stable_event(self):
        report = TimeframeReport(
            timeframe="15m",
            status="ok",
            latest_time="2025-01-01T10:15:00",
            latest_price=10.0,
            indicators={
                "golden_cross": True,
                "golden_cross_entry_ready": True,
                "golden_cross_entry_zone": "above",
                "golden_cross_zone": "above",
            },
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
        self.assertEqual(
            [
                "buy_1",
                "buy_2",
                "macd_golden_cross_pullback_confirmed_above",
            ],
            [event.signal_type for event in events],
        )
        self.assertEqual(
            [
                "2025-01-01T10:00:00",
                "2025-01-01T10:15:00",
                "2025-01-01T10:15:00",
            ],
            [event.confirmed_at for event in events],
        )
        self.assertEqual(
            [["buy_1"], ["buy_2"], ["macd_golden_cross_pullback_confirmed_above"]],
            [event.evidence["components"] for event in events],
        )

    def test_list_outbox_log_joins_event_details_and_order(self):
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
                evidence={"notification_kind": "trade_signal", "content": "买入候选"},
            )
            store.enqueue_event(event, ["webhook", "wechat"])
            records = store.list_outbox_log()
            self.assertEqual(2, len(records))
            record = records[0]
            self.assertEqual("webhook", record["channel"])
            self.assertEqual("000001.SZ", record["symbol"])
            self.assertEqual("buy_1", record["signal_type"])
            self.assertEqual("平安银行", record["name"])
            self.assertEqual("买入候选", record["summary"])
            self.assertEqual("pending", record["status"])
            self.assertEqual(0, record["attempts"])
            self.assertIn("event_id", record)
            self.assertIn("confirmed_at", record)

    def test_sync_candidates_moves_dropped_symbols_to_expired_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SignalStore(os.path.join(directory, "signals.db"))
            store.upsert_candidates(
                [
                    {"symbol": "000001", "name": "A", "score": 90},
                    {"symbol": "000002", "name": "B", "score": 80},
                ],
                ttl_business_days=5,
                capacity=10,
            )
            store.sync_candidates(
                [{"symbol": "000001", "name": "A", "score": 95}],
                ttl_business_days=5,
                capacity=10,
            )
            active = store.active_candidates()
            self.assertEqual(["000001"], [item["symbol"] for item in active])
            expired = store.list_expired_candidates()
            self.assertEqual(1, len(expired))
            self.assertEqual("000002", expired[0]["symbol"])
            self.assertEqual("no_longer_qualified", expired[0]["reason"])
            self.assertIn("name", expired[0])

    def test_expired_candidates_are_moved_to_expired_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SignalStore(os.path.join(directory, "signals.db"))
            # 候选过期时间设为过去，active_candidates 清理时移入失效池
            store.upsert_candidates(
                [{"symbol": "000001", "name": "A", "score": 90}],
                ttl_business_days=5,
                capacity=10,
            )
            with store._connect() as connection:
                connection.execute(
                    "UPDATE candidate SET expires_on = '2020-01-01' WHERE symbol = '000001'"
                )
            self.assertEqual([], store.active_candidates())
            expired = store.list_expired_candidates()
            self.assertEqual(1, len(expired))
            self.assertEqual("000001", expired[0]["symbol"])
            self.assertEqual("expired", expired[0]["reason"])
            self.assertEqual(1, store.expired_candidate_count())

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

    def test_requeue_failed_resets_terminal_deliveries_for_manual_retry(self):
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
            store.mark_failed(event.event_id, "webhook", 4, "boom", claimed["claim_token"])
            self.assertEqual(1, store.requeue_failed())
            summary = store.outbox_summary()
            self.assertEqual(1, summary["pending"])
            self.assertEqual(0, summary["failed"])
            # 重置后可再次被领取投递，且重试次数清零
            re_claimed = store.claim_deliveries()
            self.assertEqual(1, len(re_claimed))
            self.assertEqual(0, re_claimed[0]["attempts"])
            # 没有 failed 记录时重置为空操作
            self.assertEqual(0, store.requeue_failed())

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
