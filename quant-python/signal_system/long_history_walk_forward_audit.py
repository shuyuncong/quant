"""Read-only audit for frozen long-history candidates and walk-forward folds.

The auditor never writes a report or modifies an input.  It independently
checks candidate identity, local-cache manifests, fold membership, purge
boundaries, artifact hashes and optional cross-directory determinism.  Every
accepted path is development/viewed data; paths containing ``holdout`` are
always rejected and there is deliberately no override flag.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from candidate_integrity import candidate_id  # noqa: E402
from long_history_walk_forward_dataset import (  # noqa: E402
    STOCK_POOL_DATA_FAILURE_REASONS,
)


VERSION = "long_history_walk_forward_audit.v1"
FOLD_ARTIFACTS = ("train", "purged_training_labels", "evaluation")
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


class WalkForwardAuditError(RuntimeError):
    """Raised when a frozen development artifact fails a read-only check."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WalkForwardAuditError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise WalkForwardAuditError(f"Holdout path is blocked: {resolved}")
    return resolved


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _effective_qfq_counts(
    path: Path,
    history_bars: int,
    start: date,
    end: date,
) -> tuple[int, int]:
    try:
        frame = pd.read_pickle(path)
    except Exception as exc:
        raise WalkForwardAuditError(f"cannot read QFQ history: {path}: {exc}") from exc
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


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WalkForwardAuditError(f"cannot read JSON object: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WalkForwardAuditError(f"JSON object required: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise WalkForwardAuditError(
                        f"JSONL object required: {path}:{line_number}"
                    )
                rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise WalkForwardAuditError(f"cannot read JSONL: {path}: {exc}") from exc
    return rows


def _resolve_reference(value: Any, base_dir: Path, label: str) -> Path:
    _require(bool(value), f"missing path reference: {label}")
    path = Path(str(value))
    if not path.is_absolute():
        path = base_dir / path
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"missing input artifact {label}: {path}")
    return path


def _parse_day(value: Any, field: str, identifier: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise WalkForwardAuditError(
            f"invalid {field} for {identifier}: {value}"
        ) from exc


def _candidate_sort_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row["entry_day"]),
        str(row["signal_type"]),
        str(row["symbol"]),
        str(row["signal_day"]),
        str(row["candidate_id"]),
    )


def _normalized_universe_symbol(value: Any) -> str:
    raw = str(value).strip().upper()
    if raw.endswith((".SH", ".SZ", ".BJ")):
        raw = raw[:-3]
    if raw.startswith(("SH", "SZ", "BJ")):
        raw = raw[2:]
    _require(raw.isdigit() and 0 < len(raw) <= 6, f"invalid universe symbol: {value}")
    code = raw.zfill(6)
    _require(code != "000000", f"invalid universe symbol: {value}")
    return code


def _universe_source_symbols(path: Path) -> list[str]:
    try:
        if path.suffix.lower() in {".pkl", ".pickle"}:
            value = pd.read_pickle(path)
            _require(isinstance(value, pd.DataFrame), "universe pickle is not a DataFrame")
            _require(
                {"code", "name"}.issubset(value.columns),
                "universe DataFrame requires code and name columns",
            )
            raw_symbols = value["code"].tolist()
        elif path.suffix.lower() == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                for key in ("symbols", "codes", "universe", "stock_list"):
                    if isinstance(value.get(key), list):
                        value = value[key]
                        break
            _require(isinstance(value, list), "universe JSON does not contain a list")
            raw_symbols = [
                item.get("code", item.get("symbol", item.get("ts_code")))
                if isinstance(item, dict)
                else item
                for item in value
            ]
        else:
            raise WalkForwardAuditError("universe source must be PKL, PICKLE or JSON")
    except WalkForwardAuditError:
        raise
    except Exception as exc:
        raise WalkForwardAuditError(f"cannot read universe source: {path}: {exc}") from exc
    symbols = [_normalized_universe_symbol(value) for value in raw_symbols]
    _require(len(symbols) == len(set(symbols)), "universe source has duplicate symbols")
    return sorted(symbols)


def _load_unique_candidates(path: Path) -> list[dict[str, Any]]:
    rows = _load_jsonl(path)
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        copy = dict(row)
        try:
            identifier = candidate_id(copy)
        except Exception as exc:
            raise WalkForwardAuditError(f"invalid candidate identity: {exc}") from exc
        existing = copy.get("candidate_id")
        _require(
            existing is None or str(existing) == identifier,
            f"candidate_id mismatch: {existing} != {identifier}",
        )
        _require(identifier not in seen, f"duplicate candidate_id: {identifier}")
        seen.add(identifier)
        copy["candidate_id"] = identifier
        signal_day = _parse_day(copy.get("signal_day"), "signal_day", identifier)
        entry_day = _parse_day(copy.get("entry_day"), "entry_day", identifier)
        exit_day = _parse_day(copy.get("exit_day"), "exit_day", identifier)
        _require(signal_day < entry_day, f"entry_day must follow signal_day: {identifier}")
        _require(exit_day >= entry_day, f"exit_day precedes entry_day: {identifier}")
        prepared.append(copy)
    prepared.sort(key=_candidate_sort_key)
    return prepared


def _validated_candidate_symbols(
    rows: list[dict[str, Any]], universe_symbols: list[str]
) -> set[str]:
    symbols = [str(row.get("symbol", "")) for row in rows]
    noncanonical = sorted(
        {symbol for symbol in symbols if len(symbol) != 6 or not symbol.isdigit()}
    )
    _require(
        not noncanonical,
        "candidate artifact contains non-canonical symbols; "
        f"strict six-digit codes required: {noncanonical[:10]}",
    )
    outside_universe = sorted(set(symbols) - set(universe_symbols))
    _require(
        not outside_universe,
        "candidate artifact contains symbols outside frozen universe: "
        f"{outside_universe[:10]}",
    )
    return set(symbols)


