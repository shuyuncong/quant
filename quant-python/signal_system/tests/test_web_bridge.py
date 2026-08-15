"""Tests for the web console bridge (no network access required)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock


import web_bridge


class NormalizeTests(unittest.TestCase):
    def test_mixed_separators_with_names(self):
        text = "600036 招商银行,600519.SH,000001.SZ 平安银行\nSH600036\n招商银行(600000)"
        data = web_bridge._parse_normalize(text)
        self.assertEqual(
            [item["symbol"] for item in data["symbols"]],
            ["600036.SH", "600519.SH", "000001.SZ", "600000.SH"],
        )
        self.assertEqual(data["symbols"][0]["name"], "招商银行")
        self.assertEqual(data["symbols"][1]["name"], "")
        self.assertEqual(data["symbols"][2]["name"], "平安银行")
        self.assertEqual(data["symbols"][3]["name"], "招商银行")
        self.assertEqual(data["unknown"], [])

    def test_dedupe_and_unknown(self):
        text = "600036 招商银行，600036，zzzz，平安银行"
        data = web_bridge._parse_normalize(text)
        self.assertEqual([item["symbol"] for item in data["symbols"]], ["600036.SH"])
        self.assertEqual(data["unknown"], ["zzzz", "平安银行"])

    def test_multiple_codes_without_names(self):
        data = web_bridge._parse_normalize("600036 600519\n000001.SZ 600000.SH")
        self.assertEqual(
            [item["symbol"] for item in data["symbols"]],
            ["600036.SH", "600519.SH", "000001.SZ", "600000.SH"],
        )
        self.assertTrue(all(item["name"] == "" for item in data["symbols"]))


class ConfigTests(unittest.TestCase):
    def test_config_masks_secrets_and_merges_overrides(self):
        overrides = {
            "scan": {"universe_mode": "all_a"},
            "notification": {"email": {"enabled": True}},
        }
        result = web_bridge._cmd_config(web_bridge._default_config_path(), overrides)
        self.assertEqual(result, 0)

    def test_env_marker_resolved(self):
        overrides = {
            "notification": {"email": {"password": {web_bridge.ENV_MARKER: "BRIDGE_TEST_PW"}}}
        }
        with mock.patch.dict(os.environ, {"BRIDGE_TEST_PW": "s3cret"}, clear=False):
            config = web_bridge._load_effective_config(web_bridge._default_config_path(), overrides)
            sources = web_bridge._secret_sources(
                web_bridge.load_config(web_bridge._default_config_path()), overrides
            )
        self.assertEqual(config["notification"]["email"]["password"], "s3cret")
        self.assertEqual(sources["notification.email.password"], "env")


class CalendarTests(unittest.TestCase):
    def test_fallback_when_market_unavailable(self):
        with mock.patch.object(web_bridge, "_make_monitor", side_effect=RuntimeError("no network")):
            with mock.patch(
                "web_bridge.now_shanghai",
                return_value=__import__("datetime").datetime(2026, 8, 15, 10, 0),
            ):
                result = web_bridge._cmd_calendar(web_bridge._default_config_path(), {})
        self.assertEqual(result, 0)


class ErrorPathTests(unittest.TestCase):
    def test_analyze_requires_symbols(self):
        result = web_bridge._cmd_analyze(web_bridge._default_config_path(), {})
        self.assertEqual(result, 2)

    def test_invalid_payload_json(self):
        with self.assertRaises(ValueError):
            web_bridge._read_payload("not-json")

    def test_unknown_command_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            web_bridge.main(["no-such-command"])
        self.assertEqual(ctx.exception.code, 2)


class OutboxSummaryTests(unittest.TestCase):
    def test_empty_db_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = __import__("storage.signal_store", fromlist=["SignalStore"]).SignalStore(
                os.path.join(tmp, "test.db")
            )
            summary = store.outbox_summary()
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["delivered"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["total_events"], 0)


if __name__ == "__main__":
    unittest.main()
