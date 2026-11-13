"""Offline invariants for the PIT ETF universe and theme mapping."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import etf_pit_study as pit
import fetch_etf_pit_prices as prices
import fetch_etf_pit_universe as universe


class ThemeMapTests(unittest.TestCase):
    def test_longest_keyword_wins(self) -> None:
        self.assertEqual(pit.map_theme("创新药ETF"), "创新药")
        self.assertEqual(pit.map_theme("医疗ETF"), "医疗")
        self.assertEqual(pit.map_theme("券商ETF"), "证券")
        self.assertEqual(pit.map_theme("5GETF"), "5G")
        self.assertEqual(pit.map_theme("中证1000ETF"), "中证1000")

    def test_broad_or_off_pool_names_are_not_mapped(self) -> None:
        self.assertIsNone(pit.map_theme("沪深300ETF"))
        self.assertIsNone(pit.map_theme("红利ETF"))
        self.assertIsNone(pit.map_theme("国债ETF"))


class PriceFilterTests(unittest.TestCase):
    def test_domestic_equity_is_kept_and_other_assets_are_dropped(self) -> None:
        self.assertTrue(prices.is_domestic_equity("证券ETF"))
        self.assertTrue(prices.is_domestic_equity("半导体设备ETF"))
        self.assertFalse(prices.is_domestic_equity("国债ETF"))
        self.assertFalse(prices.is_domestic_equity("纳斯达克100ETF"))
        self.assertFalse(prices.is_domestic_equity("黄金ETF"))

    def test_sina_volume_is_converted_to_100_share_lots(self) -> None:
        raw = pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"],
            "open": [1.0, 1.1], "high": [1.1, 1.2], "low": [0.9, 1.0],
            "close": [1.0, 1.1], "volume": [1_000_000, 2_000_000],
            "amount": [1_000_000.0, 2_200_000.0],
        })
        frame = prices.normalise_sina(raw)
        self.assertEqual(frame["volume"].tolist(), [10_000.0, 20_000.0])
        self.assertEqual(frame.attrs["source"], "akshare_sina")


class UniverseTests(unittest.TestCase):
    def test_month_ends_include_the_requested_end_date(self) -> None:
        stamps = universe.month_ends(date(2024, 1, 1), date(2024, 3, 29))
        self.assertEqual(stamps[-1], date(2024, 3, 29))
        self.assertIn(date(2024, 1, 31), stamps)
        self.assertIn(date(2024, 2, 29), stamps)

    def test_annual_snapshots_include_year_end_and_end_date(self) -> None:
        stamps = universe.snapshot_dates(date(2023, 1, 1), date(2024, 3, 29), "A")
        self.assertIn(date(2023, 12, 31), stamps)
        self.assertEqual(stamps[-1], date(2024, 3, 29))

    def test_status_marks_funds_missing_from_recent_snapshots(self) -> None:
        snapshots = pd.DataFrame(
            [
                {"date": date(2020, 1, 31), "code": "510001", "name": "旧ETF", "exchange": "SSE", "shares": 1.0},
                {"date": date(2026, 10, 30), "code": "510001", "name": "旧ETF", "exchange": "SSE", "shares": 1.0},
                {"date": date(2020, 1, 31), "code": "510002", "name": "退市ETF", "exchange": "SSE", "shares": 1.0},
                {"date": date(2026, 10, 30), "code": "510003", "name": "新ETF", "exchange": "SSE", "shares": 1.0},
            ]
        )
        built = universe.build_universe(snapshots, date(2026, 10, 30)).set_index("code")
        self.assertEqual(built.loc["510001", "status"], "current")
        self.assertEqual(built.loc["510002", "status"], "delisted_or_stale")
        self.assertEqual(built.loc["510003", "status"], "current")


if __name__ == "__main__":
    unittest.main()
