"""Regression guard for missing ETF opens during a rebalance.

The canonical simulator must not crash when a held ETF has no open quote on a
rebalance day.  It values the position at the last close, skips the impossible
trade, and keeps the equity curve finite.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from etf_rotation.backtest import run_panels


class MissingOpenTests(unittest.TestCase):
    def test_held_position_with_missing_open_does_not_crash(self) -> None:
        days = pd.bdate_range("2024-01-02", periods=45)
        close = pd.DataFrame({"A": 10.0, "B": 10.0}, index=days)
        open_ = close.copy()
        volume = pd.DataFrame(1_000_000.0, index=days, columns=["A", "B"])

        # The second month-end rebalance sees no open quote for A.
        month_ends = pd.Series(days, index=days).groupby(days.to_period("M")).max()
        self.assertGreaterEqual(len(month_ends), 2)
        missing_day = month_ends.iloc[1]
        open_.loc[missing_day, "A"] = np.nan

        def signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp):
            return {"target_weights": {"A": 1.0}}

        result = run_panels(
            close,
            open_,
            volume,
            start=str(days[0].date()),
            end=str(days[-1].date()),
            rebalance="M",
            signal_fn=signal,
        )
        self.assertTrue(np.isfinite(result["total_return_pct"]))
        self.assertTrue(np.isfinite(result["sharpe"]))
        equities = np.array([row["equity"] for row in result["curve"]], dtype=float)
        self.assertTrue(np.isfinite(equities).all())
        self.assertGreater(equities[-1], 0.0)


if __name__ == "__main__":
    unittest.main()
