"""Read-only audit of canonical source outcomes versus versioned replays.

The canonical train/validation/test files are identity artifacts reused by
candidate-factor research.  Their lifecycle outcomes were produced before the
current replay contract was versioned, so this audit compares those legacy
fields with two fixed current-simulator profiles without modifying any input.
It deliberately does not consume Holdout or make a candidate/production gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import attribution_audit
import backtest_winrate
import candidate_integrity
from attribution_audit import _config_snapshot, _replay_split
from backtest_winrate import HISTORY_DIR, prepare_closed_bars
from candidate_integrity import (
    VERSION as INTEGRITY_VERSION,
    candidate_id,
    candidate_ids_sha256,
    file_sha256,
    load_jsonl,
)
from utils.helpers import load_config


VERSION = "canonical_replay_semantics_audit.v1"
PREREGISTERED_LABEL = "canonical-replay-semantics-drift-audit"
SPLITS = ("train", "val", "test")
REPLAY_PROFILES = (
    "production_risk",
    "no_risk_sl_tp_current_simulator",
)
PROFILE_DRIVER_NAMES = {
    "production_risk": "production_risk",
    "no_risk_sl_tp_current_simulator": "frozen_source",
}
CANONICAL_INPUT_DIR = Path(r"D:\tmp\candidates_fullpool_canonical").resolve()
DEFAULT_OUTPUT_DIR = Path(r"D:\tmp\canonical_replay_semantics_audit")
DEFAULT_CONFIG = (BASE_DIR / "config" / "config.yaml").resolve()
PREREGISTERED_CONFIG_SHA256 = (
    "4e0084eb945aa2533dca25f9e0f68b4e27e9f2cbc52c11b7898c082d466000f1"
)
PREREGISTERED_BACKTEST_SHA256 = (
    "586e21d8050a4397f2664b41676c2c5c140754f161c1937e27a2fd7e875800d1"
)
PREREGISTERED_ATTRIBUTION_SHA256 = (
    "fdb13ef7d20bdf024a14f6dffe17d47c14ec73ddcfd72044bca527eb34154f4d"
)
PREREGISTERED_CANDIDATE_INTEGRITY_SHA256 = (
    "fb7e197a7bb11963b5f6b2042965f946b48bef4ab89789ca1d7c5ad4cfb4e53f"
)
PREREGISTERED_CHAN_STRATEGY_SHA256 = (
    "af70eab3207c168254f70e8674fd1ce6293e0b7937a8a2fb8a842640b306d058"
)
PREREGISTERED_MACD_STRATEGY_SHA256 = (
    "cb1e5ff36086fca041d899199c7ed8a093929feff294bc0f40da2c65522866c9"
)
PREREGISTERED_SIGNAL_POLICY_SHA256 = (
    "2288fd4492f860a1705d1539d7b1b5b396a44f81c9e333cb6fafdc3ea2b56d34"
)
PREREGISTERED_MARKET_GATE_SHA256 = (
    "fa69dbee01817e44b7b4bc73c50c9a37fa5b4f8b4eb08460dc134979d6d7abe6"
)
PREREGISTERED_UTILS_HELPERS_SHA256 = (
    "3ec75b774efe60cbe678f5dbda78a84a70a6a12442a1f458e121bc33e6804ae1"
)
NUMERIC_TOLERANCE_PP = 0.05

IDENTITY_FIELDS = frozenset(("symbol", "signal_day", "signal_type"))
IDENTITY_FIELD_ORDER = ("symbol", "signal_day", "signal_type")
SOURCE_COMPARABLE_FIELDS = (
    "entry_day",
    "exit_reason",
    "holding_days",
    "trade_pnl_pct",
    "future_5d",
    "future_20d",
    "future_40d",
    "mfe",
    "mae",
)
OUTCOME_FIELDS = frozenset(
    (
        "entry_day",
        "entry_price",
        "exit_trigger_day",
        "exit_day",
        "exit_price",
        "exit_reason",
        "holding_days",
        "holding_bars",
        "trade_pnl_pct",
        "pnl_pct",
        "future_5d",
        "future_20d",
        "future_40d",
        "mfe",
        "mae",
        "post_exit_5d",
        "post_exit_20d",
        "entry_commission_cash",
        "exit_commission_cash",
        "stamp_tax_cash",
        "slippage_cash",
    )
)

FIELD_SPECS: dict[str, dict[str, Any]] = {
    "entry_day": {"kind": "exact"},
    "entry_price": {"kind": "numeric", "tolerance": 0.0001},
    "exit_trigger_day": {"kind": "exact"},
    "exit_day": {"kind": "exact"},
    "exit_price": {"kind": "numeric", "tolerance": 0.0001},
    "exit_reason": {"kind": "exact"},
    "holding_days": {"kind": "numeric", "tolerance": 0.0},
    "holding_bars": {"kind": "numeric", "tolerance": 0.0},
    "trade_pnl_pct": {"kind": "numeric", "tolerance": NUMERIC_TOLERANCE_PP},
    "future_5d": {"kind": "numeric", "tolerance": NUMERIC_TOLERANCE_PP},
    "future_20d": {"kind": "numeric", "tolerance": NUMERIC_TOLERANCE_PP},
    "future_40d": {"kind": "numeric", "tolerance": NUMERIC_TOLERANCE_PP},
    "mfe": {"kind": "numeric", "tolerance": NUMERIC_TOLERANCE_PP},
    "mae": {"kind": "numeric", "tolerance": NUMERIC_TOLERANCE_PP},
    "entry_commission_cash": {"kind": "numeric", "tolerance": 0.01},
    "exit_commission_cash": {"kind": "numeric", "tolerance": 0.01},
    "stamp_tax_cash": {"kind": "numeric", "tolerance": 0.01},
    "slippage_cash": {"kind": "numeric", "tolerance": 0.01},
}

RISK_REASONS = {"stop_loss", "take_profit"}
TIMEOUT_REASONS = {
    "timeout",
    "timeout_ma_break",
    "timeout_hard_cap",
    "window_end",
}
SELL_SIGNAL_REASONS = {
    "sell_1",
    "sell_2",
    "sell_3",
    "zero_axis_death_cross",
}

FUTURE_REPLAY_CONTRACT: dict[str, Any] = {
    "source_artifact_role": "signal_identity_only",
    "authoritative_outcome_source": "versioned_replay",
    "candidate_id_fields": list(IDENTITY_FIELD_ORDER),
    "split_assignment": "fixed_source_filename",
    "legacy_source_outcomes_role": "diagnostic_only",
    "required_replay_metadata": [
        "replay_version",
        "simulator_script_sha256",
        "replay_driver_sha256",
        "config_sha256",
        "execution_config_snapshot",
        "history_manifest_sha256",
        "source_candidate_ids_sha256",
        "replay_candidate_ids_sha256",
    ],
    "outcomes_must_not_affect_membership": True,
    "historical_candidate_selection_outcome_independence": "unknown",
    "holdout_requires_separate_sealed_contract": True,
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _ids_sequence_sha256(ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def _history_manifest_sha256(hashes: dict[str, str]) -> str:
    return hashlib.sha256(
        "\n".join(
            f"{symbol}|{hashes[symbol]}" for symbol in sorted(hashes)
        ).encode("utf-8")
    ).hexdigest()


def _as_finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float) -> float:
    return round(float(value), 8)


def _numeric_stats(values: Iterable[float]) -> dict[str, Any]:
    numbers = [float(value) for value in values if math.isfinite(float(value))]
    if not numbers:
        return {"n": 0}
    ordered = sorted(numbers)
    p95_index = min(math.ceil(len(ordered) * 0.95) - 1, len(ordered) - 1)
    return {
        "n": len(numbers),
        "mean": _round(statistics.fmean(numbers)),
        "median": _round(statistics.median(numbers)),
        "min": _round(ordered[0]),
        "max": _round(ordered[-1]),
        "p95": _round(ordered[p95_index]),
    }


def _compare_field(source: dict[str, Any], replay: dict[str, Any], field: str) -> dict[str, Any]:
    spec = FIELD_SPECS[field]
    source_available = field in source and source.get(field) is not None
    replay_available = field in replay and replay.get(field) is not None
    result: dict[str, Any] = {
        "source_available": source_available,
        "replay_available": replay_available,
        "source": source.get(field),
        "replay": replay.get(field),
        "match": None,
        "availability_status": "both_unavailable",
    }
    if not source_available and not replay_available:
        return result
    if source_available != replay_available:
        result["availability_status"] = (
            "replay_only" if replay_available else "source_only"
        )
        result["match"] = False
        return result
    result["availability_status"] = "both_available"
    if spec["kind"] == "exact":
        result["match"] = str(source[field]) == str(replay[field])
        return result
    source_number = _as_finite_number(source[field])
    replay_number = _as_finite_number(replay[field])
    if source_number is None or replay_number is None:
        result["numeric_values_valid"] = False
        return result
    signed = replay_number - source_number
    absolute = abs(signed)
    tolerance = float(spec["tolerance"])
    result.update(
        {
            "numeric_values_valid": True,
            "tolerance": tolerance,
            "signed_diff": _round(signed),
            "abs_diff": _round(absolute),
            "match": absolute <= tolerance + 1e-12,
        }
    )
    return result


def _drift_categories(differences: dict[str, dict[str, Any]]) -> list[str]:
    changed = {
        field
        for field, detail in differences.items()
        if detail.get("match") is False and detail.get("source_available")
    }
    if not changed:
        return []
    categories: list[str] = []
    if "entry_day" in changed:
        categories.append("entry_calendar_or_history")
    if "exit_reason" in changed:
        source_reason = str(differences["exit_reason"].get("source"))
        replay_reason = str(differences["exit_reason"].get("replay"))
        reasons = {source_reason, replay_reason}
        if reasons & RISK_REASONS:
            categories.append("risk_exit_semantics")
        elif reasons & TIMEOUT_REASONS:
            categories.append("timeout_semantics")
        elif reasons & SELL_SIGNAL_REASONS:
            categories.append("sell_signal_semantics")
    if (
        "trade_pnl_pct" in changed
        and differences["exit_reason"].get("match") is True
        and differences["holding_days"].get("match") is True
    ):
        categories.append("price_cost_or_history")
    if (
        ("holding_days" in changed or "exit_day" in changed)
        and not any(
            item in categories
            for item in ("risk_exit_semantics", "timeout_semantics", "sell_signal_semantics")
        )
    ):
        categories.append("exit_timing_semantics")
    if not categories:
        categories.append("unresolved")
    return categories


def _compare_candidate(
    source: dict[str, Any],
    replay: dict[str, Any],
    *,
    split: str,
    profile: str,
) -> dict[str, Any]:
    source_id = candidate_id(source)
    replay_id = candidate_id(replay)
    explicit_replay_id = replay.get("candidate_id")
    if explicit_replay_id is not None and str(explicit_replay_id) != replay_id:
        raise RuntimeError(
            "explicit replay candidate_id disagrees with replay identity fields: "
            f"{explicit_replay_id} != {replay_id}"
        )
    if source_id != replay_id:
        raise RuntimeError(
            f"candidate id mismatch during comparison: {source_id} != {replay_id}"
        )
    differences = {
        field: _compare_field(source, replay, field) for field in FIELD_SPECS
    }
    comparable = [
        detail.get("match")
        for detail in differences.values()
        if detail.get("availability_status") == "both_available"
    ]
    return {
        "candidate_id": source_id,
        "split": split,
        "profile": profile,
        "identity": {field: source.get(field) for field in sorted(IDENTITY_FIELDS)},
        "context": {
            "regime": source.get("regime"),
            "market_cap": source.get("market_cap"),
        },
        "differences": differences,
        "drift_categories": _drift_categories(differences),
        "all_comparable_fields_match": bool(comparable) and all(comparable),
        "source_fields_unavailable": [
            field
            for field, detail in differences.items()
            if not detail["source_available"]
        ],
    }


def _aggregate_comparisons(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    total_comparable = 0
    total_matches = 0
    for field, spec in FIELD_SPECS.items():
        details = [row["differences"][field] for row in rows]
        comparable = [
            detail
            for detail in details
            if detail.get("availability_status") == "both_available"
        ]
        matches = sum(detail.get("match") is True for detail in comparable)
        total_comparable += len(comparable)
        total_matches += matches
        field_report: dict[str, Any] = {
            "source_available": sum(detail["source_available"] for detail in details),
            "replay_available": sum(detail["replay_available"] for detail in details),
            "comparable": len(comparable),
            "matches": matches,
            "both_unavailable": sum(
                detail.get("availability_status") == "both_unavailable"
                for detail in details
            ),
            "one_sided_availability_drift": sum(
                detail.get("availability_status") in {"source_only", "replay_only"}
                for detail in details
            ),
            "match_rate_pct": (
                round(matches / len(comparable) * 100.0, 2)
                if comparable
                else None
            ),
        }
        if spec["kind"] == "numeric":
            signed = [
                float(detail["signed_diff"])
                for detail in comparable
                if detail.get("signed_diff") is not None
            ]
            absolute = [
                float(detail["abs_diff"])
                for detail in comparable
                if detail.get("abs_diff") is not None
            ]
            field_report["signed_diff"] = _numeric_stats(signed)
            field_report["absolute_diff"] = _numeric_stats(absolute)
        fields[field] = field_report

    transitions: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    unavailable: Counter[str] = Counter()
    exact_rows = 0
    for row in rows:
        reason = row["differences"]["exit_reason"]
        if reason.get("match") is False:
            transitions[f"{reason.get('source')}->{reason.get('replay')}"] += 1
        categories.update(row["drift_categories"])
        unavailable.update(row["source_fields_unavailable"])
        exact_rows += int(row["all_comparable_fields_match"])

    exit_rate = fields["exit_reason"]["match_rate_pct"]
    pnl_abs_median = fields["trade_pnl_pct"]["absolute_diff"].get("median")
    return {
        "n": len(rows),
        "fields": fields,
        "comparable_field_matches": total_matches,
        "comparable_field_observations": total_comparable,
        "comparable_field_match_rate_pct": (
            round(total_matches / total_comparable * 100.0, 2)
            if total_comparable
            else None
        ),
        "all_comparable_fields_match_rows": exact_rows,
        "all_comparable_fields_match_rate_pct": (
            round(exact_rows / len(rows) * 100.0, 2) if rows else None
        ),
        "exit_reason_transitions": dict(sorted(transitions.items())),
        "drift_category_counts": dict(sorted(categories.items())),
        "source_unavailable_field_counts": dict(sorted(unavailable.items())),
        "legacy_source_artifact_match_heuristic": {
            "diagnostic_only": True,
            "criteria": (
                "exit_reason_match_rate>=95 and trade_pnl_abs_diff_median<=0.05pp"
            ),
            "pass": bool(
                (exit_rate is None or exit_rate >= 95.0)
                and (
                    pnl_abs_median is None
                    or pnl_abs_median <= NUMERIC_TOLERANCE_PP
                )
            ),
        },
    }


def _observed_difference_summary(
    comparison_report: dict[str, Any],
) -> dict[str, Any]:
    by_field: dict[str, Any] = {}
    total = 0
    for field in SOURCE_COMPARABLE_FIELDS:
        field_report = comparison_report["fields"][field]
        value_mismatches = int(field_report["comparable"]) - int(
            field_report["matches"]
        )
        availability_drift = int(field_report["one_sided_availability_drift"])
        observed = value_mismatches + availability_drift
        total += observed
        by_field[field] = {
            "value_mismatches": value_mismatches,
            "one_sided_availability_drift": availability_drift,
            "observed_differences": observed,
        }
    return {
        "source_comparable_fields": list(SOURCE_COMPARABLE_FIELDS),
        "by_field": by_field,
        "observed_difference_count": total,
        "detected": total > 0,
        "materiality_threshold_applied": False,
    }


def _membership_contract(rows: list[dict[str, Any]], split: str) -> dict[str, Any]:
    if split not in SPLITS:
        raise RuntimeError(f"unregistered split in membership contract: {split}")
    ids_before = [candidate_id(row) for row in rows]
    if len(ids_before) != len(set(ids_before)):
        raise RuntimeError(f"candidate ids must be unique in split {split}")
    redacted = [
        {key: value for key, value in row.items() if key not in OUTCOME_FIELDS}
        for row in rows
    ]
    ids_after = [candidate_id(row) for row in redacted]
    checks = {
        "identity_fields_disjoint_from_outcomes": IDENTITY_FIELDS.isdisjoint(
            OUTCOME_FIELDS
        ),
        "candidate_ids_unique": len(ids_before) == len(set(ids_before)),
        "ids_unchanged_after_outcome_redaction": ids_before == ids_after,
        "id_sequence_hash_unchanged_after_outcome_redaction": (
            _ids_sequence_sha256(ids_before) == _ids_sequence_sha256(ids_after)
        ),
        "sorted_id_hash_unchanged_after_outcome_redaction": (
            candidate_ids_sha256(rows) == candidate_ids_sha256(redacted)
        ),
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(f"membership outcome-independence failed: {checks}")
    return {
        **checks,
        "split_assignment_source": f"fixed_filename:{split}",
        "candidate_ids_before": ids_before,
        "candidate_ids_after": ids_after,
        "candidate_id_sequence_sha256": _ids_sequence_sha256(ids_before),
        "candidate_ids_sha256": candidate_ids_sha256(rows),
        "legacy_outcome_fields_present": sorted(
            {field for row in rows for field in OUTCOME_FIELDS if field in row}
        ),
    }


def _validate_integrity_manifest(
    input_dir: Path,
    split: str,
    source_path: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest_path = input_dir / "candidate_integrity_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"candidate integrity manifest not found: {manifest_path}")
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    record = (manifest.get("splits", {}) or {}).get(split, {}) or {}
    checks = {
        "manifest_version_match": manifest.get("version") == INTEGRITY_VERSION,
        "manifest_output_dir_match": Path(
            str(manifest.get("output_dir", ""))
        ).resolve()
        == input_dir.resolve(),
        "output_file_match": Path(str(record.get("output_file", ""))).resolve()
        == source_path.resolve(),
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
    return {
        **checks,
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": file_sha256(manifest_path),
    }


def _assert_replay_integrity(
    sources: list[dict[str, Any]],
    replay_rows: list[dict[str, Any]],
    skipped: Counter | dict[str, int],
    match: dict[str, Any],
    expected_history_manifest: str,
    profile: str,
) -> dict[str, Any]:
    source_ids = [candidate_id(row) for row in sources]
    replay_ids = [candidate_id(row) for row in replay_rows]
    explicit_ids_match = all(
        row.get("candidate_id") is None
        or str(row.get("candidate_id")) == candidate_id(row)
        for row in replay_rows
    )
    checks = {
        "source_ids_unique": len(source_ids) == len(set(source_ids)),
        "replay_ids_unique": len(replay_ids) == len(set(replay_ids)),
        "explicit_replay_ids_match_identity_fields": explicit_ids_match,
        "source_replay_ids_order_match": source_ids == replay_ids,
        "skips_empty": not dict(skipped),
        "source_rows_match": match.get("source_rows") == len(sources),
        "eligible_rows_match": match.get("common_eligible_rows") == len(sources),
        "replayed_rows_match": match.get("simulated_rows") == len(sources),
        "history_manifest_match": match.get("history_manifest_sha256")
        == expected_history_manifest,
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(f"{profile} replay integrity failed: {checks}")
    return checks


def _validate_fixed_args(args: argparse.Namespace) -> None:
    input_dir = Path(args.input_dir).expanduser().resolve()
    config = Path(args.config).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if input_dir != CANONICAL_INPUT_DIR:
        raise RuntimeError(
            f"input dir must remain canonical development input {CANONICAL_INPUT_DIR}"
        )
    if config != DEFAULT_CONFIG:
        raise RuntimeError(f"config path must remain {DEFAULT_CONFIG}")
    if tuple(args.splits) != SPLITS:
        raise RuntimeError(
            f"required splits are exactly {SPLITS}; Holdout/custom splits are forbidden"
        )
    if tuple(args.profiles) != REPLAY_PROFILES:
        raise RuntimeError(f"replay profiles are fixed at {REPLAY_PROFILES}")
    if args.label != PREREGISTERED_LABEL:
        raise RuntimeError("label differs from the pre-registered label")
    if output_dir == input_dir or input_dir in output_dir.parents:
        raise RuntimeError("output must not be inside the canonical input directory")


def _paths_snapshot(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for path in sorted({item.expanduser().resolve() for item in paths}, key=str):
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"protected input file does not exist: {path}")
        snapshot[str(path)] = {
            "sha256": file_sha256(path),
            "size": path.stat().st_size,
        }
    return snapshot


def _compare_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    all_paths = sorted(set(before) | set(after))
    changed = [path for path in all_paths if before.get(path) != after.get(path)]
    return {
        "all_unchanged": not changed,
        "changed_paths": changed,
        "before_path_count": len(before),
        "after_path_count": len(after),
    }


def _assert_history_inventory_matches_snapshot(
    history_inventory: dict[str, Any],
    initial_history_snapshot: dict[str, dict[str, Any]],
    *,
    history_dir: Path = HISTORY_DIR,
) -> None:
    mismatches = []
    for symbol, expected_hash in history_inventory["file_sha256_by_symbol"].items():
        path = str((history_dir / f"{symbol}_qfq.pkl").resolve())
        if initial_history_snapshot.get(path, {}).get("sha256") != expected_hash:
            mismatches.append(symbol)
    if mismatches:
        raise RuntimeError(
            "history files changed between initial snapshot and validated inventory: "
            + ", ".join(mismatches[:20])
        )


def _history_inventory(
    symbols: Iterable[str],
    *,
    history_dir: Path = HISTORY_DIR,
) -> tuple[dict[str, Any], list[Path]]:
    hashes: dict[str, str] = {}
    paths: list[Path] = []
    first_days: list[str] = []
    last_days: list[str] = []
    for symbol in sorted({str(item).zfill(6) for item in symbols}):
        path = (history_dir / f"{symbol}_qfq.pkl").resolve()
        if not path.exists():
            raise RuntimeError(f"missing QFQ replay history: {path}")
        try:
            frame = pd.read_pickle(path)
        except Exception as exc:
            raise RuntimeError(f"unable to read QFQ replay history {path}: {exc}") from exc
        if not isinstance(frame, pd.DataFrame):
            raise RuntimeError(f"QFQ replay history is not a DataFrame: {path}")
        required = {"datetime", "open", "high", "low", "close", "is_closed"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise RuntimeError(f"history missing required columns {missing}: {path}")
        if str(frame.attrs.get("adjust", "")).lower() != "qfq":
            raise RuntimeError(f"history adjustment is not qfq: {path}")
        if str(frame.attrs.get("timeframe", "")).lower() != "1d":
            raise RuntimeError(f"history timeframe is not 1d: {path}")
        raw_dates = pd.to_datetime(frame["datetime"], errors="coerce")
        if raw_dates.isna().any() or not raw_dates.is_monotonic_increasing:
            raise RuntimeError(f"history dates are invalid or unsorted: {path}")
        if raw_dates.dt.date.duplicated().any():
            raise RuntimeError(f"history has duplicate trading dates: {path}")
        closed = prepare_closed_bars(frame)
        if closed.empty:
            raise RuntimeError(f"history has no valid closed bars: {path}")
        dates = pd.to_datetime(closed["datetime"], errors="coerce")
        if dates.isna().any() or not dates.is_monotonic_increasing:
            raise RuntimeError(f"history dates are invalid or unsorted: {path}")
        if dates.dt.date.duplicated().any():
            raise RuntimeError(f"history has duplicate trading dates: {path}")
        prices = closed[["open", "high", "low", "close"]].apply(
            pd.to_numeric, errors="coerce"
        )
        valid = prices.notna().all(axis=1) & prices.gt(0).all(axis=1)
        valid &= prices.apply(lambda column: column.map(math.isfinite)).all(axis=1)
        valid &= prices["high"] >= prices[["open", "close"]].max(axis=1)
        valid &= prices["low"] <= prices[["open", "close"]].min(axis=1)
        if not bool(valid.all()):
            raise RuntimeError(f"history has invalid OHLC rows: {path}")
        hashes[symbol] = file_sha256(path)
        paths.append(path)
        first_days.append(dates.iloc[0].date().isoformat())
        last_days.append(dates.iloc[-1].date().isoformat())
    return (
        {
            "history_dir": str(history_dir.resolve()),
            "symbol_count": len(hashes),
            "file_count": len(paths),
            "history_manifest_sha256": _history_manifest_sha256(hashes),
            "file_sha256_by_symbol": dict(sorted(hashes.items())),
            "earliest_first_day": min(first_days) if first_days else None,
            "latest_last_day": max(last_days) if last_days else None,
            "adjustment": "qfq",
            "timeframe": "1d",
            "local_files_only": True,
            "all_valid": True,
        },
        paths,
    )


def _validate_signal_day_coverage(
    sources_by_split: dict[str, list[dict[str, Any]]],
    *,
    history_dir: Path = HISTORY_DIR,
) -> dict[str, Any]:
    dates_by_symbol: dict[str, list[date]] = {}
    failures: list[dict[str, Any]] = []
    checked = 0
    for split in SPLITS:
        for source in sources_by_split[split]:
            checked += 1
            symbol = str(source.get("symbol", "")).zfill(6)
            if symbol not in dates_by_symbol:
                path = history_dir / f"{symbol}_qfq.pkl"
                frame = pd.read_pickle(path)
                closed = prepare_closed_bars(frame)
                dates_by_symbol[symbol] = [
                    pd.Timestamp(value).date() for value in closed["datetime"]
                ]
            dates = dates_by_symbol[symbol]
            try:
                signal_day = date.fromisoformat(str(source.get("signal_day", "")))
            except ValueError:
                failures.append(
                    {
                        "split": split,
                        "candidate_id": candidate_id(source),
                        "reason": "invalid_signal_day",
                    }
                )
                continue
            index_by_day = {value: index for index, value in enumerate(dates)}
            signal_index = index_by_day.get(signal_day)
            if signal_index is None:
                failures.append(
                    {
                        "split": split,
                        "candidate_id": candidate_id(source),
                        "reason": "missing_exact_signal_day_bar",
                        "history_first_day": dates[0].isoformat() if dates else None,
                        "history_last_day": dates[-1].isoformat() if dates else None,
                    }
                )
                continue
            if signal_index + 1 >= len(dates):
                failures.append(
                    {
                        "split": split,
                        "candidate_id": candidate_id(source),
                        "reason": "missing_next_entry_bar",
                        "signal_day": signal_day.isoformat(),
                    }
                )
    report = {
        "checked_candidates": checked,
        "checked_symbols": len(dates_by_symbol),
        "exact_signal_day_required": True,
        "next_entry_bar_required": True,
        "failures": failures,
        "all_pass": not failures,
    }
    if failures:
        raise RuntimeError(
            "signal-day replay coverage validation failed: "
            + _canonical_json(failures[:20])
        )
    return report


def _profile_attribution(profile_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def rate(profile: str, field: str) -> float | None:
        value = profile_reports[profile]["fields"][field].get("match_rate_pct")
        return float(value) if value is not None else None

    production_exit = rate("production_risk", "exit_reason")
    no_risk_exit = rate("no_risk_sl_tp_current_simulator", "exit_reason")
    production_pnl = rate("production_risk", "trade_pnl_pct")
    no_risk_pnl = rate("no_risk_sl_tp_current_simulator", "trade_pnl_pct")
    return {
        "overall_profile_winner_selected": False,
        "reason": (
            "no post-hoc cross-field weighting; profiles are compared field by field"
        ),
        "risk_profile_explanatory_improvement": {
            "comparison": (
                "no_risk_sl_tp_current_simulator_minus_production_risk"
            ),
            "exit_reason_match_rate_pp": (
                round(no_risk_exit - production_exit, 2)
                if no_risk_exit is not None and production_exit is not None
                else None
            ),
            "trade_pnl_match_rate_pp": (
                round(no_risk_pnl - production_pnl, 2)
                if no_risk_pnl is not None and production_pnl is not None
                else None
            ),
            "interpretation": (
                "positive improvement supports risk-exit-profile differences as a "
                "partial explanation; residual mismatch remains unresolved, with "
                "possible contributors including unversioned simulator/config/history, "
                "legacy field definitions, rounding, data revisions, or defects"
            ),
        },
        "profile_isolation_contract": {
            "same_current_simulator": True,
            "same_current_config_file": True,
            "same_history_manifest": True,
            "same_candidate_identity_and_order": True,
            "isolated_profile_difference": (
                "production_risk enables configured SL/TP; the diagnostic profile "
                "disables risk SL/TP, retains current sell signals, uses a maximum "
                "holding limit of 40 bars, and keeps current fees/T+1/price-limit rules"
            ),
            "underlying_helper_profile_name": "frozen_source",
            "exact_historical_profile": False,
        },
        "exact_source_semantics_reconstructable": False,
        "exact_historical_version_identified": False,
        "limitation": (
            "canonical source has no producer script/config/replay hashes and omits "
            "entry/exit prices plus exit dates"
        ),
    }


def _dependency_definitions() -> dict[str, tuple[Path, str]]:
    paths = {
        "backtest_winrate": Path(backtest_winrate.__file__).resolve(),
        "attribution_audit": Path(attribution_audit.__file__).resolve(),
        "candidate_integrity": Path(candidate_integrity.__file__).resolve(),
        "strategy_chan": (BASE_DIR / "strategy" / "chan.py").resolve(),
        "strategy_macd": (BASE_DIR / "strategy" / "macd.py").resolve(),
        "strategy_signal_policy": (
            BASE_DIR / "strategy" / "signal_policy.py"
        ).resolve(),
        "strategy_market_gate": (
            BASE_DIR / "strategy" / "market_gate.py"
        ).resolve(),
        "utils_helpers": (BASE_DIR / "utils" / "helpers.py").resolve(),
    }
    expected = {
        "backtest_winrate": PREREGISTERED_BACKTEST_SHA256,
        "attribution_audit": PREREGISTERED_ATTRIBUTION_SHA256,
        "candidate_integrity": PREREGISTERED_CANDIDATE_INTEGRITY_SHA256,
        "strategy_chan": PREREGISTERED_CHAN_STRATEGY_SHA256,
        "strategy_macd": PREREGISTERED_MACD_STRATEGY_SHA256,
        "strategy_signal_policy": PREREGISTERED_SIGNAL_POLICY_SHA256,
        "strategy_market_gate": PREREGISTERED_MARKET_GATE_SHA256,
        "utils_helpers": PREREGISTERED_UTILS_HELPERS_SHA256,
    }
    return {
        name: (path, expected[name])
        for name, path in paths.items()
    }


def _dependency_contract(
    initial_snapshot: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for name, (path, expected) in _dependency_definitions().items():
        resolved = str(path.resolve())
        actual = (
            str(initial_snapshot[resolved]["sha256"])
            if initial_snapshot is not None
            else file_sha256(path)
        )
        report[name] = {
            "path": resolved,
            "expected_sha256": expected,
            "actual_sha256": actual,
            "match": actual == expected,
        }
    if not all(item["match"] for item in report.values()):
        raise RuntimeError(f"replay dependency hash mismatch: {report}")
    return report


def _assert_output_unused(output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    staging = output_dir.with_name(output_dir.name + ".tmp")
    conflicts = [path for path in (output_dir, staging) if path.exists()]
    if conflicts:
        raise RuntimeError(
            "formal output or staging directory already exists; overwrite is forbidden: "
            + ", ".join(str(path) for path in conflicts)
        )
    return staging


def _write_outputs_atomically(
    output_dir: Path,
    comparisons: dict[tuple[str, str], list[dict[str, Any]]],
    result: dict[str, Any],
) -> None:
    output_dir = output_dir.expanduser().resolve()
    staging = _assert_output_unused(output_dir)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(exist_ok=False)
    artifacts: dict[str, Any] = {}
    for split in SPLITS:
        for profile in REPLAY_PROFILES:
            name = f"canonical_replay_semantics_{split}_{profile}.jsonl"
            staged_path = staging / name
            with staged_path.open("x", encoding="utf-8", newline="\n") as handle:
                for row in comparisons[(split, profile)]:
                    handle.write(_canonical_json(row) + "\n")
            artifacts[f"{split}_{profile}"] = {
                "path": str(output_dir / name),
                "rows": len(comparisons[(split, profile)]),
                "sha256": file_sha256(staged_path),
            }
    result["artifacts"] = artifacts
    report_name = "canonical_replay_semantics_audit.json"
    report_path = staging / report_name
    result["report_path"] = str(output_dir / report_name)
    with report_path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    staging.replace(output_dir)


def run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = time.perf_counter()
    _validate_fixed_args(args)
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    _assert_output_unused(output_dir)

    config_path = Path(args.config).expanduser().resolve()
    manifest_path = input_dir / "candidate_integrity_manifest.json"
    declared_source_paths = [
        input_dir / f"candidates_{split}.jsonl" for split in SPLITS
    ]
    dependency_definitions = _dependency_definitions()
    dependency_paths = [
        path for path, _ in dependency_definitions.values()
    ]
    initial_static_snapshot = _paths_snapshot(
        [
            config_path,
            manifest_path,
            *declared_source_paths,
            *dependency_paths,
        ]
    )
    config_sha256 = initial_static_snapshot[str(config_path)]["sha256"]
    if config_sha256 != PREREGISTERED_CONFIG_SHA256:
        raise RuntimeError(
            "config hash differs from pre-registered snapshot: "
            f"expected={PREREGISTERED_CONFIG_SHA256}, actual={config_sha256}"
        )
    dependency_contract = _dependency_contract(initial_static_snapshot)
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")

    sources_by_split: dict[str, list[dict[str, Any]]] = {}
    integrity_by_split: dict[str, Any] = {}
    membership_by_split: dict[str, Any] = {}
    source_paths: list[Path] = []
    all_symbols: set[str] = set()
    for split in SPLITS:
        source_path = input_dir / f"candidates_{split}.jsonl"
        rows = load_jsonl(source_path)
        integrity_by_split[split] = _validate_integrity_manifest(
            input_dir, split, source_path, rows
        )
        membership_by_split[split] = _membership_contract(rows, split)
        sources_by_split[split] = rows
        source_paths.append(source_path)
        all_symbols.update(str(row["symbol"]).zfill(6) for row in rows)

    history_paths = [
        (HISTORY_DIR / f"{symbol}_qfq.pkl").resolve()
        for symbol in sorted(all_symbols)
    ]
    initial_history_snapshot = _paths_snapshot(history_paths)
    history_inventory, inventory_paths = _history_inventory(all_symbols)
    if [str(path.resolve()) for path in inventory_paths] != [
        str(path.resolve()) for path in history_paths
    ]:
        raise RuntimeError("history inventory paths differ from pre-snapshotted paths")
    _assert_history_inventory_matches_snapshot(
        history_inventory,
        initial_history_snapshot,
        history_dir=HISTORY_DIR,
    )
    history_inventory_by_split: dict[str, dict[str, Any]] = {}
    for split in SPLITS:
        split_symbols = {
            str(row["symbol"]).zfill(6) for row in sources_by_split[split]
        }
        split_hashes = {
            symbol: history_inventory["file_sha256_by_symbol"][symbol]
            for symbol in sorted(split_symbols)
        }
        history_inventory_by_split[split] = {
            "symbol_count": len(split_hashes),
            "file_count": len(split_hashes),
            "history_manifest_sha256": _history_manifest_sha256(split_hashes),
            "derived_from_validated_overall_inventory": True,
            "all_valid": True,
        }
    signal_day_coverage = _validate_signal_day_coverage(sources_by_split)
    protected_paths = [
        config_path,
        manifest_path,
        *source_paths,
        *dependency_paths,
        *history_paths,
    ]
    before_snapshot = _paths_snapshot(protected_paths)
    initial_full_snapshot = {
        **initial_static_snapshot,
        **initial_history_snapshot,
    }
    pre_replay_stability = _compare_snapshots(
        initial_full_snapshot,
        before_snapshot,
    )
    if not pre_replay_stability["all_unchanged"]:
        raise RuntimeError(
            "protected inputs changed during pre-replay validation: "
            + ", ".join(pre_replay_stability["changed_paths"])
        )

    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "design": {
            "read_only_semantics_drift_audit": True,
            "candidate_factor_test": False,
            "candidate_gate_implemented": False,
            "source_artifact_role": "signal_identity_only",
            "source_outcomes_used_for_research_gate": False,
            "current_replay_outcomes_authoritative": True,
            "profiles": list(REPLAY_PROFILES),
            "required_splits": list(SPLITS),
            "holdout_consumed": False,
            "portfolio_layer_implemented": False,
            "production_eligible": False,
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
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "config_file": str(config_path),
        "config_file_expected_sha256": PREREGISTERED_CONFIG_SHA256,
        "config_file_sha256_before": config_sha256,
        "execution_config_snapshot": _config_snapshot(config),
        "script_sha256": file_sha256(Path(__file__).resolve()),
        "dependency_contract": dependency_contract,
        "candidate_integrity": integrity_by_split,
        "membership_contract": membership_by_split,
        "history_inventory": {
            "overall": history_inventory,
            "by_split": history_inventory_by_split,
        },
        "signal_day_coverage": signal_day_coverage,
        "pre_replay_input_stability": {
            **pre_replay_stability,
            "history_inventory_matches_initial_snapshot": True,
        },
        "historical_source_provenance": {
            "producer_script_sha256": None,
            "producer_config_sha256": None,
            "replay_version": None,
            "history_manifest_sha256": None,
            "metadata_available": False,
            "exact_source_semantics_reconstructable": False,
            "historical_candidate_selection_outcome_independence": "unknown",
        },
        "current_replay_provenance": {
            "replay_driver": "attribution_audit._replay_split",
            "replay_driver_sha256": dependency_contract["attribution_audit"]
            ["actual_sha256"],
            "simulator_sha256": dependency_contract["backtest_winrate"]
            ["actual_sha256"],
            "config_sha256": config_sha256,
            "history_manifest_sha256": history_inventory[
                "history_manifest_sha256"
            ],
        },
        "splits": {},
        "future_replay_contract": FUTURE_REPLAY_CONTRACT,
    }
    comparisons_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    combined_by_profile: dict[str, list[dict[str, Any]]] = {
        profile: [] for profile in REPLAY_PROFILES
    }

    for split in SPLITS:
        result["splits"][split] = {
            "source": str(input_dir / f"candidates_{split}.jsonl"),
            "source_sha256": file_sha256(
                input_dir / f"candidates_{split}.jsonl"
            ),
            "source_rows": len(sources_by_split[split]),
            "profiles": {},
        }
        for profile in REPLAY_PROFILES:
            print(
                f"replay split={split} profile={profile} rows={len(sources_by_split[split])}",
                flush=True,
            )
            driver_profile = PROFILE_DRIVER_NAMES[profile]
            replay_rows, skipped, match = _replay_split(
                sources_by_split[split],
                config,
                driver_profile,
                HISTORY_DIR,
            )
            replay_integrity = _assert_replay_integrity(
                sources_by_split[split],
                replay_rows,
                skipped,
                match,
                history_inventory_by_split[split]["history_manifest_sha256"],
                profile,
            )
            comparisons = [
                _compare_candidate(
                    source,
                    replay,
                    split=split,
                    profile=profile,
                )
                for source, replay in zip(sources_by_split[split], replay_rows)
            ]
            comparisons_by_key[(split, profile)] = comparisons
            combined_by_profile[profile].extend(comparisons)
            result["splits"][split]["profiles"][profile] = {
                "replay_rows": len(replay_rows),
                "skipped": dict(skipped),
                "source_match": match,
                "driver_profile": driver_profile,
                "public_profile": profile,
                "replay_integrity": replay_integrity,
                "comparison": _aggregate_comparisons(comparisons),
            }

    profile_reports = {
        profile: _aggregate_comparisons(combined_by_profile[profile])
        for profile in REPLAY_PROFILES
    }
    result["profile_comparison"] = profile_reports
    result["semantic_attribution"] = _profile_attribution(profile_reports)
    observed_differences = _observed_difference_summary(
        profile_reports["production_risk"]
    )
    result["observed_comparable_field_differences"] = observed_differences
    result["semantic_drift_detected"] = observed_differences["detected"]

    after_snapshot = _paths_snapshot(protected_paths)
    snapshot_check = _compare_snapshots(before_snapshot, after_snapshot)
    result["protected_inputs"] = {
        **snapshot_check,
        "path_count": len(before_snapshot),
        "manifest_sha256_before": file_sha256(manifest_path),
        "config_sha256_after": file_sha256(config_path),
    }
    if not snapshot_check["all_unchanged"]:
        raise RuntimeError(
            "protected inputs changed during audit: "
            + ", ".join(snapshot_check["changed_paths"])
        )
    result["audit_contract_pass"] = bool(
        all(item["all_pass"] for item in integrity_by_split.values())
        and all(item["all_pass"] for item in membership_by_split.values())
        and snapshot_check["all_unchanged"]
        and signal_day_coverage["all_pass"]
        and all(
            result["splits"][split]["profiles"][profile]
            ["replay_integrity"]["all_pass"]
            for split in SPLITS
            for profile in REPLAY_PROFILES
        )
    )
    result["verdict"] = (
        "semantic_drift_confirmed_source_outcomes_non_authoritative"
        if result["semantic_drift_detected"]
        else "no_observed_source_comparable_field_differences"
    )
    result["production_decision"] = "unchanged_P0"
    result["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
    _write_outputs_atomically(output_dir, comparisons_by_key, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=str(CANONICAL_INPUT_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--label", default=PREREGISTERED_LABEL)
    parser.add_argument("--splits", nargs="+", default=list(SPLITS))
    parser.add_argument("--profiles", nargs="+", default=list(REPLAY_PROFILES))
    args = parser.parse_args()
    result = run(args)
    print(
        json.dumps(
            {
                "version": result["version"],
                "label": result["label"],
                "audit_contract_pass": result["audit_contract_pass"],
                "semantic_drift_detected": result["semantic_drift_detected"],
                "overall_profile_winner_selected": result["semantic_attribution"]
                ["overall_profile_winner_selected"],
                "verdict": result["verdict"],
                "production_decision": result["production_decision"],
                "output_dir": result["output_dir"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
