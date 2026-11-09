"""Bootstrap confidence intervals and multiple-testing checks.

The comparison report has 20 fixed variants.  A raw Sharpe ranking is not
evidence: the best variant can win by chance, and the benchmark/TSMOM gap may
have a wide confidence interval.  This module adds:

* stationary block bootstrap CI for annualized Sharpe;
* paired bootstrap for the benchmark minus TSMOM Sharpe difference;
* deflated Sharpe ratio (Bailey & Lopez de Prado) using the number of trials;
* White-style reality check across variants (max mean return).

Research-only; no production parameters are changed.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_CURVES = Path("docs/backtest-results/2026-11-05-simple-vs-etf/curves.csv")
DEFAULT_OUT = Path("D:/tmp/strategy_statistics")
TRADING_DAYS = 252
N_BOOT = 10_000
BLOCK = 20
SEED = 20261105
EULER_GAMMA = 0.5772156649015329


def _returns_from_curve(curve: pd.Series) -> pd.Series:
    return curve.astype(float).pct_change().dropna()


def annualized_sharpe(returns: np.ndarray | pd.Series) -> float:
    values = np.asarray(returns, dtype=float)
    if len(values) < 2:
        return float("nan")
    std = float(np.std(values, ddof=1))
    if std <= 0:
        return float("nan")
    return float(np.mean(values) / std * math.sqrt(TRADING_DAYS))


def stationary_indices(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap indices, wrapped circularly."""
    if n <= 0:
        return np.empty(0, dtype=int)
    if block < 1:
        raise ValueError("block must be >= 1")
    out: list[int] = []
    while len(out) < n:
        start = int(rng.integers(0, n))
        length = int(rng.geometric(1.0 / block))
        out.extend(int((start + offset) % n) for offset in range(length))
    return np.asarray(out[:n], dtype=int)


def bootstrap_sharpe_ci(returns: pd.Series, *, n_boot: int = N_BOOT,
                        block: int = BLOCK, seed: int = SEED) -> dict[str, Any]:
    values = np.asarray(returns, dtype=float)
    rng = np.random.default_rng(seed)
    samples = np.empty(n_boot, dtype=float)
    for index in range(n_boot):
        idx = stationary_indices(len(values), block, rng)
        samples[index] = annualized_sharpe(values[idx])
    samples = samples[np.isfinite(samples)]
    point = annualized_sharpe(values)
    low, high = np.percentile(samples, [2.5, 97.5])
    return {
        "sharpe": round(point, 4),
        "ci95_low": round(float(low), 4),
        "ci95_high": round(float(high), 4),
        "bootstrap_mean": round(float(np.mean(samples)), 4),
        "bootstrap_std": round(float(np.std(samples, ddof=1)), 4),
        "n_boot": int(len(samples)),
        "block": block,
    }


def paired_sharpe_diff(a: pd.Series, b: pd.Series, *, n_boot: int = N_BOOT,
                       block: int = BLOCK, seed: int = SEED) -> dict[str, Any]:
    """Bootstrap Sharpe(a) - Sharpe(b) with the same resampled dates."""
    frame = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    av = frame["a"].to_numpy(dtype=float)
    bv = frame["b"].to_numpy(dtype=float)
    rng = np.random.default_rng(seed)
    samples = np.empty(n_boot, dtype=float)
    for index in range(n_boot):
        idx = stationary_indices(len(av), block, rng)
        samples[index] = annualized_sharpe(av[idx]) - annualized_sharpe(bv[idx])
    samples = samples[np.isfinite(samples)]
    point = annualized_sharpe(av) - annualized_sharpe(bv)
    low, high = np.percentile(samples, [2.5, 97.5])
    p_value = 2.0 * min(float(np.mean(samples <= 0.0)), float(np.mean(samples >= 0.0)))
    return {
        "sharpe_diff": round(float(point), 4),
        "ci95_low": round(float(low), 4),
        "ci95_high": round(float(high), 4),
        "p_value_two_sided": round(min(1.0, p_value), 4),
        "prob_diff_gt_zero": round(float(np.mean(samples > 0.0)), 4),
        "n_boot": int(len(samples)),
        "block": block,
    }


