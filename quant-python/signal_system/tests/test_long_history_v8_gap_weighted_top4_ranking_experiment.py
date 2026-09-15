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
import long_history_v8_gap_weighted_top4_ranking_experiment as experiment
from test_long_history_v7_top4_ranking_experiment import _candidate, _patch_inputs


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


def test_gap_weights_match_hand_calculation_and_preserve_v7a_samples():
    rows = _one_bucket([5.0, 4.0, 3.0, 2.0, 1.0])
    feature_map, _ = v7a.build_cross_sectional_feature_map(rows)
    old_matrix, old_labels, old_weights, _ = v7a.build_boundary_training_data(
        rows, feature_map
    )
    matrix, labels, weights, audit = (
        experiment.build_gap_weighted_boundary_training_data(rows, feature_map)
    )
    np.testing.assert_array_equal(matrix, old_matrix)
    np.testing.assert_array_equal(labels, old_labels)
    assert not np.array_equal(weights, old_weights)
    np.testing.assert_allclose(
        weights,
        np.asarray([0.2, 0.2, 0.15, 0.15, 0.1, 0.1, 0.05, 0.05]),
    )
    assert weights.sum() == pytest.approx(1.0)
    assert audit["raw_pair_gap_sum_pp"] == 10.0
    assert audit["normalized_pair_weight_min"] == pytest.approx(0.1)
    assert audit["normalized_pair_weight_max"] == pytest.approx(0.4)
    assert audit["pair_weighting"] == experiment.PAIR_WEIGHTING
    assert audit["pair_weight_cap"] is None
    assert audit["pair_weight_floor"] is None


def test_each_bucket_is_normalized_independently_of_gap_scale():
    first = _one_bucket([5.0, 4.0, 3.0, 2.0, 1.0], "2025-01-02")
    second = _one_bucket([50.0, 40.0, 30.0, 20.0, 10.0], "2025-01-03")
    rows = first + second
    feature_map, _ = v7a.build_cross_sectional_feature_map(rows)
    _, _, weights, audit = experiment.build_gap_weighted_boundary_training_data(
        rows, feature_map
    )
    np.testing.assert_allclose(weights[:8], weights[8:])
    assert weights[:8].sum() == pytest.approx(1.0)
    assert weights[8:].sum() == pytest.approx(1.0)
    assert audit["buckets_with_pairs"] == 2
    assert audit["bucket_weight_sum"] == pytest.approx(2.0)


def test_equal_outcome_pairs_are_excluded_before_gap_normalization():
    rows = _one_bucket([5.0, 4.0, 1.0, 1.0, 1.0])
    feature_map, _ = v7a.build_cross_sectional_feature_map(rows)
    matrix, labels, weights, audit = (
        experiment.build_gap_weighted_boundary_training_data(rows, feature_map)
    )
    assert matrix.shape == (4, 19)
    assert list(labels) == [1.0, 0.0, 1.0, 0.0]
    np.testing.assert_allclose(weights, [2 / 7, 2 / 7, 3 / 14, 3 / 14])
    assert audit["boundary_ties_excluded"] == 2
    assert audit["undirected_boundary_pairs"] == 2
    assert audit["raw_pair_gap_sum_pp"] == 7.0


def test_empty_pair_input_has_frozen_shape_and_audit_values():
    rows = _one_bucket([1.0, 1.0, 1.0, 1.0, 1.0])
    feature_map, _ = v7a.build_cross_sectional_feature_map(rows)
    matrix, labels, weights, audit = (
        experiment.build_gap_weighted_boundary_training_data(rows, feature_map)
    )
    assert matrix.shape == (0, 19)
    assert labels.size == weights.size == 0
    assert audit["buckets_with_pairs"] == 0
    assert audit["raw_pair_gap_min_pp"] is None
    assert audit["normalized_pair_weight_max"] is None


def test_research_screen_is_exact_v7a_screen():
    folds = [
        {
            "fold_name": name,
            "training": {
                "pairs": {"buckets_with_pairs": 5},
                "model": {"model_fitted": True, "converged": True},
            },
            "evaluation": {
                "metrics": {
                    "bucket_weighted_boundary_accuracy": 0.51,
                    "top4_advantage_mean_pp": 0.1,
                }
            },
        }
        for name in experiment.EXPECTED_ELIGIBLE_FOLDS
    ]
    stitched = {
        "metrics": {
            "eligible_buckets": 20,
            "boundary_pairs": 100,
            "bucket_weighted_boundary_accuracy": 0.51,
            "top4_advantage_mean_pp": 0.1,
            "top4_advantage_median_pp": 0.1,
            "positive_advantage_bucket_pct": 51.0,
            "top4_hit_rate": 0.7,
            "random_expected_hit_rate": 0.6,
        }
    }
    assert experiment._research_screen(folds, stitched) == v7a._research_screen(
        folds, stitched
    )


def test_build_report_records_only_change_and_create_once(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    output = tmp_path / "v8a"
    report = experiment.build_report(factor_report, fold_report, output)
    assert isinstance(report["passes_research_screen"], bool)
    assert report["preregistered_design"]["pair_weighting"] == (
        experiment.PAIR_WEIGHTING
    )
    assert report["preregistered_design"]["v7a_screen_reused_without_change"] is True
    assert report["preregistered_design"]["pair_weight_cap"] is None
    assert report["frozen_parent"]["v7a_failure_changed"] is False
    assert report["portfolio_replayed"] is False
    assert report["production_eligible"] is False
    assert report["folds"][0]["training"]["pairs"]["bucket_weight_sum"] == 5.0
    with pytest.raises(
        experiment.GapWeightedTop4ExperimentError, match="refusing overwrite"
    ):
        experiment.build_report(factor_report, fold_report, output)


def test_frozen_v7a_core_hash_drift_fails(monkeypatch):
    monkeypatch.setattr(experiment, "V7A_RANKING_CORE_SHA256", "0" * 64)
    with pytest.raises(
        experiment.GapWeightedTop4ExperimentError, match="ranking core SHA256 drift"
    ):
        experiment._assert_frozen_v7a_core()


def test_holdout_path_is_blocked(tmp_path):
    with pytest.raises(experiment.GapWeightedTop4ExperimentError, match="Holdout"):
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
            "--output-dir",
            str(tmp_path / "output"),
        ],
    )
    assert experiment.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == experiment.VERSION
    assert payload["passes_research_screen"] is False
