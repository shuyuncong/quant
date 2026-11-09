"""Offline checks for the quality-filter A/B helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from quality_filter_study import (
    Config,
    _rebalance_map,
    extend_delisted_panels,
    extend_panels,
    latest_quality,
    quality_pass,
)


def _row(**overrides):
    row = {
        "归母净利润": 100_000_000.0,
        "扣非净利润": 80_000_000.0,
        "经营现金流量净额": 50_000_000.0,
        "营业总收入": 200_000_000.0,
        "净资产收益率(ROE)": 10.0,
        "总资产报酬率(ROA)": 5.0,
        "资产负债率": 50.0,
        "股东权益合计(净资产)": 1_000_000_000.0,
        "商誉": 100_000_000.0,
    }
    row.update(overrides)
    return pd.Series(row)


class QualityFilterTests(unittest.TestCase):
    def test_quality_pass_thresholds(self) -> None:
        self.assertTrue(quality_pass(_row(), 8.0, 4.0))
        self.assertFalse(quality_pass(_row(), 15.0, 10.0))
        self.assertFalse(quality_pass(_row(**{"归母净利润": -1.0}), 8.0, 4.0))
        self.assertFalse(quality_pass(_row(**{"资产负债率": 71.0}), 8.0, 4.0))
        self.assertFalse(quality_pass(_row(**{"商誉": 400_000_000.0}), 8.0, 4.0))
        self.assertFalse(quality_pass(_row(**{"经营现金流量净额": None}), 8.0, 4.0))

    def test_latest_quality_is_asof(self) -> None:
        quality = pd.DataFrame(
            [
                {"symbol": "000001", "report_year": 2018, "quality_date": pd.Timestamp("2019-04-20"), "value": 1.0},
                {"symbol": "000001", "report_year": 2019, "quality_date": pd.Timestamp("2020-04-20"), "value": 2.0},
                {"symbol": "000002", "report_year": 2018, "quality_date": pd.Timestamp("2019-04-21"), "value": 3.0},
            ]
        )
        before = latest_quality(quality, pd.Timestamp("2020-04-19"))
        self.assertEqual(float(before.loc["000001", "value"]), 1.0)
        after = latest_quality(quality, pd.Timestamp("2020-04-20"))
        self.assertEqual(float(after.loc["000001", "value"]), 2.0)

    def test_extend_panels_splices_and_scales(self) -> None:
        import tempfile

        days = pd.bdate_range("2024-12-30", periods=2)
        panels = {
            "open": pd.DataFrame({"000001": [10.0, 10.0]}, index=days),
            "high": pd.DataFrame({"000001": [10.0, 10.0]}, index=days),
            "low": pd.DataFrame({"000001": [10.0, 10.0]}, index=days),
            "close": pd.DataFrame({"000001": [10.0, 10.0]}, index=days),
            "volume": pd.DataFrame({"000001": [1.0, 1.0]}, index=days),
            "amount": pd.DataFrame({"000001": [1.0, 1.0]}, index=days),
            "turnover_rate": pd.DataFrame({"000001": [1.0, 1.0]}, index=days),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "000001.csv").write_text(
                "date,open,high,low,close,volume,amount,outstanding_share,turnover\n"
                "2025-01-02,100,100,100,100,10,1000,1000,0.01\n"
                "2025-01-03,110,110,110,110,10,1100,1000,0.01\n",
                encoding="utf-8",
            )
            extended = extend_panels(panels, root, ["000001"])
            self.assertEqual(float(extended["close"].loc["2025-01-02", "000001"]), 10.0)
            self.assertAlmostEqual(
                float(extended["close"].loc["2025-01-03", "000001"]), 11.0
            )
            self.assertAlmostEqual(
                float(extended["turnover_rate"].loc["2025-01-02", "000001"]), 1.0
            )

    def test_delisted_panels_add_new_symbol(self) -> None:
        import tempfile

        days = pd.bdate_range("2024-12-30", periods=2)
        panels = {
            column: pd.DataFrame({"000001": [10.0, 10.0]}, index=days)
            for column in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "000002.csv").write_text(
                "日期,开盘,收盘,最高,最低,成交量,成交额,换手率\n"
                "2024-12-30,5,5.1,5.2,4.9,1000,5100,1.5\n",
                encoding="utf-8",
            )
            extended = extend_delisted_panels(panels, root)
            self.assertIn("000002", extended["close"].columns)
            self.assertAlmostEqual(
                float(extended["close"].loc["2024-12-30", "000002"]), 5.1
            )

    def test_rebalance_map_uses_next_session(self) -> None:
        days = pd.bdate_range("2019-12-30", periods=45)
        cfg = Config(start="2020-01-01", end="2020-02-29")
        mapping = _rebalance_map(days, cfg)
        self.assertIn(pd.Timestamp("2020-01-01"), mapping)
        self.assertEqual(mapping[pd.Timestamp("2020-01-01")], pd.Timestamp("2019-12-31"))


if __name__ == "__main__":
    unittest.main()
