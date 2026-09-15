"""Read-only full-replay audit for two v7a Top-4 tail diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from long_history_v7_top4_ranking_audit import audit_report as audit_v7_report
from long_history_v7_top4_ranking_diagnostic import (
    ARTIFACT_NAMES,
    DIAGNOSTIC_STATUS,
    _canonical_json,
    _sha256_file,
    compute_diagnostic,
)
from long_history_v7_top4_ranking_diagnostic import (
    VERSION as DIAGNOSTIC_VERSION,
)

VERSION = "long_history_v7_top4_ranking_diagnostic_audit.v1"
DIAGNOSTIC_CODE = BASE_DIR / "long_history_v7_top4_ranking_diagnostic.py"


class Top4RankingDiagnosticAuditError(RuntimeError):
    """Raised when diagnostic replay or deterministic comparison differs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Top4RankingDiagnosticAuditError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require(
        not any("holdout" in part.lower() for part in resolved.parts),
        f"Holdout paths are blocked during diagnostic audit: {resolved}",
    )
    return resolved


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Top4RankingDiagnosticAuditError(
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
        raise Top4RankingDiagnosticAuditError(
            f"cannot read JSONL: {path}: {exc}"
        ) from exc
    return rows


def _resolve_record(
    record: Any,
    report_path: Path,
    label: str,
    *,
    expected_path: Path | None = None,
) -> Path:
    _require(isinstance(record, dict), f"missing record: {label}")
    raw = record.get("path")
    _require(isinstance(raw, str) and bool(raw), f"missing path: {label}")
    path = Path(raw)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"path does not exist: {path}")
    if expected_path is not None:
        _require(path == expected_path.resolve(), f"unexpected path: {label}:{path}")
    _require(
        str(record.get("sha256", "")) == _sha256_file(path),
        f"hash mismatch: {label}",
    )
    return path


def _verify_artifact(
    record: Any,
    expected_rows: list[dict[str, Any]],
    report_path: Path,
    label: str,
) -> tuple[Path, str]:
    path = _resolve_record(record, report_path, label)
    actual_rows = _load_jsonl(path)
    _require(
        isinstance(record, dict) and record.get("rows") == len(actual_rows),
        f"row count mismatch: {label}",
    )
    _require(
        [_canonical_json(row) for row in actual_rows]
        == [_canonical_json(row) for row in expected_rows],
        f"content mismatch: {label}",
    )
    return path, _sha256_file(path)


