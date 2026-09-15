import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import long_history_v10_market_regime_bucket_gate_audit as audit
import long_history_v10_market_regime_bucket_gate_experiment as experiment
from test_long_history_v10_market_regime_bucket_gate_experiment import (
    _patch_v10b_inputs,
)


def test_primary_verify_full_replay_is_deterministic(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10b_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    primary = tmp_path / "primary"
    verify = tmp_path / "verify"
    experiment.build_report(factor_report, fold_report, parent, primary)
    experiment.build_report(factor_report, fold_report, parent, verify)
    result = audit.run_audit(primary / "report.json", verify / "report.json")
    assert result["passes_audit"] is True
    assert result["determinism"]["artifact_count"] == 14
    assert result["determinism"]["all_artifacts_byte_identical"] is True
    assert result["determinism"]["normalized_report_equal"] is True
    assert result["primary"]["input_v10_factor_preflight"]["checks_passed"] is True
    assert result["primary"]["parent_v10a"]["checks_passed"] is True
    assert result["primary"]["independent_bucket_gate"]["checks_passed"] is True
    assert result["primary"]["independent_screen"]["checks_passed"] is True
    assert result["policy"]["full_model_replay"] is True
    assert result["policy"]["independent_bucket_construction_checked"] is True
    assert result["policy"]["independent_train_only_feature_transform_checked"]
    assert result["policy"]["independent_bucket_target_and_weights_checked"]
    assert result["policy"]["stock_ranking_performed"] is False
    assert result["policy"]["portfolio_replayed"] is False


def test_audit_rejects_artifact_tampering(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10b_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    path = output / "fold_03" / "bucket_gate_scores.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(audit.MarketRegimeBucketGateAuditError, match="SHA256"):
        audit.audit_report(output / "report.json")


def test_audit_rejects_target_ecdf_and_screen_tampering(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10b_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    target_output = tmp_path / "target"
    experiment.build_report(factor_report, fold_report, parent, target_output)
    target_path = target_output / "report.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target["folds"][0]["training"]["target"]["bad_buckets"] += 1
    target_path.write_text(json.dumps(target), encoding="utf-8")
    with pytest.raises(
        audit.MarketRegimeBucketGateAuditError,
        match="train-only transform, target, weights, or model differs",
    ):
        audit.audit_report(target_path)

    ecdf_output = tmp_path / "ecdf"
    experiment.build_report(factor_report, fold_report, parent, ecdf_output)
    ecdf_path = ecdf_output / "report.json"
    ecdf = json.loads(ecdf_path.read_text(encoding="utf-8"))
    ecdf["folds"][0]["training"]["feature_transform"]["training_only"] = False
    ecdf_path.write_text(json.dumps(ecdf), encoding="utf-8")
    with pytest.raises(
        audit.MarketRegimeBucketGateAuditError,
        match="train-only transform, target, weights, or model differs",
    ):
        audit.audit_report(ecdf_path)

    screen_output = tmp_path / "screen"
    experiment.build_report(factor_report, fold_report, parent, screen_output)
    screen_path = screen_output / "report.json"
    screen = json.loads(screen_path.read_text(encoding="utf-8"))
    key = "stitched_balanced_accuracy_above_half"
    screen["screen"]["checks"][key] = not screen["screen"]["checks"][key]
    screen_path.write_text(json.dumps(screen), encoding="utf-8")
    with pytest.raises(
        audit.MarketRegimeBucketGateAuditError,
        match="independent research screen differs",
    ):
        audit.audit_report(screen_path)


def test_audit_rejects_design_and_report_tampering(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10b_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    design_output = tmp_path / "design"
    experiment.build_report(factor_report, fold_report, parent, design_output)
    design_path = design_output / "report.json"
    design = json.loads(design_path.read_text(encoding="utf-8"))
    design["preregistered_design"]["bucket_key"] = ["entry_day", "signal_type"]
    design_path.write_text(json.dumps(design), encoding="utf-8")
    with pytest.raises(
        audit.MarketRegimeBucketGateAuditError, match="preregistered design drift"
    ):
        audit.audit_report(design_path)

    replay_output = tmp_path / "replay"
    experiment.build_report(factor_report, fold_report, parent, replay_output)
    replay_path = replay_output / "report.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["research_question"] = "tampered"
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    with pytest.raises(
        audit.MarketRegimeBucketGateAuditError,
        match="report differs from full model replay",
    ):
        audit.audit_report(replay_path)


def test_audit_rejects_reported_code_hash_drift(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10b_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["input"]["experiment_code"]["sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(audit.MarketRegimeBucketGateAuditError, match="SHA256"):
        audit.audit_report(report_path)


def test_holdout_path_is_blocked(tmp_path):
    with pytest.raises(audit.MarketRegimeBucketGateAuditError, match="Holdout"):
        audit._guard_development_path(tmp_path / "reserved_holdout" / "report.json")
