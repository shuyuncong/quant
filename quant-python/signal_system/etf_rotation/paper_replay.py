"""Historical replay of the ETF paper ledger.

V2 in the runtime review: the canonical backtest is not the same thing as the
`plan_paper` / `fill_paper` ledger.  This module replays the ledger semantics on
historical data:

* signal at the last actual trading day of each week;
* plan at that close, using the ledger's last marked prices;
* fill at the next trading day open, sells before buys;
* mark to market at each close;
* same 3bp commission, 5 CNY minimum, 5bp slippage, 100-share lots.

The replay is pure: it calls `_build_pending_plan` / `_apply_pending_plan`
directly and never writes the live paper ledger.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .backtest import INITIAL, rebalance_marks, run_panels
from .engine import compute_signal, load_open_panel, load_panel
from .service import _apply_pending_plan, _build_pending_plan, _paper_default

DEFAULT_START = "2019-01-02"
DEFAULT_END = "2026-10-30"


def _metrics(curve: list[tuple[pd.Timestamp, float]], *, initial: float,
             orders: list[dict[str, Any]], exposures: list[float]) -> dict[str, Any]:
    series = pd.Series(dict(curve)).sort_index()
    returns = series.pct_change().dropna()
    years = (series.index[-1] - series.index[0]).days / 365.25
    total = series.iloc[-1] / initial - 1.0
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 else float("nan")
    std = float(returns.std())
    turnover = sum(float(order.get("notional") or 0.0) for order in orders) / initial
    return {
        "start": str(series.index[0].date()),
        "end": str(series.index[-1].date()),
        "initial": initial,
        "final_equity": round(float(series.iloc[-1]), 2),
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "vol_pct": round(std * math.sqrt(252) * 100, 2),
        "sharpe": round(float(returns.mean()) / std * math.sqrt(252), 2) if std > 0 else None,
        "max_drawdown_pct": round(float((series / series.cummax() - 1.0).min()) * 100, 2),
        "avg_exposure": round(float(np.mean(exposures)), 2) if exposures else None,
        "orders": len(orders),
        "turnover_x": round(turnover, 2),
        "curve": [
            {"date": day.strftime("%Y-%m-%d"), "equity": round(float(value), 5)}
            for day, value in series.items()
        ],
    }


def _compare(paper: dict[str, Any], canonical: dict[str, Any]) -> dict[str, Any]:
    paper_curve = {row["date"]: float(row["equity"]) for row in paper["curve"]}
    canonical_curve = {row["date"]: float(row["equity"]) for row in canonical["curve"]}
    dates = sorted(set(paper_curve) & set(canonical_curve))
    if len(dates) < 2:
        return {"points": len(dates)}
    paper_values = np.array([paper_curve[day] for day in dates], dtype=float)
    canonical_values = np.array([canonical_curve[day] for day in dates], dtype=float)
    relative = paper_values / canonical_values - 1.0
    paper_returns = paper_values[1:] / paper_values[:-1] - 1.0
    canonical_returns = canonical_values[1:] / canonical_values[:-1] - 1.0
    diff = paper_returns - canonical_returns
    tracking_error = float(np.std(diff) * math.sqrt(252.0) * 100.0)
    return {
        "points": len(dates),
        "paper_total_return_pct": paper["total_return_pct"],
        "canonical_total_return_pct": canonical["total_return_pct"],
        "total_return_diff_pp": round(paper["total_return_pct"] - canonical["total_return_pct"], 4),
        "cagr_diff_pp": round(paper["cagr_pct"] - canonical["cagr_pct"], 4),
        "sharpe_diff": round((paper["sharpe"] or 0.0) - (canonical["sharpe"] or 0.0), 4),
        "max_drawdown_diff_pp": round(paper["max_drawdown_pct"] - canonical["max_drawdown_pct"], 4),
        "final_equity_diff": round(paper["final_equity"] - canonical["curve"][-1]["equity"], 2),
        "max_abs_relative_equity_diff_pct": round(float(np.max(np.abs(relative))) * 100.0, 4),
        "mean_abs_relative_equity_diff_pct": round(float(np.mean(np.abs(relative))) * 100.0, 4),
        "tracking_error_pct": round(tracking_error, 4),
    }


def replay_paper(close: pd.DataFrame, open_: pd.DataFrame, volume: pd.DataFrame, *,
                 start: str = DEFAULT_START, end: str | None = None,
                 initial: float = INITIAL,
                 signal_fn=compute_signal) -> dict[str, Any]:
    """Replay plan/fill semantics and return ledger metrics plus a parity report."""
    stop = pd.Timestamp(end) if end else close.index[-1]
    dates = close.loc[pd.Timestamp(start):stop].index
    if len(dates) < 30:
        raise ValueError(f"replay 样本过短: {len(dates)} 个交易日")
    marks = rebalance_marks(dates, "W")
    close_ffill = close.ffill()

    ledger = _paper_default()
    ledger.update({
        "initialized": True,
        "capital": round(float(initial), 2),
        "cash": round(float(initial), 2),
        "positions": {},
        "orders": [],
        "nav_history": [],
        "as_of": None,
    })
    curve: list[tuple[pd.Timestamp, float]] = []
    exposures: list[float] = []
    pending_plan = None

    for index, day in enumerate(dates):
        if pending_plan is not None:
            prices: dict[str, float] = {}
            target_symbols = {
                str(row.get("symbol") or "")
                for row in pending_plan.get("target", [])
                if isinstance(row, dict)
            }
            held_symbols = {str(symbol) for symbol in ledger.get("positions", {})}
            for symbol in target_symbols | held_symbols:
                if not symbol:
                    continue
                price = float(open_[symbol].loc[day]) if symbol in open_.columns else float("nan")
                if math.isfinite(price) and price > 0:
                    prices[symbol] = price
            ledger = _apply_pending_plan(
                ledger, pending_plan, prices=prices,
                day=day.strftime("%Y-%m-%d"), persist=False,
            )
            pending_plan = None

        positions = ledger.get("positions") or {}
        for symbol, position in positions.items():
            if symbol in close_ffill.columns:
                price = float(close_ffill[symbol].loc[day])
                if math.isfinite(price) and price > 0:
                    position["last_price"] = price
        market_value = sum(
            int(position.get("shares") or 0) * float(position.get("last_price") or 0)
            for position in positions.values()
        )
        cash = float(ledger.get("cash") or 0.0)
        equity = cash + market_value
        curve.append((day, equity))
        exposures.append(market_value / equity if equity > 0 else 0.0)

        if day in marks and index + 1 < len(dates):
            signal = signal_fn(close, volume, as_of=day)
            target = signal.get("target")
            if target is None:
                target = [
                    {
                        "symbol": symbol,
                        "name": symbol,
                        "weight": float(weight),
                        "price": float(close[symbol].loc[day]),
                    }
                    for symbol, weight in signal.get("target_weights", {}).items()
                    if float(weight) > 0
                ]
            snapshot = {
                "as_of": signal.get("as_of", day.strftime("%Y-%m-%d")),
                "target": target,
            }
            ledger = _build_pending_plan(ledger, snapshot)
            pending_plan = ledger["pending_plan"]

    metrics = _metrics(curve, initial=float(initial), orders=ledger["orders"], exposures=exposures)
    canonical = run_panels(
        close, open_, volume, start=start, end=end, initial=initial,
        rebalance="W", signal_fn=signal_fn,
    )
    return {
        "config": {
            "start": str(dates[0].date()),
            "end": str(dates[-1].date()),
            "initial": float(initial),
            "rebalance": "weekly last trading day, next open",
            "commission_bps": 3,
            "min_commission": 5.0,
            "slippage_bps": 5,
            "lot": 100,
        },
        "paper": metrics,
        "canonical": {
            key: canonical[key]
            for key in (
                "start", "end", "initial", "total_return_pct",
                "cagr_pct", "vol_pct", "sharpe", "max_drawdown_pct", "avg_exposure",
                "trades", "turnover_x", "curve",
            )
        },
        "parity": _compare(metrics, canonical),
    }


def run(data_dir: Any = None, *, start: str = DEFAULT_START, end: str | None = None,
        initial: float = INITIAL) -> dict[str, Any]:
    close, volume = load_panel(data_dir)
    open_ = load_open_panel(data_dir)
    return replay_paper(close, open_, volume, start=start, end=end, initial=initial)
