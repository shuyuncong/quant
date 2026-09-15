import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import long_history_v7_top4_ranking_diagnostic as diagnostic


def _bucket(day: str, advantage: float, accuracy: float = 0.6) -> dict:
    return {
        "entry_day": day,
        "signal_type": "buy_1",
        "bucket_size": 5,
        "boundary_pairs": 4,
        "boundary_accuracy": accuracy,
        "top4_advantage_pp": advantage,
        "top4_advantage_positive": advantage > 0.0,
        "top4_hit_rate": 0.6,
        "random_expected_hit_rate": 0.8,
        "predicted_top4_mean_pnl_pct": advantage,
        "rest_mean_pnl_pct": 0.0,
    }


def _scores(scope: str, day: str) -> list[dict]:
    rows = []
    for index in range(5):
        value = -0.5 + index * 0.25
        rows.append(
            {
                "candidate_id": f"{scope}|{day}|{index}",
                "symbol": f"{index:06d}",
                "signal_day": day,
                "entry_day": day,
                "signal_type": "buy_1",
                "eligible_bucket": True,
                "bucket_size": 5,
                "model_score": float(5 - index),
                "predicted_rank": index + 1,
                "predicted_top4": index < 4,
                "rank_features": {
                    feature: value for feature in diagnostic.MODEL_FEATURE_NAMES
                },
            }
        )
    return rows


