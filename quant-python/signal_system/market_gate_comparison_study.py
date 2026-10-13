"""闸门对照回测（有闸门 vs 无闸门）× 三策略，按指定窗口跑。

为什么用独立脚本而非 scale_out_take_profit_study：那个工具是对比"止盈方案"的，
本任务是对比"闸门开/关"，且需要一个「闸门开放日才允许入场」的信号过滤器。
口径与 scale_out 的冻结样本保持一致（同一样本目录、同一 options），
以便与既有归档对照。

用法：
  python market_gate_comparison_study.py \
      --sample output/backtests/_gate_samples/sample-2020-07-01-2020-12-31 \
      --start 2020-07-01 --end 2020-12-31 \
      --out output/backtests/_gate_extended/gate_2020h2.json --label 2020H2

先建样本：python research_sample_builder.py <start> <end>
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from pathlib import Path

BASE = Path(os.path.abspath(__file__)).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

import pandas as pd  # noqa: E402

from backtest_winrate import build_market_gate  # noqa: E402
from utils.helpers import load_config  # noqa: E402
from research_backtest import replay  # noqa: E402
from scale_out_take_profit_study import ARCHIVED_STOP, PLAN_DEFINITIONS, deepcopy  # noqa: E402

# 各策略的离场方案：与 2026-10-13 对照回测保持一致（各策略自有最优）
BEST_PLAN = {"macd_zero_axis": "ladder_b", "yearline_pullback": "none", "macd_divergence": "ladder_b"}
STRATEGIES = ("macd_zero_axis", "yearline_pullback", "macd_divergence")


def load_sample(root: Path) -> tuple[dict, dict, list, dict]:
    histories = {p.stem: pickle.loads(p.read_bytes()) for p in sorted((root / "inputs").glob("*.pkl"))}
    signals = {}
    for p in sorted(root.glob("*.signals.json")):
        payload = json.loads(p.read_text(encoding="utf-8"))
        signals[p.name[: -len(".signals.json")]] = {k: set(v) for k, v in payload["signals"].items()}
    meta = json.loads((root / "prepared_metadata.json").read_text(encoding="utf-8"))
    # build_cache_sample 写的 metadata 不含 config_snapshot（那只在 run() 汇总时补上），
    # 故这里从 H1 冻结归档取同一份快照，保证三策略参数与既有研究口径一致。
    if "config_snapshot" not in meta:
        archive = json.loads((BASE / "output/backtests/divergence-stop-1to10-250k-2024h1-bd36d220"
                              / "comparison.json").read_text(encoding="utf-8"))
        meta["config_snapshot"] = archive["config_snapshot"]
    return histories, signals, meta["calendar"], meta


def gate_map(config: dict) -> dict[str, bool]:
    index_frame = pickle.loads((BASE / "cache/index_000001_sh.pkl").read_bytes())
    return {d: bool(c.get("allows_entries")) for d, c in build_market_gate(index_frame, config).items()}


def restrict(signals: dict, histories: dict, allowed: dict[str, bool]) -> dict:
    """剔除入场日落在闸门关闭日的信号。

    .signals.json 的索引是**全历史绝对 bar 位置**，必须用 history frame 自己的日期解析
    （样本的 dates 只覆盖窗口内，直接用会越界）。
    """
    out = {}
    for symbol, per_strategy in signals.items():
        frame = histories[symbol]
        dates = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d").tolist()
        out[symbol] = {
            strategy: {i for i in indices if i + 1 < len(frame) and i < len(dates) and allowed.get(dates[i], False)}
            for strategy, indices in per_strategy.items()
        }
    return out


def run_case(histories, signals, calendar, meta, start, end, gate_on, strategy, allowed):
    config = deepcopy(meta)
    options = {"start": start, "end": end, "max_positions": 4, "initial_cash": 250000.0,
               "history_bars": 300}
    use_signals = restrict(signals, histories, allowed) if gate_on else signals
    risk = config.setdefault("risk", {})
    risk["stop_loss_pct"] = ARCHIVED_STOP[strategy]
    risk.pop("strategy_stop_loss_pct", None)
    risk.pop("strategy_exit_plan", None)
    plan_key = BEST_PLAN[strategy]
    if plan_key == "none":
        risk.pop("exit_plan", None)
    else:
        risk["exit_plan"] = deepcopy(PLAN_DEFINITIONS[plan_key])
    result = replay(histories, use_signals, calendar, config, options, strategy)
    m = result["metrics"]
    return {
        "plan": plan_key,
        "return_pct": m["total_return_pct"],
        "annualized_pct": m["annualized_return_pct"],
        "max_dd_pct": m["max_drawdown_pct"],
        "sharpe": m["sharpe_ratio"],
        "payoff": m["payoff_ratio"],
        "win_rate_pct": m["win_rate_pct"],
        "trades": m["closed_trades"],
        "open_positions": result["open_positions"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    root = BASE / args.sample
    config = load_config(BASE / "config/config.yaml")
    allowed = gate_map(config)
    histories, signals, calendar, meta = load_sample(root)
    snapshot = meta["config_snapshot"]

    days = [d for d in allowed if args.start <= d <= args.end]
    open_days = sum(1 for d in days if allowed[d])
    print(f"样本 {len(histories)} 只 | 窗口 {args.start}~{args.end} ({len(days)} 交易日)")
    print(f"闸门开放 {open_days}/{len(days)} 天 ({100*open_days/max(len(days),1):.1f}%)\n")

    report = {"window": args.label or f"{args.start}~{args.end}",
              "sample": args.sample, "gate_open_days": open_days, "gate_total_days": len(days),
              "cases": {}}
    print(f"{'策略':<18}{'方案':<10}{'闸门':<6}{'收益':>9}{'年化':>9}{'回撤':>8}{'夏普':>7}{'盈亏比':>8}{'胜率':>8}{'笔数':>6}")
    for strategy in STRATEGIES:
        for gate_on in (False, True):
            res = run_case(histories, signals, calendar, deepcopy(snapshot),
                           args.start, args.end, gate_on, strategy, allowed)
            report["cases"][f"{strategy}|{'gate' if gate_on else 'nogate'}"] = res

            def f(v, spec, suffix=""):
                return "—".rjust(len(spec)) if v is None else format(v, spec) + suffix

            print(f"{strategy:<18}{res['plan']:<10}{'有' if gate_on else '无':<6}"
                  f"{f(res['return_pct'],'9.2f','%')}{f(res['annualized_pct'],'9.2f','%')}"
                  f"{f(res['max_dd_pct'],'8.2f','%')}{f(res['sharpe'],'7.2f')}"
                  f"{f(res['payoff'],'8.2f')}{f(res['win_rate_pct'],'8.1f','%')}{res['trades']:>6}")

    out = BASE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写出 {out}")


if __name__ == "__main__":
    main()
