"""Offline invariants for the RSRS / slope x R2 study signals."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import etf_rsrs_slope_study as study


def random_bars(days: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = pd.Series(np.exp(np.cumsum(rng.normal(0.0, 0.01, days))),
                      index=pd.bdate_range("2015-01-01", periods=days))
    spread = rng.uniform(0.002, 0.02, days)
    return pd.DataFrame({"close": close, "high": close * (1 + spread), "low": close * (1 - spread)})


class SlopeScoreTests(unittest.TestCase):
    def test_matches_the_community_polyfit_formula(self) -> None:
        close = random_bars(60)["close"]
        score = study.slope_r2_score(close.to_frame("x"), 25)["x"]
        y = np.log(close.iloc[-25:].to_numpy())
        x = np.arange(25)
        slope, intercept = np.polyfit(x, y, 1)
        r2 = 1 - sum((y - (slope * x + intercept)) ** 2) / ((len(y) - 1) * np.var(y, ddof=1))
        self.assertAlmostEqual(score.iloc[-1], (math.pow(math.exp(slope), 250) - 1) * r2, places=9)
        self.assertTrue(score.iloc[:24].isna().all())
        self.assertTrue(np.isfinite(score.iloc[24]))


class RsrsTests(unittest.TestCase):
    def test_score_does_not_use_later_bars(self) -> None:
        bars = random_bars(400)
        for dull in (False, True):
            kwargs = dict(n=18, m=100, close=bars["close"] if dull else None)
            full = study.rsrs_score(bars["high"], bars["low"], **kwargs)
            cut = bars.iloc[:300]
            kwargs["close"] = cut["close"] if dull else None
            head = study.rsrs_score(cut["high"], cut["low"], **kwargs)
            self.assertTrue(np.isfinite(head.iloc[-1]))
            pd.testing.assert_series_equal(full.iloc[:300], head)

    def test_hysteresis_holds_state_inside_the_band(self) -> None:
        score = pd.Series([float("nan"), 0.5, 0.8, 0.1, -0.6, float("nan"), -0.8, 0.6, 0.71])
        self.assertEqual(study.hysteresis(score, 0.7).tolist(),
                         [False, False, True, True, True, True, False, False, True])


class SignalRewriteTests(unittest.TestCase):
    DAY = pd.Timestamp("2024-01-05")

    def signals(self, gate: bool) -> study.Signals:
        return {self.DAY: {"gate": gate, "candidates": [(2.0, "a", 0.2, 0.1), (1.0, "b", 0.1, 0.1)]}}

    def test_rerank_keeps_the_candidate_set_and_reorders_it(self) -> None:
        score = pd.DataFrame({"a": [0.1], "b": [0.9]}, index=[self.DAY])
        rows = study.rerank(self.signals(True), score)[self.DAY]["candidates"]
        self.assertEqual([row[1] for row in rows], ["b", "a"])

    def test_regate_replaces_or_intersects_the_ma200_gate(self) -> None:
        for ma200, rsrs, replaced, both in ((True, True, True, True), (True, False, False, False),
                                            (False, True, True, False), (False, False, False, False)):
            state = pd.Series({self.DAY: rsrs})
            signals = self.signals(ma200)
            self.assertEqual(study.regate(signals, state, keep_ma200=False)[self.DAY]["gate"], replaced)
            self.assertEqual(study.regate(signals, state, keep_ma200=True)[self.DAY]["gate"], both)


if __name__ == "__main__":
    unittest.main()
