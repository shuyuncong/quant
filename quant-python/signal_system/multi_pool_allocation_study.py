"""多个策略池同时出信号时, 买哪个、怎么买 —— 资金分配实验。

背景: 现在系统里有三条独立买入信号同时在跑
  - 零轴+底背离 (diverge):  559 个信号/3.5 年  -> 稀疏, 单个信号质量最好
  - 年线趋势   (yearline): 18033 个信号        -> 超订
  - 日线零轴金叉(baseline): 29513 个信号        -> 超订, 单笔边际接近 0
它们共用同一个账户的仓位, 因此"买哪个"本质上是一个**资金分配**问题。

本实验比较两种分配机制 (都在 25 万 / 10 只 / 单笔 10% 下):

A. 优先级切分 (切蛋糕): 先给某个池子 X% 的仓位, 剩下的给另一个。
   对应的实盘动作: "先买稀有的, 买完剩下的钱再买年线"。
   实现: 在 run_portfolio 的 signal_priority 里只放被选中池子的信号类型。

B. 统一排序 (先到先得 + 排名因子): 所有池子混在一起, 按
   signal_priority (类别优先级) 或 external_causal_score (因子强弱) 排序后填仓。

同时给出一池/两池/三池的最优组合基线, 用于判断"多池是否值得"。

用法:
    python multi_pool_allocation_study.py --out multi_pool_allocation.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import pickle
import statistics as st
import sys
from typing import Any

import backtest_winrate as bt
import backtest_macd_divergence as bmd
from utils.helpers import load_config

BASE_DIR = pathlib.Path(__file__).resolve().parent
CASH = 250_000.0
SLOTS = 10
LOT = 100
TIE_BREAKS = ("symbol_asc", "symbol_desc", "hash", "rotate")

# 池 -> signal_type 列表 (引擎按 signal_type 过滤优先级)
POOL_TYPES: dict[str, tuple[str, ...]] = {
    "diverge": ("macd_divergence_bottom",),
    "yearline": ("yearline_A_breakout", "yearline_B_pullback"),
    "baseline": (
        "macd_golden_cross_pullback_confirmed_above",
        "macd_golden_cross_pullback_confirmed_near",
    ),
}
POOL_LABEL = {"diverge": "零轴+底背离", "yearline": "年线趋势", "baseline": "日线零轴金叉"}

# 共用槽位时的类别顺序 (引擎按 signal_priority 排序; 未列出的池排最后)
PRIORITY_ORDERS: tuple[tuple[str, ...], ...] = (
    ("diverge", "yearline"),
    ("yearline", "diverge"),
    ("diverge", "baseline"),
    ("baseline", "diverge"),
    ("yearline", "baseline"),
    ("baseline", "yearline"),
)
# 三池同时可用时的类别优先级
THREE_POOL_ORDERS: tuple[tuple[str, ...], ...] = (
    ("diverge", "yearline", "baseline"),
    ("diverge", "baseline", "yearline"),
    ("yearline", "diverge", "baseline"),
)
# 分池独立执行: 每个池子拿一份独立资金与独立仓位数 (互补干扰, 但也互不占用)
CAPITAL_SPLITS: tuple[tuple[tuple[str, float], ...], ...] = (
    (("diverge", 0.7), ("yearline", 0.3)),
    (("diverge", 0.5), ("yearline", 0.5)),
    (("diverge", 0.3), ("yearline", 0.7)),
    (("diverge", 0.5), ("yearline", 0.3), ("baseline", 0.2)),
)
RANK_FACTORS = ("below_line", "high_vol", "low_vol")  # 仅在"统一排序"里作为候选因子


def _types_for(pools: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for pool in pools:
        out.extend(POOL_TYPES[pool])
    return out


def _load_store(cache_dir: pathlib.Path) -> dict[str, list[dict[str, Any]]]:
    store: dict[str, list[dict[str, Any]]] = {}
    for pool in POOL_TYPES:
        path = cache_dir / f"{pool}.pkl"
        if not path.exists():
            raise SystemExit(f"missing cache {path}; run ranking_study --build-cache first")
        store[pool] = pickle.loads(path.read_bytes())[pool]
    return store


def _rank_score(trade: dict[str, Any], mode: str) -> float:
    """排名因子 (只用信号当日可见信息, 见 ranking_study 的结论)。"""
    if mode == "below_line":
        # 越**低于**年线越优先 (不是"越贴近"): 与 10.2 的 near_line 同一定义。
        close = float(trade.get("signal_close") or 0.0)
        ma_long = float(trade.get("ma_long") or 0.0)
        return -(close / ma_long - 1.0) if close > 0 and ma_long > 0 else 0.0
    if mode == "high_vol":
        return float(trade.get("volume_ratio") or 0.0)
    if mode == "low_vol":
        return -float(trade.get("volume_ratio") or 0.0)
    return 0.0


def _run(
    trades: list[dict[str, Any]],
    priority: list[str],
    tie_break: str,
    costs: dict[str, Any],
    rank_mode: str | None = None,
) -> dict[str, Any]:
    prepared = [dict(trade) for trade in trades]
    score_mode = "P0"
    if rank_mode:
        # 排名因子只在同一天、同一类别内部起作用; 类别顺序仍由 signal_priority 决定。
        for trade in prepared:
            trade["_portfolio_rank_score"] = _rank_score(trade, rank_mode)
        score_mode = "external_causal_score"
    result = bt.run_portfolio(
        prepared,
        costs,
        {
            "initial_cash": CASH,
            "max_positions": SLOTS,
            "position_size_pct": 1.0 / SLOTS,
            "lot_size": LOT,
            "score_mode": score_mode,
            "signal_priority": priority,
            "tie_break": tie_break,
            "seed": 20261004,
        },
    )
    metrics = bmd.portfolio_risk_metrics(result["equity_curve"])
    summary = result["summary"]
    return {
        "total_return_pct": summary.get("total_return_pct"),
        "annualized_return_pct": metrics.get("annualized_return_pct"),
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "accepted": summary.get("accepted"),
        "candidates": summary.get("candidates"),
        "win_rate": summary.get("win_rate"),
        "average_positions": result["attribution"].get("average_positions"),
    }


def _evaluate_capital_split(
    store: dict[str, list[dict[str, Any]]],
    allocation: tuple[tuple[str, float], ...],
    costs: dict[str, Any],
) -> dict[str, Any]:
    """分池独立执行: 每个池子拿 资金×占比 与 仓位数×占比, 各跑各的组合。

    子账户的权益曲线按天相加得到组合层总权益, 这样年化/回撤/夏普与
    "共用槽位"的结果可以直接比较。分池的价值在于两个池子互不抢仓 ——
    若分池明显优于共用槽位, 说明池子之间的挤占是真实损失。
    """
    rows: dict[str, Any] = {}
    for tie_break in TIE_BREAKS:
        curves: list[list[dict[str, Any]]] = []
        sub_cash: list[float] = []
        sub_results = []
        accepted_total = 0
        for pool, ratio in allocation:
            cash = CASH * ratio
            slots = max(int(round(SLOTS * ratio)), 1)
            result = bt.run_portfolio(
                [dict(item) for item in store[pool]],
                costs,
                {
                    "initial_cash": cash,
                    "max_positions": slots,
                    "position_size_pct": 1.0 / slots,
                    "lot_size": LOT,
                    "score_mode": "P0",
                    "signal_priority": _types_for((pool,)),
                    "tie_break": tie_break,
                    "seed": 20261004,
                },
            )
            curves.append(result["equity_curve"])
            sub_cash.append(cash)
            accepted_total += int(result["summary"].get("accepted") or 0)
            sub_results.append(
                {
                    "pool": pool,
                    "ratio": ratio,
                    "cash": cash,
                    "slots": slots,
                    "final_equity": round(
                        float(result["equity_curve"][-1]["equity"]), 2
                    ),
                    "accepted": result["summary"].get("accepted"),
                    "total_return_pct": result["summary"].get("total_return_pct"),
                }
            )
        combined = _combine_equity(curves, sub_cash)
        metrics = bmd.portfolio_risk_metrics(combined)
        rows[tie_break] = {
            "total_return_pct": round(
                float(combined[-1]["equity"]) / CASH * 100.0 - 100.0, 2
            ),
            "annualized_return_pct": metrics.get("annualized_return_pct"),
            "max_drawdown_pct": _combined_max_drawdown(combined),
            "sharpe_ratio": metrics.get("sharpe_ratio"),
            "accepted": accepted_total,
            "sub_results": sub_results,
        }
    shs = [row["sharpe_ratio"] for row in rows.values()]
    anns = [row["annualized_return_pct"] for row in rows.values()]
    dds = [row["max_drawdown_pct"] for row in rows.values()]
    accepted = rows[TIE_BREAKS[0]]["accepted"]
    return {
        "by_tie_break": rows,
        "accepted": accepted,
        "median": {
            "annualized_return_pct": round(st.median(anns), 2),
            "max_drawdown_pct": round(st.median(dds), 2),
            "sharpe_ratio": round(st.median(shs), 3),
            "return_over_drawdown": round(st.median(anns) / st.median(dds), 3)
            if st.median(dds)
            else None,
            "sharpe_min": round(min(shs), 3),
            "sharpe_max": round(max(shs), 3),
        },
    }


def _combine_equity(
    curves: list[list[dict[str, Any]]], initial_cash: list[float]
) -> list[dict[str, Any]]:
    """把多个子账户的日频权益按天求和, 得到组合层总权益曲线。

    关键: 子账户的曲线只在**它有信号/持仓的那天**打点, 不能只在"当天有数据"的
    日子里相加 —— 否则缺失日会把该子账户的权益当成 0, 曲线出现锯齿,
    最大回撤会被算成 80% 这种荒唐值。正确做法是按**全部日期的并集**推进,
    子账户在它没有数据点的日子里沿用上一日权益 (首日之前等于它的初始资金)。
    """
    days = sorted({str(point["day"]) for curve in curves for point in curve})
    series: list[tuple[dict[str, float], float]] = []
    for curve, cash in zip(curves, initial_cash):
        series.append(({str(point["day"]): float(point["equity"]) for point in curve}, float(cash)))
    current = [cash for cash in initial_cash]
    combined: list[dict[str, Any]] = []
    for day in days:
        for index, (mapping, _) in enumerate(series):
            if day in mapping:
                current[index] = mapping[day]
        combined.append({"day": day, "equity": sum(current)})
    return combined


def _combined_max_drawdown(curve: list[dict[str, Any]]) -> float:
    peak = 0.0
    worst = 0.0
    for point in curve:
        value = float(point["equity"])
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return round(worst * 100.0, 2)


def _evaluate(
    store: dict[str, list[dict[str, Any]]],
    pools: tuple[str, ...],
    priority: list[str],
    costs: dict[str, Any],
    rank_mode: str | None = None,
) -> dict[str, Any]:
    trades = [dict(item) for pool in pools for item in store[pool]]
    rows = {tb: _run(trades, priority, tb, costs, rank_mode) for tb in TIE_BREAKS}
    anns = [row["annualized_return_pct"] for row in rows.values()]
    dds = [row["max_drawdown_pct"] for row in rows.values()]
    shs = [row["sharpe_ratio"] for row in rows.values()]
    return {
        "candidates": rows[TIE_BREAKS[0]]["candidates"],
        "accepted": rows[TIE_BREAKS[0]]["accepted"],
        "by_tie_break": rows,
        "median": {
            "annualized_return_pct": round(st.median(anns), 2),
            "max_drawdown_pct": round(st.median(dds), 2),
            "sharpe_ratio": round(st.median(shs), 3),
            "return_over_drawdown": round(st.median(anns) / st.median(dds), 3)
            if st.median(dds)
            else None,
            "sharpe_min": round(min(shs), 3),
            "sharpe_max": round(max(shs), 3),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--cache-dir", default=str(BASE_DIR / "rankcache"))
    parser.add_argument("--out", default=str(BASE_DIR / "multi_pool_allocation.json"))
    args = parser.parse_args()

    config = load_config(str(pathlib.Path(args.config).expanduser().resolve()))
    costs = {**bt._resolve_execution_config(config), "exit_mode": "fixed", "exit_rules_scope": []}
    store = _load_store(pathlib.Path(args.cache_dir))
    report: dict[str, Any] = {
        "cash": CASH, "slots": SLOTS, "tie_breaks": list(TIE_BREAKS),
        "pools": {pool: len(items) for pool, items in store.items()},
        "single_pool": {}, "priority_shared": {}, "three_pool": {}, "unified_ranking": {},
        "capital_split": {},
    }

    # 基线: 每个池子单独用 (整只账户都给它)。
    for pool in POOL_TYPES:
        report["single_pool"][pool] = _evaluate(
            store, (pool,), _types_for((pool,)), costs
        )

    # 共用槽位: 两池混排, 由 signal_priority 决定同日谁先占仓位。
    for order in PRIORITY_ORDERS:
        key = "_".join(order)
        report["priority_shared"][key] = _evaluate(
            store, order, _types_for(order), costs
        )

    # 三池: 不同类别优先级
    for order in THREE_POOL_ORDERS:
        report["three_pool"]["_".join(order)] = _evaluate(
            store, ("diverge", "yearline", "baseline"), _types_for(order), costs
        )

    # 统一排序: 三池混排, 类别优先级 fixed, 候选因子决定同日谁先买
    base_priority = _types_for(("diverge", "yearline", "baseline"))
    report["unified_ranking"]["priority_only"] = _evaluate(
        store, ("diverge", "yearline", "baseline"), base_priority, costs
    )
    for factor in RANK_FACTORS:
        report["unified_ranking"][factor] = _evaluate(
            store, ("diverge", "yearline", "baseline"), base_priority, costs, factor
        )

    # 分池独立: 每个池子独立资金 + 独立仓位数, 互不抢占。
    for allocation in CAPITAL_SPLITS:
        key = "+".join(f"{pool}{int(ratio * 100)}" for pool, ratio in allocation)
        report["capital_split"][key] = _evaluate_capital_split(store, allocation, costs)

    pathlib.Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _print(report)
    print(f"\nwritten: {args.out}")
    return 0


def _print(report: dict[str, Any]) -> None:
    print(f"本金 {report['cash']:.0f} / {report['slots']} 只 / 单笔 {100/report['slots']:.0f}%")
    print("池子信号数:", {POOL_LABEL[k]: v for k, v in report["pools"].items()})

    def line(label: str, block: dict[str, Any]) -> str:
        m = block["median"]
        return (
            f"{label:<26} 年化 {m['annualized_return_pct']:>7} 回撤 {m['max_drawdown_pct']:>6} "
            f"夏普 {m['sharpe_ratio']:>6} (区间 {m['sharpe_min']}~{m['sharpe_max']}) "
            f"收益/回撤 {m['return_over_drawdown']:>5} 成交 {block['accepted']:>4}"
        )

    print("\n=== 基线: 只用一个池子 ===")
    for pool, block in report["single_pool"].items():
        print(line(POOL_LABEL[pool], block))

    print("\n=== 共用槽位: 先买谁 (signal_priority 决定同日优先) ===")
    for key, block in report["priority_shared"].items():
        pools = tuple(key.split("_"))
        print(line(" > ".join(POOL_LABEL[p] for p in pools), block))

    print("\n=== 三池同时可用: 类别优先级 ===")
    for key, block in report["three_pool"].items():
        pools = tuple(key.split("_"))
        print(line(" > ".join(POOL_LABEL[p] for p in pools), block))

    print("\n=== 三池混排: 统一排序 (先到先得 + 因子) ===")
    for key, block in report["unified_ranking"].items():
        print(line(key, block))

    print("\n=== 分池独立: 每个池子一份钱 + 一份仓位, 互不抢占 ===")
    for key, block in report["capital_split"].items():
        print(line(key, block))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
