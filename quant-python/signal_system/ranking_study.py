"""当日多只信号同时触发时, 该买哪只? —— 排序因子的可测量实验。

现状 (score_mode="P0"): 组合层**完全不做排名**。当天有 N 个候选、只剩 k 个仓位时,
先挑的是 `order_bucket` 的排序结果 (默认 symbol_asc = 按代码从小到大), 够仓位就停。
也就是说"买哪只"目前由**股票代码**决定, 与信号强弱无关。

本实验在同一批 trades 上（逐笔只算一次）比较不同的**截断规则**:
- priority   : 现行 P0 口径 (按代码/排序, 无排名因子)
- high_vol   : 按放量倍数从大到小 (越大越买)
- low_vol    : 按放量倍数从小到大 (高基数下的"量比越小越干净"假设)
- high_drive : 按距年线相对位置从高到低 (强度)
- near_line  : 按距年线相对位置从低到高 (越贴近年线买, 止损空间越小)

同时扫仓位档位 (5 / 20 / 50), 因为**排名只在被截断时才起作用**: 仓位足够时
所有候选都能成交, 排序因子不影响任何结果。这决定了实验能否测出差异。

用法:
    python ranking_study.py --cache D:/tmp/rank_cache.pkl --build-cache
    python ranking_study.py --cache D:/tmp/rank_cache.pkl
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path
from typing import Any

import pandas as pd

import backtest_macd_divergence as bmd
from utils.helpers import load_config

BASE_DIR = Path(__file__).resolve().parent
BUYS = ("yearline", "diverge", "baseline")
SLOTS = (5, 20, 50)
EXIT_TOKEN = "fixed"
RANK_MODES = ("priority", "high_vol", "low_vol", "high_drive", "near_line")


def build_trades_cache(
    config_path: str,
    start: str,
    end: str,
    out: Path,
    buy_token: str,
    limit: int = 0,
) -> None:
    """逐笔只算一次并缓存: 排名实验要在同一批交易上反复截断, 重算太贵。

    按买入信号分文件, 这样三个信号可以并行构建 (单个进程串行太慢)。
    """
    import backtest_winrate as bt

    config = load_config(str(Path(config_path).expanduser().resolve()))
    settings = bmd.resolve_divergence_config(config)
    costs = bt._resolve_execution_config(config)
    costs = {**costs, "exit_mode": "fixed", "exit_rules_scope": []}
    start_date = pd.Timestamp(start).date()
    end_date = pd.Timestamp(end).date()
    paths = sorted((BASE_DIR / "cache" / "daily_history").glob("*_qfq.pkl"))
    if limit:
        paths = paths[:limit]
    trades: list[dict[str, Any]] = []
    for index, path in enumerate(paths, start=1):
        symbol = path.name.split("_")[0]
        try:
            raw = pd.read_pickle(path)
        except Exception:
            continue
        closed = bt.prepare_closed_bars(raw)
        if closed is None or len(closed) < bmd.MIN_BARS:
            continue
        if buy_token == "baseline":
            buys = bmd.build_production_entries(symbol, closed, config)
        elif buy_token == "yearline":
            buys = bmd.build_yearline_entries(symbol, closed)
        else:
            buys, _ = bmd.build_entries(symbol, closed, settings)
        window = [
            item
            for item in buys
            if start_date <= pd.Timestamp(str(item["day"])).date() <= end_date
        ]
        if not window:
            continue
        dates = [item.date() for item in pd.to_datetime(closed["datetime"])]
        for item in window:
            trade, _ = bt.simulate_single_trade(symbol, closed, dates, item, {}, costs)
            if trade is None:
                continue
            # 排名因子必须来自**信号当日**可见的信息, 否则是未来函数。
            trade["volume_ratio"] = float(item.get("volume_ratio") or 0.0)
            trade["signal_close"] = float(item.get("price") or 0.0)
            trade["ma_long"] = _ma_long_at(closed, str(item["day"]))
            trades.append(trade)
        if index % 1000 == 0:
            print(f"  {buy_token}: {index}/{len(paths)} symbols, {len(trades)} trades", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pickle.dumps({buy_token: trades}))
    print(f"{buy_token}: {len(trades)} trades -> {out}", flush=True)


def _ma_long_at(closed: pd.DataFrame, day: str) -> float | None:
    """年线取值 (只用信号当日及之前的数据)。"""
    dates = pd.to_datetime(closed["datetime"])
    target = pd.Timestamp(day)
    upto = closed[dates <= target]
    if len(upto) < 250:
        return None
    return float(pd.to_numeric(upto["close"], errors="coerce").tail(250).mean())


def _drive(trade: dict[str, Any]) -> float:
    """距年线的相对位置 (强度代理): 越大说明离年线越远。"""
    close = float(trade.get("signal_close") or 0.0)
    ma_long = float(trade.get("ma_long") or 0.0)
    if close <= 0 or ma_long <= 0:
        return 0.0
    return close / ma_long - 1.0


def _apply_rank(trades: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """把排名因子写进 _portfolio_rank_score, 由引擎按分数降序优先成交。

    只影响"同一天候选超过剩余仓位时先买谁"; 不影响任何单笔的进出场。
    """
    ranked = [dict(trade) for trade in trades]
    for trade in ranked:
        if mode == "priority":
            trade["_portfolio_rank_score"] = 0.0
        elif mode == "high_vol":
            trade["_portfolio_rank_score"] = float(trade.get("volume_ratio") or 0.0)
        elif mode == "low_vol":
            trade["_portfolio_rank_score"] = -float(trade.get("volume_ratio") or 0.0)
        elif mode == "high_drive":
            trade["_portfolio_rank_score"] = _drive(trade)
        elif mode == "near_line":
            trade["_portfolio_rank_score"] = -_drive(trade)
        else:
            raise SystemExit(f"unknown rank mode: {mode}")
    return ranked


def _portfolio(trades: list[dict[str, Any]], slots: int, rank_mode: str, costs: dict, cash: float):
    import backtest_winrate as bt

    return bt.run_portfolio(
        _apply_rank(trades, rank_mode),
        costs,
        {
            "initial_cash": cash,
            "max_positions": slots,
            "position_size_pct": 1.0 / slots,
            "lot_size": int(costs.get("lot_size") or 100),
            "score_mode": "external_causal_score",
            "tie_break": "symbol_asc",
            "seed": 20261004,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--start", default="2023-05-01")
    parser.add_argument("--end", default="2026-10-04")
    parser.add_argument("--cache-dir", default=str(BASE_DIR / "rankcache"))
    parser.add_argument("--limit", type=int, default=0, help="只取前 N 个标的; 冒烟")
    parser.add_argument("--build-cache", action="store_true")
    parser.add_argument("--slots", default=",".join(str(item) for item in SLOTS))
    parser.add_argument("--cash", type=float, default=1_000_000.0)
    parser.add_argument("--out", default=str(BASE_DIR / "ranking_study.json"))
    parser.add_argument("--build-one", default=None, help="内部: 只构建单个策略的逐笔缓存")
    args = parser.parse_args()

    if args.build_one:
        build_trades_cache(
            args.config,
            args.start,
            args.end,
            Path(args.cache_dir) / f"{args.build_one}.pkl",
            args.build_one,
            args.limit,
        )
        return 0

    cache_dir = Path(args.cache_dir)
    if args.build_cache:
        import subprocess

        cache_dir.mkdir(parents=True, exist_ok=True)
        # 三个策略各起一个进程: 构建是 CPU 密集的纯 Python, 线程会被 GIL 串行化。
        procs = []
        for buy_token in BUYS:
            log = cache_dir / f"{buy_token}.log"
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--build-one",
                buy_token,
                "--cache-dir",
                str(cache_dir),
                "--config",
                args.config,
                "--start",
                args.start,
                "--end",
                args.end,
            ]
            if args.limit:
                command.extend(["--limit", str(args.limit)])
            procs.append((buy_token, subprocess.Popen(command, stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT)))
        for buy_token, proc in procs:
            code = proc.wait()
            print(f"cache {buy_token}: exit {code}", flush=True)
            if code != 0:
                raise SystemExit(f"cache build failed for {buy_token}")

    store: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(cache_dir.glob("*.pkl")):
        store.update(pickle.loads(path.read_bytes()))
    missing = [buy for buy in BUYS if buy not in store]
    if missing:
        raise SystemExit(f"missing caches for {missing} in {cache_dir}; run with --build-cache")

    import backtest_winrate as bt

    config = load_config(str(Path(args.config).expanduser().resolve()))
    costs = bt._resolve_execution_config(config)
    costs = {**costs, "exit_mode": "fixed", "exit_rules_scope": []}
    slots_list = [int(item) for item in args.slots.split(",") if item.strip()]

    report: dict[str, Any] = {"slots": slots_list, "rank_modes": list(RANK_MODES), "arms": {}}
    for buy_token, trades in store.items():
        arm: dict[str, Any] = {"trade_count": len(trades), "by_slot": {}}
        for slots in slots_list:
            rows: dict[str, Any] = {}
            for mode in RANK_MODES:
                result = _portfolio(trades, slots, mode, costs, args.cash)
                summary = result["summary"]
                metrics = bmd.portfolio_risk_metrics(result["equity_curve"])
                rows[mode] = {
                    "total_return_pct": summary.get("total_return_pct"),
                    "annualized_return_pct": metrics.get("annualized_return_pct"),
                    "max_drawdown_pct": summary.get("max_drawdown_pct"),
                    "sharpe_ratio": metrics.get("sharpe_ratio"),
                    "accepted": summary.get("accepted"),
                    "candidates": summary.get("candidates"),
                    "win_rate": summary.get("win_rate"),
                    "average_positions": result["attribution"].get("average_positions"),
                }
            arm["by_slot"][str(slots)] = rows
        report["arms"][buy_token] = arm

    # 混合组合: 25 万 / 5 仓位下, 年线趋势(超订)与零轴+底背离(稀疏)共用仓位。
    # 谁优先决定稀疏策略还能不能挤进去。
    if {"diverge", "yearline"} <= set(store):
        blends: dict[str, Any] = {}
        for label, priority in (
            ("diverge_first", ["macd_divergence_bottom", "yearline_A_breakout", "yearline_B_pullback"]),
            ("yearline_first", ["yearline_A_breakout", "yearline_B_pullback", "macd_divergence_bottom"]),
        ):
            merged = _blend_stores(store, "diverge", "yearline")
            per_slot: dict[str, Any] = {}
            for slots in slots_list:
                result = _blend_portfolio(merged, slots, costs, args.cash, priority)
                summary = result["summary"]
                metrics = bmd.portfolio_risk_metrics(result["equity_curve"])
                per_slot[str(slots)] = {
                    "total_return_pct": summary.get("total_return_pct"),
                    "annualized_return_pct": metrics.get("annualized_return_pct"),
                    "max_drawdown_pct": summary.get("max_drawdown_pct"),
                    "sharpe_ratio": metrics.get("sharpe_ratio"),
                    "accepted": summary.get("accepted"),
                    "candidates": summary.get("candidates"),
                    "win_rate": summary.get("win_rate"),
                    "average_positions": result["attribution"].get("average_positions"),
                }
            blends[label] = per_slot
        report["blends"] = blends
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _print_report(report)
    print(f"\nwritten: {args.out}")
    return 0


def _blend_stores(
    store: dict[str, list[dict[str, Any]]],
    primary: str,
    secondary: str,
) -> list[dict[str, Any]]:
    """两个策略共用同一批仓位: 直接拼接, 由 signal_priority 决定同日冲突谁先成交。

    同一只股票同一天同时出现两个信号时, 引擎会按优先级只保留一个候选,
    因此不需要在这里去重。
    """
    return [dict(trade) for trade in store[primary]] + [
        dict(trade) for trade in store[secondary]
    ]


def _blend_portfolio(
    trades: list[dict[str, Any]],
    slots: int,
    costs: dict,
    cash: float,
    priority: list[str],
):
    """score_mode 固定 P0: 混排时只按信号的类别优先级, 不再叠加排名因子。"""
    import backtest_winrate as bt

    return bt.run_portfolio(
        trades,
        costs,
        {
            "initial_cash": cash,
            "max_positions": slots,
            "position_size_pct": 1.0 / slots,
            "lot_size": int(costs.get("lot_size") or 100),
            "score_mode": "P0",
            "signal_priority": priority,
            "tie_break": "symbol_asc",
            "seed": 20261004,
        },
    )


def _print_report(report: dict[str, Any]) -> None:
    for buy_token, arm in report["arms"].items():
        print(f"\n{'=' * 92}\n{buy_token}  ({arm['trade_count']} trades)\n{'=' * 92}")
        for slots, rows in arm["by_slot"].items():
            base = rows["priority"]
            print(f"\n  仓位档 {slots}: 成交 {base['accepted']}/{base['candidates']}")
            print(f"  {'排名规则':<12} {'年化%':>8} {'回撤%':>8} {'夏普':>7} {'总收益%':>9} {'胜率':>7}")
            for mode, row in rows.items():
                print(
                    f"  {mode:<12} {row['annualized_return_pct']:>8} "
                    f"{row['max_drawdown_pct']:>8} {row['sharpe_ratio']:>7} "
                    f"{row['total_return_pct']:>9} {row['win_rate']:>7}"
                )
    for label, per_slot in (report.get("blends") or {}).items():
        print(f"\n{'=' * 92}\n混合组合: {label} (零轴+底背离 与 年线趋势 共用仓位)\n{'=' * 92}")
        for slots, row in per_slot.items():
            print(
                f"  仓位档 {slots}: 成交 {row['accepted']}/{row['candidates']} "
                f"占用 {row['average_positions']} | 年化 {row['annualized_return_pct']}% "
                f"回撤 {row['max_drawdown_pct']}% 夏普 {row['sharpe_ratio']} "
                f"总收益 {row['total_return_pct']}% 胜率 {row['win_rate']}"
            )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
