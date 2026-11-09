"""Bootstrap helpers must be deterministic and statistically sane."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from strategy_statistics import (
    annualized_sharpe,
    bootstrap_sharpe_ci,
    deflated_sharpe,
    paired_sharpe_diff,
    reality_check,
    stationary_indices,
)


def _returns(seed: int = 1, days: int = 500) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0.0004, 0.01, days), index=pd.bdate_range("2022-01-03", periods=days))


class StrategyStatisticsTests(unittest.TestCase):
    def test_stationary_indices_shape_and_range(self) -> None:
        idx = stationary_indices(100, 10, np.random.default_rng(1))
        self.assertEqual(len(idx), 100)
        self.assertTrue(np.all(idx >= 0))
        self.assertTrue(np.all(idx < 100))

    def test_sharpe_ci_contains_point(self) -> None:
        returns = _returns()
        result = bootstrap_sharpe_ci(returns, n_boot=500, block=10, seed=7)
        self.assertLessEqual(result["ci95_low"], result["sharpe"])
        self.assertGreaterEqual(result["ci95_high"], result["sharpe"])
        self.assertGreater(result["bootstrap_std"], 0.0)

    def test_paired_diff_of_identical_series_is_zero(self) -> None:
        returns = _returns()
        result = paired_sharpe_diff(returns, returns, n_boot=500, block=10, seed=7)
        self.assertAlmostEqual(result["sharpe_diff"], 0.0, places=6)
        self.assertAlmostEqual(result["p_value_two_sided"], 1.0, places=6)
        self.assertLessEqual(result["ci95_low"], 0.0)
        self.assertGreaterEqual(result["ci95_high"], 0.0)

    def test_deflated_sharpe_in_unit_interval(self) -> None:
        returns = _returns()
        result = deflated_sharpe(returns, n_trials=20, trial_sharpes=[0.1, 0.5, 1.0])
        self.assertGreaterEqual(result["deflated_sharpe"], 0.0)
        self.assertLessEqual(result["deflated_sharpe"], 1.0)

    def test_reality_check_p_value_in_unit_interval(self) -> None:
        frame = pd.DataFrame({
            "a": _returns(1),
            "b": _returns(2),
            "c": _returns(3),
        })
        result = reality_check(frame, n_boot=300, block=10, seed=7)
        self.assertGreaterEqual(result["p_value"], 0.0)
        self.assertLessEqual(result["p_value"], 1.0)
        self.assertIn(result["best_variant"], frame.columns)

    def test_constant_series_has_no_sharpe(self) -> None:
        self.assertTrue(np.isnan(annualized_sharpe(pd.Series([0.0] * 20))))


if __name__ == "__main__":
    unittest.main()
