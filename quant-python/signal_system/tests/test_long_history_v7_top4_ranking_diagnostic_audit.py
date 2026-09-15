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

from test_long_history_v7_top4_ranking_diagnostic import (
    _fake_v7_audit,
    _make_v7_report,
)

import long_history_v7_top4_ranking_diagnostic as diagnostic
import long_history_v7_top4_ranking_diagnostic_audit as audit


def _patch_audits(monkeypatch):
    monkeypatch.setattr(diagnostic, "audit_v7_report", _fake_v7_audit)
    monkeypatch.setattr(audit, "audit_v7_report", _fake_v7_audit)


def test_primary_verify_full_replay_is_deterministic(monkeypatch, tmp_path):
    _patch_audits(monkeypatch)
    primary_v7 = _make_v7_report(tmp_path / "v7-primary")
    verify_v7 = _make_v7_report(tmp_path / "v7-verify")
    primary = tmp_path / "diagnostic-primary"
    verify = tmp_path / "diagnostic-verify"
    diagnostic.build_report(primary_v7, primary)
    diagnostic.build_report(verify_v7, verify)
    result = audit.run_audit(primary / "report.json", verify / "report.json")
    assert result["passes_audit"] is True
    assert result["determinism"]["artifact_count"] == 8
    assert result["determinism"]["all_artifacts_byte_identical"] is True
    assert result["determinism"]["normalized_report_equal"] is True
    input_v7 = result["determinism"]["input_v7_determinism"]
    assert input_v7["artifact_count"] == 14
    assert input_v7["all_artifacts_byte_identical"] is True
    assert input_v7["normalized_report_equal"] is True
    assert result["policy"]["portfolio_replayed"] is False


def test_audit_rejects_artifact_tampering(monkeypatch, tmp_path):
    _patch_audits(monkeypatch)
    v7_report = _make_v7_report(tmp_path / "v7")
    output = tmp_path / "diagnostic"
    diagnostic.build_report(v7_report, output)
    artifact = output / "scope_summary.jsonl"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(audit.Top4RankingDiagnosticAuditError, match="hash mismatch"):
        audit.audit_diagnostic_report(output / "report.json")


def test_audit_rejects_policy_tampering(monkeypatch, tmp_path):
    _patch_audits(monkeypatch)
    v7_report = _make_v7_report(tmp_path / "v7")
    output = tmp_path / "diagnostic"
    diagnostic.build_report(v7_report, output)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["interpretation_policy"]["authorizes_v7b"] = True
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(audit.Top4RankingDiagnosticAuditError, match="authorizes v7b"):
        audit.audit_diagnostic_report(report_path)
