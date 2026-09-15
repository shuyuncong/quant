"""Run preregistered v10b market-regime participation gating.

Each sample is one signal_day+entry_day+signal_type bucket.  Three audited
market-regime factors available on signal_day predict whether the bucket's
equal-weight mean candidate outcome is negative.  Feature transforms use only
the corresponding training fold.  The experiment does not rank stocks, replay
a portfolio, tune parameters, or access Holdout data.
"""

from __future__ import annotations

import argparse
import hashlib
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
import long_history_v10_cross_sectional_bottom_tail_risk_experiment as v10a
import long_history_v10_new_factor_preflight as v10preflight
import long_history_v10_new_factor_preflight_audit as v10audit

VERSION = "long_history_v10_market_regime_bucket_gate.v1"
EXPERIMENT_STATUS = "post_hoc_market_regime_target_shift_development"
DATASET_STATUS = v7a.DATASET_STATUS
FACTOR_REPORT_VERSION = v10preflight.VERSION
EXPECTED_ELIGIBLE_FOLDS = v10a.EXPECTED_ELIGIBLE_FOLDS
FACTOR_NAMES = v10a.RESERVED_MARKET_REGIME_FACTORS
MODEL_FEATURE_NAMES = tuple(f"train_ecdf_{name}" for name in FACTOR_NAMES)
OUTCOME_FIELDS = v10a.OUTCOME_FIELDS
MODEL_L2 = 1.0
MODEL_MAX_ITERATIONS = 100
MODEL_TOLERANCE = 1e-10
MODEL_SCORE_CLIP = 100.0
GATE_PROBABILITY_THRESHOLD = 0.5
BUCKET_KEY = ("signal_day", "entry_day", "signal_type")
BAD_BUCKET_RULE = "equal_weight_mean_trade_pnl_pct < 0"
ARTIFACT_NAMES = ("bucket_gate_scores", "bucket_metrics")

V10_FACTOR_REPORT_SHA256 = v10a.V10_FACTOR_REPORT_SHA256
V10A_PARENT_REPORT_SHA256 = (
    "cd0312588c57c0ad4c53e369d2bc608b9bc67f845d1b8e2cc185c3ace3a1d835"
)
EXPECTED_V10A_FAILED_CHECKS = (
    "at_least_four_folds_joint_risk_and_economic_advantage",
    "at_least_four_folds_kept_mean_above_rejected",
    "at_least_four_folds_risk_capture_above_random",
    "median_fold_risk_capture_excess_above_zero",
    "stitched_kept_mean_above_rejected",
)
V10_PREFLIGHT_CODE = Path(v10preflight.__file__).resolve()
V10_PREFLIGHT_CODE_SHA256 = v10a.V10_PREFLIGHT_CODE_SHA256
V10_PREFLIGHT_AUDIT_CODE = Path(v10audit.__file__).resolve()
V10_PREFLIGHT_AUDIT_CODE_SHA256 = v10a.V10_PREFLIGHT_AUDIT_CODE_SHA256
V10A_PARENT_CODE = Path(v10a.__file__).resolve()
V10A_PARENT_CODE_SHA256 = (
    "988953d51172220301349f56e21065683b26f48389fd02b2d4be881f5e17962a"
)
V10A_PARENT_AUDIT_CODE = BASE_DIR / (
    "long_history_v10_cross_sectional_bottom_tail_risk_audit.py"
)
V10A_PARENT_AUDIT_CODE_SHA256 = (
    "6a42ad97d898b6fce49ef56e695974d51ccc1abc2ec2fe9f2e710d5ca7a15205"
)
V7A_FOLD_LOADER_CODE = Path(v7a.__file__).resolve()
V7A_FOLD_LOADER_CODE_SHA256 = v10a.V7A_FOLD_MODEL_CORE_SHA256
AUDIT_CODE = BASE_DIR / "long_history_v10_market_regime_bucket_gate_audit.py"


