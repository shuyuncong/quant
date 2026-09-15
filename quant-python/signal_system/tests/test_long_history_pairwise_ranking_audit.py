import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for path in (ROOT, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_long_history_pairwise_ranking_experiment import (
    _fake_portfolio,
    _hydrate_rows,
    prepare_synthetic_environment,
)

import long_history_pairwise_ranking_audit as ranking_audit
import long_history_pairwise_ranking_experiment as experiment


def _patch_audit(monkeypatch, source_audit: dict, fold_audit: dict) -> None:
    monkeypatch.setattr(
        ranking_audit, "audit_source_report", lambda _path: source_audit
    )
    monkeypatch.setattr(
        ranking_audit, "audit_fold_report", lambda _path, _source: fold_audit
    )
    monkeypatch.setattr(
        ranking_audit, "hydrate_causal_ranking_features", _hydrate_rows
    )
    monkeypatch.setattr(ranking_audit, "load_config", lambda _path: {})
    monkeypatch.setattr(
        ranking_audit, "_resolve_execution_config", lambda _value: {}
    )
    monkeypatch.setattr(experiment, "_portfolio", _fake_portfolio)


def _build_two_runs(tmp_path: Path, monkeypatch):
    source_path, fold_path, config_path, source_audit, fold_audit = (
        prepare_synthetic_environment(tmp_path, monkeypatch)
    )
    first = tmp_path / "primary"
    second = tmp_path / "verify"
    experiment.build_report(source_path, fold_path, first, config_path)
    experiment.build_report(source_path, fold_path, second, config_path)
    _patch_audit(monkeypatch, source_audit, fold_audit)
    return first / "report.json", second / "report.json"


def test_full_read_only_replay_audit_passes_for_two_independent_runs(
    tmp_path, monkeypatch
):
    primary, verify = _build_two_runs(tmp_path, monkeypatch)
    result = ranking_audit.run_audit(primary, verify)
    assert result["passes_audit"] is True
    assert result["determinism"]["checked"] is True
    assert result["determinism"]["normalized_report_equal"] is True
    assert len(result["determinism"]["artifact_byte_identical"]) == 42
    assert all(result["determinism"]["artifact_byte_identical"].values())
    assert result["production_eligible"] is False


def test_audit_rejects_report_screen_tampering(tmp_path, monkeypatch):
    primary, verify = _build_two_runs(tmp_path, monkeypatch)
    value = json.loads(primary.read_text(encoding="utf-8"))
    value["screen"]["passes_research_screen"] = not value["screen"][
        "passes_research_screen"
    ]
    primary.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ranking_audit.PairwiseRankingAuditError, match="screen mismatch"):
        ranking_audit.run_audit(primary, verify)


def test_audit_rejects_random_seed_artifact_tampering(tmp_path, monkeypatch):
    primary, verify = _build_two_runs(tmp_path, monkeypatch)
    value = json.loads(primary.read_text(encoding="utf-8"))
    seed_record = value["folds"][0]["artifacts"]["random_seed_runs"]
    seed_path = Path(seed_record["path"])
    seed_path.write_text(seed_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(ranking_audit.PairwiseRankingAuditError, match="artifact hash"):
        ranking_audit.run_audit(primary, verify)


def test_compare_runs_rejects_missing_artifact():
    primary = {
        "report": "C:/tmp/primary/report.json",
        "artifact_sha256": {"a": "one"},
        "_report_value": {"artifacts": {"path": "C:/tmp/primary/a.jsonl"}},
    }
    verify = {
        "report": "C:/tmp/verify/report.json",
        "artifact_sha256": {},
        "_report_value": {"artifacts": {"path": "C:/tmp/verify/a.jsonl"}},
    }
    with pytest.raises(ranking_audit.PairwiseRankingAuditError, match="sets differ"):
        ranking_audit.compare_runs(primary, verify)
