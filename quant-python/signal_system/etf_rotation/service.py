"""ETF rotation overview and local snapshot refresh.

The committed snapshot is the seed used by the read-only page. Refreshing writes
a runtime snapshot under signal_system/state (gitignored), so a local data sync
does not dirty the repository. Orders and the paper ledger remain separate.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path(__file__).with_name("snapshot.json")
STATE_DIR = Path(os.getenv("ETF_ROTATION_STATE_DIR", Path(__file__).resolve().parents[1] / "state"))
RUNTIME_SNAPSHOT = STATE_DIR / "etf_rotation_snapshot.json"
HISTORY_PATH = STATE_DIR / "etf_rotation_history.jsonl"
PAPER_LEDGER = STATE_DIR / "etf_paper_ledger.json"
PAPER_REFERENCE = STATE_DIR / "etf_paper_reference.json"
REQUIRED_KEYS = {"strategy", "as_of", "mode", "metrics", "signal", "target", "curve"}
LOT = 100
COMMISSION = 0.0003
MIN_COMMISSION = 5.0
SLIPPAGE = 0.0005
# Sessions the reference backtest runs before the ledger's first NAV point:
# `run_panels` needs 30, and it leaves room for several weekly rebalances.
REFERENCE_WARMUP = 30

METRIC_KEYS = (
    "total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe",
    "daily_win_rate_pct", "daily_payoff", "trade_win_rate_pct", "trade_payoff",
    "avg_exposure", "trades", "closed_rounds",
)
CANONICAL = {
    "strategy": "ETF v5.1 40/35 + 回填35",
    "strategy_id": "etf_v5.1_backfill_40_35",
    "config": {
        "core_weight": 0.40,
        "satellite_max_weight": 0.35,
        "backfill_weight": 0.35,
        "backfill_gold": 0.40,
        "backfill_bond": 0.60,
        "target_vol_pct": 10,
        "max_picks": 1,
        "breadth": "min(1, N_trend/2)",
        "crash_overlay": "off",
        "rebalance": "weekly last trading day, next open",
        "commission_bps": 3,
        "slippage_bps": 5,
    },
}


class EtfSnapshotError(ValueError):
    """Raised when the committed ETF snapshot is missing or malformed."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtfSnapshotError(f"ETF snapshot 读取失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise EtfSnapshotError("ETF snapshot 必须是 JSON 对象")
    missing = REQUIRED_KEYS.difference(payload)
    if missing:
        raise EtfSnapshotError(f"ETF snapshot 缺少字段: {sorted(missing)}")
    return payload


def _runtime_path() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_SNAPSHOT


def _history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                entries.append(item)
    except (OSError, json.JSONDecodeError):
        return []
    return entries[-30:]


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    """Load the snapshot, ignoring a runtime file written by a retired rule set.

    `state/etf_rotation_snapshot.json` survives deploys. After a rule change it
    still carries the old metrics and curve, so preferring it would put a
    retired strategy back on the page. A strategy_id mismatch falls back to the
    committed canonical snapshot; the next refresh then rewrites the runtime
    copy with a fresh signal.
    """
    if path is not None:
        source = path
    elif RUNTIME_SNAPSHOT.exists():
        cached = _read_json(RUNTIME_SNAPSHOT)
        if cached.get("strategy_id") == CANONICAL["strategy_id"]:
            return cached
        source = SNAPSHOT_PATH
    else:
        source = SNAPSHOT_PATH
    if not source.exists():
        raise EtfSnapshotError(f"ETF snapshot 不存在: {source}")
    return _read_json(source)


