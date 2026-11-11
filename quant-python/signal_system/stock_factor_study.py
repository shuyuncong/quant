"""Pre-registered long-only factor test in the PIT shell-free A-share universe.

Research only: no production config, paper ledger or broker is touched.  The
design was fixed before the first run and follows what the 2026-11-05
fundamental/momentum study found on 300 current large caps (low volatility,
1-month reversal and recent ROE surprise carried the IC; price momentum did
not), now on the survivorship-corrected all-A panel.

Universe
    PIT all-A plus delisted names with local bars; price >= 2, 250-day
    history, ADV20 >= 50M CNY; float-cap rank percentile >= 30% (shell-free).

Factors (percentile rank inside the universe on the signal day)
    lowvol     minus the 60-day standard deviation of daily returns
    reversal   minus the 20-day return
    surprise   same-period ROE change versus one year earlier, from the latest
               report with ``available_date <= signal`` and announced within
               the last 90 calendar days; missing otherwise
    composite  mean of the three ranks, a missing surprise counted as 0.5

Arms (50 names, sector <= 25%, weight capped at 1/50 so a thin signal month
holds cash instead of concentrating)
    shellfree_me     v2 baseline: 50 smallest in the 30%-50% band
    lowvol           50 highest lowvol rank
    reversal         50 highest reversal rank
    surprise         50 highest positive surprise
    composite        50 highest composite
    composite_band   composite inside the baseline's 30%-50% band (diagnostic:
                     separates the factor from the size tilt)

Engine, costs and risk overlay are the v2 ones (monthly, next open, 5% band,
10bp slippage / 20bp stress, 1% ADV participation, 10% vol target, -15%/-25%
drawdown de-risk), so selection is the only thing that changes.

Acceptance (all must hold)
    Sharpe >= 0.8, maximum drawdown <= 25%, CAGR at 20bp > 0 (the 2026-11-09
    thresholds), and against the baseline a paired stationary-bootstrap
    P(Sharpe difference > 0) >= 0.90 with a non-negative difference in both
    2020-2024 and 2025-2026.

2025-2026 was inspected by earlier studies: a time holdout, not pristine OOS.

Usage
-----
    python fetch_pit_qfq_extension.py --workers 4
    python stock_factor_study.py \
        --out ../../docs/backtest-results/2026-11-11-strategy-optimization/stock-factors
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quality_filter_study import _rebalance_map, extend_delisted_panels, extend_panels
from smallcap_optimized_study import OptConfig, OptimizedBacktest, period_metrics
from smallcap_strategy_study import Config, build_features, load_industry, load_panels
from smallcap_value_study import _candidate_mask, latest_value, load_value
from strategy_statistics import bootstrap_sharpe_ci, paired_sharpe_diff

STUDY_DIR = Path(__file__).resolve().parent / "cache" / "smallcap_study"
PIT_DIR = Path(__file__).resolve().parent / "cache" / "stock_pit"

VOL_WINDOW = 60
VOL_MIN_PERIODS = 40
REVERSAL_WINDOW = 20
SURPRISE_DAYS = 90
FACTORS = ("lowvol", "reversal", "surprise")
DESIGN = ("2020-01-02", "2024-12-31")
HOLDOUT = ("2025-01-01", "2026-10-30")
BASELINE = "shellfree_me"
# arm -> (universe, ranking mode)
ARMS: dict[str, tuple[str, str]] = {
    BASELINE: ("band", "size"),
    "lowvol": ("wide", "lowvol"),
    "reversal": ("wide", "reversal"),
    "surprise": ("wide", "surprise"),
    "composite": ("wide", "composite"),
    "composite_band": ("band", "composite"),
}
UNIVERSES = {"wide": (0.30, 1.0), "band": (0.30, 0.50)}
ACCEPTANCE = {
    "sharpe_min": 0.8,
    "max_drawdown_pct_max": 25.0,
    "stress_cagr_min_pct": 0.0,
    "prob_diff_gt_zero": 0.90,
}


def load_surprise(path: Path) -> pd.DataFrame:
    """Report panel with ``roe_yoy``: period ROE minus the same period a year earlier."""

    frame = load_value(path)
    frame["roe"] = pd.to_numeric(frame["roe"], errors="coerce")
    prior = frame[["symbol", "period", "roe"]].rename(columns={"roe": "roe_prior"})
    prior["period"] = prior["period"] + pd.DateOffset(years=1)
    frame = frame.merge(prior, on=["symbol", "period"], how="left")
    frame["roe_yoy"] = frame["roe"] - frame["roe_prior"]
    return frame


def build_factors(close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    returns = close.pct_change(fill_method=None)
    return {
        "vol": returns.rolling(VOL_WINDOW, min_periods=VOL_MIN_PERIODS).std(),
        "ret": close / close.shift(REVERSAL_WINDOW) - 1.0,
    }


def score_table(
    features: dict[str, Any],
    adv20: pd.DataFrame,
    factors: dict[str, pd.DataFrame],
    surprise: pd.DataFrame,
    cfg: OptConfig,
    signal: pd.Timestamp,
) -> pd.DataFrame:
    """Factor values and in-universe percentile ranks for one signal day."""

    mask, mv, _ = _candidate_mask(features, adv20, cfg, signal)
    table = pd.DataFrame({"mv": mv[mask].dropna()})
    table["lowvol"] = -factors["vol"].loc[signal].reindex(table.index)
    table["reversal"] = -factors["ret"].loc[signal].reindex(table.index)
    latest = latest_value(surprise, signal)
    recent = latest[(signal - latest["available_date"]).dt.days <= SURPRISE_DAYS]
    table["surprise"] = recent["roe_yoy"].reindex(table.index)
    for name in FACTORS:
        table[f"{name}_r"] = table[name].rank(pct=True)
    table["composite"] = (
        table["lowvol_r"] + table["reversal_r"] + table["surprise_r"].fillna(0.5)
    ) / 3.0
    return table


def select_targets(
    table: pd.DataFrame, mode: str, industry: dict[str, str], cfg: OptConfig
) -> list[str]:
    """Rank by ``mode`` and fill up to ``max_names`` under the sector cap."""

    if table.empty:
        return []
    if mode == "size":
        ranked = table["mv"].sort_values(kind="mergesort").head(cfg.max_names * 2)
    elif mode == "surprise":
        values = table["surprise"].dropna()
        ranked = values[values > 0].sort_values(ascending=False, kind="mergesort")
    elif mode in {"lowvol", "reversal", "composite"}:
        ranked = table[mode].dropna().sort_values(ascending=False, kind="mergesort")
    else:
        raise ValueError(f"unknown mode: {mode}")

    max_per_sector = max(1, int(math.ceil(cfg.max_sector_weight * cfg.max_names)))
    selected: list[str] = []
    counts: dict[str, int] = {}
    for symbol in ranked.index:
        sector = industry.get(symbol, f"__{symbol}")
        if counts.get(sector, 0) >= max_per_sector:
            continue
        counts[sector] = counts.get(sector, 0) + 1
        selected.append(symbol)
        if len(selected) >= cfg.max_names:
            break
    return selected


class FactorBacktest(OptimizedBacktest):
    """The v2 engine with selection read from precomputed score tables."""

    def __init__(self, *args: Any, tables: dict[pd.Timestamp, pd.DataFrame], mode: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.tables = tables
        self.mode = mode

    def _select(self, signal: pd.Timestamp) -> tuple[list[str], int]:
        table = self.tables[signal]
        selected = select_targets(table, self.mode, self.industry, self.cfg)
        self.selections.append(
            {
                "signal": signal.strftime("%Y-%m-%d"),
                "pool": int(len(table)),
                "surprise_available": int(table["surprise"].notna().sum()),
                "target": len(selected),
                "median_mv": float(table["mv"].reindex(selected).median()) if selected else None,
            }
        )
        # Sizing by max_names, not by the number found: a thin month keeps cash.
        return selected, self.cfg.max_names


def information_coefficients(
    tables: dict[pd.Timestamp, pd.DataFrame], close: pd.DataFrame
) -> pd.DataFrame:
    """Spearman IC of each rank against the return to the next signal day."""

    signals = sorted(tables)
    rows: list[dict[str, Any]] = []
    for current, following in zip(signals[:-1], signals[1:]):
        table = tables[current]
        forward = close.loc[following].reindex(table.index) / close.loc[current].reindex(table.index) - 1.0
        row: dict[str, Any] = {"signal": current, "names": int(forward.notna().sum())}
        for column in ("lowvol", "reversal", "surprise", "composite"):
            pair = pd.concat([table[column], forward], axis=1).dropna().rank()
            # Pearson on ranks is Spearman; pandas' own spearman needs scipy.
            row[column] = float(pair.iloc[:, 0].corr(pair.iloc[:, 1])) if len(pair) >= 30 else np.nan
            row[f"{column}_n"] = int(len(pair))
        rows.append(row)
    return pd.DataFrame(rows).set_index("signal")


def ic_summary(ic: pd.DataFrame) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for column in ("lowvol", "reversal", "surprise", "composite"):
        values = ic[column].dropna()
        std = float(values.std()) if len(values) > 1 else 0.0
        summary[column] = {
            "months": int(len(values)),
            "mean_ic": round(float(values.mean()), 4) if len(values) else None,
            "icir": round(float(values.mean()) / std, 3) if std > 0 else None,
            "positive_share": round(float((values > 0).mean()), 3) if len(values) else None,
            "median_names": int(ic[f"{column}_n"].median()),
        }
    return summary


def universe_curve(
    close: pd.DataFrame,
    tables: dict[pd.Timestamp, pd.DataFrame],
    execution_map: dict[pd.Timestamp, pd.Timestamp],
    start: str,
    initial: float,
) -> pd.Series:
    """Costless equal-weight return of the universe, members reset monthly."""

    returns = close.pct_change(fill_method=None)
    members: pd.Index | None = None
    equity = initial
    values: dict[pd.Timestamp, float] = {}
    for date in close.index[close.index >= pd.Timestamp(start)]:
        if members is not None:
            daily = returns.loc[date].reindex(members).mean()
            if np.isfinite(daily):
                equity *= 1.0 + float(daily)
        values[date] = equity
        if date in execution_map:
            members = tables[execution_map[date]].index
    return pd.Series(values)


def acceptance(row: dict[str, Any]) -> dict[str, bool]:
    diff = row["vs_baseline"]
    checks = {
        "sharpe": row["metrics"]["sharpe"] >= ACCEPTANCE["sharpe_min"],
        "max_drawdown": abs(row["metrics"]["max_drawdown_pct"]) <= ACCEPTANCE["max_drawdown_pct_max"],
        "stress_cagr": row["stress_20bp"]["cagr_pct"] > ACCEPTANCE["stress_cagr_min_pct"],
        "beats_baseline": diff["full"]["prob_diff_gt_zero"] >= ACCEPTANCE["prob_diff_gt_zero"],
        "design_not_worse": diff["design"]["sharpe_diff"] >= 0,
        "holdout_not_worse": diff["holdout"]["sharpe_diff"] >= 0,
    }
    # numpy booleans would be written as strings by json's ``default=str``.
    return {name: bool(value) for name, value in checks.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--panels", default=str(STUDY_DIR / "panels_2018_2024.pkl"))
    parser.add_argument("--data-dir", default="cache/baostock_history_20261026")
    parser.add_argument("--index-file", default=str(STUDY_DIR / "index_399101.csv"))
    parser.add_argument("--industry-file", default=str(STUDY_DIR / "sina_all.jsonl"))
    parser.add_argument("--extension-dir", default=str(PIT_DIR / "prices_qfq"))
    parser.add_argument("--delisted-dir", default=str(PIT_DIR / "prices_delisted"))
    parser.add_argument("--reports", default=str(PIT_DIR / "financials" / "value_ttm.csv"))
    parser.add_argument("--out", default=str(STUDY_DIR / "stock_factors"))
    parser.add_argument("--start", default=DESIGN[0])
    parser.add_argument("--end", default=HOLDOUT[1])
    parser.add_argument("--initial", type=float, default=250_000.0)
    parser.add_argument("--slippage", type=float, default=0.0010)
    parser.add_argument("--stress-slippage", type=float, default=0.0020)
    parser.add_argument("--n-boot", type=int, default=5000)
    args = parser.parse_args()

    extension_dir = Path(args.extension_dir)
    if not extension_dir.exists():
        raise SystemExit(f"missing adjusted extension {extension_dir}; run fetch_pit_qfq_extension.py first")

    base_cfg = Config(start=args.start, end=args.end, min_history=250)
    panels = load_panels(Path(args.data_dir), Path(args.panels), base_cfg)
    panels = extend_panels(panels, extension_dir, list(panels["close"].columns), args.start)
    panels = extend_delisted_panels(panels, Path(args.delisted_dir))
    index = pd.read_csv(args.index_file, parse_dates=["date"]).set_index("date")["close"]
    industry = load_industry(Path(args.industry_file))
    delisted_path = PIT_DIR / "universe" / "delisted.csv"
    if delisted_path.exists():
        for code in pd.read_csv(delisted_path, dtype={"symbol": str})["symbol"]:
            industry.setdefault(str(code).zfill(6), f"__delisted_{code}")
    features = build_features(panels, index, base_cfg)
    adv20 = panels["amount"].rolling(20, min_periods=20).mean()
    execution_map = _rebalance_map(features["close"].index, base_cfg)
    surprise = load_surprise(Path(args.reports))
    factors = build_factors(features["close"])

    def config(universe: str, slippage: float) -> OptConfig:
        low, high = UNIVERSES[universe]
        return OptConfig(start=args.start, end=args.end, initial=args.initial, rank_low=1,
                         rank_high=999999, rank_pct_low=low, rank_pct_high=high, slippage=slippage)

    signals = sorted(set(execution_map.values()))
    tables = {
        universe: {
            signal: score_table(features, adv20, factors, surprise, config(universe, args.slippage), signal)
            for signal in signals
        }
        for universe in UNIVERSES
    }
    print(f"symbols={len(features['close'].columns)} signals={len(signals)} "
          f"median pool wide={int(np.median([len(t) for t in tables['wide'].values()]))} "
          f"band={int(np.median([len(t) for t in tables['band'].values()]))}", flush=True)

    def run(arm: str, slippage: float) -> tuple[FactorBacktest, dict[str, Any], pd.Series]:
        universe, mode = ARMS[arm]
        engine = FactorBacktest(features, adv20, industry, config(universe, slippage),
                                tables=tables[universe], mode=mode)
        metrics = engine.run(execution_map)
        curve = pd.Series([value for _, value in engine.equity],
                          index=pd.DatetimeIndex([date for date, _ in engine.equity]))
        return engine, metrics, curve

    periods = {"full": (args.start, args.end), "design": DESIGN, "holdout": HOLDOUT}
    rows: dict[str, dict[str, Any]] = {}
    curves: dict[str, pd.Series] = {}
    for arm in ARMS:
        engine, metrics, curve = run(arm, args.slippage)
        _, stress_metrics, _ = run(arm, args.stress_slippage)
        curves[arm] = curve
        targets = [row["target"] for row in engine.selections]
        rows[arm] = {
            "universe": ARMS[arm][0],
            "mode": ARMS[arm][1],
            "metrics": metrics,
            "periods": {name: period_metrics(curve, start, end) for name, (start, end) in periods.items()},
            "stress_20bp": {key: stress_metrics[key] for key in ("cagr_pct", "sharpe", "max_drawdown_pct")},
            "selection": {
                "mean_target": round(float(np.mean(targets)), 1),
                "months_below_max_names": int(sum(1 for value in targets if value < engine.cfg.max_names)),
                "median_mv_yi": round(float(np.nanmedian(
                    [row["median_mv"] or np.nan for row in engine.selections])) / 1e8, 1),
            },
        }
        print(f"{arm:<15} cagr {metrics['cagr_pct']:>6.2f}%  sharpe {metrics['sharpe']:>6.3f}  "
              f"dd {metrics['max_drawdown_pct']:>7.2f}%  exp {metrics['avg_exposure_pct']:>5.1f}%  "
              f"turn {metrics['turnover_annual_x']:>5.2f}x  20bp cagr {stress_metrics['cagr_pct']:>6.2f}%  "
              f"mv {rows[arm]['selection']['median_mv_yi']:>7.1f}亿", flush=True)

    base_returns = curves[BASELINE].pct_change().dropna()
    rows[BASELINE]["sharpe_ci"] = bootstrap_sharpe_ci(base_returns, n_boot=args.n_boot)
    for arm, row in rows.items():
        if arm == BASELINE:
            continue
        returns = curves[arm].pct_change().dropna()
        row["sharpe_ci"] = bootstrap_sharpe_ci(returns, n_boot=args.n_boot)
        row["vs_baseline"] = {
            name: paired_sharpe_diff(returns.loc[start:end], base_returns.loc[start:end], n_boot=args.n_boot)
            for name, (start, end) in periods.items()
        }
        row["checks"] = acceptance(row)
        row["accepted"] = all(row["checks"].values())
        diff = row["vs_baseline"]
        print(f"{arm:<15} dSharpe {diff['full']['sharpe_diff']:+.3f} "
              f"[{diff['full']['ci95_low']:+.3f},{diff['full']['ci95_high']:+.3f}] "
              f"P>0 {diff['full']['prob_diff_gt_zero']:.2f}  design {diff['design']['sharpe_diff']:+.3f}  "
              f"holdout {diff['holdout']['sharpe_diff']:+.3f}  accepted={row['accepted']}", flush=True)

    ic = information_coefficients(tables["wide"], features["close"])
    reference = universe_curve(features["close"], tables["wide"], execution_map, args.start, args.initial)
    curves["universe_equal_weight_costless"] = reference
    print("ic", json.dumps(ic_summary(ic), ensure_ascii=False), flush=True)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = {
        "design": {
            "start": args.start, "end": args.end, "design_period": DESIGN, "holdout_period": HOLDOUT,
            "initial": args.initial, "slippage": args.slippage, "stress_slippage": args.stress_slippage,
            "vol_window": VOL_WINDOW, "reversal_window": REVERSAL_WINDOW, "surprise_days": SURPRISE_DAYS,
            "universes": UNIVERSES, "arms": ARMS, "acceptance": ACCEPTANCE, "n_boot": args.n_boot,
            "engine": asdict(config("wide", args.slippage)),
        },
        "data": {
            "symbols": len(features["close"].columns),
            "signals": len(signals),
            "extension_dir": str(extension_dir),
            "reports": str(args.reports),
        },
        "arms": rows,
        "ic": ic_summary(ic),
        "universe_equal_weight_costless": {
            name: period_metrics(reference, start, end) for name, (start, end) in periods.items()
        },
        "limitations": [
            "ST history is unavailable; price/ADV/history filters are the proxy.",
            "Only delisted names with a local price file can trade; the rest are absent.",
            "Sector labels are a current Sina snapshot, not point-in-time.",
            "ROE is the reported period value; restatements are not tracked.",
            "2025-2026 was inspected earlier: a time holdout, not pristine out-of-sample.",
        ],
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                                      encoding="utf-8")
    pd.DataFrame(curves).to_csv(out / "curves.csv")
    ic.to_csv(out / "ic.csv")
    print("wrote", out, flush=True)


if __name__ == "__main__":
    main()
