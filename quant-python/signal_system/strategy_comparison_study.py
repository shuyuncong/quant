"""买入×卖出 策略对照: 同一份数据、同一套执行, 只变买入信号或卖出规则。

回答两个问题:
1. 三条买入信号 (日线零轴金叉 / 年线趋势 / 零轴+底背离) 谁更强?
2. 两套卖出规则 (fixed 8%/30%/40根 vs v1b 顶背离+破年线+40根) 谁更强?

口径固定, 不引入任何新变量:
- 数据: cache/daily_history/*_qfq.pkl (前复权), 2023-05-01 → 2026-10-04
- 执行: backtest_winrate 生产引擎 (T+1 / 涨跌停 / 手续费 / 印花税)
- 组合: 10 万本金, 4 个仓位, 单笔 25%; 排序敏感性用 4 种 tie-break 报区间
- 无闸门: 与既往研究保持一致, 闸门影响留作后续

指标分两层 (混在一张表里会误导):
- 单笔层: 胜率 / 盈亏比(平均盈利÷平均亏损) / 盈亏因子(PF) / 单笔均值
- 组合层: 年化收益 / 最大回撤 / 夏普 (来自日频权益曲线, 无风险利率取 0)

每条臂单独起一个进程 (6 条臂串行会超过一小时), 中间结果留在
strategy_comparison_arms/, 可断点续跑。

用法:
    python strategy_comparison_study.py --jobs 6 --reuse
    python strategy_comparison_study.py --out strategy_comparison.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
BACKTEST = BASE_DIR / "backtest_macd_divergence.py"

# 买入×卖出 交叉臂。买入信号取自 backtest_macd_divergence.CROSS_SIGNALS:
#   baseline = 日线零轴金叉 + 回落确认 (当前唯一实盘推送的买入信号)
#   yearline = 年线趋势 (A 放量突破 + B 回踩收复)
#   diverge  = 零轴 + 底背离 四条件 AND
# 卖出规则两套:
#   fixed = 现行生产规则 (-8% 止损 / +30% 止盈 / 40 根超时)
#   v1b   = divergence_trend (-8% 止损 / 日线顶背离 / 跌破年线 / 40 根超时)
BUY_ARMS = ("baseline", "yearline", "diverge")
EXIT_ARMS = ("fixed", "v1b")
TIE_BREAKS = ("symbol_asc", "symbol_desc", "hash", "rotate")

METRIC_KEYS = (
    "win_rate",
    "avg_pnl_pct",
    "median_pnl_pct",
    "avg_win_pct",
    "avg_loss_pct",
    "avg_win_loss_ratio",
    "profit_factor",
    "avg_holding_days",
)


def _arm_name(buy: str, exit_token: str) -> str:
    return f"{buy}__{exit_token}"


def _arm_file(arm_dir: Path, buy: str, exit_token: str) -> Path:
    return arm_dir / f"{_arm_name(buy, exit_token)}.json"


def _run_arm(
    buy: str,
    exit_token: str,
    arm_dir: Path,
    start: str,
    end: str,
    reuse: bool,
    symbols: str | None = None,
    limit: int = 0,
) -> tuple[str, int]:
    """One arm per process: 6 arms serially exceed an hour of wall clock."""
    out = _arm_file(arm_dir, buy, exit_token)
    if reuse and out.exists():
        return _arm_name(buy, exit_token), 0
    log = out.with_suffix(".log")
    command = [
        sys.executable,
        str(BACKTEST),
        "--arms",
        f"{buy} x {exit_token}",
        "--start",
        start,
        "--end",
        end,
        "--tie-breaks",
        ",".join(TIE_BREAKS),
        "--out",
        str(out),
    ]
    if symbols:
        command.extend(["--symbols", symbols])
    if limit:
        command.extend(["--limit", str(limit)])
    with log.open("w", encoding="utf-8") as handle:
        code = subprocess.run(
            command, cwd=str(BASE_DIR), stdout=handle, stderr=subprocess.STDOUT
        ).returncode
    return _arm_name(buy, exit_token), code


def _metrics_for(arm: dict[str, Any]) -> dict[str, Any]:
    """Pull the two metric layers out of one arm, keeping tie-break dispersion."""
    single = dict(arm.get("single_trade_summary") or {})
    portfolio: list[dict[str, Any]] = []
    for tie_break, variant in sorted((arm.get("portfolio_variants") or {}).items()):
        summary = variant.get("summary") or {}
        portfolio.append(
            {
                "tie_break": tie_break,
                "total_return_pct": summary.get("total_return_pct"),
                "annualized_return_pct": summary.get("annualized_return_pct"),
                "max_drawdown_pct": summary.get("max_drawdown_pct"),
                "sharpe_ratio": summary.get("sharpe_ratio"),
                "span_days": summary.get("span_days"),
                "accepted": summary.get("accepted"),
                "candidates": summary.get("candidates"),
                "average_gross_exposure_pct": summary.get("average_gross_exposure_pct"),
            }
        )
    return {
        "buy_token": arm.get("buy_token"),
        "exit_token": arm.get("exit_token"),
        "signal_count": (arm.get("stats") or {}).get("signal_count"),
        "trade_count": arm.get("trade_count"),
        "single_trade": {key: single.get(key) for key in METRIC_KEYS},
        "exit_reasons": arm.get("exit_reasons"),
        "portfolio": portfolio,
    }


def _dispersion(values: list[Any]) -> dict[str, Any]:
    """Min / median / max across tie-breaks — a point estimate would overstate."""
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return {"min": None, "median": None, "max": None}
    middle = len(clean) // 2
    median = (
        clean[middle] if len(clean) % 2 else (clean[middle - 1] + clean[middle]) / 2.0
    )
    return {"min": round(clean[0], 2), "median": round(median, 2), "max": round(clean[-1], 2)}


def load_arms(arm_dir: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"arms": {}, "window": None, "universe": None, "execution": None}
    for buy in BUY_ARMS:
        for exit_token in EXIT_ARMS:
            path = _arm_file(arm_dir, buy, exit_token)
            if not path.exists():
                raise SystemExit(f"missing arm file: {path} (run without --reuse first)")
            payload = json.loads(path.read_text(encoding="utf-8"))
            name = _arm_name(buy, exit_token)
            if not report["arms"]:
                report["window"] = payload.get("window")
                report["universe"] = payload.get("universe")
                report["execution"] = payload.get("execution")
            arms = payload.get("arms") or {}
            if name not in arms:
                raise SystemExit(f"arm {name} missing inside {path}")
            report["arms"][name] = _metrics_for(arms[name])
    return report


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, payload in report["arms"].items():
        portfolio = payload["portfolio"]
        summary[name] = {
            "trades": payload["trade_count"],
            "signals": payload["signal_count"],
            "win_rate": payload["single_trade"]["win_rate"],
            "payoff_ratio": payload["single_trade"]["avg_win_loss_ratio"],
            "profit_factor": payload["single_trade"]["profit_factor"],
            "avg_pnl_pct": payload["single_trade"]["avg_pnl_pct"],
            "annualized_return_pct": _dispersion(
                [item["annualized_return_pct"] for item in portfolio]
            ),
            "max_drawdown_pct": _dispersion(
                [item["max_drawdown_pct"] for item in portfolio]
            ),
            "sharpe_ratio": _dispersion([item["sharpe_ratio"] for item in portfolio]),
            "total_return_pct": _dispersion(
                [item["total_return_pct"] for item in portfolio]
            ),
            "span_days": sorted(
                {item["span_days"] for item in portfolio if item["span_days"]}
            ),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2023-05-01")
    parser.add_argument("--end", default="2026-10-04")
    parser.add_argument("--jobs", type=int, default=6, help="并行进程数 (8 核机器上 6 合适)")
    parser.add_argument("--reuse", action="store_true", help="复用已存在的单臂结果")
    parser.add_argument("--skip-run", action="store_true", help="只合并, 不跑回测")
    parser.add_argument("--arm-dir", default=str(BASE_DIR / "strategy_comparison_arms"))
    parser.add_argument("--symbols", default=None, help="逗号分隔代码; 用于小样本冒烟或样本外切分")
    parser.add_argument("--limit", type=int, default=0, help="只取前 N 个标的; 用于冒烟")
    parser.add_argument("--out", default=str(BASE_DIR / "strategy_comparison.json"))
    args = parser.parse_args()

    arm_dir = Path(args.arm_dir)
    arm_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_run:
        jobs = max(1, min(args.jobs, len(BUY_ARMS) * len(EXIT_ARMS)))
        combos = [(buy, token) for buy in BUY_ARMS for token in EXIT_ARMS]
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [
                pool.submit(
                    _run_arm,
                    buy,
                    token,
                    arm_dir,
                    args.start,
                    args.end,
                    args.reuse,
                    args.symbols,
                    args.limit,
                )
                for buy, token in combos
            ]
            for future in futures:
                name, code = future.result()
                status = "ok" if code == 0 else f"FAILED (exit {code})"
                print(f"arm {name}: {status}", flush=True)
    report = load_arms(arm_dir)
    report["tie_breaks"] = list(TIE_BREAKS)
    report["summary"] = summarize(report)
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, default=str))
    print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
