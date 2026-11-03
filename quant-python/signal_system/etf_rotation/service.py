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

import pandas as pd

from .engine import EtfDataError, compute_signal, load_panel

SNAPSHOT_PATH = Path(__file__).with_name("snapshot.json")
STATE_DIR = Path(os.getenv("ETF_ROTATION_STATE_DIR", Path(__file__).resolve().parents[1] / "state"))
RUNTIME_SNAPSHOT = STATE_DIR / "etf_rotation_snapshot.json"
HISTORY_PATH = STATE_DIR / "etf_rotation_history.jsonl"
PAPER_LEDGER = STATE_DIR / "etf_paper_ledger.json"
REQUIRED_KEYS = {"strategy", "as_of", "mode", "metrics", "signal", "target", "curve"}
LOT = 100
COMMISSION = 0.0003
MIN_COMMISSION = 5.0
SLIPPAGE = 0.0005


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
    source = path or (RUNTIME_SNAPSHOT if RUNTIME_SNAPSHOT.exists() else SNAPSHOT_PATH)
    if not source.exists():
        raise EtfSnapshotError(f"ETF snapshot 不存在: {source}")
    return _read_json(source)


def refresh_snapshot() -> dict[str, Any]:
    """Recompute the current signal from the local qfq cache and persist it."""
    close, volume = load_panel()
    signal = compute_signal(close, volume)
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


def _rebalance_paper(ledger: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    target = _target_map(snapshot)
    prices = {symbol: row["price"] for symbol, row in target.items() if row["price"] > 0}
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

    equity_after_sells = cash + sum(
        float(position.get("shares") or 0) * float(position.get("last_price") or 0)
        for position in positions.values()
    )
    for symbol, row in sorted(target.items()):
        price = float(row["price"])
        if price <= 0:
            continue
        target_value = equity_after_sells * float(row["weight"])
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

    ledger.update({
        "initialized": True,
        "cash": round(cash, 2),
        "positions": positions,
        "orders": (ledger.get("orders") or []) + orders,
        "as_of": snapshot.get("as_of"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save_paper(ledger)
    return ledger


def initialize_paper(capital: float = 250_000.0) -> dict[str, Any]:
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("纸面账本初始资金必须大于 0")
    ledger = _paper_default()
    ledger["capital"] = round(capital, 2)
    ledger["cash"] = round(capital, 2)
    return _rebalance_paper(ledger, load_snapshot())


def rebalance_paper() -> dict[str, Any]:
    ledger = _load_paper()
    if not ledger.get("initialized"):
        raise ValueError("纸面账本尚未初始化")
    return _rebalance_paper(ledger, load_snapshot())


def mark_paper_to_market(as_of: str | None = None) -> dict[str, Any]:
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


def _tracking(ledger: dict[str, Any]) -> dict[str, Any]:
    nav = sorted(
        [item for item in ledger.get("nav_history", []) if isinstance(item, dict)],
        key=lambda item: str(item.get("date") or ""),
    )
    empty = {
        "points": len(nav),
        "paper_return_pct": None,
        "backtest_return_pct": None,
        "difference_pct": None,
        "tracking_error_pct": None,
    }
    if len(nav) < 2:
        return empty
    snapshot = load_snapshot()
    curve = {
        str(item.get("date")): float(item.get("equity") or 0.0)
        for item in snapshot.get("curve", [])
        if isinstance(item, dict)
    }
    if not curve:
        return empty
    backtest_dates = sorted(curve)
    aligned: list[tuple[float, float]] = []
    for item in nav:
        date = str(item.get("date") or "")
        candidates = [day for day in backtest_dates if day <= date]
        if not candidates:
            continue
        aligned.append((float(item.get("equity") or 0.0), curve[candidates[-1]]))
    if len(aligned) < 2:
        return empty
    paper_returns = []
    backtest_returns = []
    for (prev_paper, prev_bt), (cur_paper, cur_bt) in zip(aligned, aligned[1:]):
        if prev_paper <= 0 or prev_bt <= 0:
            continue
        paper_returns.append(cur_paper / prev_paper - 1.0)
        backtest_returns.append(cur_bt / prev_bt - 1.0)
    if len(paper_returns) < 1:
        return empty
    diffs = [paper - backtest for paper, backtest in zip(paper_returns, backtest_returns)]
    mean = sum(diffs) / len(diffs)
    variance = sum((value - mean) ** 2 for value in diffs) / len(diffs)
    tracking_error = math.sqrt(variance) * math.sqrt(252.0) * 100.0
    paper_return = aligned[-1][0] / aligned[0][0] - 1.0
    backtest_return = aligned[-1][1] / aligned[0][1] - 1.0
    return {
        "points": len(aligned),
        "paper_return_pct": round(paper_return * 100, 2),
        "backtest_return_pct": round(backtest_return * 100, 2),
        "difference_pct": round((paper_return - backtest_return) * 100, 2),
        "tracking_error_pct": round(tracking_error, 2),
    }


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
