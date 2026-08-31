import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import freeze_holdout as freeze  # noqa: E402
import macd_near_regime_audit as audit  # noqa: E402


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
    import pytest
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
