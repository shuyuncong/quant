"""Run a real-data daily-scan acceptance check against a fixed stock sample.

The goal of this script is not to search the whole market. It validates that
the current production path:

1. loads a real stock list
2. builds selector inputs from real market data
3. produces a non-empty candidate pool and buy signal set on a curated sample

This gives us a stable regression guard for data-source, selector and routing
changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

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


# The sample intentionally mixes industries and styles. It is small enough to
# run quickly, but rich enough to catch regressions in the selector pipeline.
DEFAULT_WATCHLIST = [
    {"ts_code": "002028.SZ", "name": "思源电气"},
    {"ts_code": "002032.SZ", "name": "苏泊尔"},
    {"ts_code": "002311.SZ", "name": "海大集团"},
    {"ts_code": "002422.SZ", "name": "科伦药业"},
    {"ts_code": "002444.SZ", "name": "巨星科技"},
    {"ts_code": "002572.SZ", "name": "索菲亚"},
    {"ts_code": "002595.SZ", "name": "豪迈科技"},
    {"ts_code": "002603.SZ", "name": "以岭药业"},
    {"ts_code": "002737.SZ", "name": "葵花药业"},
    {"ts_code": "002867.SZ", "name": "周大生"},
    {"ts_code": "300628.SZ", "name": "亿联网络"},
    {"ts_code": "600160.SH", "name": "巨化股份"},
    {"ts_code": "600298.SH", "name": "安琪酵母"},
    {"ts_code": "600332.SH", "name": "白云山"},
    {"ts_code": "600801.SH", "name": "华新建材"},
    {"ts_code": "603156.SH", "name": "养元饮品"},
    {"ts_code": "603225.SH", "name": "新凤鸣"},
    {"ts_code": "603345.SH", "name": "安井食品"},
    {"ts_code": "603589.SH", "name": "口子窖"},
    {"ts_code": "603650.SH", "name": "彤程新材"},
    {"ts_code": "000651.SZ", "name": "格力电器"},
    {"ts_code": "000333.SZ", "name": "美的集团"},
    {"ts_code": "600690.SH", "name": "海尔智家"},
    {"ts_code": "601225.SH", "name": "陕西煤业"},
    {"ts_code": "601088.SH", "name": "中国神华"},
    {"ts_code": "600019.SH", "name": "宝钢股份"},
    {"ts_code": "600309.SH", "name": "万华化学"},
    {"ts_code": "600585.SH", "name": "海螺水泥"},
    {"ts_code": "600036.SH", "name": "招商银行"},
    {"ts_code": "000858.SZ", "name": "五粮液"},
    {"ts_code": "600887.SH", "name": "伊利股份"},
    {"ts_code": "600276.SH", "name": "恒瑞医药"},
    {"ts_code": "601899.SH", "name": "紫金矿业"},
    {"ts_code": "600426.SH", "name": "华鲁恒升"},
    {"ts_code": "600048.SH", "name": "保利发展"},
    {"ts_code": "002142.SZ", "name": "宁波银行"},
    {"ts_code": "603288.SH", "name": "海天味业"},
    {"ts_code": "000786.SZ", "name": "北新建材"},
    {"ts_code": "002594.SZ", "name": "比亚迪"},
    {"ts_code": "601166.SH", "name": "兴业银行"},
]


def build_runtime_config(config_path: Path) -> Dict:
    """Load the production config and pin the acceptance watchlist."""
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"Failed to load config: {config_path}")

    # Acceptance should validate the current defaults, not a one-off relaxed
    # config. Only the scan universe is constrained.
    config["manual_overrides"]["watchlist_only"] = [item["ts_code"] for item in DEFAULT_WATCHLIST]
    config["notification"]["wechat"]["enabled"] = False
    config["notification"]["email"]["enabled"] = False
    return config


class AcceptanceDataFetcher(DataFetcher):
    """Use the real data layer for prices/fundamentals, but pin the stock list.

    The dynamic AKShare stock-list endpoint can be blocked by certificate issues
    on this machine. The acceptance flow does not need a full-market stock list,
    so we inject a stable curated watchlist here and still use the real provider
    for all per-symbol data.
    """

    def get_stock_list(self, exchange: str = "", list_status: str = "L"):
        del exchange, list_status
        return pd.DataFrame(DEFAULT_WATCHLIST)


def build_summary(scan_result: Dict, min_candidates: int, min_buy_signals: int) -> Dict:
    """Build a compact machine-readable acceptance summary."""
    stats = scan_result["stats"]
    candidate_pool = scan_result.get("candidate_pool", [])
    buy_signals = scan_result.get("buy_signals", [])

    summary = {
        "run_time": datetime.now().isoformat(),
        "watchlist_size": len(DEFAULT_WATCHLIST),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real-data daily-scan acceptance")
    parser.add_argument(
        "--config",
        type=str,
        default=str(SIGNAL_SYSTEM_ROOT / "config" / "config.yaml"),
        help="Path to config.yaml",
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
        help="Minimum acceptable candidate_pool_count",
    )
    parser.add_argument(
        "--min-buy-signals",
        type=int,
        default=1,
        help="Minimum acceptable buy_signals_count",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = build_runtime_config(config_path)
    cache_dir = str(SIGNAL_SYSTEM_ROOT / "cache_acceptance")
    # Acceptance uses a fixed real-data watchlist to avoid depending on the
    # flaky public stock-list endpoint during CI-like validation. All
    # per-symbol quotes, fundamentals and indicators still come from the real
    # configured data providers.
    data_fetcher = AcceptanceDataFetcher(
        tushare_token=config["data_source"]["tushare_token"],
        use_cache=not args.no_cache,
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
        print("Acceptance failed: run_daily_scan returned None")
        return 1

    summary = build_summary(
        scan_result=scan_result,
        min_candidates=args.min_candidates,
        min_buy_signals=args.min_buy_signals,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    return 0 if summary["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
