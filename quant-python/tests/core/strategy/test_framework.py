import unittest

from core.strategy.framework import build_config_snapshot, resolve_strategy_framework


class StrategyFrameworkConfigTests(unittest.TestCase):
    def test_missing_metadata_keeps_p0_compatible_defaults(self):
        result = resolve_strategy_framework({})

        self.assertEqual("four-layer-v1", result["version"])
        self.assertEqual("P0", result["profile"])
        self.assertFalse(result["selection_layers"]["fundamental"])
        self.assertTrue(result["selection_layers"]["volume"])
        self.assertTrue(result["effective"]["risk_enabled"])

    def test_existing_historical_switch_is_reflected_when_metadata_is_absent(self):
        result = resolve_strategy_framework(
            {"backtest": {"fundamental": {"enabled": True}}}
        )

        self.assertFalse(result["selection_layers"]["fundamental"])
        self.assertTrue(result["effective"]["fundamental_enabled"])

    def test_runtime_switch_overrides_declared_fundamental_intent(self):
        result = resolve_strategy_framework(
            {
                "strategy": {
                    "framework": {
                        "selection_layers": {"fundamental": False}
                    }
                },
                "backtest": {"fundamental": {"enabled": True}},
            }
        )

        self.assertFalse(result["declared"]["selection_layers"]["fundamental"])
        self.assertTrue(result["effective"]["fundamental_enabled"])

    def test_unknown_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "version must be"):
            resolve_strategy_framework(
                {"strategy": {"framework": {"version": "four-layer-v2"}}}
            )

    def test_unknown_framework_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            resolve_strategy_framework(
                {"strategy": {"framework": {"volum_layers": {}}}}
            )

    def test_invalid_dataset_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "dataset_role"):
            resolve_strategy_framework(
                {"strategy": {"framework": {"dataset_role": "holdout"}}}
            )

    def test_config_snapshot_is_redacted_and_deterministic(self):
        config = {
            "market_data": {"tushare_token": "secret-token"},
            "notification": {"webhook_url": "https://example.invalid"},
            "strategy": {"framework": {"profile": "P0"}},
        }

        first = build_config_snapshot(config)
        second = build_config_snapshot(config)

        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual("<redacted>", first["config"]["market_data"]["tushare_token"])
        self.assertEqual("<redacted>", first["config"]["notification"]["webhook_url"])

    def test_explicit_framework_metadata_is_preserved(self):
        result = resolve_strategy_framework(
            {
                "strategy": {
                    "framework": {
                        "version": "four-layer-v1",
                        "profile": "research-ma",
                        "selection_layers": {
                            "fundamental": True,
                            "volume": False,
                            "technical": True,
                        },
                        "execution_layers": {
                            "regime": True,
                            "position": True,
                            "risk": False,
                            "t_trading": False,
                        },
                    }
                }
            }
        )

        self.assertEqual("four-layer-v1", result["version"])
        self.assertEqual("research-ma", result["profile"])
        self.assertTrue(result["selection_layers"]["fundamental"])
        self.assertFalse(result["selection_layers"]["volume"])
        self.assertFalse(result["execution_layers"]["risk"])

    def test_unwired_layer_flags_are_reported_separately_from_runtime(self):
        result = resolve_strategy_framework(
            {
                "strategy": {
                    "framework": {
                        "selection_layers": {
                            "volume": False,
                            "technical": False,
                        },
                        "execution_layers": {
                            "risk": False,
                            "t_trading": False,
                        },
                    }
                },
                "entry_filters": {"market_gate_enabled": True},
                "stock_pool": {"enabled": True},
            }
        )

        self.assertFalse(result["declared"]["selection_layers"]["volume"])
        self.assertTrue(result["runtime_switches"]["volume"])
        self.assertIn("volume", result["unsupported_layer_flags"])
        self.assertIn("risk", result["declaration_mismatches"])

    def test_non_boolean_layer_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "selection_layers.volume"):
            resolve_strategy_framework(
                {
                    "strategy": {
                        "framework": {
                            "selection_layers": {"volume": "yes"}
                        }
                    }
                }
            )
