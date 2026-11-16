"""首板低开 + 市场走势分类状态机（预注册，不调参）。

状态定义（固定，首次运行前确定）：
- 市场代理：等权主板+创业板日线净值（与首板四策略基准一致）。
- MA20、MA60、MA20 五日斜率 = MA20_t / MA20_{t-5} - 1。
- up: close > MA20 > MA60 且斜率 > 0
- down: close < MA20 < MA60 且斜率 < 0
- turn_up: close > MA20、斜率 > 0，但 close <= MA60
- turn_down: close < MA20、斜率 < 0，但 close >= MA60
- range: 其余
- 信号日 T 的状态只使用 T-1 及以前的数据，无未来函数。

叠加规则（全部预注册，不根据结果挑选）：
- base: 全部信号
- skip_down: 跳过 down
- skip_up: 跳过 up
- skip_turn_down: 跳过 turn_down
- only_up: 只在 up
- only_down: 只在 down
- half_down: 全部，但 down 状态只投入 50% 现金

组合口径：
- 100 万初始，空仓时把现金按当日允许信号等分，T 开盘买，T+1 收盘卖；
- 佣金 3bp/边、卖出印花税 10bp、滑点 10bp/边；
- 忽略涨停持有/跌停无法卖出，所有变体同一简化口径。

研究脚本，不接生产、不自动交易。
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd

from first_board_low_open_15m_study import signal_table

BASE_DIR = pathlib.Path(__file__).resolve().parent
BENCHMARK = BASE_DIR.parent.parent / "docs" / "backtest-results" / "2026-11-14-first-board-four-strategies" / "curves.csv"
FIFTEEN = BASE_DIR.parent.parent / "docs" / "backtest-results" / "2026-11-16-first-board-low-open-15m" / "trades.csv"
OUT = BASE_DIR.parent.parent / "docs" / "backtest-results" / "2026-11-16-first-board-state-machine"
COMMISSION = 0.0003
STAMP_TAX = 0.0010
SLIPPAGE = 0.0010


def load_benchmark() -> pd.Series:
    frame = pd.read_csv(BENCHMARK)
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[frame["strategy"] == "s1_first_board_low_open"]
    return frame.set_index("date")["benchmark"].astype(float).sort_index()


def classify(benchmark: pd.Series) -> pd.DataFrame:
    ma20 = benchmark.rolling(20).mean()
    ma60 = benchmark.rolling(60).mean()
    slope = ma20 / ma20.shift(5) - 1.0
    state = pd.Series("range", index=benchmark.index, dtype=object)
    state[(benchmark > ma20) & (ma20 > ma60) & (slope > 0)] = "up"
    state[(benchmark < ma20) & (ma20 < ma60) & (slope < 0)] = "down"
    state[(state == "range") & (benchmark > ma20) & (slope > 0) & (benchmark <= ma60)] = "turn_up"
    state[(state == "range") & (benchmark < ma20) & (slope < 0) & (benchmark >= ma60)] = "turn_down"
    frame = pd.DataFrame({"state": state, "close": benchmark, "ma20": ma20, "ma60": ma60, "slope5": slope})
    frame["known_at"] = frame.index
    return frame


def state_before(states: pd.DataFrame, day: pd.Timestamp) -> str:
    idx = states.index.searchsorted(day, side="left") - 1
    if idx < 0:
        return "n/a"
    return str(states.iloc[idx]["state"])


def allowed(state: str, rule: str) -> tuple[bool, float]:
    if rule == "base":
        return True, 1.0
    if rule == "skip_down":
        return state != "down", 1.0
    if rule == "skip_up":
        return state != "up", 1.0
    if rule == "skip_turn_down":
        return state != "turn_down", 1.0
    if rule == "only_up":
        return state == "up", 1.0
    if rule == "only_down":
        return state == "down", 1.0
    if rule == "half_down":
        return True, 0.5 if state == "down" else 1.0
    raise ValueError(rule)


def portfolio(signals: pd.DataFrame, states: pd.DataFrame, rule: str) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    dates = states.index[(states.index >= "2020-01-02") & (states.index <= "2026-10-30")]
    by_entry: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    for row in signals.itertuples(index=False):
        entry = pd.Timestamp(row.signal_date)
        if entry not in dates:
            continue
        state = state_before(states, entry)
        ok, weight = allowed(state, rule)
        if not ok:
            continue
        by_entry.setdefault(entry, []).append(
            {
                "symbol": row.symbol,
                "state": state,
                "weight": weight,
                "open": float(row.open),
                "close": float(row.close),
                "next_date": pd.Timestamp(row.next_date),
                "next_close": float(row.next_close),
            }
        )

    cash = 1_000_000.0
    positions: list[dict[str, Any]] = []
    curve = []
    trades = []
    for day in dates:
        # buy at open only if flat at start of day
        if not positions:
            candidates = by_entry.get(day, [])
            if candidates:
                cash_start = cash
                count = len(candidates)
                for cand in candidates:
                    budget = cash_start * cand["weight"] / count
                    fill = cand["open"] * (1.0 + SLIPPAGE)
                    qty = int(budget / fill // 100) * 100
                    while qty >= 100:
                        cost = qty * fill * (1.0 + COMMISSION)
                        if cost <= cash:
                            break
                        qty -= 100
                    if qty < 100:
                        continue
                    cash -= cost
                    positions.append(
                        {
                            "symbol": cand["symbol"],
                            "qty": qty,
                            "entry_date": day,
                            "entry_fill": fill,
                            "close": cand["close"],
                            "cost": cost,
                            "state": cand["state"],
                            "exit_date": cand["next_date"],
                            "exit_close": cand["next_close"],
                        }
                    )
        # sell at close on T+1
        remaining = []
        for pos in positions:
            if pos["exit_date"] == day:
                proceeds = pos["qty"] * pos["exit_close"] * (1.0 - SLIPPAGE) * (1.0 - COMMISSION - STAMP_TAX)
                cash += proceeds
                trades.append(
                    {
                        "symbol": pos["symbol"],
                        "entry_date": pos["entry_date"],
                        "exit_date": day,
                        "state": pos["state"],
                        "entry_fill": pos["entry_fill"],
                        "exit_price": pos["exit_close"],
                        "net_return": proceeds / pos["cost"] - 1.0,
                    }
                )
            else:
                remaining.append(pos)
        positions = remaining
        invested = sum(pos["qty"] * (pos["close"] if pos["entry_date"] == day else pos["exit_close"]) for pos in positions)
        equity = cash + invested
        curve.append({"date": day, "equity": equity, "cash": cash, "invested": invested, "positions": len(positions)})

    curve_frame = pd.DataFrame(curve)
    trades_frame = pd.DataFrame(trades)
    if curve_frame.empty:
        return curve_frame, {}, trades_frame
    equity = curve_frame.set_index("date")["equity"]
    daily = equity.pct_change().dropna()
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    ann = (1.0 + total) ** (252.0 / len(daily)) - 1.0 if len(daily) else 0.0
    sharpe = float(daily.mean() / daily.std(ddof=1) * math.sqrt(252.0)) if len(daily) > 1 and daily.std(ddof=1) > 0 else 0.0
    mdd = float((equity / equity.cummax() - 1.0).min())
    returns = trades_frame["net_return"].to_numpy(dtype=float) if len(trades_frame) else np.array([])
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    metrics = {
        "trades": int(len(returns)),
        "total_return_pct": round(float(total) * 100, 2),
        "annualized_return_pct": round(float(ann) * 100, 2),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(mdd * 100, 2),
        "win_rate_pct": round(float((returns > 0).mean()) * 100, 2) if len(returns) else None,
        "payoff": round(float(wins.mean() / abs(losses.mean())), 2) if len(wins) and len(losses) else None,
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if len(wins) and len(losses) else None,
        "avg_exposure_pct": round(float((curve_frame["invested"] / curve_frame["equity"]).mean()) * 100, 2),
    }
    return curve_frame, metrics, trades_frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    benchmark = load_benchmark()
    states = classify(benchmark)
    signals = signal_table("2020-01-02", "2026-10-30")

    # Conditional event table uses the B0 fixed 15m trades from the prior study.
    fifteen = pd.read_csv(FIFTEEN, dtype={"symbol": str})
    fifteen = fifteen[(fifteen["variant"] == "B0") & (fifteen["exit_mode"] == "fixed")].copy()
    fifteen["state"] = [state_before(states, pd.Timestamp(d)) for d in fifteen["signal_date"]]
    fifteen["year"] = fifteen["signal_date"].str[:4]

    rows = []
    for state, group in fifteen.groupby("state"):
        ret = group["net_return"].to_numpy(dtype=float)
        wins = ret[ret > 0]
        losses = ret[ret < 0]
        rows.append(
            {
                "state": state,
                "trades": int(len(group)),
                "mean_pct": round(float(ret.mean()) * 100, 3),
                "median_pct": round(float(np.median(ret)) * 100, 3),
                "win_rate_pct": round(float((ret > 0).mean()) * 100, 2),
                "payoff": round(float(wins.mean() / abs(losses.mean())), 2) if len(wins) and len(losses) else None,
                "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if len(wins) and len(losses) else None,
                "t_stat": round(float(ret.mean() / (ret.std(ddof=1) / math.sqrt(len(ret)))), 2) if len(ret) > 1 and ret.std(ddof=1) > 0 else 0.0,
            }
        )
    conditional = pd.DataFrame(rows).sort_values("mean_pct", ascending=False)

    results = {}
    curves = []
    trades_all = []
    for rule in ["base", "skip_down", "skip_up", "skip_turn_down", "only_up", "only_down", "half_down"]:
        curve, metrics, trades = portfolio(signals, states, rule)
        results[rule] = metrics
        if not curve.empty:
            curves.append(curve.assign(strategy=rule))
        if not trades.empty:
            trades_all.append(trades.assign(strategy=rule))

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    states.reset_index(names="date").to_csv(out / "states.csv", index=False)
    fifteen.to_csv(out / "conditional_trades.csv", index=False)
    conditional.to_csv(out / "conditional.csv", index=False)
    pd.concat(curves, ignore_index=True).to_csv(out / "curves.csv", index=False) if curves else None
    pd.concat(trades_all, ignore_index=True).to_csv(out / "trades.csv", index=False) if trades_all else None
    summary = {
        "state_definition": {
            "market": "equal-weight main+chinext benchmark",
            "ma_fast": 20,
            "ma_slow": 60,
            "slope_days": 5,
            "states": {
                "up": "close>MA20>MA60 and slope5>0",
                "down": "close<MA20<MA60 and slope5<0",
                "turn_up": "close>MA20, slope5>0, close<=MA60",
                "turn_down": "close<MA20, slope5<0, close>=MA60",
                "range": "otherwise",
            },
            "lookahead": "signal day state uses previous trading day only",
        },
        "conditional": conditional.to_dict(orient="records"),
        "overlays": results,
        "costs": {"commission": COMMISSION, "stamp_tax": STAMP_TAX, "slippage": SLIPPAGE},
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(conditional.to_string(index=False))
    for rule, metrics in results.items():
        print(rule, metrics)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
