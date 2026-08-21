"""Backtest win-rate using single-pass full-history analysis + vectorised MACD.

Every stock is analysed ONCE (instead of sliding-window 120× per stock),
which brings full-market runtime from ~5h down to ~3 minutes.

Method:
- Chan signals (buy_1/2/3, sell_1/2/3): full-history analyze_chan, uses
  `confirmed_at` as signal day (locked strokes are immutable).
- MACD golden/death cross days: vectorised pandas over full history.
- Entry: signal day D → next-trading-day open (T+1, lot 100).
- Exit: first Chan sell or zero-axis death cross after entry day;
  fallback to max_holding_bars=40 periods else window-close.
- Costs: config backtest section.

Run:
    python backtest_winrate.py [--limit N] [--workers 8] [--out backtest_report.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from strategy.chan import analyze_chan
from strategy.macd import calculate_macd, classify_zero_axis_zone
from utils.helpers import load_config

logger = logging.getLogger(__name__)

HISTORY_DIR = BASE_DIR / "cache" / "daily_history"
SELL_TYPES = {"sell_1", "sell_2", "sell_3", "zero_axis_death_cross"}


def _day(value) -> date:
    return pd.Timestamp(value).date()


def find_signals(history: pd.DataFrame, config: dict) -> dict[str, list[dict]]:
    """Single-pass: chan signals + vectorised MACD cross days.

    Returns dict with:
        buy:  list of {day, signal_type, side, price, confirmed_at}
        sell: list of {day, signal_type, side, price, confirmed_at}
    """
    closed = history[history["is_closed"].fillna(False).astype(bool)].copy().reset_index(drop=True)
    if closed.empty or len(closed) < 60:
        return {"buy": [], "sell": []}

    chan = config.get("signal_strategy", {}).get("chan", {})
    min_bi_bars = int(chan.get("min_bi_bars", 4))
    divergence_ratio = float(chan.get("divergence_ratio", 0.9))

    # Chan needs the MACD columns (hist area divergence for type-1 points).
    macd_frame = calculate_macd(closed["close"], fast=12, slow=26, signal=9)
    enriched = closed.join(macd_frame)
    chan_result = analyze_chan(enriched, min_bi_bars=min_bi_bars, divergence_ratio=divergence_ratio)
    chan_signals = chan_result.get("signals", [])

    buys: list[dict] = []
    sells: list[dict] = []

    # --- Chan signals ---
    for sig in chan_signals:
        d = _day(sig["confirmed_at"])
        record = {
            "day": d.isoformat(),
            "signal_type": sig["signal_type"],
            "side": sig["side"],
            "price": float(sig["price"]),
            "confirmed_at": str(sig["confirmed_at"]),
        }
        if sig["side"] == "buy":
            buys.append(record)
        elif sig["signal_type"] in SELL_TYPES:
            sells.append(record)

    # --- Vectorised MACD crosses ---
    dif = macd_frame["dif"]
    dea = macd_frame["dea"]
    prev_dif = dif.shift(1)
    prev_dea = dea.shift(1)
    dates = pd.to_datetime(closed["datetime"])
    closes = closed["close"]
    zero_tol = float(config.get("signal_strategy", {}).get("macd", {}).get("zero_axis_tolerance", 0.005))

    for i in range(1, len(closed)):
        raw_date = dates.iloc[i]
        d = raw_date.date()

        if i < 2 or pd.isna(dif.iloc[i]) or pd.isna(dea.iloc[i]):
            continue

        gc = bool(dif.iloc[i] > dea.iloc[i] and prev_dif.iloc[i] <= prev_dea.iloc[i])
        dc = bool(dif.iloc[i] < dea.iloc[i] and prev_dif.iloc[i] >= prev_dea.iloc[i])

        if gc or dc:
            zone = classify_zero_axis_zone(
                float(dif.iloc[i]), float(dea.iloc[i]), float(closes.iloc[i]), zero_tol,
            )

        if gc:
            # Only above-axis crosses clear the buy_threshold (60) as standalone
            # events (zone points 50 + confirmations); near/below crosses are
            # events only when a Chan buy point coexists (already covered above).
            if zone == "above":
                buys.append({
                    "day": d.isoformat(),
                    "signal_type": "macd_golden_cross_above",
                    "side": "buy",
                    "price": float(closes.iloc[i]),
                    "confirmed_at": str(raw_date),
                })
        if dc and zone == "near":
            sells.append({
                "day": d.isoformat(),
                "signal_type": "zero_axis_death_cross",
                "side": "sell",
                "price": float(closes.iloc[i]),
                "confirmed_at": str(raw_date),
            })

    return {"buy": buys, "sell": sells}


def next_bar_index(bars: pd.DataFrame, dates: list[date], day: date) -> int | None:
    for i, d in enumerate(dates):
        if d > day:
            return i
    return None


def simulate(
    symbol: str,
    closed: pd.DataFrame,
    events: dict[str, list[dict]],
    start: date,
    end: date,
    costs: dict,
) -> list[dict]:
    dates = [_day(pd.Timestamp(x)) for x in closed["datetime"]]
    by_day = {d: i for i, d in enumerate(dates)}

    buys = sorted(events.get("buy", []), key=lambda item: item["day"])
    sells = sorted(events.get("sell", []), key=lambda item: item["day"])

    commission_pct = float(costs.get("commission_pct", 0.0003))
    stamp_pct = float(costs.get("stamp_tax_pct", 0.001))
    slippage_pct = float(costs.get("slippage_pct", 0.0005))
    lot = int(costs.get("lot_size", 100))
    max_holding_bars = int(costs.get("chan_zero_axis", {}).get("max_holding_bars", 40))

    trades: list[dict] = []
    for buy in buys:
        day = date.fromisoformat(buy["day"])
        if day < start or day > end:
            continue
        entry_idx = next_bar_index(closed, dates, day)
        if entry_idx is None:
            continue
        open_price = float(closed.iloc[entry_idx]["open"])
        if open_price <= 0:
            continue

        exit_idx = None
        exit_day = None
        exit_reason = "chan_sell"
        for sell in sells:
            s_day = date.fromisoformat(sell["day"])
            if s_day > day:
                exit_idx = by_day.get(s_day)
                exit_day = s_day
                if exit_idx is not None:
                    break
        if exit_idx is not None and exit_idx <= entry_idx:
            exit_idx = None

        timeout_idx = entry_idx + max_holding_bars
        if exit_idx is None or timeout_idx < exit_idx:
            exit_idx = min(timeout_idx, len(closed) - 1)
            exit_day = dates[exit_idx]
            exit_reason = "timeout" if timeout_idx < len(closed) else "window_end"

        # Fill at next day open (or close if last bar)
        fill_idx = exit_idx + 1 if exit_idx < len(closed) - 1 else None
        if fill_idx is not None:
            exit_price = float(closed.iloc[fill_idx]["open"])
        else:
            exit_price = float(closed.iloc[exit_idx]["close"])
        if exit_price <= 0:
            continue

        buy_cost = open_price * (1 + commission_pct + slippage_pct)
        sell_gain = exit_price * (1 - commission_pct - stamp_pct - slippage_pct)
        pnl_pct = (sell_gain - buy_cost) / buy_cost * 100.0
        pnl_cash = pnl_pct * open_price * lot / 100.0
        holding_days = max((exit_day - day).days, 0)

        trades.append({
            "symbol": symbol,
            "entry_day": day.isoformat(),
            "exit_day": exit_day.isoformat(),
            "signal_type": buy["signal_type"],
            "entry_price": round(open_price, 3),
            "exit_price": round(exit_price, 3),
            "pnl_pct": round(pnl_pct, 2),
            "pnl_cash": round(pnl_cash, 2),
            "holding_days": holding_days,
            "exit_reason": exit_reason,
        })
    return trades


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {
            "count": 0,
            "win_rate": None,
            "avg_pnl_pct": None,
            "median_pnl_pct": None,
            "total_pnl_cash": 0.0,
        }
    wins = [t for t in trades if t["pnl_pct"] > 0]
    pnls = [t["pnl_pct"] for t in trades]
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0.0
    losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] <= 0]
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return {
        "count": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 2),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 2),
        "median_pnl_pct": round(sorted(pnls)[len(pnls) // 2], 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(abs(avg_win / avg_loss), 2) if avg_loss else None,
        "total_pnl_cash": round(sum(t["pnl_cash"] for t in trades), 2),
        "avg_holding_days": round(sum(t["holding_days"] for t in trades) / len(trades), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest signal win rate")
    parser.add_argument("--limit", type=int, default=0, help="only first N symbols")
    parser.add_argument("--start", type=str, default="2026-02-21", help="backtest window start")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", type=str, default="backtest_report.json")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.today()
    config = load_config(BASE_DIR / "config" / "config.yaml")
    costs = config.get("backtest", {})

    files = sorted(HISTORY_DIR.glob("*_none.pkl"))
    if args.limit:
        files = files[: args.limit]
    logger.info("Backtesting %s symbols, window %s → %s", len(files), start, end)

    all_trades: list[dict] = []
    errors = 0

    def analyse_one(path: Path):
        symbol = path.name.split("_")[0]
        try:
            history = pd.read_pickle(path)
        except Exception:
            return []
        closed = history[history["is_closed"].fillna(False).astype(bool)].copy().reset_index(drop=True)
        if closed.empty or len(closed) < 60:
            return []
        events = find_signals(history, config)
        return simulate(symbol, closed, events, start, end, costs)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(analyse_one, path) for path in files]
        for i, future in enumerate(as_completed(futures), 1):
            try:
                trades = future.result()
                all_trades.extend(trades)
            except Exception:
                errors += 1
            if i % 200 == 0:
                logger.info("processed %d/%d, trades so far %d", i, len(files), len(all_trades))

    summary = summarize(all_trades)
    by_signal = {}
    for trade in all_trades:
        by_signal.setdefault(trade["signal_type"], []).append(trade)
    by_signal_summary = {k: summarize(v) for k, v in sorted(by_signal.items())}
    exit_breakdown = dict(Counter(t["exit_reason"] for t in all_trades))

    report = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "symbols_analyzed": len(files),
        "symbols_failed": errors,
        "costs": costs,
        "summary": summary,
        "by_signal_type": by_signal_summary,
        "exit_reasons": exit_breakdown,
    }
    out_path = BASE_DIR / args.out
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    trades_path = out_path.with_name(out_path.stem + "_trades.jsonl")
    with trades_path.open("w", encoding="utf-8") as handle:
        for trade in all_trades:
            handle.write(json.dumps(trade, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())