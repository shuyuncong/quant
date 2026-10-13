"""修复 backfill_history_cache 早期版本造成的 attrs 丢失。

背景：`pd.concat` / `reset_index` 会丢弃 `DataFrame.attrs`。回填脚本最初版本
在合并后直接 `to_pickle`，导致所有被回填的 `*_qfq.pkl` 丢掉了
`attrs["adjust"] == "qfq"`。下游审计（`atr_volatility_audit._validate_history_frame`、
`canonical_replay_semantics_audit` 等）用该 attrs 判定前复权口径，于是把整批缓存
判为"非 qfq"并拒绝 —— 表现为 `history safety gate failed: input_failures=86`。

修复方式：attrs 里唯一不可从文件自身推断的是 `source`，其余（timeframe/adjust/
timezone/timestamp_semantics）对 qfq 缓存是固定值。`source` 从同标的的
`*_none.pkl` 继承（同一目录、同一抓取批次），缺失时回退 "tencent"。

不改变任何价格数据，只补元数据。

用法：
  python fix_qfq_attrs.py            # 仅检查并报告
  python fix_qfq_attrs.py --apply    # 实际写回
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache" / "daily_history"

FIXED = {
    "timeframe": "1d",
    "adjust": "qfq",
    "timezone": "Asia/Shanghai",
    "timestamp_semantics": "end",
}


def repair_one(path: Path, apply: bool) -> str:
    frame = pd.read_pickle(path)
    if frame.attrs.get("adjust") == "qfq":
        return "ok"
    sibling = path.with_name(path.name.replace("_qfq.pkl", "_none.pkl"))
    source = "tencent"
    if sibling.exists():
        try:
            source = pd.read_pickle(sibling).attrs.get("source", source)
        except Exception:  # noqa: BLE001 - 兄弟文件损坏不应中断整轮
            pass
    if not apply:
        return "would-fix"
    frame.attrs = {"source": source, **FIXED}
    frame.to_pickle(path)
    return "fixed"


def main() -> None:
    parser = argparse.ArgumentParser(description="补回 *_qfq.pkl 丢失的 attrs")
    parser.add_argument("--apply", action="store_true", help="实际写回（默认仅报告）")
    args = parser.parse_args()

    counts: dict[str, int] = {}
    for path in sorted(CACHE.glob("*_qfq.pkl")):
        if path.name.startswith("_"):
            continue
        try:
            outcome = repair_one(path, args.apply)
        except Exception as error:  # noqa: BLE001
            outcome = f"error:{type(error).__name__}"
        counts[outcome] = counts.get(outcome, 0) + 1

    for key in sorted(counts):
        print(f"{key:>12}: {counts[key]}")
    if not args.apply and counts.get("would-fix"):
        print("\n加 --apply 实际写回")


if __name__ == "__main__":
    main()
