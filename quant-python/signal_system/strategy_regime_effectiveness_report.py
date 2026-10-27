#!/usr/bin/env python
"""Regime-conditional effectiveness report for the three live strategies.

The unconditional report answers "what if one strategy runs all year".  This
script answers the intended question: **only take a strategy when its market
regime is active**, using the same causal MA10/MA20 regime as the production
gate (``bull`` / ``range`` / ``bear``).

Default mapping (change ``REGIME_STRATEGY`` if the desk uses a different one):

* bull  -> yearline_pullback  (年线趋势)
* range -> macd_zero_axis     (日线零轴金叉)
* bear  -> macd_divergence    (零轴＋底背离)

Entries are restricted by **signal day** regime.  The market gate is not applied
on top, so this isolates "regime selection" from "gate on/off".  Each strategy
runs with its own 250k portfolio; there is no combined router portfolio yet.

Read-only: no market data fetch, no production state writes.
"""
from __future__ import annotations

import argparse
import gc
import json
import pickle
import sys
import traceback
from collections import Counter
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import strategy_effectiveness_report as base  # noqa: E402
from backtest_winrate import build_market_gate  # noqa: E402

REGIME_STRATEGY = {
    "bull": "yearline_pullback",
    "range": "macd_zero_axis",
    "bear": "macd_divergence",
}
REGIME_NAMES = {"bull": "牛市/上行", "range": "震荡", "bear": "熊市/下行"}


def regime_map(config: dict) -> dict[str, str]:
    index_frame = pickle.loads((BASE / "cache/index_000001_sh.pkl").read_bytes())
    gate = build_market_gate(index_frame, config)
    return {str(day): str(case.get("regime") or "unknown") for day, case in gate.items()}


def filter_signals_by_regime(
    histories: dict[str, pd.DataFrame],
    signals: dict[str, dict[str, set[int]]],
    regimes: dict[str, str],
    target: str,
) -> dict[str, dict[str, set[int]]]:
    """Keep only signal bars whose index day is classified as ``target``."""
    out: dict[str, dict[str, set[int]]] = {}
    for symbol, per_strategy in signals.items():
        dates = pd.to_datetime(histories[symbol]["datetime"]).dt.strftime("%Y-%m-%d").tolist()
        out[symbol] = {
            strategy: {
                int(index)
                for index in indices
                if 0 <= int(index) < len(dates) and regimes.get(dates[int(index)]) == target
            }
            for strategy, indices in per_strategy.items()
        }
    return out


def row_from_result(
    window: str,
    strategy: str,
    target: str,
    coverage: dict,
    regime_days: int,
    total_days: int,
    result: dict,
    bootstrap: dict,
) -> dict:
    metrics = result["metrics"]
    return {
        "window": window,
        "regime": target,
        "regime_name": REGIME_NAMES.get(target, target),
        "strategy": strategy,
        "strategy_name": base.STRATEGY_NAMES.get(strategy, strategy),
        "plan": "prod_stop8_tp30_no_exit_plan",
        "included": coverage["included"],
        "total": coverage["total"],
        "included_pct": coverage["included_pct"],
        "regime_days": regime_days,
        "total_days": total_days,
        "regime_share_pct": round(regime_days / total_days * 100, 2) if total_days else None,
        "total_return_pct": base._num(metrics.get("total_return_pct"), 2),
        "annualized_return_pct": base._num(metrics.get("annualized_return_pct"), 2),
        "max_drawdown_pct": base._num(metrics.get("max_drawdown_pct"), 2),
        "sharpe_ratio": base._num(metrics.get("sharpe_ratio"), 3),
        "payoff_ratio": base._num(metrics.get("payoff_ratio"), 3),
        "win_rate_pct": base._num(metrics.get("win_rate_pct"), 2),
        "closed_trades": int(metrics.get("closed_trades") or 0),
        "open_positions": int(result.get("open_positions") or 0),
        "sharpe_ci_low": bootstrap["sharpe"]["low"],
        "sharpe_ci_high": bootstrap["sharpe"]["high"],
        "annualized_ci_low": bootstrap["annualized"]["low"],
        "annualized_ci_high": bootstrap["annualized"]["high"],
        "maxdd_ci_low": bootstrap["maxdd"]["low"],
        "maxdd_ci_high": bootstrap["maxdd"]["high"],
        "warnings": "; ".join(result.get("warnings") or []),
    }


