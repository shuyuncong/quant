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

import long_history_v7_top4_ranking_audit as audit
import long_history_v7_top4_ranking_experiment as experiment
from test_long_history_v7_top4_ranking_experiment import _patch_inputs


def test_holdout_report_is_blocked(tmp_path):
    with pytest.raises(audit.Top4RankingAuditError, match="Holdout"):
        audit._guard_development_path(tmp_path / "reserved_holdout" / "report.json")


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
    assert result["policy"]["portfolio_replayed"] is False


def test_audit_rejects_artifact_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    primary = tmp_path / "primary"
    verify = tmp_path / "verify"
    experiment.build_report(factor_report, fold_report, primary)
    experiment.build_report(factor_report, fold_report, verify)
    path = primary / "fold_03" / "candidate_scores.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(audit.Top4RankingAuditError, match="SHA256 drift"):
        audit.run_audit(primary / "report.json", verify / "report.json")


def test_audit_rejects_report_tampering(monkeypatch, tmp_path):
    factor_report, fold_report = _patch_inputs(monkeypatch, tmp_path / "inputs")
    primary = tmp_path / "primary"
    experiment.build_report(factor_report, fold_report, primary)
    report_path = primary / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["screen"]["passes_research_screen"] = not report["screen"][
        "passes_research_screen"
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(audit.Top4RankingAuditError, match="report differs"):
        audit.audit_report(report_path)