def _manifest_from_paths(paths: list[Path]) -> dict[str, Any]:
    entries = [f"{path.name}|{_sha256_file(path)}" for path in sorted(paths)]
    return {
        "files": len(entries),
        "manifest_sha256": _sha256_text("\n".join(entries)),
    }


def _audit_stock_pool_manifest(
    report: dict[str, Any], report_path: Path
) -> dict[str, Any]:
    filters = report.get("filters")
    filters = filters if isinstance(filters, dict) else {}
    stock_pool = filters.get("stock_pool")
    stock_pool = stock_pool if isinstance(stock_pool, dict) else {}
    if not bool(stock_pool.get("enabled")):
        return {
            "enabled": False,
            "files": 0,
            "manifest_sha256": _sha256_text(""),
            "_path_sha256": {},
        }

    artifacts = report.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    manifest_path = _resolve_reference(
        artifacts.get("stock_pool_history_manifest"),
        report_path.parent,
        "stock_pool_history_manifest",
    )
    manifest_rows = _load_jsonl(manifest_path)
    paths: list[Path] = []
    seen_files: set[str] = set()
    for row in manifest_rows:
        cache_path = _resolve_reference(
            row.get("path"), manifest_path.parent, "stock_pool cache"
        )
        _require(
            str(row.get("file")) == cache_path.name,
            f"stock-pool cache filename mismatch: {cache_path}",
        )
        _require(cache_path.name not in seen_files, f"duplicate cache file: {cache_path}")
        seen_files.add(cache_path.name)
        actual_hash = _sha256_file(cache_path)
        _require(
            str(row.get("sha256", "")).lower() == actual_hash,
            f"stock-pool cache hash mismatch: {cache_path}",
        )
        paths.append(cache_path)

    actual = _manifest_from_paths(paths)
    stock_pool_history = filters.get("stock_pool_history")
    stock_pool_history = (
        stock_pool_history if isinstance(stock_pool_history, dict) else {}
    )
    recorded = stock_pool_history.get("local_cache_manifest")
    recorded = recorded if isinstance(recorded, dict) else {}
    _require(actual == recorded, "stock-pool aggregate manifest mismatch")
    _require(bool(paths), "enabled stock-pool has an empty cache manifest")
    return {
        "enabled": True,
        **actual,
        "artifact": str(manifest_path),
        "artifact_sha256": _sha256_file(manifest_path),
        "_path_sha256": {str(path): _sha256_file(path) for path in paths},
    }


def _audit_universe_manifest(
    report: dict[str, Any], report_path: Path, requested: int
) -> dict[str, Any]:
    artifacts = report.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    universe = report.get("universe")
    universe = universe if isinstance(universe, dict) else {}
    _require(
        universe.get("source") == "frozen_universe_file"
        and universe.get("strict") is True,
        "source report does not use a strict frozen universe",
    )
    manifest_path = _resolve_reference(
        artifacts.get("universe_manifest"), report_path.parent, "universe_manifest"
    )
    rows = _load_jsonl(manifest_path)
    symbols = [str(row.get("symbol", "")) for row in rows]
    _require(bool(symbols), "universe manifest is empty")
    _require(
        all(len(symbol) == 6 and symbol.isdigit() for symbol in symbols),
        "universe manifest contains an invalid symbol",
    )
    _require(symbols == sorted(symbols), "universe manifest is not sorted")
    _require(len(symbols) == len(set(symbols)), "universe manifest has duplicate symbols")
    _require(len(symbols) == requested, "universe manifest count differs from requested")
    manifest_sha256 = _sha256_text("\n".join(symbols))
    _require(
        universe.get("selected_symbols") == len(symbols),
        "recorded universe count mismatch",
    )
    _require(
        universe.get("symbol_manifest_sha256") == manifest_sha256,
        "universe symbol manifest hash mismatch",
    )
    source_path = _resolve_reference(
        universe.get("source_path"), report_path.parent, "universe source"
    )
    source_sha256 = _sha256_file(source_path)
    _require(
        universe.get("source_sha256") == source_sha256,
        "universe source file hash mismatch",
    )
    _require(
        universe.get("expected_sha256") == source_sha256,
        "universe expected SHA256 is missing or mismatched",
    )
    _require(
        _universe_source_symbols(source_path) == symbols,
        "universe source symbols differ from manifest",
    )
    return {
        "symbols": len(symbols),
        "symbol_manifest_sha256": manifest_sha256,
        "artifact": str(manifest_path),
        "artifact_sha256": _sha256_file(manifest_path),
        "source": str(source_path),
        "source_sha256": source_sha256,
        "_symbols": symbols,
    }


