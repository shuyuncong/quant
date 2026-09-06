"""Read-only data contract audit for canonical volume-stability research.

The audit never replays outcomes and never writes an artifact.  It validates
the frozen QFQ/NONE daily histories used by canonical candidates, proves exact
required-window equality, and detects synchronized order-of-magnitude unit
changes before Volume Stability60 may be implemented or run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import HISTORY_DIR
from candidate_integrity import (
    VERSION as INTEGRITY_VERSION,
)
from candidate_integrity import (
    candidate_id,
    candidate_ids_sha256,
    file_sha256,
)
from utils.helpers import load_config

VERSION = "volume_unit_continuity_audit.v1"
SPLITS = ("train", "val", "test")
CANONICAL_INPUT_DIR = Path(r"D:\tmp\candidates_fullpool_canonical")
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
MARKET_DATA_PATH = BASE_DIR / "data" / "market_data.py"
EXPECTED_CONFIG_SHA256 = (
    "4e0084eb945aa2533dca25f9e0f68b4e27e9f2cbc52c11b7898c082d466000f1"
)
EXPECTED_MARKET_DATA_SHA256 = (
    "07cc9e23d1686811a6ef188ae44233b07e4ff293f3572479ba168982a7ab4f82"
)
EXPECTED_INTEGRITY_MANIFEST_SHA256 = (
    "4e12f3d53851e0cc7f49a3c67f4fcdf84d3b2584630e166c0a0cf9a997e01753"
)
EXPECTED_CANONICAL_SHA256 = {
    "train": "28a2defd6f7f09904295cb270cc311bf989add84b4cd0c8dc947d4e59a4fb40f",
    "val": "921a2ddc6916030a270aa136c0ce05eabdcddfb9ab289ec07c4ef5e8d2ba2bd0",
    "test": "d90836a21610f77c6bb6afca3169d3eedb79c228026415f2e8da956fc8def174",
}
EXPECTED_DUAL_HISTORY_MANIFEST_SHA256 = (
    "0be91a8fd76f1b436b01a67a1e5612995bdee6dedd3b4902ac4f90a7f835635d"
)
WINDOW_BARS = 60
BLOCK_COUNT = 6
BLOCK_BARS = 10
VOLUME_UNIT_SHARES = 100.0
MIN_SYNCHRONIZED_SYMBOLS = 20
UNIT_SHIFT_UPPER_RATIO = 10.0
UNIT_SHIFT_LOWER_RATIO = 0.1
SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")


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


def _load_identities(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"invalid canonical JSON: {path}:{line_number}"
                ) from exc
            if not isinstance(raw, dict):
                raise RuntimeError(  # noqa: TRY004
                    f"canonical row must be an object: {path}:{line_number}"
                )
            rows.append(_identity_projection(raw))
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["symbol"], row["signal_day"])
        if key in seen:
            raise RuntimeError(
                f"canonical symbol x signal_day must be unique: {key}"
            )
        seen.add(key)
    return rows


def _validate_integrity_manifest(
    input_dir: Path,
    split: str,
    source_path: Path,
    identities: list[dict[str, str]],
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
        "output_hash_match": record.get("output_sha256")
        == file_sha256(source_path),
        "output_row_count_match": record.get("output_rows")
        == len(identities),
        "candidate_ids_hash_match": record.get("candidate_ids_sha256")
        == candidate_ids_sha256(identities),
        "all_candidate_ids_unique": len(identities)
        == len({candidate_id(row) for row in identities}),
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(
            f"candidate integrity validation failed for {split}: {checks}"
        )
    return checks


def _history_paths(symbols: Iterable[str]) -> dict[str, Path]:
    return {
        f"{adjustment}:{symbol}": HISTORY_DIR / f"{symbol}_{adjustment}.pkl"
        for symbol in sorted({_normalize_symbol(value) for value in symbols})
        for adjustment in ("none", "qfq")
    }


def _validate_history_frame(
    frame: pd.DataFrame,
    path: Path,
    expected_adjustment: str,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise RuntimeError(  # noqa: TRY004
            f"history is not a DataFrame: {path}"
        )
    numeric_columns = ["open", "high", "low", "close", "volume", "amount"]
    required = {"datetime", "is_closed", *numeric_columns}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"history missing columns {missing}: {path}")
    if frame.attrs.get("adjust") != expected_adjustment:
        raise RuntimeError(
            f"history adjustment must be {expected_adjustment}: {path}"
        )
    if frame.attrs.get("timeframe") != "1d":
        raise RuntimeError(f"history timeframe must be 1d: {path}")

    flags = frame["is_closed"]
    if not all(isinstance(value, (bool, np.bool_)) for value in flags):
        raise RuntimeError(f"history is_closed contains non-bool values: {path}")

    timestamps = pd.to_datetime(frame["datetime"], errors="coerce")
    if timestamps.isna().any():
        raise RuntimeError(f"history contains invalid datetime: {path}")
    if timestamps.dt.tz is not None:
        raise RuntimeError(f"history datetime must be timezone-naive: {path}")
    normalized = timestamps.dt.normalize()
    if not timestamps.equals(normalized):
        raise RuntimeError(f"history datetime must be normalized daily bars: {path}")
    index = pd.DatetimeIndex(normalized)
    if index.duplicated().any():
        raise RuntimeError(f"history contains duplicate dates: {path}")
    if not index.is_monotonic_increasing:
        raise RuntimeError(f"history dates must be strictly increasing: {path}")

    for column in numeric_columns:
        if not all(
            isinstance(value, (int, float, np.integer, np.floating))
            and not isinstance(value, (bool, np.bool_))
            for value in frame[column]
        ):
            raise RuntimeError(
                f"history {column} contains non-numeric values: {path}"
            )
    values = frame[numeric_columns].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise RuntimeError(f"history numeric values must be finite: {path}")
    open_values, high_values, low_values, close_values, volumes, amounts = (
        values.T
    )
    if (
        (open_values <= 0).any()
        or (high_values <= 0).any()
        or (low_values <= 0).any()
        or (close_values <= 0).any()
    ):
        raise RuntimeError(f"history OHLC must be strictly positive: {path}")
    if (
        (high_values < np.maximum(open_values, close_values)).any()
        or (low_values > np.minimum(open_values, close_values)).any()
        or (high_values < low_values).any()
    ):
        raise RuntimeError(f"history OHLC relationships are invalid: {path}")
    if (volumes < 0).any() or (amounts < 0).any():
        raise RuntimeError(f"history volume/amount must be non-negative: {path}")

    closed_mask = flags.to_numpy(dtype=bool)
    return pd.DataFrame(
        values[closed_mask],
        index=index[closed_mask],
        columns=numeric_columns,
    )


def _load_history(path: Path, adjustment: str) -> pd.DataFrame:
    try:
        frame = pd.read_pickle(path)
    except Exception as exc:  # pragma: no cover - pickle errors vary
        raise RuntimeError(f"unable to read history {path}: {exc}") from exc
    return _validate_history_frame(frame, path, adjustment)


def _required_window(frame: pd.DataFrame, signal_day: str) -> pd.DataFrame:
    cutoff = pd.Timestamp(signal_day)
    if cutoff not in frame.index:
        raise RuntimeError(f"signal day is absent from closed history: {signal_day}")
    window = frame.loc[:cutoff].tail(WINDOW_BARS)
    if len(window) != WINDOW_BARS:
        raise RuntimeError(
            f"insufficient {WINDOW_BARS}-bar volume history: {signal_day}"
        )
    if (window["volume"].to_numpy(dtype=float) <= 0).any():
        raise RuntimeError(
            f"required volume window contains non-positive values: {signal_day}"
        )
    return window


def _volume_features(volume: np.ndarray) -> dict[str, float | int]:
    values = np.asarray(volume, dtype=float)
    if values.shape != (WINDOW_BARS,):
        raise RuntimeError(f"volume feature requires exactly {WINDOW_BARS} values")
    if not np.isfinite(values).all() or (values <= 0).any():
        raise RuntimeError("volume feature requires finite positive values")
    blocks = values.reshape(BLOCK_COUNT, BLOCK_BARS)
    block_means = np.mean(blocks, axis=1)
    mean_block = float(np.mean(block_means))
    if not math.isfinite(mean_block) or mean_block <= 0:
        raise RuntimeError("mean block volume must be finite and positive")
    positions = np.arange(BLOCK_COUNT, dtype=float)
    centered = positions - positions.mean()
    slope = float(
        np.sum(centered * (block_means - block_means.mean()))
        / np.sum(np.square(centered))
    )
    with np.errstate(over="ignore", invalid="ignore"):
        factors = {
            "volume_instability_cv_6x10": float(
                np.std(block_means, ddof=0) / mean_block
            ),
            "daily_volume_cv_60": float(
                np.std(values, ddof=0) / np.mean(values)
            ),
            "block_mean_volume_range_ratio_6x10": float(
                (np.max(block_means) - np.min(block_means)) / mean_block
            ),
            "latest_block_mean_volume_ratio_6x10": float(
                block_means[-1] / mean_block
            ),
            "minimum_block_mean_volume_ratio_6x10": float(
                np.min(block_means) / mean_block
            ),
            "block_mean_volume_trend_slope_ratio_6x10": slope / mean_block,
        }
    if not all(math.isfinite(value) for value in factors.values()):
        raise RuntimeError("derived volume features must all be finite")
    return {
        **factors,
        "window_bars": WINDOW_BARS,
        "block_count": BLOCK_COUNT,
        "block_bars": BLOCK_BARS,
    }


def _validate_producer_contract(config_path: Path) -> dict[str, Any]:
    config_sha = file_sha256(config_path)
    market_data_sha = file_sha256(MARKET_DATA_PATH)
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")
    volume_unit = float(
        (config.get("market_data", {}) or {}).get("volume_unit_shares", 0)
    )
    checks = {
        "config_sha_match": config_sha == EXPECTED_CONFIG_SHA256,
        "market_data_sha_match": market_data_sha
        == EXPECTED_MARKET_DATA_SHA256,
        "volume_unit_shares_is_100": volume_unit == VOLUME_UNIT_SHARES,
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(f"current volume interpretation contract failed: {checks}")
    return {
        **checks,
        "config_sha256": config_sha,
        "market_data_sha256": market_data_sha,
        "volume_unit_shares": volume_unit,
        "tencent_volume_storage": "raw_fields_5_without_multiplication_or_division",
        "historical_history_producer_reconstructable": False,
        "current_code_is_interpretation_provenance_only": True,
    }


def _unit_shift_report(
    required_dates: dict[str, set[pd.Timestamp]],
    qfq_frames: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    ratios_by_date: dict[str, list[float]] = defaultdict(list)
    observation_count = 0
    for symbol in sorted(required_dates):
        frame = qfq_frames[symbol]
        position = {timestamp: index for index, timestamp in enumerate(frame.index)}
        for timestamp in sorted(required_dates[symbol]):
            current_index = position.get(timestamp)
            if current_index is None:
                raise RuntimeError(
                    f"required QFQ date disappeared: {symbol} {timestamp.date()}"
                )
            if current_index <= 0:
                raise RuntimeError(
                    f"preceding closed bar is unavailable: {symbol} {timestamp.date()}"
                )
            previous = float(frame.iloc[current_index - 1]["volume"])
            current = float(frame.iloc[current_index]["volume"])
            if previous <= 0 or current <= 0:
                raise RuntimeError(
                    f"unit ratio uses non-positive volume: {symbol} {timestamp.date()}"
                )
            ratios_by_date[timestamp.date().isoformat()].append(current / previous)
            observation_count += 1

    evaluated: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for trade_day in sorted(ratios_by_date):
        ratios = np.asarray(ratios_by_date[trade_day], dtype=float)
        if len(ratios) < MIN_SYNCHRONIZED_SYMBOLS:
            continue
        median_ratio = float(np.median(ratios))
        item = {
            "trade_day": trade_day,
            "symbol_count": len(ratios),
            "median_ratio": median_ratio,
        }
        evaluated.append(item)
        if (
            median_ratio >= UNIT_SHIFT_UPPER_RATIO
            or median_ratio <= UNIT_SHIFT_LOWER_RATIO
        ):
            failures.append(item)
    return {
        "unique_symbol_date_observations": observation_count,
        "evaluated_trade_days": len(evaluated),
        "minimum_symbols_per_day": MIN_SYNCHRONIZED_SYMBOLS,
        "lower_ratio": UNIT_SHIFT_LOWER_RATIO,
        "upper_ratio": UNIT_SHIFT_UPPER_RATIO,
        "minimum_evaluated_median_ratio": min(
            (item["median_ratio"] for item in evaluated), default=None
        ),
        "maximum_evaluated_median_ratio": max(
            (item["median_ratio"] for item in evaluated), default=None
        ),
        "failures": failures,
        "all_pass": not failures,
    }


def audit(
    input_dir: Path = CANONICAL_INPUT_DIR,
    config_path: Path = CONFIG_PATH,
) -> dict[str, Any]:
    input_dir = input_dir.expanduser().resolve()
    config_path = config_path.expanduser().resolve()
    producer_contract = _validate_producer_contract(config_path)
    manifest_path = input_dir / "candidate_integrity_manifest.json"
    if file_sha256(manifest_path) != EXPECTED_INTEGRITY_MANIFEST_SHA256:
        raise RuntimeError("candidate integrity manifest SHA mismatch")

    identities_by_split: dict[str, list[dict[str, str]]] = {}
    integrity: dict[str, Any] = {}
    symbols: set[str] = set()
    for split in SPLITS:
        source_path = (input_dir / f"candidates_{split}.jsonl").resolve()
        if file_sha256(source_path) != EXPECTED_CANONICAL_SHA256[split]:
            raise RuntimeError(f"canonical {split} SHA mismatch")
        identities = _load_identities(source_path)
        identities_by_split[split] = identities
        integrity[split] = _validate_integrity_manifest(
            input_dir, split, source_path, identities
        )
        symbols.update(row["symbol"] for row in identities)

    history_snapshot = _snapshot_named_paths(_history_paths(symbols))
    history_manifest = _snapshot_manifest(history_snapshot)
    if history_manifest != EXPECTED_DUAL_HISTORY_MANIFEST_SHA256:
        raise RuntimeError(
            "dual-adjustment history manifest mismatch: "
            f"expected={EXPECTED_DUAL_HISTORY_MANIFEST_SHA256}, "
            f"actual={history_manifest}"
        )

    qfq_frames: dict[str, pd.DataFrame] = {}
    none_frames: dict[str, pd.DataFrame] = {}
    for symbol in sorted(symbols):
        qfq_frames[symbol] = _load_history(
            Path(history_snapshot[f"qfq:{symbol}"]["path"]), "qfq"
        )
        none_frames[symbol] = _load_history(
            Path(history_snapshot[f"none:{symbol}"]["path"]), "none"
        )

    required_dates: dict[str, set[pd.Timestamp]] = defaultdict(set)
    split_reports: dict[str, Any] = {}
    total_window_observations = 0
    exact_window_observations = 0
    unique_amount_keys: set[tuple[str, pd.Timestamp]] = set()
    amount_implied_checks: list[dict[str, Any]] = []
    factor_by_split: dict[str, list[dict[str, Any]]] = {}

    for split in SPLITS:
        factor_rows: list[dict[str, Any]] = []
        for identity in identities_by_split[split]:
            symbol = identity["symbol"]
            signal_day = identity["signal_day"]
            qfq_window = _required_window(qfq_frames[symbol], signal_day)
            none_window = _required_window(none_frames[symbol], signal_day)
            if not qfq_window.index.equals(none_window.index):
                raise RuntimeError(
                    f"QFQ/NONE required dates differ: {symbol} {signal_day}"
                )
            qfq_volume = qfq_window["volume"].to_numpy(dtype=float)
            none_volume = none_window["volume"].to_numpy(dtype=float)
            if not np.array_equal(qfq_volume, none_volume):
                raise RuntimeError(
                    f"QFQ/NONE required volume differs: {symbol} {signal_day}"
                )
            total_window_observations += WINDOW_BARS
            exact_window_observations += WINDOW_BARS
            required_dates[symbol].update(qfq_window.index)
            features = _volume_features(qfq_volume)
            factor_rows.append(
                {
                    **identity,
                    "candidate_id": candidate_id(identity),
                    **features,
                }
            )

            for timestamp in qfq_window.index:
                amount_key = (symbol, timestamp)
                if amount_key in unique_amount_keys:
                    continue
                amount = float(qfq_window.loc[timestamp, "amount"])
                if amount <= 0:
                    continue
                unique_amount_keys.add(amount_key)
                volume = float(qfq_window.loc[timestamp, "volume"])
                implied_vwap = amount / (volume * VOLUME_UNIT_SHARES)
                low = float(none_window.loc[timestamp, "low"])
                high = float(none_window.loc[timestamp, "high"])
                passed = low <= implied_vwap <= high
                amount_implied_checks.append(
                    {
                        "symbol": symbol,
                        "trade_day": timestamp.date().isoformat(),
                        "implied_vwap": implied_vwap,
                        "none_low": low,
                        "none_high": high,
                        "pass": passed,
                    }
                )
                if not passed:
                    raise RuntimeError(
                        f"amount-implied unit check failed: {symbol} "
                        f"{timestamp.date()}"
                    )
        factor_by_split[split] = factor_rows

    for split in SPLITS:
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in factor_by_split[split]:
            by_day[row["signal_day"]].append(row)
        comparable: list[dict[str, Any]] = []
        excluded_days = 0
        for signal_day, day_rows in by_day.items():
            median = float(
                np.median(
                    [row["volume_instability_cv_6x10"] for row in day_rows]
                )
            )
            labels = [
                row["volume_instability_cv_6x10"] <= median for row in day_rows
            ]
            if all(labels) or not any(labels):
                excluded_days += 1
                continue
            for row, low in zip(day_rows, labels):
                row["primary_low_volume_instability_risk"] = low
                comparable.append(row)
        split_reports[split] = {
            "source_candidates": len(factor_by_split[split]),
            "available_candidates": len(factor_by_split[split]),
            "assignment_coverage": 1.0,
            "comparable_candidates": len(comparable),
            "comparable_symbols": len({row["symbol"] for row in comparable}),
            "comparable_signal_days": len(
                {row["signal_day"] for row in comparable}
            ),
            "low_n": sum(
                row["primary_low_volume_instability_risk"] is True
                for row in comparable
            ),
            "high_n": sum(
                row["primary_low_volume_instability_risk"] is False
                for row in comparable
            ),
            "excluded_single_group_signal_days": excluded_days,
            "factor_min": min(
                row["volume_instability_cv_6x10"]
                for row in factor_by_split[split]
            ),
            "factor_median": float(
                np.median(
                    [
                        row["volume_instability_cv_6x10"]
                        for row in factor_by_split[split]
                    ]
                )
            ),
            "factor_max": max(
                row["volume_instability_cv_6x10"]
                for row in factor_by_split[split]
            ),
        }

    unit_shift = _unit_shift_report(required_dates, qfq_frames)
    if not unit_shift["all_pass"]:
        raise RuntimeError(
            f"synchronized volume unit shift detected: {unit_shift['failures']}"
        )

    unique_required_observations = sum(
        len(timestamps) for timestamps in required_dates.values()
    )
    return {
        "version": VERSION,
        "contract": {
            "all_pass": True,
            "scope": "canonical_required_60_session_windows_only",
            "qfq_none_dates_exact": True,
            "qfq_none_volume_exact": True,
            "required_volume_strictly_positive": True,
            "historical_history_producer_reconstructable": False,
            "absolute_volume_unit_proven_for_all_rows": False,
            "relative_window_factor_research_allowed": True,
        },
        "safety": {
            "outcome_fields_accessed": [],
            "replay_performed": False,
            "production_database_connected": False,
            "local_database_connected": False,
            "network_fetch_performed": False,
            "history_cache_write_performed": False,
            "artifact_write_performed": False,
            "holdout_consumed": False,
        },
        "producer_contract": producer_contract,
        "candidate_integrity": integrity,
        "history_inventory": {
            "symbol_count": len(symbols),
            "file_count": len(history_snapshot),
            "adjustments": ["none", "qfq"],
            "manifest_sha256": history_manifest,
            "expected_manifest_sha256": (
                EXPECTED_DUAL_HISTORY_MANIFEST_SHA256
            ),
        },
        "window_comparison": {
            "candidate_windows": sum(
                len(rows) for rows in identities_by_split.values()
            ),
            "window_observations": total_window_observations,
            "exact_window_observations": exact_window_observations,
            "unique_required_symbol_dates": unique_required_observations,
        },
        "unit_shift_detection": unit_shift,
        "amount_contract": {
            "usable_as_factor": False,
            "reason": "sparse_and_nonuniform_required_window_coverage",
            "positive_unique_required_rows": len(amount_implied_checks),
            "implied_vwap_checks_all_pass": all(
                item["pass"] for item in amount_implied_checks
            ),
        },
        "factor_feasibility": split_reports,
        "research_provenance": {
            "outcome_free": True,
            "canonical_reused_across_candidate_research": True,
            "independent_confirmation": False,
            "holdout_reserved": True,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only canonical volume unit continuity audit"
    )
    parser.add_argument("--input-dir", default=str(CANONICAL_INPUT_DIR))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = audit(Path(args.input_dir), Path(args.config))
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "contract": {"all_pass": False},
                    "error": str(exc),
                    "safety": {
                        "replay_performed": False,
                        "network_fetch_performed": False,
                        "artifact_write_performed": False,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
