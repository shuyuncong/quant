#!/usr/bin/env python3
"""首板低开 vs 首板缩量回调：统一组合层回测。

Both strategies keep their own entry/exit signals and net trade returns.  The
only thing standardized is the portfolio layer:

- 100 万初始资金；
- 每笔目标名义本金 = 初始资金 × weight；
- max_slots 个仓位；
- 同日先退出、再开仓；同一策略同一股票不重复持有；
- 交易成本已包含在各策略的 net_return 中；
- 组合层用日线收盘 mark-to-market，不按整手取整。

Panels:
- 5% × 20 slots
- 10% × 10 slots
- combined 50/50: each strategy has half capital and half slots, each trade
  still uses the same absolute target as the solo book.

This is a portfolio-layer comparison, not a re-optimization of either signal.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SIGNAL = ROOT / "quant-python" / "signal_system"
sys.path.insert(0, str(SIGNAL))

import first_board_pullback_study as base  # noqa: E402

RESULTS = ROOT / "docs" / "backtest-results"
OUT = RESULTS / "2026-11-18-first-board-low-open-vs-pullback-same-sizing"
START = pd.Timestamp("2020-01-02")
END = pd.Timestamp("2026-10-30")
INITIAL = 1_000_000.0


def _read_trades() -> tuple[pd.DataFrame, pd.DataFrame]:
    low = pd.read_csv(
        RESULTS / "2026-11-15-first-board-low-open-maxweight33" / "trades.csv",
        dtype={"symbol": str},
    )
    low["entry_date"] = pd.to_datetime(low["entry_date"], errors="coerce")
    low["exit_date"] = pd.to_datetime(low["exit_date"], errors="coerce")
    low = low.rename(columns={"entry_price": "entry_fill"})
    low["net_return"] = pd.to_numeric(low["net_return"], errors="coerce")
    low["strategy"] = "low_open"
    low["score"] = 0.0
    low = low[
        ["strategy", "symbol", "entry_date", "exit_date", "entry_fill", "net_return", "score"]
    ]

    v1 = pd.read_csv(
        RESULTS / "2026-11-16-first-board-pullback-v1-v5" / "trades_V1.csv",
        dtype={"symbol": str},
    )
    v1["entry_date"] = pd.to_datetime(v1["entry_date"], errors="coerce")
    v1["exit_date"] = pd.to_datetime(v1["exit_date"], errors="coerce")
    v1["entry_fill"] = pd.to_numeric(v1["entry_open"], errors="coerce")
    v1["net_return"] = pd.to_numeric(v1["net_return_pct"], errors="coerce") / 100.0
    v1["score"] = pd.to_numeric(v1["volume_multiple"], errors="coerce")
    v1["strategy"] = "pullback_v1"
    v1 = v1[
        ["strategy", "symbol", "entry_date", "exit_date", "entry_fill", "net_return", "score"]
    ]

    cleaned = []
    for df in (low, v1):
        df = df.dropna(subset=["entry_date", "exit_date", "entry_fill", "net_return"]).copy()
        df = df.drop_duplicates(["strategy", "symbol", "entry_date"])
        df = df[(df["entry_date"] >= START) & (df["exit_date"] <= END) & (df["exit_date"] > df["entry_date"])]
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        df = df.sort_values(["entry_date", "score", "symbol"], ascending=[True, False, True])
        cleaned.append(df.reset_index(drop=True))
    return cleaned[0], cleaned[1]


def _calendar_and_closes(low: pd.DataFrame, v1: pd.DataFrame) -> tuple[pd.DatetimeIndex, dict[str, pd.Series]]:
    bars = base.load_bars()
    dates = set(pd.to_datetime(pd.read_csv(RESULTS / "2026-11-16-first-board-pullback-v1-v5" / "portfolio_V1.csv")["date"]))
    for df in (low, v1):
        dates.update(df["entry_date"])
        dates.update(df["exit_date"])
    calendar = pd.DatetimeIndex(sorted(d for d in dates if START <= d <= END))
    symbols = sorted(set(low["symbol"]) | set(v1["symbol"]))
    close_maps: dict[str, pd.Series] = {}
    missing = []
    for symbol in symbols:
        frame = bars.get(symbol)
        if frame is None or frame.empty:
            missing.append(symbol)
            continue
        s = frame.set_index("datetime")["close"].reindex(calendar).ffill()
        close_maps[symbol] = s
    if missing:
        print(f"missing bars: {len(missing)} {missing[:10]}")
    return calendar, close_maps


def _book(
    trades: pd.DataFrame,
    *,
    initial: float,
    weight: float,
    max_slots: int,
    close_maps: dict[str, pd.Series],
    calendar: pd.DatetimeIndex,
    label: str,
) -> dict[str, Any]:
    # The absolute target is always expressed on total 1M capital, so a 50/50
    # sleeve uses exactly the same per-trade notional as the solo book.
    target = INITIAL * weight
    cash = float(initial)
    positions: list[dict[str, Any]] = []
    by_entry: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    for row in trades.itertuples(index=False):
        by_entry.setdefault(pd.Timestamp(row.entry_date), []).append(row._asdict())
    for rows in by_entry.values():
        rows.sort(key=lambda r: (-float(r.get("score") or 0.0), r["symbol"]))

    accepted: list[dict[str, Any]] = []
    skipped_slots = skipped_cash = skipped_dup = 0
    equities: list[float] = []
    exposures: list[float] = []
    for day in calendar:
        # Exits first; proceeds are available for the same day's open.
        still = []
        for pos in positions:
            if pos["trade"]["exit_date"] == day:
                cash += pos["target"] * (1.0 + float(pos["trade"]["net_return"]))
                accepted.append(
                    {
                        **pos["trade"],
                        "target": pos["target"],
                        "net_return": float(pos["trade"]["net_return"]),
                    }
                )
            else:
                still.append(pos)
        positions = still

        held = {pos["trade"]["symbol"] for pos in positions}
        for trade in by_entry.get(day, []):
            if len(positions) >= max_slots:
                skipped_slots += 1
                continue
            symbol = trade["symbol"]
            if symbol in held:
                skipped_dup += 1
                continue
            if cash < target:
                skipped_cash += 1
                continue
            cash -= target
            positions.append(
                {
                    "trade": trade,
                    "target": target,
                    "entry_price": float(trade["entry_fill"]),
                }
            )
            held.add(symbol)

        invested = 0.0
        for pos in positions:
            close = close_maps.get(pos["trade"]["symbol"])
            value = float(close.get(day, np.nan)) if close is not None else np.nan
            if not math.isfinite(value) or value <= 0:
                value = pos["entry_price"]
            invested += pos["target"] * value / pos["entry_price"]
        equity = cash + invested
        equities.append(equity)
        exposures.append(invested / equity if equity > 0 else 0.0)

    eq = pd.Series(equities, index=calendar, name=label)
    returns = eq.pct_change().dropna()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    annualized = float((1.0 + total) ** (252.0 / (len(eq) - 1)) - 1.0)
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else None
    )
    mdd = float((eq / eq.cummax() - 1.0).min())
    wins = [t for t in accepted if float(t["net_return"]) > 0]
    annual: dict[str, float] = {}
    prev = float(eq.iloc[0])
    for year, group in eq.groupby(eq.index.year):
        end = float(group.iloc[-1])
        annual[str(int(year))] = end / prev - 1.0
        prev = end
    return {
        "label": label,
        "equity": eq,
        "exposure": pd.Series(exposures, index=calendar),
        "metrics": {
            "total_return_pct": round(total * 100.0, 2),
            "annualized_pct": round(annualized * 100.0, 2),
            "sharpe": round(sharpe, 3) if sharpe is not None else None,
            "max_drawdown_pct": round(mdd * 100.0, 2),
            "win_rate_pct": round(len(wins) / len(accepted) * 100.0, 2) if accepted else None,
            "trades": len(accepted),
            "avg_exposure_pct": round(float(np.mean(exposures)) * 100.0, 2),
            "final_equity": round(float(eq.iloc[-1]), 2),
            "skipped_slots": skipped_slots,
            "skipped_cash": skipped_cash,
            "skipped_duplicate": skipped_dup,
        },
        "accepted": accepted,
        "annual": annual,
    }


def _combined(a: dict[str, Any], b: dict[str, Any], label: str) -> dict[str, Any]:
    eq = (a["equity"] + b["equity"]).rename(label)
    accepted = a["accepted"] + b["accepted"]
    returns = eq.pct_change().dropna()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    annualized = float((1.0 + total) ** (252.0 / (len(eq) - 1)) - 1.0)
    sharpe = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0))
    mdd = float((eq / eq.cummax() - 1.0).min())
    wins = [t for t in accepted if float(t["net_return"]) > 0]
    annual: dict[str, float] = {}
    prev = float(eq.iloc[0])
    for year, group in eq.groupby(eq.index.year):
        end = float(group.iloc[-1])
        annual[str(int(year))] = end / prev - 1.0
        prev = end
    combined_exposure = (
        a["equity"] * a["exposure"] + b["equity"] * b["exposure"]
    ) / eq
    return {
        "label": label,
        "equity": eq,
        "exposure": combined_exposure,
        "metrics": {
            "total_return_pct": round(total * 100.0, 2),
            "annualized_pct": round(annualized * 100.0, 2),
            "sharpe": round(sharpe, 3),
            "max_drawdown_pct": round(mdd * 100.0, 2),
            "win_rate_pct": round(len(wins) / len(accepted) * 100.0, 2) if accepted else None,
            "trades": len(accepted),
            "avg_exposure_pct": round(float(combined_exposure.mean()) * 100.0, 2),
            "final_equity": round(float(eq.iloc[-1]), 2),
        },
        "accepted": accepted,
        "annual": annual,
    }


def _corr(a: pd.Series, b: pd.Series) -> float:
    r = pd.concat([a.pct_change(), b.pct_change()], axis=1).dropna()
    return float(r.iloc[:, 0].corr(r.iloc[:, 1]))


def _run_regime(
    low: pd.DataFrame, v1: pd.DataFrame, close_maps: dict[str, pd.Series], calendar: pd.DatetimeIndex,
    weight: float, slots: int, label: str,
) -> dict[str, Any]:
    low_solo = _book(low, initial=INITIAL, weight=weight, max_slots=slots, close_maps=close_maps, calendar=calendar, label=f"{label} low")
    v1_solo = _book(v1, initial=INITIAL, weight=weight, max_slots=slots, close_maps=close_maps, calendar=calendar, label=f"{label} v1")
    half = INITIAL / 2.0
    low_half = _book(low, initial=half, weight=weight, max_slots=slots // 2, close_maps=close_maps, calendar=calendar, label=f"{label} low half")
    v1_half = _book(v1, initial=half, weight=weight, max_slots=slots // 2, close_maps=close_maps, calendar=calendar, label=f"{label} v1 half")
    combined = _combined(low_half, v1_half, f"{label} combined 50/50")
    combined["correlation"] = _corr(low_solo["equity"], v1_solo["equity"])
    return {"label": label, "weight": weight, "slots": slots, "low": low_solo, "v1": v1_solo, "combined": combined}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    low, v1 = _read_trades()
    calendar, close_maps = _calendar_and_closes(low, v1)
    print(f"low={len(low)} v1={len(v1)} calendar={len(calendar)} closes={len(close_maps)}")

    overlap_exact = len(low.merge(v1, on=["symbol", "entry_date"]))
    overlap_symbols = len(set(low["symbol"]) & set(v1["symbol"]))
    m = low[["symbol", "entry_date"]].merge(v1[["symbol", "entry_date"]], on="symbol", suffixes=("_low", "_v1"))
    m["gap_days"] = (m["entry_date_v1"] - m["entry_date_low"]).dt.days
    overlap_seq = int(((m["gap_days"] >= 0) & (m["gap_days"] <= 15)).sum())

    regimes = []
    for weight, slots, label in [(0.05, 20, "5%×20"), (0.10, 10, "10%×10")]:
        regimes.append(_run_regime(low, v1, close_maps, calendar, weight, slots, label))

    rows = []
    curves = []
    for regime in regimes:
        for key, name in [("low", "首板低开"), ("v1", "首板缩量回调 V1"), ("combined", "50/50 组合")]:
            item = regime[key]
            m = item["metrics"]
            rows.append({
                "panel": regime["label"],
                "portfolio": name,
                "total_return_pct": m["total_return_pct"],
                "annualized_pct": m["annualized_pct"],
                "sharpe": m["sharpe"],
                "max_drawdown_pct": m["max_drawdown_pct"],
                "win_rate_pct": m["win_rate_pct"],
                "trades": m["trades"],
                "avg_exposure_pct": m.get("avg_exposure_pct"),
                "final_equity": m["final_equity"],
            })
            curves.append(item["equity"].rename(f"{regime['label']} · {name}"))

    metrics_df = pd.DataFrame(rows)
    metrics_df.to_csv(OUT / "metrics.csv", index=False, encoding="utf-8-sig")
    curve_df = pd.concat(curves, axis=1).sort_index()
    curve_df.to_csv(OUT / "curves.csv", encoding="utf-8-sig")
    annual_rows = []
    for regime in regimes:
        for key, name in [("low", "首板低开"), ("v1", "首板缩量回调 V1"), ("combined", "50/50 组合")]:
            annual_rows.append({"panel": regime["label"], "portfolio": name, **{k: round(v * 100, 2) for k, v in regime[key]["annual"].items()}})
    pd.DataFrame(annual_rows).to_csv(OUT / "annual_returns.csv", index=False, encoding="utf-8-sig")

    summary = {
        "generated_at": "2026-11-18",
        "period": {"start": str(START.date()), "end": str(END.date())},
        "initial": INITIAL,
        "overlap": {
            "low_trades": int(len(low)),
            "v1_trades": int(len(v1)),
            "exact_same_symbol_entry_date": overlap_exact,
            "same_symbol_any_date": overlap_symbols,
            "same_symbol_v1_within_15_days_after_low": overlap_seq,
        },
        "regimes": [
            {
                "label": r["label"],
                "weight": r["weight"],
                "slots": r["slots"],
                "low": r["low"]["metrics"],
                "v1": r["v1"]["metrics"],
                "combined": r["combined"]["metrics"],
                "correlation": r["combined"].get("correlation"),
            }
            for r in regimes
        ],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(metrics_df.to_string(index=False))
    print("overlap", summary["overlap"])


if __name__ == "__main__":
    main()
