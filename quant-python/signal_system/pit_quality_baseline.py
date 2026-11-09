"""PIT all-A analog of the quality A/B baseline.

Same selection as the quality baseline (100 smallest -> 30 smallest, monthly,
no quality filter), but the universe is PIT all-A plus available delisted
stocks, instead of the 2026 CSI 1000 snapshot.

This is still not historical CSI 1000 membership.  It tests whether the
baseline's size/selection core survives when the universe is not selected by
2026 membership.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from quality_filter_study import (
    Backtest,
    Config,
    Variant,
    _load_current,
    _load_panels,
    _rebalance_map,
    build_features,
    extend_delisted_panels,
    extend_panels,
    select_targets,
)

# Study artifacts live next to the code (``cache/`` is gitignored); the old
# ``D:/tmp/...`` defaults only resolved on the Windows research machine.
STUDY_DIR = Path(__file__).resolve().parent / "cache" / "smallcap_study"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panels", default=str(STUDY_DIR / "panels_2018_2024.pkl"))
    parser.add_argument("--data-dir", default="cache/baostock_history_20261026")
    parser.add_argument("--extension-dir", default="cache/stock_pit/prices")
    parser.add_argument("--delisted-dir", default="cache/stock_pit/prices_delisted")
    parser.add_argument("--out", default=str(STUDY_DIR / "pit_quality_baseline"))
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--initial", type=float, default=250_000.0)
    args = parser.parse_args()

    cfg = Config(start=args.start, end=args.end, initial=args.initial, pool_size=100, max_names=30)
    panels = _load_panels(Path(args.panels))
    panels = extend_panels(panels, Path(args.extension_dir), list(panels["close"].columns), args.start)
    panels = extend_delisted_panels(panels, Path(args.delisted_dir))
    current_all = list(panels["close"].columns)
    features = build_features(panels, current_all, cfg)
    execution_map = _rebalance_map(features["close"].index, cfg)
    variant = Variant("pit_baseline", "none")
    stats: list[dict[str, Any]] = []

    def target_fn(signal: pd.Timestamp) -> list[str]:
        return select_targets(
            signal, features, pd.DataFrame(), variant, cfg, stats
        )

    engine = Backtest(features, cfg, variant, target_fn)
    metrics = engine.run(execution_map)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "config": asdict(cfg),
        "variant": asdict(variant),
        "data": {
            "symbols": len(current_all),
            "signals": len(execution_map),
            "start": str(features["close"].index.min().date()),
            "end": str(features["close"].index.max().date()),
        },
        "metrics": metrics,
        "selection": stats,
        "limitations": [
            "PIT all-A, not historical CSI 1000 membership.",
            "237/367 delisted stocks have bars; remaining delisted names are missing.",
            "No industry cap (same as quality baseline).",
            "2025-2026 prices are Sina unadjusted, scaled at the 2024 splice.",
        ],
    }
    (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    curve = pd.Series(
        [value for _, value in engine.equity],
        index=pd.DatetimeIndex([date for date, _ in engine.equity]),
    )
    curve.to_frame("equity").to_csv(out / "curves.csv")
    print(metrics, flush=True)
    print("wrote", out, flush=True)


if __name__ == "__main__":
    main()
