"""Pre-registered optimization study for the ETF runtime rule set (40/35 + backfill).

Research only. Reads a local qfq ETF cache, never touches production config,
the paper ledger, or a broker. Every arm changes one thing against the runtime
baseline; the design below was fixed before the first run.

Baseline (runtime canonical)
    core 40% gold/bond 12-1 winner, satellite <= 35% top-1 sector ETF with
    breadth scaling, 35% gold/bond backfill when no satellite is held, 10% vol
    target, weekly signal, next-open execution, 3bp commission + 5bp slippage.

Attribution arms (descriptive, no verdict)
    core_only                  no satellite, no backfill
    core_sat                   core + satellite, no backfill
    core_backfill              core + permanent backfill, no satellite

Optimization arms
    E1_cash_sleeve             idle weight above a 2% buffer held in 511880
    E2_backfill_trend          backfill leg only when its own 12-1 momentum > 0
    E3_band2                   2%-of-equity no-trade band
    E3s_band5                  5% band (sensitivity, not selectable)
    E4_package                 E1 + E2 + E3

Nasdaq QDII arms (513100, CNY prices, so FX and premium are in the series)
    N1_idle10 / N2_idle20      fixed weight funded from idle cash
    N3_carve10 / N4_carve20    fixed weight, the strategy scaled to (1 - w)
    N5_tsmom20                 20% from idle cash only while 12-1 momentum > 0

Acceptance (all must hold against the baseline)
    full-period Sharpe difference > 0 with paired stationary-bootstrap
    P(diff > 0) >= 0.90; Sharpe difference >= 0 in both the design period
    (2020-2023) and the time holdout (2024-2026); maximum drawdown no more than
    1pp deeper; CAGR at 20bp slippage not lower than the baseline at 20bp.

2024-2026 has been inspected by earlier studies; it is a time holdout, not a
pristine out-of-sample period.

Usage
-----
    python fetch_etf_research_cache.py --out cache/etf_research_long
    python etf_optimization_study.py --data-dir cache/etf_research_long \
        --out ../../docs/backtest-results/2026-11-11-strategy-optimization/etf
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from etf_extended_study import (
    BOND,
    GOLD,
    INITIAL,
    PARITY_TOL,
    SLIPPAGE,
    RunResult,
    StudyError,
    build_panels,
    build_signals,
    data_manifest,
    execute,
    load_panels,
    make_target,
)
from strategy_statistics import bootstrap_sharpe_ci, paired_sharpe_diff

CASH_ETF = "511880"
NASDAQ_ETF = "513100"
START, END = "2020-01-02", "2026-10-30"
DESIGN_END = "2023-12-31"
HOLDOUT_START = "2024-01-01"
TARGET_VOL = 0.10
CASH_BUFFER = 0.02
STRESS_SLIPPAGE = 0.0020
RUNTIME_EXPECTED = (90.10, 1.20, -10.19)
PROB_THRESHOLD = 0.90
DRAWDOWN_TOLERANCE_PP = 1.0

TargetFn = Callable[[pd.Timestamp], dict[str, float]]


# ----------------------------------------------------------------------- targets


def runtime_target(signals: dict[pd.Timestamp, dict[str, Any]], panels: dict[str, pd.DataFrame],
                   **overrides: Any) -> TargetFn:
    """The runtime rule set; ``overrides`` change exactly the named arguments."""
    params: dict[str, Any] = dict(
        max_picks=1, target_vol=TARGET_VOL, breadth=True, crash_overlay=False,
        core_w=0.40, sat_max=0.35, backfill=True,
    )
    params.update(overrides)

    def target(day: pd.Timestamp) -> dict[str, float]:
        weights, _ = make_target(signals[day], panels, day, **params)
        return weights

    return target


def with_cash_sleeve(target_fn: TargetFn, *, symbol: str = CASH_ETF,
                     buffer: float = CASH_BUFFER) -> TargetFn:
    """Park the weight left idle by the strategy, minus a cash buffer, in ``symbol``."""

    def target(day: pd.Timestamp) -> dict[str, float]:
        weights = dict(target_fn(day))
        idle = 1.0 - sum(weights.values()) - buffer
        if idle > 0:
            weights[symbol] = weights.get(symbol, 0.0) + idle
        return weights

    return target


def with_overlay(target_fn: TargetFn, *, symbol: str, weight: float, mode: str,
                 momentum: pd.Series | None = None, buffer: float = CASH_BUFFER) -> TargetFn:
    """Add a fixed-weight overlay asset.

    ``idle``  funds the overlay from unused cash, capped so the book stays
              within ``1 - buffer``; the strategy weights are untouched.
    ``carve`` scales the strategy to ``1 - weight`` and gives ``weight`` to the
              overlay, so gross exposure never increases beyond the strategy's.
    ``momentum`` (optional) switches the overlay off while it is not positive.
    """
    if mode not in {"idle", "carve"}:
        raise ValueError(f"unknown overlay mode: {mode}")

    def target(day: pd.Timestamp) -> dict[str, float]:
        weights = dict(target_fn(day))
        if momentum is not None:
            value = float(momentum.loc[day])
            if not np.isfinite(value) or value <= 0:
                return weights
        if mode == "carve":
            weights = {s: w * (1.0 - weight) for s, w in weights.items()}
            weights[symbol] = weights.get(symbol, 0.0) + weight
            return weights
        room = 1.0 - sum(weights.values()) - buffer
        size = min(weight, room)
        if size > 0:
            weights[symbol] = weights.get(symbol, 0.0) + size
        return weights

    return target


def build_arms(signals: dict[pd.Timestamp, dict[str, Any]],
               panels: dict[str, pd.DataFrame]) -> dict[str, dict[str, Any]]:
    base = runtime_target(signals, panels)
    trend = runtime_target(signals, panels, backfill_trend_only=True)
    nasdaq_mom = panels["mom"][NASDAQ_ETF]

    def overlay(weight: float, mode: str, momentum: pd.Series | None = None) -> TargetFn:
        return with_overlay(base, symbol=NASDAQ_ETF, weight=weight, mode=mode, momentum=momentum)

    return {
        "baseline": dict(group="baseline", target=base, band=0.0),
        "core_only": dict(group="attribution", band=0.0,
                          target=runtime_target(signals, panels, sat_max=0.0, backfill=False)),
        "core_sat": dict(group="attribution", band=0.0,
                         target=runtime_target(signals, panels, backfill=False)),
        "core_backfill": dict(group="attribution", band=0.0,
                              target=runtime_target(signals, panels, sat_max=0.0)),
        "E1_cash_sleeve": dict(group="optimization", target=with_cash_sleeve(base), band=0.0),
        "E2_backfill_trend": dict(group="optimization", target=trend, band=0.0),
        "E3_band2": dict(group="optimization", target=base, band=0.02),
        "E3s_band5": dict(group="sensitivity", target=base, band=0.05),
        "E4_package": dict(group="optimization", target=with_cash_sleeve(trend), band=0.02),
        "N1_idle10": dict(group="nasdaq", target=overlay(0.10, "idle"), band=0.0),
        "N2_idle20": dict(group="nasdaq", target=overlay(0.20, "idle"), band=0.0),
        "N3_carve10": dict(group="nasdaq", target=overlay(0.10, "carve"), band=0.0),
        "N4_carve20": dict(group="nasdaq", target=overlay(0.20, "carve"), band=0.0),
        "N5_tsmom20": dict(group="nasdaq", target=overlay(0.20, "idle", nasdaq_mom), band=0.0),
    }


# ----------------------------------------------------------------------- metrics


def curve_metrics(curve: pd.Series, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Metrics on a slice, anchored on the last value before ``start``."""
    part = curve.loc[start:end] if (start or end) else curve
    if start is not None:
        before = curve.loc[:pd.Timestamp(start)].iloc[:-1] if pd.Timestamp(start) in curve.index \
            else curve.loc[:pd.Timestamp(start)]
        if len(before):
            part = pd.concat([before.iloc[-1:], part])
    if len(part) < 3:
        return {}
    rets = part.pct_change().dropna()
    years = (part.index[-1] - part.index[0]).days / 365.25
    total = float(part.iloc[-1] / part.iloc[0] - 1.0)
    std = float(rets.std())
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(((1.0 + total) ** (1.0 / years) - 1.0) * 100, 2) if years > 0 else None,
        "vol_pct": round(std * math.sqrt(252) * 100, 2),
        "sharpe": round(float(rets.mean()) / std * math.sqrt(252), 3) if std > 0 else None,
        "max_drawdown_pct": round(float((part / part.cummax() - 1.0).min()) * 100, 2),
    }


