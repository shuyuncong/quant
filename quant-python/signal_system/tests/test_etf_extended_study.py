"""Semantic guards for the ETF extended study.

Pins the rules that the report depends on, not the replay plumbing:

* `use_market_gate` is the only switch between the gate and no-gate arms, and a
  closed gate must remove satellite exposure without touching the core leg;
* `max_picks=2` splits the satellite budget equally instead of doubling it;
* `breadth` scales the satellite budget by `min(1, n_trend / 2)`;
* `target_vol` only ever scales weights down, never up;
* crowding buckets are descriptive labels, not a selector.
"""

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

import etf_extended_study as study


def make_series(days: int, start: float, drift: float, wiggle: float, phase: float = 0.0) -> pd.Series:
    index = pd.bdate_range("2022-01-03", periods=days)
    steps = np.arange(days, dtype=float)
    values = start * (1.0 + drift * steps) + wiggle * np.sin(steps / 5.0 + phase)
    return pd.Series(values, index=index)


def synthetic_panels(market_down: bool = True) -> tuple[dict[str, str], dict[str, pd.DataFrame], pd.Timestamp]:
    days = 420
    index = pd.bdate_range("2022-01-03", periods=days)
    drift = -0.0004 if market_down else 0.0004
    close = pd.DataFrame({
        study.MARKET: make_series(days, 100.0, drift, 0.4, 0.0),
        study.GOLD: make_series(days, 6.0, 0.0006, 0.02, 1.0),
        study.BOND: make_series(days, 100.0, 0.0002, 0.05, 2.0),
        "159611": make_series(days, 1.0, 0.0012, 0.004, 3.0),
        "159770": make_series(days, 1.0, 0.0010, 0.004, 4.0),
    }, index=index)
    volume = pd.DataFrame(1e7, index=index, columns=close.columns)
    close.index = index
    panels = study.build_panels(close, volume)
    satellite = {"159611": "电力", "159770": "机器人"}
    day = index[-1]
    return satellite, panels, day


def signals_for(satellite: dict[str, str], panels: dict[str, pd.DataFrame],
                close: pd.DataFrame, day: pd.Timestamp) -> dict:
    return study.build_signals(close, satellite, panels, pd.DatetimeIndex([day]))[day]


class GateAblationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.satellite, self.panels, self.day = synthetic_panels()
        self.close = self.panels["close"]
        self.signal = signals_for(self.satellite, self.panels, self.close, self.day)

    def test_setup_has_candidates_and_closed_gate(self) -> None:
        self.assertFalse(self.signal["gate"], "synthetic market must close the gate")
        self.assertFalse(self.signal["crash"], "synthetic market must not trigger the crash overlay")
        self.assertGreaterEqual(len(self.signal["candidates"]), 2)

    def test_gate_switch_only_changes_satellite(self) -> None:
        common = dict(max_picks=1, target_vol=None, breadth=True, crash_overlay=False)
        gated, _ = study.make_target(self.signal, self.panels, self.day, use_market_gate=True, **common)
        open_, _ = study.make_target(self.signal, self.panels, self.day, use_market_gate=False, **common)

        core = set(gated) & {study.GOLD, study.BOND}
        self.assertTrue(core, "core leg must be present in both arms")
        self.assertEqual(core, set(open_) & {study.GOLD, study.BOND})
        for symbol in core:
            self.assertAlmostEqual(gated[symbol], open_[symbol], places=9)

        self.assertFalse(set(gated) & set(self.satellite), "closed gate must not hold satellites")
        self.assertTrue(set(open_) & set(self.satellite), "no-gate arm must hold a satellite")


class SizingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.satellite, self.panels, self.day = synthetic_panels()
        self.close = self.panels["close"]
        self.signal = signals_for(self.satellite, self.panels, self.close, self.day)

    def test_top2_splits_budget_equally(self) -> None:
        weights, meta = study.make_target(
            self.signal, self.panels, self.day, max_picks=2, target_vol=None,
            breadth=False, crash_overlay=False, use_market_gate=False,
        )
        self.assertEqual(meta["n_picks"], 2)
        first, second = meta["picks"]
        self.assertAlmostEqual(weights[first], weights[second], places=9)
        satellite_total = sum(weights[s] for s in self.satellite if s in weights)
        self.assertAlmostEqual(satellite_total, study.SAT_MAX, places=9)

    def test_breadth_halves_budget_with_single_candidate(self) -> None:
        one = {"159611": "电力"}
        signal = signals_for(one, self.panels, self.close, self.day)
        _, meta = study.make_target(
            signal, self.panels, self.day, max_picks=1, target_vol=None,
            breadth=True, crash_overlay=False, use_market_gate=False,
        )
        self.assertEqual(meta["n_trend"], 1)
        self.assertAlmostEqual(meta["satellite_budget"], study.SAT_MAX / 2, places=9)

    def test_vol_target_only_scales_down(self) -> None:
        base, _ = study.make_target(
            self.signal, self.panels, self.day, max_picks=1, target_vol=None,
            breadth=True, crash_overlay=False, use_market_gate=False,
        )
        scaled, meta = study.make_target(
            self.signal, self.panels, self.day, max_picks=1, target_vol=0.01,
            breadth=True, crash_overlay=False, use_market_gate=False,
        )
        self.assertLessEqual(meta["vol_scale"], 1.0)
        self.assertLessEqual(sum(scaled.values()), sum(base.values()) + 1e-12)

        loose, loose_meta = study.make_target(
            self.signal, self.panels, self.day, max_picks=1, target_vol=10.0,
            breadth=True, crash_overlay=False, use_market_gate=False,
        )
        self.assertEqual(loose_meta["vol_scale"], 1.0)
        self.assertAlmostEqual(sum(loose.values()), sum(base.values()), places=9)


class CrowdingTests(unittest.TestCase):
    def test_buckets_are_labels_not_scores(self) -> None:
        rows = [
            {"n_candidates": 0, "satellite_weight": 0.0, "candidate_corr": None,
             "basket_forward_5d_pct": None, "basket_forward_20d_pct": None},
            {"n_candidates": 1, "satellite_weight": 0.17, "candidate_corr": None,
             "basket_forward_5d_pct": -0.4, "basket_forward_20d_pct": 3.2},
            {"n_candidates": 7, "satellite_weight": 0.30, "candidate_corr": 0.5,
             "basket_forward_5d_pct": 0.1, "basket_forward_20d_pct": -0.0},
            {"n_candidates": 40, "satellite_weight": 0.25, "candidate_corr": 0.6,
             "basket_forward_5d_pct": 0.5, "basket_forward_20d_pct": 0.2},
        ]
        groups = study.crowding_groups(rows, label="unit")
        labels = [group["bucket"] for group in groups]
        self.assertEqual(labels, ["0", "1", "5-9", "10+"])
        self.assertEqual(sum(group["rebalances"] for group in groups), len(rows))
        self.assertTrue(math.isnan(float(groups[0]["mean_candidate_corr"] or float("nan"))))


if __name__ == "__main__":
    unittest.main()
