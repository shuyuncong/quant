"""仓位满了又来新信号 —— 该忽略、轮换、还是加仓?

场景（用户提问）: 今天买了 3 只底背离, 第二天又筛出新的底背离, 怎么办?

三件事, 全部只用已有数据 (rankcache + 引擎) 实测:
1. **机会成本**: 被丢弃的新信号本身质量如何? (对比已成交的)
2. **轮换代价**: 要腾仓位就得卖掉手中的仓位 —— 那些仓位在"新信号到来日"之后
   还能赚多少? (持有中的仓位用 _mark_prices 逐日估值, 不许用未来信息做决策,
   但**衡量放弃的收益**必须用到后续价格, 这是事后统计而不是交易规则)
3. **多接信号是否有用**: 把仓位上限放大到几乎每个信号都能进, 看收益是否变好。

结论 (25 万 / 4 只 / 25% 单笔):
- 被丢弃的新信号：均值 +1.06%、PF 1.19 —— 有正期望, 但远低于已成交的 +4.55%、PF 2.04
- 手上仓位在信号日之后的剩余收益：均值 **+2.40%** —— 比新信号更高
- 轮换双边成本 ≈ 0.23% (佣金 0.03% + 印花税 0.1% + 滑点 0.05%×2)
- 放大仓位上限 (4→30) 让年化从 38.71% 掉到 14.82%
=> **忽略是对的**: 轮换要放弃 +2.40% 去换 +1.06% 再付 0.23% 成本, 期望为负;
   "多接信号"不是增收而是摊薄。

用法:
    python full_book_policy_study.py --out full_book_policy.json
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
LOT = 100
SLOT_CASES = ((4, 0.25), (5, 0.25))
LADDER = (4, 10, 20, 30)
TIE_BREAK = "symbol_asc"  # 与用户实盘口径一致


def _describe(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(t["pnl_pct"]) for t in trades if t and t.get("pnl_pct") is not None]
    if not pnls:
        return {"count": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "count": len(pnls),
        "mean_pnl_pct": round(st.mean(pnls), 2),
        "median_pnl_pct": round(st.median(pnls), 2),
        "win_rate": round(100.0 * len(wins) / len(pnls), 1),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
        if losses and sum(losses)
        else None,
        "best_pct": round(max(pnls), 1),
        "worst_pct": round(min(pnls), 1),
    }


def _run(
    trades: list[dict[str, Any]], slots: int, pct: float, costs: dict[str, Any]
) -> dict[str, Any]:
    return bt.run_portfolio(
        [dict(t) for t in trades],
        costs,
        {
            "initial_cash": CASH,
            "max_positions": slots,
            "position_size_pct": pct,
            "lot_size": LOT,
            "score_mode": "P0",
            "tie_break": TIE_BREAK,
            "seed": 20261004,
        },
    )


def _forgone_upside(
    result: dict[str, Any], index: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    """轮换要付出的代价: 持仓中的仓位在"新信号到来日"之后还能赚多少。"""
    held = []
    for entry in result["accepted_entries"]:
        trade = index.get((str(entry["symbol"]), str(entry["signal_day"])))
        if trade:
            # 建仓日以 trade 记录为准 (accepted_entries 是引擎的轻量记录, 字段可能更少)。
            held.append((str(trade["entry_day"]), str(trade["exit_day"]), trade))
    remaining: list[float] = []
    for rejection in result["rejections"]:
        if rejection["reason"] != "max_positions":
            continue
        day = str(rejection["entry_day"])
        for entry_day, exit_day, trade in held:
            if entry_day <= day < exit_day:
                close = (trade.get("_mark_prices") or {}).get(day)
                if close:
                    remaining.append(float(trade["exit_price"]) / float(close) - 1.0)
    if not remaining:
        return {"observations": 0}
    return {
        "observations": len(remaining),
        "mean_remaining_pct": round(st.mean(remaining) * 100.0, 2),
        "median_remaining_pct": round(st.median(remaining) * 100.0, 2),
        "positive_share_pct": round(
            100.0 * sum(1 for item in remaining if item > 0) / len(remaining), 1
        ),
        "best_remaining_pct": round(max(remaining) * 100.0, 1),
        "worst_remaining_pct": round(min(remaining) * 100.0, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--cache", default=str(BASE_DIR / "rankcache" / "diverge.pkl"))
    parser.add_argument("--out", default=str(BASE_DIR / "full_book_policy.json"))
    args = parser.parse_args()

    config = load_config(str(pathlib.Path(args.config).expanduser().resolve()))
    costs = {**bt._resolve_execution_config(config), "exit_mode": "fixed", "exit_rules_scope": []}
    trades = pickle.loads(pathlib.Path(args.cache).read_bytes())["diverge"]
    index = {(str(t["symbol"]), str(t["signal_day"])): t for t in trades}

    report: dict[str, Any] = {
        "cash": CASH,
        "round_trip_cost_pct": 0.23,
        "cases": {},
        "slot_ladder": {},
    }
    for slots, pct in SLOT_CASES:
        result = _run(trades, slots, pct, costs)
        case: dict[str, Any] = {
            "slots": slots,
            "position_size_pct": pct,
            "accepted": _describe(result["trades"]),
            "rejection_reasons": {
                key: value
                for key, value in (result["rejection_reasons"] or {}).items()
                if value
            },
        }
        for reason in ("max_positions", "symbol_already_held"):
            items = [
                index.get((str(item["symbol"]), str(item["signal_day"])))
                for item in result["rejections"]
                if item["reason"] == reason
            ]
            case[f"rejected_{reason}"] = _describe([item for item in items if item])
        case["forgone_upside"] = _forgone_upside(result, index)
        report["cases"][f"slots{slots}"] = case

    for slots in LADDER:
        result = _run(trades, slots, 1.0 / slots, costs)
        metrics = bmd.portfolio_risk_metrics(result["equity_curve"])
        summary = result["summary"]
        report["slot_ladder"][str(slots)] = {
            "accepted": summary.get("accepted"),
            "candidates": summary.get("candidates"),
            "annualized_return_pct": metrics.get("annualized_return_pct"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "sharpe_ratio": metrics.get("sharpe_ratio"),
            "average_positions": result["attribution"].get("average_positions"),
        }

    pathlib.Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _print(report)
    print(f"\nwritten: {args.out}")
    return 0


def _print(report: dict[str, Any]) -> None:
    for key, case in report["cases"].items():
        print(f"\n=== 25 万 / {key} (单笔 {case['position_size_pct']:.0%}) ===")
        acc = case["accepted"]
        print(
            f"  已成交 {acc['count']:>4} 笔: 均值 {acc['mean_pnl_pct']:>6}% 中位 {acc['median_pnl_pct']:>6}% "
            f"胜率 {acc['win_rate']:>5}% PF {acc['profit_factor']}"
        )
        for reason in ("max_positions", "symbol_already_held"):
            block = case.get(f"rejected_{reason}") or {}
            if block.get("count"):
                print(
                    f"  被丢弃({reason}) {block['count']:>4} 笔: 均值 {block['mean_pnl_pct']:>6}% "
                    f"胜率 {block['win_rate']:>5}% PF {block['profit_factor']}"
                )
        forgone = case["forgone_upside"]
        if forgone.get("observations"):
            print(
                f"  手中仓位在信号日之后的剩余收益: 均值 {forgone['mean_remaining_pct']:>5}% "
                f"(n={forgone['observations']}, 正收益占 {forgone['positive_share_pct']}%)"
            )
    print("\n=== 放大仓位上限 (每个信号几乎都能进) ===")
    print(f"{'仓位上限':>7} {'成交':>9} {'年化':>8} {'回撤':>7} {'夏普':>7} {'平均持仓':>8}")
    for slots, row in report["slot_ladder"].items():
        print(
            f"{slots:>7} {str(row['accepted']) + '/' + str(row['candidates']):>9} "
            f"{row['annualized_return_pct']:>8} {row['max_drawdown_pct']:>7} "
            f"{row['sharpe_ratio']:>7} {row['average_positions']:>8}"
        )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
