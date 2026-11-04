"""Canonical ETF backtest.

Uses the same `engine.compute_signal` that produces live signals, so the curve
shown on the page and the signal pushed to the ledger cannot drift apart.

Execution model: weekly last actual trading day close signal, next trading day
open fill, sells before buys, 100-share lots, 3bp commission + 5bp slippage,
5 CNY minimum commission, no stamp duty, no leverage.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
import pandas as pd

from .engine import compute_signal, load_open_panel, load_panel

INITIAL = 250_000.0
COMMISSION = 0.0003
SLIPPAGE = 0.0005
MIN_COMMISSION = 5.0
LOT = 100
DEFAULT_START = "2020-01-02"


def fee_for(notional: float, slippage: float) -> float:
    return max(notional * COMMISSION, MIN_COMMISSION) + notional * slippage


def rebalance_marks(index: pd.Index, rebalance: str = "W") -> set[pd.Timestamp]:
    """Last actual trading day of each week/month, never an assumed Friday."""
    period = "W" if rebalance == "W" else "M"
    marks = pd.Series(index, index=index).groupby(index.to_period(period)).max()
    return set(marks.tolist())


def run_panels(close: pd.DataFrame, open_: pd.DataFrame, volume: pd.DataFrame, *,
               start: str = DEFAULT_START,
               end: str | None = None, initial: float = INITIAL,
               slippage: float = SLIPPAGE, rebalance: str = "W",
               signal_fn: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Backtest the canonical rules on pre-loaded close/open/volume panels."""
    if signal_fn is None:
        signal_fn = compute_signal
    stop = pd.Timestamp(end) if end else close.index[-1]
    dates = close.loc[pd.Timestamp(start):stop].index
    if len(dates) < 30:
        raise ValueError(f"回测样本过短: {len(dates)} 个交易日")
    marks = rebalance_marks(dates, rebalance)

    positions: dict[str, dict[str, Any]] = {}
    cash = float(initial)
    pending: dict[str, float] | None = None
    curve: list[tuple[pd.Timestamp, float]] = []
    exposures: list[float] = []
    rounds: list[float] = []
    trades = 0
    turnover = 0.0

    def target_weights(day: pd.Timestamp) -> dict[str, float]:
        signal = signal_fn(close, volume, as_of=day)
        return {s: float(w) for s, w in signal["target_weights"].items() if float(w) > 0}

    for index, day in enumerate(dates):
        if pending is not None:
            equity_open = cash + sum(p["qty"] * float(open_[s].loc[day]) for s, p in positions.items())
            targets = {s: equity_open * w for s, w in pending.items()}
            for symbol in sorted(list(positions)):
                price = float(open_[symbol].loc[day])
                if not np.isfinite(price) or price <= 0:
                    continue
                current = positions[symbol]["qty"]
                target_value = targets.get(symbol, 0.0)
                if current * price <= target_value:
                    continue
                qty = int((current * price - target_value) / price / LOT) * LOT
                if target_value <= 0:
                    qty = current
                qty = min(qty, current)
                if qty <= 0:
                    continue
                notional = qty * price
                fee = fee_for(notional, slippage)
                cash += notional - fee
                held = positions[symbol]["qty"]
                cost_part = positions[symbol].get("cost_basis", 0.0) * (qty / held) if held > 0 else 0.0
                positions[symbol]["cost_basis"] = positions[symbol].get("cost_basis", 0.0) - cost_part
                positions[symbol]["realized"] = positions[symbol].get("realized", 0.0) + notional - fee - cost_part
                positions[symbol]["qty"] -= qty
                trades += 1
                turnover += notional / initial
                if positions[symbol]["qty"] <= 0:
                    rounds.append(float(positions[symbol].get("realized", 0.0)))
                    del positions[symbol]
            for symbol, target_value in sorted(targets.items()):
                price = float(open_[symbol].loc[day])
                if not np.isfinite(price) or price <= 0:
                    continue
                current = positions.get(symbol, {}).get("qty", 0)
                delta = target_value - current * price
                if delta <= price * LOT * 0.5:
                    continue
                qty = int(delta / price / LOT) * LOT
                if qty <= 0:
                    continue
                qty = min(qty, int(cash / price / LOT) * LOT)
                while qty > 0 and qty * price + fee_for(qty * price, slippage) > cash:
                    qty -= LOT
                if qty <= 0:
                    continue
                notional = qty * price
                fee = fee_for(notional, slippage)
                old = positions.get(symbol)
                if old:
                    old["entry_price"] = (old["entry_price"] * old["qty"] + price * qty) / (old["qty"] + qty)
                    old["qty"] += qty
                    old["cost_basis"] = old.get("cost_basis", 0.0) + notional + fee
                else:
                    positions[symbol] = {
                        "qty": qty, "entry_price": price,
                        "cost_basis": notional + fee, "realized": 0.0,
                    }
                cash -= notional + fee
                trades += 1
                turnover += notional / initial
            pending = None

        equity = cash + sum(p["qty"] * float(close[s].loc[day]) for s, p in positions.items())
        invested = sum(p["qty"] * float(close[s].loc[day]) for s, p in positions.items())
        curve.append((day, equity))
        exposures.append(invested / equity if equity > 0 else 0.0)

        if day in marks and index + 1 < len(dates):
            pending = target_weights(day)

    series = pd.Series(dict(curve)).sort_index()
    returns = series.pct_change().dropna()
    years = (series.index[-1] - series.index[0]).days / 365.25
    total = series.iloc[-1] / initial - 1.0
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 else float("nan")
    std = float(returns.std())
    gains = returns[returns > 0]
    losses = returns[returns < 0]
    daily_payoff = (float(gains.mean()) / abs(float(losses.mean()))) if len(gains) and len(losses) else None
    wins = [value for value in rounds if value > 0]
    losses_round = [value for value in rounds if value < 0]
    trade_payoff = (
        (float(np.mean(wins)) / abs(float(np.mean(losses_round))))
        if wins and losses_round else None
    )
    yearly_last = series.resample("YE").last()
    yearly = yearly_last.pct_change()
    if len(yearly):
        yearly.iloc[0] = yearly_last.iloc[0] / initial - 1.0
    return {
        "start": str(series.index[0].date()),
        "end": str(series.index[-1].date()),
        "initial": initial,
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "vol_pct": round(std * math.sqrt(252) * 100, 2),
        "sharpe": round(float(returns.mean()) / std * math.sqrt(252), 2) if std > 0 else None,
        "max_drawdown_pct": round(float((series / series.cummax() - 1.0).min()) * 100, 2),
        "avg_exposure": round(float(np.mean(exposures)), 2),
        "daily_win_rate_pct": round(float((returns > 0).mean()) * 100, 2) if len(returns) else None,
        "daily_payoff": round(daily_payoff, 3) if daily_payoff is not None else None,
        "trade_win_rate_pct": round(len(wins) / len(rounds) * 100, 2) if rounds else None,
        "trade_payoff": round(trade_payoff, 3) if trade_payoff is not None else None,
        "closed_rounds": len(rounds),
        "trades": trades,
        "turnover_x": round(turnover, 2),
        "slippage_bp": int(round(slippage * 10000)),
        "yearly": {str(int(getattr(k, "year", k))): round(v * 100, 2) for k, v in yearly.items()},
        "curve": [
            {"date": day.strftime("%Y-%m-%d"), "equity": round(float(value), 5)}
            for day, value in series.items()
        ],
    }


def run(data_dir: Any = None, *, start: str = DEFAULT_START, end: str | None = None,
        initial: float = INITIAL, slippage: float = SLIPPAGE,
        rebalance: str = "W") -> dict[str, Any]:
    """Backtest from the local qfq cache."""
    close, volume = load_panel(data_dir)
    open_ = load_open_panel(data_dir)
    return run_panels(close, open_, volume, start=start, end=end, initial=initial,
                      slippage=slippage, rebalance=rebalance)
