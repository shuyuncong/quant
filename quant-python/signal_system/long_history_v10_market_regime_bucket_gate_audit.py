"""Read-only full-replay audit for two v10b market-regime gate runs."""

from __future__ import annotations

import argparse
import bisect
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

import long_history_v10_market_regime_bucket_gate_experiment as experiment

VERSION = "long_history_v10_market_regime_bucket_gate_audit.v1"
AUDITED_FOLDS = tuple(f"fold_{number:02d}" for number in range(3, 9))
AUDITED_FACTORS = (
    "index_return_60",
    "index_realized_volatility_20",
    "index_drawdown_from_high_60",
)
AUDITED_MODEL_FEATURES = tuple(f"train_ecdf_{name}" for name in AUDITED_FACTORS)
AUDITED_BUCKET_KEY = ("signal_day", "entry_day", "signal_type")
AUDITED_ARTIFACTS = ("bucket_gate_scores", "bucket_metrics")
AUDITED_L2 = 1.0
AUDITED_MAX_ITERATIONS = 100
AUDITED_TOLERANCE = 1e-10
AUDITED_SCORE_CLIP = 100.0
AUDITED_GATE_THRESHOLD = 0.5


class MarketRegimeBucketGateAuditError(RuntimeError):
    """Raised when a v10b report or deterministic replay differs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MarketRegimeBucketGateAuditError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require(
        not any("holdout" in part.lower() for part in resolved.parts),
        f"Holdout path is blocked: {resolved}",
    )
    return resolved


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketRegimeBucketGateAuditError(
            f"cannot read JSON: {path}: {exc}"
        ) from exc
    _require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                _require(
                    isinstance(value, dict),
                    f"JSONL object required: {path}:{line_number}",
                )
                rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketRegimeBucketGateAuditError(
            f"cannot read JSONL: {path}: {exc}"
        ) from exc
    return rows


def _resolve_record(record: Any, report_path: Path, label: str) -> Path:
    _require(isinstance(record, dict), f"missing record: {label}")
    raw = record.get("path")
    _require(isinstance(raw, str) and raw, f"missing path: {label}")
    path = Path(raw)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"missing file: {path}")
    _require(
        record.get("sha256") == _sha256_file(path),
        f"SHA256 drift: {label}",
    )
    return path


def _reported_artifacts(
    report: dict[str, Any], report_path: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    records: dict[str, dict[str, Any]] = {}
    rows: dict[str, list[dict[str, Any]]] = {}
    folds = report.get("folds")
    _require(isinstance(folds, list), "fold list missing")
    _require(
        [fold.get("fold_name") for fold in folds] == list(AUDITED_FOLDS),
        "eligible fold list differs from contract",
    )
    scopes = [(str(fold["fold_name"]), fold) for fold in folds]
    stitched = report.get("stitched_oos")
    _require(isinstance(stitched, dict), "stitched_oos missing")
    scopes.append(("stitched_oos", stitched))
    for scope_name, scope in scopes:
        artifacts = scope.get("artifacts")
        _require(isinstance(artifacts, dict), f"artifacts missing: {scope_name}")
        _require(
            set(artifacts) == set(AUDITED_ARTIFACTS),
            f"artifact names differ: {scope_name}",
        )
        for artifact_name in AUDITED_ARTIFACTS:
            key = f"{scope_name}/{artifact_name}"
            record = artifacts[artifact_name]
            path = _resolve_record(record, report_path, key)
            loaded = _load_jsonl(path)
            _require(record.get("rows") == len(loaded), f"row count differs: {key}")
            if artifact_name == "bucket_gate_scores":
                for row in loaded:
                    _require(
                        not (set(row) & experiment.OUTCOME_FIELDS),
                        f"bucket gate score leaks outcome fields: {key}",
                    )
                    _require(
                        set(row.get("train_ecdf_features", {}))
                        == set(AUDITED_MODEL_FEATURES),
                        f"ECDF feature schema differs: {key}",
                    )
                    _require(
                        row.get("gate_threshold") == AUDITED_GATE_THRESHOLD,
                        f"gate threshold differs: {key}",
                    )
            records[key] = {
                "path": str(path),
                "rows": len(loaded),
                "sha256": _sha256_file(path),
            }
            rows[key] = loaded
    return records, rows


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _outcome(row: dict[str, Any]) -> float:
    value = _finite(row.get("trade_pnl_pct", row.get("pnl_pct")))
    _require(value is not None, f"missing outcome: {row.get('candidate_id')}")
    return float(value)


def _bucket_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return tuple(str(row[name]) for name in AUDITED_BUCKET_KEY)


def _independent_bucket_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_bucket_key(row), []).append(row)
    buckets: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        features: dict[str, float] = {}
        for name in AUDITED_FACTORS:
            values: set[float | None] = set()
            for row in group:
                payload = row.get("_v10_features")
                _require(isinstance(payload, dict), f"factor payload missing: {key}")
                values.add(_finite(payload.get(name)))
            _require(
                None not in values and len(values) == 1,
                f"market factor differs inside signal bucket: {key}.{name}",
            )
            features[name] = float(next(iter(values)))
        outcomes = [_outcome(row) for row in group]
        mean_outcome = float(math.fsum(outcomes) / len(outcomes))
        buckets.append(
            {
                "signal_day": key[0],
                "entry_day": key[1],
                "signal_type": key[2],
                "bucket_id": "|".join(key),
                "candidate_count": len(group),
                "features": features,
                "equal_weight_mean_trade_pnl_pct": mean_outcome,
                "actual_bad_bucket": mean_outcome < 0.0,
            }
        )
    return buckets


def _reference_digest(values: list[float]) -> str:
    payload = "\n".join(format(value, ".17g") for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _independent_references(
    buckets: list[dict[str, Any]],
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    references: dict[str, list[float]] = {}
    factor_references: dict[str, Any] = {}
    for name in AUDITED_FACTORS:
        values = sorted(float(row["features"][name]) for row in buckets)
        _require(
            values and all(math.isfinite(value) for value in values), f"bad {name}"
        )
        references[name] = values
        factor_references[name] = {
            "count": len(values),
            "minimum": round(values[0], 12),
            "maximum": round(values[-1], 12),
            "values_sha256": _reference_digest(values),
        }
    return references, {
        "method": "training-fold empirical CDF centered at zero",
        "training_only": True,
        "training_bucket_count": len(buckets),
        "factor_references": factor_references,
        "missing_value": 0.0,
        "range": [-0.5, 0.5],
    }


def _training_midrank(value: float, values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    left = bisect.bisect_left(values, value)
    right = bisect.bisect_right(values, value)
    return (left + 0.5 * (right - left - 1)) / (len(values) - 1) - 0.5


def _oos_ecdf(value: float, values: list[float]) -> float:
    left = bisect.bisect_left(values, value)
    right = bisect.bisect_right(values, value)
    return (left + 0.5 * (right - left)) / len(values) - 0.5


def _independent_features(
    buckets: list[dict[str, Any]],
    references: dict[str, list[float]],
    *,
    training: bool,
) -> dict[str, np.ndarray]:
    output: dict[str, np.ndarray] = {}
    for bucket in buckets:
        transformed = []
        for name in AUDITED_FACTORS:
            raw = _finite(bucket["features"].get(name))
            if raw is None:
                transformed.append(0.0)
            elif training:
                transformed.append(_training_midrank(raw, references[name]))
            else:
                transformed.append(_oos_ecdf(raw, references[name]))
        output[str(bucket["bucket_id"])] = np.asarray(transformed, dtype=float)
    return output


def _independent_training_data(
    buckets: list[dict[str, Any]], feature_map: dict[str, np.ndarray]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    ordered = sorted(buckets, key=_bucket_key)
    bad_count = sum(bool(row["actual_bad_bucket"]) for row in ordered)
    safe_count = len(ordered) - bad_count
    _require(bad_count > 0 and safe_count > 0, "training classes missing")
    bad_weight = 0.5 / bad_count
    safe_weight = 0.5 / safe_count
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
            "bad_buckets": bad_count,
            "non_bad_buckets": safe_count,
            "bad_bucket_rule": "equal_weight_mean_trade_pnl_pct < 0",
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
    return 1.0 / (1.0 + np.exp(-np.clip(values, -35.0, 35.0)))


def _independent_fit(
    matrix: np.ndarray, labels: np.ndarray, sample_weights: np.ndarray
) -> dict[str, Any]:
    _require(matrix.shape[1] == len(AUDITED_MODEL_FEATURES), "model width differs")
    coefficients = np.zeros(matrix.shape[1], dtype=float)
    identity = np.eye(matrix.shape[1], dtype=float)
    converged = False
    iteration = 0
    for iteration in range(1, AUDITED_MAX_ITERATIONS + 1):
        probabilities = _sigmoid(matrix @ coefficients)
        gradient = matrix.T @ (sample_weights * (probabilities - labels))
        gradient += AUDITED_L2 * coefficients
        curvature = sample_weights * probabilities * (1.0 - probabilities)
        hessian = (matrix.T * curvature) @ matrix + AUDITED_L2 * identity
        step = np.linalg.solve(hessian, gradient)
        coefficients -= step
        if float(np.max(np.abs(step))) <= AUDITED_TOLERANCE:
            converged = True
            break
    return {
        "weights": coefficients,
        "l2": AUDITED_L2,
        "iterations": iteration,
        "converged": converged,
        "objective_weight_sum": float(sample_weights.sum()),
        "model_fitted": True,
    }


def _public_model(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "feature_names": list(AUDITED_MODEL_FEATURES),
        "coefficients": {
            name: round(float(value), 10)
            for name, value in zip(
                AUDITED_MODEL_FEATURES, model["weights"], strict=True
            )
        },
        "l2": model["l2"],
        "iterations": model["iterations"],
        "converged": model["converged"],
        "objective_weight_sum": round(float(model["objective_weight_sum"]), 12),
        "model_fitted": model["model_fitted"],
        "intercept": False,
    }


def _independent_scores(
    buckets: list[dict[str, Any]],
    feature_map: dict[str, np.ndarray],
    model: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    weights = np.asarray(model["weights"], dtype=float)
    probabilities: dict[str, float] = {}
    logits: dict[str, float] = {}
    for bucket in buckets:
        identifier = str(bucket["bucket_id"])
        logit = float(
            np.clip(
                feature_map[identifier] @ weights,
                -AUDITED_SCORE_CLIP,
                AUDITED_SCORE_CLIP,
            )
        )
        logits[identifier] = logit
        probabilities[identifier] = float(_sigmoid(np.asarray([logit]))[0])
    output: list[dict[str, Any]] = []
    for bucket in sorted(buckets, key=_bucket_key):
        identifier = str(bucket["bucket_id"])
        probability = probabilities[identifier]
        output.append(
            {
                "bucket_id": identifier,
                "signal_day": str(bucket["signal_day"]),
                "entry_day": str(bucket["entry_day"]),
                "signal_type": str(bucket["signal_type"]),
                "candidate_count": int(bucket["candidate_count"]),
                "bad_bucket_logit": round(logits[identifier], 10),
                "bad_bucket_probability": round(probability, 10),
                "gate_threshold": AUDITED_GATE_THRESHOLD,
                "predicted_rejected": probability > AUDITED_GATE_THRESHOLD,
                "train_ecdf_features": {
                    name: round(float(value), 12)
                    for name, value in zip(
                        AUDITED_MODEL_FEATURES,
                        feature_map[identifier],
                        strict=True,
                    )
                },
            }
        )
    return output, probabilities


def _mean(values: list[float]) -> float | None:
    return float(math.fsum(values) / len(values)) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 10) if value is not None else None


def _independent_metrics(
    buckets: list[dict[str, Any]], probabilities: dict[str, float]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bucket in sorted(buckets, key=_bucket_key):
        identifier = str(bucket["bucket_id"])
        actual_bad = bool(bucket["actual_bad_bucket"])
        rejected = probabilities[identifier] > AUDITED_GATE_THRESHOLD
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
    balanced = (
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
    baseline_negative = sum(value < 0.0 for value in all_outcomes) / len(all_outcomes)
    kept_negative = (
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
        "balanced_accuracy": _rounded(balanced),
        "random_expected_balanced_accuracy": 0.5,
        "balanced_accuracy_excess": _rounded(
            balanced - 0.5 if balanced is not None else None
        ),
        "all_bucket_mean_pnl_pct": _rounded(_mean(all_outcomes)),
        "kept_bucket_mean_pnl_pct": _rounded(kept_mean),
        "rejected_bucket_mean_pnl_pct": _rounded(rejected_mean),
        "kept_minus_rejected_mean_pnl_pp": _rounded(advantage),
        "baseline_negative_bucket_rate": _rounded(baseline_negative),
        "kept_negative_bucket_rate": _rounded(kept_negative),
        "kept_negative_rate_reduction": _rounded(
            baseline_negative - kept_negative if kept_negative is not None else None
        ),
    }


def _independent_screen(
    fold_results: list[dict[str, Any]], stitched_metrics: dict[str, Any]
) -> dict[str, Any]:
    balanced = [
        fold["evaluation"]["metrics"]["balanced_accuracy"] for fold in fold_results
    ]
    advantages = [
        fold["evaluation"]["metrics"]["kept_minus_rejected_mean_pnl_pp"]
        for fold in fold_results
    ]
    valid_balanced = [float(value) for value in balanced if value is not None]
    valid_advantages = [float(value) for value in advantages if value is not None]
    balanced_passes = sum(value > 0.5 for value in valid_balanced)
    advantage_passes = sum(value > 0.0 for value in valid_advantages)
    checks = {
        "eligible_folds_exactly_fold_03_through_fold_08": [
            fold["fold_name"] for fold in fold_results
        ]
        == list(AUDITED_FOLDS),
        "all_six_models_fitted_and_converged": all(
            fold["training"]["model"]["model_fitted"]
            and fold["training"]["model"]["converged"]
            for fold in fold_results
        ),
        "every_fold_training_buckets_at_least_20": all(
            fold["training"]["target"]["bucket_count"] >= 20 for fold in fold_results
        ),
        "every_fold_training_has_at_least_5_buckets_per_class": all(
            fold["training"]["target"]["bad_buckets"] >= 5
            and fold["training"]["target"]["non_bad_buckets"] >= 5
            for fold in fold_results
        ),
        "at_least_four_folds_balanced_accuracy_above_half": balanced_passes >= 4,
        "at_least_four_folds_kept_mean_above_rejected": advantage_passes >= 4,
        "median_fold_balanced_accuracy_above_half": (
            len(valid_balanced) == len(fold_results)
            and statistics.median(valid_balanced) > 0.5
        ),
        "median_fold_kept_mean_advantage_above_zero": (
            len(valid_advantages) == len(fold_results)
            and statistics.median(valid_advantages) > 0.0
        ),
        "stitched_buckets_at_least_50": stitched_metrics["buckets"] >= 50,
        "stitched_kept_buckets_at_least_20": stitched_metrics["predicted_kept_buckets"]
        >= 20,
        "stitched_rejected_buckets_at_least_20": stitched_metrics[
            "predicted_rejected_buckets"
        ]
        >= 20,
        "stitched_balanced_accuracy_above_half": (
            stitched_metrics["balanced_accuracy"] is not None
            and stitched_metrics["balanced_accuracy"] > 0.5
        ),
        "stitched_kept_mean_above_rejected": (
            stitched_metrics["kept_minus_rejected_mean_pnl_pp"] is not None
            and stitched_metrics["kept_minus_rejected_mean_pnl_pp"] > 0.0
        ),
        "stitched_kept_negative_rate_below_ungated_baseline": (
            stitched_metrics["kept_negative_rate_reduction"] is not None
            and stitched_metrics["kept_negative_rate_reduction"] > 0.0
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


def _audit_independent_model(
    replay: dict[str, Any],
    report: dict[str, Any],
    reported_rows: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    all_rows = replay.get("_all_joined_rows")
    _require(isinstance(all_rows, list), "replay all joined rows missing")
    independent_all = _independent_bucket_rows(all_rows)
    _require(
        len(independent_all)
        == report.get("integrity", {}).get("dataset_signal_buckets"),
        "independent dataset bucket count differs",
    )
    train_rows_by_fold = replay.get("_training_rows_by_fold")
    eval_rows_by_fold = replay.get("_evaluation_rows_by_fold")
    _require(isinstance(train_rows_by_fold, dict), "replay train rows missing")
    _require(isinstance(eval_rows_by_fold, dict), "replay evaluation rows missing")
    reported_folds = {str(row["fold_name"]): row for row in report["folds"]}
    fold_results: list[dict[str, Any]] = []
    stitched_buckets: list[dict[str, Any]] = []
    stitched_probabilities: dict[str, float] = {}
    stitched_scores: list[dict[str, Any]] = []
    total_training_buckets = 0
    total_evaluation_buckets = 0
    for fold_name in AUDITED_FOLDS:
        train_buckets = _independent_bucket_rows(train_rows_by_fold[fold_name])
        evaluation_buckets = _independent_bucket_rows(eval_rows_by_fold[fold_name])
        _require(
            _canonical_json(train_buckets)
            == _canonical_json(replay["_training_buckets_by_fold"][fold_name]),
            f"independent training bucket construction differs: {fold_name}",
        )
        _require(
            _canonical_json(evaluation_buckets)
            == _canonical_json(replay["_evaluation_buckets_by_fold"][fold_name]),
            f"independent evaluation bucket construction differs: {fold_name}",
        )
        references, reference_audit = _independent_references(train_buckets)
        train_features = _independent_features(train_buckets, references, training=True)
        matrix, labels, weights, target = _independent_training_data(
            train_buckets, train_features
        )
        _require(
            math.isclose(float(weights[labels == 1.0].sum()), 0.5, abs_tol=1e-12),
            f"independent bad class weight differs: {fold_name}",
        )
        _require(
            math.isclose(float(weights[labels == 0.0].sum()), 0.5, abs_tol=1e-12),
            f"independent non-bad class weight differs: {fold_name}",
        )
        model = _independent_fit(matrix, labels, weights)
        _, train_probabilities = _independent_scores(
            train_buckets, train_features, model
        )
        _, in_sample_metrics = _independent_metrics(train_buckets, train_probabilities)
        training = {
            "feature_transform": reference_audit,
            "target": target,
            "in_sample_metrics": in_sample_metrics,
            "model": _public_model(model),
        }
        _require(
            _canonical_json(training)
            == _canonical_json(reported_folds[fold_name]["training"]),
            f"independent train-only transform, target, weights, or model differs: {fold_name}",
        )
        evaluation_features = _independent_features(
            evaluation_buckets, references, training=False
        )
        score_rows, probabilities = _independent_scores(
            evaluation_buckets, evaluation_features, model
        )
        for row in score_rows:
            row["fold_name"] = fold_name
        metric_rows, metrics = _independent_metrics(evaluation_buckets, probabilities)
        for row in metric_rows:
            row["fold_name"] = fold_name
        _require(
            _canonical_json(score_rows)
            == _canonical_json(reported_rows[f"{fold_name}/bucket_gate_scores"]),
            f"independent OOS ECDF features or gate scores differ: {fold_name}",
        )
        _require(
            _canonical_json(metric_rows)
            == _canonical_json(reported_rows[f"{fold_name}/bucket_metrics"]),
            f"independent bucket targets or metrics differ: {fold_name}",
        )
        _require(
            _canonical_json(metrics)
            == _canonical_json(reported_folds[fold_name]["evaluation"]["metrics"]),
            f"independent evaluation summary differs: {fold_name}",
        )
        fold_results.append(
            {
                "fold_name": fold_name,
                "training": training,
                "evaluation": {"metrics": metrics},
            }
        )
        stitched_buckets.extend(evaluation_buckets)
        stitched_probabilities.update(probabilities)
        stitched_scores.extend(score_rows)
        total_training_buckets += len(train_buckets)
        total_evaluation_buckets += len(evaluation_buckets)
    stitched_scores.sort(
        key=lambda row: (
            row["signal_day"],
            row["entry_day"],
            row["signal_type"],
            row["bucket_id"],
        )
    )
    stitched_metric_rows, stitched_metrics = _independent_metrics(
        stitched_buckets, stitched_probabilities
    )
    _require(
        _canonical_json(stitched_scores)
        == _canonical_json(reported_rows["stitched_oos/bucket_gate_scores"]),
        "independent stitched gate scores differ",
    )
    _require(
        _canonical_json(stitched_metric_rows)
        == _canonical_json(reported_rows["stitched_oos/bucket_metrics"]),
        "independent stitched bucket metrics differ",
    )
    _require(
        _canonical_json(stitched_metrics)
        == _canonical_json(report["stitched_oos"]["metrics"]),
        "independent stitched summary differs",
    )
    screen = _independent_screen(fold_results, stitched_metrics)
    _require(
        _canonical_json(screen) == _canonical_json(report["screen"]),
        "independent research screen differs",
    )
    return {
        "bucket_key": list(AUDITED_BUCKET_KEY),
        "dataset_buckets": len(independent_all),
        "training_buckets_checked": total_training_buckets,
        "evaluation_buckets_checked": total_evaluation_buckets,
        "market_features_constant_inside_signal_bucket": True,
        "training_reference_uses_training_buckets_only": True,
        "training_midrank_checked": True,
        "oos_ecdf_against_training_reference_checked": True,
        "bad_bucket_rule": "equal_weight_mean_trade_pnl_pct < 0",
        "bad_class_weight": 0.5,
        "non_bad_class_weight": 0.5,
        "threshold_rule": "reject only when probability > 0.5; ties are kept",
        "fold_models_checked": len(fold_results),
        "stitched_buckets_checked": stitched_metrics["buckets"],
        "checks_passed": True,
    }


def _audit_parent_v10a(path: Path) -> dict[str, Any]:
    report = experiment._load_parent_v10a_report(path)
    return {
        "version": report["version"],
        "report_sha256": _sha256_file(path),
        "failed_checks": list(experiment.EXPECTED_V10A_FAILED_CHECKS),
        "checks_passed": True,
    }


def audit_report(report_path: Path) -> dict[str, Any]:
    report_path = _guard_development_path(report_path)
    _require(
        report_path.exists() and report_path.is_file(), f"missing report: {report_path}"
    )
    report = _load_json(report_path)
    _require(report.get("version") == experiment.VERSION, "unexpected version")
    _require(
        report.get("experiment_status") == experiment.EXPERIMENT_STATUS,
        "unexpected experiment status",
    )
    _require(report.get("dataset_status") == experiment.DATASET_STATUS, "dataset drift")
    for key in (
        "hyperparameters_selected",
        "threshold_selected",
        "factor_selection_performed",
        "stock_ranking_performed",
        "portfolio_replayed",
        "holdout_used",
        "production_eligible",
    ):
        _require(report.get(key) is False, f"forbidden report state: {key}")
    design = report.get("preregistered_design")
    _require(isinstance(design, dict), "preregistered design missing")
    expected_design = {
        "eligible_folds": list(AUDITED_FOLDS),
        "bucket_key": list(AUDITED_BUCKET_KEY),
        "bucket_outcome": "equal-weight mean candidate trade_pnl_pct",
        "bad_bucket_rule": "equal_weight_mean_trade_pnl_pct < 0",
        "raw_factor_names": list(AUDITED_FACTORS),
        "model_feature_names": list(AUDITED_MODEL_FEATURES),
        "model": "no-intercept binary logistic bad-bucket classifier",
        "bad_label": 1,
        "non_bad_label": 0,
        "outcome_magnitude_used_as_training_weight": False,
        "l2": AUDITED_L2,
        "max_iterations": AUDITED_MAX_ITERATIONS,
        "tolerance": AUDITED_TOLERANCE,
        "model_score_clip": [-AUDITED_SCORE_CLIP, AUDITED_SCORE_CLIP],
        "gate_probability_threshold": AUDITED_GATE_THRESHOLD,
        "threshold_rule": "reject only when probability > 0.5; ties are kept",
        "hyperparameter_search": False,
        "threshold_search": False,
        "factor_selection": False,
        "stock_ranking": False,
        "portfolio_replay": False,
        "holdout_used": False,
    }
    for key, expected in expected_design.items():
        _require(design.get(key) == expected, f"preregistered design drift: {key}")
    _require(
        design.get("feature_transform")
        == (
            "training-fold empirical CDF; OOS values mapped only against "
            "training references; centered at zero"
        ),
        "feature transform drift",
    )
    _require(
        design.get("class_weighting")
        == "bad=0.5, non_bad=0.5; equal bucket weight within each class",
        "class weighting drift",
    )
    _require(
        design.get("target_shift_from_v10a")
        == "gate whole signal buckets; do not rank stocks",
        "target shift drift",
    )
    _require(
        tuple(experiment.FACTOR_NAMES) == AUDITED_FACTORS, "experiment factor drift"
    )
    _require(
        tuple(experiment.MODEL_FEATURE_NAMES) == AUDITED_MODEL_FEATURES,
        "experiment feature drift",
    )
    _require(
        tuple(experiment.BUCKET_KEY) == AUDITED_BUCKET_KEY,
        "experiment bucket key drift",
    )

    inputs = report.get("input")
    _require(isinstance(inputs, dict), "input section missing")
    factor_report_path = _resolve_record(
        inputs.get("factor_report"), report_path, "factor_report"
    )
    fold_report_path = _resolve_record(
        inputs.get("fold_report"), report_path, "fold_report"
    )
    v10a_report_path = _resolve_record(
        inputs.get("v10a_parent_report"), report_path, "v10a_parent_report"
    )
    _require(
        _sha256_file(factor_report_path) == experiment.V10_FACTOR_REPORT_SHA256,
        "v10 factor report anchor drift",
    )
    parent_v10a = _audit_parent_v10a(v10a_report_path)
    expected_code_paths = {
        "experiment_code": Path(experiment.__file__).resolve(),
        "audit_code": Path(__file__).resolve(),
        "v10_preflight_code": experiment.V10_PREFLIGHT_CODE,
        "v10_preflight_audit_code": experiment.V10_PREFLIGHT_AUDIT_CODE,
        "v10a_parent_code": experiment.V10A_PARENT_CODE,
        "v10a_parent_audit_code": experiment.V10A_PARENT_AUDIT_CODE,
        "v7a_fold_loader_code": experiment.V7A_FOLD_LOADER_CODE,
    }
    for label, expected in expected_code_paths.items():
        actual = _resolve_record(inputs.get(label), report_path, label)
        _require(actual == expected.resolve(), f"unexpected code path: {label}")
    experiment._assert_frozen_cores()

    records, reported_rows = _reported_artifacts(report, report_path)
    replay = experiment.build_snapshot(
        factor_report_path, fold_report_path, v10a_report_path
    )
    independent = _audit_independent_model(replay, report, reported_rows)
    _require(
        set(reported_rows) == set(replay["artifact_rows"]),
        "artifact set differs from full replay",
    )
    for key in sorted(reported_rows):
        _require(
            _canonical_json(reported_rows[key])
            == _canonical_json(replay["artifact_rows"][key]),
            f"full model replay differs: {key}",
        )
    expected_report = experiment.assemble_report(
        replay,
        factor_report_path,
        fold_report_path,
        v10a_report_path,
        records,
    )
    _require(
        _canonical_json(report) == _canonical_json(expected_report),
        "report differs from full model replay",
    )
    return {
        "report": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "passes_research_screen": report["passes_research_screen"],
        "model_fitted": report["model_fitted"],
        "artifact_sha256": {
            key: record["sha256"] for key, record in sorted(records.items())
        },
        "input_v10_factor_preflight": {
            "report_sha256": _sha256_file(factor_report_path),
            "candidate_count": replay["factor_audit"]["candidate_count"],
            "factor_count": replay["factor_audit"]["factor_count"],
            "independent_factor_implementation": replay["factor_audit"][
                "independent_factor_implementation"
            ],
            "checks_passed": replay["factor_audit"]["checks_passed"],
        },
        "parent_v10a": parent_v10a,
        "independent_bucket_gate": independent,
        "independent_screen": {"checks_passed": True},
        "checks_passed": True,
        "_report_value": report,
        "_artifact_paths": {
            key: Path(record["path"]) for key, record in records.items()
        },
    }


def _normalize_run_paths(value: Any, run_root: Path) -> Any:
    root = str(run_root.resolve())
    if isinstance(value, dict):
        return {
            key: _normalize_run_paths(item, run_root)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_normalize_run_paths(item, run_root) for item in value]
    if isinstance(value, str):
        for separator in ("\\", "/"):
            prefix = root + separator
            if value.startswith(prefix):
                return "<RUN_ROOT>/" + value[len(prefix) :].replace("\\", "/")
        if value == root:
            return "<RUN_ROOT>"
    return value


def run_audit(primary_report: Path, verify_report: Path) -> dict[str, Any]:
    primary_report = _guard_development_path(primary_report)
    verify_report = _guard_development_path(verify_report)
    _require(primary_report != verify_report, "primary and verify reports must differ")
    primary = audit_report(primary_report)
    verify = audit_report(verify_report)
    _require(
        set(primary["_artifact_paths"]) == set(verify["_artifact_paths"]),
        "artifact sets differ across runs",
    )
    artifact_checks: list[dict[str, Any]] = []
    for key in sorted(primary["_artifact_paths"]):
        primary_path = primary["_artifact_paths"][key]
        verify_path = verify["_artifact_paths"][key]
        identical = primary_path.read_bytes() == verify_path.read_bytes()
        artifact_checks.append(
            {
                "name": key,
                "primary_sha256": _sha256_file(primary_path),
                "verify_sha256": _sha256_file(verify_path),
                "artifact_byte_identical": identical,
            }
        )
    all_identical = all(row["artifact_byte_identical"] for row in artifact_checks)
    normalized_primary = _normalize_run_paths(
        primary["_report_value"], primary_report.parent
    )
    normalized_verify = _normalize_run_paths(
        verify["_report_value"], verify_report.parent
    )
    normalized_equal = _canonical_json(normalized_primary) == _canonical_json(
        normalized_verify
    )
    _require(all_identical, "primary/verify artifact bytes differ")
    _require(normalized_equal, "primary/verify normalized reports differ")
    return {
        "version": VERSION,
        "audit_mode": "read_only_full_v10b_market_regime_bucket_gate_replay",
        "passes_audit": True,
        "primary": {
            key: value for key, value in primary.items() if not key.startswith("_")
        },
        "verify": {
            key: value for key, value in verify.items() if not key.startswith("_")
        },
        "artifact_determinism": artifact_checks,
        "determinism": {
            "checked": True,
            "artifact_count": len(artifact_checks),
            "all_artifacts_byte_identical": all_identical,
            "normalized_report_equal": normalized_equal,
            "checks_passed": True,
        },
        "input_v10_factor_preflight_replayed": True,
        "parent_v10a_failure_anchored": True,
        "policy": {
            "read_only": True,
            "full_model_replay": True,
            "independent_bucket_construction_checked": True,
            "market_features_constant_inside_signal_bucket_checked": True,
            "independent_train_only_feature_transform_checked": True,
            "independent_bucket_target_and_weights_checked": True,
            "independent_research_screen_checked": True,
            "stock_ranking_performed": False,
            "outcome_magnitude_used_as_training_weight": False,
            "factor_selection_performed": False,
            "portfolio_replayed": False,
            "network_used": False,
            "database_used": False,
            "sql_executed": False,
            "holdout_used": False,
            "production_eligible": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-report", type=Path, required=True)
    parser.add_argument("--verify-report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_audit(args.primary_report, args.verify_report)
    except Exception as exc:  # noqa: BLE001 - audit CLI fails closed as one JSON.
        print(
            json.dumps(
                {"version": VERSION, "passes_audit": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
