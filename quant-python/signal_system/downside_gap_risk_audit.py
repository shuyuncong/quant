"""Candidate-layer audit of 60-session downside opening-gap risk.

The pre-registered hypothesis is that candidates with lower downside opening-
gap semideviation have better future and production-risk replay outcomes.  The
primary factor uses 60 QFQ open/previous-close gaps through the closed signal
day.  Raw none gaps, large-gap frequencies, a limit-down proxy, and a post-
large-up chase proxy are diagnostic only.  Canonical train/val/test are all
required; holdout and the portfolio layer are forbidden.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from attribution_audit import (  # noqa: E402
    FIELDS,
    _config_snapshot,
    _replay_split as _production_replay_split,
    stats,
)
from backtest_winrate import HISTORY_DIR, prepare_closed_bars  # noqa: E402
from candidate_integrity import (  # noqa: E402
    VERSION as INTEGRITY_VERSION,
    candidate_id,
    candidate_ids_sha256,
    file_sha256,
    load_jsonl,
)
from macd_divergence_audit import _daily_rank_ic, _quantile_report  # noqa: E402
from utils.helpers import load_config  # noqa: E402


VERSION = "downside_gap_risk_audit.v1"
SPLITS = ("train", "val", "test")
PREREGISTERED_LABEL = "downside-gap60-risk-candidate-audit"
PREREGISTERED_SEED = 20260903
PREREGISTERED_BOOTSTRAP_REPS = 2000
CANONICAL_INPUT_DIR = Path(r"D:\tmp\candidates_fullpool_canonical")
FORMAL_OUTPUT_DIR = Path(r"D:\tmp\downside_gap60_risk_candidate_final")
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
PREREGISTERED_CONFIG_SHA256 = (
    "4e0084eb945aa2533dca25f9e0f68b4e27e9f2cbc52c11b7898c082d466000f1"
)
PREREGISTERED_CANONICAL_SHA256 = {
    "train": "28a2defd6f7f09904295cb270cc311bf989add84b4cd0c8dc947d4e59a4fb40f",
    "val": "921a2ddc6916030a270aa136c0ce05eabdcddfb9ab289ec07c4ef5e8d2ba2bd0",
    "test": "d90836a21610f77c6bb6afca3169d3eedb79c228026415f2e8da956fc8def174",
}
PREREGISTERED_INTEGRITY_MANIFEST_SHA256 = (
    "4e12f3d53851e0cc7f49a3c67f4fcdf84d3b2584630e166c0a0cf9a997e01753"
)
PREREGISTERED_HISTORY_MANIFEST_SHA256 = (
    "1a2fdda785d3b528ad42a14cc225083ba483adca6a9d519ad290e00c12e32fb1"
)

DEPENDENCY_PATHS = {
    "attribution_audit": BASE_DIR / "attribution_audit.py",
    "backtest_winrate": BASE_DIR / "backtest_winrate.py",
    "candidate_integrity": BASE_DIR / "candidate_integrity.py",
    "macd_divergence_audit": BASE_DIR / "macd_divergence_audit.py",
    "strategy_chan": BASE_DIR / "strategy" / "chan.py",
    "strategy_macd": BASE_DIR / "strategy" / "macd.py",
    "strategy_signal_policy": BASE_DIR / "strategy" / "signal_policy.py",
    "strategy_market_gate": BASE_DIR / "strategy" / "market_gate.py",
    "utils_helpers": BASE_DIR / "utils" / "helpers.py",
}
DEPENDENCY_EXPECTED_SHA256 = {
    "attribution_audit": "fdb13ef7d20bdf024a14f6dffe17d47c14ec73ddcfd72044bca527eb34154f4d",
    "backtest_winrate": "586e21d8050a4397f2664b41676c2c5c140754f161c1937e27a2fd7e875800d1",
    "candidate_integrity": "fb7e197a7bb11963b5f6b2042965f946b48bef4ab89789ca1d7c5ad4cfb4e53f",
    "macd_divergence_audit": "ae17dd4906b52fc07058156ab229c3fe264dbde83183c482e5a6fb6ac919ca1e",
    "strategy_chan": "af70eab3207c168254f70e8674fd1ce6293e0b7937a8a2fb8a842640b306d058",
    "strategy_macd": "cb1e5ff36086fca041d899199c7ed8a093929feff294bc0f40da2c65522866c9",
    "strategy_signal_policy": "2288fd4492f860a1705d1539d7b1b5b396a44f81c9e333cb6fafdc3ea2b56d34",
    "strategy_market_gate": "fa69dbee01817e44b7b4bc73c50c9a37fa5b4f8b4eb08460dc134979d6d7abe6",
    "utils_helpers": "3ec75b774efe60cbe678f5dbda78a84a70a6a12442a1f458e121bc33e6804ae1",
}

PRIMARY_FACTOR = "downside_gap_semideviation_60"
FACTOR_SPECS: dict[str, dict[str, Any]] = {
    PRIMARY_FACTOR: {"larger_is_better": False, "role": "primary"},
    "large_down_gap_frequency_60": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
    "worst_down_gap_60": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
    "none_downside_gap_semideviation_60": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
    "open_limit_down_proxy_frequency_60": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
    "post_large_up_day_chase_gap_mean_60": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
}

GAP_OBSERVATIONS = 60
MAIN_WINDOW_BARS = GAP_OBSERVATIONS + 1
CHASE_WINDOW_BARS = GAP_OBSERVATIONS + 2
LARGE_DOWN_GAP_THRESHOLD = -0.03
LIMIT_DOWN_PROXY_THRESHOLD = -0.095
LARGE_UP_DAY_THRESHOLD = 0.095
MIN_ASSIGNMENT_COVERAGE = 0.90
MIN_AFFECTED_CANDIDATES = 30
MIN_AFFECTED_SYMBOLS = 10
MIN_AFFECTED_SIGNAL_DAYS = 10
MIN_OUTCOME_CLUSTERS = 10
VALID_REGIMES = {"bull", "range", "bear"}


def _sha_manifest(items: Iterable[tuple[str, str]]) -> str:
    payload = "\n".join(f"{key}|{value}" for key, value in sorted(items))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snapshot_named_paths(paths: dict[str, Path]) -> dict[str, dict[str, str]]:
    snapshot: dict[str, dict[str, str]] = {}
    for name, raw_path in sorted(paths.items()):
        path = raw_path.expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"protected input does not exist: {path}")
        snapshot[name] = {"path": str(path), "sha256": file_sha256(path)}
    return snapshot


def _snapshot_manifest(snapshot: dict[str, dict[str, str]]) -> str:
    return _sha_manifest(
        (name, f"{item['path']}|{item['sha256']}")
        for name, item in snapshot.items()
    )


def _snapshot_changes(
    initial: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, str]], list[dict[str, str | None]]]:
    current_paths = {name: Path(item["path"]) for name, item in initial.items()}
    current = _snapshot_named_paths(current_paths)
    changes: list[dict[str, str | None]] = []
    for name in sorted(initial):
        before = initial[name]["sha256"]
        after = current[name]["sha256"]
        if before != after:
            changes.append(
                {
                    "name": name,
                    "path": initial[name]["path"],
                    "before": before,
                    "after": after,
                }
            )
    return current, changes


def _static_protected_paths(input_dir: Path, config_path: Path) -> dict[str, Path]:
    paths = {
        "audit_script": Path(__file__).resolve(),
        "config": config_path,
        "candidate_integrity_manifest": input_dir
        / "candidate_integrity_manifest.json",
        **DEPENDENCY_PATHS,
    }
    for split in SPLITS:
        paths[f"canonical_{split}"] = input_dir / f"candidates_{split}.jsonl"
    return paths


def _expected_static_hashes(expected_script_sha256: str) -> dict[str, str]:
    return {
        "audit_script": expected_script_sha256,
        "config": PREREGISTERED_CONFIG_SHA256,
        "candidate_integrity_manifest": PREREGISTERED_INTEGRITY_MANIFEST_SHA256,
        **DEPENDENCY_EXPECTED_SHA256,
        **{
            f"canonical_{split}": value
            for split, value in PREREGISTERED_CANONICAL_SHA256.items()
        },
    }


def _validate_expected_snapshot(
    snapshot: dict[str, dict[str, str]],
    expected: dict[str, str],
) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for name in sorted(expected):
        actual = snapshot.get(name, {}).get("sha256")
        report[name] = {
            "path": snapshot.get(name, {}).get("path"),
            "expected_sha256": expected[name],
            "actual_sha256": actual,
            "match": actual == expected[name],
        }
    if not all(item["match"] for item in report.values()):
        raise RuntimeError(f"protected static SHA contract failed: {report}")
    return report


def _history_paths(symbols: Iterable[str]) -> dict[str, Path]:
    return {
        f"{symbol}|{family}": HISTORY_DIR / f"{symbol}_{family}.pkl"
        for symbol in sorted({str(value).zfill(6) for value in symbols})
        for family in ("qfq", "none")
    }


def _history_manifest(snapshot: dict[str, dict[str, str]]) -> str:
    return _sha_manifest(
        (name, item["sha256"]) for name, item in snapshot.items()
    )


def _empty_features(error: str) -> dict[str, Any]:
    return {
        **{factor: None for factor in FACTOR_SPECS},
        "factor_assignment_available": False,
        "factor_error": error,
        "gap_observations": GAP_OBSERVATIONS,
        "factor_lookback_bars": None,
        "chase_history_available": False,
        "chase_qualifying_days": None,
        "entry_timing": "T+1",
    }


def _validate_history_frame(
    frame: pd.DataFrame,
    *,
    expected_adjust: str,
) -> pd.DataFrame:
    required = {"datetime", "open", "high", "low", "close", "is_closed"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"missing columns {missing}")
    if str(frame.attrs.get("adjust", "")).lower() != expected_adjust:
        raise RuntimeError(f"history adjustment is not {expected_adjust}")
    if str(frame.attrs.get("timeframe", "")).lower() != "1d":
        raise RuntimeError("history timeframe is not 1d")
    datetimes = pd.to_datetime(frame["datetime"], errors="coerce")
    if datetimes.isna().any() or not datetimes.is_monotonic_increasing:
        raise RuntimeError("history datetime is invalid or unsorted")
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
    closed = closed.copy().reset_index(drop=True)
    closed_days = pd.to_datetime(closed["datetime"], errors="coerce")
    if (
        closed_days.isna().any()
        or not closed_days.is_monotonic_increasing
        or closed_days.dt.date.duplicated().any()
    ):
        raise RuntimeError("closed history dates are invalid, unsorted, or duplicated")
    closed.attrs.update(frame.attrs)
    return closed


def _load_history_pair(
    symbol: str,
    cache: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if symbol in cache:
        return cache[symbol]
    frames: dict[str, pd.DataFrame] = {}
    for family in ("qfq", "none"):
        path = (HISTORY_DIR / f"{symbol}_{family}.pkl").resolve()
        try:
            raw = pd.read_pickle(path)
            if not isinstance(raw, pd.DataFrame):
                raise RuntimeError("history pickle is not a DataFrame")
            frames[family] = _validate_history_frame(
                raw,
                expected_adjust=family,
            )
        except Exception as exc:
            raise RuntimeError(
                f"invalid {family} history for {symbol}: {type(exc).__name__}: {exc}"
            ) from exc
    cache[symbol] = (frames["qfq"], frames["none"])
    return cache[symbol]


def _day_values(frame: pd.DataFrame) -> list[date]:
    return list(pd.to_datetime(frame["datetime"], errors="coerce").dt.date)


def _signal_position(frame: pd.DataFrame, signal_day: date) -> int | None:
    days = _day_values(frame)
    positions = [index for index, value in enumerate(days) if value == signal_day]
    return positions[0] if len(positions) == 1 else None


def _gap_array(frame: pd.DataFrame) -> np.ndarray:
    opens = pd.to_numeric(frame["open"], errors="coerce").to_numpy(dtype=float)
    closes = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    gaps = opens[1:] / closes[:-1] - 1.0
    if len(gaps) != GAP_OBSERVATIONS or not np.isfinite(gaps).all():
        raise RuntimeError("invalid 60-gap feature window")
    return gaps


def _downside_semideviation(gaps: np.ndarray) -> float:
    downside = np.minimum(gaps, 0.0)
    return float(np.sqrt(np.mean(np.square(downside))))


def _gap_features(
    qfq: pd.DataFrame,
    none: pd.DataFrame,
    signal_day: date,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    qfq_position = _signal_position(qfq, signal_day)
    none_position = _signal_position(none, signal_day)
    if qfq_position is None or none_position is None:
        return _empty_features("missing_or_duplicate_signal_day"), {
            "error": "missing_or_duplicate_signal_day",
            "signal_day": signal_day.isoformat(),
            "qfq_match_count": _day_values(qfq).count(signal_day),
            "none_match_count": _day_values(none).count(signal_day),
        }
    if qfq_position < GAP_OBSERVATIONS or none_position < GAP_OBSERVATIONS:
        result = _empty_features("insufficient_60_gap_history")
        result["factor_lookback_bars"] = min(qfq_position, none_position) + 1
        return result, None

    qfq_window = qfq.iloc[
        qfq_position - GAP_OBSERVATIONS : qfq_position + 1
    ].copy()
    none_window = none.iloc[
        none_position - GAP_OBSERVATIONS : none_position + 1
    ].copy()
    qfq_days = _day_values(qfq_window)
    none_days = _day_values(none_window)
    if qfq_days != none_days:
        return _empty_features("qfq_none_window_date_mismatch"), {
            "error": "qfq_none_window_date_mismatch",
            "signal_day": signal_day.isoformat(),
            "qfq_days": [value.isoformat() for value in qfq_days],
            "none_days": [value.isoformat() for value in none_days],
        }

    qfq_gaps = _gap_array(qfq_window)
    none_gaps = _gap_array(none_window)
    chase_value: float | None = None
    chase_qualifying_days: int | None = None
    chase_history_available = qfq_position >= GAP_OBSERVATIONS + 1
    if chase_history_available:
        chase_window = qfq.iloc[
            qfq_position - GAP_OBSERVATIONS - 1 : qfq_position + 1
        ].copy()
        closes = pd.to_numeric(
            chase_window["close"],
            errors="coerce",
        ).to_numpy(dtype=float)
        prior_returns = closes[1:61] / closes[0:60] - 1.0
        if len(prior_returns) != GAP_OBSERVATIONS or not np.isfinite(
            prior_returns
        ).all():
            raise RuntimeError("invalid chase diagnostic return window")
        qualifying = prior_returns >= LARGE_UP_DAY_THRESHOLD - 1e-12
        chase_qualifying_days = int(np.sum(qualifying))
        if chase_qualifying_days:
            chase_value = float(np.mean(np.maximum(qfq_gaps[qualifying], 0.0)))

    return {
        "factor_assignment_available": True,
        "factor_error": None,
        PRIMARY_FACTOR: _downside_semideviation(qfq_gaps),
        "large_down_gap_frequency_60": float(
            np.mean(qfq_gaps <= LARGE_DOWN_GAP_THRESHOLD + 1e-12)
        ),
        "worst_down_gap_60": float(max(-float(np.min(qfq_gaps)), 0.0)),
        "none_downside_gap_semideviation_60": _downside_semideviation(
            none_gaps
        ),
        "open_limit_down_proxy_frequency_60": float(
            np.mean(none_gaps <= LIMIT_DOWN_PROXY_THRESHOLD + 1e-12)
        ),
        "post_large_up_day_chase_gap_mean_60": chase_value,
        "gap_observations": GAP_OBSERVATIONS,
        "factor_lookback_bars": MAIN_WINDOW_BARS,
        "chase_history_available": chase_history_available,
        "chase_qualifying_days": chase_qualifying_days,
        "entry_timing": "T+1",
    }, None


def _factor_features_for_sources(
    sources: list[dict[str, Any]],
    cache: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
    history_snapshot: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    features: dict[str, dict[str, Any]] = {}
    errors: Counter[str] = Counter()
    coverage_failures: list[dict[str, Any]] = []
    consistency_failures: list[dict[str, Any]] = []
    used_symbols: set[str] = set()
    for source in sources:
        identifier = candidate_id(source)
        if identifier in features:
            raise RuntimeError(f"duplicate candidate id after integrity gate: {identifier}")
        symbol = str(source.get("symbol", "")).zfill(6)
        used_symbols.add(symbol)
        qfq, none = _load_history_pair(symbol, cache)
        signal_day = date.fromisoformat(str(source["signal_day"]))
        base = {
            "candidate_id": identifier,
            "normalized_regime": (
                str(source.get("regime", "unknown")).strip().lower()
                if str(source.get("regime", "unknown")).strip().lower()
                in VALID_REGIMES
                else "unknown"
            ),
        }
        factor, failure = _gap_features(qfq, none, signal_day)
        if failure is not None:
            failure.update({"candidate_id": identifier, "symbol": symbol})
            if failure["error"] == "qfq_none_window_date_mismatch":
                consistency_failures.append(failure)
            else:
                coverage_failures.append(failure)
        if factor.get("factor_error"):
            errors[str(factor["factor_error"])] += 1
        base.update(factor)
        features[identifier] = base

    used_history = {
        key: history_snapshot[key]
        for symbol in sorted(used_symbols)
        for key in (f"{symbol}|qfq", f"{symbol}|none")
    }
    return features, {
        "history_manifest_sha256": _history_manifest(used_history),
        "history_file_count": len(used_history),
        "history_symbol_count": len(used_symbols),
        "history_input_safe": True,
        "history_input_failures": [],
        "replay_history_coverage_safe": not coverage_failures,
        "replay_history_coverage_failures": coverage_failures,
        "qfq_none_window_consistency_safe": not consistency_failures,
        "qfq_none_window_consistency_failures": consistency_failures,
        "factor_errors": dict(errors),
        "diagnostic_unavailable_counts": {
            factor: sum(value.get(factor) is None for value in features.values())
            for factor, spec in FACTOR_SPECS.items()
            if spec["role"] == "diagnostic"
        },
        "primary_definition": (
            "sqrt(mean(min(qfq_open_t/qfq_close_t_minus_1-1,0)^2))_"
            "over_60_gaps_through_signal_day"
        ),
        "qfq_none_window_dates_must_match": True,
        "diagnostic_thresholds": {
            "large_down_gap": LARGE_DOWN_GAP_THRESHOLD,
            "open_limit_down_proxy": LIMIT_DOWN_PROXY_THRESHOLD,
            "prior_large_up_day": LARGE_UP_DAY_THRESHOLD,
        },
        "signal_day_closed_bars_only": True,
        "post_signal_suffix_cannot_influence_features": True,
        "entry_timing": "T+1",
    }


def _assert_history_safe(factor_meta: dict[str, Any]) -> None:
    failures = (
        list(factor_meta.get("history_input_failures") or [])
        + list(factor_meta.get("replay_history_coverage_failures") or [])
        + list(factor_meta.get("qfq_none_window_consistency_failures") or [])
    )
    if failures:
        raise RuntimeError(
            "history safety gate failed before replay: "
            f"count={len(failures)}, examples={failures[:3]}"
        )


def _assign_primary_halves(rows: list[dict[str, Any]]) -> None:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row["primary_low_gap_risk"] = None
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
            row["primary_low_gap_risk"] = low
            row["variant_included"] = low


def _validate_authoritative_outcomes(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        identifier = candidate_id(row)
        for outcome in FIELDS:
            value = row.get(outcome)
            if value is None:
                continue
            if isinstance(value, (bool, np.bool_)) or not isinstance(
                value,
                (int, float, np.integer, np.floating),
            ):
                raise RuntimeError(
                    f"authoritative replay outcome is not numeric: "
                    f"candidate_id={identifier}, outcome={outcome}, value={value!r}"
                )
            if not math.isfinite(float(value)):
                raise RuntimeError(
                    f"authoritative replay outcome is not finite: "
                    f"candidate_id={identifier}, outcome={outcome}, value={value!r}"
                )


def _valid_outcome_rows(
    rows: list[dict[str, Any]],
    outcome: str,
) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    for row in rows:
        value = row.get(outcome)
        if value is None:
            continue
        if isinstance(value, (bool, np.bool_)) or not isinstance(
            value,
            (int, float, np.integer, np.floating),
        ) or not math.isfinite(float(value)):
            raise RuntimeError(
                f"invalid authoritative outcome for bootstrap: "
                f"candidate_id={row.get('candidate_id')}, "
                f"outcome={outcome}, value={value!r}"
            )
        if (
            str(row.get("symbol", "")).strip()
            and row.get("primary_low_gap_risk") is not None
        ):
            valid.append(row)
    return valid


def _outcome_sample_gate(
    rows: list[dict[str, Any]],
    outcome: str,
) -> dict[str, Any]:
    valid = _valid_outcome_rows(rows, outcome)
    low = [row for row in valid if row.get("primary_low_gap_risk")]
    high = [row for row in valid if row.get("primary_low_gap_risk") is False]
    candidates = {str(row["candidate_id"]) for row in valid}
    symbols = {str(row["symbol"]) for row in valid}
    days = {str(row["signal_day"]) for row in valid}
    sufficient = bool(
        len(candidates) >= MIN_AFFECTED_CANDIDATES
        and len(symbols) >= MIN_AFFECTED_SYMBOLS
        and len(days) >= MIN_AFFECTED_SIGNAL_DAYS
        and low
        and high
    )
    return {
        "outcome": outcome,
        "valid_candidates": len(candidates),
        "valid_symbols": len(symbols),
        "valid_signal_days": len(days),
        "low_n": len(low),
        "high_n": len(high),
        "minimum_candidates": MIN_AFFECTED_CANDIDATES,
        "minimum_symbols": MIN_AFFECTED_SYMBOLS,
        "minimum_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
        "sufficient": sufficient,
    }


def _cluster_bootstrap_low_minus_high(
    rows: list[dict[str, Any]],
    outcome: str,
    *,
    reps: int,
    seed: int,
) -> dict[str, Any]:
    valid = _valid_outcome_rows(rows, outcome)
    low = [float(row[outcome]) for row in valid if row["primary_low_gap_risk"]]
    high = [
        float(row[outcome])
        for row in valid
        if row["primary_low_gap_risk"] is False
    ]
    observed = float(np.mean(low) - np.mean(high)) if low and high else None
    clusters: dict[str, list[dict[str, Any]]] = {}
    for row in valid:
        clusters.setdefault(str(row["symbol"]), []).append(row)
    names = sorted(clusters)
    if observed is None or not names or reps <= 0:
        return {
            "mean_delta": observed,
            "ci95_low": None,
            "ci95_high": None,
            "reps_requested": reps,
            "reps_valid": 0,
            "cluster_count": len(names),
            "seed": seed,
        }
    rng = np.random.default_rng(seed)
    samples: list[float] = []
    for _ in range(reps):
        selected_indexes = rng.integers(0, len(names), size=len(names))
        sampled_rows = [
            row
            for selected_index in selected_indexes
            for row in clusters[names[int(selected_index)]]
        ]
        sampled_low = [
            float(row[outcome])
            for row in sampled_rows
            if row["primary_low_gap_risk"]
        ]
        sampled_high = [
            float(row[outcome])
            for row in sampled_rows
            if row["primary_low_gap_risk"] is False
        ]
        if sampled_low and sampled_high:
            samples.append(float(np.mean(sampled_low) - np.mean(sampled_high)))
    if samples:
        ci_low, ci_high = np.percentile(
            np.asarray(samples, dtype=float),
            [2.5, 97.5],
            method="linear",
        )
    else:
        ci_low, ci_high = None, None
    return {
        "mean_delta": observed,
        "ci95_low": float(ci_low) if ci_low is not None else None,
        "ci95_high": float(ci_high) if ci_high is not None else None,
        "reps_requested": reps,
        "reps_valid": len(samples),
        "cluster_count": len(names),
        "seed": seed,
    }


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
        "non_gating_diagnostic": factor != PRIMARY_FACTOR,
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
            row
            for row in regime_rows
            if row.get("primary_low_gap_risk") is not None
        ]
        low = [row for row in comparable if row["primary_low_gap_risk"]]
        high = [
            row
            for row in comparable
            if row["primary_low_gap_risk"] is False
        ]
        reports[regime] = {
            "n": len(regime_rows),
            "comparable_n": len(comparable),
            "low_n": len(low),
            "high_n": len(high),
            "mean_delta_low_minus_high": {
                field: (
                    float(
                        np.mean(
                            [float(row[field]) for row in low if row.get(field) is not None]
                        )
                    )
                    - float(
                        np.mean(
                            [
                                float(row[field])
                                for row in high
                                if row.get(field) is not None
                            ]
                        )
                    )
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
        row
        for row in assigned
        if row.get("primary_low_gap_risk") is not None
    ]
    low = [row for row in primary_rows if row["primary_low_gap_risk"]]
    high = [
        row for row in primary_rows if row["primary_low_gap_risk"] is False
    ]
    primary_outcomes = ("future_40d", "trade_pnl_pct")
    outcome_sample_gates = {
        outcome: _outcome_sample_gate(primary_rows, outcome)
        for outcome in primary_outcomes
    }
    bootstraps = {
        outcome: _cluster_bootstrap_low_minus_high(
            primary_rows,
            outcome,
            reps=PREREGISTERED_BOOTSTRAP_REPS,
            seed=seed,
        )
        for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
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
    outcome_samples_sufficient = all(
        report["sufficient"] for report in outcome_sample_gates.values()
    )
    direction_positive = all(
        bootstraps[outcome]["mean_delta"] is not None
        and bootstraps[outcome]["mean_delta"] > 0
        for outcome in primary_outcomes
    )
    contract_ok = all(
        _bootstrap_contract_ok(bootstraps[outcome])
        for outcome in primary_outcomes
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
        "primary_group_rule": (
            "same_signal_day_downside_gap_semideviation_60_lte_median_is_low"
        ),
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
            "rng": "numpy.random.default_rng_PCG64",
            "cluster_key": "symbol",
            "fixed_original_group_labels": True,
            "candidate_weighted_arithmetic_mean_delta": True,
            "missing_outcomes_excluded_before_clustering": True,
            "resample_n_unique_clusters_with_replacement": True,
            "repeated_cluster_repeats_whole_cluster_weight": True,
            "invalid_if_either_group_absent": True,
            "percentile_ci": [2.5, 97.5],
            "percentile_method": "linear",
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
            "outcome_specific": outcome_sample_gates,
            "all_primary_outcomes_sufficient": outcome_samples_sufficient,
        },
        "gate": {
            "assignment_coverage_ok": coverage >= MIN_ASSIGNMENT_COVERAGE,
            "sample_sufficient": sample_sufficient,
            "outcome_samples_sufficient": outcome_samples_sufficient,
            "replay_integrity_ok": bool(replay_integrity.get("all_pass")),
            "primary_direction_positive": direction_positive,
            "primary_bootstrap_contract_ok": contract_ok,
            "primary_cluster_bootstrap_ci95_positive": bootstrap_supported,
            "eligible_for_cross_split": bool(
                coverage >= MIN_ASSIGNMENT_COVERAGE
                and sample_sufficient
                and outcome_samples_sufficient
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
    raw_source_match: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_ids = [candidate_id(row) for row in sources]
    replay_ids = [candidate_id(row) for row in replay_rows]
    feature_ids = list(feature_map)
    explicit_replay_ids_match_identity_fields = all(
        str(row.get("candidate_id", "")) == candidate_id(row)
        for row in replay_rows
    )
    baseline_replay_complete = bool(
        raw_source_match.get("source_rows") == len(sources)
        and raw_source_match.get("common_eligible_rows") == len(sources)
        and raw_source_match.get("simulated_rows") == len(sources)
        and source_ids == replay_ids
        and not dict(replay_skips)
    )
    checks = {
        "source_ids_unique": len(source_ids) == len(set(source_ids)),
        "replay_ids_unique": len(replay_ids) == len(set(replay_ids)),
        "feature_ids_unique": len(feature_ids) == len(set(feature_ids)),
        "explicit_replay_ids_match_identity_fields": (
            explicit_replay_ids_match_identity_fields
        ),
        "source_replay_ids_order_match": source_ids == replay_ids,
        "source_feature_ids_order_match": source_ids == feature_ids,
        "replay_feature_ids_order_match": replay_ids == feature_ids,
        "replay_skips_empty": not dict(replay_skips),
        "source_rows_match": raw_source_match.get("source_rows") == len(sources),
        "eligible_rows_match": raw_source_match.get("common_eligible_rows")
        == len(sources),
        "replayed_rows_match": raw_source_match.get("simulated_rows")
        == len(sources),
        "completed_source_id_sequence_match": source_ids == replay_ids,
        "baseline_replay_complete": baseline_replay_complete,
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(f"replay integrity validation failed: {checks}")
    source_match = dict(raw_source_match)
    source_match.update(
        {
            "baseline_replay_complete": baseline_replay_complete,
            "source_candidate_ids_sha256": candidate_ids_sha256(sources),
            "replay_candidate_ids_sha256": candidate_ids_sha256(replay_rows),
            "source_outcomes_used": False,
            "replay_outcomes_used": True,
            "source_artifact_match_is_diagnostic": True,
        }
    )
    return checks, source_match


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
            directions[split][outcome] is not None
            and directions[split][outcome] > 0
            for split in SPLITS
            for outcome in ("future_40d", "trade_pnl_pct")
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
            "script SHA differs from the formally supplied snapshot: "
            f"expected={expected_script_sha256}, actual={actual_script_sha256}"
        )
    if Path(args.input_dir).expanduser().resolve() != CANONICAL_INPUT_DIR.resolve():
        raise RuntimeError(f"input dir must remain {CANONICAL_INPUT_DIR.resolve()}")
    if Path(args.output_dir).expanduser().resolve() != FORMAL_OUTPUT_DIR.resolve():
        raise RuntimeError(f"output dir must remain {FORMAL_OUTPUT_DIR.resolve()}")
    if Path(args.config).expanduser().resolve() != CONFIG_PATH.resolve():
        raise RuntimeError(f"config path must remain {CONFIG_PATH.resolve()}")


def _assert_output_directory_unused(output_dir: Path) -> Path:
    staging = output_dir.with_name(output_dir.name + ".tmp")
    conflicts = [path for path in (output_dir, staging) if path.exists()]
    if conflicts:
        raise RuntimeError(
            "formal output directory already exists; overwrite is forbidden: "
            + ", ".join(str(path) for path in conflicts)
        )
    return staging


def _write_outputs_atomically(
    output_dir: Path,
    staging_dir: Path,
    enriched_by_split: dict[str, list[dict[str, Any]]],
    result: dict[str, Any],
) -> None:
    staging_dir.mkdir(parents=True, exist_ok=False)
    artifacts: dict[str, Any] = {}
    for split in SPLITS:
        filename = f"downside_gap_risk_{split}.jsonl"
        staged = staging_dir / filename
        with staged.open("x", encoding="utf-8", newline="\n") as handle:
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
        final = output_dir / filename
        sha256 = file_sha256(staged)
        result["splits"][split]["enriched"] = str(final)
        result["splits"][split]["enriched_sha256"] = sha256
        artifacts[split] = {
            "path": str(final),
            "rows": len(enriched_by_split[split]),
            "sha256": sha256,
            "size_bytes": staged.stat().st_size,
        }
    result["artifacts"] = artifacts
    result["report_path"] = str(output_dir / "downside_gap_risk_audit.json")
    report_path = staging_dir / "downside_gap_risk_audit.json"
    with report_path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    staging_dir.replace(output_dir)


def run(args: argparse.Namespace) -> dict[str, Any]:
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

    sources_by_split: dict[str, list[dict[str, Any]]] = {}
    integrity_by_split: dict[str, dict[str, Any]] = {}
    symbols: set[str] = set()
    for split in SPLITS:
        source_path = input_dir / f"candidates_{split}.jsonl"
        sources = load_jsonl(source_path)
        sources_by_split[split] = sources
        integrity_by_split[split] = _validate_integrity_manifest(
            input_dir,
            split,
            source_path,
            sources,
        )
        symbols.update(str(row["symbol"]).zfill(6) for row in sources)

    history_initial = _snapshot_named_paths(_history_paths(symbols))
    history_manifest_initial = _history_manifest(history_initial)
    if history_manifest_initial != PREREGISTERED_HISTORY_MANIFEST_SHA256:
        raise RuntimeError(
            "history manifest differs from pre-registration: "
            f"expected={PREREGISTERED_HISTORY_MANIFEST_SHA256}, "
            f"actual={history_manifest_initial}"
        )

    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "design": {
            "candidate_layer_only": True,
            "primary_scope": "all_canonical",
            "primary_factor": PRIMARY_FACTOR,
            "primary_larger_is_better": False,
            "primary_comparison": "low_minus_high",
            "same_signal_day_comparison": True,
            "diagnostic_factors_cannot_pass_gate": [
                factor
                for factor, spec in FACTOR_SPECS.items()
                if spec["role"] == "diagnostic"
            ],
            "rank_ic_and_quantiles_non_gating": True,
            "canonical_candidates_required": True,
            "source_outcomes_used": False,
            "replay_outcomes_used": True,
            "authoritative_replay_profile": "production_risk",
            "required_splits": list(SPLITS),
            "signal_day_closed_bars_only": True,
            "entry_timing": "T+1",
            "holdout_consumed": False,
            "portfolio_layer_implemented": False,
            "production_eligible": False,
            "period_scan_performed": False,
            "threshold_scan_performed": False,
            "reverse_direction_tested": False,
        },
        "research_provenance": {
            "direction_source": "preregistered_lower_downside_gap_risk_hypothesis",
            "candidate_selected_after_prior_technical_factor_pause": True,
            "liquidity_factor_deferred_due_to_unavailable_local_fields": True,
            "outcome_free_precheck": {
                "train": {
                    "available": 89,
                    "comparable_candidates": 85,
                    "comparable_signal_days": 12,
                },
                "val": {
                    "available": 1107,
                    "comparable_candidates": 1103,
                    "comparable_signal_days": 96,
                },
                "test": {
                    "available": 346,
                    "comparable_candidates": 342,
                    "comparable_signal_days": 32,
                },
            },
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
            "gap_observations": GAP_OBSERVATIONS,
            "large_down_gap": LARGE_DOWN_GAP_THRESHOLD,
            "open_limit_down_proxy": LIMIT_DOWN_PROXY_THRESHOLD,
            "prior_large_up_day": LARGE_UP_DAY_THRESHOLD,
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
        "config_file_sha256_before": static_initial["config"]["sha256"],
        "config_snapshot": _config_snapshot(config),
        "script_expected_sha256": str(args.expected_script_sha256).lower(),
        "script_sha256": static_initial["audit_script"]["sha256"],
        "dependency_contract": dependency_contract,
        "candidate_integrity": integrity_by_split,
        "history_inventory": {
            "symbol_count": len(symbols),
            "file_count": len(history_initial),
            "expected_manifest_sha256": PREREGISTERED_HISTORY_MANIFEST_SHA256,
            "history_manifest_sha256": history_manifest_initial,
            "adjustments": ["qfq", "none"],
            "local_files_only": True,
        },
        "current_replay_provenance": {
            "driver": "attribution_audit._replay_split",
            "driver_profile": "production_risk",
            "driver_sha256": static_initial["attribution_audit"]["sha256"],
            "simulator": "backtest_winrate.simulate_single_trade",
            "simulator_sha256": static_initial["backtest_winrate"]["sha256"],
            "outcomes_generated_in_process": True,
            "external_replay_artifact_consumed": False,
        },
        "seed": args.seed,
        "bootstrap_reps": args.bootstrap_reps,
        "splits": {},
    }

    history_cache: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    features_by_split: dict[str, dict[str, dict[str, Any]]] = {}
    factor_meta_by_split: dict[str, dict[str, Any]] = {}
    for split in SPLITS:
        features, factor_meta = _factor_features_for_sources(
            sources_by_split[split],
            history_cache,
            history_initial,
        )
        _assert_history_safe(factor_meta)
        features_by_split[split] = features
        factor_meta_by_split[split] = factor_meta

    static_pre_replay, static_pre_replay_changes = _snapshot_changes(static_initial)
    history_pre_replay, history_pre_replay_changes = _snapshot_changes(history_initial)
    if static_pre_replay_changes or history_pre_replay_changes:
        raise RuntimeError(
            "protected inputs changed before replay: "
            f"static={static_pre_replay_changes}, history={history_pre_replay_changes}"
        )
    result["pre_replay_input_stability"] = {
        "all_unchanged": True,
        "static_path_count": len(static_initial),
        "history_path_count": len(history_initial),
        "static_manifest_sha256": _snapshot_manifest(static_pre_replay),
        "history_manifest_sha256": _history_manifest(history_pre_replay),
        "changed_paths": [],
    }

    enriched_by_split: dict[str, list[dict[str, Any]]] = {}
    for split in SPLITS:
        sources = sources_by_split[split]
        replay_rows, replay_skips, raw_source_match = _production_replay_split(
            sources,
            config,
            "production_risk",
            HISTORY_DIR,
        )
        replay_integrity, source_match = _assert_replay_integrity(
            sources,
            features_by_split[split],
            replay_rows,
            replay_skips,
            raw_source_match,
        )
        _validate_authoritative_outcomes(replay_rows)
        enriched: list[dict[str, Any]] = []
        for replay in replay_rows:
            identifier = candidate_id(replay)
            row = dict(replay)
            row.update(features_by_split[split][identifier])
            enriched.append(row)
        _assign_primary_halves(enriched)
        enriched_by_split[split] = enriched
        source_path = input_dir / f"candidates_{split}.jsonl"
        result["splits"][split] = {
            "source": str(source_path),
            "source_sha256": static_initial[f"canonical_{split}"]["sha256"],
            "source_rows": len(sources),
            "enriched": None,
            "enriched_sha256": None,
            "replay_skips": dict(replay_skips),
            "candidate_report": _candidate_report(
                enriched,
                source_match,
                replay_integrity,
                factor_meta_by_split[split],
                PREREGISTERED_SEED,
            ),
        }

    result["candidate_gate"] = _cross_split_gate(result["splits"])
    result["baseline_replay_complete"] = all(
        result["splits"][split]["candidate_report"]["source_match"]
        ["baseline_replay_complete"]
        for split in SPLITS
    )
    static_after, static_after_changes = _snapshot_changes(static_initial)
    history_after, history_after_changes = _snapshot_changes(history_initial)
    if static_after_changes or history_after_changes:
        raise RuntimeError(
            "protected inputs changed during audit: "
            f"static={static_after_changes}, history={history_after_changes}"
        )
    result["protected_inputs"] = {
        "all_unchanged": True,
        "static_path_count": len(static_initial),
        "history_path_count": len(history_initial),
        "total_path_count": len(static_initial) + len(history_initial),
        "static_initial_manifest_sha256": _snapshot_manifest(static_initial),
        "static_after_manifest_sha256": _snapshot_manifest(static_after),
        "history_initial_manifest_sha256": _history_manifest(history_initial),
        "history_after_manifest_sha256": _history_manifest(history_after),
        "changed_paths": [],
    }
    result["config_file_sha256_after"] = static_after["config"]["sha256"]
    result["config_file_unchanged"] = (
        result["config_file_sha256_after"]
        == result["config_file_sha256_before"]
    )
    if not result["baseline_replay_complete"]:
        raise RuntimeError("baseline replay became incomplete after integrity gate")
    result["verdict"] = (
        "candidate_layer_historical_support_requires_portfolio_rule_preregistration"
        if result["candidate_gate"]["pass"]
        else "candidate_layer_rejected_or_insufficient_sample"
    )
    result["production_decision"] = "unchanged_P0"
    _write_outputs_atomically(
        output_dir,
        staging_dir,
        enriched_by_split,
        result,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=str(CANONICAL_INPUT_DIR))
    parser.add_argument("--output-dir", default=str(FORMAL_OUTPUT_DIR))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--label", default=PREREGISTERED_LABEL)
    parser.add_argument("--splits", nargs="+", default=list(SPLITS))
    parser.add_argument("--seed", type=int, default=PREREGISTERED_SEED)
    parser.add_argument(
        "--bootstrap-reps",
        type=int,
        default=PREREGISTERED_BOOTSTRAP_REPS,
    )
    parser.add_argument("--expected-script-sha256", required=True)
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
                "protected_inputs_unchanged": result["protected_inputs"]
                ["all_unchanged"],
                "production_decision": result["production_decision"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
