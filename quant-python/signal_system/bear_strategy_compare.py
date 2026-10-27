#!/usr/bin/env python
"""Compare all three strategies inside the bear regime.

For every frozen window, keep only signal days classified as ``bear`` by the
same causal MA10/MA20 regime used by the production gate, then replay each
strategy with production risk defaults.  A cash baseline (0%) is included.

Read-only: no market data fetch, no production writes.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import strategy_effectiveness_report as base  # noqa: E402
from backtest_winrate import build_market_gate  # noqa: E402

MIN_BEAR_DAYS = 20
STRATEGIES = base.STRATEGIES


def regime_map(config: dict) -> dict[str, str]:
    index_frame = pickle.loads((BASE / "cache/index_000001_sh.pkl").read_bytes())
    gate = build_market_gate(index_frame, config)
    return {str(day): str(case.get("regime") or "unknown") for day, case in gate.items()}


def bear_signals(
    histories: dict[str, pd.DataFrame],
    signals: dict[str, dict[str, set[int]]],
    regimes: dict[str, str],
) -> dict[str, dict[str, set[int]]]:
    out: dict[str, dict[str, set[int]]] = {}
    for symbol, per_strategy in signals.items():
        dates = pd.to_datetime(histories[symbol]["datetime"]).dt.strftime("%Y-%m-%d").tolist()
        out[symbol] = {
            strategy: {
                int(index)
                for index in indices
                if 0 <= int(index) < len(dates) and regimes.get(dates[int(index)]) == "bear"
            }
            for strategy, indices in per_strategy.items()
        }
    return out


def row_from_result(
    window: str,
    strategy: str,
    bear_days: int,
    total_days: int,
    coverage: dict,
    result: dict,
    bootstrap: dict,
) -> dict:
    metrics = result["metrics"]
    return {
        "window": window,
        "strategy": strategy,
        "strategy_name": base.STRATEGY_NAMES.get(strategy, strategy),
        "plan": "prod_stop8_tp30_no_exit_plan",
        "bear_days": bear_days,
        "total_days": total_days,
        "bear_share_pct": round(bear_days / total_days * 100, 2) if total_days else None,
        "included": coverage["included"],
        "total": coverage["total"],
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
) -> tuple[list[dict], dict]:
    root = BASE / window["root"]
    histories, signals, calendar, _metadata, coverage = base.load_sample(root)
    days = [day for day in calendar if window["start"] <= day <= window["end"]]
    counts = Counter(regimes.get(day, "unknown") for day in days)
    bear_days = counts.get("bear", 0)
    filtered = bear_signals(histories, signals, regimes)

    rows: list[dict] = []
    for strategy in STRATEGIES:
        result = base.run_case(
            histories,
            filtered,
            calendar,
            config,
            start=window["start"],
            end=window["end"],
            gate_on=False,
            allowed={day: True for day in days},
            strategy=strategy,
        )
        seed = base.BOOTSTRAP_SEED + base._sha(window["name"], strategy, "bear")
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
                bear_days,
                len(days),
                coverage,
                result,
                bootstrap,
            )
        )
    return rows, {"bear": bear_days, "total": len(days), "counts": dict(counts)}


def _fmt(value, digits: int = 2, suffix: str = "") -> str:
    return base._fmt(value, digits, suffix)


def aggregate(rows: list[dict], windows: list[str]) -> list[dict]:
    eligible = [window for window in windows if rows and any(
        row["window"] == window and row["bear_days"] >= MIN_BEAR_DAYS for row in rows
    )]
    out: list[dict] = []
    for strategy in STRATEGIES:
        items = [row for row in rows if row["strategy"] == strategy and row["window"] in eligible]
        if not items:
            continue
        returns = [row["total_return_pct"] for row in items if row["total_return_pct"] is not None]
        sharpes = [row["sharpe_ratio"] for row in items if row["sharpe_ratio"] is not None]
        out.append(
            {
                "strategy": strategy,
                "strategy_name": base.STRATEGY_NAMES.get(strategy, strategy),
                "windows": len(items),
                "mean_return_pct": round(float(np.mean(returns)), 2) if returns else None,
                "median_return_pct": round(float(np.median(returns)), 2) if returns else None,
                "mean_sharpe": round(float(np.mean(sharpes)), 3) if sharpes else None,
                "worst_drawdown_pct": max(
                    (row["max_drawdown_pct"] for row in items if row["max_drawdown_pct"] is not None),
                    default=None,
                ),
                "positive_windows": sum(1 for value in returns if value > 0),
                "positive_ci": sum(
                    1 for row in items
                    if row["sharpe_ci_low"] is not None and row["sharpe_ci_low"] > 0
                ),
                "negative_ci": sum(
                    1 for row in items
                    if row["sharpe_ci_high"] is not None and row["sharpe_ci_high"] < 0
                ),
                "total_trades": sum(row["closed_trades"] for row in items),
                "total_open": sum(row["open_positions"] for row in items),
            }
        )
    out.append(
        {
            "strategy": "cash",
            "strategy_name": "空仓",
            "windows": len(eligible),
            "mean_return_pct": 0.0,
            "median_return_pct": 0.0,
            "mean_sharpe": None,
            "worst_drawdown_pct": 0.0,
            "positive_windows": 0,
            "positive_ci": 0,
            "negative_ci": 0,
            "total_trades": 0,
            "total_open": 0,
        }
    )
    return out


def markdown_report(
    rows: list[dict],
    windows: list[str],
    counts: dict[str, dict],
    agg: list[dict],
    generated_at: str,
) -> str:
    lines: list[str] = []
    lines.append("# Bear 环境：三策略对比（只读）")
    lines.append("")
    lines.append(f"生成时间：{generated_at}")
    lines.append("")
    lines.append("## 口径")
    lines.append("")
    lines.append("- Bear：`calculate_strict_regime`（MA10/MA20，因果口径）判定为 `bear` 的交易日。")
    lines.append("- 只保留**信号日**为 bear 的入场；不叠加市场闸门。")
    lines.append("- 三个策略各自独立 250k 组合；现金基线 = 0%。")
    lines.append("- 生产风险默认：止损 8%、止盈 30%、不启用分批止盈。")
    lines.append("- 信号来自冻结样本；样本冻结早于 2026-10-22 的 `volume_gate_enabled` 变更。")
    lines.append("- 2018–2022 覆盖约 22%–27%，2024 约 87%–95%；无退市股。")
    lines.append("- 总收益含期末未平仓估值；`未平仓>0` 或 `笔数=0` 时不是纯已实现收益。")
    lines.append("")
    lines.append("## Bear 天数")
    lines.append("")
    lines.append("| 窗口 | bear | 合计 |")
    lines.append("|---|---:|---:|")
    for window in windows:
        info = counts[window]
        lines.append(f"| {window} | {info['bear']} | {info['total']} |")
    lines.append("")
    lines.append("## 每个窗口（只跑 bear 信号日）")
    lines.append("")
    lines.append(
        "| 窗口 | 策略 | bear 天数 | 收益% | 年化% | Sharpe | 95% CI | 回撤% | 笔数 | 未平仓 | 胜率% |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|")
    for row in rows:
        ci = (
            "—"
            if row["sharpe_ci_low"] is None or row["sharpe_ci_high"] is None
            else f"[{row['sharpe_ci_low']:.2f}, {row['sharpe_ci_high']:.2f}]"
        )
        lines.append(
            f"| {row['window']} | {row['strategy_name']} | {row['bear_days']}/{row['total_days']} | "
            f"{_fmt(row['total_return_pct'])} | {_fmt(row['annualized_return_pct'])} | "
            f"{_fmt(row['sharpe_ratio'], 2)} | {ci} | {_fmt(row['max_drawdown_pct'])} | "
            f"{row['closed_trades']} | {row['open_positions']} | {_fmt(row['win_rate_pct'], 1)} |"
        )
    lines.append("")
    lines.append(f"## 汇总（bear 天数 ≥ {MIN_BEAR_DAYS} 的窗口）")
    lines.append("")
    lines.append("| 策略 | 窗口数 | 平均收益% | 中位收益% | 平均 Sharpe | 最差回撤% | 正收益窗口 | 正 CI | 负 CI | 总笔数 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for item in agg:
        lines.append(
            f"| {item['strategy_name']} | {item['windows']} | {_fmt(item['mean_return_pct'])} | "
            f"{_fmt(item['median_return_pct'])} | {_fmt(item['mean_sharpe'], 2)} | "
            f"{_fmt(item['worst_drawdown_pct'])} | {item['positive_windows']}/{item['windows']} | "
            f"{item['positive_ci']} | {item['negative_ci']} | {item['total_trades']} |"
        )
    lines.append("")
    lines.append("## 自动读数")
    lines.append("")
    ranked = [item for item in agg if item["strategy"] != "cash" and item["mean_return_pct"] is not None]
    if ranked:
        best = max(ranked, key=lambda item: (item["mean_sharpe"] or -999, item["mean_return_pct"] or -999))
        lines.append(
            f"- 平均 Sharpe 最高：{best['strategy_name']}（{_fmt(best['mean_sharpe'], 2)}，"
            f"平均收益 {_fmt(best['mean_return_pct'])}%）。"
        )
        cash = next(item for item in agg if item["strategy"] == "cash")
        lines.append(
            f"- 现金基线 0%；{sum(1 for item in ranked if (item['mean_return_pct'] or 0) > 0)}/3 个策略平均收益为正。"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/backtest-results/2026-10-26-bear-strategies")
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
    counts: dict[str, dict] = {}
    available: list[str] = []
    for window in base.WINDOWS:
        if selected and window["name"] not in selected:
            continue
        if not (BASE / window["root"]).is_dir():
            continue
        window_rows, info = evaluate_window(window, config, regimes)
        rows.extend(window_rows)
        counts[window["name"]] = info
        available.append(window["name"])
        print(f"{window['name']}: bear {info['bear']}/{info['total']} days", flush=True)
        partial = {
            "partial": True,
            "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S%z"),
            "counts": counts,
            "rows": rows,
        }
        (out_dir / "bear_strategies.partial.json").write_text(
            json.dumps(partial, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    if not rows:
        raise SystemExit("no windows selected")

    agg = aggregate(rows, available)
    generated_at = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S%z")
    payload = {
        "generated_at": generated_at,
        "method": {
            "regime": "calculate_strict_regime MA10/MA20; bear signal-day filter",
            "risk": "stop_loss_pct=0.08, stop_profit_pct=0.30, no exit_plan",
            "bootstrap": {"reps": base.BOOTSTRAP_REPS, "block": base.BOOTSTRAP_BLOCK},
            "cash_baseline": 0.0,
        },
        "counts": counts,
        "rows": rows,
        "aggregate": agg,
    }
    (out_dir / "bear_strategies.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    fieldnames = list(rows[0].keys())
    with (out_dir / "bear_strategies.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        import csv

        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    report = markdown_report(rows, available, counts, agg, generated_at)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "bear_strategies.partial.json").unlink(missing_ok=True)
    print(f"\nwrote {out_dir / 'bear_strategies.json'}")
    print(f"wrote {out_dir / 'bear_strategies.csv'}")
    print(f"wrote {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
