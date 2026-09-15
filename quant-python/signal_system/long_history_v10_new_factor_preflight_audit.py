"""Read-only independent full-replay audit for two v10 factor preflights."""

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

import long_history_v10_new_factor_preflight as preflight

VERSION = "long_history_v10_new_factor_preflight_audit.v1"


class NewFactorPreflightAuditError(RuntimeError):
    """Raised when a v10 preflight or deterministic replay differs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise NewFactorPreflightAuditError(message)


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


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NewFactorPreflightAuditError(f"cannot read JSON: {path}: {exc}") from exc
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
        raise NewFactorPreflightAuditError(f"cannot read JSONL: {path}: {exc}") from exc
    return rows


def _resolve_input(record: Any, report_path: Path, label: str) -> Path:
    _require(isinstance(record, dict), f"input record missing: {label}")
    raw = record.get("path")
    _require(isinstance(raw, str) and raw, f"input path missing: {label}")
    path = Path(raw)
    if not path.is_absolute():
        path = report_path.parent / path
    path = _guard_development_path(path)
    _require(path.exists() and path.is_file(), f"input file missing: {path}")
    _require(
        str(record.get("sha256", "")) == _sha256_file(path),
        f"input SHA256 drift: {label}",
    )
    return path


def _resolve_artifact(
    record: Any, report_path: Path, label: str
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    path = _resolve_input(record, report_path, f"artifact.{label}")
    rows = _load_jsonl(path)
    _require(record.get("rows") == len(rows), f"artifact rows differ: {label}")
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
    return round(result, 12) if math.isfinite(result) else None


def _tail(values: pd.Series, length: int) -> np.ndarray | None:
    series = pd.to_numeric(values, errors="coerce").tail(length)
    if len(series) != length or series.isna().any():
        return None
    return series.to_numpy(dtype=float)


def _independent_lag_return(values: pd.Series, lag: int) -> float | None:
    data = _tail(values, lag + 1)
    if data is None or abs(float(data[0])) <= 1e-15:
        return None
    return _number(float(data[-1] / data[0] - 1.0))


def _independent_returns(values: pd.Series, periods: int) -> np.ndarray | None:
    data = _tail(values, periods + 1)
    if data is None or (np.abs(data[:-1]) <= 1e-15).any():
        return None
    result = data[1:] / data[:-1] - 1.0
    return result if len(result) == periods and np.isfinite(result).all() else None


def _independent_volatility(values: pd.Series, periods: int) -> float | None:
    returns = _independent_returns(values, periods)
    return None if returns is None else _number(float(np.std(returns, ddof=0)))


def _independent_drawdown(values: pd.Series, window: int) -> float | None:
    data = _tail(values, window)
    if data is None:
        return None
    peak = float(np.max(data))
    return None if abs(peak) <= 1e-15 else _number(float(data[-1] / peak - 1.0))


def _independent_efficiency(values: pd.Series, periods: int) -> float | None:
    returns = _independent_returns(values, periods)
    lag_return = _independent_lag_return(values, periods)
    if returns is None or lag_return is None:
        return None
    path = float(np.abs(returns).sum())
    return None if path <= 1e-15 else _number(abs(lag_return) / path)


def _independent_mean_ratio(values: pd.Series, short: int, long: int) -> float | None:
    data = _tail(values, long)
    if data is None:
        return None
    denominator = float(np.mean(data))
    return (
        None
        if abs(denominator) <= 1e-15
        else _number(float(np.mean(data[-short:])) / denominator)
    )


def _independent_pearson(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) != len(right) or len(left) == 0:
        return None
    left_delta = left - float(np.mean(left))
    right_delta = right - float(np.mean(right))
    denominator = math.sqrt(
        max(float(left_delta @ left_delta) * float(right_delta @ right_delta), 0.0)
    )
    return (
        None
        if denominator <= 1e-24
        else _number(float(left_delta @ right_delta) / denominator)
    )


def _independent_market_metrics(
    stock: pd.DataFrame, index: pd.DataFrame
) -> tuple[float | None, float | None]:
    left = stock[["datetime", "close"]].rename(columns={"close": "stock"})
    right = index[["datetime", "close"]].rename(columns={"close": "index"})
    merged = left.merge(right, on="datetime", how="inner").sort_values("datetime")
    values = merged[["stock", "index"]].tail(21)
    if len(values) != 21 or values.isna().any().any():
        return None, None
    raw = values.to_numpy(dtype=float)
    if (np.abs(raw[:-1]) <= 1e-15).any():
        return None, None
    returns = raw[1:] / raw[:-1] - 1.0
    stock_returns = returns[:, 0]
    index_returns = returns[:, 1]
    correlation = _independent_pearson(stock_returns, index_returns)
    index_delta = index_returns - float(np.mean(index_returns))
    index_ss = float(index_delta @ index_delta)
    if index_ss <= 1e-24:
        return correlation, None
    stock_delta = stock_returns - float(np.mean(stock_returns))
    beta = float(stock_delta @ index_delta) / index_ss
    residual = stock_returns - beta * index_returns
    return correlation, _number(float(np.std(residual, ddof=0)))


def _independent_effective_amount(frame: pd.DataFrame) -> pd.Series:
    if "amount" in frame:
        amount = pd.to_numeric(frame["amount"], errors="coerce")
    else:
        amount = pd.Series(np.nan, index=frame.index, dtype=float)
    if {"close", "volume"}.issubset(frame.columns):
        estimate = pd.to_numeric(frame["close"], errors="coerce") * pd.to_numeric(
            frame["volume"], errors="coerce"
        )
        amount = amount.where(amount > 0, estimate)
    return amount


def _independent_return_volume_correlation(frame: pd.DataFrame) -> float | None:
    if not {"close", "volume"}.issubset(frame.columns):
        return None
    close = _tail(frame["close"], 21)
    volume = _tail(frame["volume"], 21)
    if close is None or volume is None or (volume <= 0).any():
        return None
    if (np.abs(close[:-1]) <= 1e-15).any():
        return None
    returns = close[1:] / close[:-1] - 1.0
    volume_change = np.diff(np.log(volume))
    return _independent_pearson(returns, volume_change)


def _independent_candidate_features(
    candidate: dict[str, Any], qfq: pd.DataFrame, index: pd.DataFrame
) -> dict[str, Any]:
    """Second implementation of all twelve formulas used only by the audit."""

    _require("close" in qfq.columns and not qfq.empty, "QFQ close is missing")
    _require("close" in index.columns and not index.empty, "index close is missing")
    stock_close = pd.to_numeric(qfq["close"], errors="coerce")
    index_close = pd.to_numeric(index["close"], errors="coerce")
    stock_return_5 = _independent_lag_return(stock_close, 5)
    index_return_5 = _independent_lag_return(index_close, 5)
    correlation, residual_volatility = _independent_market_metrics(qfq, index)
    volume = (
        pd.to_numeric(qfq["volume"], errors="coerce")
        if "volume" in qfq
        else pd.Series(np.nan, index=qfq.index, dtype=float)
    )
    amount = _independent_effective_amount(qfq)
    features: dict[str, float | None] = {
        "index_return_60": _independent_lag_return(index_close, 60),
        "index_realized_volatility_20": _independent_volatility(index_close, 20),
        "index_drawdown_from_high_60": _independent_drawdown(index_close, 60),
        "excess_return_5": (
            _number(stock_return_5 - index_return_5)
            if stock_return_5 is not None and index_return_5 is not None
            else None
        ),
        "stock_index_correlation_20": correlation,
        "residual_volatility_20": residual_volatility,
        "stock_realized_volatility_20": _independent_volatility(stock_close, 20),
        "drawdown_from_high_60": _independent_drawdown(stock_close, 60),
        "price_efficiency_20": _independent_efficiency(stock_close, 20),
        "volume_mean_5_to_20": _independent_mean_ratio(volume, 5, 20),
        "amount_mean_5_to_20": _independent_mean_ratio(amount, 5, 20),
        "return_volume_change_correlation_20": (
            _independent_return_volume_correlation(qfq)
        ),
    }
    _require(
        tuple(features) == preflight.FACTOR_NAMES,
        f"independent factor schema differs: {candidate.get('candidate_id')}",
    )
    return {
        "features": features,
        "missing": {name: features[name] is None for name in preflight.FACTOR_NAMES},
    }


def _independent_snapshot(
    v7_factor_report_path: Path,
    source_report_path: Path,
    fold_report_path: Path,
    index_data_path: Path,
    v9a_report_path: Path,
) -> dict[str, Any]:
    original = preflight.compute_candidate_features
    preflight.compute_candidate_features = _independent_candidate_features
    try:
        return preflight.build_snapshot(
            v7_factor_report_path,
            source_report_path,
            fold_report_path,
            index_data_path,
            v9a_report_path,
        )
    finally:
        preflight.compute_candidate_features = original


def _audit_candidate_rows(rows: list[dict[str, Any]]) -> None:
    expected = {
        *preflight.PUBLIC_CANDIDATE_FIELDS,
        "features",
        "missing",
        "data_cutoffs",
    }
    seen: set[str] = set()
    for row in rows:
        _require(set(row) == expected, "unexpected candidate fields")
        identifier = str(row.get("candidate_id", ""))
        _require(
            identifier and identifier not in seen, f"duplicate candidate: {identifier}"
        )
        seen.add(identifier)
        features = row.get("features")
        missing = row.get("missing")
        _require(
            isinstance(features, dict) and set(features) == set(preflight.FACTOR_NAMES),
            f"factor schema differs: {identifier}",
        )
        _require(
            isinstance(missing, dict) and set(missing) == set(preflight.FACTOR_NAMES),
            f"missing schema differs: {identifier}",
        )
        _require(
            all(bool(missing[name]) is (features[name] is None) for name in features),
            f"missing flags differ: {identifier}",
        )
        signal_day = date.fromisoformat(str(row["signal_day"]))
        cutoffs = row.get("data_cutoffs")
        _require(
            isinstance(cutoffs, dict)
            and set(cutoffs) == {"qfq_max_day_used", "index_max_day_used"},
            f"cutoffs differ: {identifier}",
        )
        _require(
            all(
                date.fromisoformat(str(value)) <= signal_day
                for value in cutoffs.values()
            ),
            f"future cutoff found: {identifier}",
        )
        bad_keys = [
            key
            for key in _walk_keys(row)
            if any(token in key for token in preflight.OUTCOME_TOKENS)
        ]
        _require(not bad_keys, f"outcome-like fields emitted: {identifier}: {bad_keys}")


def audit_run(report_path: Path) -> dict[str, Any]:
    report_path = _guard_development_path(report_path)
    _require(
        report_path.exists() and report_path.is_file(), f"missing report: {report_path}"
    )
    report = _load_json(report_path)
    _require(report.get("version") == preflight.VERSION, "unexpected version")
    _require(report.get("passes_preflight") is True, "preflight did not pass")
    _require(report.get("model_fitted") is False, "preflight fitted a model")
    _require(
        report.get("candidate_outcomes_read") is False,
        "preflight read candidate outcomes",
    )
    _require(
        report.get("factor_selection_performed") is False,
        "preflight selected factors",
    )
    _require(report.get("holdout_used") is False, "preflight used Holdout")
    _require(report.get("production_eligible") is False, "production eligible")
    contract = report.get("factor_contract")
    _require(isinstance(contract, dict), "factor contract missing")
    _require(
        contract.get("factor_names") == list(preflight.FACTOR_NAMES),
        "factor names differ",
    )
    _require(
        contract.get("factor_specs") == list(preflight.FACTOR_SPECS),
        "factor specs differ",
    )
    _require(
        contract.get("disjoint_from_v7_factor_names") is True,
        "new factors overlap v7",
    )
    _require(
        contract.get("coverage_is_not_a_selection_rule") is True,
        "coverage became a selection rule",
    )

    inputs = report.get("input")
    _require(isinstance(inputs, dict), "input section missing")
    v7_factor_report_path = _resolve_input(
        inputs.get("v7_factor_report"), report_path, "v7_factor_report"
    )
    source_report_path = _resolve_input(
        inputs.get("source_report"), report_path, "source_report"
    )
    fold_report_path = _resolve_input(
        inputs.get("fold_report"), report_path, "fold_report"
    )
    index_data_path = _resolve_input(
        inputs.get("index_data"), report_path, "index_data"
    )
    v9a_report_path = _resolve_input(
        inputs.get("v9a_parent_report"), report_path, "v9a_parent_report"
    )
    _require(
        _sha256_file(v7_factor_report_path) == preflight.V7_FACTOR_REPORT_SHA256,
        "v7 factor report anchor drift",
    )
    _require(
        _sha256_file(v9a_report_path) == preflight.V9A_PARENT_REPORT_SHA256,
        "v9a parent report anchor drift",
    )
    expected_code = {
        "preflight_code": Path(preflight.__file__).resolve(),
        "audit_code": Path(__file__).resolve(),
        "v7_preflight_code": preflight.V7_PREFLIGHT_CODE,
        "v7_audit_code": preflight.V7_AUDIT_CODE,
        "v9a_parent_code": preflight.V9A_PARENT_CODE,
    }
    for label, expected in expected_code.items():
        actual = _resolve_input(inputs.get(label), report_path, label)
        _require(actual == expected.resolve(), f"unexpected code path: {label}")

    artifact_value = report.get("artifacts")
    _require(isinstance(artifact_value, dict), "artifact section missing")
    _require(
        set(artifact_value) == set(preflight.ARTIFACT_NAMES),
        "artifact names differ",
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

    replay = _independent_snapshot(
        v7_factor_report_path,
        source_report_path,
        fold_report_path,
        index_data_path,
        v9a_report_path,
    )
    for name in preflight.ARTIFACT_NAMES:
        _require(
            _canonical_json(artifact_rows[name]) == _canonical_json(replay[name]),
            f"independent full replay differs: {name}",
        )
    expected_report = preflight.assemble_report(
        replay,
        v7_factor_report_path,
        source_report_path,
        fold_report_path,
        index_data_path,
        v9a_report_path,
        artifact_records,
    )
    _require(
        _canonical_json(report) == _canonical_json(expected_report),
        "report differs from independent full replay",
    )
    return {
        "report": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "candidate_count": len(artifact_rows["candidate_features"]),
        "factor_count": len(preflight.FACTOR_NAMES),
        "artifact_sha256": {
            name: _sha256_file(path) for name, path in artifact_paths.items()
        },
        "input_v7_factor_preflight_replayed": True,
        "parent_v9a_failure_hash_anchored": True,
        "independent_factor_implementation": True,
        "checks_passed": True,
        "_report_value": report,
        "_artifact_paths": artifact_paths,
    }


def _normalize_run_paths(value: Any, run_root: Path) -> Any:
    root = str(run_root.resolve())
    if isinstance(value, dict):
        return {
            key: _normalize_run_paths(item, run_root)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_normalize_run_paths(item, run_root) for item in value]
    if isinstance(value, str):
        for separator in ("\\", "/"):
            prefix = root + separator
            if value.startswith(prefix):
                return "<RUN_ROOT>/" + value[len(prefix) :].replace("\\", "/")
        if value == root:
            return "<RUN_ROOT>"
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
        "mode": "read_only_independent_full_feature_replay",
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
        "input_v7_factor_preflight_replayed": True,
        "parent_v9a_failure_hash_anchored": True,
        "policy": {
            "read_only": True,
            "full_feature_replay": True,
            "independent_factor_implementation": True,
            "coverage_used_for_selection": False,
            "candidate_outcomes_read": False,
            "factor_selection_performed": False,
            "hyperparameters_selected": False,
            "model_fitted": False,
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
    except Exception as exc:  # noqa: BLE001 - audit CLI fails closed as one JSON.
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