def _audit_full_qfq_manifest(
    report: dict[str, Any], report_path: Path, universe: dict[str, Any]
) -> dict[str, Any]:
    artifacts = report.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    frozen_inputs = report.get("frozen_inputs")
    frozen_inputs = frozen_inputs if isinstance(frozen_inputs, dict) else {}
    recorded = frozen_inputs.get("qfq_history")
    recorded = recorded if isinstance(recorded, dict) else {}
    manifest_path = _resolve_reference(
        artifacts.get("qfq_history_manifest"),
        report_path.parent,
        "qfq_history_manifest",
    )
    rows = _load_jsonl(manifest_path)
    symbols: list[str] = []
    entries: list[str] = []
    for row in rows:
        symbol = _normalized_universe_symbol(row.get("symbol"))
        path = _resolve_reference(row.get("path"), manifest_path.parent, "QFQ history")
        digest = _sha256_file(path)
        _require(row.get("exists") is True, f"QFQ manifest marks missing: {symbol}")
        _require(row.get("file") == path.name, f"QFQ filename mismatch: {symbol}")
        _require(path.name == f"{symbol}_qfq.pkl", f"QFQ symbol/path mismatch: {symbol}")
        _require(row.get("sha256") == digest, f"QFQ hash mismatch: {symbol}")
        symbols.append(symbol)
        entries.append(f"{symbol}|{digest}")
    _require(symbols == sorted(symbols), "QFQ manifest is not sorted")
    _require(len(symbols) == len(set(symbols)), "QFQ manifest has duplicate symbols")
    _require(symbols == _universe_source_symbols(Path(universe["source"])), "QFQ manifest does not cover the full universe")
    aggregate = _sha256_text("\n".join(entries))
    _require(recorded.get("symbols") == len(symbols), "QFQ recorded symbol count mismatch")
    _require(recorded.get("missing_symbols") == 0, "QFQ recorded missing symbols")
    _require(recorded.get("manifest_sha256") == aggregate, "QFQ aggregate manifest mismatch")
    return {
        "symbols": len(symbols),
        "manifest_sha256": aggregate,
        "artifact": str(manifest_path),
        "artifact_sha256": _sha256_file(manifest_path),
        "_symbols": symbols,
        "_symbol_sha256": {
            symbol: entry.split("|", 1)[1]
            for symbol, entry in zip(symbols, entries)
        },
        "_symbol_path": {
            symbol: str(
                _resolve_reference(
                    row.get("path"), manifest_path.parent, "QFQ history"
                )
            )
            for symbol, row in zip(symbols, rows)
        },
    }


def _audit_frozen_source_inputs(
    report: dict[str, Any], report_path: Path, universe: dict[str, Any]
) -> dict[str, Any]:
    frozen_inputs = report.get("frozen_inputs")
    frozen_inputs = frozen_inputs if isinstance(frozen_inputs, dict) else {}
    result: dict[str, Any] = {}
    for name in ("index_data", "backtest_engine", "signal_engine"):
        record = frozen_inputs.get(name)
        record = record if isinstance(record, dict) else {}
        path = _resolve_reference(record.get("path"), report_path.parent, name)
        digest = _sha256_file(path)
        _require(record.get("sha256") == digest, f"{name} hash mismatch")
        result[name] = {"path": str(path), "sha256": digest}
    result["qfq_history"] = _audit_full_qfq_manifest(report, report_path, universe)
    return result


