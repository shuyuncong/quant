"""ETF rotation overview and local snapshot refresh.

The committed snapshot is the seed used by the read-only page. Refreshing writes
a runtime snapshot under signal_system/state (gitignored), so a local data sync
does not dirty the repository. Orders and the paper ledger remain separate.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .engine import compute_signal, load_panel

SNAPSHOT_PATH = Path(__file__).with_name("snapshot.json")
STATE_DIR = Path(os.getenv("ETF_ROTATION_STATE_DIR", Path(__file__).resolve().parents[1] / "state"))
RUNTIME_SNAPSHOT = STATE_DIR / "etf_rotation_snapshot.json"
HISTORY_PATH = STATE_DIR / "etf_rotation_history.jsonl"
REQUIRED_KEYS = {"strategy", "as_of", "mode", "metrics", "signal", "target", "curve"}


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


def get_overview() -> dict[str, Any]:
    """Return the current overview plus recent signal history."""
    snapshot = load_snapshot()
    runtime = RUNTIME_SNAPSHOT.exists()
    return {
        **snapshot,
        "stale": not runtime,
        "history": _history(),
        "refresh_note": (
            "当前为本地行情刷新快照；未接入交易与纸面账本。"
            if runtime
            else "当前为只读研究快照；实时信号与纸面账本尚未接入。"
        ),
    }
