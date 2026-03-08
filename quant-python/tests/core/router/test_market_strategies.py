import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.router.strategies import BreakoutStrategy, DefensiveStrategy, MeanReversionStrategy, TrendFollowingStrategy


class MarketStrategiesTest(unittest.TestCase):
    def setUp(self):
        self.base_stock = {
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "current_price": 10.5,
            "selection_score": 70,
            "divergence": "bullish",
            "near_ma250": True,
            "price_change_pct": -0.01,
            "volume_ratio": 1.8,
            "ma250_slope": 0.02,
            "bear_trap": True,
            "is_above_ma250": True,
            "close_above_recent_high": True,
        }

    def test_mean_reversion_strategy_supports_range(self):
        signal = MeanReversionStrategy().generate(self.base_stock)
        self.assertEqual(signal["strategy_name"], "mean_reversion")
        self.assertIn(signal["signal_type"], {"BUY", "ADD"})

    def test_trend_following_strategy_supports_bull_market(self):
        signal = TrendFollowingStrategy().generate(self.base_stock, market_status="bull")
        self.assertEqual(signal["strategy_name"], "trend_following")
        self.assertEqual(signal["signal_type"], "BUY")

    def test_defensive_strategy_supports_bear(self):
        signal = DefensiveStrategy().generate(self.base_stock)
        self.assertEqual(signal["strategy_name"], "defensive")
        self.assertEqual(signal["signal_type"], "BUY")

    def test_breakout_strategy_supports_breakout_setup(self):
        signal = BreakoutStrategy().generate(self.base_stock)
        self.assertEqual(signal["strategy_name"], "breakout")
        self.assertEqual(signal["signal_type"], "BUY")


if __name__ == "__main__":
    unittest.main()
