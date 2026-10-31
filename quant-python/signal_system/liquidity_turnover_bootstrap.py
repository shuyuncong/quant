#!/usr/bin/env python
"""Paired cluster bootstrap for the frozen turnover 1-5% study.

The archived liquidity JSONs keep only an equity-curve digest, so this script
replays the two comparison arms from the frozen expanded samples and keeps the
full daily curves.  It then runs a circular block bootstrap on the *paired*
daily return difference and on the paired max-drawdown difference.

Research-only: no network, no database, no production config changes.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

BASE = Path(__file__).resolve().parent
for _path in (BASE, BASE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import liquidity_strategy_study as L  # noqa: E402

OUT_ROOT = BASE / "output/backtests/_history_expansion_20261026/expanded"
SAMPLE_DIRS = {
    "2020H1": OUT_ROOT / "sample-2020h1",
    "2021H2": OUT_ROOT / "sample-2021h2",
    "2023H1": OUT_ROOT / "sample-2023h1",
}
REPORTS = {
    "2020H1": OUT_ROOT / "liquidity-2020h1.json",
    "2021H2": OUT_ROOT / "liquidity-2021h2.json",
    "2023H1": OUT_ROOT / "liquidity-2023h1.json",
}
STRATEGIES = ("yearline_pullback", "macd_zero_axis")
ARMS = ("available_baseline", "turnover_1_to_5")
BLOCK = 5
REPS = 2_000
INITIAL = 250_000.0
OUT = BASE / "output/backtests/_history_expansion_20261026/turnover_bootstrap"


def _full_execute_arm(
    histories: dict[str, Any],
    signals: dict[str, Any],
    calendar: list[str],
    config: dict[str, Any],
    options: dict[str, Any],
    strategy: str,
) -> dict[str, Any]:
    """Same as L.execute_arm but keeps the full daily equity curve."""
    result = L.replay(histories, signals, calendar, deepcopy(config), dict(options), strategy)
    curve = result.get("equity_curve") or []
    rejected = Counter(item.get("reason", "unknown") for item in result.get("rejected") or [])
    return {
        "metrics": deepcopy(result["metrics"]),
        "trades": deepcopy(result.get("trades") or []),
        "open_positions": int(result.get("open_positions") or 0),
        "final_positions": int(curve[-1]["positions"]) if curve else 0,
        "equity_curve": curve,
        "warnings": list(result.get("warnings") or []),
        "rejected_orders": {"count": sum(rejected.values()), "by_reason": dict(sorted(rejected.items()))},
    }


def _seed(window: str, strategy: str) -> int:
    raw = hashlib.sha256(f"turnover-bootstrap|{window}|{strategy}|v1".encode()).hexdigest()
    return int(raw[:8], 16)


def _max_drawdown(equity: np.ndarray) -> float:
    peaks = np.maximum.accumulate(equity)
    return float(np.max(1.0 - equity / peaks) * 100.0)


def _sharpe(returns: np.ndarray) -> float | None:
    if len(returns) < 2:
        return None
    std = float(np.std(returns, ddof=1))
    if std <= 1e-12:
        return None
    return float(np.mean(returns) / std * math.sqrt(252.0))


def _paired_returns(
    base_curve: list[dict[str, Any]], filtered_curve: list[dict[str, Any]]
) -> tuple[list[str], np.ndarray, np.ndarray]:
    base = {str(point["date"]): float(point["equity"]) for point in base_curve}
    filtered = {str(point["date"]): float(point["equity"]) for point in filtered_curve}
    dates = sorted(set(base) & set(filtered))
    if len(dates) != len(base) or len(dates) != len(filtered):
        raise ValueError("arms have different calendars; pairing would be invalid")
    ret_base = []
    ret_filtered = []
    prev_base = prev_filtered = INITIAL
    for day in dates:
        ret_base.append(base[day] / prev_base - 1.0)
        ret_filtered.append(filtered[day] / prev_filtered - 1.0)
        prev_base, prev_filtered = base[day], filtered[day]
    return dates, np.asarray(ret_base, dtype=float), np.asarray(ret_filtered, dtype=float)


def _bootstrap(
    ret_base: np.ndarray, ret_filtered: np.ndarray, *, seed: int
) -> dict[str, Any]:
    n = len(ret_base)
    rng = np.random.default_rng(seed)
    blocks = int(math.ceil(n / BLOCK))
    starts = rng.integers(0, n, size=(REPS, blocks))
    offsets = np.arange(BLOCK)
    indices = ((starts[:, :, None] + offsets[None, None, :]) % n).reshape(REPS, -1)[:, :n]

    diff = ret_filtered - ret_base
    sum_diff = diff[indices].sum(axis=1)
    annual_diff = sum_diff * (252.0 / n) * 100.0

    paths_base = np.cumprod(1.0 + ret_base[indices], axis=1)
    paths_filtered = np.cumprod(1.0 + ret_filtered[indices], axis=1)
    dd_base = np.max(1.0 - paths_base / np.maximum.accumulate(paths_base, axis=1), axis=1) * 100.0
    dd_filtered = np.max(1.0 - paths_filtered / np.maximum.accumulate(paths_filtered, axis=1), axis=1) * 100.0
    dd_diff = dd_filtered - dd_base

    def interval(values: np.ndarray) -> list[float | None]:
        valid = values[np.isfinite(values)]
        if not len(valid):
            return [None, None]
        low, high = np.percentile(valid, [2.5, 97.5])
        return [float(low), float(high)]

    return {
        "reps": REPS,
        "block": BLOCK,
        "seed": seed,
        "days": n,
        "delta_total_pct": float(sum_diff.mean() * 100.0),
        "delta_total_ci95": interval(sum_diff * 100.0),
        "p_delta_total_le_zero": float(np.mean(sum_diff <= 0.0)),
        "delta_annualized_pct": float(annual_diff.mean()),
        "delta_annualized_ci95": interval(annual_diff),
        "delta_maxdd_pct": float(dd_diff.mean()),
        "delta_maxdd_ci95": interval(dd_diff),
        "p_delta_maxdd_ge_zero": float(np.mean(dd_diff >= 0.0)),
    }


def _window(window: str) -> dict[str, Any]:
    params = json.loads(REPORTS[window].read_text(encoding="utf-8"))["parameters"]
    config = params["engine_config"]
    options = params["replay_options"]
    meta = json.loads((SAMPLE_DIRS[window] / "prepared_metadata.json").read_text(encoding="utf-8"))

    L.execute_arm = _full_execute_arm
    report = L.build_report(
        SAMPLE_DIRS[window],
        config,
        config_source={"ignored_config": None},
        start=str(meta["start"]),
        end=str(meta["end"]),
        options=options,
        label=window,
    )

    out: dict[str, Any] = {}
    for strategy in STRATEGIES:
        arms = report["strategies"][strategy]["arms"]
        dates, ret_base, ret_filtered = _paired_returns(
            arms["available_baseline"]["equity_curve"],
            arms["turnover_1_to_5"]["equity_curve"],
        )
        equity_base = np.asarray(
            [INITIAL, *[float(p["equity"]) for p in arms["available_baseline"]["equity_curve"]]]
        )
        equity_filtered = np.asarray(
            [INITIAL, *[float(p["equity"]) for p in arms["turnover_1_to_5"]["equity_curve"]]]
        )
        boot = _bootstrap(ret_base, ret_filtered, seed=_seed(window, strategy))
        out[strategy] = {
            "window": window,
            "days": len(dates),
            "first_day": dates[0],
            "last_day": dates[-1],
            "trades_base": len(arms["available_baseline"]["trades"]),
            "trades_filtered": len(arms["turnover_1_to_5"]["trades"]),
            "total_return_base_pct": float((equity_base[-1] / INITIAL - 1.0) * 100.0),
            "total_return_filtered_pct": float((equity_filtered[-1] / INITIAL - 1.0) * 100.0),
            "maxdd_base_pct": _max_drawdown(equity_base),
            "maxdd_filtered_pct": _max_drawdown(equity_filtered),
            "sharpe_base": _sharpe(ret_base),
            "sharpe_filtered": _sharpe(ret_filtered),
            "bootstrap": boot,
        }
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    results = {window: _window(window) for window in SAMPLE_DIRS}
    summary: dict[str, Any] = {}
    for strategy in STRATEGIES:
        rows = [results[window][strategy] for window in SAMPLE_DIRS]
        deltas = [row["total_return_filtered_pct"] - row["total_return_base_pct"] for row in rows]
        summary[strategy] = {
            "windows": len(rows),
            "positive_delta_windows": sum(delta > 0 for delta in deltas),
            "median_delta_pct": float(np.median(deltas)),
            "mean_delta_pct": float(np.mean(deltas)),
            "delta_ci95_per_window": [row["bootstrap"]["delta_total_ci95"] for row in rows],
            "p_le_zero_per_window": [row["bootstrap"]["p_delta_total_le_zero"] for row in rows],
        }
    report = {
        "version": "turnover_paired_bootstrap.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "pairing": "same calendar day for both arms",
            "bootstrap": f"circular block bootstrap, block={BLOCK}, reps={REPS}",
            "seed": "sha256(window|strategy|turnover-bootstrap|v1)",
            "primary": "paired daily return difference, cumulative window total",
            "drawdown": "paired synthetic paths from resampled days; filtered minus baseline",
        },
        "results": results,
        "summary": summary,
        "scope": {
            "network": False,
            "database": False,
            "production_config_changed": False,
            "future_holdout_used": False,
        },
    }
    (OUT / "turnover_bootstrap.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# 换手率 1–5% 配对 bootstrap",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        f"方法：{report['method']['bootstrap']}；同一天配对两臂日收益；主指标为窗口累计收益差（过滤 − 基线）。",
        "",
        "| 窗口 | 策略 | 收益 基线→过滤 | Δpp | Δ 95% CI | p(Δ≤0) | 回撤 基线→过滤 | Δ回撤 CI |",
        "|---|---|---:|---:|---|---:|---|---|",
    ]
    for window, strategies in results.items():
        for strategy, row in strategies.items():
            delta = row["total_return_filtered_pct"] - row["total_return_base_pct"]
            ci = row["bootstrap"]["delta_total_ci95"]
            dd_ci = row["bootstrap"]["delta_maxdd_ci95"]
            lines.append(
                f"| {window} | {strategy} | {row['total_return_base_pct']:.2f} → "
                f"{row['total_return_filtered_pct']:.2f} | {delta:+.2f} | "
                f"[{ci[0]:+.2f}, {ci[1]:+.2f}] | {row['bootstrap']['p_delta_total_le_zero']:.3f} | "
                f"{row['maxdd_base_pct']:.2f} → {row['maxdd_filtered_pct']:.2f} | "
                f"[{dd_ci[0]:+.2f}, {dd_ci[1]:+.2f}] |"
            )
    lines += ["", "## 汇总", ""]
    for strategy, item in summary.items():
        lines.append(
            f"- `{strategy}`：{item['positive_delta_windows']}/{item['windows']} 窗口正 Δ，"
            f"中位 {item['median_delta_pct']:+.2f}pp，均值 {item['mean_delta_pct']:+.2f}pp。"
        )
    lines += [
        "",
        "## 边界",
        "",
        "- 这是对 2020H1/2021H2/2023H1 已冻结样本的**同一批数据**做不确定性量化，不是独立 holdout。",
        "- 收益差来自组合层日权益，换手率过滤改变了入场集合；不能把差异单独归因于换手率 alpha。",
        "- 窗口只有 3 个，bootstrap 只回答“这批日收益差是否稳定”，不回答“未来是否有效”。",
    ]
    (OUT / "turnover_bootstrap.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"out": str(OUT), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
