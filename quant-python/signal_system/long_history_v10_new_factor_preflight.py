"""Build an outcome-free v10 snapshot of twelve new causal factors.

The preflight uses only frozen local v5 histories and the audited v7 candidate
identity.  It does not fit a model, inspect candidate outcomes, select factors,
access a database/network, or authorize a subsequent training stage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import long_history_v7_factor_preflight as v7preflight
import long_history_v7_factor_preflight_audit as v7audit

VERSION = "long_history_v10_new_factor_preflight.v1"
DATASET_STATUS = "development_viewed_not_holdout"
AUDIT_CODE = BASE_DIR / "long_history_v10_new_factor_preflight_audit.py"
V7_PREFLIGHT_CODE = Path(v7preflight.__file__).resolve()
V7_PREFLIGHT_CODE_SHA256 = (
    "a9f03d39c255b7b157befaf8de7daf50c92bd18dd9c8c8707d4a321d5ca5cc54"
)
V7_AUDIT_CODE = Path(v7audit.__file__).resolve()
V7_AUDIT_CODE_SHA256 = (
    "508aa24a30f21197c46cc37f59e98bd4cb8eabf8811f82b19f68c3c91a6850b4"
)
V7_FACTOR_REPORT_SHA256 = (
    "d504d29a76776f99abee50acd7c0860474b5a90d80cb2efa1ba937ca32b4c100"
)
V9A_PARENT_REPORT_SHA256 = (
    "bde1dc12e7ff82b27f6da2efa66433563b98ca89217703c3bd50e638feaab8db"
)
V9A_PARENT_CODE = BASE_DIR / "long_history_v9_bottom_tail_risk_experiment.py"
V9A_PARENT_CODE_SHA256 = (
    "bff8153ab445cf885fb051f3475864e5e17b123ca1f590dcd88ec1bcc014b3b9"
)
V9A_FAILED_CHECKS = (
    "at_least_four_folds_joint_risk_and_economic_advantage",
    "at_least_four_folds_kept_mean_above_rejected",
    "median_fold_kept_mean_advantage_above_zero",
)

FACTOR_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "index_return_60",
        "family": "market_regime",
        "source": "index",
        "formula": "index close[t]/close[t-60]-1",
        "minimum_bars": 61,
    },
    {
        "name": "index_realized_volatility_20",
        "family": "market_regime",
        "source": "index",
        "formula": "population std of latest 20 index close-to-close returns",
        "minimum_bars": 21,
    },
    {
        "name": "index_drawdown_from_high_60",
        "family": "market_regime",
        "source": "index",
        "formula": "index close[t]/max(index close[t-59:t])-1",
        "minimum_bars": 60,
    },
    {
        "name": "excess_return_5",
        "family": "short_relative_risk",
        "source": "qfq+index",
        "formula": "stock 5-day return minus index 5-day return",
        "minimum_bars": 6,
    },
    {
        "name": "stock_index_correlation_20",
        "family": "short_relative_risk",
        "source": "qfq+index",
        "formula": "Pearson correlation of latest 20 aligned daily returns",
        "minimum_bars": 21,
    },
    {
        "name": "residual_volatility_20",
        "family": "short_relative_risk",
        "source": "qfq+index",
        "formula": "population std of stock_ret-beta20*index_ret on 20 aligned returns",
        "minimum_bars": 21,
    },
    {
        "name": "stock_realized_volatility_20",
        "family": "price_path",
        "source": "qfq",
        "formula": "population std of latest 20 stock close-to-close returns",
        "minimum_bars": 21,
    },
    {
        "name": "drawdown_from_high_60",
        "family": "price_path",
        "source": "qfq",
        "formula": "stock close[t]/max(stock close[t-59:t])-1",
        "minimum_bars": 60,
    },
    {
        "name": "price_efficiency_20",
        "family": "price_path",
        "source": "qfq",
        "formula": "abs(stock 20-day return)/sum(abs(latest 20 daily returns))",
        "minimum_bars": 21,
    },
    {
        "name": "volume_mean_5_to_20",
        "family": "volume_confirmation",
        "source": "qfq",
        "formula": "mean(volume[t-4:t])/mean(volume[t-19:t])",
        "minimum_bars": 20,
    },
    {
        "name": "amount_mean_5_to_20",
        "family": "volume_confirmation",
        "source": "qfq",
        "formula": "mean(effective_amount[t-4:t])/mean(effective_amount[t-19:t])",
        "minimum_bars": 20,
    },
    {
        "name": "return_volume_change_correlation_20",
        "family": "volume_confirmation",
        "source": "qfq",
        "formula": "Pearson correlation of 20 stock returns and log-volume changes",
        "minimum_bars": 21,
    },
)
FACTOR_NAMES = tuple(str(spec["name"]) for spec in FACTOR_SPECS)
ARTIFACT_NAMES = (
    "candidate_features",
    "qfq_manifest",
    "index_cutoffs",
    "coverage",
)
PUBLIC_CANDIDATE_FIELDS = (
    "candidate_id",
    "symbol",
    "signal_day",
    "signal_type",
)
OUTCOME_TOKENS = (
    "pnl",
    "profit",
    "return_after",
    "exit",
    "mfe",
    "mae",
    "future",
    "post_",
)


class NewFactorPreflightError(RuntimeError):
    """Raised when a frozen input or v10 factor contract is violated."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise NewFactorPreflightError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require(
        not any("holdout" in part.lower() for part in resolved.parts),
        f"Holdout path is blocked: {resolved}",
    )
    return resolved


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _input_record(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"input file does not exist: {path}")
    return {"path": str(path), "sha256": _sha256_file(path)}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                _require(
                    isinstance(value, dict),
                    f"JSONL object required: {path}:{line_number}",
                )
                rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise NewFactorPreflightError(f"cannot read JSONL: {path}: {exc}") from exc
    return rows


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NewFactorPreflightError(f"cannot read JSON: {path}: {exc}") from exc
    _require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_json(row) + "\n")
    return {"path": str(path), "rows": len(rows), "sha256": _sha256_file(path)}


