"""Microstructure estimators must be deterministic and bounded."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from etf_microstructure import _corwin_schultz


class MicrostructureTests(unittest.TestCase):
    def test_flat_high_low_gives_zero_spread(self) -> None:
        high = pd.Series([10.0] * 20)
        low = pd.Series([10.0] * 20)
        spread = _corwin_schultz(high, low)
        self.assertTrue((spread.fillna(0.0) == 0.0).all())

    def test_wide_range_gives_positive_bounded_spread(self) -> None:
        high = pd.Series(np.linspace(11.0, 12.0, 20))
        low = pd.Series(np.linspace(10.0, 11.0, 20))
        spread = _corwin_schultz(high, low).dropna()
        self.assertGreater(float(spread.mean()), 0.0)
        self.assertTrue((spread >= 0.0).all())
        self.assertTrue((spread <= 0.05).all())

    def test_invalid_prices_do_not_crash(self) -> None:
        high = pd.Series([10.0, np.nan, 0.0, 12.0])
        low = pd.Series([9.0, 9.0, 8.0, 11.0])
        spread = _corwin_schultz(high, low)
        self.assertEqual(len(spread), len(high))


if __name__ == "__main__":
    unittest.main()
