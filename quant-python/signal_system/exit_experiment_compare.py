"""Compare exit experiment variants on paired completed candidate IDs."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


VARIANTS = ("baseline", "zero_axis_confirm_2", "timeout_ma_break")
SPLITS = ("train", "val", "test")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


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


def _paired_split(root: Path, split: str) -> dict[str, Any]:
    by_variant = {
        variant: {
            str(row["candidate_id"]): row
            for row in _load_jsonl(root / variant / f"candidates_{split}.jsonl")
        }
        for variant in VARIANTS
    }
    ids_by_variant = {variant: set(rows) for variant, rows in by_variant.items()}
    paired_ids = set.intersection(*ids_by_variant.values())
    baseline = by_variant["baseline"]
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
            for variant in VARIANTS
        },
        "variants": {},
    }
    for variant in VARIANTS[1:]:
        rows = by_variant[variant]
        pnl_delta = [
            float(rows[key]["trade_pnl_pct"]) - float(baseline[key]["trade_pnl_pct"])
            for key in sorted(paired_ids)
        ]
        efficiency_delta = [
            (
                float(rows[key]["trade_pnl_pct"])
                - float(baseline[key]["trade_pnl_pct"])
            )
            / float(baseline[key]["mfe_common_60"])
            for key in sorted(paired_ids)
            if baseline[key].get("mfe_common_60") not in (None, 0)
        ]
        post20_pairs = [
            (
                float(rows[key]["post_exit_20d"]),
                float(baseline[key]["post_exit_20d"]),
            )
            for key in sorted(paired_ids)
            if rows[key].get("post_exit_20d") is not None
            and baseline[key].get("post_exit_20d") is not None
        ]
        post20_delta = [variant_value - base_value for variant_value, base_value in post20_pairs]
        holding_delta = [
            float(rows[key]["holding_bars"]) - float(baseline[key]["holding_bars"])
            for key in sorted(paired_ids)
        ]
        changed_pnl_delta = [value for value in pnl_delta if abs(value) > 1e-9]
        exit_transitions = Counter(
            f"{baseline[key].get('exit_reason')}->{rows[key].get('exit_reason')}"
            for key in paired_ids
        )
        group_deltas: dict[str, list[float]] = defaultdict(list)
        for key in paired_ids:
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
            "exit_reason_counts": dict(Counter(rows[key].get("exit_reason") for key in paired_ids)),
            "exit_reason_transitions": dict(exit_transitions),
            "by_regime_signal_type": {
                key: _bootstrap_mean_ci(values)
                for key, values in sorted(group_deltas.items())
            },
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
        "passes_research_screen": all(checks.values()),
        "production_eligible": False,
        "production_blocker": "A genuinely untouched holdout is still required.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=r"D:\tmp\exit_experiments_v1")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    baseline = json.loads((root / "baseline" / "experiment.json").read_text(encoding="utf-8"))
    execution_profile = baseline.get("execution_profile", "frozen_source")
    if execution_profile == "frozen_source" and not baseline.get("baseline_reproduction_ok"):
        raise RuntimeError("baseline reproduction gate failed; variant comparison is invalid")
    experiment_meta = {
        variant: json.loads(
            (root / variant / "experiment.json").read_text(encoding="utf-8")
        )
        for variant in VARIANTS
    }
    if any(
        meta.get("execution_profile", "frozen_source") != execution_profile
        for meta in experiment_meta.values()
    ):
        raise RuntimeError("execution profiles differ across variants")
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
        "version": "exit_experiment_compare.v1",
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
        "splits": {split: _paired_split(root, split) for split in SPLITS},
    }
    report["screen"] = {
        variant: _screen(report, variant) for variant in VARIANTS[1:]
    }
    output = Path(args.output).expanduser().resolve() if args.output else root / "comparison.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
