"""Run real-data daily-scan acceptance against fixed watchlist groups.

This script validates the production scan path with real market data while
keeping the stock universe stable. It is designed to catch regressions in:

1. data fetching and provider routing
2. selector pre-filtering
3. technical analysis and strategy routing
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
SIGNAL_SYSTEM_ROOT = ROOT / "signal_system"
for candidate in (str(ROOT), str(SIGNAL_SYSTEM_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from data.data_fetcher import DataFetcher
from strategy.indicators import TechnicalIndicators
from strategy.strategy_engine import StrategyEngine
from utils.helpers import load_config


def _watchlist(codes: Iterable[str]) -> List[Dict[str, str]]:
    return [{"ts_code": code, "name": code} for code in codes]


WATCHLIST_GROUPS: Dict[str, List[Dict[str, str]]] = {
    "curated_40": _watchlist(
        [
            "002028.SZ",
            "002032.SZ",
            "002311.SZ",
            "002422.SZ",
            "002444.SZ",
            "002572.SZ",
            "002595.SZ",
            "002603.SZ",
            "002737.SZ",
            "002867.SZ",
            "300628.SZ",
            "600160.SH",
            "600298.SH",
            "600332.SH",
            "600801.SH",
            "603156.SH",
            "603225.SH",
            "603345.SH",
            "603589.SH",
            "603650.SH",
            "000651.SZ",
            "000333.SZ",
            "600690.SH",
            "601225.SH",
            "601088.SH",
            "600019.SH",
            "600309.SH",
            "600585.SH",
            "600036.SH",
            "000858.SZ",
            "600887.SH",
            "600276.SH",
            "601899.SH",
            "600426.SH",
            "600048.SH",
            "002142.SZ",
            "603288.SH",
            "000786.SZ",
            "002594.SZ",
            "601166.SH",
        ]
    ),
    "expanded_60": _watchlist(
        [
            "002028.SZ",
            "002032.SZ",
            "002311.SZ",
            "002422.SZ",
            "002444.SZ",
            "002572.SZ",
            "002595.SZ",
            "002603.SZ",
            "002737.SZ",
            "002867.SZ",
            "300628.SZ",
            "600160.SH",
            "600298.SH",
            "600332.SH",
            "600801.SH",
            "603156.SH",
            "603225.SH",
            "603345.SH",
            "603589.SH",
            "603650.SH",
            "000651.SZ",
            "000333.SZ",
            "600690.SH",
            "601225.SH",
            "601088.SH",
            "600019.SH",
            "600309.SH",
            "600585.SH",
            "600036.SH",
            "000858.SZ",
            "600887.SH",
            "600276.SH",
            "601899.SH",
            "600426.SH",
            "600048.SH",
            "002142.SZ",
            "603288.SH",
            "000786.SZ",
            "002594.SZ",
            "601166.SH",
            "603129.SH",
            "002353.SZ",
            "600079.SH",
            "600660.SH",
            "000596.SZ",
            "000538.SZ",
            "601233.SH",
            "002415.SZ",
            "002475.SZ",
            "002938.SZ",
            "600132.SH",
            "600809.SH",
            "002001.SZ",
            "603899.SH",
            "600529.SH",
            "603866.SH",
            "601100.SH",
            "603198.SH",
            "600511.SH",
            "000963.SZ",
        ]
    ),
    "quality_midcap_20": _watchlist(
        [
            "002444.SZ",
            "002867.SZ",
            "300628.SZ",
            "603129.SH",
            "002353.SZ",
            "600529.SH",
            "603866.SH",
            "603198.SH",
            "603345.SH",
            "603156.SH",
            "603288.SH",
            "600079.SH",
            "600511.SH",
            "000596.SZ",
            "000963.SZ",
            "002415.SZ",
            "002475.SZ",
            "002001.SZ",
            "600660.SH",
            "000538.SZ",
        ]
    ),
}

DEFAULT_GROUP = "curated_40"


def build_runtime_config(config_path: Path, watchlist: List[Dict[str, str]]) -> Dict:
    """Load production config and pin the acceptance stock universe."""
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"Failed to load config: {config_path}")

    config["manual_overrides"]["watchlist_only"] = [item["ts_code"] for item in watchlist]
    config["notification"]["wechat"]["enabled"] = False
    config["notification"]["email"]["enabled"] = False
    return config


class AcceptanceDataFetcher(DataFetcher):
    """Use the real data layer for symbol-level data with a fixed stock list."""

    def __init__(self, watchlist: List[Dict[str, str]], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.watchlist = watchlist

    def get_stock_list(self, exchange: str = "", list_status: str = "L"):
        del exchange, list_status
        return pd.DataFrame(self.watchlist)


def build_summary(
    scan_result: Dict,
    min_candidates: int,
    min_buy_signals: int,
    group_name: str = DEFAULT_GROUP,
    watchlist_size: int | None = None,
) -> Dict:
    """Build a compact machine-readable acceptance summary."""
    stats = scan_result["stats"]
    candidate_pool = scan_result.get("candidate_pool", [])
    buy_signals = scan_result.get("buy_signals", [])

    summary = {
        "run_time": datetime.now().isoformat(),
        "group_name": group_name,
        "watchlist_size": watchlist_size if watchlist_size is not None else len(WATCHLIST_GROUPS[group_name]),
        "market_status": scan_result["market_status"],
        "stats": stats,
        "candidate_pool": [
            {
                "ts_code": item["ts_code"],
                "score": item["score"],
                "checks": item["passed_checks"],
            }
            for item in candidate_pool
        ],
        "buy_signals": [
            {
                "ts_code": item["ts_code"],
                "signal_type": item.get("signal_type"),
                "score": item.get("score"),
                "reason": item.get("reason"),
            }
            for item in buy_signals
        ],
        "acceptance": {
            "min_candidates": min_candidates,
            "min_buy_signals": min_buy_signals,
            "candidate_pool_ok": stats["candidate_pool_count"] >= min_candidates,
            "buy_signals_ok": stats["buy_signals_count"] >= min_buy_signals,
        },
    }
    summary["acceptance"]["passed"] = (
        summary["acceptance"]["candidate_pool_ok"] and summary["acceptance"]["buy_signals_ok"]
    )
    return summary


def build_multi_group_report(group_summaries: List[Dict]) -> Dict:
    """Aggregate multiple group summaries into one report."""
    passed_groups = [item for item in group_summaries if item["acceptance"]["passed"]]
    aggregate = {
        "run_time": datetime.now().isoformat(),
        "group_count": len(group_summaries),
        "passed_group_count": len(passed_groups),
        "all_passed": len(passed_groups) == len(group_summaries),
        "total_candidate_pool_count": sum(item["stats"]["candidate_pool_count"] for item in group_summaries),
        "total_buy_signals_count": sum(item["stats"]["buy_signals_count"] for item in group_summaries),
    }
    return {
        "aggregate": aggregate,
        "groups": group_summaries,
    }


def run_group(
    config_path: Path,
    group_name: str,
    watchlist: List[Dict[str, str]],
    use_cache: bool,
    min_candidates: int,
    min_buy_signals: int,
) -> Dict:
    """Run one fixed watchlist group through the production daily scan."""
    config = build_runtime_config(config_path=config_path, watchlist=watchlist)
    cache_dir = str(SIGNAL_SYSTEM_ROOT / "cache_acceptance")
    data_fetcher = AcceptanceDataFetcher(
        watchlist=watchlist,
        tushare_token=config["data_source"]["tushare_token"],
        use_cache=use_cache,
        cache_dir=cache_dir,
        config=config,
    )
    strategy_engine = StrategyEngine(
        config=config,
        data_fetcher=data_fetcher,
        technical_indicators=TechnicalIndicators(),
    )

    scan_result = strategy_engine.run_daily_scan(positions=[])
    if scan_result is None:
        raise RuntimeError(f"Daily scan returned None for group {group_name}")

    return build_summary(
        scan_result=scan_result,
        min_candidates=min_candidates,
        min_buy_signals=min_buy_signals,
        group_name=group_name,
        watchlist_size=len(watchlist),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real-data daily-scan acceptance")
    parser.add_argument(
        "--config",
        type=str,
        default=str(SIGNAL_SYSTEM_ROOT / "config" / "config.yaml"),
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--group",
        type=str,
        default=DEFAULT_GROUP,
        choices=["all", *WATCHLIST_GROUPS.keys()],
        help="Acceptance watchlist group to run",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="Optional JSON output path",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable market data cache for this run",
    )
    parser.add_argument(
        "--min-candidates",
        type=int,
        default=1,
        help="Minimum acceptable candidate_pool_count per group",
    )
    parser.add_argument(
        "--min-buy-signals",
        type=int,
        default=1,
        help="Minimum acceptable buy_signals_count per group",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    target_groups = (
        WATCHLIST_GROUPS.items()
        if args.group == "all"
        else [(args.group, WATCHLIST_GROUPS[args.group])]
    )

    summaries = [
        run_group(
            config_path=config_path,
            group_name=group_name,
            watchlist=watchlist,
            use_cache=not args.no_cache,
            min_candidates=args.min_candidates,
            min_buy_signals=args.min_buy_signals,
        )
        for group_name, watchlist in target_groups
    ]

    report = build_multi_group_report(summaries) if len(summaries) > 1 else summaries[0]
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    if len(summaries) > 1:
        return 0 if report["aggregate"]["all_passed"] else 1
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
