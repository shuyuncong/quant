"""Semantic guards for the fundamental + momentum research study.

These tests pin the rules the report depends on, not the replay plumbing:

* estimated announcement dates are pushed to the statutory deadline, so a
  report cannot become visible before it could have been disclosed;
* a report is never used on its announcement date or before it;
* inactive long-short months stay in the return series as zero, not omitted;
* the long-only simulator never spends more cash than it has and only trades
  whole 100-share lots.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import fundamental_momentum_study as study


class AvailabilityTests(unittest.TestCase):
    def test_statutory_deadlines(self) -> None:
        cases = {
            "2024-03-31": "2024-04-30",
            "2024-06-30": "2024-08-31",
            "2024-09-30": "2024-10-31",
            "2024-12-31": "2025-04-30",
        }
        for period, expected in cases.items():
            self.assertEqual(
                study.statutory_deadline(pd.Timestamp(period)),
                pd.Timestamp(expected),
                period,
            )

    def test_estimated_announcement_waits_for_deadline(self) -> None:
        self.assertEqual(
            study.report_availability(
                pd.Timestamp("2024-03-31"),
                pd.Timestamp("2024-04-20"),
                True,
            ),
            pd.Timestamp("2024-04-30"),
        )

    def test_late_estimated_announcement_is_not_moved_earlier(self) -> None:
        self.assertEqual(
            study.report_availability(
                pd.Timestamp("2024-03-31"),
                pd.Timestamp("2024-05-10"),
                True,
            ),
            pd.Timestamp("2024-05-10"),
        )

    def test_exact_announcement_is_used(self) -> None:
        self.assertEqual(
            study.report_availability(
                pd.Timestamp("2024-03-31"),
                pd.Timestamp("2024-04-20"),
                False,
            ),
            pd.Timestamp("2024-04-20"),
        )

    def test_report_never_available_on_or_before_period_end(self) -> None:
        self.assertEqual(
            study.report_availability(
                pd.Timestamp("2024-03-31"),
                pd.Timestamp("2024-03-01"),
                False,
            ),
            pd.Timestamp("2024-04-01"),
        )


class FinancialLoaderTests(unittest.TestCase):
    def test_roe_yoy_uses_same_quarter_previous_year(self) -> None:
        rows = [
            {
                "symbol": "000001",
                "period": "2023-03-31",
                "ann_date": "2023-04-25",
                "ann_date_estimated": False,
                "roe": 2.0,
                "debt_ratio": 50.0,
            },
            {
                "symbol": "000001",
                "period": "2024-03-31",
                "ann_date": "2024-04-25",
                "ann_date_estimated": False,
                "roe": 2.5,
                "debt_ratio": 49.0,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "financial.jsonl"
            path.write_text(
                "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
            )
            frame = study.load_financials(path)
        latest = frame[frame["period_date"] == pd.Timestamp("2024-03-31")].iloc[0]
        self.assertAlmostEqual(float(latest["roe_yoy"]), 0.5)
        self.assertAlmostEqual(float(latest["debt_yoy"]), -1.0)


class PanelAvailabilityTests(unittest.TestCase):
    def test_report_available_after_signal_is_not_used(self) -> None:
        days = pd.bdate_range("2019-01-01", periods=700)
        symbols = ["000001", "000002"]
        close = pd.DataFrame(
            {symbol: np.linspace(10.0, 20.0, len(days)) for symbol in symbols},
            index=days,
        )
        open_ = close.copy()
        volume = pd.DataFrame(1_000_000.0, index=days, columns=symbols)
        signal_dates = study.month_end_dates(days)
        signal_dates = signal_dates[
            (signal_dates >= pd.Timestamp("2019-01-01"))
            & (signal_dates <= pd.Timestamp("2021-06-30"))
        ]
        pe = pd.DataFrame(10.0, index=signal_dates, columns=symbols)
        mv = pd.DataFrame(1e10, index=signal_dates, columns=symbols)

        rows = [
            {
                "symbol": "000001",
                "period": "2021-03-31",
                "ann_date": "2021-05-05",
                "ann_date_estimated": False,
                "roe": 10.0,
                "debt_ratio": 50.0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "financial.jsonl"
            path.write_text(
                "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
            )
            financial = study.load_financials(path)

        panel, _ = study.build_panel(
            financial,
            close,
            open_,
            volume,
            pe,
            mv,
            symbols,
            start=pd.Timestamp("2019-01-01"),
            end=pd.Timestamp("2021-06-30"),
        )

        april = panel[
            (panel["date"] == pd.Timestamp("2021-04-30"))
            & (panel["symbol"] == "000001")
        ].iloc[0]
        may = panel[
            (panel["date"] == pd.Timestamp("2021-05-31"))
            & (panel["symbol"] == "000001")
        ].iloc[0]
        self.assertTrue(pd.isna(april["roe"]))
        self.assertEqual(float(may["roe"]), 10.0)


class LongShortSeriesTests(unittest.TestCase):
    def test_inactive_months_are_kept_as_zero(self) -> None:
        date_active = pd.Timestamp("2024-01-31")
        date_inactive = pd.Timestamp("2024-02-29")
        active_rows = []
        for i in range(60):
            active_rows.append(
                {
                    "date": date_active,
                    "symbol": f"{i:06d}",
                    "eligible": True,
                    "score": float(i),
                    "fwd_1m": float(i) / 100.0,
                }
            )
        inactive_rows = [
            {
                "date": date_inactive,
                "symbol": f"{i:06d}",
                "eligible": True,
                "score": float(i),
                "fwd_1m": 0.0,
            }
            for i in range(10)
        ]
        panel = pd.DataFrame(active_rows + inactive_rows)
        result = study.simulate_long_short(
            panel,
            column="score",
            quantiles=5,
            min_names=60,
            cost_per_active_month=0.0,
        )
        self.assertEqual(result["months"], 2)
        self.assertEqual(result["active_months"], 1)
        self.assertEqual(result["gross_win_rate_full_pct"], 50.0)
        active = panel[panel["date"] == date_active].copy()
        active["bucket"] = pd.qcut(
            active["score"].rank(method="first"), 5, labels=False
        )
        bucket_means = active.groupby("bucket")["fwd_1m"].mean()
        spread = float(bucket_means.iloc[-1] - bucket_means.iloc[0])
        self.assertAlmostEqual(
            result["gross_annualized_pct"],
            spread * 12 * 100 / 2,
            places=6,
        )


class LongOnlyCostTests(unittest.TestCase):
    def test_whole_lots_and_non_negative_cash(self) -> None:
        days = pd.bdate_range("2024-01-02", periods=10)
        symbols = ["000001", "000002"]
        close = pd.DataFrame(
            {"000001": 10.0, "000002": 20.0}, index=days
        )
        open_ = close.copy()
        signal_dates = pd.DatetimeIndex([days[0], days[5]])
        panel = pd.DataFrame(
            [
                {
                    "date": signal_dates[0],
                    "symbol": "000001",
                    "eligible": True,
                    "score": 1.0,
                },
                {
                    "date": signal_dates[0],
                    "symbol": "000002",
                    "eligible": True,
                    "score": 2.0,
                },
                {
                    "date": signal_dates[1],
                    "symbol": "000001",
                    "eligible": True,
                    "score": 2.0,
                },
                {
                    "date": signal_dates[1],
                    "symbol": "000002",
                    "eligible": True,
                    "score": 1.0,
                },
            ]
        )
        result = study.simulate_long_only(
            panel,
            close,
            open_,
            signal_dates,
            score_column="score",
            top_n=1,
            market_ma=None,
            start=signal_dates[0],
            end=signal_dates[1],
        )
        self.assertGreaterEqual(result["metrics"]["min_cash"], 0.0)
        self.assertFalse(result["trades"].empty)
        for quantity in result["trades"]["quantity"]:
            self.assertEqual(int(quantity) % 100, 0)


if __name__ == "__main__":
    unittest.main()
