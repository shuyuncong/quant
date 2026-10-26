"""Semantic guards for the 30-day turnover overlay study.

These tests pin the causal and grouping rules rather than replay plumbing:

* a later bar's turnover can never change an earlier signal's assessment;
* zero / NaN / missing column / short history / unclosed bar are *unavailable*,
  never "pass" and never "zero";
* the 1 % and 5 % bounds are inclusive, while 0.999 % / 5.001 % fail;
* each symbol's window is built from its own bars (no cross-symbol date shift);
* the three arms only differ by entry candidates, with identical engine settings;
* malformed samples (attrs, duplicate dates, illegal signal indices, calendar holes)
  are rejected instead of silently repaired;
* sample hashes are SHA256 of the real file bytes.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import liquidity_strategy_study as study
from strategy.registry import STRATEGIES


def frame(
    *,
    days: int = 60,
    start: str = "2024-01-02",
    turnover: float | list[float] = 2.0,
    attrs: bool = True,
) -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=days)
    values = [turnover] * days if isinstance(turnover, (int, float)) else list(turnover)
    if len(values) != days:
        raise ValueError("turnover values must match days")
    result = pd.DataFrame(
        {
            "datetime": dates,
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 10_000.0,
            "turnover_rate": values,
        }
    )
    result.attrs = {"adjust": "qfq", "timeframe": "1d"} if attrs else {}
    return result


def empty_signals() -> dict[str, set[int]]:
    return {strategy: set() for strategy in STRATEGIES}


def signals_at(**by_strategy: set[int]) -> dict[str, set[int]]:
    result = empty_signals()
    result.update(by_strategy)
    return result


def metadata(days: list[str]) -> dict:
    return {"start": days[0], "end": days[-1], "calendar": list(days)}


class TurnoverAvailabilityTests(unittest.TestCase):
    def test_future_turnover_never_changes_an_earlier_signal(self):
        original = frame(turnover=2.0)
        before = study.evaluate_turnover(original, 35)
        self.assertTrue(before["available"])
        self.assertAlmostEqual(before["mean_turnover_pct"], 2.0)

        future_changed = original.copy()
        future_changed.loc[36:, "turnover_rate"] = 999.0
        after = study.evaluate_turnover(future_changed, 35)
        self.assertEqual(after, before)
        self.assertAlmostEqual(after["mean_turnover_pct"], 2.0)

    def test_missing_column_is_unavailable_not_zero(self):
        result = study.evaluate_turnover(frame().drop(columns=["turnover_rate"]), 35)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], study.REASON_MISSING_COLUMN)
        self.assertIsNone(result["mean_turnover_pct"])
        self.assertIsNone(result["passes"])

    def test_zero_in_window_is_unavailable(self):
        bars = frame()
        bars.loc[20, "turnover_rate"] = 0.0
        result = study.evaluate_turnover(bars, 35)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], study.REASON_NONPOSITIVE)
        self.assertIsNone(result["passes"])

    def test_zero_before_the_window_does_not_matter(self):
        bars = frame()
        bars.loc[0, "turnover_rate"] = 0.0
        result = study.evaluate_turnover(bars, 35)
        self.assertTrue(result["available"], "index 0 lies outside bars 6..35")
        self.assertTrue(result["passes"])

    def test_nan_in_window_is_unavailable(self):
        bars = frame()
        bars.loc[30, "turnover_rate"] = float("nan")
        result = study.evaluate_turnover(bars, 35)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], study.REASON_NAN)

    def test_short_history_needs_all_thirty_bars(self):
        bars = frame(days=40)
        self.assertEqual(study.evaluate_turnover(bars, 28)["reason"], study.REASON_SHORT_HISTORY)
        self.assertFalse(study.evaluate_turnover(bars, 28)["available"])
        self.assertTrue(study.evaluate_turnover(bars, 29)["available"])

    def test_unclosed_bar_in_window_is_unavailable(self):
        bars = frame()
        bars["is_closed"] = True
        bars.loc[20, "is_closed"] = False
        result = study.evaluate_turnover(bars, 35)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], study.REASON_UNCLOSED)

    def test_bounds_are_inclusive(self):
        one = study.evaluate_turnover(frame(turnover=1.0), 35)
        five = study.evaluate_turnover(frame(turnover=5.0), 35)
        below = study.evaluate_turnover(frame(turnover=0.999), 35)
        above = study.evaluate_turnover(frame(turnover=5.001), 35)
        self.assertTrue(one["available"] and one["passes"])
        self.assertTrue(five["available"] and five["passes"])
        self.assertTrue(below["available"])
        self.assertFalse(below["passes"])
        self.assertTrue(above["available"])
        self.assertFalse(above["passes"])


class SignalGroupingTests(unittest.TestCase):
    def test_unavailable_signals_are_excluded_from_comparison_arms(self):
        passing = frame(turnover=2.0)
        high = frame(turnover=8.0)
        unknown = frame(turnover=2.0)
        unknown.loc[35, "turnover_rate"] = 0.0
        histories = {"PASS": passing, "HIGH": high, "UNKNOWN": unknown}
        signals = {
            symbol: signals_at(macd_zero_axis={35}) for symbol in histories
        }
        # A second strategy's indices must survive untouched in every arm.
        signals["PASS"]["macd_divergence"] = {3}

        classifications = study.classify_sample(histories, signals, "2024-01-01", "2024-12-31")
        summary = study.summarize_classifications(classifications, "macd_zero_axis", signals)
        self.assertEqual(summary["candidates"]["unique_candidates"], 3)
        self.assertEqual(summary["available_count"], 2)
        self.assertEqual(summary["turnover_pool"]["unique_candidates"], 1)
        self.assertEqual(summary["by_reason"], {study.REASON_NONPOSITIVE: 1})

        arms = study.arm_signal_maps(signals, classifications, "macd_zero_axis")
        self.assertEqual(arms["all_signals"]["PASS"]["macd_zero_axis"], {35})
        self.assertEqual(arms["all_signals"]["UNKNOWN"]["macd_zero_axis"], {35})
        self.assertEqual(arms["available_baseline"]["UNKNOWN"]["macd_zero_axis"], set())
        self.assertEqual(arms["turnover_1_to_5"]["HIGH"]["macd_zero_axis"], set())
        self.assertEqual(arms["turnover_1_to_5"]["PASS"]["macd_zero_axis"], {35})
        self.assertEqual(arms["turnover_1_to_5"]["PASS"]["macd_divergence"], {3})

    def test_arms_do_not_mutate_the_source_signals(self):
        histories = {"A": frame(turnover=2.0)}
        signals = {"A": signals_at(macd_zero_axis={35}, yearline_pullback={4})}
        before = deepcopy(signals)
        classifications = study.classify_sample(histories, signals, "2024-01-01", "2024-12-31")
        study.arm_signal_maps(signals, classifications, "macd_zero_axis")
        self.assertEqual(signals, before)

    def test_each_symbol_uses_its_own_dates_and_turnover(self):
        first = frame(start="2024-01-02", turnover=2.0)
        second = frame(start="2024-03-01", turnover=8.0)
        histories = {"FIRST": first, "SECOND": second}
        signals = {symbol: signals_at(macd_zero_axis={30}) for symbol in histories}
        classifications = study.classify_sample(histories, signals, "2024-01-01", "2024-12-31")

        first_record = classifications["FIRST"]["macd_zero_axis"][0]
        second_record = classifications["SECOND"]["macd_zero_axis"][0]
        self.assertNotEqual(first_record["date"], second_record["date"])
        self.assertEqual(first_record["date"], first["datetime"].iloc[30].strftime("%Y-%m-%d"))
        self.assertEqual(second_record["date"], second["datetime"].iloc[30].strftime("%Y-%m-%d"))
        self.assertAlmostEqual(first_record["mean_turnover_pct"], 2.0)
        self.assertAlmostEqual(second_record["mean_turnover_pct"], 8.0)
        self.assertTrue(first_record["passes"])
        self.assertFalse(second_record["passes"])

    def test_future_bars_are_ignored_after_truncation(self):
        bars = frame(days=60)
        end = bars["datetime"].iloc[20].strftime("%Y-%m-%d")
        truncated = bars.loc[bars["datetime"] <= pd.Timestamp(end)].reset_index(drop=True)
        truncated.attrs = dict(bars.attrs)
        histories = {"A": truncated}
        signals = {"A": signals_at(macd_zero_axis={10, 40})}
        classifications = study.classify_sample(histories, signals, "2024-01-01", end)
        records = classifications["A"]["macd_zero_axis"]
        self.assertEqual([record["index"] for record in records], [10])
        summary = study.summarize_classifications(classifications, "macd_zero_axis", signals)
        self.assertEqual(summary["signals_total"], 2)
        self.assertEqual(summary["signals_in_window"], 1)
        self.assertEqual(summary["signals_outside_window"], 1)



class SufficiencyTests(unittest.TestCase):
    def base(self) -> dict:
        return {
            "available_count": 90,
            "candidate_count": 100,
            "candidate_symbols": 10,
            "candidate_days": 10,
            "turnover_count": 30,
            "turnover_symbols": 10,
            "turnover_days": 10,
        }

    def test_exact_thresholds_are_sufficient(self):
        result = study.sufficiency(**self.base())
        self.assertEqual(result["status"], "sufficient")
        self.assertEqual(result["flags"], [])

    def test_every_threshold_triggers_insufficient_sample(self):
        cases = {
            "coverage_below_90pct": {"available_count": 89},
            "unique_candidates_below_30": {"candidate_count": 29},
            "unique_symbols_below_10": {"candidate_symbols": 9},
            "unique_signal_days_below_10": {"candidate_days": 9},
            "turnover_pool_unique_candidates_below_30": {"turnover_count": 29},
            "turnover_pool_unique_symbols_below_10": {"turnover_symbols": 9},
            "turnover_pool_unique_signal_days_below_10": {"turnover_days": 9},
        }
        for flag, override in cases.items():
            with self.subTest(flag=flag):
                values = {**self.base(), **override}
                result = study.sufficiency(**values)
                self.assertEqual(result["status"], "insufficient_sample")
                self.assertIn(flag, result["flags"])

    def test_zero_candidates_is_insufficient_not_a_zero_return(self):
        result = study.sufficiency(
            available_count=0,
            candidate_count=0,
            candidate_symbols=0,
            candidate_days=0,
            turnover_count=0,
            turnover_symbols=0,
            turnover_days=0,
        )
        self.assertEqual(result["status"], "insufficient_sample")
        self.assertIn("coverage_below_90pct", result["flags"])
        self.assertIn("turnover_pool_unique_candidates_below_30", result["flags"])


class SampleValidationTests(unittest.TestCase):
    def build(self, bars: pd.DataFrame | None = None) -> tuple[dict, dict, dict, dict]:
        bars = frame() if bars is None else bars
        histories = {"A": bars}
        signals = {"A": signals_at(macd_zero_axis={35})}
        days = bars["datetime"].dt.strftime("%Y-%m-%d").tolist()
        meta = metadata(days)
        return histories, signals, meta, {"A": len(bars)}

    def test_valid_sample_passes(self):
        histories, signals, meta, lengths = self.build()
        result = study.validate_sample(histories, signals, meta, meta["start"], meta["end"], lengths)
        self.assertEqual(result["symbols"], 1)
        self.assertEqual(result["signal_index_check"], "ok")

    def test_missing_or_wrong_attrs_is_rejected(self):
        for attrs in ({}, {"adjust": "none", "timeframe": "1d"}, {"adjust": "qfq", "timeframe": "1h"}):
            with self.subTest(attrs=attrs):
                bars = frame()
                bars.attrs = dict(attrs)
                histories, signals, meta, lengths = self.build(bars)
                with self.assertRaises(study.SampleValidationError):
                    study.validate_sample(histories, signals, meta, meta["start"], meta["end"], lengths)

    def test_duplicate_dates_are_rejected(self):
        bars = frame()
        duplicated = pd.concat([bars, bars.iloc[[5]]], ignore_index=True)
        duplicated.attrs = dict(bars.attrs)
        histories, signals, meta, _ = self.build(duplicated)
        with self.assertRaises(study.SampleValidationError):
            study.validate_sample(histories, signals, meta, meta["start"], meta["end"], {"A": len(duplicated)})

    def test_non_monotonic_dates_are_rejected(self):
        bars = frame()
        shuffled = pd.concat([bars.iloc[:5], bars.iloc[[6, 5]], bars.iloc[7:]], ignore_index=True)
        shuffled.attrs = dict(bars.attrs)
        histories, signals, meta, _ = self.build(shuffled)
        with self.assertRaises(study.SampleValidationError):
            study.validate_sample(histories, signals, meta, meta["start"], meta["end"], {"A": len(shuffled)})

    def test_out_of_range_signal_indices_are_rejected(self):
        histories, _signals, meta, lengths = self.build()
        for index in (len(histories["A"]), -1):
            with self.subTest(index=index):
                broken = {"A": signals_at(macd_zero_axis={index})}
                with self.assertRaises(study.SampleValidationError):
                    study.validate_sample(histories, broken, meta, meta["start"], meta["end"], lengths)

    def test_boolean_signal_index_is_rejected(self):
        histories, _signals, meta, lengths = self.build()
        broken = {"A": signals_at(macd_zero_axis={True})}
        with self.assertRaises(study.SampleValidationError):
            study.validate_sample(histories, broken, meta, meta["start"], meta["end"], lengths)

    def test_missing_strategy_or_symbol_pairing_is_rejected(self):
        histories, _signals, meta, lengths = self.build()
        missing = {"A": signals_at(macd_zero_axis={35})}
        missing["A"].pop("macd_divergence")
        with self.assertRaises(study.SampleValidationError):
            study.validate_sample(histories, missing, meta, meta["start"], meta["end"], lengths)

        extra = {"A": signals_at(macd_zero_axis={35}), "B": signals_at()}
        with self.assertRaises(study.SampleValidationError):
            study.validate_sample(histories, extra, meta, meta["start"], meta["end"], lengths)

    def test_calendar_holes_are_rejected(self):
        histories, _signals, meta, _lengths = self.build()
        meta = dict(meta)
        meta["calendar"] = meta["calendar"][:-1]
        with self.assertRaises(study.SampleValidationError):
            study.resolve_calendar(meta, histories, meta["start"], meta["end"])

    def test_window_must_repeat_metadata(self):
        meta = {"start": "2024-01-02", "end": "2024-06-28"}
        self.assertEqual(
            study.resolve_window(meta, None, None)[:2], ("2024-01-02", "2024-06-28")
        )
        self.assertEqual(
            study.resolve_window(meta, "2024-01-02", "2024-06-28")[:2],
            ("2024-01-02", "2024-06-28"),
        )
        with self.assertRaises(ValueError):
            study.resolve_window(meta, "2024-01-03", "2024-06-28")


class SampleHashTests(unittest.TestCase):
    def test_hashes_are_sha256_of_real_file_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            inputs.mkdir()
            bars = frame()
            bars.to_pickle(inputs / "A.pkl")
            signals = {"signals": {"macd_zero_axis": [35]}}
            (root / "A.signals.json").write_text(json.dumps(signals), encoding="utf-8")
            days = bars["datetime"].dt.strftime("%Y-%m-%d").tolist()
            (root / "prepared_metadata.json").write_text(
                json.dumps(metadata(days)), encoding="utf-8"
            )

            hashes = study.hash_sample_files(root)
            meta_path = root / "prepared_metadata.json"
            pkl_path = inputs / "A.pkl"
            signal_path = root / "A.signals.json"
            self.assertEqual(
                hashes["prepared_metadata_sha256"], hashlib.sha256(meta_path.read_bytes()).hexdigest()
            )
            expected_input_manifest = hashlib.sha256(
                f"A.pkl\t{hashlib.sha256(pkl_path.read_bytes()).hexdigest()}\n".encode()
            ).hexdigest()
            self.assertEqual(hashes["inputs"]["manifest_sha256"], expected_input_manifest)
            expected_signal_manifest = hashlib.sha256(
                f"A.signals.json\t{hashlib.sha256(signal_path.read_bytes()).hexdigest()}\n".encode()
            ).hexdigest()
            self.assertEqual(hashes["signals"]["manifest_sha256"], expected_signal_manifest)


if __name__ == "__main__":
    unittest.main()
