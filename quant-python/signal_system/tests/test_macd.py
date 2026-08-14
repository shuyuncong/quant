import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()

