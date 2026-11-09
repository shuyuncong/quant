"""Pre-registered EP/ME test inside the shell-free small-cap band.

The design is fixed before looking at results:

* universe: PIT all-A plus delisted names with local bars;
* filters: price >= 2, 250-day history, ADV20 >= 50M CNY;
* size band: PIT float-market-cap rank percentile 30%-50%
  (the same shell-free band as optimization v2);
* value signal: latest available parent net profit / PIT float market cap,
  using only reports whose announcement date is on or before the signal;
  the supplied panel can be annual (``value_annual.csv``) or TTM
  (``value_ttm.csv``);
* selection: 50 names, sector <= 25%;
* variants:
  - ``shellfree_me``: 50 smallest names in the band (v2 baseline);
  - ``ep_top``: 50 highest positive EP in the band;
  - ``ep_me``: top 50% by EP, then 50 smallest by ME;
  - ``ep_top_small100``: EP within v2's smallest-100 candidate pool
    (diagnostic; isolates the factor from the pool-breadth change);
* monthly rebalance, next open, 5% no-trade band;
* 10bp one-way slippage, 1% ADV participation, commission and stamp tax;
* 10% annualized vol target, portfolio drawdown de-risk at -15% / -25%;
* no individual stop-loss.

2020-2024 is the design period; 2025-2026 is a time holdout, but it was
observed in earlier research and is therefore not pristine out-of-sample.

Usage
-----
    python smallcap_value_study.py
    python smallcap_value_study.py --out docs/backtest-results/2026-11-09-smallcap-value
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

from quality_filter_study import (
    _rebalance_map,
    extend_delisted_panels,
    extend_panels,
)
from smallcap_optimized_study import (
    OptConfig,
    OptimizedBacktest,
    period_metrics,
)
from smallcap_strategy_study import (
    Config,
    build_features,
    load_industry,
    load_panels,
)

STUDY_DIR = Path(__file__).resolve().parent / "cache" / "smallcap_study"
DEFAULT_VALUE = Path(__file__).resolve().parent / "cache" / "stock_pit" / "financials" / "value_annual.csv"


def load_value(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"symbol": str})
    frame["symbol"] = frame["symbol"].map(lambda value: str(value).zfill(6))
    frame["report_year"] = pd.to_numeric(frame["report_year"], errors="coerce")
    frame["net_profit"] = pd.to_numeric(frame["net_profit"], errors="coerce")
    frame["revenue"] = pd.to_numeric(frame["revenue"], errors="coerce")
    frame["available_date"] = pd.to_datetime(frame["available_date"], errors="coerce")
    if "period" in frame.columns:
        frame["period"] = pd.to_datetime(frame["period"], errors="coerce")
    else:
        frame["period"] = pd.to_datetime(
            frame["report_year"].astype("Int64").astype(str) + "1231",
            errors="coerce",
        )
    frame = frame.dropna(
        subset=["symbol", "report_year", "period", "available_date", "net_profit"]
    )
    frame = frame.sort_values(["symbol", "period", "available_date"])
    frame = frame.drop_duplicates(["symbol", "period"], keep="first")
    return frame.reset_index(drop=True)


def latest_value(value: pd.DataFrame, signal: pd.Timestamp) -> pd.DataFrame:
    """Latest report per symbol with ``available_date <= signal``."""

    available = value[value["available_date"] <= signal]
    if available.empty:
        return available
    if "period" not in available.columns:
        available = available.copy()
        available["period"] = pd.to_datetime(
            available["report_year"].astype(int).astype(str) + "1231",
            errors="coerce",
        )
    sort_columns = ["symbol", "available_date", "period"]
    return (
        available.sort_values(sort_columns)
        .groupby("symbol", as_index=False)
        .tail(1)
        .set_index("symbol")
    )


def earnings_yield(value_latest: pd.DataFrame, mv: pd.Series) -> pd.Series:
    """Latest reported parent net profit divided by PIT float market cap."""

    mv = mv.reindex(value_latest.index)
    ep = value_latest["net_profit"] / mv
    ep = ep.replace([np.inf, -np.inf], np.nan)
    return ep[ep > 0].dropna()


def _candidate_mask(
    features: dict[str, Any],
    adv20: pd.DataFrame,
    cfg: OptConfig,
    signal: pd.Timestamp,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    valid = features["valid"].loc[signal]
    close = features["close"].loc[signal]
    rank = features["rank"].loc[signal]
    mv = features["mv"].loc[signal]
    adv = adv20.loc[signal]
    valid_count = max(1, int(valid.sum()))
    rank_pct = rank / valid_count
    mask = (
        valid
        & (close >= cfg.min_price)
        & (adv >= cfg.min_adv)
        & (rank >= cfg.rank_low)
        & (rank <= cfg.rank_high)
        & (rank_pct >= cfg.rank_pct_low)
        & (rank_pct <= cfg.rank_pct_high)
    )
    return mask, mv, adv


def select_value_targets(
    features: dict[str, Any],
    adv20: pd.DataFrame,
    industry: dict[str, str],
    value: pd.DataFrame,
    cfg: OptConfig,
    signal: pd.Timestamp,
    mode: str,
    ep_screen: float = 0.5,
) -> tuple[list[str], dict[str, Any]]:
    """Select names for one of the pre-registered variants."""

    mask, mv, _ = _candidate_mask(features, adv20, cfg, signal)
    candidates = mv[mask].dropna()
    stats: dict[str, Any] = {
        "signal": signal.strftime("%Y-%m-%d"),
        "mode": mode,
        "pool": int(len(candidates)),
        "ep_available": 0,
        "target": 0,
        "median_ep": None,
        "median_mv": None,
    }
    if candidates.empty:
        return [], stats

    if mode == "shellfree_me":
        ranked = candidates.sort_values().head(cfg.max_names * 2)
    elif mode in {"ep_top", "ep_top_small100", "ep_me"}:
        latest = latest_value(value, signal)
        ep = earnings_yield(latest, candidates)
        stats["ep_available"] = int(len(ep))
        if ep.empty:
            return [], stats
        if mode == "ep_top_small100":
            small = candidates.sort_values().head(cfg.max_names * 2)
            ranked = ep.reindex(small.index).dropna().sort_values(ascending=False)
        elif mode == "ep_top":
            ranked = ep.sort_values(ascending=False)
        else:
            screen_size = max(1, int(math.ceil(len(ep) * ep_screen)))
            screen = ep.sort_values(ascending=False).head(screen_size)
            ranked = candidates.reindex(screen.index).dropna().sort_values()
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

    stats["target"] = len(selected)
    if selected:
        stats["median_mv"] = float(candidates.reindex(selected).median())
        if mode != "shellfree_me":
            stats["median_ep"] = float(
                earnings_yield(latest_value(value, signal), candidates)
                .reindex(selected)
                .median()
            )
    return selected, stats


class ValueBacktest(OptimizedBacktest):
    def __init__(self, *args: Any, value: pd.DataFrame, mode: str, ep_screen: float = 0.5, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.value = value
        self.mode = mode
        self.ep_screen = ep_screen

    def _select(self, signal: pd.Timestamp) -> tuple[list[str], int]:
        selected, stats = select_value_targets(
            self.f,
            self.adv20,
            self.industry,
            self.value,
            self.cfg,
            signal,
            self.mode,
            self.ep_screen,
        )
        self.selections.append(stats)
        return selected, len(selected)


def run_variant(
    features: dict[str, Any],
    adv20: pd.DataFrame,
    industry: dict[str, str],
    value: pd.DataFrame,
    cfg: OptConfig,
    execution_map: dict[pd.Timestamp, pd.Timestamp],
    mode: str,
    ep_screen: float = 0.5,
) -> tuple[ValueBacktest, dict[str, Any], pd.Series]:
    engine = ValueBacktest(
        features,
        adv20,
        industry,
        cfg,
        value=value,
        mode=mode,
        ep_screen=ep_screen,
    )
    metrics = engine.run(execution_map)
    curve = pd.Series(
        [value for _, value in engine.equity],
        index=pd.DatetimeIndex([date for date, _ in engine.equity]),
    )
    return engine, metrics, curve


def _value_coverage(value: pd.DataFrame, features: dict[str, Any]) -> dict[str, Any]:
    symbols = set(features["close"].columns)
    used = value[value["symbol"].isin(symbols)]
    return {
        "rows": len(used),
        "symbols": int(used["symbol"].nunique()),
        "current_symbols": int(
            used.loc[used["listed_status"] == "current", "symbol"].nunique()
        ),
        "delisted_symbols": int(
            used.loc[used["listed_status"] == "delisted", "symbol"].nunique()
        ),
        "min_available_date": str(used["available_date"].min().date()),
        "max_available_date": str(used["available_date"].max().date()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panels", default=str(STUDY_DIR / "panels_2018_2024.pkl"))
    parser.add_argument("--data-dir", default="cache/baostock_history_20261026")
    parser.add_argument("--index-file", default=str(STUDY_DIR / "index_399101.csv"))
    parser.add_argument("--industry-file", default=str(STUDY_DIR / "sina_all.jsonl"))
    parser.add_argument("--extension-dir", default="cache/stock_pit/prices")
    parser.add_argument("--delisted-dir", default="cache/stock_pit/prices_delisted")
    parser.add_argument("--value", default=str(DEFAULT_VALUE))
    parser.add_argument("--out", default=str(STUDY_DIR / "value_study"))
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--initial", type=float, default=250_000.0)
    parser.add_argument("--slippage", type=float, default=0.0010)
    parser.add_argument("--stress-slippage", type=float, default=0.0020)
    parser.add_argument("--ep-screen", type=float, default=0.5)
    args = parser.parse_args()

    base_cfg = Config(start=args.start, end=args.end, min_history=250)
    panels = load_panels(Path(args.data_dir), Path(args.panels), base_cfg)
    panels = extend_panels(
        panels,
        Path(args.extension_dir),
        list(panels["close"].columns),
        args.start,
    )
    panels = extend_delisted_panels(panels, Path(args.delisted_dir))
    index = pd.read_csv(args.index_file, parse_dates=["date"]).set_index("date")["close"]
    industry = load_industry(Path(args.industry_file))
    delisted_path = Path("cache/stock_pit/universe/delisted.csv")
    if delisted_path.exists():
        delisted = pd.read_csv(delisted_path, dtype={"symbol": str})
        for code in delisted["symbol"]:
            industry.setdefault(str(code).zfill(6), f"__delisted_{code}")
    features = build_features(panels, index, base_cfg)
    adv20 = panels["amount"].rolling(20, min_periods=20).mean()
    execution_map = _rebalance_map(features["close"].index, base_cfg)
    value = load_value(Path(args.value))

    variants = [
        ("shellfree_me", "shellfree_me"),
        ("ep_top", "ep_top"),
        ("ep_me", "ep_me"),
        ("ep_top_small100", "ep_top_small100"),
    ]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "design": {
            "start": args.start,
            "end": args.end,
            "initial": args.initial,
            "slippage": args.slippage,
            "stress_slippage": args.stress_slippage,
            "ep_screen": args.ep_screen,
            "rank_pct": [0.30, 0.50],
            "max_names": 50,
            "sector_cap": 0.25,
            "costs": "10bp base, 20bp stress; 3bp commission; 10bp sell stamp tax",
            "acceptance": {
                "sharpe_min": 0.8,
                "max_drawdown_pct_max": 25.0,
                "stress_cagr_min_pct": 0.0,
                "participation": 0.005,
            },
        },
        "data": {
            "symbols": len(features["close"].columns),
            "signals": len(execution_map),
            "value": _value_coverage(value, features),
        },
        "variants": {},
        "limitations": [
            "Historical PIT CSI 1000 membership is still unavailable; this uses PIT all-A rank percentile 30%-50%.",
            "EP uses the supplied value panel: value_annual.csv is the latest annual report; value_ttm.csv is trailing twelve months.",
            "ST history is unavailable; price/ADV/history filters are the proxy.",
            "Only delisted names with a local price file can trade; others are absent.",
            "2025-2026 was observed earlier, so it is a time holdout, not pristine OOS.",
        ],
    }

    for name, mode in variants:
        cfg = OptConfig(
            start=args.start,
            end=args.end,
            initial=args.initial,
            rank_low=1,
            rank_high=999999,
            rank_pct_low=0.30,
            rank_pct_high=0.50,
            slippage=args.slippage,
        )
        engine, metrics, curve = run_variant(
            features,
            adv20,
            industry,
            value,
            cfg,
            execution_map,
            mode,
            args.ep_screen,
        )
        stress_cfg = OptConfig(**{**asdict(cfg), "slippage": args.stress_slippage})
        stress_engine, stress_metrics, stress_curve = run_variant(
            features,
            adv20,
            industry,
            value,
            stress_cfg,
            execution_map,
            mode,
            args.ep_screen,
        )
        summary["variants"][name] = {
            "mode": mode,
            "config": asdict(cfg),
            "metrics": metrics,
            "periods": {
                "train_2020_2024": period_metrics(curve, "2020-01-02", "2024-12-31"),
                "holdout_2025_2026": period_metrics(curve, "2025-01-01", "2026-10-30"),
            },
            "stress_20bp": {
                "metrics": stress_metrics,
                "periods": {
                    "train_2020_2024": period_metrics(stress_curve, "2020-01-02", "2024-12-31"),
                    "holdout_2025_2026": period_metrics(stress_curve, "2025-01-01", "2026-10-30"),
                },
            },
            "selections": engine.selections,
        }
        curve.to_frame("equity").to_csv(out / f"curves_{name}.csv")
        pd.DataFrame(engine.trades).to_csv(
            out / f"trades_{name}.csv", index=False, encoding="utf-8-sig"
        )
        stress_curve.to_frame("equity").to_csv(out / f"curves_{name}_20bp.csv")
        print(
            name,
            {key: metrics[key] for key in ("cagr_pct", "sharpe", "max_drawdown_pct", "turnover_annual_x")},
            "stress20",
            {
                key: stress_metrics[key]
                for key in ("cagr_pct", "sharpe", "max_drawdown_pct")
            },
            flush=True,
        )

    best = max(
        summary["variants"],
        key=lambda name: summary["variants"][name]["metrics"]["sharpe"],
    )
    summary["best_variant"] = best
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print("best", best, "wrote", out, flush=True)


if __name__ == "__main__":
    main()