def attribute(result: RunResult, close: pd.DataFrame, open_: pd.DataFrame) -> dict[str, float]:
    """Split the run's P&L by sleeve, in percent of initial capital.

    Per symbol: overnight carry on yesterday's shares plus open-to-close on the
    shares traded at today's open.  Whatever is left of the equity change is
    fees and slippage.
    """
    holdings = result.holdings
    if holdings is None:
        raise StudyError(f"{result.label}: holdings were not recorded")
    columns = list(holdings.columns)
    day_close = close.loc[holdings.index, columns]
    day_open = open_.loc[holdings.index, columns]
    previous = holdings.shift(1).fillna(0.0)
    pnl = (previous * day_close.diff().fillna(0.0) + (holdings - previous) * (day_close - day_open)).sum()
    total = float(result.curve.iloc[-1] - INITIAL)

    def sleeve(symbols: list[str]) -> float:
        return float(pnl.reindex(symbols).fillna(0.0).sum())

    satellites = [s for s in columns if s not in {GOLD, BOND, CASH_ETF, NASDAQ_ETF}]
    parts = {
        "gold": sleeve([GOLD]),
        "bond": sleeve([BOND]),
        "satellite": sleeve(satellites),
        "cash_etf": sleeve([CASH_ETF]),
        "nasdaq": sleeve([NASDAQ_ETF]),
    }
    parts["costs"] = total - sum(parts.values())
    parts["total"] = total
    return {name: round(value / INITIAL * 100, 2) for name, value in parts.items()}


