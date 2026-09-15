import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import long_history_v7_top4_ranking_experiment as v7a
import long_history_v9_bottom_tail_risk_experiment as experiment
from test_long_history_v7_top4_ranking_experiment import _candidate, _patch_inputs


def _parent_v8a_report(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "v8a-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "version": experiment.v8a.VERSION,
        "passes_research_screen": False,
        "holdout_used": False,
        "portfolio_replayed": False,
        "production_eligible": False,
        "screen": {
            "checks": {name: False for name in experiment.EXPECTED_V8A_FAILED_CHECKS}
        },
        "input": {
            "experiment_code": {
                "path": str(experiment.V8A_PARENT_CODE),
                "sha256": experiment.V8A_PARENT_CODE_SHA256,
            }
        },
    }
    path.write_text(json.dumps(report), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(experiment, "V8A_PARENT_REPORT_SHA256", digest)
    return path


def _one_bucket(outcomes: list[float], day: str = "2025-01-02") -> list[dict]:
    return [
        _candidate(
            f"{index}-row-{day}",
            day,
            outcome=outcome,
            value=outcome,
        )
        for index, outcome in enumerate(outcomes)
    ]


def test_risk_count_uses_frozen_ceiling_rule():
    assert experiment._risk_count(5) == 1
    assert experiment._risk_count(6) == 2
    assert experiment._risk_count(10) == 2
    assert experiment._risk_count(11) == 3


def test_training_labels_and_class_weights_match_hand_calculation():
    rows = _one_bucket([1.0, 2.0, 3.0, 4.0, 5.0])
    feature_map, _ = v7a.build_cross_sectional_feature_map(rows)
    matrix, labels, weights, target = experiment.build_risk_training_data(
        rows, feature_map
    )
    assert matrix.shape == (5, 19)
    np.testing.assert_array_equal(labels, [1.0, 0.0, 0.0, 0.0, 0.0])
    np.testing.assert_allclose(weights, [0.5, 0.125, 0.125, 0.125, 0.125])
    assert target["risk_labels"] == 1
    assert target["safe_labels"] == 4
    assert target["objective_weight_sum"] == pytest.approx(1.0)


def test_each_bucket_and_class_are_normalized_independently():
    rows = _one_bucket([1, 2, 3, 4, 5, 6], "2025-01-02") + _one_bucket(
        [10, 20, 30, 40, 50], "2025-01-03"
    )
    feature_map, _ = v7a.build_cross_sectional_feature_map(rows)
    _, labels, weights, target = experiment.build_risk_training_data(rows, feature_map)
    assert target["eligible_buckets"] == 2
    assert target["risk_labels"] == 3
    assert target["safe_labels"] == 8
    assert weights[:6].sum() == pytest.approx(1.0)
    assert weights[:2].sum() == pytest.approx(0.5)
    assert weights[2:6].sum() == pytest.approx(0.5)
    assert weights[6:].sum() == pytest.approx(1.0)
    assert weights[labels == 1.0].sum() == pytest.approx(1.0)


def test_boundary_tie_is_reported_and_deterministically_included():
    rows = _one_bucket([1.0, 1.0, 2.0, 3.0, 4.0])
    feature_map, _ = v7a.build_cross_sectional_feature_map(rows)
    _, labels, _, target = experiment.build_risk_training_data(rows, feature_map)
    assert list(labels) == [1.0, 0.0, 0.0, 0.0, 0.0]
    assert target["boundary_tie_buckets"] == 1
    assert "included" in target["boundary_tie_policy"]


def test_risk_filter_metrics_match_hand_calculation():
    rows = _one_bucket([1.0, 2.0, 3.0, 4.0, 5.0])
    scores = {
        row["candidate_id"]: score
        for row, score in zip(rows, [5.0, 4.0, 3.0, 2.0, 1.0], strict=True)
    }
    buckets, metrics = experiment.evaluate_risk_filter(rows, scores)
    assert buckets[0]["risk_capture_rate"] == 1.0
    assert buckets[0]["random_expected_risk_capture_rate"] == 0.2
    assert buckets[0]["risk_capture_excess_rate"] == 0.8
    assert buckets[0]["kept_minus_rejected_mean_pnl_pp"] == 2.5
    assert buckets[0]["worst_candidate_avoided"] is True
    assert metrics["bucket_weighted_risk_capture_excess_rate"] == 0.8
    assert metrics["positive_kept_advantage_bucket_pct"] == 100.0


def test_candidate_risk_scores_do_not_leak_outcomes():
    rows = _one_bucket([1.0, 2.0, 3.0, 4.0, 5.0])
    feature_map, _ = v7a.build_cross_sectional_feature_map(rows)
    model = v7a._zero_model()
    output, _ = experiment.score_risk_rows(rows, feature_map, model)
    assert len(output) == 5
    assert sum(row["predicted_rejected"] for row in output) == 1
    assert all(not (set(row) & experiment.OUTCOME_FIELDS) for row in output)
    assert all(
        set(row["rank_features"]) == set(experiment.MODEL_FEATURE_NAMES)
        for row in output
    )


def _screen_fold(name: str, capture: float, advantage: float) -> dict:
    return {
        "fold_name": name,
        "training": {
            "risk_target": {"eligible_buckets": 5},
            "model": {"model_fitted": True, "converged": True},
        },
        "evaluation": {
            "metrics": {
                "bucket_weighted_risk_capture_excess_rate": capture,
                "kept_minus_rejected_mean_pnl_pp": advantage,
            }
        },
    }


def _stitched() -> dict:
    return {
        "metrics": {
            "eligible_buckets": 20,
            "actual_risk_candidates": 20,
            "bucket_weighted_risk_capture_excess_rate": 0.01,
            "kept_minus_rejected_mean_pnl_pp": 0.01,
            "positive_kept_advantage_bucket_pct": 51.0,
            "worst_candidate_avoidance_excess_rate": 0.01,
        }
    }


def test_research_screen_requires_joint_fold_stability():
    folds = [
        _screen_fold(name, 0.01, 0.01) for name in experiment.EXPECTED_ELIGIBLE_FOLDS
    ]
    passed = experiment._research_screen(folds, _stitched())
    assert passed["passes_research_screen"] is True
    folds[:3] = [
        _screen_fold(name, -0.01, 0.01)
        for name in experiment.EXPECTED_ELIGIBLE_FOLDS[:3]
    ]
    failed = experiment._research_screen(folds, _stitched())
    assert failed["passes_research_screen"] is False
    assert (
        failed["checks"]["at_least_four_folds_joint_risk_and_economic_advantage"]
        is False
    )


def test_build_report_records_frozen_target_and_refuses_overwrite(
    monkeypatch, tmp_path
):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    parent = _parent_v8a_report(monkeypatch, tmp_path / "parent")
    output = tmp_path / "v9a"
    report = experiment.build_report(factor_report, fold_report, parent, output)
    assert isinstance(report["passes_research_screen"], bool)
    assert report["preregistered_design"]["risk_fraction"] == 0.20
    assert report["preregistered_design"]["tail_count_rule"] == (
        experiment.TAIL_COUNT_RULE
    )
    assert report["frozen_parent"]["v8a_passes_research_screen"] is False
    assert report["factor_selection_performed"] is False
    assert report["portfolio_replayed"] is False
    assert report["production_eligible"] is False
    assert report["folds"][0]["training"]["risk_target"][
        "objective_weight_sum"
    ] == pytest.approx(5.0)
    with pytest.raises(
        experiment.BottomTailRiskExperimentError, match="refusing overwrite"
    ):
        experiment.build_report(factor_report, fold_report, parent, output)


def test_parent_v8a_failure_contract_is_enforced(monkeypatch, tmp_path):
    parent = _parent_v8a_report(monkeypatch, tmp_path)
    report = json.loads(parent.read_text(encoding="utf-8"))
    report["passes_research_screen"] = True
    parent.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(
        experiment,
        "V8A_PARENT_REPORT_SHA256",
        hashlib.sha256(parent.read_bytes()).hexdigest(),
    )
    with pytest.raises(experiment.BottomTailRiskExperimentError, match="did not fail"):
        experiment._load_parent_v8a_report(parent)


def test_holdout_path_is_blocked(tmp_path):
    with pytest.raises(experiment.BottomTailRiskExperimentError, match="Holdout"):
        experiment._guard_development_path(
            tmp_path / "reserved_holdout" / "report.json"
        )


def test_cli_error_payload_is_json(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "experiment",
            "--factor-report",
            str(tmp_path / "missing-factor.json"),
            "--fold-report",
            str(tmp_path / "missing-fold.json"),
            "--v8a-report",
            str(tmp_path / "missing-v8a.json"),
            "--output-dir",
            str(tmp_path / "output"),
        ],
    )
    assert experiment.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == experiment.VERSION
    assert payload["passes_research_screen"] is False
