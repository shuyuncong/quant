import unittest

import pandas as pd

from backtest.acceptance.run_real_data_acceptance import (
    AkshareAcceptanceFetcher,
    STOCK_POOL,
    build_strategy_comparison,
)


class RealDataAcceptanceFetcherTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = AkshareAcceptanceFetcher(
            STOCK_POOL[:1],
            start_date="20240101",
            end_date="20260817",
        )

    def test_latest_trade_date_comes_from_actual_index_history(self):
        self.fetcher.index_cache = pd.DataFrame(
            {
                "datetime": pd.to_datetime(["2026-08-13", "2026-08-14"]),
                "close": [10.0, 10.1],
                "price_change_pct": [0.0, 0.01],
            }
        )

        self.assertEqual("20260814", self.fetcher.align_end_date_to_latest_trade_date())
        self.assertEqual("20260814", self.fetcher.end_date)

    def test_daily_basic_keeps_turnover_rate_in_percentage_units(self):
        symbol = STOCK_POOL[0]["ts_code"]
        self.fetcher.hist_cache[symbol] = pd.DataFrame(
            {
                "datetime": pd.to_datetime(["2026-08-14"]),
                "close": [80.0],
                "outstanding_share": [1_000_000.0],
                "turnover_rate": [2.25],
                "volume": [100_000.0],
            }
        )

        result = self.fetcher.get_daily_basic(symbol, trade_date="20260814")

        self.assertEqual(2.25, result["turnover_rate"])

    def test_zero_trade_strategy_is_not_given_a_win_rate_delta(self):
        comparison = build_strategy_comparison(
            {"trade_count": 1, "completed_trade_win_rate": 0.5},
            {"trade_count": 0, "completed_trade_win_rate": 0.0},
        )

        self.assertEqual("insufficient_completed_trades", comparison["comparison_status"])
        self.assertIsNone(comparison["completed_trade_win_rate_delta"])


if __name__ == "__main__":
    unittest.main()
