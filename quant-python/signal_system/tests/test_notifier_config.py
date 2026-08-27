import os
import sys
import tempfile
import unittest
import json
from unittest.mock import MagicMock, patch


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from notification.signal_notifier import SignalNotifier
from utils.helpers import load_config


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size):
        return b"ok"


class _WechatErrorResponse(_Response):
    def read(self, size):
        return b'{"errcode":40014,"errmsg":"invalid access token"}'


class _BarkOkResponse(_Response):
    def read(self, size):
        return b'{"code":200,"message":"success"}'


class _BarkErrorResponse(_Response):
    def read(self, size):
        return b'{"code":400,"message":"device key not found"}'


class NotifierAndConfigTests(unittest.TestCase):
    def test_watch_signal_markdown_is_labeled_as_non_actionable_alert(self):
        markdown = SignalNotifier._markdown(
            {
                "event_id": "watch-1",
                "side": "buy",
                "symbol": "000001",
                "name": "平安银行",
                "timeframe": "1d",
                "signal_type": "macd_golden_cross_detected_above",
                "price": 10.0,
                "score": 25,
                "confirmed_at": "2025-01-01T15:00:00",
                "risk_notice": "test",
                "evidence": {
                    "notification_kind": "trade_signal",
                    "signal_level": "watch",
                    "strong_signal": False,
                    "score_reasons": ["等待回落确认"],
                },
            }
        )
        self.assertIn("MACD 金叉预警", markdown)
        self.assertIn("观察信号（等待回落确认）", markdown)
        self.assertIn("等待回落确认", markdown)

    def test_confirmation_markdown_is_distinct_from_raw_cross_watch(self):
        markdown = SignalNotifier._markdown(
            {
                "event_id": "confirmation-1",
                "side": "buy",
                "symbol": "000001",
                "name": "平安银行",
                "timeframe": "1d",
                "signal_type": "macd_golden_cross_pullback_confirmed_above",
                "price": 10.3,
                "score": 40,
                "confirmed_at": "2025-01-03T15:00:00",
                "risk_notice": "test",
                "evidence": {
                    "notification_kind": "trade_signal",
                    "signal_level": "confirmation",
                    "strong_signal": False,
                    "score_reasons": ["回落后重新站回"],
                },
            }
        )
        self.assertIn("MACD 回落确认", markdown)
        self.assertIn("确认信号（进入候选评估）", markdown)

    def test_candidate_markdown_contains_zone_and_confirmations(self):
        payload = {
            "event_id": "candidate-1",
            "side": "watch",
            "symbol": "000001.SZ",
            "name": "平安银行",
            "timeframe": "1d",
            "signal_type": "macd_golden_cross_above",
            "price": 10.5,
            "score": 350,
            "confirmed_at": "2025-01-02T15:00:00",
            "risk_notice": "test",
            "evidence": {
                "notification_kind": "candidate",
                "golden_cross_zone_label": "0轴上方金叉",
                "confirmation_items": ["成交量温和放大"],
                "risk_text": "优先级最高",
            },
        }
        markdown = SignalNotifier._markdown(payload)
        self.assertIn("0轴上方金叉", markdown)
        self.assertIn("成交量温和放大", markdown)

    def test_webhook_contract_has_schema_and_idempotency_key(self):
        notifier = SignalNotifier(
            {
                "notification": {
                    "webhook": {
                        "enabled": True,
                        "url": "https://example.com/signal",
                        "headers": {"Authorization": "Bearer test"},
                    }
                }
            }
        )
        opener = MagicMock()
        opener.open.return_value = _Response()
        payload = {
            "schema": "quant.signal.v1",
            "event_id": "event-1",
            "side": "buy",
            "symbol": "000001",
        }
        with patch("notification.signal_notifier.build_opener", return_value=opener):
            success, _ = notifier.send("webhook", payload)
        self.assertTrue(success)
        request = opener.open.call_args.args[0]
        self.assertEqual("event-1", request.headers["Idempotency-key"])
        self.assertEqual("Bearer test", request.headers["Authorization"])

    def test_config_expands_environment_and_resolves_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = os.path.join(directory, "config")
            os.makedirs(config_dir)
            path = os.path.join(config_dir, "config.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "market_data:\n"
                    "  cache_dir: ./cache\n"
                    "notification:\n"
                    "  webhook:\n"
                    "    url: ${TEST_SIGNAL_URL:-}\n"
                    "runtime:\n"
                    "  database_path: ./data/test.db\n"
                )
            old = os.environ.get("TEST_SIGNAL_URL")
            os.environ["TEST_SIGNAL_URL"] = "https://example.com/hook"
            try:
                config = load_config(path)
            finally:
                if old is None:
                    os.environ.pop("TEST_SIGNAL_URL", None)
                else:
                    os.environ["TEST_SIGNAL_URL"] = old
            self.assertEqual("https://example.com/hook", config["notification"]["webhook"]["url"])
            self.assertEqual(os.path.join(directory, "cache"), config["market_data"]["cache_dir"])

    def test_wechat_http_200_with_application_error_is_failure(self):
        notifier = SignalNotifier(
            {
                "notification": {
                    "wechat": {
                        "enabled": True,
                        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
                    }
                }
            }
        )
        opener = MagicMock()
        opener.open.return_value = _WechatErrorResponse()
        payload = {
            "schema": "quant.signal.v1",
            "event_id": "event-1",
            "side": "buy",
            "symbol": "000001",
            "name": "test",
            "timeframe": "5m",
            "signal_type": "buy_3",
            "price": 10.0,
            "score": 70,
            "confirmed_at": "2025-01-01T10:00:00",
            "risk_notice": "test",
            "evidence": {},
        }
        with patch("notification.signal_notifier.build_opener", return_value=opener):
            success, detail = notifier.send("wechat", payload)
        self.assertFalse(success)
        self.assertIn("40014", detail)

    def test_bark_uses_official_push_contract(self):
        notifier = SignalNotifier(
            {
                "notification": {
                    "bark": {
                        "enabled": True,
                        "url": "https://api.day.app/push",
                        "device_key": "test-device-key",
                    }
                }
            }
        )
        opener = MagicMock()
        opener.open.return_value = _BarkOkResponse()
        payload = {
            "schema": "quant.signal.v1",
            "event_id": "event-bark-1",
            "side": "buy",
            "symbol": "000001",
            "name": "平安银行",
            "timeframe": "5m",
            "signal_type": "buy_3",
            "price": 11.11,
            "score": 70,
            "confirmed_at": "2025-01-01T10:00:00",
            "risk_notice": "test",
            "evidence": {"score_reasons": ["MACD 金叉 +30"]},
        }
        with patch("notification.signal_notifier.build_opener", return_value=opener):
            success, _ = notifier.send("bark", payload)
        self.assertTrue(success)
        request = opener.open.call_args.args[0]
        self.assertEqual("https://api.day.app/push", request.full_url)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("test-device-key", body["device_key"])
        self.assertEqual("chan-signal", body["group"])
        self.assertEqual("买入观察 平安银行 000001 5m", body["title"])
        self.assertIn("MACD 金叉 +30", body["body"])

    def test_bark_truncates_long_ai_body_to_byte_budget(self):
        notifier = SignalNotifier(
            {
                "notification": {
                    "bark": {
                        "enabled": True,
                        "url": "https://api.day.app/push",
                        "device_key": "test-device-key",
                    }
                }
            }
        )
        opener = MagicMock()
        opener.open.return_value = _BarkOkResponse()
        payload = {
            "schema": "quant.signal.v1",
            "event_id": "event-bark-long-1",
            "side": "info",
            "symbol": "SYSTEM",
            "name": "AI自动解读 #999",
            "timeframe": "report",
            "signal_type": "ai_analysis",
            "price": 0.0,
            "score": 0,
            "confirmed_at": "2025-01-01T10:00:00",
            "risk_notice": "test",
            "evidence": {
                "notification_kind": "ai_analysis",
                # 1600 段约 33KB，远超 APNs 4KB 负载上限（Bark 服务端不拆分正文）
                "content": "结论：MACD 金叉确认。" + "行情趋势延续。" * 1600,
            },
        }
        with patch("notification.signal_notifier.build_opener", return_value=opener):
            success, _ = notifier.send("bark", payload)
        self.assertTrue(success)
        request = opener.open.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        encoded = body["body"].encode("utf-8")
        self.assertLessEqual(len(encoded), 3500)
        # 截断后仍是完整 UTF-8（不切断多字节字符），可正常解码
        body["body"].encode("utf-8").decode("utf-8")

    def test_truncate_utf8_bytes_never_splits_multibyte_chars(self):
        from notification.signal_notifier import _truncate_utf8_bytes

        text = "行情趋势延续。"  # 7 个 CJK 字符 × 3 字节 = 21 字节
        self.assertEqual("行情", _truncate_utf8_bytes(text, 6))
        self.assertEqual("行情趋", _truncate_utf8_bytes(text, 9))
        self.assertEqual("行情趋势延", _truncate_utf8_bytes(text, 15))
        self.assertEqual("行情趋势延续", _truncate_utf8_bytes(text, 18))
        self.assertEqual(text, _truncate_utf8_bytes(text, 21))
        self.assertEqual(text, _truncate_utf8_bytes(text, 100))
        self.assertEqual("", _truncate_utf8_bytes(text, 2))
        ascii_text = "abcdef"
        self.assertEqual("abc", _truncate_utf8_bytes(ascii_text, 3))
        mixed = "abc行情"
        self.assertEqual("abc行", _truncate_utf8_bytes(mixed, 6))
        result = _truncate_utf8_bytes(mixed, 7)
        self.assertEqual("abc行", result)
        self.assertLessEqual(len(result.encode("utf-8")), 7)

    def test_bark_application_error_is_failure(self):
        notifier = SignalNotifier(
            {
                "notification": {
                    "bark": {
                        "enabled": True,
                        "url": "https://api.day.app/push",
                        "device_key": "test-device-key",
                    }
                }
            }
        )
        opener = MagicMock()
        opener.open.return_value = _BarkErrorResponse()
        payload = {
            "schema": "quant.signal.v1",
            "event_id": "event-bark-2",
            "side": "sell",
            "symbol": "000001",
            "name": "平安银行",
            "timeframe": "1d",
            "signal_type": "zero_axis_death_cross",
            "price": 11.0,
            "score": 65,
            "confirmed_at": "2025-01-01T10:00:00",
            "risk_notice": "test",
            "evidence": {},
        }
        with patch("notification.signal_notifier.build_opener", return_value=opener):
            success, detail = notifier.send("bark", payload)
        self.assertFalse(success)
        self.assertIn("400", detail)


if __name__ == "__main__":
    unittest.main()
