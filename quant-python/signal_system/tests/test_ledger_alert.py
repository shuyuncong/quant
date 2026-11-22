import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ledger_alert


class ApplySettingsTests(unittest.TestCase):
    def test_web_settings_override_yaml_with_decoded_values(self):
        config = {"notification": {"bark": {"enabled": False, "url": "https://yaml.example"}, "timeout_seconds": 10}}
        ledger_alert.apply_settings(config, {
            "notification.bark.enabled": "true",  # Web 端存的是 JSON 文本
            "notification.bark.device_key": '"abc"',
            "notification.webhook.headers.Authorization": '"Bearer x"',
            "notification.email.smtp_server": "smtp.example.com",  # 不是合法 JSON：按原字符串
        })
        notification = config["notification"]
        self.assertIs(notification["bark"]["enabled"], True)
        self.assertEqual(notification["bark"]["device_key"], "abc")
        self.assertEqual(notification["bark"]["url"], "https://yaml.example")
        self.assertEqual(notification["webhook"]["headers"]["Authorization"], "Bearer x")
        self.assertEqual(notification["email"]["smtp_server"], "smtp.example.com")
        self.assertEqual(notification["timeout_seconds"], 10)


class SendTests(unittest.TestCase):
    def run_main(self, settings, deliveries):
        """deliveries: 渠道 → (ok, detail)；返回 (退出码, 发出去的 payload 列表)。"""
        sent = []

        def fake_send(self, channel, payload):
            sent.append((channel, payload))
            return deliveries[channel]

        stdin = type("Stdin", (), {"buffer": __import__("io").BytesIO("行情缓存只到 2026-11-19".encode())})()
        with patch.object(ledger_alert, "read_db_settings", return_value=settings), \
             patch.object(ledger_alert.SignalNotifier, "send", fake_send), \
             patch.object(ledger_alert.sys, "stdin", stdin), \
             patch("builtins.print"):
            code = ledger_alert.main(["--title", "首板低开纸面账本推进失败"])
        return code, sent

    def test_alert_goes_to_enabled_channels_with_title_and_error_text(self):
        code, sent = self.run_main(
            {"notification.bark.enabled": "true", "notification.bark.device_key": '"k"'}, {"bark": (True, "ok")}
        )
        self.assertEqual(code, 0)
        self.assertEqual([channel for channel, _ in sent], ["bark"])
        payload = sent[0][1]
        self.assertEqual(payload["name"], "首板低开纸面账本推进失败")
        self.assertIn("行情缓存只到 2026-11-19", payload["evidence"]["content"])

    def test_no_delivery_is_a_failure(self):
        # 没有启用任何渠道
        self.assertEqual(self.run_main({}, {})[0], 1)
        # 启用了但发送失败
        code, _ = self.run_main({"notification.bark.enabled": "true"}, {"bark": (False, "Bark device_key 未配置")})
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