def _record(path: Path, rows: list[dict]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "path": str(path),
        "rows": len(rows),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _make_v7_report(root: Path) -> Path:
    advantages = [-2.0, -0.5, 0.1, 0.5, 1.5]
    folds = []
    all_buckets: list[dict] = []
    all_scores: list[dict] = []
    for fold_index, fold_name in enumerate(diagnostic.EXPECTED_ELIGIBLE_FOLDS):
        bucket_rows = []
        score_rows = []
        for month, advantage in enumerate(advantages, start=1):
            day = f"2025-{month:02d}-{fold_index + 2:02d}"
            bucket_rows.append(_bucket(day, advantage))
            score_rows.extend(_scores(fold_name, day))
        all_buckets.extend(bucket_rows)
        all_scores.extend(score_rows)
        artifacts = {
            "bucket_metrics": _record(
                root / fold_name / "bucket_metrics.jsonl", bucket_rows
            ),
            "candidate_scores": _record(
                root / fold_name / "candidate_scores.jsonl", score_rows
            ),
        }
        folds.append(
            {
                "fold_name": fold_name,
                "training": {
                    "model": {
                        "coefficients": {
                            feature: (fold_index + 1) * (feature_index + 1) / 100.0
                            for feature_index, feature in enumerate(
                                diagnostic.MODEL_FEATURE_NAMES
                            )
                        }
                    }
                },
                "artifacts": artifacts,
            }
        )
    stitched_artifacts = {
        "bucket_metrics": _record(
            root / "stitched_oos" / "bucket_metrics.jsonl", all_buckets
        ),
        "candidate_scores": _record(
            root / "stitched_oos" / "candidate_scores.jsonl", all_scores
        ),
    }
    checks = {
        "eligible_folds_exactly_fold_03_through_fold_08": True,
        "all_six_models_fitted_and_converged": True,
        "every_fold_training_boundary_buckets_at_least_5": True,
        "at_least_four_folds_boundary_accuracy_above_random": True,
        "at_least_four_folds_top4_mean_advantage_above_zero": False,
        "median_fold_top4_mean_advantage_above_zero": True,
        "stitched_eligible_buckets_at_least_20": True,
        "stitched_boundary_pairs_at_least_100": True,
        "stitched_boundary_accuracy_above_random": True,
        "stitched_top4_mean_advantage_above_zero": True,
        "stitched_top4_median_advantage_above_zero": True,
        "stitched_positive_advantage_bucket_rate_above_half": True,
        "stitched_top4_hit_rate_above_random_expectation": True,
    }
    report = {
        "folds": folds,
        "stitched_oos": {"artifacts": stitched_artifacts},
        "passes_research_screen": False,
        "screen": {
            "checks": checks,
            "folds_top4_mean_advantage_above_zero": 3,
            "passes_research_screen": False,
        },
    }
    path = root / "report.json"
    path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    return path


def _fake_v7_audit(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    artifact_paths: dict[str, Path] = {}
    artifact_hashes: dict[str, str] = {}
    scopes = [(fold["fold_name"], fold) for fold in report["folds"]]
    scopes.append(("stitched_oos", report["stitched_oos"]))
    for scope_name, scope in scopes:
        for artifact_name, record in scope["artifacts"].items():
            artifact_path = Path(record["path"])
            key = f"{scope_name}/{artifact_name}"
            artifact_paths[key] = artifact_path
            artifact_hashes[key] = hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest()
    return {
        "report": str(path),
        "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "passes_research_screen": False,
        "model_fitted": True,
        "artifact_sha256": artifact_hashes,
        "checks_passed": True,
        "_report_value": report,
        "_artifact_paths": artifact_paths,
    }


def test_bind_scope_marks_fixed_tails_and_selection_tilt():
    bucket_rows = [
        _bucket(f"2025-0{index + 1}-02", advantage)
        for index, advantage in enumerate([-5.0, -1.0, 0.0, 1.0, 5.0])
    ]
    score_rows = [
        row
        for bucket_row in bucket_rows
        for row in _scores("fold_03", bucket_row["entry_day"])
    ]
    rows = diagnostic._bind_scope("fold_03", bucket_rows, score_rows)
    assert sum(row["bottom_20pct"] for row in rows) == 1
    assert sum(row["top_20pct"] for row in rows) == 1
    assert rows[0]["bottom_20pct"] is True
    assert rows[-1]["top_20pct"] is True
    assert rows[0]["selection_tilt"][
        diagnostic.MODEL_FEATURE_NAMES[0]
    ] == pytest.approx(-0.625)


def test_mechanism_and_leave_one_out_are_bucket_equal_weighted():
    bucket_rows = [
        _bucket("2025-01-02", -3.0, 0.4),
        _bucket("2025-01-03", 1.0, 0.5),
        _bucket("2025-02-02", 2.0, 0.6),
        _bucket("2025-02-03", 4.0, 0.7),
        _bucket("2025-03-02", 6.0, 0.8),
    ]
    score_rows = [
        row
        for bucket_row in bucket_rows
        for row in _scores("fold_03", bucket_row["entry_day"])
    ]
    rows = diagnostic._bind_scope("fold_03", bucket_rows, score_rows)
    mechanism = diagnostic._mechanism_rows("fold_03", rows)
    accuracy_above = next(
        row
        for row in mechanism
        if row["mechanism"] == "boundary_accuracy_vs_0.5" and row["category"] == "above"
    )
    assert accuracy_above["bucket_count"] == 3
    leaveouts = diagnostic._leave_one_out_rows("fold_03", rows)
    worst_removed = next(
        row
        for row in leaveouts
        if row["scenario"] == "leave_one_bucket_out"
        and row["removed_value"] == "2025-01-02|buy_1"
    )
    assert worst_removed["top4_mean_advantage_pp"] == pytest.approx(3.25)
    assert (
        len([row for row in leaveouts if row["scenario"] == "leave_one_month_out"]) == 3
    )


def test_build_report_has_fixed_artifact_and_policy_contract(monkeypatch, tmp_path):
    v7_report = _make_v7_report(tmp_path / "v7")
    monkeypatch.setattr(diagnostic, "audit_v7_report", _fake_v7_audit)
    output = tmp_path / "diagnostic"
    report = diagnostic.build_report(v7_report, output)
    assert set(report["artifacts"]) == set(diagnostic.ARTIFACT_NAMES)
    assert report["input_model_replayed"] is True
    assert report["model_fitted"] is False
    assert report["portfolio_replayed"] is False
    assert report["interpretation_policy"]["changes_v7a_screen"] is False
    assert report["interpretation_policy"]["authorizes_v7b"] is False
    assert report["scope_aggregates"]["stitched_oos"]["eligible_buckets"] == 30
    with pytest.raises(
        diagnostic.Top4RankingDiagnosticError, match="refusing overwrite"
    ):
        diagnostic.build_report(v7_report, output)


def test_holdout_path_is_blocked(tmp_path):
    with pytest.raises(diagnostic.Top4RankingDiagnosticError, match="Holdout"):
        diagnostic._guard_development_path(
            tmp_path / "reserved_holdout" / "report.json"
        )
