from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from models import TimeframeReport
from strategy.multi_timeframe import MultiTimeframeAnalyzer
from strategy.signal_policy import (
    effective_signal_execution_mode,
    partition_entry_signals,
    resolve_signal_execution_policy,
    signal_execution_mode,
    signal_execution_mode_with_regime,
)


class SignalExecutionPolicyTests(unittest.TestCase):
    def test_missing_policy_preserves_legacy_enabled_behavior(self):
        policy = resolve_signal_execution_policy({})

        self.assertEqual("enabled", policy["default"])
        self.assertEqual("enabled", signal_execution_mode("buy_2", policy))

    def test_resolves_configured_signal_modes(self):
        policy = resolve_signal_execution_policy(
            {
                "signal_strategy": {
                    "execution_policy": {
                        "default": "enabled",
                        "signals": {
                            "buy_2": "observe_only",
                            "buy_3": "disabled",
                        },
                    }
                }
            }
        )

        self.assertEqual("observe_only", signal_execution_mode("buy_2", policy))
        self.assertEqual("disabled", signal_execution_mode("buy_3", policy))
        self.assertEqual("enabled", signal_execution_mode("buy_1", policy))

    def test_invalid_mode_fails_closed_instead_of_silently_enabling(self):
        with self.assertRaisesRegex(ValueError, "buy_2"):
            resolve_signal_execution_policy(
                {
                    "signal_strategy": {
                        "execution_policy": {
                            "signals": {"buy_2": "maybe"},
                        }
                    }
                }
            )

    def test_enabled_component_wins_over_observe_only_component(self):
        policy = resolve_signal_execution_policy(
            {
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {"buy_2": "observe_only"},
                    }
                }
            }
        )

        self.assertEqual(
            "enabled",
            effective_signal_execution_mode(
                ["buy_2", "macd_golden_cross_pullback_confirmed_above"],
                policy,
            ),
        )

    def test_partition_keeps_observation_details_out_of_executable_signals(self):
        policy = resolve_signal_execution_policy(
            {
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {
                            "buy_2": "observe_only",
                            "buy_3": "disabled",
                        },
                    }
                }
            }
        )
        events = [
            {"day": "2026-01-05", "signal_type": "buy_1"},
            {"day": "2026-01-06", "signal_type": "buy_2"},
            {"day": "2026-01-07", "signal_type": "buy_3"},
        ]

        executable, observed, disabled = partition_entry_signals(events, policy)

        self.assertEqual(["buy_1"], [item["signal_type"] for item in executable])
        self.assertEqual(["buy_2"], [item["signal_type"] for item in observed])
        self.assertEqual("observe_only", observed[0]["execution_mode"])
        self.assertEqual(["buy_3"], [item["signal_type"] for item in disabled])
        self.assertEqual("disabled", disabled[0]["execution_mode"])

    def test_regime_override_can_observe_above_cross_in_range(self):
        policy = resolve_signal_execution_policy(
            {
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {
                            "macd_golden_cross_pullback_confirmed_above": "enabled",
                        },
                        "by_regime": {
                            "bull": {
                                "macd_golden_cross_pullback_confirmed_above": "enabled",
                            },
                            "range": {
                                "macd_golden_cross_pullback_confirmed_above": "observe_only",
                            },
                            "bear": {
                                "macd_golden_cross_pullback_confirmed_above": "observe_only",
                            },
                        },
                    }
                }
            }
        )
        signal = "macd_golden_cross_pullback_confirmed_above"
        self.assertEqual("enabled", signal_execution_mode_with_regime(signal, policy, "bull"))
        self.assertEqual("observe_only", signal_execution_mode_with_regime(signal, policy, "range"))
        self.assertEqual("observe_only", signal_execution_mode_with_regime(signal, policy, "bear"))

    def test_regime_override_cannot_loosen_static_policy(self):
        policy = resolve_signal_execution_policy(
            {
                "signal_strategy": {
                    "execution_policy": {
                        "signals": {
                            "macd_golden_cross_pullback_confirmed_above": "observe_only",
                        },
                        "by_regime": {
                            "bull": {
                                "macd_golden_cross_pullback_confirmed_above": "enabled",
                            }
                        },
                    }
                }
            }
        )
        self.assertEqual(
            "observe_only",
            signal_execution_mode_with_regime(
                "macd_golden_cross_pullback_confirmed_above", policy, "bull"
            ),
        )

    def test_multi_timeframe_events_apply_regime_override(self):
        analyzer = MultiTimeframeAnalyzer(
            {
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
                }
            }
        )
        report = TimeframeReport(
            timeframe="1d",
            status="ok",
            latest_time="2026-01-01T15:00:00",
            latest_price=10.0,
            indicators={
                "golden_cross_entry_ready": True,
                "golden_cross_entry_zone": "above",
            },
            chan={},
        )

        bull = analyzer._events("000001", "样本", report, "buy", 70, [], "bull")
        ranged = analyzer._events("000001", "样本", report, "buy", 70, [], "range")

        self.assertTrue(bull[0].evidence["actionable"])
        self.assertEqual("enabled", bull[0].evidence["execution_mode"])
        self.assertFalse(ranged[0].evidence["actionable"])
        self.assertEqual("watch", ranged[0].evidence["signal_level"])
        self.assertEqual("observe_only", ranged[0].evidence["execution_mode"])


if __name__ == "__main__":
    unittest.main()
