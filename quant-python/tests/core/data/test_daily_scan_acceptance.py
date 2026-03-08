import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SIGNAL_SYSTEM_ROOT = ROOT / "signal_system"
for candidate in (str(ROOT), str(SIGNAL_SYSTEM_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from acceptance.run_daily_scan_acceptance import build_multi_group_report, build_summary


class DailyScanAcceptanceTest(unittest.TestCase):
    def test_build_summary_marks_acceptance_pass_when_thresholds_are_met(self):
        scan_result = {
            "market_status": "bull",
            "stats": {
                "candidate_pool_count": 1,
                "buy_signals_count": 1,
            },
            "candidate_pool": [
                {"ts_code": "002444.SZ", "score": 100, "passed_checks": ["fundamental", "turnover", "volume_price"]}
            ],
            "buy_signals": [
                {"ts_code": "002444.SZ", "signal_type": "BUY", "score": 133, "reason": "trend entry"}
            ],
        }

        summary = build_summary(
            scan_result,
            min_candidates=1,
            min_buy_signals=1,
            group_name="curated_40",
            watchlist_size=40,
        )

        self.assertTrue(summary["acceptance"]["passed"])
        self.assertEqual(summary["group_name"], "curated_40")
        self.assertEqual(summary["candidate_pool"][0]["ts_code"], "002444.SZ")
        self.assertEqual(summary["buy_signals"][0]["signal_type"], "BUY")

    def test_build_summary_marks_acceptance_fail_when_buy_signals_are_missing(self):
        scan_result = {
            "market_status": "bull",
            "stats": {
                "candidate_pool_count": 1,
                "buy_signals_count": 0,
            },
            "candidate_pool": [
                {"ts_code": "002444.SZ", "score": 100, "passed_checks": ["fundamental", "turnover", "volume_price"]}
            ],
            "buy_signals": [],
        }

        summary = build_summary(
            scan_result,
            min_candidates=1,
            min_buy_signals=1,
            group_name="curated_40",
            watchlist_size=40,
        )

        self.assertFalse(summary["acceptance"]["passed"])
        self.assertTrue(summary["acceptance"]["candidate_pool_ok"])
        self.assertFalse(summary["acceptance"]["buy_signals_ok"])

    def test_build_multi_group_report_aggregates_pass_status(self):
        report = build_multi_group_report(
            [
                {
                    "stats": {"candidate_pool_count": 1, "buy_signals_count": 1},
                    "acceptance": {"passed": True},
                },
                {
                    "stats": {"candidate_pool_count": 0, "buy_signals_count": 0},
                    "acceptance": {"passed": False},
                },
            ]
        )

        self.assertEqual(report["aggregate"]["group_count"], 2)
        self.assertEqual(report["aggregate"]["passed_group_count"], 1)
        self.assertFalse(report["aggregate"]["all_passed"])
        self.assertEqual(report["aggregate"]["total_candidate_pool_count"], 1)
        self.assertEqual(report["aggregate"]["total_buy_signals_count"], 1)


if __name__ == "__main__":
    unittest.main()
