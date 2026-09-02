"""Research-only audit of MACD histogram-area divergence at P0 entry time.

The primary, pre-declared variant is ``require_bullish_divergence``:

- compare the latest two *completed* negative MACD histogram cycles;
- require price to make a lower low while absolute negative area contracts;
- keep all production entry/exit/risk semantics fixed;
- run the funded portfolio comparison only after the candidate-layer gate passes.

An alternate ``exclude_bearish_divergence`` variant is available, but it must
be run as a separate experiment.  The script only accepts the already-viewed
train/val/test development splits; it never consumes the near holdout.
"""
from __future__ import annotations

import argparse
import copy
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

from attribution_audit import FIELDS, _config_snapshot, group_stats, stats
from backtest_winrate import HISTORY_DIR, prepare_closed_bars, run_portfolio
from holdout_integrity import file_sha256, sha256_text
from macd_near_regime_audit import (
    _portfolio_config,
    _replay_split,
)
from strategy.macd import calculate_macd
from utils.helpers import load_config


VERSION = "macd_divergence_audit.v1"
SPLITS = ("train", "val", "test")
VARIANTS = ("require_bullish_divergence", "exclude_bearish_divergence")

MIN_ASSIGNMENT_COVERAGE = 0.90
MIN_FLAGGED_CANDIDATES = 30
MIN_FLAGGED_SYMBOLS = 10
MIN_FLAGGED_SIGNAL_DAYS = 10
MIN_DIRECTION_SPLITS = 2
DRAWDOWN_TOLERANCE_PP = 5.0


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
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


def _candidate_id(row: dict[str, Any]) -> str:
    return (
        f"{str(row.get('symbol', '')).zfill(6)}|{row.get('signal_day')}|"
        f"{row.get('signal_type', '')}"
    )


def _completed_cycles(
    frame: pd.DataFrame,
    signal_index: int,
    *,
    sign: int,
) -> list[dict[str, Any]]:
    """Return completed positive/negative histogram cycles without lookahead."""
    if signal_index < 0 or frame.empty:
        return []
    limited = frame.iloc[: signal_index + 1].reset_index(drop=True)
    histogram = pd.to_numeric(limited["hist"], errors="coerce")
    price_column = "low" if sign < 0 else "high"
    if price_column not in limited.columns:
        price_column = "close"
    prices = pd.to_numeric(limited[price_column], errors="coerce")
    cycles: list[dict[str, Any]] = []
    start: int | None = None
    for index, value in enumerate(histogram):
        active = pd.notna(value) and (float(value) < 0 if sign < 0 else float(value) > 0)
        if active and start is None:
            start = index
        if not active and start is not None:
            end = index - 1
            segment_hist = histogram.iloc[start : end + 1]
            segment_prices = prices.iloc[start : end + 1].dropna()
            if not segment_prices.empty:
                cycles.append(
                    {
                        "start": start,
                        "end": end,
                        "area": float(segment_hist.abs().sum()),
                        "extreme": (
                            float(segment_prices.min())
                            if sign < 0
                            else float(segment_prices.max())
                        ),
                    }
                )
            start = None
    # A cycle still active on the signal bar is incomplete.  Using it would
    # mechanically favor recent small areas and introduce partial-cycle bias.
    return [cycle for cycle in cycles if cycle["end"] < signal_index]


