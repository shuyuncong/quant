import os
import sys
import unittest
from unittest.mock import patch


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from data.network_diagnostic import (
    CheckResult,
    build_summary,
    collect_market_config,
    collect_proxy_env,
)


def _checks(**overrides):
    names = (
        "tencent_kline",
        "tencent_fqkline",
        "eastmoney_kline",
        "eastmoney_spot",
        "sina_kline",
        "sina_quote_list",
    )
    return [
        CheckResult(
            name=f"{name}:direct",
            ok=overrides.get(name, True),
            detail="status=200" if overrides.get(name, True) else "status=501",
        )
        for name in names
    ]


class NetworkDiagnosticTests(unittest.TestCase):
    def test_proxy_env_ignores_empty_values_and_market_proxy(self):
        with patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": "http://127.0.0.1:7890",
                "HTTP_PROXY": "",
                "MARKET_DATA_PROXY": "http://proxy.example:7890",
            },
            clear=True,
        ):
            env = collect_proxy_env()
            self.assertEqual("http://127.0.0.1:7890", env.get("HTTPS_PROXY"))
            self.assertNotIn("HTTP_PROXY", env)
            self.assertNotIn("MARKET_DATA_PROXY", env)
            self.assertTrue(all(value.strip() for value in env.values()))

    def test_market_config_reports_only_non_empty_overrides(self):
        with patch.dict(
            os.environ,
            {
                "MARKET_DATA_PROVIDER": "",
                "MARKET_DATA_FALLBACKS": "sina",
            },
            clear=True,
        ):
            self.assertEqual(
                {"MARKET_DATA_FALLBACKS": "sina"},
                collect_market_config(),
            )

    def test_eastmoney_blocked_message_asks_for_sina_when_not_configured(self):
        summary = build_summary(
            _checks(eastmoney_kline=False, eastmoney_spot=False),
            {},
            "",
            {},
        )
        self.assertTrue(
            any("MARKET_DATA_FALLBACKS=sina" in item for item in summary["diagnosis"])
        )

    def test_eastmoney_blocked_message_is_quiet_when_sina_already_configured(self):
        summary = build_summary(
            _checks(eastmoney_kline=False, eastmoney_spot=False),
            {},
            "",
            {"MARKET_DATA_FALLBACKS": "sina"},
        )
        joined = "\n".join(summary["diagnosis"])
        self.assertIn("已含 sina", joined)
        self.assertNotIn("设 MARKET_DATA_FALLBACKS=sina 让备用链接管", joined)

    def test_tencent_blocked_message_is_quiet_when_sina_is_primary(self):
        summary = build_summary(
            _checks(tencent_kline=False, tencent_fqkline=False),
            {},
            "",
            {"MARKET_DATA_PROVIDER": "sina"},
        )
        self.assertTrue(
            any("Sina 已是主源" in item for item in summary["diagnosis"])
        )
