import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.reports.performance_report import PerformanceReportBuilder
from backtest.reports.records import StrategyConfigSnapshot


class PerformanceReportBuilderTest(unittest.TestCase):
    def test_builds_standardized_report_and_export_bundle(self):
        builder = PerformanceReportBuilder()
        run_id = "bt_test_001"
        report = builder.build_report(
            run_id=run_id,
            strategy_name="trend_following",
            regime_scope="bull",
            start_date="2025-01-01T00:00:00",
            end_date="2025-01-10T00:00:00",
            summary={
                "initial_cash": 100000,
                "ending_equity": 108500,
                "total_return": 0.085,
                "annual_return": 0.20,
                "max_drawdown": 0.05,
                "trade_count": 1,
                "win_rate": 1.0,
                "profit_loss_ratio": 2.0,
                "avg_holding_days": 5,
            },
            raw_trades=[
                {
                    "datetime": "2025-01-02T00:00:00",
                    "notional": 50000,
                    "ts_code": "000001.SZ",
                    "side": "BUY",
                },
                {
                    "datetime": "2025-01-07T00:00:00",
                    "notional": 55000,
                    "ts_code": "000001.SZ",
                    "side": "SELL",
                },
            ],
            closed_trades=[
                {
                    "ts_code": "000001.SZ",
                    "entry_time": "2025-01-02T00:00:00",
                    "exit_time": "2025-01-07T00:00:00",
                    "entry_price": 10.0,
                    "exit_price": 11.0,
                    "shares": 5000,
                    "pnl": 4500,
                    "pnl_ratio": 0.09,
                    "holding_days": 5,
                    "side": "LONG",
                    "regime": "bull",
                    "strategy_name": "trend_following",
                    "entry_reason": "站上均线",
                    "exit_reason": "止盈",
                }
            ],
            equity_curve=[
                {"datetime": "2025-01-01T00:00:00", "equity": 100000, "cash": 100000, "shares": 0},
                {"datetime": "2025-01-02T00:00:00", "equity": 101500, "cash": 50000, "shares": 5000},
                {"datetime": "2025-01-07T00:00:00", "equity": 108500, "cash": 108500, "shares": 0},
            ],
            signals=[
                {
                    "datetime": "2025-01-02T00:00:00",
                    "ts_code": "000001.SZ",
                    "name": "平安银行",
                    "market_status": "bull",
                    "strategy_name": "trend_following",
                    "signal_type": "BUY",
                    "action": "BUY",
                    "suggested_position_change": 0.5,
                    "reason": "站上均线",
                }
            ],
            config_snapshot=StrategyConfigSnapshot.from_config(
                strategy_name="trend_following",
                config={"strategy": {"technical": {"ma_period": 5}}},
            ),
            cost_model={"commission_pct": 0.0003},
            engine_backend="internal",
            ts_code="000001.SZ",
            ending_position={
                "snapshot_time": "2025-01-10T00:00:00",
                "ts_code": "000001.SZ",
                "shares": 1000,
                "avg_price": 10.2,
                "current_price": 10.8,
            },
        )

        self.assertEqual(report["run"]["run_id"], run_id)
        self.assertIn("turnover_rate", report["metrics"])
        self.assertIn("signal_hit_rate", report["metrics"])
        self.assertEqual(report["regime_breakdown"]["bull"]["trade_count"], 1)
        self.assertEqual(report["positions"][0]["ts_code"], "000001.SZ")
        self.assertIn(f"/api/backtests/runs/{run_id}/trades", report["rest_api_mapping"]["resources"])

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"
            bundle = builder.export_bundle(report, str(output_path))

            self.assertTrue(Path(bundle["report_json"]).exists())
            self.assertTrue(Path(bundle["trades_csv"]).exists())
            self.assertTrue(Path(bundle["signals_json"]).exists())
            self.assertTrue(Path(bundle["positions_json"]).exists())


if __name__ == "__main__":
    unittest.main()