def _assert_frozen_inputs(v7_factor_report_path: Path, v9a_report_path: Path) -> None:
    frozen = (
        (V7_PREFLIGHT_CODE, V7_PREFLIGHT_CODE_SHA256, "v7 preflight code"),
        (V7_AUDIT_CODE, V7_AUDIT_CODE_SHA256, "v7 audit code"),
        (V9A_PARENT_CODE, V9A_PARENT_CODE_SHA256, "v9a parent code"),
        (v7_factor_report_path, V7_FACTOR_REPORT_SHA256, "v7 factor report"),
        (v9a_report_path, V9A_PARENT_REPORT_SHA256, "v9a parent report"),
    )
    for path, expected, label in frozen:
        _require(path.exists() and path.is_file(), f"missing frozen {label}: {path}")
        actual = _sha256_file(path)
        _require(
            actual == expected,
            f"frozen {label} SHA256 drift: expected {expected}, got {actual}",
        )


def _audit_v9a_parent(v9a_report_path: Path) -> dict[str, Any]:
    report = _load_json(v9a_report_path)
    _require(
        report.get("version") == "long_history_v9_bottom_tail_risk.v1",
        "unexpected v9a parent version",
    )
    _require(
        report.get("passes_research_screen") is False,
        "v9a parent research screen was not failed",
    )
    _require(report.get("production_eligible") is False, "v9a was production eligible")
    screen = report.get("screen")
    _require(isinstance(screen, dict), "v9a parent screen missing")
    _require(
        screen.get("passes_research_screen") is False,
        "v9a parent nested screen was not failed",
    )
    _require(
        screen.get("production_eligible") is False,
        "v9a parent nested screen was production eligible",
    )
    checks = screen.get("checks")
    _require(isinstance(checks, dict), "v9a parent screen checks missing")
    failed = tuple(sorted(name for name, passed in checks.items() if passed is False))
    _require(
        failed == tuple(sorted(V9A_FAILED_CHECKS)),
        f"v9a parent failed checks differ: {failed}",
    )
    return {
        "version": report["version"],
        "passes_research_screen": False,
        "failed_checks": list(V9A_FAILED_CHECKS),
        "current_19_factor_modeling_terminated": True,
        "checks_passed": True,
    }


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 12) if math.isfinite(number) else None


