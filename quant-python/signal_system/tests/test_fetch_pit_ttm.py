"""Offline checks for the TTM earnings builder."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fetch_pit_ttm import apply_quarterly_disclosure, build_ttm, normalize_delisted_quarters


def _interim(period: str, profit: float, available: str) -> dict[str, object]:
    stamp = pd.Timestamp(period)
    return {
        "symbol": "000001",
        "period": stamp,
        "report_year": stamp.year,
        "quarter": stamp.quarter,
        "net_profit_cum": profit,
        "revenue_cum": 100.0,
        "eps": 0.1,
        "roe": 1.0,
        "available_date": pd.Timestamp(available),
        "listed_status": "current",
        "source": "test",
    }


def _annual(period: str, profit: float, available: str) -> dict[str, object]:
    stamp = pd.Timestamp(period)
    return {
        "symbol": "000001",
        "period": stamp,
        "report_year": stamp.year,
        "quarter": 4,
        "net_profit_cum": profit,
        "revenue_cum": 400.0,
        "eps": 1.0,
        "roe": 4.0,
        "available_date": pd.Timestamp(available),
        "listed_status": "current",
        "source": "test",
    }


class TtmBuilderTests(unittest.TestCase):
    def test_ttm_uses_current_plus_prior_annual_minus_prior_same(self) -> None:
        interims = pd.DataFrame(
            [
                _interim("2020-03-31", 30.0, "2020-04-20"),
                _interim("2019-03-31", 20.0, "2019-04-20"),
            ]
        )
        annual = pd.DataFrame(
            [
                _annual("2019-12-31", 100.0, "2020-03-20"),
                _annual("2020-12-31", 120.0, "2021-03-20"),
            ]
        )
        panel = build_ttm(interims, annual).set_index("period")
        self.assertAlmostEqual(float(panel.loc[pd.Timestamp("2020-03-31"), "net_profit"]), 110.0)
        self.assertAlmostEqual(float(panel.loc[pd.Timestamp("2020-12-31"), "net_profit"]), 120.0)

    def test_missing_prior_same_quarter_is_dropped(self) -> None:
        interims = pd.DataFrame([_interim("2020-03-31", 30.0, "2020-04-20")])
        annual = pd.DataFrame([_annual("2019-12-31", 100.0, "2020-03-20")])
        panel = build_ttm(interims, annual)
        self.assertNotIn(pd.Timestamp("2020-03-31"), set(panel["period"]))
        self.assertIn(pd.Timestamp("2019-12-31"), set(panel["period"]))

    def test_delisted_interims_keep_q1_to_q3(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw_dir = Path(temp) / "financials" / "profit_delisted"
            raw_dir.mkdir(parents=True)
            pd.DataFrame(
                [
                    {
                        "SECURITY_CODE": "000004",
                        "REPORT_DATE": "2024-09-30",
                        "NOTICE_DATE": "2024-10-26",
                        "PARENT_NETPROFIT": -35.0,
                        "TOTAL_OPERATE_INCOME": 75.0,
                        "BASIC_EPS": -0.27,
                    },
                    {
                        "SECURITY_CODE": "000004",
                        "REPORT_DATE": "2024-12-31",
                        "NOTICE_DATE": "2025-04-29",
                        "PARENT_NETPROFIT": -131.0,
                        "TOTAL_OPERATE_INCOME": 98.0,
                        "BASIC_EPS": -0.99,
                    },
                ]
            ).to_csv(raw_dir / "000004.csv", index=False)
            panel = normalize_delisted_quarters(Path(temp))
        self.assertEqual(len(panel), 1)
        self.assertEqual(panel.iloc[0]["period"], pd.Timestamp("2024-09-30"))
        self.assertEqual(panel.iloc[0]["quarter"], 3)

    def test_disclosure_replaces_latest_announcement_date(self) -> None:
        interims = pd.DataFrame(
            [
                _interim("2020-03-31", 30.0, "2025-01-01"),
                {
                    **_interim("2020-03-31", 30.0, "2025-01-01"),
                    "symbol": "000002",
                },
            ]
        )
        disclosure = pd.DataFrame(
            [
                {
                    "symbol": "000001",
                    "period": pd.Timestamp("2020-03-31"),
                    "actual_disclosure": pd.Timestamp("2020-04-20"),
                }
            ]
        )
        panel = apply_quarterly_disclosure(interims, disclosure)
        self.assertEqual(len(panel), 1)
        self.assertEqual(panel.iloc[0]["symbol"], "000001")
        self.assertEqual(panel.iloc[0]["available_date"], pd.Timestamp("2020-04-20"))


if __name__ == "__main__":
    unittest.main()
