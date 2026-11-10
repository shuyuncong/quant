"""Pre-registered PIT all-A mid/large quality-value study with an MA200 gate.

This is a research harness, not a production strategy.  The design was fixed
before the first run:

* universe: PIT all-A plus delisted names with local bars;
* size band: PIT float-market-cap rank percentile 70%-100% (the largest 30%,
  i.e. mid/large caps; micro and small caps are excluded);
* filters: price >= 2, 250-day history, ADV20 >= 50M CNY;
* fundamentals: latest annual report whose ``available_date`` is on or before
  the signal; require positive parent net profit, positive revenue, positive
  ROE, and report age <= 500 calendar days;
* value: earnings yield ``EP = parent net profit / PIT float market cap``;
* quality: 0.5 * cross-sectional ROE percentile + 0.5 * net-margin percentile
  (net margin = parent net profit / revenue); no debt-ratio field is available
  in the full-A annual PIT panel, so leverage is not part of this score;
* composite: 0.5 * quality + 0.5 * value;
* selection: 50 names, sector <= 25%;
* risk switch: a PIT cap-weighted all-A index (previous-day float-cap weights)
  must close above its 200-day moving average at the signal date; otherwise
  the portfolio is moved to cash at the next open;
* execution: monthly, next open, 5% no-trade band, lot size, commission,
  stamp tax, 10bp one-way slippage (20bp stress);
* no volatility target and no portfolio-drawdown de-risking, so the MA200 gate
  is the only market-risk switch.

Controls are the same quality-value selection without the gate, and a
size-only top-50 portfolio with the gate.  The primary acceptance test is
full-period Sharpe >= 1.0, maximum drawdown <= 20%, and 20bp CAGR >= 8%.
2020-2024 is the design period; 2025-2026 is a time holdout that was observed
in earlier research and is therefore not pristine out-of-sample.
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
from smallcap_value_study import _candidate_mask, load_value

STUDY_DIR = Path(__file__).resolve().parent / "cache" / "smallcap_study"
DEFAULT_VALUE = (
    Path(__file__).resolve().parent
    / "cache"
    / "stock_pit"
    / "financials"
    / "value_annual.csv"
)

ACCEPTANCE = {
    "sharpe_min": 1.0,
    "max_drawdown_pct_max": 20.0,
    "stress_cagr_min_pct": 8.0,
}


def latest_annual_asof(value: pd.DataFrame, signal: pd.Timestamp) -> pd.DataFrame:
    """Latest annual report per symbol using only reports known by ``signal``.

    The panel is keyed by report period, not by announcement date.  This avoids
    selecting an older report that happened to be revised later.
    """

    available = value[value["available_date"] <= signal]
    if available.empty:
        return available
    return (
        available.sort_values(["symbol", "period", "available_date"])
        .groupby("symbol", as_index=False)
        .tail(1)
        .set_index("symbol")
    )


def build_cap_weighted_index(features: dict[str, Any]) -> pd.Series:
    """Build a PIT float-cap-weighted all-A index from the local panel.

    Weights are the previous day's float market caps; the return of a stock is
    included only when both the prior weight and the current return exist.  This
    is a local proxy because the repository does not carry a full CSI 300 /
    all-A index history over the study period.
    """

    close = features["close"].astype(float)
    mv = features["mv"].astype(float)
    valid = features["valid"].fillna(False)
    returns = close.pct_change().replace([np.inf, -np.inf], np.nan)
    weights = mv.shift(1).where(valid.shift(1))
    weights = weights.where(returns.notna() & weights.gt(0))
    denominator = weights.sum(axis=1).replace(0, np.nan)
    index_return = (returns.fillna(0.0) * weights.fillna(0.0)).sum(axis=1) / denominator
    index_return = index_return.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return (1.0 + index_return).cumprod().rename("market_index")


def market_gate_on(index: pd.Series, window: int = 200) -> pd.Series:
    """True when the index is above its trailing moving average."""

    ma = index.rolling(window, min_periods=window).mean()
    gate = index.gt(ma) & ma.notna()
    return gate.fillna(False).astype(bool).rename("ma_gate")


def _sector_limited(
    ranked: pd.Index,
    industry: dict[str, str],
    *,
    max_names: int,
    max_sector_weight: float,
) -> list[str]:
    max_per_sector = max(1, int(math.ceil(max_sector_weight * max_names)))
    selected: list[str] = []
    counts: dict[str, int] = {}
    for symbol in ranked:
        sector = industry.get(str(symbol), f"__{symbol}")
        if counts.get(sector, 0) >= max_per_sector:
            continue
        counts[sector] = counts.get(sector, 0) + 1
        selected.append(str(symbol))
        if len(selected) >= max_names:
            break
    return selected


def select_quality_value_targets(
    features: dict[str, Any],
    adv20: pd.DataFrame,
    industry: dict[str, str],
    value: pd.DataFrame,
    cfg: OptConfig,
    signal: pd.Timestamp,
    mode: str,
    *,
    max_report_age_days: int = 500,
) -> tuple[list[str], dict[str, Any]]:
    """Select quality-value or size-control targets for one signal."""

    mask, mv, _ = _candidate_mask(features, adv20, cfg, signal)
    candidates = mv[mask].dropna()
    stats: dict[str, Any] = {
        "signal": signal.strftime("%Y-%m-%d"),
        "mode": mode,
        "pool": int(len(candidates)),
        "fundamental_names": 0,
        "target": 0,
        "median_mv": None,
        "median_ep": None,
        "median_roe": None,
        "median_net_margin": None,
        "median_score": None,
    }
    if candidates.empty:
        return [], stats

    if mode == "size_top50":
        ranked = candidates.sort_values(ascending=False).index
        selected = _sector_limited(
            ranked,
            industry,
            max_names=cfg.max_names,
            max_sector_weight=cfg.max_sector_weight,
        )
        stats["target"] = len(selected)
        if selected:
            stats["median_mv"] = float(candidates.reindex(selected).median())
        return selected, stats

    if mode != "qv":
        raise ValueError(f"unknown mode: {mode}")

    latest = latest_annual_asof(value, signal)
    if latest.empty:
        return [], stats
    latest = latest.reindex(candidates.index)
    frame = pd.DataFrame(
        {
            "mv": candidates,
            "net_profit": pd.to_numeric(latest["net_profit"], errors="coerce"),
            "revenue": pd.to_numeric(latest["revenue"], errors="coerce"),
            "roe": pd.to_numeric(latest["roe"], errors="coerce"),
            "period": pd.to_datetime(latest["period"], errors="coerce"),
        }
    ).dropna(subset=["net_profit", "revenue", "roe", "period"])
    frame["report_age_days"] = (signal - frame["period"]).dt.days
    frame = frame[
        (frame["net_profit"] > 0)
        & (frame["revenue"] > 0)
        & (frame["roe"] > 0)
        & (frame["report_age_days"] <= max_report_age_days)
    ].copy()
    stats["fundamental_names"] = int(len(frame))
    if frame.empty:
        return [], stats

    frame["ep"] = frame["net_profit"] / frame["mv"]
    frame["net_margin"] = frame["net_profit"] / frame["revenue"]
    frame["quality"] = 0.5 * frame["roe"].rank(pct=True) + 0.5 * frame[
        "net_margin"
    ].rank(pct=True)
    frame["value"] = frame["ep"].rank(pct=True)
    frame["score"] = 0.5 * frame["quality"] + 0.5 * frame["value"]

    ranked = frame.sort_values("score", ascending=False).index
    selected = _sector_limited(
        ranked,
        industry,
        max_names=cfg.max_names,
        max_sector_weight=cfg.max_sector_weight,
    )
    stats["target"] = len(selected)
    if selected:
        chosen = frame.loc[selected]
        stats["median_mv"] = float(chosen["mv"].median())
        stats["median_ep"] = float(chosen["ep"].median())
        stats["median_roe"] = float(chosen["roe"].median())
        stats["median_net_margin"] = float(chosen["net_margin"].median())
        stats["median_score"] = float(chosen["score"].median())
    return selected, stats


class QualityValueBacktest(OptimizedBacktest):
    """Monthly quality-value engine with an optional MA200 cash gate."""

    def __init__(
        self,
        *args: Any,
        value: pd.DataFrame,
        gate: pd.Series,
        use_gate: bool,
        mode: str = "qv",
        max_report_age_days: int = 500,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.value = value
        self.gate = gate
        self.use_gate = use_gate
        self.mode = mode
        self.max_report_age_days = max_report_age_days

    def _gross_exposure(self, equity: float) -> float:
        # The MA200 gate is the only risk switch; no vol target / drawdown
        # de-risking is layered on top.
        return 1.0

    def _select(self, signal: pd.Timestamp) -> tuple[list[str], int]:
        gate_value = bool(self.gate.get(signal, False)) if self.use_gate else True
        if not gate_value:
            self.selections.append(
                {
                    "signal": signal.strftime("%Y-%m-%d"),
                    "mode": self.mode,
                    "gate": False,
                    "target": 0,
                }
            )
            return [], 0
        selected, stats = select_quality_value_targets(
            self.f,
            self.adv20,
            self.industry,
            self.value,
            self.cfg,
            signal,
            self.mode,
            max_report_age_days=self.max_report_age_days,
        )
        stats["gate"] = True
        self.selections.append(stats)
        return selected, len(selected)


def run_variant(
    features: dict[str, Any],
    adv20: pd.DataFrame,
    industry: dict[str, str],
    value: pd.DataFrame,
    gate: pd.Series,
    cfg: OptConfig,
    execution_map: dict[pd.Timestamp, pd.Timestamp],
    *,
    mode: str,
    use_gate: bool,
    max_report_age_days: int,
) -> tuple[QualityValueBacktest, dict[str, Any], pd.Series]:
    engine = QualityValueBacktest(
        features,
        adv20,
        industry,
        cfg,
        value=value,
        gate=gate,
        use_gate=use_gate,
        mode=mode,
        max_report_age_days=max_report_age_days,
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
    parser.add_argument("--out", default=str(STUDY_DIR / "quality_value"))
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--initial", type=float, default=250_000.0)
    parser.add_argument("--slippage", type=float, default=0.0010)
    parser.add_argument("--stress-slippage", type=float, default=0.0020)
    parser.add_argument("--max-report-age-days", type=int, default=500)
    parser.add_argument("--gate-window", type=int, default=200)
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

    market_index = build_cap_weighted_index(features)
    gate = market_gate_on(market_index, args.gate_window)
    signal_dates = sorted(execution_map)
    gate_on_pct = (
        float(gate.reindex(signal_dates).fillna(False).mean() * 100) if signal_dates else 0.0
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "market_index": market_index,
            "ma200": market_index.rolling(
                args.gate_window, min_periods=args.gate_window
            ).mean(),
            "gate": gate,
        }
    ).to_csv(out / "market_gate.csv")

    variants = [
        ("qv_ma200", "qv", True),
        ("qv_no_gate", "qv", False),
        ("size_top50_ma200", "size_top50", True),
    ]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "design": {
            "start": args.start,
            "end": args.end,
            "initial": args.initial,
            "universe": "PIT all-A plus delisted local bars",
            "size_band_rank_pct": [0.70, 1.00],
            "max_names": 50,
            "sector_cap": 0.25,
            "min_price": 2.0,
            "min_adv20": 50_000_000.0,
            "max_report_age_days": args.max_report_age_days,
            "quality": "0.5*ROE rank + 0.5*net-margin rank",
            "value": "parent net profit / PIT float market cap",
            "score": "0.5*quality + 0.5*value",
            "market_gate": (
                f"PIT cap-weighted all-A index > {args.gate_window}-day MA at signal"
            ),
            "risk_overlay": "none beyond the MA200 gate; 100% invested when gate is on",
            "execution": "monthly, next open, 5% band, lot/commission/stamp tax",
            "slippage": args.slippage,
            "stress_slippage": args.stress_slippage,
            "acceptance": ACCEPTANCE,
        },
        "data": {
            "symbols": len(features["close"].columns),
            "signals": len(execution_map),
            "value": _value_coverage(value, features),
            "market_index_start": str(market_index.first_valid_index().date()),
            "market_index_end": str(market_index.last_valid_index().date()),
            "gate_on_signal_pct": round(gate_on_pct, 1),
        },
        "variants": {},
        "limitations": [
            "No full CSI 300/all-A index history is present; the MA200 gate uses a locally built PIT float-cap-weighted all-A proxy.",
            "The full-A annual panel has no debt-ratio field; quality is ROE plus net margin, not a leverage-adjusted quality score.",
            "Annual reports are used; TTM fundamental variants are not part of this pre-registered test.",
            "Historical ST status is unavailable; price/ADV/history filters are the proxy.",
            "Only delisted names with local bars can trade; others are absent.",
            "2025-2026 was observed in earlier research, so it is a time holdout, not pristine out-of-sample.",
        ],
    }

    for name, mode, use_gate in variants:
        cfg = OptConfig(
            start=args.start,
            end=args.end,
            initial=args.initial,
            rank_low=1,
            rank_high=999999,
            rank_pct_low=0.70,
            rank_pct_high=1.00,
            max_names=50,
            slippage=args.slippage,
            target_vol=1.0,
            dd_derisk_1=-1.0,
            dd_derisk_2=-1.5,
        )
        engine, metrics, curve = run_variant(
            features,
            adv20,
            industry,
            value,
            gate,
            cfg,
            execution_map,
            mode=mode,
            use_gate=use_gate,
            max_report_age_days=args.max_report_age_days,
        )
        stress_cfg = OptConfig(**{**asdict(cfg), "slippage": args.stress_slippage})
        stress_engine, stress_metrics, stress_curve = run_variant(
            features,
            adv20,
            industry,
            value,
            gate,
            stress_cfg,
            execution_map,
            mode=mode,
            use_gate=use_gate,
            max_report_age_days=args.max_report_age_days,
        )
        summary["variants"][name] = {
            "mode": mode,
            "use_gate": use_gate,
            "config": asdict(cfg),
            "metrics": metrics,
            "periods": {
                "train_2020_2024": period_metrics(curve, "2020-01-02", "2024-12-31"),
                "holdout_2025_2026": period_metrics(curve, "2025-01-01", "2026-10-30"),
            },
            "stress_20bp": {
                "metrics": stress_metrics,
                "periods": {
                    "train_2020_2024": period_metrics(
                        stress_curve, "2020-01-02", "2024-12-31"
                    ),
                    "holdout_2025_2026": period_metrics(
                        stress_curve, "2025-01-01", "2026-10-30"
                    ),
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
            {
                key: metrics[key]
                for key in ("cagr_pct", "sharpe", "max_drawdown_pct", "turnover_annual_x")
            },
            "stress20",
            {
                key: stress_metrics[key]
                for key in ("cagr_pct", "sharpe", "max_drawdown_pct")
            },
            flush=True,
        )

    primary = summary["variants"]["qv_ma200"]
    checks = {
        "sharpe": bool(primary["metrics"]["sharpe"] >= ACCEPTANCE["sharpe_min"]),
        "max_drawdown": bool(
            abs(primary["metrics"]["max_drawdown_pct"])
            <= ACCEPTANCE["max_drawdown_pct_max"]
        ),
        "stress_cagr": bool(
            primary["stress_20bp"]["metrics"]["cagr_pct"]
            >= ACCEPTANCE["stress_cagr_min_pct"]
        ),
    }
    summary["decision"] = {
        "variant": "qv_ma200",
        "verdict": "accept" if all(checks.values()) else "reject",
        "checks": checks,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print("decision", summary["decision"], "wrote", out, flush=True)


if __name__ == "__main__":
    main()
