"""Cost and specification robustness for the PIT all-A small-cap baseline.

Uses only free/local data:
* 2018-2024 baostock bars (current-listed symbols);
* 2025-2026 Sina bars;
* 237 delisted Eastmoney bars.

This is a stress matrix, not a parameter search.  Cases are fixed in advance:
5/10/20 bp slippage, two adjacent size bands, industry cap on/off, pool 50/150,
and static 30 vs dynamic 3-6 names.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from quality_filter_study import extend_delisted_panels, extend_panels
from smallcap_strategy_study import (
    Backtest,
    Config,
    Variant,
    build_features,
    load_industry,
    load_panels,
)

# Study artifacts live next to the code (``cache/`` is gitignored); the old
# ``D:/tmp/...`` defaults only resolved on the Windows research machine.
STUDY_DIR = Path(__file__).resolve().parent / "cache" / "smallcap_study"


def case(
    name: str,
    *,
    rank_low: int = 801,
    rank_high: int = 1800,
    slippage: float = 0.0005,
    pool_size: int = 50,
    dynamic: bool = False,
    industry_cap: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "rank_low": rank_low,
        "rank_high": rank_high,
        "slippage": slippage,
        "pool_size": pool_size,
        "dynamic": dynamic,
        "industry_cap": industry_cap,
    }


CASES = [
    case("base_5bp"),
    case("slip_10bp", slippage=0.0010),
    case("slip_20bp", slippage=0.0020),
    case("rank_501_1500", rank_low=501, rank_high=1500),
    case("rank_1000_2000", rank_low=1000, rank_high=2000),
    case("no_industry_cap", industry_cap=False),
    case("pool_150", pool_size=150),
    case("dynamic_3_6", dynamic=True),
]


def run_case(
    spec: dict[str, Any],
    features: dict[str, Any],
    industry: dict[str, str],
    industry_unique: dict[str, str],
    base_cfg: Config,
) -> dict[str, Any]:
    cfg = replace(
        base_cfg,
        rank_low=spec["rank_low"],
        rank_high=spec["rank_high"],
        slippage=spec["slippage"],
    )
    variant = Variant(
        name=spec["name"],
        dynamic_names=spec["dynamic"],
        use_stops=False,
        use_macd=False,
        use_micro=False,
        use_volume=False,
        static_names=30,
        pool_size=spec["pool_size"],
    )
    sectors = industry if spec["industry_cap"] else industry_unique
    engine = Backtest(features, sectors, cfg, variant)
    metrics = engine.run()
    return {
        "case": spec,
        "metrics": metrics,
        "selection": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="cache/baostock_history_20261026")
    parser.add_argument("--panels", default=str(STUDY_DIR / "panels_2018_2024.pkl"))
    parser.add_argument("--index-file", default=str(STUDY_DIR / "index_399101.csv"))
    parser.add_argument("--industry-file", default=str(STUDY_DIR / "sina_all.jsonl"))
    parser.add_argument("--extension-dir", default="cache/stock_pit/prices")
    parser.add_argument("--delisted-dir", default="cache/stock_pit/prices_delisted")
    parser.add_argument("--out", default=str(STUDY_DIR / "robustness"))
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--initial", type=float, default=250_000.0)
    args = parser.parse_args()

    cfg = Config(start=args.start, end=args.end, initial=args.initial)
    panels = load_panels(Path(args.data_dir), Path(args.panels), cfg)
    panels = extend_panels(panels, Path(args.extension_dir), list(panels["close"].columns), args.start)
    panels = extend_delisted_panels(panels, Path(args.delisted_dir))
    index = pd.read_csv(args.index_file, parse_dates=["date"]).set_index("date")["close"]
    industry = load_industry(Path(args.industry_file))
    delisted_path = Path("cache/stock_pit/universe/delisted.csv")
    if delisted_path.exists():
        delisted = pd.read_csv(delisted_path, dtype={"symbol": str})
        for code in delisted["symbol"]:
            industry.setdefault(str(code).zfill(6), f"__delisted_{code}")
    all_symbols = list(panels["close"].columns)
    industry_unique = {symbol: f"__{symbol}" for symbol in all_symbols}
    features = build_features(panels, index, cfg)
    print("features", features["close"].shape, flush=True)

    results = []
    for spec in CASES:
        result = run_case(spec, features, industry, industry_unique, cfg)
        results.append(result)
        print(spec["name"], result["metrics"], flush=True)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(
            {"config": asdict(cfg), "cases": results},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    rows = []
    for result in results:
        row = {"case": result["case"]["name"], **result["metrics"]}
        row.update({f"spec_{k}": v for k, v in result["case"].items() if k != "name"})
        rows.append(row)
    pd.DataFrame(rows).to_csv(out / "cases.csv", index=False, encoding="utf-8-sig")
    print("wrote", out)


if __name__ == "__main__":
    main()
