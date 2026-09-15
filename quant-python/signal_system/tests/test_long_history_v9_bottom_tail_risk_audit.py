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

import long_history_v9_bottom_tail_risk_audit as audit
import long_history_v9_bottom_tail_risk_experiment as experiment
from test_long_history_v7_top4_ranking_experiment import _patch_inputs
from test_long_history_v9_bottom_tail_risk_experiment import _parent_v8a_report


def test_primary_verify_full_replay_is_deterministic(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    parent = _parent_v8a_report(monkeypatch, tmp_path / "parent")
    primary = tmp_path / "primary"
    verify = tmp_path / "verify"
    experiment.build_report(factor_report, fold_report, parent, primary)
    experiment.build_report(factor_report, fold_report, parent, verify)
    result = audit.run_audit(primary / "report.json", verify / "report.json")
    assert result["passes_audit"] is True
    assert result["determinism"]["artifact_count"] == 14
    assert result["determinism"]["all_artifacts_byte_identical"] is True
    assert result["determinism"]["normalized_report_equal"] is True
    assert result["primary"]["independent_risk_target"]["checks_passed"] is True
    assert result["verify"]["independent_risk_target"]["checks_passed"] is True
    assert result["primary"]["independent_screen"]["checks_passed"] is True
    assert result["policy"]["independent_risk_target_and_weights_checked"] is True
    assert result["policy"]["independent_research_screen_checked"] is True
    assert result["policy"]["portfolio_replayed"] is False


def test_independent_target_matches_hand_calculation():
    rows = [
        {
            "candidate_id": f"candidate-{index}",
            "entry_day": "2025-01-02",
            "signal_type": "buy_1",
            "trade_pnl_pct": outcome,
        }
        for index, outcome in enumerate([1.0, 2.0, 3.0, 4.0, 5.0])
    ]
    result = audit._independent_risk_target_stats(rows)
    assert result["eligible_buckets"] == 1
    assert result["risk_labels"] == 1
    assert result["safe_labels"] == 4
    assert result["sample_weight_min"] == pytest.approx(0.125)
    assert result["sample_weight_max"] == pytest.approx(0.5)
    assert result["objective_weight_sum"] == pytest.approx(1.0)


def test_audit_rejects_artifact_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    parent = _parent_v8a_report(monkeypatch, tmp_path / "parent")
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    path = output / "fold_03" / "candidate_risk_scores.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(audit.BottomTailRiskAuditError, match="SHA256 drift"):
        audit.audit_report(output / "report.json")


def test_audit_rejects_target_stat_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    parent = _parent_v8a_report(monkeypatch, tmp_path / "parent")
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["folds"][0]["training"]["risk_target"]["risk_labels"] += 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(
        audit.BottomTailRiskAuditError,
        match="independent risk target or weight differs",
    ):
        audit.audit_report(report_path)


def test_audit_rejects_screen_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    parent = _parent_v8a_report(monkeypatch, tmp_path / "parent")
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    key = "stitched_risk_capture_above_random"
    report["screen"]["checks"][key] = not report["screen"]["checks"][key]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(
        audit.BottomTailRiskAuditError, match="independent research screen differs"
    ):
        audit.audit_report(report_path)


def test_audit_rejects_design_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    parent = _parent_v8a_report(monkeypatch, tmp_path / "parent")
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["preregistered_design"]["risk_fraction"] = 0.25
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(audit.BottomTailRiskAuditError, match="risk fraction"):
        audit.audit_report(report_path)


def test_audit_rejects_report_replay_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    parent = _parent_v8a_report(monkeypatch, tmp_path / "parent")
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, parent, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["research_question"] = "tampered"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(audit.BottomTailRiskAuditError, match="report differs"):
        audit.audit_report(report_path)


def test_holdout_path_is_blocked(tmp_path):
    with pytest.raises(audit.BottomTailRiskAuditError, match="Holdout"):
        audit._guard_development_path(tmp_path / "reserved_holdout" / "report.json")
