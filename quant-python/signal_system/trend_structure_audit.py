"""Candidate-layer audit of signal-day trend structure.

The pre-declared primary hypothesis is that the 20-session percentage slope
of MA250 is positively associated with later returns.  Shorter moving-average
slopes, price-to-MA distances, and bullish alignment are diagnostics only.
The audit requires canonical development candidates, uses signal-day and prior
closed bars only, rejects holdout, and has no portfolio implementation.
"""
from __future__ import annotations

import argparse
import hashlib
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
from backtest_winrate import HISTORY_DIR, prepare_closed_bars
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
from utils.helpers import load_config


VERSION = "trend_structure_audit.v1"
SPLITS = ("train", "val", "test")
PRIMARY_FACTOR = "ma250_slope_20"
FACTOR_SPECS: dict[str, dict[str, Any]] = {
    "ma250_slope_20": {"larger_is_better": True, "role": "primary"},
    "ma60_slope_20": {"larger_is_better": True, "role": "diagnostic"},
    "ma20_slope_5": {"larger_is_better": True, "role": "diagnostic"},
    "close_ma20_distance": {"larger_is_better": True, "role": "diagnostic"},
    "close_ma60_distance": {"larger_is_better": True, "role": "diagnostic"},
    "close_ma250_distance": {"larger_is_better": True, "role": "diagnostic"},
    "bullish_alignment": {"larger_is_better": True, "role": "diagnostic"},
}
MIN_ASSIGNMENT_COVERAGE = 0.90
MIN_AFFECTED_CANDIDATES = 30
MIN_AFFECTED_SYMBOLS = 10
MIN_AFFECTED_SIGNAL_DAYS = 10
MIN_DIRECTION_SPLITS = 2


def _safe_ratio_change(current: Any, previous: Any) -> float | None:
    if pd.isna(current) or pd.isna(previous) or float(previous) == 0:
        return None
    return float(current) / float(previous) - 1.0


def _safe_distance(value: Any, average: Any) -> float | None:
    if pd.isna(value) or pd.isna(average) or float(average) == 0:
        return None
    return float(value) / float(average) - 1.0


def _trend_features(frame: pd.DataFrame, signal_index: int) -> dict[str, Any]:
    """Compute causal moving-average features at one closed signal bar."""
    result = {factor: None for factor in FACTOR_SPECS}
    result.update(
        {
            "factor_assignment_available": False,
            "factor_error": None,
            "trend_lookback_bars": signal_index + 1,
        }
    )
    if signal_index < 0 or frame.empty or "close" not in frame.columns:
        result["factor_error"] = "missing_signal_bar"
        return result
    limited = frame.iloc[: signal_index + 1]
    close = pd.to_numeric(limited["close"], errors="coerce")
    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()
    ma250 = close.rolling(250, min_periods=250).mean()
    if signal_index < 269 or pd.isna(ma250.iloc[signal_index - 20]):
        result["factor_error"] = "insufficient_ma250_slope_history"
        return result
    current_close = close.iloc[signal_index]
    current_ma20 = ma20.iloc[signal_index]
    current_ma60 = ma60.iloc[signal_index]
    current_ma250 = ma250.iloc[signal_index]
    primary = _safe_ratio_change(current_ma250, ma250.iloc[signal_index - 20])
    if primary is None:
        result["factor_error"] = "invalid_ma250_slope"
        return result
    values = (current_close, current_ma20, current_ma60, current_ma250)
    bullish_alignment = (
        int(
            float(current_close)
            > float(current_ma20)
            > float(current_ma60)
            > float(current_ma250)
        )
        if all(pd.notna(value) for value in values)
        else None
    )
    result.update(
        {
            "factor_assignment_available": True,
            "ma250_slope_20": primary,
            "ma60_slope_20": _safe_ratio_change(
                current_ma60, ma60.iloc[signal_index - 20]
            ),
            "ma20_slope_5": _safe_ratio_change(
                current_ma20, ma20.iloc[signal_index - 5]
            ),
            "close_ma20_distance": _safe_distance(current_close, current_ma20),
            "close_ma60_distance": _safe_distance(current_close, current_ma60),
            "close_ma250_distance": _safe_distance(current_close, current_ma250),
            "bullish_alignment": bullish_alignment,
        }
    )
    return result


def _history_frame(
    symbol: str,
    cache: dict[str, pd.DataFrame | None],
    history_hashes: dict[str, str],
) -> pd.DataFrame | None:
    if symbol in cache:
        return cache[symbol]
    path = HISTORY_DIR / f"{symbol}_qfq.pkl"
    if not path.exists():
        cache[symbol] = None
        return None
    history_hashes[symbol] = file_sha256(path)
    frame = prepare_closed_bars(pd.read_pickle(path))
    cache[symbol] = frame if not frame.empty else None
    return cache[symbol]


