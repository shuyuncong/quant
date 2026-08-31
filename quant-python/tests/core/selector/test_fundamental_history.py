import json
import tempfile
import unittest
from pathlib import Path

from core.selector.fundamental import evaluate_fundamental
from core.selector.fundamental_history import (
    build_point_in_time_history,
    history_coverage_report,
    load_history_records,
    main,
    normalize_history_record,
    write_fundamental_history,
)


def _config():
    return {
        "strategy": {
            "fundamental": {
                "min_roe": 10,
                "max_debt_ratio": 60,
                "max_pe": 30,
                "min_market_cap": 50,
                "max_market_cap": 3000,
            }
        },
        "backtest": {
            "fundamental": {"enabled": True, "missing_data_policy": "reject"}
        },
    }


class FundamentalHistoryTest(unittest.TestCase):
    def test_normalizes_aliases_and_market_cap_units(self):
        result, issue = normalize_history_record(
            {
                "ts_code": "1.SZ",
                "公告日期": "20250301",
                "资产负债率": "40%",
                "净资产收益率": "12.5%",
                "市盈率(TTM)": "15.2",
                "流通市值": "1200000",
                "市值单位": "万元",
            },
            kind="joined",
        )
        self.assertIsNone(issue)
        self.assertEqual(result["symbol"], "000001")
        self.assertEqual(result["ann_date"], "2025-03-01")
        self.assertAlmostEqual(result["market_cap"], 120.0)

    def test_infers_ten_thousand_cny_for_circ_mv_without_unit(self):
        result, issue = normalize_history_record(
            {
                "symbol": "000001.SZ",
                "trade_date": "2025-01-02",
                "pe": 10,
                "circ_mv": 1_200_000,
            },
            kind="market",
        )
        self.assertIsNone(issue)
        self.assertEqual(result["market_cap"], 120.0)

    def test_market_cap_alias_unit_inference_uses_selected_field(self):
        result, issue = normalize_history_record(
            {
                "symbol": "000001.SZ",
                "trade_date": "2025-01-02",
                "pe": 10,
                "market_cap": 120,
                "circ_mv": 1_200_000,
            },
            kind="market",
        )
        self.assertIsNone(issue)
        self.assertEqual(result["market_cap"], 120.0)

    def test_explicit_announcement_date_wins_generic_availability(self):
        result, issue = normalize_history_record(
            {
                "symbol": "000001.SZ",
                "ann_date": "2025-03-01",
                "available_as_of": "2025-04-01",
                "roe": 12,
                "debt_ratio": 40,
            },
            kind="financial",
        )
        self.assertIsNone(issue)
        self.assertEqual(result["ann_date"], "2025-03-01")

    def test_preserves_estimated_announcement_date_quality(self):
        result, issue = normalize_history_record(
            {
                "symbol": "000001.SZ",
                "ann_date": "2025-04-30",
                "ann_date_estimated": True,
                "roe": 12,
                "debt_ratio": 40,
            },
            kind="financial",
        )
        self.assertIsNone(issue)
        self.assertTrue(result["ann_date_estimated"])
        self.assertEqual(result["ann_date_quality"], "estimated")

    def test_build_joins_latest_prior_filing_and_keeps_uncovered_row(self):
        result = build_point_in_time_history(
            [
                {
                    "symbol": "000001.SZ",
                    "period": "20241231",
                    "ann_date": "2025-03-20",
                    "roe": 12,
                    "debt_to_assets": 40,
                    "source": "filings",
                },
                {
                    "symbol": "000001.SZ",
                    "period": "20250331",
                    "ann_date": "2025-05-01",
                    "roe": 8,
                    "debt_to_assets": 70,
                    "source": "filings",
                },
            ],
            [
                {
                    "symbol": "000001.SZ",
                    "trade_date": "2025-03-21",
                    "pe": 15,
                    "circ_mv": 1200000,
                    "circ_mv_unit": "万元",
                    "source": "daily_basic",
                },
                {
                    "symbol": "000001.SZ",
                    "trade_date": "2025-05-02",
                    "pe": 18,
                    "circ_mv": 130,
                    "market_cap_unit": "100m_cny",
                },
            ],
        )
        self.assertEqual(result.issues, ())
        rows = result.history["000001"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["roe"], 12.0)
        self.assertEqual(rows[0]["ann_date"], "2025-03-20")
        self.assertEqual(rows[0]["available_as_of"], "2025-03-21")
        self.assertEqual(rows[0]["market_cap"], 120.0)
        self.assertEqual(rows[1]["roe"], 8.0)

    def test_join_exposes_financial_date_quality(self):
        result = build_point_in_time_history(
            [
                {
                    "symbol": "000001.SZ",
                    "ann_date": "2025-03-20",
                    "ann_date_quality": "estimated",
                    "roe": 12,
                    "debt_ratio": 40,
                }
            ],
            [
                {
                    "symbol": "000001.SZ",
                    "trade_date": "2025-03-21",
                    "pe": 15,
                    "market_cap": 120,
                }
            ],
        )
        row = result.history["000001"][0]
        self.assertEqual(row["financial_ann_date_quality"], "estimated")
        self.assertTrue(row["financial_ann_date_estimated"])
        self.assertEqual(result.report["estimated_financial_dates"], 1)
        self.assertEqual(result.report["financial_snapshots"], 1)
        self.assertEqual(result.report["estimated_financial_snapshots"], 1)

    def test_build_reports_market_days_without_prior_filing(self):
        result = build_point_in_time_history(
            [],
            [{"symbol": "000001.SZ", "trade_date": "2025-01-02", "pe": 10, "market_cap": 100}],
        )
        row = result.history["000001"][0]
        self.assertNotIn("roe", row)
        self.assertEqual(result.report["complete_records"], 0)
        self.assertEqual(result.report["missing_fields"]["roe"], 1)

    def test_build_reports_duplicate_market_day_and_keeps_one_row(self):
        result = build_point_in_time_history(
            [
                {"symbol": "000001.SZ", "ann_date": "2025-01-01", "roe": 12, "debt_ratio": 40}
            ],
            [
                {"symbol": "000001.SZ", "trade_date": "2025-01-02", "pe": 10, "market_cap": 100},
                {"symbol": "000001.SZ", "trade_date": "2025-01-02", "pe": 11, "market_cap": 110},
            ],
        )
        self.assertEqual(len(result.history["000001"]), 1)
        self.assertEqual(result.history["000001"][0]["pe"], 11.0)
        self.assertTrue(any(issue["code"] == "duplicate_market_date" for issue in result.issues))

    def test_historical_evaluator_rejects_future_financial_date(self):
        result = evaluate_fundamental(
            {
                "fundamental_snapshot": {
                    "available_as_of": "2025-03-01",
                    "financial_ann_date": "2025-04-01",
                    "roe": 12,
                    "debt_ratio": 40,
                    "pe": 15,
                    "market_cap": 120,
                }
            },
            _config(),
            as_of="2025-03-15",
            context="historical",
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, "rejected")

    def test_coverage_can_measure_signal_day_availability(self):
        history = {
            "000001": [
                {
                    "available_as_of": "2025-03-01",
                    "roe": 12,
                    "debt_ratio": 40,
                    "pe": 15,
                    "market_cap": 120,
                }
            ]
        }
        report = history_coverage_report(
            history,
            signal_days={"000001.SZ": ["2025-03-02", "2024-12-31"]},
        )
        self.assertEqual(report["signal_days_requested"], 2)
        self.assertEqual(report["signal_days_covered"], 1)
        self.assertEqual(report["signal_days_causal"], 1)
        self.assertEqual(report["signal_days_complete"], 1)

    def test_coverage_excludes_future_financial_date(self):
        report = history_coverage_report(
            {
                "000001": [
                    {
                        "available_as_of": "2025-03-01",
                        "financial_ann_date": "2025-04-01",
                        "roe": 12,
                        "debt_ratio": 40,
                        "pe": 15,
                        "market_cap": 120,
                    }
                ]
            },
            signal_days={"000001": ["2025-03-15"]},
        )
        self.assertEqual(report["signal_days_covered"], 1)
        self.assertEqual(report["signal_days_causal"], 0)
        self.assertEqual(report["signal_days_future_financial"], 1)
        self.assertEqual(report["signal_days_complete"], 0)

    def test_build_rejects_invalid_date_range(self):
        with self.assertRaises(ValueError):
            build_point_in_time_history(
                [], [], start_date="2025-03-02", end_date="2025-03-01"
            )

    def test_load_and_write_jsonl_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.json"
            source.write_text(
                json.dumps({"000001.SZ": [{"trade_date": "2025-01-02", "pe": 10}]}),
                encoding="utf-8",
            )
            records = load_history_records(source)
            self.assertEqual(records[0]["symbol"], "000001.SZ")

            output = root / "history.jsonl"
            write_fundamental_history({"000001": records}, output)
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["symbol"], "000001")

    def test_cli_builds_history_and_separate_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            financial = root / "financial.json"
            market = root / "market.json"
            output = root / "history.jsonl"
            report = root / "coverage.json"
            financial.write_text(
                json.dumps(
                    [
                        {
                            "symbol": "000001.SZ",
                            "ann_date": "2025-03-01",
                            "roe": 12,
                            "debt_ratio": 40,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            market.write_text(
                json.dumps(
                    [
                        {
                            "symbol": "000001.SZ",
                            "trade_date": "2025-03-02",
                            "pe": 15,
                            "circ_mv": 1_200_000,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            exit_code = main(
                [
                    "--financial-data",
                    str(financial),
                    "--market-data",
                    str(market),
                    "--output",
                    str(output),
                    "--report",
                    str(report),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            self.assertTrue(report.exists())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8").splitlines()[0])["market_cap"], 120.0)

            with self.assertRaises(ValueError):
                main(
                    [
                        "--financial-data",
                        str(financial),
                        "--market-data",
                        str(market),
                        "--output",
                        str(output),
                        "--report",
                        str(output),
                    ]
                )


if __name__ == "__main__":
    unittest.main()
