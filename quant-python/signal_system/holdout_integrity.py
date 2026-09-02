"""Deterministic integrity primitives for the two-stage holdout seal chain."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
from typing import Any, Iterable


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def universe_sha256(symbols: Iterable[str]) -> str:
    normalized = sorted({str(symbol).zfill(6) for symbol in symbols})
    return sha256_text("\n".join(normalized))


def candidate_ids_sha256(candidate_ids: Iterable[str]) -> str:
    return sha256_text("\n".join(sorted(str(value) for value in candidate_ids)))


def freeze_seal_input(manifest: dict[str, Any]) -> dict[str, Any]:
    universe = manifest.get("universe", {}) or {}
    config = manifest.get("config", {}) or {}
    value: dict[str, Any] = {
        "version": manifest.get("version"),
        "label": manifest.get("label"),
        "holdout_window": manifest.get("holdout_window"),
        "universe_manifest_sha256": universe.get("manifest_sha256"),
        "rules_sha256": manifest.get("rules_sha256"),
        "config_snapshot_sha256": config.get("snapshot_sha256"),
        "code_hashes": manifest.get("code_hashes"),
    }
    if str(manifest.get("version", "")).endswith(".v2"):
        value.update(
            {
                "frozen_at_utc": manifest.get("frozen_at_utc"),
                "universe_source": universe.get("source"),
                "universe_count": universe.get("count"),
                "config_file_sha256": config.get("file_sha256"),
                "runtime": manifest.get("runtime"),
            }
        )
    return value


def compute_freeze_seal(manifest: dict[str, Any]) -> str:
    return sha256_text(canonical_json(freeze_seal_input(manifest)))


_CANDIDATE_SEAL_KEYS = (
    "generated_at_utc",
    "collection_end",
    "data_cutoff",
    "candidates_manifest_sha256",
    "candidate_ids_sha256",
    "universe_coverage_sha256",
    "generation_summary_sha256",
)


def candidate_seal_input(
    freeze_seal: str,
    candidate_record: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": "holdout_candidates.v1",
        "freeze_seal": freeze_seal,
        **{key: candidate_record.get(key) for key in _CANDIDATE_SEAL_KEYS},
    }


def compute_candidate_seal(
    freeze_seal: str,
    candidate_record: dict[str, Any],
) -> str:
    return sha256_text(canonical_json(candidate_seal_input(freeze_seal, candidate_record)))


def read_seal(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def runtime_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {"python": platform.python_version()}
    for distribution in ("numpy", "pandas", "PyYAML"):
        try:
            result[distribution.lower()] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            result[distribution.lower()] = None
    return result
