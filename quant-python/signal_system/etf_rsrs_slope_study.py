"""Pre-registered test of two JoinQuant-community ETF ideas against the runtime rule set.

Research only. Reads a local qfq ETF cache, never touches production config,
the paper ledger, or a broker. Every arm changes one thing against the runtime
baseline; arms, parameters and acceptance were fixed before the first run and
the parameters are the published ones, not searched.

Baseline (runtime canonical)
    core 40% gold/bond 12-1 winner, satellite <= 35% top-1 sector ETF ranked by
    12-1 momentum / 60-day vol, breadth scaling, satellite only while 510300
    closes above its MA200, 35% gold/bond backfill otherwise, 10% vol target,
    weekly signal, next-open execution, 3bp commission + 5bp slippage.

Slope x R2 arms (satellite ranking only; the candidate set is unchanged)
    S1_slope_r2_25             rank by annualised 25-day log-price slope x R2
    S1s_slope_r2_29            29-day window (sensitivity, not selectable)

RSRS arms (market gate only; ranking is unchanged)
    RSRS on 510300: beta and R2 of an 18-day OLS of high on low, beta z-scored
    over 600 days, score = z x R2 (Everbright "corrected" score). The gate
    opens when the score rises above +0.7 and closes when it falls below -0.7.
    R1_rsrs_gate               the RSRS state replaces the MA200 gate
    R2_rsrs_and_ma200          satellite needs both the MA200 gate and RSRS
    R1s_rsrs_dull              community "dulled" score z x R2^(2q), M = 700,
                               q = percentile of 18-day return vol
                               (sensitivity, not selectable)

Acceptance (all must hold against the baseline; same line as the 2026-11-11 study)
    full-period Sharpe difference > 0 with paired stationary-bootstrap
    P(diff > 0) >= 0.90; Sharpe difference >= 0 in both the design period
    (2020-2023) and the time holdout (2024-2026); maximum drawdown no more than
    1pp deeper; CAGR at 20bp slippage not lower than the baseline at 20bp.

2024-2026 has been inspected by earlier studies; it is a time holdout, not a
pristine out-of-sample period.

Usage
-----
    python etf_rsrs_slope_study.py --data-dir cache/etf_research_long \
        --out ../../docs/backtest-results/2026-11-14-etf-rsrs-slope
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from etf_extended_study import (
    MARKET,
    PARITY_TOL,
    SLIPPAGE,
    RunResult,
    StudyError,
    build_panels,
    build_signals,
    data_manifest,
    execute,
    load_panels,
    rebalance_days,
)
from etf_optimization_study import (
    DESIGN_END,
    DRAWDOWN_TOLERANCE_PP,
    END,
    HOLDOUT_START,
    PROB_THRESHOLD,
    RUNTIME_EXPECTED,
    START,
    STRESS_SLIPPAGE,
    acceptance,
    curve_metrics,
    runtime_target,
)
from strategy_statistics import bootstrap_sharpe_ci, paired_sharpe_diff

SLOPE_WINDOW = 25
SLOPE_WINDOW_SENSITIVITY = 29
RSRS_N = 18
RSRS_M = 600
RSRS_M_DULL = 700
RSRS_THRESHOLD = 0.7
SELECTABLE = {"slope", "rsrs"}

Signals = dict[pd.Timestamp, dict[str, Any]]


# ----------------------------------------------------------------------- signals


def rolling_ols(x: np.ndarray, y: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Trailing-window OLS of ``y`` on ``x``: (slope, R2), NaN until the window is full."""
    slope = np.full(len(x), np.nan)
    r2 = np.full(len(x), np.nan)
    if len(x) < window:
        return slope, r2
    xd = sliding_window_view(x, window)
    yd = sliding_window_view(y, window)
    xd = xd - xd.mean(axis=1, keepdims=True)
    yd = yd - yd.mean(axis=1, keepdims=True)
    sxx, syy, sxy = (xd * xd).sum(axis=1), (yd * yd).sum(axis=1), (xd * yd).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope[window - 1:] = sxy / sxx
        r2[window - 1:] = sxy * sxy / (sxx * syy)
    return slope, r2


def slope_r2_score(close: pd.DataFrame, window: int) -> pd.DataFrame:
    """Annualised log-price slope times R2 over the trailing ``window`` closes."""
    steps = np.arange(len(close), dtype=float)
    scores = {}
    for symbol in close.columns:
        slope, r2 = rolling_ols(steps, np.log(close[symbol].to_numpy(dtype=float)), window)
        scores[symbol] = np.expm1(slope * 250.0) * r2
    return pd.DataFrame(scores, index=close.index)


