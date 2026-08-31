import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.selector.fundamental import (
    evaluate_fundamental,
    filter_buy_events_by_fundamental,
    load_fundamental_history,
    select_historical_snapshot,
)


def _config(**overrides):
    fundamental = {
        "min_roe": 10,
        "max_debt_ratio": 50,
        "max_pe": 30,
        "min_market_cap": 50,
        "max_market_cap": 500,
    }
    fundamental.update(overrides)
    return {
        "strategy": {"fundamental": fundamental},
        "backtest": {
            "fundamental": {
                "enabled": True,
                "missing_data_policy": "unavailable",
            }
        },
    }


class FundamentalContractTest(unittest.TestCase):
    def test_historical_mode_is_disabled_without_explicit_backtest_flag(self):
        result = evaluate_fundamental(
            {
                "roe": 1,
                "debt_ratio": 99,
                "pe": 100,
                "market_cap": 1,
            },
            {"strategy": {"fundamental": {"min_roe": 10}}},
            as_of="2025-03-01",
            context="historical",
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.status, "disabled")

    def test_live_snapshot_passes_and_exposes_metrics(self):
        result = evaluate_fundamental(
            {
                "roe": 12,
                "debt_ratio": 40,
                "pe": 15,
                "market_cap": 120,
                "source": "test",
            },
            _config(),
            context="live",
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.metrics["data_status"], "available")

    def test_live_snapshot_rejects_each_out_of_range_value(self):
        result = evaluate_fundamental(
            {"roe": 5, "debt_ratio": 70, "pe": 40, "market_cap": 700},
            _config(),
            context="live",
        )
        self.assertFalse(result.passed)
        self.assertEqual(
            set(result.reasons),
            {
                "fundamental_roe_below_min",
                "fundamental_debt_ratio_above_max",
                "fundamental_pe_out_of_range",
                "fundamental_market_cap_out_of_range",
            },
        )

    def test_historical_period_without_announcement_date_is_unavailable(self):
        result = evaluate_fundamental(
            {
                "fundamental_snapshot": {
                    "period": "20241231",
                    "roe": 12,
                    "debt_to_assets": 40,
                    "pe": 15,
                    "market_cap": 120,
                }
            },
            _config(),
            as_of="2025-03-01",
            context="historical",
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("fundamental_snapshot_has_no_available_date", result.warnings)

    def test_historical_future_snapshot_is_unavailable(self):
        result = evaluate_fundamental(
            {
                "fundamental_snapshot": {
                    "ann_date": "2025-04-01",
                    "roe": 12,
                    "debt_to_assets": 40,
                    "pe": 15,
                    "market_cap": 120,
                }
            },
            _config(),
            as_of="2025-03-01",
            context="historical",
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("fundamental_snapshot_after_signal_day", result.warnings)

    def test_historical_reject_policy_does_not_fail_open(self):
        config = _config()
        config["backtest"]["fundamental"]["missing_data_policy"] = "reject"
        result = evaluate_fundamental(
            {}, config, as_of=date(2025, 3, 1), context="historical"
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reasons, ("fundamental_data_unavailable",))

    def test_selects_latest_snapshot_not_later_than_signal_day(self):
        snapshot = select_historical_snapshot(
            [
                {"ann_date": "2024-12-31", "roe": 8},
                {"ann_date": "2025-02-15", "roe": 12},
                {"ann_date": "2025-04-01", "roe": 20},
            ],
            "2025-03-01",
        )
        self.assertEqual(snapshot["roe"], 12)

    def test_loads_mapping_and_jsonl_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mapping = root / "fundamentals.json"
            mapping.write_text(
                json.dumps(
                    {
                        "000001.SZ": [
                            {"ann_date": "2025-01-01", "roe": 12}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_fundamental_history(mapping)
            self.assertIn("000001", loaded)

            records = root / "fundamentals.jsonl"
            records.write_text(
                json.dumps(
                    {"ts_code": "000002.SZ", "ann_date": "2025-01-02", "roe": 11}
                )
                + "\n",
                encoding="utf-8",
            )
            loaded_jsonl = load_fundamental_history(records)
            self.assertEqual(loaded_jsonl["000002"][0]["roe"], 11)

    def test_filter_preserves_unavailable_candidates_with_metadata(self):
        events = {
            "buy": [
                {"day": "2025-03-01", "signal_type": "buy_1"},
            ],
            "sell": [],
        }
        filtered, skipped, details = filter_buy_events_by_fundamental(
            "000001.SZ", events, {}, _config()
        )
        self.assertEqual(len(filtered["buy"]), 1)
        self.assertEqual(filtered["buy"][0]["fundamental_status"], "unavailable")
        self.assertFalse(skipped)
        self.assertEqual(details[0]["status"], "unavailable")

    def test_filter_uses_only_snapshot_announced_before_event(self):
        config = _config()
        events = {"buy": [{"day": "2025-03-01", "signal_type": "buy_1"}], "sell": []}
        history = {
            "000001": [
                {
                    "ann_date": "2025-02-01",
                    "roe": 12,
                    "debt_to_assets": 40,
                    "pe": 15,
                    "market_cap": 120,
                },
                {
                    "ann_date": "2025-04-01",
                    "roe": 2,
                    "debt_to_assets": 90,
                    "pe": 80,
                    "market_cap": 900,
                },
            ]
        }
        filtered, skipped, details = filter_buy_events_by_fundamental(
            "000001.SZ", events, history, config
        )
        self.assertEqual(len(filtered["buy"]), 1)
        self.assertEqual(filtered["buy"][0]["fundamental_status"], "passed")
        self.assertFalse(skipped)
        self.assertFalse(details)


if __name__ == "__main__":
    unittest.main()
