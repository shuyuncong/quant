import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import generate_holdout_candidates as generator  # noqa: E402
import macd_near_regime_audit as audit  # noqa: E402


def _candidate(symbol, day, *, regime="bull", signal_type="buy_1"):
    return {
        "symbol": symbol,
        "signal_day": day,
        "signal_type": signal_type,
        "market_context": {"regime": regime},
        "pnl_pct": -9.9,
        "future_40d": 20.0,
        "exit_reason": "stop_loss",
    }


def test_select_collection_end_is_first_day_all_gates_are_met(monkeypatch):
    monkeypatch.setattr(generator, "MIN_WEAK_NEAR_COUNT", 3)
    monkeypatch.setattr(generator, "MIN_WEAK_NEAR_UNIQUE_SYMBOLS", 2)
    monkeypatch.setattr(generator, "MIN_WEAK_NEAR_UNIQUE_DAYS", 2)
    rows = [
        _candidate("000001", "2026-09-02", regime="bear", signal_type=audit.NEAR_SIGNAL),
        _candidate("000002", "2026-09-03", regime="range", signal_type=audit.NEAR_SIGNAL),
        _candidate("000001", "2026-09-03", regime="bear", signal_type=audit.NEAR_SIGNAL),
        _candidate("000003", "2026-09-04", regime="bull", signal_type="buy_1"),
    ]
    end, gate = generator._select_collection_end(rows)
    assert end == "2026-09-03"
    assert gate["sufficient"] is True


def test_sanitize_candidate_removes_all_outcome_fields():
    row = _candidate(
        "000001", "2026-09-02", regime="bear", signal_type=audit.NEAR_SIGNAL
    )
    clean = generator._sanitize_candidate(row)
    assert clean["regime"] == "bear"
    assert clean["candidate_id"].startswith("000001|2026-09-02|")
    for forbidden in generator.OUTCOME_FIELDS:
        assert forbidden not in clean


def test_generation_refuses_existing_artifacts(tmp_path):
    manifest = tmp_path / "holdout_freeze.json"
    manifest.write_text(json.dumps({"candidates": {"generated": False}}), encoding="utf-8")
    candidate_path = tmp_path / "candidates_holdout.jsonl"
    candidate_path.write_text("already here", encoding="utf-8")
    with pytest.raises(RuntimeError, match="already exists"):
        generator._assert_generation_target_unused(
            json.loads(manifest.read_text(encoding="utf-8")),
            candidate_path,
            tmp_path / "holdout_candidates.seal",
        )


def test_universe_comparison_rejects_omitted_symbol(tmp_path):
    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps({"symbols": ["000001"]}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="does not match"):
        generator._verify_optional_universe_file(
            universe_path, ["000001", "000002"]
        )


def test_local_history_coverage_fails_closed(tmp_path):
    (tmp_path / "000001_none.pkl").write_bytes(b"x")
    (tmp_path / "000001_qfq.pkl").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="coverage incomplete"):
        generator._build_history_coverage(
            ["000001", "000002"], tmp_path
        )


def test_generate_finalizes_once_with_entry_only_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "MIN_WEAK_NEAR_COUNT", 2)
    monkeypatch.setattr(generator, "MIN_WEAK_NEAR_UNIQUE_SYMBOLS", 2)
    monkeypatch.setattr(generator, "MIN_WEAK_NEAR_UNIQUE_DAYS", 2)
    manifest_path = tmp_path / "holdout_freeze.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "holdout_freeze.v2",
                "seal": "frozen-seal",
                "holdout_window": {"start": "2026-09-01"},
                "universe": {"symbols": ["000001", "000002"]},
                "candidates": {"generated": False},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "holdout_freeze.seal").write_text(
        "frozen-seal", encoding="utf-8"
    )
    monkeypatch.setattr(generator, "load_config", lambda _: {"risk": {}})
    monkeypatch.setattr(generator, "_manifest_preflight", lambda *args: {})
    monkeypatch.setattr(
        generator,
        "_build_history_coverage",
        lambda symbols, history_dir: {
            "universe_count": len(symbols),
            "qfq_history_count": len(symbols),
        },
    )
    raw = [
        _candidate(
            "000001", "2026-09-02", regime="bear", signal_type=audit.NEAR_SIGNAL
        ),
        _candidate(
            "000002", "2026-09-03", regime="range", signal_type=audit.NEAR_SIGNAL
        ),
        _candidate("000001", "2026-09-04", regime="bull", signal_type="buy_1"),
    ]
    monkeypatch.setattr(
        generator,
        "_run_production_candidate_pipeline",
        lambda **kwargs: (raw, {"source_report_sha256": "x"}),
    )
    args = Namespace(
        manifest=str(manifest_path),
        output_dir=tmp_path,
        config=str(tmp_path / "config.yaml"),
        universe_file=None,
        index_data=str(tmp_path / "index.pkl"),
        data_cutoff="2026-09-30",
        workers=1,
        history_bars=800,
    )
    result = generator.generate(args)
    assert result["status"] == "candidates_sealed"
    assert result["collection_end"] == "2026-09-03"
    rows = [
        json.loads(line)
        for line in (tmp_path / "candidates_holdout.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 2
    assert all(not generator.OUTCOME_FIELDS.intersection(row) for row in rows)
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["candidates"]["generated"] is True
    assert (tmp_path / "holdout_candidates.seal").exists()
    with pytest.raises(RuntimeError, match="already generated"):
        generator.generate(args)
