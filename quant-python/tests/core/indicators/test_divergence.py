import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.indicators.divergence import DivergenceDetector


class DivergenceDetectorTest(unittest.TestCase):
    def setUp(self):
        self.detector = DivergenceDetector()

    def test_detects_bullish_divergence_by_area(self):
        price = pd.Series([12, 11, 10, 9, 10, 11, 10, 9.5, 8.8, 9.2, 10])
        hist = pd.Series([-0.2, -0.6, -1.2, -0.8, 0.2, 0.3, -0.15, -0.3, -0.45, -0.2, 0.1])

        result = self.detector.detect(price, hist, "bullish")

        self.assertTrue(result.is_divergence)
        self.assertLess(result.detail["last_area"], result.detail["prev_area"])
        self.assertTrue(result.detail["price_condition"])

    def test_detects_bearish_divergence_by_area(self):
        price = pd.Series([10, 11, 12, 13, 12.5, 12, 12.5, 13.2, 14, 13.6, 13.2])
        hist = pd.Series([0.2, 0.8, 1.1, 0.9, -0.1, -0.2, 0.15, 0.4, 0.5, 0.2, -0.1])

        result = self.detector.detect(price, hist, "bearish")

        self.assertTrue(result.is_divergence)
        self.assertTrue(result.detail["price_condition"])
        self.assertTrue(result.detail["area_condition"])

    def test_returns_none_when_no_divergence(self):
        price = pd.Series([10, 9.8, 9.5, 9.2, 9.0, 8.8, 8.5, 8.3, 8.0, 7.8, 7.5])
        hist = pd.Series([-0.2, -0.4, -0.8, -1.0, 0.1, -0.3, -0.5, -0.9, -1.2, -0.8, 0.1])

        result = self.detector.classify(price, hist)

        self.assertEqual(result, "none")

    def test_ignores_single_bar_segments_by_default(self):
        detector = DivergenceDetector()
        hist = pd.Series([-0.4, -0.2, 0.1, -0.1, 0.2, -0.3, -0.2, 0.1])

        segments = detector._find_macd_segments(hist, "bullish")

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["start"], 0)
        self.assertEqual(segments[1]["start"], 5)

    def test_supports_single_bar_segments_when_configured(self):
        detector = DivergenceDetector(min_segment_length=1)
        hist = pd.Series([-0.4, -0.2, 0.1, -0.1, 0.2, -0.3, -0.2, 0.1])

        segments = detector._find_macd_segments(hist, "bullish")

        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[1]["start"], 3)
        self.assertEqual(segments[1]["end"], 3)


if __name__ == "__main__":
    unittest.main()
