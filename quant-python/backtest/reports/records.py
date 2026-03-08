"""Standardized backtest and signal records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Dict, Iterable, List, Optional


def _as_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    ts_code: str
    stock_name: str
    signal_time: str
    regime: str
    strategy_name: str
    signal_type: str
    action: str
    suggested_position_change: float
    stop_loss_price: Optional[float] = None
    stop_profit_price: Optional[float] = None
    reason: str = ""
    risk_flags: List[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], index: int = 0, default_ts_code: str = "UNKNOWN") -> "SignalRecord":
        signal_time = payload.get("signal_time") or payload.get("datetime") or payload.get("scan_time") or datetime.now(UTC)
        ts_code = payload.get("ts_code", default_ts_code)
        signal_type = str(payload.get("signal_type") or payload.get("action") or "HOLD").upper()
        action = str(payload.get("action") or signal_type)
        return cls(
            signal_id=str(payload.get("signal_id") or f"{ts_code}-{signal_type}-{index + 1}"),
            ts_code=ts_code,
            stock_name=str(payload.get("stock_name") or payload.get("name") or ts_code),
            signal_time=_as_iso(signal_time) or "",
            regime=str(payload.get("regime") or payload.get("market_status") or "unknown"),
            strategy_name=str(payload.get("strategy_name") or "unknown"),
            signal_type=signal_type,
            action=action,
            suggested_position_change=float(payload.get("suggested_position_change", payload.get("position_pct", 0.0)) or 0.0),
            stop_loss_price=_to_optional_float(payload.get("stop_loss_price")),
            stop_profit_price=_to_optional_float(payload.get("stop_profit_price")),
            reason=str(payload.get("reason") or payload.get("explanation") or ""),
            risk_flags=list(payload.get("risk_flags", [])),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "ts_code": self.ts_code,
            "stock_name": self.stock_name,
            "signal_time": self.signal_time,
            "regime": self.regime,
            "strategy_name": self.strategy_name,
            "signal_type": self.signal_type,
            "action": self.action,
            "suggested_position_change": self.suggested_position_change,
            "stop_loss_price": self.stop_loss_price,
            "stop_profit_price": self.stop_profit_price,
            "reason": self.reason,
            "risk_flags": list(self.risk_flags),
        }


@dataclass(frozen=True)
class PositionSnapshot:
    snapshot_time: str
    ts_code: str
    base_shares: int
    base_cost: float
    mobile_shares: int
    mobile_cost: float
    current_price: float
    market_value: float
    profit_loss: float
    profit_rate: float

    @classmethod
    def from_position(
        cls,
        snapshot_time: Any,
        ts_code: str,
        shares: int,
        avg_cost: float,
        current_price: float,
        base_shares: Optional[int] = None,
    ) -> "PositionSnapshot":
        base_shares = int(base_shares if base_shares is not None else shares)
        mobile_shares = max(int(shares) - base_shares, 0)
        market_value = float(shares) * float(current_price)
        profit_loss = market_value - (float(shares) * float(avg_cost))
        denominator = float(shares) * float(avg_cost)
        profit_rate = (profit_loss / denominator) if denominator else 0.0
        return cls(
            snapshot_time=_as_iso(snapshot_time) or "",
            ts_code=ts_code,
            base_shares=base_shares,
            base_cost=float(avg_cost),
            mobile_shares=mobile_shares,
            mobile_cost=float(avg_cost),
            current_price=float(current_price),
            market_value=market_value,
            profit_loss=profit_loss,
            profit_rate=profit_rate,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_time": self.snapshot_time,
            "ts_code": self.ts_code,
            "base_shares": self.base_shares,
            "base_cost": self.base_cost,
            "mobile_shares": self.mobile_shares,
            "mobile_cost": self.mobile_cost,
            "current_price": self.current_price,
            "market_value": self.market_value,
            "profit_loss": self.profit_loss,
            "profit_rate": self.profit_rate,
        }


@dataclass(frozen=True)
class StrategyConfigSnapshot:
    strategy_name: str
    version: str
    params: Dict[str, Any]
    created_at: str

    @classmethod
    def from_config(
        cls,
        strategy_name: str,
        config: Optional[Dict[str, Any]],
        version: str = "v1",
        created_at: Optional[Any] = None,
    ) -> "StrategyConfigSnapshot":
        return cls(
            strategy_name=strategy_name,
            version=version,
            params=_jsonable(config or {}),
            created_at=_as_iso(created_at or datetime.now(UTC)) or "",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "version": self.version,
            "params": _jsonable(self.params),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class BacktestTrade:
    run_id: str
    ts_code: str
    entry_time: str
    exit_time: str
    side: str
    entry_price: float
    exit_price: float
    shares: int
    holding_days: int
    pnl: float
    pnl_ratio: float
    regime: str = "unknown"
    strategy_name: str = "unknown"
    entry_reason: str = ""
    exit_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ts_code": self.ts_code,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "side": self.side,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "shares": self.shares,
            "holding_days": self.holding_days,
            "pnl": self.pnl,
            "pnl_ratio": self.pnl_ratio,
            "regime": self.regime,
            "strategy_name": self.strategy_name,
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
        }


@dataclass(frozen=True)
class RegimeDecisionRecord:
    decision_time: str
    auto_regime: str
    manual_override: str
    final_regime: str
    score: float
    reason: str

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RegimeDecisionRecord":
        return cls(
            decision_time=_as_iso(payload.get("decision_time") or payload.get("scan_time") or datetime.now(UTC)) or "",
            auto_regime=str(payload.get("auto_regime") or payload.get("market_status") or "unknown"),
            manual_override=str(payload.get("manual_override") or "auto"),
            final_regime=str(payload.get("final_regime") or payload.get("market_status") or "unknown"),
            score=float(payload.get("score", 0.0) or 0.0),
            reason=str(payload.get("reason") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_time": self.decision_time,
            "auto_regime": self.auto_regime,
            "manual_override": self.manual_override,
            "final_regime": self.final_regime,
            "score": self.score,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BacktestRun:
    run_id: str
    strategy_name: str
    regime_scope: str
    start_date: str
    end_date: str
    config_snapshot: StrategyConfigSnapshot
    metrics: Dict[str, Any]
    engine_backend: str = "internal"
    cost_model: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy_name": self.strategy_name,
            "regime_scope": self.regime_scope,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "config_snapshot": self.config_snapshot.to_dict(),
            "metrics": _jsonable(self.metrics),
            "engine_backend": self.engine_backend,
            "cost_model": _jsonable(self.cost_model),
        }


def records_to_dicts(records: Iterable[Any]) -> List[Dict[str, Any]]:
    return [_jsonable(record) for record in records]


def _to_optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)