def deflated_sharpe(returns: pd.Series, *, n_trials: int,
                    trial_sharpes: list[float] | None = None) -> dict[str, Any]:
    """Deflated Sharpe ratio using the observed daily Sharpe and trial count.

    Trial Sharpes are annualized; they are converted to daily units before
    computing the expected maximum.  The formula follows Bailey & Lopez de
    Prado (2014); it assumes independent trials, so the result is a lower bound
    when variants are correlated.
    """
    values = np.asarray(returns, dtype=float)
    if len(values) < 3:
        return {"deflated_sharpe": None}
    sr = annualized_sharpe(values) / math.sqrt(TRADING_DAYS)
    std = float(np.std(values, ddof=1))
    if std <= 0 or not math.isfinite(sr):
        return {"deflated_sharpe": None}
    centered = values - np.mean(values)
    skew = float(np.mean(centered ** 3) / std ** 3)
    kurt = float(np.mean(centered ** 4) / std ** 4)
    if trial_sharpes and len(trial_sharpes) >= 2:
        trial_daily = np.asarray(trial_sharpes, dtype=float) / math.sqrt(TRADING_DAYS)
        variance = float(np.var(trial_daily, ddof=1))
    else:
        variance = 1.0 / max(len(values) - 1, 1)
    normal = NormalDist()
    n = max(int(n_trials), 2)
    expected_max = math.sqrt(max(variance, 0.0)) * (
        (1.0 - EULER_GAMMA) * normal.inv_cdf(1.0 - 1.0 / n)
        + EULER_GAMMA * normal.inv_cdf(1.0 - 1.0 / (n * math.e))
    )
    denominator = math.sqrt(max(1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2, 1e-12))
    z = (sr - expected_max) * math.sqrt(len(values) - 1) / denominator
    return {
        "deflated_sharpe": round(float(normal.cdf(z)), 4),
        "observed_daily_sharpe": round(sr, 6),
        "expected_max_daily_sharpe": round(float(expected_max), 6),
        "n_trials": n,
        "skew": round(skew, 4),
        "kurtosis": round(kurt, 4),
        "sample_days": len(values),
    }


