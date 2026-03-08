import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SIGNAL_SYSTEM_ROOT = ROOT / "signal_system"
for candidate in (str(ROOT), str(SIGNAL_SYSTEM_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from acceptance.run_daily_scan_acceptance import build_summary


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
                {"ts_code": "002444.SZ", "signal_type": "BUY", "score": 133, "reason": "趋势策略买入点"}
            ],
        }

        summary = build_summary(scan_result, min_candidates=1, min_buy_signals=1)

        self.assertTrue(summary["acceptance"]["passed"])
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

        summary = build_summary(scan_result, min_candidates=1, min_buy_signals=1)

        self.assertFalse(summary["acceptance"]["passed"])
        self.assertTrue(summary["acceptance"]["candidate_pool_ok"])
        self.assertFalse(summary["acceptance"]["buy_signals_ok"])


if __name__ == "__main__":
    unittest.main()
