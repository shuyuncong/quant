"""Read-only full-replay audit for two v8a gap-weighted Top-4 runs."""

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

import long_history_v8_gap_weighted_top4_ranking_experiment as experiment

VERSION = "long_history_v8_gap_weighted_top4_ranking_audit.v1"


class GapWeightedTop4AuditError(RuntimeError):
    """Raised when a v8a report or deterministic replay differs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GapWeightedTop4AuditError(message)


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
        raise GapWeightedTop4AuditError(f"cannot read JSON: {path}: {exc}") from exc
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
        raise GapWeightedTop4AuditError(f"cannot read JSONL: {path}: {exc}") from exc
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


def _independent_outcome(row: dict[str, Any]) -> float:
    raw = row.get("trade_pnl_pct", row.get("pnl_pct"))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise GapWeightedTop4AuditError(
            f"missing outcome for candidate {row.get('candidate_id')}"
        ) from exc
    _require(
        math.isfinite(value),
        f"non-finite outcome for candidate {row.get('candidate_id')}",
    )
    return value


def _independent_gap_weighting_stats(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Recompute the frozen weighting formula without experiment helpers."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["entry_day"]), str(row["signal_type"]))
        groups.setdefault(key, []).append(row)

    raw_gaps: list[float] = []
    normalized_pair_weights: list[float] = []
    directed_weights: list[float] = []
    eligible_buckets = 0
    buckets_with_pairs = 0
    candidates_in_eligible_buckets = 0
    undirected_pairs = 0
    ties_excluded = 0
    for key in sorted(groups):
        group = groups[key]
        if len(group) < experiment.MIN_BUCKET_CANDIDATES:
            continue
        eligible_buckets += 1
        candidates_in_eligible_buckets += len(group)
        ordered = sorted(
            group,
            key=lambda row: (
                -_independent_outcome(row),
                str(row["candidate_id"]),
            ),
        )
        top = ordered[: experiment.TOP_K]
        rest = ordered[experiment.TOP_K :]
        bucket_gaps: list[float] = []
        for winner in top:
            for loser in rest:
                gap = abs(_independent_outcome(winner) - _independent_outcome(loser))
                if gap <= 1e-12:
                    ties_excluded += 1
                    continue
                bucket_gaps.append(gap)
        if not bucket_gaps:
            continue
        total_gap = math.fsum(bucket_gaps)
        _require(
            math.isfinite(total_gap) and total_gap > 0.0,
            f"invalid independent pair-gap total for bucket {key}: {total_gap}",
        )
        bucket_directed_weights: list[float] = []
        for gap in bucket_gaps:
            pair_weight = gap / total_gap
            directed_weight = pair_weight / 2.0
            normalized_pair_weights.append(pair_weight)
            bucket_directed_weights.extend((directed_weight, directed_weight))
        bucket_weight = math.fsum(bucket_directed_weights)
        _require(
            math.isclose(bucket_weight, 1.0, rel_tol=0.0, abs_tol=1e-12),
            f"independent bucket weight does not sum to one: {key}: {bucket_weight}",
        )
        buckets_with_pairs += 1
        undirected_pairs += len(bucket_gaps)
        raw_gaps.extend(bucket_gaps)
        directed_weights.extend(bucket_directed_weights)

    return {
        "eligible_buckets": eligible_buckets,
        "buckets_with_pairs": buckets_with_pairs,
        "candidates_in_eligible_buckets": candidates_in_eligible_buckets,
        "undirected_boundary_pairs": undirected_pairs,
        "directed_samples": len(directed_weights),
        "boundary_ties_excluded": ties_excluded,
        "pair_weighting": experiment.PAIR_WEIGHTING,
        "pair_weight_cap": None,
        "pair_weight_floor": None,
        "pair_gap_power": 1.0,
        "raw_pair_gap_sum_pp": round(math.fsum(raw_gaps), 10),
        "raw_pair_gap_min_pp": round(min(raw_gaps), 10) if raw_gaps else None,
        "raw_pair_gap_max_pp": round(max(raw_gaps), 10) if raw_gaps else None,
        "normalized_pair_weight_min": (
            round(min(normalized_pair_weights), 12) if normalized_pair_weights else None
        ),
        "normalized_pair_weight_max": (
            round(max(normalized_pair_weights), 12) if normalized_pair_weights else None
        ),
        "each_bucket_total_training_weight": 1.0,
        "bucket_weight_sum": round(math.fsum(directed_weights), 10),
    }


def _audit_independent_gap_weighting(
    snapshot: dict[str, Any], report: dict[str, Any]
) -> dict[str, Any]:
    training_rows = snapshot.get("_training_rows_by_fold")
    _require(isinstance(training_rows, dict), "replay training rows are missing")
    report_folds = report.get("folds")
    _require(isinstance(report_folds, list), "report folds are missing")
    report_by_fold = {
        str(fold.get("fold_name")): fold
        for fold in report_folds
        if isinstance(fold, dict)
    }
    fold_checks: list[dict[str, Any]] = []
    for fold_name in experiment.EXPECTED_ELIGIBLE_FOLDS:
        rows = training_rows.get(fold_name)
        _require(isinstance(rows, list), f"missing replay training rows: {fold_name}")
        fold = report_by_fold.get(fold_name)
        _require(isinstance(fold, dict), f"missing report fold: {fold_name}")
        training = fold.get("training")
        _require(isinstance(training, dict), f"missing training audit: {fold_name}")
        reported = training.get("pairs")
        _require(isinstance(reported, dict), f"missing pair audit: {fold_name}")
        independently_recomputed = _independent_gap_weighting_stats(rows)
        _require(
            reported == independently_recomputed,
            f"independent gap weighting differs: {fold_name}",
        )
        fold_checks.append(
            {
                "fold_name": fold_name,
                "buckets_with_pairs": independently_recomputed["buckets_with_pairs"],
                "undirected_boundary_pairs": independently_recomputed[
                    "undirected_boundary_pairs"
                ],
                "boundary_ties_excluded": independently_recomputed[
                    "boundary_ties_excluded"
                ],
                "raw_pair_gap_sum_pp": independently_recomputed["raw_pair_gap_sum_pp"],
                "bucket_weight_sum": independently_recomputed["bucket_weight_sum"],
                "each_bucket_weight_sum_equals_one": True,
                "checks_passed": True,
            }
        )
    return {
        "formula": "gap/sum(bucket_gaps); each directed sample receives pair_weight/2",
        "implementation": "independent_audit_recomputation",
        "folds": fold_checks,
        "checks_passed": True,
    }


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
        report.get("experiment_status") == experiment.EXPERIMENT_STATUS,
        "unexpected experiment status",
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
    _require(report.get("portfolio_replayed") is False, "v8a replayed a portfolio")
    design = report.get("preregistered_design")
    _require(isinstance(design, dict), "preregistered design missing")
    _require(
        design.get("pair_weighting") == experiment.PAIR_WEIGHTING,
        "pair weighting differs from v8a contract",
    )
    _require(
        design.get("v7a_screen_reused_without_change") is True,
        "v7a screen was not reused",
    )
    _require(design.get("pair_weight_cap") is None, "pair weight cap was introduced")
    _require(
        design.get("pair_weight_floor") is None, "pair weight floor was introduced"
    )
    _require(design.get("pair_gap_power") == 1.0, "pair gap power drift")

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
        "v7a_ranking_core_code": experiment.V7A_RANKING_CORE_CODE.resolve(),
        "factor_preflight_code": experiment.FACTOR_PREFLIGHT_CODE.resolve(),
        "factor_preflight_audit_code": experiment.FACTOR_PREFLIGHT_AUDIT_CODE.resolve(),
        "v5_audit_code": experiment.V5_AUDIT_CODE.resolve(),
        "fold_loader_code": experiment.FOLD_LOADER_CODE.resolve(),
    }
    for label, expected in expected_code_paths.items():
        actual = _resolve_record(input_value.get(label), report_path, label)
        _require(actual == expected, f"unexpected code path: {label}")
    _require(
        _sha256_file(experiment.V7A_RANKING_CORE_CODE)
        == experiment.V7A_RANKING_CORE_SHA256,
        "frozen v7a ranking core hash drift",
    )

    records, reported_rows = _reported_artifacts(report, report_path)
    replay = experiment.build_snapshot(factor_report_path, fold_report_path)
    independent_gap_weighting = _audit_independent_gap_weighting(replay, report)
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
        "gap_weighting_replayed": True,
        "independent_gap_weighting": independent_gap_weighting,
        "v7a_screen_reused_without_change": True,
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
        "audit_mode": "read_only_full_gap_weighted_model_replay",
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
            "gap_weighting_replayed": True,
            "independent_gap_weighting_checked": True,
            "v7a_screen_reused_without_change": True,
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
