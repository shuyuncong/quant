"""25 万 / 3~5 只 / 分批建仓 vs 一次买满 —— 四个维度的对照实验。

回答两个正交问题 (2x2):
  1. 每只股票怎么买: **一次买满** vs **分 3 批买入 (每批 1/3)**
  2. 同时持有几只: **3 只** vs **5 只** (本实验按"每只占总资金 1/N"实现)

合计 8 组, 全部在 25 万本金、零轴+底背离买入信号、fixed 卖出规则下跑。

"分 3 批"的实现口径 (必须写清楚, 否则数字不可比):
- 每只的目标总仓位 = 25 万 × 1/N; 分 3 批 = 每批 1/3 目标仓位。
- 第 1 批在 T+1 开盘买入, 第 2/3 批分别在 **5、10 个交易日**后买入 (无趋势过滤)。
- 期间若该仓位已被卖出 (止损/止盈/超时), 剩余批次**不再执行**。
- 后续批次按**实际市价**买入, 因此同一只股票的成本是加权平均 —— 这与"一次买满"的主要差异来源。
- 卖出统一用引擎算出的该笔 trade 的 exit_day / exit_reason / exit_price,
  即**分批建仓不改变卖出规则**, 只改变建仓路径。

用法:
    python entry_tranche_study.py --out entry_tranche_study.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import pickle
import statistics as st
import sys
from datetime import date
from typing import Any

import pandas as pd

import backtest_winrate as bt
import backtest_macd_divergence as bmd
from utils.helpers import load_config

BASE_DIR = pathlib.Path(__file__).resolve().parent
CASH = 250_000.0
LOT = 100
# 批次间隔 (交易日): 第 1 批在信号次日开盘, 第 2 批 +5 根, 第 3 批 +10 根。
TRANCHE_GAP = 5
MAX_DEFER = 3  # 涨停买不进时最多顺延几根 K 线
# 由 main() 注入: 批次买卖价需要 execution (_bar_price_limits)。
COSTS_CACHE: dict[str, Any] = {}
TIE_BREAKS = ("symbol_asc", "symbol_desc", "hash", "rotate")
SLOT_OPTIONS = (3, 4, 5)
TRANCHES = (1, 2, 3, 5)  # 1=一次买满; 2=首日2/3+1/3; 3/5=等权分批


def load_bars(symbols: set[str]) -> dict[str, pd.DataFrame]:
    """只加载用到的标的, 比遍历全宇宙快一个量级。

    必须取 **_qfq (前复权)**: 缓存里同时存在 _none 与 _qfq 两份,
    取错会让买入价与引擎口径不一致 (差几个百分点, 且随复权因子漂移)。
    """
    cache_dir = BASE_DIR / "cache" / "daily_history"
    bars: dict[str, pd.DataFrame] = {}
    for symbol in sorted(symbols):
        path = cache_dir / f"{symbol}_qfq.pkl"
        if not path.exists():
            continue
        try:
            raw = pd.read_pickle(path)
        except Exception:
            continue
        closed = bt.prepare_closed_bars(raw)
        if closed is None or closed.empty:
            continue
        bars[symbol] = closed
    return bars


def _tranche_entry_indices(
    symbol: str,
    closed: pd.DataFrame,
    dates: list[date],
    entry_day: str,
    exit_day: str,
    execution: dict[str, Any],
    tranches: int,
) -> list[tuple[int, float]]:
    """返回 [(bar 序号, 买入价)]。买入价用**当日开盘**, 涨停买不进则顺延一根。

    分批建仓的价格基准必须是盘中可成交价, 不能用收盘价 —— 收盘价既不是成交价,
    又会让"涨停当天开盘价买入"这种真实成交被错记成涨停价买入。
    批次间隔固定为 TRANCHE_GAP (第 2 批 +5 根, 第 3 批再 +5 根, 合计 +10)。
    """
    day_to_index = {item.isoformat(): index for index, item in enumerate(dates)}
    if entry_day not in day_to_index:
        return []
    picks: list[tuple[int, float]] = []
    target = tranches - 1  # 从"最早可成交"往后取 tranches 批
    index = day_to_index[entry_day]
    exit_key = exit_day
    while len(picks) < tranches and index < len(dates):
        # 每批最多顺延 MAX_DEFER 根 K 线, 仍买不进就放弃该批。
        deferred = 0
        while index < len(dates) and deferred <= MAX_DEFER:
            if dates[index].isoformat() >= exit_key:
                return picks
            price = float(closed.iloc[index]["open"])
            limits = bt._bar_price_limits(symbol, closed, index, execution)
            if price > 0 and not (limits is not None and price >= limits[0] - 0.0001):
                picks.append((index, price))
                break
            index += 1
            deferred += 1
        else:
            return picks
        if len(picks) > target:
            break
        # 下一批: 固定在上一批之后 TRANCHE_GAP 根 K 线。
        index += TRANCHE_GAP
    return picks


def build_fractional_trades(
    trades: list[dict[str, Any]],
    bars: dict[str, pd.DataFrame],
    slots: int,
    tranches: int,
    weights: tuple[float, ...] | None = None,
) -> list[dict[str, Any]]:
    """把每笔 trade 拆成 tranches 个子仓, 后续批次延后 TRANCHE_GAP 天建仓。

    weights 为各批次的**资金权重** (和 = 1, 长度 = tranches); None = 用 _weights_for
    的默认口径 (2 批 = 首日 2/3 + 5 个交易日后 1/3, N>=3 批 = 等权)。
    注意: 默认值只能是这一处 —— 若这里用等权、而调用方用 2/3+1/3,
    同一个"2 批"会出现两种口径, 结果不可比。
    子仓沿用原 trade 的出场日/出场价/出场原因 —— **卖出规则完全不变**,
    变的只是"分几次、各按什么比例把这一只的仓位建起来"。
    tranches=1 时必须与原 trade 完全一致。
    """
    if weights is None:
        weights = _weights_for(tranches)
    if len(weights) != tranches:
        raise ValueError(f"weights 长度 {len(weights)} 与批数 {tranches} 不符")
    out: list[dict[str, Any]] = []
    for trade in trades:
        symbol = str(trade["symbol"])
        closed = bars.get(symbol)
        entry_day = str(trade["entry_day"])
        exit_day = str(trade["exit_day"])
        if closed is None:
            if tranches == 1:
                out.append(dict(trade))
            continue
        dates = [item.date() for item in pd.to_datetime(closed["datetime"])]
        picks = _tranche_entry_indices(
            symbol, closed, dates, entry_day, exit_day, COSTS_CACHE, tranches
        )
        built = len(picks)
        if not built:
            continue
        # 未建成的批次**就这样不建** (快速止损的单子只用首批仓位承担亏损),
        # 资金留在组合里给其它候选 —— 绝不能把权重摊到已建批次上,
        # 否则"分批"会退化成"一次买满", 结论完全不同。
        for index, (_, price) in enumerate(picks):
            item = dict(trade)
            item["entry_price"] = price
            item["entry_day"] = dates[picks[index][0]].isoformat()
            item["_tranche_weight"] = weights[index]
            # 引擎以 symbol 作为持仓主键; 同一只股票的多个批次必须各占一个槽位。
            item["symbol"] = symbol if index == 0 else f"{symbol}T{index}"
            out.append(item)
    return out


def _weights_for(tranches: int) -> tuple[float, ...]:
    """批次资金权重:

    - 1 批: 一次买满。
    - 2 批: **首日 2/3, +5 个交易日补 1/3** (温和折中, 用户要求加测的这一档)。
    - N>=3 批: 等权 1/N (1/3 × 3 或 1/5 × 5)。
    """
    if tranches <= 1:
        return (1.0,)
    if tranches == 2:
        return (2.0 / 3.0, 1.0 / 3.0)
    return tuple(1.0 / tranches for _ in range(tranches))


def run_variant(
    trades: list[dict[str, Any]],
    bars: dict[str, pd.DataFrame],
    slots: int,
    tranches: int,
    tie_break: str,
    costs: dict[str, Any],
) -> dict[str, Any]:
    """把分批后的候选放进组合。

    单批买 "初始资金 × (1/只数) × 该批权重" (权重经 _tranche_weight 传给引擎)。
    槽位数按**最小批次**放大 (1/min_weight),
    否则 1/3 那种小批次会在满仓时被 max_positions 挡住 -> 资金闲置。
    """
    weights = _weights_for(tranches)
    prepared = build_fractional_trades(trades, bars, slots, tranches, weights)
    min_weight = min(weights)
    result = bt.run_portfolio(
        prepared,
        costs,
        {
            "initial_cash": CASH,
            "max_positions": int(round(slots / min_weight)),
            "position_size_pct": 1.0 / slots,
            "lot_size": LOT,
            "score_mode": "P0",
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
        "position_capacity_utilization_pct": result["attribution"].get(
            "position_capacity_utilization_pct"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--cache", default=str(BASE_DIR / "rankcache" / "diverge.pkl"))
    parser.add_argument("--out", default=str(BASE_DIR / "entry_tranche_study.json"))
    parser.add_argument(
        "--slots-list", default=",".join(str(item) for item in SLOT_OPTIONS),
        help="只数 (仓位上限) 列表, 如 1,2,...,10",
    )
    parser.add_argument(
        "--tranches-list", default=",".join(str(item) for item in TRANCHES),
        help="分批数列表; 1=一次买满, 2=2/3+1/3, N>=3=等权",
    )
    args = parser.parse_args()

    config = load_config(str(pathlib.Path(args.config).expanduser().resolve()))
    costs = {**bt._resolve_execution_config(config), "exit_mode": "fixed", "exit_rules_scope": []}
    store = pickle.loads(pathlib.Path(args.cache).read_bytes())
    trades = store["diverge"]
    COSTS_CACHE.update(costs)
    bars = load_bars({str(trade["symbol"]) for trade in trades})
    print(f"loaded bars for {len(bars)} symbols")

    report: dict[str, Any] = {
        "cash": CASH,
        "trades": len(trades),
        "tranche_gap": TRANCHE_GAP,
        "tie_breaks": list(TIE_BREAKS),
        "variants": {},
    }
    slots_list = [int(item) for item in args.slots_list.split(",") if item.strip()]
    tranches_list = [int(item) for item in args.tranches_list.split(",") if item.strip()]
    report["slots"] = slots_list
    report["tranches"] = tranches_list
    for slots in slots_list:
        for tranches in tranches_list:
            label = f"hold{slots}_buy{tranches}"
            rows: dict[str, Any] = {}
            for tie_break in TIE_BREAKS:
                rows[tie_break] = run_variant(
                    trades, bars, slots, tranches, tie_break, costs
                )
            anns = [row["annualized_return_pct"] for row in rows.values()]
            dds = [row["max_drawdown_pct"] for row in rows.values()]
            shs = [row["sharpe_ratio"] for row in rows.values()]
            report["variants"][label] = {
                "hold": slots,
                "tranches": tranches,
                "per_position_pct": round(100.0 / slots, 2),
                "per_tranche_weights": [round(value, 4) for value in _weights_for(tranches)],
                "by_tie_break": rows,
                "median": {
                    "annualized_return_pct": round(st.median(anns), 2),
                    "max_drawdown_pct": round(st.median(dds), 2),
                    "sharpe_ratio": round(st.median(shs), 3),
                    "return_over_drawdown": round(st.median(anns) / st.median(dds), 3)
                    if st.median(dds)
                    else None,
                },
            }
    pathlib.Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _print_grid(report)
    _print(report)
    print(f"\nwritten: {args.out}")
    return 0


def _print_grid(report: dict[str, Any]) -> None:
    """只数 × 分批数 的二维网格 —— 一眼看出"几只最合适、要不要分批"。"""
    slots_list = report.get("slots") or []
    tranches_list = report.get("tranches") or []
    if not slots_list or not tranches_list:
        return

    def cell(slots: int, tranches: int, key: str) -> str:
        payload = report["variants"].get(f"hold{slots}_buy{tranches}")
        if not payload:
            return "-"
        value = payload["median"][key]
        return f"{value:.2f}" if key != "sharpe_ratio" else f"{value:.3f}"

    for key, title in (
        ("annualized_return_pct", "年化收益 % (中位, 4 种排序)"),
        ("sharpe_ratio", "夏普 (中位, 4 种排序)"),
        ("max_drawdown_pct", "最大回撤 % (中位, 4 种排序)"),
    ):
        header = "  ".join(f"{('买满' if item == 1 else f'{item}批'):>8}" for item in tranches_list)
        print(f"\n{title}\n{'只数':>5}  {header}")
        for slots in slots_list:
            cells = "  ".join(f"{cell(slots, item, key):>8}" for item in tranches_list)
            print(f"{slots:>5}  {cells}")


def _print(report: dict[str, Any]) -> None:
    print(f"本金 {report['cash']:.0f}, 买入信号 = 零轴+底背离, 卖出 = fixed, "
          f"分批间隔 {report['tranche_gap']} 交易日")
    print(f"\n{'组合':<16} {'每只占比':>8} {'批次权重':>12} {'年化(中位)':>10} "
          f"{'回撤(中位)':>10} {'夏普(中位)':>10} {'收益/回撤':>9} {'成交':>6} {'平均持仓':>8}")
    for label, payload in report["variants"].items():
        med = payload["median"]
        any_row = next(iter(payload["by_tie_break"].values()))
        print(
            f"{label:<16} {payload['per_position_pct']:>7.2f}% {str(payload['per_tranche_weights']):>12} "
            f"{med['annualized_return_pct']:>10} {med['max_drawdown_pct']:>10} "
            f"{med['sharpe_ratio']:>10} {med['return_over_drawdown']:>9} "
            f"{any_row['accepted']:>6} {any_row['average_positions']:>8}"
        )
    print("\n单点 (symbol_asc) 明细:")
    for label, payload in report["variants"].items():
        row = payload["by_tie_break"]["symbol_asc"]
        print(f"  {label:<16} 年化 {row['annualized_return_pct']:>7} 回撤 {row['max_drawdown_pct']:>6} "
              f"夏普 {row['sharpe_ratio']:>6} 总收益 {row['total_return_pct']:>7} 占用率 "
              f"{row['position_capacity_utilization_pct']}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
