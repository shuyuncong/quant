"""Build purged expanding-window research folds from one frozen candidate export.

This module does not fit or select a model.  It converts a deterministic local
baseline signal backtest into calendar folds whose training labels are fully
known before each evaluation interval begins.  Every generated interval is a
development/viewed interval; no output may be described as a holdout.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from candidate_integrity import (  # noqa: E402
    CandidateIntegrityError,
    candidate_id,
    normalize_candidate_rows,
)

VERSION = "long_history_walk_forward_dataset.v1"
HISTORY_DIR = BASE_DIR / "cache" / "daily_history"
DEFAULT_INDEX_DATA = BASE_DIR / "cache" / "index_000001_sh.pkl"
DEFAULT_DATASET_START = date(2023, 9, 1)
DEFAULT_FIRST_EVALUATION_START = date(2024, 7, 1)
DEFAULT_LAST_EVALUATION_END = date(2026, 6, 30)
DEFAULT_EVALUATION_MONTHS = 3
DEFAULT_MIN_TRAIN_CANDIDATES = 30
DEFAULT_MIN_EVALUATION_CANDIDATES = 15
STOCK_POOL_DATA_FAILURE_REASONS = (
    "stock_pool_history_fetch_failed",
    "stock_pool_history_stale",
    "stock_pool_history_missing",
    "stock_pool_listing_days_missing",
    "stock_pool_market_cap_missing",
    "stock_pool_avg_amount_missing",
    "stock_pool_turnover_missing",
)
PREFLIGHT_REQUIRED_CHECKS = frozenset(
    {
        "data_adjustment_is_qfq",
        "local_data_only",
        "fetch_missing_adjusted_disabled",
        "incomplete_trades_excluded",
        "dataset_role_is_full",
        "strict_frozen_universe",
        "stock_pool_missing_data_policy_is_reject",
        "index_data_explicit",
        "index_data_readable",
        "all_universe_qfq_loadable",
        "stock_pool_coverage_ready",
    }
)


def _guard_development_path(path: Path, allow_holdout: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if not allow_holdout and any("holdout" in part.lower() for part in resolved.parts):
        raise ValueError(
            f"Holdout path is blocked for development experiments: {resolved}. "
            "All long-history folds created here are development/viewed data."
        )
    return resolved


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"candidate row must be an object: {path}:{line_number}")
            rows.append(value)
    return rows


def _normalized_universe_symbol(value: Any) -> str:
    raw = str(value).strip().upper()
    if raw.endswith((".SH", ".SZ", ".BJ")):
        raw = raw[:-3]
    if raw.startswith(("SH", "SZ", "BJ")):
        raw = raw[2:]
    if not raw.isdigit() or not 0 < len(raw) <= 6:
        raise ValueError(f"invalid universe symbol: {value}")
    code = raw.zfill(6)
    if code == "000000":
        raise ValueError(f"invalid universe symbol: {value}")
    return code


def _universe_source_symbols(path: Path) -> list[str]:
    if path.suffix.lower() in {".pkl", ".pickle"}:
        value = pd.read_pickle(path)
        if not isinstance(value, pd.DataFrame) or not {"code", "name"}.issubset(
            value.columns
        ):
            raise ValueError("universe DataFrame requires code and name columns")
        raw_symbols = value["code"].tolist()
    elif path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            for key in ("symbols", "codes", "universe", "stock_list"):
                if isinstance(value.get(key), list):
                    value = value[key]
                    break
        if not isinstance(value, list):
            raise ValueError("universe JSON does not contain a list")
        raw_symbols = [
            item.get("code", item.get("symbol", item.get("ts_code")))
            if isinstance(item, dict)
            else item
            for item in value
        ]
    else:
        raise ValueError("universe source must be PKL, PICKLE or JSON")
    symbols = [_normalized_universe_symbol(value) for value in raw_symbols]
    if len(symbols) != len(set(symbols)):
        raise ValueError("universe source has duplicate symbols")
    return sorted(symbols)


def _candidate_sort_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row["entry_day"]),
        str(row["signal_type"]),
        str(row["symbol"]),
        str(row["signal_day"]),
        str(row["candidate_id"]),
    )


def _write_jsonl_once(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_json(row) + "\n")
    return {"path": str(path), "rows": len(rows), "sha256": _sha256_file(path)}


def _write_json_once(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def _parse_day(value: Any, field: str, candidate: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid {field} for {candidate}: {value}") from exc


def _add_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def build_fold_boundaries(
    first_evaluation_start: date,
    last_evaluation_end: date,
    evaluation_months: int,
) -> list[tuple[date, date]]:
    if evaluation_months < 1:
        raise ValueError("evaluation_months must be positive")
    if first_evaluation_start > last_evaluation_end:
        raise ValueError("first evaluation start is after last evaluation end")
    boundaries: list[tuple[date, date]] = []
    start = first_evaluation_start
    while start <= last_evaluation_end:
        natural_end = _add_months(start, evaluation_months) - timedelta(days=1)
        end = min(natural_end, last_evaluation_end)
        boundaries.append((start, end))
        start = end + timedelta(days=1)
    return boundaries


def _prepare_candidates(
    raw_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in raw_rows:
        copy = dict(row)
        generated_id = candidate_id(copy)
        existing_id = copy.get("candidate_id")
        if existing_id is not None and str(existing_id) != generated_id:
            raise CandidateIntegrityError(
                f"candidate_id does not match canonical fields: {existing_id} != {generated_id}"
            )
        copy["candidate_id"] = generated_id
        _parse_day(copy.get("signal_day"), "signal_day", generated_id)
        _parse_day(copy.get("entry_day"), "entry_day", generated_id)
        _parse_day(copy.get("exit_day"), "exit_day", generated_id)
        if _parse_day(copy["signal_day"], "signal_day", generated_id) >= _parse_day(
            copy["entry_day"], "entry_day", generated_id
        ):
            raise ValueError(f"entry_day must be after signal_day: {generated_id}")
        if _parse_day(copy["exit_day"], "exit_day", generated_id) < _parse_day(
            copy["entry_day"], "entry_day", generated_id
        ):
            raise ValueError(f"exit_day precedes entry_day: {generated_id}")
        prepared.append(copy)
    normalized, integrity = normalize_candidate_rows(prepared)
    normalized.sort(key=_candidate_sort_key)
    integrity["canonical_candidate_manifest_sha256"] = _sha256_bytes(
        "\n".join(str(row["candidate_id"]) for row in normalized).encode("utf-8")
    )
    return normalized, integrity


def _validated_candidate_symbols(
    rows: list[dict[str, Any]], universe_symbols: list[str]
) -> set[str]:
    symbols = [str(row.get("symbol", "")) for row in rows]
    noncanonical = sorted(
        {symbol for symbol in symbols if len(symbol) != 6 or not symbol.isdigit()}
    )
    if noncanonical:
        raise RuntimeError(
            "candidate artifact contains non-canonical symbols; "
            f"strict six-digit codes required: {noncanonical[:10]}"
        )
    outside_universe = sorted(set(symbols) - set(universe_symbols))
    if outside_universe:
        raise RuntimeError(
            "candidate artifact contains symbols outside frozen universe: "
            f"{outside_universe[:10]}"
        )
    return set(symbols)


def _effective_qfq_counts(
    path: Path,
    history_bars: int,
    start: date,
    end: date,
) -> tuple[int, int]:
    frame = pd.read_pickle(path)
    if not isinstance(frame, pd.DataFrame) or frame.empty or "datetime" not in frame:
        return 0, 0
    frame = frame.copy()
    if "is_closed" in frame:
        frame = frame[frame["is_closed"].fillna(False).astype(bool)]
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame = frame.dropna(subset=["datetime"])
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "close" not in frame:
        return 0, 0
    for column in ("open", "high", "low"):
        if column not in frame:
            frame[column] = frame["close"]
        else:
            frame[column] = frame[column].fillna(frame["close"])
    frame = (
        frame.dropna(subset=["open", "high", "low", "close"])
        .sort_values("datetime")
        .drop_duplicates("datetime", keep="last")
        .tail(history_bars)
    )
    frame = frame[frame["datetime"].dt.date <= end]
    window_bars = int(
        ((frame["datetime"].dt.date >= start) & (frame["datetime"].dt.date <= end)).sum()
    )
    return len(frame), window_bars


def _preflight_attestation_matches(
    report: dict[str, Any],
    artifacts: dict[str, Any],
    candidates_path: Path,
    universe: dict[str, Any],
    symbols: list[str],
    qfq_hashes: dict[str, str],
    qfq_paths: dict[str, Path],
    frozen_index: dict[str, Any],
    filters: dict[str, Any],
    window: dict[str, Any],
    experiment: dict[str, Any],
) -> bool:
    try:
        attestation = report["preflight_attestation"]
        if not isinstance(attestation, dict):
            return False
        artifact_value = attestation.get("artifact")
        report_artifact_value = artifacts.get("preflight")
        if not artifact_value or not report_artifact_value:
            return False
        artifact_path = Path(str(artifact_value))
        report_artifact_path = Path(str(report_artifact_value))
        if not artifact_path.is_absolute():
            artifact_path = candidates_path.parent / artifact_path
        if not report_artifact_path.is_absolute():
            report_artifact_path = candidates_path.parent / report_artifact_path
        artifact_path = _guard_development_path(artifact_path)
        report_artifact_path = _guard_development_path(report_artifact_path)
        if artifact_path != report_artifact_path or not artifact_path.exists():
            return False
        artifact_sha256 = _sha256_file(artifact_path)
        if (
            attestation.get("artifact_sha256") != artifact_sha256
            or attestation.get("full_result_sha256") != artifact_sha256
        ):
            return False
        preflight = _load_json(artifact_path)
        checks = preflight.get("checks")
        summary = preflight.get("summary")
        policy = preflight.get("policy")
        effective_run = preflight.get("effective_run")
        inputs = preflight.get("inputs")
        if not all(
            isinstance(value, dict)
            for value in (checks, summary, policy, effective_run, inputs)
        ):
            return False
        if (
            preflight.get("version") != "long_history_preflight.v1"
            or preflight.get("mode") != "read_only_preflight"
            or preflight.get("passes_preflight") is not True
            or preflight.get("production_eligible") is not False
            or set(checks) != PREFLIGHT_REQUIRED_CHECKS
            or not all(value is True for value in checks.values())
        ):
            return False
        if policy != {
            "holdout_used": False,
            "network_used": False,
            "database_used": False,
            "writes_performed": False,
        }:
            return False
        if (
            summary.get("symbols") != len(symbols)
            or summary.get("blocking_symbols") != 0
            or summary.get("blocking_by_reason") != {}
        ):
            return False
        stock_pool = filters.get("stock_pool")
        stock_pool = stock_pool if isinstance(stock_pool, dict) else {}
        if (
            effective_run.get("start") != window.get("start")
            or effective_run.get("end") != window.get("end")
            or effective_run.get("history_bars") != report.get("history_bars_requested")
            or effective_run.get("adjustment") != report.get("data_adjustment")
            or effective_run.get("local_data_only")
            is not bool(filters.get("local_data_only"))
            or effective_run.get("fetch_missing_adjusted")
            is not bool(filters.get("fetch_missing_adjusted"))
            or effective_run.get("allow_incomplete")
            is not bool(report.get("allow_incomplete"))
            or effective_run.get("dataset_role") != experiment.get("dataset_role")
            or effective_run.get("stock_pool") != stock_pool
        ):
            return False
        preflight_universe = inputs.get("universe")
        preflight_index = inputs.get("index_data")
        preflight_config = inputs.get("config")
        if not all(
            isinstance(value, dict)
            for value in (preflight_universe, preflight_index, preflight_config)
        ):
            return False
        if (
            preflight_universe.get("source_sha256") != universe.get("source_sha256")
            or preflight_universe.get("symbol_manifest_sha256")
            != universe.get("symbol_manifest_sha256")
            or preflight_universe.get("source_symbols") != len(symbols)
            or preflight_universe.get("selected_symbols") != len(symbols)
        ):
            return False
        frozen_index_path = _guard_development_path(Path(str(frozen_index["path"])))
        preflight_index_path = _guard_development_path(
            Path(str(preflight_index["path"]))
        )
        if (
            preflight_index_path != frozen_index_path
            or preflight_index.get("sha256") != frozen_index.get("sha256")
        ):
            return False
        config_source = Path(str(report["config_source"]))
        if not config_source.is_absolute():
            config_source = candidates_path.parent / config_source
        config_source = _guard_development_path(config_source)
        preflight_config_path = _guard_development_path(
            Path(str(preflight_config["path"]))
        )
        if (
            preflight_config_path != config_source
            or preflight_config.get("sha256") != _sha256_file(config_source)
        ):
            return False
        symbol_records = preflight.get("symbols")
        if not isinstance(symbol_records, list):
            return False
        if [str(row.get("symbol", "")) for row in symbol_records] != symbols:
            return False
        preflight_start = date.fromisoformat(str(effective_run["start"]))
        preflight_end = date.fromisoformat(str(effective_run["end"]))
        preflight_history_bars = int(effective_run["history_bars"])
        preflight_stock_pool_files: dict[Path, str] = {}
        for row in symbol_records:
            symbol = str(row["symbol"])
            qfq = row.get("qfq")
            qfq_path = (
                _guard_development_path(Path(str(qfq.get("path", ""))))
                if isinstance(qfq, dict)
                else None
            )
            if (
                not isinstance(qfq, dict)
                or row.get("blocking_issues") != {}
                or qfq.get("sha256") != qfq_hashes.get(symbol)
                or qfq_path != qfq_paths.get(symbol)
                or qfq_path.name != f"{symbol}_qfq.pkl"
            ):
                return False
            actual_bars, window_bars = _effective_qfq_counts(
                qfq_path,
                preflight_history_bars,
                preflight_start,
                preflight_end,
            )
            if qfq.get("bars") != actual_bars:
                return False
            nonblocking_issues = row.get("nonblocking_issues")
            nonblocking_issues = (
                nonblocking_issues
                if isinstance(nonblocking_issues, dict)
                else {}
            )
            stock_pool_exempt = (
                nonblocking_issues.get("insufficient_history") == 1
                or nonblocking_issues.get("no_history_in_window") == 1
            )
            if (
                (nonblocking_issues.get("insufficient_history") == 1)
                is not (actual_bars < 60)
                or (nonblocking_issues.get("no_history_in_window") == 1)
                is not (actual_bars >= 60 and window_bars == 0)
            ):
                return False
            if stock_pool.get("enabled"):
                stock_pool_record = row.get("stock_pool")
                if not isinstance(stock_pool_record, dict):
                    return False
                if stock_pool_exempt:
                    if (
                        stock_pool_record.get("required") is not True
                        or stock_pool_record.get("path") not in (None, "")
                        or stock_pool_record.get("sha256") not in (None, "")
                        or stock_pool_record.get("error") not in (None, "")
                    ):
                        return False
                    continue
                stock_pool_path = _guard_development_path(
                    Path(str(stock_pool_record.get("path", "")))
                )
                if (
                    not stock_pool_path.exists()
                    or stock_pool_record.get("sha256")
                    != _sha256_file(stock_pool_path)
                ):
                    return False
                preflight_stock_pool_files[stock_pool_path] = str(
                    stock_pool_record["sha256"]
                )
        if stock_pool.get("enabled"):
            manifest_value = artifacts.get("stock_pool_history_manifest")
            if not manifest_value:
                return False
            manifest_path = Path(str(manifest_value))
            if not manifest_path.is_absolute():
                manifest_path = candidates_path.parent / manifest_path
            manifest_path = _guard_development_path(manifest_path)
            for manifest_row in _load_jsonl(manifest_path):
                stock_pool_path = Path(str(manifest_row.get("path", "")))
                if not stock_pool_path.is_absolute():
                    stock_pool_path = manifest_path.parent / stock_pool_path
                stock_pool_path = _guard_development_path(stock_pool_path)
                if (
                    manifest_row.get("file") != stock_pool_path.name
                    or manifest_row.get("sha256")
                    != preflight_stock_pool_files.get(stock_pool_path)
                ):
                    return False
        if (
            attestation.get("version") != preflight.get("version")
            or attestation.get("passes_preflight") is not True
            or attestation.get("checks") != checks
            or attestation.get("summary") != summary
            or attestation.get("universe_source_sha256")
            != universe.get("source_sha256")
            or attestation.get("universe_symbol_manifest_sha256")
            != universe.get("symbol_manifest_sha256")
            or attestation.get("index_sha256") != frozen_index.get("sha256")
        ):
            return False
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


def _preflight_exempt_symbols(
    report: dict[str, Any], candidates_path: Path
) -> set[str]:
    attestation = report.get("preflight_attestation")
    attestation = attestation if isinstance(attestation, dict) else {}
    artifact_path = Path(str(attestation.get("artifact", "")))
    if not artifact_path.is_absolute():
        artifact_path = candidates_path.parent / artifact_path
    artifact_path = _guard_development_path(artifact_path)
    preflight = _load_json(artifact_path)
    symbol_records = preflight.get("symbols")
    if not isinstance(symbol_records, list):
        return set()
    return {
        str(row.get("symbol", ""))
        for row in symbol_records
        if isinstance(row.get("nonblocking_issues"), dict)
        and (
            row["nonblocking_issues"].get("insufficient_history") == 1
            or row["nonblocking_issues"].get("no_history_in_window") == 1
        )
    }


def _same_day_competition(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row["entry_day"]) for row in rows)
    multi = {day: count for day, count in counts.items() if count > 1}
    return {
        "entry_days": len(counts),
        "multi_candidate_entry_days": len(multi),
        "candidates_on_multi_candidate_days": sum(multi.values()),
        "max_candidates_same_entry_day": max(counts.values(), default=0),
    }


def _row_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "candidates": 0,
            "symbols": 0,
            "signal_days": 0,
            "date_range": None,
            **_same_day_competition(rows),
        }
    entry_days = [str(row["entry_day"]) for row in rows]
    return {
        "candidates": len(rows),
        "symbols": len({str(row["symbol"]) for row in rows}),
        "signal_days": len({str(row["signal_day"]) for row in rows}),
        "date_range": {"first_entry_day": min(entry_days), "last_entry_day": max(entry_days)},
        "signal_type_counts": dict(
            sorted(Counter(str(row["signal_type"]) for row in rows).items())
        ),
        **_same_day_competition(rows),
    }


def _source_report_audit(
    report: dict[str, Any],
    candidates_path: Path,
    index_data_path: Path,
    dataset_start: date,
    last_evaluation_end: date,
) -> dict[str, Any]:
    artifacts = report.get("artifacts")
    filters = report.get("filters")
    window = report.get("window")
    config_snapshot = report.get("config_snapshot")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    filters = filters if isinstance(filters, dict) else {}
    window = window if isinstance(window, dict) else {}
    config_snapshot = config_snapshot if isinstance(config_snapshot, dict) else {}
    stock_pool = filters.get("stock_pool")
    stock_pool_history = filters.get("stock_pool_history")
    stock_pool = stock_pool if isinstance(stock_pool, dict) else {}
    stock_pool_history = (
        stock_pool_history if isinstance(stock_pool_history, dict) else {}
    )
    local_cache_manifest = stock_pool_history.get("local_cache_manifest")
    local_cache_manifest = (
        local_cache_manifest if isinstance(local_cache_manifest, dict) else {}
    )
    signal_report = report.get("signal")
    signal_report = signal_report if isinstance(signal_report, dict) else {}
    skipped = signal_report.get("skipped")
    skipped = skipped if isinstance(skipped, dict) else {}
    stock_pool_enabled = bool(stock_pool.get("enabled"))
    universe = report.get("universe")
    universe = universe if isinstance(universe, dict) else {}
    experiment = report.get("experiment")
    experiment = experiment if isinstance(experiment, dict) else {}
    symbols_requested = report.get("symbols_requested")
    symbols_succeeded = report.get("symbols_succeeded")
    symbols_failed = report.get("symbols_failed")
    manifest_files = local_cache_manifest.get("files")
    manifest_files_valid = (
        isinstance(manifest_files, int)
        and not isinstance(manifest_files, bool)
        and manifest_files > 0
    )
    stock_pool_data_failures = {
        reason: skipped.get(reason, 0) for reason in STOCK_POOL_DATA_FAILURE_REASONS
    }
    stock_pool_failures_valid = all(
        isinstance(value, int) and not isinstance(value, bool) and value == 0
        for value in stock_pool_data_failures.values()
    )
    universe_manifest_value = artifacts.get("universe_manifest")
    universe_manifest_path = (
        Path(str(universe_manifest_value)) if universe_manifest_value else None
    )
    if universe_manifest_path is not None and not universe_manifest_path.is_absolute():
        universe_manifest_path = candidates_path.parent / universe_manifest_path
    if universe_manifest_path is not None:
        universe_manifest_path = _guard_development_path(universe_manifest_path)
    universe_rows_valid = False
    universe_manifest_matches = False
    symbols: list[str] = []
    if universe_manifest_path is not None and universe_manifest_path.exists():
        try:
            universe_rows = _load_jsonl(universe_manifest_path)
            symbols = [str(row.get("symbol", "")) for row in universe_rows]
            universe_rows_valid = (
                bool(symbols)
                and all(len(symbol) == 6 and symbol.isdigit() for symbol in symbols)
                and symbols == sorted(symbols)
                and len(symbols) == len(set(symbols))
            )
            universe_manifest_matches = (
                universe_rows_valid
                and universe.get("selected_symbols") == len(symbols)
                and universe.get("symbol_manifest_sha256")
                == _sha256_bytes("\n".join(symbols).encode("utf-8"))
            )
        except Exception:
            universe_rows_valid = False
            universe_manifest_matches = False
    universe_source_path_value = universe.get("source_path")
    universe_source_path = (
        Path(str(universe_source_path_value)) if universe_source_path_value else None
    )
    if universe_source_path is not None and not universe_source_path.is_absolute():
        universe_source_path = candidates_path.parent / universe_source_path
    if universe_source_path is not None:
        universe_source_path = _guard_development_path(universe_source_path)
    universe_source_hash_matches = (
        universe_source_path is not None
        and universe_source_path.exists()
        and universe.get("source_sha256") == _sha256_file(universe_source_path)
        and universe.get("expected_sha256") == universe.get("source_sha256")
    )
    universe_source_symbols_match = False
    if universe_source_hash_matches and universe_rows_valid:
        try:
            universe_source_symbols_match = (
                _universe_source_symbols(universe_source_path) == symbols
            )
        except Exception:
            universe_source_symbols_match = False

    frozen_inputs = report.get("frozen_inputs")
    frozen_inputs = frozen_inputs if isinstance(frozen_inputs, dict) else {}
    frozen_index = frozen_inputs.get("index_data")
    frozen_index = frozen_index if isinstance(frozen_index, dict) else {}
    frozen_engine = frozen_inputs.get("backtest_engine")
    frozen_engine = frozen_engine if isinstance(frozen_engine, dict) else {}
    frozen_signal_engine = frozen_inputs.get("signal_engine")
    frozen_signal_engine = (
        frozen_signal_engine if isinstance(frozen_signal_engine, dict) else {}
    )
    frozen_qfq = frozen_inputs.get("qfq_history")
    frozen_qfq = frozen_qfq if isinstance(frozen_qfq, dict) else {}
    try:
        frozen_index_path = _guard_development_path(Path(str(frozen_index["path"])))
        frozen_index_matches = (
            frozen_index_path == index_data_path.resolve()
            and frozen_index.get("sha256") == _sha256_file(frozen_index_path)
        )
    except (KeyError, OSError, ValueError):
        frozen_index_matches = False
    try:
        frozen_engine_path = _guard_development_path(Path(str(frozen_engine["path"])))
        frozen_engine_matches = (
            frozen_engine_path.exists()
            and frozen_engine.get("sha256") == _sha256_file(frozen_engine_path)
        )
    except (KeyError, OSError, ValueError):
        frozen_engine_matches = False
    try:
        frozen_signal_engine_path = _guard_development_path(
            Path(str(frozen_signal_engine["path"]))
        )
        frozen_signal_engine_matches = (
            frozen_signal_engine_path.exists()
            and frozen_signal_engine.get("sha256")
            == _sha256_file(frozen_signal_engine_path)
        )
    except (KeyError, OSError, ValueError):
        frozen_signal_engine_matches = False

    qfq_manifest_value = artifacts.get("qfq_history_manifest")
    qfq_manifest_path = (
        Path(str(qfq_manifest_value)) if qfq_manifest_value else None
    )
    if qfq_manifest_path is not None and not qfq_manifest_path.is_absolute():
        qfq_manifest_path = candidates_path.parent / qfq_manifest_path
    full_qfq_manifest_valid = False
    qfq_hashes: dict[str, str] = {}
    qfq_paths: dict[str, Path] = {}
    if qfq_manifest_path is not None:
        try:
            qfq_manifest_path = _guard_development_path(qfq_manifest_path)
            qfq_rows = _load_jsonl(qfq_manifest_path)
            qfq_entries: list[str] = []
            qfq_symbols: list[str] = []
            for row in qfq_rows:
                symbol = str(row.get("symbol", ""))
                path = Path(str(row.get("path", "")))
                if not path.is_absolute():
                    path = qfq_manifest_path.parent / path
                path = _guard_development_path(path)
                digest = _sha256_file(path)
                if (
                    row.get("exists") is not True
                    or row.get("file") != f"{symbol}_qfq.pkl"
                    or path.name != f"{symbol}_qfq.pkl"
                    or row.get("sha256") != digest
                ):
                    raise ValueError("QFQ manifest row mismatch")
                qfq_symbols.append(symbol)
                qfq_hashes[symbol] = digest
                qfq_paths[symbol] = path
                qfq_entries.append(f"{symbol}|{digest}")
            full_qfq_manifest_valid = (
                qfq_symbols == symbols
                and frozen_qfq.get("symbols") == len(qfq_symbols)
                and frozen_qfq.get("missing_symbols") == 0
                and frozen_qfq.get("manifest_sha256")
                == _sha256_bytes("\n".join(qfq_entries).encode("utf-8"))
            )
        except Exception:
            full_qfq_manifest_valid = False

    artifact_value = artifacts.get("signal_trades")
    artifact_path = Path(str(artifact_value)) if artifact_value else None
    artifact_matches = (
        artifact_path is not None and artifact_path.resolve() == candidates_path
    )
    try:
        source_start = date.fromisoformat(str(window.get("start")))
        source_end = date.fromisoformat(str(window.get("end")))
    except ValueError:
        source_start = None
        source_end = None
    checks = {
        "signal_artifact_matches_input": artifact_matches,
        "data_adjustment_is_qfq": str(report.get("data_adjustment", "")).lower() == "qfq",
        "local_data_only": bool(filters.get("local_data_only")),
        "fetch_missing_adjusted_disabled": not bool(
            filters.get("fetch_missing_adjusted")
        ),
        "incomplete_trades_excluded": not bool(report.get("allow_incomplete")),
        "signal_mode_present": "signal" in report,
        "dataset_role_is_full": experiment.get("dataset_role") == "full",
        "strict_frozen_universe": (
            universe.get("source") == "frozen_universe_file"
            and universe.get("strict") is True
        ),
        "universe_count_matches_requested": (
            universe.get("selected_symbols") == symbols_requested
        ),
        "complete_universe_not_limited": (
            universe.get("source_symbols") == universe.get("selected_symbols")
        ),
        "universe_manifest_valid": universe_manifest_matches,
        "universe_source_hash_matches": universe_source_hash_matches,
        "universe_source_symbols_match_manifest": universe_source_symbols_match,
        "source_index_matches_builder_input": frozen_index_matches,
        "backtest_engine_hash_matches": frozen_engine_matches,
        "signal_engine_hash_matches": frozen_signal_engine_matches,
        "full_universe_qfq_manifest_valid": full_qfq_manifest_valid,
        "successful_preflight_attestation": _preflight_attestation_matches(
            report,
            artifacts,
            candidates_path,
            universe,
            symbols,
            qfq_hashes,
            qfq_paths,
            frozen_index,
            filters,
            window,
            experiment,
        ),
        "all_requested_symbols_succeeded": (
            isinstance(symbols_requested, int)
            and not isinstance(symbols_requested, bool)
            and symbols_requested > 0
            and symbols_succeeded == symbols_requested
            and symbols_failed == 0
        ),
        "source_window_starts_by_dataset_start": (
            source_start is not None and source_start <= dataset_start
        ),
        "source_window_covers_last_evaluation_end": (
            source_end is not None and source_end >= last_evaluation_end
        ),
        "stock_pool_local_cache_manifest_present": (
            not stock_pool_enabled
            or (
                manifest_files_valid
                and bool(local_cache_manifest.get("manifest_sha256"))
            )
        ),
        "stock_pool_manifest_artifact_present": (
            not stock_pool_enabled
            or bool(artifacts.get("stock_pool_history_manifest"))
        ),
        "stock_pool_history_failures_zero": (
            not stock_pool_enabled or stock_pool_failures_valid
        ),
        "stock_pool_missing_data_policy_is_reject": (
            not stock_pool_enabled
            or stock_pool.get("missing_data_policy") == "reject"
        ),
    }
    if not all(checks.values()):
        raise RuntimeError(f"candidate source report fails local/frozen checks: {checks}")
    preflight_exempt_symbols = _preflight_exempt_symbols(report, candidates_path)
    return {
        "checks": checks,
        "window": window,
        "symbols_requested": report.get("symbols_requested"),
        "symbols_succeeded": report.get("symbols_succeeded"),
        "symbols_failed": report.get("symbols_failed"),
        "history_sources": report.get("history_sources"),
        "universe": universe,
        "frozen_inputs": frozen_inputs,
        "preflight_attestation": {
            "version": (report.get("preflight_attestation") or {}).get("version"),
            "passes_preflight": True,
            "artifact_sha256": (
                report.get("preflight_attestation") or {}
            ).get("artifact_sha256"),
        },
        "preflight_exempt_symbols": sorted(preflight_exempt_symbols),
        "_universe_symbols": symbols,
        "stock_pool_data_failures": stock_pool_data_failures,
        "execution": report.get("execution"),
        "config_snapshot_sha256": config_snapshot.get("sha256"),
    }


def _qfq_manifest(rows: list[dict[str, Any]], history_dir: Path) -> dict[str, Any]:
    entries: list[str] = []
    missing: list[str] = []
    for symbol in sorted({str(row["symbol"]) for row in rows}):
        path = history_dir / f"{symbol}_qfq.pkl"
        if not path.exists():
            missing.append(symbol)
            continue
        entries.append(f"{symbol}|{_sha256_file(path)}")
    if missing:
        raise RuntimeError(f"missing QFQ history for candidate symbols: {missing[:5]}")
    return {
        "history_dir": str(history_dir),
        "symbols": len(entries),
        "missing_symbols": 0,
        "manifest_sha256": _sha256_bytes("\n".join(entries).encode("utf-8")),
    }


def build_dataset(
    candidates_path: Path,
    source_report_path: Path,
    output_dir: Path,
    index_data_path: Path,
    *,
    dataset_start: date = DEFAULT_DATASET_START,
    first_evaluation_start: date = DEFAULT_FIRST_EVALUATION_START,
    last_evaluation_end: date = DEFAULT_LAST_EVALUATION_END,
    evaluation_months: int = DEFAULT_EVALUATION_MONTHS,
    min_train_candidates: int = DEFAULT_MIN_TRAIN_CANDIDATES,
    min_evaluation_candidates: int = DEFAULT_MIN_EVALUATION_CANDIDATES,
    allow_holdout: bool = False,
) -> dict[str, Any]:
    candidates_path = _guard_development_path(candidates_path, allow_holdout)
    source_report_path = _guard_development_path(source_report_path, allow_holdout)
    output_dir = _guard_development_path(output_dir, allow_holdout)
    index_data_path = _guard_development_path(index_data_path, allow_holdout)
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists; refusing overwrite: {output_dir}")
    if dataset_start >= first_evaluation_start:
        raise ValueError("dataset_start must be before first_evaluation_start")
    if min_train_candidates < 1 or min_evaluation_candidates < 1:
        raise ValueError("candidate minimums must be positive")
    if not candidates_path.exists() or not source_report_path.exists():
        raise FileNotFoundError("candidate file and source report must exist")
    if not index_data_path.exists():
        raise FileNotFoundError(f"index data does not exist: {index_data_path}")

    source_report = _load_json(source_report_path)
    source_audit = _source_report_audit(
        source_report,
        candidates_path,
        index_data_path,
        dataset_start,
        last_evaluation_end,
    )
    raw_rows = _load_jsonl(candidates_path)
    source_signal = source_report.get("signal")
    source_signal = source_signal if isinstance(source_signal, dict) else {}
    source_summary = source_signal.get("summary")
    source_summary = source_summary if isinstance(source_summary, dict) else {}
    if source_summary.get("count") != len(raw_rows):
        raise RuntimeError(
            "source report signal count does not match candidate artifact rows"
        )
    candidates, integrity = _prepare_candidates(raw_rows)
    candidate_symbols = _validated_candidate_symbols(
        candidates, source_audit["_universe_symbols"]
    )
    exempt_candidate_symbols = sorted(
        candidate_symbols & set(source_audit["preflight_exempt_symbols"])
    )
    if exempt_candidate_symbols:
        raise RuntimeError(
            "candidate artifact contains preflight-exempt symbols: "
            f"{exempt_candidate_symbols[:10]}"
        )
    candidates = [
        row
        for row in candidates
        if _parse_day(row["entry_day"], "entry_day", row["candidate_id"])
        >= dataset_start
        and _parse_day(row["entry_day"], "entry_day", row["candidate_id"])
        <= last_evaluation_end
    ]
    if not candidates:
        raise RuntimeError("no candidates remain inside the requested development window")
    integrity["dataset_candidate_ids_sha256"] = _sha256_bytes(
        "\n".join(str(row["candidate_id"]) for row in candidates).encode("utf-8")
    )

    boundaries = build_fold_boundaries(
        first_evaluation_start, last_evaluation_end, evaluation_months
    )
    prepared_folds: list[dict[str, Any]] = []
    for fold_index, (evaluation_start, evaluation_end) in enumerate(boundaries, start=1):
        pre_evaluation = [
            row
            for row in candidates
            if dataset_start
            <= _parse_day(row["entry_day"], "entry_day", row["candidate_id"])
            < evaluation_start
        ]
        training = [
            row
            for row in pre_evaluation
            if _parse_day(row["exit_day"], "exit_day", row["candidate_id"])
            < evaluation_start
        ]
        purged = [
            row
            for row in pre_evaluation
            if _parse_day(row["exit_day"], "exit_day", row["candidate_id"])
            >= evaluation_start
        ]
        evaluation = [
            row
            for row in candidates
            if evaluation_start
            <= _parse_day(row["entry_day"], "entry_day", row["candidate_id"])
            <= evaluation_end
        ]
        training.sort(key=_candidate_sort_key)
        purged.sort(key=_candidate_sort_key)
        evaluation.sort(key=_candidate_sort_key)
        leakage = [
            row["candidate_id"]
            for row in training
            if _parse_day(row["exit_day"], "exit_day", row["candidate_id"])
            >= evaluation_start
        ]
        if leakage:
            raise AssertionError(f"training label leakage in fold {fold_index}: {leakage[:5]}")
        prepared_folds.append(
            {
                "fold": fold_index,
                "evaluation_start": evaluation_start,
                "evaluation_end": evaluation_end,
                "training": training,
                "purged": purged,
                "evaluation": evaluation,
                "minimums_pass": (
                    len(training) >= min_train_candidates
                    and len(evaluation) >= min_evaluation_candidates
                ),
            }
        )

    output_dir.mkdir(parents=True, exist_ok=False)
    folds: list[dict[str, Any]] = []
    for prepared in prepared_folds:
        fold_name = f"fold_{prepared['fold']:02d}"
        fold_dir = output_dir / fold_name
        fold_dir.mkdir()
        artifacts = {
            "train": _write_jsonl_once(fold_dir / "train.jsonl", prepared["training"]),
            "purged_training_labels": _write_jsonl_once(
                fold_dir / "purged_training_labels.jsonl", prepared["purged"]
            ),
            "evaluation": _write_jsonl_once(
                fold_dir / "evaluation.jsonl", prepared["evaluation"]
            ),
        }
        folds.append(
            {
                "fold": prepared["fold"],
                "fold_name": fold_name,
                "training_policy": {
                    "entry_day": (
                        f">={dataset_start.isoformat()} and "
                        f"<{prepared['evaluation_start'].isoformat()}"
                    ),
                    "label_maturity": (
                        f"exit_day < {prepared['evaluation_start'].isoformat()}"
                    ),
                    "expanding_window": True,
                },
                "evaluation_window": {
                    "start": prepared["evaluation_start"].isoformat(),
                    "end": prepared["evaluation_end"].isoformat(),
                },
                "train": _row_summary(prepared["training"]),
                "purged_training_labels": _row_summary(prepared["purged"]),
                "evaluation": _row_summary(prepared["evaluation"]),
                "minimums": {
                    "train_candidates": min_train_candidates,
                    "evaluation_candidates": min_evaluation_candidates,
                    "passes": prepared["minimums_pass"],
                },
                "label_leakage_count": 0,
                "artifacts": artifacts,
            }
        )

    eligible_folds = sum(bool(fold["minimums"]["passes"]) for fold in folds)
    report = {
        "version": VERSION,
        "purpose": (
            "Create immutable development folds before any next-generation model is fitted."
        ),
        "dataset_status": "development_viewed_not_holdout",
        "holdout_used": bool(allow_holdout),
        "model_fitted": False,
        "hyperparameters_selected": False,
        "input": {
            "candidates": {
                "path": str(candidates_path),
                "sha256": _sha256_file(candidates_path),
                "raw_rows": len(raw_rows),
            },
            "source_report": {
                "path": str(source_report_path),
                "sha256": _sha256_file(source_report_path),
                "audit": {
                    key: value
                    for key, value in source_audit.items()
                    if not key.startswith("_")
                },
            },
            "index_data": {
                "path": str(index_data_path),
                "sha256": _sha256_file(index_data_path),
            },
            "qfq_history": _qfq_manifest(candidates, HISTORY_DIR),
            "builder": {
                "path": str(Path(__file__).resolve()),
                "sha256": _sha256_file(Path(__file__).resolve()),
            },
        },
        "integrity": integrity,
        "window": {
            "dataset_start": dataset_start.isoformat(),
            "first_evaluation_start": first_evaluation_start.isoformat(),
            "last_evaluation_end": last_evaluation_end.isoformat(),
            "evaluation_months": evaluation_months,
        },
        "candidate_summary": _row_summary(candidates),
        "fold_count": len(folds),
        "eligible_fold_count": eligible_folds,
        "all_fold_label_leakage_counts_zero": all(
            fold["label_leakage_count"] == 0 for fold in folds
        ),
        "all_folds_pass_minimums": eligible_folds == len(folds),
        "folds": folds,
        "next_stage_rule": (
            "Freeze one model specification before reading per-fold evaluation metrics; "
            "aggregate across all eligible folds and do not tune to individual folds."
        ),
        "production_eligible": False,
    }
    report_path = output_dir / "report.json"
    _write_json_once(report_path, report)
    report["report"] = str(report_path)
    report["report_sha256"] = _sha256_file(report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--index-data", type=Path, default=DEFAULT_INDEX_DATA)
    parser.add_argument(
        "--dataset-start", type=date.fromisoformat, default=DEFAULT_DATASET_START
    )
    parser.add_argument(
        "--first-evaluation-start",
        type=date.fromisoformat,
        default=DEFAULT_FIRST_EVALUATION_START,
    )
    parser.add_argument(
        "--last-evaluation-end",
        type=date.fromisoformat,
        default=DEFAULT_LAST_EVALUATION_END,
    )
    parser.add_argument("--evaluation-months", type=int, default=DEFAULT_EVALUATION_MONTHS)
    parser.add_argument(
        "--min-train-candidates", type=int, default=DEFAULT_MIN_TRAIN_CANDIDATES
    )
    parser.add_argument(
        "--min-evaluation-candidates",
        type=int,
        default=DEFAULT_MIN_EVALUATION_CANDIDATES,
    )
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()
    report = build_dataset(
        args.candidates,
        args.source_report,
        args.output_dir,
        args.index_data,
        dataset_start=args.dataset_start,
        first_evaluation_start=args.first_evaluation_start,
        last_evaluation_end=args.last_evaluation_end,
        evaluation_months=args.evaluation_months,
        min_train_candidates=args.min_train_candidates,
        min_evaluation_candidates=args.min_evaluation_candidates,
        allow_holdout=args.allow_holdout,
    )
    print(
        json.dumps(
            {
                "version": VERSION,
                "report": report["report"],
                "sha256": report["report_sha256"],
                "fold_count": report["fold_count"],
                "eligible_fold_count": report["eligible_fold_count"],
                "model_fitted": False,
                "production_eligible": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
