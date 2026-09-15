import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import long_history_v10_market_regime_bucket_gate_experiment as experiment


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


def _candidate(
    identifier: str,
    signal_day: str,
    entry_day: str,
    outcome: float,
) -> dict:
    return {
        "candidate_id": identifier,
        "symbol": identifier.split("-")[0].zfill(6)[-6:],
        "signal_day": signal_day,
        "entry_day": entry_day,
        "signal_type": "buy_1",
        "trade_pnl_pct": outcome,
    }


def _factor_row(row: dict, market_value: float) -> dict:
    features = {
        name: market_value + index / 100.0
        for index, name in enumerate(experiment.v10a.ALL_V10_FACTOR_NAMES)
    }
    return {
        "candidate_id": row["candidate_id"],
        "symbol": row["symbol"],
        "signal_day": row["signal_day"],
        "signal_type": row["signal_type"],
        "features": features,
        "missing": {name: False for name in features},
        "data_cutoffs": {
            "qfq_max_day_used": row["signal_day"],
            "index_max_day_used": row["signal_day"],
        },
    }


def _parent_v10a_report(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "v10a-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    checks = {"all_six_models_fitted_and_converged": True}
    checks.update({name: False for name in experiment.EXPECTED_V10A_FAILED_CHECKS})
    report = {
        "version": experiment.v10a.VERSION,
        "passes_research_screen": False,
        "holdout_used": False,
        "portfolio_replayed": False,
        "production_eligible": False,
        "screen": {"checks": checks},
        "input": {
            "experiment_code": {
                "path": str(experiment.V10A_PARENT_CODE),
                "sha256": experiment.V10A_PARENT_CODE_SHA256,
            }
        },
    }
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(
        experiment,
        "V10A_PARENT_REPORT_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    return path


def _patch_v10b_inputs(monkeypatch, tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_path = tmp_path / "source.json"
    fold_path = tmp_path / "fold.json"
    factor_report_path = tmp_path / "factor-report.json"
    source_path.write_text("{}", encoding="utf-8")
    fold_path.write_text("{}", encoding="utf-8")

    common_train: list[dict] = []
    market_by_id: dict[str, float] = {}
    for bucket in range(24):
        signal_day = f"2024-{bucket // 12 + 1:02d}-{bucket % 12 + 1:02d}"
        entry_day = signal_day
        outcome = -2.0 if bucket % 2 == 0 else 2.0
        for member in range(2):
            identifier = f"{bucket:02d}{member}-train"
            common_train.append(
                _candidate(identifier, signal_day, entry_day, outcome + member * 0.1)
            )
            market_by_id[identifier] = float(bucket - 12)

    loaded_folds = []
    all_by_id = {row["candidate_id"]: row for row in common_train}
    for fold_index in range(3, 9):
        evaluation: list[dict] = []
        for bucket in range(8):
            signal_day = f"2025-{fold_index:02d}-{bucket + 1:02d}"
            outcome = -1.5 if bucket % 2 == 0 else 1.5
            for member in range(2):
                identifier = f"{fold_index}{bucket}{member}-eval"
                evaluation.append(
                    _candidate(
                        identifier,
                        signal_day,
                        signal_day,
                        outcome + member * 0.1,
                    )
                )
                market_by_id[identifier] = float(bucket + fold_index / 10.0)
        all_by_id.update({row["candidate_id"]: row for row in evaluation})
        loaded_folds.append(
            {
                "fold_name": f"fold_{fold_index:02d}",
                "evaluation_window": {
                    "start": evaluation[0]["signal_day"],
                    "end": evaluation[-1]["signal_day"],
                },
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
    factor_rows = [
        _factor_row(row, market_by_id[row["candidate_id"]]) for row in all_rows
    ]
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
        "factor_count": len(experiment.v10a.ALL_V10_FACTOR_NAMES),
        "independent_factor_implementation": True,
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
        "evaluation_candidates": 96,
        "cross_fold_conflicting_rows": 0,
        "duplicate_evaluation_candidates": 0,
        "purged_ids_in_same_fold_training": 0,
    }
    monkeypatch.setattr(experiment, "_assert_frozen_cores", lambda: None)
    monkeypatch.setattr(experiment.v10audit, "audit_run", lambda _path: factor_audit)
    monkeypatch.setattr(
        experiment,
        "V10_FACTOR_REPORT_SHA256",
        hashlib.sha256(factor_report_path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        experiment.v7a, "audit_source_report", lambda _path: source_audit
    )
    monkeypatch.setattr(
        experiment.v7a,
        "audit_fold_report",
        lambda _path, _source: fold_audit,
    )
    monkeypatch.setattr(
        experiment.v7a,
        "_load_eligible_fold_rows",
        lambda _path, _report: (loaded_folds, all_rows, integrity),
    )
    parent = _parent_v10a_report(monkeypatch, tmp_path / "parent")
    return factor_report_path, fold_path, parent, factor_rows


def _bucket_candidates(outcomes: list[float]) -> list[dict]:
    rows = []
    for index, outcome in enumerate(outcomes):
        row = _candidate(f"{index}-candidate", "2025-01-02", "2025-01-03", outcome)
        row["_v10_features"] = {
            name: float(position + 1)
            for position, name in enumerate(experiment.FACTOR_NAMES)
        }
        rows.append(row)
    return rows


def test_signal_day_is_part_of_bucket_and_market_factors_must_be_constant():
    rows = _bucket_candidates([-2.0, 1.0])
    buckets = experiment.build_bucket_rows(rows)
    assert len(buckets) == 1
    assert buckets[0]["bucket_id"] == "2025-01-02|2025-01-03|buy_1"
    assert buckets[0]["equal_weight_mean_trade_pnl_pct"] == pytest.approx(-0.5)
    assert buckets[0]["actual_bad_bucket"] is True

    second_day = dict(rows[1])
    second_day["signal_day"] = "2025-01-01"
    second_day["_v10_features"] = dict(rows[1]["_v10_features"])
    second_day["_v10_features"][experiment.FACTOR_NAMES[0]] = 99.0
    split = experiment.build_bucket_rows([rows[0], second_day])
    assert len(split) == 2

    rows[1]["_v10_features"][experiment.FACTOR_NAMES[0]] = 99.0
    with pytest.raises(
        experiment.MarketRegimeBucketGateError, match="differs inside signal bucket"
    ):
        experiment.build_bucket_rows(rows)


def test_bad_bucket_labels_and_class_weights_are_equal_by_class():
    buckets = []
    for index, outcome in enumerate([-2.0, -1.0, 1.0, 3.0]):
        buckets.append(
            {
                "signal_day": f"2025-01-{index + 1:02d}",
                "entry_day": f"2025-01-{index + 1:02d}",
                "signal_type": "buy_1",
                "bucket_id": f"bucket-{index}",
                "features": {name: float(index) for name in experiment.FACTOR_NAMES},
                "actual_bad_bucket": outcome < 0,
            }
        )
    feature_map = {
        row["bucket_id"]: np.asarray([float(index)] * 3)
        for index, row in enumerate(buckets)
    }
    matrix, labels, weights, target = experiment.build_training_data(
        buckets, feature_map
    )
    assert matrix.shape == (4, 3)
    np.testing.assert_array_equal(labels, [1.0, 1.0, 0.0, 0.0])
    np.testing.assert_allclose(weights, [0.25, 0.25, 0.25, 0.25])
    assert weights[labels == 1.0].sum() == pytest.approx(0.5)
    assert weights[labels == 0.0].sum() == pytest.approx(0.5)
    assert target["outcome_magnitude_used_as_training_weight"] is False


def test_training_midrank_and_oos_ecdf_use_only_training_reference():
    values = [1.0, 2.0, 2.0, 4.0]
    assert experiment._training_midrank(1.0, values) == pytest.approx(-0.5)
    assert experiment._training_midrank(2.0, values) == pytest.approx(0.0)
    assert experiment._training_midrank(4.0, values) == pytest.approx(0.5)
    assert experiment._oos_ecdf(0.0, values) == pytest.approx(-0.5)
    assert experiment._oos_ecdf(2.0, values) == pytest.approx(0.0)
    assert experiment._oos_ecdf(3.0, values) == pytest.approx(0.25)
    assert experiment._oos_ecdf(5.0, values) == pytest.approx(0.5)
    digest = experiment._reference_digest(values)
    assert experiment._oos_ecdf(999.0, values) == pytest.approx(0.5)
    assert experiment._reference_digest(values) == digest


def test_probability_tie_is_kept_and_score_artifact_has_no_outcome():
    rows = experiment.build_bucket_rows(_bucket_candidates([-1.0, 2.0]))
    feature_map = {rows[0]["bucket_id"]: np.zeros(3, dtype=float)}
    model = {
        "weights": np.zeros(3, dtype=float),
        "l2": 1.0,
        "iterations": 1,
        "converged": True,
        "objective_weight_sum": 1.0,
        "model_fitted": True,
    }
    scores, probabilities = experiment.score_buckets(rows, feature_map, model)
    assert probabilities[rows[0]["bucket_id"]] == pytest.approx(0.5)
    assert scores[0]["predicted_rejected"] is False
    assert not (set(scores[0]) & experiment.OUTCOME_FIELDS)
    assert set(scores[0]["train_ecdf_features"]) == set(experiment.MODEL_FEATURE_NAMES)


def test_build_report_is_preregistered_and_refuses_overwrite(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10b_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    output = tmp_path / "v10b"
    report = experiment.build_report(factor_report, fold_report, parent, output)
    assert isinstance(report["passes_research_screen"], bool)
    assert report["preregistered_design"]["bucket_key"] == list(experiment.BUCKET_KEY)
    assert report["preregistered_design"]["stock_ranking"] is False
    assert report["frozen_parent"]["v10a_passes_research_screen"] is False
    assert report["stock_ranking_performed"] is False
    assert report["portfolio_replayed"] is False
    assert report["production_eligible"] is False
    assert len(report["folds"][0]["training"]["model"]["coefficients"]) == 3
    with pytest.raises(
        experiment.MarketRegimeBucketGateError, match="refusing overwrite"
    ):
        experiment.build_report(factor_report, fold_report, parent, output)


def test_input_drift_and_reserved_path_fail_closed(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10b_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    monkeypatch.setattr(experiment, "V10_FACTOR_REPORT_SHA256", "0" * 64)
    with pytest.raises(
        experiment.MarketRegimeBucketGateError, match="factor report SHA256"
    ):
        experiment.build_snapshot(factor_report, fold_report, parent)
    with pytest.raises(experiment.MarketRegimeBucketGateError, match="Holdout"):
        experiment._guard_development_path(tmp_path / "reserved_holdout" / "x")


def test_cli_failure_is_safe(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "experiment",
            "--factor-report",
            str(tmp_path / "missing-factor.json"),
            "--fold-report",
            str(tmp_path / "missing-fold.json"),
            "--v10a-report",
            str(tmp_path / "missing-v10a.json"),
            "--output-dir",
            str(tmp_path / "output"),
        ],
    )
    assert experiment.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == experiment.VERSION
    assert payload["passes_research_screen"] is False
