"""Frozen strategy-failure path and market-regime diagnostic batch audit.

The audit replays the canonical train/validation/test candidates exactly once
per split with the production-risk profile, then shares those authoritative
rows across two descriptive studies:

* fixed 1/3/5/10/20/40-bar entry and exit path diagnostics; and
* prefix-only reconstruction and forward calibration of the signal-day market
  regime.

This script implements no candidate gate, filter, exit variant, portfolio
layer, causal attribution, or production change. It is local-file-only and
fails closed on any frozen-input, replay, path, or publication mismatch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
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
import entry_context_batch_audit as batch_base
from attribution_audit import (
    EXIT_REASON_SEMANTICS,
    FIELDS,
    _config_snapshot,
    exit_reason_category,
)
from attribution_audit import _replay_split as _production_replay_split
from backtest_winrate import (
    HISTORY_DIR,
    _execution_values,
    _risk_trigger,
    build_market_gate,
    next_bar_index,
    prepare_closed_bars,
)
from candidate_integrity import (
    candidate_id,
    candidate_ids_sha256,
    file_sha256,
    load_jsonl,
)
from strategy.market_gate import calculate_strict_regime
from utils.helpers import load_config

VERSION = "strategy_failure_path_audit.v1"
SPLITS = ("train", "val", "test")
PREREGISTERED_LABEL = "strategy-failure-path-audit"
CANONICAL_INPUT_DIR = Path(r"D:\tmp\candidates_fullpool_canonical")
FORMAL_OUTPUT_DIR = Path(r"D:\tmp\strategy_failure_path_audit_final")
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
INDEX_PATH = BASE_DIR / "cache" / "index_000001_sh.pkl"

PREREGISTERED_CONFIG_SHA256 = (
    "4e0084eb945aa2533dca25f9e0f68b4e27e9f2cbc52c11b7898c082d466000f1"
)
PREREGISTERED_INDEX_SHA256 = (
    "e59364d0cfe2d848daec0d18d749f9b3fa20fe04ef520b8655ee3793020c6cc8"
)
PREREGISTERED_INTEGRITY_MANIFEST_SHA256 = (
    "4e12f3d53851e0cc7f49a3c67f4fcdf84d3b2584630e166c0a0cf9a997e01753"
)
PREREGISTERED_HISTORY_MANIFEST_SHA256 = (
    "ca99ebb0d9374a3159abe5ae7fe8f72a757c3c810355729390a0e6e006f3b968"
)
PREREGISTERED_CANONICAL_SHA256 = {
    "train": "28a2defd6f7f09904295cb270cc311bf989add84b4cd0c8dc947d4e59a4fb40f",
    "val": "921a2ddc6916030a270aa136c0ce05eabdcddfb9ab289ec07c4ef5e8d2ba2bd0",
    "test": "d90836a21610f77c6bb6afca3169d3eedb79c228026415f2e8da956fc8def174",
}

DEPENDENCY_PATHS = {
    "entry_context_batch_audit": BASE_DIR / "entry_context_batch_audit.py",
    **batch_base.DEPENDENCY_PATHS,
}
DEPENDENCY_EXPECTED_SHA256 = {
    "entry_context_batch_audit": (
        "3dbebb3763f5b2a1b9e1dccbbf13128f452a37a209a2a6136e0a5c956f55892c"
    ),
    **batch_base.DEPENDENCY_EXPECTED_SHA256,
}

EXPECTED_CONFIG_SNAPSHOT = {
    "stop_loss_pct": 0.08,
    "stop_profit_pct": 0.30,
    "commission_pct": 0.0003,
    "minimum_commission": 5.0,
    "stamp_tax_pct": 0.001,
    "slippage_pct": 0.0005,
    "lot_size": 100,
    "t_plus_one": True,
    "price_limit_model": "conservative",
    "intrabar_conflict": "stop_first",
    "max_holding_bars": 40,
    "timeout_exit_mode": "fixed",
    "adjustment": "qfq",
}
EXPECTED_MARKET_GATE_CONFIG = {
    "market_gate_enabled": True,
    "market_gate_fail_open": False,
    "market_index_code": "000001.SH",
    "trend_gate_enabled": True,
    "trend_fast_ma": 20,
    "trend_slow_ma": 60,
    "fast_gate_mode": "ma10_latch",
}

HORIZONS = (1, 3, 5, 10, 20, 40)
REGIMES = ("bull", "range", "bear")
SIGNAL_TYPES = (
    "macd_golden_cross_pullback_confirmed_above",
    "macd_golden_cross_pullback_confirmed_near",
    "buy_1",
)
EXIT_CATEGORIES = tuple(sorted(set(EXIT_REASON_SEMANTICS.values()) | {"unknown"}))
NEXT_SESSION_FIRST_FILL_REASONS = {
    "sell_1",
    "sell_2",
    "sell_3",
    "zero_axis_death_cross",
    "timeout_ma_break",
}
SMALL_CELL_N = 30
STUDY_REGISTRY = {
    "entry_exit_path_diagnostic": {
        "role": "safety_diagnostic_only",
        "candidate_gate_implemented": False,
        "causal_attribution_performed": False,
        "production_decision_authority": False,
    },
    "market_regime_calibration_diagnostic": {
        "role": "safety_diagnostic_only",
        "candidate_gate_implemented": False,
        "causal_attribution_performed": False,
        "production_decision_authority": False,
    },
}

SOURCE_LEGACY_OUTCOME_FIELDS = batch_base.SOURCE_LEGACY_OUTCOME_FIELDS
REPLAY_REQUIRED_FIELDS = (
    *FIELDS,
    "entry_day",
    "entry_price",
    "exit_trigger_day",
    "exit_day",
    "exit_price",
    "exit_reason",
    "exit_session",
    "holding_days",
    "holding_bars",
    "price_limit_deferred_bars",
)
PATH_METRIC_FIELDS = (
    *tuple(f"close_return_{horizon}b_pct" for horizon in HORIZONS),
    *tuple(f"mfe_{horizon}b_pct" for horizon in HORIZONS),
    *tuple(f"mae_{horizon}b_pct" for horizon in HORIZONS),
    *tuple(f"mfe_{horizon}b_offset" for horizon in HORIZONS),
    *tuple(f"mae_{horizon}b_offset" for horizon in HORIZONS),
    "first_stop_touch_offset",
    "first_take_touch_offset",
    "raw_first_risk_trigger_reason",
    "raw_first_risk_trigger_offset",
    "raw_first_risk_trigger_session",
    "raw_risk_trigger_on_entry_bar",
    "exit_trigger_offset",
    "exit_fill_offset",
    "exit_delay_bars",
    "exit_after_40b",
    "held_excursion_observability",
    "held_mfe_lower_bound_pct",
    "held_mfe_upper_bound_pct",
    "held_mae_best_bound_pct",
    "held_mae_worst_bound_pct",
    "gross_exit_price_return_pct",
    "gross_mfe_giveback_lower_bound_pct",
    "gross_mfe_giveback_upper_bound_pct",
    "net_mfe_giveback_lower_bound_pct",
    "net_mfe_giveback_upper_bound_pct",
    "exit_fill_before_mfe40",
    "exit_fill_mfe40_relation",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _rows_manifest(rows: Sequence[dict[str, Any]]) -> str:
    return _sha256_json(list(rows))


def _run_immutable_evaluator(
    value: Any,
    evaluator: Callable[[Any], Any],
    label: str,
) -> Any:
    isolated = deepcopy(value)
    expected = _sha256_json(isolated)
    result = evaluator(isolated)
    if _sha256_json(isolated) != expected:
        raise RuntimeError(f"{label} evaluator input mutated")
    return result


def _snapshot_named_paths(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    return batch_base._snapshot_named_paths(paths)


def _snapshot_manifest(snapshot: dict[str, dict[str, Any]]) -> str:
    return batch_base._snapshot_manifest(snapshot)


def _snapshot_changes(
    initial: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    return batch_base._snapshot_changes(initial)


def _static_protected_paths(input_dir: Path, config_path: Path) -> dict[str, Path]:
    return {
        "script": Path(__file__).resolve(),
        **DEPENDENCY_PATHS,
        "config": config_path,
        "index_history": INDEX_PATH,
        "candidate_integrity_manifest": input_dir
        / "candidate_integrity_manifest.json",
        **{
            f"canonical_{split}": input_dir / f"candidates_{split}.jsonl"
            for split in SPLITS
        },
    }


def _expected_static_hashes(expected_script_sha256: str) -> dict[str, str]:
    return {
        "script": expected_script_sha256,
        **DEPENDENCY_EXPECTED_SHA256,
        "config": PREREGISTERED_CONFIG_SHA256,
        "index_history": PREREGISTERED_INDEX_SHA256,
        "candidate_integrity_manifest": PREREGISTERED_INTEGRITY_MANIFEST_SHA256,
        **{
            f"canonical_{split}": digest
            for split, digest in PREREGISTERED_CANONICAL_SHA256.items()
        },
    }


def _validate_expected_snapshot(
    snapshot: dict[str, dict[str, Any]], expected: dict[str, str]
) -> dict[str, dict[str, Any]]:
    return batch_base._validate_expected_snapshot(snapshot, expected)


def _normalize_regime(value: Any) -> str:
    result = str(value or "").strip().lower()
    if result not in REGIMES:
        raise RuntimeError(f"unexpected canonical regime: {value!r}")
    return result


def _normalize_signal_type(value: Any) -> str:
    result = str(value or "").strip()
    if result not in SIGNAL_TYPES:
        raise RuntimeError(f"unexpected canonical signal type: {value!r}")
    return result


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise TypeError(f"{field} must be numeric: {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise RuntimeError(f"{field} must be finite: {value!r}")
    if positive and result <= 0:
        raise RuntimeError(f"{field} must be positive: {value!r}")
    return result


def _validate_preregistered_args(args: argparse.Namespace) -> None:
    if tuple(getattr(args, "splits", ())) != SPLITS:
        raise RuntimeError(f"all required splits must remain in order {SPLITS}")
    if getattr(args, "label", None) != PREREGISTERED_LABEL:
        raise RuntimeError("label differs from the pre-registered label")
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
    exact_paths = {
        "input_dir": CANONICAL_INPUT_DIR.resolve(),
        "output_dir": FORMAL_OUTPUT_DIR.resolve(),
        "config": CONFIG_PATH.resolve(),
        "index_data": INDEX_PATH.resolve(),
    }
    for field, expected in exact_paths.items():
        actual = Path(getattr(args, field)).expanduser().resolve()
        if actual != expected:
            raise RuntimeError(f"{field} must remain {expected}")


def _assert_output_directory_unused(output_dir: Path) -> Path:
    staging = output_dir.with_name(output_dir.name + ".tmp")
    conflicts = [path for path in (output_dir, staging) if path.exists()]
    if conflicts:
        raise RuntimeError(
            "formal output directory already exists; overwrite is forbidden: "
            + ", ".join(str(path) for path in conflicts)
        )
    return staging


def _validate_config(config: dict[str, Any]) -> dict[str, Any]:
    snapshot = _config_snapshot(config)
    if snapshot != EXPECTED_CONFIG_SNAPSHOT:
        raise RuntimeError(
            "execution config differs from preregistration: "
            f"expected={EXPECTED_CONFIG_SNAPSHOT}, actual={snapshot}"
        )
    entry_filters = config.get("entry_filters", {})
    actual_gate = {
        key: entry_filters.get(key) for key in EXPECTED_MARKET_GATE_CONFIG
    }
    if actual_gate != EXPECTED_MARKET_GATE_CONFIG:
        raise RuntimeError(
            "market gate config differs from preregistration: "
            f"expected={EXPECTED_MARKET_GATE_CONFIG}, actual={actual_gate}"
        )
    long_ma = int(
        config.get("signal_strategy", {}).get("macd", {}).get("long_ma_period", 0)
    )
    slope_bars = int(config.get("backtest", {}).get("market_gate_slope_bars", 0))
    if long_ma != 250 or slope_bars != 5:
        raise RuntimeError(
            "market long-MA contract differs from preregistration: "
            f"long_ma={long_ma}, slope_bars={slope_bars}"
        )
    return snapshot


def _load_closed_history(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise RuntimeError(f"required local history is missing: {path}")
    closed = prepare_closed_bars(pd.read_pickle(path))
    required = ("datetime", "open", "high", "low", "close")
    missing = [field for field in required if field not in closed.columns]
    if missing:
        raise RuntimeError(f"history columns are missing at {path}: {missing}")
    dates = pd.to_datetime(closed["datetime"], errors="coerce")
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise RuntimeError(f"history dates are invalid at {path}")
    for field in ("open", "high", "low", "close"):
        values = pd.to_numeric(closed[field], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy(dtype=float)).all():
            raise RuntimeError(f"history {field} is non-finite at {path}")
        if (values <= 0).any():
            raise RuntimeError(f"history {field} is not strictly positive at {path}")
    if ((closed["high"] < closed["low"]) | (closed["high"] < closed["open"]) | (closed["high"] < closed["close"]) | (closed["low"] > closed["open"]) | (closed["low"] > closed["close"])).any():
        raise RuntimeError(f"history OHLC bounds are inconsistent at {path}")
    return closed.reset_index(drop=True)


def _load_index_history() -> pd.DataFrame:
    closed = _load_closed_history(INDEX_PATH)
    if len(closed) < 296:
        raise RuntimeError("index history is too short for MA250+slope and 40-bar suffix")
    return closed


def _date_list(closed: pd.DataFrame) -> list[date]:
    return [pd.Timestamp(value).date() for value in closed["datetime"]]


def _index_regime_contexts(
    sources_by_split: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
    index_closed: pd.DataFrame,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    dates = _date_list(index_closed)
    index_by_day = {value.isoformat(): index for index, value in enumerate(dates)}
    requested: dict[str, str] = {}
    for split in SPLITS:
        for source in sources_by_split[split]:
            identity = batch_base._identity_projection(source)
            day = identity["signal_day"]
            regime = _normalize_regime(source.get("regime"))
            previous = requested.setdefault(day, regime)
            if previous != regime:
                raise RuntimeError(
                    f"canonical regimes conflict on signal day {day}: {previous} vs {regime}"
                )

    contexts: dict[str, dict[str, Any]] = {}
    for day, canonical_regime in sorted(requested.items()):
        signal_idx = index_by_day.get(day)
        if signal_idx is None:
            raise RuntimeError(f"signal day is absent from frozen index calendar: {day}")
        if signal_idx < 254:
            raise RuntimeError(f"insufficient prefix for signal-day market gate: {day}")
        if signal_idx + 41 >= len(index_closed):
            raise RuntimeError(f"incomplete index 40-bar suffix after signal day: {day}")
        prefix = index_closed.iloc[: signal_idx + 1].copy(deep=True)
        rebuilt = build_market_gate(prefix, config).get(day)
        if not rebuilt or rebuilt.get("regime") not in REGIMES:
            raise RuntimeError(f"unable to rebuild signal-day regime: {day}")
        rebuilt_regime = str(rebuilt["regime"])
        if rebuilt_regime != canonical_regime:
            raise RuntimeError(
                "canonical regime differs from prefix-only reconstruction: "
                f"day={day}, canonical={canonical_regime}, rebuilt={rebuilt_regime}"
            )
        if not bool(rebuilt.get("allows_entries")):
            raise RuntimeError(
                f"canonical candidate day is blocked by rebuilt market gate: {day}"
            )
        contexts[day] = {
            "signal_index": signal_idx,
            "canonical_regime": canonical_regime,
            "rebuilt_regime": rebuilt_regime,
            "allows_entries": bool(rebuilt.get("allows_entries")),
            "blocked_by": tuple(str(value) for value in rebuilt.get("blocked_by", [])),
        }
    return contexts, {
        "signal_days": len(contexts),
        "prefix_only_reconstruction": True,
        "all_regime_labels_match": True,
        "minimum_signal_index": min(
            (item["signal_index"] for item in contexts.values()), default=None
        ),
        "maximum_signal_index": max(
            (item["signal_index"] for item in contexts.values()), default=None
        ),
    }


def _path_feasibility(
    sources_by_split: dict[str, list[dict[str, Any]]]
) -> dict[str, dict[str, Any]]:
    date_cache: dict[str, list[date]] = {}
    result: dict[str, dict[str, Any]] = {}
    for split in SPLITS:
        rows = sources_by_split[split]
        complete = 0
        entry_days: list[str] = []
        for source in rows:
            identity = batch_base._identity_projection(source)
            symbol = identity["symbol"]
            if symbol not in date_cache:
                closed = _load_closed_history(HISTORY_DIR / f"{symbol}_qfq.pkl")
                date_cache[symbol] = _date_list(closed)
            dates = date_cache[symbol]
            entry_idx = next(
                (index for index, value in enumerate(dates) if value > date.fromisoformat(identity["signal_day"])),
                None,
            )
            if entry_idx is None or entry_idx + 40 >= len(dates):
                raise RuntimeError(
                    f"incomplete stock 40-bar path: candidate_id={candidate_id(source)}"
                )
            complete += 1
            entry_days.append(dates[entry_idx].isoformat())
        result[split] = {
            "candidates": len(rows),
            "symbols": len({batch_base._normalize_symbol(row["symbol"]) for row in rows}),
            "signal_days": len({batch_base._normalize_signal_day(row["signal_day"]) for row in rows}),
            "complete_40bar_paths": complete,
            "coverage": complete / len(rows) if rows else 0.0,
            "first_entry_day": min(entry_days) if entry_days else None,
            "last_entry_day": max(entry_days) if entry_days else None,
        }
    return result


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
    config_snapshot = _validate_config(config)

    sources_by_split: dict[str, list[dict[str, Any]]] = {}
    integrity_by_split: dict[str, dict[str, Any]] = {}
    symbols: set[str] = set()
    for split in SPLITS:
        path = input_dir / f"candidates_{split}.jsonl"
        sources = load_jsonl(path)
        batch_base._validate_symbol_day_uniqueness(sources)
        for source in sources:
            _normalize_regime(source.get("regime"))
            _normalize_signal_type(source.get("signal_type"))
        sources_by_split[split] = sources
        integrity_by_split[split] = batch_base._validate_integrity_manifest(
            input_dir, split, path, sources
        )
        symbols.update(batch_base._normalize_symbol(row["symbol"]) for row in sources)

    history_initial = _snapshot_named_paths(batch_base._history_paths(symbols))
    history_manifest = _snapshot_manifest(history_initial)
    if history_manifest != PREREGISTERED_HISTORY_MANIFEST_SHA256:
        raise RuntimeError(
            "history manifest differs from preregistration: "
            f"expected={PREREGISTERED_HISTORY_MANIFEST_SHA256}, actual={history_manifest}"
        )
    history_inventory, _ = replay_contract._history_inventory(symbols)
    replay_contract._assert_history_inventory_matches_snapshot(
        history_inventory,
        {
            item["path"]: {"sha256": item["sha256"], "size": item["size_bytes"]}
            for item in history_initial.values()
        },
    )
    signal_day_coverage = replay_contract._validate_signal_day_coverage(
        sources_by_split
    )
    path_feasibility = _path_feasibility(sources_by_split)
    index_closed = _load_index_history()
    regime_contexts, regime_feasibility = _index_regime_contexts(
        sources_by_split, config, index_closed
    )
    return {
        "input_dir": input_dir,
        "output_dir": output_dir,
        "config_path": config_path,
        "staging_dir": staging_dir,
        "static_initial": static_initial,
        "history_initial": history_initial,
        "dependency_contract": dependency_contract,
        "config": config,
        "config_snapshot": config_snapshot,
        "sources_by_split": sources_by_split,
        "integrity_by_split": integrity_by_split,
        "history_inventory": history_inventory,
        "history_manifest": history_manifest,
        "signal_day_coverage": signal_day_coverage,
        "path_feasibility": path_feasibility,
        "index_closed": index_closed,
        "regime_contexts": regime_contexts,
        "regime_feasibility": regime_feasibility,
    }


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    prepared = _prepare(args)
    return {
        "version": VERSION,
        "technical_preflight_pass": True,
        "static_contract_items": len(prepared["dependency_contract"]),
        "canonical_rows": {
            split: len(prepared["sources_by_split"][split]) for split in SPLITS
        },
        "history_symbol_count": prepared["history_inventory"]["symbol_count"],
        "history_file_count": prepared["history_inventory"]["file_count"],
        "history_manifest_sha256": prepared["history_manifest"],
        "index_file_sha256": PREREGISTERED_INDEX_SHA256,
        "path_feasibility": prepared["path_feasibility"],
        "regime_feasibility": prepared["regime_feasibility"],
        "history_ohlc_integrity_validated": True,
        "future_stock_outcomes_analyzed": False,
        "replay_performed": False,
        "output_generated": False,
        "formal_run_allowed": True,
    }


def _pct(value: float, reference: float) -> float:
    return round((float(value) / float(reference) - 1.0) * 100.0, 6)


def _extreme_with_offset(
    values: pd.Series, *, maximum: bool
) -> tuple[float, int]:
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    if numeric.size == 0 or not np.isfinite(numeric).all():
        raise RuntimeError("extreme window is empty or non-finite")
    target = float(np.max(numeric) if maximum else np.min(numeric))
    matches = np.flatnonzero(numeric == target)
    if not matches.size:
        raise RuntimeError("unable to locate extreme offset")
    return target, int(matches[0])


def _first_threshold_touch(
    closed: pd.DataFrame,
    entry_idx: int,
    threshold: float,
    *,
    stop: bool,
) -> int | None:
    for offset in range(41):
        bar = closed.iloc[entry_idx + offset]
        value = _number(bar["low" if stop else "high"], "threshold bar")
        if (stop and value <= threshold) or (not stop and value >= threshold):
            return offset
    return None


def _first_raw_risk_trigger(
    closed: pd.DataFrame,
    entry_idx: int,
    execution: dict[str, Any],
    entry_price: float,
) -> tuple[str | None, int | None, str | None]:
    stop_pct = execution.get("stop_loss_pct")
    take_pct = execution.get("take_profit_pct")
    stop_price = entry_price * (1.0 - float(stop_pct)) if stop_pct else None
    take_price = entry_price * (1.0 + float(take_pct)) if take_pct else None
    for offset in range(41):
        trigger = _risk_trigger(
            closed.iloc[entry_idx + offset],
            stop_price,
            take_price,
            str(execution["intrabar_conflict"]),
        )
        if trigger is not None:
            reason, _price, session = trigger
            return str(reason), offset, str(session)
    return None, None, None


def _held_excursion_bounds(
    closed: pd.DataFrame,
    entry_idx: int,
    exit_idx: int,
    entry_price: float,
    exit_price: float,
    exit_session: str,
) -> dict[str, Any]:
    prior = closed.iloc[entry_idx:exit_idx]
    full = closed.iloc[entry_idx : exit_idx + 1]

    prior_high = (
        _number(pd.to_numeric(prior["high"]).max(), "prior high", positive=True)
        if not prior.empty
        else exit_price
    )
    prior_low = (
        _number(pd.to_numeric(prior["low"]).min(), "prior low", positive=True)
        if not prior.empty
        else exit_price
    )
    full_high = _number(pd.to_numeric(full["high"]).max(), "full high", positive=True)
    full_low = _number(pd.to_numeric(full["low"]).min(), "full low", positive=True)

    if exit_session == "open":
        mfe_low = mfe_high = max(prior_high, exit_price)
        mae_best = mae_worst = min(prior_low, exit_price)
        observability = "exact_open_fill"
    elif exit_session == "close":
        mfe_low = mfe_high = full_high
        mae_best = mae_worst = full_low
        observability = "exact_close_fill"
    elif exit_session == "intraday":
        mfe_low = max(prior_high, exit_price)
        mfe_high = full_high
        mae_best = min(prior_low, exit_price)
        mae_worst = full_low
        observability = "bounded_intraday_fill"
    else:
        raise RuntimeError(f"unsupported replay exit_session: {exit_session!r}")
    return {
        "held_excursion_observability": observability,
        "held_mfe_lower_bound_pct": _pct(mfe_low, entry_price),
        "held_mfe_upper_bound_pct": _pct(mfe_high, entry_price),
        "held_mae_best_bound_pct": _pct(mae_best, entry_price),
        "held_mae_worst_bound_pct": _pct(mae_worst, entry_price),
    }


def _validate_replay_lifecycle(
    replay: dict[str, Any],
    closed: pd.DataFrame,
    dates: list[date],
    entry_idx: int,
) -> tuple[int, int]:
    identifier = candidate_id(replay)
    for field in REPLAY_REQUIRED_FIELDS:
        if field not in replay or replay[field] is None:
            raise RuntimeError(
                f"authoritative replay field is missing: candidate_id={identifier}, field={field}"
            )
    index_by_day = {value.isoformat(): index for index, value in enumerate(dates)}
    trigger_idx = index_by_day.get(str(replay["exit_trigger_day"]))
    exit_idx = index_by_day.get(str(replay["exit_day"]))
    if trigger_idx is None or exit_idx is None:
        raise RuntimeError(f"replay lifecycle day is absent from history: {identifier}")
    if not (entry_idx <= trigger_idx <= exit_idx):
        raise RuntimeError(
            f"replay lifecycle offsets are inconsistent: {identifier}"
        )
    if exit_idx <= entry_idx:
        raise RuntimeError(f"T+1 replay filled on or before entry bar: {identifier}")
    holding_bars = _number(replay["holding_bars"], "holding_bars")
    if not holding_bars.is_integer() or int(holding_bars) != exit_idx - entry_idx:
        raise RuntimeError(f"holding_bars differs from fill offset: {identifier}")
    holding_days = _number(replay["holding_days"], "holding_days")
    expected_holding_days = max((dates[exit_idx] - dates[entry_idx]).days, 0)
    if (
        not holding_days.is_integer()
        or int(holding_days) != expected_holding_days
    ):
        raise RuntimeError(f"holding_days differs from calendar interval: {identifier}")
    deferred = _number(
        replay["price_limit_deferred_bars"], "price_limit_deferred_bars"
    )
    exit_delay = exit_idx - trigger_idx
    if not deferred.is_integer() or deferred < 0:
        raise RuntimeError(f"price-limit deferral is inconsistent: {identifier}")
    reason = str(replay["exit_reason"])
    if exit_reason_category(reason) == "unknown":
        raise RuntimeError(f"unregistered replay exit reason: {identifier}|{reason}")
    first_fill_next_session = reason in NEXT_SESSION_FIRST_FILL_REASONS or (
        reason in {"stop_loss", "take_profit"} and trigger_idx == entry_idx
    )
    expected_deferred = exit_delay - 1 if first_fill_next_session else exit_delay
    if expected_deferred < 0 or int(deferred) != expected_deferred:
        raise RuntimeError(
            "price-limit deferral differs from simulator event timing: "
            f"candidate_id={identifier}, reason={reason}, delay={exit_delay}, "
            f"expected={expected_deferred}, actual={int(deferred)}"
        )
    session = str(replay["exit_session"])
    if session not in {"open", "intraday", "close"}:
        raise RuntimeError(f"unregistered replay exit session: {identifier}|{session}")
    entry_price = _number(replay["entry_price"], "entry_price", positive=True)
    expected_entry = _number(closed.iloc[entry_idx]["open"], "entry open", positive=True)
    if abs(entry_price - expected_entry) > 0.0001:
        raise RuntimeError(f"replay entry price differs from T+1 open: {identifier}")
    exit_price = _number(replay["exit_price"], "exit_price", positive=True)
    exit_low = _number(closed.iloc[exit_idx]["low"], "exit low", positive=True)
    exit_high = _number(closed.iloc[exit_idx]["high"], "exit high", positive=True)
    if exit_price < exit_low - 0.0001 or exit_price > exit_high + 0.0001:
        raise RuntimeError(f"replay exit price is outside fill-day OHLC: {identifier}")
    if session == "open":
        exit_open = _number(
            closed.iloc[exit_idx]["open"], "exit open", positive=True
        )
        if abs(exit_price - exit_open) > 0.0001:
            raise RuntimeError(f"open-session exit price differs from bar open: {identifier}")
    if session == "close":
        exit_close = _number(
            closed.iloc[exit_idx]["close"], "exit close", positive=True
        )
        if abs(exit_price - exit_close) > 0.0001:
            raise RuntimeError(f"close-session exit price differs from bar close: {identifier}")
    return trigger_idx, exit_idx


def _validate_replay_passthrough(
    sources: Sequence[dict[str, Any]], replay_rows: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    if len(sources) != len(replay_rows):
        raise RuntimeError("replay passthrough row count differs")
    mismatches: list[str] = []
    projection: list[dict[str, str]] = []
    for source, replay in zip(sources, replay_rows, strict=True):
        identifier = candidate_id(source)
        source_regime = _normalize_regime(source.get("regime"))
        replay_regime = _normalize_regime(replay.get("regime"))
        if identifier != candidate_id(replay) or source_regime != replay_regime:
            mismatches.append(identifier)
        projection.append(
            {
                "candidate_id": identifier,
                "canonical_regime": source_regime,
                "replay_regime": replay_regime,
            }
        )
    if mismatches:
        raise RuntimeError(
            f"canonical regime passthrough differs in replay: {mismatches[:10]}"
        )
    return {
        "all_pass": True,
        "rows": len(projection),
        "manifest_sha256": _sha256_json(projection),
    }


def _fixed_path_for_replay(
    replay: dict[str, Any],
    closed: pd.DataFrame,
    config: dict[str, Any],
) -> dict[str, Any]:
    dates = _date_list(closed)
    entry_idx = next_bar_index(
        closed, dates, date.fromisoformat(str(replay["signal_day"]))
    )
    if entry_idx is None or entry_idx + 40 >= len(closed):
        raise RuntimeError(f"incomplete fixed path: {candidate_id(replay)}")
    if dates[entry_idx].isoformat() != str(replay["entry_day"]):
        raise RuntimeError(f"replay entry day differs from T+1 path: {candidate_id(replay)}")
    trigger_idx, exit_idx = _validate_replay_lifecycle(
        replay, closed, dates, entry_idx
    )
    entry_price = _number(replay["entry_price"], "entry_price", positive=True)
    exit_price = _number(replay["exit_price"], "exit_price", positive=True)
    path: dict[str, Any] = {}
    for horizon in HORIZONS:
        close_value = _number(
            closed.iloc[entry_idx + horizon]["close"],
            f"close_return_{horizon}b",
            positive=True,
        )
        window = closed.iloc[entry_idx : entry_idx + horizon + 1]
        high, high_offset = _extreme_with_offset(window["high"], maximum=True)
        low, low_offset = _extreme_with_offset(window["low"], maximum=False)
        path[f"close_return_{horizon}b_pct"] = _pct(close_value, entry_price)
        path[f"mfe_{horizon}b_pct"] = _pct(high, entry_price)
        path[f"mae_{horizon}b_pct"] = _pct(low, entry_price)
        path[f"mfe_{horizon}b_offset"] = high_offset
        path[f"mae_{horizon}b_offset"] = low_offset

    execution = _execution_values(config.get("backtest", {}) | {
        "stop_loss_pct": config.get("risk", {}).get("stop_loss_pct"),
        "take_profit_pct": config.get("risk", {}).get("stop_profit_pct"),
    })
    stop_level = entry_price * (1.0 - float(execution["stop_loss_pct"]))
    take_level = entry_price * (1.0 + float(execution["take_profit_pct"]))
    first_reason, first_offset, first_session = _first_raw_risk_trigger(
        closed, entry_idx, execution, entry_price
    )
    trigger_offset = trigger_idx - entry_idx
    fill_offset = exit_idx - entry_idx
    exit_delay = exit_idx - trigger_idx
    if first_offset == 0 and (
        str(replay["exit_reason"]) != first_reason
        or trigger_offset != 0
        or fill_offset < 1
    ):
        raise RuntimeError(
            "entry-bar risk trigger contradicts authoritative T+1 replay: "
            f"candidate_id={candidate_id(replay)}, raw_reason={first_reason}, "
            f"replay_reason={replay['exit_reason']}, trigger_offset={trigger_offset}, "
            f"fill_offset={fill_offset}"
        )
    mfe40_offset = int(path["mfe_40b_offset"])
    exit_fill_before_mfe40: bool | None
    if fill_offset < mfe40_offset:
        exit_fill_before_mfe40 = True
        fill_mfe_relation = "fill_strictly_before_mfe40_bar"
    elif fill_offset > mfe40_offset:
        exit_fill_before_mfe40 = False
        fill_mfe_relation = "mfe40_bar_strictly_before_fill"
    else:
        exit_fill_before_mfe40 = None
        fill_mfe_relation = "same_bar_order_unobservable"
    path.update(
        {
            "first_stop_touch_offset": _first_threshold_touch(
                closed, entry_idx, stop_level, stop=True
            ),
            "first_take_touch_offset": _first_threshold_touch(
                closed, entry_idx, take_level, stop=False
            ),
            "raw_first_risk_trigger_reason": first_reason,
            "raw_first_risk_trigger_offset": first_offset,
            "raw_first_risk_trigger_session": first_session,
            "raw_risk_trigger_on_entry_bar": first_offset == 0,
            "exit_trigger_offset": trigger_offset,
            "exit_fill_offset": fill_offset,
            "exit_delay_bars": exit_delay,
            "exit_after_40b": fill_offset > 40,
            "exit_fill_before_mfe40": exit_fill_before_mfe40,
            "exit_fill_mfe40_relation": fill_mfe_relation,
        }
    )
    held = _held_excursion_bounds(
        closed,
        entry_idx,
        exit_idx,
        entry_price,
        exit_price,
        str(replay["exit_session"]),
    )
    gross_return = _pct(exit_price, entry_price)
    net_return = _number(replay["trade_pnl_pct"], "trade_pnl_pct")
    path.update(held)
    path.update(
        {
            "gross_exit_price_return_pct": gross_return,
            "gross_mfe_giveback_lower_bound_pct": round(
                held["held_mfe_lower_bound_pct"] - gross_return, 6
            ),
            "gross_mfe_giveback_upper_bound_pct": round(
                held["held_mfe_upper_bound_pct"] - gross_return, 6
            ),
            "net_mfe_giveback_lower_bound_pct": round(
                held["held_mfe_lower_bound_pct"] - net_return, 6
            ),
            "net_mfe_giveback_upper_bound_pct": round(
                held["held_mfe_upper_bound_pct"] - net_return, 6
            ),
        }
    )
    return path


def _replay_projection(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "candidate_id",
        "symbol",
        "signal_day",
        "signal_type",
        "regime",
        "entry_day",
        "entry_price",
        "exit_trigger_day",
        "exit_day",
        "exit_price",
        "exit_reason",
        "exit_session",
        "holding_days",
        "holding_bars",
        "price_limit_deferred_bars",
        *FIELDS,
    )
    return {field: deepcopy(row.get(field)) for field in fields}


def _enrich_replay_paths(
    replay_by_split: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    history_cache: dict[str, pd.DataFrame] = {}
    result: dict[str, list[dict[str, Any]]] = {}
    for split in SPLITS:
        enriched: list[dict[str, Any]] = []
        for replay in replay_by_split[split]:
            symbol = batch_base._normalize_symbol(replay["symbol"])
            if symbol not in history_cache:
                history_cache[symbol] = _load_closed_history(
                    HISTORY_DIR / f"{symbol}_qfq.pkl"
                )
            base_row = _replay_projection(replay)
            base_row["candidate_id"] = candidate_id(replay)
            base_row["split"] = split
            base_row["normalized_regime"] = _normalize_regime(replay.get("regime"))
            base_row["normalized_signal_type"] = _normalize_signal_type(
                replay.get("signal_type")
            )
            base_row["exit_category"] = exit_reason_category(
                replay.get("exit_reason")
            )
            base_row.update(
                _fixed_path_for_replay(replay, history_cache[symbol], config)
            )
            enriched.append(base_row)
        result[split] = enriched
    return result


def _mean(values: Iterable[Any]) -> float | None:
    clean: list[float] = []
    for value in values:
        if value is None:
            continue
        clean.append(_number(value, "mean value"))
    return round(sum(clean) / len(clean), 6) if clean else None


def _numeric_stats(values: Iterable[Any]) -> dict[str, Any]:
    clean = np.asarray(
        [_number(value, "summary value") for value in values if value is not None],
        dtype=float,
    )
    if clean.size == 0:
        return {"n": 0}
    return {
        "n": int(clean.size),
        "mean": round(float(clean.mean()), 6),
        "median": round(float(np.median(clean)), 6),
        "p10": round(float(np.quantile(clean, 0.10)), 6),
        "p90": round(float(np.quantile(clean, 0.90)), 6),
        "min": round(float(clean.min()), 6),
        "max": round(float(clean.max()), 6),
        "positive_pct": round(float((clean > 0).mean() * 100.0), 4),
    }


PATH_SUMMARY_METRICS = (
    "future_5d",
    "future_20d",
    "future_40d",
    "trade_pnl_pct",
    "post_exit_5d",
    "post_exit_20d",
    "holding_bars",
    *tuple(f"close_return_{horizon}b_pct" for horizon in HORIZONS),
    *tuple(f"mfe_{horizon}b_pct" for horizon in HORIZONS),
    *tuple(f"mae_{horizon}b_pct" for horizon in HORIZONS),
    "held_mfe_lower_bound_pct",
    "held_mfe_upper_bound_pct",
    "held_mae_best_bound_pct",
    "held_mae_worst_bound_pct",
    "gross_exit_price_return_pct",
    "gross_mfe_giveback_lower_bound_pct",
    "gross_mfe_giveback_upper_bound_pct",
    "net_mfe_giveback_lower_bound_pct",
    "net_mfe_giveback_upper_bound_pct",
    "exit_trigger_offset",
    "exit_fill_offset",
    "exit_delay_bars",
)


def _rate(rows: Sequence[dict[str, Any]], predicate: Any) -> float | None:
    if not rows:
        return None
    return round(sum(bool(predicate(row)) for row in rows) / len(rows) * 100.0, 4)


def _path_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    source = list(rows)
    return {
        "n": len(source),
        "symbols": len({str(row["symbol"]) for row in source}),
        "signal_days": len({str(row["signal_day"]) for row in source}),
        "sample_status": (
            "sufficient_descriptive" if len(source) >= SMALL_CELL_N else "small_sample_descriptive"
        ),
        "metrics": {
            field: _numeric_stats(row.get(field) for row in source)
            for field in PATH_SUMMARY_METRICS
        },
        "rates_pct": {
            "raw_stop_before_take_all_candidates": _rate(
                source,
                lambda row: row.get("raw_first_risk_trigger_reason") == "stop_loss",
            ),
            "raw_take_before_stop_all_candidates": _rate(
                source,
                lambda row: row.get("raw_first_risk_trigger_reason") == "take_profit",
            ),
            "raw_no_risk_touch_all_candidates": _rate(
                source,
                lambda row: row.get("raw_first_risk_trigger_reason") is None,
            ),
            "raw_risk_trigger_on_entry_bar": _rate(
                source, lambda row: row.get("raw_risk_trigger_on_entry_bar") is True
            ),
            "actual_risk_stop_loss_exit": _rate(
                source, lambda row: row.get("exit_category") == "risk_stop_loss"
            ),
            "actual_risk_take_profit_exit": _rate(
                source, lambda row: row.get("exit_category") == "risk_take_profit"
            ),
            "exit_fill_strictly_before_mfe40_all_candidates": _rate(
                source, lambda row: row.get("exit_fill_before_mfe40") is True
            ),
            "exit_fill_same_bar_as_mfe40_all_candidates": _rate(
                source,
                lambda row: row.get("exit_fill_mfe40_relation")
                == "same_bar_order_unobservable",
            ),
            "bounded_intraday_held_excursion": _rate(
                source,
                lambda row: row.get("held_excursion_observability")
                == "bounded_intraday_fill",
            ),
            "exit_after_40b": _rate(
                source, lambda row: row.get("exit_after_40b") is True
            ),
        },
    }


def _path_evaluator_projection(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "candidate_id",
        "split",
        "symbol",
        "signal_day",
        "normalized_regime",
        "normalized_signal_type",
        "exit_category",
        *PATH_SUMMARY_METRICS,
        *PATH_METRIC_FIELDS,
    )
    return {field: deepcopy(row.get(field)) for field in fields}


def _path_diagnostic(
    rows_by_split: dict[str, list[dict[str, Any]]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report: dict[str, Any] = {
        **STUDY_REGISTRY["entry_exit_path_diagnostic"],
        "status": "descriptive_completed",
        "registered_horizons": list(HORIZONS),
        "path_contract": {
            "horizon_offset_semantics": "entry_index_plus_H",
            "excursion_windows": "inclusive_entry_through_entry_plus_H",
            "raw_risk_touch_window": "entry_offset_0_through_40_inclusive",
            "raw_risk_touches_continue_after_actual_exit": True,
            "raw_risk_touches_are_entry_path_diagnostic_not_exit_resimulation": True,
            "authoritative_exit_source": "production_risk_replay",
            "held_excursion_daily_bar_observability": (
                "open_and_close_exact; intraday_lower_upper_bounds"
            ),
        },
        "registered_cuts": [
            "overall",
            "regime",
            "signal_type",
            "exit_category",
            "regime_x_signal_type",
        ],
        "cell_significance_tests_performed": False,
        "splits": {},
    }
    cells: list[dict[str, Any]] = []
    for split in SPLITS:
        rows = rows_by_split[split]
        overall = _path_summary(rows)
        report["splits"][split] = {"overall": overall}
        cells.append(
            {"study": "entry_exit_path", "split": split, "dimension": "overall", "key": "all", **overall}
        )
        cuts: dict[str, tuple[Sequence[str], Sequence[str]]] = {
            "regime": (REGIMES, ("normalized_regime",)),
            "signal_type": (SIGNAL_TYPES, ("normalized_signal_type",)),
            "exit_category": (EXIT_CATEGORIES, ("exit_category",)),
        }
        for dimension, (keys, fields) in cuts.items():
            output: dict[str, Any] = {}
            field = fields[0]
            for key in keys:
                summary = _path_summary(
                    [row for row in rows if row.get(field) == key]
                )
                output[str(key)] = summary
                cells.append(
                    {
                        "study": "entry_exit_path",
                        "split": split,
                        "dimension": dimension,
                        "key": str(key),
                        **summary,
                    }
                )
            report["splits"][split][f"by_{dimension}"] = output
        matrix: dict[str, Any] = {}
        for regime in REGIMES:
            for signal_type in SIGNAL_TYPES:
                key = f"{regime}|{signal_type}"
                summary = _path_summary(
                    [
                        row
                        for row in rows
                        if row.get("normalized_regime") == regime
                        and row.get("normalized_signal_type") == signal_type
                    ]
                )
                matrix[key] = summary
                cells.append(
                    {
                        "study": "entry_exit_path",
                        "split": split,
                        "dimension": "regime_x_signal_type",
                        "key": key,
                        **summary,
                    }
                )
        report["splits"][split]["by_regime_x_signal_type"] = matrix
    return report, cells


def _index_day_rows(
    enriched_by_split: dict[str, list[dict[str, Any]]],
    index_closed: pd.DataFrame,
    regime_contexts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    close = pd.to_numeric(index_closed["close"], errors="coerce").astype(float)
    ma10 = close.rolling(10, min_periods=10).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()
    ma250 = close.rolling(250, min_periods=250).mean()
    strict_regimes = calculate_strict_regime(close, fast_period=10, slow_period=20)
    day_rows: list[dict[str, Any]] = []
    for split in SPLITS:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in enriched_by_split[split]:
            groups[str(row["signal_day"])].append(row)
        for day, candidates in sorted(groups.items()):
            context = regime_contexts[day]
            signal_idx = int(context["signal_index"])
            entry_idx = signal_idx + 1
            entry_price = _number(
                index_closed.iloc[entry_idx]["open"], "index entry open", positive=True
            )
            row: dict[str, Any] = {
                "split": split,
                "signal_day": day,
                "canonical_regime": context["canonical_regime"],
                "rebuilt_regime": context["rebuilt_regime"],
                "regime_label_match": context["canonical_regime"]
                == context["rebuilt_regime"],
                "market_gate_allows_entries": context["allows_entries"],
                "market_gate_blocked_by": "|".join(context["blocked_by"]),
                "candidate_count": len(candidates),
                "candidate_symbol_count": len(
                    {str(candidate["symbol"]) for candidate in candidates}
                ),
                "candidate_mean_future_40d_pct": _mean(
                    candidate["future_40d"] for candidate in candidates
                ),
                "candidate_mean_trade_pnl_pct": _mean(
                    candidate["trade_pnl_pct"] for candidate in candidates
                ),
                "candidate_mean_mfe_40b_pct": _mean(
                    candidate["mfe_40b_pct"] for candidate in candidates
                ),
                "candidate_mean_mae_40b_pct": _mean(
                    candidate["mae_40b_pct"] for candidate in candidates
                ),
                "candidate_stop_before_take_rate_pct": _rate(
                    candidates,
                    lambda candidate: candidate.get("raw_first_risk_trigger_reason")
                    == "stop_loss",
                ),
                "index_signal_close": round(float(close.iloc[signal_idx]), 6),
            }
            for period, values in ((10, ma10), (20, ma20), (60, ma60), (250, ma250)):
                level = _number(values.iloc[signal_idx], f"index ma{period}", positive=True)
                row[f"index_distance_ma{period}_pct"] = _pct(
                    close.iloc[signal_idx], level
                )
            row["index_ma20_slope_1b_pct"] = _pct(
                ma20.iloc[signal_idx], ma20.iloc[signal_idx - 1]
            )
            row["index_ma60_slope_1b_pct"] = _pct(
                ma60.iloc[signal_idx], ma60.iloc[signal_idx - 1]
            )
            row["index_ma250_slope_5b_pct"] = _pct(
                ma250.iloc[signal_idx], ma250.iloc[signal_idx - 5]
            )
            for horizon in HORIZONS:
                probe = entry_idx + horizon
                row[f"index_future_{horizon}b_pct"] = _pct(
                    _number(index_closed.iloc[probe]["close"], "index future close", positive=True),
                    entry_price,
                )
            for horizon in (5, 10, 20, 40):
                future_regimes = strict_regimes.iloc[
                    signal_idx + 1 : signal_idx + horizon + 1
                ]
                row[f"regime_persistence_{horizon}b_pct"] = round(
                    float((future_regimes == context["canonical_regime"]).mean() * 100.0),
                    4,
                )
            transition_offset: int | None = None
            transition_to: str | None = None
            for offset in range(1, 41):
                future_regime = str(strict_regimes.iloc[signal_idx + offset])
                if future_regime != context["canonical_regime"]:
                    transition_offset = offset
                    transition_to = future_regime
                    break
            row["first_regime_transition_offset"] = transition_offset
            row["first_regime_transition_to"] = transition_to
            day_rows.append(row)
    return day_rows


REGIME_DAY_METRICS = (
    *tuple(f"index_future_{horizon}b_pct" for horizon in HORIZONS),
    "candidate_mean_future_40d_pct",
    "candidate_mean_trade_pnl_pct",
    "candidate_mean_mfe_40b_pct",
    "candidate_mean_mae_40b_pct",
    "candidate_stop_before_take_rate_pct",
    "index_distance_ma10_pct",
    "index_distance_ma20_pct",
    "index_distance_ma60_pct",
    "index_distance_ma250_pct",
    "index_ma20_slope_1b_pct",
    "index_ma60_slope_1b_pct",
    "index_ma250_slope_5b_pct",
    "regime_persistence_5b_pct",
    "regime_persistence_10b_pct",
    "regime_persistence_20b_pct",
    "regime_persistence_40b_pct",
    "first_regime_transition_offset",
)


def _regime_day_projection(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "split",
        "signal_day",
        "canonical_regime",
        "rebuilt_regime",
        "regime_label_match",
        "market_gate_allows_entries",
        "market_gate_blocked_by",
        "candidate_count",
        "candidate_symbol_count",
        *REGIME_DAY_METRICS,
        "first_regime_transition_to",
    )
    return {field: deepcopy(row.get(field)) for field in fields}


def _regime_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    source = list(rows)
    return {
        "n_signal_days": len(source),
        "sample_status": (
            "sufficient_descriptive" if len(source) >= 10 else "small_sample_descriptive"
        ),
        "all_labels_match": all(row.get("regime_label_match") is True for row in source),
        "metrics": {
            field: _numeric_stats(row.get(field) for row in source)
            for field in REGIME_DAY_METRICS
        },
        "rates_pct": {
            "transition_within_5b": _rate(
                source,
                lambda row: row.get("first_regime_transition_offset") is not None
                and int(row["first_regime_transition_offset"]) <= 5,
            ),
            "transition_within_10b": _rate(
                source,
                lambda row: row.get("first_regime_transition_offset") is not None
                and int(row["first_regime_transition_offset"]) <= 10,
            ),
            "transition_within_20b": _rate(
                source,
                lambda row: row.get("first_regime_transition_offset") is not None
                and int(row["first_regime_transition_offset"]) <= 20,
            ),
            "transition_within_40b": _rate(
                source,
                lambda row: row.get("first_regime_transition_offset") is not None,
            ),
        },
    }


def _regime_diagnostic(
    day_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report: dict[str, Any] = {
        **STUDY_REGISTRY["market_regime_calibration_diagnostic"],
        "status": "descriptive_completed",
        "candidate_aggregation": "mean_within_signal_day_then_equal_weight_days",
        "registered_persistence_horizons": [5, 10, 20, 40],
        "cell_significance_tests_performed": False,
        "splits": {},
    }
    cells: list[dict[str, Any]] = []
    for split in SPLITS:
        split_rows = [row for row in day_rows if row["split"] == split]
        overall = _regime_summary(split_rows)
        report["splits"][split] = {"overall": overall, "by_regime": {}}
        cells.append(
            {"study": "market_regime_calibration", "split": split, "dimension": "overall", "key": "all", **overall}
        )
        for regime in REGIMES:
            summary = _regime_summary(
                [row for row in split_rows if row["canonical_regime"] == regime]
            )
            report["splits"][split]["by_regime"][regime] = summary
            cells.append(
                {
                    "study": "market_regime_calibration",
                    "split": split,
                    "dimension": "regime",
                    "key": regime,
                    **summary,
                }
            )
    return report, cells


def _metric_mean(report: dict[str, Any], split: str, metric: str) -> float | None:
    return (
        report.get("splits", {})
        .get(split, {})
        .get("overall", {})
        .get("metrics", {})
        .get(metric, {})
        .get("mean")
    )


def _rate_value(report: dict[str, Any], split: str, metric: str) -> float | None:
    return (
        report.get("splits", {})
        .get(split, {})
        .get("overall", {})
        .get("rates_pct", {})
        .get(metric)
    )


def _below_both(test: float | None, train: float | None, val: float | None) -> bool:
    return all(value is not None for value in (test, train, val)) and bool(
        test < train and test < val
    )


def _above_both(test: float | None, train: float | None, val: float | None) -> bool:
    return all(value is not None for value in (test, train, val)) and bool(
        test > train and test > val
    )


def _registered_warning_flags(
    path_report: dict[str, Any], regime_report: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    def means(metric: str) -> tuple[float | None, float | None, float | None]:
        return tuple(_metric_mean(path_report, split, metric) for split in SPLITS)  # type: ignore[return-value]

    f40 = means("future_40d")
    pnl = means("trade_pnl_pct")
    mfe40 = means("mfe_40b_pct")
    mae40 = means("mae_40b_pct")
    giveback = means("net_mfe_giveback_lower_bound_pct")
    stop_rates = tuple(
        _rate_value(path_report, split, "raw_stop_before_take_all_candidates")
        for split in SPLITS
    )
    day_candidate = _metric_mean(
        regime_report, "test", "candidate_mean_future_40d_pct"
    )
    day_index = _metric_mean(regime_report, "test", "index_future_40b_pct")

    def evidence(values: tuple[Any, Any, Any]) -> dict[str, Any]:
        return dict(zip(SPLITS, values, strict=True))

    return {
        "test_mean_future40_below_zero": {
            "triggered": f40[2] is not None and f40[2] < 0,
            "evidence": evidence(f40),
        },
        "test_mean_trade_pnl_below_zero": {
            "triggered": pnl[2] is not None and pnl[2] < 0,
            "evidence": evidence(pnl),
        },
        "test_mean_future40_below_train_and_val": {
            "triggered": _below_both(f40[2], f40[0], f40[1]),
            "evidence": evidence(f40),
        },
        "test_mean_trade_pnl_below_train_and_val": {
            "triggered": _below_both(pnl[2], pnl[0], pnl[1]),
            "evidence": evidence(pnl),
        },
        "test_mean_mfe40_below_train_and_val": {
            "triggered": _below_both(mfe40[2], mfe40[0], mfe40[1]),
            "evidence": evidence(mfe40),
        },
        "test_mean_mae40_more_negative_than_train_and_val": {
            "triggered": _below_both(mae40[2], mae40[0], mae40[1]),
            "evidence": evidence(mae40),
        },
        "test_stop_before_take_rate_above_train_and_val": {
            "triggered": _above_both(stop_rates[2], stop_rates[0], stop_rates[1]),
            "evidence": evidence(stop_rates),
        },
        "test_mean_conservative_net_mfe_giveback_above_train_and_val": {
            "triggered": _above_both(giveback[2], giveback[0], giveback[1]),
            "evidence": evidence(giveback),
        },
        "test_equal_day_candidate_negative_index_nonnegative": {
            "triggered": bool(
                day_candidate is not None
                and day_index is not None
                and day_candidate < 0
                and day_index >= 0
            ),
            "evidence": {
                "test_equal_day_candidate_future40_mean_pct": day_candidate,
                "test_equal_day_index_future40_mean_pct": day_index,
            },
        },
    }


CANDIDATE_ARTIFACT_FIELDS = (
    "candidate_id",
    "split",
    "symbol",
    "signal_day",
    "signal_type",
    "regime",
    "normalized_signal_type",
    "normalized_regime",
    "entry_day",
    "entry_price",
    "exit_trigger_day",
    "exit_day",
    "exit_price",
    "exit_reason",
    "exit_category",
    "exit_session",
    "holding_days",
    "holding_bars",
    "price_limit_deferred_bars",
    *FIELDS,
    *PATH_METRIC_FIELDS,
)


def _artifact_row_projection(row: dict[str, Any]) -> dict[str, Any]:
    projected = {
        field: deepcopy(row.get(field)) for field in CANDIDATE_ARTIFACT_FIELDS
    }
    invalid_scalars = {
        field: type(value).__name__
        for field, value in projected.items()
        if not (
            value is None
            or isinstance(value, (str, bool))
            or (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        )
    }
    if invalid_scalars:
        raise RuntimeError(
            f"candidate artifact projection is not a JSON scalar: {invalid_scalars}"
        )
    forbidden = {
        "source_trade_pnl_pct",
        "source_exit_reason",
        "_mark_prices",
        "market_context",
        "confirmation_items",
        "_p5a_features",
        "_p5b_features",
    }
    if forbidden & set(projected):
        raise RuntimeError("candidate artifact contains forbidden replay internals")
    return projected


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_json(row) + "\n")


def _write_outputs_atomically(
    prepared: dict[str, Any],
    enriched_by_split: dict[str, list[dict[str, Any]]],
    cells: list[dict[str, Any]],
    day_rows: list[dict[str, Any]],
    result: dict[str, Any],
    expected_joined_manifests: dict[str, str],
) -> None:
    output_dir: Path = prepared["output_dir"]
    staging_dir: Path = prepared["staging_dir"]
    for split in SPLITS:
        if _rows_manifest(enriched_by_split[split]) != expected_joined_manifests[split]:
            raise RuntimeError(f"joined path rows changed before writer: {split}")
    if not staging_dir.is_dir():
        raise RuntimeError("formal staging marker is absent before publication")
    artifacts: dict[str, Any] = {}
    for split in SPLITS:
        published = [_artifact_row_projection(row) for row in enriched_by_split[split]]
        filename = f"strategy_failure_path_{split}.jsonl"
        staged = staging_dir / filename
        _write_jsonl(staged, published)
        artifacts[split] = {
            "path": str(output_dir / filename),
            "rows": len(published),
            "sha256": file_sha256(staged),
            "size_bytes": staged.stat().st_size,
        }
        result["splits"][split]["enriched"] = str(output_dir / filename)
        result["splits"][split]["enriched_sha256"] = artifacts[split]["sha256"]

    staged_cells = staging_dir / "strategy_failure_cells.jsonl"
    _write_jsonl(staged_cells, cells)
    artifacts["strategy_failure_cells"] = {
        "path": str(output_dir / staged_cells.name),
        "rows": len(cells),
        "sha256": file_sha256(staged_cells),
        "size_bytes": staged_cells.stat().st_size,
    }
    staged_days = staging_dir / "regime_calibration_days.jsonl"
    _write_jsonl(staged_days, day_rows)
    artifacts["regime_calibration_days"] = {
        "path": str(output_dir / staged_days.name),
        "rows": len(day_rows),
        "sha256": file_sha256(staged_days),
        "size_bytes": staged_days.stat().st_size,
    }

    static_pre_publish, static_changes = _snapshot_changes(
        prepared["static_initial"]
    )
    history_pre_publish, history_changes = _snapshot_changes(
        prepared["history_initial"]
    )
    if static_changes or history_changes:
        raise RuntimeError(
            "protected inputs changed before publish: "
            f"static={static_changes}, history={history_changes}"
        )
    for split in SPLITS:
        if _rows_manifest(enriched_by_split[split]) != expected_joined_manifests[split]:
            raise RuntimeError(f"joined path rows changed before publish: {split}")
    result["pre_publish_input_stability"] = {
        "all_unchanged": True,
        "static_manifest_sha256": _snapshot_manifest(static_pre_publish),
        "history_manifest_sha256": _snapshot_manifest(history_pre_publish),
        "changed_paths": [],
    }
    result["artifacts"] = artifacts
    result["report_path"] = str(output_dir / "strategy_failure_path_audit.json")
    staged_report = staging_dir / "strategy_failure_path_audit.json"
    with staged_report.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(
            result,
            handle,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        handle.write("\n")
    if output_dir.exists():
        raise RuntimeError(
            f"formal output directory appeared before publish: {output_dir}"
        )
    staging_dir.rename(output_dir)


def run(args: argparse.Namespace) -> dict[str, Any]:
    prepared = _prepare(args)
    prepared["staging_dir"].mkdir(parents=True, exist_ok=False)
    static_pre_replay, static_changes = _snapshot_changes(
        prepared["static_initial"]
    )
    history_pre_replay, history_changes = _snapshot_changes(
        prepared["history_initial"]
    )
    if static_changes or history_changes:
        raise RuntimeError(
            "protected inputs changed before replay: "
            f"static={static_changes}, history={history_changes}"
        )

    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "script_sha256": str(args.expected_script_sha256).lower(),
        "config": str(prepared["config_path"]),
        "config_file_expected_sha256": PREREGISTERED_CONFIG_SHA256,
        "config_snapshot": prepared["config_snapshot"],
        "index_data": str(INDEX_PATH.resolve()),
        "index_file_expected_sha256": PREREGISTERED_INDEX_SHA256,
        "input_dir": str(prepared["input_dir"]),
        "output_dir": str(prepared["output_dir"]),
        "splits_required": list(SPLITS),
        "registered_horizons": list(HORIZONS),
        "studies": deepcopy(STUDY_REGISTRY),
        "dependency_contract": prepared["dependency_contract"],
        "candidate_integrity": prepared["integrity_by_split"],
        "history_inventory": prepared["history_inventory"],
        "history_manifest_sha256": prepared["history_manifest"],
        "signal_day_coverage": prepared["signal_day_coverage"],
        "path_feasibility": prepared["path_feasibility"],
        "regime_feasibility": prepared["regime_feasibility"],
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
            "exactly_one_replay_per_split": True,
            "shared_authoritative_replay_across_diagnostics": True,
        },
        "design": {
            "diagnostic_only": True,
            "candidate_gate_implemented": False,
            "filter_rule_generated": False,
            "exit_variant_implemented": False,
            "causal_attribution_performed": False,
            "cell_significance_tests_performed": False,
            "parameter_scan_performed": False,
            "reverse_direction_tested": False,
            "holdout_consumed": False,
            "portfolio_layer_implemented": False,
            "production_eligible": False,
            "source_outcomes_used": False,
            "replay_outcomes_used": True,
        },
        "safety": {
            "production_database_connected": False,
            "local_database_connected": False,
            "network_fetch_performed": False,
            "history_cache_write_performed": False,
            "canonical_write_performed": False,
            "config_write_performed": False,
            "freeze_or_seal_write_performed": False,
            "production_change_performed": False,
        },
        "production_decision": "unchanged_P0",
        "splits": {},
    }

    replay_by_split: dict[str, list[dict[str, Any]]] = {}
    replay_calls = 0
    for split in SPLITS:
        sources = prepared["sources_by_split"][split]
        replay_sources = [batch_base._replay_input_projection(row) for row in sources]
        if any(set(row) & SOURCE_LEGACY_OUTCOME_FIELDS for row in replay_sources):
            raise RuntimeError("legacy source outcomes reached replay input")
        identity_map = {candidate_id(row): {} for row in sources}
        replay_rows, replay_skips, raw_source_match = _production_replay_split(
            replay_sources,
            prepared["config"],
            "production_risk",
            HISTORY_DIR,
        )
        replay_calls += 1
        replay_integrity, source_match = batch_base._assert_replay_integrity(
            sources,
            identity_map,
            replay_rows,
            replay_skips,
            raw_source_match,
        )
        batch_base._validate_authoritative_outcomes(replay_rows)
        passthrough_integrity = _validate_replay_passthrough(sources, replay_rows)
        replay_by_split[split] = deepcopy(replay_rows)
        result["splits"][split] = {
            "source_rows": len(sources),
            "replay_rows": len(replay_rows),
            "replay_call_count": 1,
            "replay_skips": dict(replay_skips),
            "replay_integrity": replay_integrity,
            "replay_passthrough_integrity": passthrough_integrity,
            "source_match": source_match,
            "source_candidate_ids_sha256": candidate_ids_sha256(sources),
            "replay_candidate_ids_sha256": candidate_ids_sha256(replay_rows),
        }
    if replay_calls != len(SPLITS):
        raise RuntimeError(
            f"replay call contract failed: expected={len(SPLITS)}, actual={replay_calls}"
        )
    result["replay_call_count_actual"] = replay_calls

    static_post_replay, static_changes = _snapshot_changes(
        prepared["static_initial"]
    )
    history_post_replay, history_changes = _snapshot_changes(
        prepared["history_initial"]
    )
    if static_changes or history_changes:
        raise RuntimeError(
            "protected inputs changed after replay: "
            f"static={static_changes}, history={history_changes}"
        )
    result["post_replay_input_stability"] = {
        "all_unchanged": True,
        "static_manifest_sha256": _snapshot_manifest(static_post_replay),
        "history_manifest_sha256": _snapshot_manifest(history_post_replay),
        "changed_paths": [],
    }

    enriched_by_split = _enrich_replay_paths(replay_by_split, prepared["config"])
    joined_manifests = {
        split: _rows_manifest(enriched_by_split[split]) for split in SPLITS
    }
    path_inputs = {
        split: [
            _path_evaluator_projection(row) for row in enriched_by_split[split]
        ]
        for split in SPLITS
    }
    path_input_manifests = {
        split: _rows_manifest(path_inputs[split]) for split in SPLITS
    }
    path_report, path_cells = _run_immutable_evaluator(
        path_inputs, _path_diagnostic, "path"
    )
    if any(
        _rows_manifest(path_inputs[split]) != path_input_manifests[split]
        for split in SPLITS
    ):
        raise RuntimeError("path evaluator source projection mutated")
    if any(
        _rows_manifest(enriched_by_split[split]) != joined_manifests[split]
        for split in SPLITS
    ):
        raise RuntimeError("path evaluator mutated joined replay rows")

    day_rows = _index_day_rows(
        enriched_by_split,
        prepared["index_closed"],
        prepared["regime_contexts"],
    )
    regime_inputs = [_regime_day_projection(row) for row in day_rows]
    regime_input_manifest = _rows_manifest(regime_inputs)
    regime_report, regime_cells = _run_immutable_evaluator(
        regime_inputs, _regime_diagnostic, "regime"
    )
    if _rows_manifest(regime_inputs) != regime_input_manifest:
        raise RuntimeError("regime evaluator source projection mutated")
    if any(
        _rows_manifest(enriched_by_split[split]) != joined_manifests[split]
        for split in SPLITS
    ):
        raise RuntimeError("regime evaluator mutated joined replay rows")

    warning_flags = _registered_warning_flags(path_report, regime_report)
    result["studies"]["entry_exit_path_diagnostic"].update(path_report)
    result["studies"]["market_regime_calibration_diagnostic"].update(
        regime_report
    )
    result["registered_warning_flags"] = warning_flags
    result["triggered_warning_flags"] = sorted(
        key for key, value in warning_flags.items() if value["triggered"]
    )
    result["multiple_testing"] = {
        "registered_warning_count": len(warning_flags),
        "all_registered_warnings_published": True,
        "descriptive_cells_excluded_from_inference": True,
        "no_batch_or_gate": True,
    }
    result["baseline_replay_complete"] = all(
        result["splits"][split]["source_match"]["baseline_replay_complete"]
        for split in SPLITS
    )
    result["batch_contract_pass"] = bool(
        result["baseline_replay_complete"]
        and replay_calls == len(SPLITS)
        and all(
            result["splits"][split]["replay_integrity"]["all_pass"]
            for split in SPLITS
        )
        and prepared["regime_feasibility"]["all_regime_labels_match"]
        and all(
            prepared["path_feasibility"][split]["coverage"] == 1.0
            for split in SPLITS
        )
    )
    if not result["batch_contract_pass"]:
        raise RuntimeError("strategy failure batch contract failed")
    result["verdict"] = (
        "registered_strategy_failure_diagnostics_completed_no_production_change"
    )
    cells = [*path_cells, *regime_cells]
    _write_outputs_atomically(
        prepared,
        enriched_by_split,
        cells,
        regime_inputs,
        result,
        joined_manifests,
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=str(CANONICAL_INPUT_DIR))
    parser.add_argument("--output-dir", default=str(FORMAL_OUTPUT_DIR))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--index-data", default=str(INDEX_PATH))
    parser.add_argument("--label", required=True)
    parser.add_argument("--splits", nargs="+", required=True)
    parser.add_argument("--expected-script-sha256", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = preflight(args) if args.preflight_only else run(args)
    except Exception as exc:  # noqa: BLE001 - CLI must fail closed with JSON stderr
        print(
            _canonical_json(
                {"version": VERSION, "status": "failed", "error": str(exc)}
            ),
            file=sys.stderr,
        )
        return 1
    summary = (
        result
        if args.preflight_only
        else {
            "version": result["version"],
            "verdict": result["verdict"],
            "replay_call_count_actual": result["replay_call_count_actual"],
            "triggered_warning_flags": result["triggered_warning_flags"],
            "production_decision": result["production_decision"],
            "report_path": result["report_path"],
        }
    )
    print(_canonical_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
