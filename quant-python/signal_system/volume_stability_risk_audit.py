"""Audit 6x10-block QFQ volume instability as a candidate risk factor.

The pre-registered hypothesis is that candidates with a lower coefficient of
variation across six chronological non-overlapping 10-session mean-volume
blocks have better current production-risk replay outcomes.  A frozen,
outcome-free QFQ/NONE continuity contract is rerun in-process before replay.
The primary estimator is a signal-day fixed-effect low-minus-high coefficient.
Holdout, portfolio construction, databases, network fetches, amount factors,
parameter scans, and reverse tests are forbidden.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import volume_unit_continuity_audit as volume_contract
from attribution_audit import (
    FIELDS,
    _config_snapshot,
    stats,
)
from attribution_audit import (
    _replay_split as _production_replay_split,
)
from backtest_winrate import HISTORY_DIR
from candidate_integrity import (
    VERSION as INTEGRITY_VERSION,
)
from candidate_integrity import (
    candidate_id,
    candidate_ids_sha256,
    file_sha256,
    load_jsonl,
)
from macd_divergence_audit import _daily_rank_ic, _quantile_report
from utils.helpers import load_config

VERSION = "volume_stability_risk_audit.v1"
SPLITS = ("train", "val", "test")
PREREGISTERED_LABEL = "volume-stability60-risk-candidate-audit"
PREREGISTERED_SEED = 20260907
PREREGISTERED_BOOTSTRAP_REPS = 2000
CANONICAL_INPUT_DIR = Path(r"D:\tmp\candidates_fullpool_canonical")
FORMAL_OUTPUT_DIR = Path(
    r"D:\tmp\volume_stability60_risk_candidate_final"
)
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
    "0be91a8fd76f1b436b01a67a1e5612995bdee6dedd3b4902ac4f90a7f835635d"
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
    "volume_unit_continuity_audit": BASE_DIR
    / "volume_unit_continuity_audit.py",
    "market_data": BASE_DIR / "data" / "market_data.py",
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
    "volume_unit_continuity_audit": "2aef3b363f9dd53c9feada345a6bf35efefb3d9141c0ae71eba3f0711442f53a",
    "market_data": "07cc9e23d1686811a6ef188ae44233b07e4ff293f3572479ba168982a7ab4f82",
}

PRIMARY_FACTOR = "volume_instability_cv_6x10"
FACTOR_SPECS: dict[str, dict[str, Any]] = {
    PRIMARY_FACTOR: {"larger_is_better": False, "role": "primary"},
    "daily_volume_cv_60": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
    "block_mean_volume_range_ratio_6x10": {
        "larger_is_better": False,
        "role": "diagnostic",
    },
    "latest_block_mean_volume_ratio_6x10": {
        "larger_is_better": None,
        "role": "diagnostic",
    },
    "minimum_block_mean_volume_ratio_6x10": {
        "larger_is_better": True,
        "role": "diagnostic",
    },
    "block_mean_volume_trend_slope_ratio_6x10": {
        "larger_is_better": None,
        "role": "diagnostic",
    },
}
VOLUME_OBSERVATIONS = 60
BLOCK_COUNT = 6
BLOCK_VOLUME_OBSERVATIONS = 10
MIN_ASSIGNMENT_COVERAGE = 0.90
MIN_AFFECTED_CANDIDATES = 30
MIN_AFFECTED_SYMBOLS = 10
MIN_AFFECTED_SIGNAL_DAYS = 10
MIN_OUTCOME_CLUSTERS = 10
VALID_REGIMES = {"bull", "range", "bear"}
SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
SOURCE_LEGACY_OUTCOME_FIELDS = frozenset(
    {
        *FIELDS,
        "entry_day",
        "exit_reason",
        "holding_days",
    }
)


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


def _snapshot_changes(
    initial: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    current = _snapshot_named_paths(
        {name: Path(str(item["path"])) for name, item in initial.items()}
    )
    changes: list[dict[str, Any]] = []
    for name in sorted(initial):
        before = initial[name]
        after = current[name]
        if (
            before["sha256"] != after["sha256"]
            or before["size_bytes"] != after["size_bytes"]
        ):
            changes.append(
                {
                    "name": name,
                    "path": before["path"],
                    "before_sha256": before["sha256"],
                    "after_sha256": after["sha256"],
                    "before_size_bytes": before["size_bytes"],
                    "after_size_bytes": after["size_bytes"],
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


def _history_paths(symbols: Iterable[str]) -> dict[str, Path]:
    return {
        f"{adjustment}:{symbol}": HISTORY_DIR / f"{symbol}_{adjustment}.pkl"
        for symbol in sorted({_normalize_symbol(value) for value in symbols})
        for adjustment in ("none", "qfq")
    }


def _history_manifest(snapshot: dict[str, dict[str, Any]]) -> str:
    return _snapshot_manifest(snapshot)


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
    snapshot: dict[str, dict[str, Any]],
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


def _validate_symbol_day_uniqueness(rows: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for row in rows:
        identity = _identity_projection(row)
        key = (identity["symbol"], identity["signal_day"])
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        raise RuntimeError(
            "canonical symbol x signal_day must be unique: "
            f"{sorted(set(duplicates))[:10]}"
        )


def _replay_input_projection(row: dict[str, Any]) -> dict[str, Any]:
    """Remove frozen legacy outcomes before the authoritative replay."""
    return {
        key: value
        for key, value in row.items()
        if key not in SOURCE_LEGACY_OUTCOME_FIELDS
    }


def _validate_history_frame(
    frame: pd.DataFrame,
    path: Path,
    expected_adjustment: str = "qfq",
) -> pd.DataFrame:
    """Delegate to the frozen strict dual-adjustment frame validator."""
    return volume_contract._validate_history_frame(
        frame,
        path,
        expected_adjustment,
    )


def _load_history(path: Path, adjustment: str) -> pd.DataFrame:
    return volume_contract._load_history(path, adjustment)


def _volume_stability_features(
    volume: pd.Series,
    signal_day: str,
) -> dict[str, float | int]:
    cutoff = pd.Timestamp(signal_day)
    if cutoff not in volume.index:
        raise RuntimeError(
            f"signal day is absent from closed QFQ history: {signal_day}"
        )
    window = volume.loc[:cutoff].tail(VOLUME_OBSERVATIONS)
    if len(window) != VOLUME_OBSERVATIONS:
        raise RuntimeError(
            f"insufficient {VOLUME_OBSERVATIONS}-bar volume history: "
            f"{signal_day}"
        )
    return volume_contract._volume_features(window.to_numpy(dtype=float))


def _empty_features(identity: dict[str, str]) -> dict[str, Any]:
    return {
        "candidate_id": (
            f"{identity['symbol']}|{identity['signal_day']}|{identity['signal_type']}"
        ),
        **{factor: None for factor in FACTOR_SPECS},
        "factor_assignment_available": False,
        "primary_low_volume_instability_risk": None,
        "variant_included": None,
        "volume_observations": VOLUME_OBSERVATIONS,
        "block_count": BLOCK_COUNT,
        "block_volume_observations": BLOCK_VOLUME_OBSERVATIONS,
        "entry_timing": "T+1",
    }


def _factor_features_for_sources(
    sources: list[dict[str, Any]],
    history_cache: dict[str, pd.DataFrame],
    history_snapshot: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    _validate_symbol_day_uniqueness(sources)
    by_day: dict[str, list[dict[str, str]]] = defaultdict(list)
    identities: list[dict[str, str]] = []
    for source in sources:
        identity = _identity_projection(source)
        identities.append(identity)
        by_day[identity["signal_day"]].append(identity)

    features: dict[str, dict[str, Any]] = {
        candidate_id(identity): _empty_features(identity)
        for identity in identities
    }
    for identity in identities:
        symbol = identity["symbol"]
        if symbol not in history_cache:
            history_item = history_snapshot.get(f"qfq:{symbol}")
            none_item = history_snapshot.get(f"none:{symbol}")
            if history_item is None or none_item is None:
                raise RuntimeError(
                    f"history snapshot missing adjustment pair for {symbol}"
                )
            path = Path(str(history_item["path"]))
            history_cache[symbol] = _load_history(path, "qfq")
        frame = history_cache[symbol]
        computed = _volume_stability_features(
            frame["volume"],
            identity["signal_day"],
        )
        feature = features[candidate_id(identity)]
        feature.update(computed)
        feature["factor_assignment_available"] = True

    excluded_single_group_days = 0
    for day_identities in by_day.values():
        available = [
            features[candidate_id(identity)]
            for identity in day_identities
            if features[candidate_id(identity)][PRIMARY_FACTOR] is not None
        ]
        if len(available) < 2:
            excluded_single_group_days += 1
            continue
        median = float(
            np.median([float(item[PRIMARY_FACTOR]) for item in available])
        )
        labels = [float(item[PRIMARY_FACTOR]) <= median for item in available]
        if all(labels) or not any(labels):
            excluded_single_group_days += 1
            continue
        for item, low in zip(available, labels):
            item["primary_low_volume_instability_risk"] = low
            item["variant_included"] = low
            item["same_day_primary_factor_median"] = median

    used_symbols = {identity["symbol"] for identity in identities}
    used_history = {
        name: item
        for name, item in history_snapshot.items()
        if name.split(":", 1)[1] in used_symbols
    }
    unavailable_candidates = sum(
        item[PRIMARY_FACTOR] is None for item in features.values()
    )
    meta = {
        "history_manifest_sha256": _history_manifest(used_history),
        "history_file_count": len(used_history),
        "history_symbol_count": len(used_symbols),
        "history_adjustments": ["none", "qfq"],
        "history_input_safe": True,
        "history_input_failures": [],
        "factor_errors": {},
        "insufficient_history_candidates": 0,
        "nonpositive_required_volume_candidates": 0,
        "primary_unavailable_candidates": unavailable_candidates,
        "excluded_single_group_signal_days": excluded_single_group_days,
        "primary_definition": (
            "population_coefficient_of_variation_of_6_chronological_"
            "nonoverlapping_10_session_mean_volume_blocks_from_last_60_"
            "closed_qfq_volume_observations"
        ),
        "volume_observations": VOLUME_OBSERVATIONS,
        "block_count": BLOCK_COUNT,
        "block_volume_observations": BLOCK_VOLUME_OBSERVATIONS,
        "block_order": "oldest_to_newest",
        "blocks_overlap": False,
        "block_aggregation": "arithmetic_mean",
        "cv_std_ddof": 0,
        "mean_volume_floor_used": False,
        "winsorization_used": False,
        "log_transform_used": False,
        "demeaning_used": False,
        "annualization_used": False,
        "window_constant_scale_invariant": True,
        "time_varying_adjustment_invariant": False,
        "signal_day_closed_bars_only": True,
        "post_signal_suffix_cannot_influence_features": True,
        "entry_timing": "T+1",
    }
    if unavailable_candidates:
        raise RuntimeError(
            "volume contract requires every canonical candidate to have an "
            f"available primary factor: unavailable={unavailable_candidates}"
        )
    return features, meta


def _validate_authoritative_outcomes(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        identifier = candidate_id(row)
        for outcome in FIELDS:
            if outcome not in row:
                raise RuntimeError(
                    f"authoritative replay outcome field is missing: "
                    f"candidate_id={identifier}, outcome={outcome}"
                )
            value = row[outcome]
            if value is None:
                continue
            if isinstance(value, (bool, np.bool_)) or not isinstance(
                value,
                (int, float, np.integer, np.floating),
            ):
                raise RuntimeError(  # noqa: TRY004
                    f"authoritative replay outcome is not numeric: "
                    f"candidate_id={identifier}, outcome={outcome}, value={value!r}"
                )
            if not math.isfinite(float(value)):
                raise RuntimeError(
                    f"authoritative replay outcome is not finite: "
                    f"candidate_id={identifier}, outcome={outcome}, value={value!r}"
                )


def _valid_fe_rows(
    rows: list[dict[str, Any]],
    outcome: str,
) -> list[dict[str, Any]]:
    preliminary: list[dict[str, Any]] = []
    for row in rows:
        if outcome not in row:
            raise RuntimeError(f"outcome field is missing: {outcome}")
        value = row[outcome]
        if (
            value is None
            or row.get("primary_low_volume_instability_risk") is None
        ):
            continue
        if isinstance(value, (bool, np.bool_)) or not isinstance(
            value,
            (int, float, np.integer, np.floating),
        ) or not math.isfinite(float(value)):
            raise RuntimeError(
                f"invalid authoritative outcome for FE estimator: "
                f"candidate_id={row.get('candidate_id')}, "
                f"outcome={outcome}, value={value!r}"
            )
        preliminary.append(row)
    labels_by_day: dict[str, set[bool]] = defaultdict(set)
    for row in preliminary:
        labels_by_day[str(row["signal_day"])].add(
            bool(row["primary_low_volume_instability_risk"])
        )
    valid_days = {day for day, labels in labels_by_day.items() if len(labels) == 2}
    return sorted(
        (
            row
            for row in preliminary
            if str(row["signal_day"]) in valid_days
        ),
        key=lambda row: (str(row["signal_day"]), candidate_id(row)),
    )


def _fixed_effect_beta(
    rows: list[dict[str, Any]],
    outcome: str,
    weights: dict[str, int] | None = None,
) -> float | None:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_day[str(row["signal_day"])].append(row)
    numerator = 0.0
    denominator = 0.0
    for day_rows in by_day.values():
        day_values: list[tuple[float, float, float]] = []
        for row in day_rows:
            symbol = _normalize_symbol(row["symbol"])
            weight = float(1 if weights is None else weights.get(symbol, 0))
            if weight <= 0:
                continue
            x_value = 1.0 if row[
                "primary_low_volume_instability_risk"
            ] is True else 0.0
            day_values.append((weight, x_value, float(row[outcome])))
        total_weight = sum(item[0] for item in day_values)
        if total_weight <= 0:
            continue
        x_mean = sum(w * x for w, x, _ in day_values) / total_weight
        y_mean = sum(w * y for w, _, y in day_values) / total_weight
        for weight, x_value, y_value in day_values:
            numerator += weight * (x_value - x_mean) * (y_value - y_mean)
            denominator += weight * (x_value - x_mean) ** 2
    if denominator <= 0:
        return None
    beta = numerator / denominator
    return float(beta) if math.isfinite(beta) else None


def _outcome_sample_gate(
    rows: list[dict[str, Any]],
    outcome: str,
) -> dict[str, Any]:
    valid = _valid_fe_rows(rows, outcome)
    low = [
        row
        for row in valid
        if row["primary_low_volume_instability_risk"] is True
    ]
    high = [
        row
        for row in valid
        if row["primary_low_volume_instability_risk"] is False
    ]
    candidates = {str(row["candidate_id"]) for row in valid}
    symbols = {_normalize_symbol(row["symbol"]) for row in valid}
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
        "valid_signal_days_with_both_groups": len(days),
        "low_n": len(low),
        "high_n": len(high),
        "minimum_candidates": MIN_AFFECTED_CANDIDATES,
        "minimum_symbols": MIN_AFFECTED_SYMBOLS,
        "minimum_signal_days": MIN_AFFECTED_SIGNAL_DAYS,
        "sufficient": sufficient,
    }


def _cluster_bootstrap_fe_low_minus_high(
    rows: list[dict[str, Any]],
    outcome: str,
    *,
    reps: int,
    seed: int,
) -> dict[str, Any]:
    valid = _valid_fe_rows(rows, outcome)
    observed = _fixed_effect_beta(valid, outcome)
    symbols = sorted({_normalize_symbol(row["symbol"]) for row in valid})
    if observed is None or not symbols or reps <= 0:
        return {
            "fixed_effect_beta": observed,
            "ci95_low": None,
            "ci95_high": None,
            "reps_requested": reps,
            "reps_valid": 0,
            "cluster_count": len(symbols),
            "signal_day_count": len({str(row["signal_day"]) for row in valid}),
            "seed": seed,
        }
    rng = np.random.default_rng(seed)
    samples: list[float] = []
    for _ in range(reps):
        selected = rng.integers(0, len(symbols), size=len(symbols))
        counts = np.bincount(selected, minlength=len(symbols))
        weights = {
            symbol: int(counts[index])
            for index, symbol in enumerate(symbols)
            if counts[index] > 0
        }
        beta = _fixed_effect_beta(valid, outcome, weights)
        if beta is not None:
            samples.append(beta)
    if samples:
        ci_low, ci_high = np.percentile(
            np.asarray(samples, dtype=float),
            [2.5, 97.5],
            method="linear",
        )
    else:
        ci_low, ci_high = None, None
    return {
        "fixed_effect_beta": observed,
        "ci95_low": float(ci_low) if ci_low is not None else None,
        "ci95_high": float(ci_high) if ci_high is not None else None,
        "reps_requested": reps,
        "reps_valid": len(samples),
        "cluster_count": len(symbols),
        "signal_day_count": len({str(row["signal_day"]) for row in valid}),
        "seed": seed,
    }


def _factor_report(
    rows: list[dict[str, Any]],
    factor: str,
    larger_is_better: bool | None,
) -> dict[str, Any]:
    usable = [row for row in rows if row.get(factor) is not None]
    if larger_is_better is None:
        return {
            "n": len(usable),
            "coverage": len(usable) / len(rows) if rows else 0.0,
            "larger_is_better": None,
            "directional_analysis_performed": False,
            "non_gating_diagnostic": True,
            "raw_stats": stats([row.get(factor) for row in usable]),
            "rank_ic": {
                outcome: {
                    "computed": False,
                    "reason": "factor_direction_not_preregistered",
                }
                for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
            },
            "quantiles": {
                outcome: {
                    "computed": False,
                    "best_minus_worst_pp": None,
                    "reason": "factor_direction_not_preregistered",
                }
                for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
            },
        }
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
        regime_rows = [row for row in rows if row.get("normalized_regime") == regime]
        reports[regime] = {
            "n": len(regime_rows),
            "comparable_n": len(
                [
                    row
                    for row in regime_rows
                    if row.get("primary_low_volume_instability_risk")
                    is not None
                ]
            ),
            "fixed_effect_beta_low_minus_high": {
                outcome: _fixed_effect_beta(
                    _valid_fe_rows(regime_rows, outcome),
                    outcome,
                )
                for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
            },
        }
    return reports


def _bootstrap_contract_ok(report: dict[str, Any]) -> bool:
    return bool(
        report.get("fixed_effect_beta") is not None
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
        if row.get("primary_low_volume_instability_risk") is not None
    ]
    low = [
        row
        for row in primary_rows
        if row["primary_low_volume_instability_risk"]
    ]
    high = [
        row
        for row in primary_rows
        if row["primary_low_volume_instability_risk"] is False
    ]
    primary_outcomes = ("future_40d", "trade_pnl_pct")
    outcome_gates = {
        outcome: _outcome_sample_gate(primary_rows, outcome)
        for outcome in primary_outcomes
    }
    bootstraps = {
        outcome: _cluster_bootstrap_fe_low_minus_high(
            primary_rows,
            outcome,
            reps=PREREGISTERED_BOOTSTRAP_REPS,
            seed=seed,
        )
        for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
    }
    affected_ids = {str(row["candidate_id"]) for row in primary_rows}
    affected_symbols = {_normalize_symbol(row["symbol"]) for row in primary_rows}
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
        report["sufficient"] for report in outcome_gates.values()
    )
    direction_positive = all(
        bootstraps[outcome]["fixed_effect_beta"] is not None
        and bootstraps[outcome]["fixed_effect_beta"] > 0
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
            "same_signal_day_volume_instability_cv_6x10_"
            "lte_median_is_low"
        ),
        "primary_comparison": "signal_day_fixed_effect_low_minus_high",
        "primary_low_n": len(low),
        "primary_high_n": len(high),
        "baseline": {field: stats([row.get(field) for row in rows]) for field in FIELDS},
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
        "fixed_effect_cluster_bootstrap_low_minus_high": bootstraps,
        "fixed_effect_contract": {
            "observation": "unique_symbol_x_signal_day",
            "signal_day_fixed_effect": True,
            "x": "1_for_low_volume_instability_risk_0_for_high",
            "cluster_key": "normalized_canonical_symbol",
            "cluster_sort": "ascending_full_six_digit_symbol",
            "fixed_factor_and_group_labels": True,
            "multiplicity_weighted_day_means": True,
            "rng": "numpy.random.default_rng_PCG64",
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
            "outcome_specific": outcome_gates,
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
    explicit_ids_match = all(
        str(row.get("candidate_id", "")) == candidate_id(row)
        for row in replay_rows
    )
    baseline_complete = bool(
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
        "explicit_replay_ids_match_identity_fields": explicit_ids_match,
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
        "baseline_replay_complete": baseline_complete,
    }
    checks["all_pass"] = all(checks.values())
    if not checks["all_pass"]:
        raise RuntimeError(f"replay integrity validation failed: {checks}")
    source_match = dict(raw_source_match)
    source_match.update(
        {
            "baseline_replay_complete": baseline_complete,
            "source_candidate_ids_sha256": candidate_ids_sha256(sources),
            "replay_candidate_ids_sha256": candidate_ids_sha256(replay_rows),
            "source_outcomes_used": False,
            "source_outcomes_used_for_diagnostic": False,
            "source_legacy_outcomes_stripped_before_replay": True,
            "source_legacy_outcome_fields_stripped": sorted(
                SOURCE_LEGACY_OUTCOME_FIELDS
            ),
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
        and split_reports[split]["candidate_report"]["gate"][
            "eligible_for_cross_split"
        ]
    ]
    all_required_eligible = required_present and eligible == list(SPLITS)
    directions = {
        split: {
            outcome: split_reports[split]["candidate_report"]
            ["fixed_effect_cluster_bootstrap_low_minus_high"][outcome]
            ["fixed_effect_beta"]
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
    if tuple(getattr(args, "splits", ())) != SPLITS:
        raise RuntimeError(f"all required splits must remain in order {SPLITS}")
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
            "script SHA differs from formally supplied snapshot: "
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
    static_initial: dict[str, dict[str, Any]],
    history_initial: dict[str, dict[str, Any]],
) -> None:
    staging_dir.mkdir(parents=True, exist_ok=False)
    artifacts: dict[str, Any] = {}
    for split in SPLITS:
        filename = f"volume_stability_risk_{split}.jsonl"
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
        digest = file_sha256(staged)
        result["splits"][split]["enriched"] = str(final)
        result["splits"][split]["enriched_sha256"] = digest
        artifacts[split] = {
            "path": str(final),
            "rows": len(enriched_by_split[split]),
            "sha256": digest,
            "size_bytes": staged.stat().st_size,
        }

    static_pre_publish, static_changes = _snapshot_changes(static_initial)
    history_pre_publish, history_changes = _snapshot_changes(history_initial)
    if static_changes or history_changes:
        raise RuntimeError(
            "protected inputs changed before publish: "
            f"static={static_changes}, history={history_changes}"
        )
    result["pre_publish_input_stability"] = {
        "all_unchanged": True,
        "static_manifest_sha256": _snapshot_manifest(static_pre_publish),
        "history_manifest_sha256": _history_manifest(history_pre_publish),
        "changed_paths": [],
    }
    result["artifacts"] = artifacts
    result["report_path"] = str(
        output_dir / "volume_stability_risk_audit.json"
    )
    report_path = staging_dir / "volume_stability_risk_audit.json"
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
        _validate_symbol_day_uniqueness(sources)
        sources_by_split[split] = sources
        integrity_by_split[split] = _validate_integrity_manifest(
            input_dir,
            split,
            source_path,
            sources,
        )
        symbols.update(_normalize_symbol(row["symbol"]) for row in sources)

    history_initial = _snapshot_named_paths(_history_paths(symbols))
    history_manifest_initial = _history_manifest(history_initial)
    if history_manifest_initial != PREREGISTERED_HISTORY_MANIFEST_SHA256:
        raise RuntimeError(
            "history manifest differs from pre-registration: "
            f"expected={PREREGISTERED_HISTORY_MANIFEST_SHA256}, "
            f"actual={history_manifest_initial}"
        )
    volume_data_contract = volume_contract.audit(input_dir, config_path)
    contract = volume_data_contract.get("contract", {})
    contract_inventory = volume_data_contract.get("history_inventory", {})
    if (
        contract.get("all_pass") is not True
        or contract.get("relative_window_factor_research_allowed") is not True
        or contract_inventory.get("manifest_sha256")
        != history_manifest_initial
        or contract_inventory.get("file_count") != len(history_initial)
        or contract_inventory.get("symbol_count") != len(symbols)
    ):
        raise RuntimeError(
            "in-process volume continuity contract failed binding checks: "
            f"contract={contract}, inventory={contract_inventory}"
        )

    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "design": {
            "candidate_layer_only": True,
            "candidate_risk_role": "qfq_six_block_mean_volume_instability_proxy",
            "primary_scope": "all_canonical",
            "primary_factor": PRIMARY_FACTOR,
            "primary_larger_is_better": False,
            "primary_comparison": "signal_day_fixed_effect_low_minus_high",
            "same_signal_day_comparison": True,
            "qfq_six_block_mean_volume_cv_proxy": True,
            "fixed_chronological_6x10_partition": True,
            "nonoverlapping_volume_blocks": True,
            "block_aggregation": "arithmetic_mean",
            "cv_population_std_ddof": 0,
            "required_volume_strictly_positive": True,
            "nonpositive_required_volume_global_failure": True,
            "mean_volume_floor_used": False,
            "winsorization_used": False,
            "log_transform_used": False,
            "demeaning_used": False,
            "annualization_used": False,
            "frozen_qfq_none_pair_contract": True,
            "amount_used_as_factor": False,
            "amount_unit_contract_check_performed": True,
            "amount_used_only_for_unit_consistency_check": True,
            "absolute_cross_symbol_volume_ranking_used": False,
            "window_constant_scale_invariant": True,
            "time_varying_adjustment_invariance_claimed": False,
            "diagnostic_factors_cannot_pass_gate": [
                factor
                for factor, spec in FACTOR_SPECS.items()
                if spec["role"] == "diagnostic"
            ],
            "rank_ic_and_quantiles_non_gating": True,
            "source_outcomes_used": False,
            "source_outcomes_used_for_diagnostic": False,
            "source_legacy_outcomes_stripped_before_replay": True,
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
            "block_length_scan_performed": False,
            "block_count_scan_performed": False,
            "block_start_phase_scan_performed": False,
            "overlapping_window_scan_performed": False,
            "ddof_scan_performed": False,
            "aggregation_scan_performed": False,
            "transform_scan_performed": False,
            "reverse_direction_tested": False,
        },
        "research_provenance": {
            "direction_source": "outcome_free_volume_continuity_and_feasibility_precheck",
            "industry_mapping_available": False,
            "industry_density_tested": False,
            "historical_history_producer_reconstructable": False,
            "absolute_volume_unit_proven_for_all_rows": False,
            "relative_window_factor_research_allowed": True,
            "outcome_free_history_precheck": {
                "train": {
                    "source": 89,
                    "available": 89,
                    "coverage": 1.0,
                    "comparable_candidates": 85,
                    "comparable_symbols": 82,
                    "comparable_signal_days": 12,
                    "low_n": 46,
                    "high_n": 39,
                },
                "val": {
                    "source": 1107,
                    "available": 1107,
                    "coverage": 1.0,
                    "comparable_candidates": 1103,
                    "comparable_symbols": 667,
                    "comparable_signal_days": 96,
                    "low_n": 576,
                    "high_n": 527,
                },
                "test": {
                    "source": 346,
                    "available": 346,
                    "coverage": 1.0,
                    "comparable_candidates": 342,
                    "comparable_symbols": 296,
                    "comparable_signal_days": 32,
                    "low_n": 182,
                    "high_n": 160,
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
            "volume_observations": VOLUME_OBSERVATIONS,
            "volume_requirement": "finite_and_strictly_positive",
            "block_formula": "arithmetic_mean_of_10_volume_observations",
            "block_count": BLOCK_COUNT,
            "block_volume_observations": BLOCK_VOLUME_OBSERVATIONS,
            "block_order": "oldest_to_newest",
            "blocks_overlap": False,
            "cv_std_ddof": 0,
            "mean_volume_floor": None,
            "unit_shift_minimum_symbols_per_day": (
                volume_contract.MIN_SYNCHRONIZED_SYMBOLS
            ),
            "unit_shift_lower_ratio": volume_contract.UNIT_SHIFT_LOWER_RATIO,
            "unit_shift_upper_ratio": volume_contract.UNIT_SHIFT_UPPER_RATIO,
        },
        "safety": {
            "production_database_connected": False,
            "local_database_connected": False,
            "network_fetch_performed": False,
            "history_cache_write_performed": False,
            "amount_factor_used": False,
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
        "volume_data_contract": volume_data_contract,
        "history_inventory": {
            "symbol_count": len(symbols),
            "file_count": len(history_initial),
            "expected_manifest_sha256": PREREGISTERED_HISTORY_MANIFEST_SHA256,
            "history_manifest_sha256": history_manifest_initial,
            "adjustments": ["none", "qfq"],
            "local_files_only": True,
        },
        "current_replay_provenance": {
            "driver": "attribution_audit._replay_split",
            "driver_profile": "production_risk",
            "driver_sha256": static_initial["attribution_audit"]["sha256"],
            "simulator": "backtest_winrate.simulate_single_trade",
            "simulator_sha256": static_initial["backtest_winrate"]["sha256"],
            "outcomes_generated_in_process": True,
            "source_legacy_outcomes_stripped_before_replay": True,
            "source_outcomes_used_for_diagnostic": False,
            "external_replay_artifact_consumed": False,
        },
        "seed": args.seed,
        "bootstrap_reps": args.bootstrap_reps,
        "splits": {},
    }

    history_cache: dict[str, pd.DataFrame] = {}
    features_by_split: dict[str, dict[str, dict[str, Any]]] = {}
    factor_meta_by_split: dict[str, dict[str, Any]] = {}
    for split in SPLITS:
        features, factor_meta = _factor_features_for_sources(
            sources_by_split[split],
            history_cache,
            history_initial,
        )
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
        "static_manifest_sha256": _snapshot_manifest(static_pre_replay),
        "history_manifest_sha256": _history_manifest(history_pre_replay),
        "changed_paths": [],
    }

    enriched_by_split: dict[str, list[dict[str, Any]]] = {}
    for split in SPLITS:
        sources = sources_by_split[split]
        replay_sources = [
            _replay_input_projection(source) for source in sources
        ]
        replay_rows, replay_skips, raw_source_match = _production_replay_split(
            replay_sources,
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
            regime = str(row.get("regime", "")).strip().lower()
            row["normalized_regime"] = regime if regime in VALID_REGIMES else "unknown"
            enriched.append(row)
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
        "candidate_volume_stability_risk_support_requires_"
        "portfolio_preregistration"
        if result["candidate_gate"]["pass"]
        else "candidate_layer_rejected_or_insufficient_sample"
    )
    result["production_decision"] = "unchanged_P0"
    _write_outputs_atomically(
        output_dir,
        staging_dir,
        enriched_by_split,
        result,
        static_initial,
        history_initial,
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
                "pre_publish_inputs_unchanged": result[
                    "pre_publish_input_stability"
                ]["all_unchanged"],
                "production_decision": result["production_decision"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
