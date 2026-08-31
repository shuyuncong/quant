import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.selector.stock_selector import StockSelector


class StockSelectorTest(unittest.TestCase):
    def setUp(self):
        self.selector = StockSelector(
            {
                "selector": {
                    "roe_min": 10,
                    "debt_ratio_max": 50,
                    "pe_acceptable_max": 30,
                    "market_cap_min": 50,
                    "market_cap_max": 500,
                    "turnover_rate_min": 1,
                    "turnover_rate_max": 3,
                    "volume_ratio_min": 1.5,
                    "near_ma_threshold": 0.05,
                    "price_change_soft_min": -0.03,
                    "price_change_soft_max": 0.03,
                }
            }
        )

    def test_selects_stock_with_three_leg_alignment(self):
        result = self.selector.select(
            [
                {
                    "ts_code": "000001.SZ",
                    "name": "平安银行",
                    "roe": 12.5,
                    "debt_ratio": 42.0,
                    "pe": 7.2,
                    "market_cap": 220,
                    "avg_turnover": 2.1,
                    "volume_ratio": 1.8,
                    "price_change_pct": 0.01,
                    "close_vs_ma_long": 0.03,
                    "ma_long_slope": 0.02,
                    "divergence": "bullish",
                    "bear_trap": True,
                }
            ]
        )

        self.assertEqual(result["candidate_pool"], ["000001.SZ"])
        self.assertEqual(len(result["selected"]), 1)
        self.assertEqual(result["selected"][0]["score"], 100)
        self.assertEqual(result["selected"][0]["fundamental"]["status"], "passed")

    def test_rejects_stock_and_outputs_failed_reason(self):
        result = self.selector.select(
            [
                {
                    "ts_code": "600000.SH",
                    "name": "浦发银行",
                    "roe": 8.0,
                    "debt_ratio": 55.0,
                    "pe": 35.0,
                    "market_cap": 600,
                    "avg_turnover": 0.5,
                    "volume_ratio": 0.9,
                    "price_change_pct": 0.08,
                    "close_vs_ma_long": 0.12,
                    "ma_long_slope": -0.01,
                    "divergence": "none",
                    "bear_trap": False,
                }
            ]
        )

        self.assertEqual(len(result["selected"]), 0)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertTrue(any("ROE" in reason for reason in result["rejected"][0]["failed_reasons"]))
        self.assertTrue(any("换手率" in reason for reason in result["rejected"][0]["failed_reasons"]))

    def test_sorts_selected_pool_by_score(self):
        result = self.selector.select(
            [
                {
                    "ts_code": "A",
                    "name": "A",
                    "roe": 15,
                    "debt_ratio": 30,
                    "pe": 18,
                    "market_cap": 120,
                    "avg_turnover": 2,
                    "volume_ratio": 1.6,
                    "price_change_pct": 0.02,
                    "close_vs_ma_long": 0.02,
                    "ma_long_slope": 0.01,
                    "divergence": "bullish",
                    "bear_trap": False,
                },
                {
                    "ts_code": "B",
                    "name": "B",
                    "roe": 15,
                    "debt_ratio": 30,
                    "pe": 18,
                    "market_cap": 120,
                    "avg_turnover": 2,
                    "volume_ratio": 1.7,
                    "price_change_pct": 0.00,
                    "close_vs_ma_long": 0.01,
                    "ma_long_slope": 0.02,
                    "divergence": "bullish",
                    "bear_trap": True,
                },
            ]
        )

        self.assertEqual(result["candidate_pool"], ["A", "B"])
        self.assertGreaterEqual(result["selected"][0]["score"], result["selected"][1]["score"])

    def test_falls_back_to_strategy_thresholds_when_selector_values_are_absent(self):
        selector = StockSelector(
            {
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
                },
                "selector": {
                    "near_ma_threshold": 0.05,
                    "price_change_soft_min": -0.03,
                    "price_change_soft_max": 0.03,
                },
            }
        )

        record = selector.evaluate(
            {
                "ts_code": "FALLBACK",
                "name": "Fallback",
                "roe": 12,
                "debt_ratio": 35,
                "pe": 20,
                "market_cap": 120,
                "avg_turnover": 4.0,
            },
            checks=("fundamental", "turnover"),
        )

        self.assertTrue(record.passed)
        self.assertEqual(record.passed_checks, ["fundamental", "turnover"])


if __name__ == "__main__":
    unittest.main()