def _audit_preflight_attestation(
    report: dict[str, Any],
    report_path: Path,
    universe: dict[str, Any],
    frozen_inputs: dict[str, Any],
    requested: int,
) -> dict[str, Any]:
    attestation = report.get("preflight_attestation")
    _require(isinstance(attestation, dict), "missing preflight attestation")
    artifacts = report.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    artifact_path = _resolve_reference(
        attestation.get("artifact"), report_path.parent, "preflight attestation"
    )
    recorded_artifact_path = _resolve_reference(
        artifacts.get("preflight"), report_path.parent, "preflight artifact"
    )
    _require(
        artifact_path == recorded_artifact_path,
        "preflight artifact path mismatch",
    )
    artifact_sha256 = _sha256_file(artifact_path)
    _require(
        attestation.get("artifact_sha256") == artifact_sha256
        and attestation.get("full_result_sha256") == artifact_sha256,
        "preflight artifact hash mismatch",
    )
    preflight = _load_json(artifact_path)
    checks = preflight.get("checks")
    summary = preflight.get("summary")
    policy = preflight.get("policy")
    effective_run = preflight.get("effective_run")
    inputs = preflight.get("inputs")
    _require(
        preflight.get("version") == "long_history_preflight.v1",
        "invalid preflight attestation version",
    )
    _require(
        preflight.get("mode") == "read_only_preflight"
        and preflight.get("passes_preflight") is True
        and preflight.get("production_eligible") is False,
        "source preflight did not pass",
    )
    _require(
        isinstance(checks, dict)
        and set(checks) == PREFLIGHT_REQUIRED_CHECKS
        and all(value is True for value in checks.values()),
        "preflight attestation contains failed checks",
    )
    _require(isinstance(summary, dict), "missing preflight attestation summary")
    _require(summary.get("symbols") == requested, "preflight universe count mismatch")
    _require(
        summary.get("blocking_symbols") == 0
        and summary.get("blocking_by_reason") == {},
        "preflight attestation contains blocking data issues",
    )
    _require(
        policy
        == {
            "holdout_used": False,
            "network_used": False,
            "database_used": False,
            "writes_performed": False,
        },
        "invalid preflight read-only policy",
    )
    filters = report.get("filters")
    filters = filters if isinstance(filters, dict) else {}
    window = report.get("window")
    window = window if isinstance(window, dict) else {}
    experiment = report.get("experiment")
    experiment = experiment if isinstance(experiment, dict) else {}
    stock_pool = filters.get("stock_pool")
    stock_pool = stock_pool if isinstance(stock_pool, dict) else {}
    _require(isinstance(effective_run, dict), "missing preflight effective run")
    _require(
        effective_run.get("start") == window.get("start")
        and effective_run.get("end") == window.get("end")
        and effective_run.get("history_bars") == report.get("history_bars_requested")
        and effective_run.get("adjustment") == report.get("data_adjustment")
        and effective_run.get("local_data_only")
        is bool(filters.get("local_data_only"))
        and effective_run.get("fetch_missing_adjusted")
        is bool(filters.get("fetch_missing_adjusted"))
        and effective_run.get("allow_incomplete")
        is bool(report.get("allow_incomplete"))
        and effective_run.get("dataset_role") == experiment.get("dataset_role")
        and effective_run.get("stock_pool") == stock_pool,
        "preflight effective run differs from source report",
    )
    _require(isinstance(inputs, dict), "missing preflight inputs")
    preflight_universe = inputs.get("universe")
    preflight_index = inputs.get("index_data")
    preflight_config = inputs.get("config")
    _require(
        all(
            isinstance(value, dict)
            for value in (preflight_universe, preflight_index, preflight_config)
        ),
        "invalid preflight input records",
    )
    _require(
        preflight_universe.get("source_sha256") == universe.get("source_sha256")
        and preflight_universe.get("symbol_manifest_sha256")
        == universe.get("symbol_manifest_sha256")
        and preflight_universe.get("source_symbols") == requested
        and preflight_universe.get("selected_symbols") == requested,
        "preflight universe hash mismatch",
    )
    frozen_index = frozen_inputs.get("index_data")
    frozen_index = frozen_index if isinstance(frozen_index, dict) else {}
    preflight_index_path = _resolve_reference(
        preflight_index.get("path"), report_path.parent, "preflight index"
    )
    _require(
        preflight_index_path == Path(str(frozen_index.get("path"))).resolve()
        and preflight_index.get("sha256") == frozen_index.get("sha256"),
        "preflight index hash mismatch",
    )
    config_path = _resolve_reference(
        report.get("config_source"), report_path.parent, "source config"
    )
    preflight_config_path = _resolve_reference(
        preflight_config.get("path"), report_path.parent, "preflight config"
    )
    _require(
        preflight_config_path == config_path
        and preflight_config.get("sha256") == _sha256_file(config_path),
        "preflight config hash mismatch",
    )
    qfq = frozen_inputs.get("qfq_history")
    qfq = qfq if isinstance(qfq, dict) else {}
    expected_symbols = qfq.get("_symbols")
    expected_hashes = qfq.get("_symbol_sha256")
    expected_paths = qfq.get("_symbol_path")
    symbol_records = preflight.get("symbols")
    _require(isinstance(symbol_records, list), "missing preflight symbol records")
    _require(
        [str(row.get("symbol", "")) for row in symbol_records] == expected_symbols,
        "preflight symbol records differ from frozen universe",
    )
    preflight_start = _parse_day(
        effective_run.get("start"), "preflight start", "preflight"
    )
    preflight_end = _parse_day(
        effective_run.get("end"), "preflight end", "preflight"
    )
    preflight_history_bars = effective_run.get("history_bars")
    _require(
        isinstance(preflight_history_bars, int) and preflight_history_bars > 0,
        "invalid preflight history_bars",
    )
    preflight_stock_pool_files: dict[str, str] = {}
    preflight_exempt_symbols: set[str] = set()
    for row in symbol_records:
        symbol = str(row.get("symbol", ""))
        qfq_record = row.get("qfq")
        _require(isinstance(qfq_record, dict), f"missing preflight QFQ record: {symbol}")
        _require(row.get("blocking_issues") == {}, f"preflight symbol blocked: {symbol}")
        qfq_path = _resolve_reference(
            qfq_record.get("path"), artifact_path.parent, f"preflight QFQ {symbol}"
        )
        _require(
            qfq_record.get("sha256") == expected_hashes.get(symbol)
            and str(qfq_path) == expected_paths.get(symbol)
            and qfq_path.name == f"{symbol}_qfq.pkl",
            f"preflight QFQ hash mismatch: {symbol}",
        )
        actual_bars, window_bars = _effective_qfq_counts(
            qfq_path,
            preflight_history_bars,
            preflight_start,
            preflight_end,
        )
        _require(
            qfq_record.get("bars") == actual_bars,
            f"preflight QFQ bar count mismatch: {symbol}",
        )
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
        _require(
            (
                (nonblocking_issues.get("insufficient_history") == 1)
                is (actual_bars < 60)
            )
            and (
                (nonblocking_issues.get("no_history_in_window") == 1)
                is (actual_bars >= 60 and window_bars == 0)
            ),
            f"invalid preflight stock-pool exemption evidence: {symbol}",
        )
        if stock_pool_exempt:
            preflight_exempt_symbols.add(symbol)
        if stock_pool.get("enabled"):
            stock_pool_record = row.get("stock_pool")
            _require(
                isinstance(stock_pool_record, dict),
                f"missing preflight stock-pool record: {symbol}",
            )
            if stock_pool_exempt:
                _require(
                    stock_pool_record.get("required") is True
                    and stock_pool_record.get("path") in (None, "")
                    and stock_pool_record.get("sha256") in (None, "")
                    and stock_pool_record.get("error") in (None, ""),
                    f"invalid preflight stock-pool exemption: {symbol}",
                )
                continue
            stock_pool_path = _resolve_reference(
                stock_pool_record.get("path"),
                artifact_path.parent,
                f"preflight stock-pool {symbol}",
            )
            _require(
                stock_pool_record.get("sha256") == _sha256_file(stock_pool_path),
                f"preflight stock-pool hash mismatch: {symbol}",
            )
            preflight_stock_pool_files[str(stock_pool_path)] = str(
                stock_pool_record["sha256"]
            )
    _require(
        attestation.get("version") == preflight.get("version")
        and attestation.get("passes_preflight") is True
        and attestation.get("checks") == checks
        and attestation.get("summary") == summary
        and attestation.get("universe_source_sha256")
        == universe.get("source_sha256")
        and attestation.get("universe_symbol_manifest_sha256")
        == universe.get("symbol_manifest_sha256")
        and attestation.get("index_sha256") == frozen_index.get("sha256"),
        "preflight attestation summary mismatch",
    )
    return {
        "version": preflight["version"],
        "passes_preflight": True,
        "artifact": str(artifact_path),
        "artifact_sha256": artifact_sha256,
        "_stock_pool_path_sha256": preflight_stock_pool_files,
        "_exempt_symbols": preflight_exempt_symbols,
    }


