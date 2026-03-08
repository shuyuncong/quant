import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.risk.risk_manager import RiskManager


class RiskManagerTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "risk": {
                "stop_loss_pct": 0.08,
                "stop_profit_pct": 0.30,
                "max_portfolio_drawdown_pct": 0.20,
                "max_single_day_drawdown_pct": 0.02,
                "allow_new_position_when_drawdown_exceeded": False,
            },
            "manual_overrides": {
                "disable_new_positions": False,
                "only_reduce_positions": False,
                "max_total_exposure": 1.0,
            },
            "position": {"max_position_per_stock": 0.40},
        }
        self.manager = RiskManager(self.config)

    def test_supports_stop_loss_and_stop_profit(self):
        stop_loss = self.manager.evaluate_position(
            profit_pct=-0.10,
            tech_result={"is_above_ma250": True, "ma250_slope": 0.01},
            market_status="range",
        )
        self.assertEqual(stop_loss.action, "SELL")
        self.assertIn("stop_loss", stop_loss.risk_flags)

        take_profit = self.manager.evaluate_position(
            profit_pct=0.35,
            tech_result={"is_above_ma250": True, "ma250_slope": 0.01},
            market_status="range",
        )
        self.assertEqual(take_profit.action, "REDUCE")
        self.assertIn("take_profit", take_profit.risk_flags)

    def test_supports_portfolio_drawdown_constraint(self):
        decision = self.manager.evaluate_portfolio(
            {
                "portfolio_drawdown_pct": 0.25,
                "single_day_drawdown_pct": 0.01,
                "current_exposure_pct": 0.6,
            }
        )
        self.assertFalse(decision.allowed)
        self.assertIn("portfolio_drawdown_limit", decision.risk_flags)

    def test_supports_disable_new_positions_switch(self):
        manager = RiskManager(
            {
                **self.config,
                "manual_overrides": {
                    "disable_new_positions": True,
                    "only_reduce_positions": False,
                    "max_total_exposure": 1.0,
                },
            }
        )
        decision = manager.evaluate_portfolio({"current_exposure_pct": 0.2})
        self.assertFalse(decision.allowed)
        self.assertIn("manual_disable_new_positions", decision.risk_flags)


if __name__ == "__main__":
    unittest.main()
