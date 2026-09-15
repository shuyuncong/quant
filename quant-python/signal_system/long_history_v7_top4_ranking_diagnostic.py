"""Post-hoc tail-stability diagnostics for a frozen failed v7a Top-4 run.

This command is read-only with respect to its inputs.  It does not fit a model,
replay a portfolio, alter the v7a screen, select factors, or authorize v7b.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from long_history_v7_top4_ranking_audit import audit_report as audit_v7_report
from long_history_v7_top4_ranking_experiment import (
    EXPECTED_ELIGIBLE_FOLDS,
    MODEL_FEATURE_NAMES,
)

VERSION = "long_history_v7_top4_ranking_diagnostic.v1"
DIAGNOSTIC_STATUS = "post_hoc_exploratory"
TAIL_FRACTION = 0.20
ARTIFACT_NAMES = (
    "bucket_tail_diagnostics",
    "temporal_diagnostics",
    "signal_type_diagnostics",
    "mechanism_alignment",
    "leave_one_out_diagnostics",
    "factor_tail_exposure",
    "coefficient_stability",
    "scope_summary",
)
AUDIT_CODE = BASE_DIR / "long_history_v7_top4_ranking_diagnostic_audit.py"


class Top4RankingDiagnosticError(RuntimeError):
    """Raised when the frozen v7a input or diagnostic contract is violated."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Top4RankingDiagnosticError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require(
        not any("holdout" in part.lower() for part in resolved.parts),
        f"Holdout paths are blocked for v7a diagnostics: {resolved}",
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
        raise Top4RankingDiagnosticError(f"cannot read JSON: {path}: {exc}") from exc
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
        raise Top4RankingDiagnosticError(f"cannot read JSONL: {path}: {exc}") from exc
    return rows


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_json(row) + "\n")
    return {"path": str(path), "rows": len(rows), "sha256": _sha256_file(path)}


def _input_record(path: Path) -> dict[str, Any]:
    path = path.resolve()
    _require(path.exists() and path.is_file(), f"input does not exist: {path}")
    return {"path": str(path), "sha256": _sha256_file(path)}


def _resolve_artifact(record: Any, report_path: Path, label: str) -> Path:
    _require(isinstance(record, dict), f"missing artifact record: {label}")
    raw = record.get("path")
    _require(isinstance(raw, str) and bool(raw), f"missing path: {label}")
    path = Path(raw)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"artifact does not exist: {path}")
    _require(
        str(record.get("sha256", "")) == _sha256_file(path),
        f"artifact hash mismatch: {label}",
    )
    return path


