"""Read-only audit for two independent v6 long-history pairwise runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import _resolve_execution_config
from long_history_pairwise_ranking_experiment import (
    BACKTEST_ENGINE,
    EXPECTED_ELIGIBLE_FOLDS,
    MODEL_FEATURE_NAMES,
    MODEL_L2,
    MODEL_MAX_ITERATIONS,
    MODEL_SCORE_CLIP,
    MODEL_TOLERANCE,
    PORTFOLIO_CONFIG,
    PRIORITY_SCORE_GAP,
    RANDOM_SEED_COUNT,
    RANDOM_SEED_START,
    RANDOM_SEEDS,
    RAW_FEATURE_NAMES,
    SIGNAL_ENGINE,
    V4_PAIRWISE_CODE,
    _audited_history_dir,
    _candidate_manifest,
    _canonical_json,
    _evaluate_portfolios,
    _fit_training_rows,
    _load_eligible_fold_rows,
    _model_public,
    _pairwise_metrics,
    _research_screen,
    _score_rows,
    _sha256_bytes,
    _sha256_file,
    hydrate_causal_ranking_features,
)
from long_history_pairwise_ranking_experiment import VERSION as EXPERIMENT_VERSION
from long_history_walk_forward_audit import (
    audit_fold_report,
    audit_source_report,
)
from utils.helpers import load_config

VERSION = "long_history_pairwise_ranking_audit.v1"
ARTIFACT_NAMES = (
    "candidate_scores",
    "accepted_entries",
    "trades",
    "rejections",
    "equity_curve",
    "random_seed_runs",
)


class PairwiseRankingAuditError(RuntimeError):
    """Raised when a v6 report, calculation, or deterministic replay differs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PairwiseRankingAuditError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require(
        not any("holdout" in part.lower() for part in resolved.parts),
        f"Holdout paths are blocked during v6 audit: {resolved}",
    )
    return resolved


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


def _resolve_record(
    record: Any,
    report_path: Path,
    label: str,
    *,
    expected_path: Path | None = None,
) -> Path:
    _require(isinstance(record, dict), f"missing input record: {label}")
    raw_path = record.get("path")
    _require(isinstance(raw_path, str) and bool(raw_path), f"missing path: {label}")
    path = Path(raw_path)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    _require(path.exists(), f"input does not exist: {path}")
    if expected_path is not None:
        _require(path == expected_path.resolve(), f"unexpected path for {label}: {path}")
    _require(
        str(record.get("sha256", "")).lower() == _sha256_file(path),
        f"input hash mismatch: {label}",
    )
    return path


def _artifact_path(record: Any, report_path: Path, label: str) -> Path:
    _require(isinstance(record, dict), f"missing artifact record: {label}")
    raw_path = record.get("path")
    _require(isinstance(raw_path, str) and bool(raw_path), f"missing artifact path: {label}")
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


def _public_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in row.items() if not str(key).startswith("_")}
        for row in rows
    ]


def _expected_score_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": row["candidate_id"],
            "symbol": row["symbol"],
            "signal_day": row["signal_day"],
            "entry_day": row["entry_day"],
            "signal_type": row["signal_type"],
            "pairwise_model_score": row["pairwise_model_score"],
            "portfolio_rank_score": row["portfolio_rank_score"],
        }
        for row in rows
    ]


def _verify_jsonl_artifact(
    record: Any,
    expected_rows: list[dict[str, Any]],
    report_path: Path,
    label: str,
) -> tuple[Path, str]:
    path = _artifact_path(record, report_path, label)
    actual_rows = _load_jsonl(path)
    _require(
        isinstance(record, dict) and record.get("rows") == len(actual_rows),
        f"artifact row count mismatch: {label}",
    )
    _require(
        [_canonical_json(row) for row in actual_rows]
        == [_canonical_json(row) for row in expected_rows],
        f"artifact content mismatch: {label}",
    )
    return path, _sha256_file(path)


