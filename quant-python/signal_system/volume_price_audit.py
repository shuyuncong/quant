"""Candidate-layer audit of signal-day price/volume confirmation.

The pre-declared primary hypothesis is binary: a positive signal-day return
with volume at least equal to the mean of the prior 20 sessions should improve
future and realized trade returns.  The four price/volume quadrants and all
continuous volume, amount, price-location, and turnover-proxy fields are
diagnostics only.  The baseline excludes the signal day, uses closed bars only,
requires canonical development candidates, rejects holdout, and has no
portfolio implementation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from attribution_audit import FIELDS, _config_snapshot, group_stats, stats
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
    _write_jsonl,
)
from macd_near_regime_audit import _replay_split
from utils.helpers import load_config


VERSION = "volume_price_audit.v1"
SPLITS = ("train", "val", "test")
PRIMARY_FACTOR = "up_volume_confirmation"
FACTOR_SPECS: dict[str, dict[str, Any]] = {
    "up_volume_confirmation": {"larger_is_better": True, "role": "primary"},
    "signal_day_return": {"larger_is_better": True, "role": "diagnostic"},
    "volume_ratio_20": {"larger_is_better": True, "role": "diagnostic"},
    "amount_ratio_20": {"larger_is_better": True, "role": "diagnostic"},
    "volume_ratio_3_vs_20": {"larger_is_better": True, "role": "diagnostic"},
    "price_location_20": {"larger_is_better": True, "role": "diagnostic"},
    "turnover_value_proxy": {"larger_is_better": True, "role": "diagnostic"},
    "signed_volume_impulse": {"larger_is_better": True, "role": "diagnostic"},
}
MIN_ASSIGNMENT_COVERAGE = 0.90
MIN_GROUP_CANDIDATES = 30
MIN_GROUP_SYMBOLS = 10
MIN_GROUP_SIGNAL_DAYS = 10
MIN_DIRECTION_SPLITS = 2


def _positive_mean(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    mean = float(values.mean())
    return mean if mean > 0 else None


def _safe_ratio(value: Any, baseline: float | None) -> float | None:
    if baseline is None or pd.isna(value):
        return None
    return float(value) / baseline


def _volume_price_features(
    frame: pd.DataFrame,
    signal_index: int,
    market_cap_100m: Any,
) -> dict[str, Any]:
    """Compute causal signal-day features against prior-session baselines."""
    result: dict[str, Any] = {
        **{factor: None for factor in FACTOR_SPECS},
        "volume_price_quadrant": None,
        "factor_assignment_available": False,
        "factor_error": None,
        "baseline_excludes_signal_day": True,
    }
    required = {"close", "high", "low", "volume"}
    if signal_index < 20 or frame.empty or not required.issubset(frame.columns):
        result["factor_error"] = "insufficient_volume_price_history"
        return result
    limited = frame.iloc[: signal_index + 1]
    close = pd.to_numeric(limited["close"], errors="coerce")
    volume = pd.to_numeric(limited["volume"], errors="coerce")
    current_close = close.iloc[signal_index]
    previous_close = close.iloc[signal_index - 1]
    prior_20 = limited.iloc[signal_index - 20 : signal_index]
    volume_baseline = _positive_mean(prior_20["volume"])
    volume_ratio = _safe_ratio(volume.iloc[signal_index], volume_baseline)
    if (
        pd.isna(current_close)
        or pd.isna(previous_close)
        or float(previous_close) == 0
        or volume_ratio is None
    ):
        result["factor_error"] = "invalid_volume_price_values"
        return result
    signal_return = float(current_close) / float(previous_close) - 1.0
    expanding = volume_ratio >= 1.0
    up = signal_return > 0
    quadrant = (
        "up_expanding"
        if up and expanding
        else "up_contracting"
        if up
        else "down_or_flat_expanding"
        if expanding
        else "down_or_flat_contracting"
    )

    prior_high = pd.to_numeric(prior_20["high"], errors="coerce").max()
    prior_low = pd.to_numeric(prior_20["low"], errors="coerce").min()
    price_location = None
    if pd.notna(prior_high) and pd.notna(prior_low) and float(prior_high) > float(prior_low):
        price_location = (
            float(current_close) - float(prior_low)
        ) / (float(prior_high) - float(prior_low))

    amount_ratio = None
    turnover_proxy = None
    if "amount" in limited.columns:
        amount = pd.to_numeric(limited["amount"], errors="coerce")
        amount_baseline = _positive_mean(
            amount.iloc[signal_index - 20 : signal_index]
        )
        current_amount = amount.iloc[signal_index]
        if pd.notna(current_amount) and float(current_amount) > 0:
            amount_ratio = _safe_ratio(current_amount, amount_baseline)
        if (
            pd.notna(current_amount)
            and float(current_amount) > 0
            and isinstance(market_cap_100m, (int, float))
            and float(market_cap_100m) > 0
        ):
            turnover_proxy = float(current_amount) / (
                float(market_cap_100m) * 100_000_000.0
            )

    volume_ratio_3 = None
    if signal_index >= 22:
        current_3_mean = _positive_mean(
            volume.iloc[signal_index - 2 : signal_index + 1]
        )
        prior_3_baseline = _positive_mean(
            volume.iloc[signal_index - 22 : signal_index - 2]
        )
        volume_ratio_3 = _safe_ratio(current_3_mean, prior_3_baseline)

    result.update(
        {
            "factor_assignment_available": True,
            "up_volume_confirmation": int(up and expanding),
            "signal_day_return": signal_return,
            "volume_ratio_20": volume_ratio,
            "amount_ratio_20": amount_ratio,
            "volume_ratio_3_vs_20": volume_ratio_3,
            "price_location_20": price_location,
            "turnover_value_proxy": turnover_proxy,
            "signed_volume_impulse": (
                signal_return * math.log(volume_ratio)
                if volume_ratio > 0
                else None
            ),
            "volume_price_quadrant": quadrant,
        }
    )
    return result


def _history_frame(
    symbol: str,
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
    cache[symbol] = frame if not frame.empty else None
    return cache[symbol]


def _factor_features_for_sources(
    sources: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache: dict[str, pd.DataFrame | None] = {}
    history_hashes: dict[str, str] = {}
    features: dict[str, dict[str, Any]] = {}
    errors: Counter = Counter()
    for source in sources:
        identifier = candidate_id(source)
        if identifier in features:
            raise RuntimeError(f"duplicate candidate id after integrity gate: {identifier}")
        symbol = str(source.get("symbol", "")).zfill(6)
        frame = _history_frame(symbol, cache, history_hashes)
        base = {"candidate_id": identifier}
        if frame is None:
            base.update(
                {
                    **{factor: None for factor in FACTOR_SPECS},
                    "volume_price_quadrant": None,
                    "factor_assignment_available": False,
                    "factor_error": "missing_history",
                    "baseline_excludes_signal_day": True,
                }
            )
            errors["missing_history"] += 1
            features[identifier] = base
            continue
        signal_day = date.fromisoformat(str(source["signal_day"]))
        day_values = pd.to_datetime(frame["datetime"]).dt.date
        matches = frame.index[day_values == signal_day].tolist()
        if not matches:
            base.update(
                {
                    **{factor: None for factor in FACTOR_SPECS},
                    "volume_price_quadrant": None,
                    "factor_assignment_available": False,
                    "factor_error": "missing_signal_bar",
                    "baseline_excludes_signal_day": True,
                }
            )
            errors["missing_signal_bar"] += 1
            features[identifier] = base
            continue
        factor = _volume_price_features(
            frame, int(matches[-1]), source.get("market_cap")
        )
        if factor.get("factor_error"):
            errors[str(factor["factor_error"])] += 1
        base.update(factor)
        features[identifier] = base
    history_manifest = hashlib.sha256(
        "\n".join(
            f"{symbol}|{history_hashes[symbol]}" for symbol in sorted(history_hashes)
        ).encode("utf-8")
    ).hexdigest()
    diagnostic_unavailable = {
        factor: sum(value.get(factor) is None for value in features.values())
        for factor, spec in FACTOR_SPECS.items()
        if spec["role"] == "diagnostic"
    }
    return features, {
        "history_manifest_sha256": history_manifest,
        "history_symbol_count": len(history_hashes),
        "factor_errors": dict(errors),
        "diagnostic_unavailable_counts": diagnostic_unavailable,
        "primary_definition": (
            "signal_day_return>0 and signal_day_volume/prior_20_session_mean>=1"
        ),
        "volume_baseline_excludes_signal_day": True,
        "turnover_field": (
            "amount/(market_cap_100m*1e8), diagnostic proxy only; unavailable "
            "when cached amount is non-positive"
        ),
        "exchange_turnover_rate_available": False,
    }


def _assign_primary_group(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        value = row.get(PRIMARY_FACTOR)
        row["primary_high_quality"] = bool(value) if value is not None else None
        row["variant_included"] = bool(value) if value is not None else False


def _factor_report(
    rows: list[dict[str, Any]], factor: str, larger_is_better: bool
) -> dict[str, Any]:
    usable = [row for row in rows if row.get(factor) is not None]
    return {
        "n": len(usable),
        "coverage": len(usable) / len(rows) if rows else 0.0,
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


def _quadrant_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("volume_price_quadrant"):
            groups[str(row["volume_price_quadrant"])].append(row)
    return {
        name: {
            "n": len(group),
            "unique_symbols": len({str(row["symbol"]) for row in group}),
            "unique_signal_days": len({str(row["signal_day"]) for row in group}),
            "stats": {
                field: stats([row.get(field) for row in group]) for field in FIELDS
            },
        }
        for name, group in sorted(groups.items())
    }


def _group_sample(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "unique_candidates": len({str(row["candidate_id"]) for row in rows}),
        "unique_symbols": len({str(row["symbol"]) for row in rows}),
        "unique_signal_days": len({str(row["signal_day"]) for row in rows}),
    }


def _candidate_report(
    rows: list[dict[str, Any]],
    source_match: dict[str, Any],
    factor_meta: dict[str, Any],
    bootstrap_reps: int,
    seed: int,
) -> dict[str, Any]:
    assigned = [row for row in rows if row.get("factor_assignment_available")]
    confirmed = [row for row in assigned if row.get("primary_high_quality")]
    comparison = [
        row for row in assigned if row.get("primary_high_quality") is False
    ]
    bootstraps = {
        outcome: _cluster_bootstrap_delta(
            assigned, outcome, reps=bootstrap_reps, seed=seed + index
        )
        for index, outcome in enumerate(
            ("future_20d", "future_40d", "trade_pnl_pct")
        )
    }
    confirmed_sample = _group_sample(confirmed)
    comparison_sample = _group_sample(comparison)
    coverage = len(assigned) / len(rows) if rows else 0.0

    def group_sufficient(sample: dict[str, Any]) -> bool:
        return bool(
            sample["unique_candidates"] >= MIN_GROUP_CANDIDATES
            and sample["unique_symbols"] >= MIN_GROUP_SYMBOLS
            and sample["unique_signal_days"] >= MIN_GROUP_SIGNAL_DAYS
        )

    sample_sufficient = group_sufficient(confirmed_sample) and group_sufficient(
        comparison_sample
    )
    direction_positive = all(
        bootstraps[outcome]["mean_delta"] is not None
        and bootstraps[outcome]["mean_delta"] > 0
        for outcome in ("future_40d", "trade_pnl_pct")
    )
    bootstrap_supported = all(
        bootstraps[outcome]["ci95_low"] is not None
        and bootstraps[outcome]["ci95_low"] > 0
        for outcome in ("future_40d", "trade_pnl_pct")
    )
    return {
        "n": len(rows),
        "assignment_coverage": coverage,
        "primary_factor": PRIMARY_FACTOR,
        "primary_group_rule": (
            "signal_day_return>0_and_volume_ratio_20_gte_1"
        ),
        "primary_confirmed_n": len(confirmed),
        "primary_comparison_n": len(comparison),
        "baseline": {field: stats([row.get(field) for row in rows]) for field in FIELDS},
        "primary_confirmed": {
            field: stats([row.get(field) for row in confirmed]) for field in FIELDS
        },
        "primary_comparison": {
            field: stats([row.get(field) for row in comparison]) for field in FIELDS
        },
        "by_primary_factor": group_stats(assigned, PRIMARY_FACTOR),
        "quadrants": _quadrant_report(assigned),
        "factors": {
            factor: _factor_report(rows, factor, spec["larger_is_better"])
            for factor, spec in FACTOR_SPECS.items()
        },
        "cluster_bootstrap_confirmed_minus_comparison": bootstraps,
        "sample_gate": {
            "confirmed": confirmed_sample,
            "comparison": comparison_sample,
            "minimum_candidates_per_group": MIN_GROUP_CANDIDATES,
            "minimum_symbols_per_group": MIN_GROUP_SYMBOLS,
            "minimum_signal_days_per_group": MIN_GROUP_SIGNAL_DAYS,
            "sufficient": sample_sufficient,
        },
        "gate": {
            "assignment_coverage_ok": coverage >= MIN_ASSIGNMENT_COVERAGE,
            "sample_sufficient": sample_sufficient,
            "primary_direction_positive": direction_positive,
            "primary_cluster_bootstrap_ci95_positive": bootstrap_supported,
            "eligible_for_cross_split": bool(
                coverage >= MIN_ASSIGNMENT_COVERAGE and sample_sufficient
            ),
        },
        "source_match": source_match,
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


def _cross_split_gate(split_reports: dict[str, Any]) -> dict[str, Any]:
    eligible = [
        split
        for split, report in split_reports.items()
        if report["candidate_report"]["gate"]["eligible_for_cross_split"]
    ]
    directions = {
        split: {
            outcome: report["candidate_report"]
            ["cluster_bootstrap_confirmed_minus_comparison"][outcome]["mean_delta"]
            for outcome in ("future_40d", "trade_pnl_pct")
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
            ["primary_cluster_bootstrap_ci95_positive"]
            for split in eligible
        )
    )
    return {
        "eligible_splits": eligible,
        "directions": directions,
        "minimum_direction_splits": MIN_DIRECTION_SPLITS,
        "direction_consistent": direction_consistent,
        "cluster_bootstrap_supported": bootstrap_supported,
        "pass": bool(direction_consistent and bootstrap_supported),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    splits = tuple(args.splits)
    invalid = sorted(set(splits) - set(SPLITS))
    if invalid:
        raise RuntimeError(
            f"unregistered split(s) {invalid}; this audit cannot consume holdout"
        )
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
        "design": {
            "candidate_layer_only": True,
            "primary_factor": PRIMARY_FACTOR,
            "diagnostic_factors_cannot_pass_gate": [
                factor
                for factor, spec in FACTOR_SPECS.items()
                if spec["role"] == "diagnostic"
            ],
            "four_quadrants_diagnostic_only": True,
            "canonical_candidates_required": True,
            "signal_day_closed_bars_only": True,
            "prior_20_baseline_excludes_signal_day": True,
            "holdout_consumed": False,
            "portfolio_layer_implemented": False,
            "production_eligible": False,
        },
        "thresholds": {
            "assignment_coverage": MIN_ASSIGNMENT_COVERAGE,
            "candidates_per_group": MIN_GROUP_CANDIDATES,
            "symbols_per_group": MIN_GROUP_SYMBOLS,
            "signal_days_per_group": MIN_GROUP_SIGNAL_DAYS,
            "direction_splits": MIN_DIRECTION_SPLITS,
        },
        "config_file": str(config_path),
        "config_file_sha256_before": config_hash_before,
        "config_snapshot": _config_snapshot(config),
        "script_sha256": file_sha256(Path(__file__).resolve()),
        "seed": args.seed,
        "bootstrap_reps": args.bootstrap_reps,
        "splits": {},
    }
    for split_index, split in enumerate(splits):
        source_path = input_dir / f"candidates_{split}.jsonl"
        sources = load_jsonl(source_path)
        integrity = _validate_integrity_manifest(
            input_dir, split, source_path, sources
        )
        feature_map, factor_meta = _factor_features_for_sources(sources)
        replay_rows, replay_skips, source_match = _replay_split(source_path, config)
        enriched: list[dict[str, Any]] = []
        for replay in replay_rows:
            identifier = candidate_id(replay)
            row = dict(replay)
            row.update(feature_map.get(identifier, {}))
            enriched.append(row)
        _assign_primary_group(enriched)
        enriched_path = output_dir / f"volume_price_{split}.jsonl"
        _write_jsonl(enriched_path, enriched)
        result["splits"][split] = {
            "source": str(source_path),
            "source_sha256": file_sha256(source_path),
            "candidate_integrity": integrity,
            "enriched": str(enriched_path),
            "enriched_sha256": file_sha256(enriched_path),
            "replay_skips": dict(replay_skips),
            "candidate_report": _candidate_report(
                enriched,
                source_match,
                factor_meta,
                args.bootstrap_reps,
                args.seed + split_index * 100,
            ),
        }
    result["candidate_gate"] = _cross_split_gate(result["splits"])
    baseline_complete = all(
        result["splits"][split]["candidate_report"]["source_match"]
        ["baseline_replay_complete"]
        for split in splits
    )
    result["baseline_replay_complete"] = baseline_complete
    result["config_file_sha256_after"] = file_sha256(config_path)
    result["config_file_unchanged"] = (
        result["config_file_sha256_after"] == config_hash_before
    )
    if not baseline_complete:
        result["verdict"] = "invalid_baseline_replay_incomplete"
    elif not result["candidate_gate"]["pass"]:
        result["verdict"] = "candidate_layer_rejected_or_insufficient_sample"
    else:
        result["verdict"] = (
            "candidate_layer_historical_support_requires_portfolio_rule_preregistration"
        )
    result["production_decision"] = "unchanged_P0"
    result_path = output_dir / "volume_price_audit.json"
    with result_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir", default=r"D:\tmp\candidates_fullpool_canonical"
    )
    parser.add_argument(
        "--output-dir", default=r"D:\tmp\volume_price_candidate"
    )
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--label", default="volume-price-candidate-audit")
    parser.add_argument("--splits", nargs="+", default=list(SPLITS))
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--bootstrap-reps", type=int, default=2000)
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
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