def _numeric_tail(values: pd.Series, length: int) -> pd.Series | None:
    numeric = pd.to_numeric(values, errors="coerce").tail(length)
    if len(numeric) != length or numeric.isna().any():
        return None
    return numeric.astype(float)


def _lag_return(values: pd.Series, lag: int) -> float | None:
    tail = _numeric_tail(values, lag + 1)
    if tail is None or abs(float(tail.iloc[0])) <= 1e-15:
        return None
    return _finite(float(tail.iloc[-1] / tail.iloc[0] - 1.0))


def _return_vector(values: pd.Series, periods: int) -> np.ndarray | None:
    tail = _numeric_tail(values, periods + 1)
    if tail is None or (tail.iloc[:-1].abs() <= 1e-15).any():
        return None
    returns = tail.pct_change(fill_method=None).iloc[1:].to_numpy(dtype=float)
    if len(returns) != periods or not np.isfinite(returns).all():
        return None
    return returns


def _realized_volatility(values: pd.Series, periods: int) -> float | None:
    returns = _return_vector(values, periods)
    return None if returns is None else _finite(float(np.std(returns, ddof=0)))


def _drawdown_from_high(values: pd.Series, window: int) -> float | None:
    tail = _numeric_tail(values, window)
    if tail is None:
        return None
    peak = float(tail.max())
    if abs(peak) <= 1e-15:
        return None
    return _finite(float(tail.iloc[-1] / peak - 1.0))


def _price_efficiency(values: pd.Series, periods: int) -> float | None:
    returns = _return_vector(values, periods)
    lag_return = _lag_return(values, periods)
    if returns is None or lag_return is None:
        return None
    path = float(np.abs(returns).sum())
    if path <= 1e-15:
        return None
    return _finite(abs(lag_return) / path)


def _mean_ratio(values: pd.Series, short: int, long: int) -> float | None:
    tail = _numeric_tail(values, long)
    if tail is None:
        return None
    long_mean = float(tail.mean())
    short_mean = float(tail.tail(short).mean())
    if abs(long_mean) <= 1e-15:
        return None
    return _finite(short_mean / long_mean)


def _pearson(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) != len(right) or len(left) == 0:
        return None
    left_centered = left - float(left.mean())
    right_centered = right - float(right.mean())
    denominator = math.sqrt(
        max(
            float(left_centered @ left_centered)
            * float(right_centered @ right_centered),
            0.0,
        )
    )
    if denominator <= 1e-24:
        return None
    return _finite(float(left_centered @ right_centered) / denominator)


def _aligned_short_market_metrics(
    stock: pd.DataFrame, index: pd.DataFrame
) -> tuple[float | None, float | None]:
    left = stock[["datetime", "close"]].rename(columns={"close": "stock_close"})
    right = index[["datetime", "close"]].rename(columns={"close": "index_close"})
    merged = left.merge(right, on="datetime", how="inner").sort_values("datetime")
    if len(merged) < 21:
        return None, None
    prices = merged[["stock_close", "index_close"]].tail(21)
    if prices.isna().any().any():
        return None, None
    returns = prices.pct_change(fill_method=None).iloc[1:]
    if len(returns) != 20 or not np.isfinite(returns.to_numpy(dtype=float)).all():
        return None, None
    stock_values = returns["stock_close"].to_numpy(dtype=float)
    index_values = returns["index_close"].to_numpy(dtype=float)
    correlation = _pearson(stock_values, index_values)
    index_centered = index_values - float(index_values.mean())
    index_ss = float(index_centered @ index_centered)
    if index_ss <= 1e-24:
        return correlation, None
    stock_centered = stock_values - float(stock_values.mean())
    beta = float(stock_centered @ index_centered) / index_ss
    residuals = stock_values - beta * index_values
    return correlation, _finite(float(np.std(residuals, ddof=0)))


def _effective_amount(frame: pd.DataFrame) -> pd.Series:
    amount = (
        pd.to_numeric(frame["amount"], errors="coerce")
        if "amount" in frame
        else pd.Series(np.nan, index=frame.index, dtype=float)
    )
    if {"close", "volume"}.issubset(frame.columns):
        estimated = pd.to_numeric(frame["close"], errors="coerce") * pd.to_numeric(
            frame["volume"], errors="coerce"
        )
        amount = amount.where(amount > 0, estimated)
    return amount