def _verify_scope(
    reported: dict[str, Any],
    evaluation_rows: list[dict[str, Any]],
    scored_rows: list[dict[str, Any]],
    model_scores: dict[str, float],
    costs: dict[str, Any],
    report_path: Path,
    label: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    comparison, ranked, random_runs = _evaluate_portfolios(
        evaluation_rows, scored_rows, costs
    )
    expected_evaluation = {
        "candidates": len(evaluation_rows),
        "candidate_manifest_sha256": _candidate_manifest(evaluation_rows),
        "entry_days": len({str(row["entry_day"]) for row in evaluation_rows}),
        "pairwise_metrics": _pairwise_metrics(evaluation_rows, model_scores),
    }
    _require(
        reported.get("evaluation") == expected_evaluation,
        f"evaluation metrics mismatch: {label}",
    )
    _require(
        reported.get("portfolio_comparison") == comparison,
        f"portfolio comparison mismatch: {label}",
    )
    artifacts = reported.get("artifacts")
    _require(isinstance(artifacts, dict), f"missing artifacts: {label}")
    expected_artifacts = {
        "candidate_scores": _expected_score_rows(scored_rows),
        "accepted_entries": _public_rows(ranked["accepted_entries"]),
        "trades": _public_rows(ranked["trades"]),
        "rejections": _public_rows(ranked["rejections"]),
        "equity_curve": _public_rows(ranked["equity_curve"]),
        "random_seed_runs": random_runs,
    }
    _require(
        set(artifacts) == set(ARTIFACT_NAMES),
        f"unexpected artifact set: {label}",
    )
    hashes: dict[str, str] = {}
    for name in ARTIFACT_NAMES:
        _, digest = _verify_jsonl_artifact(
            artifacts.get(name),
            expected_artifacts[name],
            report_path,
            f"{label}.{name}",
        )
        hashes[f"{label}/{name}.jsonl"] = digest
    return {
        "evaluation": expected_evaluation,
        "portfolio_comparison": comparison,
    }, hashes


def _expected_v5_audit(
    source_audit: dict[str, Any], fold_audit: dict[str, Any]
) -> dict[str, Any]:
    return {
        "source_checks_passed": source_audit["checks_passed"],
        "fold_checks_passed": fold_audit["checks_passed"],
        "fold_count": fold_audit["fold_count"],
        "eligible_fold_count": fold_audit["eligible_fold_count"],
        "dataset_candidates": fold_audit["dataset_candidates"],
        "dataset_candidate_ids_sha256": fold_audit["dataset_candidate_ids_sha256"],
        "qfq_manifest_sha256": fold_audit["qfq_manifest_sha256"],
    }


def audit_report(report_path: Path) -> dict[str, Any]:
    report_path = _guard_development_path(report_path)
    _require(report_path.exists(), f"v6 report does not exist: {report_path}")
    report = _load_json(report_path)
    _require(report.get("version") == EXPERIMENT_VERSION, "unexpected v6 version")
    _require(
        report.get("dataset_status") == "development_viewed_not_holdout",
        "invalid dataset status",
    )
    _require(report.get("holdout_used") is False, "v6 report used Holdout")
    _require(report.get("production_eligible") is False, "v6 report is production eligible")

    design = report.get("preregistered_design")
    _require(isinstance(design, dict), "missing preregistered design")
    expected_design = {
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
    }
    _require(design == expected_design, "preregistered design drift")
    _require(report.get("portfolio_config") == PORTFOLIO_CONFIG, "portfolio config drift")

    inputs = report.get("input")
    _require(isinstance(inputs, dict), "missing input records")
    source_report_path = _resolve_record(
        inputs.get("source_report"), report_path, "source_report"
    )
    fold_report_path = _resolve_record(
        inputs.get("fold_report"), report_path, "fold_report"
    )
    config_path = _resolve_record(inputs.get("config"), report_path, "config")
    _resolve_record(
        inputs.get("v4_pairwise_code"),
        report_path,
        "v4_pairwise_code",
        expected_path=V4_PAIRWISE_CODE,
    )
    _resolve_record(
        inputs.get("backtest_engine"),
        report_path,
        "backtest_engine",
        expected_path=BACKTEST_ENGINE,
    )
    _resolve_record(
        inputs.get("signal_engine"),
        report_path,
        "signal_engine",
        expected_path=SIGNAL_ENGINE,
    )
    _resolve_record(
        inputs.get("experiment_code"),
        report_path,
        "experiment_code",
        expected_path=BASE_DIR / "long_history_pairwise_ranking_experiment.py",
    )

    source_audit = audit_source_report(source_report_path)
    fold_audit = audit_fold_report(fold_report_path, source_audit)
    _require(
        inputs.get("v5_audit") == _expected_v5_audit(source_audit, fold_audit),
        "embedded v5 audit summary mismatch",
    )
    loaded_folds, all_rows, integrity = _load_eligible_fold_rows(
        fold_report_path, fold_audit["_report_value"]
    )
    _require(
        len(all_rows) == int(fold_audit["dataset_candidates"]),
        "eligible fold artifacts do not cover the audited v5 dataset",
    )
    _require(report.get("integrity") == integrity, "candidate integrity summary mismatch")
    history_dir = _audited_history_dir(fold_audit["_report_value"])
    hydrated, history_audit = hydrate_causal_ranking_features(
        {"eligible_fold_candidates": all_rows}, history_dir=history_dir
    )
    _require(
        history_audit.get("history_manifest_sha256")
        == fold_audit["qfq_manifest_sha256"],
        "current QFQ manifest differs from v5 audit",
    )
    _require(inputs.get("qfq_history") == history_audit, "QFQ audit record mismatch")
    hydrated_by_id = {
        str(row["candidate_id"]): row for row in hydrated["eligible_fold_candidates"]
    }
    costs = _resolve_execution_config(load_config(config_path))
    _require(
        inputs.get("resolved_execution_sha256")
        == _sha256_bytes(_canonical_json(costs).encode("utf-8")),
        "resolved execution config mismatch",
    )

    reported_folds = report.get("folds")
    _require(isinstance(reported_folds, list), "missing v6 fold results")
    _require(
        [fold.get("fold_name") for fold in reported_folds]
        == list(EXPECTED_ELIGIBLE_FOLDS),
        "reported fold names differ from frozen fold set",
    )
    artifact_hashes: dict[str, str] = {}
    expected_fold_results: list[dict[str, Any]] = []
    stitched_rows: list[dict[str, Any]] = []
    stitched_scored: list[dict[str, Any]] = []
    stitched_scores: dict[str, float] = {}
    for loaded, reported in zip(loaded_folds, reported_folds, strict=True):
        fold_name = str(loaded["fold_name"])
        _require(reported.get("fold_name") == fold_name, f"fold order mismatch: {fold_name}")
        _require(
            reported.get("evaluation_window") == loaded["evaluation_window"],
            f"evaluation window mismatch: {fold_name}",
        )
        _require(
            reported.get("input_artifacts") == loaded["input_artifacts"],
            f"fold input artifact mismatch: {fold_name}",
        )
        train_rows = [
            hydrated_by_id[str(row["candidate_id"])] for row in loaded["rows"]["train"]
        ]
        evaluation_rows = [
            hydrated_by_id[str(row["candidate_id"])]
            for row in loaded["rows"]["evaluation"]
        ]
        model, training = _fit_training_rows(train_rows)
        expected_training = {**training, "model": _model_public(model)}
        _require(
            reported.get("training") == expected_training,
            f"training replay mismatch: {fold_name}",
        )
        scored_rows, model_scores, feature_audit = _score_rows(evaluation_rows, model)
        _require(
            reported.get("evaluation_feature_audit") == feature_audit,
            f"evaluation feature audit mismatch: {fold_name}",
        )
        scope_expected, hashes = _verify_scope(
            reported,
            evaluation_rows,
            scored_rows,
            model_scores,
            costs,
            report_path,
            fold_name,
        )
        artifact_hashes.update(hashes)
        expected_fold_results.append(
            {
                "fold_name": fold_name,
                "evaluation_window": loaded["evaluation_window"],
                "input_artifacts": loaded["input_artifacts"],
                "training": expected_training,
                "evaluation_feature_audit": feature_audit,
                **scope_expected,
            }
        )
        stitched_rows.extend(evaluation_rows)
        stitched_scored.extend(scored_rows)
        stitched_scores.update(model_scores)

    reported_stitched = report.get("stitched_oos")
    _require(isinstance(reported_stitched, dict), "missing stitched OOS result")
    _require(
        reported_stitched.get("fold_names") == list(EXPECTED_ELIGIBLE_FOLDS),
        "stitched fold set mismatch",
    )
    _require(
        reported_stitched.get("score_origin")
        == "each candidate uses only its own fold train model",
        "stitched score origin mismatch",
    )
    stitched_expected, hashes = _verify_scope(
        reported_stitched,
        stitched_rows,
        stitched_scored,
        stitched_scores,
        costs,
        report_path,
        "stitched_oos",
    )
    artifact_hashes.update(hashes)
    expected_stitched = {
        "fold_names": list(EXPECTED_ELIGIBLE_FOLDS),
        "score_origin": "each candidate uses only its own fold train model",
        **stitched_expected,
    }
    expected_screen = _research_screen(expected_fold_results, expected_stitched)
    _require(report.get("screen") == expected_screen, "research screen mismatch")
    expected_model_fitted = all(
        bool(fold["training"]["model_fitted"]) for fold in expected_fold_results
    )
    _require(report.get("model_fitted") is expected_model_fitted, "model_fitted mismatch")

    return {
        "report": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "source_report_sha256": source_audit["report_sha256"],
        "fold_report_sha256": fold_audit["report_sha256"],
        "eligible_folds": list(EXPECTED_ELIGIBLE_FOLDS),
        "artifact_sha256": artifact_hashes,
        "passes_research_screen": expected_screen["passes_research_screen"],
        "model_fitted": expected_model_fitted,
        "holdout_used": False,
        "production_eligible": False,
        "checks_passed": True,
        "_report_value": report,
    }


def _normalize_run_paths(value: Any, run_root: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_run_paths(item, run_root)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_normalize_run_paths(item, run_root) for item in value]
    if isinstance(value, str):
        try:
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(run_root):
                return str(Path("<RUN_ROOT>") / path.relative_to(run_root))
        except (OSError, ValueError):
            pass
    return value


def compare_runs(primary: dict[str, Any], verify: dict[str, Any]) -> dict[str, Any]:
    primary_artifacts = primary["artifact_sha256"]
    verify_artifacts = verify["artifact_sha256"]
    _require(
        set(primary_artifacts) == set(verify_artifacts),
        "v6 artifact sets differ across runs",
    )
    artifact_equal = {
        name: primary_artifacts[name] == verify_artifacts[name]
        for name in sorted(primary_artifacts)
    }
    _require(all(artifact_equal.values()), "v6 JSONL artifacts differ across runs")
    primary_value = _normalize_run_paths(
        primary["_report_value"], Path(primary["report"]).parent
    )
    verify_value = _normalize_run_paths(
        verify["_report_value"], Path(verify["report"]).parent
    )
    report_equal = primary_value == verify_value
    _require(report_equal, "normalized v6 reports differ across runs")
    return {
        "checked": True,
        "artifact_byte_identical": artifact_equal,
        "normalized_report_equal": report_equal,
        "checks_passed": True,
    }


def _strip_private(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_private(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_strip_private(item) for item in value]
    return value


def run_audit(primary_report: Path, verify_report: Path) -> dict[str, Any]:
    primary = audit_report(primary_report)
    verify = audit_report(verify_report)
    determinism = compare_runs(primary, verify)
    return _strip_private(
        {
            "version": VERSION,
            "audit_mode": "read_only_full_replay",
            "primary": primary,
            "verify": verify,
            "determinism": determinism,
            "holdout_used": False,
            "production_eligible": False,
            "passes_audit": True,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-report", type=Path, required=True)
    parser.add_argument("--verify-report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_audit(args.primary_report, args.verify_report)
    except Exception as exc:  # noqa: BLE001 - audit CLI must fail closed as JSON.
        print(
            json.dumps(
                {"version": VERSION, "passes_audit": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
