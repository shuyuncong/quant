"""Run preregistered v8a gap-weighted Top-4 ranking on development data.

The only intended change from frozen v7a is within-bucket boundary-pair
weighting by absolute realized PnL gap.  Factors, model, folds, metrics, screen,
and the prohibition on Holdout/portfolio replay remain unchanged.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import long_history_v7_top4_ranking_experiment as v7a

VERSION = "long_history_v8_gap_weighted_top4_ranking.v1"
EXPERIMENT_STATUS = "post_hoc_followup_development"
DATASET_STATUS = v7a.DATASET_STATUS
FACTOR_REPORT_VERSION = v7a.FACTOR_REPORT_VERSION
EXPECTED_ELIGIBLE_FOLDS = v7a.EXPECTED_ELIGIBLE_FOLDS
TOP_K = v7a.TOP_K
MIN_BUCKET_CANDIDATES = v7a.MIN_BUCKET_CANDIDATES
FACTOR_NAMES = v7a.FACTOR_NAMES
MODEL_FEATURE_NAMES = v7a.MODEL_FEATURE_NAMES
MODEL_L2 = v7a.MODEL_L2
MODEL_MAX_ITERATIONS = v7a.MODEL_MAX_ITERATIONS
MODEL_TOLERANCE = v7a.MODEL_TOLERANCE
MODEL_SCORE_CLIP = v7a.MODEL_SCORE_CLIP
ARTIFACT_NAMES = v7a.ARTIFACT_NAMES
OUTCOME_FIELDS = v7a.OUTCOME_FIELDS
FACTOR_PREFLIGHT_CODE = v7a.FACTOR_PREFLIGHT_CODE
FACTOR_PREFLIGHT_AUDIT_CODE = v7a.FACTOR_PREFLIGHT_AUDIT_CODE
V5_AUDIT_CODE = v7a.V5_AUDIT_CODE
FOLD_LOADER_CODE = v7a.FOLD_LOADER_CODE
V7A_RANKING_CORE_CODE = Path(v7a.__file__).resolve()
V7A_RANKING_CORE_SHA256 = (
    "4efe0e06bc0b2c4a96c06ec79b6b51fce4c5bae411c7ac001f6f0f277a16ef14"
)
AUDIT_CODE = BASE_DIR / "long_history_v8_gap_weighted_top4_ranking_audit.py"
PAIR_WEIGHTING = "absolute_trade_pnl_gap_normalized_within_bucket"


class GapWeightedTop4ExperimentError(RuntimeError):
    """Raised when an input or frozen v8a experiment contract is violated."""


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise GapWeightedTop4ExperimentError(f"Holdout path is blocked: {resolved}")
    return resolved


def _sha256_file(path: Path) -> str:
    return v7a._sha256_file(path)


def _input_record(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    if not path.exists() or not path.is_file():
        raise GapWeightedTop4ExperimentError(f"input file does not exist: {path}")
    return {"path": str(path), "sha256": _sha256_file(path)}


def _assert_frozen_v7a_core() -> None:
    actual = _sha256_file(V7A_RANKING_CORE_CODE)
    if actual != V7A_RANKING_CORE_SHA256:
        raise GapWeightedTop4ExperimentError(
            "frozen v7a ranking core SHA256 drift: "
            f"expected {V7A_RANKING_CORE_SHA256}, got {actual}"
        )


def build_gap_weighted_boundary_training_data(
    rows: list[dict[str, Any]], feature_map: dict[str, np.ndarray]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Build Top-4 boundary samples weighted by normalized absolute PnL gap."""

    differences: list[np.ndarray] = []
    labels: list[float] = []
    sample_weights: list[float] = []
    normalized_pair_weights: list[float] = []
    raw_gaps: list[float] = []
    eligible_buckets = 0
    buckets_with_pairs = 0
    candidates_in_eligible_buckets = 0
    undirected_pairs = 0
    ties_excluded = 0
    groups = v7a._bucket_groups(rows)
    for key in sorted(groups):
        group = groups[key]
        if len(group) < MIN_BUCKET_CANDIDATES:
            continue
        eligible_buckets += 1
        candidates_in_eligible_buckets += len(group)
        ordered = sorted(
            group,
            key=lambda row: (-v7a._outcome(row), str(row["candidate_id"])),
        )
        actual_top = ordered[:TOP_K]
        rest = ordered[TOP_K:]
        valid_pairs: list[tuple[dict[str, Any], dict[str, Any], float]] = []
        for winner in actual_top:
            for loser in rest:
                gap = abs(v7a._outcome(winner) - v7a._outcome(loser))
                if gap <= 1e-12:
                    ties_excluded += 1
                    continue
                valid_pairs.append((winner, loser, gap))
        if not valid_pairs:
            continue
        total_gap = math.fsum(gap for _, _, gap in valid_pairs)
        if not math.isfinite(total_gap) or total_gap <= 0.0:
            raise GapWeightedTop4ExperimentError(
                f"invalid positive pair-gap total for bucket {key}: {total_gap}"
            )
        buckets_with_pairs += 1
        undirected_pairs += len(valid_pairs)
        for winner, loser, gap in valid_pairs:
            pair_weight = gap / total_gap
            directed_weight = pair_weight / 2.0
            difference = (
                feature_map[str(winner["candidate_id"])]
                - feature_map[str(loser["candidate_id"])]
            )
            differences.extend((difference, -difference))
            labels.extend((1.0, 0.0))
            sample_weights.extend((directed_weight, directed_weight))
            normalized_pair_weights.append(pair_weight)
            raw_gaps.append(gap)
    matrix = (
        np.asarray(differences, dtype=float)
        if differences
        else np.empty((0, len(MODEL_FEATURE_NAMES)), dtype=float)
    )
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
            "pair_weighting": PAIR_WEIGHTING,
            "pair_weight_cap": None,
            "pair_weight_floor": None,
            "pair_gap_power": 1.0,
            "raw_pair_gap_sum_pp": round(math.fsum(raw_gaps), 10),
            "raw_pair_gap_min_pp": round(min(raw_gaps), 10) if raw_gaps else None,
            "raw_pair_gap_max_pp": round(max(raw_gaps), 10) if raw_gaps else None,
            "normalized_pair_weight_min": (
                round(min(normalized_pair_weights), 12)
                if normalized_pair_weights
                else None
            ),
            "normalized_pair_weight_max": (
                round(max(normalized_pair_weights), 12)
                if normalized_pair_weights
                else None
            ),
            "each_bucket_total_training_weight": 1.0,
            "bucket_weight_sum": round(float(math.fsum(sample_weights)), 10),
        },
    )


