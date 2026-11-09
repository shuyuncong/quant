"""Quarterly observable crowding / lifecycle review for the ETF pool.

The runtime review asks for factor-crowding and lifecycle monitoring.  Local
data cannot measure fund flows, short interest, ETF premium/discount or
positioning, so this script does **not** invent a crowding score.  It records
only observables and fixed review triggers:

* benchmark trend / drawdown / volatility;
* trend breadth (how many satellites pass the canonical trend template);
* cross-sectional momentum dispersion;
* average pairwise correlation among liquid satellites;
* median satellite ADV and liquid count;
* explicit `unavailable` flags for flow / premium / short-interest data.

Outputs are review prompts, not trading signals.  No production rule reads them.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from etf_rotation.engine import (
    ADV_MIN,
    MARKET,
    SATELLITE,
    compute_signal,
    load_panel,
)

DEFAULT_START = "2019-01-02"
DEFAULT_END = "2026-10-30"
UNAVAILABLE = {
    "fund_flow": "local cache has no ETF fund-flow data",
    "short_interest": "local cache has no securities-lending/short-interest data",
    "premium_discount": "local cache has no quote/NAV pair",
    "positioning": "local cache has no holder/positioning data",
}


def quarter_ends(index: pd.Index, start: str, end: str) -> list[pd.Timestamp]:
    dates = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    if len(dates) == 0:
        return []
    marks = pd.Series(dates, index=dates).groupby(dates.to_period("Q")).max()
    return [pd.Timestamp(value) for value in marks.tolist()]


def _metric_row(close: pd.DataFrame, volume: pd.DataFrame, day: pd.Timestamp) -> dict[str, Any]:
    satellites = [symbol for symbol in SATELLITE if symbol in close.columns]
    returns = close[satellites].pct_change()
    adv20 = (close[satellites] * volume[satellites] * 100.0).rolling(20).mean()
    liquid = [
        symbol for symbol in satellites
        if math.isfinite(float(adv20[symbol].loc[day]))
        and float(adv20[symbol].loc[day]) >= ADV_MIN
    ]
    momentum = close[satellites].shift(21) / close[satellites].shift(252) - 1.0
    mom_today = momentum.loc[day, liquid].astype(float).dropna() if liquid else pd.Series(dtype=float)
    dispersion = float(mom_today.std()) if len(mom_today) >= 2 else None
    sample = returns.loc[:day].tail(60)[liquid].dropna(how="all") if liquid else pd.DataFrame()
    if len(sample) >= 20 and sample.shape[1] >= 2:
        corr = sample.corr().to_numpy()
        upper = corr[np.triu_indices_from(corr, k=1)]
        upper = upper[np.isfinite(upper)]
        avg_corr = float(np.mean(upper)) if len(upper) else None
    else:
        avg_corr = None

    market = close[MARKET]
    market_ma200 = market.rolling(200).mean()
    market_high252 = market.rolling(252).max()
    market_ret60 = market / market.shift(60) - 1.0
    market_vol60 = market.pct_change().rolling(60).std() * math.sqrt(252.0)
    market_mom = market.shift(21) / market.shift(252) - 1.0

    try:
        signal = compute_signal(close, volume, as_of=day)
        n_trend = int(signal["n_trend"])
        gate = bool(signal["gate"])
        picks = list(signal["picks"])
    except Exception:
        n_trend = None
        gate = None
        picks = []

    sat_ret60 = close[satellites] / close[satellites].shift(60) - 1.0
    sat_vol60 = returns[satellites].rolling(60).std() * math.sqrt(252.0)
    sat_ret60_median = float(sat_ret60.loc[day].median()) if satellites else None
    sat_vol60_median = float(sat_vol60.loc[day].median()) if satellites else None
    median_adv = float(adv20.loc[day, liquid].median()) if liquid else None

    def finite(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    drawdown = finite(market.loc[day] / market_high252.loc[day] - 1.0) if finite(market_high252.loc[day]) else None
    row = {
        "quarter": str(pd.Timestamp(day).to_period("Q")),
        "date": day.strftime("%Y-%m-%d"),
        "gate": gate,
        "picks": picks,
        "n_trend": n_trend,
        "n_liquid": len(liquid),
        "median_adv20": median_adv,
        "avg_pairwise_corr_60d": avg_corr,
        "momentum_dispersion_12_1": dispersion,
        "benchmark_drawdown_252d": drawdown,
        "benchmark_momentum_12_1": finite(market_mom.loc[day]),
        "benchmark_ret60": finite(market_ret60.loc[day]),
        "benchmark_vol60": finite(market_vol60.loc[day]),
        "satellite_ret60_median": sat_ret60_median,
        "satellite_vol60_median": sat_vol60_median,
        "premium_discount": None,
        "fund_flow": None,
        "short_interest": None,
        "positioning": None,
        "data_status": UNAVAILABLE,
    }
    triggers = {
        "drawdown_gt_10pct": bool(drawdown is not None and drawdown <= -0.10),
        "corr_high_ge_0_65": bool(avg_corr is not None and avg_corr >= 0.65),
        "dispersion_low_le_0_10": bool(dispersion is not None and dispersion <= 0.10),
        "trend_narrow_le_2": bool(n_trend is not None and n_trend <= 2),
        "liquidity_thin_le_10": bool(len(liquid) <= 10),
        "benchmark_momentum_negative": bool(
            row["benchmark_momentum_12_1"] is not None
            and row["benchmark_momentum_12_1"] <= 0
        ),
        "crowding_data_unavailable": True,
    }
    row["triggers"] = triggers
    row["trigger_count"] = sum(1 for value in triggers.values() if value)
    row["market_trigger_count"] = sum(
        1 for key, value in triggers.items()
        if value and key != "crowding_data_unavailable"
    )
    return row


def review(close: pd.DataFrame, volume: pd.DataFrame, *,
           start: str = DEFAULT_START, end: str = DEFAULT_END) -> dict[str, Any]:
    rows = [_metric_row(close, volume, day) for day in quarter_ends(close.index, start, end)]
    return {
        "period": {"start": start, "end": end},
        "data_status": UNAVAILABLE,
        "thresholds": {
            "drawdown_gt_10pct": "benchmark drawdown from 252d high <= -10%",
            "corr_high_ge_0_65": "average pairwise 60d correlation >= 0.65",
            "dispersion_low_le_0_10": "cross-sectional 12-1 momentum std <= 0.10",
            "trend_narrow_le_2": "canonical trend candidates <= 2",
            "liquidity_thin_le_10": "liquid satellites (ADV20 >= 50M) <= 10",
            "benchmark_momentum_negative": "CSI300 12-1 momentum <= 0",
            "crowding_data_unavailable": "always true; not a market signal",
        },
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path("D:/tmp/etf_crowding_review")))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    close, volume = load_panel()
    payload = review(close, volume, start=args.start, end=args.end)
    (out / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = payload["rows"]
    frame = pd.DataFrame([
        {
            key: row[key]
            for key in (
                "quarter", "date", "gate", "n_trend", "n_liquid", "median_adv20",
                "avg_pairwise_corr_60d", "momentum_dispersion_12_1",
                "benchmark_drawdown_252d", "benchmark_momentum_12_1",
                "trigger_count", "market_trigger_count",
            )
        }
        for row in rows
    ])
    frame.to_csv(out / "quarters.csv", index=False)

    lines = [
        "# ETF quarterly crowding / lifecycle review",
        "",
        f"- Period: {args.start} → {args.end}",
        "- Observable metrics only; no fund-flow, short-interest, premium/discount or positioning data.",
        "- Triggers are review prompts, not trading signals.",
        "",
        "| Quarter | Date | Gate | n_trend | n_liquid | Median ADV20 | Avg corr 60d | Mom dispersion | Bench DD 252d | Bench mom 12-1 | Market triggers |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        def fmt(value: Any, digits: int = 3) -> str:
            return "—" if value is None else f"{float(value):.{digits}f}"

        median_adv = "—" if row["median_adv20"] is None else f"{row['median_adv20'] / 1e6:.1f}M"
        lines.append(
            f"| {row['quarter']} | {row['date']} | {row['gate']} | {row['n_trend']} | "
            f"{row['n_liquid']} | {median_adv} | {fmt(row['avg_pairwise_corr_60d'])} | "
            f"{fmt(row['momentum_dispersion_12_1'])} | {fmt(row['benchmark_drawdown_252d'])} | "
            f"{fmt(row['benchmark_momentum_12_1'])} | {row['market_trigger_count']} |"
        )
    lines += [
        "",
        "## Trigger definitions",
        "",
    ]
    for key, description in payload["thresholds"].items():
        lines.append(f"- `{key}`: {description}")
    lines += [
        "",
        "## Data gaps",
        "",
    ]
    for key, description in UNAVAILABLE.items():
        lines.append(f"- **{key}**: {description}.")
    lines += [
        "",
        "Use this table to ask *why* breadth/correlation/liquidity changed, not to auto-trade the trigger. "
        "A high trigger count is a prompt for manual review, not a sell signal.",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out / 'summary.json'}")
    print(f"wrote {out / 'quarters.csv'}")
    print(f"wrote {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