def _area_divergence_features(
    frame: pd.DataFrame,
    signal_index: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "bottom_comparable": False,
        "bottom_area_ratio": None,
        "bottom_area_contraction": None,
        "bottom_price_new_low_pct": None,
        "bottom_confirmation_wait_bars": None,
        "bullish_divergence": False,
        "top_comparable": False,
        "top_area_ratio": None,
        "top_area_contraction": None,
        "top_price_new_high_pct": None,
        "top_confirmation_wait_bars": None,
        "bearish_divergence": False,
    }
    negative = _completed_cycles(frame, signal_index, sign=-1)
    if len(negative) >= 2:
        prior, latest = negative[-2], negative[-1]
        if prior["area"] > 0 and prior["extreme"] != 0:
            ratio = latest["area"] / prior["area"]
            price_change = (latest["extreme"] / prior["extreme"] - 1.0) * 100.0
            result.update(
                {
                    "bottom_comparable": True,
                    "bottom_area_ratio": ratio,
                    "bottom_area_contraction": 1.0 - ratio,
                    "bottom_price_new_low_pct": price_change,
                    "bottom_confirmation_wait_bars": signal_index - latest["end"],
                    "bullish_divergence": bool(price_change < 0 and ratio < 1.0),
                }
            )

    positive = _completed_cycles(frame, signal_index, sign=1)
    if len(positive) >= 2:
        prior, latest = positive[-2], positive[-1]
        if prior["area"] > 0 and prior["extreme"] != 0:
            ratio = latest["area"] / prior["area"]
            price_change = (latest["extreme"] / prior["extreme"] - 1.0) * 100.0
            result.update(
                {
                    "top_comparable": True,
                    "top_area_ratio": ratio,
                    "top_area_contraction": 1.0 - ratio,
                    "top_price_new_high_pct": price_change,
                    "top_confirmation_wait_bars": signal_index - latest["end"],
                    "bearish_divergence": bool(price_change > 0 and ratio < 1.0),
                }
            )
    return result


def _variant_included(row: dict[str, Any], variant: str) -> bool:
    if variant == "require_bullish_divergence":
        return bool(row.get("bullish_divergence"))
    if variant == "exclude_bearish_divergence":
        return not bool(row.get("bearish_divergence"))
    raise ValueError(f"unknown divergence variant: {variant}")


def _apply_variant(
    rows: list[dict[str, Any]],
    variant: str,
) -> list[dict[str, Any]]:
    return [row for row in rows if _variant_included(row, variant)]


def _daily_rank_ic(
    rows: list[dict[str, Any]],
    *,
    factor: str,
    outcome: str,
    ascending_good: bool,
) -> dict[str, Any]:
    values: list[float] = []
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(str(row.get("signal_day", "")), []).append(row)
    for day_rows in by_day.values():
        frame = pd.DataFrame(day_rows)
        if factor not in frame.columns or outcome not in frame.columns:
            continue
        sample = frame[[factor, outcome]].apply(pd.to_numeric, errors="coerce").dropna()
        if len(sample) < 3 or sample[factor].nunique() < 2 or sample[outcome].nunique() < 2:
            continue
        oriented = sample[factor] if ascending_good else -sample[factor]
        correlation = oriented.rank(method="average").corr(
            sample[outcome].rank(method="average"), method="pearson"
        )
        if pd.notna(correlation):
            values.append(float(correlation))
    return {
        "days": len(values),
        "mean": float(np.mean(values)) if values else None,
        "median": float(np.median(values)) if values else None,
        "positive_day_pct": (
            float(sum(value > 0 for value in values) / len(values) * 100.0)
            if values
            else None
        ),
    }


def _cluster_bootstrap_delta(
    rows: list[dict[str, Any]],
    value_key: str,
    *,
    reps: int,
    seed: int,
) -> dict[str, Any]:
    valid = [
        row
        for row in rows
        if row.get(value_key) is not None and str(row.get("symbol", "")).strip()
    ]
    included = [float(row[value_key]) for row in valid if row.get("variant_included")]
    excluded = [float(row[value_key]) for row in valid if not row.get("variant_included")]
    observed = (
        float(np.mean(included) - np.mean(excluded))
        if included and excluded
        else None
    )
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
        }
    rng = np.random.default_rng(seed)
    samples: list[float] = []
    for _ in range(reps):
        selected = rng.choice(names, size=len(names), replace=True)
        sampled_rows = [row for name in selected for row in clusters[str(name)]]
        selected_included = [
            float(row[value_key]) for row in sampled_rows if row.get("variant_included")
        ]
        selected_excluded = [
            float(row[value_key]) for row in sampled_rows if not row.get("variant_included")
        ]
        if selected_included and selected_excluded:
            samples.append(float(np.mean(selected_included) - np.mean(selected_excluded)))
    return {
        "mean_delta": observed,
        "ci95_low": float(np.percentile(samples, 2.5)) if samples else None,
        "ci95_high": float(np.percentile(samples, 97.5)) if samples else None,
        "reps_requested": reps,
        "reps_valid": len(samples),
        "cluster_count": len(names),
    }


