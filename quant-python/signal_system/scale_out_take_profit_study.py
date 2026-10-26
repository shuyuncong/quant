"""三策略 × 分批止盈 vs 一次全清 —— 冻结 2024H1 半年样本的参数对照。

样本、成本与执行口径完全沿用 2026-10-07 三策略止损回测 (25 万、最多 4 只、117 个交易日、
日线零轴金叉 / 年线趋势 / 零轴＋底背离)，只改变**止盈怎么卖**：

  none       固定止盈一次性全清 (生产口径基线, 30%)
  ladder_a   1/3@+15% + 1/3@+30% + 1/3@+45%
  ladder_b   1/2@+15% + 1/2@+30%
  ladder_c   1/2@+20% + 1/2@+40%
  trailing_a 激活 +15% 后回撤 8% 全清
  trailing_b 激活 +20% 后回撤 10% 全清
  hybrid_a   1/2@+15% + 尾仓 (激活 +15% / 回撤 10%)
  hybrid_b   1/2@+15% + 尾仓保本止损

分批减仓的引擎口径见 strategy.registry.resolve_exit_plan：每档在日线最高价触及绝对
涨幅当日成交 (成交价取档次价与开盘价的较优者)，卖出**原始仓位**固定比例，同一天最多
执行一档，比例按整手截断且最后一档吸收零头；尾仓保护只看收盘价，激活当根不清仓
(避免用当根最高价做当根决策的前视)；止损先于止盈。计划一旦生效即接管止盈侧，
固定止盈 (risk.stop_profit_pct) 对该策略不再产生离场条件。

用法:
    python scale_out_take_profit_study.py --workers 4 \\
        --out-json output/backtests/scale-out-take-profit-250k-2024h1/comparison.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from copy import deepcopy
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from research_backtest import _snapshot_config, replay, write_json  # noqa: E402
from strategy.registry import STRATEGIES, VERSION  # noqa: E402

# 归档目录 (2026-10-07 三策略止损敏感性回测)：提供 30 组基线指标做 parity 核对。
ARCHIVE_DIR = BASE_DIR / "output" / "backtests" / "three-strategies-stop-1to10-250k-2024h1-d4376c1c"
# 源冻结样本：行情 pickle、信号缓存、日历与配置快照。
DEFAULT_SOURCE = BASE_DIR / "output" / "backtests" / "divergence-stop-1to10-250k-2024h1-bd36d220"
DEFAULT_OUT = BASE_DIR / "output" / "backtests" / "scale-out-take-profit-250k-2024h1" / "comparison.json"

# 三策略各自在归档里收益最好的止损档位 (2026-10-07)。这不是投产参数，
# 只是本次"只动止盈"的固定口径，换档位会同时改动止损与止盈两个变量。
ARCHIVED_STOP = {"macd_zero_axis": 0.10, "yearline_pullback": 0.03, "macd_divergence": 0.06}
PLAN_DEFINITIONS = {
    "ladder_a": {"initial_fraction": 1 / 3, "first_gain": .15,
                 "tiers": [{"gain": .30, "fraction": 1 / 3}, {"gain": .45, "fraction": 1 / 3}]},
    "ladder_b": {"initial_fraction": .5, "first_gain": .15,
                 "tiers": [{"gain": .30, "fraction": .5}]},
    "ladder_c": {"initial_fraction": .5, "first_gain": .20,
                 "tiers": [{"gain": .40, "fraction": .5}]},
    "trailing_a": {"trailing_activation_gain": .15, "trailing_pct": .08},
    "trailing_b": {"trailing_activation_gain": .20, "trailing_pct": .10},
    "hybrid_a": {"initial_fraction": .5, "first_gain": .15,
                 "trailing_activation_gain": .15, "trailing_pct": .10},
    "hybrid_b": {"initial_fraction": .5, "first_gain": .15, "protect_tail": "breakeven"},
}
PLAN_NAMES = {
    "none": "固定止盈全清 (30%)",
    "ladder_a": "阶梯 1/3@+15% / 1/3@+30% / 1/3@+45%",
    "ladder_b": "阶梯 1/2@+15% / 1/2@+30%",
    "ladder_c": "阶梯 1/2@+20% / 1/2@+40%",
    "trailing_a": "移动止盈 激活+15% / 回撤8%",
    "trailing_b": "移动止盈 激活+20% / 回撤10%",
    "hybrid_a": "半仓@+15% + 尾仓移动止盈 (+15%/10%)",
    "hybrid_b": "半仓@+15% + 尾仓保本止损",
}
PARITY_FIELDS = ("total_return_pct", "annualized_return_pct", "max_drawdown_pct", "sharpe_ratio",
                 "payoff_ratio", "win_rate_pct", "closed_trades", "final_equity")
# 基线 (metrics.csv) 的列名 -> 归档 JSON 字段名，两者用的标签不同。
ARCHIVE_COLUMNS = ("strategy_id", "strategy", "stop_loss_percent", "initial_cash", "total_return_pct",
                   "annualized_return_pct", "max_drawdown_pct", "sharpe_ratio", "payoff_ratio",
                   "win_rate_pct", "closed_trades", "final_equity", "pnl_cash", "open_positions",
                   "wins", "losses")

# 每个 worker 进程只加载一次冻结样本，避免每个场景重读 182 MB pickle。
_WORKER: dict = {}


def _read_archive_baseline() -> dict[tuple[str, int], dict]:
    with (ARCHIVE_DIR / "metrics.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(zip(ARCHIVE_COLUMNS, row)) for row in csv.reader(handle) if row and row[0] != "strategy_id"]
    return {(row["strategy_id"], int(row["stop_loss_percent"])): row for row in rows}


def _read_source_meta(source: Path) -> tuple[list[str], dict]:
    archive = json.loads((ARCHIVE_DIR / "comparison.json").read_text(encoding="utf-8"))
    calendar = json.loads((source / "prepared_metadata.json").read_text(encoding="utf-8"))["calendar"]
    # 只保留公共 options：output_dir 属于服务端路径，不进入研究快照。
    options = {key: value for key, value in archive["options"].items() if key != "output_dir"}
    return calendar, {"config_snapshot": archive["config_snapshot"], "options": options}


def _init_worker(source: str, config_json: str, options_json: str, calendar_json: str) -> None:
    histories = {path.stem: pd.read_pickle(path) for path in sorted((Path(source) / "inputs").glob("*.pkl"))}
    signals = {}
    for path in Path(source).glob("*.signals.json"):
        cached = json.loads(path.read_text(encoding="utf-8"))
        signals[path.name[: -len(".signals.json")]] = {name: set(indices) for name, indices in cached["signals"].items()}
    _WORKER.update(histories=histories, signals=signals, config_snapshot=json.loads(config_json),
                   options=json.loads(options_json), calendar=json.loads(calendar_json))


def _out_of_sample_job(symbol: str) -> tuple[str, str]:
    """One symbol of the cache-built sample: load qfq bars, screen, compute signals, persist.

    Writes the same layout as a frozen archive package so the grid can be re-run later
    without recomputing (``inputs/<symbol>.pkl`` + ``<symbol>.signals.json``).  An
    existing pair is treated as done, which makes a long build resumable.
    """
    from research_backtest import ILLEGAL_HISTORY_REASON, MIN_WARMUP_BARS, entry_indices, _illegal_history_rows

    start, end = _WORKER["period"]
    out_dir: Path = _WORKER["out_dir"]
    inputs_dir = out_dir / "inputs"
    if (inputs_dir / f"{symbol}.pkl").exists() and (out_dir / f"{symbol}.signals.json").exists():
        return symbol, "cached"
    frame = pd.read_pickle(_WORKER["cache_dir"] / f"{symbol}_qfq.pkl")
    if frame.attrs.get("adjust") != "qfq" or frame.attrs.get("timeframe") != "1d":
        return symbol, "缓存不是带口径元数据的前复权日线"
    if "is_closed" in frame:
        frame = frame[frame["is_closed"].astype(bool)]
    # Never generate candidates beyond the requested research window, including
    # a sealed future holdout that may happen to be present in the source cache.
    frame = frame[pd.to_datetime(frame["datetime"]) <= end].reset_index(drop=True)
    dates = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d")
    if int((dates < start).sum()) < MIN_WARMUP_BARS:
        return symbol, "首个交易日前不足 270 根已收盘日线"
    if not int(((dates >= start) & (dates <= end)).sum()):
        return symbol, "区间内无历史行情"
    # 缓存的 qfq 序列按抓取时点前复权；区间内非法价格与冻结样本同样剔除。
    if _illegal_history_rows(frame[(dates >= start) & (dates <= end)]):
        return symbol, ILLEGAL_HISTORY_REASON
    try:
        signals = entry_indices(frame, _WORKER["config_snapshot"])
    except Exception as error:  # noqa: BLE001 - 单只股票的信号失败不应吞掉整轮样本
        return symbol, f"信号计算失败: {error}"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    frame.to_pickle(inputs_dir / f"{symbol}.pkl")
    (out_dir / f"{symbol}.signals.json").write_text(
        json.dumps({"symbol": symbol, "start": start, "end": end,
                    "dates": dates[(dates >= start) & (dates <= end)].tolist(),
                    "signals": {name: sorted(indices) for name, indices in signals.items()}},
                   ensure_ascii=False), encoding="utf-8")
    return symbol, ""


def build_cache_sample(config: dict, start: str, end: str, workers: int, out_dir: Path, *,
                       cache_dir: Path | None = None) -> dict:
    """构建区间样本包：本地前复权缓存 -> 与冻结样本相同的预热/剔除规则 -> 三策略信号。

    输出目录与归档冻结包同构 (`inputs/<symbol>.pkl` + `<symbol>.signals.json` +
    `prepared_metadata.json`)，因此可以用 `--period archive --source <out_dir>` 复用，
    也可以中途中断后重跑 (已写入的股票直接跳过)。
    """
    cache_dir = (cache_dir or BASE_DIR / "cache" / "daily_history").resolve()
    symbols = sorted(path.name[: -len("_qfq.pkl")] for path in cache_dir.glob("*_qfq.pkl"))
    if not symbols:
        raise ValueError(f"没有本地前复权历史：{cache_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot_config(config)
    spec = {"cache_dir": str(cache_dir), "start": start, "end": end,
            "config_snapshot": snapshot, "strategy_version": VERSION}
    spec_path = out_dir / "sample_spec.json"
    if spec_path.exists() and json.loads(spec_path.read_text(encoding="utf-8")) != spec:
        raise ValueError("样本的数据目录、窗口或配置已变化；请使用新 --out-dir，不覆盖已冻结输入")
    write_json(spec_path, spec)
    initializer = (_init_sample_worker, (json.dumps(snapshot, ensure_ascii=False), start, end,
                                        str(out_dir), str(cache_dir)))
    with Pool(processes=max(1, workers), initializer=initializer[0], initargs=initializer[1]) as pool:
        outcomes = pool.map(_out_of_sample_job, symbols)
    excluded = [{"symbol": symbol, "reason": reason} for symbol, reason in outcomes if reason not in ("", "cached")]
    included = sum(1 for _, reason in outcomes if reason in ("", "cached"))
    # 每日历来自各标的信号文件里记录的区间交易日，不必再读一遍行情 pickle。
    calendar: set[str] = set()
    for path in out_dir.glob("*.signals.json"):
        calendar.update(json.loads(path.read_text(encoding="utf-8"))["dates"])
    metadata = {"start": start, "end": end, "calendar": sorted(calendar), "universe": {
        "total": len(symbols), "included": included, "excluded": len(excluded),
        "cached": sum(1 for _, reason in outcomes if reason == "cached")},
        "exclusions": excluded, "source": str(cache_dir), "version": VERSION,
        "cache_dir": str(cache_dir), "config_snapshot": snapshot}
    write_json(out_dir / "prepared_metadata.json", metadata)
    return {"out_dir": out_dir, "metadata": metadata, "excluded": excluded}


def _init_sample_worker(config_json: str, start: str, end: str, out_dir: str, cache_dir: str) -> None:
    """Sample-building pool needs the explicit source directory in each child."""
    _WORKER.update(config_snapshot=json.loads(config_json), period=(start, end), out_dir=Path(out_dir),
                   cache_dir=Path(cache_dir))


def _scenario_job(job: tuple[str, str, float | None]) -> dict:
    """Replay one (strategy, plan, stop) cell; a missing stop uses the archived level."""
    strategy, plan_key, stop = job
    if stop is None:
        stop = ARCHIVED_STOP[strategy]
    config = deepcopy(_WORKER["config_snapshot"])
    risk = config.setdefault("risk", {})
    risk["stop_loss_pct"] = stop
    # 归档快照没有 strategy_stop_loss_pct / exit_plan；显式清理可确保换源时不会
    # 悄悄从策略级覆盖取值，让 "只改止盈" 这个单变量前提成立。
    risk.pop("strategy_stop_loss_pct", None)
    risk.pop("strategy_exit_plan", None)
    plan = None if plan_key == "none" else deepcopy(PLAN_DEFINITIONS[plan_key])
    if plan is None:
        risk.pop("exit_plan", None)
    else:
        risk["exit_plan"] = plan
    result = replay(_WORKER["histories"], _WORKER["signals"], _WORKER["calendar"], config, _WORKER["options"], strategy)
    # 曲线与拒单明细不进研究快照：曲线按场景重放即可重建，拒单明细本样本恒为空。
    result.pop("equity_curve", None)
    result.pop("rejected", None)
    return {"strategy_id": strategy, "strategy": STRATEGIES[strategy], "plan": plan_key,
            "plan_name": PLAN_NAMES[plan_key], "stop_loss_pct": stop, "plan_definition": plan,
            "metrics": result["metrics"], "trades": result["trades"],
            "open_positions": result["open_positions"], "warnings": result["warnings"]}


def _position_rows(trades: list[dict]) -> list[dict]:
    """Position-level P&L: one row per (symbol, entry_day) instead of one per scale-out.

    分批减仓会把一笔持仓拆成多行成交，直接按行统计胜率/盈亏比会让"分两次卖"的持仓
    在分母里出现两次。这里同时给出持仓级聚合，便于与一次性全清基线同口径比较。
    """
    grouped: dict[tuple[str, str], dict] = {}
    for trade in trades:
        key = (trade["symbol"], trade["entry_day"])
        row = grouped.setdefault(key, {"symbol": trade["symbol"], "entry_day": trade["entry_day"],
                                       "pnl_cash": 0.0, "exits": [], "sell_count": 0})
        row["pnl_cash"] += float(trade["pnl_cash"])
        row["exits"].append(trade["exit_reason"])
        row["sell_count"] += 1
    return list(grouped.values())


def _stats(rows: list[dict]) -> dict:
    wins = [row["pnl_cash"] for row in rows if row["pnl_cash"] > 0]
    losses = [row["pnl_cash"] for row in rows if row["pnl_cash"] <= 0]
    return {
        "positions": len(rows),
        "position_wins": len(wins),
        "position_losses": len(losses),
        "position_win_rate_pct": round(len(wins) / len(rows) * 100, 4) if rows else None,
        "position_payoff_ratio": round(sum(wins) / len(wins) / abs(sum(losses) / len(losses)), 4)
        if wins and losses and sum(losses) else None,
        "position_pnl_cash": round(sum(row["pnl_cash"] for row in rows), 6),
        "scale_out_positions": sum(1 for row in rows if row["sell_count"] > 1),
    }


def _summary_row(scenario: dict) -> dict:
    metrics = scenario["metrics"]
    positions = _position_rows(scenario["trades"])
    exits: dict[str, int] = {}
    for trade in scenario["trades"]:
        exits[trade["exit_reason"]] = exits.get(trade["exit_reason"], 0) + 1
    return {
        "strategy_id": scenario["strategy_id"], "strategy": scenario["strategy"],
        "plan": scenario["plan"], "plan_name": scenario["plan_name"],
        "stop_loss_percent": round(scenario["stop_loss_pct"] * 100),
        "total_return_pct": metrics["total_return_pct"],
        "annualized_return_pct": metrics["annualized_return_pct"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "sharpe_ratio": metrics["sharpe_ratio"],
        "payoff_ratio": metrics["payoff_ratio"],
        "win_rate_pct": metrics["win_rate_pct"],
        "closed_trades": metrics["closed_trades"],
        "final_equity": metrics["final_equity"],
        "open_positions": scenario["open_positions"],
        "exit_take_profit": exits.get("take_profit", 0),
        "exit_profit_lock": exits.get("profit_lock", 0),
        "exit_stop_loss": exits.get("stop_loss", 0),
        **_stats(positions),
    }


def _parity_check(scenarios: list[dict], baseline: dict[tuple[str, int], dict]) -> dict:
    """The plan-free cells must reproduce the archived 30-cell grid exactly."""
    cells = [scenario for scenario in scenarios if scenario["plan"] == "none"]
    if not cells:
        return {"checked": False, "reason": "本次运行未包含 none 基线场景"}
    result = {}
    for scenario in cells:
        row = baseline[(scenario["strategy_id"], round(scenario["stop_loss_pct"] * 100))]
        metrics = scenario["metrics"]
        differences = {field: [metrics[field], float(row[field])] for field in PARITY_FIELDS
                       if round(float(metrics[field]), 6) != round(float(row[field]), 6)}
        result[scenario["strategy_id"]] = {"match": not differences, "differences": differences}
    return {"checked": True, "baselines": result}


def _parity_grid_job(job: tuple[str, float]) -> dict:
    """One cell of the archived stop-loss grid: no plan, one stop level."""
    strategy, stop = job
    return _scenario_job((strategy, "none", stop))


def run_parity_grid(workers: int) -> dict:
    """Re-run all 30 archived stop-loss cells through the current engine and diff them.

    This is the regression gate for engine changes: the plan-free path must stay
    byte-identical to ``docs/backtests-results/2026-10-07-...`` archive.
    """
    calendar, meta = _read_source_meta(Path(DEFAULT_SOURCE))
    options = {**meta["options"], "start": calendar[0], "end": calendar[-1]}
    jobs = [(strategy, round(stop / 100, 4)) for strategy in STRATEGIES for stop in range(1, 11)]
    initializer = (_init_worker, (str(DEFAULT_SOURCE), json.dumps(meta["config_snapshot"], ensure_ascii=False),
                                  json.dumps(options, ensure_ascii=False), json.dumps(calendar, ensure_ascii=False)))
    with Pool(processes=max(1, workers), initializer=initializer[0], initargs=initializer[1]) as pool:
        scenarios = pool.map(_parity_grid_job, jobs)
    baseline = _read_archive_baseline()
    cells = []
    for scenario in scenarios:
        row = baseline[(scenario["strategy_id"], round(scenario["stop_loss_pct"] * 100))]
        differences = {field: [scenario["metrics"][field], float(row[field])] for field in PARITY_FIELDS
                       if round(float(scenario["metrics"][field]), 6) != round(float(row[field]), 6)}
        cells.append({"strategy_id": scenario["strategy_id"], "stop_loss_pct": scenario["stop_loss_pct"],
                      "match": not differences, "differences": differences})
    return {"kind": "scale_out_engine_parity_grid", "checked_at": datetime.now().isoformat(timespec="seconds"),
            "strategy_version": VERSION, "cells": cells, "mismatches": sum(1 for cell in cells if not cell["match"])}


def _exclusion_breakdown(excluded: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in excluded:
        reason = str(row["reason"]).split(":")[0]
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def run(args: argparse.Namespace) -> dict:
    jobs = [(strategy, plan, None) for strategy in STRATEGIES for plan in args.plans]
    calendar, meta = _read_source_meta(Path(args.source))
    options = {**meta["options"], "max_positions": args.max_positions, "initial_cash": args.initial_cash}
    config_json = json.dumps(meta["config_snapshot"], ensure_ascii=False)
    if args.period == "archive":
        options = {**options, "start": calendar[0], "end": calendar[-1]}
        initializer = (_init_worker, (str(args.source), config_json, json.dumps(options, ensure_ascii=False),
                                      json.dumps(calendar, ensure_ascii=False)))
        sample = {"kind": "archive_frozen", "source": str(args.source), "calendar_days": len(calendar)}
    else:
        # 样本外区间先用缓存在本地"烤"出一份与冻结包同构的样本 (可中断续跑),
        # 之后与 archive 一样按目录加载，网格本身不再区分数据来源。
        source = Path(args.out_json).parent / f"sample-{args.start}-{args.end}"
        built = build_cache_sample(meta["config_snapshot"], args.start, args.end, args.workers, source)
        calendar = built["metadata"]["calendar"]
        options = {**options, "start": args.start, "end": args.end}
        initializer = (_init_worker, (str(source), config_json, json.dumps(options, ensure_ascii=False),
                                      json.dumps(calendar, ensure_ascii=False)))
        sample = {"kind": "cache_out_of_sample", "source": str(source), "start": args.start, "end": args.end,
                  "universe": built["metadata"]["universe"],
                  "exclusion_reasons": _exclusion_breakdown(built["excluded"]),
                  "calendar_days": len(calendar)}
    with Pool(processes=max(1, min(args.workers, len(jobs))), initializer=initializer[0], initargs=initializer[1]) as pool:
        scenarios = pool.map(_scenario_job, jobs)
    verification = ({"parity_vs_archive": _parity_check(scenarios, _read_archive_baseline())}
                    if args.period == "archive" else {"parity_vs_archive": {"checked": False, "reason": "样本外区间不与 H1 归档核字段"}})
    summary = [_summary_row(scenario) for scenario in scenarios]
    report = {
        "kind": "scale_out_take_profit_study",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "strategy_version": VERSION,
        "sample": sample,
        "options": options,
        "archived_stop_loss_pct": ARCHIVED_STOP,
        "varied_parameter": "risk.exit_plan",
        "plan_definitions": {"none": None, **{key: PLAN_DEFINITIONS[key] for key in args.plans if key != "none"}},
        "plan_names": {key: PLAN_NAMES[key] for key in args.plans},
        "config_snapshot": meta["config_snapshot"],
        "verification": verification,
        "summary": summary,
        "scenarios": scenarios,
    }
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, default=str), encoding="utf-8")
    with out_json.with_name("metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--out-json", default=str(DEFAULT_OUT))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--plans", nargs="*", default=["none", *PLAN_DEFINITIONS])
    parser.add_argument("--period", choices=["archive", "oos"], default="archive",
                        help="archive=复用归档冻结样本; oos=用本地前复权缓存构建的样本外区间")
    parser.add_argument("--start", default="2024-07-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--initial-cash", type=float, default=250_000.0)
    parser.add_argument("--max-positions", type=int, default=4)
    parser.add_argument("--parity-grid", action="store_true",
                        help="重跑归档 30 组止损网格并逐字段比对，用于引擎改动的回归核验")
    args = parser.parse_args()
    if args.parity_grid:
        report = run_parity_grid(args.workers)
        out = Path(args.out_json).with_name("parity-grid.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        print(f"wrote {out}: {len(report['cells'])} cells, mismatches={report['mismatches']}")
        for cell in report["cells"]:
            if not cell["match"]:
                print("MISMATCH", cell["strategy_id"], cell["stop_loss_pct"], json.dumps(cell["differences"], ensure_ascii=False))
        return 0 if report["mismatches"] == 0 else 1
    report = run(args)
    print(f"wrote {args.out_json}: {len(report['scenarios'])} scenarios; sample={json.dumps(report['sample'], ensure_ascii=False)}")
    if report["verification"]:
        print("parity:", json.dumps(report["verification"]["parity_vs_archive"], ensure_ascii=False))
    for row in report["summary"]:
        print(f"{row['strategy']:<8} {row['plan']:<11} stop={row['stop_loss_percent']}% "
              f"ret={row['total_return_pct']:8.3f}% dd={row['max_drawdown_pct']:6.2f}% "
              f"sharpe={row['sharpe_ratio'] if row['sharpe_ratio'] is None else round(row['sharpe_ratio'], 3)} "
              f"pos_win={row['position_win_rate_pct']}% sellouts={row['scale_out_positions']}/{row['positions']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
