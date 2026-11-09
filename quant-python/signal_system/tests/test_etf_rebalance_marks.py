"""Guards for daily/weekly/monthly rebalance marks."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from etf_rotation.backtest import rebalance_marks


class RebalanceMarksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dates = pd.bdate_range("2024-01-01", periods=15)

    def test_daily_marks_every_trading_day(self) -> None:
        self.assertEqual(rebalance_marks(self.dates, "D"), set(self.dates))

    def test_weekly_marks_last_trading_day_of_week(self) -> None:
        marks = rebalance_marks(self.dates, "W")
        expected = pd.Series(self.dates, index=self.dates).groupby(
            self.dates.to_period("W")
        ).max()
        self.assertEqual(marks, set(expected.tolist()))

    def test_monthly_marks_last_trading_day_of_month(self) -> None:
        marks = rebalance_marks(self.dates, "M")
        expected = pd.Series(self.dates, index=self.dates).groupby(
            self.dates.to_period("M")
        ).max()
        self.assertEqual(marks, set(expected.tolist()))

    def test_unknown_rebalance_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            rebalance_marks(self.dates, "Q")


if __name__ == "__main__":
    unittest.main()
