"""Post-hoc, read-only mechanism diagnostics for a frozen v6 pairwise run.

The diagnostic does not fit a model, change the v6 screen, select new factors,
or authorize Holdout/production use.  It only explains frozen OOS decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from itertools import combinations, pairwise
from pathlib import Path
from typing import Any

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from long_history_pairwise_ranking_audit import audit_report as audit_v6_report
from long_history_pairwise_ranking_experiment import (
    EXPECTED_ELIGIBLE_FOLDS,
    MODEL_FEATURE_NAMES,
    RANDOM_SEED_COUNT,
    RANDOM_SEED_START,
    RANDOM_SEEDS,
    _evaluate_portfolios,
    _pairwise_metrics,
)

VERSION = "long_history_pairwise_ranking_diagnostic.v1"
DIAGNOSTIC_STATUS = "post_hoc_exploratory"
ARTIFACT_NAMES = (
    "bucket_diagnostics",
    "capacity_diagnostics",
    "score_quintiles",
    "signal_type_diagnostics",
    "coefficient_stability",
    "scope_summary",
    "leave_one_out_random_runs",
)


class PairwiseRankingDiagnosticError(RuntimeError):
    """Raised when a frozen v6 input or diagnostic contract is violated."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PairwiseRankingDiagnosticError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require(
        not any("holdout" in part.lower() for part in resolved.parts),
        f"Holdout paths are blocked for v6 diagnostics: {resolved}",
    )
    return resolved


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            _require(
                isinstance(value, dict),
                f"JSONL object required at {path}:{line_number}",
            )
            rows.append(value)
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


def _resolve_artifact(record: Any, report_path: Path, label: str) -> Path:
    _require(isinstance(record, dict), f"missing artifact record: {label}")
    raw_path = record.get("path")
    _require(isinstance(raw_path, str) and bool(raw_path), f"missing path: {label}")
    path = Path(raw_path)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    _require(path.exists(), f"artifact does not exist: {path}")
    _require(
        str(record.get("sha256", "")).lower() == _sha256_file(path),
        f"artifact hash mismatch: {label}",
    )
    return path


