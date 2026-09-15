"""Run preregistered v10a bottom-tail risk filtering on development data.

The v9a target, weights, model, folds, and research screen remain frozen.  The
only model-input change is an outcome-blind structural partition of the audited
v10 factor snapshot: nine stock-varying factors enter within-bucket ranking,
while three market-regime factors are reserved because they are shared within a
signal day and must not rank stocks by mixed-signal-day timing.  No portfolio or
Holdout data is used.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import long_history_v7_top4_ranking_experiment as v7a
import long_history_v9_bottom_tail_risk_audit as v9audit
import long_history_v9_bottom_tail_risk_experiment as v9a
import long_history_v10_new_factor_preflight as v10preflight
import long_history_v10_new_factor_preflight_audit as v10audit

VERSION = "long_history_v10_cross_sectional_bottom_tail_risk.v1"
EXPERIMENT_STATUS = "preregistered_new_factor_bottom_tail_development"
DATASET_STATUS = v7a.DATASET_STATUS
FACTOR_REPORT_VERSION = v10preflight.VERSION
EXPECTED_ELIGIBLE_FOLDS = v9a.EXPECTED_ELIGIBLE_FOLDS
MIN_BUCKET_CANDIDATES = v9a.MIN_BUCKET_CANDIDATES
MODEL_L2 = v9a.MODEL_L2
MODEL_MAX_ITERATIONS = v9a.MODEL_MAX_ITERATIONS
MODEL_TOLERANCE = v9a.MODEL_TOLERANCE
MODEL_SCORE_CLIP = v9a.MODEL_SCORE_CLIP
RISK_FRACTION = v9a.RISK_FRACTION
TAIL_COUNT_RULE = v9a.TAIL_COUNT_RULE
OUTCOME_FIELDS = v9a.OUTCOME_FIELDS
ARTIFACT_NAMES = v9a.ARTIFACT_NAMES

FACTOR_NAMES = (
    "excess_return_5",
    "stock_index_correlation_20",
    "residual_volatility_20",
    "stock_realized_volatility_20",
    "drawdown_from_high_60",
    "price_efficiency_20",
    "volume_mean_5_to_20",
    "amount_mean_5_to_20",
    "return_volume_change_correlation_20",
)
RESERVED_MARKET_REGIME_FACTORS = (
    "index_return_60",
    "index_realized_volatility_20",
    "index_drawdown_from_high_60",
)
ALL_V10_FACTOR_NAMES = v10preflight.FACTOR_NAMES
MODEL_FEATURE_NAMES = tuple(f"rank_{name}" for name in FACTOR_NAMES)

V10_FACTOR_REPORT_SHA256 = (
    "bf0bab3217b99e74756488ab652d678b44f4ee513dbe878d4e8452285978c769"
)
V9A_PARENT_REPORT_SHA256 = v10preflight.V9A_PARENT_REPORT_SHA256
EXPECTED_V9A_FAILED_CHECKS = v10preflight.V9A_FAILED_CHECKS
V10_PREFLIGHT_CODE = Path(v10preflight.__file__).resolve()
V10_PREFLIGHT_CODE_SHA256 = (
    "0e2a28cf03365824a8985cb8214da147bca7bcae511817ad09ac82e85c3c6c38"
)
V10_PREFLIGHT_AUDIT_CODE = Path(v10audit.__file__).resolve()
V10_PREFLIGHT_AUDIT_CODE_SHA256 = (
    "5a656f5b7c83ac388893eba950f8c7fb6e784075b19be405e6c97b9cfb295bab"
)
V9A_TARGET_SCREEN_CORE_CODE = Path(v9a.__file__).resolve()
V9A_TARGET_SCREEN_CORE_SHA256 = (
    "bff8153ab445cf885fb051f3475864e5e17b123ca1f590dcd88ec1bcc014b3b9"
)
V9A_INDEPENDENT_AUDIT_CODE = Path(v9audit.__file__).resolve()
V9A_INDEPENDENT_AUDIT_CODE_SHA256 = (
    "0bf5d6ca2fa6b04c0ce34b803939153ed22831957c7ea8b22bd989bf0592dfef"
)
V7A_FOLD_MODEL_CORE_CODE = Path(v7a.__file__).resolve()
V7A_FOLD_MODEL_CORE_SHA256 = v9a.V7A_FEATURE_MODEL_CORE_SHA256
AUDIT_CODE = BASE_DIR / "long_history_v10_cross_sectional_bottom_tail_risk_audit.py"


class CrossSectionalBottomTailRiskError(RuntimeError):
    """Raised when a v10a input or frozen experiment contract is violated."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CrossSectionalBottomTailRiskError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require(
        not any("holdout" in part.lower() for part in resolved.parts),
        f"Holdout path is blocked: {resolved}",
    )
    return resolved


