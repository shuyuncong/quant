"""Read-only status inspection for the sealed MACD-near holdout workflow.

This command never updates market data, generates candidates, writes seals,
edits the freeze manifest, runs the formal audit, or connects to a database.
It reports the next allowed action from the current local artifacts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import HISTORY_DIR, prepare_closed_bars
from candidate_integrity import file_sha256, load_jsonl
from holdout_integrity import sha256_text
from macd_near_regime_audit import _validate_freeze_manifest
from utils.helpers import load_config


VERSION = "holdout_readiness_status.v1"
MIN_SIGNAL_DAYS_BEFORE_PROBE = 10
MIN_OUTCOME_SESSIONS = 40
WRITE_OPERATIONS_PERFORMED = False
MANIFEST_REQUIRED_CHECKS = (
    "seal_match",
    "external_freeze_seal_match",
    "rules_hash_match",
    "rules_match",
    "universe_count_match",
    "universe_manifest_hash_match",
    "code_hashes_match",
    "runtime_versions_match",
    "config_file_hash_match",
    "config_snapshot_hash_match",
)
CANDIDATE_REQUIRED_CHECKS = (
    "candidate_generated",
    "all_signal_days_after_start",
    "all_candidates_in_universe",
    "candidate_manifest_hash_match",
    "candidate_ids_hash_match",
    "candidate_seal_match",
    "external_candidate_seal_match",
    "universe_coverage_hash_match",
    "generation_summary_hash_match",
)


def _decide_state(
    *,
    manifest_integrity_ok: bool,
    cache_coverage_ok: bool,
    index_available: bool,
    holdout_trading_days: int,
    candidate_generated: bool,
    candidate_integrity_ok: bool | None,
    outcome_sessions_after_collection_end: int | None,
) -> str:
    if not (manifest_integrity_ok and cache_coverage_ok and index_available):
        return "data_cache_not_ready"
    if not candidate_generated:
        return (
            "ready_to_probe_candidates"
            if holdout_trading_days >= MIN_SIGNAL_DAYS_BEFORE_PROBE
            else "waiting_for_signal_days"
        )
    if candidate_integrity_ok is not True:
        return "sealed_integrity_failed"
    if (
        outcome_sessions_after_collection_end is None
        or outcome_sessions_after_collection_end < MIN_OUTCOME_SESSIONS
    ):
        return "sealed_waiting_outcomes"
    return "ready_for_final_audit"


def _index_summary(
    frame: pd.DataFrame,
    *,
    holdout_start: str,
    collection_end: str | None,
) -> dict[str, Any]:
    closed = prepare_closed_bars(frame)
    if closed.empty:
        return {
            "available": False,
            "bars": 0,
            "first_date": None,
            "latest_date": None,
            "holdout_trading_days": 0,
            "outcome_sessions_after_collection_end": None,
        }
    days = sorted({pd.Timestamp(value).date() for value in closed["datetime"]})
    start = pd.Timestamp(holdout_start).date()
    holdout_days = [day for day in days if day >= start]
    outcome_sessions = None
    if collection_end:
        end = pd.Timestamp(collection_end).date()
        outcome_sessions = sum(day > end for day in days)
    return {
        "available": True,
        "bars": len(closed),
        "first_date": days[0].isoformat(),
        "latest_date": days[-1].isoformat(),
        "holdout_trading_days": len(holdout_days),
        "outcome_sessions_after_collection_end": outcome_sessions,
    }


def _history_coverage(
    symbols: list[str],
    history_dir: Path,
) -> dict[str, Any]:
    missing_none: list[str] = []
    missing_qfq: list[str] = []
    qfq_hashes: list[str] = []
    for symbol in symbols:
        none_path = history_dir / f"{symbol}_none.pkl"
        qfq_path = history_dir / f"{symbol}_qfq.pkl"
        if not none_path.exists():
            missing_none.append(symbol)
        if not qfq_path.exists():
            missing_qfq.append(symbol)
        else:
            qfq_hashes.append(f"{symbol}|{file_sha256(qfq_path)}")
    return {
        "universe_count": len(symbols),
        "none_history_count": len(symbols) - len(missing_none),
        "qfq_history_count": len(symbols) - len(missing_qfq),
        "missing_none_count": len(missing_none),
        "missing_qfq_count": len(missing_qfq),
        "missing_none": missing_none,
        "missing_qfq": missing_qfq,
        "qfq_history_manifest_sha256": sha256_text("\n".join(qfq_hashes)),
        "all_present": not missing_none and not missing_qfq,
    }


def _recommended_action(state: str) -> str:
    return {
        "data_cache_not_ready": (
            "repair local cache or freeze integrity; do not generate candidates"
        ),
        "waiting_for_signal_days": (
            "update local closed-bar cache and wait until at least 10 holdout "
            "trading days; do not run the generator"
        ),
        "ready_to_probe_candidates": (
            "another model may run generate_holdout_candidates.py once with the "
            "latest completed local-data cutoff"
        ),
        "sealed_integrity_failed": (
            "stop; do not audit or regenerate; investigate sealed artifact drift"
        ),
        "sealed_waiting_outcomes": (
            "keep sealed candidates unchanged, update only local market history, "
            "and wait for 40 post-collection sessions"
        ),
        "ready_for_final_audit": (
            "assemble canonical val plus sealed holdout and run the one-time formal audit"
        ),
    }[state]


def inspect(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest_dir = manifest_path.parent
    freeze_seal_path = manifest_dir / "holdout_freeze.seal"
    candidate_path = manifest_dir / "candidates_holdout.jsonl"
    candidate_seal_path = manifest_dir / "holdout_candidates.seal"
    config_path = Path(args.config).expanduser().resolve()
    history_dir = Path(args.history_dir).expanduser().resolve()
    index_path = Path(args.index_data).expanduser().resolve()

    if not manifest_path.exists():
        raise FileNotFoundError(f"freeze manifest not found: {manifest_path}")
    with manifest_path.open(encoding="utf-8") as handle:
        freeze = json.load(handle)
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")

    symbols = sorted(
        {str(symbol).zfill(6) for symbol in freeze.get("universe", {}).get("symbols", [])}
    )
    coverage = _history_coverage(symbols, history_dir)
    manifest_checks = _validate_freeze_manifest(
        freeze,
        [],
        config=config,
        config_path=config_path,
        freeze_seal_path=freeze_seal_path,
    )
    manifest_integrity_ok = all(
        manifest_checks.get(name) is True for name in MANIFEST_REQUIRED_CHECKS
    )

    candidate_record = freeze.get("candidates", {}) or {}
    candidate_generated = bool(candidate_record.get("generated"))
    collection_end = (
        str(candidate_record.get("collection_end"))
        if candidate_record.get("collection_end")
        else None
    )
    candidate_checks = None
    candidate_integrity_ok = None
    candidate_count = None
    if candidate_generated:
        candidates = load_jsonl(candidate_path) if candidate_path.exists() else []
        candidate_count = len(candidates)
        candidate_checks = _validate_freeze_manifest(
            freeze,
            candidates,
            config=config,
            config_path=config_path,
            candidate_path=candidate_path,
            freeze_seal_path=freeze_seal_path,
            candidate_seal_path=candidate_seal_path,
        )
        candidate_integrity_ok = all(
            candidate_checks.get(name) is True for name in CANDIDATE_REQUIRED_CHECKS
        )

    if index_path.exists():
        try:
            index_frame = pd.read_pickle(index_path)
            index = _index_summary(
                index_frame,
                holdout_start=str(freeze["holdout_window"]["start"]),
                collection_end=collection_end,
            )
            index["sha256"] = file_sha256(index_path)
            index["path"] = str(index_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            index = {
                "available": False,
                "path": str(index_path),
                "error": f"{type(exc).__name__}: {exc}",
                "holdout_trading_days": 0,
                "outcome_sessions_after_collection_end": None,
            }
    else:
        index = {
            "available": False,
            "path": str(index_path),
            "error": "missing_index_file",
            "holdout_trading_days": 0,
            "outcome_sessions_after_collection_end": None,
        }

    state = _decide_state(
        manifest_integrity_ok=manifest_integrity_ok,
        cache_coverage_ok=bool(coverage["all_present"]),
        index_available=bool(index.get("available")),
        holdout_trading_days=int(index.get("holdout_trading_days", 0)),
        candidate_generated=candidate_generated,
        candidate_integrity_ok=candidate_integrity_ok,
        outcome_sessions_after_collection_end=index.get(
            "outcome_sessions_after_collection_end"
        ),
    )
    return {
        "version": VERSION,
        "state": state,
        "recommended_action": _recommended_action(state),
        "write_operations_performed": WRITE_OPERATIONS_PERFORMED,
        "production_database_connected": False,
        "thresholds": {
            "minimum_holdout_trading_days_before_probe": (
                MIN_SIGNAL_DAYS_BEFORE_PROBE
            ),
            "minimum_outcome_sessions_after_collection_end": (
                MIN_OUTCOME_SESSIONS
            ),
            "outcome_maturity_basis": (
                "index-session proxy only; formal audit must still require "
                "baseline_replay_complete because suspended stocks may mature later"
            ),
        },
        "freeze": {
            "manifest": str(manifest_path),
            "manifest_sha256": file_sha256(manifest_path),
            "freeze_seal": str(freeze_seal_path),
            "freeze_seal_sha256": (
                file_sha256(freeze_seal_path) if freeze_seal_path.exists() else None
            ),
            "holdout_start": freeze.get("holdout_window", {}).get("start"),
            "manifest_integrity_ok": manifest_integrity_ok,
            "checks": {
                name: manifest_checks.get(name) for name in MANIFEST_REQUIRED_CHECKS
            },
        },
        "history_coverage": coverage,
        "index": index,
        "candidates": {
            "generated": candidate_generated,
            "candidate_file_exists": candidate_path.exists(),
            "candidate_seal_exists": candidate_seal_path.exists(),
            "candidate_count": candidate_count,
            "collection_end": collection_end,
            "integrity_ok": candidate_integrity_ok,
            "checks": (
                {
                    name: candidate_checks.get(name)
                    for name in CANDIDATE_REQUIRED_CHECKS
                }
                if candidate_checks is not None
                else None
            ),
        },
        "config": {
            "path": str(config_path),
            "sha256": file_sha256(config_path),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=r"D:\tmp\holdout_full_a_v2_final\holdout_freeze.json",
    )
    parser.add_argument(
        "--history-dir",
        default=str(HISTORY_DIR),
    )
    parser.add_argument(
        "--index-data",
        default=str(BASE_DIR / "cache" / "index_000001_sh.pkl"),
    )
    parser.add_argument(
        "--config",
        default=str(BASE_DIR / "config" / "config.yaml"),
    )
    args = parser.parse_args()
    try:
        result = inspect(args)
    except (RuntimeError, FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "version": VERSION,
            "state": "data_cache_not_ready",
            "recommended_action": (
                "repair local cache or freeze integrity; do not generate candidates"
            ),
            "write_operations_performed": WRITE_OPERATIONS_PERFORMED,
            "production_database_connected": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["state"] in {
        "data_cache_not_ready",
        "sealed_integrity_failed",
    } else 0


if __name__ == "__main__":
    raise SystemExit(main())
