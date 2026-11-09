"""Forward paper-ledger runner for the ETF rotation strategy.

This is the V2 follow-through: a dedicated, local-only paper ledger that starts
now and is advanced by a weekly cycle.  It never talks to a broker.

Typical use::

    # one-time, no plan until the first week-end
    python forward_paper.py --action init --capital 500000

    # after each close (or daily); add --sync to refresh the local cache first
    python forward_paper.py --action cycle --sync

    # inspect without changing state
    python forward_paper.py --action status

The ledger lives in ``signal_system/state/forward_paper_500k`` by default and
does not touch the existing page/runtime ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

DEFAULT_STATE = BASE_DIR / "state" / "forward_paper_500k"
DEFAULT_CAPITAL = 500_000.0


def _service(state_dir: Path):
    os.environ["ETF_ROTATION_STATE_DIR"] = str(state_dir)
    from etf_rotation import service

    return service


def _status(service: Any) -> dict[str, Any]:
    ledger = service.get_paper_ledger()
    return {
        "as_of": ledger.get("as_of"),
        "capital": ledger.get("capital"),
        "cash": ledger.get("cash"),
        "equity": ledger.get("equity"),
        "pnl_pct": ledger.get("pnl_pct"),
        "positions": ledger.get("positions"),
        "pending_plan": ledger.get("pending_plan"),
        "last_plan": ledger.get("last_plan"),
        "nav_points": len(ledger.get("nav_history") or []),
        "tracking": ledger.get("tracking"),
    }


def _action_init(service: Any, capital: float, state_dir: Path) -> dict[str, Any]:
    service.initialize_paper(capital, plan=False)
    service.mark_paper_to_market()
    return {
        "action": "init",
        "state_dir": str(state_dir),
        "capital": capital,
        "ledger": _status(service),
    }


def _action_cycle(service: Any, *, sync: bool, config: str, calendar: bool) -> dict[str, Any]:
    sync_result = None
    if sync:
        from etf_rotation.data_sync import sync as sync_data

        sync_result = sync_data(config_path=config)
    service.refresh_snapshot()
    fill_error = None
    plan_error = None
    try:
        service.fill_paper()
    except ValueError as exc:
        fill_error = str(exc)
    trade_dates = None
    calendar_mode = "friday_fallback"
    if calendar:
        try:
            from data.market_data import MarketDataClient
            from utils.helpers import load_config

            trade_dates = MarketDataClient(load_config(config)).get_trade_dates()
            calendar_mode = "exchange"
        except Exception:  # noqa: BLE001 - calendar is best-effort
            trade_dates = None
    schedule = service.week_end_status(trade_dates=trade_dates)
    if schedule["is_week_end"]:
        try:
            service.plan_paper()
        except ValueError as exc:
            plan_error = str(exc)
    else:
        plan_error = (
            f"非周度调仓日（本周最后交易日 {schedule['week_last_session']}），"
            "今日只成交与盯市，不生成新计划"
        )
    service.mark_paper_to_market()
    return {
        "action": "cycle",
        "sync": sync_result,
        "fill_error": fill_error,
        "plan_error": plan_error,
        "calendar": calendar_mode,
        "schedule": schedule,
        "ledger": _status(service),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ETF forward paper ledger")
    parser.add_argument("--action", choices=["init", "cycle", "status"], default="status")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE))
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--sync", action="store_true", help="refresh the local qfq cache first")
    parser.add_argument("--calendar", action="store_true", help="use exchange calendar for week-end")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    service = _service(state_dir)
    try:
        if args.action == "init":
            payload = _action_init(service, args.capital, state_dir)
        elif args.action == "cycle":
            payload = _action_cycle(service, sync=args.sync, config=args.config, calendar=args.calendar)
        else:
            payload = {"action": "status", "state_dir": str(state_dir), "ledger": _status(service)}
        print(json.dumps({"ok": True, "data": payload}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
