"""Audit funded-portfolio sensitivity to same-day candidate ordering.

The random-seed sweep is an algorithmic sensitivity analysis, not a confidence
interval and not an efficacy test. Viewed-test results are reported but never
used by the frozen train/validation robustness screen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import (  # noqa: E402
    DEFAULT_SIGNAL_PRIORITY,
    _resolve_execution_config,
    run_portfolio,
)
from utils.helpers import load_config  # noqa: E402

VERSION = "portfolio_order_sensitivity.v1"
BASELINE = "baseline"
DEFAULT_VARIANTS = ("mfe_profit_lock", "atr_trailing")
SPLITS = ("train", "val", "test")
DETERMINISTIC_TIE_BREAKS = ("symbol_asc", "symbol_desc", "hash", "rotate")
DEFAULT_CONFIG = BASE_DIR / "config" / "config.yaml"
DEFAULT_PORTFOLIO_CONFIG = {
    "initial_cash": 100000.0,
    "max_positions": 4,
    "position_size_pct": 0.25,
    "signal_priority": list(DEFAULT_SIGNAL_PRIORITY),
    "score_mode": "P0",
}
ENTRY_INVARIANT_FIELDS = (
    "symbol",
    "signal_day",
    "entry_day",
    "entry_price",
    "signal_type",
)


def _guard_development_path(path: Path, allow_holdout: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if not allow_holdout and any("holdout" in part.lower() for part in resolved.parts):
        raise ValueError(
            f"Holdout path is blocked for development experiments: {resolved}. "
            "Use only after the experiment is frozen and separately authorized."
        )
    return resolved


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _candidate_manifest(candidate_ids: list[str] | set[str] | tuple[str, ...]) -> str:
    return _sha256_bytes("\n".join(sorted(candidate_ids)).encode("utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_json(row) + "\n")
    return {"path": str(path), "rows": len(rows), "sha256": _sha256_file(path)}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _index_unique(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in rows:
        candidate_id = str(row.get("candidate_id", ""))
        if not candidate_id:
            raise RuntimeError(f"missing candidate_id in {label}")
        if candidate_id in indexed:
            duplicates.append(candidate_id)
        else:
            indexed[candidate_id] = row
    if duplicates:
        sample = ", ".join(sorted(set(duplicates))[:5])
        raise RuntimeError(f"duplicate candidate_id values in {label}: {sample}")
    return indexed


def _entry_signature(row: dict[str, Any]) -> str:
    return _canonical_json({key: row.get(key) for key in ENTRY_INVARIANT_FIELDS})


def _load_aligned_profiles(
    root: Path,
    split: str,
    variants: tuple[str, ...],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    profiles = (BASELINE,) + variants
    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    input_files: dict[str, Any] = {}
    for profile in profiles:
        path = root / profile / f"candidates_{split}.jsonl"
        rows = _load_jsonl(path)
        indexed[profile] = _index_unique(rows, f"{profile}/{split}")
        input_files[profile] = {
            "path": str(path),
            "rows": len(rows),
            "sha256": _sha256_file(path),
            "candidate_manifest_sha256": _candidate_manifest(set(indexed[profile])),
        }

    paired_ids = set(indexed[BASELINE])
    for profile in variants:
        paired_ids &= set(indexed[profile])
    coverage = {
        profile: round(len(paired_ids) / max(len(indexed[profile]), 1) * 100.0, 4)
        for profile in profiles
    }
    if not paired_ids or any(value < 99.0 for value in coverage.values()):
        raise RuntimeError(
            f"paired candidate coverage below 99% for {split}: {coverage}"
        )

    baseline = indexed[BASELINE]
    mismatches: list[str] = []
    for candidate_id in sorted(paired_ids):
        expected = _entry_signature(baseline[candidate_id])
        for profile in variants:
            if _entry_signature(indexed[profile][candidate_id]) != expected:
                mismatches.append(f"{profile}:{candidate_id}")
    if mismatches:
        raise RuntimeError(
            "entry-time fields differ across exit profiles: " + ", ".join(mismatches[:5])
        )

    ordered_ids = sorted(
        paired_ids,
        key=lambda candidate_id: (
            str(baseline[candidate_id].get("entry_day", "")),
            str(baseline[candidate_id].get("signal_type", "")),
            str(baseline[candidate_id].get("symbol", "")),
            str(baseline[candidate_id].get("signal_day", "")),
            candidate_id,
        ),
    )
    aligned = {
        profile: [indexed[profile][candidate_id] for candidate_id in ordered_ids]
        for profile in profiles
    }
    integrity = {
        "input_files": input_files,
        "completed_counts": {profile: len(indexed[profile]) for profile in profiles},
        "paired_count": len(ordered_ids),
        "paired_coverage_pct": coverage,
        "paired_candidate_manifest_sha256": _candidate_manifest(ordered_ids),
        "entry_invariant_fields": list(ENTRY_INVARIANT_FIELDS),
        "entry_invariant_mismatches": 0,
        "canonical_profile_order": list(profiles),
    }
    return aligned, integrity


def _accepted_ids(portfolio: dict[str, Any]) -> set[str]:
    return {str(row["candidate_id"]) for row in portfolio["accepted_entries"]}


def _portfolio_snapshot(portfolio: dict[str, Any]) -> dict[str, Any]:
    summary = portfolio["summary"]
    attribution = portfolio["attribution"]
    accepted_ids = _accepted_ids(portfolio)
    return {
        "total_return_pct": float(summary["total_return_pct"]),
        "max_drawdown_pct": float(summary["max_drawdown_pct"]),
        "trade_count": int(summary["count"]),
        "win_rate": float(summary["win_rate"]),
        "final_equity": float(summary["final_equity"]),
        "transaction_cost_cash": float(attribution["transaction_cost_cash"]),
        "position_capacity_utilization_pct": float(
            attribution["position_capacity_utilization_pct"]
        ),
        "max_positions_rejections": int(attribution["max_positions_rejections"]),
        "accepted_candidate_ids_sha256": _candidate_manifest(accepted_ids),
        "accepted_candidate_count": len(accepted_ids),
        "_accepted_ids": accepted_ids,
    }


def _run_profiles_once(
    aligned: dict[str, list[dict[str, Any]]],
    costs: dict[str, Any],
    portfolio_config: dict[str, Any],
    tie_break: str,
    seed: int,
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for profile, rows in aligned.items():
        config = dict(portfolio_config)
        config.update({"score_mode": "P0", "tie_break": tie_break, "seed": seed})
        portfolio = run_portfolio([dict(row) for row in rows], costs, config)
        snapshots[profile] = _portfolio_snapshot(portfolio)
    return snapshots


def _jaccard_pct(left: set[str], right: set[str]) -> float:
    union = left | right
    if not union:
        return 100.0
    return round(len(left & right) / len(union) * 100.0, 4)


def _paired_effect(
    baseline: dict[str, Any],
    variant: dict[str, Any],
) -> dict[str, Any]:
    baseline_ids = baseline["_accepted_ids"]
    variant_ids = variant["_accepted_ids"]
    return {
        "return_delta_pp": round(
            variant["total_return_pct"] - baseline["total_return_pct"], 4
        ),
        "max_drawdown_delta_pp": round(
            variant["max_drawdown_pct"] - baseline["max_drawdown_pct"], 4
        ),
        "trade_count_delta": variant["trade_count"] - baseline["trade_count"],
        "win_rate_delta_pp": round(variant["win_rate"] - baseline["win_rate"], 4),
        "transaction_cost_delta_cash": round(
            variant["transaction_cost_cash"] - baseline["transaction_cost_cash"], 2
        ),
        "accepted_id_jaccard_pct": _jaccard_pct(baseline_ids, variant_ids),
        "newly_accepted_candidate_ids": len(variant_ids - baseline_ids),
        "baseline_candidate_ids_not_accepted": len(baseline_ids - variant_ids),
    }


def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if not key.startswith("_")}


def _distribution(values: list[float]) -> dict[str, Any]:
    clean = np.asarray(
        [float(value) for value in values if value is not None and math.isfinite(float(value))],
        dtype=float,
    )
    if clean.size == 0:
        return {"n": 0}
    return {
        "n": int(clean.size),
        "mean": round(float(clean.mean()), 4),
        "std": round(float(clean.std(ddof=0)), 4),
        "min": round(float(clean.min()), 4),
        "p10": round(float(np.quantile(clean, 0.10)), 4),
        "p25": round(float(np.quantile(clean, 0.25)), 4),
        "median": round(float(np.median(clean)), 4),
        "p75": round(float(np.quantile(clean, 0.75)), 4),
        "p90": round(float(np.quantile(clean, 0.90)), 4),
        "max": round(float(clean.max()), 4),
        "positive_pct": round(float((clean > 0).mean() * 100.0), 2),
        "non_negative_pct": round(float((clean >= 0).mean() * 100.0), 2),
    }


def _percentile_of_distribution(observed: float, values: list[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return None
    return round(sum(value <= observed for value in clean) / len(clean) * 100.0, 2)


def _run_order_sensitivity_split(
    root: Path,
    split: str,
    variants: tuple[str, ...],
    costs: dict[str, Any],
    output_dir: Path,
    *,
    random_seed_count: int,
    random_seed_start: int,
    portfolio_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if random_seed_count < 1:
        raise ValueError("random_seed_count must be positive")
    config = dict(DEFAULT_PORTFOLIO_CONFIG if portfolio_config is None else portfolio_config)
    aligned, integrity = _load_aligned_profiles(root, split, variants)

    deterministic: dict[str, Any] = {}
    deterministic_effects: dict[str, dict[str, Any]] = {variant: {} for variant in variants}
    for tie_break in DETERMINISTIC_TIE_BREAKS:
        snapshots = _run_profiles_once(
            aligned, costs, config, tie_break, random_seed_start
        )
        deterministic[tie_break] = {
            profile: _public_snapshot(snapshot) for profile, snapshot in snapshots.items()
        }
        for variant in variants:
            deterministic_effects[variant][tie_break] = _paired_effect(
                snapshots[BASELINE], snapshots[variant]
            )

    seed_rows: list[dict[str, Any]] = []
    delta_rows: list[dict[str, Any]] = []
    random_profile_metrics: dict[str, dict[str, list[float]]] = {
        profile: {
            "total_return_pct": [],
            "max_drawdown_pct": [],
            "trade_count": [],
            "win_rate": [],
            "transaction_cost_cash": [],
            "position_capacity_utilization_pct": [],
        }
        for profile in aligned
    }
    random_delta_metrics: dict[str, dict[str, list[float]]] = {
        variant: {
            "return_delta_pp": [],
            "max_drawdown_delta_pp": [],
            "trade_count_delta": [],
            "win_rate_delta_pp": [],
            "transaction_cost_delta_cash": [],
            "accepted_id_jaccard_pct": [],
            "newly_accepted_candidate_ids": [],
            "baseline_candidate_ids_not_accepted": [],
        }
        for variant in variants
    }
    for offset in range(random_seed_count):
        seed = random_seed_start + offset
        snapshots = _run_profiles_once(aligned, costs, config, "random", seed)
        for profile, snapshot in snapshots.items():
            public = _public_snapshot(snapshot)
            seed_rows.append({"split": split, "seed": seed, "profile": profile, **public})
            for metric in random_profile_metrics[profile]:
                random_profile_metrics[profile][metric].append(float(public[metric]))
        for variant in variants:
            effect = _paired_effect(snapshots[BASELINE], snapshots[variant])
            delta_rows.append({"split": split, "seed": seed, "variant": variant, **effect})
            for metric in random_delta_metrics[variant]:
                random_delta_metrics[variant][metric].append(float(effect[metric]))

    seed_artifact = _write_jsonl(output_dir / f"random_seed_runs_{split}.jsonl", seed_rows)
    delta_artifact = _write_jsonl(output_dir / f"paired_seed_deltas_{split}.jsonl", delta_rows)
    profile_distributions = {
        profile: {metric: _distribution(values) for metric, values in metrics.items()}
        for profile, metrics in random_profile_metrics.items()
    }
    delta_distributions = {
        variant: {metric: _distribution(values) for metric, values in metrics.items()}
        for variant, metrics in random_delta_metrics.items()
    }

    symbol_asc_percentiles = {
        profile: {
            "observed_total_return_pct": deterministic["symbol_asc"][profile][
                "total_return_pct"
            ],
            "random_order_return_percentile": _percentile_of_distribution(
                deterministic["symbol_asc"][profile]["total_return_pct"],
                random_profile_metrics[profile]["total_return_pct"],
            ),
        }
        for profile in aligned
    }
    symbol_asc_effect_percentiles = {
        variant: {
            "observed_return_delta_pp": deterministic_effects[variant]["symbol_asc"][
                "return_delta_pp"
            ],
            "random_order_delta_percentile": _percentile_of_distribution(
                deterministic_effects[variant]["symbol_asc"]["return_delta_pp"],
                random_delta_metrics[variant]["return_delta_pp"],
            ),
        }
        for variant in variants
    }
    return {
        "integrity": integrity,
        "deterministic_controls": {
            "tie_breaks": list(DETERMINISTIC_TIE_BREAKS),
            "profiles": deterministic,
            "paired_variant_effects": deterministic_effects,
        },
        "random_seed_sweep": {
            "interpretation": (
                "Algorithmic candidate-order sensitivity distribution; seeds are not "
                "independent market samples and these quantiles are not confidence intervals."
            ),
            "tie_break": "random",
            "seed_start": random_seed_start,
            "seed_count": random_seed_count,
            "profile_metric_distributions": profile_distributions,
            "paired_variant_delta_distributions": delta_distributions,
            "symbol_asc_profile_percentiles": symbol_asc_percentiles,
            "symbol_asc_effect_percentiles": symbol_asc_effect_percentiles,
            "artifacts": {
                "seed_runs": seed_artifact,
                "paired_seed_deltas": delta_artifact,
            },
        },
    }


def _order_robustness_screen(
    split_reports: dict[str, dict[str, Any]],
    variants: tuple[str, ...],
) -> dict[str, Any]:
    if "train" not in split_reports or "val" not in split_reports:
        raise ValueError("train and val are required for the frozen robustness screen")
    result: dict[str, Any] = {}
    for variant in variants:
        train = split_reports["train"]["random_seed_sweep"][
            "paired_variant_delta_distributions"
        ][variant]["return_delta_pp"]
        validation = split_reports["val"]["random_seed_sweep"][
            "paired_variant_delta_distributions"
        ][variant]["return_delta_pp"]
        validation_controls = split_reports["val"]["deterministic_controls"][
            "paired_variant_effects"
        ][variant]
        positive_controls = sum(
            1
            for effect in validation_controls.values()
            if float(effect["return_delta_pp"]) > 0
        )
        checks = {
            "train_random_median_non_negative": float(train["median"]) >= 0,
            "validation_random_median_positive": float(validation["median"]) > 0,
            "validation_random_p10_non_negative": float(validation["p10"]) >= 0,
            "validation_positive_seed_pct_at_least_70": float(
                validation["positive_pct"]
            )
            >= 70.0,
            "validation_positive_deterministic_controls_at_least_3_of_4": (
                positive_controls >= 3
            ),
        }
        result[variant] = {
            "checks": checks,
            "validation_positive_deterministic_controls": positive_controls,
            "passes_order_robustness_screen": all(checks.values()),
            "changes_preregistered_candidate_efficacy_screen": False,
            "production_eligible": False,
            "production_blocker": (
                "Order robustness is diagnostic only. The frozen candidate-level efficacy "
                "screen and a genuinely untouched holdout are still required."
            ),
        }
    return result


def build_report(
    root: Path,
    output_dir: Path,
    config_path: Path,
    variants: tuple[str, ...],
    *,
    random_seed_count: int,
    random_seed_start: int,
    allow_holdout: bool = False,
) -> dict[str, Any]:
    root = _guard_development_path(root, allow_holdout)
    output_dir = _guard_development_path(output_dir, allow_holdout)
    config_path = _guard_development_path(config_path, allow_holdout)
    costs = _resolve_execution_config(load_config(config_path))
    portfolio_config = dict(DEFAULT_PORTFOLIO_CONFIG)
    split_reports = {
        split: _run_order_sensitivity_split(
            root,
            split,
            variants,
            costs,
            output_dir,
            random_seed_count=random_seed_count,
            random_seed_start=random_seed_start,
            portfolio_config=portfolio_config,
        )
        for split in SPLITS
    }
    comparison_path = root / "comparison.json"
    report = {
        "version": VERSION,
        "research_question": (
            "Are funded-portfolio exit improvements robust to arbitrary same-day ordering "
            "within the frozen P0 signal-priority buckets?"
        ),
        "causal_policy": {
            "ranking_inputs": [
                "entry_day",
                "signal_type priority",
                "symbol only for deterministic controls",
                "predeclared random seed only for random controls",
            ],
            "forbidden_ranking_inputs": [
                "exit_day",
                "exit_reason",
                "pnl",
                "MFE/MAE",
                "future returns",
                "post-exit returns",
            ],
            "score_mode": "P0",
            "viewed_test_used_for_screen": False,
            "holdout_used": bool(allow_holdout),
        },
        "frozen_order_robustness_rule": {
            "train_random_median_return_delta_pp": ">=0",
            "validation_random_median_return_delta_pp": ">0",
            "validation_random_p10_return_delta_pp": ">=0",
            "validation_positive_seed_pct": ">=70%",
            "validation_positive_deterministic_controls": ">=3 of 4",
            "note": (
                "This screen diagnoses order robustness only and cannot override the existing "
                "candidate-level efficacy failure."
            ),
        },
        "input": {
            "exit_experiment_root": str(root),
            "comparison_report": {
                "path": str(comparison_path),
                "sha256": _sha256_file(comparison_path),
            },
            "config": {"path": str(config_path), "sha256": _sha256_file(config_path)},
            "resolved_execution_sha256": _sha256_bytes(
                _canonical_json(costs).encode("utf-8")
            ),
            "backtest_engine": {
                "path": str(BASE_DIR / "backtest_winrate.py"),
                "sha256": _sha256_file(BASE_DIR / "backtest_winrate.py"),
            },
            "experiment_code": {
                "path": str(Path(__file__).resolve()),
                "sha256": _sha256_file(Path(__file__).resolve()),
            },
        },
        "profiles": [BASELINE, *variants],
        "portfolio_config": {
            **portfolio_config,
            "random_seed_start": random_seed_start,
            "random_seed_count": random_seed_count,
        },
        "splits": split_reports,
        "screen": _order_robustness_screen(split_reports, variants),
        "production_eligible": False,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit portfolio results across deterministic and random candidate orders"
    )
    parser.add_argument("--exit-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--variants", nargs="+", default=list(DEFAULT_VARIANTS))
    parser.add_argument("--random-seeds", type=int, default=200)
    parser.add_argument("--seed-start", type=int, default=20260830)
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()
    variants = tuple(dict.fromkeys(str(value) for value in args.variants))
    if BASELINE in variants:
        raise ValueError("baseline is implicit and must not appear in --variants")
    report = build_report(
        args.exit_root,
        args.output_dir,
        args.config,
        variants,
        random_seed_count=args.random_seeds,
        random_seed_start=args.seed_start,
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
                "production_eligible": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
