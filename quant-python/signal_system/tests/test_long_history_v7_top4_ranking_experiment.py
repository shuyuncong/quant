import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import long_history_v7_top4_ranking_experiment as experiment


def _features(value: float, *, missing_first: bool = False) -> dict:
    return {
        name: (None if missing_first and index == 0 else value + index / 1000.0)
        for index, name in enumerate(experiment.FACTOR_NAMES)
    }


def _candidate(identifier: str, day: str, outcome: float, value: float) -> dict:
    return {
        "candidate_id": identifier,
        "symbol": identifier.split("-")[0].zfill(6)[-6:],
        "signal_day": day,
        "entry_day": day,
        "exit_day": day,
        "signal_type": "buy_1",
        "trade_pnl_pct": outcome,
        "_v7_features": _features(value),
    }


def _factor_row(row: dict) -> dict:
    features = dict(row["_v7_features"])
    return {
        "candidate_id": row["candidate_id"],
        "symbol": row["symbol"],
        "signal_day": row["signal_day"],
        "signal_type": row["signal_type"],
        "source_fields": {"cross_day": None, "confirmation_bars": None},
        "features": features,
        "missing": {name: features[name] is None for name in experiment.FACTOR_NAMES},
        "data_cutoffs": {
            "qfq_max_day_used": row["signal_day"],
            "index_max_day_used": row["signal_day"],
            "stock_pool_max_day_used": row["signal_day"],
        },
    }


