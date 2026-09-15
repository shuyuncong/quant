"""Read-only replay and determinism audit for two v6 mechanism diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from long_history_pairwise_ranking_audit import (
    audit_report as audit_v6_report,
)
from long_history_pairwise_ranking_audit import compare_runs as compare_v6_runs
from long_history_pairwise_ranking_diagnostic import (
    ARTIFACT_NAMES,
    DIAGNOSTIC_STATUS,
    _canonical_json,
    _sha256_file,
    compute_diagnostic,
)
from long_history_pairwise_ranking_diagnostic import (
    VERSION as DIAGNOSTIC_VERSION,
)

VERSION = "long_history_pairwise_ranking_diagnostic_audit.v1"
DIAGNOSTIC_CODE = BASE_DIR / "long_history_pairwise_ranking_diagnostic.py"


class PairwiseRankingDiagnosticAuditError(RuntimeError):
    """Raised when a diagnostic replay or deterministic comparison differs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PairwiseRankingDiagnosticAuditError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require(
        not any("holdout" in part.lower() for part in resolved.parts),
        f"Holdout paths are blocked during diagnostic audit: {resolved}",
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
    _require(isinstance(record, dict), f"missing record: {label}")
    raw_path = record.get("path")
    _require(isinstance(raw_path, str) and bool(raw_path), f"missing path: {label}")
    path = Path(raw_path)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    _require(path.exists(), f"path does not exist: {path}")
    if expected_path is not None:
        _require(path == expected_path.resolve(), f"unexpected path: {label}:{path}")
    _require(
        str(record.get("sha256", "")).lower() == _sha256_file(path),
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
    _require(report.get("holdout_used") is False, "diagnostic used Holdout")
    _require(
        report.get("production_eligible") is False,
        "diagnostic is marked production eligible",
    )
    inputs = report.get("input")
    _require(isinstance(inputs, dict), "diagnostic input record missing")
    v6_report_path = _resolve_record(
        inputs.get("v6_report"), report_path, "v6_report"
    )
    _resolve_record(
        inputs.get("diagnostic_code"),
        report_path,
        "diagnostic_code",
        expected_path=DIAGNOSTIC_CODE,
    )
    v6_audit = audit_v6_report(v6_report_path)
    expected_core, expected_artifacts = compute_diagnostic(
        v6_report_path, v6_audit=v6_audit
    )
    actual_core = {key: value for key, value in report.items() if key != "artifacts"}
    _require(actual_core == expected_core, "diagnostic report core mismatch")
    artifacts = report.get("artifacts")
    _require(isinstance(artifacts, dict), "diagnostic artifacts missing")
    _require(set(artifacts) == set(ARTIFACT_NAMES), "diagnostic artifact set drift")
    artifact_hashes: dict[str, str] = {}
    for name in ARTIFACT_NAMES:
        _, digest = _verify_artifact(
            artifacts.get(name),
            expected_artifacts[name],
            report_path,
            name,
        )
        artifact_hashes[f"{name}.jsonl"] = digest
    return {
        "report": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "v6_report": str(v6_report_path),
        "v6_report_sha256": _sha256_file(v6_report_path),
        "artifact_sha256": artifact_hashes,
        "checks_passed": True,
        "holdout_used": False,
        "production_eligible": False,
        "_report_value": report,
        "_v6_audit": v6_audit,
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
                return str(Path("<DIAGNOSTIC_ROOT>") / path.relative_to(run_root))
        except (OSError, ValueError):
            pass
    return value


def _semantic_report(report: dict[str, Any], run_root: Path) -> dict[str, Any]:
    value = json.loads(json.dumps(report))
    input_value = value.get("input")
    if isinstance(input_value, dict):
        v6_record = input_value.get("v6_report")
        if isinstance(v6_record, dict):
            v6_record["path"] = "<V6_REPORT>"
            v6_record.pop("sha256", None)
        v6_audit = input_value.get("v6_audit")
        if isinstance(v6_audit, dict):
            v6_audit.pop("report_sha256", None)
    return _normalize_run_paths(value, run_root)


def compare_runs(primary: dict[str, Any], verify: dict[str, Any]) -> dict[str, Any]:
    primary_hashes = primary["artifact_sha256"]
    verify_hashes = verify["artifact_sha256"]
    _require(
        set(primary_hashes) == set(verify_hashes),
        "diagnostic artifact sets differ",
    )
    artifacts_equal = {
        name: primary_hashes[name] == verify_hashes[name]
        for name in sorted(primary_hashes)
    }
    _require(all(artifacts_equal.values()), "diagnostic artifacts differ across runs")
    primary_semantic = _semantic_report(
        primary["_report_value"], Path(primary["report"]).parent
    )
    verify_semantic = _semantic_report(
        verify["_report_value"], Path(verify["report"]).parent
    )
    reports_equal = primary_semantic == verify_semantic
    _require(reports_equal, "normalized diagnostic reports differ")
    v6_determinism = compare_v6_runs(primary["_v6_audit"], verify["_v6_audit"])
    return {
        "checked": True,
        "artifact_byte_identical": artifacts_equal,
        "normalized_report_equal": reports_equal,
        "input_v6_determinism": v6_determinism,
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
    primary = audit_diagnostic_report(primary_report)
    verify = audit_diagnostic_report(verify_report)
    determinism = compare_runs(primary, verify)
    return _strip_private(
        {
            "version": VERSION,
            "audit_mode": "read_only_full_replay",
            "primary": primary,
            "verify": verify,
            "determinism": determinism,
            "diagnostic_status": DIAGNOSTIC_STATUS,
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
    except Exception as exc:  # noqa: BLE001 - audit CLI fails closed as JSON.
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
