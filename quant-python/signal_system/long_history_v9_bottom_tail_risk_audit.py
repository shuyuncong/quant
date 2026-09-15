"""Read-only full-replay audit for two v9a bottom-tail risk runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import long_history_v9_bottom_tail_risk_experiment as experiment

VERSION = "long_history_v9_bottom_tail_risk_audit.v1"


class BottomTailRiskAuditError(RuntimeError):
    """Raised when a v9a report or deterministic replay differs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BottomTailRiskAuditError(message)


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
        raise BottomTailRiskAuditError(f"cannot read JSON: {path}: {exc}") from exc
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
        raise BottomTailRiskAuditError(f"cannot read JSONL: {path}: {exc}") from exc
    return rows


def _resolve_record(record: Any, report_path: Path, label: str) -> Path:
    _require(isinstance(record, dict), f"missing record: {label}")
    raw = record.get("path")
    _require(isinstance(raw, str) and bool(raw), f"missing path: {label}")
    path = Path(raw)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"missing file: {path}")
    _require(
        str(record.get("sha256", "")) == _sha256_file(path),
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
        [fold.get("fold_name") for fold in folds]
        == list(experiment.EXPECTED_ELIGIBLE_FOLDS),
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
            set(artifacts) == set(experiment.ARTIFACT_NAMES),
            f"artifact names differ: {scope_name}",
        )
        for artifact_name in experiment.ARTIFACT_NAMES:
            key = f"{scope_name}/{artifact_name}"
            record = artifacts[artifact_name]
            path = _resolve_record(record, report_path, key)
            loaded = _load_jsonl(path)
            _require(record.get("rows") == len(loaded), f"row count differs: {key}")
            if artifact_name == "candidate_risk_scores":
                for row in loaded:
                    _require(
                        not (set(row) & experiment.OUTCOME_FIELDS),
                        f"candidate risk score leaks outcome fields: {key}",
                    )
                    _require(
                        set(row.get("rank_features", {}))
                        == set(experiment.MODEL_FEATURE_NAMES),
                        f"candidate risk feature schema differs: {key}",
                    )
            records[key] = {
                "path": str(path),
                "rows": len(loaded),
                "sha256": _sha256_file(path),
            }
            rows[key] = loaded
    return records, rows


def _independent_outcome(row: dict[str, Any]) -> float:
    raw = row.get("trade_pnl_pct", row.get("pnl_pct"))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise BottomTailRiskAuditError(
            f"missing outcome for candidate {row.get('candidate_id')}"
        ) from exc
    _require(
        math.isfinite(value),
        f"non-finite outcome for candidate {row.get('candidate_id')}",
    )
    return value


def _independent_risk_count(bucket_size: int) -> int:
    _require(
        bucket_size >= experiment.MIN_BUCKET_CANDIDATES,
        "independent risk count received an ineligible bucket",
    )
    return max(1, math.ceil(bucket_size * 0.20))


