"""构建指定区间的研究样本（可中断续跑）。

为什么单独写成文件：`build_cache_sample` 内部用 `multiprocessing.Pool`，
Windows 上是 spawn 模式，会用 `runpy.run_path(__main__)` 重新导入主模块。
若主程序来自 stdin/heredoc，路径是 `<stdin>`，spawn 会 `OSError: Invalid argument`。
故必须落成真实 .py 文件再跑。

用法：
  python research_sample_builder.py 2020-07-01 2020-12-31
  python research_sample_builder.py 2019-07-01 2019-12-31
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

from scale_out_take_profit_study import build_cache_sample  # noqa: E402

ARCHIVE = BASE / "output/backtests/divergence-stop-1to10-250k-2024h1-bd36d220/comparison.json"


def main() -> None:
    start, end = sys.argv[1], sys.argv[2]
    config = json.loads(ARCHIVE.read_text(encoding="utf-8"))["config_snapshot"]
    out = BASE / f"output/backtests/_gate_samples/sample-{start}-{end}"
    if (out / "prepared_metadata.json").exists():
        meta = json.loads((out / "prepared_metadata.json").read_text(encoding="utf-8"))
        print(f"[跳过] {out} 已存在")
    else:
        built = build_cache_sample(config, start, end, workers=6, out_dir=out)
        meta = built["metadata"]
    print(f"窗口 {start}~{end}")
    print(f"  样本: {meta['universe']}")
    print(f"  日历天数: {len(meta['calendar'])}")
    if meta.get("exclusions"):
        for reason, count in Counter(e["reason"] for e in meta["exclusions"]).most_common(5):
            print(f"  剔除 {count:>4}  {reason}")


if __name__ == "__main__":
    main()
