import os
import sys
import unittest

import pandas as pd


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from strategy.chan import (
    Center,
    Fractal,
    Stroke,
    build_strokes,
    calculate_stroke_macd_area,
    detect_chan_signals,
    find_fractals,
    lock_confirmed_strokes,
    merge_inclusions,
)


def stroke(index, direction, start_price, end_price, area=10.0):
    return Stroke(
        index=index,
        start_index=index * 4,
        end_index=index * 4 + 4,
        direction=direction,
        start_price=start_price,
        end_price=end_price,
        high=max(start_price, end_price),
        low=min(start_price, end_price),
        start_time=f"2025-01-{index + 1:02d}T10:00:00",
        end_time=f"2025-01-{index + 1:02d}T10:30:00",
        confirmed_at=f"2025-01-{index + 1:02d}T10:31:00",
        macd_area=area,
    )


class ChanTests(unittest.TestCase):
    def test_last_stroke_is_provisional_until_following_stroke_exists(self):
        fractals = [
            Fractal(0, "bottom", 10, "2025-01-01T10:00:00", "2025-01-01T10:01:00"),
            Fractal(4, "top", 15, "2025-01-01T10:05:00", "2025-01-01T10:06:00"),
        ]
        strokes = build_strokes(fractals)
        self.assertEqual(1, len(strokes))
        self.assertEqual([], lock_confirmed_strokes(strokes))

        fractals.append(
            Fractal(8, "bottom", 11, "2025-01-01T10:09:00", "2025-01-01T10:10:00")
        )
        strokes = build_strokes(fractals)
        confirmed = lock_confirmed_strokes(strokes)
        self.assertEqual(1, len(confirmed))
        self.assertEqual("2025-01-01T10:10:00", confirmed[0].confirmed_at)

    def test_strict_fractal_after_inclusion_merge(self):
        frame = pd.DataFrame(
            [
                ["2025-01-01", 9, 10, 8, 9, 1, 1],
                ["2025-01-02", 10, 12, 9, 11, 1, 1],
                ["2025-01-03", 9, 11, 7, 8, 1, 1],
            ],
            columns=["datetime", "open", "high", "low", "close", "volume", "amount"],
        )
        merged = merge_inclusions(frame)
        fractals = find_fractals(merged)
        self.assertEqual("top", fractals[0].kind)
        self.assertEqual(12, fractals[0].price)

    def test_merge_inclusions_without_optional_volume_columns(self):
        frame = pd.DataFrame(
            [
                ["2025-01-01", 9, 10, 8, 9],
                ["2025-01-02", 10, 12, 9, 11],
                ["2025-01-03", 9, 11, 7, 8],
            ],
            columns=["datetime", "open", "high", "low", "close"],
        )
        merged = merge_inclusions(frame)
        self.assertEqual(3, len(merged))
        self.assertEqual([0.0, 0.0, 0.0], [bar.volume for bar in merged])
        self.assertEqual([0.0, 0.0, 0.0], [bar.amount for bar in merged])
        self.assertEqual("2025-01-03T00:00:00", merged[-1].time)

    def test_stroke_macd_area_matches_for_sorted_and_unsorted_frames(self):
        frame = pd.DataFrame(
            {
                "datetime": pd.date_range("2025-01-01", periods=5, freq="D"),
                "hist": [1.0, -2.0, 3.0, -4.0, 5.0],
            }
        )
        up = Stroke(
            index=0,
            start_index=1,
            end_index=4,
            direction="up",
            start_price=10,
            end_price=12,
            high=12,
            low=10,
            start_time="2025-01-02T00:00:00",
            end_time="2025-01-05T00:00:00",
            confirmed_at="2025-01-05T00:00:00",
        )
        calculate_stroke_macd_area([up], frame)
        self.assertEqual(8.0, up.macd_area)

        up.macd_area = 0.0
        calculate_stroke_macd_area([up], frame.sample(frac=1, random_state=7))
        self.assertEqual(8.0, up.macd_area)

    def test_first_and_second_buy(self):
        strokes = [
            stroke(0, "up", 8, 11, area=8),
            stroke(1, "down", 11, 9, area=9),
            stroke(2, "up", 9, 10.5, area=8),
            stroke(3, "down", 10.5, 8, area=10),
            stroke(4, "up", 8, 10, area=7),
            stroke(5, "down", 10, 7, area=5),
            stroke(6, "up", 7, 9.5, area=7),
            stroke(7, "down", 9.5, 7.4, area=6),
        ]
        center = Center(0, 0, 2, 9.0, 10.5, 8, 11, strokes[0].start_time, strokes[2].end_time)
        signals = detect_chan_signals(strokes, [center], divergence_ratio=0.9)
        types = [item.signal_type for item in signals]
        self.assertIn("buy_1", types)
        self.assertIn("buy_2", types)

    def test_third_buy_and_third_sell(self):
        buy_strokes = [
            stroke(0, "down", 12, 9),
            stroke(1, "up", 9, 11),
            stroke(2, "down", 11, 9.5),
            stroke(3, "down", 10.5, 9.4),
            stroke(4, "up", 9.4, 12),
            stroke(5, "down", 12, 11.2),
        ]
        buy_center = Center(0, 0, 3, 9.5, 10.5, 9, 12, buy_strokes[0].start_time, buy_strokes[3].end_time)
        buy_types = [item.signal_type for item in detect_chan_signals(buy_strokes, [buy_center])]
        self.assertIn("buy_3", buy_types)

        sell_strokes = [
            stroke(0, "up", 8, 11),
            stroke(1, "down", 11, 9),
            stroke(2, "up", 9, 10.5),
            stroke(3, "up", 9.5, 10.6),
            stroke(4, "down", 10.6, 8),
            stroke(5, "up", 8, 8.8),
        ]
        sell_center = Center(0, 0, 3, 9.0, 10.0, 8, 11, sell_strokes[0].start_time, sell_strokes[3].end_time)
        sell_types = [item.signal_type for item in detect_chan_signals(sell_strokes, [sell_center])]
        self.assertIn("sell_3", sell_types)

    def test_confirmed_signal_is_stable_when_future_stroke_arrives(self):
        base = [
            stroke(0, "up", 8, 11, area=8),
            stroke(1, "down", 11, 9, area=9),
            stroke(2, "up", 9, 10.5, area=8),
            stroke(3, "down", 10.5, 8, area=10),
            stroke(4, "up", 8, 10, area=7),
            stroke(5, "down", 10, 7, area=5),
        ]
        center = Center(0, 0, 2, 9.0, 10.5, 8, 11, base[0].start_time, base[2].end_time)
        before = [(item.signal_type, item.confirmed_at) for item in detect_chan_signals(base, [center])]
        after = [
            (item.signal_type, item.confirmed_at)
            for item in detect_chan_signals(base + [stroke(6, "up", 7, 10)], [center])
        ]
        self.assertTrue(set(before).issubset(set(after)))


if __name__ == "__main__":
    unittest.main()
