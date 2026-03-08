import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.detectors.bear_trap import BearTrapDetector


class BearTrapDetectorTest(unittest.TestCase):
    def setUp(self):
        self.detector = BearTrapDetector(ma_period=5, break_days=5, recovery_days=5, slope_window=3)

    def test_detects_bear_trap(self):
        close = pd.Series([10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 10.7, 10.5, 11.3])
        df = pd.DataFrame({"close": close})

        result = self.detector.detect(df, {"is_divergence": True})

        self.assertTrue(result.is_bear_trap)
        self.assertIn("年线向上", result.reason)
        self.assertIn("快速收回", result.reason)

    def test_rejects_without_bullish_divergence(self):
        close = pd.Series([10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 10.7, 10.5, 11.3])
        df = pd.DataFrame({"close": close})

        result = self.detector.detect(df, {"is_divergence": False})

        self.assertFalse(result.is_bear_trap)
        self.assertEqual(result.reason, "无底背离")


if __name__ == "__main__":
    unittest.main()
