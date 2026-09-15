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

import long_history_v8_gap_weighted_top4_ranking_audit as audit
import long_history_v8_gap_weighted_top4_ranking_experiment as experiment
from test_long_history_v7_top4_ranking_experiment import _patch_inputs


def test_primary_verify_full_replay_is_deterministic(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    primary = tmp_path / "primary"
    verify = tmp_path / "verify"
    experiment.build_report(factor_report, fold_report, primary)
    experiment.build_report(factor_report, fold_report, verify)
    result = audit.run_audit(primary / "report.json", verify / "report.json")
    assert result["passes_audit"] is True
    assert result["determinism"]["artifact_count"] == 14
    assert result["determinism"]["all_artifacts_byte_identical"] is True
    assert result["determinism"]["normalized_report_equal"] is True
    assert result["policy"]["gap_weighting_replayed"] is True
    assert result["policy"]["independent_gap_weighting_checked"] is True
    assert result["primary"]["independent_gap_weighting"]["checks_passed"] is True
    assert result["verify"]["independent_gap_weighting"]["checks_passed"] is True
    assert result["policy"]["v7a_screen_reused_without_change"] is True
    assert result["policy"]["portfolio_replayed"] is False


def test_audit_rejects_artifact_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, output)
    path = output / "fold_03" / "candidate_scores.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(audit.GapWeightedTop4AuditError, match="SHA256 drift"):
        audit.audit_report(output / "report.json")


def test_audit_rejects_weight_contract_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["preregistered_design"]["pair_weight_cap"] = 10.0
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(audit.GapWeightedTop4AuditError, match="cap"):
        audit.audit_report(report_path)


def test_independent_gap_weighting_matches_hand_calculation():
    rows = [
        {
            "candidate_id": f"candidate-{index}",
            "entry_day": "2025-01-02",
            "signal_type": "buy_1",
            "trade_pnl_pct": outcome,
        }
        for index, outcome in enumerate([5.0, 4.0, 3.0, 2.0, 1.0])
    ]
    result = audit._independent_gap_weighting_stats(rows)
    assert result["eligible_buckets"] == 1
    assert result["buckets_with_pairs"] == 1
    assert result["undirected_boundary_pairs"] == 4
    assert result["directed_samples"] == 8
    assert result["boundary_ties_excluded"] == 0
    assert result["raw_pair_gap_sum_pp"] == 10.0
    assert result["normalized_pair_weight_min"] == pytest.approx(0.1)
    assert result["normalized_pair_weight_max"] == pytest.approx(0.4)
    assert result["bucket_weight_sum"] == pytest.approx(1.0)


def test_audit_rejects_independent_weight_stat_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["folds"][0]["training"]["pairs"]["raw_pair_gap_sum_pp"] += 0.1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(
        audit.GapWeightedTop4AuditError, match="independent gap weighting differs"
    ):
        audit.audit_report(report_path)


def test_audit_rejects_report_replay_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    output = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["screen"]["passes_research_screen"] = not report["screen"][
        "passes_research_screen"
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(audit.GapWeightedTop4AuditError, match="report differs"):
        audit.audit_report(report_path)


def test_holdout_path_is_blocked(tmp_path):
    with pytest.raises(audit.GapWeightedTop4AuditError, match="Holdout"):
        audit._guard_development_path(tmp_path / "reserved_holdout" / "report.json")
