"""Shared serializable models for the signal monitor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any


RISK_NOTICE = "量化信号仅供研究，不构成投资建议；请独立判断并控制风险。"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


@dataclass(frozen=True)
class SignalEvent:
    symbol: str
    name: str
    timeframe: str
    signal_type: str
    side: str
    price: float
    structure_time: str
    confirmed_at: str
    score: int
    evidence: dict[str, Any] = field(default_factory=dict)
    schema: str = "quant.signal.v1"
    risk_notice: str = RISK_NOTICE

    @property
    def event_id(self) -> str:
        raw = "|".join(
            [
                self.symbol,
                self.timeframe,
                self.signal_type,
                self.side,
                self.confirmed_at,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_id"] = self.event_id
        return json.loads(json.dumps(payload, ensure_ascii=False, default=_json_default))


@dataclass
class TimeframeReport:
    timeframe: str
    status: str
    latest_time: str | None = None
    latest_price: float | None = None
    indicators: dict[str, Any] = field(default_factory=dict)
    chan: dict[str, Any] = field(default_factory=dict)
    buy_score: int = 0
    sell_score: int = 0
    events: list[SignalEvent] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["events"] = [event.to_payload() for event in self.events]
        return json.loads(json.dumps(result, ensure_ascii=False, default=_json_default))