def _fit_scope(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, float]]:
    feature_map, feature_audit = v7a.build_cross_sectional_feature_map(rows)
    matrix, labels, weights, pair_audit = build_gap_weighted_boundary_training_data(
        rows, feature_map
    )
    model = v7a.fit_pairwise_logistic(matrix, labels, weights)
    _, scores = v7a.score_rows(rows, feature_map, model)
    _, metrics = v7a.evaluate_top4(rows, scores)
    return (
        model,
        {
            "feature_audit": feature_audit,
            "pairs": pair_audit,
            "in_sample_metrics": metrics,
            "model": v7a._model_public(model),
        },
        scores,
    )


def _research_screen(
    folds: list[dict[str, Any]], stitched: dict[str, Any]
) -> dict[str, Any]:
    return v7a._research_screen(folds, stitched)


def build_snapshot(factor_report_path: Path, fold_report_path: Path) -> dict[str, Any]:
    _assert_frozen_v7a_core()
    factor_report_path = _guard_development_path(factor_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    factor_audit = v7a.audit_factor_report(factor_report_path)
    if factor_audit.get("checks_passed") is not True:
        raise GapWeightedTop4ExperimentError(
            "factor preflight single-report audit failed"
        )
    factor_report = factor_audit["_report_value"]
    if factor_report.get("version") != FACTOR_REPORT_VERSION:
        raise GapWeightedTop4ExperimentError("factor report version is not frozen v2")
    recorded_fold = v7a._resolve_report_input(
        factor_report, "fold_report", factor_report_path.parent
    )
    if recorded_fold != fold_report_path:
        raise GapWeightedTop4ExperimentError(
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
        raise GapWeightedTop4ExperimentError(
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
        score_output, scores = v7a.score_rows(
            evaluation_rows, evaluation_feature_map, model
        )
        for record in score_output:
            record["fold_name"] = fold_name
        bucket_output, metrics = v7a.evaluate_top4(evaluation_rows, scores)
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
    stitched_bucket_rows, stitched_metrics = v7a.evaluate_top4(
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
        "_training_rows_by_fold": training_rows_by_fold,
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
        "experiment_status": EXPERIMENT_STATUS,
        "dataset_status": DATASET_STATUS,
        "research_question": (
            "Does within-bucket absolute-PnL-gap weighting make the frozen 19-factor "
            "linear Top-4 boundary ranker economically stable across development folds?"
        ),
        "preregistered_design": {
            "only_change_from_v7a": (
                "boundary pair weights are absolute trade_pnl_pct gaps normalized "
                "within each training bucket"
            ),
            "v7a_screen_reused_without_change": True,
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
            "pair_weighting": PAIR_WEIGHTING,
            "pair_gap_power": 1.0,
            "pair_weight_cap": None,
            "pair_weight_floor": None,
            "each_bucket_total_training_weight": 1.0,
            "predicted_tie_break": "candidate_id asc",
            "hyperparameter_search": False,
            "portfolio_replay": False,
            "holdout_used": False,
        },
        "outcome_isolation": {
            "training_outcome": "trade_pnl_pct from same-fold train.jsonl only",
            "training_outcome_use": "Top-4 labels and normalized absolute pair gaps",
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
            "v7a_ranking_core_code": _input_record(V7A_RANKING_CORE_CODE),
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
            "v7a_version": v7a.VERSION,
            "v7a_ranking_core_sha256": V7A_RANKING_CORE_SHA256,
            "v7a_result": "passes_research_screen=false",
            "v7a_failure_changed": False,
            "diagnostic_status": "post_hoc_exploratory",
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
        raise GapWeightedTop4ExperimentError(
            f"output directory already exists; refusing overwrite: {output_dir}"
        )
    snapshot = build_snapshot(factor_report_path, fold_report_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = {
        name: v7a._write_jsonl(output_dir / f"{name}.jsonl", rows)
        for name, rows in sorted(snapshot["artifact_rows"].items())
    }
    report = assemble_report(snapshot, factor_report_path, fold_report_path, artifacts)
    v7a._write_json(output_dir / "report.json", report)
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
