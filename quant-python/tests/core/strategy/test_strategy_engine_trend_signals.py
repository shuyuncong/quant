import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
SIGNAL_SYSTEM_ROOT = ROOT / "signal_system"
for candidate in (str(ROOT), str(SIGNAL_SYSTEM_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from strategy.strategy_engine import StrategyEngine


class FakeDataFetcher:
    def __init__(self):
        self.latest_trade_date_calls = 0
        self.latest_report_period_calls = 0
        self.price_map = {
            "HOLD_RED": self._build_df("HOLD_RED", [10.0] * 95 + [11.0, 11.2, 11.1, 11.3, 11.4]),
            "HOLD_SELL": self._build_df("HOLD_SELL", [10.0] * 95 + [9.2, 9.0, 8.8, 8.7, 8.5]),
            "SELECT": self._build_df("SELECT", [10.0 + i * 0.02 for i in range(320)]),
        }

    @staticmethod
    def _build_df(ts_code, closes):
        df = pd.DataFrame({"close": closes})
        df.attrs["ts_code"] = ts_code
        return df

    def get_daily_data(self, ts_code, period=100):
        return self.price_map[ts_code]

    def get_financial_data(self, ts_code, period=None):
        del ts_code, period
        return {"roe": 18.0, "debt_to_assets": 35.0}

    def get_daily_basic(self, ts_code, trade_date=None):
        del ts_code, trade_date
        return {"turnover_rate": 1.6, "pe": 18.0, "total_mv": 1800000}

    def get_latest_trade_date(self):
        self.latest_trade_date_calls += 1
        return "20260306"

    def _get_latest_report_period(self):
        self.latest_report_period_calls += 1
        return "20250930"


class FakeTechnicalIndicators:
    def analyze_stock_technical(self, df, *args, **kwargs):
        ts_code = df.attrs["ts_code"]
        if ts_code == "HOLD_RED":
            return {
                "current_price": 11.4,
                "ma250": 10.8,
                "ma250_slope": 0.01,
                "distance_to_ma250": 0.02,
                "near_ma250": False,
                "macd": 0.3,
                "macd_signal": 0.35,
                "macd_hist": -0.05,
                "divergence": "bearish",
                "volume_ratio": 1.1,
                "is_above_ma250": True,
                "macd_golden_cross": False,
                "macd_death_cross": False,
            }
        return {
            "current_price": 8.5,
            "ma250": 9.3,
            "ma250_slope": -0.02,
            "distance_to_ma250": 0.08,
            "near_ma250": False,
            "macd": -0.6,
            "macd_signal": -0.4,
            "macd_hist": -0.2,
            "divergence": "none",
            "volume_ratio": 1.9,
            "is_above_ma250": False,
            "macd_golden_cross": False,
            "macd_death_cross": True,
        }


class StrategyEngineTrendSignalsTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "strategy": {
                "fundamental": {
                    "min_roe": 10,
                    "max_debt_ratio": 50,
                    "max_pe": 30,
                    "min_market_cap": 50,
                    "max_market_cap": 500,
                },
                "volume": {
                    "min_turnover_rate": 1,
                    "max_turnover_rate": 5,
                    "volume_burst_ratio": 1.5,
                },
                "candidate_pool_size": 30,
            },
            "regime": {"ma_long": 250},
            "position": {
                "min_stocks": 2,
                "target_stocks": 3,
                "max_stocks": 4,
                "base_position_per_stock": 0.25,
                "mobile_cash_ratio": 0.25,
                "max_position_per_stock": 0.40,
            },
            "backtest": {"lot_size": 100},
            "risk": {
                "stop_loss_pct": 0.08,
                "stop_profit_pct": 0.30,
                "max_portfolio_drawdown_pct": 0.20,
                "max_single_day_drawdown_pct": 0.02,
                "allow_new_position_when_drawdown_exceeded": False,
            },
            "risk_control": {
                "stop_loss": 0.08,
                "stop_profit": 0.30,
            },
            "manual_overrides": {
                "disable_new_positions": False,
                "max_total_exposure": 1.0,
            },
            "t_trading": {
                "enabled": True,
                "positive_t_step_pct": 0.05,
                "negative_t_step_pct": 0.05,
                "range_t_step_pct": 0.05,
            },
        }
        self.engine = StrategyEngine(
            config=self.config,
            data_fetcher=FakeDataFetcher(),
            technical_indicators=FakeTechnicalIndicators(),
        )

    def test_generates_buy_signal_for_unheld_stock(self):
        signal = self.engine.generate_buy_signals(
            analyzed_stocks=[
                {
                    "ts_code": "BUY",
                    "name": "买入标的",
                    "current_price": 10.5,
                    "ma250_slope": 0.03,
                    "near_ma250": True,
                    "is_above_ma250": True,
                    "divergence": "bullish",
                    "bear_trap": True,
                    "macd_golden_cross": True,
                    "volume_ratio": 1.8,
                    "price_change_pct": -0.01,
                    "selection_score": 80,
                    "roe": 15,
                    "pe": 18,
                    "market_cap": 180,
                }
            ],
            market_status="bull",
            positions=[],
        )[0]

        self.assertEqual(signal["signal_type"], "BUY")
        self.assertGreater(signal["suggested_position_change"], 0)
        self.assertIn("年线向上", signal["explanation"])

    def test_generates_add_signal_for_held_stock(self):
        signal = self.engine.generate_buy_signals(
            analyzed_stocks=[
                {
                    "ts_code": "ADD",
                    "name": "加仓标的",
                    "current_price": 12.0,
                    "ma250_slope": 0.02,
                    "near_ma250": True,
                    "is_above_ma250": True,
                    "divergence": "bullish",
                    "bear_trap": False,
                    "macd_golden_cross": True,
                    "volume_ratio": 1.6,
                    "price_change_pct": -0.02,
                    "selection_score": 78,
                    "roe": 13,
                    "pe": 16,
                    "market_cap": 220,
                }
            ],
            market_status="bull",
            positions=[{"ts_code": "ADD", "buy_price": 11.0}],
        )[0]

        self.assertEqual(signal["signal_type"], "ADD")
        self.assertEqual(signal["action"], "加仓")

    def test_generates_reduce_and_sell_exit_signals(self):
        signals, risk_alerts = self.engine.check_positions_for_sell(
            positions=[
                {"ts_code": "HOLD_RED", "name": "减仓标的", "buy_price": 9.5, "base_shares": 1000, "mobile_shares": 500},
                {"ts_code": "HOLD_SELL", "name": "卖出标的", "buy_price": 10.0, "base_shares": 1000, "mobile_shares": 0},
            ],
            market_status="bull",
        )

        signal_map = {signal["ts_code"]: signal for signal in signals}
        self.assertEqual(signal_map["HOLD_RED"]["signal_type"], "REDUCE")
        self.assertEqual(signal_map["HOLD_SELL"]["signal_type"], "SELL")
        self.assertLess(signal_map["HOLD_SELL"]["suggested_position_change"], 0)
        risk_map = {signal["ts_code"]: signal for signal in risk_alerts}
        self.assertEqual(risk_map["HOLD_SELL"]["signal_type"], "SELL")

    def test_generates_t_signals_for_positions(self):
        t_signals = self.engine.generate_t_signals(
            positions=[
                {"ts_code": "HOLD_RED", "name": "做T标的", "buy_price": 9.5, "base_shares": 1000, "mobile_shares": 500},
            ],
            market_status="bull",
        )
        self.assertEqual(t_signals[0]["signal_type"], "positive_t_sell")


    def test_build_selection_inputs_reuses_shared_runtime_context(self):
        stock_list = pd.DataFrame([{"ts_code": "SELECT", "name": "Selector Sample"}])

        selection_inputs = self.engine.build_selection_inputs(stock_list)

        self.assertEqual(len(selection_inputs), 1)
        self.assertEqual(selection_inputs[0]["ts_code"], "SELECT")
        self.assertEqual(self.engine.data_fetcher.latest_trade_date_calls, 1)
        self.assertEqual(self.engine.data_fetcher.latest_report_period_calls, 1)


if __name__ == "__main__":
    unittest.main()
