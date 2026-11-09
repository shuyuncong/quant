"""Offline invariants for the stock PIT data builder."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from stock_pit_data import (
    _em_symbol,
    _number,
    _quarter_ends,
    _sina_symbol,
    _symbol,
    build_manifest,
    normalize_financial_abstract,
)


class StockPitDataTests(unittest.TestCase):
    def test_symbol_normalization(self) -> None:
        self.assertEqual(_symbol("SH600000"), "600000")
        self.assertEqual(_symbol("sz000001"), "000001")
        self.assertEqual(_symbol("000001.SZ"), "000001")
        self.assertEqual(_symbol(1), "000001")

    def test_em_symbol_mapping(self) -> None:
        self.assertEqual(_em_symbol("600000"), "SH600000")
        self.assertEqual(_em_symbol("000001"), "SZ000001")
        self.assertEqual(_em_symbol("830001"), "BJ830001")
        self.assertEqual(_sina_symbol("600000"), "sh600000")
        self.assertEqual(_sina_symbol("000001"), "sz000001")
        self.assertEqual(_sina_symbol("830001"), "bj830001")

    def test_quarter_ends_are_weekdays(self) -> None:
        dates = _quarter_ends([2024])
        self.assertEqual(dates, ["2024-03-29", "2024-06-28", "2024-09-30", "2024-12-31"])
        self.assertTrue(all(datetime_weekday(date) < 5 for date in dates))

    def test_number_parsing(self) -> None:
        self.assertEqual(_number("1,234.5"), 1234.5)
        self.assertEqual(_number("12.3%"), 12.3)
        self.assertIsNone(_number("--"))
        self.assertIsNone(_number(None))

    def test_annual_abstract_joins_disclosure_and_marks_pit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            abstract = root / "financials" / "abstract"
            abstract.mkdir(parents=True)
            (abstract / "000001.csv").write_text(
                "选项,指标,20231231,20241231\n"
                "常用指标,归母净利润,1000000,1200000\n"
                "常用指标,商誉,200000,210000\n"
                "常用指标,资产负债率,52.1,51.0\n",
                encoding="utf-8",
            )
            disclosure = root / "disclosure"
            disclosure.mkdir()
            (disclosure / "disclosure_annual.csv").write_text(
                "symbol,report_year,actual_disclosure\n"
                "000001,2023,2024-03-20\n"
                "000001,2024,2025-04-01\n",
                encoding="utf-8",
            )
            errors: list[dict[str, str]] = []
            normalize_financial_abstract(root, errors)
            panel = __import__("pandas").read_csv(
                root / "financials" / "abstract_annual.csv", dtype={"symbol": str}
            )
            self.assertEqual(errors, [])
            self.assertEqual(len(panel), 6)
            self.assertTrue(panel["pit_available"].all())
            self.assertEqual(
                panel.loc[panel["report_year"] == 2023, "statutory_deadline"].iloc[0],
                "2024-04-30",
            )
            self.assertEqual(
                panel.loc[panel["report_year"] == 2023, "available_date"].iloc[0],
                "2024-03-20",
            )

    def test_manifest_records_files_and_unavailable_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "universe").mkdir()
            (root / "universe" / "csi1000_current.csv").write_text(
                "symbol,name\n000001,test\n", encoding="utf-8"
            )
            build_manifest(root, [{"step": "x", "error": "y"}])
            manifest = (root / "manifest.json").read_text(encoding="utf-8")
            self.assertIn("csi1000_current.csv", manifest)
            self.assertIn("historical PIT CSI 1000 constituents", manifest)
            self.assertIn("structured audit opinions", manifest)
            self.assertIn('"errors"', manifest)


def datetime_weekday(value: str) -> int:
    import datetime as _datetime

    return _datetime.date.fromisoformat(value).weekday()


if __name__ == "__main__":
    unittest.main()