def _factor_features_for_sources(
    sources: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache: dict[str, pd.DataFrame | None] = {}
    history_hashes: dict[str, str] = {}
    features: dict[str, dict[str, Any]] = {}
    errors: Counter = Counter()
    for source in sources:
        identifier = candidate_id(source)
        if identifier in features:
            raise RuntimeError(f"duplicate candidate id after integrity gate: {identifier}")
        symbol = str(source.get("symbol", "")).zfill(6)
        frame = _history_frame(symbol, cache, history_hashes)
        base = {"candidate_id": identifier}
        if frame is None:
            base.update(
                {
                    **{factor: None for factor in FACTOR_SPECS},
                    "factor_assignment_available": False,
                    "factor_error": "missing_history",
                    "trend_lookback_bars": None,
                }
            )
            errors["missing_history"] += 1
            features[identifier] = base
            continue
        signal_day = date.fromisoformat(str(source["signal_day"]))
        day_values = pd.to_datetime(frame["datetime"]).dt.date
        matches = frame.index[day_values == signal_day].tolist()
        if not matches:
            base.update(
                {
                    **{factor: None for factor in FACTOR_SPECS},
                    "factor_assignment_available": False,
                    "factor_error": "missing_signal_bar",
                    "trend_lookback_bars": None,
                }
            )
            errors["missing_signal_bar"] += 1
            features[identifier] = base
            continue
        factor = _trend_features(frame, int(matches[-1]))
        if factor.get("factor_error"):
            errors[str(factor["factor_error"])] += 1
        base.update(factor)
        features[identifier] = base
    history_manifest = hashlib.sha256(
        "\n".join(
            f"{symbol}|{history_hashes[symbol]}" for symbol in sorted(history_hashes)
        ).encode("utf-8")
    ).hexdigest()
    return features, {
        "history_manifest_sha256": history_manifest,
        "history_symbol_count": len(history_hashes),
        "factor_errors": dict(errors),
        "primary_definition": "MA250(signal_day)/MA250(signal_day-20 sessions)-1",
        "signal_day_closed_bars_only": True,
    }


def _assign_primary_halves(rows: list[dict[str, Any]]) -> None:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row["primary_high_quality"] = None
        row["variant_included"] = False
        if row.get(PRIMARY_FACTOR) is not None:
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
    rows: list[dict[str, Any]], factor: str, larger_is_better: bool
) -> dict[str, Any]:
    usable = [row for row in rows if row.get(factor) is not None]
    return {
        "n": len(usable),
        "coverage": len(usable) / len(rows) if rows else 0.0,
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
    assigned = [row for row in rows if row.get("factor_assignment_available")]
    primary_rows = [
        row for row in assigned if row.get("primary_high_quality") is not None
    ]
    high = [row for row in primary_rows if row.get("primary_high_quality")]
    low = [row for row in primary_rows if row.get("primary_high_quality") is False]
    bootstraps = {
        outcome: _cluster_bootstrap_delta(
            primary_rows, outcome, reps=bootstrap_reps, seed=seed + index
        )
        for index, outcome in enumerate(
            ("future_20d", "future_40d", "trade_pnl_pct")
        )
    }
    affected_ids = {str(row["candidate_id"]) for row in primary_rows}
    affected_symbols = {str(row["symbol"]) for row in primary_rows}
    affected_days = {str(row["signal_day"]) for row in primary_rows}
    coverage = len(assigned) / len(rows) if rows else 0.0
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
        "assignment_coverage": coverage,
        "primary_factor": PRIMARY_FACTOR,
        "primary_group_rule": "same_signal_day_ma250_slope_20_gte_median",
        "primary_high_n": len(high),
        "primary_low_n": len(low),
        "baseline": {field: stats([row.get(field) for row in rows]) for field in FIELDS},
        "primary_high": {
            field: stats([row.get(field) for row in high]) for field in FIELDS
        },
        "primary_low": {
            field: stats([row.get(field) for row in low]) for field in FIELDS
        },
        "factors": {
            factor: _factor_report(rows, factor, spec["larger_is_better"])
            for factor, spec in FACTOR_SPECS.items()
        },
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
            "assignment_coverage_ok": coverage >= MIN_ASSIGNMENT_COVERAGE,
            "sample_sufficient": sample_sufficient,
            "primary_direction_positive": direction_positive,
            "primary_cluster_bootstrap_ci95_positive": bootstrap_supported,
            "eligible_for_cross_split": bool(
                coverage >= MIN_ASSIGNMENT_COVERAGE and sample_sufficient
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
            "signal_day_closed_bars_only": True,
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
        feature_map, factor_meta = _factor_features_for_sources(sources)
        replay_rows, replay_skips, source_match = _replay_split(source_path, config)
        enriched: list[dict[str, Any]] = []
        for replay in replay_rows:
            identifier = candidate_id(replay)
            row = dict(replay)
            row.update(feature_map.get(identifier, {}))
            enriched.append(row)
        _assign_primary_halves(enriched)
        enriched_path = output_dir / f"trend_structure_{split}.jsonl"
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
    result["candidate_gate"] = _cross_split_gate(result["splits"])
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
    elif not result["candidate_gate"]["pass"]:
        result["verdict"] = "candidate_layer_rejected_or_insufficient_sample"
    else:
        result["verdict"] = (
            "candidate_layer_historical_support_requires_portfolio_rule_preregistration"
        )
    result["production_decision"] = "unchanged_P0"
    result_path = output_dir / "trend_structure_audit.json"
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
        "--output-dir", default=r"D:\tmp\trend_structure_candidate"
    )
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--label", default="trend-structure-candidate-audit")
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