def evaluate_window(
    window: dict,
    config: dict,
    regimes: dict[str, str],
) -> tuple[list[dict], dict, dict]:
    root = BASE / window["root"]
    histories, signals, calendar, _metadata, coverage = base.load_sample(root)
    days = [day for day in calendar if window["start"] <= day <= window["end"]]
    counts = Counter(regimes.get(day, "unknown") for day in days)
    allowed = {day: True for day in days}

    rows: list[dict] = []
    for target, strategy in REGIME_STRATEGY.items():
        filtered = filter_signals_by_regime(histories, signals, regimes, target)
        result = base.run_case(
            histories,
            filtered,
            calendar,
            config,
            start=window["start"],
            end=window["end"],
            gate_on=False,
            allowed=allowed,
            strategy=strategy,
        )
        seed = base.BOOTSTRAP_SEED + base._sha(window["name"], strategy, target)
        bootstrap = base.block_bootstrap(
            result.get("equity_curve") or [],
            base.INITIAL_CASH,
            reps=base.BOOTSTRAP_REPS,
            block=base.BOOTSTRAP_BLOCK,
            seed=seed,
        )
        rows.append(
            row_from_result(
                window["name"],
                strategy,
                target,
                coverage,
                counts.get(target, 0),
                len(days),
                result,
                bootstrap,
            )
        )
    return rows, coverage, dict(counts)


def _fmt(value, digits: int = 2, suffix: str = "") -> str:
    return base._fmt(value, digits, suffix)