def _finite(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise Top4RankingDiagnosticError(f"non-numeric {label}: {value}") from exc
    _require(math.isfinite(result), f"non-finite {label}: {value}")
    return result


def _rounded(value: float, digits: int = 10) -> float:
    result = round(float(value), digits)
    return 0.0 if result == 0.0 else result


def _distribution(values: list[float]) -> dict[str, Any]:
    clean = np.asarray(values, dtype=float)
    if clean.size == 0:
        return {"n": 0}
    _require(bool(np.isfinite(clean).all()), "distribution contains non-finite values")
    return {
        "n": int(clean.size),
        "mean": _rounded(float(clean.mean())),
        "std": _rounded(float(clean.std(ddof=0))),
        "min": _rounded(float(clean.min())),
        "p10": _rounded(float(np.quantile(clean, 0.10))),
        "p25": _rounded(float(np.quantile(clean, 0.25))),
        "median": _rounded(float(np.median(clean))),
        "p75": _rounded(float(np.quantile(clean, 0.75))),
        "p90": _rounded(float(np.quantile(clean, 0.90))),
        "max": _rounded(float(clean.max())),
        "positive_pct": _rounded(float((clean > 0.0).mean() * 100.0)),
    }


def _midranks(values: list[float]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    order = np.argsort(array, kind="mergesort")
    ranks = np.empty(len(array), dtype=float)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and array[order[end]] == array[order[start]]:
            end += 1
        ranks[order[start:end]] = (start + end - 1) / 2.0
        start = end
    return ranks


def _correlation(left: list[float], right: list[float], *, rank: bool) -> float | None:
    _require(len(left) == len(right), "correlation vectors differ in length")
    if len(left) < 2:
        return None
    x = _midranks(left) if rank else np.asarray(left, dtype=float)
    y = _midranks(right) if rank else np.asarray(right, dtype=float)
    if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return None
    return _rounded(float(np.corrcoef(x, y)[0, 1]))


def _bucket_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("entry_day", "")), str(row.get("signal_type", ""))


def _selection_tilt(score_rows: list[dict[str, Any]], label: str) -> dict[str, float]:
    top = [row for row in score_rows if row.get("predicted_top4") is True]
    rest = [row for row in score_rows if row.get("predicted_top4") is False]
    _require(len(top) == 4, f"predicted Top-4 count differs: {label}")
    _require(bool(rest), f"predicted Top-4 has no remainder: {label}")
    result: dict[str, float] = {}
    for feature in MODEL_FEATURE_NAMES:
        top_values: list[float] = []
        rest_values: list[float] = []
        for row in top:
            features = row.get("rank_features")
            _require(isinstance(features, dict), f"rank_features missing: {label}")
            _require(
                set(features) == set(MODEL_FEATURE_NAMES),
                f"feature schema differs: {label}",
            )
            top_values.append(_finite(features.get(feature), f"{label}.{feature}"))
        for row in rest:
            features = row.get("rank_features")
            _require(isinstance(features, dict), f"rank_features missing: {label}")
            _require(
                set(features) == set(MODEL_FEATURE_NAMES),
                f"feature schema differs: {label}",
            )
            rest_values.append(_finite(features.get(feature), f"{label}.{feature}"))
        result[feature] = _rounded(float(np.mean(top_values) - np.mean(rest_values)))
    return result


def _bind_scope(
    scope: str,
    bucket_rows: list[dict[str, Any]],
    score_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    bucket_index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in bucket_rows:
        key = _bucket_key(row)
        _require(all(key), f"bucket key missing: {scope}")
        _require(key not in bucket_index, f"duplicate bucket: {scope}:{key}")
        bucket_index[key] = row
    score_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    candidate_ids: set[str] = set()
    for row in score_rows:
        candidate_id = str(row.get("candidate_id", ""))
        _require(bool(candidate_id), f"candidate_id missing: {scope}")
        _require(
            candidate_id not in candidate_ids,
            f"duplicate candidate_id: {scope}:{candidate_id}",
        )
        candidate_ids.add(candidate_id)
        if row.get("eligible_bucket") is True:
            score_groups[_bucket_key(row)].append(row)
    _require(
        set(bucket_index) == set(score_groups), f"bucket/score key mismatch: {scope}"
    )

    bound: list[dict[str, Any]] = []
    for key in sorted(bucket_index):
        source = bucket_index[key]
        scores = score_groups[key]
        bucket_size = int(source.get("bucket_size", 0))
        _require(bucket_size >= 5, f"ineligible bucket emitted: {scope}:{key}")
        _require(len(scores) == bucket_size, f"bucket size mismatch: {scope}:{key}")
        advantage = _finite(source.get("top4_advantage_pp"), "top4_advantage_pp")
        accuracy = _finite(source.get("boundary_accuracy"), "boundary_accuracy")
        hit_rate = _finite(source.get("top4_hit_rate"), "top4_hit_rate")
        random_hit = _finite(
            source.get("random_expected_hit_rate"), "random_expected_hit_rate"
        )
        bound.append(
            {
                "scope": scope,
                "entry_day": key[0],
                "entry_month": key[0][:7],
                "signal_type": key[1],
                "bucket_size": bucket_size,
                "boundary_pairs": int(source.get("boundary_pairs", 0)),
                "boundary_accuracy": _rounded(accuracy),
                "top4_advantage_pp": _rounded(advantage),
                "top4_advantage_positive": advantage > 0.0,
                "top4_hit_rate": _rounded(hit_rate),
                "random_expected_hit_rate": _rounded(random_hit),
                "top4_hit_excess": _rounded(hit_rate - random_hit),
                "predicted_top4_mean_pnl_pct": _rounded(
                    _finite(
                        source.get("predicted_top4_mean_pnl_pct"),
                        "predicted_top4_mean_pnl_pct",
                    )
                ),
                "rest_mean_pnl_pct": _rounded(
                    _finite(source.get("rest_mean_pnl_pct"), "rest_mean_pnl_pct")
                ),
                "negative_advantage_magnitude_pp": _rounded(max(-advantage, 0.0)),
                "selection_tilt": _selection_tilt(scores, f"{scope}:{key}"),
            }
        )
    _require(bool(bound), f"scope has no eligible buckets: {scope}")
    ordered = sorted(
        range(len(bound)),
        key=lambda index: (
            bound[index]["top4_advantage_pp"],
            bound[index]["entry_day"],
            bound[index]["signal_type"],
        ),
    )
    tail_count = max(1, math.ceil(len(bound) * TAIL_FRACTION))
    worst = set(ordered[:tail_count])
    best = set(ordered[-tail_count:])
    ranks = {index: rank + 1 for rank, index in enumerate(ordered)}
    for index, row in enumerate(bound):
        row["advantage_rank_ascending"] = ranks[index]
        row["tail_count"] = tail_count
        row["bottom_20pct"] = index in worst
        row["top_20pct"] = index in best
    return bound


def _negative_concentration(values: list[float]) -> dict[str, Any]:
    losses = sorted((-value for value in values if value < 0.0), reverse=True)
    total = float(sum(losses))
    result: dict[str, Any] = {
        "negative_bucket_count": len(losses),
        "total_negative_magnitude_pp": _rounded(total),
    }
    for count in (1, 3, 5):
        share = 0.0 if total == 0.0 else sum(losses[:count]) / total * 100.0
        result[f"top_{count}_loss_share_pct"] = _rounded(share)
    return result


def _scope_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    advantages = [float(row["top4_advantage_pp"]) for row in rows]
    accuracies = [float(row["boundary_accuracy"]) for row in rows]
    hit_excesses = [float(row["top4_hit_excess"]) for row in rows]
    bottom = [float(row["top4_advantage_pp"]) for row in rows if row["bottom_20pct"]]
    top = [float(row["top4_advantage_pp"]) for row in rows if row["top_20pct"]]
    return {
        "eligible_buckets": len(rows),
        "top4_advantage": _distribution(advantages),
        "boundary_accuracy": _distribution(accuracies),
        "top4_hit_excess": _distribution(hit_excesses),
        "bottom_20pct_advantage": _distribution(bottom),
        "top_20pct_advantage": _distribution(top),
        "negative_concentration": _negative_concentration(advantages),
        "accuracy_advantage_pearson": _correlation(accuracies, advantages, rank=False),
        "accuracy_advantage_spearman": _correlation(accuracies, advantages, rank=True),
        "hit_excess_advantage_pearson": _correlation(
            hit_excesses, advantages, rank=False
        ),
        "hit_excess_advantage_spearman": _correlation(
            hit_excesses, advantages, rank=True
        ),
    }


def _group_rows(
    scope: str,
    rows: list[dict[str, Any]],
    field: str,
    output_field: str,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row[field])].append(row)
    result: list[dict[str, Any]] = []
    for value in sorted(groups):
        subset = groups[value]
        metrics = _scope_metrics(subset)
        result.append({"scope": scope, output_field: value, **metrics})
    return result


def _three_way(value: float, pivot: float) -> str:
    if value < pivot:
        return "below"
    if value > pivot:
        return "above"
    return "equal"


def _mechanism_rows(scope: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    definitions = (
        ("boundary_accuracy_vs_0.5", "boundary_accuracy", 0.5),
        ("top4_hit_excess_vs_0", "top4_hit_excess", 0.0),
    )
    result: list[dict[str, Any]] = []
    for mechanism, field, pivot in definitions:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[_three_way(float(row[field]), pivot)].append(row)
        for category in ("below", "equal", "above"):
            subset = groups.get(category, [])
            result.append(
                {
                    "scope": scope,
                    "mechanism": mechanism,
                    "category": category,
                    "pivot": pivot,
                    "bucket_count": len(subset),
                    "top4_advantage": _distribution(
                        [float(row["top4_advantage_pp"]) for row in subset]
                    ),
                }
            )
    return result


def _loo_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["top4_advantage_pp"]) for row in rows]
    return {
        "remaining_buckets": len(rows),
        "top4_mean_advantage_pp": _rounded(float(np.mean(values))),
        "top4_median_advantage_pp": _rounded(float(np.median(values))),
        "positive_advantage_bucket_pct": _rounded(
            float(np.mean(np.asarray(values) > 0.0) * 100.0)
        ),
        "boundary_accuracy": _rounded(
            float(np.mean([float(row["boundary_accuracy"]) for row in rows]))
        ),
        "top4_hit_excess": _rounded(
            float(np.mean([float(row["top4_hit_excess"]) for row in rows]))
        ),
    }