def _sha256_file(path: Path) -> str:
    return v7a._sha256_file(path)


def _input_record(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"input file does not exist: {path}")
    return {"path": str(path), "sha256": _sha256_file(path)}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrossSectionalBottomTailRiskError(
            f"cannot read JSON: {path}: {exc}"
        ) from exc
    _require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def _assert_frozen_cores() -> None:
    frozen = (
        (V10_PREFLIGHT_CODE, V10_PREFLIGHT_CODE_SHA256, "v10 preflight code"),
        (
            V10_PREFLIGHT_AUDIT_CODE,
            V10_PREFLIGHT_AUDIT_CODE_SHA256,
            "v10 preflight audit code",
        ),
        (
            V9A_TARGET_SCREEN_CORE_CODE,
            V9A_TARGET_SCREEN_CORE_SHA256,
            "v9a target/screen core",
        ),
        (
            V9A_INDEPENDENT_AUDIT_CODE,
            V9A_INDEPENDENT_AUDIT_CODE_SHA256,
            "v9a independent audit core",
        ),
        (
            V7A_FOLD_MODEL_CORE_CODE,
            V7A_FOLD_MODEL_CORE_SHA256,
            "v7a fold/model core",
        ),
    )
    for path, expected, label in frozen:
        _require(path.exists() and path.is_file(), f"missing frozen {label}: {path}")
        actual = _sha256_file(path)
        _require(
            actual == expected,
            f"frozen {label} SHA256 drift: expected {expected}, got {actual}",
        )


def _load_parent_v9a_report(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"missing v9a parent report: {path}")
    _require(
        _sha256_file(path) == V9A_PARENT_REPORT_SHA256,
        "frozen v9a parent report SHA256 drift",
    )
    report = _load_json(path)
    _require(report.get("version") == v9a.VERSION, "unexpected v9a parent version")
    _require(
        report.get("passes_research_screen") is False,
        "v9a parent did not fail its screen",
    )
    _require(report.get("holdout_used") is False, "v9a parent used Holdout")
    _require(
        report.get("portfolio_replayed") is False,
        "v9a parent replayed a portfolio",
    )
    _require(
        report.get("production_eligible") is False,
        "v9a parent is production eligible",
    )
    screen = report.get("screen")
    checks = screen.get("checks") if isinstance(screen, dict) else None
    _require(isinstance(checks, dict), "v9a parent screen checks are missing")
    failed = tuple(sorted(name for name, passed in checks.items() if passed is False))
    _require(
        failed == tuple(sorted(EXPECTED_V9A_FAILED_CHECKS)),
        f"v9a parent failed checks changed: {failed}",
    )
    inputs = report.get("input")
    code_record = inputs.get("experiment_code") if isinstance(inputs, dict) else None
    _require(isinstance(code_record, dict), "v9a parent experiment code is missing")
    _require(
        code_record.get("sha256") == V9A_TARGET_SCREEN_CORE_SHA256,
        "v9a parent experiment hash changed",
    )
    return report


