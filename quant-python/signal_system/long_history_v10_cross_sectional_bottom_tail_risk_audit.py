"""Read-only full-replay audit for two v10a bottom-tail risk runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import long_history_v9_bottom_tail_risk_audit as v9audit
import long_history_v10_cross_sectional_bottom_tail_risk_experiment as experiment

VERSION = "long_history_v10_cross_sectional_bottom_tail_risk_audit.v1"


class CrossSectionalBottomTailRiskAuditError(RuntimeError):
    """Raised when a v10a report or deterministic replay differs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CrossSectionalBottomTailRiskAuditError(message)


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
        raise CrossSectionalBottomTailRiskAuditError(
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
        raise CrossSectionalBottomTailRiskAuditError(
            f"cannot read JSONL: {path}: {exc}"
        ) from exc
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


def _audit_parent_v9a(path: Path) -> dict[str, Any]:
    report = experiment._load_parent_v9a_report(path)
    return {
        "version": report["version"],
        "report_sha256": _sha256_file(path),
        "failed_checks": list(experiment.EXPECTED_V9A_FAILED_CHECKS),
        "checks_passed": True,
    }


def _independent_partition(snapshot: dict[str, Any]) -> dict[str, Any]:
    rows = snapshot.get("_all_joined_rows")
    _require(isinstance(rows, list), "replay joined rows missing")
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["entry_day"]), str(row["signal_type"]))
        groups.setdefault(key, []).append(row)
    varying_execution = {name: 0 for name in experiment.RESERVED_MARKET_REGIME_FACTORS}
    same_signal_day_violations = {
        name: 0 for name in experiment.RESERVED_MARKET_REGIME_FACTORS
    }
    mixed_signal_day_buckets = 0
    for key in sorted(groups):
        group = groups[key]
        signal_days = {str(row["signal_day"]) for row in group}
        mixed_signal_day_buckets += len(signal_days) > 1
        for name in experiment.RESERVED_MARKET_REGIME_FACTORS:
            values: set[tuple[str, float | None]] = set()
            for row in group:
                raw = row.get("_v10_features")
                _require(isinstance(raw, dict), f"joined factor payload missing: {key}")
                try:
                    number = float(raw.get(name))
                except (TypeError, ValueError):
                    values.add(("missing", None))
                else:
                    values.add(
                        ("finite", round(number, 12))
                        if math.isfinite(number)
                        else ("missing", None)
                    )
            varying_execution[name] += len(values) > 1
            by_signal_day: dict[str, list[dict[str, Any]]] = {}
            for row in group:
                by_signal_day.setdefault(str(row["signal_day"]), []).append(row)
            for subgroup in by_signal_day.values():
                subgroup_values: set[tuple[str, float | None]] = set()
                for row in subgroup:
                    raw = row["_v10_features"]
                    try:
                        number = float(raw.get(name))
                    except (TypeError, ValueError):
                        subgroup_values.add(("missing", None))
                    else:
                        subgroup_values.add(
                            ("finite", round(number, 12))
                            if math.isfinite(number)
                            else ("missing", None)
                        )
                same_signal_day_violations[name] += len(subgroup_values) > 1
    same_signal_day_constant = not any(same_signal_day_violations.values())
    result = {
        "bucket_key": ["entry_day", "signal_type"],
        "candidate_count": len(rows),
        "bucket_count": len(groups),
        "single_signal_day_buckets": len(groups) - mixed_signal_day_buckets,
        "mixed_signal_day_buckets": mixed_signal_day_buckets,
        "reserved_factor_names": list(experiment.RESERVED_MARKET_REGIME_FACTORS),
        "varying_execution_buckets_by_factor": varying_execution,
        "same_signal_day_violations_by_factor": same_signal_day_violations,
        "all_reserved_factors_constant_within_signal_day": same_signal_day_constant,
        "execution_bucket_variation_only_from_mixed_signal_days": (
            same_signal_day_constant
        ),
        "ranking_use_blocked_to_avoid_signal_timing_as_stock_selection": True,
        "checked_without_outcomes": True,
    }
    _require(
        result["all_reserved_factors_constant_within_signal_day"] is True,
        "independent market-regime same-signal-day check failed",
    )
    return result


def _independent_risk_targets(
    snapshot: dict[str, Any], report: dict[str, Any]
) -> dict[str, Any]:
    try:
        return v9audit._audit_independent_risk_targets(snapshot, report)
    except Exception as exc:
        raise CrossSectionalBottomTailRiskAuditError(str(exc)) from exc