def _leave_one_out_rows(scope: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    full = _loo_metrics(rows)
    result: list[dict[str, Any]] = []
    for removed in sorted(rows, key=lambda row: (row["entry_day"], row["signal_type"])):
        remaining = [row for row in rows if row is not removed]
        if not remaining:
            continue
        metrics = _loo_metrics(remaining)
        result.append(
            {
                "scope": scope,
                "scenario": "leave_one_bucket_out",
                "removed_value": f"{removed['entry_day']}|{removed['signal_type']}",
                "removed_buckets": 1,
                "removed_top4_advantage_mean_pp": removed["top4_advantage_pp"],
                **metrics,
                "mean_advantage_change_pp": _rounded(
                    metrics["top4_mean_advantage_pp"] - full["top4_mean_advantage_pp"]
                ),
            }
        )
    months = sorted({str(row["entry_month"]) for row in rows})
    for month in months:
        removed_rows = [row for row in rows if row["entry_month"] == month]
        remaining = [row for row in rows if row["entry_month"] != month]
        if not remaining:
            continue
        metrics = _loo_metrics(remaining)
        result.append(
            {
                "scope": scope,
                "scenario": "leave_one_month_out",
                "removed_value": month,
                "removed_buckets": len(removed_rows),
                "removed_top4_advantage_mean_pp": _rounded(
                    float(np.mean([row["top4_advantage_pp"] for row in removed_rows]))
                ),
                **metrics,
                "mean_advantage_change_pp": _rounded(
                    metrics["top4_mean_advantage_pp"] - full["top4_mean_advantage_pp"]
                ),
            }
        )
    return result


def _factor_exposure_rows(
    scope: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for feature in MODEL_FEATURE_NAMES:
        bottom = [row["selection_tilt"][feature] for row in rows if row["bottom_20pct"]]
        non_bottom = [
            row["selection_tilt"][feature] for row in rows if not row["bottom_20pct"]
        ]
        top = [row["selection_tilt"][feature] for row in rows if row["top_20pct"]]
        non_top = [
            row["selection_tilt"][feature] for row in rows if not row["top_20pct"]
        ]
        bottom_mean = float(np.mean(bottom))
        non_bottom_mean = float(np.mean(non_bottom))
        top_mean = float(np.mean(top))
        non_top_mean = float(np.mean(non_top))
        result.append(
            {
                "scope": scope,
                "feature": feature,
                "bottom_20pct_buckets": len(bottom),
                "bottom_20pct_selection_tilt_mean": _rounded(bottom_mean),
                "non_bottom_selection_tilt_mean": _rounded(non_bottom_mean),
                "bottom_minus_non_bottom_tilt": _rounded(bottom_mean - non_bottom_mean),
                "top_20pct_buckets": len(top),
                "top_20pct_selection_tilt_mean": _rounded(top_mean),
                "non_top_selection_tilt_mean": _rounded(non_top_mean),
                "top_minus_non_top_tilt": _rounded(top_mean - non_top_mean),
            }
        )
    return result


def _coefficient_diagnostics(
    v7_report: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    folds = v7_report.get("folds")
    _require(isinstance(folds, list), "v7a fold list missing")
    vectors: dict[str, np.ndarray] = {}
    rows: list[dict[str, Any]] = []
    for fold in folds:
        fold_name = str(fold.get("fold_name", ""))
        coefficients = fold.get("training", {}).get("model", {}).get("coefficients")
        _require(isinstance(coefficients, dict), f"coefficients missing: {fold_name}")
        _require(
            set(coefficients) == set(MODEL_FEATURE_NAMES),
            f"coefficient schema differs: {fold_name}",
        )
        vectors[fold_name] = np.asarray(
            [
                _finite(coefficients[name], f"{fold_name}.{name}")
                for name in MODEL_FEATURE_NAMES
            ],
            dtype=float,
        )
    _require(
        list(vectors) == list(EXPECTED_ELIGIBLE_FOLDS), "coefficient fold order differs"
    )
    for feature_index, feature in enumerate(MODEL_FEATURE_NAMES):
        values = [
            float(vectors[name][feature_index]) for name in EXPECTED_ELIGIBLE_FOLDS
        ]
        positive = sum(value > 0.0 for value in values)
        negative = sum(value < 0.0 for value in values)
        zero = len(values) - positive - negative
        rows.append(
            {
                "record_type": "feature",
                "feature": feature,
                "fold_coefficients": {
                    name: _rounded(float(vectors[name][feature_index]))
                    for name in EXPECTED_ELIGIBLE_FOLDS
                },
                "positive_folds": positive,
                "negative_folds": negative,
                "zero_folds": zero,
                "same_nonzero_sign_all_folds": positive == 6 or negative == 6,
                "distribution": _distribution(values),
            }
        )
    similarities: list[float] = []
    for left, right in combinations(EXPECTED_ELIGIBLE_FOLDS, 2):
        left_vector = vectors[left]
        right_vector = vectors[right]
        denominator = float(np.linalg.norm(left_vector) * np.linalg.norm(right_vector))
        similarity = (
            0.0
            if denominator == 0.0
            else float(np.dot(left_vector, right_vector) / denominator)
        )
        similarities.append(similarity)
        rows.append(
            {
                "record_type": "fold_cosine",
                "left_fold": left,
                "right_fold": right,
                "cosine_similarity": _rounded(similarity),
            }
        )
    summary = {
        "feature_count": len(MODEL_FEATURE_NAMES),
        "fold_pair_count": len(similarities),
        "same_nonzero_sign_feature_count": sum(
            row.get("same_nonzero_sign_all_folds") is True for row in rows
        ),
        "fold_cosine_similarity": _distribution(similarities),
    }
    return rows, summary


def _scope_data(
    v7_report_path: Path, v7_report: dict[str, Any]
) -> list[dict[str, Any]]:
    folds = v7_report.get("folds")
    _require(isinstance(folds, list), "v7a fold list missing")
    _require(
        [fold.get("fold_name") for fold in folds] == list(EXPECTED_ELIGIBLE_FOLDS),
        "v7a fold order differs",
    )
    scopes: list[dict[str, Any]] = []
    for scope_name, scope_value in [
        *((str(fold["fold_name"]), fold) for fold in folds),
        ("stitched_oos", v7_report.get("stitched_oos")),
    ]:
        _require(isinstance(scope_value, dict), f"scope missing: {scope_name}")
        artifacts = scope_value.get("artifacts")
        _require(isinstance(artifacts, dict), f"artifacts missing: {scope_name}")
        bucket_rows = _load_jsonl(
            _resolve_artifact(
                artifacts.get("bucket_metrics"),
                v7_report_path,
                f"{scope_name}.bucket_metrics",
            )
        )
        score_rows = _load_jsonl(
            _resolve_artifact(
                artifacts.get("candidate_scores"),
                v7_report_path,
                f"{scope_name}.candidate_scores",
            )
        )
        scopes.append(
            {
                "scope": scope_name,
                "bucket_rows": _bind_scope(scope_name, bucket_rows, score_rows),
            }
        )
    return scopes


def _v7_audit_summary(v7_audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_sha256": v7_audit["report_sha256"],
        "artifact_count": len(v7_audit["artifact_sha256"]),
        "passes_research_screen": v7_audit["passes_research_screen"],
        "model_fitted": v7_audit["model_fitted"],
        "checks_passed": v7_audit["checks_passed"],
    }


def compute_diagnostic(
    v7_report_path: Path,
    *,
    v7_audit: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    v7_report_path = _guard_development_path(v7_report_path)
    _require(v7_report_path.exists(), f"v7a report does not exist: {v7_report_path}")
    if v7_audit is None:
        v7_audit = audit_v7_report(v7_report_path)
    _require(v7_audit.get("checks_passed") is True, "v7a report audit did not pass")
    _require(
        v7_audit.get("passes_research_screen") is False,
        "tail diagnostic requires the frozen failed v7a screen",
    )
    v7_report = _load_json(v7_report_path)
    screen = v7_report.get("screen")
    _require(isinstance(screen, dict), "v7a screen missing")
    checks = screen.get("checks")
    _require(isinstance(checks, dict), "v7a screen checks missing")
    failed_checks = sorted(name for name, value in checks.items() if value is not True)
    _require(
        failed_checks == ["at_least_four_folds_top4_mean_advantage_above_zero"],
        f"unexpected v7a failure set: {failed_checks}",
    )
    _require(
        screen.get("folds_top4_mean_advantage_above_zero") == 3,
        "unexpected positive-advantage fold count",
    )

    scopes = _scope_data(v7_report_path, v7_report)
    coefficient_rows, coefficient_summary = _coefficient_diagnostics(v7_report)
    artifact_rows: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ARTIFACT_NAMES
    }
    scope_aggregates: dict[str, Any] = {}
    for scope in scopes:
        scope_name = str(scope["scope"])
        rows = scope["bucket_rows"]
        summary = {"scope": scope_name, **_scope_metrics(rows)}
        leaveouts = _leave_one_out_rows(scope_name, rows)
        bucket_loo = [
            row for row in leaveouts if row["scenario"] == "leave_one_bucket_out"
        ]
        month_loo = [
            row for row in leaveouts if row["scenario"] == "leave_one_month_out"
        ]
        summary["leave_one_out_robustness"] = {
            "bucket_runs": len(bucket_loo),
            "bucket_positive_mean_pct": _rounded(
                sum(row["top4_mean_advantage_pp"] > 0.0 for row in bucket_loo)
                / len(bucket_loo)
                * 100.0
            ),
            "month_runs": len(month_loo),
            "month_positive_mean_pct": _rounded(
                sum(row["top4_mean_advantage_pp"] > 0.0 for row in month_loo)
                / len(month_loo)
                * 100.0
            )
            if month_loo
            else None,
        }
        artifact_rows["bucket_tail_diagnostics"].extend(rows)
        artifact_rows["temporal_diagnostics"].extend(
            _group_rows(scope_name, rows, "entry_month", "entry_month")
        )
        artifact_rows["signal_type_diagnostics"].extend(
            _group_rows(scope_name, rows, "signal_type", "signal_type")
        )
        artifact_rows["mechanism_alignment"].extend(_mechanism_rows(scope_name, rows))
        artifact_rows["leave_one_out_diagnostics"].extend(leaveouts)
        artifact_rows["factor_tail_exposure"].extend(
            _factor_exposure_rows(scope_name, rows)
        )
        artifact_rows["scope_summary"].append(summary)
        scope_aggregates[scope_name] = summary
    artifact_rows["coefficient_stability"] = coefficient_rows

    core = {
        "version": VERSION,
        "diagnostic_status": DIAGNOSTIC_STATUS,
        "purpose": (
            "Explain the frozen v7a Top-4 mean-advantage instability and loss tail; "
            "do not rescue v7a, select factors, or authorize v7b."
        ),
        "fixed_design": {
            "scopes": [*EXPECTED_ELIGIBLE_FOLDS, "stitched_oos"],
            "bucket": "entry_day + signal_type",
            "bucket_weighting": "equal",
            "tail_fraction": TAIL_FRACTION,
            "tail_count": "ceil(scope eligible buckets * 0.20)",
            "tail_order": "top4_advantage_pp asc, entry_day asc, signal_type asc",
            "temporal_period": "entry_day calendar month",
            "leave_one_out": ["every eligible bucket", "every entry month"],
            "leave_one_out_refits_model": False,
            "mechanism_bins": {
                "boundary_accuracy": ["below 0.5", "equal 0.5", "above 0.5"],
                "top4_hit_excess": ["below 0", "equal 0", "above 0"],
            },
            "factor_exposure": (
                "predicted Top-4 mean rank feature minus remaining-candidate mean "
                "rank feature, compared across fixed advantage tails"
            ),
            "research_screen_defined": False,
        },
        "input": {
            "v7_report": _input_record(v7_report_path),
            "diagnostic_code": _input_record(Path(__file__).resolve()),
            "audit_code": _input_record(AUDIT_CODE),
            "v7_audit": _v7_audit_summary(v7_audit),
        },
        "v7a_failure": {
            "passes_research_screen": False,
            "failed_checks": failed_checks,
            "folds_top4_mean_advantage_above_zero": 3,
            "changes_v7a_screen": False,
        },
        "scope_aggregates": scope_aggregates,
        "coefficient_stability": coefficient_summary,
        "interpretation_policy": {
            "post_hoc_exploratory": True,
            "changes_v7a_screen": False,
            "selects_new_factors": False,
            "authorizes_v7b": False,
            "tail_exposure_is_causal_evidence": False,
        },
        "input_model_replayed": True,
        "model_fitted": False,
        "outcomes_read": True,
        "hyperparameters_selected": False,
        "portfolio_replayed": False,
        "holdout_used": False,
        "production_eligible": False,
    }
    return core, artifact_rows


def build_report(v7_report_path: Path, output_dir: Path) -> dict[str, Any]:
    v7_report_path = _guard_development_path(v7_report_path)
    output_dir = _guard_development_path(output_dir)
    _require(
        not output_dir.exists(),
        f"output directory already exists; refusing overwrite: {output_dir}",
    )
    core, artifact_rows = compute_diagnostic(v7_report_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = {
        name: _write_jsonl(output_dir / f"{name}.jsonl", artifact_rows[name])
        for name in ARTIFACT_NAMES
    }
    report = {**core, "artifacts": artifacts}
    _write_json(output_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v7-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(args.v7_report, args.output_dir)
    except Exception as exc:  # noqa: BLE001 - CLI fails closed as JSON.
        print(
            json.dumps(
                {"version": VERSION, "diagnostic_completed": False, "error": str(exc)},
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
                "diagnostic_status": report["diagnostic_status"],
                "changes_v7a_screen": False,
                "authorizes_v7b": False,
                "production_eligible": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
