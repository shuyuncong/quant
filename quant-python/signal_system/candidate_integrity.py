"""Canonicalize research candidate exports without weakening uniqueness rules.

The production signal generator is part of the sealed 2026-09 holdout code
surface, so development audits use this separate, research-only export gate.
It removes only semantically identical rows that share a candidate id.  If the
same id carries different content, the gate fails closed instead of choosing a
record and introducing an implicit factor rule.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


VERSION = "candidate_integrity.v1"
DEFAULT_SPLITS = ("train", "val", "test")
REQUIRED_ID_FIELDS = ("symbol", "signal_day", "signal_type")


class CandidateIntegrityError(RuntimeError):
    """Raised when a candidate export cannot be canonicalized safely."""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_id(row: dict[str, Any]) -> str:
    missing = [
        field
        for field in REQUIRED_ID_FIELDS
        if not str(row.get(field, "")).strip()
    ]
    if missing:
        raise CandidateIntegrityError(
            f"candidate is missing required id field(s): {', '.join(missing)}"
        )
    return (
        f"{str(row['symbol']).zfill(6)}|{row['signal_day']}|"
        f"{row['signal_type']}"
    )


def _canonical_row(row: dict[str, Any]) -> str:
    return json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def candidate_ids_sha256(rows: Iterable[dict[str, Any]]) -> str:
    ids = sorted(candidate_id(row) for row in rows)
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def normalize_candidate_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop exact duplicate ids and reject conflicting duplicate ids."""
    first_by_id: dict[str, tuple[int, str]] = {}
    normalized: list[dict[str, Any]] = []
    exact_duplicates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for line_number, row in enumerate(rows, start=1):
        identifier = candidate_id(row)
        canonical = _canonical_row(row)
        previous = first_by_id.get(identifier)
        if previous is None:
            first_by_id[identifier] = (line_number, canonical)
            normalized.append(dict(row))
            continue
        first_line, first_canonical = previous
        detail = {
            "candidate_id": identifier,
            "first_line": first_line,
            "duplicate_line": line_number,
        }
        if canonical == first_canonical:
            exact_duplicates.append(detail)
        else:
            conflicts.append(detail)

    if conflicts:
        conflict_text = ", ".join(
            f"{item['candidate_id']}@{item['first_line']}/{item['duplicate_line']}"
            for item in conflicts
        )
        raise CandidateIntegrityError(
            "conflicting duplicate candidate ids; refusing implicit selection: "
            + conflict_text
        )

    return normalized, {
        "input_rows": len(rows),
        "output_rows": len(normalized),
        "removed_exact_duplicate_rows": len(exact_duplicates),
        "exact_duplicates": exact_duplicates,
        "conflicting_duplicate_rows": 0,
        "unique_candidate_ids": len(first_by_id),
        "candidate_ids_sha256": candidate_ids_sha256(normalized),
        "all_candidate_ids_unique": len(normalized) == len(first_by_id),
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise CandidateIntegrityError(
                    f"candidate row must be an object: {path}:{line_number}"
                )
            rows.append(value)
    return rows


def _write_jsonl_once(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                + "\n"
            )


def normalize_candidate_directory(
    input_dir: Path,
    output_dir: Path,
    splits: Iterable[str] = DEFAULT_SPLITS,
) -> dict[str, Any]:
    input_dir = input_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if input_dir == output_dir:
        raise CandidateIntegrityError("input and output directories must differ")
    if output_dir.exists():
        raise CandidateIntegrityError(
            f"output directory already exists; refusing overwrite: {output_dir}"
        )

    prepared: dict[str, tuple[Path, list[dict[str, Any]], dict[str, Any]]] = {}
    for split in splits:
        source = input_dir / f"candidates_{split}.jsonl"
        if not source.exists():
            raise CandidateIntegrityError(f"candidate input does not exist: {source}")
        normalized, report = normalize_candidate_rows(load_jsonl(source))
        report.update(
            {
                "input_file": str(source),
                "input_sha256": file_sha256(source),
            }
        )
        prepared[str(split)] = (source, normalized, report)

    output_dir.mkdir(parents=True, exist_ok=False)
    split_reports: dict[str, Any] = {}
    for split, (_, rows, report) in prepared.items():
        target = output_dir / f"candidates_{split}.jsonl"
        _write_jsonl_once(target, rows)
        report.update(
            {
                "output_file": str(target),
                "output_sha256": file_sha256(target),
            }
        )
        split_reports[split] = report

    result = {
        "version": VERSION,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "script_sha256": file_sha256(Path(__file__).resolve()),
        "splits": split_reports,
        "all_candidate_ids_unique": all(
            report["all_candidate_ids_unique"]
            for report in split_reports.values()
        ),
        "total_removed_exact_duplicate_rows": sum(
            report["removed_exact_duplicate_rows"]
            for report in split_reports.values()
        ),
    }
    manifest = output_dir / "candidate_integrity_manifest.json"
    with manifest.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    result["manifest"] = str(manifest)
    result["manifest_sha256"] = file_sha256(manifest)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--splits", nargs="+", default=list(DEFAULT_SPLITS))
    args = parser.parse_args()
    result = normalize_candidate_directory(
        Path(args.input_dir),
        Path(args.output_dir),
        args.splits,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
