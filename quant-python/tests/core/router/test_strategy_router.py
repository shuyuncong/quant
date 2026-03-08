import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.router.strategy_router import StrategyRouter


class StrategyRouterTest(unittest.TestCase):
    def setUp(self):
        self.router = StrategyRouter()
        self.stock = {
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "current_price": 10.5,
            "selection_score": 72,
            "divergence": "bullish",
            "near_ma250": True,
            "price_change_pct": -0.01,
            "volume_ratio": 1.8,
            "ma250_slope": 0.02,
            "bear_trap": True,
            "is_above_ma250": True,
            "close_above_recent_high": True,
        }

    def test_routes_range_to_mean_reversion_family(self):
        signals = self.router.route_signals("range", [self.stock], [])
        self.assertTrue(any(signal["strategy_name"] == "mean_reversion" for signal in signals))

    def test_routes_bear_to_defensive(self):
        signals = self.router.route_signals("bear", [self.stock], [])
        self.assertEqual(signals[0]["strategy_name"], "defensive")

    def test_resolves_conflict_by_action_priority(self):
        signals = self.router.resolve_conflicts(
            [
                {"ts_code": "000001.SZ", "signal_type": "BUY", "score": 95},
                {"ts_code": "000001.SZ", "signal_type": "SELL", "score": 70},
            ]
        )
        self.assertEqual(signals[0]["signal_type"], "SELL")


if __name__ == "__main__":
    unittest.main()
