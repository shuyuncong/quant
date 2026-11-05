"""Sync ETF qfq daily bars into the local ETF rotation cache.

Uses the existing MarketDataClient provider chain. The cache remains local
(gitignored); the sync reports stale or missing symbols explicitly.
"""
from __future__ import annotations

import os
from pathlib import Path
from time import monotonic
from typing import Any

import pandas as pd

from data.market_data import MarketDataClient
from utils.helpers import load_config

from .engine import CORE, MARKET, SATELLITE

SYMBOLS = [MARKET, *CORE, *SATELLITE]
REQUIRED = {"datetime", "open", "close", "high", "low", "volume"}


class EtfSyncError(RuntimeError):
    """Raised when the ETF cache cannot be refreshed to the expected session."""


def _cache_dir(override: Path | None = None) -> Path:
    if override:
        return override
    env = os.getenv("ETF_ROTATION_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "cache" / "etf_rotation"


def _merge(path: Path, frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    if path.exists():
        try:
            old = pd.read_pickle(path)
            if isinstance(old, pd.DataFrame) and not old.empty:
                frame = pd.concat([old, frame], ignore_index=True)
        except Exception:
            pass
    frame = frame.drop_duplicates("datetime", keep="last").sort_values("datetime")
    return frame.reset_index(drop=True)


def sync(
    data_dir: Path | None = None,
    config_path: str | None = None,
    time_budget_s: float | None = None,
) -> dict[str, Any]:
    cache_dir = _cache_dir(data_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(config_path) if config_path else load_config()
    config.setdefault("market_data", {})["adjust"] = "qfq"
    client = MarketDataClient(config)
    expected = client.latest_expected_trade_date()
    started = monotonic()
    updated: list[str] = []
    errors: dict[str, str] = {}
    latest_dates: dict[str, str] = {}
    sources: dict[str, int] = {}
    skipped: list[str] = []
    for index, symbol in enumerate(SYMBOLS):
        # 缺缓存时刷新会现场补数据：AkShare 兜底约 4 秒/只，必须留退出预算，
        # 否则请求先被 nginx 的 60s 掐断，前端只能看到网关错误页。
        if time_budget_s is not None and monotonic() - started >= time_budget_s:
            skipped = list(SYMBOLS[index:])
            break
        try:
            frame = client.get_bars(symbol, "1d", limit=1200)
            if frame.empty:
                errors[symbol] = "empty"
                continue
            missing = REQUIRED.difference(frame.columns)
            if missing:
                errors[symbol] = f"missing columns: {sorted(missing)}"
                continue
            source = str(frame.attrs.get("source") or "unknown")
            merged = _merge(cache_dir / f"{symbol}_qfq.pkl", frame)
            merged.to_pickle(cache_dir / f"{symbol}_qfq.pkl")
            latest = pd.Timestamp(merged["datetime"].iloc[-1]).date()
            latest_dates[symbol] = latest.isoformat()
            updated.append(symbol)
            sources[source] = sources.get(source, 0) + 1
        except Exception as exc:  # noqa: BLE001 - report per-symbol and continue
            errors[symbol] = f"{type(exc).__name__}: {exc}"
    elapsed = monotonic() - started
    if skipped:
        raise EtfSyncError(
            f"ETF 行情同步超出 {float(time_budget_s):.0f}s 预算：已完成 {len(updated)}/{len(SYMBOLS)}，"
            f"剩余 {len(skipped)} 只。请在后台提交 etf-cycle 任务补全（任务无 HTTP 超时），"
            f"完成后再次刷新即可"
        )
    if not updated:
        raise EtfSyncError(f"ETF 行情同步全部失败: {errors}")
    critical_errors = {symbol: errors[symbol] for symbol in (MARKET, *CORE) if symbol in errors}
    if critical_errors:
        raise EtfSyncError(f"核心/闸门 ETF 行情同步失败: {critical_errors}")
    freshest = max(latest_dates.values()) if latest_dates else ""
    if freshest < expected.isoformat():
        raise EtfSyncError(
            f"ETF 行情未更新到最近交易日: freshest={freshest} expected={expected.isoformat()}"
        )
    return {
        "as_of": freshest,
        "expected": expected.isoformat(),
        "updated": len(updated),
        "total": len(SYMBOLS),
        "errors": errors,
        "sources": sources,
        "elapsed_s": round(elapsed, 1),
        "cache_dir": str(cache_dir),
    }