def refresh_snapshot(as_of: str | None = None) -> dict[str, Any]:
    """Recompute the signal at `as_of` (default: latest cached session) and persist it."""
    from .engine import compute_signal, load_panel

    close, volume = load_panel()
    signal = compute_signal(close, volume, as_of=as_of)
    snapshot = load_snapshot()
    snapshot.update(
        {
            "as_of": signal["as_of"],
            "mode": "local_refresh",
            "source": "local qfq cache",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "signal": signal,
            "target": signal["target"],
        }
    )
    runtime = _runtime_path()
    temporary = runtime.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(runtime)

    state_dir = _runtime_path().parent
    state_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "as_of": signal["as_of"],
        "generated_at": snapshot["generated_at"],
        "gate": signal["gate"],
        "core": signal["core"],
        "picks": signal["picks"],
        "target_weights": signal["target_weights"],
        "cash_weight": signal["cash_weight"],
        "vol_scale": signal["vol_scale"],
        "reason": signal["reason"],
    }
    existing = [item for item in _history() if item.get("as_of") != entry["as_of"]]
    existing.append(entry)
    HISTORY_PATH.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in existing[-30:]) + "\n",
        encoding="utf-8",
    )
    return snapshot


def _week_end(index: Any, day: Any, trade_dates: Any = None) -> dict[str, Any]:
    """Is `day` the last trading day of its ISO week?

    Price data alone cannot answer this live: on Tuesday the cache ends on
    Tuesday and still looks like the week's last session. Pass an exchange
    calendar (`trade_dates`, a set of dates) to look at the next session; the
    fallback is the conservative "Friday only" rule.
    """
    import pandas as pd

    target = pd.Timestamp(day) if day is not None else index[-1]
    if target not in index:
        earlier = index[index <= target]
        if len(earlier) == 0:
            raise ValueError(f"没有不晚于 {target.date()} 的交易日")
        target = earlier[-1]
    iso = target.isocalendar()[:2]

    if trade_dates:
        calendar = sorted(item for item in trade_dates)
        later = [item for item in calendar if item > target.date()]
        # A calendar built from realised bars (Tencent) ends at `target` and
        # cannot see the next session. Reading that as "week end" would plan
        # every day, so only a calendar that knows a later session decides.
        if later:
            week_last = max(item for item in calendar if item.isocalendar()[:2] == iso)
            return {
                "is_week_end": bool(later[0].isocalendar()[:2] != iso),
                "session": target.strftime("%Y-%m-%d"),
                "week_last_session": week_last.strftime("%Y-%m-%d"),
                "calendar": "exchange",
            }

    week = [item for item in index if item.isocalendar()[:2] == iso]
    return {
        "is_week_end": bool(target.weekday() == 4),
        "session": target.strftime("%Y-%m-%d"),
        "week_last_session": (max(week) if week else target).strftime("%Y-%m-%d"),
        "calendar": "friday_fallback",
    }


