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

import long_history_v10_cross_sectional_bottom_tail_risk_experiment as experiment
from test_long_history_v7_top4_ranking_experiment import _patch_inputs


def _json_record(path: Path) -> dict:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _features(value: float, market_value: float) -> dict[str, float]:
    result = {name: market_value for name in experiment.RESERVED_MARKET_REGIME_FACTORS}
    result.update(
        {
            name: value + index / 100.0
            for index, name in enumerate(experiment.FACTOR_NAMES)
        }
    )
    return result


def _factor_row(row: dict) -> dict:
    value = float(next(iter(row["features"].values())))
    market_value = float(sum(ord(char) for char in row["signal_day"]))
    features = _features(value, market_value)
    return {
        "candidate_id": row["candidate_id"],
        "symbol": row["symbol"],
        "signal_day": row["signal_day"],
        "signal_type": row["signal_type"],
        "features": features,
        "missing": {name: False for name in experiment.ALL_V10_FACTOR_NAMES},
        "data_cutoffs": {
            "qfq_max_day_used": row["signal_day"],
            "index_max_day_used": row["signal_day"],
        },
    }


def _parent_v9a_report(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "v9a-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    checks = {
        "all_six_models_fitted_and_converged": True,
        "at_least_four_folds_risk_capture_above_random": True,
    }
    checks.update({name: False for name in experiment.EXPECTED_V9A_FAILED_CHECKS})
    report = {
        "version": experiment.v9a.VERSION,
        "passes_research_screen": False,
        "holdout_used": False,
        "portfolio_replayed": False,
        "production_eligible": False,
        "screen": {"checks": checks},
        "input": {
            "experiment_code": {
                "path": str(experiment.V9A_TARGET_SCREEN_CORE_CODE),
                "sha256": experiment.V9A_TARGET_SCREEN_CORE_SHA256,
            }
        },
    }
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(
        experiment,
        "V9A_PARENT_REPORT_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    return path


def _patch_v10_inputs(monkeypatch, tmp_path: Path):
    old_report_path, fold_path = _patch_inputs(monkeypatch, tmp_path)
    old_report = json.loads(old_report_path.read_text(encoding="utf-8"))
    old_rows = [
        json.loads(line)
        for line in (tmp_path / "candidate_features.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    factor_rows = [_factor_row(row) for row in old_rows]
    factor_artifact = tmp_path / "v10_candidate_features.jsonl"
    factor_artifact.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in factor_rows
        ),
        encoding="utf-8",
        newline="\n",
    )
    factor_report_path = tmp_path / "v10-factor-report.json"
    factor_report = {
        "version": experiment.FACTOR_REPORT_VERSION,
        "input": old_report["input"],
    }
    factor_report_path.write_text(json.dumps(factor_report), encoding="utf-8")
    factor_audit = {
        "checks_passed": True,
        "candidate_count": len(factor_rows),
        "factor_count": len(experiment.ALL_V10_FACTOR_NAMES),
        "independent_factor_implementation": True,
        "_report_value": factor_report,
        "_artifact_paths": {"candidate_features": factor_artifact},
    }
    monkeypatch.setattr(experiment, "_assert_frozen_cores", lambda: None)
    monkeypatch.setattr(experiment.v10audit, "audit_run", lambda _path: factor_audit)
    monkeypatch.setattr(
        experiment,
        "V10_FACTOR_REPORT_SHA256",
        hashlib.sha256(factor_report_path.read_bytes()).hexdigest(),
    )
    parent = _parent_v9a_report(monkeypatch, tmp_path / "parent")
    return factor_report_path, fold_path, parent, factor_rows


def _one_bucket(outcomes: list[float], day: str = "2025-01-02") -> list[dict]:
    rows: list[dict] = []
    for index, outcome in enumerate(outcomes):
        features = _features(float(index), 7.0)
        rows.append(
            {
                "candidate_id": f"candidate-{index}",
                "symbol": f"{index:06d}",
                "signal_day": day,
                "entry_day": day,
                "signal_type": "buy_1",
                "trade_pnl_pct": outcome,
                "_v10_features": features,
            }
        )
    return rows


def test_factor_partition_is_complete_disjoint_and_outcome_blind():
    assert len(experiment.FACTOR_NAMES) == 9
    assert len(experiment.RESERVED_MARKET_REGIME_FACTORS) == 3
    assert set(experiment.FACTOR_NAMES).isdisjoint(
        experiment.RESERVED_MARKET_REGIME_FACTORS
    )
    assert set(experiment.FACTOR_NAMES) | set(
        experiment.RESERVED_MARKET_REGIME_FACTORS
    ) == set(experiment.ALL_V10_FACTOR_NAMES)
    assert not (set(experiment.FACTOR_NAMES) & experiment.OUTCOME_FIELDS)


def test_market_regime_factors_are_constant_within_signal_day():
    rows = _one_bucket([1, 2, 3, 4, 5])
    passed = experiment.audit_market_regime_structure(rows)
    assert passed["all_reserved_factors_constant_within_signal_day"] is True
    rows[0]["_v10_features"][experiment.RESERVED_MARKET_REGIME_FACTORS[0]] = 99.0
    failed = experiment.audit_market_regime_structure(rows)
    assert failed["all_reserved_factors_constant_within_signal_day"] is False
    assert (
        failed["same_signal_day_violations_by_factor"][
            experiment.RESERVED_MARKET_REGIME_FACTORS[0]
        ]
        == 1
    )

    mixed = _one_bucket([1, 2, 3, 4, 5])
    mixed[0]["signal_day"] = "2025-01-01"
    mixed[0]["_v10_features"][experiment.RESERVED_MARKET_REGIME_FACTORS[0]] = 8.0
    mixed_result = experiment.audit_market_regime_structure(mixed)
    assert mixed_result["mixed_signal_day_buckets"] == 1
    assert mixed_result["all_reserved_factors_constant_within_signal_day"] is True


def test_cross_sectional_features_are_nine_dimensional_midrank_values():
    rows = _one_bucket([1, 2, 3, 4, 5])
    first_name = experiment.FACTOR_NAMES[0]
    rows[0]["_v10_features"][first_name] = None
    feature_map, feature_audit = experiment.build_cross_sectional_feature_map(rows)
    assert len(feature_map["candidate-0"]) == 9
    assert feature_map["candidate-0"][0] == 0.0
    assert feature_map["candidate-1"][0] == -0.5
    assert feature_map["candidate-4"][0] == 0.5
    assert feature_audit["feature_names"] == list(experiment.MODEL_FEATURE_NAMES)


def test_risk_labels_weights_and_model_match_frozen_v9a_contract():
    rows = _one_bucket([1, 2, 3, 4, 5])
    feature_map, _ = experiment.build_cross_sectional_feature_map(rows)
    matrix, labels, weights, target = experiment.build_risk_training_data(
        rows, feature_map
    )
    assert matrix.shape == (5, 9)
    np.testing.assert_array_equal(labels, [1.0, 0.0, 0.0, 0.0, 0.0])
    np.testing.assert_allclose(weights, [0.5, 0.125, 0.125, 0.125, 0.125])
    assert target["risk_fraction"] == 0.20
    assert target["objective_weight_sum"] == pytest.approx(1.0)
    model = experiment.fit_logistic(matrix, labels, weights)
    assert len(model["weights"]) == 9
    assert model["l2"] == 1.0
    assert model["model_fitted"] is True


def test_candidate_score_artifact_contains_no_outcome():
    rows = _one_bucket([1, 2, 3, 4, 5])
    feature_map, _ = experiment.build_cross_sectional_feature_map(rows)
    output, _ = experiment.score_risk_rows(rows, feature_map, experiment._zero_model())
    assert len(output) == 5
    assert sum(row["predicted_rejected"] for row in output) == 1
    assert all(not (set(row) & experiment.OUTCOME_FIELDS) for row in output)
    assert all(
        set(row["rank_features"]) == set(experiment.MODEL_FEATURE_NAMES)
        for row in output
    )


def test_build_report_records_frozen_comparison_and_refuses_overwrite(
    monkeypatch, tmp_path
):
    factor_report, fold_report, parent, _ = _patch_v10_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    output = tmp_path / "v10a"
    report = experiment.build_report(factor_report, fold_report, parent, output)
    assert isinstance(report["passes_research_screen"], bool)
    assert report["preregistered_design"]["raw_factor_names"] == list(
        experiment.FACTOR_NAMES
    )
    assert (
        report["preregistered_design"]["v9a_research_screen_reused_without_change"]
        is True
    )
    assert (
        report["integrity"]["market_regime_structure"][
            "all_reserved_factors_constant_within_signal_day"
        ]
        is True
    )
    assert report["frozen_parent"]["v9a_passes_research_screen"] is False
    assert report["factor_selection_performed"] is False
    assert report["portfolio_replayed"] is False
    assert report["production_eligible"] is False
    assert report["folds"][0]["training"]["model"]["feature_names"] == list(
        experiment.MODEL_FEATURE_NAMES
    )
    with pytest.raises(
        experiment.CrossSectionalBottomTailRiskError, match="refusing overwrite"
    ):
        experiment.build_report(factor_report, fold_report, parent, output)


def test_factor_report_parent_and_bucket_invariance_fail_closed(monkeypatch, tmp_path):
    factor_report, fold_report, parent, factor_rows = _patch_v10_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    monkeypatch.setattr(experiment, "V10_FACTOR_REPORT_SHA256", "0" * 64)
    with pytest.raises(
        experiment.CrossSectionalBottomTailRiskError, match="factor report SHA256"
    ):
        experiment.build_snapshot(factor_report, fold_report, parent)

    monkeypatch.setattr(
        experiment,
        "V10_FACTOR_REPORT_SHA256",
        hashlib.sha256(factor_report.read_bytes()).hexdigest(),
    )
    first = factor_rows[0]
    same_day_peer = next(
        row
        for row in factor_rows[1:]
        if row["signal_day"] == first["signal_day"]
        and row["signal_type"] == first["signal_type"]
    )
    same_day_peer["features"][experiment.RESERVED_MARKET_REGIME_FACTORS[0]] += 1000.0
    artifact = Path(
        experiment.v10audit.audit_run(factor_report)["_artifact_paths"][
            "candidate_features"
        ]
    )
    artifact.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in factor_rows
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        experiment.CrossSectionalBottomTailRiskError, match="same-signal-day"
    ):
        experiment.build_snapshot(factor_report, fold_report, parent)


def test_holdout_path_and_cli_failure_are_safe(monkeypatch, capsys, tmp_path):
    with pytest.raises(experiment.CrossSectionalBottomTailRiskError, match="Holdout"):
        experiment._guard_development_path(
            tmp_path / "reserved_holdout" / "report.json"
        )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "experiment",
            "--factor-report",
            str(tmp_path / "missing-factor.json"),
            "--fold-report",
            str(tmp_path / "missing-fold.json"),
            "--v9a-report",
            str(tmp_path / "missing-v9a.json"),
            "--output-dir",
            str(tmp_path / "output"),
        ],
    )
    assert experiment.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == experiment.VERSION
    assert payload["passes_research_screen"] is False
