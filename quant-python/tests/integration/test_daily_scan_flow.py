import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SIGNAL_SYSTEM_ROOT = ROOT / "signal_system"
for candidate in (str(ROOT), str(SIGNAL_SYSTEM_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from notification.notifier import NotificationService
from strategy.strategy_engine import StrategyEngine


class IntegrationDataFetcher:
    def __init__(self, turnover_rate=2.0):
        self.turnover_rate = turnover_rate

    def get_stock_list(self):
        return pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行"}])

    def get_financial_data(self, ts_code, period=None):
        del ts_code, period
        return {"roe": 12.5, "debt_to_assets": 42.0}

    def get_daily_basic(self, ts_code, trade_date=None):
        del ts_code, trade_date
        return {"pe": 8.5, "total_mv": 2200000, "turnover_rate": self.turnover_rate}

    def get_daily_data(self, ts_code, period=300):
        closes = [10 + idx * 0.03 for idx in range(295)] + [18.7, 18.5, 18.4, 18.6, 18.8]
        turnover = [self.turnover_rate] * len(closes)
        df = pd.DataFrame({"close": closes, "turnover_rate": turnover, "volume": [1000] * len(closes)})
        df.attrs["ts_code"] = ts_code
        return df

    def get_index_daily(self, ts_code, period=300):
        closes = [3000 + idx * 3 for idx in range(period)]
        return pd.DataFrame({"close": closes})

    def get_latest_trade_date(self):
        return "20260306"

    def _get_latest_report_period(self):
        return "20250930"


class IntegrationTechnicalIndicators:
    def analyze_stock_technical(self, df, *args, **kwargs):
        return {
            "current_price": 18.8,
            "ma250": 18.5,
            "ma250_slope": 0.02,
            "distance_to_ma250": 0.016,
            "near_ma250": True,
            "macd": 0.5,
            "macd_signal": 0.4,
            "macd_hist": 0.1,
            "divergence": "bullish",
            "volume_ratio": 1.8,
            "is_above_ma250": True,
            "macd_golden_cross": True,
            "macd_death_cross": False,
        }


class DailyScanFlowTest(unittest.TestCase):
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
                "technical": {
                    "ma_period": 250,
                    "macd_fast": 12,
                    "macd_slow": 26,
                    "macd_signal": 9,
                },
                "volume": {
                    "min_turnover_rate": 1,
                    "max_turnover_rate": 5,
                    "volume_burst_ratio": 1.5,
                },
                "candidate_pool_size": 30,
            },
            "selector": {
                "near_ma_threshold": 0.05,
                "price_change_soft_min": -0.03,
                "price_change_soft_max": 0.03,
            },
            "regime": {
                "index_code": "000001.SH",
                "ma_short": 20,
                "ma_long": 250,
                "lookback_bars": 300,
                "bull_score_threshold": 0.7,
                "bear_score_threshold": 0.7,
                "range_score_threshold": 0.6,
            },
            "position": {
                "min_stocks": 2,
                "target_stocks": 3,
                "max_stocks": 4,
                "base_position_per_stock": 0.25,
                "mobile_cash_ratio": 0.25,
                "max_position_per_stock": 0.40,
            },
            "risk": {
                "stop_loss_pct": 0.08,
                "stop_profit_pct": 0.30,
                "max_portfolio_drawdown_pct": 0.20,
                "max_single_day_drawdown_pct": 0.02,
                "allow_new_position_when_drawdown_exceeded": False,
            },
            "t_trading": {
                "enabled": True,
                "positive_t_step_pct": 0.05,
                "negative_t_step_pct": 0.05,
                "range_t_step_pct": 0.05,
            },
            "backtest": {"lot_size": 100},
            "risk_control": {"stop_loss": 0.08, "stop_profit": 0.30},
            "manual_overrides": {
                "regime_override": "auto",
                "disable_new_positions": False,
                "only_reduce_positions": False,
                "max_total_exposure": 1.0,
            },
            "notification": {
                "wechat": {"enabled": False},
                "email": {"enabled": False},
            },
        }

    def test_daily_scan_to_notification_payload(self):
        engine = StrategyEngine(
            config=self.config,
            data_fetcher=IntegrationDataFetcher(),
            technical_indicators=IntegrationTechnicalIndicators(),
        )
        notifier = NotificationService(self.config)

        result = engine.run_daily_scan(positions=[])
        payload = notifier.build_daily_payload(result)

        self.assertEqual(result["market_status"], "bull")
        self.assertEqual(result["stats"]["candidate_pool_count"], 1)
        self.assertEqual(len(result["buy_signals"]), 1)
        self.assertEqual(result["stats"]["t_signals_count"], 0)
        self.assertEqual(payload["candidate_pool"][0]["ts_code"], "000001.SZ")
        self.assertEqual(payload["high_priority_trade_signals"][0]["signal_type"], "BUY")
        self.assertEqual(payload["stats"]["risk_alerts_count"], 0)

    def test_daily_scan_stops_before_technical_analysis_when_selector_turnover_rejects(self):
        engine = StrategyEngine(
            config=self.config,
            data_fetcher=IntegrationDataFetcher(turnover_rate=6.0),
            technical_indicators=IntegrationTechnicalIndicators(),
        )

        result = engine.run_daily_scan(positions=[])

        self.assertEqual(result["stats"]["fundamental_passed"], 1)
        self.assertEqual(result["stats"]["volume_passed"], 0)
        self.assertEqual(result["stats"]["technical_analyzed"], 0)
        self.assertEqual(result["stats"]["candidate_pool_count"], 0)


if __name__ == "__main__":
    unittest.main()