def audit_source_report(report_path: Path) -> dict[str, Any]:
    report_path = _guard_development_path(report_path)
    _require(report_path.exists(), f"source report does not exist: {report_path}")
    report = _load_json(report_path)
    filters = report.get("filters")
    filters = filters if isinstance(filters, dict) else {}
    signal = report.get("signal")
    signal = signal if isinstance(signal, dict) else {}
    skipped = signal.get("skipped")
    skipped = skipped if isinstance(skipped, dict) else {}
    summary = signal.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    artifacts = report.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}

    _require(str(report.get("data_adjustment", "")).lower() == "qfq", "source is not QFQ")
    _require(bool(filters.get("local_data_only")), "local_data_only is not true")
    _require(
        not bool(filters.get("fetch_missing_adjusted")),
        "fetch_missing_adjusted is not false",
    )
    _require(not bool(report.get("allow_incomplete")), "allow_incomplete is not false")
    _require(report.get("mode") in {"signal", "both"}, "source report lacks signal mode")
    experiment = report.get("experiment")
    experiment = experiment if isinstance(experiment, dict) else {}
    _require(experiment.get("dataset_role") == "full", "source dataset_role is not full")
    requested = report.get("symbols_requested")
    succeeded = report.get("symbols_succeeded")
    failed = report.get("symbols_failed")
    _require(isinstance(requested, int) and requested > 0, "invalid symbols_requested")
    _require(succeeded == requested, "not every requested symbol completed analysis")
    _require(failed == 0, "source report contains symbol failures")
    stock_pool = filters.get("stock_pool")
    stock_pool = stock_pool if isinstance(stock_pool, dict) else {}
    _require(
        not bool(stock_pool.get("enabled"))
        or stock_pool.get("missing_data_policy") == "reject",
        "stock-pool missing-data policy is not reject",
    )
    _require(
        all(skipped.get(reason, 0) == 0 for reason in STOCK_POOL_DATA_FAILURE_REASONS),
        "source report contains stock-pool data failures",
    )

    report_artifact = artifacts.get("report")
    if report_artifact:
        recorded_report_path = _resolve_reference(
            report_artifact, report_path.parent, "report"
        )
        _require(recorded_report_path == report_path, "source report self-path mismatch")

    candidates_path = _resolve_reference(
        artifacts.get("signal_trades"), report_path.parent, "signal_trades"
    )
    candidates = _load_unique_candidates(candidates_path)
    _require(bool(candidates), "source candidate artifact is empty")
    _require(summary.get("count") == len(candidates), "signal count does not match JSONL")
    candidate_manifest = _sha256_text(
        "\n".join(str(row["candidate_id"]) for row in candidates)
    )
    universe_manifest = _audit_universe_manifest(report, report_path, requested)
    candidate_symbols = _validated_candidate_symbols(
        candidates, universe_manifest["_symbols"]
    )
    raw_universe = report.get("universe")
    raw_universe = raw_universe if isinstance(raw_universe, dict) else {}
    _require(
        raw_universe.get("source_symbols")
        == raw_universe.get("selected_symbols")
        == requested,
        "frozen universe was limited or count-mismatched",
    )
    frozen_inputs = _audit_frozen_source_inputs(
        report, report_path, universe_manifest
    )
    preflight_attestation = _audit_preflight_attestation(
        report, report_path, universe_manifest, frozen_inputs, requested
    )
    exempt_candidate_symbols = sorted(
        candidate_symbols & preflight_attestation["_exempt_symbols"]
    )
    _require(
        not exempt_candidate_symbols,
        "candidate artifact contains preflight-exempt symbols: "
        f"{exempt_candidate_symbols[:10]}",
    )
    stock_pool_manifest = _audit_stock_pool_manifest(report, report_path)
    _require(
        all(
            preflight_attestation["_stock_pool_path_sha256"].get(path) == digest
            for path, digest in stock_pool_manifest["_path_sha256"].items()
        ),
        "formal stock-pool manifest is not covered by preflight inputs",
    )

    artifact_hashes: dict[str, str] = {}
    for name, raw_path in sorted(artifacts.items()):
        if name == "report":
            continue
        path = _resolve_reference(raw_path, report_path.parent, name)
        artifact_hashes[name] = _sha256_file(path)

    return {
        "report": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "symbols_requested": requested,
        "candidate_count": len(candidates),
        "candidate_ids_sha256": candidate_manifest,
        "candidate_artifact": str(candidates_path),
        "candidate_artifact_sha256": _sha256_file(candidates_path),
        "universe": universe_manifest,
        "frozen_inputs": frozen_inputs,
        "preflight_attestation": preflight_attestation,
        "stock_pool_cache": stock_pool_manifest,
        "artifact_sha256": artifact_hashes,
        "checks_passed": True,
        "_report_value": report,
        "_candidates": candidates,
    }


