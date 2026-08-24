import os
import sys
import unittest
from unittest.mock import patch

import pandas as pd


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from strategy.macd import analyze_macd, calculate_macd, find_golden_cross_entries


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

    def _patched_macd(self, length=50, cross_index=48, zone="above"):
        if zone == "below":
            before_dif, before_dea = -0.30, -0.20
            cross_dif, cross_dea = -0.10, -0.15
            after_dif, after_dea = -0.08, -0.12
        else:
            before_dif, before_dea = 0.10, 0.20
            cross_dif, cross_dea = 0.25, 0.20
            after_dif, after_dea = 0.28, 0.22
        dif = [before_dif] * length
        dea = [before_dea] * length
        dif[cross_index] = cross_dif
        dea[cross_index] = cross_dea
        for index in range(cross_index + 1, length):
            dif[index] = after_dif
            dea[index] = after_dea
        return pd.DataFrame(
            {"dif": dif, "dea": dea, "hist": [0.1] * length}
        )

    def _analyze_cross(self, closes, macd, confirmation_bars=5):
        with patch("strategy.macd.calculate_macd", return_value=macd):
            return analyze_macd(
                self._frame(closes),
                zero_axis_tolerance=0.005,
                pullback_confirmation_bars=confirmation_bars,
            )[1]

    def test_golden_cross_waits_for_pullback_confirmation(self):
        summary = self._analyze_cross([10.0] * 49, self._patched_macd(49))
        self.assertTrue(summary["golden_cross"])
        self.assertEqual("pending_pullback", summary["golden_cross_state"])
        self.assertFalse(summary["golden_cross_entry_ready"])

    def test_pullback_confirmation_creates_entry(self):
        closes = [10.0] * 49 + [9.98, 10.4]
        frame = self._frame(closes)
        frame.loc[49, "low"] = 9.95
        frame.loc[50, "low"] = 10.1
        with patch("strategy.macd.calculate_macd", return_value=self._patched_macd(51)):
            summary = analyze_macd(
                frame,
                zero_axis_tolerance=0.005,
                pullback_confirmation_bars=5,
            )[1]
        self.assertEqual("confirmed_pullback", summary["golden_cross_state"])
        self.assertTrue(summary["golden_cross_entry_ready"])
        self.assertEqual(2, summary["golden_cross_confirmation_bars"])

        later_frame = self._frame(closes + [10.5])
        later_frame.loc[49, "low"] = 9.95
        later_frame.loc[50, "low"] = 10.1
        with patch("strategy.macd.calculate_macd", return_value=self._patched_macd(52)):
            later_summary = analyze_macd(
                later_frame,
                zero_axis_tolerance=0.005,
                pullback_confirmation_bars=5,
            )[1]
        self.assertEqual("confirmed_pullback", later_summary["golden_cross_state"])
        self.assertFalse(later_summary["golden_cross_entry_ready"])
        self.assertEqual(
            summary["golden_cross_first_confirmation_time"],
            later_summary["golden_cross_first_confirmation_time"],
        )

    def test_pullback_breaking_cross_low_invalidates_entry(self):
        closes = [10.0] * 49 + [9.8]
        frame = self._frame(closes)
        frame.loc[49, "low"] = 9.7
        macd = self._patched_macd(50)
        with patch("strategy.macd.calculate_macd", return_value=macd):
            summary = analyze_macd(frame, zero_axis_tolerance=0.005)[1]
        self.assertEqual("invalidated", summary["golden_cross_state"])
        self.assertFalse(summary["golden_cross_entry_ready"])

    def test_pullback_is_rejected_after_a_new_death_cross(self):
        frame = self._frame([10.0] * 49 + [9.95, 10.4])
        frame.loc[49, "low"] = 9.9
        macd = self._patched_macd(51)
        macd.loc[50, "dif"] = 0.15
        macd.loc[50, "dea"] = 0.20
        with patch("strategy.macd.calculate_macd", return_value=macd):
            summary = analyze_macd(frame, zero_axis_tolerance=0.005)[1]
        self.assertEqual("confirmed_pullback", summary["golden_cross_state"])
        self.assertFalse(summary["golden_cross_entry_ready"])

    def test_pullback_confirmation_expires(self):
        closes = [10.0] * 49 + [10.2] * 6
        summary = self._analyze_cross(closes, self._patched_macd(55), confirmation_bars=5)
        self.assertEqual("expired", summary["golden_cross_state"])
        self.assertFalse(summary["golden_cross_entry_ready"])

    def test_below_zero_cross_is_not_an_entry(self):
        closes = [10.0] * 49 + [10.2]
        summary = self._analyze_cross(
            closes,
            self._patched_macd(50, zone="below"),
        )
        self.assertEqual("below", summary["golden_cross_entry_zone"])
        self.assertFalse(summary["golden_cross_entry_ready"])

    def test_long_ma_and_recent_return_flag_high_position_risk(self):
        closes = [10.0] * 55 + [15.0] * 5
        _, summary = analyze_macd(
            self._frame(closes),
            long_ma_period=20,
            position_lookback=5,
            max_long_ma_distance=0.05,
            max_recent_return=0.30,
        )
        self.assertTrue(summary["above_ma_long"])
        self.assertTrue(summary["ma_long_up"])
        self.assertTrue(summary["high_position_risk"])
        self.assertFalse(summary["high_volume_risk"])

    def test_find_entries_uses_confirmation_day_and_excludes_below_zero(self):
        frame = self._frame([10.0] * 49 + [9.95, 10.2])
        frame.loc[49, "low"] = 9.9
        with patch("strategy.macd.calculate_macd", return_value=self._patched_macd(51)):
            entries = find_golden_cross_entries(frame, confirmation_bars=5)
        self.assertEqual(1, len(entries))
        self.assertEqual(48, entries[0]["cross_index"])
        self.assertEqual(50, entries[0]["confirmation_index"])
        self.assertIn(entries[0]["zone"], {"above", "near"})

        with patch("strategy.macd.calculate_macd", return_value=self._patched_macd(51, zone="below")):
            self.assertEqual([], find_golden_cross_entries(frame, confirmation_bars=5))

        death_after_pullback = self._patched_macd(51)
        death_after_pullback.loc[50, "dif"] = 0.15
        death_after_pullback.loc[50, "dea"] = 0.20
        with patch("strategy.macd.calculate_macd", return_value=death_after_pullback):
            self.assertEqual([], find_golden_cross_entries(frame, confirmation_bars=5))

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

