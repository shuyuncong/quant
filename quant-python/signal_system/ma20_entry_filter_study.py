#!/usr/bin/env python
"""MA20 entry-filter counterfactual on the frozen multi-window samples.

For every (window, strategy) this replays two entry sets through the same
execution engine:

* ``baseline``: all frozen ``*.signals.json`` signals.
* ``ma20``: only signals whose signal-day close is *strictly above* the
  stock's 20-day simple moving average.  The signal is confirmed at the close;
  the entry remains the next day's open, so the filter is causal.

Risk defaults match the effectiveness report: 8% stop, 30% take-profit,
40-bar timeout, 250k initial cash, 4 positions, 25% per position.  The
production market gate is optional because it is an index-level rule; the
default (gate off) isolates the stock-level MA20 filter.

The samples are frozen research inputs, not a fresh scan.  Results are
window-specific and carry the usual current-listed-universe survivorship bias;
this script makes no significance claim and never writes production config.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import strategy_effectiveness_report as eff  # noqa: E402
from research_backtest import replay  # noqa: E402

WINDOW_NAMES = tuple(window["name"] for window in eff.WINDOWS)
STRATEGY_NAMES = dict(eff.STRATEGY_NAMES)
GATE_OFF = "off"
GATE_ON = "on"


def _num(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _prepare_indicators(histories: dict[str, pd.DataFrame]) -> dict[str, dict[str, Any]]:
    """Causal close / MA20 / date arrays aligned to each history frame."""
    out: dict[str, dict[str, Any]] = {}
    for symbol, frame in histories.items():
        close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
        ma20 = pd.Series(close).rolling(20, min_periods=20).mean().to_numpy(dtype=float)
        dates = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d").tolist()
        out[symbol] = {
            "close": close,
            "ma20": ma20,
            "dates": dates,
            "date_index": {day: index for index, day in enumerate(dates)},
        }
    return out


def _in_window(indicators: dict[str, Any], index: int, start: str, end: str) -> bool:
    """A signal is actionable only if its confirmation and next-bar entry fall in the window."""
    dates = indicators["dates"]
    if index < 0 or index + 1 >= len(dates):
        return False
    return start <= dates[index] <= end and dates[index + 1] <= end


def _filter_signals(
    histories: dict[str, pd.DataFrame],
    indicators: dict[str, dict[str, Any]],
    signals: dict[str, dict[str, set[int]]],
    start: str,
    end: str,
) -> tuple[dict[str, dict[str, set[int]]], dict[str, dict[str, int]]]:
    """Return (MA20-filtered signals, window-scoped signal counts per strategy)."""
    filtered: dict[str, dict[str, set[int]]] = {}
    stats = {
        strategy: {"candidates": 0, "above": 0, "below": 0, "missing_ma20": 0}
        for strategy in eff.STRATEGIES
    }
    for symbol, per_strategy in signals.items():
        data = indicators[symbol]
        filtered[symbol] = {}
        for strategy in eff.STRATEGIES:
            kept: set[int] = set()
            for raw_index in per_strategy.get(strategy, ()):
                index = int(raw_index)
                if not _in_window(data, index, start, end):
                    continue
                stats[strategy]["candidates"] += 1
                ma20 = data["ma20"][index]
                if not np.isfinite(ma20) or float(ma20) <= 0:
                    stats[strategy]["missing_ma20"] += 1
                    continue
                if float(data["close"][index]) > float(ma20):
                    stats[strategy]["above"] += 1
                    kept.add(index)
                else:
                    stats[strategy]["below"] += 1
            filtered[symbol][strategy] = kept
    return filtered, stats


def _arm_metrics(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") or {}
    trades = result.get("trades") or []
    trade_returns: list[float] = []
    for trade in trades:
        basis = float(trade.get("entry_price") or 0.0) * int(trade.get("quantity") or 0)
        if basis > 0:
            trade_returns.append(float(trade.get("pnl_cash") or 0.0) / basis * 100.0)
    return {
        "closed_trades": int(metrics.get("closed_trades") or 0),
        "total_return_pct": _num(metrics.get("total_return_pct")),
        "annualized_return_pct": _num(metrics.get("annualized_return_pct")),
        "max_drawdown_pct": _num(metrics.get("max_drawdown_pct")),
        "sharpe_ratio": _num(metrics.get("sharpe_ratio"), 3),
        "win_rate_pct": _num(metrics.get("win_rate_pct")),
        "payoff_ratio": _num(metrics.get("payoff_ratio"), 3),
        "avg_trade_pnl_pct": _num(float(np.mean(trade_returns))) if trade_returns else None,
    }


def _trade_split(
    result: dict[str, Any],
    indicators: dict[str, dict[str, Any]],
    strategy: str,
) -> dict[str, Any]:
    """Classify baseline portfolio trades by the MA20 state of their signal day."""
    buckets = {
        "above": {"trades": 0, "pnl_sum": 0.0, "wins": 0},
        "below": {"trades": 0, "pnl_sum": 0.0, "wins": 0},
        "unknown": {"trades": 0, "pnl_sum": 0.0, "wins": 0},
    }
    for trade in result.get("trades") or []:
        symbol = str(trade.get("symbol") or "")
        data = indicators.get(symbol)
        if data is None:
            buckets["unknown"]["trades"] += 1
            continue
        entry_day = str(trade.get("entry_day") or "")
        entry_index = data["date_index"].get(entry_day)
        if entry_index is None or entry_index <= 0:
            buckets["unknown"]["trades"] += 1
            continue
        signal_index = entry_index - 1
        if signal_index >= len(data["close"]):
            buckets["unknown"]["trades"] += 1
            continue
        basis = float(trade.get("entry_price") or 0.0) * int(trade.get("quantity") or 0)
        if basis <= 0:
            buckets["unknown"]["trades"] += 1
            continue
        pnl_pct = float(trade.get("pnl_cash") or 0.0) / basis * 100.0
        ma20 = data["ma20"][signal_index]
        if not np.isfinite(ma20) or float(ma20) <= 0:
            bucket = "unknown"
        elif float(data["close"][signal_index]) > float(ma20):
            bucket = "above"
        else:
            bucket = "below"
        buckets[bucket]["trades"] += 1
        buckets[bucket]["pnl_sum"] += pnl_pct
        if pnl_pct > 0:
            buckets[bucket]["wins"] += 1
    out: dict[str, Any] = {}
    for bucket, values in buckets.items():
        count = values["trades"]
        out[bucket] = {
            "trades": count,
            "avg_trade_pnl_pct": _num(values["pnl_sum"] / count) if count else None,
            "win_rate_pct": _num(100.0 * values["wins"] / count) if count else None,
        }
    out["strategy"] = strategy
    return out


def _run_window(
    job: tuple[dict[str, str], tuple[str, ...], tuple[str, ...]],
) -> dict[str, Any]:
    window, strategies, gate_states = job
    config = eff.production_config()
    histories, signals, calendar, _metadata, coverage = eff.load_sample(BASE / window["root"])
    indicators = _prepare_indicators(histories)
    filtered, signal_stats = _filter_signals(
        histories, indicators, signals, window["start"], window["end"]
    )
    allowed = eff.gate_map(config) if GATE_ON in gate_states else {}
    options = {
        "start": window["start"],
        "end": window["end"],
        "max_positions": eff.MAX_POSITIONS,
        "initial_cash": eff.INITIAL_CASH,
        "history_bars": 300,
    }
    rows: list[dict[str, Any]] = []
    trade_splits: dict[str, Any] = {}
    for gate in gate_states:
        for strategy in strategies:
            arm_signals = {
                "baseline": signals,
                "ma20": filtered,
            }
            for arm, source in arm_signals.items():
                use_signals = (
                    eff.restrict_signals(histories, source, allowed)
                    if gate == GATE_ON
                    else source
                )
                result = replay(histories, use_signals, calendar, config, options, strategy)
                row = {
                    "window": window["name"],
                    "strategy": strategy,
                    "strategy_name": STRATEGY_NAMES.get(strategy, strategy),
                    "gate": gate,
                    "arm": arm,
                    "candidates": (
                        signal_stats[strategy]["candidates"]
                        if arm == "baseline"
                        else signal_stats[strategy]["above"]
                    ),
                    **_arm_metrics(result),
                }
                rows.append(row)
                if arm == "baseline":
                    trade_splits[f"{gate}:{strategy}"] = _trade_split(
                        result, indicators, strategy
                    )
    return {
        "window": window["name"],
        "root": window["root"],
        "start": window["start"],
        "end": window["end"],
        "coverage": coverage,
        "signal_stats": signal_stats,
        "rows": rows,
        "trade_splits": trade_splits,
    }


def _comparison_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for result in results:
        for strategy in sorted({row["strategy"] for row in result["rows"]}):
            for gate in (GATE_OFF, GATE_ON):
                rows = [
                    row
                    for row in result["rows"]
                    if row["window"] == result["window"]
                    and row["strategy"] == strategy
                    and row["gate"] == gate
                ]
                if len(rows) != 2:
                    continue
                baseline_row = next(row for row in rows if row["arm"] == "baseline")
                ma20_row = next(row for row in rows if row["arm"] == "ma20")
                comparison = {
                    "window": result["window"],
                    "strategy": strategy,
                    "strategy_name": STRATEGY_NAMES.get(strategy, strategy),
                    "gate": gate,
                    "baseline": baseline_row,
                    "ma20": ma20_row,
                    "delta_return_pct": _num(
                        (ma20_row["total_return_pct"] or 0.0)
                        - (baseline_row["total_return_pct"] or 0.0)
                    ),
                    "delta_max_drawdown_pct": _num(
                        (ma20_row["max_drawdown_pct"] or 0.0)
                        - (baseline_row["max_drawdown_pct"] or 0.0)
                    ),
                    "delta_sharpe": _num(
                        (ma20_row["sharpe_ratio"] or 0.0)
                        - (baseline_row["sharpe_ratio"] or 0.0),
                        3,
                    ),
                    "delta_trades": ma20_row["closed_trades"] - baseline_row["closed_trades"],
                }
                comparisons.append(comparison)
    return comparisons


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}{suffix}"


def _pair(baseline: Any, ma20: Any, digits: int = 2, suffix: str = "") -> str:
    if baseline is None or ma20 is None:
        return f"{_fmt(baseline, digits, suffix)} → {_fmt(ma20, digits, suffix)}"
    delta = float(ma20) - float(baseline)
    return (
        f"{_fmt(baseline, digits, suffix)} → {_fmt(ma20, digits, suffix)} "
        f"({delta:+.{digits}f}{suffix})"
    )


def _summary(comparisons: list[dict[str, Any]], gate: str) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for strategy in eff.STRATEGIES:
        cases = [
            row for row in comparisons
            if row["strategy"] == strategy and row["gate"] == gate
        ]
        if not cases:
            continue
        deltas = [float(row["delta_return_pct"] or 0.0) for row in cases]
        drawdowns = [float(row["delta_max_drawdown_pct"] or 0.0) for row in cases]
        shapres = [float(row["delta_sharpe"] or 0.0) for row in cases]
        summary.append(
            {
                "gate": gate,
                "strategy": strategy,
                "strategy_name": STRATEGY_NAMES.get(strategy, strategy),
                "windows": len(cases),
                "improved": sum(1 for value in deltas if value > 0.01),
                "degraded": sum(1 for value in deltas if value < -0.01),
                "unchanged": sum(1 for value in deltas if abs(value) <= 0.01),
                "median_delta_return_pct": _num(float(np.median(deltas))),
                "mean_delta_return_pct": _num(float(np.mean(deltas))),
                "median_delta_max_drawdown_pct": _num(float(np.median(drawdowns))),
                "median_delta_sharpe": _num(float(np.median(shapres)), 3),
            }
        )
    return summary


def _markdown(
    results: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
    gate_states: tuple[str, ...],
) -> str:
    lines: list[str] = []
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append("# MA20 入场过滤对照回测")
    lines.append("")
    lines.append(f"生成时间：{generated}")
    lines.append("")
    lines.append("## 规则与口径")
    lines.append("")
    lines.append("- 规则：只在信号日收盘价 **严格高于** 该股 20 日简单移动平均时保留买入信号；信号日收盘确认，次日开盘成交，不使用未来数据。")
    lines.append("- 两臂：`baseline` 为冻结样本全部信号；`ma20` 为同一批信号中满足上述条件的子集。")
    lines.append("- 执行：同一 `research_backtest.replay`；8% 止损、30% 止盈、40 根超时、25 万初始资金、最多 4 仓、单仓 25%。")
    lines.append("- 信号：各窗口冻结的 `*.signals.json`，不是按今天的配置重算；零轴信号早于 2026-10-22 的成交量门槛变更。")
    lines.append("- 股票池：各样本实际落盘的当前上市股票，不含完整退市样本，存在幸存者偏差。")
    lines.append("- 候选数为窗口内、MA20 过滤前的信号数；gate on 时未计市场闸门剔除量。")
    lines.append("- 结果只代表对应窗口，不构成统计显著性结论。")
    lines.append("")
    lines.append("## 信号覆盖")
    lines.append("")
    lines.append("| 窗口 | 策略 | 窗口内候选 | 20日线上方 | 下方 | 保留比例 |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for result in results:
        for strategy in eff.STRATEGIES:
            stats = result["signal_stats"].get(strategy)
            if not stats:
                continue
            candidates = stats["candidates"]
            retained = stats["above"] / candidates * 100.0 if candidates else None
            lines.append(
                f"| {result['window']} | {STRATEGY_NAMES.get(strategy, strategy)} | "
                f"{candidates} | {stats['above']} | {stats['below']} + {stats['missing_ma20']} 缺 | "
                f"{_fmt(retained, 1, '%')} |"
            )
    lines.append("")
    for gate in gate_states:
        label = "生产市场闸门（gate on）" if gate == GATE_ON else "无市场闸门（gate off）"
        lines.append(f"## 组合回放：{label}")
        lines.append("")
        lines.append("| 窗口 | 策略 | 候选 基线→MA20 | 成交 基线→MA20 | 收益% 基线→MA20 | Δ收益pp | 回撤% 基线→MA20 | Sharpe 基线→MA20 | 胜率% 基线→MA20 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for row in comparisons:
            if row["gate"] != gate:
                continue
            baseline = row["baseline"]
            ma20 = row["ma20"]
            lines.append(
                f"| {row['window']} | {row['strategy_name']} | "
                f"{baseline['candidates']} → {ma20['candidates']} | "
                f"{baseline['closed_trades']} → {ma20['closed_trades']} | "
                f"{_pair(baseline['total_return_pct'], ma20['total_return_pct'])} | "
                f"{_fmt(row['delta_return_pct'], 2, '')} | "
                f"{_pair(baseline['max_drawdown_pct'], ma20['max_drawdown_pct'])} | "
                f"{_pair(baseline['sharpe_ratio'], ma20['sharpe_ratio'], 3)} | "
                f"{_pair(baseline['win_rate_pct'], ma20['win_rate_pct'], 1)} |"
            )
        lines.append("")
        lines.append("## 汇总")
        lines.append("")
        lines.append("| 策略 | 窗口数 | 改善 | 变差 | 持平 | 收益Δ中位数pp | 收益Δ均值pp | 回撤Δ中位数pp | SharpeΔ中位数 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in _summary(comparisons, gate):
            lines.append(
                f"| {row['strategy_name']} | {row['windows']} | {row['improved']} | "
                f"{row['degraded']} | {row['unchanged']} | "
                f"{_fmt(row['median_delta_return_pct'])} | "
                f"{_fmt(row['mean_delta_return_pct'])} | "
                f"{_fmt(row['median_delta_max_drawdown_pct'])} | "
                f"{_fmt(row['median_delta_sharpe'], 3)} |"
            )
        lines.append("")
        lines.append("## 基线成交按信号日 MA20 分组（诊断）")
        lines.append("")
        lines.append("| 窗口 | 策略 | MA20上方笔数 | 上方均收益% | 上方胜率% | MA20下方笔数 | 下方均收益% | 下方胜率% |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
        for result in results:
            for strategy in eff.STRATEGIES:
                split = result.get("trade_splits", {}).get(f"{gate}:{strategy}")
                if not split:
                    continue
                above, below = split["above"], split["below"]
                lines.append(
                    f"| {result['window']} | {STRATEGY_NAMES.get(strategy, strategy)} | "
                    f"{above['trades']} | {_fmt(above['avg_trade_pnl_pct'])} | "
                    f"{_fmt(above['win_rate_pct'], 1)} | {below['trades']} | "
                    f"{_fmt(below['avg_trade_pnl_pct'])} | "
                    f"{_fmt(below['win_rate_pct'], 1)} |"
                )
        lines.append("")
    lines.append("## 读数边界")
    lines.append("")
    lines.append("- 零轴金叉和零轴＋底背离在冻结信号里几乎全部已站在 20 日线上方；MA20 过滤对它们是近似无操作，不能据此宣称“过滤提高了胜率”。")
    lines.append("- 年线趋势的回踩信号按定义经常发生在 20 日线下方；MA20 过滤会砍掉大部分候选，改变的是策略性质，不只是加一层风控。")
    lines.append("- 各窗口方向不一致时，应看分布和样本量，而不是挑一个最好窗口。窗口收益为半年/单季，年化列仅作参考。")
    lines.append("- 要进入生产，需要在未触碰的 holdout 上按预注册规则复核；本报告不修改 `config.yaml`。")
    lines.append("")
    return "\n".join(lines)


def _parse_csv(value: str, allowed: tuple[str, ...], label: str) -> tuple[str, ...]:
    tokens = tuple(item.strip() for item in value.split(",") if item.strip())
    unknown = [item for item in tokens if item not in allowed]
    if unknown:
        raise SystemExit(f"unknown {label}: {', '.join(unknown)}; choose from {', '.join(allowed)}")
    return tokens or allowed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--windows",
        default=",".join(WINDOW_NAMES),
        help="comma list of window names; default all",
    )
    parser.add_argument(
        "--strategies",
        default=",".join(eff.STRATEGIES),
        help="comma list of strategy ids; default all",
    )
    parser.add_argument(
        "--gate-on",
        action="store_true",
        help="also apply the production market gate (default: gate off isolates the MA20 rule)",
    )
    parser.add_argument("--workers", type=int, default=4, help="parallel windows; 1 = sequential")
    parser.add_argument(
        "--out",
        default=None,
        help="output directory; default output/backtests/ma20-entry-filter-<timestamp>",
    )
    args = parser.parse_args()

    window_names = _parse_csv(args.windows, WINDOW_NAMES, "window")
    strategies = _parse_csv(args.strategies, tuple(eff.STRATEGIES), "strategy")
    windows = [window for window in eff.WINDOWS if window["name"] in window_names]
    gate_states = (GATE_OFF, GATE_ON) if args.gate_on else (GATE_OFF,)
    jobs = [(window, strategies, gate_states) for window in windows]

    started = datetime.now()
    if args.workers > 1 and len(jobs) > 1:
        results: list[dict[str, Any]] = []
        with ProcessPoolExecutor(max_workers=min(args.workers, len(jobs))) as pool:
            futures = {pool.submit(_run_window, job): job[0]["name"] for job in jobs}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(f"finished {result['window']}", flush=True)
    else:
        results = []
        for job in jobs:
            result = _run_window(job)
            results.append(result)
            print(f"finished {result['window']}", flush=True)

    order = {name: index for index, name in enumerate(WINDOW_NAMES)}
    results.sort(key=lambda item: order[item["window"]])
    comparisons = _comparison_rows(results)
    output_dir = (
        Path(args.out)
        if args.out
        else BASE
        / "output"
        / "backtests"
        / f"ma20-entry-filter-{started.strftime('%Y%m%d-%H%M%S')}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": started.isoformat(timespec="seconds"),
        "rule": "signal-day close > MA20(20), strict; entry next open",
        "risk": "SL8% / TP30% / 40-bar timeout / 250k / max 4 positions / 25% each",
        "gate_states": list(gate_states),
        "windows": results,
        "comparisons": comparisons,
        "summary": [row for gate in gate_states for row in _summary(comparisons, gate)],
    }
    (output_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "report.md").write_text(_markdown(results, comparisons, gate_states), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "windows": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