def _add_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _expected_boundaries(first: date, last: date, months: int) -> list[tuple[date, date]]:
    _require(months > 0, "evaluation_months must be positive")
    boundaries: list[tuple[date, date]] = []
    start = first
    while start <= last:
        end = min(_add_months(start, months) - timedelta(days=1), last)
        boundaries.append((start, end))
        start = end + timedelta(days=1)
    return boundaries


def _artifact_rows(
    record: Any,
    report_path: Path,
    label: str,
) -> tuple[list[dict[str, Any]], Path]:
    _require(isinstance(record, dict), f"missing fold artifact record: {label}")
    path = _resolve_reference(record.get("path"), report_path.parent, label)
    rows = _load_jsonl(path)
    _require(record.get("rows") == len(rows), f"fold artifact row mismatch: {label}")
    _require(
        str(record.get("sha256", "")).lower() == _sha256_file(path),
        f"fold artifact hash mismatch: {label}",
    )
    return rows, path


def audit_fold_report(
    fold_report_path: Path,
    source_audit: dict[str, Any],
) -> dict[str, Any]:
    fold_report_path = _guard_development_path(fold_report_path)
    _require(fold_report_path.exists(), f"fold report does not exist: {fold_report_path}")
    report = _load_json(fold_report_path)
    _require(
        report.get("dataset_status") == "development_viewed_not_holdout",
        "invalid dataset_status",
    )
    _require(report.get("holdout_used") is False, "fold report used holdout")
    _require(report.get("model_fitted") is False, "fold builder fitted a model")
    _require(
        report.get("hyperparameters_selected") is False,
        "fold builder selected hyperparameters",
    )
    _require(report.get("production_eligible") is False, "fold report is production eligible")

    input_value = report.get("input")
    input_value = input_value if isinstance(input_value, dict) else {}
    candidates_record = input_value.get("candidates")
    source_record = input_value.get("source_report")
    index_record = input_value.get("index_data")
    builder_record = input_value.get("builder")
    _require(isinstance(candidates_record, dict), "missing candidate input record")
    _require(isinstance(source_record, dict), "missing source-report input record")
    _require(isinstance(index_record, dict), "missing index input record")
    _require(isinstance(builder_record, dict), "missing builder input record")

    source_candidate_path = Path(str(source_audit["candidate_artifact"])).resolve()
    recorded_candidate_path = _resolve_reference(
        candidates_record.get("path"), fold_report_path.parent, "fold candidates"
    )
    _require(
        recorded_candidate_path == source_candidate_path,
        "fold report does not reference audited candidates",
    )
    _require(
        candidates_record.get("sha256") == source_audit["candidate_artifact_sha256"],
        "fold candidate input hash mismatch",
    )
    source_report_path = _resolve_reference(
        source_record.get("path"), fold_report_path.parent, "fold source report"
    )
    _require(
        source_report_path == Path(str(source_audit["report"])).resolve(),
        "fold report references a different source report",
    )
    _require(
        source_record.get("sha256") == source_audit["report_sha256"],
        "fold source-report hash mismatch",
    )
    for label, record in (("index_data", index_record), ("builder", builder_record)):
        path = _resolve_reference(record.get("path"), fold_report_path.parent, label)
        _require(
            str(record.get("sha256", "")).lower() == _sha256_file(path),
            f"{label} hash mismatch",
        )
        if label == "index_data":
            frozen_index = source_audit["frozen_inputs"]["index_data"]
            _require(
                path == Path(str(frozen_index["path"])).resolve()
                and record.get("sha256") == frozen_index["sha256"],
                "fold index input differs from source backtest index",
            )

    window = report.get("window")
    window = window if isinstance(window, dict) else {}
    dataset_start = _parse_day(window.get("dataset_start"), "dataset_start", "report")
    first_evaluation = _parse_day(
        window.get("first_evaluation_start"), "first_evaluation_start", "report"
    )
    last_evaluation = _parse_day(
        window.get("last_evaluation_end"), "last_evaluation_end", "report"
    )
    months = window.get("evaluation_months")
    _require(isinstance(months, int) and months > 0, "invalid evaluation_months")
    boundaries = _expected_boundaries(first_evaluation, last_evaluation, months)
    source_window = source_audit["_report_value"].get("window")
    source_window = source_window if isinstance(source_window, dict) else {}
    source_start = _parse_day(source_window.get("start"), "source start", "report")
    source_end = _parse_day(source_window.get("end"), "source end", "report")
    _require(source_start <= dataset_start, "source window starts after dataset_start")
    _require(
        source_end >= last_evaluation,
        "source window ends before last evaluation interval",
    )

    candidates = [
        row
        for row in source_audit["_candidates"]
        if dataset_start
        <= _parse_day(row["entry_day"], "entry_day", row["candidate_id"])
        <= last_evaluation
    ]
    _require(bool(candidates), "no candidates in fold window")
    dataset_manifest = _sha256_text(
        "\n".join(str(row["candidate_id"]) for row in candidates)
    )
    integrity = report.get("integrity")
    integrity = integrity if isinstance(integrity, dict) else {}
    _require(
        integrity.get("dataset_candidate_ids_sha256") == dataset_manifest,
        "dataset candidate manifest mismatch",
    )
    candidate_summary = report.get("candidate_summary")
    candidate_summary = candidate_summary if isinstance(candidate_summary, dict) else {}
    _require(
        candidate_summary.get("candidates") == len(candidates),
        "dataset candidate count mismatch",
    )

    qfq = input_value.get("qfq_history")
    qfq = qfq if isinstance(qfq, dict) else {}
    history_dir = _guard_development_path(Path(str(qfq.get("history_dir", ""))))
    qfq_entries: list[str] = []
    for symbol in sorted({str(row["symbol"]) for row in candidates}):
        path = history_dir / f"{symbol}_qfq.pkl"
        _require(path.exists(), f"missing QFQ cache during audit: {path}")
        qfq_entries.append(f"{symbol}|{_sha256_file(path)}")
    _require(qfq.get("symbols") == len(qfq_entries), "QFQ symbol count mismatch")
    _require(qfq.get("missing_symbols") == 0, "QFQ report contains missing symbols")
    _require(
        qfq.get("manifest_sha256") == _sha256_text("\n".join(qfq_entries)),
        "QFQ manifest mismatch",
    )

    folds = report.get("folds")
    _require(isinstance(folds, list), "fold list missing")
    _require(len(folds) == len(boundaries), "fold count does not match boundaries")
    _require(report.get("fold_count") == len(folds), "reported fold_count mismatch")
    previous_train_ids: set[str] = set()
    evaluation_ids: set[str] = set()
    eligible_count = 0
    artifact_hashes: dict[str, str] = {}
    fold_counts: list[dict[str, Any]] = []

    for index, ((evaluation_start, evaluation_end), fold) in enumerate(
        zip(boundaries, folds, strict=True), start=1
    ):
        _require(isinstance(fold, dict), f"invalid fold record: {index}")
        _require(fold.get("fold") == index, f"fold number mismatch: {index}")
        evaluation_window = fold.get("evaluation_window")
        evaluation_window = (
            evaluation_window if isinstance(evaluation_window, dict) else {}
        )
        _require(
            evaluation_window
            == {"start": evaluation_start.isoformat(), "end": evaluation_end.isoformat()},
            f"evaluation boundary mismatch: fold {index}",
        )

        expected_train = [
            row
            for row in candidates
            if dataset_start
            <= _parse_day(row["entry_day"], "entry_day", row["candidate_id"])
            < evaluation_start
            and _parse_day(row["exit_day"], "exit_day", row["candidate_id"])
            < evaluation_start
        ]
        expected_purged = [
            row
            for row in candidates
            if dataset_start
            <= _parse_day(row["entry_day"], "entry_day", row["candidate_id"])
            < evaluation_start
            and _parse_day(row["exit_day"], "exit_day", row["candidate_id"])
            >= evaluation_start
        ]
        expected_evaluation = [
            row
            for row in candidates
            if evaluation_start
            <= _parse_day(row["entry_day"], "entry_day", row["candidate_id"])
            <= evaluation_end
        ]
        artifacts = fold.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        actual_by_name: dict[str, list[dict[str, Any]]] = {}
        for artifact_name in FOLD_ARTIFACTS:
            rows, path = _artifact_rows(
                artifacts.get(artifact_name),
                fold_report_path,
                f"fold_{index:02d}.{artifact_name}",
            )
            actual_by_name[artifact_name] = rows
            artifact_hashes[f"fold_{index:02d}/{artifact_name}.jsonl"] = _sha256_file(path)

        for name, expected in (
            ("train", expected_train),
            ("purged_training_labels", expected_purged),
            ("evaluation", expected_evaluation),
        ):
            actual = actual_by_name[name]
            _require(
                [_canonical_json(row) for row in actual]
                == [_canonical_json(row) for row in expected],
                f"independent membership mismatch: fold {index} {name}",
            )
            summary = fold.get(name)
            summary = summary if isinstance(summary, dict) else {}
            _require(
                summary.get("candidates") == len(expected),
                f"summary count mismatch: fold {index} {name}",
            )

        train_ids = {str(row["candidate_id"]) for row in expected_train}
        current_eval_ids = {str(row["candidate_id"]) for row in expected_evaluation}
        _require(
            previous_train_ids <= train_ids,
            f"training window is not expanding: fold {index}",
        )
        _require(
            not evaluation_ids.intersection(current_eval_ids),
            f"evaluation candidate repeated across folds: fold {index}",
        )
        previous_train_ids = train_ids
        evaluation_ids.update(current_eval_ids)
        _require(fold.get("label_leakage_count") == 0, f"leakage flag: fold {index}")

        minimums = fold.get("minimums")
        minimums = minimums if isinstance(minimums, dict) else {}
        train_minimum = minimums.get("train_candidates")
        evaluation_minimum = minimums.get("evaluation_candidates")
        _require(
            isinstance(train_minimum, int)
            and not isinstance(train_minimum, bool)
            and train_minimum > 0,
            f"invalid train minimum: fold {index}",
        )
        _require(
            isinstance(evaluation_minimum, int)
            and not isinstance(evaluation_minimum, bool)
            and evaluation_minimum > 0,
            f"invalid evaluation minimum: fold {index}",
        )
        minimum_pass = (
            len(expected_train) >= train_minimum
            and len(expected_evaluation) >= evaluation_minimum
        )
        _require(
            minimums.get("passes") is minimum_pass,
            f"minimum-sample flag mismatch: fold {index}",
        )
        eligible_count += int(minimum_pass)
        fold_counts.append(
            {
                "fold": index,
                "train": len(expected_train),
                "purged": len(expected_purged),
                "evaluation": len(expected_evaluation),
                "minimums_pass": minimum_pass,
            }
        )

    expected_evaluation_ids = {
        str(row["candidate_id"])
        for row in candidates
        if first_evaluation
        <= _parse_day(row["entry_day"], "entry_day", row["candidate_id"])
        <= last_evaluation
    }
    _require(
        evaluation_ids == expected_evaluation_ids,
        "evaluation folds do not exactly cover the evaluation window",
    )
    _require(report.get("eligible_fold_count") == eligible_count, "eligible fold mismatch")
    _require(
        report.get("all_fold_label_leakage_counts_zero") is True,
        "aggregate leakage flag is false",
    )
    _require(
        report.get("all_folds_pass_minimums") is (eligible_count == len(folds)),
        "aggregate minimum flag mismatch",
    )
    return {
        "report": str(fold_report_path),
        "report_sha256": _sha256_file(fold_report_path),
        "fold_count": len(folds),
        "eligible_fold_count": eligible_count,
        "dataset_candidates": len(candidates),
        "dataset_candidate_ids_sha256": dataset_manifest,
        "qfq_manifest_sha256": qfq.get("manifest_sha256"),
        "fold_counts": fold_counts,
        "artifact_sha256": artifact_hashes,
        "checks_passed": True,
        "_report_value": report,
    }


