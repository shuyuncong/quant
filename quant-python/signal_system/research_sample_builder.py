"""Build an offline research sample from an explicitly selected QFQ cache.

Use a real entrypoint for Windows multiprocessing (spawn cannot import stdin).
Existing callers can keep the two positional dates; new studies can choose an
independent --cache-dir and --out-dir without touching frozen/production data.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

from scale_out_take_profit_study import build_cache_sample  # noqa: E402
from utils.helpers import load_config  # noqa: E402

ARCHIVE = BASE / "output/backtests/divergence-stop-1to10-250k-2024h1-bd36d220/comparison.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="从本地前复权缓存冻结三策略研究样本，不联网")
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--cache-dir", type=Path, default=BASE / "cache/daily_history")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--config", type=Path, help="显式 YAML 配置；不指定则沿用既有 H1 研究归档快照")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    start, end = args.start, args.end
    if start > end or args.workers < 1:
        parser.error("起点不能晚于终点，workers 至少为 1")
    config = (load_config(args.config) if args.config else
              json.loads(ARCHIVE.read_text(encoding="utf-8"))["config_snapshot"])
    out = args.out_dir or BASE / f"output/backtests/_gate_samples/sample-{start}-{end}"
    built = build_cache_sample(config, start, end, workers=args.workers, out_dir=out,
                               cache_dir=args.cache_dir)
    meta = built["metadata"]
    print(f"窗口 {start}~{end}")
    print(f"  样本: {meta['universe']}")
    print(f"  日历天数: {len(meta['calendar'])}")
    if meta.get("exclusions"):
        for reason, count in Counter(e["reason"] for e in meta["exclusions"]).most_common(5):
            print(f"  剔除 {count:>4}  {reason}")


if __name__ == "__main__":
    main()
