"""Backtest simple strategy archetypes against the canonical ETF benchmark.

The benchmark is the current runtime rule set: core 40% + satellite <=35% +
backfill 35%, 10% volatility target, weekly, no crash overlay.  All variants use
the same ETF cache, period, execution model and cost model as the benchmark:

* signal at the last actual trading day of the week/month;
* next trading day open fill, sells before buys;
* 3 bp commission, 5 CNY minimum, 5 bp slippage, 100-share lots;
* no stamp duty on ETFs, no leverage, no shorting.

Variants are deliberately small and fixed; this is a comparison harness, not a
parameter search.  Outputs are research-only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import etf_extended_study as study
from etf_rotation.backtest import run_panels
from etf_rotation.engine import load_open_panel, load_panel

SATELLITE = study.SATELLITE
GOLD, BOND, MARKET = study.GOLD, study.BOND, study.MARKET
ALL = [*SATELLITE, GOLD, BOND, MARKET]
TREND_3 = [MARKET, GOLD, BOND]
DEFAULT_START = "2020-01-02"
DEFAULT_END = "2026-10-30"
OPEN_PANEL: pd.DataFrame | None = None


def _liquid(universe: list[str], close: pd.DataFrame, day: pd.Timestamp) -> list[str]:
    """Require a live quote and the same 20-day ADV floor as the benchmark."""
    if OPEN_PANEL is None:
        return universe
    close_row = close.loc[day, universe].astype(float)
    open_row = OPEN_PANEL.loc[day, universe].astype(float)
    adv = panels["adv20"].loc[day, universe].astype(float)
    mask = (close_row > 0) & (open_row > 0) & (adv >= study.ADV_MIN)
    return [symbol for symbol in universe if bool(mask.get(symbol, False))]


def _equal(weights: dict[str, float], vol_target: float | None,
           panels: dict[str, pd.DataFrame], day: pd.Timestamp) -> dict[str, float]:
    if not weights:
        return {}
    if vol_target is not None:
        scale = study.vol_scale_for(panels, day, weights, vol_target)
        if scale < 1.0:
            return {symbol: weight * scale for symbol, weight in weights.items()}
    return weights


def make_tsmom(
    universe: list[str],
    *,
    use_ma200: bool = False,
    vol_target: float | None = None,
) -> Callable[..., dict[str, Any]]:
    def signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
        day = as_of
        selected_universe = _liquid(universe, close, day)
        momentum = panels["mom"].loc[day, selected_universe].astype(float)
        passed = momentum[momentum > 0].dropna()
        if use_ma200:
            price = close.loc[day, universe]
            ma = panels["ma200"].loc[day, universe]
            passed = passed[price.reindex(passed.index) > ma.reindex(passed.index)]
        weights = {symbol: 1.0 / len(passed) for symbol in passed.index} if len(passed) else {}
        return {"target_weights": _equal(weights, vol_target, panels, day)}

    return signal


def make_dual(
    universe: list[str],
    *,
    top_n: int,
    vol_target: float | None = None,
) -> Callable[..., dict[str, Any]]:
    def signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
        day = as_of
        selected_universe = _liquid(universe, close, day)
        momentum = panels["mom"].loc[day, selected_universe].astype(float).dropna()
        positive = momentum[momentum > 0].sort_values(ascending=False).head(top_n)
        if len(positive):
            weights = {symbol: 1.0 / len(positive) for symbol in positive.index}
        else:
            bond_momentum = panels["mom"].loc[day, BOND]
            weights = {BOND: 1.0} if pd.notna(bond_momentum) and bond_momentum > 0 else {}
        return {"target_weights": _equal(weights, vol_target, panels, day)}

    return signal


def make_reversal(
    universe: list[str],
    *,
    top_n: int,
    market_filter: bool = True,
    vol_target: float | None = None,
) -> Callable[..., dict[str, Any]]:
    def signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
        day = as_of
        if market_filter:
            market_price = float(close[MARKET].loc[day])
            market_ma = float(panels["csi_ma200"].loc[day])
            if not (np.isfinite(market_ma) and market_price > market_ma):
                return {"target_weights": {}}
        selected_universe = _liquid(universe, close, day)
        ret_5 = (
            close.loc[day, selected_universe].astype(float)
            / close.shift(5).loc[day, selected_universe].astype(float)
            - 1.0
        )
        ret_5 = ret_5.replace([np.inf, -np.inf], np.nan).dropna()
        picked = ret_5.nsmallest(top_n)
        weights = {symbol: 1.0 / len(picked) for symbol in picked.index} if len(picked) else {}
        return {"target_weights": _equal(weights, vol_target, panels, day)}

    return signal


def make_lowvol(
    universe: list[str],
    *,
    top_n: int,
    vol_target: float | None = None,
) -> Callable[..., dict[str, Any]]:
    def signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
        day = as_of
        selected_universe = _liquid(universe, close, day)
        vol = panels["vol60"].loc[day, selected_universe].astype(float).dropna()
        picked = vol.nsmallest(top_n)
        weights = {symbol: 1.0 / len(picked) for symbol in picked.index} if len(picked) else {}
        return {"target_weights": _equal(weights, vol_target, panels, day)}

    return signal


def wrap_target(target_fn: Callable[[pd.Timestamp], dict[str, float]]) -> Callable[..., dict[str, Any]]:
    def signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
        return {"target_weights": target_fn(as_of)}

    return signal


def run_variant(
    name: str,
    signal_fn: Callable[..., dict[str, Any]],
    rebalance: str,
    close: pd.DataFrame,
    open_: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    start: str,
    end: str,
) -> tuple[dict[str, Any], pd.Series]:
    result = run_panels(
        close,
        open_,
        volume,
        start=start,
        end=end,
        rebalance=rebalance,
        signal_fn=signal_fn,
    )
    metrics = {
        "variant": name,
        "rebalance": rebalance,
        **{
            key: result[key]
            for key in (
                "total_return_pct",
                "cagr_pct",
                "vol_pct",
                "sharpe",
                "max_drawdown_pct",
                "avg_exposure",
                "daily_win_rate_pct",
                "trade_win_rate_pct",
                "trade_payoff",
                "closed_rounds",
                "trades",
                "turnover_x",
            )
        },
        "yearly": result["yearly"],
    }
    curve = pd.Series(
        {pd.Timestamp(row["date"]): row["equity"] for row in result["curve"]}
    ).sort_index()
    return metrics, curve


def write_report(
    output_dir: Path,
    *,
    meta: dict[str, Any],
    rows: list[dict[str, Any]],
    curves: dict[str, pd.Series],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps({"meta": meta, "results": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    curve_frame = pd.DataFrame(curves)
    curve_frame.index.name = "date"
    curve_frame.to_csv(output_dir / "curves.csv", float_format="%.4f")

    lines = [
        "# Simple strategies vs canonical ETF benchmark",
        "",
        f"- Period: {meta['start']} → {meta['end']}",
        f"- Costs: commission 3bp (min 5 CNY), slippage 5bp, lot 100, no stamp duty",
        f"- Benchmark: {meta['benchmark']}",
        "",
        "| Variant | Rebalance | CAGR | Sharpe | Max DD | Trade win | Closed rounds | Daily win | Exposure | Turnover |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| `{variant}` | {rebalance} | {cagr:.2f}% | {sharpe:.2f} | {dd:.2f}% | "
            "{trade_win}% | {rounds} | {daily_win}% | {exposure:.2f} | {turnover:.1f}x |".format(
                variant=row["variant"],
                rebalance=row["rebalance"],
                cagr=row["cagr_pct"],
                sharpe=row["sharpe"],
                dd=row["max_drawdown_pct"],
                trade_win=row["trade_win_rate_pct"],
                rounds=row["closed_rounds"],
                daily_win=row["daily_win_rate_pct"],
                exposure=row["avg_exposure"],
                turnover=row["turnover_x"],
            )
        )
    lines.extend(
        [
            "",
            "Notes:",
            "- `trade_win` is per closed round; `closed_rounds` is the sample size.",
            "- `daily_win` is the share of positive days; it is not a trade win rate.",
            "- The benchmark uses the canonical 40/35 + backfill 10% weekly rule set.",
            "- TSMOM/dual momentum use 12−1 momentum; Faber uses price > MA200.",
            "- Reversal uses the worst 5-day return; low-vol uses the lowest 60-day vol.",
            "- No parameter sweep: these are fixed, pre-registered variants.",
            "",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("D:/tmp/simple_strategy_compare"))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    args = parser.parse_args(argv)

    close, volume = load_panel()
    open_ = load_open_panel()
    dates = close.loc[pd.Timestamp(args.start) : pd.Timestamp(args.end)].index
    global panels, OPEN_PANEL
    panels = study.build_panels(close, volume)
    OPEN_PANEL = open_
    signals = study.build_signals(close, study.SATELLITE, panels, dates)

    def benchmark_signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
        weights, _ = study.make_target(
            signals[as_of],
            panels,
            as_of,
            max_picks=1,
            target_vol=0.10,
            breadth=True,
            crash_overlay=False,
            use_market_gate=True,
            sat_max=0.35,
            core_mode="winner",
            core_w=0.40,
            backfill=True,
        )
        return {"target_weights": weights}

    variants: list[tuple[str, Callable[..., dict[str, Any]], str]] = [
        ("benchmark_etf_v5.1_40_35_10", benchmark_signal, "W"),
        ("benchmark_etf_v5.1_40_35_10_M", benchmark_signal, "M"),
        ("benchmark_etf_v5.1_40_35_10_D", benchmark_signal, "D"),
        ("tsmom_12_1_3asset", make_tsmom(TREND_3), "M"),
        ("tsmom_12_1_3asset_W", make_tsmom(TREND_3), "W"),
        ("tsmom_12_1_3asset_D", make_tsmom(TREND_3), "D"),
        ("tsmom_12_1_3asset_vt10", make_tsmom(TREND_3, vol_target=0.10), "M"),
        ("tsmom_12_1_3asset_vt10_W", make_tsmom(TREND_3, vol_target=0.10), "W"),
        ("tsmom_12_1_3asset_vt10_D", make_tsmom(TREND_3, vol_target=0.10), "D"),
        ("tsmom_12_1_all", make_tsmom(ALL), "M"),
        ("tsmom_12_1_all_vt10", make_tsmom(ALL, vol_target=0.10), "M"),
        ("tsmom_ma200_3asset", wrap_target(study.faber_targets(close, panels, TREND_3, vol_target=None)), "M"),
        ("tsmom_ma200_all", wrap_target(study.faber_targets(close, panels, ALL, vol_target=None)), "M"),
        ("dual_momentum_gem_vt10", wrap_target(study.gem_targets(close, panels, vol_target=0.10)), "M"),
        ("dual_momentum_top3_vt10", make_dual(ALL, top_n=3, vol_target=0.10), "M"),
        ("reversal_5d_top10", make_reversal(ALL, top_n=10, market_filter=True), "W"),
        ("reversal_5d_top20", make_reversal(ALL, top_n=20, market_filter=True), "W"),
        ("reversal_5d_top10_no_filter", make_reversal(ALL, top_n=10, market_filter=False), "W"),
        ("lowvol_60d_top10", make_lowvol(ALL, top_n=10), "M"),
        ("lowvol_60d_top20", make_lowvol(ALL, top_n=20), "M"),
    ]

    rows: list[dict[str, Any]] = []
    curves: dict[str, pd.Series] = {}
    for name, signal_fn, rebalance in variants:
        metrics, curve = run_variant(
            name,
            signal_fn,
            rebalance,
            close,
            open_,
            volume,
            start=args.start,
            end=args.end,
        )
        rows.append(metrics)
        curves[name] = curve
        print(
            f"{name:32s} CAGR {metrics['cagr_pct']:7.2f}%  "
            f"Sharpe {metrics['sharpe']:5.2f}  DD {metrics['max_drawdown_pct']:7.2f}%  "
            f"trade win {metrics['trade_win_rate_pct']:5.1f}%  "
            f"turn {metrics['turnover_x']:5.1f}x"
        )

    meta = {
        "start": args.start,
        "end": args.end,
        "benchmark": "canonical ETF 40/35 + backfill, 10% vol, weekly",
        "universe": len(ALL),
        "costs": {
            "commission_pct": 0.0003,
            "minimum_commission": 5.0,
            "slippage_pct": 0.0005,
            "lot": 100,
            "stamp_duty": 0.0,
        },
        "variants": [name for name, _, _ in variants],
    }
    write_report(args.out, meta=meta, rows=rows, curves=curves)
    print(f"wrote {args.out / 'summary.json'}")
    print(f"wrote {args.out / 'report.md'}")
    print(f"wrote {args.out / 'curves.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