def _return_volume_change_correlation(frame: pd.DataFrame) -> float | None:
    if not {"close", "volume"}.issubset(frame.columns):
        return None
    close = _numeric_tail(frame["close"], 21)
    volume = _numeric_tail(frame["volume"], 21)
    if close is None or volume is None or (volume <= 0).any():
        return None
    returns = close.pct_change(fill_method=None).iloc[1:].to_numpy(dtype=float)
    log_volume_change = np.diff(np.log(volume.to_numpy(dtype=float)))
    if not np.isfinite(returns).all() or not np.isfinite(log_volume_change).all():
        return None
    return _pearson(returns, log_volume_change)


def compute_candidate_features(
    candidate: dict[str, Any], qfq: pd.DataFrame, index: pd.DataFrame
) -> dict[str, Any]:
    """Compute the frozen twelve-factor v10 schema from already-cut frames."""

    _require("close" in qfq.columns and not qfq.empty, "QFQ close is missing")
    _require("close" in index.columns and not index.empty, "index close is missing")
    close = pd.to_numeric(qfq["close"], errors="coerce")
    index_close = pd.to_numeric(index["close"], errors="coerce")
    stock_return_5 = _lag_return(close, 5)
    index_return_5 = _lag_return(index_close, 5)
    correlation_20, residual_volatility_20 = _aligned_short_market_metrics(qfq, index)
    volume = (
        pd.to_numeric(qfq["volume"], errors="coerce")
        if "volume" in qfq
        else pd.Series(np.nan, index=qfq.index, dtype=float)
    )
    amount = _effective_amount(qfq)
    features: dict[str, float | None] = {
        "index_return_60": _lag_return(index_close, 60),
        "index_realized_volatility_20": _realized_volatility(index_close, 20),
        "index_drawdown_from_high_60": _drawdown_from_high(index_close, 60),
        "excess_return_5": (
            _finite(stock_return_5 - index_return_5)
            if stock_return_5 is not None and index_return_5 is not None
            else None
        ),
        "stock_index_correlation_20": correlation_20,
        "residual_volatility_20": residual_volatility_20,
        "stock_realized_volatility_20": _realized_volatility(close, 20),
        "drawdown_from_high_60": _drawdown_from_high(close, 60),
        "price_efficiency_20": _price_efficiency(close, 20),
        "volume_mean_5_to_20": _mean_ratio(volume, 5, 20),
        "amount_mean_5_to_20": _mean_ratio(amount, 5, 20),
        "return_volume_change_correlation_20": (_return_volume_change_correlation(qfq)),
    }
    _require(
        tuple(features) == FACTOR_NAMES,
        f"factor implementation order differs: {candidate.get('candidate_id')}",
    )
    return {
        "features": features,
        "missing": {name: features[name] is None for name in FACTOR_NAMES},
    }


def _candidate_sort_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row["signal_day"]),
        str(row["signal_type"]),
        str(row["symbol"]),
        str(row["candidate_id"]),
    )


def _load_v7_candidates(v7_run: dict[str, Any]) -> list[dict[str, Any]]:
    paths = v7_run.get("_artifact_paths")
    _require(isinstance(paths, dict), "v7 audit artifact paths missing")
    path = paths.get("candidate_features")
    _require(isinstance(path, Path), "v7 candidate feature path missing")
    rows = _load_jsonl(path)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        identifier = str(row.get("candidate_id", ""))
        _require(
            identifier and identifier not in seen, f"duplicate candidate: {identifier}"
        )
        seen.add(identifier)
        lower_keys = [str(key).lower() for key in row]
        _require(
            not any(token in key for key in lower_keys for token in OUTCOME_TOKENS),
            f"v7 candidate contains outcome field: {identifier}",
        )
        _require(
            all(field in row for field in PUBLIC_CANDIDATE_FIELDS),
            f"v7 candidate identity fields missing: {identifier}",
        )
        result.append({field: row[field] for field in PUBLIC_CANDIDATE_FIELDS})
    result.sort(key=_candidate_sort_key)
    return result


