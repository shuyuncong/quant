import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.position.position_manager import PositionManager
from core.position.t_trading import TTradingStrategy


class TTradingStrategyTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "position": {
                "min_stocks": 2,
                "target_stocks": 3,
                "max_stocks": 4,
                "base_position_per_stock": 0.25,
                "mobile_cash_ratio": 0.25,
                "max_position_per_stock": 0.40,
            },
            "backtest": {"lot_size": 100},
            "manual_overrides": {"disable_new_positions": False, "max_total_exposure": 1.0},
            "t_trading": {
                "enabled": True,
                "positive_t_step_pct": 0.05,
                "negative_t_step_pct": 0.05,
                "range_t_step_pct": 0.05,
            },
        }
        self.strategy = TTradingStrategy(PositionManager(self.config), self.config)
        self.position = {"ts_code": "000001.SZ", "base_shares": 1000, "mobile_shares": 500}

    def test_supports_positive_t(self):
        signal = self.strategy.analyze_t_opportunity(
            self.position,
            "bull",
            {"divergence": "bullish", "near_ma250": True, "volume_ratio": 1.2},
        )
        self.assertEqual(signal["signal_type"], "positive_t_buy")
        self.assertGreater(signal["suggested_position_change"], 0)

    def test_supports_negative_t(self):
        signal = self.strategy.analyze_t_opportunity(
            self.position,
            "bear",
            {"divergence": "bearish", "macd_golden_cross": False},
        )
        self.assertEqual(signal["signal_type"], "negative_t_sell")
        self.assertLess(signal["suggested_position_change"], 0)

    def test_supports_range_t(self):
        signal = self.strategy.analyze_t_opportunity(
            self.position,
            "range",
            {"divergence": "bullish", "price_change_pct": -0.01},
        )
        self.assertEqual(signal["signal_type"], "range_t_buy")


if __name__ == "__main__":
    unittest.main()
