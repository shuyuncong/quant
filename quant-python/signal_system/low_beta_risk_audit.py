"""Candidate-layer audit of low 60-session market beta as a risk factor.

The pre-registered hypothesis is that, within the same signal day, candidates
with lower beta to the Shanghai Composite have better subsequent and replayed
trade returns.  The primary scope is all canonical train/val/test candidates.
The originally proposed range/bear-only scope was changed before implementation
because its frozen development sample was structurally insufficient; regime
results and all secondary risk measures are diagnostic only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from attribution_audit import FIELDS, _config_snapshot, stats
from backtest_winrate import HISTORY_DIR, prepare_closed_bars
from candidate_integrity import (
    VERSION as INTEGRITY_VERSION,
    candidate_id,
    candidate_ids_sha256,
    file_sha256,
    load_jsonl,
)
from macd_divergence_audit import (
    _cluster_bootstrap_delta,
    _daily_rank_ic,
    _quantile_report,
)
from macd_near_regime_audit import _replay_split
from utils.helpers import load_config


VERSION = "low_beta_risk_audit.v1"
SPLITS = ("train", "val", "test")
PREREGISTERED_LABEL = "low-beta60-risk-candidate-audit"
PREREGISTERED_SEED = 20260831
PREREGISTERED_BOOTSTRAP_REPS = 2000
PREREGISTERED_CONFIG_SHA256 = (
    "4e0084eb945aa2533dca25f9e0f68b4e27e9f2cbc52c11b7898c082d466000f1"
)
PREREGISTERED_INDEX_SHA256 = (
    "e59364d0cfe2d848daec0d18d749f9b3fa20fe04ef520b8655ee3793020c6cc8"
)
DEFAULT_INDEX_DATA = BASE_DIR / "cache" / "index_000001_sh.pkl"
PRIMARY_FACTOR = "beta60"
FACTOR_SPECS: dict[str, dict[str, Any]] = {
    PRIMARY_FACTOR: {"larger_is_better": False, "role": "primary"},
    "market_correlation_60": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
    "downside_beta_60": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
    "idiosyncratic_volatility_60": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
}
MIN_ASSIGNMENT_COVERAGE = 0.90
MIN_AFFECTED_CANDIDATES = 30
MIN_AFFECTED_SYMBOLS = 10
MIN_AFFECTED_SIGNAL_DAYS = 10
MIN_OUTCOME_CLUSTERS = 10
BETA_RETURN_OBSERVATIONS = 60
BETA_CALENDAR_SESSIONS = BETA_RETURN_OBSERVATIONS + 1
MIN_ACTUAL_STOCK_SESSIONS = 50
MIN_DOWNSIDE_OBSERVATIONS = 10
VALID_REGIMES = {"bull", "range", "bear"}


def _empty_features(error: str) -> dict[str, Any]:
    return {
        **{factor: None for factor in FACTOR_SPECS},
        "factor_assignment_available": False,
        "factor_error": error,
        "beta_return_observations": BETA_RETURN_OBSERVATIONS,
        "beta_calendar_sessions": BETA_CALENDAR_SESSIONS,
        "actual_stock_sessions": None,
        "forward_filled_stock_sessions": None,
        "downside_observations": None,
        "entry_timing": "T+1",
    }


def _validate_stock_history_frame(
    frame: pd.DataFrame,
    *,
    expected_adjust: str,
) -> pd.DataFrame:
    required = {"datetime", "open", "high", "low", "close", "is_closed"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"missing columns {missing}")
    if str(frame.attrs.get("adjust", "")).lower() != expected_adjust:
        raise RuntimeError(
            f"history adjustment is not {expected_adjust}"
        )
    if str(frame.attrs.get("timeframe", "")).lower() != "1d":
        raise RuntimeError("history timeframe is not 1d")
    datetimes = pd.to_datetime(frame["datetime"], errors="coerce")
    if datetimes.isna().any():
        raise RuntimeError("history contains invalid datetime")
    if not datetimes.is_monotonic_increasing:
        raise RuntimeError("history datetime is not sorted")
    if datetimes.duplicated().any() or datetimes.dt.date.duplicated().any():
        raise RuntimeError("history contains duplicate trading dates")

    prices = frame[["open", "high", "low", "close"]].apply(
        pd.to_numeric,
        errors="coerce",
    )
    finite = np.isfinite(prices.to_numpy(dtype=float)).all(axis=1)
    valid = prices.notna().all(axis=1) & prices.gt(0).all(axis=1) & finite
    valid &= prices["high"] >= prices[["open", "close"]].max(axis=1)
    valid &= prices["low"] <= prices[["open", "close"]].min(axis=1)
    valid &= prices["high"] >= prices["low"]
    if not bool(valid.all()):
        raise RuntimeError("history contains invalid OHLC rows")

    closed = prepare_closed_bars(frame)
    if closed.empty:
        raise RuntimeError("history has no closed bars")
    closed_datetimes = pd.to_datetime(closed["datetime"], errors="coerce")
    if closed_datetimes.isna().any() or closed_datetimes.dt.date.duplicated().any():
        raise RuntimeError("closed history dates are invalid or duplicated")
    closed.attrs.update(frame.attrs)
    return closed


def _validate_index_frame(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"datetime", "close", "is_closed"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"index missing columns {missing}")
    if str(frame.attrs.get("adjust", "")).lower() != "none":
        raise RuntimeError("index adjustment is not none")
    if str(frame.attrs.get("timeframe", "")).lower() != "1d":
        raise RuntimeError("index timeframe is not 1d")
    datetimes = pd.to_datetime(frame["datetime"], errors="coerce")
    if datetimes.isna().any():
        raise RuntimeError("index contains invalid datetime")
    if not datetimes.is_monotonic_increasing:
        raise RuntimeError("index datetime is not sorted")
    if datetimes.duplicated().any() or datetimes.dt.date.duplicated().any():
        raise RuntimeError("index contains duplicate trading dates")
    closed_flags = frame["is_closed"].fillna(False).astype(bool)
    if not bool(closed_flags.all()):
        raise RuntimeError("index contains unclosed bars")
    close = pd.to_numeric(frame["close"], errors="coerce")
    if (
        close.isna().any()
        or not np.isfinite(close.to_numpy(dtype=float)).all()
        or bool((close <= 0).any())
    ):
        raise RuntimeError("index contains invalid close values")
    result = frame.copy().reset_index(drop=True)
    result["datetime"] = datetimes.reset_index(drop=True)
    result["close"] = close.reset_index(drop=True)
    result.attrs.update(frame.attrs)
    return result


def _load_index_frame(index_path: Path) -> pd.DataFrame:
    if not index_path.exists():
        raise RuntimeError(f"index data does not exist: {index_path}")
    try:
        raw = pd.read_pickle(index_path)
        if not isinstance(raw, pd.DataFrame):
            raise RuntimeError("index pickle is not a DataFrame")
        return _validate_index_frame(raw)
    except Exception as exc:
        raise RuntimeError(
            f"invalid index data {index_path}: {type(exc).__name__}: {exc}"
        ) from exc


def _history_pair(
    symbol: str,
    cache: dict[str, tuple[pd.DataFrame, pd.DataFrame] | None],
    history_hashes: dict[str, str],
    history_input_failures: list[dict[str, Any]],
) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    if symbol in cache:
        return cache[symbol]
    paths = {
        "qfq": HISTORY_DIR / f"{symbol}_qfq.pkl",
        "none": HISTORY_DIR / f"{symbol}_none.pkl",
    }
    missing = [family for family, path in paths.items() if not path.exists()]
    if missing:
        history_input_failures.append(
            {
                "symbol": symbol,
                "error": "missing_history_file",
                "missing_families": missing,
                "paths": {family: str(path) for family, path in paths.items()},
            }
        )
        cache[symbol] = None
        return None
    for family, path in paths.items():
        history_hashes[f"{symbol}|{family}"] = file_sha256(path)
    try:
        raw_qfq = pd.read_pickle(paths["qfq"])
        raw_none = pd.read_pickle(paths["none"])
        if not isinstance(raw_qfq, pd.DataFrame) or not isinstance(
            raw_none, pd.DataFrame
        ):
            raise RuntimeError("history pickle is not a DataFrame")
        qfq = _validate_stock_history_frame(raw_qfq, expected_adjust="qfq")
        none = _validate_stock_history_frame(raw_none, expected_adjust="none")
    except Exception as exc:
        history_input_failures.append(
            {
                "symbol": symbol,
                "error": f"{type(exc).__name__}: {exc}",
                "paths": {family: str(path) for family, path in paths.items()},
            }
        )
        cache[symbol] = None
        return None
    cache[symbol] = (qfq, none)
    return cache[symbol]


def _day_series(frame: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(frame["datetime"], errors="coerce").dt.date


def _coverage_error(
    signal_day: date,
    days: pd.Series,
    *,
    prefix: str,
) -> str:
    if signal_day < days.iloc[0]:
        return f"signal_day_before_{prefix}_history"
    if signal_day > days.iloc[-1]:
        return f"signal_day_after_{prefix}_history"
    return f"missing_signal_day_in_{prefix}_history"


def _ols_slope(stock_returns: np.ndarray, market_returns: np.ndarray) -> float | None:
    market_centered = market_returns - float(np.mean(market_returns))
    denominator = float(np.dot(market_centered, market_centered))
    variance_tolerance = np.finfo(float).eps * max(
        1.0,
        float(np.dot(market_returns, market_returns)),
    )
    if (
        not np.isfinite(denominator)
        or denominator <= variance_tolerance
    ):
        return None
    stock_centered = stock_returns - float(np.mean(stock_returns))
    numerator = float(np.dot(market_centered, stock_centered))
    value = numerator / denominator
    return value if np.isfinite(value) else None


def _beta_features(
    qfq: pd.DataFrame,
    none: pd.DataFrame,
    index_frame: pd.DataFrame,
    signal_day: date,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    index_days = _day_series(index_frame)
    index_matches = index_frame.index[index_days == signal_day].tolist()
    if len(index_matches) != 1:
        return _empty_features("missing_signal_day_in_index_history"), None
    signal_index = int(index_matches[0])
    if signal_index < BETA_RETURN_OBSERVATIONS:
        return _empty_features("insufficient_60_market_return_history"), None

    window = index_frame.iloc[
        signal_index - BETA_RETURN_OBSERVATIONS : signal_index + 1
    ].copy()
    window_days = list(_day_series(window))
    qfq_days = _day_series(qfq)
    none_days = _day_series(none)
    qfq_dates = set(qfq_days)
    none_dates = set(none_days)
    qfq_real_dates = {day for day in window_days if day in qfq_dates}
    none_real_dates = {day for day in window_days if day in none_dates}
    if qfq_real_dates != none_real_dates:
        detail = {
            "error": "qfq_none_window_date_mismatch",
            "signal_day": signal_day.isoformat(),
            "qfq_only_dates": sorted(
                day.isoformat() for day in qfq_real_dates - none_real_dates
            ),
            "none_only_dates": sorted(
                day.isoformat() for day in none_real_dates - qfq_real_dates
            ),
        }
        return _empty_features("qfq_none_window_date_mismatch"), detail
    actual_sessions = len(qfq_real_dates)
    if actual_sessions < MIN_ACTUAL_STOCK_SESSIONS:
        features = _empty_features("insufficient_actual_stock_sessions")
        features["actual_stock_sessions"] = actual_sessions
        features["forward_filled_stock_sessions"] = (
            BETA_CALENDAR_SESSIONS - actual_sessions
        )
        return features, None

    stock_close = pd.Series(
        pd.to_numeric(qfq["close"], errors="coerce").to_numpy(dtype=float),
        index=pd.Index(qfq_days, name="day"),
    ).sort_index()
    aligned_stock_close = stock_close.reindex(window_days, method="ffill")
    if aligned_stock_close.isna().any():
        features = _empty_features("missing_pre_window_stock_seed")
        features["actual_stock_sessions"] = actual_sessions
        features["forward_filled_stock_sessions"] = (
            BETA_CALENDAR_SESSIONS - actual_sessions
        )
        return features, None
    market_close = pd.to_numeric(window["close"], errors="coerce").reset_index(
        drop=True
    )
    if (
        market_close.isna().any()
        or bool((market_close <= 0).any())
        or aligned_stock_close.isna().any()
        or bool((aligned_stock_close <= 0).any())
    ):
        return _empty_features("invalid_beta_close_values"), None

    market_returns = market_close.pct_change(fill_method=None).iloc[1:].to_numpy(
        dtype=float
    )
    stock_returns = aligned_stock_close.pct_change(fill_method=None).iloc[1:].to_numpy(
        dtype=float
    )
    if (
        len(market_returns) != BETA_RETURN_OBSERVATIONS
        or len(stock_returns) != BETA_RETURN_OBSERVATIONS
        or not np.isfinite(market_returns).all()
        or not np.isfinite(stock_returns).all()
    ):
        return _empty_features("invalid_beta_return_values"), None

    beta = _ols_slope(stock_returns, market_returns)
    if beta is None:
        return _empty_features("zero_or_invalid_market_variance"), None
    stock_std = float(np.std(stock_returns, ddof=1))
    market_std = float(np.std(market_returns, ddof=1))
    correlation = (
        float(np.corrcoef(stock_returns, market_returns)[0, 1])
        if stock_std > 0 and market_std > 0
        else None
    )
    alpha = float(np.mean(stock_returns) - beta * np.mean(market_returns))
    residuals = stock_returns - (alpha + beta * market_returns)
    residual_volatility = float(np.std(residuals, ddof=1))
    downside_mask = market_returns < 0
    downside_observations = int(np.sum(downside_mask))
    downside_beta = (
        _ols_slope(stock_returns[downside_mask], market_returns[downside_mask])
        if downside_observations >= MIN_DOWNSIDE_OBSERVATIONS
        else None
    )
    return {
        "factor_assignment_available": True,
        "factor_error": None,
        "beta60": beta,
        "market_correlation_60": correlation,
        "downside_beta_60": downside_beta,
        "idiosyncratic_volatility_60": residual_volatility,
        "beta_return_observations": BETA_RETURN_OBSERVATIONS,
        "beta_calendar_sessions": BETA_CALENDAR_SESSIONS,
        "actual_stock_sessions": actual_sessions,
        "forward_filled_stock_sessions": BETA_CALENDAR_SESSIONS - actual_sessions,
        "downside_observations": downside_observations,
        "entry_timing": "T+1",
    }, None


def _factor_features_for_sources(
    sources: list[dict[str, Any]],
    index_frame: pd.DataFrame,
    index_sha256: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache: dict[str, tuple[pd.DataFrame, pd.DataFrame] | None] = {}
    history_hashes: dict[str, str] = {}
    history_input_failures: list[dict[str, Any]] = []
    coverage_failures: list[dict[str, Any]] = []
    window_consistency_failures: list[dict[str, Any]] = []
    features: dict[str, dict[str, Any]] = {}
    errors: Counter = Counter()
    index_days = _day_series(index_frame)

    for source in sources:
        identifier = candidate_id(source)
        if identifier in features:
            raise RuntimeError(f"duplicate candidate id after integrity gate: {identifier}")
        symbol = str(source.get("symbol", "")).zfill(6)
        pair = _history_pair(
            symbol,
            cache,
            history_hashes,
            history_input_failures,
        )
        base = {
            "candidate_id": identifier,
            "normalized_regime": (
                str(source.get("regime", "unknown")).strip().lower()
                if str(source.get("regime", "unknown")).strip().lower()
                in VALID_REGIMES
                else "unknown"
            ),
        }
        if pair is None:
            base.update(_empty_features("missing_or_invalid_history_pair"))
            errors["missing_or_invalid_history_pair"] += 1
            features[identifier] = base
            continue
        qfq, none = pair
        signal_day = date.fromisoformat(str(source["signal_day"]))
        family_frames = {"qfq": qfq, "none": none, "index": index_frame}
        missing_family = False
        for family, frame in family_frames.items():
            days = _day_series(frame)
            matches = frame.index[days == signal_day].tolist()
            if len(matches) == 1:
                continue
            error = _coverage_error(signal_day, days, prefix=family)
            coverage_failures.append(
                {
                    "candidate_id": identifier,
                    "symbol": symbol,
                    "signal_day": signal_day.isoformat(),
                    "family": family,
                    "history_first_day": days.iloc[0].isoformat(),
                    "history_last_day": days.iloc[-1].isoformat(),
                    "match_count": len(matches),
                    "error": error,
                }
            )
            base.update(_empty_features(error))
            errors[error] += 1
            features[identifier] = base
            missing_family = True
            break
        if missing_family:
            continue

        factor, consistency_failure = _beta_features(
            qfq,
            none,
            index_frame,
            signal_day,
        )
        if consistency_failure is not None:
            consistency_failure.update(
                {
                    "candidate_id": identifier,
                    "symbol": symbol,
                }
            )
            window_consistency_failures.append(consistency_failure)
        if factor.get("factor_error"):
            errors[str(factor["factor_error"])] += 1
        base.update(factor)
        features[identifier] = base

    history_manifest = hashlib.sha256(
        "\n".join(
            f"{key}|{history_hashes[key]}" for key in sorted(history_hashes)
        ).encode("utf-8")
    ).hexdigest()
    return features, {
        "history_manifest_sha256": history_manifest,
        "history_file_count": len(history_hashes),
        "history_symbol_count": len(cache),
        "history_input_safe": not history_input_failures,
        "history_input_failures": history_input_failures,
        "replay_history_coverage_safe": not coverage_failures,
        "replay_history_coverage_failures": coverage_failures,
        "qfq_none_window_consistency_safe": not window_consistency_failures,
        "qfq_none_window_consistency_failures": window_consistency_failures,
        "index_sha256": index_sha256,
        "index_first_day": index_days.iloc[0].isoformat(),
        "index_last_day": index_days.iloc[-1].isoformat(),
        "index_rows": len(index_frame),
        "factor_errors": dict(errors),
        "diagnostic_unavailable_counts": {
            factor: sum(value.get(factor) is None for value in features.values())
            for factor, spec in FACTOR_SPECS.items()
            if spec["role"] == "diagnostic"
        },
        "primary_definition": (
            "ols_slope_with_intercept_of_60_arithmetic_stock_returns_on_"
            "shanghai_composite_returns"
        ),
        "return_definition": "close_t/close_t_minus_1-1",
        "calendar_alignment": (
            "61_index_sessions_stock_qfq_forward_fill_from_latest_prior_close"
        ),
        "minimum_actual_stock_sessions": MIN_ACTUAL_STOCK_SESSIONS,
        "qfq_none_window_dates_must_match": True,
        "signal_day_closed_bars_only": True,
        "entry_timing": "T+1",
    }


def _assert_history_safe(factor_meta: dict[str, Any]) -> None:
    input_failures = list(factor_meta.get("history_input_failures") or [])
    coverage_failures = list(
        factor_meta.get("replay_history_coverage_failures") or []
    )
    consistency_failures = list(
        factor_meta.get("qfq_none_window_consistency_failures") or []
    )
    if not input_failures and not coverage_failures and not consistency_failures:
        return
    examples = (
        input_failures[:2]
        + coverage_failures[:2]
        + consistency_failures[:2]
    )
    raise RuntimeError(
        "history safety gate failed before replay: "
        f"input_failures={len(input_failures)}, "
        f"coverage_failures={len(coverage_failures)}, "
        f"consistency_failures={len(consistency_failures)}, examples={examples}"
    )


def _assign_primary_halves(rows: list[dict[str, Any]]) -> None:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row["primary_low_beta"] = None
        row["variant_included"] = False
        if row.get(PRIMARY_FACTOR) is not None:
            by_day.setdefault(str(row.get("signal_day", "")), []).append(row)
    for day_rows in by_day.values():
        values = [float(row[PRIMARY_FACTOR]) for row in day_rows]
        if len(day_rows) < 2 or len(set(values)) < 2:
            continue
        median = float(np.median(values))
        for row in day_rows:
            low = float(row[PRIMARY_FACTOR]) <= median
            row["primary_low_beta"] = low
            row["variant_included"] = low


def _factor_report(
    rows: list[dict[str, Any]],
    factor: str,
    larger_is_better: bool,
) -> dict[str, Any]:
    usable = [row for row in rows if row.get(factor) is not None]
    return {
        "n": len(usable),
        "coverage": len(usable) / len(rows) if rows else 0.0,
        "larger_is_better": larger_is_better,
        "positive_ic_interpretation": (
            "higher_factor_values_associated_with_higher_outcomes"
            if larger_is_better
            else "lower_factor_values_associated_with_higher_outcomes"
        ),
        "rank_ic": {
            outcome: _daily_rank_ic(
                usable,
                factor=factor,
                outcome=outcome,
                ascending_good=larger_is_better,
            )
            for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
        },
        "quantiles": {
            outcome: _quantile_report(
                usable,
                factor=factor,
                outcome=outcome,
                smaller_is_better=not larger_is_better,
            )
            for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
        },
    }


def _regime_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    for regime in ("bull", "range", "bear", "unknown"):
        regime_rows = [
            row for row in rows if row.get("normalized_regime") == regime
        ]
        comparable = [
            row for row in regime_rows if row.get("primary_low_beta") is not None
        ]
        low = [row for row in comparable if row.get("primary_low_beta")]
        high = [
            row for row in comparable if row.get("primary_low_beta") is False
        ]
        reports[regime] = {
            "n": len(regime_rows),
            "comparable_n": len(comparable),
            "low_n": len(low),
            "high_n": len(high),
            "low": {
                field: stats([row.get(field) for row in low]) for field in FIELDS
            },
            "high": {
                field: stats([row.get(field) for row in high]) for field in FIELDS
            },
            "mean_delta_low_minus_high": {
                field: (
                    float(np.mean([float(row[field]) for row in low if row.get(field) is not None]))
                    - float(np.mean([float(row[field]) for row in high if row.get(field) is not None]))
                    if any(row.get(field) is not None for row in low)
                    and any(row.get(field) is not None for row in high)
                    else None
                )
                for field in FIELDS
            },
        }
    return reports


def _bootstrap_contract_ok(report: dict[str, Any]) -> bool:
    return bool(
        report.get("mean_delta") is not None
        and report.get("reps_requested") == PREREGISTERED_BOOTSTRAP_REPS
        and report.get("reps_valid") == PREREGISTERED_BOOTSTRAP_REPS
        and int(report.get("cluster_count") or 0) >= MIN_OUTCOME_CLUSTERS
    )


def _candidate_report(
    rows: list[dict[str, Any]],
    source_match: dict[str, Any],
    replay_integrity: dict[str, Any],
    factor_meta: dict[str, Any],
    seed: int,
) -> dict[str, Any]:
    assigned = [row for row in rows if row.get("factor_assignment_available")]
    primary_rows = [
        row for row in assigned if row.get("primary_low_beta") is not None
    ]
    low = [row for row in primary_rows if row.get("primary_low_beta")]
    high = [
        row for row in primary_rows if row.get("primary_low_beta") is False
    ]
    bootstraps = {
        outcome: _cluster_bootstrap_delta(
            primary_rows,
            outcome,
            reps=PREREGISTERED_BOOTSTRAP_REPS,
            seed=seed + index,
        )
        for index, outcome in enumerate(
            ("future_20d", "future_40d", "trade_pnl_pct")
        )
    }
    affected_ids = {str(row["candidate_id"]) for row in primary_rows}
    affected_symbols = {str(row["symbol"]) for row in primary_rows}
    affected_days = {str(row["signal_day"]) for row in primary_rows}
    coverage = len(assigned) / len(rows) if rows else 0.0
    sample_sufficient = bool(
        len(affected_ids) >= MIN_AFFECTED_CANDIDATES
        and len(affected_symbols) >= MIN_AFFECTED_SYMBOLS
        and len(affected_days) >= MIN_AFFECTED_SIGNAL_DAYS
        and low
        and high
    )
    primary_outcomes = ("future_40d", "trade_pnl_pct")
    direction_positive = all(
        bootstraps[outcome]["mean_delta"] is not None
        and bootstraps[outcome]["mean_delta"] > 0
        for outcome in primary_outcomes
    )
    contract_ok = all(
        _bootstrap_contract_ok(bootstraps[outcome]) for outcome in primary_outcomes
    )
    bootstrap_supported = bool(
        contract_ok
        and all(
            bootstraps[outcome]["ci95_low"] is not None
            and bootstraps[outcome]["ci95_low"] > 0
            for outcome in primary_outcomes
        )
    )
    return {
        "n": len(rows),
        "assignment_coverage": coverage,
        "primary_factor": PRIMARY_FACTOR,
        "primary_larger_is_better": False,
        "primary_group_rule": "same_signal_day_beta60_lte_median_is_low",
        "primary_comparison": "low_minus_high",
        "primary_low_n": len(low),
        "primary_high_n": len(high),
        "baseline": {
            field: stats([row.get(field) for row in rows]) for field in FIELDS
        },
        "primary_low": {
            field: stats([row.get(field) for row in low]) for field in FIELDS
        },
        "primary_high": {
            field: stats([row.get(field) for row in high]) for field in FIELDS
        },
        "factors": {
            factor: _factor_report(rows, factor, spec["larger_is_better"])
            for factor, spec in FACTOR_SPECS.items()
        },
        "regime_diagnostics": _regime_diagnostics(rows),
        "cluster_bootstrap_low_minus_high": bootstraps,
        "bootstrap_contract": {
            "cluster_key": "symbol",
            "missing_outcomes_excluded_before_clustering": True,
            "resample_whole_clusters_with_replacement": True,
            "repeated_cluster_selection_repeats_whole_cluster_weight": True,
            "low_and_high_valid_outcomes_required": True,
            "percentile_ci": [2.5, 97.5],
            "reps_required": PREREGISTERED_BOOTSTRAP_REPS,
            "minimum_outcome_clusters": MIN_OUTCOME_CLUSTERS,
            "primary_outcomes_contract_ok": contract_ok,
        },
        "sample_gate": {
            "affected_unique_candidates": len(affected_ids),
            "affected_unique_symbols": len(affected_symbols),
            "affected_unique_signal_days": len(affected_days),
            "minimum_candidates": MIN_AFFECTED_CANDIDATES,
            "minimum_symbols": MIN_AFFECTED_SYMBOLS,
            "minimum_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
            "sufficient": sample_sufficient,
        },
        "gate": {
            "assignment_coverage_ok": coverage >= MIN_ASSIGNMENT_COVERAGE,
            "sample_sufficient": sample_sufficient,
            "replay_integrity_ok": bool(replay_integrity.get("all_pass")),
            "primary_direction_positive": direction_positive,
            "primary_bootstrap_contract_ok": contract_ok,
            "primary_cluster_bootstrap_ci95_positive": bootstrap_supported,
            "eligible_for_cross_split": bool(
                coverage >= MIN_ASSIGNMENT_COVERAGE
                and sample_sufficient
                and replay_integrity.get("all_pass")
            ),
        },
        "source_match": source_match,
        "replay_integrity": replay_integrity,
        "factor_meta": factor_meta,
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


def _assert_replay_integrity(
    sources: list[dict[str, Any]],
    feature_map: dict[str, dict[str, Any]],
    replay_rows: list[dict[str, Any]],
    replay_skips: Counter,
    source_match: dict[str, Any],
) -> dict[str, Any]:
    source_ids = [candidate_id(row) for row in sources]
    replay_ids = [candidate_id(row) for row in replay_rows]
    feature_ids = list(feature_map)
    policy_skips = dict(source_match.get("policy_skips") or {})
    checks = {
        "source_ids_unique": len(source_ids) == len(set(source_ids)),
        "replay_ids_unique": len(replay_ids) == len(set(replay_ids)),
        "feature_ids_unique": len(feature_ids) == len(set(feature_ids)),
        "source_replay_ids_order_match": source_ids == replay_ids,
        "source_feature_ids_set_match": set(source_ids) == set(feature_ids),
        "replay_feature_ids_set_match": set(replay_ids) == set(feature_ids),
        "replay_skips_empty": not dict(replay_skips),
        "policy_skips_empty": not policy_skips,
        "source_rows_match": source_match.get("source_rows") == len(sources),
        "eligible_rows_match": source_match.get("eligible_rows") == len(sources),
        "replayed_rows_match": source_match.get("replayed_rows") == len(sources),
        "baseline_replay_complete": source_match.get("baseline_replay_complete")
        is True,
        "entry_signal_policy_validated": source_match.get(
            "entry_signal_policy_validated"
        )
        is True,
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(f"replay integrity validation failed: {checks}")
    return checks


def _cross_split_gate(split_reports: dict[str, Any]) -> dict[str, Any]:
    required_present = (
        len(split_reports) == len(SPLITS) and set(split_reports) == set(SPLITS)
    )
    eligible = [
        split
        for split in SPLITS
        if split in split_reports
        and split_reports[split]["candidate_report"]["gate"]
        ["eligible_for_cross_split"]
    ]
    all_required_eligible = required_present and eligible == list(SPLITS)
    directions = {
        split: {
            outcome: split_reports[split]["candidate_report"]
            ["cluster_bootstrap_low_minus_high"][outcome]["mean_delta"]
            for outcome in ("future_40d", "trade_pnl_pct")
        }
        for split in SPLITS
        if split in split_reports
    }
    direction_consistent = bool(
        all_required_eligible
        and all(
            directions[split]["future_40d"] is not None
            and directions[split]["future_40d"] > 0
            and directions[split]["trade_pnl_pct"] is not None
            and directions[split]["trade_pnl_pct"] > 0
            for split in SPLITS
        )
    )
    bootstrap_supported = bool(
        direction_consistent
        and all(
            split_reports[split]["candidate_report"]["gate"]
            ["primary_bootstrap_contract_ok"]
            and split_reports[split]["candidate_report"]["gate"]
            ["primary_cluster_bootstrap_ci95_positive"]
            for split in SPLITS
        )
    )
    return {
        "required_splits": list(SPLITS),
        "eligible_splits": eligible,
        "all_required_splits_present": required_present,
        "all_required_splits_eligible": all_required_eligible,
        "directions": directions,
        "direction_consistent": direction_consistent,
        "cluster_bootstrap_supported": bootstrap_supported,
        "pass": bool(
            all_required_eligible and direction_consistent and bootstrap_supported
        ),
    }


def _validate_preregistered_args(args: argparse.Namespace) -> None:
    splits = tuple(getattr(args, "splits", ()))
    invalid = sorted(set(splits) - set(SPLITS))
    if invalid:
        raise RuntimeError(
            f"unregistered split(s) {invalid}; this audit cannot consume holdout"
        )
    if splits != SPLITS:
        raise RuntimeError(
            f"all required splits must be supplied in order {SPLITS}; got {splits}"
        )
    if getattr(args, "label", None) != PREREGISTERED_LABEL:
        raise RuntimeError("label differs from the pre-registered label")
    if getattr(args, "seed", None) != PREREGISTERED_SEED:
        raise RuntimeError("seed differs from the pre-registered seed")
    if getattr(args, "bootstrap_reps", None) != PREREGISTERED_BOOTSTRAP_REPS:
        raise RuntimeError("bootstrap reps differ from the pre-registered value")


def _output_targets(output_dir: Path) -> list[Path]:
    return [
        *(output_dir / f"low_beta_risk_{split}.jsonl" for split in SPLITS),
        output_dir / "low_beta_risk_audit.json",
    ]


def _assert_output_targets_unused(output_dir: Path) -> None:
    conflicts = [
        path
        for target in _output_targets(output_dir)
        for path in (target, target.with_name(target.name + ".tmp"))
        if path.exists()
    ]
    if conflicts:
        raise RuntimeError(
            "formal output target already exists; overwrite is forbidden: "
            + ", ".join(str(path) for path in conflicts)
        )


def _write_outputs_atomically(
    output_dir: Path,
    enriched_by_split: dict[str, list[dict[str, Any]]],
    result: dict[str, Any],
) -> None:
    staged: list[tuple[Path, Path]] = []
    for split in SPLITS:
        target = output_dir / f"low_beta_risk_{split}.jsonl"
        temporary = target.with_name(target.name + ".tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            for row in enriched_by_split[split]:
                handle.write(
                    json.dumps(
                        row,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    + "\n"
                )
        result["splits"][split]["enriched"] = str(target)
        result["splits"][split]["enriched_sha256"] = file_sha256(temporary)
        staged.append((temporary, target))

    result_target = output_dir / "low_beta_risk_audit.json"
    result_temporary = result_target.with_name(result_target.name + ".tmp")
    with result_temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    staged.append((result_temporary, result_target))
    for temporary, target in staged:
        temporary.replace(target)


def run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_preregistered_args(args)
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    _assert_output_targets_unused(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    config_path = Path(args.config).expanduser().resolve()
    expected_config_path = (BASE_DIR / "config" / "config.yaml").resolve()
    if config_path != expected_config_path:
        raise RuntimeError(
            f"config path must remain {expected_config_path}; got {config_path}"
        )
    config_hash_before = file_sha256(config_path)
    if config_hash_before != PREREGISTERED_CONFIG_SHA256:
        raise RuntimeError(
            "config hash differs from the pre-registered snapshot: "
            f"expected={PREREGISTERED_CONFIG_SHA256}, actual={config_hash_before}"
        )

    index_path = Path(args.index_data).expanduser().resolve()
    expected_index_path = DEFAULT_INDEX_DATA.resolve()
    if index_path != expected_index_path:
        raise RuntimeError(
            f"index path must remain {expected_index_path}; got {index_path}"
        )
    index_hash_before = file_sha256(index_path)
    if index_hash_before != PREREGISTERED_INDEX_SHA256:
        raise RuntimeError(
            "index hash differs from the pre-registered snapshot: "
            f"expected={PREREGISTERED_INDEX_SHA256}, actual={index_hash_before}"
        )
    index_frame = _load_index_frame(index_path)
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")

    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "design": {
            "candidate_layer_only": True,
            "primary_scope": "all_canonical",
            "primary_factor": PRIMARY_FACTOR,
            "primary_larger_is_better": False,
            "same_signal_day_comparison": True,
            "primary_comparison": "low_minus_high",
            "diagnostic_factors_cannot_pass_gate": [
                factor
                for factor, spec in FACTOR_SPECS.items()
                if spec["role"] == "diagnostic"
            ],
            "regime_diagnostic_only": True,
            "canonical_candidates_required": True,
            "source_outcomes_used": False,
            "replay_outcomes_used": True,
            "source_artifact_match_is_diagnostic": True,
            "required_splits": list(SPLITS),
            "signal_day_closed_bars_only": True,
            "entry_timing": "T+1",
            "holdout_consumed": False,
            "portfolio_layer_implemented": False,
            "production_eligible": False,
            "period_scan_performed": False,
            "reverse_direction_tested": False,
        },
        "research_provenance": {
            "original_scope": "range_bear_only",
            "revised_scope": "all_canonical",
            "scope_changed_before_implementation": True,
            "weak_regime_precheck_counts": {
                "train": {"candidates": 2, "signal_days": 2},
                "val": {"candidates": 53, "signal_days": 11},
                "test": {"candidates": 17, "signal_days": 7},
            },
            "regime_diagnostic_only": True,
            "direction_source": "user_confirmed_low_beta_risk_hypothesis",
            "independent_confirmation": False,
            "canonical_data_reused_across_candidate_research": True,
            "holdout_reserved": True,
        },
        "thresholds": {
            "assignment_coverage": MIN_ASSIGNMENT_COVERAGE,
            "affected_candidates": MIN_AFFECTED_CANDIDATES,
            "affected_symbols": MIN_AFFECTED_SYMBOLS,
            "affected_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
            "minimum_outcome_clusters": MIN_OUTCOME_CLUSTERS,
            "minimum_actual_stock_sessions": MIN_ACTUAL_STOCK_SESSIONS,
            "minimum_downside_observations": MIN_DOWNSIDE_OBSERVATIONS,
        },
        "config_file": str(config_path),
        "config_file_expected_sha256": PREREGISTERED_CONFIG_SHA256,
        "config_file_sha256_before": config_hash_before,
        "config_snapshot": _config_snapshot(config),
        "index_file": str(index_path),
        "index_file_expected_sha256": PREREGISTERED_INDEX_SHA256,
        "index_file_sha256_before": index_hash_before,
        "script_sha256": file_sha256(Path(__file__).resolve()),
        "seed": args.seed,
        "bootstrap_reps": args.bootstrap_reps,
        "production_database_connected": False,
        "splits": {},
    }
    enriched_by_split: dict[str, list[dict[str, Any]]] = {}

    for split_index, split in enumerate(SPLITS):
        source_path = input_dir / f"candidates_{split}.jsonl"
        sources = load_jsonl(source_path)
        integrity = _validate_integrity_manifest(
            input_dir,
            split,
            source_path,
            sources,
        )
        feature_map, factor_meta = _factor_features_for_sources(
            sources,
            index_frame,
            index_hash_before,
        )
        _assert_history_safe(factor_meta)
        replay_rows, replay_skips, source_match = _replay_split(source_path, config)
        replay_integrity = _assert_replay_integrity(
            sources,
            feature_map,
            replay_rows,
            replay_skips,
            source_match,
        )
        enriched: list[dict[str, Any]] = []
        for replay in replay_rows:
            identifier = candidate_id(replay)
            row = dict(replay)
            row.update(feature_map[identifier])
            enriched.append(row)
        _assign_primary_halves(enriched)
        enriched_by_split[split] = enriched
        result["splits"][split] = {
            "source": str(source_path),
            "source_sha256": file_sha256(source_path),
            "candidate_integrity": integrity,
            "enriched": None,
            "enriched_sha256": None,
            "replay_skips": dict(replay_skips),
            "candidate_report": _candidate_report(
                enriched,
                source_match,
                replay_integrity,
                factor_meta,
                PREREGISTERED_SEED + split_index * 100,
            ),
        }

    result["candidate_gate"] = _cross_split_gate(result["splits"])
    result["baseline_replay_complete"] = all(
        result["splits"][split]["candidate_report"]["source_match"]
        ["baseline_replay_complete"]
        for split in SPLITS
    )
    config_hash_after = file_sha256(config_path)
    index_hash_after = file_sha256(index_path)
    result["config_file_sha256_after"] = config_hash_after
    result["config_file_unchanged"] = config_hash_after == config_hash_before
    result["index_file_sha256_after"] = index_hash_after
    result["index_file_unchanged"] = index_hash_after == index_hash_before
    if not result["config_file_unchanged"]:
        raise RuntimeError("config file changed during audit")
    if not result["index_file_unchanged"]:
        raise RuntimeError("index file changed during audit")
    if not result["baseline_replay_complete"]:
        raise RuntimeError("baseline replay became incomplete after integrity gate")
    result["verdict"] = (
        "candidate_layer_historical_support_requires_portfolio_rule_preregistration"
        if result["candidate_gate"]["pass"]
        else "candidate_layer_rejected_or_insufficient_sample"
    )
    result["production_decision"] = "unchanged_P0"
    _write_outputs_atomically(output_dir, enriched_by_split, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        default=r"D:\tmp\candidates_fullpool_canonical",
    )
    parser.add_argument(
        "--output-dir",
        default=r"D:\tmp\low_beta60_risk_candidate",
    )
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--index-data", default=str(DEFAULT_INDEX_DATA))
    parser.add_argument("--label", default=PREREGISTERED_LABEL)
    parser.add_argument("--splits", nargs="+", default=list(SPLITS))
    parser.add_argument("--seed", type=int, default=PREREGISTERED_SEED)
    parser.add_argument(
        "--bootstrap-reps",
        type=int,
        default=PREREGISTERED_BOOTSTRAP_REPS,
    )
    args = parser.parse_args()
    result = run(args)
    print(
        json.dumps(
            {
                "version": result["version"],
                "primary_factor": PRIMARY_FACTOR,
                "candidate_gate": result["candidate_gate"],
                "verdict": result["verdict"],
                "config_file_unchanged": result["config_file_unchanged"],
                "index_file_unchanged": result["index_file_unchanged"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