def acceptance(variant: dict[str, Any], baseline: dict[str, Any]) -> dict[str, bool]:
    diff = variant["vs_baseline"]
    return {
        "sharpe_diff_positive": diff["full"]["sharpe_diff"] > 0,
        "prob_diff_gt_zero": diff["full"]["prob_diff_gt_zero"] >= PROB_THRESHOLD,
        "design_not_worse": diff["design"]["sharpe_diff"] >= 0,
        "holdout_not_worse": diff["holdout"]["sharpe_diff"] >= 0,
        "drawdown_within_tolerance": (
            variant["full"]["max_drawdown_pct"]
            >= baseline["full"]["max_drawdown_pct"] - DRAWDOWN_TOLERANCE_PP
        ),
        "stress_cagr_not_worse": variant["stress_20bp"]["cagr_pct"] >= baseline["stress_20bp"]["cagr_pct"],
    }


# -------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--data-dir", default="cache/etf_research_long")
    parser.add_argument("--out", default="cache/etf_optimization_study")
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--n-boot", type=int, default=5000)
    parser.add_argument("--skip-parity", action="store_true")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    satellite, close, open_, volume = load_panels(data_dir, extra=(CASH_ETF, NASDAQ_ETF))
    dates = close.loc[pd.Timestamp(args.start):pd.Timestamp(args.end)].index
    for symbol in (CASH_ETF, NASDAQ_ETF):
        first = close[symbol].first_valid_index()
        if first is None or first > dates[0] - pd.Timedelta(days=400):
            raise StudyError(f"{symbol}: 历史不足以覆盖 {args.start} 之前一年")
    panels = build_panels(close, volume)
    signals = build_signals(close, satellite, panels, dates)
    arms = build_arms(signals, panels)
    print(f"satellite={len(satellite)} days={len(dates)} arms={len(arms)}", flush=True)

    def run(name: str, slippage: float, keep_holdings: bool = False) -> RunResult:
        arm = arms[name]
        return execute(close, open_, dates=dates, target_fn=arm["target"], rebalance="W",
                       slippage=slippage, label=name, band=arm["band"], keep_holdings=keep_holdings)

    periods = {
        "full": (None, None),
        "design": (None, DESIGN_END),
        "holdout": (HOLDOUT_START, None),
    }
    results: dict[str, RunResult] = {}
    rows: dict[str, dict[str, Any]] = {}
    for name, arm in arms.items():
        result = run(name, SLIPPAGE, keep_holdings=True)
        stress = run(name, STRESS_SLIPPAGE)
        results[name] = result
        headline = result.metrics()
        rows[name] = {
            "group": arm["group"],
            "band": arm["band"],
            **{period: curve_metrics(result.curve, start, end) for period, (start, end) in periods.items()},
            "yearly_pct": headline["yearly"],
            "avg_exposure": headline["avg_exposure"],
            "trades": headline["trades"],
            "turnover_x": headline["turnover_x"],
            "stress_20bp": curve_metrics(stress.curve),
            "attribution_pct": attribute(result, close, open_),
        }
        full = rows[name]["full"]
        print(f"{name:<18} ret {full['total_return_pct']:>7.2f}%  cagr {full['cagr_pct']:>5.2f}%  "
              f"sharpe {full['sharpe']:>5.3f}  dd {full['max_drawdown_pct']:>7.2f}%  "
              f"exp {headline['avg_exposure']:>4.2f}  turn {headline['turnover_x']:>6.2f}x  "
              f"20bp cagr {rows[name]['stress_20bp']['cagr_pct']:>5.2f}%", flush=True)

    baseline_metrics = results["baseline"].metrics()
    actual = (baseline_metrics["total_return_pct"], baseline_metrics["sharpe"],
              baseline_metrics["max_drawdown_pct"])
    deltas = [round(a - e, 3) for a, e in zip(actual, RUNTIME_EXPECTED)]
    default_window = (args.start, args.end) == (START, END)
    parity_ok = all(abs(delta) <= PARITY_TOL for delta in deltas) if default_window else None
    print(f"parity baseline {actual} expected {RUNTIME_EXPECTED} delta {deltas} ok={parity_ok}", flush=True)

    base_returns = results["baseline"].curve.pct_change().dropna()
    rows["baseline"]["sharpe_ci"] = bootstrap_sharpe_ci(base_returns, n_boot=args.n_boot)
    for name, row in rows.items():
        if name == "baseline":
            continue
        returns = results[name].curve.pct_change().dropna()
        row["vs_baseline"] = {
            period: paired_sharpe_diff(returns.loc[start:end], base_returns.loc[start:end],
                                       n_boot=args.n_boot)
            for period, (start, end) in periods.items()
        }
        if row["group"] == "attribution":
            continue
        row["checks"] = acceptance(row, rows["baseline"])
        row["accepted"] = all(row["checks"].values())
        diff = row["vs_baseline"]["full"]
        print(f"{name:<18} dSharpe {diff['sharpe_diff']:+.3f} "
              f"[{diff['ci95_low']:+.3f},{diff['ci95_high']:+.3f}] P>0 {diff['prob_diff_gt_zero']:.2f}  "
              f"design {row['vs_baseline']['design']['sharpe_diff']:+.3f}  "
              f"holdout {row['vs_baseline']['holdout']['sharpe_diff']:+.3f}  "
              f"accepted={row['accepted']}", flush=True)

    references = {
        symbol: {period: curve_metrics(close[symbol].loc[dates[0]:dates[-1]], start, end)
                 for period, (start, end) in periods.items()}
        for symbol in (NASDAQ_ETF, CASH_ETF, GOLD, BOND)
    }

    payload = {
        "meta": {
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "data_dir": str(data_dir),
            "start": str(dates[0].date()),
            "end": str(dates[-1].date()),
            "design_end": DESIGN_END,
            "holdout_start": HOLDOUT_START,
            "initial": INITIAL,
            "slippage": SLIPPAGE,
            "stress_slippage": STRESS_SLIPPAGE,
            "target_vol": TARGET_VOL,
            "cash_etf": CASH_ETF,
            "nasdaq_etf": NASDAQ_ETF,
            "cash_buffer": CASH_BUFFER,
            "n_boot": args.n_boot,
            "acceptance": {
                "prob_diff_gt_zero": PROB_THRESHOLD,
                "drawdown_tolerance_pp": DRAWDOWN_TOLERANCE_PP,
                "tested_arms": sum(1 for arm in arms.values() if arm["group"] in {"optimization", "nasdaq"}),
            },
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "data_sha256": data_manifest(data_dir),
        },
        "parity": {"expected": RUNTIME_EXPECTED, "actual": actual, "delta": deltas,
                   "tolerance": PARITY_TOL, "ok": parity_ok},
        "arms": rows,
        "buy_and_hold_reference": references,
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    pd.DataFrame({name: result.curve for name, result in results.items()}).to_csv(out_dir / "curves.csv")
    table = []
    for name, row in rows.items():
        diff = row.get("vs_baseline", {})
        table.append({
            "arm": name, "group": row["group"],
            **{f"{period}_{key}": row[period].get(key) for period in periods
               for key in ("cagr_pct", "sharpe", "max_drawdown_pct")},
            "avg_exposure": row["avg_exposure"], "turnover_x": row["turnover_x"],
            "stress_20bp_cagr_pct": row["stress_20bp"]["cagr_pct"],
            **{f"d_sharpe_{period}": diff.get(period, {}).get("sharpe_diff") for period in periods},
            "prob_diff_gt_zero": diff.get("full", {}).get("prob_diff_gt_zero"),
            "accepted": row.get("accepted"),
        })
    pd.DataFrame(table).to_csv(out_dir / "metrics.csv", index=False)
    print("wrote", out_dir, flush=True)

    if parity_ok is False and not args.skip_parity:
        print("PARITY MISMATCH: runtime baseline is not reproduced")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
