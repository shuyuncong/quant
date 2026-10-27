import json
import unittest

from strategy.regime_guidance import build_regime_guidance


class RegimeGuidanceTests(unittest.TestCase):
    def test_bear_prefers_divergence_and_avoids_yearline(self):
        guidance = build_regime_guidance("bear")

        self.assertEqual("bear", guidance["regime"])
        self.assertEqual("macd_divergence", guidance["primary_strategy"])
        self.assertEqual("macd_zero_axis", guidance["secondary_strategy"])
        self.assertIn("yearline_pullback", guidance["avoid_strategies"])
        self.assertEqual("low", guidance["confidence"])
        self.assertTrue(guidance["read_only"])
        self.assertIn("2 笔", " ".join(guidance["caveats"]) + " ".join(guidance["evidence"]))

    def test_each_regime_is_json_serializable(self):
        for regime in ("bull", "range", "bear", "unknown"):
            payload = build_regime_guidance(regime)
            self.assertEqual(regime, payload["regime"])
            json.dumps(payload, ensure_ascii=False)

    def test_unknown_regime_has_no_recommendation(self):
        guidance = build_regime_guidance("sideways")

        self.assertEqual("unknown", guidance["regime"])
        self.assertIsNone(guidance["primary_strategy"])
        self.assertEqual("none", guidance["confidence"])

    def test_payload_is_copy_and_cannot_mutate_the_template(self):
        first = build_regime_guidance("bear")
        first["evidence"].append("mutated")
        second = build_regime_guidance("bear")

        self.assertNotIn("mutated", second["evidence"])


if __name__ == "__main__":
    unittest.main()
