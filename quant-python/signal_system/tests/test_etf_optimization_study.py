"""Offline invariants for the ETF optimization study wrappers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import etf_optimization_study as study
from etf_extended_study import GOLD, INITIAL, RunResult

DAY = pd.Timestamp("2024-01-05")


def fixed(weights: dict[str, float]) -> study.TargetFn:
    return lambda day: dict(weights)


class CashSleeveTests(unittest.TestCase):
    def test_idle_weight_goes_to_the_cash_etf_minus_buffer(self) -> None:
        weights = study.with_cash_sleeve(fixed({GOLD: 0.40, "159611": 0.20}))(DAY)
        self.assertAlmostEqual(weights[study.CASH_ETF], 0.38)
        self.assertAlmostEqual(weights[GOLD], 0.40)
        self.assertAlmostEqual(sum(weights.values()), 1.0 - study.CASH_BUFFER)

    def test_full_book_gets_no_sleeve(self) -> None:
        weights = study.with_cash_sleeve(fixed({GOLD: 0.60, "159611": 0.39}))(DAY)
        self.assertNotIn(study.CASH_ETF, weights)


class OverlayTests(unittest.TestCase):
    def test_idle_overlay_is_capped_by_free_cash(self) -> None:
        base = {GOLD: 0.75, "159611": 0.15}
        weights = study.with_overlay(fixed(base), symbol=study.NASDAQ_ETF, weight=0.20, mode="idle")(DAY)
        self.assertAlmostEqual(weights[study.NASDAQ_ETF], 0.08)
        self.assertAlmostEqual(weights[GOLD], 0.75)
        self.assertLessEqual(sum(weights.values()), 1.0 - study.CASH_BUFFER + 1e-12)

    def test_carve_overlay_scales_the_strategy(self) -> None:
        base = {GOLD: 0.40, "159611": 0.35}
        weights = study.with_overlay(fixed(base), symbol=study.NASDAQ_ETF, weight=0.20, mode="carve")(DAY)
        self.assertAlmostEqual(weights[GOLD], 0.32)
        self.assertAlmostEqual(weights["159611"], 0.28)
        self.assertAlmostEqual(weights[study.NASDAQ_ETF], 0.20)

    def test_momentum_switch_turns_the_overlay_off(self) -> None:
        base = {GOLD: 0.40}
        for value, held in ((0.05, True), (0.0, False), (-0.10, False), (float("nan"), False)):
            momentum = pd.Series({DAY: value})
            weights = study.with_overlay(fixed(base), symbol=study.NASDAQ_ETF, weight=0.20,
                                         mode="idle", momentum=momentum)(DAY)
            self.assertEqual(study.NASDAQ_ETF in weights, held, value)


class MetricsTests(unittest.TestCase):
    def test_period_metrics_include_the_first_day_of_the_period(self) -> None:
        days = pd.bdate_range("2023-12-27", periods=5)
        curve = pd.Series([100.0, 110.0, 121.0, 133.1, 133.1], index=days)
        metrics = study.curve_metrics(curve, start=str(days[2].date()))
        # Anchored on the close before the period: 133.1 / 110, not 133.1 / 121.
        self.assertAlmostEqual(metrics["total_return_pct"], 21.0)

    def test_attribution_splits_pnl_by_sleeve_and_leaves_costs(self) -> None:
        days = pd.bdate_range("2024-01-02", periods=3)
        close = pd.DataFrame({GOLD: [5.0, 5.5, 6.5], "159611": [1.0, 1.0, 2.0]}, index=days)
        open_ = pd.DataFrame({GOLD: [5.0, 5.0, 6.0], "159611": [1.0, 1.0, 1.0]}, index=days)
        holdings = pd.DataFrame({GOLD: [0.0, 100_000.0, 100_000.0], "159611": [0.0, 0.0, 1_000.0]}, index=days)
        # gold: 100k * (5.5 - 5.0) intraday + 100k * (6.5 - 5.5) carry; sector: 1k * (2 - 1)
        gross = 150_000.0 + 1_000.0
        fees = 2_500.0
        result = RunResult(
            label="unit", start=str(days[0].date()), end=str(days[-1].date()),
            curve=pd.Series([INITIAL, INITIAL + 50_000.0, INITIAL + gross - fees], index=days),
            exposures=pd.Series(0.0, index=days), trace=[], trades=2, turnover=0.0,
            slippage=0.0005, holdings=holdings,
        )
        parts = study.attribute(result, close, open_)
        self.assertAlmostEqual(parts["gold"], 150_000.0 / INITIAL * 100, places=2)
        self.assertAlmostEqual(parts["satellite"], 1_000.0 / INITIAL * 100, places=2)
        self.assertAlmostEqual(parts["costs"], -fees / INITIAL * 100, places=2)
        self.assertAlmostEqual(parts["total"], (gross - fees) / INITIAL * 100, places=2)


if __name__ == "__main__":
    unittest.main()