def rsrs_score(high: pd.Series, low: pd.Series, *, n: int, m: int,
               close: pd.Series | None = None) -> pd.Series:
    """RSRS standard score times R2; with ``close`` the R2 weight is vol-dulled."""
    beta, r2 = rolling_ols(low.to_numpy(dtype=float), high.to_numpy(dtype=float), n)
    beta = pd.Series(beta, index=high.index)
    weight = pd.Series(r2, index=high.index)
    zscore = (beta - beta.rolling(m).mean()) / beta.rolling(m).std(ddof=0)
    if close is not None:
        quantile = close.pct_change().rolling(n).std().rolling(m).rank(pct=True)
        weight = weight ** (2.0 * quantile)
    return zscore * weight


def hysteresis(score: pd.Series, threshold: float) -> pd.Series:
    """On above ``+threshold``, off below ``-threshold``, otherwise the previous state."""
    state = False
    states = []
    for value in score.to_numpy(dtype=float):
        if value > threshold:
            state = True
        elif value < -threshold:
            state = False
        states.append(state)
    return pd.Series(states, index=score.index)


def rerank(signals: Signals, score: pd.DataFrame) -> Signals:
    """Same candidates, ordered by ``score`` instead of momentum / vol."""
    out: Signals = {}
    for day, sig in signals.items():
        rows = [(float(score[symbol].loc[day]), symbol, mom, vol)
                for _, symbol, mom, vol in sig["candidates"]]
        missing = [row[1] for row in rows if not np.isfinite(row[0])]
        if missing:
            raise StudyError(f"{day.date()}: 打分缺失 {missing}")
        rows.sort(key=lambda row: (-row[0], row[1]))
        out[day] = {**sig, "candidates": rows}
    return out


def regate(signals: Signals, state: pd.Series, *, keep_ma200: bool) -> Signals:
    """Replace the MA200 gate with ``state``, or require both."""
    return {day: {**sig, "gate": bool(state.loc[day]) and (sig["gate"] or not keep_ma200)}
            for day, sig in signals.items()}


def load_high_low(data_dir: Path, symbol: str, index: pd.Index) -> tuple[pd.Series, pd.Series]:
    frame = pd.read_pickle(data_dir / f"{symbol}_qfq.pkl")
    frame = frame[frame["close"].gt(0)].copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    frame = frame.set_index("datetime").sort_index().reindex(index)
    if frame[["high", "low"]].isna().any().any():
        raise StudyError(f"{symbol}: high/low 在行情日历上有缺口")
    return frame["high"], frame["low"]


# ------------------------------------------------------------------- diagnostics


def gate_diagnostics(base: pd.Series, other: pd.Series) -> dict[str, Any]:
    return {
        "ma200_on_pct": round(float(base.mean()) * 100, 1),
        "arm_on_pct": round(float(other.mean()) * 100, 1),
        "agree_pct": round(float((base == other).mean()) * 100, 1),
        "ma200_flips": int((base != base.shift()).iloc[1:].sum()),
        "arm_flips": int((other != other.shift()).iloc[1:].sum()),
    }


def pick_diagnostics(base: Signals, other: Signals, days: list[pd.Timestamp]) -> dict[str, Any]:
    contested = [day for day in days if len(base[day]["candidates"]) > 1]
    changed = sum(base[day]["candidates"][0][1] != other[day]["candidates"][0][1] for day in contested)
    return {
        "rebalance_days": len(days),
        "days_with_choice": len(contested),
        "top_pick_changed": changed,
        "top_pick_changed_pct": round(changed / len(contested) * 100, 1) if contested else None,
    }


