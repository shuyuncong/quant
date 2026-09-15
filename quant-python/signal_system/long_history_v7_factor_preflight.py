"""Build a causal, outcome-free v7 factor snapshot from frozen v5 inputs.

This development-only preflight never fits a model, reads Holdout data, uses a
network/database, or selects factors from outcomes.  It writes only to a new
output directory and fails closed when any frozen input drifts.
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

from backtest_winrate import load_stock_pool_history
from long_history_walk_forward_audit import (
    audit_fold_report,
    audit_source_report,
)
from strategy.macd import calculate_macd

VERSION = "long_history_v7_factor_preflight.v2"
DATASET_STATUS = "development_viewed_not_holdout"
STOCK_POOL_HISTORY_BARS = 1200
STOCK_POOL_CACHE_DIR = BASE_DIR / "cache"
AUDIT_CODE = BASE_DIR / "long_history_v7_factor_preflight_audit.py"
BACKTEST_ENGINE = BASE_DIR / "backtest_winrate.py"
SIGNAL_ENGINE = BASE_DIR / "strategy" / "macd.py"

FACTOR_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "confirmation_bars",
        "family": "signal_freshness",
        "source": "candidate",
        "formula": "frozen source confirmation_bars",
        "minimum_bars": 0,
    },
    {
        "name": "cross_age_trading_days",
        "family": "signal_freshness",
        "source": "qfq+candidate",
        "formula": "QFQ position(signal_day) - QFQ position(cross_day)",
        "minimum_bars": 1,
    },
    {
        "name": "macd_hist_ratio",
        "family": "macd_shape",
        "source": "qfq",
        "formula": "MACD_hist[t] / abs(close[t])",
        "minimum_bars": 34,
    },
    {
        "name": "macd_hist_slope_3",
        "family": "macd_shape",
        "source": "qfq",
        "formula": "(MACD_hist[t] - MACD_hist[t-3]) / abs(close[t])",
        "minimum_bars": 37,
    },
    {
        "name": "macd_hist_acceleration",
        "family": "macd_shape",
        "source": "qfq",
        "formula": "(hist[t] - 2*hist[t-1] + hist[t-2]) / abs(close[t])",
        "minimum_bars": 36,
    },
    {
        "name": "dif_slope_3",
        "family": "macd_shape",
        "source": "qfq",
        "formula": "(DIF[t] - DIF[t-3]) / abs(close[t])",
        "minimum_bars": 37,
    },
    {
        "name": "dea_slope_3",
        "family": "macd_shape",
        "source": "qfq",
        "formula": "(DEA[t] - DEA[t-3]) / abs(close[t])",
        "minimum_bars": 37,
    },
    {
        "name": "hist_expanding_3",
        "family": "macd_shape",
        "source": "qfq",
        "formula": "int(0 < hist[t-2] < hist[t-1] < hist[t])",
        "minimum_bars": 36,
    },
    {
        "name": "excess_return_20",
        "family": "relative_strength",
        "source": "qfq+index",
        "formula": "stock close[t]/close[t-20]-1 minus index equivalent",
        "minimum_bars": 21,
    },
    {
        "name": "excess_return_60",
        "family": "relative_strength",
        "source": "qfq+index",
        "formula": "stock close[t]/close[t-60]-1 minus index equivalent",
        "minimum_bars": 61,
    },
    {
        "name": "beta_60",
        "family": "market_environment",
        "source": "qfq+index",
        "formula": "sum((stock_ret-mean)*(index_ret-mean))/sum((index_ret-mean)^2) on 60 aligned returns",
        "minimum_bars": 61,
    },
    {
        "name": "index_return_20",
        "family": "market_environment",
        "source": "index",
        "formula": "index close[t]/close[t-20]-1",
        "minimum_bars": 21,
    },
    {
        "name": "index_above_ma60",
        "family": "market_environment",
        "source": "index",
        "formula": "int(index close[t] > mean(index close[t-59:t]))",
        "minimum_bars": 60,
    },
    {
        "name": "stock_index_correlation_60",
        "family": "market_environment",
        "source": "qfq+index",
        "formula": "Pearson correlation of 60 aligned stock/index daily returns",
        "minimum_bars": 61,
    },
    {
        "name": "amount_to_median_20",
        "family": "liquidity_crowding",
        "source": "stock_pool",
        "formula": "effective_amount[t] / median(effective_amount[t-19:t])",
        "minimum_bars": 20,
    },
    {
        "name": "turnover_to_median_20",
        "family": "liquidity_crowding",
        "source": "stock_pool",
        "formula": "turnover[t] / median(turnover[t-19:t])",
        "minimum_bars": 20,
    },
    {
        "name": "turnover_percentile_60",
        "family": "liquidity_crowding",
        "source": "stock_pool",
        "formula": "mean(turnover[t-59:t] <= turnover[t])",
        "minimum_bars": 60,
    },
    {
        "name": "amplitude_percentile_60",
        "family": "liquidity_crowding",
        "source": "qfq",
        "formula": "empirical percentile of (high-low)/abs(prev_close) over latest 60 amplitudes",
        "minimum_bars": 61,
    },
    {
        "name": "circulating_market_cap",
        "family": "liquidity_crowding",
        "source": "stock_pool",
        "formula": "latest positive circulating_market_cap",
        "minimum_bars": 1,
    },
)
FACTOR_NAMES = tuple(spec["name"] for spec in FACTOR_SPECS)
CORE_CANDIDATE_FIELDS = (
    "candidate_id",
    "symbol",
    "signal_day",
    "signal_type",
)
CROSS_METADATA_SIGNAL_PREFIX = "macd_golden_cross_pullback_confirmed_"
FORBIDDEN_OUTCOME_TOKENS = (
    "pnl",
    "profit",
    "return_pct",
    "exit",
    "mfe",
    "mae",
    "future",
    "post_exit",
)
ARTIFACT_NAMES = (
    "candidate_features",
    "qfq_manifest",
    "stock_pool_manifest",
    "coverage",
)


class FactorPreflightError(RuntimeError):
    """Raised when a frozen input or causal feature contract is violated."""


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise FactorPreflightError(f"Holdout path is blocked: {resolved}")
    return resolved


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _input_record(path: Path) -> dict[str, Any]:
    path = _guard_development_path(path)
    if not path.exists() or not path.is_file():
        raise FactorPreflightError(f"input file does not exist: {path}")
    return {"path": str(path), "sha256": _sha256_file(path)}


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


def _parse_day(value: Any, field: str, identifier: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise FactorPreflightError(
            f"invalid {field} for {identifier}: {value}"
        ) from exc


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 12)


def _prepare_history(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "datetime" not in frame:
        raise FactorPreflightError(f"empty or invalid history: {label}")
    prepared = frame.copy()
    prepared["datetime"] = pd.to_datetime(prepared["datetime"], errors="coerce")
    prepared = prepared.dropna(subset=["datetime"])
    if "is_closed" in prepared:
        prepared = prepared[prepared["is_closed"].fillna(False).astype(bool)]
    numeric = (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turnover_rate",
        "circulating_market_cap",
    )
    for column in numeric:
        if column in prepared:
            prepared[column] = pd.to_numeric(prepared[column], errors="coerce")
    prepared = (
        prepared.sort_values("datetime")
        .drop_duplicates("datetime", keep="last")
        .reset_index(drop=True)
    )
    if prepared.empty:
        raise FactorPreflightError(f"history has no closed dated bars: {label}")
    return prepared


def _as_of(frame: pd.DataFrame, signal_day: date, *, label: str) -> pd.DataFrame:
    result = frame[frame["datetime"].dt.date <= signal_day].reset_index(drop=True)
    if result.empty:
        raise FactorPreflightError(
            f"history has no bar on or before {signal_day.isoformat()}: {label}"
        )
    max_day = result["datetime"].iloc[-1].date()
    if max_day > signal_day:
        raise FactorPreflightError(f"future bar used by {label}: {max_day}")
    return result


def _window_return(values: pd.Series, lag: int) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce")
    if len(numeric) < lag + 1:
        return None
    current = _finite(numeric.iloc[-1])
    previous = _finite(numeric.iloc[-lag - 1])
    if current is None or previous is None or abs(previous) <= 1e-15:
        return None
    return _finite(current / previous - 1.0)


def _ratio_to_median(values: pd.Series, window: int) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").tail(window)
    if len(numeric) < window or numeric.isna().any():
        return None
    current = _finite(numeric.iloc[-1])
    median = _finite(numeric.median())
    if current is None or median is None or abs(median) <= 1e-15:
        return None
    return _finite(current / median)


def _percentile_latest(values: pd.Series, window: int) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").tail(window)
    if len(numeric) < window or numeric.isna().any():
        return None
    latest = _finite(numeric.iloc[-1])
    if latest is None:
        return None
    return _finite(float((numeric <= latest).mean()))


def _aligned_market_metrics(
    stock: pd.DataFrame, index: pd.DataFrame
) -> tuple[float | None, float | None]:
    left = stock[["datetime", "close"]].rename(columns={"close": "stock_close"})
    right = index[["datetime", "close"]].rename(columns={"close": "index_close"})
    merged = left.merge(right, on="datetime", how="inner").sort_values("datetime")
    if len(merged) < 61:
        return None, None
    returns = merged[["stock_close", "index_close"]].pct_change(fill_method=None)
    returns = returns.replace([np.inf, -np.inf], np.nan).dropna().tail(60)
    if len(returns) < 60:
        return None, None
    stock_values = returns["stock_close"].to_numpy(dtype=float)
    index_values = returns["index_close"].to_numpy(dtype=float)
    stock_centered = stock_values - float(stock_values.mean())
    index_centered = index_values - float(index_values.mean())
    index_ss = float(index_centered @ index_centered)
    stock_ss = float(stock_centered @ stock_centered)
    cross = float(stock_centered @ index_centered)
    beta = None if index_ss <= 1e-24 else _finite(cross / index_ss)
    denominator = math.sqrt(max(stock_ss * index_ss, 0.0))
    correlation = None if denominator <= 1e-24 else _finite(cross / denominator)
    return beta, correlation


def _effective_amount(frame: pd.DataFrame, volume_unit_shares: float) -> pd.Series:
    if "amount" in frame:
        amount = pd.to_numeric(frame["amount"], errors="coerce")
    else:
        amount = pd.Series(np.nan, index=frame.index, dtype=float)
    if {"close", "volume"}.issubset(frame.columns):
        estimated = (
            pd.to_numeric(frame["close"], errors="coerce")
            * pd.to_numeric(frame["volume"], errors="coerce")
            * volume_unit_shares
        )
        amount = amount.where(amount > 0, estimated)
    return amount


def _sanitize_candidate(row: dict[str, Any]) -> dict[str, Any]:
    identifier = str(row.get("candidate_id", ""))
    if not identifier:
        raise FactorPreflightError("candidate is missing candidate_id")
    missing = [field for field in CORE_CANDIDATE_FIELDS if row.get(field) is None]
    if missing:
        raise FactorPreflightError(
            f"candidate {identifier} missing core fields: {', '.join(missing)}"
        )
    symbol = str(row["symbol"])
    if len(symbol) != 6 or not symbol.isdigit():
        raise FactorPreflightError(f"invalid symbol for {identifier}: {symbol}")
    signal_day = _parse_day(row["signal_day"], "signal_day", identifier)
    signal_type = str(row["signal_type"])
    raw_cross_day = row.get("cross_day")
    raw_confirmation_bars = row.get("confirmation_bars")
    cross_present = raw_cross_day is not None and str(raw_cross_day) != ""
    bars_present = (
        raw_confirmation_bars is not None and str(raw_confirmation_bars) != ""
    )
    if cross_present is not bars_present:
        raise FactorPreflightError(
            f"partial cross metadata for {identifier}: cross_day and "
            "confirmation_bars must both be present or both be absent"
        )
    if signal_type.startswith(CROSS_METADATA_SIGNAL_PREFIX) and not cross_present:
        raise FactorPreflightError(
            f"MACD pullback candidate {identifier} is missing cross metadata"
        )
    cross_day: date | None = None
    confirmation_bars: int | None = None
    if cross_present:
        cross_day = _parse_day(raw_cross_day, "cross_day", identifier)
        if cross_day > signal_day:
            raise FactorPreflightError(f"cross_day follows signal_day: {identifier}")
        try:
            confirmation_bars = int(raw_confirmation_bars)
        except (TypeError, ValueError) as exc:
            raise FactorPreflightError(
                f"invalid confirmation_bars for {identifier}: {raw_confirmation_bars}"
            ) from exc
        if confirmation_bars < 0 or float(raw_confirmation_bars) != confirmation_bars:
            raise FactorPreflightError(
                f"invalid confirmation_bars for {identifier}: {raw_confirmation_bars}"
            )
    return {
        "candidate_id": identifier,
        "symbol": symbol,
        "signal_day": signal_day.isoformat(),
        "signal_type": signal_type,
        "source_fields": {
            "cross_day": cross_day.isoformat() if cross_day is not None else None,
            "confirmation_bars": confirmation_bars,
        },
    }


def _candidate_sort_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row["signal_day"]),
        str(row["signal_type"]),
        str(row["symbol"]),
        str(row["candidate_id"]),
    )


def compute_candidate_features(
    candidate: dict[str, Any],
    qfq: pd.DataFrame,
    index: pd.DataFrame,
    stock_pool: pd.DataFrame,
    *,
    volume_unit_shares: float,
) -> dict[str, Any]:
    """Compute the frozen 19-factor schema from causal, already-cut frames."""
    identifier = str(candidate["candidate_id"])
    signal_day = _parse_day(candidate["signal_day"], "signal_day", identifier)
    qfq_days = qfq["datetime"].dt.date.tolist()
    if signal_day not in qfq_days:
        raise FactorPreflightError(f"signal_day is absent from QFQ: {identifier}")
    raw_cross_day = candidate["source_fields"]["cross_day"]
    confirmation_bars = candidate["source_fields"]["confirmation_bars"]
    cross_age: int | None = None
    if raw_cross_day is not None:
        cross_day = _parse_day(raw_cross_day, "cross_day", identifier)
        if cross_day not in qfq_days:
            raise FactorPreflightError(f"cross_day is absent from QFQ: {identifier}")
        cross_age = qfq_days.index(signal_day) - qfq_days.index(cross_day)
        if cross_age < 0:
            raise FactorPreflightError(f"negative cross age: {identifier}")

    features: dict[str, float | int | None] = {
        "confirmation_bars": (
            int(confirmation_bars) if confirmation_bars is not None else None
        ),
        "cross_age_trading_days": cross_age,
    }
    close = pd.to_numeric(qfq.get("close"), errors="coerce")
    if close is None or close.empty:
        raise FactorPreflightError(f"QFQ close is missing: {identifier}")
    scale = _finite(abs(float(close.iloc[-1])))
    macd = calculate_macd(close, fast=12, slow=26, signal=9)
    hist = pd.to_numeric(macd["hist"], errors="coerce")
    dif = pd.to_numeric(macd["dif"], errors="coerce")
    dea = pd.to_numeric(macd["dea"], errors="coerce")

    def normalized(value: Any) -> float | None:
        number = _finite(value)
        if number is None or scale is None or scale <= 1e-15:
            return None
        return _finite(number / scale)

    features["macd_hist_ratio"] = normalized(hist.iloc[-1])
    features["macd_hist_slope_3"] = (
        normalized(hist.iloc[-1] - hist.iloc[-4]) if len(hist) >= 4 else None
    )
    features["macd_hist_acceleration"] = (
        normalized(hist.iloc[-1] - 2.0 * hist.iloc[-2] + hist.iloc[-3])
        if len(hist) >= 3
        else None
    )
    features["dif_slope_3"] = (
        normalized(dif.iloc[-1] - dif.iloc[-4]) if len(dif) >= 4 else None
    )
    features["dea_slope_3"] = (
        normalized(dea.iloc[-1] - dea.iloc[-4]) if len(dea) >= 4 else None
    )
    recent_hist = hist.tail(3)
    features["hist_expanding_3"] = (
        None
        if len(recent_hist) < 3 or recent_hist.isna().any()
        else int(
            bool(
                (recent_hist > 0).all()
                and abs(float(recent_hist.iloc[0])) < abs(float(recent_hist.iloc[1]))
                and abs(float(recent_hist.iloc[1])) < abs(float(recent_hist.iloc[2]))
            )
        )
    )

    stock_return_20 = _window_return(close, 20)
    stock_return_60 = _window_return(close, 60)
    index_close = pd.to_numeric(index.get("close"), errors="coerce")
    if index_close is None or index_close.empty:
        raise FactorPreflightError("index close is missing")
    index_return_20 = _window_return(index_close, 20)
    index_return_60 = _window_return(index_close, 60)
    features["excess_return_20"] = (
        _finite(stock_return_20 - index_return_20)
        if stock_return_20 is not None and index_return_20 is not None
        else None
    )
    features["excess_return_60"] = (
        _finite(stock_return_60 - index_return_60)
        if stock_return_60 is not None and index_return_60 is not None
        else None
    )
    beta, correlation = _aligned_market_metrics(qfq, index)
    features["beta_60"] = beta
    features["index_return_20"] = index_return_20
    latest_index = _finite(index_close.iloc[-1])
    index_tail60 = index_close.tail(60)
    index_ma60 = (
        _finite(index_tail60.mean())
        if len(index_tail60) == 60 and not index_tail60.isna().any()
        else None
    )
    features["index_above_ma60"] = (
        int(latest_index > index_ma60)
        if latest_index is not None and index_ma60 is not None
        else None
    )
    features["stock_index_correlation_60"] = correlation

    amount = _effective_amount(stock_pool, volume_unit_shares)
    features["amount_to_median_20"] = _ratio_to_median(amount, 20)
    turnover = (
        pd.to_numeric(stock_pool["turnover_rate"], errors="coerce")
        if "turnover_rate" in stock_pool
        else pd.Series(np.nan, index=stock_pool.index, dtype=float)
    )
    features["turnover_to_median_20"] = _ratio_to_median(turnover, 20)
    features["turnover_percentile_60"] = _percentile_latest(turnover, 60)
    if {"high", "low", "close"}.issubset(qfq.columns):
        previous_close = pd.to_numeric(qfq["close"], errors="coerce").shift(1).abs()
        amplitude = (
            pd.to_numeric(qfq["high"], errors="coerce")
            - pd.to_numeric(qfq["low"], errors="coerce")
        ) / previous_close.replace(0, np.nan)
    else:
        amplitude = pd.Series(np.nan, index=qfq.index, dtype=float)
    features["amplitude_percentile_60"] = _percentile_latest(amplitude, 60)
    market_cap = (
        pd.to_numeric(stock_pool["circulating_market_cap"], errors="coerce").iloc[-1]
        if "circulating_market_cap" in stock_pool
        else None
    )
    market_cap_value = _finite(market_cap)
    features["circulating_market_cap"] = (
        market_cap_value
        if market_cap_value is not None and market_cap_value > 0
        else None
    )

    if tuple(features) != FACTOR_NAMES:
        raise FactorPreflightError("factor implementation order differs from contract")
    missing = {name: features[name] is None for name in FACTOR_NAMES}
    return {"features": features, "missing": missing}


def _volume_unit_shares(source_audit: dict[str, Any]) -> float:
    report = source_audit.get("_report_value")
    report = report if isinstance(report, dict) else {}
    filters = report.get("filters")
    filters = filters if isinstance(filters, dict) else {}
    stock_pool = filters.get("stock_pool")
    stock_pool = stock_pool if isinstance(stock_pool, dict) else {}
    value = _finite(stock_pool.get("volume_unit_shares"))
    if value is None or value <= 0:
        raise FactorPreflightError("missing frozen stock_pool.volume_unit_shares")
    return value


def _source_end(source_audit: dict[str, Any]) -> date:
    report = source_audit.get("_report_value")
    report = report if isinstance(report, dict) else {}
    window = report.get("window")
    window = window if isinstance(window, dict) else {}
    return _parse_day(window.get("end"), "source window end", "source_report")


def _fold_history_dir(fold_audit: dict[str, Any]) -> Path:
    report = fold_audit.get("_report_value")
    report = report if isinstance(report, dict) else {}
    input_value = report.get("input")
    input_value = input_value if isinstance(input_value, dict) else {}
    qfq = input_value.get("qfq_history")
    qfq = qfq if isinstance(qfq, dict) else {}
    raw = qfq.get("history_dir")
    if not raw:
        raise FactorPreflightError("fold report lacks QFQ history_dir")
    return _guard_development_path(Path(str(raw)))


def _coverage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    total = len(rows)
    specs = {str(spec["name"]): spec for spec in FACTOR_SPECS}
    for name in FACTOR_NAMES:
        missing = sum(bool(row["missing"][name]) for row in rows)
        result.append(
            {
                **specs[name],
                "candidates": total,
                "non_missing": total - missing,
                "missing": missing,
                "coverage_pct": round((total - missing) / total * 100.0, 6),
            }
        )
    return result


def build_snapshot(
    source_report_path: Path,
    fold_report_path: Path,
    index_data_path: Path,
) -> dict[str, Any]:
    """Compute all preflight values in memory without writing output files."""
    source_report_path = _guard_development_path(source_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    index_data_path = _guard_development_path(index_data_path)
    source_audit = audit_source_report(source_report_path)
    fold_audit = audit_fold_report(fold_report_path, source_audit)
    if (
        source_audit.get("checks_passed") is not True
        or fold_audit.get("checks_passed") is not True
    ):
        raise FactorPreflightError("v5 source/fold audit did not pass")
    if source_audit.get("candidate_ids_sha256") != fold_audit.get(
        "dataset_candidate_ids_sha256"
    ):
        raise FactorPreflightError("source and fold candidate manifests differ")
    candidates = source_audit.get("_candidates")
    if not isinstance(candidates, list) or not candidates:
        raise FactorPreflightError("audited source has no candidates")
    if len(candidates) != int(fold_audit.get("dataset_candidates", -1)):
        raise FactorPreflightError("source and fold candidate counts differ")

    frozen_index = source_audit["frozen_inputs"]["index_data"]
    frozen_index_path = Path(str(frozen_index["path"])).resolve()
    if index_data_path != frozen_index_path:
        raise FactorPreflightError("explicit index path differs from frozen v5 index")
    if _sha256_file(index_data_path) != str(frozen_index["sha256"]):
        raise FactorPreflightError("frozen index SHA256 drift")
    index_frame = _prepare_history(pd.read_pickle(index_data_path), label="index")

    history_dir = _fold_history_dir(fold_audit)
    qfq_audit = source_audit["frozen_inputs"]["qfq_history"]
    qfq_paths = qfq_audit.get("_symbol_path")
    qfq_hashes = qfq_audit.get("_symbol_sha256")
    if not isinstance(qfq_paths, dict) or not isinstance(qfq_hashes, dict):
        raise FactorPreflightError("source audit lacks full QFQ path/hash manifest")
    frozen_stock_paths = {
        str(Path(raw).resolve()): str(digest)
        for raw, digest in source_audit["stock_pool_cache"]
        .get("_path_sha256", {})
        .items()
    }
    if not frozen_stock_paths:
        raise FactorPreflightError("source audit lacks stock-pool cache manifest")
    volume_unit_shares = _volume_unit_shares(source_audit)
    source_end = _source_end(source_audit)

    sanitized = [_sanitize_candidate(row) for row in candidates]
    sanitized.sort(key=_candidate_sort_key)
    identifiers = [str(row["candidate_id"]) for row in sanitized]
    if len(identifiers) != len(set(identifiers)):
        raise FactorPreflightError("duplicate candidate_id after sanitization")

    qfq_cache: dict[str, pd.DataFrame] = {}
    qfq_manifest: dict[str, dict[str, Any]] = {}
    stock_pool_cache: dict[str, tuple[pd.DataFrame, dict[str, Any]]] = {}
    stock_pool_manifest: dict[tuple[str, str], dict[str, Any]] = {}
    candidate_features: list[dict[str, Any]] = []
    for candidate in sanitized:
        symbol = str(candidate["symbol"])
        signal_day = _parse_day(
            candidate["signal_day"], "signal_day", candidate["candidate_id"]
        )
        if symbol not in qfq_cache:
            raw_path = qfq_paths.get(symbol)
            expected_hash = qfq_hashes.get(symbol)
            if not raw_path or not expected_hash:
                raise FactorPreflightError(
                    f"symbol absent from frozen QFQ manifest: {symbol}"
                )
            path = _guard_development_path(Path(str(raw_path)))
            expected_path = (history_dir / f"{symbol}_qfq.pkl").resolve()
            if path != expected_path:
                raise FactorPreflightError(
                    f"QFQ path differs from fold input: {symbol}"
                )
            actual_hash = _sha256_file(path)
            if actual_hash != str(expected_hash):
                raise FactorPreflightError(f"QFQ SHA256 drift: {symbol}")
            qfq_cache[symbol] = _prepare_history(
                pd.read_pickle(path), label=f"QFQ {symbol}"
            )
            qfq_manifest[symbol] = {
                "symbol": symbol,
                "file": path.name,
                "path": str(path),
                "sha256": actual_hash,
            }
        qfq_as_of = _as_of(qfq_cache[symbol], signal_day, label=f"QFQ {symbol}")
        index_as_of = _as_of(index_frame, signal_day, label="index")

        if signal_day > source_end:
            raise FactorPreflightError(
                f"candidate signal_day exceeds frozen source window: {candidate['candidate_id']}"
            )
        if symbol not in stock_pool_cache:
            raw_stock = load_stock_pool_history(
                symbol,
                config={},
                history_bars=STOCK_POOL_HISTORY_BARS,
                end=source_end,
                local_only=True,
                cache_dir=STOCK_POOL_CACHE_DIR,
                history_dir=history_dir,
            )
            source = str(raw_stock.attrs.get("stock_pool_history_source", ""))
            raw_stock_path = raw_stock.attrs.get("stock_pool_history_path")
            if not raw_stock_path:
                raise FactorPreflightError(
                    f"local-only stock-pool loader did not attest a path: {symbol}"
                )
            stock_path = _guard_development_path(Path(str(raw_stock_path)))
            actual_hash = _sha256_file(stock_path)
            expected_hash = frozen_stock_paths.get(str(stock_path))
            if expected_hash != actual_hash:
                raise FactorPreflightError(
                    f"stock-pool file is not in frozen v5 manifest or drifted: {stock_path}"
                )
            stock_frame = _prepare_history(raw_stock, label=f"stock-pool {symbol}")
            metadata = {
                "symbol": symbol,
                "file": stock_path.name,
                "path": str(stock_path),
                "sha256": actual_hash,
                "source": source,
            }
            stock_pool_cache[symbol] = (stock_frame, metadata)
        stock_frame, metadata = stock_pool_cache[symbol]
        stock_as_of = _as_of(stock_frame, signal_day, label=f"stock-pool {symbol}")
        manifest_key = (symbol, str(metadata["path"]))
        record = stock_pool_manifest.setdefault(
            manifest_key,
            {**metadata, "_signal_days": set()},
        )
        record["_signal_days"].add(signal_day.isoformat())

        computed = compute_candidate_features(
            candidate,
            qfq_as_of,
            index_as_of,
            stock_as_of,
            volume_unit_shares=volume_unit_shares,
        )
        cutoffs = {
            "qfq_max_day_used": qfq_as_of["datetime"].iloc[-1].date().isoformat(),
            "index_max_day_used": index_as_of["datetime"].iloc[-1].date().isoformat(),
            "stock_pool_max_day_used": stock_as_of["datetime"]
            .iloc[-1]
            .date()
            .isoformat(),
        }
        if any(date.fromisoformat(value) > signal_day for value in cutoffs.values()):
            raise FactorPreflightError(
                f"future data cutoff detected: {candidate['candidate_id']}"
            )
        candidate_features.append({**candidate, **computed, "data_cutoffs": cutoffs})

    qfq_rows = [qfq_manifest[symbol] for symbol in sorted(qfq_manifest)]
    stock_rows: list[dict[str, Any]] = []
    for key in sorted(stock_pool_manifest):
        record = dict(stock_pool_manifest[key])
        days = sorted(record.pop("_signal_days"))
        stock_rows.append(
            {
                **record,
                "signal_days": len(days),
                "first_signal_day": days[0],
                "last_signal_day": days[-1],
            }
        )
    coverage = _coverage_rows(candidate_features)
    return {
        "candidate_features": candidate_features,
        "qfq_manifest": qfq_rows,
        "stock_pool_manifest": stock_rows,
        "coverage": coverage,
        "source_audit": source_audit,
        "fold_audit": fold_audit,
        "volume_unit_shares": volume_unit_shares,
        "index_bars": len(index_frame),
    }


def _manifest_sha256(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> str:
    entries = ["|".join(str(row[field]) for field in fields) for row in rows]
    return _sha256_bytes("\n".join(entries).encode("utf-8"))


def assemble_report(
    snapshot: dict[str, Any],
    source_report_path: Path,
    fold_report_path: Path,
    index_data_path: Path,
    output_dir: Path,
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_audit = snapshot["source_audit"]
    fold_audit = snapshot["fold_audit"]
    coverage = snapshot["coverage"]
    candidate_rows = snapshot["candidate_features"]
    missing_totals = {row["name"]: int(row["missing"]) for row in coverage}
    code_inputs = {
        "preflight_code": _input_record(Path(__file__).resolve()),
        "audit_code": _input_record(AUDIT_CODE),
        "backtest_engine": _input_record(BACKTEST_ENGINE),
        "signal_engine": _input_record(SIGNAL_ENGINE),
    }
    return {
        "version": VERSION,
        "mode": "outcome_free_factor_preflight",
        "dataset_status": DATASET_STATUS,
        "research_question": (
            "Are the preregistered v7 candidate factor inputs causally available "
            "and reproducible on the frozen v5 development dataset?"
        ),
        "passes_preflight": True,
        "factor_contract": {
            "factor_count": len(FACTOR_NAMES),
            "factor_names": list(FACTOR_NAMES),
            "factor_specs": list(FACTOR_SPECS),
            "macd_parameters": {"fast": 12, "slow": 26, "signal": 9},
            "feature_time_boundary": "closed local bars with datetime <= signal_day",
            "missing_policy": "null feature plus explicit per-feature missing boolean",
            "coverage_is_not_a_selection_rule": True,
            "amount_fallback": "close * volume * frozen volume_unit_shares",
            "volume_unit_shares": snapshot["volume_unit_shares"],
        },
        "outcome_isolation": {
            "outcomes_read": False,
            "outcomes_read_semantics": (
                "the prerequisite v5 integrity audit parses frozen candidate rows, "
                "but the v7 factor pipeline never accesses outcome values"
            ),
            "outcomes_emitted": False,
            "forbidden_outcome_tokens": list(FORBIDDEN_OUTCOME_TOKENS),
            "candidate_public_fields": [
                "candidate_id",
                "symbol",
                "signal_day",
                "signal_type",
                "source_fields.cross_day",
                "source_fields.confirmation_bars",
            ],
            "factor_selection_performed": False,
        },
        "input": {
            "source_report": _input_record(source_report_path),
            "fold_report": _input_record(fold_report_path),
            "index_data": _input_record(index_data_path),
            **code_inputs,
            "v5_audit": {
                "source_checks_passed": source_audit["checks_passed"],
                "fold_checks_passed": fold_audit["checks_passed"],
                "candidate_count": len(candidate_rows),
                "candidate_ids_sha256": source_audit["candidate_ids_sha256"],
                "full_qfq_manifest_sha256": source_audit["frozen_inputs"][
                    "qfq_history"
                ]["manifest_sha256"],
                "fold_qfq_manifest_sha256": fold_audit["qfq_manifest_sha256"],
                "stock_pool_manifest_sha256": source_audit["stock_pool_cache"][
                    "manifest_sha256"
                ],
            },
        },
        "artifacts": artifacts,
        "summary": {
            "candidates": len(candidate_rows),
            "candidate_ids_sha256": _manifest_sha256(candidate_rows, ("candidate_id",)),
            "qfq_symbols_used": len(snapshot["qfq_manifest"]),
            "stock_pool_files_used": len(snapshot["stock_pool_manifest"]),
            "index_bars_available": snapshot["index_bars"],
            "missing_by_factor": missing_totals,
            "all_cutoffs_on_or_before_signal_day": True,
        },
        "policy": {
            "local_data_only": True,
            "network_used": False,
            "database_used": False,
            "sql_executed": False,
            "holdout_used": False,
            "output_directory_create_once": True,
        },
        "next_stage_rule": (
            "After the user reviews coverage, freeze the final factor/model/screen "
            "specification before reading any new model evaluation metrics."
        ),
        "research_screen_defined": False,
        "model_fitted": False,
        "outcomes_read": False,
        "hyperparameters_selected": False,
        "holdout_used": False,
        "production_eligible": False,
    }


def build_report(
    source_report_path: Path,
    fold_report_path: Path,
    index_data_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    source_report_path = _guard_development_path(source_report_path)
    fold_report_path = _guard_development_path(fold_report_path)
    index_data_path = _guard_development_path(index_data_path)
    output_dir = _guard_development_path(output_dir)
    if output_dir.exists():
        raise FactorPreflightError(
            f"output directory already exists; refusing overwrite: {output_dir}"
        )
    snapshot = build_snapshot(source_report_path, fold_report_path, index_data_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = {
        name: _write_jsonl(output_dir / f"{name}.jsonl", snapshot[name])
        for name in ARTIFACT_NAMES
    }
    report = assemble_report(
        snapshot,
        source_report_path,
        fold_report_path,
        index_data_path,
        output_dir,
        artifacts,
    )
    _write_json(output_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--fold-report", type=Path, required=True)
    parser.add_argument("--index-data", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(
            args.source_report,
            args.fold_report,
            args.index_data,
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
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
