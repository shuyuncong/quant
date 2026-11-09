"""Quarterly review must be observable-only, deterministic and lookahead-free."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from etf_crowding_review import review
from etf_rotation.engine import CORE, MARKET, SATELLITE


def _panels(days: int = 500):
    dates = pd.bdate_range("2022-01-03", periods=days)
    rng = np.random.default_rng(3)
    symbols = [*SATELLITE, MARKET, *CORE]
    close = pd.DataFrame({
        symbol: 10.0 * np.cumprod(1.0 + rng.normal(0.0002, 0.008, days))
        for symbol in symbols
    }, index=dates)
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=symbols)
    return close, volume


class CrowdingReviewTests(unittest.TestCase):
    def test_review_rows_and_data_status(self) -> None:
        close, volume = _panels()
        payload = review(close, volume, start="2023-01-02", end="2024-12-31")
        self.assertGreaterEqual(len(payload["rows"]), 4)
        row = payload["rows"][0]
        self.assertIn("n_trend", row)
        self.assertIn("avg_pairwise_corr_60d", row)
        self.assertIn("market_trigger_count", row)
        self.assertTrue(row["triggers"]["crowding_data_unavailable"])
        self.assertGreaterEqual(row["market_trigger_count"], 0)
        self.assertEqual(payload["data_status"]["fund_flow"], "local cache has no ETF fund-flow data")

    def test_no_lookahead_in_metrics(self) -> None:
        close, volume = _panels()
        first = review(close, volume, start="2023-01-02", end="2023-12-31")["rows"][0]
        extended = review(close, volume, start="2023-01-02", end="2024-12-31")["rows"][0]
        for key in ("date", "n_trend", "n_liquid", "avg_pairwise_corr_60d",
                    "momentum_dispersion_12_1", "benchmark_drawdown_252d"):
            if isinstance(first[key], float):
                self.assertAlmostEqual(first[key], extended[key], places=10)
            else:
                self.assertEqual(first[key], extended[key])


if __name__ == "__main__":
    unittest.main()
