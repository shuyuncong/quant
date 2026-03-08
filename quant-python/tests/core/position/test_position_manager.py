import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.position.position_manager import PositionManager


class PositionManagerTest(unittest.TestCase):
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
            "manual_overrides": {
                "disable_new_positions": False,
                "max_total_exposure": 1.0,
            },
        }
        self.manager = PositionManager(self.config)

    def test_enforces_symbol_count_constraints(self):
        self.assertFalse(self.manager.validate_symbol_count(1))
        self.assertTrue(self.manager.validate_symbol_count(2))
        self.assertTrue(self.manager.validate_symbol_count(4))
        self.assertFalse(self.manager.validate_symbol_count(5))

    def test_limits_single_stock_exposure(self):
        self.assertAlmostEqual(self.manager.base_exposure_ratio(), 0.25, places=4)
        self.assertAlmostEqual(self.manager.mobile_exposure_ratio(), 0.15, places=4)
        self.assertAlmostEqual(self.manager.total_single_stock_limit(), 0.40, places=4)
        self.assertTrue(self.manager.validate_single_position(0.40))
        self.assertFalse(self.manager.validate_single_position(0.41))

    def test_builds_position_with_base_and_mobile_shares(self):
        position = self.manager.build_position(
            ts_code="000001.SZ",
            name="平安银行",
            total_capital=100000,
            current_price=10.0,
        )

        self.assertEqual(position.base_shares, 2500)
        self.assertEqual(position.mobile_shares, 1500)
        self.assertEqual(position.total_shares, 4000)

    def test_respects_disable_new_position_override(self):
        disabled_manager = PositionManager(
            {
                **self.config,
                "manual_overrides": {
                    "disable_new_positions": True,
                    "max_total_exposure": 0.8,
                },
            }
        )
        self.assertFalse(disabled_manager.can_open_new_position(1))
        self.assertAlmostEqual(disabled_manager.remaining_total_exposure(0.3), 0.5, places=4)


if __name__ == "__main__":
    unittest.main()