def audit_diagnostic_report(report_path: Path) -> dict[str, Any]:
    report_path = _guard_development_path(report_path)
    _require(report_path.exists(), f"diagnostic report does not exist: {report_path}")
    report = _load_json(report_path)
    _require(report.get("version") == DIAGNOSTIC_VERSION, "diagnostic version drift")
    _require(
        report.get("diagnostic_status") == DIAGNOSTIC_STATUS,
        "diagnostic status drift",
    )
    required_false = (
        "model_fitted",
        "hyperparameters_selected",
        "portfolio_replayed",
        "holdout_used",
        "production_eligible",
    )
    for field in required_false:
        _require(report.get(field) is False, f"diagnostic policy drift: {field}")
    _require(report.get("outcomes_read") is True, "diagnostic outcome-read flag drift")
    _require(
        report.get("input_model_replayed") is True,
        "diagnostic input-model replay flag drift",
    )
    policy = report.get("interpretation_policy")
    _require(isinstance(policy, dict), "interpretation policy missing")
    _require(policy.get("changes_v7a_screen") is False, "diagnostic changes v7a screen")
    _require(policy.get("selects_new_factors") is False, "diagnostic selects factors")
    _require(policy.get("authorizes_v7b") is False, "diagnostic authorizes v7b")

    inputs = report.get("input")
    _require(isinstance(inputs, dict), "diagnostic input record missing")
    v7_report_path = _resolve_record(inputs.get("v7_report"), report_path, "v7_report")
    _resolve_record(
        inputs.get("diagnostic_code"),
        report_path,
        "diagnostic_code",
        expected_path=DIAGNOSTIC_CODE,
    )
    _resolve_record(
        inputs.get("audit_code"),
        report_path,
        "audit_code",
        expected_path=Path(__file__).resolve(),
    )
    v7_audit = audit_v7_report(v7_report_path)
    expected_core, expected_artifacts = compute_diagnostic(
        v7_report_path, v7_audit=v7_audit
    )
    actual_core = {key: value for key, value in report.items() if key != "artifacts"}
    _require(actual_core == expected_core, "diagnostic report core mismatch")
    artifacts = report.get("artifacts")
    _require(isinstance(artifacts, dict), "diagnostic artifacts missing")
    _require(set(artifacts) == set(ARTIFACT_NAMES), "diagnostic artifact set drift")
    artifact_hashes: dict[str, str] = {}
    artifact_paths: dict[str, Path] = {}
    for name in ARTIFACT_NAMES:
        path, digest = _verify_artifact(
            artifacts.get(name), expected_artifacts[name], report_path, name
        )
        artifact_hashes[f"{name}.jsonl"] = digest
        artifact_paths[f"{name}.jsonl"] = path
    return {
        "report": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "v7_report": str(v7_report_path),
        "v7_report_sha256": _sha256_file(v7_report_path),
        "artifact_sha256": artifact_hashes,
        "checks_passed": True,
        "diagnostic_status": DIAGNOSTIC_STATUS,
        "changes_v7a_screen": False,
        "selects_new_factors": False,
        "authorizes_v7b": False,
        "holdout_used": False,
        "production_eligible": False,
        "_report_value": report,
        "_artifact_paths": artifact_paths,
        "_v7_audit": v7_audit,
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


def _semantic_diagnostic_report(
    report: dict[str, Any], run_root: Path
) -> dict[str, Any]:
    value = json.loads(json.dumps(report))
    inputs = value.get("input")
    if isinstance(inputs, dict):
        v7_record = inputs.get("v7_report")
        if isinstance(v7_record, dict):
            v7_record["path"] = "<V7_REPORT>"
            v7_record.pop("sha256", None)
        v7_audit = inputs.get("v7_audit")
        if isinstance(v7_audit, dict):
            v7_audit.pop("report_sha256", None)
    return _normalize_run_paths(value, run_root)


def _compare_input_v7(
    primary: dict[str, Any], verify: dict[str, Any]
) -> dict[str, Any]:
    primary_audit = primary["_v7_audit"]
    verify_audit = verify["_v7_audit"]
    primary_paths = primary_audit["_artifact_paths"]
    verify_paths = verify_audit["_artifact_paths"]
    _require(set(primary_paths) == set(verify_paths), "input v7a artifact sets differ")
    checks: list[dict[str, Any]] = []
    for name in sorted(primary_paths):
        primary_path = primary_paths[name]
        verify_path = verify_paths[name]
        identical = primary_path.read_bytes() == verify_path.read_bytes()
        checks.append(
            {
                "name": name,
                "primary_sha256": _sha256_file(primary_path),
                "verify_sha256": _sha256_file(verify_path),
                "artifact_byte_identical": identical,
            }
        )
    all_identical = all(row["artifact_byte_identical"] for row in checks)
    primary_report = primary_audit["_report_value"]
    verify_report = verify_audit["_report_value"]
    normalized_primary = _normalize_run_paths(
        primary_report, Path(primary["v7_report"]).parent
    )
    normalized_verify = _normalize_run_paths(
        verify_report, Path(verify["v7_report"]).parent
    )
    normalized_equal = _canonical_json(normalized_primary) == _canonical_json(
        normalized_verify
    )
    _require(all_identical, "input v7a artifacts differ across runs")
    _require(normalized_equal, "input v7a normalized reports differ")
    return {
        "checked": True,
        "artifact_count": len(checks),
        "artifact_determinism": checks,
        "all_artifacts_byte_identical": all_identical,
        "normalized_report_equal": normalized_equal,
        "checks_passed": True,
    }


def compare_runs(primary: dict[str, Any], verify: dict[str, Any]) -> dict[str, Any]:
    primary_paths = primary["_artifact_paths"]
    verify_paths = verify["_artifact_paths"]
    _require(set(primary_paths) == set(verify_paths), "diagnostic artifact sets differ")
    artifact_checks: list[dict[str, Any]] = []
    for name in sorted(primary_paths):
        primary_path = primary_paths[name]
        verify_path = verify_paths[name]
        identical = primary_path.read_bytes() == verify_path.read_bytes()
        artifact_checks.append(
            {
                "name": name,
                "primary_sha256": _sha256_file(primary_path),
                "verify_sha256": _sha256_file(verify_path),
                "artifact_byte_identical": identical,
            }
        )
    all_identical = all(row["artifact_byte_identical"] for row in artifact_checks)
    primary_semantic = _semantic_diagnostic_report(
        primary["_report_value"], Path(primary["report"]).parent
    )
    verify_semantic = _semantic_diagnostic_report(
        verify["_report_value"], Path(verify["report"]).parent
    )
    reports_equal = _canonical_json(primary_semantic) == _canonical_json(
        verify_semantic
    )
    _require(all_identical, "diagnostic artifacts differ across runs")
    _require(reports_equal, "normalized diagnostic reports differ")
    return {
        "checked": True,
        "artifact_count": len(artifact_checks),
        "artifact_determinism": artifact_checks,
        "all_artifacts_byte_identical": all_identical,
        "normalized_report_equal": reports_equal,
        "input_v7_determinism": _compare_input_v7(primary, verify),
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
    primary_report = _guard_development_path(primary_report)
    verify_report = _guard_development_path(verify_report)
    _require(primary_report != verify_report, "primary and verify reports must differ")
    primary = audit_diagnostic_report(primary_report)
    verify = audit_diagnostic_report(verify_report)
    determinism = compare_runs(primary, verify)
    return _strip_private(
        {
            "version": VERSION,
            "audit_mode": "read_only_full_diagnostic_and_input_model_replay",
            "primary": primary,
            "verify": verify,
            "determinism": determinism,
            "diagnostic_status": DIAGNOSTIC_STATUS,
            "changes_v7a_screen": False,
            "selects_new_factors": False,
            "authorizes_v7b": False,
            "holdout_used": False,
            "production_eligible": False,
            "policy": {
                "read_only": True,
                "full_diagnostic_replay": True,
                "input_full_model_replay": True,
                "portfolio_replayed": False,
                "network_used": False,
                "database_used": False,
                "sql_executed": False,
                "holdout_used": False,
                "production_eligible": False,
            },
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
    except Exception as exc:  # noqa: BLE001 - audit CLI fails closed as JSON.
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
