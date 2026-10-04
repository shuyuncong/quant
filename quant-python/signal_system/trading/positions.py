"""持仓台账与交易闸门: 把 max_stocks / 单只上限 / 单日回撤 / 每日交易次数落到实盘。

设计要点
--------
- **台账是人工录入的**: 本系统不下单。持仓由使用者按券商实际成交录入, 闸门据此判断
  "今天还能不能开新仓、能开多少"。因此这里只做**校验与记账**, 不做委托。
- **闸门在信号侧生效**: 每条买卖信号在进入通知 outbox 之前过一遍闸门, 被拦下的信号
  记入 `trade_gate_rejection` 并在报告里可见 —— 静默丢弃是最坏的结果。
- **单日幂等**: 同一股票同一天同一方向最多一次 (`max_trades_per_symbol_per_day_per_side`)。
  重复信号本来就有 event_id 去重, 但那条路径只拦"完全相同"的事件; 这里按
  (日期, 股票, 方向) 去重, 覆盖同日不同信号的重复。
- **时点口径**: 一律用上海时区的自然日, 与扫描/通知的日期口径一致。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from utils.time_utils import now_shanghai

POSITION_TABLE_DDL = """
                CREATE TABLE IF NOT EXISTS position (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    opened_on TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """

TRADE_LEDGER_TABLE_DDL = """
                CREATE TABLE IF NOT EXISTS trade_ledger (
                    trade_day TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    amount REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    note TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (trade_day, symbol, side)
                );
            """

TRADE_GATE_REJECTION_DDL = """
                CREATE TABLE IF NOT EXISTS trade_gate_rejection (
                    trade_day TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    rule TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (trade_day, symbol, side, rule)
                );
            """

DEFAULT_LIMITS: dict[str, Any] = {
    "enabled": True,
    "max_new_positions_per_day": 2,
    "max_trades_per_day": 5,
    "max_trades_per_symbol_per_day_per_side": 1,
    "max_new_position_pct": 0.25,
    "enforce_max_stocks": True,
    "enforce_single_position_cap": True,
    "enforce_single_day_drawdown": True,
}


def resolve_trading_limits(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the trade-gate settings, defaulting to the documented limits."""
    raw = config.get("trading_limits")
    if not isinstance(raw, dict):
        return dict(DEFAULT_LIMITS)
    limits = dict(DEFAULT_LIMITS)
    for key, default in DEFAULT_LIMITS.items():
        value = raw.get(key, default)
        if isinstance(default, bool):
            limits[key] = bool(value)
        elif isinstance(default, int):
            limits[key] = max(int(value), 0)
        elif isinstance(default, float):
            limits[key] = float(value)
        else:
            limits[key] = value
    return limits


class TradeGate:
    """持仓台账 + 交易闸门。所有读写都走 SignalStore 已初始化的 sqlite 连接。"""

    def __init__(self, store: Any, config: dict[str, Any]):
        self.store = store
        self.limits = resolve_trading_limits(config)
        self.position_config = dict(config.get("position") or {})
        self.risk_config = dict(config.get("risk") or {})

    # ---------- 持仓台账 ----------

    def list_positions(self) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT symbol, name, quantity, avg_cost, opened_on, updated_at "
                "FROM position WHERE quantity > 0 ORDER BY symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT symbol, name, quantity, avg_cost, opened_on, updated_at "
                "FROM position WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_position(
        self,
        symbol: str,
        quantity: int,
        avg_cost: float,
        name: str = "",
        opened_on: str | None = None,
    ) -> dict[str, Any]:
        """新增或覆盖一条持仓; quantity<=0 视为清仓并从台账移除。"""
        if quantity <= 0:
            return self.remove_position(symbol)
        if avg_cost <= 0:
            raise ValueError("avg_cost 必须为正数")
        today = now_shanghai().date().isoformat()
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO position (symbol, name, quantity, avg_cost, opened_on, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "name=excluded.name, quantity=excluded.quantity, avg_cost=excluded.avg_cost, "
                "opened_on=excluded.opened_on, updated_at=excluded.updated_at",
                (
                    symbol,
                    name,
                    int(quantity),
                    float(avg_cost),
                    opened_on or today,
                    now_shanghai().isoformat(timespec="seconds"),
                ),
            )
        return {"symbol": symbol, "quantity": int(quantity), "avg_cost": float(avg_cost)}

    def remove_position(self, symbol: str) -> dict[str, Any]:
        with self.store._connect() as connection:
            connection.execute("DELETE FROM position WHERE symbol = ?", (symbol,))
        return {"symbol": symbol, "quantity": 0, "removed": True}

    # ---------- 当日台账 ----------

    def record_trade(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        source: str = "manual",
        note: str = "",
        trade_day: str | None = None,
    ) -> dict[str, Any]:
        """记录一笔已发生的买卖, 并同步台账。这是"人工成交后回填"的入口。"""
        side = str(side).strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side 必须是 buy 或 sell")
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity 与 price 必须为正数")
        day = trade_day or now_shanghai().date().isoformat()
        amount = float(quantity) * float(price)
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO trade_ledger "
                "(trade_day, symbol, side, quantity, price, amount, source, note, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(trade_day, symbol, side) DO UPDATE SET "
                "quantity=excluded.quantity, price=excluded.price, amount=excluded.amount, "
                "source=excluded.source, note=excluded.note, recorded_at=excluded.recorded_at",
                (
                    day,
                    symbol,
                    side,
                    int(quantity),
                    float(price),
                    amount,
                    source,
                    note,
                    now_shanghai().isoformat(timespec="seconds"),
                ),
            )
        current = self.get_position(symbol)
        if side == "buy":
            held = int(current["quantity"]) if current else 0
            cost = float(current["avg_cost"]) if current else 0.0
            new_quantity = held + int(quantity)
            new_cost = (
                (held * cost + amount) / new_quantity if new_quantity > 0 else 0.0
            )
            self.upsert_position(
                symbol,
                new_quantity,
                new_cost,
                name=str((current or {}).get("name") or ""),
                opened_on=str((current or {}).get("opened_on") or day),
            )
        else:
            held = int(current["quantity"]) if current else 0
            self.upsert_position(
                symbol,
                max(held - int(quantity), 0),
                float(current["avg_cost"]) if current else 0.0,
                name=str((current or {}).get("name") or ""),
            )
        return {
            "trade_day": day,
            "symbol": symbol,
            "side": side,
            "quantity": int(quantity),
            "price": float(price),
            "amount": round(amount, 2),
        }

    def trades_today(self, trade_day: str | None = None) -> list[dict[str, Any]]:
        day = trade_day or now_shanghai().date().isoformat()
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT trade_day, symbol, side, quantity, price, amount, source, note, recorded_at "
                "FROM trade_ledger WHERE trade_day = ? ORDER BY recorded_at",
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def rejections_today(self, trade_day: str | None = None) -> list[dict[str, Any]]:
        day = trade_day or now_shanghai().date().isoformat()
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT trade_day, symbol, side, rule, detail, recorded_at "
                "FROM trade_gate_rejection WHERE trade_day = ? ORDER BY recorded_at",
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _record_rejection(self, symbol: str, side: str, rule: str, detail: str) -> None:
        day = now_shanghai().date().isoformat()
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO trade_gate_rejection "
                "(trade_day, symbol, side, rule, detail, recorded_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(trade_day, symbol, side, rule) DO UPDATE SET "
                "detail=excluded.detail, recorded_at=excluded.recorded_at",
                (day, symbol, side, rule, detail, now_shanghai().isoformat(timespec="seconds")),
            )

    # ---------- 闸门 ----------

    def _day_drawdown_exceeded(self, account_equity: float | None) -> tuple[bool, str]:
        """单日回撤: 用运行期记录的基准权益对比当前权益。"""
        threshold = float(self.risk_config.get("max_single_day_drawdown_pct", 0.02) or 0.0)
        if threshold <= 0 or account_equity is None or account_equity <= 0:
            return False, ""
        day = now_shanghai().date().isoformat()
        baseline = self.store.get_state("risk.day_open_equity", "") or ""
        payload = self.store.get_state("risk.day_open_equity_day", "") or ""
        if payload != day:
            # 当日首次调用即建立基准; 不追溯历史, 避免用陈旧权益误判。
            self.store.set_state("risk.day_open_equity", f"{float(account_equity):.2f}")
            self.store.set_state("risk.day_open_equity_day", day)
            return False, ""
        try:
            opened = float(baseline)
        except ValueError:
            return False, ""
        if opened <= 0:
            return False, ""
        drawdown = (opened - float(account_equity)) / opened
        if drawdown >= threshold:
            return True, f"day_drawdown={drawdown:.4f}>={threshold:.4f}"
        return False, ""

    def check(
        self,
        symbol: str,
        side: str,
        account_equity: float | None = None,
        name: str = "",
        quantity: int = 0,
        price: float = 0.0,
    ) -> dict[str, Any]:
        """判断一笔拟成交的买卖是否放行。

        返回 {"allowed": bool, "rule": str|None, "detail": str, "context": {...}}。
        被拒时同时写入 trade_gate_rejection, 便于事后复盘"为什么没提醒我"。
        """
        side = str(side).strip().lower()
        if not self.limits.get("enabled", True):
            return {"allowed": True, "rule": None, "detail": "gate_disabled", "context": {}}
        day = now_shanghai().date().isoformat()
        trades = self.trades_today(day)
        positions = self.list_positions()
        held_symbols = {str(item["symbol"]) for item in positions}
        same_side_today = [
            item for item in trades if str(item["symbol"]) == symbol and str(item["side"]) == side
        ]
        buys_today = [item for item in trades if item["side"] == "buy"]
        context = {
            "trade_day": day,
            "open_positions": len(positions),
            "buys_today": len(buys_today),
            "trades_today": len(trades),
            "already_held": symbol in held_symbols,
            "same_side_today": len(same_side_today),
        }

        # 1) 同日同股同向最多一次
        per_side = int(self.limits.get("max_trades_per_symbol_per_day_per_side", 1) or 0)
        if per_side and len(same_side_today) >= per_side:
            detail = f"{symbol} {side} 今日已记录 {len(same_side_today)} 笔, 上限 {per_side}"
            self._record_rejection(symbol, side, "per_symbol_per_day", detail)
            return {"allowed": False, "rule": "per_symbol_per_day", "detail": detail, "context": context}

        # 2) 每日总笔数
        max_trades = int(self.limits.get("max_trades_per_day", 0) or 0)
        if max_trades and len(trades) >= max_trades:
            detail = f"今日已成交 {len(trades)} 笔, 上限 {max_trades}"
            self._record_rejection(symbol, side, "max_trades_per_day", detail)
            return {"allowed": False, "rule": "max_trades_per_day", "detail": detail, "context": context}

        if side == "buy":
            # 3) 单日开新仓数量
            if not context["already_held"]:
                max_new = int(self.limits.get("max_new_positions_per_day", 0) or 0)
                if max_new and len(buys_today) >= max_new:
                    detail = f"今日已开 {len(buys_today)} 个新仓, 上限 {max_new}"
                    self._record_rejection(symbol, side, "max_new_positions_per_day", detail)
                    return {"allowed": False, "rule": "max_new_positions_per_day", "detail": detail, "context": context}
                # 4) 持仓只数
                max_stocks = int(self.position_config.get("max_stocks", 0) or 0)
                if self.limits.get("enforce_max_stocks", True) and max_stocks and len(positions) >= max_stocks:
                    detail = f"当前持仓 {len(positions)} 只, 上限 {max_stocks}"
                    self._record_rejection(symbol, side, "max_stocks", detail)
                    return {"allowed": False, "rule": "max_stocks", "detail": detail, "context": context}
            # 5) 单只仓位上限
            if (
                self.limits.get("enforce_single_position_cap", True)
                and account_equity
                and account_equity > 0
                and quantity > 0
                and price > 0
            ):
                cap = float(self.position_config.get("max_position_per_stock", 0.40) or 0.0)
                current = self.get_position(symbol)
                held_value = (
                    float(current["quantity"]) * float(current["avg_cost"]) if current else 0.0
                )
                new_value = held_value + float(quantity) * float(price)
                if cap and new_value / float(account_equity) > cap + 1e-9:
                    detail = (
                        f"{symbol} 加仓后市值占比 {new_value / account_equity:.4f} > 上限 {cap:.4f}"
                    )
                    self._record_rejection(symbol, side, "max_position_per_stock", detail)
                    return {"allowed": False, "rule": "max_position_per_stock", "detail": detail, "context": context}
            # 6) 单日回撤
            if self.limits.get("enforce_single_day_drawdown", True):
                breached, detail = self._day_drawdown_exceeded(account_equity)
                if breached:
                    self._record_rejection(symbol, side, "single_day_drawdown", detail)
                    return {"allowed": False, "rule": "single_day_drawdown", "detail": detail, "context": context}

        del name
        return {"allowed": True, "rule": None, "detail": "ok", "context": context}

    def filter_events(
        self,
        events: list[Any],
        account_equity: float | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """按闸门过滤信号事件; 返回 (放行事件, 拒绝明细)。

        买卖同权: 卖出也要过闸 (同日同股同向一次), 因为重复卖出提醒同样会误导操作。
        """
        allowed: list[Any] = []
        blocked: list[dict[str, Any]] = []
        for event in events:
            side = str(getattr(event, "side", "") or "").strip().lower()
            if side not in {"buy", "sell"}:
                allowed.append(event)
                continue
            symbol = str(getattr(event, "symbol", "") or "")
            price = float(getattr(event, "price", 0.0) or 0.0)
            verdict = self.check(symbol, side, account_equity=account_equity, price=price)
            if verdict["allowed"]:
                allowed.append(event)
            else:
                blocked.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "signal_type": str(getattr(event, "signal_type", "") or ""),
                        "rule": verdict["rule"],
                        "detail": verdict["detail"],
                    }
                )
        return allowed, blocked
