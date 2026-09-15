"""Causal pairwise ranker for same-day, same-priority portfolio candidates.

The model never changes the frozen P0 signal-type priority.  It learns only a
tie-break inside an entry-day/priority bucket, using signal-day technical
features whitelisted below.  Validation is train -> val; viewed-test is
train+val -> test and is excluded from the frozen research screen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import (  # noqa: E402
    DEFAULT_SIGNAL_PRIORITY,
    _resolve_execution_config,
    prepare_closed_bars,
    run_portfolio,
)
from strategy.macd import calculate_macd  # noqa: E402
from utils.helpers import load_config  # noqa: E402

VERSION = "portfolio_pairwise_ranking.v1"
SPLITS = ("train", "val", "test")
DEFAULT_CONFIG = BASE_DIR / "config" / "config.yaml"
HISTORY_DIR = BASE_DIR / "cache" / "daily_history"
RAW_FEATURE_NAMES = (
    "confirmation_count",
    "dif_dea_gap",
    "zero_dist",
    "ma60_dist",
    "ma60_slope",
    "ma250_dist",
    "ma250_slope",
    "atr_ratio",
    "recent_return",
)
MODEL_FEATURE_NAMES = tuple(
    [f"rank_{name}" for name in RAW_FEATURE_NAMES]
    + [f"missing_{name}" for name in RAW_FEATURE_NAMES]
)
FORBIDDEN_OUTCOME_FIELDS = {
    "exit_day",
    "exit_reason",
    "exit_price",
    "pnl_pct",
    "trade_pnl_pct",
    "mfe",
    "mae",
    "mfe_common_60",
    "mae_common_60",
    "future_5d",
    "future_20d",
    "future_40d",
    "post_exit_5d",
    "post_exit_20d",
}
MODEL_L2 = 1.0
MODEL_MAX_ITERATIONS = 100
MODEL_TOLERANCE = 1e-10
MIN_WALK_FORWARD_PRIOR_DAYS = 5
PRIORITY_SCORE_GAP = 1000.0
MODEL_SCORE_CLIP = 100.0
PORTFOLIO_CONFIG = {
    "initial_cash": 100000.0,
    "max_positions": 4,
    "position_size_pct": 0.25,
    "signal_priority": list(DEFAULT_SIGNAL_PRIORITY),
    "score_mode": "external_causal_score",
    "tie_break": "hash",
    "seed": 20260830,
}


def _technical_features_at_signal_day(
    history: pd.DataFrame,
    signal_day: str,
) -> tuple[dict[str, float | None], dict[str, float | None]] | None:
    """Calculate the frozen technical whitelist using bars no later than signal_day."""
    causal = history[history["datetime"] <= pd.Timestamp(signal_day)].copy()
    if causal.empty or str(causal.iloc[-1]["datetime"].date()) != signal_day:
        return None
    close = causal["close"].astype(float)
    high = causal["high"].astype(float)
    low = causal["low"].astype(float)
    index = len(causal) - 1
    current = float(close.iloc[index])
    macd = calculate_macd(close, fast=12, slow=26, signal=9)
    dif = _finite(macd["dif"].iloc[index])
    dea = _finite(macd["dea"].iloc[index])
    p5a = {
        "dif_dea_gap": (
            abs(float(dif) - float(dea)) / current
            if current > 0 and dif is not None and dea is not None
            else None
        ),
        "zero_dist": abs(float(dif)) / current if current > 0 and dif is not None else None,
    }
    ma60 = float(close.iloc[index - 59 : index + 1].mean()) if index >= 59 else None
    ma250 = float(close.iloc[index - 249 : index + 1].mean()) if index >= 249 else None
    ma60_previous = (
        float(close.iloc[max(0, index - 64) : index - 3].mean())
        if index >= 64
        else None
    )
    ma250_previous = (
        float(close.iloc[max(0, index - 254) : index - 3].mean())
        if index >= 254
        else None
    )
    true_ranges = []
    for offset in range(max(1, index - 13), index + 1):
        day_high = float(high.iloc[offset])
        day_low = float(low.iloc[offset])
        previous_close = float(close.iloc[offset - 1])
        true_ranges.append(
            max(
                day_high - day_low,
                abs(day_high - previous_close),
                abs(day_low - previous_close),
            )
        )
    atr = float(sum(true_ranges) / len(true_ranges)) if true_ranges else None
    previous_20 = float(close.iloc[index - 20]) if index >= 20 else None
    p5b = {
        "ma60_dist": current / ma60 - 1.0 if ma60 not in {None, 0.0} else None,
        "ma60_slope": (
            ma60 / ma60_previous - 1.0
            if ma60 not in {None, 0.0} and ma60_previous not in {None, 0.0}
            else None
        ),
        "ma250_dist": current / ma250 - 1.0 if ma250 not in {None, 0.0} else None,
        "ma250_slope": (
            ma250 / ma250_previous - 1.0
            if ma250 not in {None, 0.0} and ma250_previous not in {None, 0.0}
            else None
        ),
        "atr_ratio": atr / current if atr is not None and current > 0 else None,
        "recent_return": (
            current / previous_20 - 1.0 if previous_20 not in {None, 0.0} else None
        ),
    }
    return p5a, p5b


def hydrate_causal_ranking_features(
    rows_by_split: dict[str, list[dict[str, Any]]],
    history_dir: Path = HISTORY_DIR,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Attach signal-day features from local QFQ history and record input hashes."""
    history_cache: dict[str, pd.DataFrame | None] = {}
    history_hashes: dict[str, str] = {}
    hydrated: dict[str, list[dict[str, Any]]] = {}
    split_audit: dict[str, Any] = {}
    for split, rows in rows_by_split.items():
        result: list[dict[str, Any]] = []
        missing_history = 0
        missing_signal_day = 0
        hydrated_rows = 0
        for row in rows:
            symbol = str(row["symbol"])
            if symbol not in history_cache:
                path = history_dir / f"{symbol}_qfq.pkl"
                if not path.exists():
                    history_cache[symbol] = None
                else:
                    history_cache[symbol] = prepare_closed_bars(pd.read_pickle(path))
                    history_hashes[symbol] = _sha256_file(path)
            history = history_cache[symbol]
            copy = dict(row)
            if history is None or history.empty:
                missing_history += 1
                copy["_p5a_features"] = None
                copy["_p5b_features"] = None
            else:
                features = _technical_features_at_signal_day(
                    history, str(row["signal_day"])
                )
                if features is None:
                    missing_signal_day += 1
                    copy["_p5a_features"] = None
                    copy["_p5b_features"] = None
                else:
                    copy["_p5a_features"], copy["_p5b_features"] = features
                    hydrated_rows += 1
            result.append(copy)
        hydrated[split] = result
        split_audit[split] = {
            "rows": len(rows),
            "hydrated_rows": hydrated_rows,
            "missing_history": missing_history,
            "signal_day_not_in_history": missing_signal_day,
            "hydrated_coverage_pct": round(
                hydrated_rows / max(len(rows), 1) * 100.0, 2
            ),
        }
    manifest = "\n".join(
        f"{symbol}|{history_hashes[symbol]}" for symbol in sorted(history_hashes)
    )
    return hydrated, {
        "history_dir": str(history_dir),
        "history_symbols": len(history_hashes),
        "history_manifest_sha256": _sha256_bytes(manifest.encode("utf-8")),
        "splits": split_audit,
        "feature_time_boundary": "history datetime <= signal_day",
        "macd_parameters": {"fast": 12, "slow": 26, "signal": 9},
        "atr_period": 14,
        "ma_periods": [60, 250],
        "slope_lag_bars": 5,
        "recent_return_bars": 20,
    }


