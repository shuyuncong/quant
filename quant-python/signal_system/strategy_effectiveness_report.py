#!/usr/bin/env python
"""Read-only multi-window effectiveness report for the three live strategies.

What it does
------------
Replays frozen backtest samples under ``output/backtests`` with the **production
risk defaults** (8% stop, 30% take-profit, no scale-out plan), once with the
production market gate and once without it.  For every (window, strategy, gate)
cell it reports return, annualized return, max drawdown, Sharpe, win rate,
payoff and closed trades, plus a circular block-bootstrap 95% CI for Sharpe,
annualized return and max drawdown.

It never fetches market data, never runs a scan and never writes production
state.  The only writes are the report files under the output directory.

Signal caveat
-------------
The frozen samples carry precomputed ``*.signals.json`` produced when the sample
was built.  Those signals predate the 2026-10-22 ``volume_gate_enabled`` change
for ``macd_zero_axis``.  This report therefore measures the **frozen strategy
definition** of each sample, not a fresh recomputation under today's config.
Risk/exit rules are production defaults; the signal definition is the archived
one.  Use ``--recompute-signals`` to verify a sample under the current config.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import pickle
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from backtest_winrate import build_market_gate  # noqa: E402
from research_backtest import entry_indices, replay  # noqa: E402
from utils.helpers import load_config  # noqa: E402

STRATEGIES = ("macd_zero_axis", "yearline_pullback", "macd_divergence")
STRATEGY_NAMES = {
    "macd_zero_axis": "日线零轴金叉",
    "yearline_pullback": "年线趋势",
    "macd_divergence": "零轴＋底背离",
}
INITIAL_CASH = 250_000.0
MAX_POSITIONS = 4
BOOTSTRAP_REPS = 2_000
BOOTSTRAP_BLOCK = 5
BOOTSTRAP_SEED = 20261026

WINDOWS: tuple[dict[str, str], ...] = (
    {
        "name": "2018H2",
        "root": "output/backtests/_gate_samples/sample-2018-07-01-2018-12-31",
        "start": "2018-07-01",
        "end": "2018-12-31",
    },
    {
        "name": "2019H2",
        "root": "output/backtests/_gate_samples/sample-2019-07-01-2019-12-31",
        "start": "2019-07-01",
        "end": "2019-12-31",
    },
    {
        "name": "2020H2",
        "root": "output/backtests/_gate_samples/sample-2020-07-01-2020-12-31",
        "start": "2020-07-01",
        "end": "2020-12-31",
    },
    {
        "name": "2021H1",
        "root": "output/backtests/_gate_samples/sample-2021-01-01-2021-06-30",
        "start": "2021-01-01",
        "end": "2021-06-30",
    },
    {
        "name": "2022",
        "root": "output/backtests/_gate_samples/sample-2022-01-01-2022-12-31",
        "start": "2022-01-01",
        "end": "2022-12-31",
    },
    {
        "name": "2024H1",
        "root": "output/backtests/divergence-stop-1to10-250k-2024h1-bd36d220",
        "start": "2024-01-01",
        "end": "2024-06-30",
    },
    {
        "name": "2024H2",
        "root": "output/backtests/scale-out-take-profit-250k-2024h2-oos/sample-2024-07-01-2024-12-31",
        "start": "2024-07-01",
        "end": "2024-12-31",
    },
    {
        "name": "2024Q4",
        "root": "output/backtests/scale-out-take-profit-250k-2024q4/sample-2024-10-01-2024-12-31",
        "start": "2024-10-01",
        "end": "2024-12-31",
    },
)


def _sha(*parts: str) -> int:
    payload = "|".join(parts).encode("utf-8")
    return int(hashlib.sha256(payload).hexdigest()[:8], 16)


def _num(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def production_config() -> dict:
    """Current production risk rules, no scale-out plan, no strategy overrides."""
    config = copy.deepcopy(load_config(BASE / "config/config.yaml"))
    risk = config.setdefault("risk", {})
    risk["stop_loss_pct"] = 0.08
    risk["stop_profit_pct"] = 0.30
    # Production defaults: no strategy-level stop/exit override.
    risk.pop("strategy_stop_loss_pct", None)
    risk.pop("exit_plan", None)
    risk.pop("strategy_exit_plan", None)
    return config


def load_sample(root: Path) -> tuple[dict, dict, list[str], dict, dict]:
    """Load a frozen sample in either the gate-sample or task-input layout."""
    inputs = root / "inputs"
    if not inputs.is_dir():
        raise FileNotFoundError(f"sample inputs missing: {inputs}")
    histories: dict[str, pd.DataFrame] = {}
    for path in sorted(inputs.glob("*.pkl")):
        frame = pickle.loads(path.read_bytes())
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            histories[path.stem] = frame

    signals: dict[str, dict[str, set[int]]] = {}
    for path in sorted(root.glob("*.signals.json")):
        stem = path.name[: -len(".signals.json")]
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("signals", payload)
        signals[stem] = {
            strategy: {int(index) for index in indices}
            for strategy, indices in raw.items()
            if strategy in STRATEGIES
        }

    common = sorted(set(histories) & set(signals))
    histories = {symbol: histories[symbol] for symbol in common}
    signals = {symbol: signals[symbol] for symbol in common}
    for per_strategy in signals.values():
        for strategy in STRATEGIES:
            per_strategy.setdefault(strategy, set())

    metadata = json.loads((root / "prepared_metadata.json").read_text(encoding="utf-8"))
    calendar = [str(day) for day in metadata.get("calendar", [])]
    coverage = normalize_coverage(metadata, len(histories))
    return histories, signals, calendar, metadata, coverage


def normalize_coverage(metadata: dict, included: int) -> dict:
    universe = metadata.get("universe")
    total = None
    excluded = None
    if isinstance(universe, dict):
        if "total" in universe:
            total = universe.get("total")
            excluded = universe.get("excluded")
        elif "symbols" in universe and isinstance(universe["symbols"], list):
            total = len(universe["symbols"])
            excluded = len(metadata.get("excluded") or [])
    total = int(total) if total else included
    excluded = int(excluded) if excluded is not None else max(total - included, 0)
    return {
        "included": int(included),
        "total": total,
        "excluded": excluded,
        "included_pct": round(included / total * 100, 2) if total else None,
    }


def gate_map(config: dict) -> dict[str, bool]:
    index_frame = pickle.loads((BASE / "cache/index_000001_sh.pkl").read_bytes())
    gate = build_market_gate(index_frame, config)
    return {str(day): bool(case.get("allows_entries")) for day, case in gate.items()}


def restrict_signals(
    histories: dict[str, pd.DataFrame],
    signals: dict[str, dict[str, set[int]]],
    allowed: dict[str, bool],
) -> dict[str, dict[str, set[int]]]:
    """Drop signal days on which the live market gate forbids new entries."""
    out: dict[str, dict[str, set[int]]] = {}
    for symbol, per_strategy in signals.items():
        frame = histories[symbol]
        dates = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d").tolist()
        out[symbol] = {
            strategy: {
                int(index)
                for index in indices
                if 0 <= int(index) < len(dates) and allowed.get(dates[int(index)], False)
            }
            for strategy, indices in per_strategy.items()
        }
    return out


def recompute_signals(
    histories: dict[str, pd.DataFrame],
    config: dict,
    limit: int = 0,
) -> dict[str, dict[str, set[int]]]:
    """Recompute signals under the current config (optional, slow)."""
    symbols = sorted(histories)
    if limit > 0:
        symbols = symbols[:limit]
    out: dict[str, dict[str, set[int]]] = {}
    for number, symbol in enumerate(symbols, 1):
        out[symbol] = entry_indices(histories[symbol], config)
        if number % 200 == 0:
            print(f"  recomputed {number}/{len(symbols)}", flush=True)
    return out


def restrict_sample(
    histories: dict[str, pd.DataFrame],
    signals: dict[str, dict[str, set[int]]],
    symbols: set[str],
) -> tuple[dict, dict]:
    kept = sorted(symbols & set(histories) & set(signals))
    return (
        {symbol: histories[symbol] for symbol in kept},
        {symbol: signals[symbol] for symbol in kept},
    )


def block_bootstrap(
    curve: list[dict],
    initial: float,
    *,
    reps: int,
    block: int,
    seed: int,
) -> dict[str, dict[str, float | None]]:
    """Circular block bootstrap of daily equity returns."""
    empty = {name: {"low": None, "high": None} for name in ("sharpe", "annualized", "maxdd")}
    if not curve:
        return empty
    equities = np.array([initial] + [float(point["equity"]) for point in curve], dtype=float)
    daily = equities[1:] / equities[:-1] - 1.0
    n = len(daily)
    if n < 2:
        return empty
    rng = np.random.default_rng(seed)
    blocks = int(math.ceil(n / block))
    starts = rng.integers(0, n, size=(reps, blocks))
    offsets = np.arange(block)
    indices = (starts[:, :, None] + offsets[None, None, :]) % n
    samples = daily[indices.reshape(reps, -1)[:, :n]]

    means = samples.mean(axis=1)
    deviations = samples.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpe = np.where(deviations > 1e-12, means / deviations * math.sqrt(252), np.nan)

    log_returns = np.log1p(samples)
    annualized = np.expm1(log_returns.sum(axis=1) * (252.0 / n)) * 100.0
    equity_paths = np.cumprod(1.0 + samples, axis=1)
    peaks = np.maximum.accumulate(equity_paths, axis=1)
    drawdowns = np.max(1.0 - equity_paths / peaks, axis=1) * 100.0

    def interval(values: np.ndarray) -> dict[str, float | None]:
        valid = values[np.isfinite(values)]
        if not len(valid):
            return {"low": None, "high": None}
        low, high = np.percentile(valid, [2.5, 97.5])
        return {"low": _num(low), "high": _num(high)}

    return {
        "sharpe": interval(sharpe),
        "annualized": interval(annualized),
        "maxdd": interval(drawdowns),
    }


def run_case(
    histories: dict[str, pd.DataFrame],
    signals: dict[str, dict[str, set[int]]],
    calendar: list[str],
    config: dict,
    *,
    start: str,
    end: str,
    gate_on: bool,
    allowed: dict[str, bool],
    strategy: str,
) -> dict:
    use_signals = restrict_signals(histories, signals, allowed) if gate_on else signals
    options = {
        "start": start,
        "end": end,
        "max_positions": MAX_POSITIONS,
        "initial_cash": INITIAL_CASH,
        "history_bars": 300,
    }
    return replay(histories, use_signals, calendar, config, options, strategy)


def row_from_result(
    window: str,
    strategy: str,
    gate: str,
    coverage: dict,
    gate_open_days: int,
    gate_total_days: int,
    result: dict,
    bootstrap: dict,
) -> dict:
    metrics = result["metrics"]
    return {
        "window": window,
        "strategy": strategy,
        "strategy_name": STRATEGY_NAMES.get(strategy, strategy),
        "gate": gate,
        "plan": "prod_stop8_tp30_no_exit_plan",
        "included": coverage["included"],
        "total": coverage["total"],
        "included_pct": coverage["included_pct"],
        "gate_open_days": gate_open_days,
        "gate_total_days": gate_total_days,
        "total_return_pct": _num(metrics.get("total_return_pct"), 2),
        "annualized_return_pct": _num(metrics.get("annualized_return_pct"), 2),
        "max_drawdown_pct": _num(metrics.get("max_drawdown_pct"), 2),
        "sharpe_ratio": _num(metrics.get("sharpe_ratio"), 3),
        "payoff_ratio": _num(metrics.get("payoff_ratio"), 3),
        "win_rate_pct": _num(metrics.get("win_rate_pct"), 2),
        "closed_trades": int(metrics.get("closed_trades") or 0),
        "open_positions": (
            len(result["open_positions"])
            if isinstance(result.get("open_positions"), (list, tuple, set, dict))
            else int(result.get("open_positions") or 0)
        ),
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
    allowed: dict[str, bool],
    *,
    recompute: bool,
    recompute_limit: int,
) -> tuple[list[dict], dict]:
    root = BASE / window["root"]
    histories, signals, calendar, _metadata, coverage = load_sample(root)
    if recompute:
        signals = recompute_signals(histories, config, limit=recompute_limit)
        if recompute_limit > 0:
            keep = set(signals)
            histories = {symbol: frame for symbol, frame in histories.items() if symbol in keep}

    days = [day for day in calendar if window["start"] <= day <= window["end"]]
    open_days = sum(1 for day in days if allowed.get(day, False))
    rows: list[dict] = []
    for strategy in STRATEGIES:
        for gate_on in (False, True):
            result = run_case(
                histories,
                signals,
                calendar,
                config,
                start=window["start"],
                end=window["end"],
                gate_on=gate_on,
                allowed=allowed,
                strategy=strategy,
            )
            seed = BOOTSTRAP_SEED + _sha(window["name"], strategy, "on" if gate_on else "off")
            bootstrap = block_bootstrap(
                result.get("equity_curve") or [],
                INITIAL_CASH,
                reps=BOOTSTRAP_REPS,
                block=BOOTSTRAP_BLOCK,
                seed=seed,
            )
            rows.append(
                row_from_result(
                    window["name"],
                    strategy,
                    "on" if gate_on else "off",
                    coverage,
                    open_days,
                    len(days),
                    result,
                    bootstrap,
                )
            )
    return rows, coverage


def bias_check(
    config: dict,
    allowed: dict[str, bool],
    base_window: dict,
    reference_window: dict,
    full_rows: list[dict],
) -> dict:
    """Compare 2024H2 full sample with the subset that also has 2018H2 history.

    ``full_rows`` already contains the 2024H2 gate-off result from the main
    pass, so only the subset needs a replay.
    """
    base_root = BASE / base_window["root"]
    ref_root = BASE / reference_window["root"]
    histories, signals, calendar, _metadata, coverage = load_sample(base_root)
    ref_histories, _ref_signals, _ref_calendar, _ref_meta, _ref_coverage = load_sample(ref_root)
    subset_symbols = set(histories) & set(ref_histories)
    subset_histories, subset_signals = restrict_sample(histories, signals, subset_symbols)

    full_by_strategy = {
        row["strategy"]: row
        for row in full_rows
        if row["window"] == base_window["name"] and row["gate"] == "off"
    }

    cases: dict[str, dict] = {}
    for strategy in STRATEGIES:
        full = full_by_strategy[strategy]
        subset = run_case(
            subset_histories,
            subset_signals,
            calendar,
            config,
            start=base_window["start"],
            end=base_window["end"],
            gate_on=False,
            allowed=allowed,
            strategy=strategy,
        )
        subset_metrics = subset["metrics"]
        cases[strategy] = {
            "full": {
                "symbols": len(histories),
                "return_pct": full["total_return_pct"],
                "sharpe": full["sharpe_ratio"],
                "max_dd_pct": full["max_drawdown_pct"],
                "trades": full["closed_trades"],
            },
            "subset": {
                "symbols": len(subset_histories),
                "return_pct": _num(subset_metrics.get("total_return_pct"), 2),
                "sharpe": _num(subset_metrics.get("sharpe_ratio"), 3),
                "max_dd_pct": _num(subset_metrics.get("max_drawdown_pct"), 2),
                "trades": int(subset_metrics.get("closed_trades") or 0),
            },
            "delta": {
                "return_pct": _num(
                    float(subset_metrics.get("total_return_pct") or 0)
                    - float(full["total_return_pct"] or 0),
                    2,
                ),
                "sharpe": _num(
                    float(subset_metrics.get("sharpe_ratio") or 0)
                    - float(full["sharpe_ratio"] or 0),
                    3,
                ),
                "max_dd_pct": _num(
                    float(subset_metrics.get("max_drawdown_pct") or 0)
                    - float(full["max_drawdown_pct"] or 0),
                    2,
                ),
            },
        }
    return {
        "base_window": base_window["name"],
        "reference_window": reference_window["name"],
        "full_symbols": len(histories),
        "reference_symbols": len(ref_histories),
        "subset_symbols": len(subset_histories),
        "base_coverage": coverage,
        "reference_coverage": _ref_coverage,
        "cases": cases,
        "note": "subset = symbols present in both the 2024H2 sample and the 2018H2 sample; gate off",
    }


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}{suffix}"


def markdown_report(
    rows: list[dict],
    windows: list[str],
    coverage_rows: dict[str, dict],
    bias: dict,
    *,
    generated_at: str,
) -> str:
    lines: list[str] = []
    lines.append("# 三策略历史有效性汇总（只读）")
    lines.append("")
    lines.append(f"生成时间：{generated_at}")
    lines.append("")
    lines.append("## 口径")
    lines.append("")
    lines.append("- 生产风险默认：止损 8%、止盈 30%、**不启用分批止盈**、无策略级覆盖。")
    lines.append("- 策略信号来自冻结样本的 `*.signals.json`；样本冻结早于 2026-10-22 的 `volume_gate_enabled` 变更。")
    lines.append("- 股票池为各样本实际落盘的当前上市股票；无退市股。")
    lines.append("- 引擎沿用 `research_backtest.replay`；成本、T+1、涨跌停、印花税与既有研究一致。")
    lines.append("- CI 为按交易日 circular block bootstrap（block=5，2,000 次）的 95% 区间；点估计与 CI 都只代表该窗口。")
    lines.append("- `gate=off` 是策略本身；`gate=on` 是生产市场闸门后的结果。")
    lines.append("- 半年/单季窗口的年化收益仅作参考，不应当作长期年化。")
    lines.append("- 2018H2/2024H1 闸门开放 0 天、2022 仅 1 天：这些窗口的 `gate=on` 是零交易/极少交易，不能当作策略失效证据。")
    lines.append("- 2018–2022 样本覆盖约 22%–27%，2024 约 87%–95%；跨窗口只比同一窗口内的策略排序，不直接比绝对收益。")
    lines.append("")

    lines.append("## 覆盖")
    lines.append("")
    lines.append("| 窗口 | 样本/全市场 | 覆盖 | 闸门开放 |")
    lines.append("|---|---:|---:|---:|")
    for window in windows:
        cov = coverage_rows[window]
        gate_cell = next(
            (row for row in rows if row["window"] == window and row["gate"] == "off"),
            None,
        )
        open_days = gate_cell["gate_open_days"] if gate_cell else 0
        total_days = gate_cell["gate_total_days"] if gate_cell else 0
        lines.append(
            f"| {window} | {cov['included']}/{cov['total']} | {_fmt(cov['included_pct'], 1, '%')} | "
            f"{open_days}/{total_days} |"
        )
    lines.append("")

    for gate, title in (("off", "策略本身（无市场闸门）"), ("on", "生产市场闸门")):
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| 窗口 | 策略 | 收益% | 年化% | Sharpe | 95% CI | 回撤% | 笔数 | 胜率% |")
        lines.append("|---|---|---:|---:|---:|---|---:|---:|---:|")
        for row in rows:
            if row["gate"] != gate:
                continue
            ci = (
                "—"
                if row["sharpe_ci_low"] is None or row["sharpe_ci_high"] is None
                else f"[{row['sharpe_ci_low']:.2f}, {row['sharpe_ci_high']:.2f}]"
            )
            lines.append(
                f"| {row['window']} | {row['strategy_name']} | {_fmt(row['total_return_pct'])} | "
                f"{_fmt(row['annualized_return_pct'])} | {_fmt(row['sharpe_ratio'], 2)} | {ci} | "
                f"{_fmt(row['max_drawdown_pct'])} | {row['closed_trades']} | {_fmt(row['win_rate_pct'], 1)} |"
            )
        lines.append("")

    lines.append("## 子样本偏差检查（2024H2 gate off）")
    lines.append("")
    if bias.get("skipped") or not bias.get("cases"):
        lines.append("本次运行跳过（`--no-bias` 或未同时选择 2024H2 / 2018H2）。")
    else:
        lines.append(
            f"全市场 {bias['full_symbols']} 只 vs 同时拥有 2018H2 历史的 {bias['subset_symbols']} 只子样本。"
        )
        lines.append("")
        lines.append("| 策略 | 全市场收益% | 子样本收益% | Δ收益 | 全市场 Sharpe | 子样本 Sharpe | ΔSharpe |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for strategy, case in bias["cases"].items():
            lines.append(
                f"| {STRATEGY_NAMES.get(strategy, strategy)} | {_fmt(case['full']['return_pct'])} | "
                f"{_fmt(case['subset']['return_pct'])} | {_fmt(case['delta']['return_pct'])} | "
                f"{_fmt(case['full']['sharpe'], 2)} | {_fmt(case['subset']['sharpe'], 2)} | "
                f"{_fmt(case['delta']['sharpe'], 2)} |"
            )
    lines.append("")

    lines.append("## 自动读数（只描述表，不做显著性结论）")
    lines.append("")
    gate_off = [row for row in rows if row["gate"] == "off"]
    for window in windows:
        window_rows = [row for row in gate_off if row["window"] == window]
        ranked = [row for row in window_rows if row["sharpe_ratio"] is not None]
        if not ranked:
            lines.append(f"- {window}: 无可计算 Sharpe 的交易。")
            continue
        best = max(ranked, key=lambda row: float(row["sharpe_ratio"]))
        lines.append(
            f"- {window}: Sharpe 最高 {best['strategy_name']} "
            f"({best['sharpe_ratio']:.2f}, 收益 {_fmt(best['total_return_pct'])}%, {best['closed_trades']} 笔)。"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    global BOOTSTRAP_REPS, BOOTSTRAP_BLOCK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/backtest-results/2026-10-26-effectiveness")
    parser.add_argument("--recompute-signals", action="store_true", help="recompute signals under current config (slow)")
    parser.add_argument("--recompute-limit", type=int, default=0, help="limit symbols for recompute; 0 = all")
    parser.add_argument("--reps", type=int, default=BOOTSTRAP_REPS)
    parser.add_argument("--block", type=int, default=BOOTSTRAP_BLOCK)
    parser.add_argument("--windows", default="", help="comma-separated subset of window names")
    parser.add_argument("--no-bias", action="store_true", help="skip the 2024H2 vs 2018H2 subset check")
    args = parser.parse_args()

    BOOTSTRAP_REPS = max(args.reps, 100)
    BOOTSTRAP_BLOCK = max(args.block, 1)

    out_dir = BASE.parent.parent / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    config = production_config()
    allowed = gate_map(config)
    selected = {name.strip() for name in args.windows.split(",") if name.strip()}

    rows: list[dict] = []
    coverage_rows: dict[str, dict] = {}
    available_windows: list[str] = []
    for window in WINDOWS:
        if selected and window["name"] not in selected:
            continue
        root = BASE / window["root"]
        if not root.is_dir():
            print(f"skip {window['name']}: missing {root}", flush=True)
            continue
        window_rows, coverage = evaluate_window(
            window,
            config,
            allowed,
            recompute=args.recompute_signals,
            recompute_limit=args.recompute_limit,
        )
        rows.extend(window_rows)
        coverage_rows[window["name"]] = coverage
        available_windows.append(window["name"])
        print(f"{window['name']}: {coverage['included']}/{coverage['total']} symbols", flush=True)
        # Checkpoint: a long run can be inspected without waiting for every window.
        partial = {
            "partial": True,
            "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S%z"),
            "coverage": coverage_rows,
            "rows": rows,
        }
        (out_dir / "effectiveness.partial.json").write_text(
            json.dumps(partial, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    if not rows:
        raise SystemExit("no windows selected or no sample directories found")

    bias: dict = {
        "base_window": "2024H2",
        "reference_window": "2018H2",
        "full_symbols": None,
        "reference_symbols": None,
        "subset_symbols": None,
        "base_coverage": {},
        "reference_coverage": {},
        "cases": {},
        "skipped": True,
        "note": "skipped (--no-bias or window not selected)",
    }
    if not args.no_bias and {"2024H2", "2018H2"}.issubset(available_windows):
        bias = bias_check(
            config,
            allowed,
            next(window for window in WINDOWS if window["name"] == "2024H2"),
            next(window for window in WINDOWS if window["name"] == "2018H2"),
            rows,
        )

    generated_at = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S%z")
    payload = {
        "generated_at": generated_at,
        "method": {
            "risk": "stop_loss_pct=0.08, stop_profit_pct=0.30, no exit_plan, no strategy overrides",
            "initial_cash": INITIAL_CASH,
            "max_positions": MAX_POSITIONS,
            "bootstrap": {"reps": BOOTSTRAP_REPS, "block": BOOTSTRAP_BLOCK, "ci": "percentile 2.5/97.5"},
            "signals": "frozen sample *.signals.json; recompute flag=%s" % args.recompute_signals,
            "universe": "each sample's persisted current-listed set; no delisted names",
        },
        "coverage": coverage_rows,
        "rows": rows,
        "bias_check": bias,
    }
    (out_dir / "effectiveness.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    fieldnames = list(rows[0].keys())
    with (out_dir / "effectiveness.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        import csv

        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    report = markdown_report(rows, available_windows, coverage_rows, bias, generated_at=generated_at)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"\nwrote {out_dir / 'effectiveness.json'}")
    print(f"wrote {out_dir / 'effectiveness.csv'}")
    print(f"wrote {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
