import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for path in (ROOT, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_long_history_pairwise_ranking_diagnostic import (
    _fake_evaluate_portfolios,
    fake_v6_audit,
    prepare_diagnostic_runs,
)

import long_history_pairwise_ranking_diagnostic as diagnostic
import long_history_pairwise_ranking_diagnostic_audit as diagnostic_audit


def _patch_audit(monkeypatch):
    monkeypatch.setattr(diagnostic_audit, "audit_v6_report", fake_v6_audit)
    monkeypatch.setattr(diagnostic, "_evaluate_portfolios", _fake_evaluate_portfolios)
    monkeypatch.setattr("utils.helpers.load_config", lambda _path: {})


def test_full_diagnostic_replay_and_determinism_audit_passes(tmp_path, monkeypatch):
    _, primary, verify = prepare_diagnostic_runs(tmp_path, monkeypatch)
    _patch_audit(monkeypatch)
    result = diagnostic_audit.run_audit(primary, verify)
    assert result["passes_audit"] is True
    assert result["determinism"]["checked"] is True
    assert result["determinism"]["normalized_report_equal"] is True
    assert len(result["determinism"]["artifact_byte_identical"]) == 7
    assert all(result["determinism"]["artifact_byte_identical"].values())
    assert result["determinism"]["input_v6_determinism"]["checks_passed"] is True
    assert result["production_eligible"] is False


def test_audit_rejects_report_core_tampering(tmp_path, monkeypatch):
    _, primary, verify = prepare_diagnostic_runs(tmp_path, monkeypatch)
    _patch_audit(monkeypatch)
    value = json.loads(primary.read_text(encoding="utf-8"))
    value["interpretation_policy"]["changes_v6_screen"] = True
    primary.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(
        diagnostic_audit.PairwiseRankingDiagnosticAuditError,
        match="report core mismatch",
    ):
        diagnostic_audit.run_audit(primary, verify)


def test_audit_rejects_artifact_tampering(tmp_path, monkeypatch):
    _, primary, verify = prepare_diagnostic_runs(tmp_path, monkeypatch)
    _patch_audit(monkeypatch)
    value = json.loads(primary.read_text(encoding="utf-8"))
    path = Path(value["artifacts"]["bucket_diagnostics"]["path"])
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(
        diagnostic_audit.PairwiseRankingDiagnosticAuditError,
        match="hash mismatch",
    ):
        diagnostic_audit.run_audit(primary, verify)


def test_compare_runs_rejects_different_artifact_sets():
    primary = {
        "report": "C:/tmp/one/report.json",
        "artifact_sha256": {"one.jsonl": "a"},
        "_report_value": {},
        "_v6_audit": {},
    }
    verify = {
        "report": "C:/tmp/two/report.json",
        "artifact_sha256": {},
        "_report_value": {},
        "_v6_audit": {},
    }
    with pytest.raises(
        diagnostic_audit.PairwiseRankingDiagnosticAuditError,
        match="artifact sets differ",
    ):
        diagnostic_audit.compare_runs(primary, verify)

