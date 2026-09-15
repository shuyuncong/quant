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

import long_history_v7_factor_preflight as preflight
import long_history_v7_factor_preflight_audit as audit
from test_long_history_v7_factor_preflight import _patch_build_inputs


def test_holdout_report_is_blocked(tmp_path):
    with pytest.raises(audit.FactorPreflightAuditError, match="Holdout"):
        audit._guard_development_path(tmp_path / "reserved_holdout" / "report.json")


def test_primary_verify_full_replay_is_deterministic(monkeypatch, tmp_path):
    source_path, fold_path, index_path, _stock_path, _calls = _patch_build_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    primary = tmp_path / "primary"
    verify = tmp_path / "verify"
    preflight.build_report(source_path, fold_path, index_path, primary)
    preflight.build_report(source_path, fold_path, index_path, verify)
    result = audit.run_audit(primary / "report.json", verify / "report.json")
    assert result["passes_audit"] is True
    assert result["determinism"]["checked"] is True
    assert result["determinism"]["artifact_count"] == 4
    assert result["determinism"]["all_artifacts_byte_identical"] is True
    assert result["determinism"]["normalized_report_equal"] is True
    assert all(row["artifact_byte_identical"] for row in result["artifact_determinism"])


def test_audit_rejects_artifact_tampering(monkeypatch, tmp_path):
    source_path, fold_path, index_path, _stock_path, _calls = _patch_build_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    primary = tmp_path / "primary"
    verify = tmp_path / "verify"
    preflight.build_report(source_path, fold_path, index_path, primary)
    preflight.build_report(source_path, fold_path, index_path, verify)
    path = primary / "candidate_features.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(audit.FactorPreflightAuditError, match="SHA256 drift"):
        audit.run_audit(primary / "report.json", verify / "report.json")


def test_audit_rejects_outcome_like_candidate_field(monkeypatch, tmp_path):
    source_path, fold_path, index_path, _stock_path, _calls = _patch_build_inputs(
        monkeypatch, tmp_path / "inputs"
    )
    primary = tmp_path / "primary"
    preflight.build_report(source_path, fold_path, index_path, primary)
    artifact = primary / "candidate_features.jsonl"
    row = json.loads(artifact.read_text(encoding="utf-8").strip())
    row["trade_pnl_pct"] = 99.0
    artifact.write_text(json.dumps(row) + "\n", encoding="utf-8")
    report_path = primary / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    import hashlib

    report["artifacts"]["candidate_features"]["sha256"] = hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(
        audit.FactorPreflightAuditError, match="unexpected candidate fields"
    ):
        audit.audit_run(report_path)
