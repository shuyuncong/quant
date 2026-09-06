"""Candidate-layer audit of normalized 20-session ATR.

The pre-registered robustness hypothesis is that a larger ATR20 divided by the
closed signal-day price identifies stronger breakouts with better subsequent
and realized returns.  The prior frozen P5b probe used a nearby 14-true-range
feature, so this audit records that provenance and is not an exact replication
or an independent confirmation.  Downside semideviation is diagnostic only.
Canonical train/val/test are all required, holdout is rejected, and no
portfolio layer is implemented.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

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
)
from macd_near_regime_audit import _replay_split
from utils.helpers import load_config


VERSION = "atr_volatility_audit.v1"
SPLITS = ("train", "val", "test")
PREREGISTERED_LABEL = "atr20-high-volatility-candidate-audit"
PREREGISTERED_SEED = 20260831
PREREGISTERED_BOOTSTRAP_REPS = 2000
PREREGISTERED_CONFIG_SHA256 = (
    "4e0084eb945aa2533dca25f9e0f68b4e27e9f2cbc52c11b7898c082d466000f1"
)
PRIMARY_FACTOR = "atr20_ratio"
FACTOR_SPECS: dict[str, dict[str, Any]] = {
    PRIMARY_FACTOR: {"larger_is_better": True, "role": "primary"},
    "downside_semideviation_20": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
}
MIN_ASSIGNMENT_COVERAGE = 0.90
MIN_AFFECTED_CANDIDATES = 30
MIN_AFFECTED_SYMBOLS = 10
MIN_AFFECTED_SIGNAL_DAYS = 10
MIN_OUTCOME_CLUSTERS = 10
ATR_TRUE_RANGES = 20
PRIOR_PROBE_TRUE_RANGES = 14


def _empty_features(error: str, lookback_bars: int | None = None) -> dict[str, Any]:
    return {
        **{factor: None for factor in FACTOR_SPECS},
        "factor_assignment_available": False,
        "factor_error": error,
        "atr_true_ranges": ATR_TRUE_RANGES,
        "factor_lookback_bars": lookback_bars,
        "entry_timing": "T+1",
    }


def _atr_volatility_features(
    frame: pd.DataFrame,
    signal_index: int,
) -> dict[str, Any]:
    """Compute ATR20/close and diagnostic downside semideviation causally."""
    required = {"high", "low", "close"}
    if frame.empty or not required.issubset(frame.columns) or signal_index < 0:
        return _empty_features("missing_signal_bar")
    if signal_index < ATR_TRUE_RANGES:
        return _empty_features(
            "insufficient_20_true_range_history",
            lookback_bars=signal_index + 1,
        )

    limited = frame.iloc[: signal_index + 1]
    high = pd.to_numeric(limited["high"], errors="coerce")
    low = pd.to_numeric(limited["low"], errors="coerce")
    close = pd.to_numeric(limited["close"], errors="coerce")
    window_start = signal_index - ATR_TRUE_RANGES + 1
    true_ranges: list[float] = []
    for index in range(window_start, signal_index + 1):
        values = (
            high.iloc[index],
            low.iloc[index],
            close.iloc[index - 1],
        )
        if any(pd.isna(value) or not np.isfinite(float(value)) for value in values):
            return _empty_features(
                "invalid_atr_price_values",
                lookback_bars=signal_index + 1,
            )
        current_high, current_low, previous_close = map(float, values)
        if (
            current_high <= 0
            or current_low <= 0
            or previous_close <= 0
            or current_high < current_low
        ):
            return _empty_features(
                "invalid_atr_price_values",
                lookback_bars=signal_index + 1,
            )
        true_ranges.append(
            max(
                current_high - current_low,
                abs(current_high - previous_close),
                abs(current_low - previous_close),
            )
        )

    current_close = close.iloc[signal_index]
    if pd.isna(current_close) or not np.isfinite(float(current_close)):
        return _empty_features(
            "invalid_signal_close",
            lookback_bars=signal_index + 1,
        )
    current_close_value = float(current_close)
    if current_close_value <= 0:
        return _empty_features(
            "invalid_signal_close",
            lookback_bars=signal_index + 1,
        )

    close_window = close.iloc[signal_index - ATR_TRUE_RANGES : signal_index + 1]
    if (
        len(close_window) != ATR_TRUE_RANGES + 1
        or close_window.isna().any()
        or not np.isfinite(close_window.astype(float)).all()
        or (close_window.astype(float) <= 0).any()
    ):
        return _empty_features(
            "invalid_downside_return_values",
            lookback_bars=signal_index + 1,
        )
    interval_returns = close_window.astype(float).pct_change().iloc[1:]
    downside = np.minimum(interval_returns.to_numpy(dtype=float), 0.0)
    downside_semideviation = float(np.sqrt(np.mean(np.square(downside))))
    atr20 = float(np.mean(true_ranges))
    return {
        "factor_assignment_available": True,
        "factor_error": None,
        "atr20_ratio": atr20 / current_close_value,
        "downside_semideviation_20": downside_semideviation,
        "atr20": atr20,
        "signal_day_close": current_close_value,
        "atr_true_ranges": ATR_TRUE_RANGES,
        "factor_lookback_bars": signal_index + 1,
        "entry_timing": "T+1",
    }


def _validate_history_frame(frame: pd.DataFrame, path: Path) -> pd.DataFrame:
    required = {"datetime", "open", "high", "low", "close", "is_closed"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"missing columns {missing}")
    if str(frame.attrs.get("adjust", "")).lower() != "qfq":
        raise RuntimeError("history adjustment is not qfq")
    datetimes = pd.to_datetime(frame["datetime"], errors="coerce")
    if datetimes.isna().any():
        raise RuntimeError("history contains invalid datetime")
    if not datetimes.is_monotonic_increasing:
        raise RuntimeError("history datetime is not sorted")
    if datetimes.duplicated().any() or datetimes.dt.date.duplicated().any():
        raise RuntimeError("history contains duplicate trading dates")

    prices = frame[["open", "high", "low", "close"]].apply(
        pd.to_numeric,
        errors="coerce",
    )
    finite = np.isfinite(prices.to_numpy(dtype=float)).all(axis=1)
    valid = prices.notna().all(axis=1) & prices.gt(0).all(axis=1) & finite
    valid &= prices["high"] >= prices[["open", "close"]].max(axis=1)
    valid &= prices["low"] <= prices[["open", "close"]].min(axis=1)
    valid &= prices["high"] >= prices["low"]
    if not bool(valid.all()):
        raise RuntimeError("history contains invalid OHLC rows")

    closed = prepare_closed_bars(frame)
    if closed.empty:
        raise RuntimeError("history has no closed bars")
    closed_datetimes = pd.to_datetime(closed["datetime"], errors="coerce")
    if closed_datetimes.isna().any() or closed_datetimes.dt.date.duplicated().any():
        raise RuntimeError("closed history dates are invalid or duplicated")
    closed.attrs.update(frame.attrs)
    return closed


def _history_frame(
    symbol: str,
    cache: dict[str, pd.DataFrame | None],
    history_hashes: dict[str, str],
    history_input_failures: list[dict[str, Any]],
) -> pd.DataFrame | None:
    if symbol in cache:
        return cache[symbol]
    path = HISTORY_DIR / f"{symbol}_qfq.pkl"
    if not path.exists():
        history_input_failures.append(
            {"symbol": symbol, "path": str(path), "error": "missing_history_file"}
        )
        cache[symbol] = None
        return None
    history_hashes[symbol] = file_sha256(path)
    try:
        raw = pd.read_pickle(path)
        if not isinstance(raw, pd.DataFrame):
            raise RuntimeError("history pickle is not a DataFrame")
        frame = _validate_history_frame(raw, path)
    except Exception as exc:
        history_input_failures.append(
            {
                "symbol": symbol,
                "path": str(path),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        cache[symbol] = None
        return None
    cache[symbol] = frame
    return frame


def _factor_features_for_sources(
    sources: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache: dict[str, pd.DataFrame | None] = {}
    history_hashes: dict[str, str] = {}
    history_input_failures: list[dict[str, Any]] = []
    coverage_failures: list[dict[str, Any]] = []
    features: dict[str, dict[str, Any]] = {}
    errors: Counter = Counter()

    for source in sources:
        identifier = candidate_id(source)
        if identifier in features:
            raise RuntimeError(f"duplicate candidate id after integrity gate: {identifier}")
        symbol = str(source.get("symbol", "")).zfill(6)
        frame = _history_frame(
            symbol,
            cache,
            history_hashes,
            history_input_failures,
        )
        base = {"candidate_id": identifier}
        if frame is None:
            base.update(_empty_features("missing_or_invalid_history"))
            errors["missing_or_invalid_history"] += 1
            features[identifier] = base
            continue

        signal_day = date.fromisoformat(str(source["signal_day"]))
        day_values = pd.to_datetime(frame["datetime"]).dt.date
        matches = frame.index[day_values == signal_day].tolist()
        if len(matches) != 1:
            first_history_day = day_values.iloc[0]
            last_history_day = day_values.iloc[-1]
            coverage_error = "missing_signal_bar"
            if signal_day < first_history_day:
                coverage_error = "signal_day_before_history"
            elif signal_day > last_history_day:
                coverage_error = "signal_day_after_history"
            base.update(_empty_features(coverage_error))
            errors[coverage_error] += 1
            coverage_failures.append(
                {
                    "candidate_id": identifier,
                    "symbol": symbol,
                    "signal_day": signal_day.isoformat(),
                    "history_first_day": first_history_day.isoformat(),
                    "history_last_day": last_history_day.isoformat(),
                    "match_count": len(matches),
                    "error": coverage_error,
                }
            )
            features[identifier] = base
            continue

        factor = _atr_volatility_features(frame, int(matches[0]))
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
        "history_input_safe": not history_input_failures,
        "history_input_failures": history_input_failures,
        "replay_history_coverage_safe": not coverage_failures,
        "replay_history_coverage_failures": coverage_failures,
        "factor_errors": dict(errors),
        "diagnostic_unavailable_counts": {
            factor: sum(value.get(factor) is None for value in features.values())
            for factor, spec in FACTOR_SPECS.items()
            if spec["role"] == "diagnostic"
        },
        "primary_definition": (
            "mean_20_true_ranges_through_signal_day/close_signal_day"
        ),
        "true_range_definition": (
            "max(high-low,abs(high-prev_close),abs(low-prev_close))"
        ),
        "downside_semideviation_definition": (
            "sqrt(mean(min(close_to_close_return,0)^2))_over_20_intervals"
        ),
        "signal_day_closed_bars_only": True,
        "entry_timing": "T+1",
    }


def _assert_history_safe(factor_meta: dict[str, Any]) -> None:
    input_failures = list(factor_meta.get("history_input_failures") or [])
    coverage_failures = list(
        factor_meta.get("replay_history_coverage_failures") or []
    )
    if not input_failures and not coverage_failures:
        return
    examples = input_failures[:3] + coverage_failures[:3]
    raise RuntimeError(
        "history safety gate failed before replay: "
        f"input_failures={len(input_failures)}, "
        f"coverage_failures={len(coverage_failures)}, examples={examples}"
    )


def _assign_primary_halves(rows: list[dict[str, Any]]) -> None:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row["primary_high_atr"] = None
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
            row["primary_high_atr"] = high
            row["variant_included"] = high


def _factor_report(
    rows: list[dict[str, Any]],
    factor: str,
    larger_is_better: bool,
) -> dict[str, Any]:
    usable = [row for row in rows if row.get(factor) is not None]
    return {
        "n": len(usable),
        "coverage": len(usable) / len(rows) if rows else 0.0,
        "larger_is_better": larger_is_better,
        "positive_ic_interpretation": (
            "higher_factor_values_associated_with_higher_outcomes"
            if larger_is_better
            else "lower_factor_values_associated_with_higher_outcomes"
        ),
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


def _bootstrap_contract_ok(report: dict[str, Any]) -> bool:
    return bool(
        report.get("mean_delta") is not None
        and report.get("reps_requested") == PREREGISTERED_BOOTSTRAP_REPS
        and report.get("reps_valid") == PREREGISTERED_BOOTSTRAP_REPS
        and int(report.get("cluster_count") or 0) >= MIN_OUTCOME_CLUSTERS
    )


def _candidate_report(
    rows: list[dict[str, Any]],
    source_match: dict[str, Any],
    replay_integrity: dict[str, Any],
    factor_meta: dict[str, Any],
    seed: int,
) -> dict[str, Any]:
    assigned = [row for row in rows if row.get("factor_assignment_available")]
    primary_rows = [
        row for row in assigned if row.get("primary_high_atr") is not None
    ]
    high = [row for row in primary_rows if row.get("primary_high_atr")]
    low = [row for row in primary_rows if row.get("primary_high_atr") is False]
    bootstraps = {
        outcome: _cluster_bootstrap_delta(
            primary_rows,
            outcome,
            reps=PREREGISTERED_BOOTSTRAP_REPS,
            seed=seed + index,
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
    primary_outcomes = ("future_40d", "trade_pnl_pct")
    direction_positive = all(
        bootstraps[outcome]["mean_delta"] is not None
        and bootstraps[outcome]["mean_delta"] > 0
        for outcome in primary_outcomes
    )
    contract_ok = all(
        _bootstrap_contract_ok(bootstraps[outcome]) for outcome in primary_outcomes
    )
    bootstrap_supported = bool(
        contract_ok
        and all(
            bootstraps[outcome]["ci95_low"] is not None
            and bootstraps[outcome]["ci95_low"] > 0
            for outcome in primary_outcomes
        )
    )
    return {
        "n": len(rows),
        "assignment_coverage": coverage,
        "primary_factor": PRIMARY_FACTOR,
        "primary_larger_is_better": True,
        "primary_group_rule": (
            "same_signal_day_atr20_ratio_gte_median_is_high"
        ),
        "primary_comparison": "high_minus_low",
        "primary_high_n": len(high),
        "primary_low_n": len(low),
        "baseline": {
            field: stats([row.get(field) for row in rows]) for field in FIELDS
        },
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
        "bootstrap_contract": {
            "cluster_key": "symbol",
            "resample_whole_clusters_with_replacement": True,
            "percentile_ci": [2.5, 97.5],
            "reps_required": PREREGISTERED_BOOTSTRAP_REPS,
            "minimum_outcome_clusters": MIN_OUTCOME_CLUSTERS,
            "primary_outcomes_contract_ok": contract_ok,
        },
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
            "replay_integrity_ok": bool(replay_integrity.get("all_pass")),
            "primary_direction_positive": direction_positive,
            "primary_bootstrap_contract_ok": contract_ok,
            "primary_cluster_bootstrap_ci95_positive": bootstrap_supported,
            "eligible_for_cross_split": bool(
                coverage >= MIN_ASSIGNMENT_COVERAGE
                and sample_sufficient
                and replay_integrity.get("all_pass")
            ),
        },
        "source_match": source_match,
        "replay_integrity": replay_integrity,
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


def _assert_replay_integrity(
    sources: list[dict[str, Any]],
    feature_map: dict[str, dict[str, Any]],
    replay_rows: list[dict[str, Any]],
    replay_skips: Counter,
    source_match: dict[str, Any],
) -> dict[str, Any]:
    source_ids = [candidate_id(row) for row in sources]
    replay_ids = [candidate_id(row) for row in replay_rows]
    feature_ids = list(feature_map)
    policy_skips = dict(source_match.get("policy_skips") or {})
    checks = {
        "source_ids_unique": len(source_ids) == len(set(source_ids)),
        "replay_ids_unique": len(replay_ids) == len(set(replay_ids)),
        "feature_ids_unique": len(feature_ids) == len(set(feature_ids)),
        "source_replay_ids_order_match": source_ids == replay_ids,
        "source_feature_ids_set_match": set(source_ids) == set(feature_ids),
        "replay_feature_ids_set_match": set(replay_ids) == set(feature_ids),
        "replay_skips_empty": not dict(replay_skips),
        "policy_skips_empty": not policy_skips,
        "source_rows_match": source_match.get("source_rows") == len(sources),
        "eligible_rows_match": source_match.get("eligible_rows") == len(sources),
        "replayed_rows_match": source_match.get("replayed_rows") == len(sources),
        "baseline_replay_complete": source_match.get("baseline_replay_complete")
        is True,
        "entry_signal_policy_validated": source_match.get(
            "entry_signal_policy_validated"
        )
        is True,
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(f"replay integrity validation failed: {checks}")
    return checks


def _cross_split_gate(split_reports: dict[str, Any]) -> dict[str, Any]:
    required_present = (
        len(split_reports) == len(SPLITS)
        and set(split_reports) == set(SPLITS)
    )
    eligible = [
        split
        for split in SPLITS
        if split in split_reports
        and split_reports[split]["candidate_report"]["gate"]
        ["eligible_for_cross_split"]
    ]
    all_required_eligible = required_present and eligible == list(SPLITS)
    directions = {
        split: {
            outcome: split_reports[split]["candidate_report"]
            ["cluster_bootstrap_high_minus_low"][outcome]["mean_delta"]
            for outcome in ("future_40d", "trade_pnl_pct")
        }
        for split in SPLITS
        if split in split_reports
    }
    direction_consistent = bool(
        all_required_eligible
        and all(
            directions[split]["future_40d"] is not None
            and directions[split]["future_40d"] > 0
            and directions[split]["trade_pnl_pct"] is not None
            and directions[split]["trade_pnl_pct"] > 0
            for split in SPLITS
        )
    )
    bootstrap_supported = bool(
        direction_consistent
        and all(
            split_reports[split]["candidate_report"]["gate"]
            ["primary_bootstrap_contract_ok"]
            and split_reports[split]["candidate_report"]["gate"]
            ["primary_cluster_bootstrap_ci95_positive"]
            for split in SPLITS
        )
    )
    return {
        "required_splits": list(SPLITS),
        "eligible_splits": eligible,
        "all_required_splits_present": required_present,
        "all_required_splits_eligible": all_required_eligible,
        "directions": directions,
        "direction_consistent": direction_consistent,
        "cluster_bootstrap_supported": bootstrap_supported,
        "pass": bool(all_required_eligible and direction_consistent and bootstrap_supported),
    }


def _validate_preregistered_args(args: argparse.Namespace) -> None:
    splits = tuple(getattr(args, "splits", ()))
    invalid = sorted(set(splits) - set(SPLITS))
    if invalid:
        raise RuntimeError(
            f"unregistered split(s) {invalid}; this audit cannot consume holdout"
        )
    if splits != SPLITS:
        raise RuntimeError(
            f"all required splits must be supplied in order {SPLITS}; got {splits}"
        )
    if getattr(args, "label", None) != PREREGISTERED_LABEL:
        raise RuntimeError("label differs from the pre-registered label")
    if getattr(args, "seed", None) != PREREGISTERED_SEED:
        raise RuntimeError("seed differs from the pre-registered seed")
    if (
        getattr(args, "bootstrap_reps", None)
        != PREREGISTERED_BOOTSTRAP_REPS
    ):
        raise RuntimeError("bootstrap reps differ from the pre-registered value")


def _output_targets(output_dir: Path) -> list[Path]:
    return [
        *(output_dir / f"atr_volatility_{split}.jsonl" for split in SPLITS),
        output_dir / "atr_volatility_audit.json",
    ]


def _assert_output_targets_unused(output_dir: Path) -> None:
    conflicts = [
        path
        for target in _output_targets(output_dir)
        for path in (target, target.with_name(target.name + ".tmp"))
        if path.exists()
    ]
    if conflicts:
        raise RuntimeError(
            "formal output target already exists; overwrite is forbidden: "
            + ", ".join(str(path) for path in conflicts)
        )


def _write_outputs_atomically(
    output_dir: Path,
    enriched_by_split: dict[str, list[dict[str, Any]]],
    result: dict[str, Any],
) -> None:
    staged: list[tuple[Path, Path]] = []
    for split in SPLITS:
        target = output_dir / f"atr_volatility_{split}.jsonl"
        temporary = target.with_name(target.name + ".tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            for row in enriched_by_split[split]:
                handle.write(
                    json.dumps(
                        row,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    + "\n"
                )
        result["splits"][split]["enriched"] = str(target)
        result["splits"][split]["enriched_sha256"] = file_sha256(temporary)
        staged.append((temporary, target))

    result_target = output_dir / "atr_volatility_audit.json"
    result_temporary = result_target.with_name(result_target.name + ".tmp")
    with result_temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    staged.append((result_temporary, result_target))
    for temporary, target in staged:
        temporary.replace(target)


def run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_preregistered_args(args)
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    _assert_output_targets_unused(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config).expanduser().resolve()
    expected_config_path = (BASE_DIR / "config" / "config.yaml").resolve()
    if config_path != expected_config_path:
        raise RuntimeError(
            f"config path must remain {expected_config_path}; got {config_path}"
        )
    config_hash_before = file_sha256(config_path)
    if config_hash_before != PREREGISTERED_CONFIG_SHA256:
        raise RuntimeError(
            "config hash differs from the pre-registered snapshot: "
            f"expected={PREREGISTERED_CONFIG_SHA256}, actual={config_hash_before}"
        )
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")

    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "design": {
            "candidate_layer_only": True,
            "primary_factor": PRIMARY_FACTOR,
            "primary_larger_is_better": True,
            "same_signal_day_comparison": True,
            "primary_comparison": "high_minus_low",
            "diagnostic_factors_cannot_pass_gate": [
                factor
                for factor, spec in FACTOR_SPECS.items()
                if spec["role"] == "diagnostic"
            ],
            "canonical_candidates_required": True,
            "required_splits": list(SPLITS),
            "signal_day_closed_bars_only": True,
            "entry_timing": "T+1",
            "holdout_consumed": False,
            "portfolio_layer_implemented": False,
            "production_eligible": False,
            "period_scan_performed": False,
            "reverse_direction_tested": False,
        },
        "research_provenance": {
            "prior_probe_window_true_ranges": PRIOR_PROBE_TRUE_RANGES,
            "audit_window_true_ranges": ATR_TRUE_RANGES,
            "direction_source": "frozen_P5b_three_window_probe",
            "audit_type": "nearby_fixed_horizon_robustness",
            "exact_replication": False,
            "independent_confirmation": False,
            "prior_probe_may_overlap_canonical_splits": True,
            "holdout_reserved": True,
        },
        "thresholds": {
            "assignment_coverage": MIN_ASSIGNMENT_COVERAGE,
            "affected_candidates": MIN_AFFECTED_CANDIDATES,
            "affected_symbols": MIN_AFFECTED_SYMBOLS,
            "affected_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
            "minimum_outcome_clusters": MIN_OUTCOME_CLUSTERS,
        },
        "config_file": str(config_path),
        "config_file_expected_sha256": PREREGISTERED_CONFIG_SHA256,
        "config_file_sha256_before": config_hash_before,
        "config_snapshot": _config_snapshot(config),
        "script_sha256": file_sha256(Path(__file__).resolve()),
        "seed": args.seed,
        "bootstrap_reps": args.bootstrap_reps,
        "production_database_connected": False,
        "splits": {},
    }
    enriched_by_split: dict[str, list[dict[str, Any]]] = {}

    for split_index, split in enumerate(SPLITS):
        source_path = input_dir / f"candidates_{split}.jsonl"
        sources = load_jsonl(source_path)
        integrity = _validate_integrity_manifest(
            input_dir,
            split,
            source_path,
            sources,
        )
        feature_map, factor_meta = _factor_features_for_sources(sources)
        _assert_history_safe(factor_meta)
        replay_rows, replay_skips, source_match = _replay_split(source_path, config)
        replay_integrity = _assert_replay_integrity(
            sources,
            feature_map,
            replay_rows,
            replay_skips,
            source_match,
        )
        enriched: list[dict[str, Any]] = []
        for replay in replay_rows:
            identifier = candidate_id(replay)
            row = dict(replay)
            row.update(feature_map[identifier])
            enriched.append(row)
        _assign_primary_halves(enriched)
        enriched_by_split[split] = enriched
        result["splits"][split] = {
            "source": str(source_path),
            "source_sha256": file_sha256(source_path),
            "candidate_integrity": integrity,
            "enriched": None,
            "enriched_sha256": None,
            "replay_skips": dict(replay_skips),
            "candidate_report": _candidate_report(
                enriched,
                source_match,
                replay_integrity,
                factor_meta,
                PREREGISTERED_SEED + split_index * 100,
            ),
        }

    result["candidate_gate"] = _cross_split_gate(result["splits"])
    result["baseline_replay_complete"] = all(
        result["splits"][split]["candidate_report"]["source_match"]
        ["baseline_replay_complete"]
        for split in SPLITS
    )
    config_hash_after = file_sha256(config_path)
    result["config_file_sha256_after"] = config_hash_after
    result["config_file_unchanged"] = config_hash_after == config_hash_before
    if not result["config_file_unchanged"]:
        raise RuntimeError("config file changed during audit")
    if not result["baseline_replay_complete"]:
        raise RuntimeError("baseline replay became incomplete after integrity gate")
    result["verdict"] = (
        "candidate_layer_historical_support_requires_portfolio_rule_preregistration"
        if result["candidate_gate"]["pass"]
        else "candidate_layer_rejected_or_insufficient_sample"
    )
    result["production_decision"] = "unchanged_P0"
    _write_outputs_atomically(output_dir, enriched_by_split, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        default=r"D:\tmp\candidates_fullpool_canonical",
    )
    parser.add_argument(
        "--output-dir",
        default=r"D:\tmp\atr_volatility_candidate",
    )
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--label", default=PREREGISTERED_LABEL)
    parser.add_argument("--splits", nargs="+", default=list(SPLITS))
    parser.add_argument("--seed", type=int, default=PREREGISTERED_SEED)
    parser.add_argument(
        "--bootstrap-reps",
        type=int,
        default=PREREGISTERED_BOOTSTRAP_REPS,
    )
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
