import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.regime.market_regime_engine import MarketRegimeEngine
from core.regime.regime_override import RegimeOverride


def build_price_frame(start: float, step: float, size: int = 280) -> pd.DataFrame:
    prices = [start + step * idx for idx in range(size)]
    return pd.DataFrame({"close": prices})


class MarketRegimeEngineTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "regime": {
                "ma_short": 20,
                "ma_long": 250,
                "bull_score_threshold": 0.7,
                "bear_score_threshold": 0.7,
                "range_score_threshold": 0.6,
            },
            "manual_overrides": {"regime_override": "auto"},
        }
        self.engine = MarketRegimeEngine(self.config)

    def test_detects_bull_regime(self):
        decision = self.engine.decide(build_price_frame(100, 0.6))
        self.assertEqual(decision.regime, "bull")
        self.assertGreaterEqual(decision.scores["bull"], 0.7)
        self.assertIn("价格位于长期均线上方", decision.reasons)

    def test_detects_bear_regime(self):
        decision = self.engine.decide(build_price_frame(300, -0.5))
        self.assertEqual(decision.regime, "bear")
        self.assertGreaterEqual(decision.scores["bear"], 0.7)

    def test_detects_range_regime(self):
        prices = [100 + (0.4 if idx % 2 == 0 else -0.4) for idx in range(280)]
        decision = self.engine.decide(pd.DataFrame({"close": prices}))
        self.assertEqual(decision.regime, "range")
        self.assertGreaterEqual(decision.scores["range"], 0.6)

    def test_applies_manual_override(self):
        decision = self.engine.decide(
            build_price_frame(100, 0.6),
            override=RegimeOverride(mode="force_bear", reason="风险事件"),
        )
        self.assertEqual(decision.auto_regime, "bull")
        self.assertEqual(decision.final_regime, "bear")
        self.assertTrue(decision.override["is_overridden"])
        self.assertIn("人工覆盖生效", decision.to_dict()["reason"])


if __name__ == "__main__":
    unittest.main()
