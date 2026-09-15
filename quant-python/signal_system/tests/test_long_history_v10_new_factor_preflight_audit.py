import hashlib
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

import long_history_v10_new_factor_preflight as preflight
import long_history_v10_new_factor_preflight_audit as audit
from test_long_history_v10_new_factor_preflight import (
    _build_report,
    _patch_build_inputs,
)


def test_holdout_report_is_blocked(tmp_path):
    with pytest.raises(audit.NewFactorPreflightAuditError, match="Holdout"):
        audit._guard_development_path(tmp_path / "reserved_holdout" / "report.json")


def test_primary_verify_full_replay_is_deterministic(monkeypatch, tmp_path):
    paths = _patch_build_inputs(monkeypatch, tmp_path / "inputs")
    primary = tmp_path / "primary"
    verify = tmp_path / "verify"
    _build_report(paths, primary)
    _build_report(paths, verify)
    result = audit.run_audit(primary / "report.json", verify / "report.json")
    assert result["passes_audit"] is True
    assert result["determinism"]["checked"] is True
    assert result["determinism"]["checks_passed"] is True
    assert result["determinism"]["artifact_count"] == 4
    assert result["determinism"]["all_artifacts_byte_identical"] is True
    assert result["determinism"]["normalized_report_equal"] is True
    assert all(row["artifact_byte_identical"] for row in result["artifact_determinism"])
    assert result["policy"]["full_feature_replay"] is True
    assert result["policy"]["independent_factor_implementation"] is True
    assert result["policy"]["coverage_used_for_selection"] is False
    assert result["policy"]["candidate_outcomes_read"] is False
    assert result["policy"]["model_fitted"] is False


def test_audit_replay_does_not_trust_main_factor_function(monkeypatch, tmp_path):
    paths = _patch_build_inputs(monkeypatch, tmp_path / "inputs")
    primary = tmp_path / "primary"
    verify = tmp_path / "verify"
    _build_report(paths, primary)
    _build_report(paths, verify)

    def wrong_main(*_args, **_kwargs):
        raise AssertionError("audit must replace the main factor implementation")

    monkeypatch.setattr(preflight, "compute_candidate_features", wrong_main)
    result = audit.run_audit(primary / "report.json", verify / "report.json")
    assert result["passes_audit"] is True
    assert result["primary"]["independent_factor_implementation"] is True


def test_audit_rejects_artifact_tampering_even_if_report_hash_is_updated(
    monkeypatch, tmp_path
):
    paths = _patch_build_inputs(monkeypatch, tmp_path / "inputs")
    primary = tmp_path / "primary"
    _build_report(paths, primary)
    artifact = primary / "candidate_features.jsonl"
    row = json.loads(artifact.read_text(encoding="utf-8"))
    row["features"][preflight.FACTOR_NAMES[0]] = 12345.0
    artifact.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    report_path = primary / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["artifacts"]["candidate_features"]["sha256"] = hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(
        audit.NewFactorPreflightAuditError, match="independent full replay differs"
    ):
        audit.audit_run(report_path)


def test_audit_rejects_outcome_like_candidate_field(monkeypatch, tmp_path):
    paths = _patch_build_inputs(monkeypatch, tmp_path / "inputs")
    primary = tmp_path / "primary"
    _build_report(paths, primary)
    artifact = primary / "candidate_features.jsonl"
    row = json.loads(artifact.read_text(encoding="utf-8"))
    row["trade_pnl_pct"] = 99.0
    artifact.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    report_path = primary / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["artifacts"]["candidate_features"]["sha256"] = hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(
        audit.NewFactorPreflightAuditError, match="unexpected candidate"
    ):
        audit.audit_run(report_path)
