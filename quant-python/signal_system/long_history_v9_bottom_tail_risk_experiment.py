"""Run preregistered v9a bottom-tail risk filtering on development data.

The frozen target is the lowest-return 20% of candidates inside each
entry_day+signal_type bucket.  The experiment tests whether the existing 19
factors can identify candidates to avoid; it does not replay a portfolio,
select hyperparameters, or access Holdout data.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import long_history_v7_top4_ranking_experiment as v7a
import long_history_v8_gap_weighted_top4_ranking_experiment as v8a

VERSION = "long_history_v9_bottom_tail_risk.v1"
EXPERIMENT_STATUS = "post_hoc_target_shift_development"
DATASET_STATUS = v7a.DATASET_STATUS
FACTOR_REPORT_VERSION = v7a.FACTOR_REPORT_VERSION
EXPECTED_ELIGIBLE_FOLDS = v7a.EXPECTED_ELIGIBLE_FOLDS
MIN_BUCKET_CANDIDATES = v7a.MIN_BUCKET_CANDIDATES
FACTOR_NAMES = v7a.FACTOR_NAMES
MODEL_FEATURE_NAMES = v7a.MODEL_FEATURE_NAMES
MODEL_L2 = v7a.MODEL_L2
MODEL_MAX_ITERATIONS = v7a.MODEL_MAX_ITERATIONS
MODEL_TOLERANCE = v7a.MODEL_TOLERANCE
MODEL_SCORE_CLIP = v7a.MODEL_SCORE_CLIP
OUTCOME_FIELDS = v7a.OUTCOME_FIELDS
FACTOR_PREFLIGHT_CODE = v7a.FACTOR_PREFLIGHT_CODE
FACTOR_PREFLIGHT_AUDIT_CODE = v7a.FACTOR_PREFLIGHT_AUDIT_CODE
V5_AUDIT_CODE = v7a.V5_AUDIT_CODE
FOLD_LOADER_CODE = v7a.FOLD_LOADER_CODE
V7A_FEATURE_MODEL_CORE_CODE = Path(v7a.__file__).resolve()
V7A_FEATURE_MODEL_CORE_SHA256 = (
    "4efe0e06bc0b2c4a96c06ec79b6b51fce4c5bae411c7ac001f6f0f277a16ef14"
)
V8A_PARENT_CODE = Path(v8a.__file__).resolve()
V8A_PARENT_CODE_SHA256 = (
    "1d63b971d7b626c27e6785e0ba54dae69a44052576d74a581ea57a11e9c07c74"
)
V8A_PARENT_REPORT_SHA256 = (
    "4a87a1bd83d446913e83a77f44b33dabce8dc1e2367c767c2188436e01cbd22b"
)
EXPECTED_V8A_FAILED_CHECKS = (
    "at_least_four_folds_top4_mean_advantage_above_zero",
    "stitched_top4_hit_rate_above_random_expectation",
    "stitched_top4_mean_advantage_above_zero",
)
AUDIT_CODE = BASE_DIR / "long_history_v9_bottom_tail_risk_audit.py"
RISK_FRACTION = 0.20
TAIL_COUNT_RULE = "max(1, ceil(bucket_size * 0.20))"
ARTIFACT_NAMES = ("candidate_risk_scores", "bucket_metrics")


class BottomTailRiskExperimentError(RuntimeError):
    """Raised when a v9a input or frozen experiment contract is violated."""


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise BottomTailRiskExperimentError(f"Holdout path is blocked: {resolved}")
    return resolved


def _sha256_file(path: Path) -> str:
    return v7a._sha256_file(path)


def _input_record(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    if not path.exists() or not path.is_file():
        raise BottomTailRiskExperimentError(f"input file does not exist: {path}")
    return {"path": str(path), "sha256": _sha256_file(path)}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BottomTailRiskExperimentError(f"cannot read JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BottomTailRiskExperimentError(f"JSON object required: {path}")
    return value


def _assert_frozen_cores() -> None:
    frozen = (
        (
            V7A_FEATURE_MODEL_CORE_CODE,
            V7A_FEATURE_MODEL_CORE_SHA256,
            "v7a feature/model core",
        ),
        (V8A_PARENT_CODE, V8A_PARENT_CODE_SHA256, "v8a parent code"),
    )
    for path, expected, label in frozen:
        actual = _sha256_file(path)
        if actual != expected:
            raise BottomTailRiskExperimentError(
                f"frozen {label} SHA256 drift: expected {expected}, got {actual}"
            )


def _load_parent_v8a_report(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    if _sha256_file(path) != V8A_PARENT_REPORT_SHA256:
        raise BottomTailRiskExperimentError("frozen v8a parent report SHA256 drift")
    report = _load_json(path)
    if report.get("version") != v8a.VERSION:
        raise BottomTailRiskExperimentError("unexpected v8a parent version")
    if report.get("passes_research_screen") is not False:
        raise BottomTailRiskExperimentError("v8a parent did not fail its screen")
    if report.get("holdout_used") is not False:
        raise BottomTailRiskExperimentError("v8a parent used Holdout")
    if report.get("portfolio_replayed") is not False:
        raise BottomTailRiskExperimentError("v8a parent replayed a portfolio")
    if report.get("production_eligible") is not False:
        raise BottomTailRiskExperimentError("v8a parent is production eligible")
    screen = report.get("screen")
    checks = screen.get("checks") if isinstance(screen, dict) else None
    if not isinstance(checks, dict):
        raise BottomTailRiskExperimentError("v8a parent screen checks are missing")
    failed = tuple(sorted(name for name, passed in checks.items() if passed is False))
    if failed != tuple(sorted(EXPECTED_V8A_FAILED_CHECKS)):
        raise BottomTailRiskExperimentError("v8a parent failed checks changed")
    inputs = report.get("input")
    code_record = inputs.get("experiment_code") if isinstance(inputs, dict) else None
    if not isinstance(code_record, dict):
        raise BottomTailRiskExperimentError("v8a parent experiment code is missing")
    if code_record.get("sha256") != V8A_PARENT_CODE_SHA256:
        raise BottomTailRiskExperimentError("v8a parent experiment hash changed")
    return report


def _risk_count(bucket_size: int) -> int:
    if bucket_size < MIN_BUCKET_CANDIDATES:
        raise BottomTailRiskExperimentError(
            f"risk count requires at least {MIN_BUCKET_CANDIDATES} candidates"
        )
    return max(1, math.ceil(bucket_size * RISK_FRACTION))


def _actual_risk_order(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        group,
        key=lambda row: (v7a._outcome(row), str(row["candidate_id"])),
    )


def build_risk_training_data(
    rows: list[dict[str, Any]], feature_map: dict[str, np.ndarray]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Build class-balanced bottom-tail labels with equal total bucket weight."""

    samples: list[np.ndarray] = []
    labels: list[float] = []
    sample_weights: list[float] = []
    eligible_buckets = 0
    candidates_in_eligible_buckets = 0
    risk_labels = 0
    safe_labels = 0
    boundary_tie_buckets = 0
    groups = v7a._bucket_groups(rows)
    for key in sorted(groups):
        group = groups[key]
        if len(group) < MIN_BUCKET_CANDIDATES:
            continue
        eligible_buckets += 1
        candidates_in_eligible_buckets += len(group)
        ordered = _actual_risk_order(group)
        tail_count = _risk_count(len(group))
        if (
            abs(
                v7a._outcome(ordered[tail_count - 1])
                - v7a._outcome(ordered[tail_count])
            )
            <= 1e-12
        ):
            boundary_tie_buckets += 1
        risk_weight = 0.5 / tail_count
        safe_weight = 0.5 / (len(group) - tail_count)
        for index, row in enumerate(ordered):
            is_risk = index < tail_count
            samples.append(feature_map[str(row["candidate_id"])])
            labels.append(1.0 if is_risk else 0.0)
            sample_weights.append(risk_weight if is_risk else safe_weight)
            if is_risk:
                risk_labels += 1
            else:
                safe_labels += 1
    matrix = (
        np.asarray(samples, dtype=float)
        if samples
        else np.empty((0, len(MODEL_FEATURE_NAMES)), dtype=float)
    )
    return (
        matrix,
        np.asarray(labels, dtype=float),
        np.asarray(sample_weights, dtype=float),
        {
            "eligible_buckets": eligible_buckets,
            "candidates_in_eligible_buckets": candidates_in_eligible_buckets,
            "candidate_samples": len(samples),
            "risk_labels": risk_labels,
            "safe_labels": safe_labels,
            "boundary_tie_buckets": boundary_tie_buckets,
            "risk_fraction": RISK_FRACTION,
            "tail_count_rule": TAIL_COUNT_RULE,
            "actual_risk_tie_break": "trade_pnl_pct asc then candidate_id asc",
            "boundary_tie_policy": "deterministic candidate_id tie-break; included",
            "bucket_class_weighting": "risk=0.5, safe=0.5",
            "each_bucket_total_training_weight": 1.0,
            "each_bucket_risk_class_weight": 0.5,
            "each_bucket_safe_class_weight": 0.5,
            "sample_weight_min": (
                round(min(sample_weights), 12) if sample_weights else None
            ),
            "sample_weight_max": (
                round(max(sample_weights), 12) if sample_weights else None
            ),
            "objective_weight_sum": round(math.fsum(sample_weights), 10),
        },
    )