class MarketRegimeBucketGateError(RuntimeError):
    """Raised when a v10b input or preregistered contract is violated."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MarketRegimeBucketGateError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require(
        not any("holdout" in part.lower() for part in resolved.parts),
        f"Holdout path is blocked: {resolved}",
    )
    return resolved


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _input_record(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"input file does not exist: {path}")
    return {"path": str(path), "sha256": _sha256_file(path)}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketRegimeBucketGateError(f"cannot read JSON: {path}: {exc}") from exc
    _require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _assert_frozen_cores() -> None:
    frozen = (
        (V10_PREFLIGHT_CODE, V10_PREFLIGHT_CODE_SHA256, "v10 preflight code"),
        (
            V10_PREFLIGHT_AUDIT_CODE,
            V10_PREFLIGHT_AUDIT_CODE_SHA256,
            "v10 preflight audit code",
        ),
        (V10A_PARENT_CODE, V10A_PARENT_CODE_SHA256, "v10a parent code"),
        (
            V10A_PARENT_AUDIT_CODE,
            V10A_PARENT_AUDIT_CODE_SHA256,
            "v10a parent audit code",
        ),
        (V7A_FOLD_LOADER_CODE, V7A_FOLD_LOADER_CODE_SHA256, "v7a fold loader"),
    )
    for path, expected, label in frozen:
        _require(path.exists() and path.is_file(), f"missing frozen {label}: {path}")
        actual = _sha256_file(path)
        _require(
            actual == expected,
            f"frozen {label} SHA256 drift: expected {expected}, got {actual}",
        )


def _load_parent_v10a_report(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"missing v10a parent report: {path}")
    _require(
        _sha256_file(path) == V10A_PARENT_REPORT_SHA256,
        "frozen v10a parent report SHA256 drift",
    )
    report = _load_json(path)
    _require(report.get("version") == v10a.VERSION, "unexpected v10a parent version")
    _require(
        report.get("passes_research_screen") is False,
        "v10a parent did not fail its screen",
    )
    _require(report.get("holdout_used") is False, "v10a parent used Holdout")
    _require(
        report.get("portfolio_replayed") is False,
        "v10a parent replayed a portfolio",
    )
    _require(
        report.get("production_eligible") is False,
        "v10a parent is production eligible",
    )
    screen = report.get("screen")
    checks = screen.get("checks") if isinstance(screen, dict) else None
    _require(isinstance(checks, dict), "v10a parent screen checks missing")
    failed = tuple(sorted(name for name, passed in checks.items() if passed is False))
    _require(
        failed == tuple(sorted(EXPECTED_V10A_FAILED_CHECKS)),
        f"v10a parent failed checks changed: {failed}",
    )
    inputs = report.get("input")
    code_record = inputs.get("experiment_code") if isinstance(inputs, dict) else None
    _require(isinstance(code_record, dict), "v10a parent code record missing")
    _require(
        code_record.get("sha256") == V10A_PARENT_CODE_SHA256,
        "v10a parent experiment hash changed",
    )
    return report


def _outcome(row: dict[str, Any]) -> float:
    return v7a._outcome(row)


def _bucket_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return tuple(str(row[name]) for name in BUCKET_KEY)


def build_bucket_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = tuple(str(row[name]) for name in BUCKET_KEY)
        groups.setdefault(key, []).append(row)
    output: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        factor_values: dict[str, float] = {}
        for name in FACTOR_NAMES:
            values = {v7a._finite(row["_v10_features"].get(name)) for row in group}
            _require(
                None not in values and len(values) == 1,
                f"market factor differs inside signal bucket: {key}.{name}",
            )
            factor_values[name] = float(next(iter(values)))
        outcomes = [_outcome(row) for row in group]
        mean_outcome = float(math.fsum(outcomes) / len(outcomes))
        output.append(
            {
                "signal_day": key[0],
                "entry_day": key[1],
                "signal_type": key[2],
                "bucket_id": "|".join(key),
                "candidate_count": len(group),
                "features": factor_values,
                "equal_weight_mean_trade_pnl_pct": mean_outcome,
                "actual_bad_bucket": mean_outcome < 0.0,
            }
        )
    output.sort(key=_bucket_sort_key)
    return output


def _reference_digest(values: list[float]) -> str:
    payload = "\n".join(format(value, ".17g") for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_training_references(
    buckets: list[dict[str, Any]],
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    references: dict[str, list[float]] = {}
    audit: dict[str, Any] = {}
    for name in FACTOR_NAMES:
        values = sorted(float(row["features"][name]) for row in buckets)
        _require(
            values and all(math.isfinite(value) for value in values), f"bad {name}"
        )
        references[name] = values
        audit[name] = {
            "count": len(values),
            "minimum": round(values[0], 12),
            "maximum": round(values[-1], 12),
            "values_sha256": _reference_digest(values),
        }
    return references, {
        "method": "training-fold empirical CDF centered at zero",
        "training_only": True,
        "training_bucket_count": len(buckets),
        "factor_references": audit,
        "missing_value": 0.0,
        "range": [-0.5, 0.5],
    }


def _training_midrank(value: float, values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    below = sum(other < value for other in values)
    equal = sum(other == value for other in values)
    return (below + 0.5 * (equal - 1)) / (len(values) - 1) - 0.5


def _oos_ecdf(value: float, values: list[float]) -> float:
    below = sum(other < value for other in values)
    equal = sum(other == value for other in values)
    return (below + 0.5 * equal) / len(values) - 0.5


def transform_buckets(
    buckets: list[dict[str, Any]],
    references: dict[str, list[float]],
    *,
    training: bool,
) -> dict[str, np.ndarray]:
    result: dict[str, np.ndarray] = {}
    for row in buckets:
        values: list[float] = []
        for name in FACTOR_NAMES:
            raw = v7a._finite(row["features"].get(name))
            if raw is None:
                values.append(0.0)
            elif training:
                values.append(_training_midrank(raw, references[name]))
            else:
                values.append(_oos_ecdf(raw, references[name]))
        result[str(row["bucket_id"])] = np.asarray(values, dtype=float)
    return result


def build_training_data(
    buckets: list[dict[str, Any]], feature_map: dict[str, np.ndarray]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    bad = [row for row in buckets if row["actual_bad_bucket"]]
    safe = [row for row in buckets if not row["actual_bad_bucket"]]
    _require(bad and safe, "training buckets require both outcome classes")
    bad_weight = 0.5 / len(bad)
    safe_weight = 0.5 / len(safe)
    ordered = sorted(buckets, key=_bucket_sort_key)
    matrix = np.asarray(
        [feature_map[str(row["bucket_id"])] for row in ordered], dtype=float
    )
    labels = np.asarray(
        [1.0 if row["actual_bad_bucket"] else 0.0 for row in ordered], dtype=float
    )
    weights = np.asarray(
        [bad_weight if row["actual_bad_bucket"] else safe_weight for row in ordered],
        dtype=float,
    )
    return (
        matrix,
        labels,
        weights,
        {
            "bucket_count": len(ordered),
            "bad_buckets": len(bad),
            "non_bad_buckets": len(safe),
            "bad_bucket_rule": BAD_BUCKET_RULE,
            "bucket_outcome": "equal-weight mean candidate trade_pnl_pct",
            "outcome_magnitude_used_as_training_weight": False,
            "class_weighting": "bad=0.5, non_bad=0.5; equal weight within each class",
            "bad_class_weight": 0.5,
            "non_bad_class_weight": 0.5,
            "sample_weight_min": round(float(weights.min()), 12),
            "sample_weight_max": round(float(weights.max()), 12),
            "objective_weight_sum": round(float(weights.sum()), 12),
        },
    )


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def fit_logistic(
    matrix: np.ndarray, labels: np.ndarray, sample_weights: np.ndarray
) -> dict[str, Any]:
    _require(
        matrix.ndim == 2 and matrix.shape[1] == len(MODEL_FEATURE_NAMES),
        "gate training matrix has wrong shape",
    )
    _require(len(matrix) > 0, "gate training matrix is empty")
    _require(
        len(matrix) == len(labels) == len(sample_weights),
        "gate training arrays differ",
    )
    _require(set(np.unique(labels)) == {0.0, 1.0}, "gate labels need both classes")
    weights = np.zeros(matrix.shape[1], dtype=float)
    identity = np.eye(matrix.shape[1], dtype=float)
    converged = False
    iteration = 0
    for iteration in range(1, MODEL_MAX_ITERATIONS + 1):
        probabilities = _sigmoid(matrix @ weights)
        gradient = matrix.T @ (sample_weights * (probabilities - labels))
        gradient += MODEL_L2 * weights
        curvature = sample_weights * probabilities * (1.0 - probabilities)
        hessian = (matrix.T * curvature) @ matrix + MODEL_L2 * identity
        step = np.linalg.solve(hessian, gradient)
        weights -= step
        if float(np.max(np.abs(step))) <= MODEL_TOLERANCE:
            converged = True
            break
    return {
        "weights": weights,
        "l2": MODEL_L2,
        "iterations": iteration,
        "converged": converged,
        "objective_weight_sum": float(sample_weights.sum()),
        "model_fitted": True,
    }


def _model_public(model: dict[str, Any]) -> dict[str, Any]:
    weights = np.asarray(model["weights"], dtype=float)
    return {
        "feature_names": list(MODEL_FEATURE_NAMES),
        "coefficients": {
            name: round(float(value), 10)
            for name, value in zip(MODEL_FEATURE_NAMES, weights, strict=True)
        },
        "l2": model["l2"],
        "iterations": model["iterations"],
        "converged": model["converged"],
        "objective_weight_sum": round(float(model["objective_weight_sum"]), 12),
        "model_fitted": model["model_fitted"],
        "intercept": False,
    }


def score_buckets(
    buckets: list[dict[str, Any]],
    feature_map: dict[str, np.ndarray],
    model: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    weights = np.asarray(model["weights"], dtype=float)
    logits = {
        str(row["bucket_id"]): float(
            np.clip(
                feature_map[str(row["bucket_id"])] @ weights,
                -MODEL_SCORE_CLIP,
                MODEL_SCORE_CLIP,
            )
        )
        for row in buckets
    }
    probabilities = {
        key: float(_sigmoid(np.asarray([value], dtype=float))[0])
        for key, value in logits.items()
    }
    output: list[dict[str, Any]] = []
    for row in sorted(buckets, key=_bucket_sort_key):
        identifier = str(row["bucket_id"])
        probability = probabilities[identifier]
        values = feature_map[identifier]
        output.append(
            {
                "bucket_id": identifier,
                "signal_day": str(row["signal_day"]),
                "entry_day": str(row["entry_day"]),
                "signal_type": str(row["signal_type"]),
                "candidate_count": int(row["candidate_count"]),
                "bad_bucket_logit": round(logits[identifier], 10),
                "bad_bucket_probability": round(probability, 10),
                "gate_threshold": GATE_PROBABILITY_THRESHOLD,
                "predicted_rejected": probability > GATE_PROBABILITY_THRESHOLD,
                "train_ecdf_features": {
                    name: round(float(value), 12)
                    for name, value in zip(MODEL_FEATURE_NAMES, values, strict=True)
                },
            }
        )
    return output, probabilities


def _mean(values: list[float]) -> float | None:
    return float(math.fsum(values) / len(values)) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 10) if value is not None else None


def evaluate_gate(
    buckets: list[dict[str, Any]], probabilities: dict[str, float]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bucket in sorted(buckets, key=_bucket_sort_key):
        identifier = str(bucket["bucket_id"])
        actual_bad = bool(bucket["actual_bad_bucket"])
        rejected = probabilities[identifier] > GATE_PROBABILITY_THRESHOLD
        rows.append(
            {
                "bucket_id": identifier,
                "signal_day": str(bucket["signal_day"]),
                "entry_day": str(bucket["entry_day"]),
                "signal_type": str(bucket["signal_type"]),
                "candidate_count": int(bucket["candidate_count"]),
                "equal_weight_mean_trade_pnl_pct": round(
                    float(bucket["equal_weight_mean_trade_pnl_pct"]), 10
                ),
                "actual_bad_bucket": actual_bad,
                "predicted_rejected": rejected,
                "true_bad_rejected": actual_bad and rejected,
                "non_bad_kept": (not actual_bad) and (not rejected),
            }
        )
    bad = [row for row in rows if row["actual_bad_bucket"]]
    non_bad = [row for row in rows if not row["actual_bad_bucket"]]
    kept = [row for row in rows if not row["predicted_rejected"]]
    rejected = [row for row in rows if row["predicted_rejected"]]
    bad_capture = (
        sum(row["predicted_rejected"] for row in bad) / len(bad) if bad else None
    )
    non_bad_keep = (
        sum(not row["predicted_rejected"] for row in non_bad) / len(non_bad)
        if non_bad
        else None
    )
    balanced_accuracy = (
        (bad_capture + non_bad_keep) / 2.0
        if bad_capture is not None and non_bad_keep is not None
        else None
    )
    all_outcomes = [float(row["equal_weight_mean_trade_pnl_pct"]) for row in rows]
    kept_outcomes = [float(row["equal_weight_mean_trade_pnl_pct"]) for row in kept]
    rejected_outcomes = [
        float(row["equal_weight_mean_trade_pnl_pct"]) for row in rejected
    ]
    kept_mean = _mean(kept_outcomes)
    rejected_mean = _mean(rejected_outcomes)
    advantage = (
        kept_mean - rejected_mean
        if kept_mean is not None and rejected_mean is not None
        else None
    )
    baseline_negative_rate = (
        sum(value < 0.0 for value in all_outcomes) / len(all_outcomes)
        if all_outcomes
        else None
    )
    kept_negative_rate = (
        sum(value < 0.0 for value in kept_outcomes) / len(kept_outcomes)
        if kept_outcomes
        else None
    )
    return rows, {
        "buckets": len(rows),
        "candidates": sum(int(row["candidate_count"]) for row in rows),
        "actual_bad_buckets": len(bad),
        "actual_non_bad_buckets": len(non_bad),
        "predicted_kept_buckets": len(kept),
        "predicted_rejected_buckets": len(rejected),
        "bad_bucket_capture_rate": _rounded(bad_capture),
        "non_bad_bucket_keep_rate": _rounded(non_bad_keep),
        "balanced_accuracy": _rounded(balanced_accuracy),
        "random_expected_balanced_accuracy": 0.5,
        "balanced_accuracy_excess": _rounded(
            balanced_accuracy - 0.5 if balanced_accuracy is not None else None
        ),
        "all_bucket_mean_pnl_pct": _rounded(_mean(all_outcomes)),
        "kept_bucket_mean_pnl_pct": _rounded(kept_mean),
        "rejected_bucket_mean_pnl_pct": _rounded(rejected_mean),
        "kept_minus_rejected_mean_pnl_pp": _rounded(advantage),
        "baseline_negative_bucket_rate": _rounded(baseline_negative_rate),
        "kept_negative_bucket_rate": _rounded(kept_negative_rate),
        "kept_negative_rate_reduction": _rounded(
            baseline_negative_rate - kept_negative_rate
            if baseline_negative_rate is not None and kept_negative_rate is not None
            else None
        ),
    }


def _fit_scope(buckets: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    references, reference_audit = build_training_references(buckets)
    feature_map = transform_buckets(buckets, references, training=True)
    matrix, labels, weights, target_audit = build_training_data(buckets, feature_map)
    model = fit_logistic(matrix, labels, weights)
    _, probabilities = score_buckets(buckets, feature_map, model)
    _, metrics = evaluate_gate(buckets, probabilities)
    return model, {
        "feature_transform": reference_audit,
        "target": target_audit,
        "in_sample_metrics": metrics,
        "model": _model_public(model),
        "_references": references,
    }


def _research_screen(
    folds: list[dict[str, Any]], stitched: dict[str, Any]
) -> dict[str, Any]:
    balanced = [fold["evaluation"]["metrics"]["balanced_accuracy"] for fold in folds]
    advantages = [
        fold["evaluation"]["metrics"]["kept_minus_rejected_mean_pnl_pp"]
        for fold in folds
    ]
    balanced_passes = sum(value is not None and value > 0.5 for value in balanced)
    advantage_passes = sum(value is not None and value > 0.0 for value in advantages)
    valid_balanced = [float(value) for value in balanced if value is not None]
    valid_advantages = [float(value) for value in advantages if value is not None]
    metrics = stitched["metrics"]
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
        "every_fold_training_buckets_at_least_20": all(
            fold["training"]["target"]["bucket_count"] >= 20 for fold in folds
        ),
        "every_fold_training_has_at_least_5_buckets_per_class": all(
            fold["training"]["target"]["bad_buckets"] >= 5
            and fold["training"]["target"]["non_bad_buckets"] >= 5
            for fold in folds
        ),
        "at_least_four_folds_balanced_accuracy_above_half": balanced_passes >= 4,
        "at_least_four_folds_kept_mean_above_rejected": advantage_passes >= 4,
        "median_fold_balanced_accuracy_above_half": (
            len(valid_balanced) == len(folds)
            and statistics.median(valid_balanced) > 0.5
        ),
        "median_fold_kept_mean_advantage_above_zero": (
            len(valid_advantages) == len(folds)
            and statistics.median(valid_advantages) > 0.0
        ),
        "stitched_buckets_at_least_50": metrics["buckets"] >= 50,
        "stitched_kept_buckets_at_least_20": metrics["predicted_kept_buckets"] >= 20,
        "stitched_rejected_buckets_at_least_20": metrics["predicted_rejected_buckets"]
        >= 20,
        "stitched_balanced_accuracy_above_half": (
            metrics["balanced_accuracy"] is not None
            and metrics["balanced_accuracy"] > 0.5
        ),
        "stitched_kept_mean_above_rejected": (
            metrics["kept_minus_rejected_mean_pnl_pp"] is not None
            and metrics["kept_minus_rejected_mean_pnl_pp"] > 0.0
        ),
        "stitched_kept_negative_rate_below_ungated_baseline": (
            metrics["kept_negative_rate_reduction"] is not None
            and metrics["kept_negative_rate_reduction"] > 0.0
        ),
    }
    return {
        "checks": checks,
        "folds_balanced_accuracy_above_half": balanced_passes,
        "folds_kept_mean_above_rejected": advantage_passes,
        "median_fold_balanced_accuracy": (
            round(float(statistics.median(valid_balanced)), 10)
            if valid_balanced
            else None
        ),
        "median_fold_kept_mean_advantage_pp": (
            round(float(statistics.median(valid_advantages)), 10)
            if valid_advantages
            else None
        ),
        "passes_research_screen": all(checks.values()),
        "production_eligible": False,
    }


def build_snapshot(
    factor_report_path: Path, fold_report_path: Path, v10a_report_path: Path
) -> dict[str, Any]:
    _assert_frozen_cores()
    factor_report_path = _guard_development_path(factor_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    v10a_report_path = _guard_development_path(v10a_report_path)
    _require(
        _sha256_file(factor_report_path) == V10_FACTOR_REPORT_SHA256,
        "frozen v10 factor report SHA256 drift",
    )
    parent_v10a = _load_parent_v10a_report(v10a_report_path)
    factor_audit = v10audit.audit_run(factor_report_path)
    _require(
        factor_audit.get("checks_passed") is True,
        "v10 factor preflight single-report audit failed",
    )
    factor_report = factor_audit.get("_report_value")
    _require(isinstance(factor_report, dict), "v10 factor report value missing")
    _require(
        factor_report.get("version") == FACTOR_REPORT_VERSION,
        "factor report version is not frozen v10",
    )
    recorded_fold = v7a._resolve_report_input(
        factor_report, "fold_report", factor_report_path.parent
    )
    _require(recorded_fold == fold_report_path, "fold report differs from factor input")
    source_report_path = v7a._resolve_report_input(
        factor_report, "source_report", factor_report_path.parent
    )
    source_audit = v7a.audit_source_report(source_report_path)
    fold_audit = v7a.audit_fold_report(fold_report_path, source_audit)
    loaded_folds, all_fold_rows, integrity = v7a._load_eligible_fold_rows(
        fold_report_path, fold_audit["_report_value"]
    )
    factor_rows, factor_artifact = v10a._load_factor_rows(factor_audit)
    factor_by_id = {str(row["candidate_id"]): row for row in factor_rows}
    _require(
        set(factor_by_id) == {str(row["candidate_id"]) for row in all_fold_rows},
        "v10 factor snapshot does not cover audited fold candidates",
    )
    all_joined_rows = v10a._join_factor_rows(all_fold_rows, factor_by_id, "dataset")
    all_buckets = build_bucket_rows(all_joined_rows)

    fold_results: list[dict[str, Any]] = []
    artifact_rows: dict[str, list[dict[str, Any]]] = {}
    stitched_buckets: list[dict[str, Any]] = []
    stitched_probabilities: dict[str, float] = {}
    stitched_score_rows: list[dict[str, Any]] = []
    training_buckets_by_fold: dict[str, list[dict[str, Any]]] = {}
    evaluation_buckets_by_fold: dict[str, list[dict[str, Any]]] = {}
    training_rows_by_fold: dict[str, list[dict[str, Any]]] = {}
    evaluation_rows_by_fold: dict[str, list[dict[str, Any]]] = {}
    for fold in loaded_folds:
        fold_name = str(fold["fold_name"])
        train_rows = v10a._join_factor_rows(
            fold["rows"]["train"], factor_by_id, f"{fold_name}.train"
        )
        evaluation_rows = v10a._join_factor_rows(
            fold["rows"]["evaluation"], factor_by_id, f"{fold_name}.evaluation"
        )
        train_buckets = build_bucket_rows(train_rows)
        evaluation_buckets = build_bucket_rows(evaluation_rows)
        model, training = _fit_scope(train_buckets)
        references = training.pop("_references")
        evaluation_features = transform_buckets(
            evaluation_buckets, references, training=False
        )
        score_rows, probabilities = score_buckets(
            evaluation_buckets, evaluation_features, model
        )
        for row in score_rows:
            row["fold_name"] = fold_name
        metric_rows, metrics = evaluate_gate(evaluation_buckets, probabilities)
        for row in metric_rows:
            row["fold_name"] = fold_name
        artifact_rows[f"{fold_name}/bucket_gate_scores"] = score_rows
        artifact_rows[f"{fold_name}/bucket_metrics"] = metric_rows
        fold_results.append(
            {
                "fold_name": fold_name,
                "evaluation_window": fold["evaluation_window"],
                "input_artifacts": fold["input_artifacts"],
                "training": training,
                "evaluation": {"metrics": metrics},
            }
        )
        training_buckets_by_fold[fold_name] = train_buckets
        evaluation_buckets_by_fold[fold_name] = evaluation_buckets
        training_rows_by_fold[fold_name] = train_rows
        evaluation_rows_by_fold[fold_name] = evaluation_rows
        stitched_buckets.extend(evaluation_buckets)
        stitched_probabilities.update(probabilities)
        stitched_score_rows.extend(score_rows)
    stitched_score_rows.sort(
        key=lambda row: (
            row["signal_day"],
            row["entry_day"],
            row["signal_type"],
            row["bucket_id"],
        )
    )
    stitched_metric_rows, stitched_metrics = evaluate_gate(
        stitched_buckets, stitched_probabilities
    )
    artifact_rows["stitched_oos/bucket_gate_scores"] = stitched_score_rows
    artifact_rows["stitched_oos/bucket_metrics"] = stitched_metric_rows
    stitched = {
        "fold_names": list(EXPECTED_ELIGIBLE_FOLDS),
        "score_origin": "each bucket uses only its own fold train model and ECDF",
        "metrics": stitched_metrics,
    }
    screen = _research_screen(fold_results, stitched)
    return {
        "factor_audit": factor_audit,
        "factor_report": factor_report,
        "factor_artifact": factor_artifact,
        "source_audit": source_audit,
        "fold_audit": fold_audit,
        "parent_v10a": parent_v10a,
        "integrity": {
            **integrity,
            "factor_candidates": len(factor_rows),
            "factor_fold_identity_matches": True,
            "market_factor_names": list(FACTOR_NAMES),
            "feature_schema_intersection_with_outcomes": sorted(
                set(FACTOR_NAMES) & OUTCOME_FIELDS
            ),
            "bucket_key": list(BUCKET_KEY),
            "dataset_signal_buckets": len(all_buckets),
            "market_features_constant_inside_signal_bucket": True,
            "purged_training_labels_used": False,
            "evaluation_labels_used_for_fitting": False,
        },
        "fold_results": fold_results,
        "stitched": stitched,
        "screen": screen,
        "artifact_rows": artifact_rows,
        "source_report_path": source_report_path,
        "_all_joined_rows": all_joined_rows,
        "_training_rows_by_fold": training_rows_by_fold,
        "_evaluation_rows_by_fold": evaluation_rows_by_fold,
        "_training_buckets_by_fold": training_buckets_by_fold,
        "_evaluation_buckets_by_fold": evaluation_buckets_by_fold,
    }


def assemble_report(
    snapshot: dict[str, Any],
    factor_report_path: Path,
    fold_report_path: Path,
    v10a_report_path: Path,
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    folds: list[dict[str, Any]] = []
    for fold in snapshot["fold_results"]:
        fold_name = fold["fold_name"]
        folds.append(
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
            "Can three preregistered signal-day market-regime factors identify "
            "negative equal-weight signal buckets across development folds?"
        ),
        "preregistered_design": {
            "target_shift_from_v10a": "gate whole signal buckets; do not rank stocks",
            "eligible_folds": list(EXPECTED_ELIGIBLE_FOLDS),
            "bucket_key": list(BUCKET_KEY),
            "bucket_outcome": "equal-weight mean candidate trade_pnl_pct",
            "bad_bucket_rule": BAD_BUCKET_RULE,
            "raw_factor_names": list(FACTOR_NAMES),
            "model_feature_names": list(MODEL_FEATURE_NAMES),
            "feature_transform": (
                "training-fold empirical CDF; OOS values mapped only against "
                "training references; centered at zero"
            ),
            "model": "no-intercept binary logistic bad-bucket classifier",
            "bad_label": 1,
            "non_bad_label": 0,
            "class_weighting": (
                "bad=0.5, non_bad=0.5; equal bucket weight within each class"
            ),
            "outcome_magnitude_used_as_training_weight": False,
            "l2": MODEL_L2,
            "max_iterations": MODEL_MAX_ITERATIONS,
            "tolerance": MODEL_TOLERANCE,
            "model_score_clip": [-MODEL_SCORE_CLIP, MODEL_SCORE_CLIP],
            "gate_probability_threshold": GATE_PROBABILITY_THRESHOLD,
            "threshold_rule": "reject only when probability > 0.5; ties are kept",
            "hyperparameter_search": False,
            "threshold_search": False,
            "factor_selection": False,
            "stock_ranking": False,
            "portfolio_replay": False,
            "holdout_used": False,
        },
        "outcome_isolation": {
            "training_outcome": "same-fold train candidate trade_pnl_pct only",
            "training_outcome_use": "binary negative bucket membership only",
            "purged_training_labels_used": False,
            "evaluation_labels_used_for_fitting": False,
            "evaluation_labels_used_only_for_oos_metrics": True,
            "factor_snapshot_contains_outcomes": False,
        },
        "input": {
            "factor_report": _input_record(factor_report_path),
            "fold_report": _input_record(fold_report_path),
            "v10a_parent_report": _input_record(v10a_report_path),
            "source_report": _input_record(snapshot["source_report_path"]),
            "factor_candidates": snapshot["factor_artifact"],
            "experiment_code": _input_record(Path(__file__).resolve()),
            "audit_code": _input_record(AUDIT_CODE),
            "v10_preflight_code": _input_record(V10_PREFLIGHT_CODE),
            "v10_preflight_audit_code": _input_record(V10_PREFLIGHT_AUDIT_CODE),
            "v10a_parent_code": _input_record(V10A_PARENT_CODE),
            "v10a_parent_audit_code": _input_record(V10A_PARENT_AUDIT_CODE),
            "v7a_fold_loader_code": _input_record(V7A_FOLD_LOADER_CODE),
            "factor_preflight_audit": {
                "checks_passed": snapshot["factor_audit"]["checks_passed"],
                "candidate_count": snapshot["factor_audit"]["candidate_count"],
                "factor_count": snapshot["factor_audit"]["factor_count"],
                "independent_factor_implementation": snapshot["factor_audit"][
                    "independent_factor_implementation"
                ],
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
            "v10a_version": v10a.VERSION,
            "v10a_report_sha256": V10A_PARENT_REPORT_SHA256,
            "v10a_code_sha256": V10A_PARENT_CODE_SHA256,
            "v10a_passes_research_screen": False,
            "v10a_failed_checks": list(EXPECTED_V10A_FAILED_CHECKS),
            "nine_factor_cross_sectional_risk_route_terminated": True,
            "v10b_is_final_preregistered_route_for_current_v10_factors": True,
        },
        "integrity": snapshot["integrity"],
        "folds": folds,
        "stitched_oos": stitched,
        "screen": snapshot["screen"],
        "passes_research_screen": snapshot["screen"]["passes_research_screen"],
        "model_fitted": all(
            fold["training"]["model"]["model_fitted"] for fold in folds
        ),
        "hyperparameters_selected": False,
        "threshold_selected": False,
        "factor_selection_performed": False,
        "stock_ranking_performed": False,
        "portfolio_replayed": False,
        "holdout_used": False,
        "production_eligible": False,
    }


def build_report(
    factor_report_path: Path,
    fold_report_path: Path,
    v10a_report_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    factor_report_path = _guard_development_path(factor_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    v10a_report_path = _guard_development_path(v10a_report_path)
    output_dir = _guard_development_path(output_dir)
    _require(
        not output_dir.exists(),
        f"output directory already exists; refusing overwrite: {output_dir}",
    )
    snapshot = build_snapshot(factor_report_path, fold_report_path, v10a_report_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = {
        name: v7a._write_jsonl(output_dir / f"{name}.jsonl", rows)
        for name, rows in sorted(snapshot["artifact_rows"].items())
    }
    report = assemble_report(
        snapshot,
        factor_report_path,
        fold_report_path,
        v10a_report_path,
        artifacts,
    )
    v7a._write_json(output_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-report", type=Path, required=True)
    parser.add_argument("--fold-report", type=Path, required=True)
    parser.add_argument("--v10a-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(
            args.factor_report,
            args.fold_report,
            args.v10a_report,
            args.output_dir,
        )
    except Exception as exc:  # noqa: BLE001 - CLI fails closed as one JSON.
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
                "stock_ranking_performed": False,
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
