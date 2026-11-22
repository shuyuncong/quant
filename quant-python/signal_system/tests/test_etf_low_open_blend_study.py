import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import etf_low_open_blend_study as study


def curve(values, dates):
    return pd.Series([float(value) for value in values], index=pd.DatetimeIndex(dates))


class BlendTests(unittest.TestCase):
    # 1 月两天、2 月两天：月末再平衡落在第 2、4 个交易日。
    dates = ["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-29"]

    def test_month_end_rebalance_resets_weights_and_charges_the_transfer(self):
        etf = curve([100, 100, 100, 110], self.dates)
        low_open = curve([100, 150, 150, 150], self.dates)
        blended, moved = study.blend(etf, low_open, 0.20, 0.01, initial=1000.0)
        # 1/31：ETF 800、低开 300 → 低开应为 220，划出 80，成本 0.8。
        self.assertAlmostEqual(blended.iloc[1], 1100.0 - 0.8)
        # 2 月 ETF +10% 只作用在再平衡后的 80% 上。
        february = 1099.2 * (0.8 * 1.10 + 0.2)
        transfer = abs(0.2 * february - 0.2 * 1099.2)
        self.assertAlmostEqual(blended.iloc[3], february - transfer * 0.01)
        self.assertAlmostEqual(moved, 80.0 / 1100.0 + transfer / february)

    def test_zero_weight_is_the_etf_curve(self):
        etf = curve([250, 260, 255, 270], self.dates)
        low_open = curve([100, 90, 120, 60], self.dates)
        blended, moved = study.blend(etf, low_open, 0.0, 0.01, initial=1000.0)
        pd.testing.assert_series_equal(blended, etf * 4.0)
        self.assertEqual(moved, 0.0)

    def test_mismatched_calendars_are_rejected(self):
        etf = curve([1, 1, 1, 1], self.dates)
        low_open = curve([1, 1, 1, 1], ["2024-01-30", "2024-01-31", "2024-02-02", "2024-02-29"])
        with self.assertRaises(study.StudyError):
            study.blend(etf, low_open, 0.2, 0.0)


class BestTradesTests(unittest.TestCase):
    def test_ranks_by_pnl_over_prior_equity_inside_the_window(self):
        days = pd.DatetimeIndex(["2019-12-31", "2020-01-02", "2020-01-03", "2020-01-06"])
        equity = pd.Series([100.0, 100.0, 200.0, 200.0], index=days)
        trades = pd.DataFrame({
            "symbol": ["early", "small_account", "big_account"],
            "entry_date": [days[0], days[1], days[3]],
            "net_pnl": [90.0, 10.0, 15.0],
        })
        best = study.best_trades(trades, equity, pd.Timestamp("2020-01-02"), 1)
        # 窗口前的那笔不算；10/100 比 15/200 贡献大。
        self.assertEqual(best["symbol"].tolist(), ["small_account"])
        self.assertAlmostEqual(best["contribution"].iloc[0], 0.10)


if __name__ == "__main__":
    unittest.main()