def _risk_probability(logit: float) -> float:
    clipped = max(-35.0, min(35.0, logit))
    return 1.0 / (1.0 + math.exp(-clipped))


def score_risk_rows(
    rows: list[dict[str, Any]],
    feature_map: dict[str, np.ndarray],
    model: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    weights = np.asarray(model["weights"], dtype=float)
    scores = {
        str(row["candidate_id"]): float(
            np.clip(
                feature_map[str(row["candidate_id"])] @ weights,
                -MODEL_SCORE_CLIP,
                MODEL_SCORE_CLIP,
            )
        )
        for row in rows
    }
    output: list[dict[str, Any]] = []
    groups = v7a._bucket_groups(rows)
    for key in sorted(groups):
        group = groups[key]
        ordered = sorted(
            group,
            key=lambda row: (
                -scores[str(row["candidate_id"])],
                str(row["candidate_id"]),
            ),
        )
        ranks = {
            str(row["candidate_id"]): index + 1 for index, row in enumerate(ordered)
        }
        eligible = len(group) >= MIN_BUCKET_CANDIDATES
        reject_count = _risk_count(len(group)) if eligible else 0
        for row in group:
            identifier = str(row["candidate_id"])
            logit = scores[identifier]
            values = feature_map[identifier]
            output.append(
                {
                    "candidate_id": identifier,
                    "symbol": str(row["symbol"]),
                    "signal_day": str(row["signal_day"]),
                    "entry_day": str(row["entry_day"]),
                    "signal_type": str(row["signal_type"]),
                    "bucket_size": len(group),
                    "eligible_bucket": eligible,
                    "rejection_count": reject_count,
                    "model_risk_logit": round(logit, 10),
                    "model_risk_probability": round(_risk_probability(logit), 10),
                    "predicted_risk_rank": ranks[identifier],
                    "predicted_rejected": bool(
                        eligible and ranks[identifier] <= reject_count
                    ),
                    "rank_features": {
                        name: round(float(value), 12)
                        for name, value in zip(MODEL_FEATURE_NAMES, values, strict=True)
                    },
                }
            )
    output.sort(
        key=lambda row: (
            row["entry_day"],
            row["signal_type"],
            row["predicted_risk_rank"],
            row["candidate_id"],
        )
    )
    return output, scores


def _mean(values: list[float]) -> float | None:
    return float(math.fsum(values) / len(values)) if values else None


def _rounded(value: float | None, digits: int = 10) -> float | None:
    return round(value, digits) if value is not None else None


def evaluate_risk_filter(
    rows: list[dict[str, Any]], scores: dict[str, float]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bucket_rows: list[dict[str, Any]] = []
    groups = v7a._bucket_groups(rows)
    for key in sorted(groups):
        group = groups[key]
        if len(group) < MIN_BUCKET_CANDIDATES:
            continue
        actual = _actual_risk_order(group)
        tail_count = _risk_count(len(group))
        actual_risk = {str(row["candidate_id"]) for row in actual[:tail_count]}
        predicted = sorted(
            group,
            key=lambda row: (
                -scores[str(row["candidate_id"])],
                str(row["candidate_id"]),
            ),
        )
        predicted_rejected = {
            str(row["candidate_id"]) for row in predicted[:tail_count]
        }
        rejected_outcomes = [
            v7a._outcome(row)
            for row in group
            if str(row["candidate_id"]) in predicted_rejected
        ]
        kept_outcomes = [
            v7a._outcome(row)
            for row in group
            if str(row["candidate_id"]) not in predicted_rejected
        ]
        true_positives = len(actual_risk & predicted_rejected)
        capture_rate = true_positives / tail_count
        random_rate = tail_count / len(group)
        kept_mean = float(math.fsum(kept_outcomes) / len(kept_outcomes))
        rejected_mean = float(math.fsum(rejected_outcomes) / len(rejected_outcomes))
        kept_median = float(statistics.median(kept_outcomes))
        rejected_median = float(statistics.median(rejected_outcomes))
        boundary_tied = (
            abs(v7a._outcome(actual[tail_count - 1]) - v7a._outcome(actual[tail_count]))
            <= 1e-12
        )
        bucket_rows.append(
            {
                "entry_day": key[0],
                "signal_type": key[1],
                "bucket_size": len(group),
                "actual_risk_count": tail_count,
                "predicted_rejection_count": tail_count,
                "true_positive_risk_candidates": true_positives,
                "risk_capture_rate": round(capture_rate, 10),
                "random_expected_risk_capture_rate": round(random_rate, 10),
                "risk_capture_excess_rate": round(capture_rate - random_rate, 10),
                "kept_mean_pnl_pct": round(kept_mean, 10),
                "rejected_mean_pnl_pct": round(rejected_mean, 10),
                "kept_minus_rejected_mean_pnl_pp": round(kept_mean - rejected_mean, 10),
                "kept_median_pnl_pct": round(kept_median, 10),
                "rejected_median_pnl_pct": round(rejected_median, 10),
                "kept_minus_rejected_median_pnl_pp": round(
                    kept_median - rejected_median, 10
                ),
                "worst_candidate_avoided": (
                    str(actual[0]["candidate_id"]) in predicted_rejected
                ),
                "actual_boundary_tied": boundary_tied,
            }
        )
    captures = [float(row["risk_capture_rate"]) for row in bucket_rows]
    random_rates = [
        float(row["random_expected_risk_capture_rate"]) for row in bucket_rows
    ]
    capture_excesses = [float(row["risk_capture_excess_rate"]) for row in bucket_rows]
    mean_advantages = [
        float(row["kept_minus_rejected_mean_pnl_pp"]) for row in bucket_rows
    ]
    median_advantages = [
        float(row["kept_minus_rejected_median_pnl_pp"]) for row in bucket_rows
    ]
    worst_avoided = [
        1.0 if row["worst_candidate_avoided"] else 0.0 for row in bucket_rows
    ]
    metrics = {
        "eligible_buckets": len(bucket_rows),
        "candidates_in_eligible_buckets": sum(
            int(row["bucket_size"]) for row in bucket_rows
        ),
        "actual_risk_candidates": sum(
            int(row["actual_risk_count"]) for row in bucket_rows
        ),
        "predicted_rejected_candidates": sum(
            int(row["predicted_rejection_count"]) for row in bucket_rows
        ),
        "bucket_weighted_risk_capture_rate": _rounded(_mean(captures)),
        "bucket_weighted_random_expected_risk_capture_rate": _rounded(
            _mean(random_rates)
        ),
        "bucket_weighted_risk_capture_excess_rate": _rounded(_mean(capture_excesses)),
        "risk_capture_above_random_bucket_pct": (
            round(
                100.0
                * sum(value > 0.0 for value in capture_excesses)
                / len(capture_excesses),
                10,
            )
            if capture_excesses
            else None
        ),
        "kept_minus_rejected_mean_pnl_pp": _rounded(_mean(mean_advantages)),
        "kept_minus_rejected_median_pnl_pp": (
            round(float(statistics.median(mean_advantages)), 10)
            if mean_advantages
            else None
        ),
        "bucket_median_kept_minus_rejected_median_pnl_pp": (
            round(float(statistics.median(median_advantages)), 10)
            if median_advantages
            else None
        ),
        "positive_kept_advantage_bucket_pct": (
            round(
                100.0
                * sum(value > 0.0 for value in mean_advantages)
                / len(mean_advantages),
                10,
            )
            if mean_advantages
            else None
        ),
        "worst_candidate_avoidance_rate": _rounded(_mean(worst_avoided)),
        "random_expected_worst_candidate_avoidance_rate": _rounded(_mean(random_rates)),
        "worst_candidate_avoidance_excess_rate": _rounded(
            (_mean(worst_avoided) - _mean(random_rates))
            if worst_avoided and random_rates
            else None
        ),
        "actual_boundary_tie_buckets": sum(
            bool(row["actual_boundary_tied"]) for row in bucket_rows
        ),
    }
    return bucket_rows, metrics


def _fit_scope(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, float]]:
    feature_map, feature_audit = v7a.build_cross_sectional_feature_map(rows)
    matrix, labels, weights, target_audit = build_risk_training_data(rows, feature_map)
    model = v7a.fit_pairwise_logistic(matrix, labels, weights)
    _, scores = score_risk_rows(rows, feature_map, model)
    _, metrics = evaluate_risk_filter(rows, scores)
    return (
        model,
        {
            "feature_audit": feature_audit,
            "risk_target": target_audit,
            "in_sample_metrics": metrics,
            "model": v7a._model_public(model),
        },
        scores,
    )


def _research_screen(
    folds: list[dict[str, Any]], stitched: dict[str, Any]
) -> dict[str, Any]:
    capture_excesses = [
        fold["evaluation"]["metrics"]["bucket_weighted_risk_capture_excess_rate"]
        for fold in folds
    ]
    economic_advantages = [
        fold["evaluation"]["metrics"]["kept_minus_rejected_mean_pnl_pp"]
        for fold in folds
    ]
    capture_passes = sum(
        value is not None and value > 0.0 for value in capture_excesses
    )
    economic_passes = sum(
        value is not None and value > 0.0 for value in economic_advantages
    )
    joint_passes = sum(
        capture is not None
        and capture > 0.0
        and advantage is not None
        and advantage > 0.0
        for capture, advantage in zip(
            capture_excesses, economic_advantages, strict=True
        )
    )
    valid_capture = [float(value) for value in capture_excesses if value is not None]
    valid_economic = [
        float(value) for value in economic_advantages if value is not None
    ]
    stitched_metrics = stitched["metrics"]
    checks = {
        "eligible_folds_exactly_fold_03_through_fold_08": [
            fold["fold_name"] for fold in folds
        ]
        == list(EXPECTED_ELIGIBLE_FOLDS),
        "all_six_models_fitted_and_converged": all(
            fold["training"]["model"]["model_fitted"]
            and fold["training"]["model"]["converged"]
            for fold in folds
        ),
        "every_fold_training_risk_buckets_at_least_5": all(
            fold["training"]["risk_target"]["eligible_buckets"] >= 5 for fold in folds
        ),
        "at_least_four_folds_risk_capture_above_random": capture_passes >= 4,
        "at_least_four_folds_kept_mean_above_rejected": economic_passes >= 4,
        "at_least_four_folds_joint_risk_and_economic_advantage": joint_passes >= 4,
        "median_fold_risk_capture_excess_above_zero": (
            len(valid_capture) == len(folds) and statistics.median(valid_capture) > 0.0
        ),
        "median_fold_kept_mean_advantage_above_zero": (
            len(valid_economic) == len(folds)
            and statistics.median(valid_economic) > 0.0
        ),
        "stitched_eligible_buckets_at_least_20": stitched_metrics["eligible_buckets"]
        >= 20,
        "stitched_actual_risk_candidates_at_least_20": stitched_metrics[
            "actual_risk_candidates"
        ]
        >= 20,
        "stitched_risk_capture_above_random": (
            stitched_metrics["bucket_weighted_risk_capture_excess_rate"] is not None
            and stitched_metrics["bucket_weighted_risk_capture_excess_rate"] > 0.0
        ),
        "stitched_kept_mean_above_rejected": (
            stitched_metrics["kept_minus_rejected_mean_pnl_pp"] is not None
            and stitched_metrics["kept_minus_rejected_mean_pnl_pp"] > 0.0
        ),
        "stitched_positive_kept_advantage_bucket_rate_above_half": (
            stitched_metrics["positive_kept_advantage_bucket_pct"] is not None
            and stitched_metrics["positive_kept_advantage_bucket_pct"] > 50.0
        ),
        "stitched_worst_candidate_avoidance_above_random": (
            stitched_metrics["worst_candidate_avoidance_excess_rate"] is not None
            and stitched_metrics["worst_candidate_avoidance_excess_rate"] > 0.0
        ),
    }
    return {
        "checks": checks,
        "folds_risk_capture_above_random": capture_passes,
        "folds_kept_mean_above_rejected": economic_passes,
        "folds_joint_risk_and_economic_advantage": joint_passes,
        "median_fold_risk_capture_excess_rate": (
            round(float(statistics.median(valid_capture)), 10)
            if valid_capture
            else None
        ),
        "median_fold_kept_mean_advantage_pp": (
            round(float(statistics.median(valid_economic)), 10)
            if valid_economic
            else None
        ),
        "passes_research_screen": all(checks.values()),
        "production_eligible": False,
    }


def build_snapshot(
    factor_report_path: Path, fold_report_path: Path, v8a_report_path: Path
) -> dict[str, Any]:
    _assert_frozen_cores()
    factor_report_path = _guard_development_path(factor_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    v8a_report_path = _guard_development_path(v8a_report_path)
    parent_v8a = _load_parent_v8a_report(v8a_report_path)
    factor_audit = v7a.audit_factor_report(factor_report_path)
    if factor_audit.get("checks_passed") is not True:
        raise BottomTailRiskExperimentError(
            "factor preflight single-report audit failed"
        )
    factor_report = factor_audit["_report_value"]
    if factor_report.get("version") != FACTOR_REPORT_VERSION:
        raise BottomTailRiskExperimentError("factor report version is not frozen v2")
    recorded_fold = v7a._resolve_report_input(
        factor_report, "fold_report", factor_report_path.parent
    )
    if recorded_fold != fold_report_path:
        raise BottomTailRiskExperimentError(
            "explicit fold report differs from factor input"
        )
    source_report_path = v7a._resolve_report_input(
        factor_report, "source_report", factor_report_path.parent
    )
    source_audit = v7a.audit_source_report(source_report_path)
    fold_audit = v7a.audit_fold_report(fold_report_path, source_audit)
    loaded_folds, all_fold_rows, integrity = v7a._load_eligible_fold_rows(
        fold_report_path, fold_audit["_report_value"]
    )
    factor_rows, factor_artifact = v7a._load_factor_rows(factor_audit)
    factor_by_id = {str(row["candidate_id"]): row for row in factor_rows}
    if set(factor_by_id) != {str(row["candidate_id"]) for row in all_fold_rows}:
        raise BottomTailRiskExperimentError(
            "factor snapshot does not exactly cover audited fold candidates"
        )
    v7a._join_factor_rows(all_fold_rows, factor_by_id, "dataset")

    fold_results: list[dict[str, Any]] = []
    artifact_rows: dict[str, list[dict[str, Any]]] = {}
    stitched_rows: list[dict[str, Any]] = []
    stitched_scores: dict[str, float] = {}
    stitched_score_rows: list[dict[str, Any]] = []
    training_rows_by_fold: dict[str, list[dict[str, Any]]] = {}
    for fold in loaded_folds:
        fold_name = str(fold["fold_name"])
        train_rows = v7a._join_factor_rows(
            fold["rows"]["train"], factor_by_id, f"{fold_name}.train"
        )
        evaluation_rows = v7a._join_factor_rows(
            fold["rows"]["evaluation"],
            factor_by_id,
            f"{fold_name}.evaluation",
        )
        model, training, _ = _fit_scope(train_rows)
        training_rows_by_fold[fold_name] = train_rows
        evaluation_feature_map, feature_audit = v7a.build_cross_sectional_feature_map(
            evaluation_rows
        )
        score_output, scores = score_risk_rows(
            evaluation_rows, evaluation_feature_map, model
        )
        for record in score_output:
            record["fold_name"] = fold_name
        bucket_output, metrics = evaluate_risk_filter(evaluation_rows, scores)
        for record in bucket_output:
            record["fold_name"] = fold_name
        artifact_rows[f"{fold_name}/candidate_risk_scores"] = score_output
        artifact_rows[f"{fold_name}/bucket_metrics"] = bucket_output
        fold_results.append(
            {
                "fold_name": fold_name,
                "evaluation_window": fold["evaluation_window"],
                "input_artifacts": fold["input_artifacts"],
                "training": training,
                "evaluation": {
                    "feature_audit": feature_audit,
                    "metrics": metrics,
                },
            }
        )
        stitched_rows.extend(evaluation_rows)
        stitched_scores.update(scores)
        stitched_score_rows.extend(score_output)

    stitched_score_rows.sort(
        key=lambda row: (
            row["entry_day"],
            row["signal_type"],
            row["predicted_risk_rank"],
            row["candidate_id"],
        )
    )
    stitched_bucket_rows, stitched_metrics = evaluate_risk_filter(
        stitched_rows, stitched_scores
    )
    artifact_rows["stitched_oos/candidate_risk_scores"] = stitched_score_rows
    artifact_rows["stitched_oos/bucket_metrics"] = stitched_bucket_rows
    stitched = {
        "fold_names": list(EXPECTED_ELIGIBLE_FOLDS),
        "score_origin": "each candidate uses only its own fold train model",
        "metrics": stitched_metrics,
    }
    screen = _research_screen(fold_results, stitched)
    return {
        "factor_audit": factor_audit,
        "factor_report": factor_report,
        "source_audit": source_audit,
        "fold_audit": fold_audit,
        "factor_artifact": factor_artifact,
        "parent_v8a": parent_v8a,
        "integrity": {
            **integrity,
            "factor_candidates": len(factor_rows),
            "factor_fold_identity_matches": True,
            "feature_schema_intersection_with_outcomes": sorted(
                set(FACTOR_NAMES) & OUTCOME_FIELDS
            ),
            "purged_training_labels_used": False,
            "evaluation_labels_used_for_fitting": False,
        },
        "fold_results": fold_results,
        "stitched": stitched,
        "screen": screen,
        "artifact_rows": artifact_rows,
        "source_report_path": source_report_path,
        "_training_rows_by_fold": training_rows_by_fold,
    }


def assemble_report(
    snapshot: dict[str, Any],
    factor_report_path: Path,
    fold_report_path: Path,
    v8a_report_path: Path,
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fold_results: list[dict[str, Any]] = []
    for fold in snapshot["fold_results"]:
        fold_name = fold["fold_name"]
        fold_results.append(
            {
                **fold,
                "artifacts": {
                    name: artifacts[f"{fold_name}/{name}"] for name in ARTIFACT_NAMES
                },
            }
        )
    stitched = {
        **snapshot["stitched"],
        "artifacts": {
            name: artifacts[f"stitched_oos/{name}"] for name in ARTIFACT_NAMES
        },
    }
    return {
        "version": VERSION,
        "experiment_status": EXPERIMENT_STATUS,
        "dataset_status": DATASET_STATUS,
        "research_question": (
            "Can the frozen 19 factors identify the realized bottom 20% inside "
            "entry_day+signal_type buckets, so the retained candidates outperform "
            "the rejected risk set across development folds?"
        ),
        "preregistered_design": {
            "target_shift_from_v8a": (
                "predict bottom-tail risk instead of ranking realized Top-4 winners"
            ),
            "eligible_folds": list(EXPECTED_ELIGIBLE_FOLDS),
            "bucket_key": ["entry_day", "signal_type"],
            "minimum_bucket_candidates": MIN_BUCKET_CANDIDATES,
            "risk_fraction": RISK_FRACTION,
            "tail_count_rule": TAIL_COUNT_RULE,
            "actual_risk_tie_break": "trade_pnl_pct asc then candidate_id asc",
            "boundary_tie_policy": "deterministic candidate_id tie-break; included",
            "raw_factor_names": list(FACTOR_NAMES),
            "model_feature_names": list(MODEL_FEATURE_NAMES),
            "feature_transform": "within-bucket centered midrank; missing=0",
            "model": "no-intercept binary logistic bottom-tail risk classifier",
            "risk_label": 1,
            "safe_label": 0,
            "bucket_class_weighting": "risk=0.5, safe=0.5",
            "each_bucket_total_training_weight": 1.0,
            "l2": MODEL_L2,
            "max_iterations": MODEL_MAX_ITERATIONS,
            "tolerance": MODEL_TOLERANCE,
            "model_score_clip": [-MODEL_SCORE_CLIP, MODEL_SCORE_CLIP],
            "predicted_risk_tie_break": "candidate_id asc",
            "hyperparameter_search": False,
            "factor_selection": False,
            "portfolio_replay": False,
            "holdout_used": False,
        },
        "outcome_isolation": {
            "training_outcome": "trade_pnl_pct from same-fold train.jsonl only",
            "training_outcome_use": (
                "within-bucket bottom-tail membership only; magnitude is not a weight"
            ),
            "purged_training_labels_used": False,
            "evaluation_labels_used_for_fitting": False,
            "evaluation_labels_used_only_for_oos_metrics": True,
            "factor_snapshot_contains_outcomes": False,
        },
        "input": {
            "factor_report": _input_record(factor_report_path),
            "fold_report": _input_record(fold_report_path),
            "v8a_parent_report": _input_record(v8a_report_path),
            "source_report": _input_record(snapshot["source_report_path"]),
            "factor_candidates": snapshot["factor_artifact"],
            "experiment_code": _input_record(Path(__file__).resolve()),
            "audit_code": _input_record(AUDIT_CODE),
            "v7a_feature_model_core_code": _input_record(V7A_FEATURE_MODEL_CORE_CODE),
            "v8a_parent_code": _input_record(V8A_PARENT_CODE),
            "factor_preflight_code": _input_record(FACTOR_PREFLIGHT_CODE),
            "factor_preflight_audit_code": _input_record(FACTOR_PREFLIGHT_AUDIT_CODE),
            "v5_audit_code": _input_record(V5_AUDIT_CODE),
            "fold_loader_code": _input_record(FOLD_LOADER_CODE),
            "factor_preflight_audit": {
                "version": snapshot["factor_report"]["version"],
                "checks_passed": snapshot["factor_audit"]["checks_passed"],
                "candidate_count": snapshot["factor_audit"]["candidate_count"],
                "factor_count": snapshot["factor_audit"]["factor_count"],
            },
            "v5_audit": {
                "source_checks_passed": snapshot["source_audit"]["checks_passed"],
                "fold_checks_passed": snapshot["fold_audit"]["checks_passed"],
                "dataset_candidates": snapshot["fold_audit"]["dataset_candidates"],
                "dataset_candidate_ids_sha256": snapshot["fold_audit"][
                    "dataset_candidate_ids_sha256"
                ],
            },
        },
        "frozen_parent": {
            "v8a_version": v8a.VERSION,
            "v8a_report_sha256": V8A_PARENT_REPORT_SHA256,
            "v8a_code_sha256": V8A_PARENT_CODE_SHA256,
            "v8a_passes_research_screen": False,
            "v8a_failed_checks": list(EXPECTED_V8A_FAILED_CHECKS),
            "linear_top4_winner_ranking_route_terminated": True,
            "v9a_is_post_hoc_target_shift": True,
        },
        "integrity": snapshot["integrity"],
        "folds": fold_results,
        "stitched_oos": stitched,
        "screen": snapshot["screen"],
        "passes_research_screen": snapshot["screen"]["passes_research_screen"],
        "model_fitted": all(
            fold["training"]["model"]["model_fitted"] for fold in fold_results
        ),
        "hyperparameters_selected": False,
        "factor_selection_performed": False,
        "holdout_used": False,
        "portfolio_replayed": False,
        "production_eligible": False,
    }


def build_report(
    factor_report_path: Path,
    fold_report_path: Path,
    v8a_report_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    factor_report_path = _guard_development_path(factor_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    v8a_report_path = _guard_development_path(v8a_report_path)
    output_dir = _guard_development_path(output_dir)
    if output_dir.exists():
        raise BottomTailRiskExperimentError(
            f"output directory already exists; refusing overwrite: {output_dir}"
        )
    snapshot = build_snapshot(factor_report_path, fold_report_path, v8a_report_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = {
        name: v7a._write_jsonl(output_dir / f"{name}.jsonl", rows)
        for name, rows in sorted(snapshot["artifact_rows"].items())
    }
    report = assemble_report(
        snapshot,
        factor_report_path,
        fold_report_path,
        v8a_report_path,
        artifacts,
    )
    v7a._write_json(output_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-report", type=Path, required=True)
    parser.add_argument("--fold-report", type=Path, required=True)
    parser.add_argument("--v8a-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(
            args.factor_report,
            args.fold_report,
            args.v8a_report,
            args.output_dir,
        )
    except Exception as exc:  # noqa: BLE001 - CLI fails closed as one JSON object.
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "passes_research_screen": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    report_path = _guard_development_path(args.output_dir) / "report.json"
    print(
        json.dumps(
            {
                "version": VERSION,
                "report": str(report_path),
                "sha256": _sha256_file(report_path),
                "passes_research_screen": report["passes_research_screen"],
                "model_fitted": report["model_fitted"],
                "portfolio_replayed": False,
                "production_eligible": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