def _guard_development_path(path: Path, allow_holdout: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if not allow_holdout and any("holdout" in part.lower() for part in resolved.parts):
        raise ValueError(
            f"Holdout path is blocked for development experiments: {resolved}. "
            "Use only after the experiment is frozen and separately authorized."
        )
    return resolved


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _candidate_manifest(rows: list[dict[str, Any]]) -> str:
    ids = sorted(str(row["candidate_id"]) for row in rows)
    return _sha256_bytes("\n".join(ids).encode("utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            public = {key: value for key, value in row.items() if not key.startswith("_")}
            handle.write(_canonical_json(public) + "\n")
    return {"path": str(path), "rows": len(rows), "sha256": _sha256_file(path)}


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def extract_ranking_raw_features(row: dict[str, Any]) -> dict[str, float | None]:
    """Extract the fixed signal-day whitelist without reading any outcome field."""
    p5a = row.get("_p5a_features") or {}
    p5b = row.get("_p5b_features") or {}
    features = {
        "confirmation_count": _finite(row.get("confirmation_count")),
        "dif_dea_gap": _finite(p5a.get("dif_dea_gap")),
        "zero_dist": _finite(p5a.get("zero_dist")),
        "ma60_dist": _finite(p5b.get("ma60_dist")),
        "ma60_slope": _finite(p5b.get("ma60_slope")),
        "ma250_dist": _finite(p5b.get("ma250_dist")),
        "ma250_slope": _finite(p5b.get("ma250_slope")),
        "atr_ratio": _finite(p5b.get("atr_ratio")),
        "recent_return": _finite(p5b.get("recent_return")),
    }
    if tuple(features) != RAW_FEATURE_NAMES:
        raise AssertionError("ranking feature schema changed")
    if set(features) & FORBIDDEN_OUTCOME_FIELDS:
        raise AssertionError("outcome field entered ranking feature schema")
    return features


def _midrank(value: float, values: list[float]) -> float:
    if len(values) < 2:
        return 0.5
    below = sum(other < value for other in values)
    equal = sum(other == value for other in values)
    return (below + (equal - 1) * 0.5) / (len(values) - 1)


def build_cross_sectional_feature_map(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["entry_day"]), str(row["signal_type"]))].append(row)
    feature_map: dict[str, np.ndarray] = {}
    coverage = {name: 0 for name in RAW_FEATURE_NAMES}
    for group in groups.values():
        raw_by_id = {
            str(row["candidate_id"]): extract_ranking_raw_features(row) for row in group
        }
        valid_by_feature = {
            name: [
                float(raw[name])
                for raw in raw_by_id.values()
                if raw[name] is not None
            ]
            for name in RAW_FEATURE_NAMES
        }
        for row in group:
            candidate_id = str(row["candidate_id"])
            raw = raw_by_id[candidate_id]
            ranks: list[float] = []
            missing: list[float] = []
            for name in RAW_FEATURE_NAMES:
                value = raw[name]
                if value is None:
                    ranks.append(0.0)
                    missing.append(1.0)
                else:
                    ranks.append(_midrank(float(value), valid_by_feature[name]) - 0.5)
                    missing.append(0.0)
                    coverage[name] += 1
            feature_map[candidate_id] = np.asarray(ranks + missing, dtype=float)
    return feature_map, {
        "candidate_count": len(rows),
        "entry_priority_buckets": len(groups),
        "raw_feature_non_missing": coverage,
        "raw_feature_coverage_pct": {
            name: round(count / max(len(rows), 1) * 100.0, 2)
            for name, count in coverage.items()
        },
    }


def _outcome(row: dict[str, Any]) -> float:
    value = _finite(row.get("trade_pnl_pct", row.get("pnl_pct")))
    if value is None:
        raise ValueError(f"missing training outcome for {row.get('candidate_id')}")
    return value


def build_pairwise_training_data(
    rows: list[dict[str, Any]],
    feature_map: dict[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    by_day: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        by_day[str(row["entry_day"])][str(row["signal_type"])].append(row)
    differences: list[np.ndarray] = []
    labels: list[float] = []
    sample_weights: list[float] = []
    used_days = 0
    undirected_pairs = 0
    for day_groups in by_day.values():
        day_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for group in day_groups.values():
            for left_index in range(len(group)):
                for right_index in range(left_index + 1, len(group)):
                    left = group[left_index]
                    right = group[right_index]
                    if abs(_outcome(left) - _outcome(right)) <= 1e-12:
                        continue
                    day_pairs.append((left, right))
        if not day_pairs:
            continue
        used_days += 1
        pair_weight = 1.0 / (2.0 * len(day_pairs))
        undirected_pairs += len(day_pairs)
        for left, right in day_pairs:
            left_id = str(left["candidate_id"])
            right_id = str(right["candidate_id"])
            difference = feature_map[left_id] - feature_map[right_id]
            label = 1.0 if _outcome(left) > _outcome(right) else 0.0
            differences.extend((difference, -difference))
            labels.extend((label, 1.0 - label))
            sample_weights.extend((pair_weight, pair_weight))
    if not differences:
        empty = np.empty((0, len(MODEL_FEATURE_NAMES)), dtype=float)
        return empty, np.empty(0), np.empty(0), {
            "entry_days_with_pairs": 0,
            "undirected_pairs": 0,
            "directed_samples": 0,
        }
    return (
        np.asarray(differences, dtype=float),
        np.asarray(labels, dtype=float),
        np.asarray(sample_weights, dtype=float),
        {
            "entry_days_with_pairs": used_days,
            "undirected_pairs": undirected_pairs,
            "directed_samples": len(differences),
        },
    )


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def fit_pairwise_logistic(
    matrix: np.ndarray,
    labels: np.ndarray,
    sample_weights: np.ndarray,
    *,
    l2: float = MODEL_L2,
    max_iterations: int = MODEL_MAX_ITERATIONS,
    tolerance: float = MODEL_TOLERANCE,
) -> dict[str, Any]:
    if matrix.ndim != 2 or matrix.shape[1] != len(MODEL_FEATURE_NAMES):
        raise ValueError("pairwise matrix has the wrong shape")
    if len(matrix) == 0 or len(matrix) != len(labels) or len(labels) != len(sample_weights):
        raise ValueError("pairwise training data is empty or inconsistent")
    if set(np.unique(labels)) != {0.0, 1.0}:
        raise ValueError("pairwise labels require both classes")
    weights = np.zeros(matrix.shape[1], dtype=float)
    identity = np.eye(matrix.shape[1], dtype=float)
    converged = False
    iteration = 0
    for iteration in range(1, max_iterations + 1):
        probabilities = _sigmoid(matrix @ weights)
        residual = sample_weights * (probabilities - labels)
        gradient = matrix.T @ residual + l2 * weights
        curvature = sample_weights * probabilities * (1.0 - probabilities)
        hessian = (matrix.T * curvature) @ matrix + l2 * identity
        step = np.linalg.solve(hessian, gradient)
        weights -= step
        if float(np.max(np.abs(step))) <= tolerance:
            converged = True
            break
    return {
        "weights": weights,
        "l2": float(l2),
        "iterations": iteration,
        "converged": converged,
        "objective_weight_sum": float(sample_weights.sum()),
    }


def _pairwise_metrics(
    rows: list[dict[str, Any]],
    scores: dict[str, float],
    *,
    eligible_days: set[str] | None = None,
) -> dict[str, Any]:
    by_day: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        day = str(row["entry_day"])
        if eligible_days is not None and day not in eligible_days:
            continue
        by_day[day][str(row["signal_type"])].append(row)
    weighted_correct = 0.0
    total_weight = 0.0
    pairs = 0
    used_days = 0
    for day_groups in by_day.values():
        comparisons: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for group in day_groups.values():
            for left_index in range(len(group)):
                for right_index in range(left_index + 1, len(group)):
                    left = group[left_index]
                    right = group[right_index]
                    if abs(_outcome(left) - _outcome(right)) > 1e-12:
                        comparisons.append((left, right))
        if not comparisons:
            continue
        used_days += 1
        weight = 1.0 / len(comparisons)
        for left, right in comparisons:
            left_score = scores[str(left["candidate_id"])]
            right_score = scores[str(right["candidate_id"])]
            expected = 1 if _outcome(left) > _outcome(right) else -1
            predicted = 1 if left_score > right_score else -1 if left_score < right_score else 0
            correct = 1.0 if predicted == expected else 0.5 if predicted == 0 else 0.0
            weighted_correct += weight * correct
            total_weight += weight
            pairs += 1
    return {
        "entry_days_with_pairs": used_days,
        "undirected_pairs": pairs,
        "day_weighted_pairwise_accuracy": round(
            weighted_correct / total_weight if total_weight else 0.0, 6
        ),
    }


def _model_public(model: dict[str, Any]) -> dict[str, Any]:
    weights = np.asarray(model["weights"], dtype=float)
    return {
        "feature_names": list(MODEL_FEATURE_NAMES),
        "coefficients": {
            name: round(float(value), 8)
            for name, value in zip(MODEL_FEATURE_NAMES, weights, strict=True)
        },
        "l2": model["l2"],
        "iterations": model["iterations"],
        "converged": model["converged"],
        "objective_weight_sum": round(float(model["objective_weight_sum"]), 6),
    }


def _fit_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    feature_map, feature_audit = build_cross_sectional_feature_map(rows)
    matrix, labels, sample_weights, pair_audit = build_pairwise_training_data(
        rows, feature_map
    )
    model = fit_pairwise_logistic(matrix, labels, sample_weights)
    scores = {
        candidate_id: float(features @ model["weights"])
        for candidate_id, features in feature_map.items()
    }
    return model, {
        "features": feature_audit,
        "pairs": pair_audit,
        "in_sample_pairwise": _pairwise_metrics(rows, scores),
    }


def _score_rows(
    rows: list[dict[str, Any]],
    model: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, Any]]:
    feature_map, audit = build_cross_sectional_feature_map(rows)
    rank = {name: index for index, name in enumerate(DEFAULT_SIGNAL_PRIORITY)}
    weights = np.asarray(model["weights"], dtype=float)
    scored: list[dict[str, Any]] = []
    model_scores: dict[str, float] = {}
    for row in rows:
        candidate_id = str(row["candidate_id"])
        model_score = float(feature_map[candidate_id] @ weights)
        clipped = max(-MODEL_SCORE_CLIP, min(MODEL_SCORE_CLIP, model_score))
        priority_rank = rank.get(str(row.get("signal_type", "")), 999)
        copy = dict(row)
        copy["_pairwise_model_score"] = model_score
        copy["_portfolio_rank_score"] = -priority_rank * PRIORITY_SCORE_GAP + clipped
        copy["pairwise_model_score"] = round(model_score, 10)
        copy["portfolio_rank_score"] = round(copy["_portfolio_rank_score"], 10)
        scored.append(copy)
        model_scores[candidate_id] = model_score
    audit["model_score_clip"] = [-MODEL_SCORE_CLIP, MODEL_SCORE_CLIP]
    audit["priority_score_gap"] = PRIORITY_SCORE_GAP
    return scored, model_scores, audit


def _portfolio(rows: list[dict[str, Any]], costs: dict[str, Any], config: dict[str, Any]):
    return run_portfolio([dict(row) for row in rows], costs, dict(config))


def _portfolio_summary(portfolio: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": portfolio["summary"],
        "attribution": portfolio["attribution"],
        "rejection_reasons": portfolio["rejection_reasons"],
    }


def _write_portfolio_artifacts(
    output_dir: Path,
    label: str,
    portfolio: dict[str, Any],
) -> dict[str, Any]:
    return {
        "accepted_entries": _write_jsonl(
            output_dir / f"{label}_accepted_entries.jsonl", portfolio["accepted_entries"]
        ),
        "trades": _write_jsonl(output_dir / f"{label}_trades.jsonl", portfolio["trades"]),
        "rejections": _write_jsonl(
            output_dir / f"{label}_rejections.jsonl", portfolio["rejections"]
        ),
        "equity_curve": _write_jsonl(
            output_dir / f"{label}_equity_curve.jsonl", portfolio["equity_curve"]
        ),
    }


def _write_score_artifact(
    output_dir: Path,
    label: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    score_rows = [
        {
            "candidate_id": row["candidate_id"],
            "symbol": row["symbol"],
            "signal_day": row["signal_day"],
            "entry_day": row["entry_day"],
            "signal_type": row["signal_type"],
            "pairwise_model_score": row["pairwise_model_score"],
            "portfolio_rank_score": row["portfolio_rank_score"],
        }
        for row in rows
    ]
    return _write_jsonl(output_dir / f"{label}_candidate_scores.jsonl", score_rows)


def _percentile(observed: float, values: list[float]) -> float:
    return round(sum(value <= observed for value in values) / len(values) * 100.0, 2)


def _random_reference(
    order_report: dict[str, Any],
    split: str,
    allow_holdout: bool,
) -> tuple[dict[str, Any], list[float]]:
    sweep = order_report["splits"][split]["random_seed_sweep"]
    artifact = sweep["artifacts"]["seed_runs"]
    path = _guard_development_path(Path(artifact["path"]), allow_holdout)
    if _sha256_file(path) != str(artifact["sha256"]):
        raise RuntimeError(f"v3 random seed artifact hash mismatch: {path}")
    rows = _load_jsonl(path)
    returns = [
        float(row["total_return_pct"])
        for row in rows
        if row.get("split") == split and row.get("profile") == "baseline"
    ]
    if len(returns) != int(sweep["seed_count"]):
        raise RuntimeError(f"unexpected v3 baseline seed count for {split}")
    reference = sweep["profile_metric_distributions"]["baseline"]
    return reference, returns


def _evaluate_stage(
    training_rows: list[dict[str, Any]],
    evaluation_rows: list[dict[str, Any]],
    evaluation_split: str,
    costs: dict[str, Any],
    output_dir: Path,
    order_report: dict[str, Any],
    allow_holdout: bool,
) -> dict[str, Any]:
    model, training_audit = _fit_rows(training_rows)
    scored, scores, evaluation_feature_audit = _score_rows(evaluation_rows, model)
    ranked = _portfolio(scored, costs, PORTFOLIO_CONFIG)
    symbol_config = {
        **PORTFOLIO_CONFIG,
        "score_mode": "P0",
        "tie_break": "symbol_asc",
    }
    hash_config = {**symbol_config, "tie_break": "hash"}
    symbol_baseline = _portfolio(evaluation_rows, costs, symbol_config)
    hash_baseline = _portfolio(evaluation_rows, costs, hash_config)
    random_reference, random_returns = _random_reference(
        order_report, evaluation_split, allow_holdout
    )
    ranked_return = float(ranked["summary"]["total_return_pct"])
    ranked_drawdown = float(ranked["summary"]["max_drawdown_pct"])
    return {
        "training": {
            "candidates": len(training_rows),
            "candidate_manifest_sha256": _candidate_manifest(training_rows),
            "entry_days": len({str(row["entry_day"]) for row in training_rows}),
            "model": _model_public(model),
            "audit": training_audit,
        },
        "evaluation": {
            "split": evaluation_split,
            "candidates": len(evaluation_rows),
            "candidate_manifest_sha256": _candidate_manifest(evaluation_rows),
            "feature_audit": evaluation_feature_audit,
            "pairwise_metrics": _pairwise_metrics(evaluation_rows, scores),
        },
        "portfolio_comparison": {
            "pairwise_ranked": _portfolio_summary(ranked),
            "p0_symbol_asc": _portfolio_summary(symbol_baseline),
            "p0_hash": _portfolio_summary(hash_baseline),
            "random_order_reference": random_reference,
            "ranked_return_vs_symbol_asc_pp": round(
                ranked_return - float(symbol_baseline["summary"]["total_return_pct"]), 4
            ),
            "ranked_return_vs_hash_pp": round(
                ranked_return - float(hash_baseline["summary"]["total_return_pct"]), 4
            ),
            "ranked_return_random_order_percentile": _percentile(
                ranked_return, random_returns
            ),
            "ranked_drawdown_vs_random_median_pp": round(
                ranked_drawdown
                - float(random_reference["max_drawdown_pct"]["median"]),
                4,
            ),
        },
        "artifacts": {
            **_write_portfolio_artifacts(
                output_dir, f"{evaluation_split}_pairwise_ranked", ranked
            ),
            "candidate_scores": _write_score_artifact(
                output_dir, evaluation_split, scored
            ),
        },
    }


def _walk_forward_train(
    rows: list[dict[str, Any]],
    costs: dict[str, Any],
    output_dir: Path,
    order_report: dict[str, Any],
    allow_holdout: bool,
) -> dict[str, Any]:
    days = sorted({str(row["entry_day"]) for row in rows})
    scored_rows: list[dict[str, Any]] = []
    score_map: dict[str, float] = {}
    evaluated_days: set[str] = set()
    fit_records: list[dict[str, Any]] = []
    zero_model = {
        "weights": np.zeros(len(MODEL_FEATURE_NAMES), dtype=float),
        "l2": MODEL_L2,
        "iterations": 0,
        "converged": True,
        "objective_weight_sum": 0.0,
    }
    for index, day in enumerate(days):
        current = [row for row in rows if str(row["entry_day"]) == day]
        prior = [row for row in rows if str(row["entry_day"]) < day]
        fitted = False
        model = zero_model
        if index >= MIN_WALK_FORWARD_PRIOR_DAYS:
            try:
                model, audit = _fit_rows(prior)
                fitted = True
                evaluated_days.add(day)
                fit_records.append(
                    {
                        "evaluation_day": day,
                        "latest_training_day": max(str(row["entry_day"]) for row in prior),
                        "training_candidates": len(prior),
                        "training_pairs": audit["pairs"]["undirected_pairs"],
                    }
                )
            except ValueError:
                model = zero_model
        scored, scores, _ = _score_rows(current, model)
        scored_rows.extend(scored)
        score_map.update(scores)
        if not fitted:
            fit_records.append(
                {
                    "evaluation_day": day,
                    "latest_training_day": (
                        max((str(row["entry_day"]) for row in prior), default=None)
                    ),
                    "training_candidates": len(prior),
                    "training_pairs": 0,
                    "fallback_hash_order": True,
                }
            )
    portfolio = _portfolio(scored_rows, costs, PORTFOLIO_CONFIG)
    random_reference, random_returns = _random_reference(order_report, "train", allow_holdout)
    total_return = float(portfolio["summary"]["total_return_pct"])
    return {
        "policy": {
            "minimum_prior_entry_days": MIN_WALK_FORWARD_PRIOR_DAYS,
            "early_day_fallback": "P0 priority plus fixed hash tie-break",
            "future_days_used_for_training": False,
        },
        "entry_days": len(days),
        "model_evaluated_days": len(evaluated_days),
        "fit_records": fit_records,
        "pairwise_metrics_on_model_evaluated_days": _pairwise_metrics(
            rows, score_map, eligible_days=evaluated_days
        ),
        "portfolio": _portfolio_summary(portfolio),
        "portfolio_return_random_order_percentile": _percentile(
            total_return, random_returns
        ),
        "portfolio_return_vs_random_median_pp": round(
            total_return - float(random_reference["total_return_pct"]["median"]), 4
        ),
        "artifacts": {
            **_write_portfolio_artifacts(
                output_dir, "train_walk_forward_pairwise_ranked", portfolio
            ),
            "candidate_scores": _write_score_artifact(
                output_dir, "train_walk_forward", scored_rows
            ),
        },
    }


def _research_screen(
    walk_forward: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    train_accuracy = float(
        walk_forward["pairwise_metrics_on_model_evaluated_days"][
            "day_weighted_pairwise_accuracy"
        ]
    )
    validation_accuracy = float(
        validation["evaluation"]["pairwise_metrics"][
            "day_weighted_pairwise_accuracy"
        ]
    )
    comparison = validation["portfolio_comparison"]
    ranked_summary = comparison["pairwise_ranked"]["summary"]
    random_reference = comparison["random_order_reference"]
    checks = {
        "train_walk_forward_model_days_at_least_5": int(
            walk_forward["model_evaluated_days"]
        )
        >= 5,
        "train_walk_forward_pairwise_accuracy_above_random": train_accuracy > 0.5,
        "validation_pairwise_accuracy_above_random": validation_accuracy > 0.5,
        "validation_portfolio_return_above_random_median": float(
            ranked_summary["total_return_pct"]
        )
        > float(random_reference["total_return_pct"]["median"]),
        "validation_portfolio_return_random_percentile_at_least_75": float(
            comparison["ranked_return_random_order_percentile"]
        )
        >= 75.0,
        "validation_max_drawdown_not_above_random_median": float(
            ranked_summary["max_drawdown_pct"]
        )
        <= float(random_reference["max_drawdown_pct"]["median"]),
    }
    return {
        "checks": checks,
        "passes_research_screen": all(checks.values()),
        "viewed_test_used": False,
        "production_eligible": False,
        "production_blocker": (
            "A single train/validation ranking experiment cannot authorize production; "
            "a genuinely untouched holdout and independent review remain required."
        ),
    }


def _validate_unique(rows: list[dict[str, Any]], label: str) -> None:
    ids = [str(row.get("candidate_id", "")) for row in rows]
    if any(not candidate_id for candidate_id in ids):
        raise RuntimeError(f"missing candidate_id in {label}")
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"duplicate candidate_id in {label}")


def build_report(
    exit_root: Path,
    order_report_path: Path,
    output_dir: Path,
    config_path: Path,
    *,
    allow_holdout: bool = False,
) -> dict[str, Any]:
    exit_root = _guard_development_path(exit_root, allow_holdout)
    order_report_path = _guard_development_path(order_report_path, allow_holdout)
    output_dir = _guard_development_path(output_dir, allow_holdout)
    config_path = _guard_development_path(config_path, allow_holdout)
    order_report = json.loads(order_report_path.read_text(encoding="utf-8"))
    source_rows = {
        split: _load_jsonl(exit_root / "baseline" / f"candidates_{split}.jsonl")
        for split in SPLITS
    }
    for split, split_rows in source_rows.items():
        _validate_unique(split_rows, split)
    rows, history_audit = hydrate_causal_ranking_features(source_rows)
    costs = _resolve_execution_config(load_config(config_path))
    walk_forward = _walk_forward_train(
        rows["train"], costs, output_dir, order_report, allow_holdout
    )
    validation = _evaluate_stage(
        rows["train"],
        rows["val"],
        "val",
        costs,
        output_dir,
        order_report,
        allow_holdout,
    )
    viewed_test = _evaluate_stage(
        rows["train"] + rows["val"],
        rows["test"],
        "test",
        costs,
        output_dir,
        order_report,
        allow_holdout,
    )
    report = {
        "version": VERSION,
        "research_question": (
            "Can a fixed causal pairwise model rank candidates inside the same entry-day "
            "and P0-priority bucket better than arbitrary ordering?"
        ),
        "preregistered_design": {
            "model": "day-weighted pairwise logistic regression",
            "l2": MODEL_L2,
            "max_iterations": MODEL_MAX_ITERATIONS,
            "feature_names": list(MODEL_FEATURE_NAMES),
            "feature_source": (
                "local QFQ history recomputed with datetime <= signal_day; source hashes recorded"
            ),
            "priority_policy": "P0 signal-type priority is immutable",
            "equal_score_tie_break": "fixed hash",
            "validation_stage": "train -> val",
            "viewed_test_stage": "train+val -> viewed test",
            "hyperparameter_search": False,
            "viewed_test_used_for_screen": False,
            "holdout_used": bool(allow_holdout),
        },
        "outcome_isolation": {
            "training_label": "within-day/same-priority ordering by trade_pnl_pct",
            "label_used_only_in_training_splits": True,
            "forbidden_feature_fields": sorted(FORBIDDEN_OUTCOME_FIELDS),
            "feature_schema_intersection_with_forbidden_fields": sorted(
                set(MODEL_FEATURE_NAMES) & FORBIDDEN_OUTCOME_FIELDS
            ),
        },
        "input": {
            "exit_root": str(exit_root),
            "order_report": {
                "path": str(order_report_path),
                "sha256": _sha256_file(order_report_path),
            },
            "config": {"path": str(config_path), "sha256": _sha256_file(config_path)},
            "resolved_execution_sha256": _sha256_bytes(
                _canonical_json(costs).encode("utf-8")
            ),
            "candidate_files": {
                split: {
                    "path": str(exit_root / "baseline" / f"candidates_{split}.jsonl"),
                    "sha256": _sha256_file(
                        exit_root / "baseline" / f"candidates_{split}.jsonl"
                    ),
                    "rows": len(source_rows[split]),
                    "candidate_manifest_sha256": _candidate_manifest(source_rows[split]),
                }
                for split in SPLITS
            },
            "qfq_history": history_audit,
            "backtest_engine": {
                "path": str(BASE_DIR / "backtest_winrate.py"),
                "sha256": _sha256_file(BASE_DIR / "backtest_winrate.py"),
            },
            "experiment_code": {
                "path": str(Path(__file__).resolve()),
                "sha256": _sha256_file(Path(__file__).resolve()),
            },
        },
        "portfolio_config": PORTFOLIO_CONFIG,
        "train_walk_forward": walk_forward,
        "validation": validation,
        "viewed_test": viewed_test,
        "screen": _research_screen(walk_forward, validation),
        "production_eligible": False,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fit and evaluate a fixed causal pairwise candidate ranker"
    )
    parser.add_argument("--exit-root", type=Path, required=True)
    parser.add_argument("--order-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()
    report = build_report(
        args.exit_root,
        args.order_report,
        args.output_dir,
        args.config,
        allow_holdout=args.allow_holdout,
    )
    report_path = args.output_dir.expanduser().resolve() / "report.json"
    _write_json(report_path, report)
    print(
        json.dumps(
            {
                "version": VERSION,
                "report": str(report_path),
                "sha256": _sha256_file(report_path),
                "passes_research_screen": report["screen"]["passes_research_screen"],
                "production_eligible": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