def _json_record(path: Path) -> dict:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def _patch_inputs(monkeypatch, tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_path = tmp_path / "source.json"
    fold_path = tmp_path / "fold.json"
    factor_report_path = tmp_path / "factor-report.json"
    source_path.write_text("{}", encoding="utf-8")
    fold_path.write_text("{}", encoding="utf-8")

    common_train: list[dict] = []
    for bucket in range(5):
        day = f"2024-12-{bucket + 2:02d}"
        for rank in range(5):
            common_train.append(
                _candidate(
                    f"{bucket}{rank}-train",
                    day,
                    outcome=float(5 - rank),
                    value=float(5 - rank),
                )
            )
    loaded_folds = []
    all_by_id = {row["candidate_id"]: row for row in common_train}
    for fold_index in range(3, 9):
        day = f"2025-{fold_index:02d}-03"
        evaluation = [
            _candidate(
                f"{fold_index}{rank}-eval",
                day,
                outcome=float(5 - rank),
                value=float(5 - rank),
            )
            for rank in range(5)
        ]
        all_by_id.update({row["candidate_id"]: row for row in evaluation})
        loaded_folds.append(
            {
                "fold_name": f"fold_{fold_index:02d}",
                "evaluation_window": {"start": day, "end": day},
                "input_artifacts": {
                    "train": {"sha256": "train"},
                    "purged_training_labels": {"sha256": "purged"},
                    "evaluation": {"sha256": "evaluation"},
                },
                "rows": {
                    "train": common_train,
                    "purged_training_labels": [],
                    "evaluation": evaluation,
                },
            }
        )
    all_rows = sorted(all_by_id.values(), key=lambda row: row["candidate_id"])
    factor_rows = [_factor_row(row) for row in all_rows]
    factor_artifact = tmp_path / "candidate_features.jsonl"
    _write_jsonl(factor_artifact, factor_rows)
    factor_report = {
        "version": experiment.FACTOR_REPORT_VERSION,
        "input": {
            "source_report": _json_record(source_path),
            "fold_report": _json_record(fold_path),
        },
    }
    factor_report_path.write_text(json.dumps(factor_report), encoding="utf-8")
    factor_audit = {
        "checks_passed": True,
        "candidate_count": len(factor_rows),
        "factor_count": len(experiment.FACTOR_NAMES),
        "_report_value": factor_report,
        "_artifact_paths": {"candidate_features": factor_artifact},
    }
    source_audit = {"checks_passed": True}
    fold_audit = {
        "checks_passed": True,
        "dataset_candidates": len(all_rows),
        "dataset_candidate_ids_sha256": "dataset-manifest",
        "_report_value": {},
    }
    integrity = {
        "eligible_folds": list(experiment.EXPECTED_ELIGIBLE_FOLDS),
        "unique_candidates_loaded": len(all_rows),
        "evaluation_candidates": 30,
        "cross_fold_conflicting_rows": 0,
        "duplicate_evaluation_candidates": 0,
        "purged_ids_in_same_fold_training": 0,
    }
    monkeypatch.setattr(experiment, "audit_factor_report", lambda _path: factor_audit)
    monkeypatch.setattr(experiment, "audit_source_report", lambda _path: source_audit)
    monkeypatch.setattr(
        experiment, "audit_fold_report", lambda _path, _source: fold_audit
    )
    monkeypatch.setattr(
        experiment,
        "_load_eligible_fold_rows",
        lambda _path, _report: (loaded_folds, all_rows, integrity),
    )
    return factor_report_path, fold_path


def test_cross_sectional_midrank_and_missing_value():
    rows = [
        _candidate("1-a", "2025-01-02", 1.0, 1.0),
        _candidate("2-b", "2025-01-02", 2.0, 2.0),
        _candidate("3-c", "2025-01-02", 3.0, 3.0),
    ]
    rows[0]["_v7_features"] = _features(1.0, missing_first=True)
    feature_map, audit = experiment.build_cross_sectional_feature_map(rows)
    assert feature_map["1-a"][0] == 0.0
    assert feature_map["2-b"][0] == -0.5
    assert feature_map["3-c"][0] == 0.5
    assert audit["missing_rank_value"] == 0.0
    assert len(feature_map["1-a"]) == 19


def test_boundary_training_uses_only_top4_vs_rest_and_equal_bucket_weights():
    rows = []
    for bucket in range(2):
        day = f"2025-01-0{bucket + 2}"
        rows.extend(
            _candidate(
                f"{bucket}{rank}-row",
                day,
                outcome=float(5 - rank),
                value=float(5 - rank),
            )
            for rank in range(5)
        )
    feature_map, _ = experiment.build_cross_sectional_feature_map(rows)
    matrix, labels, weights, audit = experiment.build_boundary_training_data(
        rows, feature_map
    )
    assert matrix.shape == (16, 19)
    assert set(labels) == {0.0, 1.0}
    assert audit["undirected_boundary_pairs"] == 8
    assert audit["buckets_with_pairs"] == 2
    assert weights.sum() == pytest.approx(2.0)


def test_equal_outcome_boundary_pairs_are_excluded():
    rows = [
        _candidate(f"{rank}-row", "2025-01-02", 1.0, float(rank)) for rank in range(5)
    ]
    feature_map, _ = experiment.build_cross_sectional_feature_map(rows)
    matrix, labels, weights, audit = experiment.build_boundary_training_data(
        rows, feature_map
    )
    assert matrix.shape == (0, 19)
    assert len(labels) == len(weights) == 0
    assert audit["boundary_ties_excluded"] == 4


def test_top4_metrics_match_hand_calculation():
    rows = [
        _candidate(f"{rank}-row", "2025-01-02", float(5 - rank), float(5 - rank))
        for rank in range(5)
    ]
    scores = {
        row["candidate_id"]: _outcome for row, _outcome in zip(rows, [5, 4, 3, 2, 1])
    }
    buckets, metrics = experiment.evaluate_top4(rows, scores)
    assert buckets[0]["boundary_accuracy"] == 1.0
    assert buckets[0]["top4_advantage_pp"] == 2.5
    assert metrics["top4_hit_rate"] == 1.0
    assert metrics["random_expected_hit_rate"] == 0.8
    assert metrics["positive_advantage_bucket_pct"] == 100.0


def _screen_fold(name: str, accuracy: float, advantage: float) -> dict:
    return {
        "fold_name": name,
        "training": {
            "pairs": {"buckets_with_pairs": 5},
            "model": {"model_fitted": True, "converged": True},
        },
        "evaluation": {
            "metrics": {
                "bucket_weighted_boundary_accuracy": accuracy,
                "top4_advantage_mean_pp": advantage,
            }
        },
    }


def _stitched_metrics(**overrides) -> dict:
    values = {
        "eligible_buckets": 20,
        "boundary_pairs": 100,
        "bucket_weighted_boundary_accuracy": 0.51,
        "top4_advantage_mean_pp": 0.1,
        "top4_advantage_median_pp": 0.1,
        "positive_advantage_bucket_pct": 51.0,
        "top4_hit_rate": 0.7,
        "random_expected_hit_rate": 0.6,
    }
    values.update(overrides)
    return {"metrics": values}


def test_research_screen_requires_four_folds_and_all_stitched_checks():
    passing = [
        _screen_fold(name, 0.51 if index < 4 else 0.49, 0.1 if index < 4 else -0.1)
        for index, name in enumerate(experiment.EXPECTED_ELIGIBLE_FOLDS)
    ]
    assert experiment._research_screen(passing, _stitched_metrics())[
        "passes_research_screen"
    ]
    failing = [dict(fold) for fold in passing]
    failing[3] = _screen_fold(failing[3]["fold_name"], 0.49, -0.1)
    assert not experiment._research_screen(failing, _stitched_metrics())[
        "passes_research_screen"
    ]
    assert not experiment._research_screen(
        passing, _stitched_metrics(top4_hit_rate=0.5)
    )["passes_research_screen"]


def test_holdout_and_existing_output_are_blocked(tmp_path):
    with pytest.raises(experiment.Top4RankingExperimentError, match="Holdout"):
        experiment._guard_development_path(tmp_path / "reserved_holdout" / "report")


def test_existing_output_is_blocked(tmp_path):
    output = tmp_path / "exists"
    output.mkdir()
    with pytest.raises(
        experiment.Top4RankingExperimentError, match="refusing overwrite"
    ):
        experiment.build_report(
            tmp_path / "factor.json", tmp_path / "fold.json", output
        )


def test_synthetic_build_writes_14_artifacts_without_portfolio(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    output = tmp_path / "run"
    report = experiment.build_report(factor_report, fold_report, output)
    assert report["model_fitted"] is True
    assert report["portfolio_replayed"] is False
    assert isinstance(report["passes_research_screen"], bool)
    artifact_records = [
        record for fold in report["folds"] for record in fold["artifacts"].values()
    ] + list(report["stitched_oos"]["artifacts"].values())
    assert len(artifact_records) == 14
    assert all(Path(record["path"]).exists() for record in artifact_records)
    assert "portfolio" not in report["stitched_oos"]
    score_text = (output / "fold_03" / "candidate_scores.jsonl").read_text(
        encoding="utf-8"
    )
    assert "trade_pnl_pct" not in score_text
    assert "exit_reason" not in score_text


def test_fit_is_deterministic():
    rows = [
        _candidate(f"{rank}-row", "2025-01-02", float(5 - rank), float(5 - rank))
        for rank in range(5)
    ]
    feature_map, _ = experiment.build_cross_sectional_feature_map(rows)
    matrix, labels, weights, _ = experiment.build_boundary_training_data(
        rows, feature_map
    )
    first = experiment.fit_pairwise_logistic(matrix, labels, weights)
    second = experiment.fit_pairwise_logistic(matrix, labels, weights)
    assert np.array_equal(first["weights"], second["weights"])
    assert first["converged"] is True