def _finite(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PairwiseRankingDiagnosticError(f"non-numeric {label}: {value}") from exc
    _require(math.isfinite(result), f"non-finite {label}: {value}")
    return result


def _outcome(row: dict[str, Any]) -> float:
    value = row.get("trade_pnl_pct", row.get("pnl_pct"))
    return _finite(value, f"outcome for {row.get('candidate_id')}")


def _fixed_hash(row: dict[str, Any]) -> str:
    value = f"{row['symbol']}|{row['signal_day']}|fixed-seed".encode()
    return hashlib.sha256(value).hexdigest()


def _score_order_key(row: dict[str, Any]) -> tuple[Any, ...]:
    score = _finite(row.get("portfolio_rank_score"), "portfolio_rank_score")
    return (-round(score, 6), _fixed_hash(row), str(row["candidate_id"]))


def _distribution(values: list[float]) -> dict[str, Any]:
    clean = np.asarray(
        [float(value) for value in values if math.isfinite(float(value))], dtype=float
    )
    if clean.size == 0:
        return {"n": 0}
    return {
        "n": int(clean.size),
        "mean": round(float(clean.mean()), 6),
        "std": round(float(clean.std(ddof=0)), 6),
        "min": round(float(clean.min()), 6),
        "p10": round(float(np.quantile(clean, 0.10)), 6),
        "p25": round(float(np.quantile(clean, 0.25)), 6),
        "median": round(float(np.median(clean)), 6),
        "p75": round(float(np.quantile(clean, 0.75)), 6),
        "p90": round(float(np.quantile(clean, 0.90)), 6),
        "max": round(float(clean.max()), 6),
        "positive_pct": round(float((clean > 0).mean() * 100.0), 2),
        "above_half_pct": round(float((clean > 0.5).mean() * 100.0), 2),
    }


def _index_unique(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        candidate_id = str(row.get("candidate_id", ""))
        _require(bool(candidate_id), f"missing candidate_id: {label}")
        _require(candidate_id not in indexed, f"duplicate candidate_id: {label}:{candidate_id}")
        indexed[candidate_id] = row
    return indexed


def _attach_scores(
    candidates: list[dict[str, Any]], score_rows: list[dict[str, Any]], label: str
) -> list[dict[str, Any]]:
    candidate_index = _index_unique(candidates, f"{label}.candidates")
    score_index = _index_unique(score_rows, f"{label}.scores")
    _require(
        set(candidate_index) == set(score_index),
        f"candidate/score ID mismatch: {label}",
    )
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        score = score_index[candidate_id]
        for field in ("symbol", "signal_day", "entry_day", "signal_type"):
            _require(
                str(score.get(field)) == str(candidate.get(field)),
                f"candidate/score field mismatch: {label}:{candidate_id}:{field}",
            )
        copy = dict(candidate)
        copy["pairwise_model_score"] = _finite(
            score.get("pairwise_model_score"), "pairwise_model_score"
        )
        copy["portfolio_rank_score"] = _finite(
            score.get("portfolio_rank_score"), "portfolio_rank_score"
        )
        copy["_pairwise_model_score"] = copy["pairwise_model_score"]
        copy["_portfolio_rank_score"] = copy["portfolio_rank_score"]
        result.append(copy)
    return result


def _bucket_rows(scope: str, scored_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in scored_rows:
        groups[(str(row["entry_day"]), str(row["signal_type"]))].append(row)
    results: list[dict[str, Any]] = []
    for (entry_day, signal_type), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=_score_order_key)
        outcomes = [_outcome(row) for row in ordered]
        top1 = outcomes[0]
        result: dict[str, Any] = {
            "scope": scope,
            "entry_day": entry_day,
            "signal_type": signal_type,
            "candidate_count": len(group),
            "top1_candidate_id": str(ordered[0]["candidate_id"]),
            "top1_outcome_pct": round(top1, 6),
            "bucket_mean_outcome_pct": round(float(np.mean(outcomes)), 6),
            "top1_advantage_vs_bucket_mean_pp": round(
                top1 - float(np.mean(outcomes)), 6
            ),
            "top1_outcome_percentile": round(
                sum(value <= top1 for value in outcomes) / len(outcomes) * 100.0, 2
            ),
            "top1_is_best": top1 >= max(outcomes) - 1e-12,
            "top4_eligible": len(group) >= 5,
        }
        if len(group) >= 5:
            selected = outcomes[:4]
            rest = outcomes[4:]
            comparisons_count = len(selected) * len(rest)
            correct = sum(
                1.0 if left > right else 0.5 if abs(left - right) <= 1e-12 else 0.0
                for left in selected
                for right in rest
            )
            result.update(
                {
                    "top4_candidate_ids": [
                        str(row["candidate_id"]) for row in ordered[:4]
                    ],
                    "top4_mean_outcome_pct": round(float(np.mean(selected)), 6),
                    "rest_mean_outcome_pct": round(float(np.mean(rest)), 6),
                    "top4_mean_advantage_pp": round(
                        float(np.mean(selected)) - float(np.mean(rest)), 6
                    ),
                    "top4_median_advantage_pp": round(
                        float(np.median(selected)) - float(np.median(rest)), 6
                    ),
                    "top4_boundary_pairs": comparisons_count,
                    "top4_boundary_pairwise_accuracy": round(
                        correct / comparisons_count, 6
                    ),
                }
            )
        results.append(result)
    return results


def _bucket_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    top4 = [row for row in rows if row["top4_eligible"]]
    return {
        "pairwise_buckets": len(rows),
        "top1_best_hit_pct": round(
            sum(bool(row["top1_is_best"]) for row in rows) / max(len(rows), 1) * 100.0,
            2,
        ),
        "top1_outcome_percentile": _distribution(
            [float(row["top1_outcome_percentile"]) for row in rows]
        ),
        "top1_advantage_vs_bucket_mean_pp": _distribution(
            [float(row["top1_advantage_vs_bucket_mean_pp"]) for row in rows]
        ),
        "top4_eligible_buckets": len(top4),
        "top4_mean_advantage_pp": _distribution(
            [float(row["top4_mean_advantage_pp"]) for row in top4]
        ),
        "top4_boundary_pairwise_accuracy": _distribution(
            [float(row["top4_boundary_pairwise_accuracy"]) for row in top4]
        ),
    }


def _midrank_score(value: float, values: list[float]) -> float:
    if len(values) < 2:
        return 0.5
    below = sum(other < value for other in values)
    equal = sum(other == value for other in values)
    return (below + (equal - 1) * 0.5) / (len(values) - 1)


def _quintile_assignments(
    scope: str, scored_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in scored_rows:
        groups[(str(row["entry_day"]), str(row["signal_type"]))].append(row)
    assignments: list[dict[str, Any]] = []
    for (entry_day, signal_type), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        scores = [_finite(row["pairwise_model_score"], "model score") for row in group]
        for row, score in zip(group, scores, strict=True):
            percentile = _midrank_score(score, scores)
            quintile = min(5, math.floor(percentile * 5.0) + 1)
            assignments.append(
                {
                    "scope": scope,
                    "entry_day": entry_day,
                    "signal_type": signal_type,
                    "candidate_id": str(row["candidate_id"]),
                    "quintile": quintile,
                    "score_percentile": round(percentile * 100.0, 2),
                    "outcome_pct": round(_outcome(row), 6),
                }
            )
    return assignments


def _quintile_rows(
    scope: str, assignments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for quintile in range(1, 6):
        rows = [row for row in assignments if int(row["quintile"]) == quintile]
        bucket_values: dict[tuple[str, str], list[float]] = defaultdict(list)
        for row in rows:
            bucket_values[(str(row["entry_day"]), str(row["signal_type"]))].append(
                float(row["outcome_pct"])
            )
        outcomes = [float(row["outcome_pct"]) for row in rows]
        results.append(
            {
                "scope": scope,
                "quintile": quintile,
                "candidates": len(rows),
                "buckets": len(bucket_values),
                "mean_outcome_pct": (
                    round(float(np.mean(outcomes)), 6) if outcomes else None
                ),
                "median_outcome_pct": (
                    round(float(np.median(outcomes)), 6) if outcomes else None
                ),
                "win_rate_pct": (
                    round(sum(value > 0 for value in outcomes) / len(outcomes) * 100.0, 2)
                    if outcomes
                    else None
                ),
                "bucket_equal_mean_outcome_pct": (
                    round(
                        float(
                            np.mean(
                                [float(np.mean(values)) for values in bucket_values.values()]
                            )
                        ),
                        6,
                    )
                    if bucket_values
                    else None
                ),
            }
        )
    return results


def _quintile_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    populated = [row for row in rows if row["bucket_equal_mean_outcome_pct"] is not None]
    values = [float(row["bucket_equal_mean_outcome_pct"]) for row in populated]
    monotonic = len(values) >= 2 and all(
        right >= left for left, right in pairwise(values)
    )
    bottom = next((row for row in rows if row["quintile"] == 1), None)
    top = next((row for row in rows if row["quintile"] == 5), None)
    top_bottom = None
    if (
        bottom
        and top
        and bottom["bucket_equal_mean_outcome_pct"] is not None
        and top["bucket_equal_mean_outcome_pct"] is not None
    ):
        top_bottom = round(
            float(top["bucket_equal_mean_outcome_pct"])
            - float(bottom["bucket_equal_mean_outcome_pct"]),
            6,
        )
    return {
        "populated_quintiles": len(populated),
        "bucket_equal_mean_monotonic_non_decreasing": monotonic,
        "q5_minus_q1_bucket_equal_mean_pp": top_bottom,
    }


def _candidate_key(row: dict[str, Any], signal_type: str) -> tuple[str, str, str]:
    return (str(row["symbol"]), str(row["signal_day"]), signal_type)


def _capacity_rows(
    scope: str,
    scored_rows: list[dict[str, Any]],
    accepted_rows: list[dict[str, Any]],
    rejection_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = _index_unique(scored_rows, f"{scope}.scored")
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in scored_rows:
        key = _candidate_key(row, str(row["signal_type"]))
        _require(key not in by_key, f"ambiguous candidate key: {scope}:{key}")
        by_key[key] = row
    accepted_by_bucket: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for accepted in accepted_rows:
        candidate_id = str(accepted.get("candidate_id", ""))
        _require(candidate_id in by_id, f"accepted candidate not in evaluation: {scope}:{candidate_id}")
        row = by_id[candidate_id]
        accepted_by_bucket[(str(row["entry_day"]), str(row["signal_type"]))].append(row)
    rejected_by_bucket: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen_rejected: set[str] = set()
    for rejection in rejection_rows:
        if rejection.get("reason") != "max_positions":
            continue
        signal_types = rejection.get("signal_types")
        _require(
            isinstance(signal_types, list) and bool(signal_types),
            f"max_positions rejection lacks signal_types: {scope}",
        )
        signal_type = str(signal_types[0])
        key = _candidate_key(rejection, signal_type)
        _require(key in by_key, f"rejected candidate not in evaluation: {scope}:{key}")
        row = by_key[key]
        candidate_id = str(row["candidate_id"])
        _require(candidate_id not in seen_rejected, f"duplicate rejection: {scope}:{candidate_id}")
        seen_rejected.add(candidate_id)
        rejected_by_bucket[(str(row["entry_day"]), signal_type)].append(row)
    results: list[dict[str, Any]] = []
    for bucket in sorted(set(accepted_by_bucket) & set(rejected_by_bucket)):
        accepted = accepted_by_bucket[bucket]
        rejected = rejected_by_bucket[bucket]
        accepted_outcomes = [_outcome(row) for row in accepted]
        rejected_outcomes = [_outcome(row) for row in rejected]
        pair_count = len(accepted) * len(rejected)
        correct = sum(
            1.0 if left > right else 0.5 if abs(left - right) <= 1e-12 else 0.0
            for left in accepted_outcomes
            for right in rejected_outcomes
        )
        results.append(
            {
                "scope": scope,
                "entry_day": bucket[0],
                "signal_type": bucket[1],
                "accepted_candidates": len(accepted),
                "max_positions_rejected_candidates": len(rejected),
                "accepted_candidate_ids": sorted(str(row["candidate_id"]) for row in accepted),
                "rejected_candidate_ids": sorted(str(row["candidate_id"]) for row in rejected),
                "accepted_mean_outcome_pct": round(float(np.mean(accepted_outcomes)), 6),
                "rejected_mean_outcome_pct": round(float(np.mean(rejected_outcomes)), 6),
                "accepted_advantage_pp": round(
                    float(np.mean(accepted_outcomes)) - float(np.mean(rejected_outcomes)),
                    6,
                ),
                "accepted_vs_rejected_pairs": pair_count,
                "accepted_vs_rejected_pairwise_accuracy": round(correct / pair_count, 6),
            }
        )
    return results


def _capacity_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_pairs = sum(int(row["accepted_vs_rejected_pairs"]) for row in rows)
    weighted_correct = sum(
        float(row["accepted_vs_rejected_pairwise_accuracy"])
        * int(row["accepted_vs_rejected_pairs"])
        for row in rows
    )
    return {
        "comparable_buckets": len(rows),
        "accepted_candidates": sum(int(row["accepted_candidates"]) for row in rows),
        "max_positions_rejected_candidates": sum(
            int(row["max_positions_rejected_candidates"]) for row in rows
        ),
        "accepted_vs_rejected_pairs": total_pairs,
        "pair_weighted_accuracy": round(
            weighted_correct / total_pairs if total_pairs else 0.0, 6
        ),
        "bucket_equal_accuracy": _distribution(
            [float(row["accepted_vs_rejected_pairwise_accuracy"]) for row in rows]
        ),
        "accepted_advantage_pp": _distribution(
            [float(row["accepted_advantage_pp"]) for row in rows]
        ),
    }


def _signal_type_rows(
    scope: str,
    scored_rows: list[dict[str, Any]],
    bucket_rows: list[dict[str, Any]],
    capacity_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    score_map = {
        str(row["candidate_id"]): float(row["pairwise_model_score"])
        for row in scored_rows
    }
    for signal_type in sorted({str(row["signal_type"]) for row in scored_rows}):
        candidates = [row for row in scored_rows if str(row["signal_type"]) == signal_type]
        outcomes = [_outcome(row) for row in candidates]
        signal_buckets = [row for row in bucket_rows if row["signal_type"] == signal_type]
        signal_capacity = [row for row in capacity_rows if row["signal_type"] == signal_type]
        results.append(
            {
                "scope": scope,
                "signal_type": signal_type,
                "candidates": len(candidates),
                "entry_days": len({str(row["entry_day"]) for row in candidates}),
                "mean_outcome_pct": round(float(np.mean(outcomes)), 6),
                "median_outcome_pct": round(float(np.median(outcomes)), 6),
                "win_rate_pct": round(
                    sum(value > 0 for value in outcomes) / len(outcomes) * 100.0, 2
                ),
                "pairwise_metrics": _pairwise_metrics(candidates, score_map),
                "bucket_diagnostics": _bucket_aggregate(signal_buckets),
                "capacity_diagnostics": _capacity_aggregate(signal_capacity),
            }
        )
    return results


def _coefficient_diagnostics(v6_report: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    folds = v6_report.get("folds")
    _require(isinstance(folds, list) and len(folds) == 6, "v6 fold results missing")
    fold_names: list[str] = []
    vectors: list[np.ndarray] = []
    for fold in folds:
        fold_name = str(fold.get("fold_name", ""))
        coefficients = fold.get("training", {}).get("model", {}).get("coefficients")
        _require(isinstance(coefficients, dict), f"coefficients missing: {fold_name}")
        _require(
            set(coefficients) == set(MODEL_FEATURE_NAMES),
            f"coefficient schema mismatch: {fold_name}",
        )
        fold_names.append(fold_name)
        vectors.append(
            np.asarray(
                [_finite(coefficients[name], f"{fold_name}.{name}") for name in MODEL_FEATURE_NAMES],
                dtype=float,
            )
        )
    _require(fold_names == list(EXPECTED_ELIGIBLE_FOLDS), "coefficient fold order drift")
    rows: list[dict[str, Any]] = []
    for feature_index, feature_name in enumerate(MODEL_FEATURE_NAMES):
        values = [float(vector[feature_index]) for vector in vectors]
        positive = sum(value > 1e-12 for value in values)
        negative = sum(value < -1e-12 for value in values)
        zero = len(values) - positive - negative
        rows.append(
            {
                "row_type": "feature",
                "feature_name": feature_name,
                "coefficients_by_fold": {
                    name: round(value, 8) for name, value in zip(fold_names, values, strict=True)
                },
                "positive_folds": positive,
                "negative_folds": negative,
                "zero_folds": zero,
                "majority_sign_fraction": round(max(positive, negative, zero) / len(values), 6),
                "all_nonzero_same_sign": zero == 0 and (positive == len(values) or negative == len(values)),
                "mean_coefficient": round(float(np.mean(values)), 8),
                "std_coefficient": round(float(np.std(values, ddof=0)), 8),
            }
        )
    cosine_values: list[float] = []
    for (left_name, left), (right_name, right) in combinations(
        zip(fold_names, vectors, strict=True), 2
    ):
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        cosine = float(left @ right / denominator) if denominator > 0 else 0.0
        cosine_values.append(cosine)
        rows.append(
            {
                "row_type": "fold_pair",
                "left_fold": left_name,
                "right_fold": right_name,
                "cosine_similarity": round(cosine, 8),
            }
        )
    return rows, {
        "folds": fold_names,
        "features": len(MODEL_FEATURE_NAMES),
        "all_nonzero_same_sign_features": sum(
            bool(row.get("all_nonzero_same_sign"))
            for row in rows
            if row["row_type"] == "feature"
        ),
        "pairwise_cosine_similarity": _distribution(cosine_values),
    }


def _concentration(trades: list[dict[str, Any]]) -> dict[str, Any]:
    by_day: dict[str, float] = defaultdict(float)
    by_month: dict[str, float] = defaultdict(float)
    for trade in trades:
        entry_day = str(trade.get("entry_day", ""))
        _require(len(entry_day) >= 7, f"trade lacks entry_day: {trade}")
        pnl = _finite(trade.get("pnl_cash"), "trade pnl_cash")
        by_day[entry_day] += pnl
        by_month[entry_day[:7]] += pnl

    def summary(values: dict[str, float]) -> dict[str, Any]:
        ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
        positive_total = sum(max(value, 0.0) for _, value in ordered)

        def share(count: int) -> float | None:
            if positive_total <= 0:
                return None
            return round(
                sum(max(value, 0.0) for _, value in ordered[:count])
                / positive_total
                * 100.0,
                2,
            )

        return {
            "periods": len(ordered),
            "net_pnl_cash": round(sum(values.values()), 2),
            "positive_period_pnl_cash": round(positive_total, 2),
            "top_period": ordered[0][0] if ordered else None,
            "top_period_net_pnl_cash": round(ordered[0][1], 2) if ordered else None,
            "top_1_positive_pnl_share_pct": share(1),
            "top_5_positive_pnl_share_pct": share(5),
            "top_10_positive_pnl_share_pct": share(10),
        }

    return {"entry_day": summary(by_day), "entry_month": summary(by_month)}


def _leave_one_out(
    stitched_rows: list[dict[str, Any]],
    costs: dict[str, Any],
    concentration: dict[str, Any],
    full_ranked_return_pct: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scenarios = (
        ("top_profit_entry_day", "entry_day", concentration["entry_day"]["top_period"]),
        (
            "top_profit_entry_month",
            "entry_month",
            concentration["entry_month"]["top_period"],
        ),
    )
    summaries: list[dict[str, Any]] = []
    random_rows: list[dict[str, Any]] = []
    for scenario, period_type, period in scenarios:
        _require(isinstance(period, str) and bool(period), f"no period for {scenario}")
        if period_type == "entry_day":
            keep = [row for row in stitched_rows if str(row["entry_day"]) != period]
        else:
            keep = [row for row in stitched_rows if str(row["entry_day"])[:7] != period]
        _require(bool(keep) and len(keep) < len(stitched_rows), f"invalid leave-one-out: {scenario}")
        comparison, ranked, seed_runs = _evaluate_portfolios(keep, keep, costs)
        leaveout_return = float(
            comparison["pairwise_ranked"]["summary"]["total_return_pct"]
        )
        summaries.append(
            {
                "scenario": scenario,
                "removed_period_type": period_type,
                "removed_period": period,
                "input_candidates": len(stitched_rows),
                "remaining_candidates": len(keep),
                "removed_candidates": len(stitched_rows) - len(keep),
                "ranked_trades": int(ranked["summary"]["count"]),
                "full_ranked_return_pct": full_ranked_return_pct,
                "leave_one_out_ranked_return_pct": leaveout_return,
                "ranked_return_change_vs_full_pp": round(
                    leaveout_return - full_ranked_return_pct, 4
                ),
                "portfolio_comparison": comparison,
                "ranked_return_above_random_median": float(
                    comparison["ranked_return_vs_random_median_pp"]
                )
                > 0.0,
                "ranked_return_above_fixed_hash": float(
                    comparison["ranked_return_vs_hash_pp"]
                )
                > 0.0,
            }
        )
        random_rows.extend({"scenario": scenario, **row} for row in seed_runs)
    return summaries, random_rows


def _scope_data(v6_report_path: Path, v6_report: dict[str, Any]) -> list[dict[str, Any]]:
    folds = v6_report.get("folds")
    _require(isinstance(folds, list), "v6 report has no folds")
    scopes: list[dict[str, Any]] = []
    stitched_candidates: list[dict[str, Any]] = []
    for fold in folds:
        fold_name = str(fold.get("fold_name", ""))
        _require(fold_name in EXPECTED_ELIGIBLE_FOLDS, f"unexpected fold: {fold_name}")
        input_artifacts = fold.get("input_artifacts")
        artifacts = fold.get("artifacts")
        _require(isinstance(input_artifacts, dict), f"missing fold inputs: {fold_name}")
        _require(isinstance(artifacts, dict), f"missing fold artifacts: {fold_name}")
        evaluation = _load_jsonl(
            _resolve_artifact(
                input_artifacts.get("evaluation"), v6_report_path, f"{fold_name}.evaluation"
            )
        )
        scores = _load_jsonl(
            _resolve_artifact(
                artifacts.get("candidate_scores"), v6_report_path, f"{fold_name}.scores"
            )
        )
        scored = _attach_scores(evaluation, scores, fold_name)
        stitched_candidates.extend(scored)
        scopes.append(
            {
                "scope": fold_name,
                "scored_rows": scored,
                "accepted_rows": _load_jsonl(
                    _resolve_artifact(
                        artifacts.get("accepted_entries"),
                        v6_report_path,
                        f"{fold_name}.accepted_entries",
                    )
                ),
                "rejection_rows": _load_jsonl(
                    _resolve_artifact(
                        artifacts.get("rejections"),
                        v6_report_path,
                        f"{fold_name}.rejections",
                    )
                ),
                "trade_rows": _load_jsonl(
                    _resolve_artifact(
                        artifacts.get("trades"), v6_report_path, f"{fold_name}.trades"
                    )
                ),
                "v6_result": fold,
            }
        )
    _require(
        [scope["scope"] for scope in scopes] == list(EXPECTED_ELIGIBLE_FOLDS),
        "v6 fold order mismatch",
    )
    stitched = v6_report.get("stitched_oos")
    _require(isinstance(stitched, dict), "v6 stitched result missing")
    artifacts = stitched.get("artifacts")
    _require(isinstance(artifacts, dict), "v6 stitched artifacts missing")
    stitched_scores = _load_jsonl(
        _resolve_artifact(
            artifacts.get("candidate_scores"), v6_report_path, "stitched_oos.scores"
        )
    )
    stitched_scored = _attach_scores(stitched_candidates, stitched_scores, "stitched_oos")
    scopes.append(
        {
            "scope": "stitched_oos",
            "scored_rows": stitched_scored,
            "accepted_rows": _load_jsonl(
                _resolve_artifact(
                    artifacts.get("accepted_entries"),
                    v6_report_path,
                    "stitched_oos.accepted_entries",
                )
            ),
            "rejection_rows": _load_jsonl(
                _resolve_artifact(
                    artifacts.get("rejections"),
                    v6_report_path,
                    "stitched_oos.rejections",
                )
            ),
            "trade_rows": _load_jsonl(
                _resolve_artifact(
                    artifacts.get("trades"), v6_report_path, "stitched_oos.trades"
                )
            ),
            "v6_result": stitched,
        }
    )
    return scopes


def _v6_audit_summary(v6_audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_sha256": v6_audit["report_sha256"],
        "eligible_folds": v6_audit["eligible_folds"],
        "artifact_count": len(v6_audit["artifact_sha256"]),
        "passes_research_screen": v6_audit["passes_research_screen"],
        "model_fitted": v6_audit["model_fitted"],
        "checks_passed": v6_audit["checks_passed"],
    }


def compute_diagnostic(
    v6_report_path: Path,
    *,
    v6_audit: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    v6_report_path = _guard_development_path(v6_report_path)
    _require(v6_report_path.exists(), f"v6 report does not exist: {v6_report_path}")
    if v6_audit is None:
        v6_audit = audit_v6_report(v6_report_path)
    _require(v6_audit.get("checks_passed") is True, "v6 report audit did not pass")
    v6_report = _load_json(v6_report_path)
    scopes = _scope_data(v6_report_path, v6_report)
    coefficient_rows, coefficient_summary = _coefficient_diagnostics(v6_report)

    artifact_rows: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ARTIFACT_NAMES
    }
    scope_aggregates: dict[str, Any] = {}
    stitched_rows: list[dict[str, Any]] | None = None
    stitched_trades: list[dict[str, Any]] | None = None
    stitched_full_return: float | None = None
    for scope in scopes:
        scope_name = str(scope["scope"])
        scored_rows = scope["scored_rows"]
        buckets = _bucket_rows(scope_name, scored_rows)
        capacity = _capacity_rows(
            scope_name,
            scored_rows,
            scope["accepted_rows"],
            scope["rejection_rows"],
        )
        assignments = _quintile_assignments(scope_name, scored_rows)
        quintiles = _quintile_rows(scope_name, assignments)
        signal_types = _signal_type_rows(
            scope_name, scored_rows, buckets, capacity
        )
        bucket_summary = _bucket_aggregate(buckets)
        capacity_summary = _capacity_aggregate(capacity)
        quintile_summary = _quintile_summary(quintiles)
        v6_result = scope["v6_result"]
        comparison = v6_result["portfolio_comparison"]
        scope_summary = {
            "scope": scope_name,
            "candidates": len(scored_rows),
            "entry_days": len({str(row["entry_day"]) for row in scored_rows}),
            "v6_pairwise_accuracy": float(
                v6_result["evaluation"]["pairwise_metrics"][
                    "day_weighted_pairwise_accuracy"
                ]
            ),
            "v6_ranked_return_pct": float(
                comparison["pairwise_ranked"]["summary"]["total_return_pct"]
            ),
            "v6_ranked_return_vs_random_median_pp": float(
                comparison["ranked_return_vs_random_median_pp"]
            ),
            "v6_random_percentile": float(
                comparison["ranked_return_random_order_percentile"]
            ),
            "bucket_diagnostics": bucket_summary,
            "capacity_diagnostics": capacity_summary,
            "quintile_diagnostics": quintile_summary,
        }
        artifact_rows["bucket_diagnostics"].extend(buckets)
        artifact_rows["capacity_diagnostics"].extend(capacity)
        artifact_rows["score_quintiles"].extend(quintiles)
        artifact_rows["signal_type_diagnostics"].extend(signal_types)
        artifact_rows["scope_summary"].append(scope_summary)
        scope_aggregates[scope_name] = {
            "bucket_diagnostics": bucket_summary,
            "capacity_diagnostics": capacity_summary,
            "quintile_diagnostics": quintile_summary,
        }
        if scope_name == "stitched_oos":
            stitched_rows = scored_rows
            stitched_trades = scope["trade_rows"]
            stitched_full_return = float(scope_summary["v6_ranked_return_pct"])

    _require(
        stitched_rows is not None
        and stitched_trades is not None
        and stitched_full_return is not None,
        "stitched scope missing",
    )
    concentration = _concentration(stitched_trades)
    config_path_record = v6_report.get("input", {}).get("config")
    config_path = _resolve_artifact(config_path_record, v6_report_path, "v6.config")
    from backtest_winrate import _resolve_execution_config
    from utils.helpers import load_config

    execution_costs = _resolve_execution_config(load_config(config_path))
    leaveout_summaries, leaveout_random_rows = _leave_one_out(
        stitched_rows, execution_costs, concentration, stitched_full_return
    )
    artifact_rows["coefficient_stability"] = coefficient_rows
    artifact_rows["leave_one_out_random_runs"] = leaveout_random_rows
    core = {
        "version": VERSION,
        "diagnostic_status": DIAGNOSTIC_STATUS,
        "purpose": (
            "Explain the frozen v6 full-ranking versus funded-portfolio mismatch; "
            "do not rescue v6 or select v7."
        ),
        "fixed_design": {
            "scopes": [*EXPECTED_ELIGIBLE_FOLDS, "stitched_oos"],
            "bucket": "entry_day + signal_type",
            "top1_minimum_candidates": 2,
            "top4_size": 4,
            "top4_minimum_candidates": 5,
            "score_bins": "within-bucket model-score quintiles Q1..Q5",
            "capacity_comparison": (
                "accepted versus max_positions rejected inside the same entry_day "
                "and signal_type"
            ),
            "coefficient_folds": list(EXPECTED_ELIGIBLE_FOLDS),
            "leave_one_out_scenarios": [
                "highest net-PnL entry day",
                "highest net-PnL entry month",
            ],
            "leave_one_out_refits_model": False,
            "random_seed_start": RANDOM_SEED_START,
            "random_seed_count": RANDOM_SEED_COUNT,
            "random_seed_end": RANDOM_SEEDS[-1],
            "research_screen_defined": False,
            "holdout_used": False,
        },
        "input": {
            "v6_report": {
                "path": str(v6_report_path),
                "sha256": _sha256_file(v6_report_path),
            },
            "diagnostic_code": {
                "path": str(Path(__file__).resolve()),
                "sha256": _sha256_file(Path(__file__).resolve()),
            },
            "v6_audit": _v6_audit_summary(v6_audit),
        },
        "scope_aggregates": scope_aggregates,
        "coefficient_stability": coefficient_summary,
        "stitched_profit_concentration": concentration,
        "leave_one_out": leaveout_summaries,
        "interpretation_policy": {
            "post_hoc_exploratory": True,
            "changes_v6_screen": False,
            "selects_new_factors": False,
            "authorizes_v7": False,
            "random_order_is_confidence_interval": False,
        },
        "holdout_used": False,
        "production_eligible": False,
    }
    return core, artifact_rows


def build_report(v6_report_path: Path, output_dir: Path) -> dict[str, Any]:
    v6_report_path = _guard_development_path(v6_report_path)
    output_dir = _guard_development_path(output_dir)
    _require(
        not output_dir.exists(),
        f"output directory already exists; refusing overwrite: {output_dir}",
    )
    core, artifact_rows = compute_diagnostic(v6_report_path)
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
    parser.add_argument("--v6-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(args.v6_report, args.output_dir)
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