def markdown_report(
    rows: list[dict],
    windows: list[str],
    counts_by_window: dict[str, dict],
    generated_at: str,
) -> str:
    lines: list[str] = []
    lines.append("# 三策略：分市场环境的有效性（只读）")
    lines.append("")
    lines.append(f"生成时间：{generated_at}")
    lines.append("")
    lines.append("## 口径")
    lines.append("")
    lines.append("- Regime：`calculate_strict_regime`（MA10/MA20，因果口径），`bull` / `range` / `bear`。")
    lines.append("- 策略映射：bull → 年线趋势；range → 日线零轴金叉；bear → 零轴＋底背离。")
    lines.append("- 只保留**信号日**落在目标 regime 的入场；不叠加市场闸门，避免两套筛选混在一起。")
    lines.append("- 每个策略独立 250k 组合，不是三策略合并路由组合。")
    lines.append("- 生产风险默认：止损 8%、止盈 30%、不启用分批止盈。")
    lines.append("- 信号来自冻结样本的 `*.signals.json`；样本冻结早于 2026-10-22 的 `volume_gate_enabled` 变更。")
    lines.append("- CI：按交易日 circular block bootstrap（block=5，2,000 次）的 95% 区间。")
    lines.append("- 样本为各窗口实际落盘的当前上市股票；2018–2022 覆盖约 22%–27%，无退市股。")
    lines.append("- 总收益含期末未平仓按最后收盘估值；`未平仓>0` 或 `笔数=0` 时不是纯已实现收益。")
    lines.append("")

    lines.append("## Regime 天数")
    lines.append("")
    lines.append("| 窗口 | bull | range | bear | 合计 |")
    lines.append("|---|---:|---:|---:|---:|")
    for window in windows:
        counts = counts_by_window.get(window, {})
        total = sum(counts.values())
        lines.append(
            f"| {window} | {counts.get('bull', 0)} | {counts.get('range', 0)} | "
            f"{counts.get('bear', 0)} | {total} |"
        )
    lines.append("")

    lines.append("## 目标环境内的表现")
    lines.append("")
    lines.append(
        "| 窗口 | 环境 | 策略 | 环境天数 | 收益% | 年化% | Sharpe | 95% CI | 回撤% | 笔数 | 未平仓 | 胜率% |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|")
    for row in rows:
        ci = (
            "—"
            if row["sharpe_ci_low"] is None or row["sharpe_ci_high"] is None
            else f"[{row['sharpe_ci_low']:.2f}, {row['sharpe_ci_high']:.2f}]"
        )
        lines.append(
            f"| {row['window']} | {row['regime_name']} | {row['strategy_name']} | "
            f"{row['regime_days']}/{row['total_days']} | {_fmt(row['total_return_pct'])} | "
            f"{_fmt(row['annualized_return_pct'])} | {_fmt(row['sharpe_ratio'], 2)} | {ci} | "
            f"{_fmt(row['max_drawdown_pct'])} | {row['closed_trades']} | {row['open_positions']} | "
            f"{_fmt(row['win_rate_pct'], 1)} |"
        )
    lines.append("")

    lines.append("## 自动读数（只描述表，不做显著性结论）")
    lines.append("")
    by_strategy: dict[str, list[dict]] = {}
    for row in rows:
        by_strategy.setdefault(row["strategy"], []).append(row)
    for strategy, items in by_strategy.items():
        positive = [
            item
            for item in items
            if item["sharpe_ci_low"] is not None and item["sharpe_ci_low"] > 0
        ]
        negative = [
            item
            for item in items
            if item["sharpe_ci_high"] is not None and item["sharpe_ci_high"] < 0
        ]
        avg_return = sum(item["total_return_pct"] for item in items if item["total_return_pct"] is not None)
        lines.append(
            f"- {base.STRATEGY_NAMES.get(strategy, strategy)}：{len(items)} 个窗口，"
            f"{len(positive)} 个正 Sharpe CI，{len(negative)} 个负 Sharpe CI，"
            f"平均收益 {avg_return / max(len(items), 1):.2f}%。"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/backtest-results/2026-10-26-regime-effectiveness")
    parser.add_argument("--windows", default="", help="comma-separated subset of window names")
    parser.add_argument("--reps", type=int, default=base.BOOTSTRAP_REPS)
    parser.add_argument("--block", type=int, default=base.BOOTSTRAP_BLOCK)
    args = parser.parse_args()

    base.BOOTSTRAP_REPS = max(args.reps, 100)
    base.BOOTSTRAP_BLOCK = max(args.block, 1)

    out_dir = BASE.parent.parent / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    config = base.production_config()
    regimes = regime_map(config)
    selected = {name.strip() for name in args.windows.split(",") if name.strip()}

    rows: list[dict] = []
    counts_by_window: dict[str, dict] = {}
    available: list[str] = []
    for window in base.WINDOWS:
        if selected and window["name"] not in selected:
            continue
        root = BASE / window["root"]
        if not root.is_dir():
            print(f"skip {window['name']}: missing {root}", flush=True)
            continue
        try:
            window_rows, coverage, counts = evaluate_window(window, config, regimes)
        except Exception:
            print(f"skip {window['name']}: evaluation failed", flush=True)
            traceback.print_exc()
            continue
        rows.extend(window_rows)
        counts_by_window[window["name"]] = counts
        available.append(window["name"])
        print(f"{window['name']}: {coverage['included']}/{coverage['total']} symbols, regimes={counts}", flush=True)
        gc.collect()
        partial = {
            "partial": True,
            "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S%z"),
            "counts": counts_by_window,
            "rows": rows,
        }
        (out_dir / "regime_effectiveness.partial.json").write_text(
            json.dumps(partial, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    if not rows:
        raise SystemExit("no windows selected or no sample directories found")

    generated_at = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S%z")
    payload = {
        "generated_at": generated_at,
        "method": {
            "regime": "calculate_strict_regime MA10/MA20; signal-day filter",
            "mapping": REGIME_STRATEGY,
            "risk": "stop_loss_pct=0.08, stop_profit_pct=0.30, no exit_plan",
            "bootstrap": {"reps": base.BOOTSTRAP_REPS, "block": base.BOOTSTRAP_BLOCK},
            "signals": "frozen sample *.signals.json",
            "universe": "sample-persisted current-listed set; no delisted names",
        },
        "counts": counts_by_window,
        "rows": rows,
    }
    (out_dir / "regime_effectiveness.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    fieldnames = list(rows[0].keys())
    with (out_dir / "regime_effectiveness.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        import csv

        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    report = markdown_report(rows, available, counts_by_window, generated_at)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "regime_effectiveness.partial.json").unlink(missing_ok=True)
    print(f"\nwrote {out_dir / 'regime_effectiveness.json'}")
    print(f"wrote {out_dir / 'regime_effectiveness.csv'}")
    print(f"wrote {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
