"""Run the preregistered v6 pairwise ranker on frozen v5 walk-forward folds.

This is a development-only experiment.  It never accepts Holdout paths, never
changes candidates or exits, and never authorizes production use.
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

from backtest_winrate import _resolve_execution_config
from long_history_walk_forward_audit import (
    audit_fold_report,
    audit_source_report,
)
from portfolio_pairwise_ranking_experiment import (
    FORBIDDEN_OUTCOME_FIELDS,
    MODEL_FEATURE_NAMES,
    MODEL_L2,
    MODEL_MAX_ITERATIONS,
    MODEL_SCORE_CLIP,
    MODEL_TOLERANCE,
    PORTFOLIO_CONFIG,
    PRIORITY_SCORE_GAP,
    RAW_FEATURE_NAMES,
    _model_public,
    _pairwise_metrics,
    _portfolio,
    _portfolio_summary,
    _score_rows,
    _write_portfolio_artifacts,
    _write_score_artifact,
    build_cross_sectional_feature_map,
    build_pairwise_training_data,
    fit_pairwise_logistic,
    hydrate_causal_ranking_features,
)
from utils.helpers import load_config

VERSION = "long_history_pairwise_ranking.v1"
EXPECTED_ELIGIBLE_FOLDS = tuple(f"fold_{index:02d}" for index in range(3, 9))
RANDOM_SEED_START = 20260830
RANDOM_SEED_COUNT = 200
RANDOM_SEEDS = tuple(range(RANDOM_SEED_START, RANDOM_SEED_START + RANDOM_SEED_COUNT))
DEFAULT_CONFIG = BASE_DIR / "config" / "config.yaml"
V4_PAIRWISE_CODE = BASE_DIR / "portfolio_pairwise_ranking_experiment.py"
BACKTEST_ENGINE = BASE_DIR / "backtest_winrate.py"
SIGNAL_ENGINE = BASE_DIR / "strategy" / "macd.py"


class PairwiseRankingExperimentError(RuntimeError):
    """Raised when a frozen input or experiment contract is violated."""


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise PairwiseRankingExperimentError(
            f"Holdout paths are blocked for v6 development experiments: {resolved}"
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
    if not isinstance(value, dict):
        raise PairwiseRankingExperimentError(f"JSON object required: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise PairwiseRankingExperimentError(
                    f"JSONL object required at {path}:{line_number}"
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_json(row) + "\n")
    return {"path": str(path), "rows": len(rows), "sha256": _sha256_file(path)}


def _candidate_manifest(rows: list[dict[str, Any]]) -> str:
    identifiers = sorted(str(row["candidate_id"]) for row in rows)
    return _sha256_bytes("\n".join(identifiers).encode("utf-8"))


def _validate_unique(
    rows: list[dict[str, Any]], label: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in rows:
        candidate_id = str(row.get("candidate_id", ""))
        if not candidate_id:
            raise PairwiseRankingExperimentError(f"missing candidate_id in {label}")
        if candidate_id in indexed:
            duplicates.append(candidate_id)
        else:
            indexed[candidate_id] = row
    if duplicates:
        sample = ", ".join(sorted(set(duplicates))[:5])
        raise PairwiseRankingExperimentError(
            f"duplicate candidate_id values in {label}: {sample}"
        )
    return indexed


def _artifact_path(
    record: Any, report_path: Path, label: str
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(record, dict):
        raise PairwiseRankingExperimentError(f"missing artifact record: {label}")
    raw_path = record.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise PairwiseRankingExperimentError(f"missing artifact path: {label}")
    path = Path(raw_path)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    if not path.exists():
        raise PairwiseRankingExperimentError(f"artifact does not exist: {path}")
    digest = _sha256_file(path)
    if str(record.get("sha256", "")).lower() != digest:
        raise PairwiseRankingExperimentError(f"artifact hash mismatch: {label}")
    return path, {"path": str(path), "rows": record.get("rows"), "sha256": digest}


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _load_eligible_fold_rows(
    fold_report_path: Path,
    fold_report: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    folds = fold_report.get("folds")
    if not isinstance(folds, list):
        raise PairwiseRankingExperimentError("fold report has no fold list")
    eligible_names = [
        str(fold.get("fold_name", ""))
        for fold in folds
        if isinstance(fold, dict)
        and isinstance(fold.get("minimums"), dict)
        and fold["minimums"].get("passes") is True
    ]
    if eligible_names != list(EXPECTED_ELIGIBLE_FOLDS):
        raise PairwiseRankingExperimentError(
            "eligible folds must be exactly "
            f"{list(EXPECTED_ELIGIBLE_FOLDS)}; got {eligible_names}"
        )

    seen_rows: dict[str, str] = {}
    unique_rows: dict[str, dict[str, Any]] = {}
    evaluation_seen: set[str] = set()
    loaded_folds: list[dict[str, Any]] = []
    integrity = {
        "eligible_folds": eligible_names,
        "cross_fold_conflicting_rows": 0,
        "duplicate_evaluation_candidates": 0,
        "purged_ids_in_same_fold_training": 0,
    }
    for fold in folds:
        if not isinstance(fold, dict) or fold.get("fold_name") not in eligible_names:
            continue
        fold_name = str(fold["fold_name"])
        artifacts = fold.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        fold_rows: dict[str, list[dict[str, Any]]] = {}
        input_artifacts: dict[str, Any] = {}
        for artifact_name in ("train", "purged_training_labels", "evaluation"):
            path, artifact = _artifact_path(
                artifacts.get(artifact_name),
                fold_report_path,
                f"{fold_name}.{artifact_name}",
            )
            rows = _load_jsonl(path)
            if artifact["rows"] != len(rows):
                raise PairwiseRankingExperimentError(
                    f"artifact row count mismatch: {fold_name}.{artifact_name}"
                )
            indexed = _validate_unique(rows, f"{fold_name}.{artifact_name}")
            fold_rows[artifact_name] = rows
            artifact["candidate_manifest_sha256"] = _candidate_manifest(rows)
            input_artifacts[artifact_name] = artifact
            for candidate_id, row in indexed.items():
                canonical = _canonical_json(row)
                previous = seen_rows.get(candidate_id)
                if previous is not None and previous != canonical:
                    integrity["cross_fold_conflicting_rows"] += 1
                    raise PairwiseRankingExperimentError(
                        "candidate content differs across folds: "
                        f"{candidate_id} in {fold_name}.{artifact_name}"
                    )
                seen_rows[candidate_id] = canonical
                unique_rows.setdefault(candidate_id, row)

        train_ids = set(_validate_unique(fold_rows["train"], f"{fold_name}.train"))
        purged_ids = set(
            _validate_unique(
                fold_rows["purged_training_labels"],
                f"{fold_name}.purged_training_labels",
            )
        )
        overlap = train_ids & purged_ids
        if overlap:
            integrity["purged_ids_in_same_fold_training"] += len(overlap)
            raise PairwiseRankingExperimentError(
                f"purged candidates entered {fold_name} training: {sorted(overlap)[:5]}"
            )
        evaluation_ids = set(
            _validate_unique(fold_rows["evaluation"], f"{fold_name}.evaluation")
        )
        repeated = evaluation_seen & evaluation_ids
        if repeated:
            integrity["duplicate_evaluation_candidates"] += len(repeated)
            raise PairwiseRankingExperimentError(
                "evaluation candidates repeat across folds: "
                f"{sorted(repeated)[:5]}"
            )
        evaluation_seen.update(evaluation_ids)
        loaded_folds.append(
            {
                "fold_name": fold_name,
                "evaluation_window": fold.get("evaluation_window"),
                "rows": fold_rows,
                "input_artifacts": input_artifacts,
            }
        )

    all_rows = sorted(
        unique_rows.values(),
        key=lambda row: (
            str(row.get("entry_day", "")),
            str(row.get("signal_type", "")),
            str(row.get("symbol", "")),
            str(row.get("signal_day", "")),
            str(row.get("candidate_id", "")),
        ),
    )
    integrity.update(
        {
            "unique_candidates_loaded": len(all_rows),
            "evaluation_candidates": len(evaluation_seen),
            "all_candidate_ids_unique_within_artifacts": True,
            "cross_fold_rows_consistent": True,
            "evaluation_candidates_disjoint": True,
            "purged_excluded_from_same_fold_training": True,
        }
    )
    return loaded_folds, all_rows, integrity


def _zero_model() -> dict[str, Any]:
    return {
        "weights": np.zeros(len(MODEL_FEATURE_NAMES), dtype=float),
        "l2": MODEL_L2,
        "iterations": 0,
        "converged": False,
        "objective_weight_sum": 0.0,
    }


def _fit_training_rows(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    feature_map, feature_audit = build_cross_sectional_feature_map(rows)
    matrix, labels, sample_weights, pair_audit = build_pairwise_training_data(
        rows, feature_map
    )
    if len(matrix) == 0:
        model = _zero_model()
        model_fitted = False
        fallback_reason = "no_non_tied_within_day_same_priority_pairs"
    else:
        model = fit_pairwise_logistic(matrix, labels, sample_weights)
        model_fitted = bool(model["converged"])
        fallback_reason = None if model_fitted else "optimizer_not_converged"
    training_scores = {
        candidate_id: float(features @ np.asarray(model["weights"], dtype=float))
        for candidate_id, features in feature_map.items()
    }
    return model, {
        "model_fitted": model_fitted,
        "fallback_reason": fallback_reason,
        "candidates": len(rows),
        "candidate_manifest_sha256": _candidate_manifest(rows),
        "entry_days": len({str(row["entry_day"]) for row in rows}),
        "features": feature_audit,
        "pairs": pair_audit,
        "in_sample_pairwise": _pairwise_metrics(rows, training_scores),
    }


def _accepted_manifest(portfolio: dict[str, Any]) -> str:
    identifiers = sorted(str(row["candidate_id"]) for row in portfolio["accepted_entries"])
    return _sha256_bytes("\n".join(identifiers).encode("utf-8"))


def _portfolio_snapshot(portfolio: dict[str, Any]) -> dict[str, Any]:
    summary = portfolio["summary"]
    attribution = portfolio["attribution"]
    return {
        "total_return_pct": float(summary["total_return_pct"]),
        "max_drawdown_pct": float(summary["max_drawdown_pct"]),
        "trade_count": int(summary["count"]),
        "win_rate": float(summary["win_rate"]),
        "final_equity": float(summary["final_equity"]),
        "transaction_cost_cash": float(attribution["transaction_cost_cash"]),
        "position_capacity_utilization_pct": float(
            attribution["position_capacity_utilization_pct"]
        ),
        "max_positions_rejections": int(attribution["max_positions_rejections"]),
        "accepted_candidate_count": len(portfolio["accepted_entries"]),
        "accepted_candidate_ids_sha256": _accepted_manifest(portfolio),
    }


def _distribution(values: list[float]) -> dict[str, Any]:
    clean = np.asarray(
        [float(value) for value in values if math.isfinite(float(value))], dtype=float
    )
    if clean.size == 0:
        return {"n": 0}
    return {
        "n": int(clean.size),
        "mean": round(float(clean.mean()), 4),
        "std": round(float(clean.std(ddof=0)), 4),
        "min": round(float(clean.min()), 4),
        "p10": round(float(np.quantile(clean, 0.10)), 4),
        "p25": round(float(np.quantile(clean, 0.25)), 4),
        "median": round(float(np.median(clean)), 4),
        "p75": round(float(np.quantile(clean, 0.75)), 4),
        "p90": round(float(np.quantile(clean, 0.90)), 4),
        "max": round(float(clean.max()), 4),
    }


def _percentile(observed: float, values: list[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return None
    return round(sum(value <= observed for value in clean) / len(clean) * 100.0, 2)


def _random_seed_runs(
    rows: list[dict[str, Any]], costs: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for seed in RANDOM_SEEDS:
        config = {
            **PORTFOLIO_CONFIG,
            "score_mode": "P0",
            "tie_break": "random",
            "seed": seed,
        }
        snapshot = _portfolio_snapshot(_portfolio(rows, costs, config))
        runs.append({"seed": seed, **snapshot})
    metrics = (
        "total_return_pct",
        "max_drawdown_pct",
        "trade_count",
        "win_rate",
        "transaction_cost_cash",
        "position_capacity_utilization_pct",
    )
    return runs, {
        "seed_start": RANDOM_SEED_START,
        "seed_count": RANDOM_SEED_COUNT,
        "seeds": [RANDOM_SEEDS[0], RANDOM_SEEDS[-1]],
        "interpretation": (
            "Algorithmic same-priority order sensitivity only; seeds are not "
            "independent market samples and percentiles are not confidence intervals."
        ),
        "metric_distributions": {
            metric: _distribution([float(row[metric]) for row in runs])
            for metric in metrics
        },
    }


def _evaluate_portfolios(
    evaluation_rows: list[dict[str, Any]],
    scored_rows: list[dict[str, Any]],
    costs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    ranked = _portfolio(scored_rows, costs, PORTFOLIO_CONFIG)
    symbol_config = {
        **PORTFOLIO_CONFIG,
        "score_mode": "P0",
        "tie_break": "symbol_asc",
        "seed": RANDOM_SEED_START,
    }
    hash_config = {**symbol_config, "tie_break": "hash"}
    symbol = _portfolio(evaluation_rows, costs, symbol_config)
    fixed_hash = _portfolio(evaluation_rows, costs, hash_config)
    random_runs, random_reference = _random_seed_runs(evaluation_rows, costs)
    ranked_return = float(ranked["summary"]["total_return_pct"])
    ranked_drawdown = float(ranked["summary"]["max_drawdown_pct"])
    random_returns = [float(row["total_return_pct"]) for row in random_runs]
    comparison = {
        "pairwise_ranked": _portfolio_summary(ranked),
        "p0_symbol_asc": _portfolio_summary(symbol),
        "p0_fixed_hash": _portfolio_summary(fixed_hash),
        "random_order_reference": random_reference,
        "ranked_return_vs_symbol_asc_pp": round(
            ranked_return - float(symbol["summary"]["total_return_pct"]), 4
        ),
        "ranked_return_vs_hash_pp": round(
            ranked_return - float(fixed_hash["summary"]["total_return_pct"]), 4
        ),
        "ranked_return_vs_random_median_pp": round(
            ranked_return
            - float(random_reference["metric_distributions"]["total_return_pct"]["median"]),
            4,
        ),
        "ranked_return_random_order_percentile": _percentile(
            ranked_return, random_returns
        ),
        "ranked_drawdown_vs_random_median_pp": round(
            ranked_drawdown
            - float(random_reference["metric_distributions"]["max_drawdown_pct"]["median"]),
            4,
        ),
    }
    return comparison, ranked, random_runs


def _evaluate_scope(
    scope_dir: Path,
    evaluation_rows: list[dict[str, Any]],
    scored_rows: list[dict[str, Any]],
    model_scores: dict[str, float],
    costs: dict[str, Any],
) -> dict[str, Any]:
    comparison, ranked, random_runs = _evaluate_portfolios(
        evaluation_rows, scored_rows, costs
    )
    return {
        "evaluation": {
            "candidates": len(evaluation_rows),
            "candidate_manifest_sha256": _candidate_manifest(evaluation_rows),
            "entry_days": len({str(row["entry_day"]) for row in evaluation_rows}),
            "pairwise_metrics": _pairwise_metrics(evaluation_rows, model_scores),
        },
        "portfolio_comparison": comparison,
        "artifacts": {
            "candidate_scores": _write_score_artifact(
                scope_dir, "evaluation", scored_rows
            ),
            **_write_portfolio_artifacts(scope_dir, "pairwise_ranked", ranked),
            "random_seed_runs": _write_jsonl(
                scope_dir / "random_seed_runs.jsonl", random_runs
            ),
        },
    }


def _research_screen(
    fold_results: list[dict[str, Any]], stitched: dict[str, Any]
) -> dict[str, Any]:
    fold_names = [str(fold["fold_name"]) for fold in fold_results]
    model_fitted = [bool(fold["training"]["model_fitted"]) for fold in fold_results]
    training_pair_days = [
        int(fold["training"]["pairs"]["entry_days_with_pairs"])
        for fold in fold_results
    ]
    fold_accuracies = [
        float(fold["evaluation"]["pairwise_metrics"]["day_weighted_pairwise_accuracy"])
        for fold in fold_results
    ]
    fold_return_deltas = [
        float(fold["portfolio_comparison"]["ranked_return_vs_random_median_pp"])
        for fold in fold_results
    ]
    stitched_metrics = stitched["evaluation"]["pairwise_metrics"]
    stitched_comparison = stitched["portfolio_comparison"]
    stitched_ranked = stitched_comparison["pairwise_ranked"]["summary"]
    stitched_random = stitched_comparison["random_order_reference"][
        "metric_distributions"
    ]
    checks = {
        "eligible_folds_exactly_fold_03_through_fold_08": (
            fold_names == list(EXPECTED_ELIGIBLE_FOLDS) and len(fold_results) == 6
        ),
        "all_six_models_fitted": len(model_fitted) == 6 and all(model_fitted),
        "every_fold_training_pairwise_entry_days_at_least_5": (
            len(training_pair_days) == 6 and all(value >= 5 for value in training_pair_days)
        ),
        "stitched_pairwise_entry_days_at_least_20": int(
            stitched_metrics["entry_days_with_pairs"]
        )
        >= 20,
        "stitched_undirected_pairs_at_least_100": int(
            stitched_metrics["undirected_pairs"]
        )
        >= 100,
        "stitched_pairwise_accuracy_above_random": float(
            stitched_metrics["day_weighted_pairwise_accuracy"]
        )
        > 0.5,
        "at_least_four_folds_pairwise_accuracy_above_random": sum(
            value > 0.5 for value in fold_accuracies
        )
        >= 4,
        "at_least_four_folds_ranked_return_above_random_median": sum(
            value > 0.0 for value in fold_return_deltas
        )
        >= 4,
        "median_fold_ranked_return_advantage_above_zero": (
            len(fold_return_deltas) == 6
            and float(statistics.median(fold_return_deltas)) > 0.0
        ),
        "stitched_ranked_return_above_random_median": float(
            stitched_ranked["total_return_pct"]
        )
        > float(stitched_random["total_return_pct"]["median"]),
        "stitched_ranked_return_random_percentile_at_least_75": float(
            stitched_comparison["ranked_return_random_order_percentile"]
        )
        >= 75.0,
        "stitched_max_drawdown_not_above_random_median": float(
            stitched_ranked["max_drawdown_pct"]
        )
        <= float(stitched_random["max_drawdown_pct"]["median"]),
    }
    return {
        "checks": checks,
        "fold_pairwise_accuracy_above_random_count": sum(
            value > 0.5 for value in fold_accuracies
        ),
        "fold_ranked_return_above_random_median_count": sum(
            value > 0.0 for value in fold_return_deltas
        ),
        "median_fold_ranked_return_advantage_pp": (
            round(float(statistics.median(fold_return_deltas)), 4)
            if fold_return_deltas
            else None
        ),
        "passes_research_screen": all(checks.values()),
        "holdout_used": False,
        "production_eligible": False,
        "production_blocker": (
            "Development walk-forward results cannot authorize production. A genuinely "
            "untouched Holdout and independent review remain required."
        ),
    }


def _input_record(path: Path) -> dict[str, Any]:
    return {"path": str(path), "sha256": _sha256_file(path)}


def _audited_history_dir(fold_report: dict[str, Any]) -> Path:
    input_value = fold_report.get("input")
    input_value = input_value if isinstance(input_value, dict) else {}
    qfq = input_value.get("qfq_history")
    qfq = qfq if isinstance(qfq, dict) else {}
    raw_path = qfq.get("history_dir")
    if not isinstance(raw_path, str) or not raw_path:
        raise PairwiseRankingExperimentError("fold report lacks QFQ history_dir")
    return _guard_development_path(Path(raw_path))


def build_report(
    source_report_path: Path,
    fold_report_path: Path,
    output_dir: Path,
    config_path: Path = DEFAULT_CONFIG,
) -> dict[str, Any]:
    source_report_path = _guard_development_path(source_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    output_dir = _guard_development_path(output_dir)
    config_path = _guard_development_path(config_path)
    if output_dir.exists():
        raise PairwiseRankingExperimentError(
            f"output directory already exists; refusing overwrite: {output_dir}"
        )

    source_audit = audit_source_report(source_report_path)
    fold_audit = audit_fold_report(fold_report_path, source_audit)
    fold_report = fold_audit["_report_value"]
    loaded_folds, all_rows, integrity = _load_eligible_fold_rows(
        fold_report_path, fold_report
    )
    if len(all_rows) != int(fold_audit["dataset_candidates"]):
        raise PairwiseRankingExperimentError(
            "eligible fold artifacts do not cover the full audited v5 dataset"
        )
    history_dir = _audited_history_dir(fold_report)
    hydrated, history_audit = hydrate_causal_ranking_features(
        {"eligible_fold_candidates": all_rows}, history_dir=history_dir
    )
    if history_audit["history_manifest_sha256"] != fold_audit["qfq_manifest_sha256"]:
        raise PairwiseRankingExperimentError(
            "v6 QFQ manifest differs from the audited v5 fold manifest"
        )
    hydrated_by_id = {
        str(row["candidate_id"]): row for row in hydrated["eligible_fold_candidates"]
    }
    costs = _resolve_execution_config(load_config(config_path))
    output_dir.mkdir(parents=True, exist_ok=False)

    fold_results: list[dict[str, Any]] = []
    stitched_rows: list[dict[str, Any]] = []
    stitched_scored: list[dict[str, Any]] = []
    stitched_scores: dict[str, float] = {}
    for fold in loaded_folds:
        fold_name = str(fold["fold_name"])
        train_rows = [
            hydrated_by_id[str(row["candidate_id"])] for row in fold["rows"]["train"]
        ]
        evaluation_rows = [
            hydrated_by_id[str(row["candidate_id"])]
            for row in fold["rows"]["evaluation"]
        ]
        model, training = _fit_training_rows(train_rows)
        scored_rows, model_scores, feature_audit = _score_rows(evaluation_rows, model)
        scope = _evaluate_scope(
            output_dir / fold_name,
            evaluation_rows,
            scored_rows,
            model_scores,
            costs,
        )
        fold_results.append(
            {
                "fold_name": fold_name,
                "evaluation_window": fold["evaluation_window"],
                "input_artifacts": fold["input_artifacts"],
                "training": {**training, "model": _model_public(model)},
                "evaluation_feature_audit": feature_audit,
                **scope,
            }
        )
        stitched_rows.extend(evaluation_rows)
        stitched_scored.extend(scored_rows)
        stitched_scores.update(model_scores)

    stitched = {
        "fold_names": list(EXPECTED_ELIGIBLE_FOLDS),
        "score_origin": "each candidate uses only its own fold train model",
        **_evaluate_scope(
            output_dir / "stitched_oos",
            stitched_rows,
            stitched_scored,
            stitched_scores,
            costs,
        ),
    }
    screen = _research_screen(fold_results, stitched)
    report = {
        "version": VERSION,
        "dataset_status": "development_viewed_not_holdout",
        "research_question": (
            "Does the frozen v4 causal pairwise ranker generalize across the six "
            "eligible v5 purged expanding walk-forward folds?"
        ),
        "preregistered_design": {
            "eligible_folds": list(EXPECTED_ELIGIBLE_FOLDS),
            "model": "day-weighted pairwise logistic regression",
            "l2": MODEL_L2,
            "max_iterations": MODEL_MAX_ITERATIONS,
            "tolerance": MODEL_TOLERANCE,
            "raw_feature_names": list(RAW_FEATURE_NAMES),
            "model_feature_names": list(MODEL_FEATURE_NAMES),
            "feature_time_boundary": "local QFQ datetime <= signal_day",
            "training_artifact": "train.jsonl only",
            "purged_training_labels_used": False,
            "evaluation_labels_used_for_fitting": False,
            "priority_policy": "P0 signal-type priority is immutable",
            "priority_score_gap": PRIORITY_SCORE_GAP,
            "model_score_clip": [-MODEL_SCORE_CLIP, MODEL_SCORE_CLIP],
            "equal_score_tie_break": "fixed hash",
            "portfolio_baselines": ["P0 symbol_asc", "P0 fixed hash"],
            "random_seed_start": RANDOM_SEED_START,
            "random_seed_count": RANDOM_SEED_COUNT,
            "random_seed_end": RANDOM_SEEDS[-1],
            "hyperparameter_search": False,
            "holdout_used": False,
        },
        "outcome_isolation": {
            "training_label": "within-day/same-priority ordering by trade_pnl_pct",
            "forbidden_feature_fields": sorted(FORBIDDEN_OUTCOME_FIELDS),
            "feature_schema_intersection_with_forbidden_fields": sorted(
                set(MODEL_FEATURE_NAMES) & FORBIDDEN_OUTCOME_FIELDS
            ),
            "purged_training_labels_used": False,
            "evaluation_labels_used_only_for_oos_metrics": True,
        },
        "input": {
            "source_report": _input_record(source_report_path),
            "fold_report": _input_record(fold_report_path),
            "config": _input_record(config_path),
            "resolved_execution_sha256": _sha256_bytes(
                _canonical_json(costs).encode("utf-8")
            ),
            "qfq_history": history_audit,
            "v4_pairwise_code": _input_record(V4_PAIRWISE_CODE),
            "backtest_engine": _input_record(BACKTEST_ENGINE),
            "signal_engine": _input_record(SIGNAL_ENGINE),
            "experiment_code": _input_record(Path(__file__).resolve()),
            "v5_audit": {
                "source_checks_passed": source_audit["checks_passed"],
                "fold_checks_passed": fold_audit["checks_passed"],
                "fold_count": fold_audit["fold_count"],
                "eligible_fold_count": fold_audit["eligible_fold_count"],
                "dataset_candidates": fold_audit["dataset_candidates"],
                "dataset_candidate_ids_sha256": fold_audit[
                    "dataset_candidate_ids_sha256"
                ],
                "qfq_manifest_sha256": fold_audit["qfq_manifest_sha256"],
            },
        },
        "integrity": integrity,
        "portfolio_config": PORTFOLIO_CONFIG,
        "folds": fold_results,
        "stitched_oos": stitched,
        "screen": screen,
        "model_fitted": all(
            bool(fold["training"]["model_fitted"]) for fold in fold_results
        ),
        "holdout_used": False,
        "production_eligible": False,
    }
    _write_json(output_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--fold-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    try:
        report = build_report(
            args.source_report,
            args.fold_report,
            args.output_dir,
            args.config,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must fail closed as JSON.
        print(
            json.dumps(
                {"version": VERSION, "passes_research_screen": False, "error": str(exc)},
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
                "passes_research_screen": report["screen"]["passes_research_screen"],
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
