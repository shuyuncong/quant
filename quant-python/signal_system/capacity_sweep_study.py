"""两条主策略的资金容量对比: 零轴+底背离 vs 年线趋势。

背景: 在 4 个仓位的默认组合下, 年线趋势给出 18033 个候选却只成交 89~105 笔,
每个成交对应约 180 个被丢弃的信号。因此"年线趋势年化只有 2.31%"这个数字
衡量的是**抽样的运气**, 不是策略表现。同理零轴+底背离(559 个候选)看起来好得多,
也可能只是因为它被丢弃得少。

本实验把卖出规则固定为 fixed, 只扫仓位上限:
    4 / 10 / 20 / 50 / 100 / 无限
仓位越多, 越接近"每个信号都下注"的真实表现。同时输出:
- 占用率 (平均同时持仓 / 仓位上限): 区分"持仓少因为没信号"还是"因为没资金"
- 全信号等权组合 (上界): 组合层再好也不会超过它
- 入场抽样 5 份 (step=5, offset=0..4): 同一策略换一批成交, 看差异有多大

这样能回答真正的资金配置问题:
    "如果资金有限, 应该把仓位留给谁?" — 答案是**等资金边际收益**最高的那条。

用法:
    python capacity_sweep_study.py --jobs 4 --reuse
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

# 买入信号: 只对比两条主策略, 卖出规则固定 fixed (生产现状)。
BUYS = ("yearline", "diverge")
SLOTS = (4, 10, 20, 50, 100)
SAMPLES = 5
# 单笔金额固定 2.5 万, 总本金 = 2.5 万 × 仓位档位; 使各档位可比较, 且高仓位
# 档位不会因 "100 股一手买不起" 被挡掉 (那会被误读成容量不足)。
CASH_PER_SLOT = 25_000
TIE_BREAKS = ("symbol_asc", "symbol_desc", "hash", "rotate")
SLOT_JOBS = ",".join(str(item) for item in SLOTS)


def _subsample_file(arm_dir: Path, buy: str, offset: int) -> Path:
    return arm_dir / f"{buy}__sample{offset}.json"


def _slots_file(arm_dir: Path, buy: str) -> Path:
    return arm_dir / f"{buy}__slots.json"


def _run(command: list[str], out: Path) -> int:
    log = out.with_suffix(".log")
    with log.open("w", encoding="utf-8") as handle:
        return subprocess.run(
            command, cwd=str(BASE_DIR), stdout=handle, stderr=subprocess.STDOUT
        ).returncode


def _arm_command(buy: str, start: str, end: str, out: Path, extra: list[str]) -> list[str]:
    return [
        sys.executable,
        str(BACKTEST),
        "--arms",
        f"{buy} x fixed",
        "--start",
        start,
        "--end",
        end,
        "--tie-breaks",
        ",".join(TIE_BREAKS),
        "--out",
        str(out),
        *extra,
    ]


def _slots_job(buy: str, arm_dir: Path, start: str, end: str, reuse: bool) -> tuple[str, int]:
    """一个进程里扫完全部仓位档位: 逐笔只算一次, 组合层反复跑很便宜。"""
    out = _slots_file(arm_dir, buy)
    if reuse and out.exists():
        return out.stem, 0
    return out.stem, _run(
        _arm_command(
            buy, start, end, out,
            ["--slots-list", SLOT_JOBS, "--cash-per-slot", str(CASH_PER_SLOT)],
        ),
        out
    )


def _sample_job(buy: str, offset: int, arm_dir: Path, start: str, end: str, reuse: bool) -> tuple[str, int]:
    out = _subsample_file(arm_dir, buy, offset)
    if reuse and out.exists():
        return out.stem, 0
    # step=SAMPLES 让候选降到 1/5, 组合层不再被仓位瓶颈卡死;
    # 换 offset 就等于"换一批成交", 差异直接暴露抽样运气。
    return out.stem, _run(
        _arm_command(
            buy,
            start,
            end,
            out,
            [
                "--entry-step", str(SAMPLES), "--entry-offset", str(offset),
                "--slots-list", SLOT_JOBS, "--cash-per-slot", str(CASH_PER_SLOT),
            ],
        ),
        out,
    )


def _collect(path: Path, buy: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for name, arm in payload.get("arms", {}).items():
        if name.startswith(buy):
            return arm
    return None


def _portfolio_rows(arm: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, variant in sorted(arm.get("portfolio_variants", {}).items()):
        summary = variant["summary"]
        rows.append(
            {
                "variant": key,
                "total_return_pct": summary.get("total_return_pct"),
                "annualized_return_pct": summary.get("annualized_return_pct"),
                "max_drawdown_pct": summary.get("max_drawdown_pct"),
                "sharpe_ratio": summary.get("sharpe_ratio"),
                "accepted": summary.get("accepted"),
                "candidates": summary.get("candidates"),
                "average_positions": summary.get("average_positions"),
                "occupancy_pct": summary.get("occupancy_pct"),
                "span_days": summary.get("span_days"),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2023-05-01")
    parser.add_argument("--end", default="2026-10-04")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--skip-run", action="store_true")
    parser.add_argument("--arm-dir", default=str(BASE_DIR / "capacity_sweep_arms"))
    parser.add_argument("--out", default=str(BASE_DIR / "capacity_sweep.json"))
    args = parser.parse_args()

    arm_dir = Path(args.arm_dir)
    arm_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_run:
        jobs: list[tuple[str, Any]] = []
        for buy in BUYS:
            jobs.append(("slots", buy))
            for offset in range(SAMPLES):
                jobs.append(("sample", (buy, offset)))
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = []
            for kind, payload in jobs:
                if kind == "slots":
                    futures.append(
                        pool.submit(
                            _slots_job, payload, arm_dir, args.start, args.end, args.reuse
                        )
                    )
                else:
                    buy, offset = payload
                    futures.append(
                        pool.submit(
                            _sample_job, buy, offset, arm_dir, args.start, args.end, args.reuse
                        )
                    )
            for future in futures:
                name, code = future.result()
                print(f"{name}: {'ok' if code == 0 else f'FAILED ({code})'}", flush=True)

    report: dict[str, Any] = {"slots": list(SLOTS), "samples": SAMPLES, "arms": {}}
    for buy in BUYS:
        arm_report: dict[str, Any] = {"slots": {}, "samples": {}}
        arm = _collect(_slots_file(arm_dir, buy), buy)
        if arm is not None:
            arm_report["slots"] = {
                "portfolio": _portfolio_rows(arm),
                "single_trade": {
                    key: (arm.get("single_trade_summary") or {}).get(key)
                    for key in ("count", "win_rate", "avg_pnl_pct", "profit_factor")
                },
            }
        for offset in range(SAMPLES):
            arm = _collect(_subsample_file(arm_dir, buy, offset), buy)
            if arm is None:
                continue
            arm_report["samples"][str(offset)] = {
                "portfolio": _portfolio_rows(arm),
                "single_trade": {
                    key: (arm.get("single_trade_summary") or {}).get(key)
                    for key in ("count", "win_rate", "avg_pnl_pct", "profit_factor")
                },
            }
        report["arms"][buy] = arm_report
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _print_summary(report)
    print(f"\nwritten: {args.out}")
    return 0


def _median(values: list[float]) -> float | None:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    middle = len(clean) // 2
    return (
        clean[middle]
        if len(clean) % 2
        else (clean[middle - 1] + clean[middle]) / 2.0
    )


def _print_summary(report: dict[str, Any]) -> None:
    for buy, payload in report["arms"].items():
        print(f"\n{'=' * 84}\n{buy}\n{'=' * 84}")
        block = payload.get("slots") or {}
        rows = block.get("portfolio") or []
        single = block.get("single_trade") or {}
        print(f"  single-trade: count={single.get('count')} win={single.get('win_rate')} "
              f"avg={single.get('avg_pnl_pct')} PF={single.get('profit_factor')}")
        print(f"\n{'level':>7} {'acc/cand':>12} {'occ%':>7} {'ann%':>8} {'dd%':>7} {'sharpe':>7} {'total%':>8}")
        for row in rows:
            level = row["variant"].split(":")[0].replace("slots", "")
            print(f"{level:>7} {str(row['accepted']) + '/' + str(row['candidates']):>12} "
                  f"{row['occupancy_pct'] if row['occupancy_pct'] is None else round(row['occupancy_pct'], 1):>7} "
                  f"{row['annualized_return_pct']:>8} {row['max_drawdown_pct']:>7} "
                  f"{row['sharpe_ratio']:>7} {row['total_return_pct']:>8}")
        print(f"\n  entry sampling (每份 1/{report['samples']} 候选, 换批 = 换运气):")
        for offset, sample in sorted(payload.get("samples", {}).items()):
            sample_rows = sample["portfolio"] or []
            sample_single = sample["single_trade"] or {}
            count = sample_single.get("count")
            if not count:
                print(f"    sample{offset}: 样本为空 (该 offset 下没有成交)")
                continue
            # 只取 4 仓位那一档, 与生产配置对齐。
            four = next(
                (item for item in sample_rows if item["variant"].startswith("slots4:")),
                None,
            )
            ann = four["annualized_return_pct"] if four else None
            dd = four["max_drawdown_pct"] if four else None
            print(f"    sample{offset}: trades={count:>5} "
                  f"avg={sample_single.get('avg_pnl_pct'):>6} PF={sample_single.get('profit_factor'):>5} "
                  f"| 4槽 ann={ann if ann is not None else 'n/a':>7} "
                  f"dd={dd if dd is not None else 'n/a':>6}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