def rebuild_snapshot(*, start: str | None = None,
                     persist_committed: bool = False) -> dict[str, Any]:
    """Recompute metrics/curve/yearly with the canonical rules.

    `refresh_snapshot` only updates the current signal and target; the static
    metrics and curve must come from the same rule set, otherwise the page
    shows a strategy the runtime no longer implements.
    """
    from . import backtest

    result = backtest.run(start=start or backtest.DEFAULT_START)
    curve = result["curve"]
    snapshot = load_snapshot()
    snapshot.update({
        "strategy": CANONICAL["strategy"],
        "strategy_id": CANONICAL["strategy_id"],
        "config": CANONICAL["config"],
        "metrics": {key: result.get(key) for key in METRIC_KEYS},
        "yearly": result["yearly"],
        "curve": curve,
        "backtest": {
            key: result.get(key)
            for key in ("start", "end", "initial", "trades", "turnover_x", "slippage_bp", "closed_rounds")
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    })
    payload = json.dumps(snapshot, ensure_ascii=False, indent=1)
    runtime = _runtime_path()
    temporary = runtime.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(runtime)
    if persist_committed:
        committed_tmp = SNAPSHOT_PATH.with_suffix(".tmp")
        committed_tmp.write_text(payload, encoding="utf-8")
        committed_tmp.replace(SNAPSHOT_PATH)
    return snapshot


def _paper_default() -> dict[str, Any]:
    return {
        "initialized": False,
        "capital": 0.0,
        "cash": 0.0,
        "positions": {},
        "orders": [],
        "nav_history": [],
        "as_of": None,
        "updated_at": None,
    }


def _load_paper() -> dict[str, Any]:
    if not PAPER_LEDGER.exists():
        return _paper_default()
    try:
        payload = json.loads(PAPER_LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _paper_default()
    if not isinstance(payload, dict):
        return _paper_default()
    merged = _paper_default()
    merged.update(payload)
    if not isinstance(merged.get("positions"), dict):
        merged["positions"] = {}
    if not isinstance(merged.get("orders"), list):
        merged["orders"] = []
    if not isinstance(merged.get("nav_history"), list):
        merged["nav_history"] = []
    return merged


def _save_paper(ledger: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PAPER_LEDGER.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(PAPER_LEDGER)


def _fee(notional: float) -> float:
    return max(notional * COMMISSION, MIN_COMMISSION) + notional * SLIPPAGE


def _target_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = snapshot.get("target", [])
    if not isinstance(rows, list):
        raise EtfSnapshotError("ETF snapshot.target 必须是数组")
    target: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol == "CASH":
            continue
        target[symbol] = {
            "symbol": symbol,
            "name": str(row.get("name") or symbol),
            "weight": float(row.get("weight") or 0.0),
            "price": float(row.get("price") or 0.0),
        }
    return target


def _rebalance_paper(
    ledger: dict[str, Any],
    snapshot: dict[str, Any],
    prices_override: dict[str, float] | None = None,
    fill_date: str | None = None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    target = _target_map(snapshot)
    prices = prices_override or {symbol: row["price"] for symbol, row in target.items() if row["price"] > 0}
    if not prices:
        raise EtfSnapshotError("ETF 目标缺少有效价格，无法生成纸面账本")
    positions = ledger.setdefault("positions", {})
    cash = float(ledger.get("cash") or 0.0)
    for symbol, position in positions.items():
        if symbol in prices:
            position["last_price"] = prices[symbol]
    equity_before = cash + sum(
        float(position.get("shares") or 0) * float(position.get("last_price") or 0)
        for position in positions.values()
    )
    orders: list[dict[str, Any]] = []

    for symbol in sorted(list(positions)):
        position = positions[symbol]
        price = float(prices.get(symbol, position.get("last_price") or 0))
        shares = int(position.get("shares") or 0)
        if price <= 0 or shares <= 0:
            continue
        target_weight = float(target.get(symbol, {}).get("weight") or 0.0)
        target_value = equity_before * target_weight
        current_value = shares * price
        if current_value <= target_value + price * LOT * 0.5:
            continue
        sell_shares = int((current_value - target_value) / price / LOT) * LOT if target_weight > 0 else shares
        sell_shares = min(sell_shares, shares)
        if sell_shares <= 0:
            continue
        notional = sell_shares * price
        fee = _fee(notional)
        cash += notional - fee
        position["shares"] = shares - sell_shares
        orders.append({
            "symbol": symbol,
            "name": position.get("name", symbol),
            "side": "sell",
            "shares": sell_shares,
            "price": round(price, 6),
            "notional": round(notional, 2),
            "fee": round(fee, 2),
        })
        if position["shares"] <= 0:
            del positions[symbol]

    # Backtest parity: both sell and buy targets are sized on the same
    # pre-trade equity (equity_before). Using post-sell equity would silently
    # shrink every buy after a rebalance that also sells.
    for symbol, row in sorted(target.items()):
        price = float(row["price"])
        if price <= 0:
            continue
        target_value = equity_before * float(row["weight"])
        current = positions.get(symbol, {"shares": 0, "avg_cost": 0.0, "last_price": price})
        current_shares = int(current.get("shares") or 0)
        delta = target_value - current_shares * price
        if delta <= price * LOT * 0.5:
            continue
        buy_shares = int(delta / price / LOT) * LOT
        while buy_shares > 0 and buy_shares * price + _fee(buy_shares * price) > cash:
            buy_shares -= LOT
        if buy_shares <= 0:
            continue
        notional = buy_shares * price
        fee = _fee(notional)
        cash -= notional + fee
        old_shares = current_shares
        old_cost = float(current.get("avg_cost") or 0.0)
        total_cost = old_shares * old_cost + notional
        total_shares = old_shares + buy_shares
        positions[symbol] = {
            "symbol": symbol,
            "name": row["name"],
            "shares": total_shares,
            "avg_cost": total_cost / total_shares,
            "last_price": price,
        }
        orders.append({
            "symbol": symbol,
            "name": row["name"],
            "side": "buy",
            "shares": buy_shares,
            "price": round(price, 6),
            "notional": round(notional, 2),
            "fee": round(fee, 2),
        })

    for order in orders:
        order["status"] = "filled" if fill_date else "paper"
        if fill_date:
            order["fill_date"] = fill_date
    ledger.update({
        "initialized": True,
        "strategy_id": CANONICAL["strategy_id"],
        "cash": round(cash, 2),
        "positions": positions,
        "orders": (ledger.get("orders") or []) + orders,
        "as_of": fill_date or snapshot.get("as_of"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    if persist:
        _save_paper(ledger)
    return ledger


def _build_pending_plan(ledger: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build a pending plan in memory; the caller decides whether to persist."""
    target = _target_map(snapshot)
    if not target:
        raise EtfSnapshotError("ETF 目标为空，无法生成纸面计划")
    positions = ledger.get("positions") or {}
    equity = float(ledger.get("cash") or 0.0) + sum(
        int(position.get("shares") or 0) * float(position.get("last_price") or 0)
        for position in positions.values()
    )
    ledger["pending_plan"] = {
        "as_of": snapshot.get("as_of"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "planned",
        "strategy_id": CANONICAL["strategy_id"],
        "equity_at_plan": round(equity, 2),
        "equity_basis": "ledger_last_price",
        "target": [
            {
                "symbol": symbol,
                "name": row["name"],
                "weight": round(row["weight"], 6),
            }
            for symbol, row in sorted(target.items())
            if row["weight"] > 0
        ],
        "fill_note": "下一交易日开盘价成交",
    }
    return ledger


def _apply_pending_plan(
    ledger: dict[str, Any],
    plan: dict[str, Any],
    *,
    prices: dict[str, float],
    day: str,
    persist: bool = False,
) -> dict[str, Any]:
    """Apply a pending plan at supplied open prices without touching the live ledger."""
    target: list[dict[str, Any]] = []
    for row in plan.get("target", []):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        price = float(prices.get(symbol, float("nan")))
        if not symbol or not math.isfinite(price) or price <= 0:
            continue
        target.append({
            "symbol": symbol,
            "name": str(row.get("name") or symbol),
            "weight": float(row.get("weight") or 0.0),
            "price": price,
        })
    snapshot = {"as_of": plan.get("as_of"), "target": target}
    ledger = _rebalance_paper(
        ledger, snapshot, prices_override=prices, fill_date=day, persist=persist,
    )
    filled = {**plan, "status": "filled", "fill_date": day}
    ledger["pending_plan"] = filled
    ledger["last_plan"] = filled
    if persist:
        _save_paper(ledger)
    return ledger


def plan_paper() -> dict[str, Any]:
    ledger = _load_paper()
    if not ledger.get("initialized"):
        raise ValueError("纸面账本尚未初始化")
    pending = ledger.get("pending_plan")
    if isinstance(pending, dict) and pending.get("status") == "planned":
        raise ValueError("已有待成交的纸面计划，请先按开盘价成交")
    snapshot = load_snapshot()
    last = ledger.get("last_plan")
    if isinstance(last, dict) and last.get("status") == "filled" and last.get("as_of") == snapshot.get("as_of"):
        raise ValueError(f"{snapshot.get('as_of')} 的计划已成交，等待下一个周度信号日")
    ledger = _build_pending_plan(ledger, snapshot)
    _save_paper(ledger)
    return ledger


def fill_paper(as_of: str | None = None) -> dict[str, Any]:
    ledger = _load_paper()
    plan = ledger.get("pending_plan")
    if not isinstance(plan, dict) or plan.get("status") != "planned":
        raise ValueError("没有待成交的纸面计划")
    if plan.get("strategy_id") != CANONICAL["strategy_id"]:
        plan["status"] = "superseded"
        plan["superseded_reason"] = "策略口径已更新，旧计划不执行"
        ledger["pending_plan"] = plan
        ledger["last_plan"] = plan
        _save_paper(ledger)
        raise ValueError("待成交计划来自旧规则版本，已作废；等待下一个周度信号日")
    from .engine import EtfDataError, load_open_panel

    plan_date = str(plan.get("as_of") or "")
    opens = load_open_panel()
    dates = [day for day in opens.index if day.strftime("%Y-%m-%d") > plan_date]
    if as_of:
        dates = [day for day in dates if day.strftime("%Y-%m-%d") <= as_of]
    if not dates:
        raise ValueError("行情还没有下一交易日开盘价，暂不能成交")
    day = dates[0]
    prices: dict[str, float] = {}
    missing: list[str] = []
    for row in plan.get("target", []):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        price = float(opens[symbol].loc[day]) if symbol in opens.columns else float("nan")
        if not math.isfinite(price) or price <= 0:
            missing.append(symbol)
        else:
            prices[symbol] = price
    if missing:
        raise EtfDataError(f"缺少下一交易日开盘价: {missing}")
    # Positions leaving the target must also sell at this open (replay parity);
    # without a quote `_rebalance_paper` falls back to the last marked price.
    for symbol in ledger.get("positions") or {}:
        if symbol in prices or symbol not in opens.columns:
            continue
        price = float(opens[symbol].loc[day])
        if math.isfinite(price) and price > 0:
            prices[symbol] = price
    snapshot = load_snapshot()
    names = {row.get("symbol"): row.get("name") for row in snapshot.get("target", []) if isinstance(row, dict)}
    snapshot["as_of"] = day.strftime("%Y-%m-%d")
    snapshot["target"] = [
        {
            "symbol": str(row["symbol"]),
            "name": str(names.get(row["symbol"]) or row.get("name") or row["symbol"]),
            "weight": float(row["weight"]),
            "price": prices[str(row["symbol"])],
        }
        for row in plan.get("target", [])
        if isinstance(row, dict) and str(row.get("symbol")) in prices
    ]
    ledger = _rebalance_paper(ledger, snapshot, prices_override=prices, fill_date=day.strftime("%Y-%m-%d"))
    ledger["pending_plan"] = {**plan, "status": "filled", "fill_date": day.strftime("%Y-%m-%d")}
    ledger["last_plan"] = ledger["pending_plan"]
    _save_paper(ledger)
    return ledger


def initialize_paper(capital: float = 250_000.0, *, plan: bool = True) -> dict[str, Any]:
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("纸面账本初始资金必须大于 0")
    ledger = _paper_default()
    ledger["initialized"] = True
    ledger["capital"] = round(capital, 2)
    ledger["cash"] = round(capital, 2)
    _save_paper(ledger)
    return plan_paper() if plan else ledger


def rebalance_paper() -> dict[str, Any]:
    return plan_paper()


def advance_paper(trade_dates: Any = None) -> dict[str, Any]:
    """Advance the ledger session by session up to the latest cached session.

    Every session fills at its own open and gets its own NAV point, so a missed
    run cannot collapse several sessions into one trade at the latest prices.
    A week whose last session was not planned live (missed run, or a holiday
    Friday without a forward-looking calendar) is planned from that session's
    close before the next session's open fills it.
    """
    from .engine import load_panel

    ledger = _load_paper()
    if not ledger.get("initialized"):
        raise ValueError("纸面账本尚未初始化")
    close, volume = load_panel()
    index = close.index
    latest = index[-1]
    marked = sorted(
        str(item.get("date") or "") for item in ledger["nav_history"] if isinstance(item, dict)
    )
    # Never trade before the ledger existed: late plans need a NAV point at or
    # before the signal session.
    started = marked[0] if marked else None
    sessions = [day for day in index if marked and day.strftime("%Y-%m-%d") > marked[-1]] or [latest]

    steps: list[dict[str, Any]] = []
    schedule: dict[str, Any] | None = None
    for day in sessions:
        key = day.strftime("%Y-%m-%d")
        step: dict[str, Any] = {"session": key, "late_plan": None, "fill_error": None, "plan_error": None}
        position = index.get_loc(day)
        previous = index[position - 1] if position > 0 else None
        if previous is not None and previous.isocalendar()[:2] != day.isocalendar()[:2]:
            previous_key = previous.strftime("%Y-%m-%d")
            current = _load_paper()
            planned = {
                str(plan.get("as_of"))
                for plan in (current.get("pending_plan"), current.get("last_plan"))
                if isinstance(plan, dict)
            }
            if started and previous_key >= started and previous_key not in planned:
                refresh_snapshot(previous_key)
                step["late_plan"] = previous_key
                try:
                    plan_paper()
                except ValueError as exc:
                    step["plan_error"] = str(exc)
        try:
            fill_paper(key)
        except ValueError as exc:
            step["fill_error"] = str(exc)
        if day == latest:
            refresh_snapshot()
            schedule = _week_end(index, day, trade_dates)
            if schedule["is_week_end"]:
                try:
                    plan_paper()
                except ValueError as exc:
                    step["plan_error"] = str(exc)
            elif step["plan_error"] is None:
                step["plan_error"] = (
                    f"非周度调仓日（本周最后交易日 {schedule['week_last_session']}），"
                    "今日只成交与盯市，不生成新计划"
                )
        mark_paper_to_market(key)
        steps.append(step)
    _refresh_reference(close, volume)
    return {
        "steps": steps,
        "schedule": schedule,
        "fill_error": steps[-1]["fill_error"],
        "plan_error": steps[-1]["plan_error"],
    }


def mark_paper_to_market(as_of: str | None = None) -> dict[str, Any]:
    import pandas as pd

    from .engine import EtfDataError, load_panel

    ledger = _load_paper()
    if not ledger.get("initialized"):
        return ledger
    close, _volume = load_panel()
    target_day = pd.Timestamp(as_of) if as_of else close.index[-1]
    day = close.index[close.index <= target_day].max()
    if pd.isna(day):
        raise EtfDataError("没有可用于盯市的行情日期")
    positions = ledger.get("positions") or {}
    for symbol, position in positions.items():
        if symbol not in close.columns:
            continue
        price = float(close[symbol].loc[day])
        if math.isfinite(price) and price > 0:
            position["last_price"] = price
    market_value = sum(
        int(position.get("shares") or 0) * float(position.get("last_price") or 0)
        for position in positions.values()
    )
    cash = float(ledger.get("cash") or 0.0)
    equity = cash + market_value
    capital = float(ledger.get("capital") or 0.0)
    entry = {
        "date": day.strftime("%Y-%m-%d"),
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "pnl_pct": round((equity / capital - 1.0) * 100, 2) if capital > 0 else None,
    }
    history = [
        item for item in ledger.get("nav_history", [])
        if isinstance(item, dict) and item.get("date") != entry["date"]
    ]
    history.append(entry)
    history.sort(key=lambda item: str(item.get("date") or ""))
    ledger.update({
        "cash": round(cash, 2),
        "positions": positions,
        "nav_history": history,
        "as_of": entry["date"],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save_paper(ledger)
    return ledger


def _refresh_reference(close: Any, volume: Any) -> None:
    """Rerun the canonical backtest across the ledger's sessions and persist its curve.

    `snapshot["curve"]` is the static evaluation curve: it stops at the last
    manual rebuild, so the ledger's sessions are not on it. The run starts
    `REFERENCE_WARMUP` sessions before the ledger's first NAV point with the
    ledger's capital. Every weekly rebalance resets the weights to target, so by
    the ledger's first session the curve no longer depends on where the run
    began. Only the sessions the ledger has lived through are kept.
    """
    import pandas as pd

    from . import backtest
    from .engine import load_open_panel

    ledger = _load_paper()
    first = min(str(item["date"]) for item in ledger["nav_history"])
    position = int(close.index.searchsorted(pd.Timestamp(first)))
    start = close.index[max(position - REFERENCE_WARMUP, 0)]
    result = backtest.run_panels(
        close, load_open_panel(), volume,
        start=start.strftime("%Y-%m-%d"), initial=float(ledger["capital"]),
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "backtest_start": result["start"],
        "curve": [row for row in result["curve"] if row["date"] >= first],
    }
    temporary = PAPER_REFERENCE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(PAPER_REFERENCE)


def _load_reference() -> dict[str, float]:
    """Backtest equity by session as the last cycle wrote it; empty when there is none."""
    try:
        payload = json.loads(PAPER_REFERENCE.read_text(encoding="utf-8"))
        return {str(row["date"]): float(row["equity"]) for row in payload["curve"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _tracking(ledger: dict[str, Any]) -> dict[str, Any]:
    """Paper NAV against the canonical backtest since the ledger first held positions.

    The cash sessions before the first fill are left out: the backtest is
    invested throughout, so they would only measure when the ledger happened to
    start. Only sessions present in both series count, so a backtest that has
    not reached the ledger's sessions gives no comparison instead of a flat one.
    """
    tracking: dict[str, Any] = {
        "points": 0,
        "since": None,
        "until": None,
        "paper_return_pct": None,
        "backtest_return_pct": None,
        "difference_pct": None,
        "tracking_error_pct": None,
    }
    reference = _load_reference()
    nav = sorted(
        (item for item in ledger.get("nav_history", []) if isinstance(item, dict)),
        key=lambda item: str(item.get("date") or ""),
    )
    invested = next(
        (position for position, item in enumerate(nav) if float(item.get("market_value") or 0.0) > 0),
        len(nav),
    )
    aligned = [
        (str(item["date"]), float(item["equity"]), reference[str(item["date"])])
        for item in nav[invested:]
        if str(item.get("date")) in reference
    ]
    tracking["points"] = len(aligned)
    if len(aligned) < 2:
        return tracking
    paper = [row[1] for row in aligned]
    canonical = [row[2] for row in aligned]
    diffs = [
        paper[position] / paper[position - 1] - canonical[position] / canonical[position - 1]
        for position in range(1, len(aligned))
    ]
    mean = sum(diffs) / len(diffs)
    variance = sum((value - mean) ** 2 for value in diffs) / len(diffs)
    paper_return = paper[-1] / paper[0] - 1.0
    canonical_return = canonical[-1] / canonical[0] - 1.0
    tracking.update({
        "since": aligned[0][0],
        "until": aligned[-1][0],
        "paper_return_pct": round(paper_return * 100, 2),
        "backtest_return_pct": round(canonical_return * 100, 2),
        "difference_pct": round((paper_return - canonical_return) * 100, 2),
        "tracking_error_pct": round(math.sqrt(variance * 252.0) * 100.0, 2),
    })
    return tracking


def get_paper_ledger() -> dict[str, Any]:
    ledger = _load_paper()
    positions = ledger.get("positions") or {}
    rows = []
    market_value = 0.0
    for position in positions.values():
        shares = int(position.get("shares") or 0)
        price = float(position.get("last_price") or 0)
        value = shares * price
        market_value += value
        rows.append({
            **position,
            "market_value": round(value, 2),
        })
    equity = float(ledger.get("cash") or 0.0) + market_value
    capital = float(ledger.get("capital") or 0.0)
    return {
        **ledger,
        "positions": sorted(rows, key=lambda row: -row["market_value"]),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "pnl_pct": round((equity / capital - 1.0) * 100, 2) if capital > 0 else None,
        "tracking": _tracking(ledger),
    }


def get_overview() -> dict[str, Any]:
    """Return the current overview plus recent signal history."""
    snapshot = load_snapshot()
    runtime = RUNTIME_SNAPSHOT.exists()
    return {
        **snapshot,
        "stale": not runtime,
        "history": _history(),
        "paper": get_paper_ledger(),
        "refresh_note": (
            "当前为本地行情刷新快照；纸面账本已接入，实盘交易未接入。"
            if runtime
            else "当前为只读研究快照；纸面账本可手动初始化，实盘交易未接入。"
        ),
    }