def _resolve_v7_input(report: dict[str, Any], name: str, expected: Path) -> None:
    inputs = report.get("input")
    record = inputs.get(name) if isinstance(inputs, dict) else None
    _require(isinstance(record, dict), f"v7 input record missing: {name}")
    actual = Path(str(record.get("path", ""))).resolve()
    _require(actual == expected, f"v7 input path differs: {name}")
    _require(
        str(record.get("sha256", "")) == _sha256_file(expected),
        f"v7 input SHA256 differs: {name}",
    )


def _coverage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = len(rows)
    specs = {str(spec["name"]): spec for spec in FACTOR_SPECS}
    result: list[dict[str, Any]] = []
    for name in FACTOR_NAMES:
        missing = sum(bool(row["missing"][name]) for row in rows)
        result.append(
            {
                **specs[name],
                "candidates": total,
                "non_missing": total - missing,
                "missing": missing,
                "coverage_pct": round(100.0 * (total - missing) / total, 10),
            }
        )
    return result


def _manifest_sha256(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> str:
    entries = ["|".join(str(row[field]) for field in fields) for row in rows]
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def build_snapshot(
    v7_factor_report_path: Path,
    source_report_path: Path,
    fold_report_path: Path,
    index_data_path: Path,
    v9a_report_path: Path,
) -> dict[str, Any]:
    """Compute all v10 preflight values in memory without writing files."""

    v7_factor_report_path = _guard_development_path(v7_factor_report_path)
    source_report_path = _guard_development_path(source_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    index_data_path = _guard_development_path(index_data_path)
    v9a_report_path = _guard_development_path(v9a_report_path)
    _assert_frozen_inputs(v7_factor_report_path, v9a_report_path)
    v9a_parent = _audit_v9a_parent(v9a_report_path)
    v7_run = v7audit.audit_run(v7_factor_report_path)
    _require(v7_run.get("checks_passed") is True, "v7 factor audit failed")
    v7_report = v7_run.get("_report_value")
    _require(isinstance(v7_report, dict), "v7 factor report value missing")
    _resolve_v7_input(v7_report, "source_report", source_report_path)
    _resolve_v7_input(v7_report, "fold_report", fold_report_path)
    _resolve_v7_input(v7_report, "index_data", index_data_path)
    candidates = _load_v7_candidates(v7_run)
    _require(
        len(candidates) == int(v7_run.get("candidate_count", -1)),
        "v7 candidate count differs",
    )

    source_audit = v7preflight.audit_source_report(source_report_path)
    fold_audit = v7preflight.audit_fold_report(fold_report_path, source_audit)
    _require(
        source_audit.get("checks_passed") is True
        and fold_audit.get("checks_passed") is True,
        "v5 source or fold audit failed",
    )
    frozen_index = source_audit["frozen_inputs"]["index_data"]
    _require(
        Path(str(frozen_index["path"])).resolve() == index_data_path,
        "explicit index path differs from frozen v5 input",
    )
    _require(
        _sha256_file(index_data_path) == str(frozen_index["sha256"]),
        "frozen index SHA256 drift",
    )
    index_frame = v7preflight._prepare_history(
        pd.read_pickle(index_data_path), label="index"
    )
    history_dir = v7preflight._fold_history_dir(fold_audit)
    qfq_audit = source_audit["frozen_inputs"]["qfq_history"]
    qfq_paths = qfq_audit.get("_symbol_path")
    qfq_hashes = qfq_audit.get("_symbol_sha256")
    _require(
        isinstance(qfq_paths, dict) and isinstance(qfq_hashes, dict),
        "source audit lacks QFQ path/hash manifest",
    )

    qfq_cache: dict[str, pd.DataFrame] = {}
    qfq_manifest: dict[str, dict[str, Any]] = {}
    index_cutoffs: dict[str, dict[str, Any]] = {}
    candidate_features: list[dict[str, Any]] = []
    for candidate in candidates:
        identifier = str(candidate["candidate_id"])
        symbol = str(candidate["symbol"])
        signal_day = date.fromisoformat(str(candidate["signal_day"]))
        if symbol not in qfq_cache:
            raw_path = qfq_paths.get(symbol)
            expected_hash = qfq_hashes.get(symbol)
            _require(raw_path and expected_hash, f"QFQ manifest missing: {symbol}")
            path = _guard_development_path(Path(str(raw_path)))
            expected_path = (history_dir / f"{symbol}_qfq.pkl").resolve()
            _require(path == expected_path, f"QFQ path differs: {symbol}")
            actual_hash = _sha256_file(path)
            _require(actual_hash == str(expected_hash), f"QFQ hash drift: {symbol}")
            qfq_cache[symbol] = v7preflight._prepare_history(
                pd.read_pickle(path), label=f"QFQ {symbol}"
            )
            qfq_manifest[symbol] = {
                "symbol": symbol,
                "file": path.name,
                "path": str(path),
                "sha256": actual_hash,
            }
        qfq_as_of = v7preflight._as_of(
            qfq_cache[symbol], signal_day, label=f"QFQ {symbol}"
        )
        index_as_of = v7preflight._as_of(index_frame, signal_day, label="index")
        _require(
            qfq_as_of["datetime"].iloc[-1].date() <= signal_day
            and index_as_of["datetime"].iloc[-1].date() <= signal_day,
            f"future cutoff detected: {identifier}",
        )
        computed = compute_candidate_features(candidate, qfq_as_of, index_as_of)
        candidate_features.append(
            {
                **candidate,
                **computed,
                "data_cutoffs": {
                    "qfq_max_day_used": qfq_as_of["datetime"]
                    .iloc[-1]
                    .date()
                    .isoformat(),
                    "index_max_day_used": index_as_of["datetime"]
                    .iloc[-1]
                    .date()
                    .isoformat(),
                },
            }
        )
        cutoff = index_cutoffs.setdefault(
            signal_day.isoformat(),
            {
                "signal_day": signal_day.isoformat(),
                "index_max_day_used": index_as_of["datetime"]
                .iloc[-1]
                .date()
                .isoformat(),
                "candidates": 0,
            },
        )
        cutoff["candidates"] += 1
    candidate_features.sort(key=_candidate_sort_key)
    qfq_rows = [qfq_manifest[symbol] for symbol in sorted(qfq_manifest)]
    cutoff_rows = [index_cutoffs[day] for day in sorted(index_cutoffs)]
    return {
        "candidate_features": candidate_features,
        "qfq_manifest": qfq_rows,
        "index_cutoffs": cutoff_rows,
        "coverage": _coverage_rows(candidate_features),
        "v7_run": v7_run,
        "source_audit": source_audit,
        "fold_audit": fold_audit,
        "v9a_parent": v9a_parent,
        "index_bars": len(index_frame),
    }


def assemble_report(
    snapshot: dict[str, Any],
    v7_factor_report_path: Path,
    source_report_path: Path,
    fold_report_path: Path,
    index_data_path: Path,
    v9a_report_path: Path,
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows = snapshot["candidate_features"]
    coverage = snapshot["coverage"]
    return {
        "version": VERSION,
        "dataset_status": DATASET_STATUS,
        "research_question": (
            "Are twelve preregistered, non-v7 market, short-relative-risk, "
            "price-path, and volume-confirmation factors causally available and "
            "reproducible on the frozen development candidates?"
        ),
        "passes_preflight": True,
        "factor_contract": {
            "factor_count": len(FACTOR_NAMES),
            "factor_names": list(FACTOR_NAMES),
            "factor_specs": list(FACTOR_SPECS),
            "disjoint_from_v7_factor_names": not bool(
                set(FACTOR_NAMES) & set(v7preflight.FACTOR_NAMES)
            ),
            "feature_time_boundary": "closed local bars with datetime <= signal_day",
            "missing_policy": "null feature plus explicit per-feature missing boolean",
            "coverage_is_not_a_selection_rule": True,
            "excluded_unavailable_sources": [
                "industry-relative factors: no frozen industry classification input",
                "market breadth factors: no frozen independent breadth input",
            ],
            "factor_selection_performed": False,
        },
        "outcome_isolation": {
            "candidate_identity_source": "audited v7 candidate_features.jsonl",
            "candidate_outcomes_read": False,
            "candidate_outcomes_used": False,
            "coverage_used_for_selection": False,
            "model_fitted": False,
        },
        "input": {
            "v7_factor_report": _input_record(v7_factor_report_path),
            "source_report": _input_record(source_report_path),
            "fold_report": _input_record(fold_report_path),
            "index_data": _input_record(index_data_path),
            "v9a_parent_report": _input_record(v9a_report_path),
            "preflight_code": _input_record(Path(__file__).resolve()),
            "audit_code": _input_record(AUDIT_CODE),
            "v7_preflight_code": _input_record(V7_PREFLIGHT_CODE),
            "v7_audit_code": _input_record(V7_AUDIT_CODE),
            "v9a_parent_code": _input_record(V9A_PARENT_CODE),
            "v7_factor_audit": {
                "checks_passed": snapshot["v7_run"]["checks_passed"],
                "candidate_count": snapshot["v7_run"]["candidate_count"],
                "factor_count": snapshot["v7_run"]["factor_count"],
            },
            "v5_audit": {
                "source_checks_passed": snapshot["source_audit"]["checks_passed"],
                "fold_checks_passed": snapshot["fold_audit"]["checks_passed"],
                "dataset_candidates": snapshot["fold_audit"]["dataset_candidates"],
                "dataset_candidate_ids_sha256": snapshot["fold_audit"][
                    "dataset_candidate_ids_sha256"
                ],
            },
        },
        "parent_decision": {
            "v9a_report_sha256": V9A_PARENT_REPORT_SHA256,
            **snapshot["v9a_parent"],
            "v10_training_authorized": False,
        },
        "artifacts": artifacts,
        "summary": {
            "candidates": len(rows),
            "candidate_ids_sha256": _manifest_sha256(rows, ("candidate_id",)),
            "qfq_symbols_used": len(snapshot["qfq_manifest"]),
            "index_signal_days": len(snapshot["index_cutoffs"]),
            "index_bars_available": snapshot["index_bars"],
            "missing_by_factor": {row["name"]: int(row["missing"]) for row in coverage},
            "all_cutoffs_on_or_before_signal_day": True,
        },
        "policy": {
            "local_data_only": True,
            "network_used": False,
            "database_used": False,
            "sql_executed": False,
            "candidate_outcomes_read": False,
            "factor_selection_performed": False,
            "hyperparameters_selected": False,
            "model_fitted": False,
            "portfolio_replayed": False,
            "holdout_used": False,
            "production_eligible": False,
        },
        "model_fitted": False,
        "candidate_outcomes_read": False,
        "factor_selection_performed": False,
        "hyperparameters_selected": False,
        "holdout_used": False,
        "production_eligible": False,
    }


def build_report(
    v7_factor_report_path: Path,
    source_report_path: Path,
    fold_report_path: Path,
    index_data_path: Path,
    v9a_report_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = _guard_development_path(output_dir)
    _require(
        not output_dir.exists(),
        f"output directory already exists; refusing overwrite: {output_dir}",
    )
    snapshot = build_snapshot(
        v7_factor_report_path,
        source_report_path,
        fold_report_path,
        index_data_path,
        v9a_report_path,
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = {
        name: _write_jsonl(output_dir / f"{name}.jsonl", snapshot[name])
        for name in ARTIFACT_NAMES
    }
    report = assemble_report(
        snapshot,
        v7_factor_report_path,
        source_report_path,
        fold_report_path,
        index_data_path,
        v9a_report_path,
        artifacts,
    )
    _write_json(output_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v7-factor-report", type=Path, required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--fold-report", type=Path, required=True)
    parser.add_argument("--index-data", type=Path, required=True)
    parser.add_argument("--v9a-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(
            args.v7_factor_report,
            args.source_report,
            args.fold_report,
            args.index_data,
            args.v9a_report,
            args.output_dir,
        )
    except Exception as exc:  # noqa: BLE001 - CLI fails closed as one JSON object.
        print(
            json.dumps(
                {"version": VERSION, "passes_preflight": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    report_path = _guard_development_path(args.output_dir) / "report.json"
    print(
        json.dumps(
            {
                "version": VERSION,
                "passes_preflight": report["passes_preflight"],
                "report": str(report_path),
                "sha256": _sha256_file(report_path),
                "candidates": report["summary"]["candidates"],
                "factor_count": report["factor_contract"]["factor_count"],
                "model_fitted": False,
                "production_eligible": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
