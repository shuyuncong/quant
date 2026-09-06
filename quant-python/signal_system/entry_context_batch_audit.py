"""Batch audit entry-context stability and one frozen zero-axis factor.

The audit performs exactly one in-process ``production_risk`` replay for each
of train, validation, and test.  The immutable replay is then projected into
two independent studies:

* a diagnostic-only strategy stability cube; and
* a registered historical candidate audit for signal-day MACD zero-axis
  distance, frozen as ``abs(DIF) / QFQ close`` with smaller values preferred.

There is no batch OR gate.  Holdout, portfolio construction, databases,
network access, parameter scans, reverse-direction tests, and production
changes are forbidden.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import canonical_replay_semantics_audit as replay_contract
from attribution_audit import FIELDS, _config_snapshot, exit_reason_category
from attribution_audit import _replay_split as _production_replay_split
from backtest_winrate import HISTORY_DIR, prepare_closed_bars
from candidate_integrity import (
    VERSION as INTEGRITY_VERSION,
)
from candidate_integrity import (
    candidate_id,
    candidate_ids_sha256,
    file_sha256,
    load_jsonl,
)
from strategy.macd import calculate_macd, find_golden_cross_entries
from utils.helpers import load_config

VERSION = "entry_context_batch_audit.v1"
SPLITS = ("train", "val", "test")
PREREGISTERED_LABEL = "entry-context-batch-audit"
PREREGISTERED_SEED = 20260908
PREREGISTERED_BOOTSTRAP_REPS = 2000
CANONICAL_INPUT_DIR = Path(r"D:\tmp\candidates_fullpool_canonical")
FORMAL_OUTPUT_DIR = Path(r"D:\tmp\entry_context_batch_audit_final")
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
PREREGISTERED_CONFIG_SHA256 = (
    "4e0084eb945aa2533dca25f9e0f68b4e27e9f2cbc52c11b7898c082d466000f1"
)
PREREGISTERED_CANONICAL_SHA256 = {
    "train": "28a2defd6f7f09904295cb270cc311bf989add84b4cd0c8dc947d4e59a4fb40f",
    "val": "921a2ddc6916030a270aa136c0ce05eabdcddfb9ab289ec07c4ef5e8d2ba2bd0",
    "test": "d90836a21610f77c6bb6afca3169d3eedb79c228026415f2e8da956fc8def174",
}
PREREGISTERED_INTEGRITY_MANIFEST_SHA256 = (
    "4e12f3d53851e0cc7f49a3c67f4fcdf84d3b2584630e166c0a0cf9a997e01753"
)
PREREGISTERED_HISTORY_MANIFEST_SHA256 = (
    "ca99ebb0d9374a3159abe5ae7fe8f72a757c3c810355729390a0e6e006f3b968"
)

DEPENDENCY_PATHS = {
    "attribution_audit": BASE_DIR / "attribution_audit.py",
    "backtest_winrate": BASE_DIR / "backtest_winrate.py",
    "candidate_integrity": BASE_DIR / "candidate_integrity.py",
    "canonical_replay_semantics_audit": BASE_DIR
    / "canonical_replay_semantics_audit.py",
    "strategy_chan": BASE_DIR / "strategy" / "chan.py",
    "strategy_macd": BASE_DIR / "strategy" / "macd.py",
    "strategy_signal_policy": BASE_DIR / "strategy" / "signal_policy.py",
    "strategy_market_gate": BASE_DIR / "strategy" / "market_gate.py",
    "utils_helpers": BASE_DIR / "utils" / "helpers.py",
}
DEPENDENCY_EXPECTED_SHA256 = {
    "attribution_audit": "fdb13ef7d20bdf024a14f6dffe17d47c14ec73ddcfd72044bca527eb34154f4d",
    "backtest_winrate": "586e21d8050a4397f2664b41676c2c5c140754f161c1937e27a2fd7e875800d1",
    "candidate_integrity": "fb7e197a7bb11963b5f6b2042965f946b48bef4ab89789ca1d7c5ad4cfb4e53f",
    "canonical_replay_semantics_audit": "267902f36b6d85b0d96afb20de552eb20848d1c7084d1f4370be7caed10c8d2c",
    "strategy_chan": "af70eab3207c168254f70e8674fd1ce6293e0b7937a8a2fb8a842640b306d058",
    "strategy_macd": "cb1e5ff36086fca041d899199c7ed8a093929feff294bc0f40da2c65522866c9",
    "strategy_signal_policy": "2288fd4492f860a1705d1539d7b1b5b396a44f81c9e333cb6fafdc3ea2b56d34",
    "strategy_market_gate": "fa69dbee01817e44b7b4bc73c50c9a37fa5b4f8b4eb08460dc134979d6d7abe6",
    "utils_helpers": "3ec75b774efe60cbe678f5dbda78a84a70a6a12442a1f458e121bc33e6804ae1",
}

STUDY_REGISTRY = {
    "strategy_stability_diagnostic": {
        "role": "safety_diagnostic_only",
        "candidate_gate_implemented": False,
        "production_decision_authority": False,
    },
    "zero_axis_distance_candidate": {
        "role": "registered_historical_candidate",
        "candidate_gate_implemented": True,
        "production_decision_authority": False,
    },
}
PRIMARY_FACTOR = "macd_zero_axis_distance"
PRIMARY_OUTCOMES = ("future_40d", "trade_pnl_pct")
MACD_PARAMETERS = {"fast": 12, "slow": 26, "signal": 9}
MIN_MACD_BARS = MACD_PARAMETERS["slow"] + MACD_PARAMETERS["signal"] - 1
ZERO_AXIS_TOLERANCE = 0.005
CONFIRMATION_BARS = 5
ALLOWED_ZONES = ("above", "near")
MACD_SIGNAL_TYPES = (
    "macd_golden_cross_pullback_confirmed_above",
    "macd_golden_cross_pullback_confirmed_near",
)
REGISTERED_SIGNAL_TYPES = (*MACD_SIGNAL_TYPES, "buy_1")
REGISTERED_REGIMES = ("bull", "range", "bear")
MIN_ASSIGNMENT_COVERAGE = 0.90
MIN_AFFECTED_CANDIDATES = 30
MIN_AFFECTED_SYMBOLS = 10
MIN_AFFECTED_SIGNAL_DAYS = 10
MIN_OUTCOME_CLUSTERS = 10
STRATEGY_SMALL_N = 30
SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
SOURCE_LEGACY_OUTCOME_FIELDS = frozenset(
    {
        *FIELDS,
        "entry_day",
        "exit_reason",
        "holding_days",
    }
)
ZERO_AXIS_FEATURE_FIELDS = (
    "candidate_id",
    PRIMARY_FACTOR,
    "zero_axis_factor_assignment_available",
    "zero_axis_factor_error",
    "zero_axis_nearer",
    "zero_axis_stratum",
    "zero_axis_stratum_median",
    "zero_axis_entry_timing",
    "zero_axis_matching_entry_count",
    "zero_axis_selected_cross_day",
    "zero_axis_selected_cross_index",
    "zero_axis_confirmation_index",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha_manifest(items: Iterable[tuple[str, str]]) -> str:
    payload = "\n".join(f"{key}|{value}" for key, value in sorted(items))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snapshot_named_paths(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for name, raw_path in sorted(paths.items()):
        path = raw_path.expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"protected input does not exist: {path}")
        snapshot[name] = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
    return snapshot


def _snapshot_manifest(snapshot: dict[str, dict[str, Any]]) -> str:
    return _sha_manifest(
        (
            name,
            f"{item['path']}|{item['size_bytes']}|{item['sha256']}",
        )
        for name, item in snapshot.items()
    )


def _snapshot_changes(
    initial: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    current = _snapshot_named_paths(
        {name: Path(item["path"]) for name, item in initial.items()}
    )
    changes: list[dict[str, Any]] = []
    for name, before in initial.items():
        after = current[name]
        if (
            before["sha256"] != after["sha256"]
            or before["size_bytes"] != after["size_bytes"]
        ):
            changes.append(
                {
                    "name": name,
                    "path": before["path"],
                    "before_sha256": before["sha256"],
                    "after_sha256": after["sha256"],
                    "before_size_bytes": before["size_bytes"],
                    "after_size_bytes": after["size_bytes"],
                }
            )
    return current, changes


def _static_protected_paths(input_dir: Path, config_path: Path) -> dict[str, Path]:
    paths = {
        "audit_script": Path(__file__).resolve(),
        "config": config_path,
        "candidate_integrity_manifest": input_dir
        / "candidate_integrity_manifest.json",
        **DEPENDENCY_PATHS,
    }
    for split in SPLITS:
        paths[f"canonical_{split}"] = input_dir / f"candidates_{split}.jsonl"
    return paths


def _history_paths(symbols: Iterable[str]) -> dict[str, Path]:
    return {
        f"qfq:{symbol}": HISTORY_DIR / f"{symbol}_qfq.pkl"
        for symbol in sorted({_normalize_symbol(value) for value in symbols})
    }


def _expected_static_hashes(expected_script_sha256: str) -> dict[str, str]:
    return {
        "audit_script": expected_script_sha256,
        "config": PREREGISTERED_CONFIG_SHA256,
        "candidate_integrity_manifest": PREREGISTERED_INTEGRITY_MANIFEST_SHA256,
        **DEPENDENCY_EXPECTED_SHA256,
        **{
            f"canonical_{split}": value
            for split, value in PREREGISTERED_CANONICAL_SHA256.items()
        },
    }


def _validate_expected_snapshot(
    snapshot: dict[str, dict[str, Any]], expected: dict[str, str]
) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for name in sorted(expected):
        actual = snapshot.get(name, {}).get("sha256")
        report[name] = {
            "path": snapshot.get(name, {}).get("path"),
            "expected_sha256": expected[name],
            "actual_sha256": actual,
            "match": actual == expected[name],
        }
    if not all(item["match"] for item in report.values()):
        raise RuntimeError(f"protected static SHA contract failed: {report}")
    return report


def _normalize_symbol(value: Any) -> str:
    symbol = str(value).strip()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise RuntimeError(f"canonical symbol must be six digits: {value!r}")
    return symbol


def _normalize_signal_day(value: Any) -> str:
    raw = str(value).strip()
    try:
        normalized = date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise RuntimeError(f"invalid signal_day: {value!r}") from exc
    if normalized != raw:
        raise RuntimeError(f"signal_day must be canonical ISO date: {value!r}")
    return normalized


def _identity_projection(row: dict[str, Any]) -> dict[str, str]:
    for field in ("symbol", "signal_day", "signal_type"):
        if not str(row.get(field, "")).strip():
            raise RuntimeError(f"candidate identity field is missing: {field}")
    return {
        "symbol": _normalize_symbol(row["symbol"]),
        "signal_day": _normalize_signal_day(row["signal_day"]),
        "signal_type": str(row["signal_type"]),
    }


def _validate_symbol_day_uniqueness(rows: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for row in rows:
        identity = _identity_projection(row)
        key = (identity["symbol"], identity["signal_day"])
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        raise RuntimeError(
            "canonical symbol x signal_day must be unique: "
            f"{sorted(set(duplicates))[:10]}"
        )


def _replay_input_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key not in SOURCE_LEGACY_OUTCOME_FIELDS
    }


def _factor_input_projection(row: dict[str, Any]) -> dict[str, str]:
    return _identity_projection(row)


def _feature_manifest(feature_map: dict[str, dict[str, Any]]) -> str:
    projection = [
        {field: feature_map[identifier].get(field) for field in ZERO_AXIS_FEATURE_FIELDS}
        for identifier in sorted(feature_map)
    ]
    return _sha256_json(projection)


def _enriched_feature_manifest(rows: Sequence[dict[str, Any]]) -> str:
    feature_map = {str(row["candidate_id"]): row for row in rows}
    if len(feature_map) != len(rows):
        raise RuntimeError("enriched rows contain duplicate candidate ids")
    return _feature_manifest(feature_map)


def _assert_feature_manifest(
    feature_map: dict[str, dict[str, Any]], expected: str, stage: str
) -> None:
    actual = _feature_manifest(feature_map)
    if actual != expected:
        raise RuntimeError(
            f"zero-axis feature manifest changed at {stage}: "
            f"expected={expected}, actual={actual}"
        )


def _assert_enriched_feature_manifest(
    rows: Sequence[dict[str, Any]], expected: str, stage: str
) -> None:
    actual = _enriched_feature_manifest(rows)
    if actual != expected:
        raise RuntimeError(
            f"enriched zero-axis feature manifest changed at {stage}: "
            f"expected={expected}, actual={actual}"
        )


def _validate_integrity_manifest(
    input_dir: Path,
    split: str,
    source_path: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest_path = input_dir / "candidate_integrity_manifest.json"
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    record = (manifest.get("splits", {}) or {}).get(split, {}) or {}
    checks = {
        "manifest_version_match": manifest.get("version") == INTEGRITY_VERSION,
        "manifest_output_dir_match": Path(
            str(manifest.get("output_dir", ""))
        ).resolve()
        == input_dir,
        "output_file_match": Path(str(record.get("output_file", ""))).resolve()
        == source_path,
        "output_hash_match": record.get("output_sha256") == file_sha256(source_path),
        "output_row_count_match": record.get("output_rows") == len(rows),
        "candidate_ids_hash_match": record.get("candidate_ids_sha256")
        == candidate_ids_sha256(rows),
        "all_candidate_ids_unique": len(rows)
        == len({candidate_id(row) for row in rows}),
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(
            f"candidate integrity validation failed for {split}: {checks}"
        )
    checks["manifest"] = str(manifest_path)
    checks["manifest_sha256"] = file_sha256(manifest_path)
    return checks


def _zero_axis_distance(close_prefix: pd.Series) -> float:
    values = pd.to_numeric(close_prefix, errors="coerce").astype(float)
    if len(values) < MIN_MACD_BARS:
        raise RuntimeError(
            f"insufficient MACD history: required={MIN_MACD_BARS}, actual={len(values)}"
        )
    if values.isna().any() or not np.isfinite(values.to_numpy(dtype=float)).all():
        raise RuntimeError("MACD close prefix contains non-finite values")
    if bool((values <= 0).any()):
        raise RuntimeError("MACD close prefix must be strictly positive")
    macd = calculate_macd(values, **MACD_PARAMETERS)
    dif = float(macd["dif"].iloc[-1])
    close = float(values.iloc[-1])
    if not math.isfinite(dif):
        raise RuntimeError("signal-day DIF is unavailable or non-finite")
    factor = abs(dif) / close
    if not math.isfinite(factor):
        raise RuntimeError("zero-axis distance is non-finite")
    return float(factor)


def _load_closed_qfq(path: Path) -> pd.DataFrame:
    frame = pd.read_pickle(path)
    closed = prepare_closed_bars(frame)
    if closed.empty:
        raise RuntimeError(f"history has no closed bars: {path}")
    return closed


def _macd_history_context(
    symbol: str,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if symbol in cache:
        return cache[symbol]
    closed = _load_closed_qfq(HISTORY_DIR / f"{symbol}_qfq.pkl")
    entries = find_golden_cross_entries(
        closed,
        **MACD_PARAMETERS,
        zero_axis_tolerance=ZERO_AXIS_TOLERANCE,
        confirmation_bars=CONFIRMATION_BARS,
        allowed_zones=ALLOWED_ZONES,
    )
    entry_map: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        entry_map[(int(entry["confirmation_index"]), str(entry["zone"]))].append(
            entry
        )
    context = {"closed": closed, "entry_map": entry_map}
    cache[symbol] = context
    return context


def _zero_axis_features_for_sources(
    sources: list[dict[str, Any]],
    history_cache: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    _validate_symbol_day_uniqueness(sources)
    feature_map: dict[str, dict[str, Any]] = {}
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    errors: Counter[str] = Counter()
    macd_candidates = 0
    for source in sources:
        identity = _identity_projection(source)
        identifier = candidate_id(identity)
        signal_type = identity["signal_type"]
        feature: dict[str, Any] = {
            "candidate_id": identifier,
            PRIMARY_FACTOR: None,
            "zero_axis_factor_assignment_available": False,
            "zero_axis_factor_error": None,
            "zero_axis_nearer": None,
            "zero_axis_stratum": None,
            "zero_axis_stratum_median": None,
            "zero_axis_entry_timing": "T+1",
            "zero_axis_matching_entry_count": 0,
            "zero_axis_selected_cross_day": None,
            "zero_axis_selected_cross_index": None,
            "zero_axis_confirmation_index": None,
        }
        if signal_type not in MACD_SIGNAL_TYPES:
            feature["zero_axis_factor_error"] = "non_macd_signal"
            errors["non_macd_signal"] += 1
            feature_map[identifier] = feature
            continue
        macd_candidates += 1
        symbol = identity["symbol"]
        context = _macd_history_context(symbol, history_cache)
        closed: pd.DataFrame = context["closed"]
        days = pd.to_datetime(closed["datetime"], errors="coerce").dt.date
        signal_day = date.fromisoformat(identity["signal_day"])
        matches = closed.index[days == signal_day].tolist()
        if len(matches) != 1:
            feature["zero_axis_factor_error"] = "missing_or_duplicate_signal_bar"
            errors["missing_or_duplicate_signal_bar"] += 1
            feature_map[identifier] = feature
            continue
        signal_index = int(matches[0])
        expected_zone = signal_type.removeprefix(
            "macd_golden_cross_pullback_confirmed_"
        )
        matching_entries = list(
            context["entry_map"].get((signal_index, expected_zone), [])
        )
        if not matching_entries:
            feature["zero_axis_factor_error"] = "entry_provenance_mismatch"
            errors["entry_provenance_mismatch"] += 1
            feature_map[identifier] = feature
            continue
        selected = max(
            matching_entries, key=lambda item: int(item["cross_index"])
        )
        try:
            factor = _zero_axis_distance(
                closed["close"].iloc[: signal_index + 1]
            )
        except RuntimeError as exc:
            feature["zero_axis_factor_error"] = str(exc)
            errors["invalid_macd_prefix"] += 1
            feature_map[identifier] = feature
            continue
        stratum = f"{identity['signal_day']}|{signal_type}"
        feature.update(
            {
                PRIMARY_FACTOR: factor,
                "zero_axis_factor_assignment_available": True,
                "zero_axis_factor_error": None,
                "zero_axis_stratum": stratum,
                "zero_axis_matching_entry_count": len(matching_entries),
                "zero_axis_selected_cross_day": pd.Timestamp(
                    closed.iloc[int(selected["cross_index"])]["datetime"]
                ).date().isoformat(),
                "zero_axis_selected_cross_index": int(selected["cross_index"]),
                "zero_axis_confirmation_index": signal_index,
            }
        )
        feature_map[identifier] = feature
        strata[stratum].append(feature)

    unavailable_macd = sum(
        1
        for item in feature_map.values()
        if item["zero_axis_factor_error"] not in (None, "non_macd_signal")
    )
    if unavailable_macd:
        raise RuntimeError(
            "every registered MACD candidate must have a zero-axis factor: "
            f"unavailable={unavailable_macd}, errors={dict(errors)}"
        )

    excluded_strata = 0
    for stratum, items in sorted(strata.items()):
        if len(items) < 2:
            excluded_strata += 1
            continue
        median = float(np.median([float(item[PRIMARY_FACTOR]) for item in items]))
        labels = [float(item[PRIMARY_FACTOR]) <= median for item in items]
        if all(labels) or not any(labels):
            excluded_strata += 1
            continue
        for item, nearer in zip(items, labels, strict=True):
            item["zero_axis_nearer"] = nearer
            item["zero_axis_stratum_median"] = median

    assigned = sum(
        bool(item["zero_axis_factor_assignment_available"])
        for item in feature_map.values()
    )
    comparable = [
        item for item in feature_map.values() if item["zero_axis_nearer"] is not None
    ]
    meta = {
        "primary_factor": PRIMARY_FACTOR,
        "formula": "abs(DIF_signal_day) / QFQ_CLOSE_signal_day",
        "direction": "smaller_is_better",
        "signal_day_is_pullback_confirmation_day": True,
        "entry_provenance_reconstructed": True,
        "entry_provenance_match_required": True,
        "same_day_same_zone_duplicate_rule": "latest_cross_index",
        "ambiguous_same_day_entry_candidates": sum(
            int(item["zero_axis_matching_entry_count"] > 1)
            for item in feature_map.values()
        ),
        "macd_parameters": dict(MACD_PARAMETERS),
        "macd_ewm_adjust": False,
        "zero_axis_tolerance": ZERO_AXIS_TOLERANCE,
        "confirmation_bars": CONFIRMATION_BARS,
        "allowed_zones": list(ALLOWED_ZONES),
        "full_history_prefix_used": True,
        "minimum_macd_bars": MIN_MACD_BARS,
        "signal_day_closed_bars_only": True,
        "post_signal_suffix_cannot_influence_factor": True,
        "qfq_constant_scale_invariant": True,
        "eligible_signal_types": list(MACD_SIGNAL_TYPES),
        "non_macd_candidates_receive_zero": False,
        "grouping": "within_signal_day_x_signal_type_median",
        "comparison": "nearer_minus_farther",
        "macd_candidates": macd_candidates,
        "assigned_candidates": assigned,
        "assignment_coverage": assigned / macd_candidates if macd_candidates else 0.0,
        "comparable_candidates": len(comparable),
        "comparable_symbols": len(
            {
                identifier.split("|", 1)[0]
                for identifier, item in feature_map.items()
                if item["zero_axis_nearer"] is not None
            }
        ),
        "comparable_signal_days": len(
            {
                str(item["zero_axis_stratum"]).split("|", 1)[0]
                for item in comparable
            }
        ),
        "nearer_n": sum(item["zero_axis_nearer"] is True for item in comparable),
        "farther_n": sum(item["zero_axis_nearer"] is False for item in comparable),
        "excluded_singleton_or_tied_strata": excluded_strata,
        "factor_errors": dict(errors),
        "feature_manifest_sha256": _feature_manifest(feature_map),
    }
    return feature_map, meta


def _validate_authoritative_outcomes(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        identifier = candidate_id(row)
        for outcome in FIELDS:
            if outcome not in row:
                raise RuntimeError(
                    "authoritative replay outcome field is missing: "
                    f"candidate_id={identifier}, outcome={outcome}"
                )
            value = row[outcome]
            if value is None:
                continue
            if isinstance(value, (bool, np.bool_)) or not isinstance(
                value, (int, float, np.integer, np.floating)
            ):
                raise TypeError(
                    "authoritative replay outcome is not numeric: "
                    f"candidate_id={identifier}, outcome={outcome}, value={value!r}"
                )
            if not math.isfinite(float(value)):
                raise RuntimeError(
                    "authoritative replay outcome is not finite: "
                    f"candidate_id={identifier}, outcome={outcome}, value={value!r}"
                )


def _assert_replay_integrity(
    sources: list[dict[str, Any]],
    feature_map: dict[str, dict[str, Any]],
    replay_rows: list[dict[str, Any]],
    replay_skips: Counter,
    raw_source_match: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_ids = [candidate_id(row) for row in sources]
    replay_ids = [candidate_id(row) for row in replay_rows]
    feature_ids = list(feature_map)
    explicit_ids_match = all(
        str(row.get("candidate_id", "")) == candidate_id(row)
        for row in replay_rows
    )
    baseline_complete = bool(
        raw_source_match.get("source_rows") == len(sources)
        and raw_source_match.get("common_eligible_rows") == len(sources)
        and raw_source_match.get("simulated_rows") == len(sources)
        and source_ids == replay_ids
        and not dict(replay_skips)
    )
    checks = {
        "source_ids_unique": len(source_ids) == len(set(source_ids)),
        "replay_ids_unique": len(replay_ids) == len(set(replay_ids)),
        "feature_ids_unique": len(feature_ids) == len(set(feature_ids)),
        "explicit_replay_ids_match_identity_fields": explicit_ids_match,
        "source_replay_ids_order_match": source_ids == replay_ids,
        "source_feature_ids_order_match": source_ids == feature_ids,
        "replay_feature_ids_order_match": replay_ids == feature_ids,
        "replay_skips_empty": not dict(replay_skips),
        "source_rows_match": raw_source_match.get("source_rows") == len(sources),
        "eligible_rows_match": raw_source_match.get("common_eligible_rows")
        == len(sources),
        "replayed_rows_match": raw_source_match.get("simulated_rows")
        == len(sources),
        "baseline_replay_complete": baseline_complete,
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(f"replay integrity validation failed: {checks}")
    source_match = dict(raw_source_match)
    source_match.update(
        {
            "baseline_replay_complete": baseline_complete,
            "source_candidate_ids_sha256": candidate_ids_sha256(sources),
            "replay_candidate_ids_sha256": candidate_ids_sha256(replay_rows),
            "source_outcomes_used": False,
            "source_outcomes_used_for_diagnostic": False,
            "source_legacy_outcomes_stripped_before_replay": True,
            "source_legacy_outcome_fields_stripped": sorted(
                SOURCE_LEGACY_OUTCOME_FIELDS
            ),
            "replay_outcomes_used": True,
        }
    )
    return checks, source_match


def _valid_zero_axis_rows(
    rows: list[dict[str, Any]], outcome: str
) -> list[dict[str, Any]]:
    preliminary: list[dict[str, Any]] = []
    for row in rows:
        value = row.get(outcome)
        if value is None or row.get("zero_axis_nearer") is None:
            continue
        if isinstance(value, (bool, np.bool_)) or not isinstance(
            value, (int, float, np.integer, np.floating)
        ) or not math.isfinite(float(value)):
            raise RuntimeError(
                "invalid authoritative outcome for zero-axis FE: "
                f"candidate_id={row.get('candidate_id')}, outcome={outcome}"
            )
        preliminary.append(row)
    labels_by_stratum: dict[str, set[bool]] = defaultdict(set)
    for row in preliminary:
        labels_by_stratum[str(row["zero_axis_stratum"])].add(
            bool(row["zero_axis_nearer"])
        )
    valid_strata = {
        key for key, labels in labels_by_stratum.items() if len(labels) == 2
    }
    return sorted(
        (
            row
            for row in preliminary
            if str(row["zero_axis_stratum"]) in valid_strata
        ),
        key=lambda row: (str(row["zero_axis_stratum"]), candidate_id(row)),
    )


def _stratum_fixed_effect_beta(
    rows: list[dict[str, Any]],
    outcome: str,
    weights: dict[str, int] | None = None,
) -> float | None:
    by_stratum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_stratum[str(row["zero_axis_stratum"])].append(row)
    numerator = 0.0
    denominator = 0.0
    for stratum_rows in by_stratum.values():
        values: list[tuple[float, float, float]] = []
        for row in stratum_rows:
            symbol = _normalize_symbol(row["symbol"])
            weight = float(1 if weights is None else weights.get(symbol, 0))
            if weight <= 0:
                continue
            x_value = 1.0 if row["zero_axis_nearer"] is True else 0.0
            values.append((weight, x_value, float(row[outcome])))
        total_weight = sum(item[0] for item in values)
        if total_weight <= 0:
            continue
        x_mean = sum(w * x for w, x, _ in values) / total_weight
        y_mean = sum(w * y for w, _, y in values) / total_weight
        for weight, x_value, y_value in values:
            numerator += weight * (x_value - x_mean) * (y_value - y_mean)
            denominator += weight * (x_value - x_mean) ** 2
    if denominator <= 0:
        return None
    beta = numerator / denominator
    return float(beta) if math.isfinite(beta) else None


def _zero_axis_outcome_sample_gate(
    rows: list[dict[str, Any]], outcome: str
) -> dict[str, Any]:
    valid = _valid_zero_axis_rows(rows, outcome)
    candidates = {str(row["candidate_id"]) for row in valid}
    symbols = {_normalize_symbol(row["symbol"]) for row in valid}
    days = {str(row["signal_day"]) for row in valid}
    nearer = [row for row in valid if row["zero_axis_nearer"] is True]
    farther = [row for row in valid if row["zero_axis_nearer"] is False]
    sufficient = bool(
        len(candidates) >= MIN_AFFECTED_CANDIDATES
        and len(symbols) >= MIN_AFFECTED_SYMBOLS
        and len(days) >= MIN_AFFECTED_SIGNAL_DAYS
        and nearer
        and farther
    )
    return {
        "outcome": outcome,
        "valid_candidates": len(candidates),
        "valid_symbols": len(symbols),
        "valid_signal_days": len(days),
        "nearer_n": len(nearer),
        "farther_n": len(farther),
        "minimum_candidates": MIN_AFFECTED_CANDIDATES,
        "minimum_symbols": MIN_AFFECTED_SYMBOLS,
        "minimum_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
        "sufficient": sufficient,
    }


def _cluster_bootstrap_zero_axis(
    rows: list[dict[str, Any]],
    outcome: str,
    *,
    reps: int,
    seed: int,
) -> dict[str, Any]:
    valid = _valid_zero_axis_rows(rows, outcome)
    observed = _stratum_fixed_effect_beta(valid, outcome)
    symbols = sorted({_normalize_symbol(row["symbol"]) for row in valid})
    if observed is None or not symbols or reps <= 0:
        return {
            "fixed_effect_beta": observed,
            "ci95_low": None,
            "ci95_high": None,
            "reps_requested": reps,
            "reps_valid": 0,
            "cluster_count": len(symbols),
            "signal_day_count": len({str(row["signal_day"]) for row in valid}),
            "stratum_count": len(
                {str(row["zero_axis_stratum"]) for row in valid}
            ),
            "seed": seed,
        }
    rng = np.random.default_rng(seed)
    samples: list[float] = []
    for _ in range(reps):
        selected = rng.integers(0, len(symbols), size=len(symbols))
        counts = np.bincount(selected, minlength=len(symbols))
        weights = {
            symbol: int(counts[index])
            for index, symbol in enumerate(symbols)
            if counts[index] > 0
        }
        beta = _stratum_fixed_effect_beta(valid, outcome, weights)
        if beta is not None:
            samples.append(beta)
    if samples:
        ci_low, ci_high = np.percentile(
            np.asarray(samples, dtype=float),
            [2.5, 97.5],
            method="linear",
        )
    else:
        ci_low, ci_high = None, None
    return {
        "fixed_effect_beta": observed,
        "ci95_low": float(ci_low) if ci_low is not None else None,
        "ci95_high": float(ci_high) if ci_high is not None else None,
        "reps_requested": reps,
        "reps_valid": len(samples),
        "cluster_count": len(symbols),
        "signal_day_count": len({str(row["signal_day"]) for row in valid}),
        "stratum_count": len({str(row["zero_axis_stratum"]) for row in valid}),
        "seed": seed,
    }


def _bootstrap_contract_ok(report: dict[str, Any]) -> bool:
    return bool(
        report.get("fixed_effect_beta") is not None
        and report.get("reps_requested") == PREREGISTERED_BOOTSTRAP_REPS
        and report.get("reps_valid") == PREREGISTERED_BOOTSTRAP_REPS
        and int(report.get("cluster_count") or 0) >= MIN_OUTCOME_CLUSTERS
    )


def _numeric_stats(values: Iterable[Any]) -> dict[str, Any]:
    cleaned = [
        float(value)
        for value in values
        if value is not None
        and not isinstance(value, (bool, np.bool_))
        and isinstance(value, (int, float, np.integer, np.floating))
        and math.isfinite(float(value))
    ]
    if not cleaned:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    array = np.asarray(cleaned, dtype=float)
    return {
        "n": len(cleaned),
        "mean": float(np.mean(array)),
        "median": float(np.median(array)),
        "min": float(np.min(array)),
        "max": float(np.max(array)),
    }


def _zero_axis_split_report(
    rows: list[dict[str, Any]],
    replay_integrity: dict[str, Any],
    factor_meta: dict[str, Any],
    seed: int,
) -> dict[str, Any]:
    macd_rows = [row for row in rows if row["signal_type"] in MACD_SIGNAL_TYPES]
    assigned = [
        row for row in macd_rows if row.get("zero_axis_factor_assignment_available")
    ]
    comparable = [row for row in assigned if row.get("zero_axis_nearer") is not None]
    nearer = [row for row in comparable if row["zero_axis_nearer"] is True]
    farther = [row for row in comparable if row["zero_axis_nearer"] is False]
    candidates = {str(row["candidate_id"]) for row in comparable}
    symbols = {_normalize_symbol(row["symbol"]) for row in comparable}
    days = {str(row["signal_day"]) for row in comparable}
    coverage = len(assigned) / len(macd_rows) if macd_rows else 0.0
    sample_sufficient = bool(
        len(candidates) >= MIN_AFFECTED_CANDIDATES
        and len(symbols) >= MIN_AFFECTED_SYMBOLS
        and len(days) >= MIN_AFFECTED_SIGNAL_DAYS
        and nearer
        and farther
    )
    outcome_gates = {
        outcome: _zero_axis_outcome_sample_gate(comparable, outcome)
        for outcome in PRIMARY_OUTCOMES
    }
    bootstraps = {
        outcome: _cluster_bootstrap_zero_axis(
            comparable,
            outcome,
            reps=PREREGISTERED_BOOTSTRAP_REPS,
            seed=seed + index,
        )
        for index, outcome in enumerate(("future_20d", *PRIMARY_OUTCOMES))
    }
    direction_positive = all(
        bootstraps[outcome]["fixed_effect_beta"] is not None
        and bootstraps[outcome]["fixed_effect_beta"] > 0
        for outcome in PRIMARY_OUTCOMES
    )
    contract_ok = all(
        _bootstrap_contract_ok(bootstraps[outcome])
        for outcome in PRIMARY_OUTCOMES
    )
    ci_supported = bool(
        contract_ok
        and all(
            bootstraps[outcome]["ci95_low"] is not None
            and bootstraps[outcome]["ci95_low"] > 0
            for outcome in PRIMARY_OUTCOMES
        )
    )
    outcome_samples_sufficient = all(
        item["sufficient"] for item in outcome_gates.values()
    )
    return {
        "n_all_candidates": len(rows),
        "n_macd_candidates": len(macd_rows),
        "assignment_coverage": coverage,
        "primary_nearer_n": len(nearer),
        "primary_farther_n": len(farther),
        "factor_stats": _numeric_stats(
            row.get(PRIMARY_FACTOR) for row in assigned
        ),
        "nearer_outcomes": {
            outcome: _numeric_stats(row.get(outcome) for row in nearer)
            for outcome in ("future_20d", *PRIMARY_OUTCOMES)
        },
        "farther_outcomes": {
            outcome: _numeric_stats(row.get(outcome) for row in farther)
            for outcome in ("future_20d", *PRIMARY_OUTCOMES)
        },
        "fixed_effect_cluster_bootstrap_nearer_minus_farther": bootstraps,
        "sample_gate": {
            "affected_unique_candidates": len(candidates),
            "affected_unique_symbols": len(symbols),
            "affected_unique_signal_days": len(days),
            "minimum_candidates": MIN_AFFECTED_CANDIDATES,
            "minimum_symbols": MIN_AFFECTED_SYMBOLS,
            "minimum_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
            "sufficient": sample_sufficient,
            "outcome_specific": outcome_gates,
            "all_primary_outcomes_sufficient": outcome_samples_sufficient,
        },
        "gate": {
            "assignment_coverage_ok": coverage >= MIN_ASSIGNMENT_COVERAGE,
            "sample_sufficient": sample_sufficient,
            "outcome_samples_sufficient": outcome_samples_sufficient,
            "replay_integrity_ok": bool(replay_integrity.get("all_pass")),
            "primary_direction_positive": direction_positive,
            "primary_bootstrap_contract_ok": contract_ok,
            "primary_cluster_bootstrap_ci95_positive": ci_supported,
            "eligible_for_cross_split": bool(
                coverage >= MIN_ASSIGNMENT_COVERAGE
                and sample_sufficient
                and outcome_samples_sufficient
                and replay_integrity.get("all_pass")
            ),
        },
        "factor_meta": factor_meta,
    }


def _zero_axis_cross_split_gate(
    split_reports: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    required_present = set(split_reports) == set(SPLITS)
    eligible = [
        split
        for split in SPLITS
        if split_reports.get(split, {}).get("gate", {}).get(
            "eligible_for_cross_split"
        )
    ]
    all_eligible = required_present and eligible == list(SPLITS)
    directions = {
        split: {
            outcome: split_reports[split][
                "fixed_effect_cluster_bootstrap_nearer_minus_farther"
            ][outcome]["fixed_effect_beta"]
            for outcome in PRIMARY_OUTCOMES
        }
        for split in SPLITS
        if split in split_reports
    }
    direction_consistent = bool(
        all_eligible
        and all(
            directions[split][outcome] is not None
            and directions[split][outcome] > 0
            for split in SPLITS
            for outcome in PRIMARY_OUTCOMES
        )
    )
    bootstrap_supported = bool(
        direction_consistent
        and all(
            split_reports[split]["gate"]["primary_bootstrap_contract_ok"]
            and split_reports[split]["gate"][
                "primary_cluster_bootstrap_ci95_positive"
            ]
            for split in SPLITS
        )
    )
    return {
        "required_splits": list(SPLITS),
        "eligible_splits": eligible,
        "all_required_splits_present": required_present,
        "all_required_splits_eligible": all_eligible,
        "directions": directions,
        "direction_consistent": direction_consistent,
        "cluster_bootstrap_supported": bootstrap_supported,
        "pass": bool(all_eligible and direction_consistent and bootstrap_supported),
    }


def _zero_axis_preflight_only_report(
    factor_meta: dict[str, Any], replay_integrity: dict[str, Any]
) -> dict[str, Any]:
    sample_sufficient = bool(
        factor_meta["comparable_candidates"] >= MIN_AFFECTED_CANDIDATES
        and factor_meta["comparable_symbols"] >= MIN_AFFECTED_SYMBOLS
        and factor_meta["comparable_signal_days"] >= MIN_AFFECTED_SIGNAL_DAYS
    )
    return {
        "outcome_analysis_performed": False,
        "outcomes_accessed": [],
        "bootstrap_performed": False,
        "reason": "preflight_sample_ineligible",
        "sample_gate": {
            "affected_unique_candidates": factor_meta["comparable_candidates"],
            "affected_unique_symbols": factor_meta["comparable_symbols"],
            "affected_unique_signal_days": factor_meta[
                "comparable_signal_days"
            ],
            "minimum_candidates": MIN_AFFECTED_CANDIDATES,
            "minimum_symbols": MIN_AFFECTED_SYMBOLS,
            "minimum_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
            "sufficient": sample_sufficient,
        },
        "gate": {
            "assignment_coverage_ok": factor_meta["assignment_coverage"]
            >= MIN_ASSIGNMENT_COVERAGE,
            "sample_sufficient": sample_sufficient,
            "replay_integrity_ok": bool(replay_integrity.get("all_pass")),
            "eligible_for_cross_split": False,
        },
        "factor_meta": factor_meta,
    }


def _normalized_regime(value: Any) -> str:
    regime = str(value or "").strip().lower()
    return regime if regime in REGISTERED_REGIMES else "unknown"


def _normalized_signal_type(value: Any) -> str:
    signal_type = str(value or "").strip()
    return signal_type if signal_type in REGISTERED_SIGNAL_TYPES else "unknown"


def _stability_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    pnl_values = [
        float(row["trade_pnl_pct"])
        for row in rows
        if row.get("trade_pnl_pct") is not None
    ]
    mfe_pnl = [
        float(row["trade_pnl_pct"]) / float(row["mfe"])
        for row in rows
        if row.get("trade_pnl_pct") is not None
        and row.get("mfe") is not None
        and float(row["mfe"]) > 0
    ]
    return {
        "n": len(rows),
        "unique_symbols": len({_normalize_symbol(row["symbol"]) for row in rows}),
        "unique_signal_days": len({str(row["signal_day"]) for row in rows}),
        "small_n": len(rows) < STRATEGY_SMALL_N,
        "outcomes": {
            outcome: _numeric_stats(row.get(outcome) for row in rows)
            for outcome in ("future_40d", "trade_pnl_pct", "mfe", "mae")
        },
        "holding_days": _numeric_stats(row.get("holding_days") for row in rows),
        "win_rate_pct": (
            100.0 * sum(value > 0 for value in pnl_values) / len(pnl_values)
            if pnl_values
            else None
        ),
        "stop_loss_rate_pct": (
            100.0
            * sum(exit_reason_category(row.get("exit_reason")) == "risk_stop_loss" for row in rows)
            / len(rows)
            if rows
            else None
        ),
        "exit_efficiency": _numeric_stats(mfe_pnl),
        "trade_pnl_sum_pp": float(sum(pnl_values)) if pnl_values else None,
    }


def _grouped_stability_records(
    rows: Sequence[dict[str, Any]],
    split: str,
    level: str,
    keys: Sequence[str],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(str(row[key]) for key in keys)].append(row)
    records: list[dict[str, Any]] = []
    for values, group_rows in sorted(groups.items()):
        dimensions = dict(zip(keys, values, strict=True))
        records.append(
            {
                "split": split,
                "level": level,
                **dimensions,
                **_stability_metrics(group_rows),
            }
        )
    return records


def _strategy_stability_report(
    replay_by_split: dict[str, list[dict[str, Any]]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_split: dict[str, Any] = {}
    detailed_cells: list[dict[str, Any]] = []
    overlap: dict[str, Any] = {}
    completeness_checks: dict[str, bool] = {}
    for split in SPLITS:
        source = replay_by_split[split]
        rows: list[dict[str, Any]] = []
        for item in source:
            row = dict(item)
            row["signal_month"] = str(row["signal_day"])[:7]
            row["normalized_signal_type"] = _normalized_signal_type(
                row.get("signal_type")
            )
            row["normalized_regime"] = _normalized_regime(row.get("regime"))
            row["normalized_exit_reason"] = str(
                row.get("exit_reason") or "unknown"
            )
            row["exit_category"] = exit_reason_category(row.get("exit_reason"))
            rows.append(row)
        completeness_checks[split] = bool(
            len(rows) == len(source)
            and all(len(row["signal_month"]) == 7 for row in rows)
            and all(row["normalized_signal_type"] != "unknown" for row in rows)
            and all(row["exit_category"] != "unknown" for row in rows)
        )
        by_split[split] = {
            "overall": _stability_metrics(rows),
            "by_month": _grouped_stability_records(
                rows, split, "month", ("signal_month",)
            ),
            "by_signal_type": _grouped_stability_records(
                rows, split, "signal_type", ("normalized_signal_type",)
            ),
            "by_regime": _grouped_stability_records(
                rows, split, "regime", ("normalized_regime",)
            ),
            "month_x_signal_type_x_regime": _grouped_stability_records(
                rows,
                split,
                "month_x_signal_type_x_regime",
                ("signal_month", "normalized_signal_type", "normalized_regime"),
            ),
        }
        detailed_cells.extend(
            _grouped_stability_records(
                rows,
                split,
                "month_x_signal_type_x_regime_x_exit_reason",
                (
                    "signal_month",
                    "normalized_signal_type",
                    "normalized_regime",
                    "normalized_exit_reason",
                    "exit_category",
                ),
            )
        )
        cell_reports: dict[str, Any] = {}
        for regime in REGISTERED_REGIMES:
            for signal_type in REGISTERED_SIGNAL_TYPES:
                cell_rows = [
                    row
                    for row in rows
                    if row["normalized_regime"] == regime
                    and row["normalized_signal_type"] == signal_type
                ]
                metrics = _stability_metrics(cell_rows)
                metrics["overlap_sample_sufficient"] = bool(
                    metrics["n"] >= MIN_AFFECTED_CANDIDATES
                    and metrics["unique_symbols"] >= MIN_AFFECTED_SYMBOLS
                    and metrics["unique_signal_days"] >= MIN_AFFECTED_SIGNAL_DAYS
                )
                cell_reports[f"{regime}|{signal_type}"] = metrics
        overlap[split] = cell_reports

    complete = all(completeness_checks.values())
    full_overlap = all(
        overlap[split][f"{regime}|{signal_type}"][
            "overlap_sample_sufficient"
        ]
        for split in SPLITS
        for regime in REGISTERED_REGIMES
        for signal_type in REGISTERED_SIGNAL_TYPES
    )
    split_means = {
        split: by_split[split]["overall"]["outcomes"]["trade_pnl_pct"]["mean"]
        for split in SPLITS
    }
    non_null_means = [value for value in split_means.values() if value is not None]
    warnings = {
        "overall_trade_pnl_sign_reversal_across_splits": bool(
            any(value > 0 for value in non_null_means)
            and any(value < 0 for value in non_null_means)
        ),
        "negative_overall_trade_pnl_splits": [
            split for split, value in split_means.items() if value is not None and value < 0
        ],
        "time_split_regime_signal_composition_confounding": True,
        "exit_reason_is_post_trade_diagnostic_only": True,
    }
    status = "insufficient_overlap" if not full_overlap else "descriptive_only"
    report = {
        **STUDY_REGISTRY["strategy_stability_diagnostic"],
        "can_rescue_other_study": False,
        "can_reverse_factor_direction": False,
        "diagnostic_completeness_gate": {
            "by_split": completeness_checks,
            "all_pass": complete,
        },
        "matrix_overlap_gate": {
            "required_regimes": list(REGISTERED_REGIMES),
            "required_signal_types": list(REGISTERED_SIGNAL_TYPES),
            "minimum_candidates": MIN_AFFECTED_CANDIDATES,
            "minimum_symbols": MIN_AFFECTED_SYMBOLS,
            "minimum_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
            "by_split": overlap,
            "full_matrix_overlap": full_overlap,
        },
        "status": status,
        "causal_attribution_performed": False,
        "equivalence_or_noninferiority_claim_performed": False,
        "cell_significance_tests_performed": False,
        "performance_warning_flags": warnings,
        "by_split": by_split,
        "detailed_cell_count": len(detailed_cells),
    }
    if not complete:
        raise RuntimeError("strategy stability diagnostic completeness failed")
    return report, detailed_cells


def _study_projection_manifest(rows: Sequence[dict[str, Any]]) -> str:
    fields = (
        "candidate_id",
        "symbol",
        "signal_day",
        "signal_type",
        "regime",
        "exit_reason",
        "holding_days",
        *FIELDS,
        PRIMARY_FACTOR,
        "zero_axis_nearer",
        "zero_axis_stratum",
    )
    return _sha256_json(
        [{field: row.get(field) for field in fields} for row in rows]
    )


def _full_rows_manifest(rows: Sequence[dict[str, Any]]) -> str:
    return _sha256_json(list(rows))


def _strategy_study_projection(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "candidate_id",
        "symbol",
        "signal_day",
        "signal_type",
        "regime",
        "exit_reason",
        "holding_days",
        *FIELDS,
    )
    return {field: deepcopy(row.get(field)) for field in fields}


def _zero_axis_study_projection(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "candidate_id",
        "symbol",
        "signal_day",
        "signal_type",
        PRIMARY_FACTOR,
        "zero_axis_factor_assignment_available",
        "zero_axis_nearer",
        "zero_axis_stratum",
        *PRIMARY_OUTCOMES,
        "future_20d",
    )
    return {field: deepcopy(row.get(field)) for field in fields}


def _validate_preregistered_args(args: argparse.Namespace) -> None:
    if tuple(getattr(args, "splits", ())) != SPLITS:
        raise RuntimeError(f"all required splits must remain in order {SPLITS}")
    if getattr(args, "label", None) != PREREGISTERED_LABEL:
        raise RuntimeError("label differs from the pre-registered label")
    if getattr(args, "seed", None) != PREREGISTERED_SEED:
        raise RuntimeError("seed differs from the pre-registered seed")
    if getattr(args, "bootstrap_reps", None) != PREREGISTERED_BOOTSTRAP_REPS:
        raise RuntimeError("bootstrap reps differ from the pre-registered value")
    expected_script_sha256 = str(
        getattr(args, "expected_script_sha256", "")
    ).lower()
    if len(expected_script_sha256) != 64 or any(
        value not in "0123456789abcdef" for value in expected_script_sha256
    ):
        raise RuntimeError("expected script SHA256 must be a 64-character hex value")
    actual_script_sha256 = file_sha256(Path(__file__).resolve())
    if actual_script_sha256 != expected_script_sha256:
        raise RuntimeError(
            "script SHA differs from formally supplied snapshot: "
            f"expected={expected_script_sha256}, actual={actual_script_sha256}"
        )
    if Path(args.input_dir).expanduser().resolve() != CANONICAL_INPUT_DIR.resolve():
        raise RuntimeError(f"input dir must remain {CANONICAL_INPUT_DIR.resolve()}")
    if Path(args.output_dir).expanduser().resolve() != FORMAL_OUTPUT_DIR.resolve():
        raise RuntimeError(f"output dir must remain {FORMAL_OUTPUT_DIR.resolve()}")
    if Path(args.config).expanduser().resolve() != CONFIG_PATH.resolve():
        raise RuntimeError(f"config path must remain {CONFIG_PATH.resolve()}")


def _assert_output_directory_unused(output_dir: Path) -> Path:
    staging = output_dir.with_name(output_dir.name + ".tmp")
    conflicts = [path for path in (output_dir, staging) if path.exists()]
    if conflicts:
        raise RuntimeError(
            "formal output directory already exists; overwrite is forbidden: "
            + ", ".join(str(path) for path in conflicts)
        )
    return staging


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    _validate_preregistered_args(args)
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve()
    staging_dir = _assert_output_directory_unused(output_dir)
    static_initial = _snapshot_named_paths(
        _static_protected_paths(input_dir, config_path)
    )
    dependency_contract = _validate_expected_snapshot(
        static_initial,
        _expected_static_hashes(str(args.expected_script_sha256).lower()),
    )
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")
    configured_macd = {
        key: int(
            config.get("signal_strategy", {}).get("macd", {}).get(key, value)
        )
        for key, value in MACD_PARAMETERS.items()
    }
    if configured_macd != MACD_PARAMETERS:
        raise RuntimeError(
            f"MACD config differs from preregistration: {configured_macd}"
        )
    configured_tolerance = float(
        config.get("signal_strategy", {})
        .get("macd", {})
        .get("zero_axis_tolerance", ZERO_AXIS_TOLERANCE)
    )
    configured_confirmation_bars = int(
        config.get("backtest", {})
        .get("chan_zero_axis", {})
        .get("cross_window_bars", CONFIRMATION_BARS)
    )
    configured_allowed_zones = tuple(
        str(value).lower()
        for value in config.get("backtest", {})
        .get("chan_zero_axis", {})
        .get("allowed_zones", list(ALLOWED_ZONES))
    )
    if (
        configured_tolerance != ZERO_AXIS_TOLERANCE
        or configured_confirmation_bars != CONFIRMATION_BARS
        or configured_allowed_zones != ALLOWED_ZONES
    ):
        raise RuntimeError(
            "MACD entry provenance config differs from preregistration: "
            f"tolerance={configured_tolerance}, "
            f"confirmation_bars={configured_confirmation_bars}, "
            f"allowed_zones={configured_allowed_zones}"
        )

    sources_by_split: dict[str, list[dict[str, Any]]] = {}
    integrity_by_split: dict[str, dict[str, Any]] = {}
    symbols: set[str] = set()
    for split in SPLITS:
        source_path = input_dir / f"candidates_{split}.jsonl"
        sources = load_jsonl(source_path)
        _validate_symbol_day_uniqueness(sources)
        sources_by_split[split] = sources
        integrity_by_split[split] = _validate_integrity_manifest(
            input_dir, split, source_path, sources
        )
        symbols.update(_normalize_symbol(row["symbol"]) for row in sources)

    history_initial = _snapshot_named_paths(_history_paths(symbols))
    frozen_history_manifest = _snapshot_manifest(history_initial)
    history_inventory, _ = replay_contract._history_inventory(symbols)
    replay_contract._assert_history_inventory_matches_snapshot(
        history_inventory,
        {
            item["path"]: {
                "sha256": item["sha256"],
                "size": item["size_bytes"],
            }
            for item in history_initial.values()
        },
    )
    if frozen_history_manifest != PREREGISTERED_HISTORY_MANIFEST_SHA256:
        raise RuntimeError(
            "history manifest differs from preregistration: "
            f"expected={PREREGISTERED_HISTORY_MANIFEST_SHA256}, "
            f"actual={frozen_history_manifest}"
        )
    signal_day_coverage = replay_contract._validate_signal_day_coverage(
        sources_by_split
    )

    history_cache: dict[str, dict[str, Any]] = {}
    features_by_split: dict[str, dict[str, dict[str, Any]]] = {}
    factor_meta_by_split: dict[str, dict[str, Any]] = {}
    for split in SPLITS:
        factor_sources = [
            _factor_input_projection(source)
            for source in sources_by_split[split]
        ]
        if any(
            set(source) & SOURCE_LEGACY_OUTCOME_FIELDS
            for source in factor_sources
        ):
            raise RuntimeError("factor input projection contains legacy outcomes")
        feature_map, meta = _zero_axis_features_for_sources(
            factor_sources, history_cache
        )
        features_by_split[split] = feature_map
        factor_meta_by_split[split] = meta

    return {
        "input_dir": input_dir,
        "output_dir": output_dir,
        "config_path": config_path,
        "staging_dir": staging_dir,
        "static_initial": static_initial,
        "history_initial": history_initial,
        "dependency_contract": dependency_contract,
        "config": config,
        "sources_by_split": sources_by_split,
        "integrity_by_split": integrity_by_split,
        "history_inventory": history_inventory,
        "frozen_history_manifest": frozen_history_manifest,
        "signal_day_coverage": signal_day_coverage,
        "features_by_split": features_by_split,
        "factor_meta_by_split": factor_meta_by_split,
    }


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    prepared = _prepare(args)
    feasibility = {
        split: {
            key: prepared["factor_meta_by_split"][split][key]
            for key in (
                "macd_candidates",
                "assigned_candidates",
                "assignment_coverage",
                "comparable_candidates",
                "comparable_symbols",
                "comparable_signal_days",
                "nearer_n",
                "farther_n",
            )
        }
        for split in SPLITS
    }
    zero_axis_all_sample_eligible = all(
        item["comparable_candidates"] >= MIN_AFFECTED_CANDIDATES
        and item["comparable_symbols"] >= MIN_AFFECTED_SYMBOLS
        and item["comparable_signal_days"] >= MIN_AFFECTED_SIGNAL_DAYS
        for item in feasibility.values()
    )
    return {
        "version": VERSION,
        "technical_preflight_pass": True,
        "static_contract_items": len(prepared["dependency_contract"]),
        "canonical_rows": {
            split: len(prepared["sources_by_split"][split]) for split in SPLITS
        },
        "history_symbol_count": prepared["history_inventory"]["symbol_count"],
        "history_file_count": prepared["history_inventory"]["file_count"],
        "history_manifest_sha256": prepared["frozen_history_manifest"],
        "schema_inventory_manifest_sha256": prepared["history_inventory"][
            "history_manifest_sha256"
        ],
        "zero_axis_outcome_free_feasibility": feasibility,
        "zero_axis_all_splits_sample_eligible": zero_axis_all_sample_eligible,
        "formal_run_allowed_for_strategy_diagnostic": True,
        "formal_run_can_make_zero_axis_candidate_pass": zero_axis_all_sample_eligible,
        "replay_performed": False,
        "output_generated": False,
    }


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_json(row) + "\n")


def _artifact_row_projection(
    row: dict[str, Any], *, publish_zero_axis_pairing: bool
) -> dict[str, Any]:
    if publish_zero_axis_pairing:
        return deepcopy(row)
    forbidden = set(ZERO_AXIS_FEATURE_FIELDS) - {"candidate_id"}
    forbidden.add(PRIMARY_FACTOR)
    return {
        key: deepcopy(value)
        for key, value in row.items()
        if key not in forbidden and not key.startswith("zero_axis_")
    }


def _write_outputs_atomically(
    output_dir: Path,
    staging_dir: Path,
    enriched_by_split: dict[str, list[dict[str, Any]]],
    strategy_cells: list[dict[str, Any]],
    result: dict[str, Any],
    static_initial: dict[str, dict[str, Any]],
    history_initial: dict[str, dict[str, Any]],
    expected_feature_manifests: dict[str, str],
    publish_zero_axis_pairing: bool,
) -> None:
    for split in SPLITS:
        _assert_enriched_feature_manifest(
            enriched_by_split[split],
            expected_feature_manifests[split],
            "writer_entry",
        )
    staging_dir.mkdir(parents=True, exist_ok=False)
    artifacts: dict[str, Any] = {}
    for split in SPLITS:
        filename = f"entry_context_batch_{split}.jsonl"
        staged = staging_dir / filename
        published_rows = [
            _artifact_row_projection(
                row, publish_zero_axis_pairing=publish_zero_axis_pairing
            )
            for row in enriched_by_split[split]
        ]
        if not publish_zero_axis_pairing and any(
            PRIMARY_FACTOR in row
            or "zero_axis_nearer" in row
            or "zero_axis_stratum" in row
            for row in published_rows
        ):
            raise RuntimeError(
                "ineligible zero-axis factor/outcome pairing reached artifact"
            )
        _write_jsonl(staged, published_rows)
        final = output_dir / filename
        digest = file_sha256(staged)
        result["splits"][split]["enriched"] = str(final)
        result["splits"][split]["enriched_sha256"] = digest
        artifacts[split] = {
            "path": str(final),
            "rows": len(published_rows),
            "sha256": digest,
            "size_bytes": staged.stat().st_size,
        }

    cells_name = "strategy_stability_cells.jsonl"
    staged_cells = staging_dir / cells_name
    _write_jsonl(staged_cells, strategy_cells)
    artifacts["strategy_stability_cells"] = {
        "path": str(output_dir / cells_name),
        "rows": len(strategy_cells),
        "sha256": file_sha256(staged_cells),
        "size_bytes": staged_cells.stat().st_size,
    }

    static_pre_publish, static_changes = _snapshot_changes(static_initial)
    history_pre_publish, history_changes = _snapshot_changes(history_initial)
    if static_changes or history_changes:
        raise RuntimeError(
            "protected inputs changed before publish: "
            f"static={static_changes}, history={history_changes}"
        )
    for split in SPLITS:
        _assert_enriched_feature_manifest(
            enriched_by_split[split],
            expected_feature_manifests[split],
            "pre_publish",
        )
    result["pre_publish_input_stability"] = {
        "all_unchanged": True,
        "static_manifest_sha256": _snapshot_manifest(static_pre_publish),
        "history_manifest_sha256": _snapshot_manifest(history_pre_publish),
        "changed_paths": [],
    }
    result["artifacts"] = artifacts
    result["report_path"] = str(output_dir / "entry_context_batch_audit.json")
    staged_report = staging_dir / "entry_context_batch_audit.json"
    with staged_report.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    if output_dir.exists():
        raise RuntimeError(
            f"formal output directory appeared before publish: {output_dir}"
        )
    staging_dir.rename(output_dir)


def run(args: argparse.Namespace) -> dict[str, Any]:
    prepared = _prepare(args)
    static_initial = prepared["static_initial"]
    history_initial = prepared["history_initial"]
    expected_feature_manifests = {
        split: prepared["factor_meta_by_split"][split][
            "feature_manifest_sha256"
        ]
        for split in SPLITS
    }
    zero_preflight_eligibility = {
        split: bool(
            prepared["factor_meta_by_split"][split]["comparable_candidates"]
            >= MIN_AFFECTED_CANDIDATES
            and prepared["factor_meta_by_split"][split]["comparable_symbols"]
            >= MIN_AFFECTED_SYMBOLS
            and prepared["factor_meta_by_split"][split]["comparable_signal_days"]
            >= MIN_AFFECTED_SIGNAL_DAYS
        )
        for split in SPLITS
    }
    zero_preflight_all_eligible = all(zero_preflight_eligibility.values())
    for split in SPLITS:
        _assert_feature_manifest(
            prepared["features_by_split"][split],
            expected_feature_manifests[split],
            "pre_replay",
        )
    static_pre_replay, static_changes = _snapshot_changes(static_initial)
    history_pre_replay, history_changes = _snapshot_changes(history_initial)
    if static_changes or history_changes:
        raise RuntimeError(
            "protected inputs changed before replay: "
            f"static={static_changes}, history={history_changes}"
        )

    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "design": {
            "one_production_risk_replay_per_split": True,
            "replay_call_count_expected": len(SPLITS),
            "study_evaluators_state_isolated": True,
            "studies_share_authoritative_replay": True,
            "statistical_independence_claimed": False,
            "no_cross_study_rescue": True,
            "batch_or_gate_implemented": False,
            "source_legacy_outcomes_stripped_before_replay": True,
            "factor_and_labels_frozen_before_replay": True,
            "holdout_consumed": False,
            "portfolio_layer_implemented": False,
            "production_eligible": False,
            "parameter_scan_performed": False,
            "formula_scan_performed": False,
            "direction_scan_performed": False,
            "reverse_direction_tested": False,
        },
        "preregistration": {
            "study_registry": deepcopy(STUDY_REGISTRY),
            "primary_factor": PRIMARY_FACTOR,
            "primary_factor_formula": "abs(DIF_signal_day) / QFQ_CLOSE_signal_day",
            "primary_factor_smaller_is_better": True,
            "primary_stratum": "signal_day_x_signal_type",
            "primary_comparison": "nearer_minus_farther",
            "primary_outcomes": list(PRIMARY_OUTCOMES),
            "seed": args.seed,
            "bootstrap_reps": args.bootstrap_reps,
        },
        "multiple_testing": {
            "current_batch_registered_candidate_count": 1,
            "historical_candidate_family_size": "unknown_and_greater_than_one",
            "prior_same_factor_explored": True,
            "diagnostic_cells_excluded_from_inference": True,
            "intersection_union_gate_across_splits_and_outcomes": True,
            "canonical_reused_across_prior_factor_audits": True,
            "historical_sequential_reuse_contamination": True,
            "confirmatory_claim_allowed": False,
            "untouched_holdout_required": True,
        },
        "thresholds": {
            "assignment_coverage": MIN_ASSIGNMENT_COVERAGE,
            "affected_candidates": MIN_AFFECTED_CANDIDATES,
            "affected_symbols": MIN_AFFECTED_SYMBOLS,
            "affected_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
            "minimum_outcome_clusters": MIN_OUTCOME_CLUSTERS,
            "strategy_cell_small_n": STRATEGY_SMALL_N,
        },
        "safety": {
            "production_database_connected": False,
            "local_database_connected": False,
            "network_fetch_performed": False,
            "history_cache_write_performed": False,
            "canonical_write_performed": False,
            "config_write_performed": False,
            "freeze_or_seal_write_performed": False,
            "production_decision": "unchanged_P0",
        },
        "input_dir": str(prepared["input_dir"]),
        "output_dir": str(prepared["output_dir"]),
        "config_file": str(prepared["config_path"]),
        "config_file_expected_sha256": PREREGISTERED_CONFIG_SHA256,
        "config_file_sha256_before": static_initial["config"]["sha256"],
        "config_snapshot": _config_snapshot(prepared["config"]),
        "script_expected_sha256": str(args.expected_script_sha256).lower(),
        "script_sha256": static_initial["audit_script"]["sha256"],
        "dependency_contract": prepared["dependency_contract"],
        "candidate_integrity": prepared["integrity_by_split"],
        "history_inventory": {
            "symbol_count": prepared["history_inventory"]["symbol_count"],
            "file_count": prepared["history_inventory"]["file_count"],
            "expected_manifest_sha256": PREREGISTERED_HISTORY_MANIFEST_SHA256,
            "history_manifest_sha256": prepared["frozen_history_manifest"],
            "schema_inventory_manifest_sha256": prepared["history_inventory"][
                "history_manifest_sha256"
            ],
            "adjustments": ["qfq"],
            "local_files_only": True,
            "all_valid": prepared["history_inventory"]["all_valid"],
        },
        "signal_day_coverage": prepared["signal_day_coverage"],
        "pre_replay_input_stability": {
            "all_unchanged": True,
            "static_manifest_sha256": _snapshot_manifest(static_pre_replay),
            "history_manifest_sha256": _snapshot_manifest(history_pre_replay),
            "changed_paths": [],
        },
        "replay_provenance": {
            "driver": "attribution_audit._replay_split",
            "profile": "production_risk",
            "outcomes_generated_in_process": True,
            "external_replay_artifact_consumed": False,
        },
        "splits": {},
        "studies": {},
    }

    enriched_by_split: dict[str, list[dict[str, Any]]] = {}
    replay_by_split: dict[str, list[dict[str, Any]]] = {}
    replay_calls = 0
    for split in SPLITS:
        sources = prepared["sources_by_split"][split]
        replay_sources = [_replay_input_projection(source) for source in sources]
        replay_rows, replay_skips, raw_source_match = _production_replay_split(
            replay_sources,
            prepared["config"],
            "production_risk",
            HISTORY_DIR,
        )
        replay_calls += 1
        replay_integrity, source_match = _assert_replay_integrity(
            sources,
            prepared["features_by_split"][split],
            replay_rows,
            replay_skips,
            raw_source_match,
        )
        _validate_authoritative_outcomes(replay_rows)
        enriched: list[dict[str, Any]] = []
        for replay in replay_rows:
            row = dict(replay)
            identifier = candidate_id(row)
            row.update(prepared["features_by_split"][split][identifier])
            row["normalized_regime"] = _normalized_regime(row.get("regime"))
            row["normalized_signal_type"] = _normalized_signal_type(
                row.get("signal_type")
            )
            row["signal_month"] = str(row["signal_day"])[:7]
            row["exit_category"] = exit_reason_category(row.get("exit_reason"))
            enriched.append(row)
        manifest = _study_projection_manifest(enriched)
        enriched_by_split[split] = enriched
        _assert_enriched_feature_manifest(
            enriched,
            expected_feature_manifests[split],
            "post_join",
        )
        replay_by_split[split] = deepcopy(enriched)
        source_path = prepared["input_dir"] / f"candidates_{split}.jsonl"
        result["splits"][split] = {
            "source": str(source_path),
            "source_sha256": static_initial[f"canonical_{split}"]["sha256"],
            "source_rows": len(sources),
            "replay_rows": len(replay_rows),
            "replay_call_count": 1,
            "replay_skips": dict(replay_skips),
            "replay_integrity": replay_integrity,
            "source_match": source_match,
            "feature_manifest_sha256": prepared["factor_meta_by_split"][split][
                "feature_manifest_sha256"
            ],
            "authoritative_replay_projection_manifest_sha256": manifest,
            "enriched": None,
            "enriched_sha256": None,
        }
    if replay_calls != len(SPLITS):
        raise RuntimeError(
            f"replay call contract failed: expected={len(SPLITS)}, actual={replay_calls}"
        )
    result["replay_call_count_actual"] = replay_calls

    manifests_before = {
        split: _full_rows_manifest(replay_by_split[split]) for split in SPLITS
    }
    strategy_input = {
        split: [
            _strategy_study_projection(row) for row in replay_by_split[split]
        ]
        for split in SPLITS
    }
    strategy_report, strategy_cells = _strategy_stability_report(strategy_input)
    if any(
        _full_rows_manifest(replay_by_split[split]) != manifests_before[split]
        for split in SPLITS
    ):
        raise RuntimeError("strategy stability evaluator mutated shared replay")

    zero_split_reports: dict[str, dict[str, Any]] = {}
    if zero_preflight_all_eligible:
        for index, split in enumerate(SPLITS):
            zero_input = [
                _zero_axis_study_projection(row)
                for row in replay_by_split[split]
            ]
            zero_split_reports[split] = _zero_axis_split_report(
                zero_input,
                result["splits"][split]["replay_integrity"],
                prepared["factor_meta_by_split"][split],
                PREREGISTERED_SEED + index * 100,
            )
        if any(
            _full_rows_manifest(replay_by_split[split])
            != manifests_before[split]
            for split in SPLITS
        ):
            raise RuntimeError("zero-axis evaluator mutated shared replay")
        zero_gate = _zero_axis_cross_split_gate(zero_split_reports)
    else:
        zero_split_reports = {
            split: _zero_axis_preflight_only_report(
                prepared["factor_meta_by_split"][split],
                result["splits"][split]["replay_integrity"],
            )
            for split in SPLITS
        }
        zero_gate = {
            "required_splits": list(SPLITS),
            "preflight_sample_eligible_splits": [
                split for split in SPLITS if zero_preflight_eligibility[split]
            ],
            "eligible_splits": [],
            "all_required_splits_present": True,
            "all_required_splits_eligible": False,
            "outcome_analysis_performed": False,
            "directions": {},
            "direction_consistent": False,
            "cluster_bootstrap_supported": False,
            "reason": "preflight_sample_ineligible",
            "pass": False,
        }
    result["studies"] = {
        "strategy_stability_diagnostic": strategy_report,
        "zero_axis_distance_candidate": {
            **STUDY_REGISTRY["zero_axis_distance_candidate"],
            "factor_contract": {
                "primary_factor": PRIMARY_FACTOR,
                "formula": "abs(DIF_signal_day) / QFQ_CLOSE_signal_day",
                "smaller_is_better": True,
                "universe": list(MACD_SIGNAL_TYPES),
                "stratum": "signal_day_x_signal_type",
                "comparison": "nearer_minus_farther",
                "buy_1_excluded": True,
            },
            "by_split": zero_split_reports,
            "preflight_eligibility": {
                "by_split": zero_preflight_eligibility,
                "all_splits_eligible": zero_preflight_all_eligible,
                "status": (
                    "eligible_for_historical_gate"
                    if zero_preflight_all_eligible
                    else "ineligible_preflight"
                ),
            },
            "candidate_gate": zero_gate,
            "candidate_level_factor_outcome_pairing_published": (
                zero_preflight_all_eligible
            ),
            "verdict": (
                "sample_insufficient"
                if not zero_preflight_all_eligible
                else (
                    "historical_candidate_support_requires_blind_holdout"
                    if zero_gate["pass"]
                    else "candidate_layer_rejected_or_insufficient_sample"
                )
            ),
        },
    }

    static_after, static_after_changes = _snapshot_changes(static_initial)
    history_after, history_after_changes = _snapshot_changes(history_initial)
    if static_after_changes or history_after_changes:
        raise RuntimeError(
            "protected inputs changed during audit: "
            f"static={static_after_changes}, history={history_after_changes}"
        )
    result["protected_inputs"] = {
        "all_unchanged": True,
        "static_path_count": len(static_initial),
        "history_path_count": len(history_initial),
        "total_path_count": len(static_initial) + len(history_initial),
        "static_initial_manifest_sha256": _snapshot_manifest(static_initial),
        "static_after_manifest_sha256": _snapshot_manifest(static_after),
        "history_initial_manifest_sha256": _snapshot_manifest(history_initial),
        "history_after_manifest_sha256": _snapshot_manifest(history_after),
        "changed_paths": [],
    }
    result["config_file_sha256_after"] = static_after["config"]["sha256"]
    result["config_file_unchanged"] = (
        result["config_file_sha256_after"]
        == result["config_file_sha256_before"]
    )
    result["baseline_replay_complete"] = all(
        result["splits"][split]["source_match"]["baseline_replay_complete"]
        for split in SPLITS
    )
    result["batch_contract_pass"] = bool(
        result["baseline_replay_complete"]
        and replay_calls == len(SPLITS)
        and strategy_report["diagnostic_completeness_gate"]["all_pass"]
        and result["protected_inputs"]["all_unchanged"]
    )
    if not result["batch_contract_pass"]:
        raise RuntimeError("batch technical contract failed before publication")
    result["batch_verdict"] = "registered_studies_completed_no_production_change"
    result["production_decision"] = "unchanged_P0"
    _write_outputs_atomically(
        prepared["output_dir"],
        prepared["staging_dir"],
        enriched_by_split,
        strategy_cells,
        result,
        static_initial,
        history_initial,
        expected_feature_manifests,
        zero_preflight_all_eligible,
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=str(CANONICAL_INPUT_DIR))
    parser.add_argument("--output-dir", default=str(FORMAL_OUTPUT_DIR))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--label", default=PREREGISTERED_LABEL)
    parser.add_argument("--splits", nargs="+", default=list(SPLITS))
    parser.add_argument("--seed", type=int, default=PREREGISTERED_SEED)
    parser.add_argument(
        "--bootstrap-reps", type=int, default=PREREGISTERED_BOOTSTRAP_REPS
    )
    parser.add_argument("--expected-script-sha256", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.preflight_only:
        print(json.dumps(preflight(args), ensure_ascii=False, indent=2))
        return 0
    result = run(args)
    print(
        json.dumps(
            {
                "version": result["version"],
                "replay_call_count_actual": result["replay_call_count_actual"],
                "strategy_stability_status": result["studies"]
                ["strategy_stability_diagnostic"]["status"],
                "zero_axis_candidate_gate": result["studies"]
                ["zero_axis_distance_candidate"]["candidate_gate"],
                "zero_axis_verdict": result["studies"]
                ["zero_axis_distance_candidate"]["verdict"],
                "batch_contract_pass": result["batch_contract_pass"],
                "production_decision": result["production_decision"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