def _independent_risk_target_stats(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Recompute candidate labels and class weights without experiment helpers."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["entry_day"]), str(row["signal_type"]))
        groups.setdefault(key, []).append(row)
    sample_weights: list[float] = []
    eligible_buckets = 0
    candidates_in_eligible_buckets = 0
    risk_labels = 0
    safe_labels = 0
    boundary_tie_buckets = 0
    for key in sorted(groups):
        group = groups[key]
        if len(group) < experiment.MIN_BUCKET_CANDIDATES:
            continue
        eligible_buckets += 1
        candidates_in_eligible_buckets += len(group)
        ordered = sorted(
            group,
            key=lambda row: (
                _independent_outcome(row),
                str(row["candidate_id"]),
            ),
        )
        tail_count = _independent_risk_count(len(group))
        if (
            abs(
                _independent_outcome(ordered[tail_count - 1])
                - _independent_outcome(ordered[tail_count])
            )
            <= 1e-12
        ):
            boundary_tie_buckets += 1
        risk_weight = 0.5 / tail_count
        safe_weight = 0.5 / (len(group) - tail_count)
        bucket_weights = [risk_weight] * tail_count + [safe_weight] * (
            len(group) - tail_count
        )
        _require(
            math.isclose(math.fsum(bucket_weights), 1.0, rel_tol=0.0, abs_tol=1e-12),
            f"independent bucket weight does not sum to one: {key}",
        )
        _require(
            math.isclose(
                math.fsum(bucket_weights[:tail_count]),
                0.5,
                rel_tol=0.0,
                abs_tol=1e-12,
            ),
            f"independent risk class weight differs: {key}",
        )
        _require(
            math.isclose(
                math.fsum(bucket_weights[tail_count:]),
                0.5,
                rel_tol=0.0,
                abs_tol=1e-12,
            ),
            f"independent safe class weight differs: {key}",
        )
        risk_labels += tail_count
        safe_labels += len(group) - tail_count
        sample_weights.extend(bucket_weights)
    return {
        "eligible_buckets": eligible_buckets,
        "candidates_in_eligible_buckets": candidates_in_eligible_buckets,
        "candidate_samples": len(sample_weights),
        "risk_labels": risk_labels,
        "safe_labels": safe_labels,
        "boundary_tie_buckets": boundary_tie_buckets,
        "risk_fraction": 0.20,
        "tail_count_rule": experiment.TAIL_COUNT_RULE,
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
    }


def _audit_independent_risk_targets(
    snapshot: dict[str, Any], report: dict[str, Any]
) -> dict[str, Any]:
    training_rows = snapshot.get("_training_rows_by_fold")
    _require(isinstance(training_rows, dict), "replay training rows are missing")
    folds = report.get("folds")
    _require(isinstance(folds, list), "report folds are missing")
    report_by_fold = {
        str(fold.get("fold_name")): fold for fold in folds if isinstance(fold, dict)
    }
    fold_checks: list[dict[str, Any]] = []
    for fold_name in experiment.EXPECTED_ELIGIBLE_FOLDS:
        rows = training_rows.get(fold_name)
        _require(isinstance(rows, list), f"missing replay training rows: {fold_name}")
        fold = report_by_fold.get(fold_name)
        _require(isinstance(fold, dict), f"missing report fold: {fold_name}")
        training = fold.get("training")
        _require(isinstance(training, dict), f"missing training audit: {fold_name}")
        reported = training.get("risk_target")
        _require(isinstance(reported, dict), f"missing risk target: {fold_name}")
        independent = _independent_risk_target_stats(rows)
        _require(
            reported == independent,
            f"independent risk target or weight differs: {fold_name}",
        )
        fold_checks.append(
            {
                "fold_name": fold_name,
                "eligible_buckets": independent["eligible_buckets"],
                "risk_labels": independent["risk_labels"],
                "safe_labels": independent["safe_labels"],
                "boundary_tie_buckets": independent["boundary_tie_buckets"],
                "objective_weight_sum": independent["objective_weight_sum"],
                "each_bucket_weight_sum_equals_one": True,
                "each_bucket_class_weights_equal_half": True,
                "checks_passed": True,
            }
        )
    return {
        "target": "lowest ceil(bucket_size * 0.20) candidates",
        "implementation": "independent_audit_recomputation",
        "folds": fold_checks,
        "checks_passed": True,
    }


def _independent_screen(report: dict[str, Any]) -> dict[str, Any]:
    folds = report.get("folds")
    stitched = report.get("stitched_oos")
    _require(isinstance(folds, list), "folds missing for independent screen")
    _require(isinstance(stitched, dict), "stitched missing for independent screen")
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
        == list(experiment.EXPECTED_ELIGIBLE_FOLDS),
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


def _audit_parent_v8a(path: Path) -> dict[str, Any]:
    _require(
        _sha256_file(path) == experiment.V8A_PARENT_REPORT_SHA256,
        "frozen v8a parent report hash drift",
    )
    report = _load_json(path)
    _require(report.get("version") == experiment.v8a.VERSION, "v8a version drift")
    _require(report.get("passes_research_screen") is False, "v8a did not fail")
    _require(report.get("holdout_used") is False, "v8a used Holdout")
    _require(report.get("portfolio_replayed") is False, "v8a replayed portfolio")
    _require(report.get("production_eligible") is False, "v8a is production eligible")
    screen = report.get("screen")
    checks = screen.get("checks") if isinstance(screen, dict) else None
    _require(isinstance(checks, dict), "v8a screen missing")
    failed = tuple(sorted(name for name, passed in checks.items() if passed is False))
    _require(
        failed == tuple(sorted(experiment.EXPECTED_V8A_FAILED_CHECKS)),
        "v8a failed checks drift",
    )
    return {
        "version": report["version"],
        "report_sha256": _sha256_file(path),
        "failed_checks": list(experiment.EXPECTED_V8A_FAILED_CHECKS),
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
    _require(report.get("production_eligible") is False, "production eligible")
    _require(report.get("holdout_used") is False, "report used Holdout")
    _require(report.get("portfolio_replayed") is False, "report replayed portfolio")
    _require(
        report.get("hyperparameters_selected") is False,
        "report selected hyperparameters",
    )
    _require(
        report.get("factor_selection_performed") is False,
        "report selected factors",
    )
    design = report.get("preregistered_design")
    _require(isinstance(design, dict), "preregistered design missing")
    _require(design.get("risk_fraction") == 0.20, "risk fraction drift")
    _require(
        design.get("tail_count_rule") == experiment.TAIL_COUNT_RULE,
        "tail count rule drift",
    )
    _require(
        design.get("bucket_class_weighting") == "risk=0.5, safe=0.5",
        "class weighting drift",
    )
    _require(
        design.get("each_bucket_total_training_weight") == 1.0,
        "bucket weight drift",
    )
    _require(design.get("factor_selection") is False, "factor selection enabled")
    _require(design.get("portfolio_replay") is False, "portfolio replay enabled")

    inputs = report.get("input")
    _require(isinstance(inputs, dict), "input section missing")
    factor_report_path = _resolve_record(
        inputs.get("factor_report"), report_path, "factor_report"
    )
    fold_report_path = _resolve_record(
        inputs.get("fold_report"), report_path, "fold_report"
    )
    v8a_report_path = _resolve_record(
        inputs.get("v8a_parent_report"), report_path, "v8a_parent_report"
    )
    parent_v8a = _audit_parent_v8a(v8a_report_path)
    expected_code_paths = {
        "experiment_code": Path(experiment.__file__).resolve(),
        "audit_code": Path(__file__).resolve(),
        "v7a_feature_model_core_code": experiment.V7A_FEATURE_MODEL_CORE_CODE,
        "v8a_parent_code": experiment.V8A_PARENT_CODE,
        "factor_preflight_code": experiment.FACTOR_PREFLIGHT_CODE,
        "factor_preflight_audit_code": experiment.FACTOR_PREFLIGHT_AUDIT_CODE,
        "v5_audit_code": experiment.V5_AUDIT_CODE,
        "fold_loader_code": experiment.FOLD_LOADER_CODE,
    }
    for label, expected in expected_code_paths.items():
        actual = _resolve_record(inputs.get(label), report_path, label)
        _require(actual == expected.resolve(), f"unexpected code path: {label}")
    _require(
        _sha256_file(experiment.V7A_FEATURE_MODEL_CORE_CODE)
        == experiment.V7A_FEATURE_MODEL_CORE_SHA256,
        "v7a feature/model core hash drift",
    )
    _require(
        _sha256_file(experiment.V8A_PARENT_CODE) == experiment.V8A_PARENT_CODE_SHA256,
        "v8a parent code hash drift",
    )

    records, reported_rows = _reported_artifacts(report, report_path)
    replay = experiment.build_snapshot(
        factor_report_path, fold_report_path, v8a_report_path
    )
    independent_risk_target = _audit_independent_risk_targets(replay, report)
    independent_screen = _independent_screen(report)
    _require(
        report.get("screen") == independent_screen,
        "independent research screen differs",
    )
    _require(
        set(reported_rows) == set(replay["artifact_rows"]),
        "artifact set differs from replay",
    )
    for key in sorted(reported_rows):
        _require(
            _canonical_json(reported_rows[key])
            == _canonical_json(replay["artifact_rows"][key]),
            f"full replay differs: {key}",
        )
    expected_report = experiment.assemble_report(
        replay,
        factor_report_path,
        fold_report_path,
        v8a_report_path,
        records,
    )
    _require(
        _canonical_json(report) == _canonical_json(expected_report),
        "report differs from full replay",
    )
    return {
        "report": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "passes_research_screen": report["passes_research_screen"],
        "model_fitted": report["model_fitted"],
        "artifact_sha256": {
            key: record["sha256"] for key, record in sorted(records.items())
        },
        "parent_v8a": parent_v8a,
        "independent_risk_target": independent_risk_target,
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
        "audit_mode": "read_only_full_bottom_tail_risk_model_replay",
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
        "input_factor_preflight_replayed": True,
        "parent_v8a_failure_anchored": True,
        "policy": {
            "read_only": True,
            "full_model_replay": True,
            "independent_risk_target_and_weights_checked": True,
            "independent_research_screen_checked": True,
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
