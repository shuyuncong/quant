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
