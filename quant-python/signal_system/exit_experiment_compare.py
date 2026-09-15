"""Compare exit experiment variants on paired completed candidate IDs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
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
    run_portfolio,
    summarize,
)
from utils.helpers import load_config  # noqa: E402

BASELINE = "baseline"
DEFAULT_VARIANTS = (
    "zero_axis_confirm_2",
    "timeout_ma_break",
    "mfe_profit_lock",
    "atr_trailing",
)
LOW_OPEN_RESEARCH_VARIANTS = (
    "fixed_sl5",
    "regime_sl5_sl8",
    "weak_market_low_open",
)
ALL_VARIANTS = (*DEFAULT_VARIANTS, *LOW_OPEN_RESEARCH_VARIANTS)
SPLITS = ("train", "val", "test")
DEFAULT_CONFIG = BASE_DIR / "config" / "config.yaml"
PORTFOLIO_CONFIG = {
    "initial_cash": 100000.0,
    "max_positions": 4,
    "position_size_pct": 0.25,
    "signal_priority": list(DEFAULT_SIGNAL_PRIORITY),
    "score_mode": "P0",
    "tie_break": "symbol_asc",
    "seed": 20260830,
}


def _guard_development_path(path: Path, allow_holdout: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if not allow_holdout and any("holdout" in part.lower() for part in resolved.parts):
        raise ValueError(
            f"Holdout path is blocked for development experiments: {resolved}. "
            "Use only after the experiment is frozen and separately authorized."
        )
    return resolved


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {
        "path": str(path),
        "rows": len(rows),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _candidate_manifest(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(str(row["candidate_id"]) for row in rows)).encode("utf-8")
    ).hexdigest()


def _unique_rows_by_candidate_id(
    rows: list[dict[str, Any]],
    label: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in rows:
        identifier = str(row["candidate_id"])
        if identifier in indexed:
            duplicates.append(identifier)
            continue
        indexed[identifier] = row
    if duplicates:
        sample = ", ".join(sorted(set(duplicates))[:5])
        raise RuntimeError(
            f"duplicate candidate_id values in {label}; canonicalize the source first: {sample}"
        )
    return indexed


def _bootstrap_mean_ci(values: list[float], seed: int = 20260830) -> dict[str, Any]:
    clean = np.asarray([float(value) for value in values if value is not None], dtype=float)
    if clean.size == 0:
        return {"n": 0}
    if clean.size == 1:
        only = float(clean[0])
        return {"n": 1, "mean": only, "ci95_low": only, "ci95_high": only}
    rng = np.random.default_rng(seed)
    samples = rng.choice(clean, size=(5000, clean.size), replace=True).mean(axis=1)
    low, high = np.quantile(samples, [0.025, 0.975])
    return {
        "n": int(clean.size),
        "mean": round(float(clean.mean()), 4),
        "median": round(float(np.median(clean)), 4),
        "p10": round(float(np.quantile(clean, 0.10)), 4),
        "p90": round(float(np.quantile(clean, 0.90)), 4),
        "min": round(float(clean.min()), 4),
        "max": round(float(clean.max()), 4),
        "positive_pct": round(float((clean > 0).mean() * 100.0), 2),
        "ci95_low": round(float(low), 4),
        "ci95_high": round(float(high), 4),
    }


def _paired_split(
    root: Path,
    split: str,
    variants: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    variants = tuple(
        variants
        or (
            variant
            for variant in DEFAULT_VARIANTS
            if (root / variant / f"candidates_{split}.jsonl").exists()
        )
    )
    profiles = (BASELINE, *variants)
    by_variant = {
        variant: _unique_rows_by_candidate_id(
            _load_jsonl(root / variant / f"candidates_{split}.jsonl"),
            f"{variant}:{split}",
        )
        for variant in profiles
    }
    ids_by_variant = {variant: set(rows) for variant, rows in by_variant.items()}
    paired_ids = set.intersection(*ids_by_variant.values())
    paired_keys = sorted(paired_ids)
    baseline = by_variant[BASELINE]
    result: dict[str, Any] = {
        "completed_counts": {
            variant: len(rows) for variant, rows in by_variant.items()
        },
        "paired_count": len(paired_ids),
        "paired_coverage_pct": {
            variant: round(len(paired_ids) / len(rows) * 100.0, 2) if rows else None
            for variant, rows in by_variant.items()
        },
        "completed_only_by_variant": {
            variant: len(ids_by_variant[variant] - paired_ids)
            for variant in profiles
        },
        "variants": {},
    }
    for variant in variants:
        rows = by_variant[variant]
        pnl_delta = [
            float(rows[key]["trade_pnl_pct"]) - float(baseline[key]["trade_pnl_pct"])
            for key in paired_keys
        ]
        efficiency_delta = [
            (
                float(rows[key]["trade_pnl_pct"])
                - float(baseline[key]["trade_pnl_pct"])
            )
            / float(baseline[key]["mfe_common_60"])
            for key in paired_keys
            if baseline[key].get("mfe_common_60") not in (None, 0)
        ]
        post20_pairs = [
            (
                float(rows[key]["post_exit_20d"]),
                float(baseline[key]["post_exit_20d"]),
            )
            for key in paired_keys
            if rows[key].get("post_exit_20d") is not None
            and baseline[key].get("post_exit_20d") is not None
        ]
        post20_delta = [variant_value - base_value for variant_value, base_value in post20_pairs]
        holding_delta = [
            float(rows[key]["holding_bars"]) - float(baseline[key]["holding_bars"])
            for key in paired_keys
        ]
        changed_pnl_delta = [value for value in pnl_delta if abs(value) > 1e-9]
        exit_transitions = Counter(
            f"{baseline[key].get('exit_reason')}->{rows[key].get('exit_reason')}"
            for key in paired_keys
        )
        group_deltas: dict[str, list[float]] = defaultdict(list)
        for key in paired_keys:
            group = f"{baseline[key].get('regime', 'unknown')}|{baseline[key].get('signal_type', 'unknown')}"
            group_deltas[group].append(
                float(rows[key]["trade_pnl_pct"])
                - float(baseline[key]["trade_pnl_pct"])
            )
        result["variants"][variant] = {
            "paired_pnl_delta_pp": _bootstrap_mean_ci(pnl_delta),
            "changed_trade_pnl_delta_pp": _bootstrap_mean_ci(changed_pnl_delta),
            "changed_trade_count": len(changed_pnl_delta),
            "changed_trade_pct": round(
                len(changed_pnl_delta) / len(paired_ids) * 100.0, 2
            ) if paired_ids else None,
            "paired_common60_efficiency_delta": _bootstrap_mean_ci(efficiency_delta),
            "paired_post_exit20_delta_pp": _bootstrap_mean_ci(post20_delta),
            "paired_post_exit20_coverage_pct": round(
                len(post20_pairs) / len(paired_ids) * 100.0, 2
            ) if paired_ids else None,
            "paired_holding_bars_delta": _bootstrap_mean_ci(holding_delta),
            "exit_reason_counts": dict(
                sorted(Counter(rows[key].get("exit_reason") for key in paired_keys).items())
            ),
            "exit_reason_transitions": dict(sorted(exit_transitions.items())),
            "by_regime_signal_type": {
                key: _bootstrap_mean_ci(values)
                for key, values in sorted(group_deltas.items())
            },
        }
    return result


def _paired_portfolio_split(
    root: Path,
    split: str,
    variants: tuple[str, ...],
    costs: dict[str, Any],
) -> dict[str, Any]:
    profiles = (BASELINE, *variants)
    by_variant = {
        variant: _unique_rows_by_candidate_id(
            _load_jsonl(root / variant / f"candidates_{split}.jsonl"),
            f"{variant}:{split}",
        )
        for variant in profiles
    }
    paired_ids = set.intersection(*(set(rows) for rows in by_variant.values()))
    output_root = root / "portfolio_comparison" / split
    result: dict[str, Any] = {
        "paired_candidate_count": len(paired_ids),
        "paired_candidate_manifest_sha256": hashlib.sha256(
            "\n".join(sorted(paired_ids)).encode("utf-8")
        ).hexdigest(),
        "portfolio_config": PORTFOLIO_CONFIG,
        "profiles": {},
    }
    dynamic_results: dict[str, dict[str, Any]] = {}

    def profile_payload(
        variant: str,
        candidates: list[dict[str, Any]],
        portfolio: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        return {
            "candidate_stats": {
                **summarize(candidates),
                "candidate_manifest_sha256": _candidate_manifest(candidates),
            },
            "portfolio_summary": portfolio["summary"],
            "portfolio_attribution": portfolio["attribution"],
            "rejection_reasons": portfolio["rejection_reasons"],
            "artifacts": {
                "accepted_entries": _write_jsonl(
                    output_root / f"{variant}_{label}_accepted_entries.jsonl",
                    portfolio["accepted_entries"],
                ),
                "trades": _write_jsonl(
                    output_root / f"{variant}_{label}_portfolio_trades.jsonl",
                    portfolio["trades"],
                ),
                "rejections": _write_jsonl(
                    output_root / f"{variant}_{label}_portfolio_rejections.jsonl",
                    portfolio["rejections"],
                ),
                "equity_curve": _write_jsonl(
                    output_root / f"{variant}_{label}_equity_curve.jsonl",
                    portfolio["equity_curve"],
                ),
            },
        }

    for variant in profiles:
        candidates = [by_variant[variant][key] for key in sorted(paired_ids)]
        portfolio = run_portfolio(candidates, costs, dict(PORTFOLIO_CONFIG))
        dynamic_results[variant] = portfolio
        result["profiles"][variant] = profile_payload(
            variant, candidates, portfolio, "dynamic_universe"
        )

    baseline_entry_ids = sorted(
        {
            str(entry["candidate_id"])
            for entry in dynamic_results[BASELINE]["accepted_entries"]
        }
    )
    baseline_entry_id_set = set(baseline_entry_ids)
    cohort_profiles: dict[str, Any] = {}
    cohort_results: dict[str, dict[str, Any]] = {}
    for variant in profiles:
        candidates = [
            by_variant[variant][key]
            for key in baseline_entry_ids
            if key in by_variant[variant]
        ]
        portfolio = run_portfolio(candidates, costs, dict(PORTFOLIO_CONFIG))
        cohort_results[variant] = portfolio
        accepted_ids = {
            str(entry["candidate_id"]) for entry in portfolio["accepted_entries"]
        }
        payload = profile_payload(
            variant, candidates, portfolio, "baseline_entry_cohort"
        )
        payload["baseline_entry_acceptance_coverage_pct"] = round(
            len(accepted_ids & baseline_entry_id_set) / len(baseline_entry_id_set) * 100.0,
            2,
        ) if baseline_entry_id_set else None
        payload["baseline_entries_not_accepted"] = len(
            baseline_entry_id_set - accepted_ids
        )
        cohort_profiles[variant] = payload
    result["baseline_entry_cohort"] = {
        "definition": (
            "Re-run every exit profile using only the candidate IDs accepted by "
            "the dynamic baseline portfolio. This isolates exit/cash-flow effects "
            "from replacement trades admitted after earlier exits."
        ),
        "candidate_count": len(baseline_entry_ids),
        "candidate_manifest_sha256": hashlib.sha256(
            "\n".join(baseline_entry_ids).encode("utf-8")
        ).hexdigest(),
        "profiles": cohort_profiles,
    }
    dynamic_baseline_return = float(
        dynamic_results[BASELINE]["summary"]["total_return_pct"]
    )
    cohort_baseline_return = float(
        cohort_results[BASELINE]["summary"]["total_return_pct"]
    )
    result["capital_release_attribution"] = {}
    for variant in variants:
        dynamic_return = float(dynamic_results[variant]["summary"]["total_return_pct"])
        cohort_return = float(cohort_results[variant]["summary"]["total_return_pct"])
        dynamic_accepted_ids = {
            str(entry["candidate_id"])
            for entry in dynamic_results[variant]["accepted_entries"]
        }
        pure_exit_effect = cohort_return - cohort_baseline_return
        reallocation_effect = dynamic_return - cohort_return
        baseline_cohort_effect = cohort_baseline_return - dynamic_baseline_return
        total_effect = dynamic_return - dynamic_baseline_return
        result["capital_release_attribution"][variant] = {
            "total_dynamic_portfolio_effect_pp": round(total_effect, 4),
            "fixed_baseline_entry_cohort_exit_effect_pp": round(pure_exit_effect, 4),
            "capital_reallocation_and_replacement_effect_pp": round(
                reallocation_effect, 4
            ),
            "baseline_cohort_reproduction_effect_pp": round(
                baseline_cohort_effect, 4
            ),
            "decomposition_residual_pp": round(
                total_effect
                - pure_exit_effect
                - reallocation_effect
                - baseline_cohort_effect,
                8,
            ),
            "dynamic_accepted_entries": len(dynamic_accepted_ids),
            "baseline_dynamic_accepted_entries": len(baseline_entry_id_set),
            "newly_accepted_candidate_ids_vs_baseline": len(
                dynamic_accepted_ids - baseline_entry_id_set
            ),
            "baseline_candidate_ids_not_accepted": len(
                baseline_entry_id_set - dynamic_accepted_ids
            ),
            "dynamic_max_positions_rejections": dynamic_results[variant][
                "attribution"
            ]["max_positions_rejections"],
            "baseline_dynamic_max_positions_rejections": dynamic_results[BASELINE][
                "attribution"
            ]["max_positions_rejections"],
        }
    return result


def _screen(report: dict[str, Any], variant: str) -> dict[str, Any]:
    train = report["splits"]["train"]["variants"][variant]["paired_pnl_delta_pp"]
    val = report["splits"]["val"]["variants"][variant]["paired_pnl_delta_pp"]
    test = report["splits"]["test"]["variants"][variant]["paired_pnl_delta_pp"]
    checks = {
        "train_mean_non_negative": train.get("mean", float("-inf")) >= 0,
        "validation_mean_positive": val.get("mean", float("-inf")) > 0,
        "validation_ci95_low_positive": val.get("ci95_low", float("-inf")) > 0,
        "viewed_test_not_materially_worse": test.get("mean", float("-inf")) >= -0.5,
        "paired_coverage_at_least_99pct": all(
            report["splits"][split]["paired_coverage_pct"].get(profile, 0) >= 99.0
            for split in SPLITS
            for profile in ("baseline", variant)
        ),
    }
    return {
        "checks": checks,
        "selection_eligible": variant != "fixed_sl5",
        "decision_role": (
            "diagnostic_fixed_threshold_control"
            if variant == "fixed_sl5"
            else "preregistered_exit_policy"
        ),
        "passes_research_screen": all(checks.values()),
        "production_eligible": False,
        "production_blocker": "A genuinely untouched holdout is still required.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=r"D:\tmp\exit_experiments_v1")
    parser.add_argument("--output", default=None)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument(
        "--allow-holdout",
        action="store_true",
        help="explicit override; do not use during strategy development",
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        default=list(DEFAULT_VARIANTS),
        choices=list(ALL_VARIANTS),
    )
    args = parser.parse_args()
    root = _guard_development_path(Path(args.root), args.allow_holdout)
    baseline = json.loads((root / "baseline" / "experiment.json").read_text(encoding="utf-8"))
    execution_profile = baseline.get("execution_profile", "frozen_source")
    if execution_profile == "frozen_source" and not baseline.get("baseline_reproduction_ok"):
        raise RuntimeError("baseline reproduction gate failed; variant comparison is invalid")
    variants = tuple(args.variants)
    experiment_meta = {
        variant: json.loads(
            (root / variant / "experiment.json").read_text(encoding="utf-8")
        )
        for variant in (BASELINE, *variants)
    }
    if any(
        meta.get("execution_profile", "frozen_source") != execution_profile
        for meta in experiment_meta.values()
    ):
        raise RuntimeError("execution profiles differ across variants")
    index_hashes = {
        meta.get("market_regime_input", {}).get("sha256")
        if isinstance(meta.get("market_regime_input"), dict)
        else None
        for meta in experiment_meta.values()
    }
    if len(index_hashes) != 1:
        raise RuntimeError("index market-regime inputs differ across variants")
    for split in SPLITS:
        candidate_hashes = {
            meta["splits"][split]["source_match"]["common_eligible_manifest_sha256"]
            for meta in experiment_meta.values()
        }
        history_hashes = {
            meta["splits"][split]["source_match"]["history_manifest_sha256"]
            for meta in experiment_meta.values()
        }
        if len(candidate_hashes) != 1 or len(history_hashes) != 1:
            raise RuntimeError(f"candidate/history manifests differ for split {split}")
    report = {
        "version": "exit_experiment_compare.v4",
        "root": str(root),
        "execution_profile": execution_profile,
        "acceptance_rule_frozen_before_variant_results": {
            "train_mean_pnl_delta_pp": ">=0",
            "validation_mean_pnl_delta_pp": ">0",
            "validation_bootstrap_ci95_low": ">0",
            "viewed_test_mean_pnl_delta_pp": ">=-0.5",
            "paired_completed_coverage": ">=99% each split",
            "production": "never without genuinely untouched holdout",
        },
        "variants": list(variants),
        "variant_roles": {
            variant: (
                "diagnostic_fixed_threshold_control"
                if variant == "fixed_sl5"
                else "preregistered_exit_policy"
            )
            for variant in variants
        },
        "splits": {
            split: _paired_split(root, split, variants) for split in SPLITS
        },
    }
    config_path = _guard_development_path(Path(args.config), args.allow_holdout)
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")
    costs = _resolve_execution_config(config)
    report["execution_config"] = {
        "path": str(config_path),
        "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "resolved_execution_sha256": hashlib.sha256(
            json.dumps(
                costs,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    report["portfolio_comparison"] = {
        split: _paired_portfolio_split(root, split, variants, costs)
        for split in SPLITS
    }
    report["portfolio_comparison_note"] = (
        "Portfolio results are calculated on the same paired completed-candidate IDs; "
        "candidate-level and funded-portfolio metrics must not be conflated. "
        "The baseline-entry cohort decomposition is diagnostic and does not change "
        "the preregistered efficacy screen."
    )
    report["screen"] = {
        variant: _screen(report, variant) for variant in variants
    }
    output = (
        _guard_development_path(Path(args.output), args.allow_holdout)
        if args.output
        else root / "comparison.json"
    )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
