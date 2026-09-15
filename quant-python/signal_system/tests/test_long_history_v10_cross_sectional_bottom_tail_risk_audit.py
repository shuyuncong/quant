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

import long_history_v10_cross_sectional_bottom_tail_risk_audit as audit
import long_history_v10_cross_sectional_bottom_tail_risk_experiment as experiment
from test_long_history_v10_cross_sectional_bottom_tail_risk_experiment import (
    _patch_v10_inputs,
)


def test_primary_verify_full_replay_is_deterministic(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10_inputs(
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
    assert result["primary"]["independent_factor_partition"]["checks_passed"] is True
    assert result["primary"]["independent_risk_target"]["checks_passed"] is True
    assert result["primary"]["independent_screen"]["checks_passed"] is True
    assert result["policy"]["full_model_replay"] is True
    assert result["policy"]["independent_factor_partition_checked"] is True
    assert result["policy"]["v9a_target_model_weights_and_screen_reused_without_change"]
    assert result["policy"]["portfolio_replayed"] is False


def test_audit_rejects_artifact_tampering(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    path = output / "fold_03" / "candidate_risk_scores.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(audit.CrossSectionalBottomTailRiskAuditError, match="SHA256"):
        audit.audit_report(output / "report.json")


def test_audit_rejects_partition_tampering(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["integrity"]["market_regime_structure"][
        "all_reserved_factors_constant_within_signal_day"
    ] = False
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(
        audit.CrossSectionalBottomTailRiskAuditError,
        match="independent market-regime factor partition differs",
    ):
        audit.audit_report(report_path)


def test_audit_rejects_target_and_screen_tampering(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    target_output = tmp_path / "target"
    experiment.build_report(factor_report, fold_report, parent, target_output)
    target_report_path = target_output / "report.json"
    target_report = json.loads(target_report_path.read_text(encoding="utf-8"))
    target_report["folds"][0]["training"]["risk_target"]["risk_labels"] += 1
    target_report_path.write_text(json.dumps(target_report), encoding="utf-8")
    with pytest.raises(
        audit.CrossSectionalBottomTailRiskAuditError,
        match="independent risk target or weight differs",
    ):
        audit.audit_report(target_report_path)

    screen_output = tmp_path / "screen"
    experiment.build_report(factor_report, fold_report, parent, screen_output)
    screen_report_path = screen_output / "report.json"
    screen_report = json.loads(screen_report_path.read_text(encoding="utf-8"))
    key = "stitched_risk_capture_above_random"
    screen_report["screen"]["checks"][key] = not screen_report["screen"]["checks"][key]
    screen_report_path.write_text(json.dumps(screen_report), encoding="utf-8")
    with pytest.raises(
        audit.CrossSectionalBottomTailRiskAuditError,
        match="independent v9a research screen differs",
    ):
        audit.audit_report(screen_report_path)


def test_audit_rejects_factor_design_and_report_tampering(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    design_output = tmp_path / "design"
    experiment.build_report(factor_report, fold_report, parent, design_output)
    design_path = design_output / "report.json"
    design = json.loads(design_path.read_text(encoding="utf-8"))
    design["preregistered_design"]["raw_factor_names"] = []
    design_path.write_text(json.dumps(design), encoding="utf-8")
    with pytest.raises(
        audit.CrossSectionalBottomTailRiskAuditError,
        match="cross-sectional factor names drift",
    ):
        audit.audit_report(design_path)

    replay_output = tmp_path / "replay"
    experiment.build_report(factor_report, fold_report, parent, replay_output)
    replay_path = replay_output / "report.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["research_question"] = "tampered"
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    with pytest.raises(
        audit.CrossSectionalBottomTailRiskAuditError,
        match="report differs from full model replay",
    ):
        audit.audit_report(replay_path)


def test_audit_rejects_reported_code_hash_drift(monkeypatch, tmp_path):
    factor_report, fold_report, parent, _ = _patch_v10_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["input"]["experiment_code"]["sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(audit.CrossSectionalBottomTailRiskAuditError, match="SHA256"):
        audit.audit_report(report_path)


def test_holdout_path_is_blocked(tmp_path):
    with pytest.raises(audit.CrossSectionalBottomTailRiskAuditError, match="Holdout"):
        audit._guard_development_path(tmp_path / "reserved_holdout" / "report.json")
