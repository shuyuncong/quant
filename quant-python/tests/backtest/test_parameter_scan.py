import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.engine.bt_engine import BacktestEngine
from backtest.strategies.trend_following_bt import TrendFollowingBacktestStrategy


class ParameterScanTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "strategy": {"technical": {"ma_period": 5}},
            "risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.20},
            "backtest": {
                "initial_cash": 100000,
                "commission_pct": 0.0003,
                "stamp_tax_pct": 0.001,
                "slippage_pct": 0.0005,
                "lot_size": 100,
                "t_plus_one": True,
                "price_limit_model": "conservative",
                "max_parameter_combinations": 3,
                "max_in_out_sample_gap": 0.15,
            },
        }
        closes = [
            10.0, 10.1, 10.2, 10.3, 10.4, 10.1, 10.0, 10.5, 10.8, 11.0,
            10.7, 10.4, 10.2, 10.0, 9.9, 10.3, 10.6, 10.9, 11.1, 11.3,
            11.0, 10.8, 10.6, 10.4, 10.2, 10.5, 10.9, 11.2, 11.5, 11.7,
        ]
        self.price_data = pd.DataFrame(
            {
                "datetime": pd.date_range("2025-01-01", periods=len(closes), freq="D"),
                "close": closes,
                "volume": [1000 + index * 10 for index in range(len(closes))],
            }
        )

    def test_parameter_scan_supports_in_and_out_of_sample_comparison(self):
        engine = BacktestEngine(self.config)
        result = engine.scan_parameters(
            price_data=self.price_data,
            strategy_cls=TrendFollowingBacktestStrategy,
            param_grid={
                "strategy.technical.ma_period": [3, 5],
                "risk.stop_loss_pct": [0.05, 0.08],
            },
            strategy_name="trend_following",
            split_ratio=0.67,
            score_field="annual_return",
            regime_scope="bull",
        )

        self.assertEqual(result["tested_combinations"], 3)
        self.assertGreater(result["sample_split"]["in_sample_rows"], 0)
        self.assertGreater(result["sample_split"]["out_of_sample_rows"], 0)
        self.assertIsInstance(result["best_params"], dict)
        self.assertEqual(len(result["comparisons"]), 3)
        self.assertIn("comparison", result["comparisons"][0])
        self.assertIn("risk_flags", result["comparisons"][0]["comparison"])


if __name__ == "__main__":
    unittest.main()
