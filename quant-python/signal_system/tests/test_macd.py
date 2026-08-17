import os
import sys
import unittest
from unittest.mock import patch

import pandas as pd


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from strategy.macd import analyze_macd, calculate_macd


class MacdTests(unittest.TestCase):
    def _frame(self, close):
        size = len(close)
        return pd.DataFrame(
            {
                "datetime": pd.date_range("2025-01-01", periods=size, freq="D"),
                "open": close,
                "high": [value + 0.1 for value in close],
                "low": [value - 0.1 for value in close],
                "close": close,
                "volume": [1000.0] * size,
                "amount": [value * 100000 for value in close],
                "is_closed": [True] * size,
            }
        )

    def test_latest_zero_axis_golden_cross(self):
        values = [10 - index * 0.01 for index in range(60)] + [9.4 + index * 0.03 for index in range(60)]
        macd = calculate_macd(pd.Series(values))
        crosses = [
            index
            for index in range(1, len(macd))
            if pd.notna(macd["dea"].iloc[index])
            and macd["dif"].iloc[index] > macd["dea"].iloc[index]
            and macd["dif"].iloc[index - 1] <= macd["dea"].iloc[index - 1]
        ]
        self.assertTrue(crosses)
        frame = self._frame(values[: crosses[0] + 1])
        _, summary = analyze_macd(frame, zero_axis_tolerance=1.0)
        self.assertTrue(summary["golden_cross"])
        self.assertTrue(summary["zero_axis_golden_cross"])

    def test_zero_axis_tolerance_filters_far_cross(self):
        values = [100 - index for index in range(60)] + [40 + index * 3 for index in range(60)]
        macd = calculate_macd(pd.Series(values))
        crosses = [
            index
            for index in range(1, len(macd))
            if pd.notna(macd["dea"].iloc[index])
            and macd["dif"].iloc[index] > macd["dea"].iloc[index]
            and macd["dif"].iloc[index - 1] <= macd["dea"].iloc[index - 1]
        ]
        self.assertTrue(crosses)
        _, summary = analyze_macd(self._frame(values[: crosses[0] + 1]), zero_axis_tolerance=0.000001)
        self.assertTrue(summary["golden_cross"])
        self.assertFalse(summary["zero_axis_golden_cross"])

    def test_golden_cross_zone_and_confirmations(self):
        close = [10.0] * 48 + [9.0, 11.0]
        frame = self._frame(close)
        frame.loc[49, "volume"] = 1500.0
        macd = pd.DataFrame(
            {
                "dif": [0.2] * 48 + [0.10, 0.30],
                "dea": [0.2] * 48 + [0.20, 0.25],
                "hist": [0.05] * 47 + [0.10, 0.20, 0.30],
            }
        )
        with patch("strategy.macd.calculate_macd", return_value=macd):
            _, summary = analyze_macd(
                frame,
                zero_axis_tolerance=0.005,
                moderate_volume_min=1.0,
                moderate_volume_max=2.0,
            )
        self.assertTrue(summary["golden_cross"])
        self.assertEqual("above", summary["golden_cross_zone"])
        self.assertTrue(summary["moderate_volume"])
        self.assertTrue(summary["price_breakout_ma5_ma10"])
        self.assertTrue(summary["hist_expanding"])
        self.assertEqual(3, summary["confirmation_count"])
        self.assertEqual("strong", summary["golden_cross_quality"])

    def test_golden_cross_below_zero_is_classified_as_high_risk(self):
        frame = self._frame([10.0] * 50)
        macd = pd.DataFrame(
            {
                "dif": [-0.3] * 49 + [-0.10],
                "dea": [-0.2] * 49 + [-0.15],
                "hist": [-0.2] * 49 + [0.10],
            }
        )
        with patch("strategy.macd.calculate_macd", return_value=macd):
            _, summary = analyze_macd(frame, zero_axis_tolerance=0.005)
        self.assertTrue(summary["golden_cross"])
        self.assertEqual("below", summary["golden_cross_zone"])
        self.assertEqual("high", summary["golden_cross_risk"])

    def test_quality_is_none_when_latest_bar_has_no_golden_cross(self):
        frame = self._frame([10.0] * 48 + [9.0, 11.0])
        frame.loc[49, "volume"] = 1500.0
        macd = pd.DataFrame(
            {
                "dif": [0.2] * 48 + [0.30, 0.40],
                "dea": [0.2] * 48 + [0.20, 0.25],
                "hist": [0.05] * 47 + [0.10, 0.20, 0.30],
            }
        )
        with patch("strategy.macd.calculate_macd", return_value=macd):
            _, summary = analyze_macd(frame, zero_axis_tolerance=0.005)
        self.assertFalse(summary["golden_cross"])
        self.assertEqual(3, summary["confirmation_count"])
        self.assertEqual("none", summary["golden_cross_quality"])


if __name__ == "__main__":
    unittest.main()

