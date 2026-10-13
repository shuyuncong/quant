"""量化交易成本对闸门对照结论的影响。

关键：成本来自 `config["backtest"]`（不是 `config["execution"]`），
经 `_resolve_execution_config` 读取。第一次实验改错了键，故零变化。

用法：python market_gate_cost_impact.py <sample_dir> <start> <end> <label>
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

from market_gate_comparison_study import (  # noqa: E402
    ARCHIVED_STOP, BEST_PLAN, PLAN_DEFINITIONS, STRATEGIES,
    gate_map, load_sample, restrict,
)
from research_backtest import replay  # noqa: E402
from utils.helpers import load_config  # noqa: E402

ARCHIVE = BASE / "output/backtests/divergence-stop-1to10-250k-2024h1-bd36d220/comparison.json"


def run(histories, signals, calendar, base_config, start, end, strategy, zero_cost):
    config = deepcopy(base_config)
    risk = config.setdefault("risk", {})
    risk["stop_loss_pct"] = ARCHIVED_STOP[strategy]
    risk.pop("strategy_stop_loss_pct", None)
    risk.pop("strategy_exit_plan", None)
    plan = BEST_PLAN[strategy]
    if plan == "none":
        risk.pop("exit_plan", None)
    else:
        risk["exit_plan"] = deepcopy(PLAN_DEFINITIONS[plan])
    if zero_cost:
        config.setdefault("backtest", {}).update(
            commission_pct=0.0, minimum_commission=0.0, slippage_pct=0.0, stamp_tax_pct=0.0)
    result = replay(histories, signals, calendar, config,
                    {"start": start, "end": end, "max_positions": 4,
                     "initial_cash": 250000.0, "history_bars": 300}, strategy)
    return result["metrics"]


def main() -> None:
    sample, start, end, label = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    base = json.loads(ARCHIVE.read_text(encoding="utf-8"))["config_snapshot"]
    histories, signals, calendar, _ = load_sample(BASE / sample)
    allowed = gate_map(load_config(BASE / "config/config.yaml"))

    print(f"{label}: {len(histories)} 只")
    print(f"{'策略':<18}{'闸门':<5}{'笔数':>5}{'计成本':>10}{'零成本':>10}{'成本拖累':>10}")
    rows = {}
    for strategy in STRATEGIES:
        for gate_on in (False, True):
            use = restrict(signals, histories, allowed) if gate_on else signals
            with_cost = run(histories, use, calendar, base, start, end, strategy, False)
            no_cost = run(histories, use, calendar, base, start, end, strategy, True)
            drag = no_cost["total_return_pct"] - with_cost["total_return_pct"]
            rows[f"{strategy}|{'gate' if gate_on else 'nogate'}"] = {
                "with_cost": with_cost["total_return_pct"],
                "no_cost": no_cost["total_return_pct"],
                "drag_pp": drag, "trades": with_cost["closed_trades"]}
            print(f"{strategy:<18}{'有' if gate_on else '无':<5}{with_cost['closed_trades']:>5}"
                  f"{with_cost['total_return_pct']:>9.2f}%{no_cost['total_return_pct']:>9.2f}%{drag:>+9.2f}pp")
    out = BASE / f"output/backtests/_gate_extended/cost_{label}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写出 {out}")


if __name__ == "__main__":
    main()
