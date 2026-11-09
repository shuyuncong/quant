"""V4/V5 execution stress test for the canonical ETF rules.

V4/V5 gaps in the runtime review are execution assumptions, not signal bugs:

* 1% ADV per order (V4) — the canonical backtest only uses ADV as a pool filter.
* ETF premium/discount, bid-ask spread, partial fills (V5) — not in the cache.

This script does not pretend those data exist.  It runs explicit scenarios:
1% ADV cap, extra spread, buy premium / sell discount, 50% partial fill, and a
combined stress case.  All scenarios use the same signals, period and cost model
as the canonical benchmark; only execution changes.  Results are research-only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import etf_extended_study as study
from etf_rotation.backtest import run_panels
from etf_rotation.engine import load_open_panel, load_panel

DEFAULT_START = "2020-01-02"
DEFAULT_END = "2026-10-30"
TREND_3 = [study.MARKET, study.GOLD, study.BOND]


def _tsmom_signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
    """12-1 absolute momentum on the 3 low-correlation assets, 10% vol target."""
    day = pd.Timestamp(as_of)
    momentum = close.shift(21) / close.shift(252) - 1.0
    passed = [symbol for symbol in TREND_3 if float(momentum[symbol].loc[day]) > 0]
    weights = {symbol: 1.0 / len(passed) for symbol in passed} if passed else {}
    if weights:
        scale = study.vol_scale_for({"rets": close.pct_change()}, day, weights, 0.10)
        if scale < 1.0:
            weights = {symbol: weight * scale for symbol, weight in weights.items()}
    return {"target_weights": weights}


def _benchmark_signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
    from etf_rotation.engine import compute_signal

    signal = compute_signal(close, volume, as_of=as_of)
    return {"target_weights": signal["target_weights"]}


SCENARIOS: list[dict[str, Any]] = [
    {"label": "baseline", "kwargs": {}},
    {"label": "adv_1pct", "kwargs": {"max_participation": 0.01}},
    {"label": "spread_5bp", "kwargs": {"extra_slippage_bps": 5}},
    {"label": "spread_10bp", "kwargs": {"extra_slippage_bps": 10}},
    {"label": "spread_20bp", "kwargs": {"extra_slippage_bps": 20}},
    {"label": "premium_discount_10bp", "kwargs": {"buy_premium_bps": 10, "sell_discount_bps": 10}},
    {"label": "partial_50pct", "kwargs": {"fill_ratio": 0.5}},
    {
        "label": "combined_stress",
        "kwargs": {
            "max_participation": 0.01,
            "fill_ratio": 0.5,
            "buy_premium_bps": 10,
            "sell_discount_bps": 10,
            "extra_slippage_bps": 10,
        },
    },
]


def _run_one(close: pd.DataFrame, open_: pd.DataFrame, volume: pd.DataFrame, *,
             signal_fn: Callable[..., dict[str, Any]], start: str, end: str,
             initial: float, rebalance: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    result = run_panels(
        close, open_, volume,
        start=start, end=end, initial=initial,
        rebalance=rebalance, signal_fn=signal_fn, **kwargs,
    )
    return result


def _cache_signals(close: pd.DataFrame, volume: pd.DataFrame, *,
                   start: str, end: str, rebalance: str,
                   signal_fn: Callable[..., dict[str, Any]]) -> dict[pd.Timestamp, dict[str, float]]:
    """Compute each rebalance target once; all stress scenarios reuse it."""
    from etf_rotation.backtest import rebalance_marks

    dates = close.loc[pd.Timestamp(start):pd.Timestamp(end)].index
    cache: dict[pd.Timestamp, dict[str, float]] = {}
    for day in sorted(rebalance_marks(dates, rebalance)):
        signal = signal_fn(close, volume, as_of=day)
        cache[pd.Timestamp(day)] = {
            symbol: float(weight)
            for symbol, weight in signal["target_weights"].items()
            if float(weight) > 0
        }
    return cache


def _cached_fn(cache: dict[pd.Timestamp, dict[str, float]]) -> Callable[..., dict[str, Any]]:
    def signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
        day = pd.Timestamp(as_of)
        if day not in cache:
            raise KeyError(f"missing cached signal for {day.date()}")
        return {"target_weights": cache[day]}

    return signal


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path("D:/tmp/etf_execution_stress")))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--initial", type=float, default=250_000.0)
    parser.add_argument("--spread-file", default="D:/tmp/etf_microstructure/half_spread.csv")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    close, volume = load_panel()
    open_ = load_open_panel()

    scenarios = [dict(scenario) for scenario in SCENARIOS]
    spread_path = Path(args.spread_file)
    if spread_path.exists():
        empirical_spread = pd.read_csv(spread_path, index_col=0)
        empirical_spread.index = pd.to_datetime(empirical_spread.index)
        empirical_spread = empirical_spread.reindex(close.index).ffill().fillna(0.0)
        scenarios.append({
            "label": "empirical_spread_proxy",
            "kwargs": {"spread_panel": empirical_spread},
        })
        print(f"loaded empirical half-spread: {spread_path}", flush=True)

    strategies = {
        "benchmark_etf_v5.1_40_35_10": {"signal": _benchmark_signal, "rebalance": "W"},
        "tsmom_12_1_3asset_vt10": {"signal": _tsmom_signal, "rebalance": "M"},
    }
    caches: dict[str, dict[pd.Timestamp, dict[str, float]]] = {}
    for strategy, spec in strategies.items():
        print(f"caching signals: {strategy} ({spec['rebalance']}) ...", flush=True)
        caches[strategy] = _cache_signals(
            close, volume, start=args.start, end=args.end,
            rebalance=spec["rebalance"], signal_fn=spec["signal"],
        )

    results: list[dict[str, Any]] = []
    for strategy, spec in strategies.items():
        signal_fn = _cached_fn(caches[strategy])
        for scenario in scenarios:
            label = scenario["label"]
            kwargs = scenario["kwargs"]
            print(f"{strategy} / {label} ...", flush=True)
            result = _run_one(
                close, open_, volume, signal_fn=signal_fn,
                start=args.start, end=args.end, initial=args.initial,
                rebalance=spec["rebalance"], kwargs=kwargs,
            )
            execution = result.get("execution", {})
            results.append({
                "strategy": strategy,
                "scenario": label,
                "initial": args.initial,
                "start": result["start"],
                "end": result["end"],
                "cagr_pct": result["cagr_pct"],
                "sharpe": result["sharpe"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "trade_win_rate_pct": result["trade_win_rate_pct"],
                "trade_payoff": result["trade_payoff"],
                "closed_rounds": result["closed_rounds"],
                "trades": result["trades"],
                "turnover_x": result["turnover_x"],
                "avg_exposure": result["avg_exposure"],
                "execution": execution,
            })

    payload = {
        "period": {"start": args.start, "end": args.end},
        "initial": args.initial,
        "data_status": {
            "premium_discount": "historical NAV/price data available separately; not applied as a constant cost",
            "bid_ask": "empirical high-low spread proxy available; not a quote",
            "partial_fill": "unavailable; scenario is a fill-ratio assumption",
            "pit_delisted_universe": "SSE historical scale available; SZSE historical list unavailable; current 47 are current-listed",
        },
        "scenarios": [
            {
                "label": scenario["label"],
                "kwargs": {
                    key: value for key, value in scenario["kwargs"].items()
                    if key != "spread_panel"
                },
                "spread_panel": "empirical_spread_proxy" if "spread_panel" in scenario["kwargs"] else None,
            }
            for scenario in scenarios
        ],
        "results": results,
    }
    (out / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# ETF execution stress test (V4/V5)",
        "",
        f"- Period: {args.start} → {args.end}",
        f"- Initial capital: {args.initial:,.0f} CNY",
        "- Signals and rebalance dates are identical across scenarios; only execution changes.",
        "- Premium/discount and empirical spread proxy use fetched historical data; partial fill remains **assumption-based**.",
        "",
        "| Strategy | Scenario | CAGR | Sharpe | Max DD | Trades | Turnover | 1% ADV caps | Partial fills | Unfilled notional |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results:
        execution = row["execution"]
        lines.append(
            f"| {row['strategy']} | {row['scenario']} | {row['cagr_pct']:.2f}% | "
            f"{row['sharpe']:.2f} | {row['max_drawdown_pct']:.2f}% | {row['trades']} | "
            f"{row['turnover_x']:.1f}x | {execution.get('capped_orders', 0)} | "
            f"{execution.get('partial_fills', 0)} | {execution.get('unfilled_notional', 0):,.0f} |"
        )
    lines += [
        "",
        "## Data gaps",
        "",
        "- **PIT delisted/liquidated ETF universe:** SSE historical scale is available; SZSE historical list is not; current 47 are current-listed. Survivorship bias remains.",
        "- **ETF premium/discount:** historical NAV/price data is available separately; it is a diagnostic, not a constant cost on top of market prices.",
        "- **Bid-ask spread:** no quote data; `empirical_spread_proxy` uses a Corwin-Schultz high-low estimator, which is not a quote; extra-slippage scenarios remain assumptions.",
        "- **Partial fill:** no order-book/queue data; the 50% fill-ratio scenario is an assumption.",
        "",
        "## Reading the result",
        "",
        "- `adv_1pct` shows whether the 1% ADV cap binds at the tested capital. At 250k/500k on this liquid pool it may not bind; that is a finding, not a failure.",
        "- `capped_orders` counts orders reduced by the ADV cap; `unfilled_notional` is the notional not executed because of ADV/partial-fill caps.",
        "- Capped residuals are not carried forward; the next weekly rebalance corrects the position. This is a conservative proxy for partial fills.",
        "- Results are in-sample research, not live trading evidence.",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out / 'summary.json'}")
    print(f"wrote {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
