"""Read-only full-replay audit for two v7 factor-preflight runs."""

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

import long_history_v7_factor_preflight as preflight

VERSION = "long_history_v7_factor_preflight_audit.v2"


class FactorPreflightAuditError(RuntimeError):
    """Raised when a v7 preflight report fails read-only replay."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FactorPreflightAuditError(message)


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise FactorPreflightAuditError(f"Holdout path is blocked: {resolved}")
    return resolved


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactorPreflightAuditError(f"cannot read JSON: {path}: {exc}") from exc
    _require(isinstance(value, dict), f"JSON object required: {path}")
    return value


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
        raise FactorPreflightAuditError(f"cannot read JSONL: {path}: {exc}") from exc
    return rows


def _resolve_input(record: Any, report_path: Path, label: str) -> Path:
    _require(isinstance(record, dict), f"missing input record: {label}")
    raw = record.get("path")
    _require(isinstance(raw, str) and bool(raw), f"missing input path: {label}")
    path = Path(raw)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"missing input file: {path}")
    digest = _sha256_file(path)
    _require(str(record.get("sha256", "")) == digest, f"input SHA256 drift: {label}")
    return path


def _resolve_artifact(
    record: Any, report_path: Path, label: str
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    path = _resolve_input(record, report_path, f"artifact.{label}")
    rows = _load_jsonl(path)
    _require(record.get("rows") == len(rows), f"artifact row mismatch: {label}")
    return (
        path,
        rows,
        {
            "path": str(path),
            "rows": len(rows),
            "sha256": _sha256_file(path),
        },
    )


def _walk_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(str(key).lower())
            keys.extend(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_walk_keys(item))
    return keys


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return round(result, 12)


def _lag_return(values: pd.Series, lag: int) -> float | None:
    series = pd.to_numeric(values, errors="coerce")
    if len(series) < lag + 1:
        return None
    latest = _number(series.iloc[-1])
    base = _number(series.iloc[-lag - 1])
    if latest is None or base is None or abs(base) <= 1e-15:
        return None
    return _number(latest / base - 1.0)


def _median_ratio(values: pd.Series, window: int) -> float | None:
    series = pd.to_numeric(values, errors="coerce").tail(window)
    if len(series) != window or series.isna().any():
        return None
    latest = _number(series.iloc[-1])
    median = _number(series.median())
    if latest is None or median is None or abs(median) <= 1e-15:
        return None
    return _number(latest / median)


def _latest_percentile(values: pd.Series, window: int) -> float | None:
    series = pd.to_numeric(values, errors="coerce").tail(window)
    if len(series) != window or series.isna().any():
        return None
    latest = _number(series.iloc[-1])
    return None if latest is None else _number(float((series <= latest).mean()))


def _market_metrics(
    stock: pd.DataFrame, index: pd.DataFrame
) -> tuple[float | None, float | None]:
    stock_close = stock[["datetime", "close"]].rename(columns={"close": "stock"})
    index_close = index[["datetime", "close"]].rename(columns={"close": "index"})
    aligned = stock_close.merge(index_close, on="datetime", how="inner")
    aligned = aligned.sort_values("datetime")
    if len(aligned) < 61:
        return None, None
    returns = aligned[["stock", "index"]].pct_change(fill_method=None)
    returns = returns.replace([np.inf, -np.inf], np.nan).dropna().tail(60)
    if len(returns) != 60:
        return None, None
    stock_values = returns["stock"].to_numpy(dtype=float)
    index_values = returns["index"].to_numpy(dtype=float)
    stock_delta = stock_values - stock_values.mean()
    index_delta = index_values - index_values.mean()
    stock_ss = float(np.dot(stock_delta, stock_delta))
    index_ss = float(np.dot(index_delta, index_delta))
    covariance_sum = float(np.dot(stock_delta, index_delta))
    beta = None if index_ss <= 1e-24 else _number(covariance_sum / index_ss)
    divisor = math.sqrt(max(stock_ss * index_ss, 0.0))
    correlation = None if divisor <= 1e-24 else _number(covariance_sum / divisor)
    return beta, correlation


def _independent_candidate_features(
    candidate: dict[str, Any],
    qfq: pd.DataFrame,
    index: pd.DataFrame,
    stock_pool: pd.DataFrame,
    *,
    volume_unit_shares: float,
) -> dict[str, Any]:
    """Second implementation of the frozen formulas used only by the audit."""
    signal_day = date.fromisoformat(str(candidate["signal_day"]))
    trading_days = qfq["datetime"].dt.date.tolist()
    _require(signal_day in trading_days, "audit signal_day absent from QFQ")
    raw_cross_day = candidate["source_fields"]["cross_day"]
    confirmation_bars = candidate["source_fields"]["confirmation_bars"]
    cross_age: int | None = None
    if raw_cross_day is not None:
        cross_day = date.fromisoformat(str(raw_cross_day))
        _require(cross_day in trading_days, "audit cross_day absent from QFQ")
        cross_age = trading_days.index(signal_day) - trading_days.index(cross_day)
        _require(cross_age >= 0, "audit negative cross age")

    close = pd.to_numeric(qfq["close"], errors="coerce")
    scale = _number(abs(float(close.iloc[-1])))
    macd = preflight.calculate_macd(close, fast=12, slow=26, signal=9)
    hist = pd.to_numeric(macd["hist"], errors="coerce")
    dif = pd.to_numeric(macd["dif"], errors="coerce")
    dea = pd.to_numeric(macd["dea"], errors="coerce")

    def scaled(value: Any) -> float | None:
        number = _number(value)
        if number is None or scale is None or scale <= 1e-15:
            return None
        return _number(number / scale)

    recent_hist = hist.tail(3)
    stock_return20 = _lag_return(close, 20)
    stock_return60 = _lag_return(close, 60)
    index_close = pd.to_numeric(index["close"], errors="coerce")
    index_return20 = _lag_return(index_close, 20)
    index_return60 = _lag_return(index_close, 60)
    beta, correlation = _market_metrics(qfq, index)
    index_tail60 = index_close.tail(60)
    index_mean60 = (
        _number(index_tail60.mean())
        if len(index_tail60) == 60 and not index_tail60.isna().any()
        else None
    )
    index_latest = _number(index_close.iloc[-1])

    if "amount" in stock_pool:
        amount = pd.to_numeric(stock_pool["amount"], errors="coerce")
    else:
        amount = pd.Series(np.nan, index=stock_pool.index, dtype=float)
    if {"close", "volume"}.issubset(stock_pool.columns):
        estimated = (
            pd.to_numeric(stock_pool["close"], errors="coerce")
            * pd.to_numeric(stock_pool["volume"], errors="coerce")
            * volume_unit_shares
        )
        amount = amount.where(amount > 0, estimated)
    turnover = (
        pd.to_numeric(stock_pool["turnover_rate"], errors="coerce")
        if "turnover_rate" in stock_pool
        else pd.Series(np.nan, index=stock_pool.index, dtype=float)
    )
    if {"high", "low", "close"}.issubset(qfq.columns):
        previous_close = close.shift(1).abs().replace(0, np.nan)
        amplitude = (
            pd.to_numeric(qfq["high"], errors="coerce")
            - pd.to_numeric(qfq["low"], errors="coerce")
        ) / previous_close
    else:
        amplitude = pd.Series(np.nan, index=qfq.index, dtype=float)
    market_cap = (
        _number(
            pd.to_numeric(stock_pool["circulating_market_cap"], errors="coerce").iloc[
                -1
            ]
        )
        if "circulating_market_cap" in stock_pool
        else None
    )

    features: dict[str, float | int | None] = {
        "confirmation_bars": (
            int(confirmation_bars) if confirmation_bars is not None else None
        ),
        "cross_age_trading_days": cross_age,
        "macd_hist_ratio": scaled(hist.iloc[-1]),
        "macd_hist_slope_3": (
            scaled(hist.iloc[-1] - hist.iloc[-4]) if len(hist) >= 4 else None
        ),
        "macd_hist_acceleration": (
            scaled(hist.iloc[-1] - 2.0 * hist.iloc[-2] + hist.iloc[-3])
            if len(hist) >= 3
            else None
        ),
        "dif_slope_3": (scaled(dif.iloc[-1] - dif.iloc[-4]) if len(dif) >= 4 else None),
        "dea_slope_3": (scaled(dea.iloc[-1] - dea.iloc[-4]) if len(dea) >= 4 else None),
        "hist_expanding_3": (
            None
            if len(recent_hist) != 3 or recent_hist.isna().any()
            else int(
                bool(
                    (recent_hist > 0).all()
                    and abs(float(recent_hist.iloc[0]))
                    < abs(float(recent_hist.iloc[1]))
                    < abs(float(recent_hist.iloc[2]))
                )
            )
        ),
        "excess_return_20": (
            _number(stock_return20 - index_return20)
            if stock_return20 is not None and index_return20 is not None
            else None
        ),
        "excess_return_60": (
            _number(stock_return60 - index_return60)
            if stock_return60 is not None and index_return60 is not None
            else None
        ),
        "beta_60": beta,
        "index_return_20": index_return20,
        "index_above_ma60": (
            int(index_latest > index_mean60)
            if index_latest is not None and index_mean60 is not None
            else None
        ),
        "stock_index_correlation_60": correlation,
        "amount_to_median_20": _median_ratio(amount, 20),
        "turnover_to_median_20": _median_ratio(turnover, 20),
        "turnover_percentile_60": _latest_percentile(turnover, 60),
        "amplitude_percentile_60": _latest_percentile(amplitude, 60),
        "circulating_market_cap": (
            market_cap if market_cap is not None and market_cap > 0 else None
        ),
    }
    _require(set(features) == set(preflight.FACTOR_NAMES), "audit factor schema drift")
    return {
        "features": features,
        "missing": {name: features[name] is None for name in preflight.FACTOR_NAMES},
    }


def _independent_snapshot(
    source_path: Path, fold_path: Path, index_path: Path
) -> dict[str, Any]:
    original = preflight.compute_candidate_features
    preflight.compute_candidate_features = _independent_candidate_features
    try:
        return preflight.build_snapshot(source_path, fold_path, index_path)
    finally:
        preflight.compute_candidate_features = original


def _audit_candidate_rows(rows: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    expected_top = {
        "candidate_id",
        "symbol",
        "signal_day",
        "signal_type",
        "source_fields",
        "features",
        "missing",
        "data_cutoffs",
    }
    for row in rows:
        identifier = str(row.get("candidate_id", ""))
        _require(bool(identifier), "candidate feature row lacks candidate_id")
        _require(
            identifier not in seen, f"duplicate candidate feature row: {identifier}"
        )
        seen.add(identifier)
        _require(set(row) == expected_top, f"unexpected candidate fields: {identifier}")
        _require(
            set(row.get("features", {})) == set(preflight.FACTOR_NAMES),
            f"factor schema mismatch: {identifier}",
        )
        _require(
            set(row.get("missing", {})) == set(preflight.FACTOR_NAMES),
            f"missing schema mismatch: {identifier}",
        )
        for name in preflight.FACTOR_NAMES:
            _require(
                bool(row["missing"][name]) is (row["features"][name] is None),
                f"missing flag mismatch: {identifier}.{name}",
            )
        signal_day = date.fromisoformat(str(row["signal_day"]))
        for label, cutoff in row.get("data_cutoffs", {}).items():
            _require(
                date.fromisoformat(str(cutoff)) <= signal_day,
                f"future cutoff in {identifier}.{label}",
            )
        bad_keys = [
            key
            for key in _walk_keys(row)
            if any(token in key for token in preflight.FORBIDDEN_OUTCOME_TOKENS)
        ]
        _require(
            not bad_keys, f"outcome-like fields emitted for {identifier}: {bad_keys}"
        )


def audit_run(report_path: Path) -> dict[str, Any]:
    report_path = _guard_development_path(report_path)
    _require(
        report_path.exists() and report_path.is_file(), f"missing report: {report_path}"
    )
    report = _load_json(report_path)
    _require(report.get("version") == preflight.VERSION, "unexpected preflight version")
    _require(report.get("passes_preflight") is True, "preflight did not pass")
    _require(
        report.get("dataset_status") == preflight.DATASET_STATUS,
        "unexpected dataset_status",
    )
    for field in (
        "model_fitted",
        "outcomes_read",
        "hyperparameters_selected",
        "holdout_used",
        "production_eligible",
        "research_screen_defined",
    ):
        _require(report.get(field) is False, f"unsafe declaration: {field}")

    input_value = report.get("input")
    _require(isinstance(input_value, dict), "report input section missing")
    source_path = _resolve_input(
        input_value.get("source_report"), report_path, "source_report"
    )
    fold_path = _resolve_input(
        input_value.get("fold_report"), report_path, "fold_report"
    )
    index_path = _resolve_input(
        input_value.get("index_data"), report_path, "index_data"
    )
    current_code = {
        "preflight_code": Path(preflight.__file__).resolve(),
        "audit_code": Path(__file__).resolve(),
        "backtest_engine": preflight.BACKTEST_ENGINE.resolve(),
        "signal_engine": preflight.SIGNAL_ENGINE.resolve(),
    }
    for label, expected_path in current_code.items():
        actual_path = _resolve_input(input_value.get(label), report_path, label)
        _require(actual_path == expected_path, f"unexpected code path: {label}")

    artifact_value = report.get("artifacts")
    _require(isinstance(artifact_value, dict), "artifact section missing")
    _require(
        set(artifact_value) == set(preflight.ARTIFACT_NAMES),
        "artifact names differ from contract",
    )
    artifact_paths: dict[str, Path] = {}
    artifact_rows: dict[str, list[dict[str, Any]]] = {}
    artifact_records: dict[str, dict[str, Any]] = {}
    for name in preflight.ARTIFACT_NAMES:
        path, rows, record = _resolve_artifact(
            artifact_value.get(name), report_path, name
        )
        artifact_paths[name] = path
        artifact_rows[name] = rows
        artifact_records[name] = record
    _audit_candidate_rows(artifact_rows["candidate_features"])

    replay = _independent_snapshot(source_path, fold_path, index_path)
    for name in preflight.ARTIFACT_NAMES:
        _require(
            _canonical_json(artifact_rows[name]) == _canonical_json(replay[name]),
            f"full replay differs from artifact: {name}",
        )
    expected_report = preflight.assemble_report(
        replay,
        source_path,
        fold_path,
        index_path,
        report_path.parent,
        artifact_records,
    )
    _require(
        _canonical_json(report) == _canonical_json(expected_report),
        "report differs from full replay",
    )
    return {
        "report": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "candidate_count": len(artifact_rows["candidate_features"]),
        "factor_count": len(preflight.FACTOR_NAMES),
        "artifact_sha256": {
            name: _sha256_file(path) for name, path in artifact_paths.items()
        },
        "checks_passed": True,
        "_report_value": report,
        "_artifact_paths": artifact_paths,
    }


def _normalize_run_paths(value: Any, run_root: Path) -> Any:
    root_text = str(run_root.resolve())
    if isinstance(value, dict):
        return {
            key: _normalize_run_paths(item, run_root)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_normalize_run_paths(item, run_root) for item in value]
    if isinstance(value, str):
        if value == root_text:
            return "<RUN_ROOT>"
        prefix = root_text + str(Path("/"))
        windows_prefix = root_text + "\\"
        if value.startswith(windows_prefix):
            return "<RUN_ROOT>/" + value[len(windows_prefix) :].replace("\\", "/")
        if value.startswith(prefix):
            return "<RUN_ROOT>/" + value[len(prefix) :].replace("\\", "/")
    return value


def run_audit(primary_report: Path, verify_report: Path) -> dict[str, Any]:
    primary_report = _guard_development_path(primary_report)
    verify_report = _guard_development_path(verify_report)
    _require(primary_report != verify_report, "primary and verify reports must differ")
    primary = audit_run(primary_report)
    verify = audit_run(verify_report)

    artifact_checks: list[dict[str, Any]] = []
    for name in preflight.ARTIFACT_NAMES:
        primary_path = primary["_artifact_paths"][name]
        verify_path = verify["_artifact_paths"][name]
        identical = primary_path.read_bytes() == verify_path.read_bytes()
        artifact_checks.append(
            {
                "name": name,
                "primary_sha256": _sha256_file(primary_path),
                "verify_sha256": _sha256_file(verify_path),
                "artifact_byte_identical": identical,
            }
        )
    all_identical = all(row["artifact_byte_identical"] for row in artifact_checks)
    normalized_primary = _normalize_run_paths(
        primary["_report_value"], primary_report.parent
    )
    normalized_verify = _normalize_run_paths(
        verify["_report_value"], verify_report.parent
    )
    normalized_equal = _canonical_json(normalized_primary) == _canonical_json(
        normalized_verify
    )
    _require(all_identical, "primary/verify artifact bytes differ")
    _require(normalized_equal, "primary/verify normalized reports differ")
    return {
        "version": VERSION,
        "mode": "read_only_full_replay",
        "passes_audit": True,
        "primary": {
            key: value for key, value in primary.items() if not key.startswith("_")
        },
        "verify": {
            key: value for key, value in verify.items() if not key.startswith("_")
        },
        "artifact_determinism": artifact_checks,
        "determinism": {
            "checked": True,
            "artifact_count": len(artifact_checks),
            "all_artifacts_byte_identical": all_identical,
            "normalized_report_equal": normalized_equal,
            "checks_passed": True,
        },
        "policy": {
            "read_only": True,
            "full_feature_replay": True,
            "independent_factor_implementation": True,
            "network_used": False,
            "database_used": False,
            "sql_executed": False,
            "holdout_used": False,
            "production_eligible": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-report", type=Path, required=True)
    parser.add_argument("--verify-report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_audit(args.primary_report, args.verify_report)
    except Exception as exc:  # noqa: BLE001 - CLI fails closed as one JSON object.
        print(
            json.dumps(
                {"version": VERSION, "passes_audit": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
