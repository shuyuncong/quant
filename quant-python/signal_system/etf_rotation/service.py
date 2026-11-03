"""Read-only ETF rotation overview.

The current implementation intentionally serves a frozen research snapshot.
Live signal refresh, orders and the paper ledger are separate follow-up steps;
the snapshot carries its as-of date and mode so the UI never presents it as a
live quote.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path(__file__).with_name("snapshot.json")
REQUIRED_KEYS = {"strategy", "as_of", "mode", "metrics", "signal", "target", "curve"}


class EtfSnapshotError(ValueError):
    """Raised when the committed ETF snapshot is missing or malformed."""


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    source = path or SNAPSHOT_PATH
    if not source.exists():
        raise EtfSnapshotError(f"ETF snapshot 不存在: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtfSnapshotError(f"ETF snapshot 读取失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise EtfSnapshotError("ETF snapshot 必须是 JSON 对象")
    missing = REQUIRED_KEYS.difference(payload)
    if missing:
        raise EtfSnapshotError(f"ETF snapshot 缺少字段: {sorted(missing)}")
    if not isinstance(payload.get("metrics"), dict):
        raise EtfSnapshotError("ETF snapshot.metrics 必须是对象")
    if not isinstance(payload.get("signal"), dict):
        raise EtfSnapshotError("ETF snapshot.signal 必须是对象")
    if not isinstance(payload.get("target"), list):
        raise EtfSnapshotError("ETF snapshot.target 必须是数组")
    return payload


def get_overview() -> dict[str, Any]:
    """Return the frozen overview payload used by the web console."""
    snapshot = load_snapshot()
    return {
        **snapshot,
        "stale": True,
        "refresh_note": "当前为只读研究快照；实时信号与纸面账本尚未接入。",
    }
