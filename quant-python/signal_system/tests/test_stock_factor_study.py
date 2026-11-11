"""Offline invariants for the PIT stock factor study."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from smallcap_optimized_study import OptConfig
from stock_factor_study import (
    information_coefficients,
    load_surprise,
    score_table,
    select_targets,
)

SYMBOLS = ["000001", "000002", "000003", "000004"]
SIGNAL = pd.Timestamp("2021-05-10")


def _cfg(**overrides: object) -> OptConfig:
    params: dict[str, object] = dict(
        max_names=2, rank_low=1, rank_high=999999, rank_pct_low=0.0, rank_pct_high=1.0,
        max_sector_weight=0.5,
    )
    params.update(overrides)
    return OptConfig(**params)


def _features(days: pd.DatetimeIndex) -> dict[str, object]:
    return {
        "close": pd.DataFrame(10.0, index=days, columns=SYMBOLS),
        "mv": pd.DataFrame({s: 1e9 * (i + 1) for i, s in enumerate(SYMBOLS)}, index=days),
        "rank": pd.DataFrame({s: float(i + 1) for i, s in enumerate(SYMBOLS)}, index=days),
        "valid": pd.DataFrame(True, index=days, columns=SYMBOLS),
    }


def _reports(rows: list[tuple[str, str, float, str]]) -> pd.DataFrame:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "value.csv"
        pd.DataFrame(
            [
                {"symbol": symbol, "report_year": int(period[:4]), "period": period,
                 "net_profit": 1.0, "revenue": 1.0, "roe": roe, "available_date": available}
                for symbol, period, roe, available in rows
            ]
        ).to_csv(path, index=False)
        return load_surprise(path)


class SurpriseTests(unittest.TestCase):
    def test_roe_change_compares_the_same_period_a_year_earlier(self) -> None:
        reports = _reports([
            ("000001", "2020-03-31", 2.0, "2020-04-20"),
            ("000001", "2020-12-31", 9.0, "2021-03-30"),
            ("000001", "2021-03-31", 3.5, "2021-04-20"),
        ])
        latest = reports.set_index("period").loc[pd.Timestamp("2021-03-31")]
        # Against 2020Q1 (2.0), not against the preceding annual report (9.0).
        self.assertAlmostEqual(float(latest["roe_yoy"]), 1.5)
        first = reports.set_index("period").loc[pd.Timestamp("2020-03-31")]
        self.assertTrue(np.isnan(float(first["roe_yoy"])))

    def test_surprise_is_point_in_time_and_expires(self) -> None:
        days = pd.bdate_range("2021-05-03", periods=6)
        reports = _reports([
            # announced 20 days before the signal: usable
            ("000001", "2020-03-31", 2.0, "2020-04-20"),
            ("000001", "2021-03-31", 3.0, "2021-04-20"),
            # announced after the signal: the stale 2020 report is all that is known
            ("000002", "2020-03-31", 2.0, "2020-04-20"),
            ("000002", "2021-03-31", 9.0, "2021-05-11"),
            # announced more than 90 days before the signal: expired
            ("000003", "2019-12-31", 2.0, "2020-04-20"),
            ("000003", "2020-12-31", 6.0, "2021-01-29"),
        ])
        factors = {
            "vol": pd.DataFrame({s: 0.01 * (i + 1) for i, s in enumerate(SYMBOLS)}, index=days),
            "ret": pd.DataFrame(0.0, index=days, columns=SYMBOLS),
        }
        adv20 = pd.DataFrame(1e9, index=days, columns=SYMBOLS)
        table = score_table(_features(days), adv20, factors, reports, _cfg(), SIGNAL)

        self.assertAlmostEqual(float(table.loc["000001", "surprise"]), 1.0)
        for symbol in ("000002", "000003", "000004"):
            self.assertTrue(np.isnan(float(table.loc[symbol, "surprise"])), symbol)
        # Lowest volatility ranks highest; a missing surprise counts as neutral.
        self.assertEqual(table["lowvol_r"].idxmax(), "000001")
        expected = (table.loc["000004", "lowvol_r"] + table.loc["000004", "reversal_r"] + 0.5) / 3.0
        self.assertAlmostEqual(float(table.loc["000004", "composite"]), float(expected))


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = pd.DataFrame(
            {
                "mv": [4e9, 3e9, 2e9, 1e9],
                "lowvol": [0.4, 0.3, 0.2, 0.1],
                "surprise": [2.0, -1.0, np.nan, 0.5],
                "composite": [0.9, 0.8, 0.7, 0.6],
            },
            index=SYMBOLS,
        )

    def test_surprise_arm_holds_only_positive_surprises(self) -> None:
        industry = {symbol: symbol for symbol in SYMBOLS}
        selected = select_targets(self.table, "surprise", industry, _cfg(max_names=4, max_sector_weight=1.0))
        self.assertEqual(selected, ["000001", "000004"])

    def test_sector_cap_skips_to_the_next_ranked_name(self) -> None:
        industry = {"000001": "bank", "000002": "bank", "000003": "tech", "000004": "tech"}
        selected = select_targets(self.table, "composite", industry, _cfg())
        self.assertEqual(selected, ["000001", "000003"])

    def test_size_arm_takes_the_smallest(self) -> None:
        industry = {symbol: symbol for symbol in SYMBOLS}
        selected = select_targets(self.table, "size", industry, _cfg())
        self.assertEqual(selected, ["000004", "000003"])


class InformationCoefficientTests(unittest.TestCase):
    def test_ic_uses_the_return_to_the_next_signal(self) -> None:
        symbols = [f"{index:06d}" for index in range(1, 41)]
        first, second = pd.Timestamp("2021-01-29"), pd.Timestamp("2021-02-26")
        score = pd.Series(np.arange(40, dtype=float), index=symbols)
        close = pd.DataFrame({first: 10.0, second: 10.0 + score}).T
        table = pd.DataFrame({"lowvol": score, "reversal": -score, "surprise": np.nan, "composite": score})
        ic = information_coefficients({first: table, second: table}, close)
        self.assertEqual(list(ic.index), [first])
        self.assertAlmostEqual(float(ic.loc[first, "lowvol"]), 1.0)
        self.assertAlmostEqual(float(ic.loc[first, "reversal"]), -1.0)
        self.assertTrue(np.isnan(float(ic.loc[first, "surprise"])))


if __name__ == "__main__":
    unittest.main()
