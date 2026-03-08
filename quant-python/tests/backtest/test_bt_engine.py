import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.engine.bt_engine import BacktestEngine
from backtest.engine.china_cost_model import ChinaCostModel
from backtest.strategies.trend_following_bt import TrendFollowingBacktestStrategy


class BacktestEngineTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "strategy": {"technical": {"ma_period": 5}},
            "risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.30},
            "backtest": {
                "initial_cash": 100000,
                "commission_pct": 0.0003,
                "stamp_tax_pct": 0.001,
                "slippage_pct": 0.0005,
                "lot_size": 100,
                "t_plus_one": True,
                "price_limit_model": "conservative",
            },
        }
        self.price_data = pd.DataFrame(
            {
                "datetime": pd.date_range("2025-01-01", periods=15, freq="D"),
                "close": [10, 10.1, 10.2, 10.3, 10.4, 10.8, 11.2, 11.5, 11.3, 11.0, 10.8, 10.6, 10.4, 10.2, 10.0],
                "volume": [1000, 980, 990, 995, 1005, 1200, 1300, 1350, 900, 850, 800, 780, 760, 740, 720],
            }
        )

    def test_cost_model_supports_a_share_constraints(self):
        model = ChinaCostModel(
            commission_pct=0.0003,
            stamp_tax_pct=0.001,
            slippage_pct=0.0005,
            lot_size=100,
            t_plus_one=True,
            price_limit_model="conservative",
        )
        buy = model.estimate_trade(price=10.0, shares=1234, side="BUY")
        sell = model.estimate_trade(price=10.5, shares=1234, side="SELL")

        self.assertEqual(buy.shares, 1200)
        self.assertGreater(sell.stamp_tax, 0)
        self.assertTrue(model.can_sell(pd.Timestamp("2025-01-01"), pd.Timestamp("2025-01-02")))

    def test_backtest_engine_runs_and_can_persist_result(self):
        engine = BacktestEngine(self.config)
        strategy = TrendFollowingBacktestStrategy(self.config)
        signals = strategy.generate_signals(self.price_data)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "bt_result.json"
            result = engine.run(self.price_data, signals, output_path=str(output_path))
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertTrue(Path(result["output_files"]["report_json"]).exists())
            self.assertTrue(Path(result["output_files"]["trades_csv"]).exists())

        self.assertIn(result["engine_backend"], {"internal", "backtesting.py"})
        self.assertIn("summary", result)
        self.assertIn("annual_return", result["summary"])
        self.assertIn("win_rate", result["summary"])
        self.assertIn("profit_loss_ratio", result["summary"])
        self.assertIn("turnover_rate", result["summary"])
        self.assertIn("signal_records", result)
        self.assertIn("rest_api_mapping", result)
        self.assertIn("output_files", result)
        self.assertTrue(saved["summary"]["trade_count"] >= 0)

    def test_trend_following_strategy_backtest_is_runnable(self):
        engine = BacktestEngine(self.config)
        strategy = TrendFollowingBacktestStrategy(self.config)
        signals = strategy.generate_signals(self.price_data)
        result = engine.run(self.price_data, signals)

        self.assertIsInstance(signals, list)
        self.assertIn("max_drawdown", result["summary"])
        self.assertIn("trade_count", result["summary"])
        self.assertIn("avg_holding_days", result["summary"])
        self.assertIn("file_share_bundle", result)
        self.assertIn("regime_breakdown", result)

    def test_supports_multi_strategy_comparison(self):
        engine = BacktestEngine(self.config)
        strategy = TrendFollowingBacktestStrategy(self.config)
        signals = strategy.generate_signals(self.price_data)
        comparison = engine.compare_strategies(
            self.price_data,
            {
                "trend_following": signals,
                "trend_following_clone": list(signals),
            },
            regime_labels=["bull", "bull", "range"],
        )

        self.assertIn("trend_following", comparison["strategies"])
        self.assertIn("best_strategy", comparison)
        self.assertIn("regime_breakdown", comparison)


if __name__ == "__main__":
    unittest.main()