def reality_check(returns: pd.DataFrame, *, n_boot: int = N_BOOT,
                  block: int = BLOCK, seed: int = SEED) -> dict[str, Any]:
    """White-style max-mean reality check across variants.

    The null is that every variant has zero expected daily return.  Columns are
    centered by their own mean, then the same stationary bootstrap indices are
    applied to all columns.  The p-value is the share of resampled maxima that
    reach the observed maximum mean.
    """
    frame = returns.dropna(how="any")
    values = frame.to_numpy(dtype=float)
    observed = float(np.max(np.mean(values, axis=0)))
    centered = values - np.mean(values, axis=0, keepdims=True)
    rng = np.random.default_rng(seed)
    maxima = np.empty(n_boot, dtype=float)
    for index in range(n_boot):
        idx = stationary_indices(len(centered), block, rng)
        maxima[index] = float(np.max(np.mean(centered[idx], axis=0)))
    p_value = float(np.mean(maxima >= observed))
    return {
        "best_variant": str(frame.columns[int(np.argmax(np.mean(values, axis=0)))]),
        "best_mean_daily_return": round(observed, 6),
        "p_value": round(p_value, 4),
        "n_variants": int(values.shape[1]),
        "n_boot": int(n_boot),
        "block": block,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--curves", default=str(DEFAULT_CURVES))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--n-boot", type=int, default=N_BOOT)
    parser.add_argument("--block", type=int, default=BLOCK)
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    curves = pd.read_csv(args.curves, parse_dates=["date"]).set_index("date").sort_index()
    returns = curves.apply(_returns_from_curve)
    returns = returns.dropna(how="any")

    benchmark = "benchmark_etf_v5.1_40_35_10"
    tsmom = "tsmom_12_1_3asset_vt10"
    if benchmark not in returns.columns or tsmom not in returns.columns:
        raise SystemExit("curves must contain benchmark and tsmom_12_1_3asset_vt10")

    trial_sharpes = [annualized_sharpe(returns[column]) for column in returns.columns]
    summary: dict[str, Any] = {
        "period": {"start": str(returns.index[0].date()), "end": str(returns.index[-1].date())},
        "n_days": int(len(returns)),
        "n_variants": int(returns.shape[1]),
        "bootstrap": {
            "method": "stationary block bootstrap",
            "n_boot": args.n_boot,
            "block": args.block,
            "seed": SEED,
        },
        "sharpe": {
            "benchmark": bootstrap_sharpe_ci(returns[benchmark], n_boot=args.n_boot, block=args.block),
            "tsmom": bootstrap_sharpe_ci(returns[tsmom], n_boot=args.n_boot, block=args.block),
        },
        "paired_diff_benchmark_minus_tsmom": paired_sharpe_diff(
            returns[benchmark], returns[tsmom], n_boot=args.n_boot, block=args.block,
        ),
        "deflated_sharpe": {
            benchmark: deflated_sharpe(returns[benchmark], n_trials=len(returns.columns),
                                       trial_sharpes=trial_sharpes),
            tsmom: deflated_sharpe(returns[tsmom], n_trials=len(returns.columns),
                                   trial_sharpes=trial_sharpes),
        },
        "reality_check": reality_check(returns, n_boot=args.n_boot, block=args.block),
        "trial_sharpes": {
            column: round(annualized_sharpe(returns[column]), 4)
            for column in returns.columns
        },
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    bench_ci = summary["sharpe"]["benchmark"]
    tsmom_ci = summary["sharpe"]["tsmom"]
    diff = summary["paired_diff_benchmark_minus_tsmom"]
    dsr_b = summary["deflated_sharpe"][benchmark]
    dsr_t = summary["deflated_sharpe"][tsmom]
    rc = summary["reality_check"]
    lines = [
        "# Benchmark vs TSMOM: bootstrap and multiple-testing",
        "",
        f"- Period: {summary['period']['start']} → {summary['period']['end']} ({summary['n_days']} days)",
        f"- Variants in the comparison: {summary['n_variants']}",
        f"- Bootstrap: stationary block, {args.n_boot:,} resamples, block {args.block}",
        "",
        "## Sharpe confidence intervals",
        "",
        "| Strategy | Sharpe | 95% CI low | 95% CI high | Bootstrap SE |",
        "|---|---:|---:|---:|---:|",
        f"| ETF benchmark | {bench_ci['sharpe']:.3f} | {bench_ci['ci95_low']:.3f} | {bench_ci['ci95_high']:.3f} | {bench_ci['bootstrap_std']:.3f} |",
        f"| TSMOM 3 asset + 10% vol | {tsmom_ci['sharpe']:.3f} | {tsmom_ci['ci95_low']:.3f} | {tsmom_ci['ci95_high']:.3f} | {tsmom_ci['bootstrap_std']:.3f} |",
        "",
        "## Paired difference (benchmark − TSMOM)",
        "",
        f"- Sharpe difference: **{diff['sharpe_diff']:.3f}**",
        f"- 95% CI: [{diff['ci95_low']:.3f}, {diff['ci95_high']:.3f}]",
        f"- P(diff > 0): {diff['prob_diff_gt_zero']:.3f}; two-sided bootstrap p-value: {diff['p_value_two_sided']:.3f}",
        "",
        "## Multiple testing",
        "",
        f"- Deflated Sharpe — benchmark: **{dsr_b['deflated_sharpe']:.3f}** (expected max daily SR {dsr_b['expected_max_daily_sharpe']:.4f}, trials {dsr_b['n_trials']})",
        f"- Deflated Sharpe — TSMOM: **{dsr_t['deflated_sharpe']:.3f}** (expected max daily SR {dsr_t['expected_max_daily_sharpe']:.4f}, trials {dsr_t['n_trials']})",
        f"- Reality check (max mean across {rc['n_variants']} variants): p = **{rc['p_value']:.3f}**; best = `{rc['best_variant']}`",
        "",
        "## Reading",
        "",
        "- Overlapping CIs do not prove equality; the paired difference is the direct test.",
        "- The deflated Sharpe is a multiple-testing adjustment, not a guarantee; it assumes independent trials and is conservative when variants are correlated.",
        "- The reality check p-value is for the best mean return across all variants, not for the benchmark/TSMOM pair.",
        "- Research-only; no production change.",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in {"trial_sharpes"}}, ensure_ascii=False, indent=2))
    print(f"wrote {out / 'summary.json'}")
    print(f"wrote {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