def _load_factor_rows(
    factor_audit: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_path = factor_audit.get("_artifact_paths", {}).get("candidate_features")
    _require(raw_path is not None, "v10 candidate feature artifact path missing")
    path = Path(str(raw_path)).resolve()
    rows = v7a._load_jsonl(path)
    seen: set[str] = set()
    for row in rows:
        identifier = str(row.get("candidate_id", ""))
        _require(
            identifier and identifier not in seen, f"duplicate factor row: {identifier}"
        )
        seen.add(identifier)
        features = row.get("features")
        missing = row.get("missing")
        _require(
            isinstance(features, dict) and set(features) == set(ALL_V10_FACTOR_NAMES),
            f"v10 factor schema mismatch: {identifier}",
        )
        _require(
            isinstance(missing, dict) and set(missing) == set(ALL_V10_FACTOR_NAMES),
            f"v10 missing schema mismatch: {identifier}",
        )
        _require(
            all(bool(missing[name]) is (features[name] is None) for name in features),
            f"v10 missing flags mismatch: {identifier}",
        )
        _require(
            not (set(features) & OUTCOME_FIELDS),
            f"outcome entered v10 factor schema: {identifier}",
        )
    rows.sort(
        key=lambda row: (
            str(row["signal_day"]),
            str(row["signal_type"]),
            str(row["symbol"]),
            str(row["candidate_id"]),
        )
    )
    return rows, {
        "path": str(path),
        "rows": len(rows),
        "sha256": _sha256_file(path),
        "candidate_ids_sha256": v7a._candidate_manifest(rows),
    }


def _join_factor_rows(
    rows: list[dict[str, Any]], factor_by_id: dict[str, dict[str, Any]], label: str
) -> list[dict[str, Any]]:
    joined: list[dict[str, Any]] = []
    for row in rows:
        identifier = str(row["candidate_id"])
        factor = factor_by_id.get(identifier)
        _require(
            factor is not None,
            f"candidate missing from v10 factors: {label}.{identifier}",
        )
        for field in ("symbol", "signal_day", "signal_type"):
            _require(
                str(row.get(field)) == str(factor.get(field)),
                f"factor/fold identity mismatch: {label}.{identifier}.{field}",
            )
        copy = dict(row)
        copy["_v10_features"] = dict(factor["features"])
        joined.append(copy)
    return joined


def _value_token(value: Any) -> tuple[str, float | None]:
    finite = v7a._finite(value)
    return ("finite", round(finite, 12)) if finite is not None else ("missing", None)


def audit_market_regime_structure(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify market factors vary only when an execution bucket mixes signal days."""

    groups = v7a._bucket_groups(rows)
    varying_execution = {name: 0 for name in RESERVED_MARKET_REGIME_FACTORS}
    same_signal_day_violations = {name: 0 for name in RESERVED_MARKET_REGIME_FACTORS}
    mixed_signal_day_buckets = 0
    for key in sorted(groups):
        group = groups[key]
        signal_days = {str(row["signal_day"]) for row in group}
        mixed_signal_day_buckets += len(signal_days) > 1
        for name in RESERVED_MARKET_REGIME_FACTORS:
            distinct = {_value_token(row["_v10_features"].get(name)) for row in group}
            varying_execution[name] += len(distinct) > 1
            signal_day_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in group:
                signal_day_groups[str(row["signal_day"])].append(row)
            same_signal_day_violations[name] += sum(
                len({_value_token(row["_v10_features"].get(name)) for row in subgroup})
                > 1
                for subgroup in signal_day_groups.values()
            )
    same_signal_day_constant = not any(same_signal_day_violations.values())
    return {
        "bucket_key": ["entry_day", "signal_type"],
        "candidate_count": len(rows),
        "bucket_count": len(groups),
        "single_signal_day_buckets": len(groups) - mixed_signal_day_buckets,
        "mixed_signal_day_buckets": mixed_signal_day_buckets,
        "reserved_factor_names": list(RESERVED_MARKET_REGIME_FACTORS),
        "varying_execution_buckets_by_factor": varying_execution,
        "same_signal_day_violations_by_factor": same_signal_day_violations,
        "all_reserved_factors_constant_within_signal_day": same_signal_day_constant,
        "execution_bucket_variation_only_from_mixed_signal_days": (
            same_signal_day_constant
        ),
        "ranking_use_blocked_to_avoid_signal_timing_as_stock_selection": True,
        "checked_without_outcomes": True,
    }


def build_cross_sectional_feature_map(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["entry_day"]), str(row["signal_type"]))].append(row)
    feature_map: dict[str, np.ndarray] = {}
    non_missing = {name: 0 for name in FACTOR_NAMES}
    for key in sorted(groups):
        group = groups[key]
        raw_by_id: dict[str, dict[str, float | None]] = {}
        for row in group:
            identifier = str(row["candidate_id"])
            raw = row.get("_v10_features")
            _require(
                isinstance(raw, dict) and set(raw) == set(ALL_V10_FACTOR_NAMES),
                f"joined v10 factor schema mismatch: {identifier}",
            )
            raw_by_id[identifier] = {
                name: v7a._finite(raw[name]) for name in FACTOR_NAMES
            }
        valid = {
            name: [
                float(raw[name]) for raw in raw_by_id.values() if raw[name] is not None
            ]
            for name in FACTOR_NAMES
        }
        for row in group:
            identifier = str(row["candidate_id"])
            values: list[float] = []
            for name in FACTOR_NAMES:
                value = raw_by_id[identifier][name]
                if value is None:
                    values.append(0.0)
                else:
                    values.append(v7a._midrank(value, valid[name]) - 0.5)
                    non_missing[name] += 1
            feature_map[identifier] = np.asarray(values, dtype=float)
    return feature_map, {
        "candidate_count": len(rows),
        "execution_buckets": len(groups),
        "feature_names": list(MODEL_FEATURE_NAMES),
        "raw_feature_non_missing": non_missing,
        "missing_rank_value": 0.0,
        "rank_range": [-0.5, 0.5],
        "bucket_key": ["entry_day", "signal_type"],
        "reserved_market_regime_factors": list(RESERVED_MARKET_REGIME_FACTORS),
    }


def build_risk_training_data(
    rows: list[dict[str, Any]], feature_map: dict[str, np.ndarray]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    matrix, labels, weights, target = v9a.build_risk_training_data(rows, feature_map)
    if len(matrix) == 0:
        matrix = np.empty((0, len(MODEL_FEATURE_NAMES)), dtype=float)
    _require(
        matrix.ndim == 2 and matrix.shape[1] == len(MODEL_FEATURE_NAMES),
        "v10a risk training matrix has the wrong shape",
    )
    return matrix, labels, weights, target


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _zero_model() -> dict[str, Any]:
    return {
        "weights": np.zeros(len(MODEL_FEATURE_NAMES), dtype=float),
        "l2": MODEL_L2,
        "iterations": 0,
        "converged": False,
        "objective_weight_sum": 0.0,
        "model_fitted": False,
    }


def fit_logistic(
    matrix: np.ndarray, labels: np.ndarray, sample_weights: np.ndarray
) -> dict[str, Any]:
    _require(
        matrix.ndim == 2 and matrix.shape[1] == len(MODEL_FEATURE_NAMES),
        "logistic matrix has the wrong shape",
    )
    if len(matrix) == 0:
        return _zero_model()
    _require(
        len(matrix) == len(labels) == len(sample_weights),
        "logistic training arrays are inconsistent",
    )
    _require(set(np.unique(labels)) == {0.0, 1.0}, "logistic labels need both classes")
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
        "objective_weight_sum": round(float(model["objective_weight_sum"]), 10),
        "model_fitted": model["model_fitted"],
        "intercept": False,
    }


def _risk_probability(logit: float) -> float:
    clipped = max(-35.0, min(35.0, logit))
    return 1.0 / (1.0 + math.exp(-clipped))


def score_risk_rows(
    rows: list[dict[str, Any]],
    feature_map: dict[str, np.ndarray],
    model: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    weights = np.asarray(model["weights"], dtype=float)
    _require(len(weights) == len(MODEL_FEATURE_NAMES), "model weight count differs")
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
        reject_count = v9a._risk_count(len(group)) if eligible else 0
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


def _fit_scope(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, float]]:
    feature_map, feature_audit = build_cross_sectional_feature_map(rows)
    matrix, labels, weights, target_audit = build_risk_training_data(rows, feature_map)
    model = fit_logistic(matrix, labels, weights)
    _, scores = score_risk_rows(rows, feature_map, model)
    _, metrics = v9a.evaluate_risk_filter(rows, scores)
    return (
        model,
        {
            "feature_audit": feature_audit,
            "risk_target": target_audit,
            "in_sample_metrics": metrics,
            "model": _model_public(model),
        },
        scores,
    )


def build_snapshot(
    factor_report_path: Path, fold_report_path: Path, v9a_report_path: Path
) -> dict[str, Any]:
    _assert_frozen_cores()
    factor_report_path = _guard_development_path(factor_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    v9a_report_path = _guard_development_path(v9a_report_path)
    _require(
        _sha256_file(factor_report_path) == V10_FACTOR_REPORT_SHA256,
        "frozen v10 factor report SHA256 drift",
    )
    parent_v9a = _load_parent_v9a_report(v9a_report_path)
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
    _require(
        recorded_fold == fold_report_path,
        "explicit fold report differs from factor input",
    )
    source_report_path = v7a._resolve_report_input(
        factor_report, "source_report", factor_report_path.parent
    )
    source_audit = v7a.audit_source_report(source_report_path)
    fold_audit = v7a.audit_fold_report(fold_report_path, source_audit)
    loaded_folds, all_fold_rows, integrity = v7a._load_eligible_fold_rows(
        fold_report_path, fold_audit["_report_value"]
    )
    factor_rows, factor_artifact = _load_factor_rows(factor_audit)
    factor_by_id = {str(row["candidate_id"]): row for row in factor_rows}
    _require(
        set(factor_by_id) == {str(row["candidate_id"]) for row in all_fold_rows},
        "v10 factor snapshot does not exactly cover audited fold candidates",
    )
    all_joined_rows = _join_factor_rows(all_fold_rows, factor_by_id, "dataset")
    market_structure = audit_market_regime_structure(all_joined_rows)
    _require(
        market_structure["all_reserved_factors_constant_within_signal_day"] is True,
        "reserved market-regime factor varies among same-signal-day candidates",
    )

    fold_results: list[dict[str, Any]] = []
    artifact_rows: dict[str, list[dict[str, Any]]] = {}
    stitched_rows: list[dict[str, Any]] = []
    stitched_scores: dict[str, float] = {}
    stitched_score_rows: list[dict[str, Any]] = []
    training_rows_by_fold: dict[str, list[dict[str, Any]]] = {}
    for fold in loaded_folds:
        fold_name = str(fold["fold_name"])
        train_rows = _join_factor_rows(
            fold["rows"]["train"], factor_by_id, f"{fold_name}.train"
        )
        evaluation_rows = _join_factor_rows(
            fold["rows"]["evaluation"], factor_by_id, f"{fold_name}.evaluation"
        )
        model, training, _ = _fit_scope(train_rows)
        training_rows_by_fold[fold_name] = train_rows
        evaluation_feature_map, feature_audit = build_cross_sectional_feature_map(
            evaluation_rows
        )
        score_output, scores = score_risk_rows(
            evaluation_rows, evaluation_feature_map, model
        )
        for record in score_output:
            record["fold_name"] = fold_name
        bucket_output, metrics = v9a.evaluate_risk_filter(evaluation_rows, scores)
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
                "evaluation": {"feature_audit": feature_audit, "metrics": metrics},
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
    stitched_bucket_rows, stitched_metrics = v9a.evaluate_risk_filter(
        stitched_rows, stitched_scores
    )
    artifact_rows["stitched_oos/candidate_risk_scores"] = stitched_score_rows
    artifact_rows["stitched_oos/bucket_metrics"] = stitched_bucket_rows
    stitched = {
        "fold_names": list(EXPECTED_ELIGIBLE_FOLDS),
        "score_origin": "each candidate uses only its own fold train model",
        "metrics": stitched_metrics,
    }
    screen = v9a._research_screen(fold_results, stitched)
    return {
        "factor_audit": factor_audit,
        "factor_report": factor_report,
        "source_audit": source_audit,
        "fold_audit": fold_audit,
        "factor_artifact": factor_artifact,
        "parent_v9a": parent_v9a,
        "market_structure": market_structure,
        "integrity": {
            **integrity,
            "factor_candidates": len(factor_rows),
            "factor_fold_identity_matches": True,
            "all_v10_factor_names": list(ALL_V10_FACTOR_NAMES),
            "selected_cross_sectional_factor_names": list(FACTOR_NAMES),
            "reserved_market_regime_factor_names": list(RESERVED_MARKET_REGIME_FACTORS),
            "factor_partition_is_complete_and_disjoint": (
                set(FACTOR_NAMES).isdisjoint(RESERVED_MARKET_REGIME_FACTORS)
                and set(FACTOR_NAMES) | set(RESERVED_MARKET_REGIME_FACTORS)
                == set(ALL_V10_FACTOR_NAMES)
            ),
            "market_regime_structure": market_structure,
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
        "_all_joined_rows": all_joined_rows,
    }


def assemble_report(
    snapshot: dict[str, Any],
    factor_report_path: Path,
    fold_report_path: Path,
    v9a_report_path: Path,
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
            "Can nine preregistered stock-varying v10 factors identify the realized "
            "bottom 20% inside entry_day+signal_type buckets under the unchanged "
            "v9a target, model, weights, folds, and research screen?"
        ),
        "preregistered_design": {
            "comparison_parent": "v9a bottom-tail risk experiment",
            "only_model_input_change": (
                "replace the former 19 factors with nine outcome-blind v10 "
                "cross-sectional factors"
            ),
            "eligible_folds": list(EXPECTED_ELIGIBLE_FOLDS),
            "bucket_key": ["entry_day", "signal_type"],
            "minimum_bucket_candidates": MIN_BUCKET_CANDIDATES,
            "risk_fraction": RISK_FRACTION,
            "tail_count_rule": TAIL_COUNT_RULE,
            "actual_risk_tie_break": "trade_pnl_pct asc then candidate_id asc",
            "boundary_tie_policy": "deterministic candidate_id tie-break; included",
            "all_v10_factor_names": list(ALL_V10_FACTOR_NAMES),
            "raw_factor_names": list(FACTOR_NAMES),
            "model_feature_names": list(MODEL_FEATURE_NAMES),
            "reserved_market_regime_factors": list(RESERVED_MARKET_REGIME_FACTORS),
            "reservation_rule": (
                "reserve market-state factors from stock selection because they are "
                "constant within signal_day; any execution-bucket variation comes "
                "only from mixed signal dates and must not rank stocks by timing"
            ),
            "reservation_is_outcome_blind": True,
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
            "v9a_research_screen_reused_without_change": True,
            "hyperparameter_search": False,
            "factor_selection": False,
            "portfolio_replay": False,
            "holdout_used": False,
        },
        "outcome_isolation": {
            "factor_partition_uses_outcomes": False,
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
            "v9a_parent_report": _input_record(v9a_report_path),
            "source_report": _input_record(snapshot["source_report_path"]),
            "factor_candidates": snapshot["factor_artifact"],
            "experiment_code": _input_record(Path(__file__).resolve()),
            "audit_code": _input_record(AUDIT_CODE),
            "v10_preflight_code": _input_record(V10_PREFLIGHT_CODE),
            "v10_preflight_audit_code": _input_record(V10_PREFLIGHT_AUDIT_CODE),
            "v9a_target_screen_core_code": _input_record(V9A_TARGET_SCREEN_CORE_CODE),
            "v9a_independent_audit_code": _input_record(V9A_INDEPENDENT_AUDIT_CODE),
            "v7a_fold_model_core_code": _input_record(V7A_FOLD_MODEL_CORE_CODE),
            "factor_preflight_audit": {
                "version": snapshot["factor_report"]["version"],
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
            "v9a_version": v9a.VERSION,
            "v9a_report_sha256": V9A_PARENT_REPORT_SHA256,
            "v9a_code_sha256": V9A_TARGET_SCREEN_CORE_SHA256,
            "v9a_passes_research_screen": False,
            "v9a_failed_checks": list(EXPECTED_V9A_FAILED_CHECKS),
            "former_19_factor_modeling_route_terminated": True,
            "v10a_is_new_factor_development_comparison": True,
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
    v9a_report_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    factor_report_path = _guard_development_path(factor_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    v9a_report_path = _guard_development_path(v9a_report_path)
    output_dir = _guard_development_path(output_dir)
    _require(
        not output_dir.exists(),
        f"output directory already exists; refusing overwrite: {output_dir}",
    )
    snapshot = build_snapshot(factor_report_path, fold_report_path, v9a_report_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = {
        name: v7a._write_jsonl(output_dir / f"{name}.jsonl", rows)
        for name, rows in sorted(snapshot["artifact_rows"].items())
    }
    report = assemble_report(
        snapshot,
        factor_report_path,
        fold_report_path,
        v9a_report_path,
        artifacts,
    )
    v7a._write_json(output_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-report", type=Path, required=True)
    parser.add_argument("--fold-report", type=Path, required=True)
    parser.add_argument("--v9a-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(
            args.factor_report,
            args.fold_report,
            args.v9a_report,
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