def _normalize_run_paths(value: Any, run_root: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_run_paths(item, run_root)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_normalize_run_paths(item, run_root) for item in value]
    if isinstance(value, str):
        try:
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(run_root):
                return str(Path("<RUN_ROOT>") / path.relative_to(run_root))
        except (OSError, ValueError):
            pass
    return value


def _fold_semantic_value(report: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(report))

    def strip_paths(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: strip_paths(child)
                for key, child in sorted(item.items())
                if key not in {"path", "source_path", "history_dir"}
            }
        if isinstance(item, list):
            return [strip_paths(child) for child in item]
        return item

    value = strip_paths(value)
    source_record = value.get("input", {}).get("source_report", {})
    if isinstance(source_record, dict):
        source_record.pop("sha256", None)
    return value


def compare_runs(
    primary_source: dict[str, Any],
    primary_folds: dict[str, Any],
    verify_source: dict[str, Any],
    verify_folds: dict[str, Any],
) -> dict[str, Any]:
    primary_source_artifacts = primary_source["artifact_sha256"]
    verify_source_artifacts = verify_source["artifact_sha256"]
    _require(
        set(primary_source_artifacts) == set(verify_source_artifacts),
        "source artifact sets differ across runs",
    )
    source_artifacts_equal = {
        name: primary_source_artifacts[name] == verify_source_artifacts[name]
        for name in sorted(primary_source_artifacts)
    }
    _require(all(source_artifacts_equal.values()), "source artifacts differ across runs")

    primary_fold_artifacts = primary_folds["artifact_sha256"]
    verify_fold_artifacts = verify_folds["artifact_sha256"]
    _require(
        set(primary_fold_artifacts) == set(verify_fold_artifacts),
        "fold artifact sets differ across runs",
    )
    fold_artifacts_equal = {
        name: primary_fold_artifacts[name] == verify_fold_artifacts[name]
        for name in sorted(primary_fold_artifacts)
    }
    _require(all(fold_artifacts_equal.values()), "fold artifacts differ across runs")

    primary_source_value = _normalize_run_paths(
        primary_source["_report_value"], Path(primary_source["report"]).parent
    )
    verify_source_value = _normalize_run_paths(
        verify_source["_report_value"], Path(verify_source["report"]).parent
    )
    source_structure_equal = primary_source_value == verify_source_value
    _require(source_structure_equal, "normalized source reports differ across runs")
    fold_structure_equal = _fold_semantic_value(
        primary_folds["_report_value"]
    ) == _fold_semantic_value(verify_folds["_report_value"])
    _require(fold_structure_equal, "normalized fold reports differ across runs")

    return {
        "source_artifacts_byte_identical": source_artifacts_equal,
        "fold_artifacts_byte_identical": fold_artifacts_equal,
        "normalized_source_report_equal": source_structure_equal,
        "normalized_fold_report_equal": fold_structure_equal,
        "checks_passed": True,
    }