# -------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--data-dir", default="cache/etf_research_long")
    parser.add_argument("--out", default="cache/etf_rsrs_slope_study")
    parser.add_argument("--n-boot", type=int, default=5000)
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    satellite, close, open_, volume = load_panels(data_dir)
    dates = close.loc[pd.Timestamp(START):pd.Timestamp(END)].index
    panels = build_panels(close, volume)
    signals = build_signals(close, satellite, panels, dates)

    high, low = load_high_low(data_dir, MARKET, close.index)
    rsrs = hysteresis(rsrs_score(high, low, n=RSRS_N, m=RSRS_M), RSRS_THRESHOLD)
    rsrs_dull = hysteresis(rsrs_score(high, low, n=RSRS_N, m=RSRS_M_DULL, close=close[MARKET]),
                           RSRS_THRESHOLD)
    slope = rerank(signals, slope_r2_score(close[list(satellite)], SLOPE_WINDOW))
    slope_long = rerank(signals, slope_r2_score(close[list(satellite)], SLOPE_WINDOW_SENSITIVITY))

    arm_signals: dict[str, tuple[str, Signals]] = {
        "baseline": ("baseline", signals),
        "S1_slope_r2_25": ("slope", slope),
        "S1s_slope_r2_29": ("sensitivity", slope_long),
        "R1_rsrs_gate": ("rsrs", regate(signals, rsrs, keep_ma200=False)),
        "R2_rsrs_and_ma200": ("rsrs", regate(signals, rsrs, keep_ma200=True)),
        "R1s_rsrs_dull": ("sensitivity", regate(signals, rsrs_dull, keep_ma200=False)),
    }
    print(f"satellite={len(satellite)} days={len(dates)} arms={len(arm_signals)}", flush=True)

    def run(name: str, slippage: float) -> RunResult:
        target = runtime_target(arm_signals[name][1], panels)
        return execute(close, open_, dates=dates, target_fn=target, rebalance="W",
                       slippage=slippage, label=name)

    periods = {"full": (None, None), "design": (None, DESIGN_END), "holdout": (HOLDOUT_START, None)}
    results: dict[str, RunResult] = {}
    rows: dict[str, dict[str, Any]] = {}
    for name, (group, _) in arm_signals.items():
        result = run(name, SLIPPAGE)
        results[name] = result
        headline = result.metrics()
        rows[name] = {
            "group": group,
            **{period: curve_metrics(result.curve, start, end) for period, (start, end) in periods.items()},
            "yearly_pct": headline["yearly"],
            "avg_exposure": headline["avg_exposure"],
            "trades": headline["trades"],
            "turnover_x": headline["turnover_x"],
            "stress_20bp": curve_metrics(run(name, STRESS_SLIPPAGE).curve),
        }
        full = rows[name]["full"]
        print(f"{name:<18} ret {full['total_return_pct']:>7.2f}%  cagr {full['cagr_pct']:>5.2f}%  "
              f"sharpe {full['sharpe']:>5.3f}  dd {full['max_drawdown_pct']:>7.2f}%  "
              f"exp {headline['avg_exposure']:>4.2f}  turn {headline['turnover_x']:>6.2f}x  "
              f"20bp cagr {rows[name]['stress_20bp']['cagr_pct']:>5.2f}%", flush=True)

    headline = results["baseline"].metrics()
    actual = (headline["total_return_pct"], headline["sharpe"], headline["max_drawdown_pct"])
    deltas = [round(a - e, 3) for a, e in zip(actual, RUNTIME_EXPECTED)]
    parity_ok = all(abs(delta) <= PARITY_TOL for delta in deltas)
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
        row["checks"] = acceptance(row, rows["baseline"])
        row["selectable"] = row["group"] in SELECTABLE
        row["accepted"] = row["selectable"] and all(row["checks"].values())
        diff = row["vs_baseline"]["full"]
        print(f"{name:<18} dSharpe {diff['sharpe_diff']:+.3f} "
              f"[{diff['ci95_low']:+.3f},{diff['ci95_high']:+.3f}] P>0 {diff['prob_diff_gt_zero']:.2f}  "
              f"design {row['vs_baseline']['design']['sharpe_diff']:+.3f}  "
              f"holdout {row['vs_baseline']['holdout']['sharpe_diff']:+.3f}  "
              f"accepted={row['accepted']}", flush=True)

    ma200_gate = pd.Series({day: sig["gate"] for day, sig in signals.items()})
    marks = sorted(rebalance_days(dates, "W"))
    diagnostics = {
        "gate_R1_rsrs": gate_diagnostics(ma200_gate, rsrs.loc[dates]),
        "gate_R1s_rsrs_dull": gate_diagnostics(ma200_gate, rsrs_dull.loc[dates]),
        "picks_S1_slope_r2_25": pick_diagnostics(signals, slope, marks),
        "picks_S1s_slope_r2_29": pick_diagnostics(signals, slope_long, marks),
    }
    for name, values in diagnostics.items():
        print(name, values, flush=True)

    payload = {
        "meta": {
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "data_dir": str(data_dir),
            "start": str(dates[0].date()),
            "end": str(dates[-1].date()),
            "design_end": DESIGN_END,
            "holdout_start": HOLDOUT_START,
            "slippage": SLIPPAGE,
            "stress_slippage": STRESS_SLIPPAGE,
            "n_boot": args.n_boot,
            "parameters": {
                "slope_window": SLOPE_WINDOW,
                "slope_window_sensitivity": SLOPE_WINDOW_SENSITIVITY,
                "rsrs_n": RSRS_N, "rsrs_m": RSRS_M, "rsrs_m_dull": RSRS_M_DULL,
                "rsrs_threshold": RSRS_THRESHOLD, "rsrs_symbol": MARKET,
            },
            "acceptance": {
                "prob_diff_gt_zero": PROB_THRESHOLD,
                "drawdown_tolerance_pp": DRAWDOWN_TOLERANCE_PP,
                "tested_arms": sum(group in SELECTABLE for group, _ in arm_signals.values()),
            },
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "data_sha256": data_manifest(data_dir),
        },
        "parity": {"expected": RUNTIME_EXPECTED, "actual": actual, "delta": deltas,
                   "tolerance": PARITY_TOL, "ok": parity_ok},
        "arms": rows,
        "diagnostics": diagnostics,
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

    if not parity_ok:
        print("PARITY MISMATCH: runtime baseline is not reproduced")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
