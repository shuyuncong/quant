"""Candidate-layer audit of MACD golden-cross quality on canonical exports.

The pre-declared primary factor is normalized DIF-DEA separation on the signal
day (larger is better).  Histogram slope, confirmation count, and confirmation
wait are diagnostics only and cannot make the primary gate pass.  The script
accepts development train/val/test splits only, never consumes holdout, and
does not implement a portfolio rule until the candidate layer passes.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from attribution_audit import FIELDS, _config_snapshot, stats
from backtest_winrate import HISTORY_DIR, _confirmation_details, prepare_closed_bars
from candidate_integrity import (
    VERSION as INTEGRITY_VERSION,
    candidate_id,
    candidate_ids_sha256,
    file_sha256,
    load_jsonl,
)
from macd_divergence_audit import (
    _cluster_bootstrap_delta,
    _daily_rank_ic,
    _quantile_report,
    _write_jsonl,
)
from macd_near_regime_audit import _replay_split
from strategy.macd import calculate_macd, find_golden_cross_entries
from utils.helpers import load_config


VERSION = "macd_cross_quality_audit.v1"
SPLITS = ("train", "val", "test")
PRIMARY_FACTOR = "gap_strength"
FACTOR_SPECS: dict[str, dict[str, Any]] = {
    "gap_strength": {"larger_is_better": True, "role": "primary"},
    "hist_slope_3": {"larger_is_better": True, "role": "diagnostic"},
    "confirmation_count": {"larger_is_better": True, "role": "diagnostic"},
    "confirmation_wait_bars": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
}
MIN_ASSIGNMENT_COVERAGE = 0.90
MIN_AFFECTED_CANDIDATES = 30
MIN_AFFECTED_SYMBOLS = 10
MIN_AFFECTED_SIGNAL_DAYS = 10
MIN_DIRECTION_SPLITS = 2


def _macd_parameters(config: dict[str, Any]) -> dict[str, int]:
    raw = config.get("signal_strategy", {}).get("macd", {}) or {}
    return {
        "fast": int(raw.get("fast", 12)),
        "slow": int(raw.get("slow", 26)),
        "signal": int(raw.get("signal", 9)),
    }


def _is_macd_candidate(row: dict[str, Any]) -> bool:
    return str(row.get("signal_type", "")).startswith(
        "macd_golden_cross_pullback_confirmed_"
    )


def _signal_zone(row: dict[str, Any]) -> str | None:
    signal_type = str(row.get("signal_type", ""))
    prefix = "macd_golden_cross_pullback_confirmed_"
    return signal_type[len(prefix) :] if signal_type.startswith(prefix) else None


def _history_context(
    symbol: str,
    config: dict[str, Any],
    cache: dict[str, dict[str, Any] | None],
    history_hashes: dict[str, str],
) -> dict[str, Any] | None:
    if symbol in cache:
        return cache[symbol]
    path = HISTORY_DIR / f"{symbol}_qfq.pkl"
    if not path.exists():
        cache[symbol] = None
        return None
    history_hashes[symbol] = file_sha256(path)
    closed = prepare_closed_bars(pd.read_pickle(path))
    if closed.empty:
        cache[symbol] = None
        return None
    params = _macd_parameters(config)
    macd = calculate_macd(closed["close"], **params)
    enriched = closed.copy()
    for column in ("dif", "dea", "hist"):
        enriched[column] = macd[column]
    entries = find_golden_cross_entries(
        closed,
        **params,
        zero_axis_tolerance=float(
            config.get("signal_strategy", {})
            .get("macd", {})
            .get("zero_axis_tolerance", 0.005)
        ),
        confirmation_bars=int(
            config.get("backtest", {})
            .get("chan_zero_axis", {})
            .get(
                "cross_window_bars",
                config.get("signal_strategy", {})
                .get("macd", {})
                .get("pullback_confirmation_bars", 5),
            )
        ),
        allowed_zones=tuple(
            str(item).lower()
            for item in config.get("backtest", {})
            .get("chan_zero_axis", {})
            .get("allowed_zones", ["above", "near"])
        ),
    )
    entry_map: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for entry in entries:
        key = (int(entry["confirmation_index"]), str(entry["zone"]))
        entry_map.setdefault(key, []).append(entry)
    context = {"closed": closed, "enriched": enriched, "entry_map": entry_map}
    cache[symbol] = context
    return context


def _quality_features_for_sources(
    sources: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache: dict[str, dict[str, Any] | None] = {}
    history_hashes: dict[str, str] = {}
    features: dict[str, dict[str, Any]] = {}
    errors: Counter = Counter()
    ambiguous_matches = 0

    for source in sources:
        identifier = candidate_id(source)
        if identifier in features:
            raise RuntimeError(f"duplicate candidate id after integrity gate: {identifier}")
        base: dict[str, Any] = {
            "candidate_id": identifier,
            "factor_assignment_available": False,
            "factor_error": None,
            "gap_strength": None,
            "hist_slope_3": None,
            "confirmation_count": None,
            "confirmation_wait_bars": None,
            "matching_cross_count": 0,
            "selected_cross_day": None,
        }
        if not _is_macd_candidate(source):
            base["factor_error"] = "non_macd_signal"
            errors["non_macd_signal"] += 1
            features[identifier] = base
            continue
        symbol = str(source.get("symbol", "")).zfill(6)
        context = _history_context(symbol, config, cache, history_hashes)
        if context is None:
            base["factor_error"] = "missing_history"
            errors["missing_history"] += 1
            features[identifier] = base
            continue
        enriched: pd.DataFrame = context["enriched"]
        signal_day = date.fromisoformat(str(source["signal_day"]))
        day_values = pd.to_datetime(enriched["datetime"]).dt.date
        matches = enriched.index[day_values == signal_day].tolist()
        if not matches:
            base["factor_error"] = "missing_signal_bar"
            errors["missing_signal_bar"] += 1
            features[identifier] = base
            continue
        signal_index = int(matches[-1])
        close = float(enriched.iloc[signal_index]["close"])
        dif = float(enriched.iloc[signal_index]["dif"])
        dea = float(enriched.iloc[signal_index]["dea"])
        hist = pd.to_numeric(enriched["hist"], errors="coerce")
        if not np.isfinite(close) or close <= 0 or not np.isfinite(dif - dea):
            base["factor_error"] = "invalid_macd_values"
            errors["invalid_macd_values"] += 1
            features[identifier] = base
            continue
        hist_slope = None
        if signal_index >= 2:
            left = hist.iloc[signal_index - 2]
            right = hist.iloc[signal_index]
            if pd.notna(left) and pd.notna(right):
                hist_slope = (float(right) - float(left)) / (2.0 * close)
        confirmation_items, confirmation_count = _confirmation_details(
            enriched,
            signal_index,
            config.get("signal_strategy", {}).get("macd", {}) or {},
        )
        zone = _signal_zone(source)
        matching_entries = list(
            context["entry_map"].get((signal_index, str(zone)), [])
        )
        selected = None
        if matching_entries:
            # Re-crosses can confirm on the same bar.  The latest cross is the
            # active setup; selecting it is deterministic and affects only the
            # diagnostic wait factor, not the primary gap factor.
            selected = max(matching_entries, key=lambda item: int(item["cross_index"]))
        if len(matching_entries) > 1:
            ambiguous_matches += 1
        base.update(
            {
                "factor_assignment_available": True,
                "gap_strength": abs(dif - dea) / close,
                "hist_slope_3": hist_slope,
                "confirmation_count": int(confirmation_count),
                "confirmation_items": confirmation_items,
                "confirmation_wait_bars": (
                    int(selected["confirmation_bars"]) if selected else None
                ),
                "matching_cross_count": len(matching_entries),
                "selected_cross_day": (
                    pd.Timestamp(
                        enriched.iloc[int(selected["cross_index"])]["datetime"]
                    ).date().isoformat()
                    if selected
                    else None
                ),
            }
        )
        features[identifier] = base

    history_manifest = hashlib_sha256_lines(
        f"{symbol}|{history_hashes[symbol]}" for symbol in sorted(history_hashes)
    )
    return features, {
        "history_manifest_sha256": history_manifest,
        "history_symbol_count": len(history_hashes),
        "factor_errors": dict(errors),
        "ambiguous_same_day_cross_candidates": ambiguous_matches,
        "same_day_cross_resolution": "latest_cross_for_wait_diagnostic_only",
        "macd_parameters": _macd_parameters(config),
    }


def hashlib_sha256_lines(lines: Iterable[str]) -> str:
    import hashlib

    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _assign_primary_halves(rows: list[dict[str, Any]]) -> None:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row["primary_high_quality"] = None
        row["variant_included"] = False
        value = row.get(PRIMARY_FACTOR)
        if value is None:
            continue
        by_day.setdefault(str(row.get("signal_day", "")), []).append(row)
    for day_rows in by_day.values():
        values = [float(row[PRIMARY_FACTOR]) for row in day_rows]
        if len(day_rows) < 2 or len(set(values)) < 2:
            continue
        median = float(np.median(values))
        for row in day_rows:
            high = float(row[PRIMARY_FACTOR]) >= median
            row["primary_high_quality"] = high
            row["variant_included"] = high


def _factor_report(
    rows: list[dict[str, Any]],
    factor: str,
    larger_is_better: bool,
) -> dict[str, Any]:
    usable = [row for row in rows if row.get(factor) is not None]
    return {
        "n": len(usable),
        "coverage_of_macd_candidates": None,
        "rank_ic": {
            outcome: _daily_rank_ic(
                usable,
                factor=factor,
                outcome=outcome,
                ascending_good=larger_is_better,
            )
            for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
        },
        "quantiles": {
            outcome: _quantile_report(
                usable,
                factor=factor,
                outcome=outcome,
                smaller_is_better=not larger_is_better,
            )
            for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
        },
    }


def _candidate_report(
    rows: list[dict[str, Any]],
    source_match: dict[str, Any],
    factor_meta: dict[str, Any],
    bootstrap_reps: int,
    seed: int,
) -> dict[str, Any]:
    macd_rows = [row for row in rows if _is_macd_candidate(row)]
    assigned = [row for row in macd_rows if row.get("factor_assignment_available")]
    primary_rows = [
        row for row in assigned if row.get("primary_high_quality") is not None
    ]
    high = [row for row in primary_rows if row.get("primary_high_quality")]
    low = [row for row in primary_rows if row.get("primary_high_quality") is False]
    factor_reports = {
        factor: _factor_report(rows, factor, spec["larger_is_better"])
        for factor, spec in FACTOR_SPECS.items()
    }
    for report in factor_reports.values():
        report["coverage_of_macd_candidates"] = (
            report["n"] / len(macd_rows) if macd_rows else 0.0
        )
    bootstraps = {
        outcome: _cluster_bootstrap_delta(
            primary_rows,
            outcome,
            reps=bootstrap_reps,
            seed=seed + index,
        )
        for index, outcome in enumerate(
            ("future_20d", "future_40d", "trade_pnl_pct")
        )
    }
    affected_ids = {str(row["candidate_id"]) for row in primary_rows}
    affected_symbols = {str(row["symbol"]) for row in primary_rows}
    affected_days = {str(row["signal_day"]) for row in primary_rows}
    assignment_coverage = len(assigned) / len(macd_rows) if macd_rows else 0.0
    sample_sufficient = bool(
        len(affected_ids) >= MIN_AFFECTED_CANDIDATES
        and len(affected_symbols) >= MIN_AFFECTED_SYMBOLS
        and len(affected_days) >= MIN_AFFECTED_SIGNAL_DAYS
        and high
        and low
    )
    direction_positive = all(
        bootstraps[outcome]["mean_delta"] is not None
        and bootstraps[outcome]["mean_delta"] > 0
        for outcome in ("future_40d", "trade_pnl_pct")
    )
    bootstrap_supported = all(
        bootstraps[outcome]["ci95_low"] is not None
        and bootstraps[outcome]["ci95_low"] > 0
        for outcome in ("future_40d", "trade_pnl_pct")
    )
    return {
        "n": len(rows),
        "macd_candidate_n": len(macd_rows),
        "assignment_coverage_of_macd_candidates": assignment_coverage,
        "primary_factor": PRIMARY_FACTOR,
        "primary_group_rule": "same_signal_day_gap_strength_gte_median",
        "primary_high_n": len(high),
        "primary_low_n": len(low),
        "baseline": {field: stats([row.get(field) for row in rows]) for field in FIELDS},
        "primary_high": {
            field: stats([row.get(field) for row in high]) for field in FIELDS
        },
        "primary_low": {
            field: stats([row.get(field) for row in low]) for field in FIELDS
        },
        "factors": factor_reports,
        "cluster_bootstrap_high_minus_low": bootstraps,
        "sample_gate": {
            "affected_unique_candidates": len(affected_ids),
            "affected_unique_symbols": len(affected_symbols),
            "affected_unique_signal_days": len(affected_days),
            "minimum_candidates": MIN_AFFECTED_CANDIDATES,
            "minimum_symbols": MIN_AFFECTED_SYMBOLS,
            "minimum_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
            "sufficient": sample_sufficient,
        },
        "gate": {
            "assignment_coverage_ok": assignment_coverage >= MIN_ASSIGNMENT_COVERAGE,
            "sample_sufficient": sample_sufficient,
            "primary_direction_positive": direction_positive,
            "primary_cluster_bootstrap_ci95_positive": bootstrap_supported,
            "eligible_for_cross_split": bool(
                assignment_coverage >= MIN_ASSIGNMENT_COVERAGE and sample_sufficient
            ),
        },
        "source_match": source_match,
        "factor_meta": factor_meta,
    }


def _validate_integrity_manifest(
    input_dir: Path,
    split: str,
    source_path: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest_path = input_dir / "candidate_integrity_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"candidate integrity manifest not found: {manifest_path}")
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    record = (manifest.get("splits", {}) or {}).get(split, {}) or {}
    checks = {
        "manifest_version_match": manifest.get("version") == INTEGRITY_VERSION,
        "manifest_output_dir_match": Path(
            str(manifest.get("output_dir", ""))
        ).resolve()
        == input_dir,
        "output_file_match": Path(str(record.get("output_file", ""))).resolve()
        == source_path,
        "output_hash_match": record.get("output_sha256") == file_sha256(source_path),
        "output_row_count_match": record.get("output_rows") == len(rows),
        "candidate_ids_hash_match": record.get("candidate_ids_sha256")
        == candidate_ids_sha256(rows),
        "all_candidate_ids_unique": len(rows)
        == len({candidate_id(row) for row in rows}),
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(
            f"candidate integrity validation failed for {split}: {checks}"
        )
    checks["manifest"] = str(manifest_path)
    checks["manifest_sha256"] = file_sha256(manifest_path)
    return checks


def _cross_split_gate(split_reports: dict[str, Any]) -> dict[str, Any]:
    eligible = [
        split
        for split, report in split_reports.items()
        if report["candidate_report"]["gate"]["eligible_for_cross_split"]
    ]
    directions = {
        split: {
            outcome: report["candidate_report"]
            ["cluster_bootstrap_high_minus_low"][outcome]["mean_delta"]
            for outcome in ("future_40d", "trade_pnl_pct")
        }
        for split, report in split_reports.items()
    }
    direction_consistent = bool(
        len(eligible) >= MIN_DIRECTION_SPLITS
        and all(
            directions[split]["future_40d"] is not None
            and directions[split]["future_40d"] > 0
            and directions[split]["trade_pnl_pct"] is not None
            and directions[split]["trade_pnl_pct"] > 0
            for split in eligible
        )
    )
    bootstrap_supported = bool(
        direction_consistent
        and all(
            split_reports[split]["candidate_report"]["gate"]
            ["primary_cluster_bootstrap_ci95_positive"]
            for split in eligible
        )
    )
    return {
        "eligible_splits": eligible,
        "directions": directions,
        "minimum_direction_splits": MIN_DIRECTION_SPLITS,
        "direction_consistent": direction_consistent,
        "cluster_bootstrap_supported": bootstrap_supported,
        "pass": bool(direction_consistent and bootstrap_supported),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    splits = tuple(args.splits)
    invalid = sorted(set(splits) - set(SPLITS))
    if invalid:
        raise RuntimeError(
            f"unregistered split(s) {invalid}; this audit cannot consume holdout"
        )
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config).expanduser().resolve()
    config_hash_before = file_sha256(config_path)
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")
    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "design": {
            "candidate_layer_only": True,
            "primary_factor": PRIMARY_FACTOR,
            "diagnostic_factors_cannot_pass_gate": [
                factor
                for factor, spec in FACTOR_SPECS.items()
                if spec["role"] == "diagnostic"
            ],
            "canonical_candidates_required": True,
            "holdout_consumed": False,
            "portfolio_layer_implemented": False,
            "production_eligible": False,
        },
        "thresholds": {
            "assignment_coverage": MIN_ASSIGNMENT_COVERAGE,
            "affected_candidates": MIN_AFFECTED_CANDIDATES,
            "affected_symbols": MIN_AFFECTED_SYMBOLS,
            "affected_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
            "direction_splits": MIN_DIRECTION_SPLITS,
        },
        "config_file": str(config_path),
        "config_file_sha256_before": config_hash_before,
        "config_snapshot": _config_snapshot(config),
        "script_sha256": file_sha256(Path(__file__).resolve()),
        "seed": args.seed,
        "bootstrap_reps": args.bootstrap_reps,
        "splits": {},
    }
    for split_index, split in enumerate(splits):
        source_path = input_dir / f"candidates_{split}.jsonl"
        sources = load_jsonl(source_path)
        integrity = _validate_integrity_manifest(
            input_dir, split, source_path, sources
        )
        feature_map, factor_meta = _quality_features_for_sources(sources, config)
        replay_rows, replay_skips, source_match = _replay_split(source_path, config)
        enriched: list[dict[str, Any]] = []
        for replay in replay_rows:
            identifier = candidate_id(replay)
            row = dict(replay)
            row.update(feature_map.get(identifier, {}))
            enriched.append(row)
        _assign_primary_halves(enriched)
        enriched_path = output_dir / f"macd_cross_quality_{split}.jsonl"
        _write_jsonl(enriched_path, enriched)
        result["splits"][split] = {
            "source": str(source_path),
            "source_sha256": file_sha256(source_path),
            "candidate_integrity": integrity,
            "enriched": str(enriched_path),
            "enriched_sha256": file_sha256(enriched_path),
            "replay_skips": dict(replay_skips),
            "candidate_report": _candidate_report(
                enriched,
                source_match,
                factor_meta,
                args.bootstrap_reps,
                args.seed + split_index * 100,
            ),
        }
    candidate_gate = _cross_split_gate(result["splits"])
    result["candidate_gate"] = candidate_gate
    baseline_complete = all(
        result["splits"][split]["candidate_report"]["source_match"]
        ["baseline_replay_complete"]
        for split in splits
    )
    result["baseline_replay_complete"] = baseline_complete
    result["config_file_sha256_after"] = file_sha256(config_path)
    result["config_file_unchanged"] = (
        result["config_file_sha256_after"] == config_hash_before
    )
    if not baseline_complete:
        result["verdict"] = "invalid_baseline_replay_incomplete"
    elif not candidate_gate["pass"]:
        result["verdict"] = "candidate_layer_rejected_or_insufficient_sample"
    else:
        result["verdict"] = (
            "candidate_layer_historical_support_requires_portfolio_rule_preregistration"
        )
    result["production_decision"] = "unchanged_P0"
    result_path = output_dir / "macd_cross_quality_audit.json"
    with result_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir", default=r"D:\tmp\candidates_fullpool_canonical"
    )
    parser.add_argument(
        "--output-dir", default=r"D:\tmp\macd_cross_quality_candidate"
    )
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--label", default="macd-cross-quality-candidate-audit")
    parser.add_argument("--splits", nargs="+", default=list(SPLITS))
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--bootstrap-reps", type=int, default=2000)
    args = parser.parse_args()
    result = run(args)
    print(
        json.dumps(
            {
                "version": result["version"],
                "primary_factor": PRIMARY_FACTOR,
                "candidate_gate": result["candidate_gate"],
                "verdict": result["verdict"],
                "config_file_unchanged": result["config_file_unchanged"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
