from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import candidate_integrity as integrity  # noqa: E402


def _row(symbol="000001", day="2026-01-02", signal_type="macd_above", **extra):
    return {
        "symbol": symbol,
        "signal_day": day,
        "signal_type": signal_type,
        **extra,
    }


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_normalize_removes_only_identical_duplicate_candidate_rows():
    first = _row(score=1)
    second = _row(symbol="000002", score=2)
    normalized, report = integrity.normalize_candidate_rows(
        [first, second, dict(first)]
    )

    assert normalized == [first, second]
    assert report["input_rows"] == 3
    assert report["output_rows"] == 2
    assert report["removed_exact_duplicate_rows"] == 1
    assert report["exact_duplicates"] == [
        {
            "candidate_id": "000001|2026-01-02|macd_above",
            "first_line": 1,
            "duplicate_line": 3,
        }
    ]
    assert report["all_candidate_ids_unique"] is True


def test_normalize_rejects_same_id_with_different_content():
    with pytest.raises(integrity.CandidateIntegrityError, match="conflicting duplicate"):
        integrity.normalize_candidate_rows([_row(score=1), _row(score=2)])


def test_candidate_id_rejects_missing_identity_fields():
    with pytest.raises(integrity.CandidateIntegrityError, match="signal_type"):
        integrity.candidate_id({"symbol": "000001", "signal_day": "2026-01-02"})


def test_directory_normalization_writes_manifest_and_refuses_overwrite(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "canonical"
    for split in integrity.DEFAULT_SPLITS:
        rows = [_row(day=f"2026-01-0{index}") for index in (2, 3)]
        if split == "val":
            rows.append(dict(rows[0]))
        _write_jsonl(source / f"candidates_{split}.jsonl", rows)

    result = integrity.normalize_candidate_directory(source, target)

    assert result["all_candidate_ids_unique"] is True
    assert result["total_removed_exact_duplicate_rows"] == 1
    assert result["splits"]["val"]["removed_exact_duplicate_rows"] == 1
    assert (target / "candidate_integrity_manifest.json").exists()
    assert len(integrity.load_jsonl(target / "candidates_val.jsonl")) == 2
    with pytest.raises(integrity.CandidateIntegrityError, match="refusing overwrite"):
        integrity.normalize_candidate_directory(source, target)