def _public_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public_result(item)
            for key, item in value.items()
            if not key.startswith("_")
        }
    if isinstance(value, list):
        return [_public_result(item) for item in value]
    return value


def run_audit(
    source_report: Path,
    fold_report: Path,
    *,
    verify_source_report: Path | None = None,
    verify_fold_report: Path | None = None,
) -> dict[str, Any]:
    _require(
        (verify_source_report is None) == (verify_fold_report is None),
        "verify source and fold reports must be supplied together",
    )
    primary_source = audit_source_report(source_report)
    primary_folds = audit_fold_report(fold_report, primary_source)
    result: dict[str, Any] = {
        "version": VERSION,
        "audit_mode": "read_only",
        "holdout_used": False,
        "primary": {
            "source": primary_source,
            "folds": primary_folds,
        },
        "determinism": {"checked": False},
        "model_fitted": False,
        "production_eligible": False,
        "passes_audit": True,
    }
    if verify_source_report is not None and verify_fold_report is not None:
        verify_source = audit_source_report(verify_source_report)
        verify_folds = audit_fold_report(verify_fold_report, verify_source)
        result["verify"] = {"source": verify_source, "folds": verify_folds}
        result["determinism"] = {
            "checked": True,
            **compare_runs(
                primary_source,
                primary_folds,
                verify_source,
                verify_folds,
            ),
        }
    return _public_result(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--fold-report", type=Path, required=True)
    parser.add_argument("--verify-source-report", type=Path)
    parser.add_argument("--verify-fold-report", type=Path)
    args = parser.parse_args()
    try:
        result = run_audit(
            args.source_report,
            args.fold_report,
            verify_source_report=args.verify_source_report,
            verify_fold_report=args.verify_fold_report,
        )
    except WalkForwardAuditError as exc:
        print(json.dumps({"version": VERSION, "passes_audit": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
