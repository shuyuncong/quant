import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import freeze_holdout as freeze  # noqa: E402
import macd_near_regime_audit as audit  # noqa: E402
from holdout_integrity import (  # noqa: E402
    canonical_json,
    compute_candidate_seal,
    sha256_text,
)


def _manifest(start="2026-09-01"):
    return {
        "version": freeze.FREEZE_VERSION,
        "rules": {
            "signal": audit.NEAR_SIGNAL,
            "weak_regimes": sorted(audit.WEAK_REGIMES),
        },
        "holdout_window": {"start": start},
        "universe": {"symbols": ["000001", "000002"]},
    }


def _sealed_manifest(start="2026-09-01", symbols=None):
    symbols = sorted(symbols or ["000001", "000002"])
    manifest = {
        "version": "holdout_freeze.v1",
        "label": "test",
        "holdout_window": {"start": start, "end": None},
        "universe": {
            "source": "test",
            "count": len(symbols),
            "manifest_sha256": hashlib.sha256(
                "\n".join(symbols).encode("utf-8")
            ).hexdigest(),
            "symbols": symbols,
        },
        "rules": {
            "experiment": "bear_range_near_observe_only",
            "signal": audit.NEAR_SIGNAL,
            "weak_regimes": sorted(audit.WEAK_REGIMES),
        },
        "rules_sha256": hashlib.sha256(b"rules").hexdigest(),
        "code_hashes": {},
        "config": {
            "file": "config.yaml",
            "file_sha256": hashlib.sha256(b"cfg").hexdigest(),
            "snapshot": {},
            "snapshot_sha256": hashlib.sha256(b"snap").hexdigest(),
        },
        "candidates": {"generated": False},
        "seal": None,
    }
    seal_input = {
        "version": manifest["version"],
        "label": manifest["label"],
        "holdout_window": manifest["holdout_window"],
        "universe_manifest_sha256": manifest["universe"]["manifest_sha256"],
        "rules_sha256": manifest["rules_sha256"],
        "config_snapshot_sha256": manifest["config"]["snapshot_sha256"],
        "code_hashes": manifest["code_hashes"],
    }
    manifest["seal"] = hashlib.sha256(
        json.dumps(
            seal_input,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return manifest


def test_validate_accepts_in_window_in_universe():
    rows = [
        {"symbol": "000001", "signal_day": "2026-09-15"},
        {"symbol": "000002", "signal_day": "2026-10-02"},
    ]
    checks = audit._validate_freeze_manifest(_manifest(), rows)
    assert checks["rules_match"] is True
    assert checks["all_signal_days_after_start"] is True
    assert checks["all_candidates_in_universe"] is True


def test_validate_rejects_pre_window_signal_days():
    rows = [{"symbol": "000001", "signal_day": "2026-08-31"}]
    checks = audit._validate_freeze_manifest(_manifest(), rows)
    assert checks["all_signal_days_after_start"] is False


def test_validate_rejects_candidates_outside_universe():
    rows = [
        {"symbol": "000001", "signal_day": "2026-09-15"},
        {"symbol": "999999", "signal_day": "2026-09-20"},
    ]
    checks = audit._validate_freeze_manifest(_manifest(), rows)
    assert checks["all_candidates_in_universe"] is False
    assert checks["candidates_outside_universe"] == ["999999"]


def test_validate_rejects_rule_drift():
    manifest = _manifest()
    manifest["rules"]["signal"] = "buy_1"
    rows = [{"symbol": "000001", "signal_day": "2026-09-15"}]
    checks = audit._validate_freeze_manifest(manifest, rows)
    assert checks["rules_match"] is False


def test_freeze_rule_definition_matches_audit_constants():
    rules = freeze.build_rule_definition()
    assert rules["signal"] == audit.NEAR_SIGNAL
    assert set(rules["weak_regimes"]) == set(audit.WEAK_REGIMES)
    assert rules["sample_gates"]["min_weak_near_unique_candidates"] == audit.MIN_WEAK_NEAR_COUNT
    assert rules["sample_gates"]["min_weak_near_unique_symbols"] == audit.MIN_WEAK_NEAR_UNIQUE_SYMBOLS
    assert rules["sample_gates"]["min_weak_near_unique_signal_days"] == audit.MIN_WEAK_NEAR_UNIQUE_DAYS


def test_freeze_rejects_window_before_2026_09(tmp_path, monkeypatch):
    monkeypatch.setattr(freeze, "HISTORY_DIR", tmp_path)
    args = type("A", (), {
        "start": "2026-08-01",
        "universe_file": None,
        "config": str(ROOT / "config" / "config.yaml"),
        "output_dir": str(tmp_path),
        "label": "x",
    })()
    with pytest.raises(ValueError, match="2026-09"):
        freeze.freeze(args)


def test_freeze_writes_seal(tmp_path, monkeypatch):
    monkeypatch.setattr(freeze, "HISTORY_DIR", tmp_path)
    args = type("A", (), {
        "start": "2026-09-01",
        "universe_file": None,
        "config": str(ROOT / "config" / "config.yaml"),
        "output_dir": str(tmp_path),
        "label": "x",
    })()
    summary = freeze.freeze(args)
    manifest_path = Path(summary["manifest_path"])
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["seal"] == summary["seal"]
    assert manifest["universe"]["count"] == 0  # empty tmp history dir
    assert manifest["version"] == "holdout_freeze.v2"
    assert "generate_holdout_candidates.py" in manifest["code_hashes"]
    assert "holdout_integrity.py" in manifest["code_hashes"]
    assert manifest["runtime"]["python"]
    assert manifest["runtime"]["pandas"]
    config_path = ROOT / "config" / "config.yaml"
    checks = audit._validate_freeze_manifest(
        manifest,
        [],
        config=freeze.load_config(str(config_path)),
        config_path=config_path,
        freeze_seal_path=tmp_path / "holdout_freeze.seal",
    )
    assert checks["seal_match"] is True
    assert checks["external_freeze_seal_match"] is True
    assert checks["rules_hash_match"] is True
    assert checks["code_hashes_match"] is True
    assert checks["runtime_versions_match"] is True
    assert checks["config_file_hash_match"] is True
    assert checks["config_snapshot_hash_match"] is True


def test_seal_tamper_detected():
    manifest = _sealed_manifest()
    rows = [{"symbol": "000001", "signal_day": "2026-09-15"}]
    checks = audit._validate_freeze_manifest(manifest, rows)
    assert checks["seal_match"] is True
    manifest["holdout_window"]["start"] = "2026-10-01"  # tamper
    checks = audit._validate_freeze_manifest(manifest, rows)
    assert checks["seal_match"] is False


def test_universe_hash_inconsistency_detected():
    manifest = _sealed_manifest()
    manifest["universe"]["manifest_sha256"] = "deadbeef"
    rows = [{"symbol": "000001", "signal_day": "2026-09-15"}]
    checks = audit._validate_freeze_manifest(manifest, rows)
    assert checks["universe_manifest_hash_match"] is False


def test_candidate_hash_mismatch_detected(tmp_path):
    manifest = _sealed_manifest()
    manifest["candidates"] = {"generated": True, "candidates_manifest_sha256": "deadbeef"}
    source = tmp_path / "candidates_holdout.jsonl"
    source.write_text(
        json.dumps({"symbol": "000001", "signal_day": "2026-09-15"}) + "\n",
        encoding="utf-8",
    )
    rows = [{"symbol": "000001", "signal_day": "2026-09-15"}]
    checks = audit._validate_freeze_manifest(manifest, rows, candidate_path=source)
    assert checks["candidate_manifest_hash_match"] is False


def test_candidate_hash_match_ok(tmp_path):
    manifest = _sealed_manifest()
    source = tmp_path / "candidates_holdout.jsonl"
    row = {"symbol": "000001", "signal_day": "2026-09-15"}
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    manifest["candidates"] = {
        "generated": True,
        "candidates_manifest_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    rows = [{"symbol": "000001", "signal_day": "2026-09-15"}]
    checks = audit._validate_freeze_manifest(manifest, rows, candidate_path=source)
    assert checks["candidate_manifest_hash_match"] is True


def test_external_freeze_seal_mismatch_detected(tmp_path):
    manifest = _sealed_manifest()
    seal_path = tmp_path / "holdout_freeze.seal"
    seal_path.write_text("deadbeef", encoding="utf-8")
    checks = audit._validate_freeze_manifest(
        manifest,
        [{"symbol": "000001", "signal_day": "2026-09-15"}],
        freeze_seal_path=seal_path,
    )
    assert checks["external_freeze_seal_match"] is False


def test_rules_hash_mismatch_detected():
    manifest = _sealed_manifest()
    checks = audit._validate_freeze_manifest(
        manifest,
        [{"symbol": "000001", "signal_day": "2026-09-15"}],
    )
    assert checks["rules_hash_match"] is False


def test_candidate_not_generated_is_rejected_for_holdout():
    manifest = _sealed_manifest()
    checks = audit._validate_freeze_manifest(
        manifest,
        [{"symbol": "000001", "signal_day": "2026-09-15"}],
    )
    assert checks["candidate_generated"] is False
    assert checks["candidate_manifest_hash_match"] is False
    assert checks["candidate_seal_match"] is False


def test_candidate_second_stage_seal_match(tmp_path):
    manifest = _sealed_manifest()
    source = tmp_path / "candidates_holdout.jsonl"
    row = {
        "symbol": "000001",
        "signal_day": "2026-09-15",
        "signal_type": audit.NEAR_SIGNAL,
        "candidate_id": f"000001|2026-09-15|{audit.NEAR_SIGNAL}",
    }
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    candidate_record = {
        "generated": True,
        "candidates_file": str(source),
        "candidates_manifest_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "candidate_ids_sha256": hashlib.sha256(
            row["candidate_id"].encode("utf-8")
        ).hexdigest(),
        "generated_at_utc": "2026-10-01T00:00:00+00:00",
        "collection_end": "2026-09-15",
        "data_cutoff": "2026-09-30",
        "universe_coverage": {"universe_count": 2, "qfq_history_count": 2},
        "generation_summary": {"frozen_candidate_count": 1},
    }
    candidate_record["universe_coverage_sha256"] = sha256_text(
        canonical_json(candidate_record["universe_coverage"])
    )
    candidate_record["generation_summary_sha256"] = sha256_text(
        canonical_json(candidate_record["generation_summary"])
    )
    candidate_record["candidate_seal"] = compute_candidate_seal(
        manifest["seal"], candidate_record
    )
    manifest["candidates"] = candidate_record
    seal_path = tmp_path / "holdout_candidates.seal"
    seal_path.write_text(candidate_record["candidate_seal"], encoding="utf-8")
    checks = audit._validate_freeze_manifest(
        manifest,
        [row],
        candidate_path=source,
        candidate_seal_path=seal_path,
    )
    assert checks["candidate_manifest_hash_match"] is True
    assert checks["candidate_ids_hash_match"] is True
    assert checks["candidate_seal_match"] is True
    assert checks["external_candidate_seal_match"] is True
    assert checks["universe_coverage_hash_match"] is True
    assert checks["generation_summary_hash_match"] is True
