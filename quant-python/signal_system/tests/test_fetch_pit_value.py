"""Offline checks for the PIT annual-earnings normalizer."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fetch_pit_value import build_panel, normalize_delisted, normalize_listed


class PitValueNormalizerTests(unittest.TestCase):
    def test_listed_keeps_first_disclosure(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "股票代码": "000001",
                    "净利润-净利润": 10.0,
                    "营业总收入-营业总收入": 100.0,
                    "每股收益": 0.1,
                    "净资产收益率": 5.0,
                    "最新公告日期": "2020-04-20",
                },
                {
                    "股票代码": "000001",
                    "净利润-净利润": 12.0,
                    "营业总收入-营业总收入": 110.0,
                    "每股收益": 0.12,
                    "净资产收益率": 6.0,
                    "最新公告日期": "2020-04-01",
                },
            ]
        )
        panel = normalize_listed(raw, 2019)
        self.assertEqual(len(panel), 1)
        self.assertEqual(float(panel.iloc[0]["net_profit"]), 12.0)
        self.assertEqual(panel.iloc[0]["available_date"], pd.Timestamp("2020-04-01"))

    def test_delisted_keeps_annual_rows_only(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "SECURITY_CODE": "000004",
                    "REPORT_DATE": "2024-12-31",
                    "NOTICE_DATE": "2025-04-29",
                    "PARENT_NETPROFIT": -131.0,
                    "TOTAL_OPERATE_INCOME": 98.0,
                    "BASIC_EPS": -0.99,
                },
                {
                    "SECURITY_CODE": "000004",
                    "REPORT_DATE": "2024-09-30",
                    "NOTICE_DATE": "2024-10-26",
                    "PARENT_NETPROFIT": -35.0,
                    "TOTAL_OPERATE_INCOME": 75.0,
                    "BASIC_EPS": -0.27,
                },
            ]
        )
        panel = normalize_delisted(raw)
        self.assertEqual(len(panel), 1)
        self.assertEqual(int(panel.iloc[0]["report_year"]), 2024)
        self.assertEqual(float(panel.iloc[0]["net_profit"]), -131.0)

    def test_build_panel_drops_undated_rows(self) -> None:
        columns = [
            "symbol",
            "report_year",
            "net_profit",
            "revenue",
            "eps",
            "roe",
            "available_date",
            "pit_available",
            "listed_status",
            "source",
        ]
        listed = pd.DataFrame(
            [
                {
                    "symbol": "000001",
                    "report_year": 2019,
                    "net_profit": 10.0,
                    "revenue": 100.0,
                    "eps": 0.1,
                    "roe": 5.0,
                    "available_date": "2020-04-01",
                    "pit_available": True,
                    "listed_status": "current",
                    "source": "test",
                }
            ],
            columns=columns,
        )
        delisted = pd.DataFrame(columns=columns)
        panel = build_panel(Path("."), listed, delisted)
        self.assertEqual(len(panel), 1)
        self.assertEqual(panel.iloc[0]["symbol"], "000001")


if __name__ == "__main__":
    unittest.main()