def _quantile_report(
    rows: list[dict[str, Any]],
    *,
    factor: str,
    outcome: str,
    smaller_is_better: bool,
) -> dict[str, Any]:
    frame = pd.DataFrame(rows)
    if factor not in frame.columns or outcome not in frame.columns:
        return {"n": 0, "groups": {}, "best_minus_worst_pp": None}
    sample = frame[[factor, outcome]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(sample) < 10 or sample[factor].nunique() < 2:
        return {"n": len(sample), "groups": {}, "best_minus_worst_pp": None}
    bins = min(5, int(sample[factor].nunique()))
    sample = sample.copy()
    sample["quantile"] = pd.qcut(
        sample[factor].rank(method="first"), bins, labels=False
    )
    groups = {
        f"Q{int(group) + 1}": {
            "n": len(part),
            "factor_mean": float(part[factor].mean()),
            "outcome_mean": float(part[outcome].mean()),
        }
        for group, part in sample.groupby("quantile")
    }
    best = "Q1" if smaller_is_better else f"Q{bins}"
    worst = f"Q{bins}" if smaller_is_better else "Q1"
    spread = (
        groups[best]["outcome_mean"] - groups[worst]["outcome_mean"]
        if best in groups and worst in groups
        else None
    )
    return {"n": len(sample), "groups": groups, "best_minus_worst_pp": spread}


def _macd_parameters(config: dict[str, Any]) -> dict[str, int]:
    raw = config.get("signal_strategy", {}).get("macd", {}) or {}
    return {
        "fast": int(raw.get("fast", 12)),
        "slow": int(raw.get("slow", 26)),
        "signal": int(raw.get("signal", 9)),
    }


def _history_with_macd(
    symbol: str,
    config: dict[str, Any],
    cache: dict[str, pd.DataFrame | None],
    history_hashes: dict[str, str],
) -> pd.DataFrame | None:
    if symbol in cache:
        return cache[symbol]
    path = HISTORY_DIR / f"{symbol}_qfq.pkl"
    if not path.exists():
        cache[symbol] = None
        return None
    history_hashes[symbol] = file_sha256(path)
    frame = prepare_closed_bars(pd.read_pickle(path))
    if frame.empty:
        cache[symbol] = None
        return None
    params = _macd_parameters(config)
    macd = calculate_macd(frame["close"], **params)
    enriched = frame.copy()
    for column in ("dif", "dea", "hist"):
        enriched[column] = macd[column]
    cache[symbol] = enriched
    return enriched


def _factor_features_for_sources(
    sources: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache: dict[str, pd.DataFrame | None] = {}
    history_hashes: dict[str, str] = {}
    features: dict[str, dict[str, Any]] = {}
    errors: Counter = Counter()
    for source in sources:
        candidate_id = str(source.get("candidate_id") or _candidate_id(source))
        if candidate_id in features:
            raise RuntimeError(f"duplicate candidate id: {candidate_id}")
        symbol = str(source.get("symbol", "")).zfill(6)
        frame = _history_with_macd(symbol, config, cache, history_hashes)
        base = {
            "candidate_id": candidate_id,
            "factor_assignment_available": False,
            "factor_error": None,
            "bullish_divergence": False,
            "bearish_divergence": False,
        }
        if frame is None:
            base["factor_error"] = "missing_history"
            errors["missing_history"] += 1
            features[candidate_id] = base
            continue
        signal_day = date.fromisoformat(str(source["signal_day"]))
        day_values = pd.to_datetime(frame["datetime"]).dt.date
        matches = frame.index[day_values == signal_day].tolist()
        if not matches:
            base["factor_error"] = "missing_signal_bar"
            errors["missing_signal_bar"] += 1
            features[candidate_id] = base
            continue
        factor = _area_divergence_features(frame, int(matches[-1]))
        base.update(factor)
        base["factor_assignment_available"] = True
        features[candidate_id] = base
    history_manifest = sha256_text(
        "\n".join(
            f"{symbol}|{history_hashes[symbol]}" for symbol in sorted(history_hashes)
        )
    )
    return features, {
        "history_manifest_sha256": history_manifest,
        "history_symbol_count": len(history_hashes),
        "factor_errors": dict(errors),
        "macd_parameters": _macd_parameters(config),
    }


def _flagged_rows(rows: list[dict[str, Any]], variant: str) -> list[dict[str, Any]]:
    if variant == "require_bullish_divergence":
        return [row for row in rows if row.get("bullish_divergence")]
    return [row for row in rows if row.get("bearish_divergence")]


def _variant_factor_spec(variant: str) -> dict[str, Any]:
    """Return the comparable-cycle factor orientation for one experiment."""
    if variant == "require_bullish_divergence":
        return {
            "comparable_key": "bottom_comparable",
            "factor": "bottom_area_ratio",
            "smaller_is_better": True,
        }
    if variant == "exclude_bearish_divergence":
        return {
            "comparable_key": "top_comparable",
            "factor": "top_area_ratio",
            # A smaller positive-cycle area ratio is the bearish condition;
            # for this exclusion experiment, larger ratios are oriented good.
            "smaller_is_better": False,
        }
    raise ValueError(f"unknown divergence variant: {variant}")


def _candidate_report(
    rows: list[dict[str, Any]],
    *,
    variant: str,
    source_match: dict[str, Any],
    factor_meta: dict[str, Any],
    bootstrap_reps: int,
    seed: int,
) -> dict[str, Any]:
    retained = [row for row in rows if row["variant_included"]]
    removed = [row for row in rows if not row["variant_included"]]
    flagged = _flagged_rows(rows, variant)
    assigned = [row for row in rows if row.get("factor_assignment_available")]
    bottom_comparable = [row for row in rows if row.get("bottom_comparable")]
    top_comparable = [row for row in rows if row.get("top_comparable")]
    factor_spec = _variant_factor_spec(variant)
    factor_rows = [row for row in rows if row.get(factor_spec["comparable_key"])]
    flagged_ids = {str(row["candidate_id"]) for row in flagged}
    flagged_symbols = {str(row["symbol"]) for row in flagged}
    flagged_days = {str(row["signal_day"]) for row in flagged}
    future_bootstrap = _cluster_bootstrap_delta(
        rows, "future_40d", reps=bootstrap_reps, seed=seed
    )
    trade_bootstrap = _cluster_bootstrap_delta(
        rows, "trade_pnl_pct", reps=bootstrap_reps, seed=seed + 1
    )
    assignment_coverage = len(assigned) / len(rows) if rows else 0.0
    sample_sufficient = (
        len(flagged_ids) >= MIN_FLAGGED_CANDIDATES
        and len(flagged_symbols) >= MIN_FLAGGED_SYMBOLS
        and len(flagged_days) >= MIN_FLAGGED_SIGNAL_DAYS
    )
    direction_positive = bool(
        future_bootstrap["mean_delta"] is not None
        and future_bootstrap["mean_delta"] > 0
        and trade_bootstrap["mean_delta"] is not None
        and trade_bootstrap["mean_delta"] > 0
    )
    bootstrap_supported = bool(
        future_bootstrap["ci95_low"] is not None
        and future_bootstrap["ci95_low"] > 0
        and trade_bootstrap["ci95_low"] is not None
        and trade_bootstrap["ci95_low"] > 0
    )
    return {
        "n": len(rows),
        "assignment_coverage": assignment_coverage,
        "bottom_comparable_coverage": len(bottom_comparable) / len(rows) if rows else 0.0,
        "top_comparable_coverage": len(top_comparable) / len(rows) if rows else 0.0,
        "bullish_divergence_count": sum(bool(row.get("bullish_divergence")) for row in rows),
        "bearish_divergence_count": sum(bool(row.get("bearish_divergence")) for row in rows),
        "baseline": {field: stats([row.get(field) for row in rows]) for field in FIELDS},
        "retained": {
            "n": len(retained),
            "stats": {field: stats([row.get(field) for row in retained]) for field in FIELDS},
        },
        "removed": {
            "n": len(removed),
            "stats": {field: stats([row.get(field) for row in removed]) for field in FIELDS},
        },
        "by_bullish_divergence": group_stats(rows, "bullish_divergence"),
        "by_bearish_divergence": group_stats(rows, "bearish_divergence"),
        "factor_analysis": factor_spec,
        "rank_ic": {
            outcome: _daily_rank_ic(
                factor_rows,
                factor=factor_spec["factor"],
                outcome=outcome,
                ascending_good=not factor_spec["smaller_is_better"],
            )
            for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
        },
        "quantiles": {
            outcome: _quantile_report(
                factor_rows,
                factor=factor_spec["factor"],
                outcome=outcome,
                smaller_is_better=factor_spec["smaller_is_better"],
            )
            for outcome in ("future_20d", "future_40d", "trade_pnl_pct")
        },
        "cluster_bootstrap": {
            "future_40d_retained_minus_removed": future_bootstrap,
            "trade_pnl_retained_minus_removed": trade_bootstrap,
        },
        "sample_gate": {
            "flagged_unique_candidates": len(flagged_ids),
            "flagged_unique_symbols": len(flagged_symbols),
            "flagged_unique_signal_days": len(flagged_days),
            "minimum_candidates": MIN_FLAGGED_CANDIDATES,
            "minimum_symbols": MIN_FLAGGED_SYMBOLS,
            "minimum_signal_days": MIN_FLAGGED_SIGNAL_DAYS,
            "sufficient": sample_sufficient,
        },
        "gate": {
            "assignment_coverage_ok": assignment_coverage >= MIN_ASSIGNMENT_COVERAGE,
            "sample_sufficient": sample_sufficient,
            "direction_positive": direction_positive,
            "cluster_bootstrap_ci95_positive": bootstrap_supported,
            "eligible_for_cross_split": bool(
                assignment_coverage >= MIN_ASSIGNMENT_COVERAGE and sample_sufficient
            ),
        },
        "source_match": source_match,
        "factor_meta": factor_meta,
    }


def _portfolio_summary(
    trades: list[dict[str, Any]],
    config: dict[str, Any],
    seed: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    costs, portfolio_config = _portfolio_config(config, seed)
    result = run_portfolio(copy.deepcopy(trades), costs, portfolio_config)
    return (
        {
            "config": portfolio_config,
            "summary": result["summary"],
            "rejection_reasons": result["rejection_reasons"],
        },
        result["trades"],
        result["rejections"],
    )


def _cross_split_candidate_gate(split_reports: dict[str, Any]) -> dict[str, Any]:
    eligible = [
        split
        for split, report in split_reports.items()
        if report["candidate_report"]["gate"]["eligible_for_cross_split"]
    ]
    directions = {
        split: {
            "future_40d": report["candidate_report"]["cluster_bootstrap"]
            ["future_40d_retained_minus_removed"]["mean_delta"],
            "trade_pnl_pct": report["candidate_report"]["cluster_bootstrap"]
            ["trade_pnl_retained_minus_removed"]["mean_delta"],
        }
        for split, report in split_reports.items()
    }
    direction_consistent = bool(
        len(eligible) >= MIN_DIRECTION_SPLITS
        and all(
            directions[split]["future_40d"] is not None
            and directions[split]["future_40d"] > 0
            and directions[split]["trade_pnl_pct"] is not None
            and directions[split]["trade_pnl_pct"] > 0
            for split in eligible
        )
    )
    bootstrap_supported = bool(
        direction_consistent
        and all(
            split_reports[split]["candidate_report"]["gate"]
            ["cluster_bootstrap_ci95_positive"]
            for split in eligible
        )
    )
    return {
        "eligible_splits": eligible,
        "minimum_direction_splits": MIN_DIRECTION_SPLITS,
        "directions": directions,
        "direction_consistent": direction_consistent,
        "cluster_bootstrap_supported": bootstrap_supported,
        "pass": bool(direction_consistent and bootstrap_supported),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    splits = tuple(args.splits)
    invalid = sorted(set(splits) - set(SPLITS))
    if invalid:
        raise RuntimeError(
            f"unregistered split(s) {invalid}; this development audit cannot use holdout"
        )
    if args.variant not in VARIANTS:
        raise RuntimeError(f"unsupported variant: {args.variant}")
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config).expanduser().resolve()
    config_hash_before = file_sha256(config_path)
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")

    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "variant": args.variant,
        "design": {
            "candidate_layer_first": True,
            "portfolio_requires_candidate_gate": True,
            "completed_macd_cycles_only": True,
            "no_lookahead": True,
            "development_splits_only": list(SPLITS),
            "holdout_consumed": False,
            "production_eligible": False,
        },
        "thresholds": {
            "assignment_coverage": MIN_ASSIGNMENT_COVERAGE,
            "flagged_candidates": MIN_FLAGGED_CANDIDATES,
            "flagged_symbols": MIN_FLAGGED_SYMBOLS,
            "flagged_signal_days": MIN_FLAGGED_SIGNAL_DAYS,
            "direction_splits": MIN_DIRECTION_SPLITS,
            "drawdown_tolerance_pp": DRAWDOWN_TOLERANCE_PP,
        },
        "config_file": str(config_path),
        "config_file_sha256_before": config_hash_before,
        "config_snapshot": _config_snapshot(config),
        "script_sha256": file_sha256(Path(__file__).resolve()),
        "seed": args.seed,
        "bootstrap_reps": args.bootstrap_reps,
        "splits": {},
    }

    replay_rows_by_split: dict[str, list[dict[str, Any]]] = {}
    for split_index, split in enumerate(splits):
        source_path = input_dir / f"candidates_{split}.jsonl"
        sources = _load_jsonl(source_path)
        source_hash = file_sha256(source_path)
        feature_map, factor_meta = _factor_features_for_sources(sources, config)
        replay_rows, replay_skips, source_match = _replay_split(source_path, config)
        enriched: list[dict[str, Any]] = []
        for replay in replay_rows:
            candidate_id = str(replay.get("candidate_id") or _candidate_id(replay))
            row = dict(replay)
            row.update(feature_map.get(candidate_id, {}))
            row["variant_included"] = _variant_included(row, args.variant)
            enriched.append(row)
        replay_rows_by_split[split] = enriched
        enriched_path = output_dir / f"macd_divergence_{split}.jsonl"
        _write_jsonl(enriched_path, enriched)
        report = _candidate_report(
            enriched,
            variant=args.variant,
            source_match=source_match,
            factor_meta=factor_meta,
            bootstrap_reps=args.bootstrap_reps,
            seed=args.seed + split_index * 100,
        )
        result["splits"][split] = {
            "source": str(source_path),
            "source_sha256": source_hash,
            "enriched": str(enriched_path),
            "enriched_sha256": file_sha256(enriched_path),
            "replay_skips": dict(replay_skips),
            "candidate_report": report,
        }

    candidate_gate = _cross_split_candidate_gate(result["splits"])
    result["candidate_gate"] = candidate_gate
    baseline_replay_complete = all(
        result["splits"][split]["candidate_report"]["source_match"]
        ["baseline_replay_complete"]
        for split in splits
    )
    result["baseline_replay_complete"] = baseline_replay_complete

    if args.run_portfolio and candidate_gate["pass"] and baseline_replay_complete:
        portfolio_direction: dict[str, Any] = {}
        for split_index, split in enumerate(splits):
            rows = replay_rows_by_split[split]
            retained = _apply_variant(rows, args.variant)
            baseline, baseline_trades, baseline_rejections = _portfolio_summary(
                rows, config, args.seed + split_index
            )
            variant, variant_trades, variant_rejections = _portfolio_summary(
                retained, config, args.seed + split_index
            )
            baseline_path = output_dir / f"portfolio_{split}_baseline.jsonl"
            variant_path = output_dir / f"portfolio_{split}_variant.jsonl"
            _write_jsonl(baseline_path, baseline_trades)
            _write_jsonl(variant_path, variant_trades)
            baseline_return = baseline["summary"].get("total_return_pct")
            variant_return = variant["summary"].get("total_return_pct")
            baseline_drawdown = baseline["summary"].get("max_drawdown_pct")
            variant_drawdown = variant["summary"].get("max_drawdown_pct")
            portfolio_direction[split] = {
                "return_delta_pp": (
                    float(variant_return) - float(baseline_return)
                    if baseline_return is not None and variant_return is not None
                    else None
                ),
                "drawdown_delta_pp": (
                    float(variant_drawdown) - float(baseline_drawdown)
                    if baseline_drawdown is not None and variant_drawdown is not None
                    else None
                ),
            }
            result["splits"][split]["portfolio"] = {
                "baseline": baseline,
                "variant": variant,
                "baseline_trades": str(baseline_path),
                "variant_trades": str(variant_path),
                "baseline_rejections": len(baseline_rejections),
                "variant_rejections": len(variant_rejections),
            }
        eligible = candidate_gate["eligible_splits"]
        portfolio_pass = bool(
            eligible
            and all(
                portfolio_direction[split]["return_delta_pp"] is not None
                and portfolio_direction[split]["return_delta_pp"] > 0
                and portfolio_direction[split]["drawdown_delta_pp"] is not None
                and portfolio_direction[split]["drawdown_delta_pp"]
                <= DRAWDOWN_TOLERANCE_PP
                for split in eligible
            )
        )
        result["portfolio_gate"] = {
            "status": "run",
            "by_split": portfolio_direction,
            "pass": portfolio_pass,
        }
    else:
        reason = (
            "not_requested"
            if not args.run_portfolio
            else "candidate_gate_failed_or_baseline_incomplete"
        )
        result["portfolio_gate"] = {"status": "skipped", "reason": reason, "pass": False}

    config_hash_after = file_sha256(config_path)
    result["config_file_sha256_after"] = config_hash_after
    result["config_file_unchanged"] = config_hash_before == config_hash_after
    if not baseline_replay_complete:
        result["verdict"] = "invalid_baseline_replay_incomplete"
    elif not candidate_gate["pass"]:
        result["verdict"] = "candidate_layer_rejected_or_insufficient_sample"
    elif not args.run_portfolio:
        result["verdict"] = "candidate_layer_historical_support_requires_portfolio_test"
    elif not result["portfolio_gate"]["pass"]:
        result["verdict"] = "portfolio_layer_rejected"
    else:
        result["verdict"] = (
            "historical_exploratory_support_requires_new_preregistered_holdout"
        )
    result["production_decision"] = "unchanged_P0"
    result_path = output_dir / "macd_divergence_audit.json"
    with result_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=r"D:\tmp\candidates")
    parser.add_argument("--output-dir", default=r"D:\tmp\macd_divergence_audit")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--label", default="macd-area-divergence-audit")
    parser.add_argument("--variant", choices=VARIANTS, default=VARIANTS[0])
    parser.add_argument("--splits", nargs="+", default=list(SPLITS))
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--bootstrap-reps", type=int, default=2000)
    parser.add_argument("--run-portfolio", action="store_true")
    args = parser.parse_args()
    result = run(args)
    print(
        json.dumps(
            {
                "version": result["version"],
                "variant": result["variant"],
                "candidate_gate": result["candidate_gate"],
                "portfolio_gate": result["portfolio_gate"],
                "verdict": result["verdict"],
                "config_file_unchanged": result["config_file_unchanged"],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
