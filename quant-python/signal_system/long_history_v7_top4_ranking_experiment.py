"""Run the preregistered v7a Top-4 boundary ranker on frozen development data.

The experiment validates ranking mechanism only.  It never accepts Holdout
paths, changes factors, searches hyperparameters, or runs a capital portfolio.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from long_history_pairwise_ranking_experiment import _load_eligible_fold_rows
from long_history_v7_factor_preflight import FACTOR_NAMES
from long_history_v7_factor_preflight_audit import audit_run as audit_factor_report
from long_history_walk_forward_audit import audit_fold_report, audit_source_report

VERSION = "long_history_v7_top4_ranking.v1"
DATASET_STATUS = "development_viewed_not_holdout"
FACTOR_REPORT_VERSION = "long_history_v7_factor_preflight.v2"
EXPECTED_ELIGIBLE_FOLDS = tuple(f"fold_{index:02d}" for index in range(3, 9))
TOP_K = 4
MIN_BUCKET_CANDIDATES = 5
MODEL_FEATURE_NAMES = tuple(f"rank_{name}" for name in FACTOR_NAMES)
MODEL_L2 = 1.0
MODEL_MAX_ITERATIONS = 100
MODEL_TOLERANCE = 1e-10
MODEL_SCORE_CLIP = 100.0
ARTIFACT_NAMES = ("candidate_scores", "bucket_metrics")
AUDIT_CODE = BASE_DIR / "long_history_v7_top4_ranking_audit.py"
FACTOR_PREFLIGHT_CODE = BASE_DIR / "long_history_v7_factor_preflight.py"
FACTOR_PREFLIGHT_AUDIT_CODE = BASE_DIR / "long_history_v7_factor_preflight_audit.py"
V5_AUDIT_CODE = BASE_DIR / "long_history_walk_forward_audit.py"
FOLD_LOADER_CODE = BASE_DIR / "long_history_pairwise_ranking_experiment.py"
OUTCOME_FIELDS = frozenset(
    {
        "trade_pnl_pct",
        "pnl_pct",
        "exit_day",
        "exit_reason",
        "exit_price",
        "mfe",
        "mae",
        "future_5d",
        "future_20d",
        "future_40d",
        "post_exit_5d",
        "post_exit_20d",
    }
)


class Top4RankingExperimentError(RuntimeError):
    """Raised when an input or frozen v7a experiment contract is violated."""


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise Top4RankingExperimentError(f"Holdout path is blocked: {resolved}")
    return resolved


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _input_record(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    if not path.exists() or not path.is_file():
        raise Top4RankingExperimentError(f"input file does not exist: {path}")
    return {"path": str(path), "sha256": _sha256_file(path)}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise Top4RankingExperimentError(
                    f"JSONL object required: {path}:{line_number}"
                )
            rows.append(value)
    return rows


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_json(row) + "\n")
    return {"path": str(path), "rows": len(rows), "sha256": _sha256_file(path)}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _outcome(row: dict[str, Any]) -> float:
    value = _finite(row.get("trade_pnl_pct", row.get("pnl_pct")))
    if value is None:
        raise Top4RankingExperimentError(
            f"missing outcome for candidate {row.get('candidate_id')}"
        )
    return value


def _candidate_manifest(rows: list[dict[str, Any]]) -> str:
    identifiers = sorted(str(row["candidate_id"]) for row in rows)
    return _sha256_bytes("\n".join(identifiers).encode("utf-8"))


def _resolve_report_input(report: dict[str, Any], name: str, base: Path) -> Path:
    inputs = report.get("input")
    inputs = inputs if isinstance(inputs, dict) else {}
    record = inputs.get(name)
    if not isinstance(record, dict) or not record.get("path"):
        raise Top4RankingExperimentError(f"factor report lacks input record: {name}")
    path = Path(str(record["path"]))
    if not path.is_absolute():
        path = base / path
    path = _guard_development_path(path)
    if _sha256_file(path) != str(record.get("sha256", "")):
        raise Top4RankingExperimentError(f"factor report input SHA256 drift: {name}")
    return path


def _load_factor_rows(
    factor_audit: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = Path(str(factor_audit["_artifact_paths"]["candidate_features"])).resolve()
    rows = _load_jsonl(path)
    seen: set[str] = set()
    for row in rows:
        identifier = str(row.get("candidate_id", ""))
        if not identifier or identifier in seen:
            raise Top4RankingExperimentError(
                f"missing or duplicate factor candidate_id: {identifier}"
            )
        seen.add(identifier)
        features = row.get("features")
        missing = row.get("missing")
        if not isinstance(features, dict) or set(features) != set(FACTOR_NAMES):
            raise Top4RankingExperimentError(f"factor schema mismatch: {identifier}")
        if not isinstance(missing, dict) or set(missing) != set(FACTOR_NAMES):
            raise Top4RankingExperimentError(f"missing schema mismatch: {identifier}")
        if any(
            bool(missing[name]) is not (features[name] is None) for name in FACTOR_NAMES
        ):
            raise Top4RankingExperimentError(f"missing flag mismatch: {identifier}")
        if set(features) & OUTCOME_FIELDS:
            raise Top4RankingExperimentError("outcome entered v7 factor schema")
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
        "candidate_ids_sha256": _candidate_manifest(rows),
    }


def _join_factor_rows(
    rows: list[dict[str, Any]], factor_by_id: dict[str, dict[str, Any]], label: str
) -> list[dict[str, Any]]:
    joined: list[dict[str, Any]] = []
    for row in rows:
        identifier = str(row["candidate_id"])
        factor = factor_by_id.get(identifier)
        if factor is None:
            raise Top4RankingExperimentError(
                f"candidate missing from factor snapshot: {label}.{identifier}"
            )
        for field in ("symbol", "signal_day", "signal_type"):
            if str(row.get(field)) != str(factor.get(field)):
                raise Top4RankingExperimentError(
                    f"factor/fold identity mismatch: {label}.{identifier}.{field}"
                )
        copy = dict(row)
        copy["_v7_features"] = dict(factor["features"])
        joined.append(copy)
    return joined


def _midrank(value: float, values: list[float]) -> float:
    if len(values) < 2:
        return 0.5
    below = sum(other < value for other in values)
    equal = sum(other == value for other in values)
    return (below + (equal - 1) * 0.5) / (len(values) - 1)


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
            raw = row.get("_v7_features")
            if not isinstance(raw, dict) or set(raw) != set(FACTOR_NAMES):
                raise Top4RankingExperimentError(
                    f"joined factor schema mismatch: {identifier}"
                )
            raw_by_id[identifier] = {name: _finite(raw[name]) for name in FACTOR_NAMES}
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
                    values.append(_midrank(value, valid[name]) - 0.5)
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
    }


def _bucket_groups(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["entry_day"]), str(row["signal_type"]))].append(row)
    return dict(groups)


def build_boundary_training_data(
    rows: list[dict[str, Any]], feature_map: dict[str, np.ndarray]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    differences: list[np.ndarray] = []
    labels: list[float] = []
    sample_weights: list[float] = []
    eligible_buckets = 0
    buckets_with_pairs = 0
    candidates_in_eligible_buckets = 0
    undirected_pairs = 0
    ties_excluded = 0
    groups = _bucket_groups(rows)
    for key in sorted(groups):
        group = groups[key]
        if len(group) < MIN_BUCKET_CANDIDATES:
            continue
        eligible_buckets += 1
        candidates_in_eligible_buckets += len(group)
        ordered = sorted(
            group,
            key=lambda row: (-_outcome(row), str(row["candidate_id"])),
        )
        actual_top = ordered[:TOP_K]
        rest = ordered[TOP_K:]
        valid_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for winner in actual_top:
            for loser in rest:
                if abs(_outcome(winner) - _outcome(loser)) <= 1e-12:
                    ties_excluded += 1
                    continue
                valid_pairs.append((winner, loser))
        if not valid_pairs:
            continue
        buckets_with_pairs += 1
        undirected_pairs += len(valid_pairs)
        directed_weight = 1.0 / (2.0 * len(valid_pairs))
        for winner, loser in valid_pairs:
            difference = (
                feature_map[str(winner["candidate_id"])]
                - feature_map[str(loser["candidate_id"])]
            )
            differences.extend((difference, -difference))
            labels.extend((1.0, 0.0))
            sample_weights.extend((directed_weight, directed_weight))
    if not differences:
        matrix = np.empty((0, len(MODEL_FEATURE_NAMES)), dtype=float)
    else:
        matrix = np.asarray(differences, dtype=float)
    return (
        matrix,
        np.asarray(labels, dtype=float),
        np.asarray(sample_weights, dtype=float),
        {
            "eligible_buckets": eligible_buckets,
            "buckets_with_pairs": buckets_with_pairs,
            "candidates_in_eligible_buckets": candidates_in_eligible_buckets,
            "undirected_boundary_pairs": undirected_pairs,
            "directed_samples": len(differences),
            "boundary_ties_excluded": ties_excluded,
            "bucket_weight_sum": round(float(sum(sample_weights)), 10),
        },
    )


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


def fit_pairwise_logistic(
    matrix: np.ndarray,
    labels: np.ndarray,
    sample_weights: np.ndarray,
) -> dict[str, Any]:
    if matrix.ndim != 2 or matrix.shape[1] != len(MODEL_FEATURE_NAMES):
        raise Top4RankingExperimentError("pairwise matrix has the wrong shape")
    if len(matrix) == 0:
        return _zero_model()
    if len(matrix) != len(labels) or len(labels) != len(sample_weights):
        raise Top4RankingExperimentError("pairwise training arrays are inconsistent")
    if set(np.unique(labels)) != {0.0, 1.0}:
        raise Top4RankingExperimentError("pairwise labels require both classes")
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


def score_rows(
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
    groups = _bucket_groups(rows)
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
        for row in group:
            identifier = str(row["candidate_id"])
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
                    "model_score": round(scores[identifier], 10),
                    "predicted_rank": ranks[identifier],
                    "predicted_top4": bool(eligible and ranks[identifier] <= TOP_K),
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
            row["predicted_rank"],
            row["candidate_id"],
        )
    )
    return output, scores


def evaluate_top4(
    rows: list[dict[str, Any]], scores: dict[str, float]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bucket_rows: list[dict[str, Any]] = []
    groups = _bucket_groups(rows)
    for key in sorted(groups):
        group = groups[key]
        if len(group) < MIN_BUCKET_CANDIDATES:
            continue
        actual_order = sorted(
            group,
            key=lambda row: (-_outcome(row), str(row["candidate_id"])),
        )
        predicted_order = sorted(
            group,
            key=lambda row: (
                -scores[str(row["candidate_id"])],
                str(row["candidate_id"]),
            ),
        )
        actual_top = actual_order[:TOP_K]
        actual_rest = actual_order[TOP_K:]
        predicted_top = predicted_order[:TOP_K]
        predicted_rest = predicted_order[TOP_K:]
        correct = 0.0
        pairs = 0
        ties_excluded = 0
        for winner in actual_top:
            for loser in actual_rest:
                if abs(_outcome(winner) - _outcome(loser)) <= 1e-12:
                    ties_excluded += 1
                    continue
                winner_score = scores[str(winner["candidate_id"])]
                loser_score = scores[str(loser["candidate_id"])]
                correct += (
                    1.0
                    if winner_score > loser_score
                    else 0.5
                    if winner_score == loser_score
                    else 0.0
                )
                pairs += 1
        predicted_mean = statistics.mean(_outcome(row) for row in predicted_top)
        rest_mean = statistics.mean(_outcome(row) for row in predicted_rest)
        advantage = predicted_mean - rest_mean
        hits = len(
            {str(row["candidate_id"]) for row in actual_top}
            & {str(row["candidate_id"]) for row in predicted_top}
        )
        bucket_rows.append(
            {
                "entry_day": key[0],
                "signal_type": key[1],
                "bucket_size": len(group),
                "boundary_pairs": pairs,
                "boundary_ties_excluded": ties_excluded,
                "boundary_accuracy": round(correct / pairs, 10) if pairs else None,
                "predicted_top4_mean_pnl_pct": round(predicted_mean, 10),
                "rest_mean_pnl_pct": round(rest_mean, 10),
                "top4_advantage_pp": round(advantage, 10),
                "top4_advantage_positive": advantage > 0.0,
                "top4_hits": hits,
                "top4_hit_rate": round(hits / TOP_K, 10),
                "random_expected_hit_rate": round(TOP_K / len(group), 10),
            }
        )
    return bucket_rows, aggregate_bucket_metrics(bucket_rows)


def aggregate_bucket_metrics(bucket_rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid_accuracy = [
        float(row["boundary_accuracy"])
        for row in bucket_rows
        if row["boundary_accuracy"] is not None
    ]
    advantages = [float(row["top4_advantage_pp"]) for row in bucket_rows]
    hit_rates = [float(row["top4_hit_rate"]) for row in bucket_rows]
    random_hit_rates = [float(row["random_expected_hit_rate"]) for row in bucket_rows]
    by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bucket_rows:
        by_signal[str(row["signal_type"])].append(row)

    def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        accuracy = [
            float(row["boundary_accuracy"])
            for row in rows
            if row["boundary_accuracy"] is not None
        ]
        advantage = [float(row["top4_advantage_pp"]) for row in rows]
        return {
            "eligible_buckets": len(rows),
            "buckets_with_boundary_pairs": len(accuracy),
            "boundary_pairs": sum(int(row["boundary_pairs"]) for row in rows),
            "bucket_weighted_boundary_accuracy": (
                round(statistics.mean(accuracy), 10) if accuracy else None
            ),
            "top4_advantage_mean_pp": (
                round(statistics.mean(advantage), 10) if advantage else None
            ),
            "top4_advantage_median_pp": (
                round(statistics.median(advantage), 10) if advantage else None
            ),
            "positive_advantage_bucket_pct": (
                round(
                    sum(value > 0.0 for value in advantage) / len(advantage) * 100.0, 10
                )
                if advantage
                else None
            ),
        }

    return {
        "eligible_buckets": len(bucket_rows),
        "buckets_with_boundary_pairs": len(valid_accuracy),
        "candidates_in_eligible_buckets": sum(
            int(row["bucket_size"]) for row in bucket_rows
        ),
        "boundary_pairs": sum(int(row["boundary_pairs"]) for row in bucket_rows),
        "boundary_ties_excluded": sum(
            int(row["boundary_ties_excluded"]) for row in bucket_rows
        ),
        "bucket_weighted_boundary_accuracy": (
            round(statistics.mean(valid_accuracy), 10) if valid_accuracy else None
        ),
        "top4_advantage_mean_pp": (
            round(statistics.mean(advantages), 10) if advantages else None
        ),
        "top4_advantage_median_pp": (
            round(statistics.median(advantages), 10) if advantages else None
        ),
        "positive_advantage_bucket_pct": (
            round(
                sum(value > 0.0 for value in advantages) / len(advantages) * 100.0, 10
            )
            if advantages
            else None
        ),
        "top4_hit_rate": round(statistics.mean(hit_rates), 10) if hit_rates else None,
        "random_expected_hit_rate": (
            round(statistics.mean(random_hit_rates), 10) if random_hit_rates else None
        ),
        "by_signal_type": {
            signal_type: summary(by_signal[signal_type])
            for signal_type in sorted(by_signal)
        },
    }


def _fit_scope(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, float]]:
    feature_map, feature_audit = build_cross_sectional_feature_map(rows)
    matrix, labels, weights, pair_audit = build_boundary_training_data(
        rows, feature_map
    )
    model = fit_pairwise_logistic(matrix, labels, weights)
    _, scores = score_rows(rows, feature_map, model)
    _, metrics = evaluate_top4(rows, scores)
    return (
        model,
        {
            "feature_audit": feature_audit,
            "pairs": pair_audit,
            "in_sample_metrics": metrics,
            "model": _model_public(model),
        },
        scores,
    )


def _research_screen(
    folds: list[dict[str, Any]], stitched: dict[str, Any]
) -> dict[str, Any]:
    accuracies = [
        fold["evaluation"]["metrics"]["bucket_weighted_boundary_accuracy"]
        for fold in folds
    ]
    advantages = [
        fold["evaluation"]["metrics"]["top4_advantage_mean_pp"] for fold in folds
    ]
    accuracy_passes = sum(value is not None and value > 0.5 for value in accuracies)
    advantage_passes = sum(value is not None and value > 0.0 for value in advantages)
    valid_advantages = [float(value) for value in advantages if value is not None]
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
        "every_fold_training_boundary_buckets_at_least_5": all(
            fold["training"]["pairs"]["buckets_with_pairs"] >= 5 for fold in folds
        ),
        "at_least_four_folds_boundary_accuracy_above_random": accuracy_passes >= 4,
        "at_least_four_folds_top4_mean_advantage_above_zero": advantage_passes >= 4,
        "median_fold_top4_mean_advantage_above_zero": (
            len(valid_advantages) == len(folds)
            and statistics.median(valid_advantages) > 0.0
        ),
        "stitched_eligible_buckets_at_least_20": stitched_metrics["eligible_buckets"]
        >= 20,
        "stitched_boundary_pairs_at_least_100": stitched_metrics["boundary_pairs"]
        >= 100,
        "stitched_boundary_accuracy_above_random": (
            stitched_metrics["bucket_weighted_boundary_accuracy"] is not None
            and stitched_metrics["bucket_weighted_boundary_accuracy"] > 0.5
        ),
        "stitched_top4_mean_advantage_above_zero": (
            stitched_metrics["top4_advantage_mean_pp"] is not None
            and stitched_metrics["top4_advantage_mean_pp"] > 0.0
        ),
        "stitched_top4_median_advantage_above_zero": (
            stitched_metrics["top4_advantage_median_pp"] is not None
            and stitched_metrics["top4_advantage_median_pp"] > 0.0
        ),
        "stitched_positive_advantage_bucket_rate_above_half": (
            stitched_metrics["positive_advantage_bucket_pct"] is not None
            and stitched_metrics["positive_advantage_bucket_pct"] > 50.0
        ),
        "stitched_top4_hit_rate_above_random_expectation": (
            stitched_metrics["top4_hit_rate"] is not None
            and stitched_metrics["random_expected_hit_rate"] is not None
            and stitched_metrics["top4_hit_rate"]
            > stitched_metrics["random_expected_hit_rate"]
        ),
    }
    return {
        "checks": checks,
        "folds_boundary_accuracy_above_random": accuracy_passes,
        "folds_top4_mean_advantage_above_zero": advantage_passes,
        "median_fold_top4_mean_advantage_pp": (
            round(statistics.median(valid_advantages), 10) if valid_advantages else None
        ),
        "passes_research_screen": all(checks.values()),
        "production_eligible": False,
    }


def build_snapshot(factor_report_path: Path, fold_report_path: Path) -> dict[str, Any]:
    factor_report_path = _guard_development_path(factor_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    factor_audit = audit_factor_report(factor_report_path)
    if factor_audit.get("checks_passed") is not True:
        raise Top4RankingExperimentError("factor preflight single-report audit failed")
    factor_report = factor_audit["_report_value"]
    if factor_report.get("version") != FACTOR_REPORT_VERSION:
        raise Top4RankingExperimentError("factor report version is not frozen v2")
    recorded_fold = _resolve_report_input(
        factor_report, "fold_report", factor_report_path.parent
    )
    if recorded_fold != fold_report_path:
        raise Top4RankingExperimentError(
            "explicit fold report differs from factor input"
        )
    source_report_path = _resolve_report_input(
        factor_report, "source_report", factor_report_path.parent
    )
    source_audit = audit_source_report(source_report_path)
    fold_audit = audit_fold_report(fold_report_path, source_audit)
    loaded_folds, all_fold_rows, integrity = _load_eligible_fold_rows(
        fold_report_path, fold_audit["_report_value"]
    )
    factor_rows, factor_artifact = _load_factor_rows(factor_audit)
    factor_by_id = {str(row["candidate_id"]): row for row in factor_rows}
    if set(factor_by_id) != {str(row["candidate_id"]) for row in all_fold_rows}:
        raise Top4RankingExperimentError(
            "factor snapshot does not exactly cover audited fold candidates"
        )
    _join_factor_rows(all_fold_rows, factor_by_id, "dataset")

    fold_results: list[dict[str, Any]] = []
    artifact_rows: dict[str, list[dict[str, Any]]] = {}
    stitched_rows: list[dict[str, Any]] = []
    stitched_scores: dict[str, float] = {}
    stitched_score_rows: list[dict[str, Any]] = []
    for fold in loaded_folds:
        fold_name = str(fold["fold_name"])
        train_rows = _join_factor_rows(
            fold["rows"]["train"], factor_by_id, f"{fold_name}.train"
        )
        evaluation_rows = _join_factor_rows(
            fold["rows"]["evaluation"], factor_by_id, f"{fold_name}.evaluation"
        )
        model, training, _ = _fit_scope(train_rows)
        evaluation_feature_map, feature_audit = build_cross_sectional_feature_map(
            evaluation_rows
        )
        score_output, scores = score_rows(
            evaluation_rows, evaluation_feature_map, model
        )
        for record in score_output:
            record["fold_name"] = fold_name
        bucket_output, metrics = evaluate_top4(evaluation_rows, scores)
        for record in bucket_output:
            record["fold_name"] = fold_name
        artifact_rows[f"{fold_name}/candidate_scores"] = score_output
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
            row["predicted_rank"],
            row["candidate_id"],
        )
    )
    stitched_bucket_rows, stitched_metrics = evaluate_top4(
        stitched_rows, stitched_scores
    )
    artifact_rows["stitched_oos/candidate_scores"] = stitched_score_rows
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
    }


def assemble_report(
    snapshot: dict[str, Any],
    factor_report_path: Path,
    fold_report_path: Path,
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
        "dataset_status": DATASET_STATUS,
        "research_question": (
            "Can the frozen 19-factor linear boundary ranker identify the realized "
            "Top-4 inside entry_day+signal_type buckets with at least five candidates?"
        ),
        "preregistered_design": {
            "eligible_folds": list(EXPECTED_ELIGIBLE_FOLDS),
            "bucket_key": ["entry_day", "signal_type"],
            "minimum_bucket_candidates": MIN_BUCKET_CANDIDATES,
            "top_k": TOP_K,
            "raw_factor_names": list(FACTOR_NAMES),
            "model_feature_names": list(MODEL_FEATURE_NAMES),
            "feature_transform": "within-bucket centered midrank; missing=0",
            "model": "no-intercept Top-4 boundary pairwise logistic regression",
            "l2": MODEL_L2,
            "max_iterations": MODEL_MAX_ITERATIONS,
            "tolerance": MODEL_TOLERANCE,
            "model_score_clip": [-MODEL_SCORE_CLIP, MODEL_SCORE_CLIP],
            "actual_top4_tie_break": "trade_pnl_pct desc then candidate_id asc",
            "equal_outcome_boundary_pairs_excluded": True,
            "each_bucket_total_training_weight": 1.0,
            "predicted_tie_break": "candidate_id asc",
            "hyperparameter_search": False,
            "portfolio_replay": False,
            "holdout_used": False,
        },
        "outcome_isolation": {
            "training_outcome": "trade_pnl_pct from same-fold train.jsonl only",
            "purged_training_labels_used": False,
            "evaluation_labels_used_for_fitting": False,
            "evaluation_labels_used_only_for_oos_metrics": True,
            "factor_snapshot_contains_outcomes": False,
        },
        "input": {
            "factor_report": _input_record(factor_report_path),
            "fold_report": _input_record(fold_report_path),
            "source_report": _input_record(snapshot["source_report_path"]),
            "factor_candidates": snapshot["factor_artifact"],
            "experiment_code": _input_record(Path(__file__).resolve()),
            "audit_code": _input_record(AUDIT_CODE),
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
        "integrity": snapshot["integrity"],
        "folds": fold_results,
        "stitched_oos": stitched,
        "screen": snapshot["screen"],
        "passes_research_screen": snapshot["screen"]["passes_research_screen"],
        "model_fitted": all(
            fold["training"]["model"]["model_fitted"] for fold in fold_results
        ),
        "hyperparameters_selected": False,
        "holdout_used": False,
        "portfolio_replayed": False,
        "production_eligible": False,
    }


def build_report(
    factor_report_path: Path, fold_report_path: Path, output_dir: Path
) -> dict[str, Any]:
    factor_report_path = _guard_development_path(factor_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    output_dir = _guard_development_path(output_dir)
    if output_dir.exists():
        raise Top4RankingExperimentError(
            f"output directory already exists; refusing overwrite: {output_dir}"
        )
    snapshot = build_snapshot(factor_report_path, fold_report_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = {
        name: _write_jsonl(output_dir / f"{name}.jsonl", rows)
        for name, rows in sorted(snapshot["artifact_rows"].items())
    }
    report = assemble_report(snapshot, factor_report_path, fold_report_path, artifacts)
    _write_json(output_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-report", type=Path, required=True)
    parser.add_argument("--fold-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(args.factor_report, args.fold_report, args.output_dir)
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
                "production_eligible": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
