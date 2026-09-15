"""Read-only full-replay audit for two v7a Top-4 ranking runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import long_history_v7_top4_ranking_experiment as experiment

VERSION = "long_history_v7_top4_ranking_audit.v1"


class Top4RankingAuditError(RuntimeError):
    """Raised when a v7a report or deterministic replay differs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Top4RankingAuditError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise Top4RankingAuditError(f"Holdout path is blocked: {resolved}")
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
        raise Top4RankingAuditError(f"cannot read JSON: {path}: {exc}") from exc
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
        raise Top4RankingAuditError(f"cannot read JSONL: {path}: {exc}") from exc
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
        artifact_value = scope.get("artifacts")
        _require(isinstance(artifact_value, dict), f"artifacts missing: {scope_name}")
        _require(
            set(artifact_value) == set(experiment.ARTIFACT_NAMES),
            f"artifact names differ: {scope_name}",
        )
        for artifact_name in experiment.ARTIFACT_NAMES:
            key = f"{scope_name}/{artifact_name}"
            record = artifact_value[artifact_name]
            path = _resolve_record(record, report_path, key)
            loaded = _load_jsonl(path)
            _require(record.get("rows") == len(loaded), f"row count differs: {key}")
            if artifact_name == "candidate_scores":
                for row in loaded:
                    _require(
                        not (set(row) & experiment.OUTCOME_FIELDS),
                        f"candidate score leaks outcome fields: {key}",
                    )
                    _require(
                        set(row.get("rank_features", {}))
                        == set(experiment.MODEL_FEATURE_NAMES),
                        f"candidate score feature schema differs: {key}",
                    )
            records[key] = {
                "path": str(path),
                "rows": len(loaded),
                "sha256": _sha256_file(path),
            }
            rows[key] = loaded
    return records, rows


def audit_report(report_path: Path) -> dict[str, Any]:
    report_path = _guard_development_path(report_path)
    _require(
        report_path.exists() and report_path.is_file(), f"missing report: {report_path}"
    )
    report = _load_json(report_path)
    _require(
        report.get("version") == experiment.VERSION, "unexpected experiment version"
    )
    _require(
        report.get("dataset_status") == experiment.DATASET_STATUS,
        "unexpected dataset_status",
    )
    _require(
        report.get("production_eligible") is False, "report is production eligible"
    )
    _require(report.get("holdout_used") is False, "report used Holdout")
    _require(
        report.get("hyperparameters_selected") is False,
        "report selected hyperparameters",
    )
    _require(report.get("portfolio_replayed") is False, "v7a replayed a portfolio")

    input_value = report.get("input")
    _require(isinstance(input_value, dict), "input section missing")
    factor_report_path = _resolve_record(
        input_value.get("factor_report"), report_path, "factor_report"
    )
    fold_report_path = _resolve_record(
        input_value.get("fold_report"), report_path, "fold_report"
    )
    expected_code_paths = {
        "experiment_code": Path(experiment.__file__).resolve(),
        "audit_code": Path(__file__).resolve(),
        "factor_preflight_code": experiment.FACTOR_PREFLIGHT_CODE.resolve(),
        "factor_preflight_audit_code": experiment.FACTOR_PREFLIGHT_AUDIT_CODE.resolve(),
        "v5_audit_code": experiment.V5_AUDIT_CODE.resolve(),
        "fold_loader_code": experiment.FOLD_LOADER_CODE.resolve(),
    }
    for label, expected in expected_code_paths.items():
        actual = _resolve_record(input_value.get(label), report_path, label)
        _require(actual == expected, f"unexpected code path: {label}")

    records, reported_rows = _reported_artifacts(report, report_path)
    replay = experiment.build_snapshot(factor_report_path, fold_report_path)
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
        replay, factor_report_path, fold_report_path, records
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
        "audit_mode": "read_only_full_model_replay",
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
        "policy": {
            "read_only": True,
            "full_model_replay": True,
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
    except Exception as exc:  # noqa: BLE001 - CLI fails closed as one JSON object.
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
