"""Causal, configurable stock-universe filtering shared by live scans and backtests."""

from __future__ import annotations

from collections import Counter
from datetime import date
import re
from typing import Any

import pandas as pd


HUNDRED_MILLION_CNY = 100_000_000.0
_ST_PATTERN = re.compile(r"(?:\*?ST|SST)", re.IGNORECASE)


def resolve_stock_pool_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return normalized stock-pool settings with explicit user-facing units."""
    raw = config.get("stock_pool")
    if not isinstance(raw, dict):
        raw = {}
        enabled = False
    else:
        enabled = bool(raw.get("enabled", True))
    market_data = config.get("market_data", {})
    return {
        "enabled": enabled,
        "min_market_cap": float(raw.get("min_market_cap", 50.0)),
        "max_market_cap": float(raw.get("max_market_cap", 3000.0)),
        "amount_window": max(int(raw.get("amount_window", 20)), 1),
        "min_avg_amount": float(raw.get("min_avg_amount", 1.0)),
        "turnover_window": max(int(raw.get("turnover_window", 20)), 1),
        "min_avg_turnover_rate": float(raw.get("min_avg_turnover_rate", 0.5)),
        "max_avg_turnover_rate": float(raw.get("max_avg_turnover_rate", 8.0)),
        "min_listing_trade_days": max(int(raw.get("min_listing_trade_days", 120)), 0),
        "exclude_st": bool(raw.get("exclude_st", True)),
        "exclude_delisting": bool(raw.get("exclude_delisting", True)),
        "missing_data_policy": str(raw.get("missing_data_policy", "reject")).lower(),
        "volume_unit_shares": max(
            float(raw.get("volume_unit_shares", market_data.get("volume_unit_shares", 100))),
            0.0,
        ),
    }


def _prepare_history(history: pd.DataFrame, as_of: date) -> pd.DataFrame:
    if history is None or history.empty or "datetime" not in history.columns:
        return pd.DataFrame()
    frame = history.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame = frame.dropna(subset=["datetime"])
    if "is_closed" in frame.columns:
        frame = frame[frame["is_closed"].fillna(False).astype(bool)]
    frame = frame[frame["datetime"].dt.date <= as_of]
    for column in (
        "close",
        "volume",
        "amount",
        "turnover_rate",
        "circulating_market_cap",
    ):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return (
        frame.sort_values("datetime")
        .drop_duplicates("datetime", keep="last")
        .reset_index(drop=True)
    )


def _market_cap(frame: pd.DataFrame, volume_unit_shares: float) -> float | None:
    latest = frame.iloc[-1]
    if "circulating_market_cap" in frame.columns:
        value = latest.get("circulating_market_cap")
        if pd.notna(value) and float(value) >= 0:
            return float(value)
    required = {"close", "volume", "turnover_rate"}
    if not required.issubset(frame.columns):
        return None
    close = latest.get("close")
    volume = latest.get("volume")
    turnover = latest.get("turnover_rate")
    if any(pd.isna(value) for value in (close, volume, turnover)) or float(turnover) <= 0:
        return None
    circulating_shares = float(volume) * volume_unit_shares / (float(turnover) / 100.0)
    return float(close) * circulating_shares / HUNDRED_MILLION_CNY


def _average_amount(
    frame: pd.DataFrame,
    window: int,
    volume_unit_shares: float,
) -> float | None:
    if len(frame) < window:
        return None
    recent = frame.tail(window)
    if "amount" in recent.columns:
        amount = pd.to_numeric(recent["amount"], errors="coerce")
    else:
        amount = pd.Series(float("nan"), index=recent.index, dtype="float64")
    if {"close", "volume"}.issubset(recent.columns):
        estimated = (
            pd.to_numeric(recent["close"], errors="coerce")
            * pd.to_numeric(recent["volume"], errors="coerce")
            * volume_unit_shares
        )
        amount = amount.where(amount > 0, estimated)
    valid = amount.dropna()
    if len(valid) < window:
        return None
    return float(valid.mean()) / HUNDRED_MILLION_CNY


def _average_turnover(frame: pd.DataFrame, window: int) -> float | None:
    if len(frame) < window or "turnover_rate" not in frame.columns:
        return None
    turnover = pd.to_numeric(frame.tail(window)["turnover_rate"], errors="coerce").dropna()
    if len(turnover) < window:
        return None
    return float(turnover.mean())


def evaluate_stock_pool(
    history: pd.DataFrame,
    as_of: date,
    config: dict[str, Any],
    *,
    name: str = "",
) -> dict[str, Any]:
    """Evaluate one symbol using information available on or before ``as_of``."""
    settings = resolve_stock_pool_config(config)
    if not settings["enabled"]:
        return {
            "passed": True,
            "reasons": [],
            "warnings": [],
            "metrics": {"enabled": False, "as_of": as_of.isoformat()},
        }

    frame = _prepare_history(history, as_of)
    reasons: list[str] = []
    warnings: list[str] = []

    def missing(code: str) -> None:
        target = warnings if settings["missing_data_policy"] == "allow" else reasons
        target.append(code)

    if settings["exclude_st"] and name and _ST_PATTERN.search(name):
        reasons.append("stock_pool_special_treatment")
    if settings["exclude_delisting"] and name and "退" in name:
        reasons.append("stock_pool_delisting_risk")

    listing_days = len(frame)
    if frame.empty:
        missing("stock_pool_listing_days_missing")
    elif listing_days < settings["min_listing_trade_days"]:
        reasons.append("stock_pool_listing_days_below_min")

    metrics_frame = frame
    if not frame.empty and frame.iloc[-1]["datetime"].date() != as_of:
        missing("stock_pool_history_stale")
        metrics_frame = pd.DataFrame()

    market_cap = None
    avg_amount = None
    avg_turnover = None
    if frame.empty:
        missing("stock_pool_history_missing")
    elif not metrics_frame.empty:
        market_cap = _market_cap(metrics_frame, settings["volume_unit_shares"])
        avg_amount = _average_amount(
            metrics_frame,
            settings["amount_window"],
            settings["volume_unit_shares"],
        )
        avg_turnover = _average_turnover(metrics_frame, settings["turnover_window"])

    if market_cap is None:
        missing("stock_pool_market_cap_missing")
    elif market_cap < settings["min_market_cap"]:
        reasons.append("stock_pool_market_cap_below_min")
    elif market_cap > settings["max_market_cap"]:
        reasons.append("stock_pool_market_cap_above_max")

    if avg_amount is None:
        missing("stock_pool_avg_amount_missing")
    elif avg_amount < settings["min_avg_amount"]:
        reasons.append("stock_pool_avg_amount_below_min")

    if avg_turnover is None:
        missing("stock_pool_turnover_missing")
    elif avg_turnover < settings["min_avg_turnover_rate"]:
        reasons.append("stock_pool_turnover_below_min")
    elif avg_turnover > settings["max_avg_turnover_rate"]:
        reasons.append("stock_pool_turnover_above_max")

    metrics = {
        "enabled": True,
        "as_of": as_of.isoformat(),
        "market_cap": round(market_cap, 2) if market_cap is not None else None,
        "market_cap_unit": "亿元流通市值",
        "avg_amount": round(avg_amount, 2) if avg_amount is not None else None,
        "avg_amount_unit": "亿元",
        "amount_window": settings["amount_window"],
        "avg_turnover_rate": round(avg_turnover, 4) if avg_turnover is not None else None,
        "turnover_rate_unit": "%",
        "turnover_window": settings["turnover_window"],
        "listing_trade_days": listing_days,
    }
    return {
        "passed": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "metrics": metrics,
    }


def filter_buy_events(
    history: pd.DataFrame,
    events: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
    *,
    name: str = "",
) -> tuple[dict[str, list[dict[str, Any]]], Counter, list[dict[str, Any]]]:
    """Filter buy events causally and preserve sell events unchanged."""
    if not resolve_stock_pool_config(config)["enabled"]:
        return events, Counter(), []
    accepted: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    rejection_details: list[dict[str, Any]] = []
    for event in events.get("buy", []):
        day = date.fromisoformat(str(event["day"]))
        evaluation = evaluate_stock_pool(history, day, config, name=name)
        if not evaluation["passed"]:
            skipped.update(evaluation["reasons"])
            rejection_details.append(
                {
                    "day": day.isoformat(),
                    "signal_type": event.get("signal_type"),
                    "reasons": evaluation["reasons"],
                    "warnings": evaluation["warnings"],
                    "metrics": evaluation["metrics"],
                }
            )
            continue
        enriched = dict(event)
        enriched["stock_pool_metrics"] = evaluation["metrics"]
        if evaluation["warnings"]:
            enriched["stock_pool_warnings"] = evaluation["warnings"]
        accepted.append(enriched)
    return (
        {"buy": accepted, "sell": list(events.get("sell", []))},
        skipped,
        rejection_details,
    )
