from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import candidate_integrity as integrity  # noqa: E402
import macd_cross_quality_audit as audit  # noqa: E402


def _row(symbol, day, gap):
    return {
        "candidate_id": f"{symbol}|{day}|macd_golden_cross_pullback_confirmed_above",
        "symbol": symbol,
        "signal_day": day,
        "signal_type": "macd_golden_cross_pullback_confirmed_above",
        "gap_strength": gap,
    }


def _write_jsonl(path: Path, rows):
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def test_primary_factor_is_predeclared_and_diagnostics_cannot_gate():
    assert audit.PRIMARY_FACTOR == "gap_strength"
    assert audit.FACTOR_SPECS["gap_strength"]["role"] == "primary"
    assert audit.FACTOR_SPECS["hist_slope_3"]["role"] == "diagnostic"
    assert audit.FACTOR_SPECS["confirmation_count"]["role"] == "diagnostic"
    assert audit.FACTOR_SPECS["confirmation_wait_bars"]["role"] == "diagnostic"


def test_primary_halves_are_assigned_within_signal_day():
    rows = [
        _row("000001", "2026-01-02", 0.1),
        _row("000002", "2026-01-02", 0.2),
        _row("000003", "2026-01-03", 0.9),
        _row("000004", "2026-01-03", 0.3),
        _row("000005", "2026-01-04", 0.8),
    ]
    audit._assign_primary_halves(rows)
    included = {row["symbol"] for row in rows if row["variant_included"]}
    assert included == {"000002", "000003"}
    assert rows[-1]["primary_high_quality"] is None


def test_signal_zone_only_accepts_macd_candidate_types():
    assert audit._signal_zone(
        {"signal_type": "macd_golden_cross_pullback_confirmed_near"}
    ) == "near"
    assert audit._signal_zone({"signal_type": "buy_1"}) is None


def test_integrity_manifest_validation_accepts_canonical_file(tmp_path):
    rows = [_row("000001", "2026-01-02", 0.1)]
    source = tmp_path / "candidates_train.jsonl"
    _write_jsonl(source, rows)
    manifest = {
        "version": integrity.VERSION,
        "output_dir": str(tmp_path.resolve()),
        "splits": {
            "train": {
                "output_file": str(source.resolve()),
                "output_sha256": integrity.file_sha256(source),
                "output_rows": 1,
                "candidate_ids_sha256": integrity.candidate_ids_sha256(rows),
            }
        },
    }
    (tmp_path / "candidate_integrity_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    checks = audit._validate_integrity_manifest(
        tmp_path.resolve(), "train", source.resolve(), rows
    )
    assert checks["all_pass"] is True


def test_integrity_manifest_validation_rejects_hash_drift(tmp_path):
    rows = [_row("000001", "2026-01-02", 0.1)]
    source = tmp_path / "candidates_train.jsonl"
    _write_jsonl(source, rows)
    manifest = {
        "version": integrity.VERSION,
        "output_dir": str(tmp_path.resolve()),
        "splits": {
            "train": {
                "output_file": str(source.resolve()),
                "output_sha256": "bad",
                "output_rows": 1,
                "candidate_ids_sha256": integrity.candidate_ids_sha256(rows),
            }
        },
    }
    (tmp_path / "candidate_integrity_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="integrity validation failed"):
        audit._validate_integrity_manifest(
            tmp_path.resolve(), "train", source.resolve(), rows
        )