def _independent_screen(report: dict[str, Any]) -> dict[str, Any]:
    try:
        return v9audit._independent_screen(report)
    except Exception as exc:
        raise CrossSectionalBottomTailRiskAuditError(str(exc)) from exc


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
        "report selected factors from outcomes",
    )
    design = report.get("preregistered_design")
    _require(isinstance(design, dict), "preregistered design missing")
    _require(design.get("risk_fraction") == 0.20, "risk fraction drift")
    _require(
        design.get("tail_count_rule") == experiment.TAIL_COUNT_RULE,
        "tail count rule drift",
    )
    _require(
        design.get("raw_factor_names") == list(experiment.FACTOR_NAMES),
        "cross-sectional factor names drift",
    )
    _require(
        design.get("model_feature_names") == list(experiment.MODEL_FEATURE_NAMES),
        "model feature names drift",
    )
    _require(
        design.get("reserved_market_regime_factors")
        == list(experiment.RESERVED_MARKET_REGIME_FACTORS),
        "reserved market factor names drift",
    )
    _require(
        design.get("all_v10_factor_names") == list(experiment.ALL_V10_FACTOR_NAMES),
        "complete v10 factor names drift",
    )
    _require(design.get("reservation_is_outcome_blind") is True, "reservation drift")
    _require(
        design.get("v9a_research_screen_reused_without_change") is True,
        "v9a screen reuse disabled",
    )
    _require(
        design.get("bucket_class_weighting") == "risk=0.5, safe=0.5",
        "class weighting drift",
    )
    _require(
        design.get("each_bucket_total_training_weight") == 1.0,
        "bucket weight drift",
    )
    _require(design.get("l2") == 1.0, "L2 drift")
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
    v9a_report_path = _resolve_record(
        inputs.get("v9a_parent_report"), report_path, "v9a_parent_report"
    )
    _require(
        _sha256_file(factor_report_path) == experiment.V10_FACTOR_REPORT_SHA256,
        "v10 factor report anchor drift",
    )
    parent_v9a = _audit_parent_v9a(v9a_report_path)
    expected_code_paths = {
        "experiment_code": Path(experiment.__file__).resolve(),
        "audit_code": Path(__file__).resolve(),
        "v10_preflight_code": experiment.V10_PREFLIGHT_CODE,
        "v10_preflight_audit_code": experiment.V10_PREFLIGHT_AUDIT_CODE,
        "v9a_target_screen_core_code": experiment.V9A_TARGET_SCREEN_CORE_CODE,
        "v9a_independent_audit_code": experiment.V9A_INDEPENDENT_AUDIT_CODE,
        "v7a_fold_model_core_code": experiment.V7A_FOLD_MODEL_CORE_CODE,
    }
    for label, expected in expected_code_paths.items():
        actual = _resolve_record(inputs.get(label), report_path, label)
        _require(actual == expected.resolve(), f"unexpected code path: {label}")
    experiment._assert_frozen_cores()

    records, reported_rows = _reported_artifacts(report, report_path)
    replay = experiment.build_snapshot(
        factor_report_path, fold_report_path, v9a_report_path
    )
    independent_partition = _independent_partition(replay)
    reported_partition = report.get("integrity", {}).get("market_regime_structure")
    _require(
        reported_partition == independent_partition,
        "independent market-regime factor partition differs",
    )
    independent_risk_target = _independent_risk_targets(replay, report)
    independent_screen = _independent_screen(report)
    _require(
        report.get("screen") == independent_screen,
        "independent v9a research screen differs",
    )
    _require(
        set(reported_rows) == set(replay["artifact_rows"]),
        "artifact set differs from replay",
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
        v9a_report_path,
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
        "parent_v9a": parent_v9a,
        "independent_factor_partition": {
            **independent_partition,
            "checks_passed": True,
        },
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
        "audit_mode": "read_only_full_v10a_bottom_tail_risk_model_replay",
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
        "parent_v9a_failure_anchored": True,
        "policy": {
            "read_only": True,
            "full_model_replay": True,
            "independent_factor_partition_checked": True,
            "reserved_market_factors_same_signal_day_constancy_checked": True,
            "mixed_signal_day_market_timing_blocked_from_stock_ranking": True,
            "independent_risk_target_and_weights_checked": True,
            "independent_research_screen_checked": True,
            "v9a_target_model_weights_and_screen_reused_without_change": True,
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
