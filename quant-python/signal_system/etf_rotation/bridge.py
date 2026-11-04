"""Lightweight JSON bridge for the ETF rotation page.

Unlike signal_system/web_bridge.py this module does not import the stock
monitor/data/strategy stack at startup, so the web page only pays for the ETF
snapshot it needs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SIGNAL_SYSTEM = Path(__file__).resolve().parents[1]
if str(SIGNAL_SYSTEM) not in sys.path:
    sys.path.insert(0, str(SIGNAL_SYSTEM))


def _emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return code


def _read_payload(raw: str) -> dict[str, Any]:
    text = sys.stdin.read() if raw == "-" else raw
    if raw.startswith("@"):
        text = Path(raw[1:]).read_text(encoding="utf-8")
    parsed = json.loads(text or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("payload 必须是 JSON 对象")
    return parsed


def _overview() -> int:
    from etf_rotation.service import get_overview

    return _emit({"ok": True, "data": {"overview": get_overview()}})


def _refresh() -> int:
    from etf_rotation.service import get_overview, refresh_snapshot

    refresh_snapshot()
    return _emit({"ok": True, "data": {"overview": get_overview()}})


def _data_sync(config_path: str, payload: dict[str, Any]) -> int:
    from etf_rotation.data_sync import sync

    result = sync(config_path=config_path, data_dir=payload.get("data_dir"))
    return _emit({"ok": True, "data": {"sync": result}})


def _cycle(config_path: str) -> int:
    from etf_rotation.data_sync import sync
    from etf_rotation.service import (
        fill_paper,
        get_overview,
        mark_paper_to_market,
        plan_paper,
        refresh_snapshot,
        week_end_status,
    )

    sync_result = sync(config_path=config_path)
    refresh_snapshot()
    fill_error = None
    plan_error = None
    try:
        fill_paper()
    except ValueError as exc:
        fill_error = str(exc)
    trade_dates = None
    try:
        from data.market_data import MarketDataClient
        from utils.helpers import load_config

        trade_dates = MarketDataClient(load_config(config_path)).get_trade_dates()
    except Exception:  # noqa: BLE001 - calendar is best-effort, fall back to Friday
        trade_dates = None
    schedule = week_end_status(trade_dates=trade_dates)
    if schedule["is_week_end"]:
        try:
            plan_paper()
        except ValueError as exc:
            plan_error = str(exc)
    else:
        plan_error = (
            f"非周度调仓日（本周最后交易日 {schedule['week_last_session']}），"
            "今日只成交与盯市，不生成新计划"
        )
    mark_paper_to_market()
    return _emit({
        "ok": True,
        "data": {
            "sync": sync_result,
            "overview": get_overview(),
            "fill_error": fill_error,
            "plan_error": plan_error,
            "plan_schedule": schedule,
        },
    })


def _backtest(_payload: dict[str, Any]) -> int:
    from etf_rotation.service import rebuild_snapshot

    snapshot = rebuild_snapshot(persist_committed=True)
    return _emit({
        "ok": True,
        "data": {
            "metrics": snapshot["metrics"],
            "backtest": snapshot.get("backtest"),
            "strategy": snapshot.get("strategy"),
        },
    })


def _paper(payload: dict[str, Any]) -> int:
    from etf_rotation.service import (
        fill_paper,
        get_overview,
        get_paper_ledger,
        initialize_paper,
        mark_paper_to_market,
        plan_paper,
        rebalance_paper,
    )

    action = str(payload.get("action") or "status")
    if action == "init":
        initialize_paper(float(payload.get("capital") or 250_000.0))
    elif action == "plan":
        plan_paper()
    elif action == "rebalance":
        rebalance_paper()
    elif action == "fill":
        fill_paper(payload.get("as_of"))
    elif action == "mark":
        mark_paper_to_market(payload.get("as_of"))
    elif action != "status":
        return _emit({"ok": False, "error": f"不支持的 ETF 纸面账本动作: {action}", "code": 2}, 2)
    return _emit({"ok": True, "data": {"overview": get_overview(), "paper": get_paper_ledger()}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ETF rotation lightweight bridge")
    parser.add_argument("command", choices=["overview", "refresh", "data-sync", "cycle", "paper", "backtest"])
    parser.add_argument("--config", default=str(SIGNAL_SYSTEM / "config" / "config.yaml"))
    parser.add_argument("--payload", default="-")
    args = parser.parse_args(argv)
    try:
        payload = _read_payload(args.payload)
        if args.command == "overview":
            return _overview()
        if args.command == "refresh":
            return _refresh()
        if args.command == "data-sync":
            return _data_sync(args.config, payload)
        if args.command == "cycle":
            return _cycle(args.config)
        if args.command == "backtest":
            return _backtest(payload)
        return _paper(payload)
    except (ValueError, TypeError) as exc:
        return _emit({"ok": False, "error": str(exc), "code": 2}, 2)
    except Exception as exc:  # noqa: BLE001 - bridge boundary
        return _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}", "code": 1}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
