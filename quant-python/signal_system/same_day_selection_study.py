"""同一天同一池子筛出多只, 只能买 3 只 —— 该买哪三只?

与 multi_pool_allocation（多个池子分钱）不同, 这里回答的是**同一个池子内部**:
当天有 N 个候选、只剩 k 个仓位时, 用什么规则挑出 k 个最优的?

三件事:
1. **诊断**: 这种"候选多于仓位"的日子到底占多少? 被 max_positions 拒掉多少信号?
   如果 90% 的日子候选 < 仓位, 那么"挑最优秀"根本不重要 (先到先得就够了)。
2. **对照因子**: 用信号当日可见的信息给候选打分 (越贴近年线 / 放量倍数 / 年线斜率 /
   回撤深度 / 近期动量 ...), 看哪个因子能把"选中的那批"质量提上来。
3. **上界**: `oracle` 模式用**已实现收益**排序 (未来函数, 现实中不可能),
   给出"同一天挑得再准也不可能超过多少"的天花板, 用来判断因子还有多少空间。

口径: 25 万本金, 「只能买 3 只 / 5 只」两种仓位上限, 单笔 = 1/仓位上限,
卖出固定 fixed, 4 种排序取中位。因子只用信号当日及之前的数据。

用法:
    python same_day_selection_study.py --out same_day_selection.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import pickle
import statistics as st
import sys
from typing import Any

import pandas as pd

import backtest_winrate as bt
import backtest_macd_divergence as bmd
from entry_tranche_study import load_bars
from utils.helpers import load_config

BASE_DIR = pathlib.Path(__file__).resolve().parent
CASH = 250_000.0
LOT = 100
SLOT_OPTIONS = (3, 5)
TIE_BREAKS = ("symbol_asc", "symbol_desc", "hash", "rotate")
POOLS = ("diverge", "yearline", "baseline")

# 因子: (名称, 说明, 是否越小越优先)
FACTORS: tuple[tuple[str, str, bool], ...] = (
    ("code", "按代码 (引擎现状)", False),
    ("random_hash", "随机 (对照组, 用代码哈希)", False),
    ("vol_high", "放量倍数越大越买", False),
    ("vol_low", "放量倍数越小越买", True),
    ("near_dist", "距年线绝对值越小越买 (贴近年线)", True),
    ("below_line", "越低于年线越买 (10.2 的那个因子)", True),
    ("ma_slope", "年线越陡向上越买", False),
    ("recovery_60", "距 60 日低点反弹越少越买 (更早介入)", True),
    ("drawdown_120", "距 120 日高点回撤越深越买", True),
    ("momentum_20", "20 日涨幅越小越买", True),
    ("oracle", "【未来函数·上界】按已实现收益排序", False),
)


def _hash_score(symbol: str) -> float:
    digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / float(0xFFFFFFFF)


def compute_factors(
    trades: list[dict[str, Any]], bars: dict[str, pd.DataFrame]
) -> None:
    """就地写入每个候选的因子值 (只用信号当日及之前的数据, 不能有未来函数)。"""
    cache: dict[str, tuple[list[str], pd.DataFrame]] = {}
    for trade in trades:
        symbol = str(trade["symbol"])
        signal_day = str(trade["signal_day"])
        if symbol not in cache:
            frame = bars.get(symbol)
            if frame is None:
                cache[symbol] = ([], pd.DataFrame())
            else:
                dates = [item.date().isoformat() for item in pd.to_datetime(frame["datetime"])]
                cache[symbol] = (dates, frame)
        dates, frame = cache[symbol]
        index = dates.index(signal_day) if signal_day in dates else None
        close = float(trade.get("signal_close") or 0.0)
        ma_long = float(trade.get("ma_long") or 0.0)
        values = {
            "code": -float(int(symbol[:6])) if symbol[:6].isdigit() else 0.0,
            "random_hash": _hash_score(symbol),
            "vol_high": float(trade.get("volume_ratio") or 0.0),
            "vol_low": float(trade.get("volume_ratio") or 0.0),
            "near_dist": -abs(close / ma_long - 1.0) if close and ma_long else -9.0,
            # 与 10.2/10.6 保持同一个定义: 驱动值越负 (越低于年线) 得分越高。
            "below_line": -(close / ma_long - 1.0) if close and ma_long else 0.0,
            "ma_slope": 0.0,
            "recovery_60": 0.0,
            "drawdown_120": 0.0,
            "momentum_20": 0.0,
            "oracle": float(trade.get("pnl_pct") or 0.0),
        }
        if index is not None and not frame.empty:
            closes = pd.to_numeric(frame["close"], errors="coerce")
            if index >= 5:
                values["ma_slope"] = float(closes.iloc[index] / closes.iloc[index - 5] - 1.0)
            if index >= 20:
                values["momentum_20"] = float(closes.iloc[index] / closes.iloc[index - 20] - 1.0)
                window = closes.iloc[max(0, index - 119) : index + 1]
                peak = float(window.max())
                values["drawdown_120"] = float(closes.iloc[index] / peak - 1.0) if peak else 0.0
            if index >= 60:
                window = closes.iloc[max(0, index - 59) : index + 1]
                trough = float(window.min())
                values["recovery_60"] = (
                    float(closes.iloc[index] / trough - 1.0) if trough > 0 else 0.0
                )
        trade["_factors"] = values


def _score(trade: dict[str, Any], factor: str, lower_better: bool) -> float:
    value = float((trade.get("_factors") or {}).get(factor) or 0.0)
    return -value if lower_better else value


def _run(
    trades: list[dict[str, Any]],
    slots: int,
    factor: str,
    lower_better: bool,
    tie_break: str,
    costs: dict[str, Any],
) -> dict[str, Any]:
    prepared = [dict(trade) for trade in trades]
    for trade in prepared:
        trade["_portfolio_rank_score"] = _score(trade, factor, lower_better)
    result = bt.run_portfolio(
        prepared,
        costs,
        {
            "initial_cash": CASH,
            "max_positions": slots,
            "position_size_pct": 1.0 / slots,
            "lot_size": LOT,
            "score_mode": "external_causal_score",
            "tie_break": tie_break,
            "seed": 20261004,
        },
    )
    metrics = bmd.portfolio_risk_metrics(result["equity_curve"])
    summary = result["summary"]
    rejections = result["rejection_reasons"] or {}
    return {
        "total_return_pct": summary.get("total_return_pct"),
        "annualized_return_pct": metrics.get("annualized_return_pct"),
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "accepted": summary.get("accepted"),
        "candidates": summary.get("candidates"),
        "win_rate": summary.get("win_rate"),
        "avg_pnl_pct": summary.get("avg_pnl_pct"),
        "max_positions_rejections": rejections.get("max_positions"),
    }


def _diagnose(trades: list[dict[str, Any]], slots: int) -> dict[str, Any]:
    """候选拥挤度: 多少天候选数 > 可用仓位, 平均每天几个候选。"""
    per_day: dict[str, int] = {}
    for trade in trades:
        day = str(trade["entry_day"])
        per_day[day] = per_day.get(day, 0) + 1
    crowding = sorted(per_day.values(), reverse=True)
    contested = [count for count in crowding if count > slots]
    return {
        "trading_days_with_candidates": len(crowding),
        "days_candidates_exceed_slots": len(contested),
        "days_exceed_pct": round(100.0 * len(contested) / len(crowding), 1) if crowding else None,
        "average_candidates_per_day": round(st.mean(crowding), 2) if crowding else None,
        "max_candidates_in_a_day": crowding[0] if crowding else None,
        "total_candidates": len(trades),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--cache-dir", default=str(BASE_DIR / "rankcache"))
    parser.add_argument("--out", default=str(BASE_DIR / "same_day_selection.json"))
    args = parser.parse_args()

    config = load_config(str(pathlib.Path(args.config).expanduser().resolve()))
    costs = {**bt._resolve_execution_config(config), "exit_mode": "fixed", "exit_rules_scope": []}
    store: dict[str, list[dict[str, Any]]] = {}
    for pool in POOLS:
        path = pathlib.Path(args.cache_dir) / f"{pool}.pkl"
        store[pool] = pickle.loads(path.read_bytes())[pool]
    symbols = {str(t["symbol"]) for items in store.values() for t in items}
    bars = load_bars(symbols)
    for items in store.values():
        compute_factors(items, bars)

    report: dict[str, Any] = {
        "cash": CASH,
        "slots": list(SLOT_OPTIONS),
        "tie_breaks": list(TIE_BREAKS),
        "pools": {},
    }
    for pool, trades in store.items():
        pool_report: dict[str, Any] = {"trades": len(trades), "diagnosis": {}, "factors": {}}
        for slots in SLOT_OPTIONS:
            pool_report["diagnosis"][str(slots)] = _diagnose(trades, slots)
        for factor, label, lower_better in FACTORS:
            per_slot: dict[str, Any] = {}
            for slots in SLOT_OPTIONS:
                rows = {
                    tie_break: _run(trades, slots, factor, lower_better, tie_break, costs)
                    for tie_break in TIE_BREAKS
                }
                anns = [row["annualized_return_pct"] for row in rows.values()]
                dds = [row["max_drawdown_pct"] for row in rows.values()]
                shs = [row["sharpe_ratio"] for row in rows.values()]
                per_slot[str(slots)] = {
                    "by_tie_break": rows,
                    "median": {
                        "annualized_return_pct": round(st.median(anns), 2),
                        "max_drawdown_pct": round(st.median(dds), 2),
                        "sharpe_ratio": round(st.median(shs), 3),
                        "win_rate": rows[TIE_BREAKS[0]]["win_rate"],
                        "avg_pnl_pct": rows[TIE_BREAKS[0]]["avg_pnl_pct"],
                    },
                }
            pool_report["factors"][factor] = {"label": label, "by_slot": per_slot}
        report["pools"][pool] = pool_report

    pathlib.Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _print(report)
    print(f"\nwritten: {args.out}")
    return 0


def _print(report: dict[str, Any]) -> None:
    for pool, payload in report["pools"].items():
        print(f"\n{'=' * 96}\n{pool}  ({payload['trades']} 个候选)\n{'=' * 96}")
        for slots, diag in payload["diagnosis"].items():
            print(
                f"  只买 {slots} 只: {diag['days_candidates_exceed_slots']}/"
                f"{diag['trading_days_with_candidates']} 天候选数超过仓位 "
                f"({diag['days_exceed_pct']}%), 平均每天 {diag['average_candidates_per_day']} 个候选, "
                f"单日最多 {diag['max_candidates_in_a_day']} 个"
            )
        for slots in map(str, report["slots"]):
            print(f"\n  --- 只买 {slots} 只 (年化 / 回撤 / 夏普 / 胜率 / 单笔均值) ---")
            rows = []
            for factor, block in payload["factors"].items():
                med = block["by_slot"][slots]["median"]
                rows.append((factor, block["label"], med))
            for factor, label, med in rows:
                marker = " <-上界" if factor == "oracle" else ""
                print(
                    f"  {factor:<14} {med['annualized_return_pct']:>8} {med['max_drawdown_pct']:>7} "
                    f"{med['sharpe_ratio']:>7} {med['win_rate']:>7} {med['avg_pnl_pct']:>7}  {label}{marker}"
                )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
