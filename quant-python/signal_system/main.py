"""CLI for A-share multi-timeframe Chan/MACD monitoring."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from models import RISK_NOTICE, SignalEvent  # noqa: E402
from monitor.service import SignalMonitor  # noqa: E402
from utils.helpers import load_config, setup_logging  # noqa: E402
from utils.time_utils import now_shanghai  # noqa: E402


logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A股缠论多周期信号监控")
    parser.add_argument(
        "--config",
        default=os.path.join(BASE_DIR, "config", "config.yaml"),
        help="配置文件路径",
    )
    subparsers = parser.add_subparsers(dest="command")

    analyze = subparsers.add_parser("analyze", help="分析指定股票的多个周期")
    analyze.add_argument("--symbols", nargs="+", help="股票代码，例如 000001.SZ 600036.SH")
    analyze.add_argument("--no-notify", action="store_true", help="只分析，不写入推送队列")

    scan = subparsers.add_parser("scan", help="扫描日线 MACD 0轴附近金叉")
    scan.add_argument("--no-notify", action="store_true", help="只扫描，不写入推送队列")

    monitor = subparsers.add_parser("monitor", help="常驻交易时段监控")
    monitor.add_argument("--once", action="store_true", help="只执行一次自选股/候选池监控")
    monitor.add_argument("--no-notify", action="store_true", help="不发送通知")

    subparsers.add_parser("test-notify", help="发送一条测试通知")
    return parser


def _print_summary(report: dict) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def _test_notification(monitor: SignalMonitor) -> int:
    channels = monitor.notifier.active_channels()
    if not channels:
        print("没有启用通知通道，请先配置 notification.wechat/email/webhook。")
        return 1
    now = now_shanghai().isoformat(timespec="seconds")
    event = SignalEvent(
        symbol="000001",
        name="通知测试",
        timeframe="5m",
        signal_type="buy_3+zero_axis_golden_cross",
        side="buy",
        price=10.0,
        structure_time=now,
        confirmed_at=now,
        score=80,
        evidence={
            "score_reasons": ["缠论buy_3 +30", "MACD 0轴附近金叉 +30", "大周期DIF向上 +15"],
            "latest_center": {"zd": 9.6, "zg": 9.8},
        },
        risk_notice=RISK_NOTICE,
    )
    results = {}
    for channel in channels:
        success, detail = monitor.notifier.send(channel, event.to_payload())
        results[channel] = {"success": success, "detail": detail}
    _print_summary(results)
    return 0 if all(item["success"] for item in results.values()) else 1


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    if config is None:
        return 2
    setup_logging(config)
    monitor = SignalMonitor(config)
    command = args.command or "scan"

    try:
        if command == "analyze":
            symbols = args.symbols or config.get("monitor", {}).get("watchlist", [])
            if not symbols:
                print("没有股票代码。请使用 --symbols 或配置 monitor.watchlist。")
                return 2
            _print_summary(monitor.analyze_symbols(symbols, notify=not args.no_notify))
        elif command == "scan":
            _print_summary(monitor.scan_zero_axis(notify=not args.no_notify))
        elif command == "monitor":
            if args.once:
                _print_summary(monitor.run_monitor_cycle(notify=not args.no_notify))
            else:
                monitor.run_forever(notify=not args.no_notify)
        elif command == "test-notify":
            return _test_notification(monitor)
        return 0
    except KeyboardInterrupt:
        logger.info("用户中断监控")
        return 130
    except Exception as exc:
        logger.exception("命令执行失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
