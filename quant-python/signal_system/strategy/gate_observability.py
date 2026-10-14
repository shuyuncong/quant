"""Market-gate observability: blocker naming and daily/weekly aggregates.

Kept in its own module on purpose: ``strategy/market_gate.py`` and
``utils/helpers.py`` are **byte-frozen by preregistered integrity audits**
(``canonical_replay_semantics_audit`` / ``entry_context_batch_audit``). Adding
observability helpers there would trip those hash checks for a change that
cannot alter any replay number.

This module is pure bookkeeping — it never decides whether entries are allowed.
The live gate (``monitor/service.py:_market_entry_context``) and the backtest
(``backtest_winrate.build_market_gate``) stay the only sources of truth.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

# 闸门 blocker 名称与顺序必须与 backtest_winrate.build_market_gate 逐字一致，
# 否则日报里的分布无法与回测口径对照（命名漂移 = 数据不可比）。
MARKET_GATE_BLOCKER_ORDER: tuple[str, ...] = (
    "below_ma_long",
    "ma_long_down",
    "macd_death_cross",
    "trend_down",
    "ma10_latch_bear",
    "macd_latch_bear",
)


def classify_market_gate_blockers(
    *,
    above_ma_long: bool,
    ma_long_down: bool,
    death_cross: bool,
    trend_up: bool,
    ma10_latch_bear: bool,
    macd_latch_bear: bool,
) -> list[str]:
    """Return which blockers fired, named exactly as the backtest names them.

    Pure and total over the six booleans the gate already computes, so the
    reported distribution can never be wider or narrower than the decision
    itself: ``blocked`` must stay equal to ``bool(classify_market_gate_blockers(...))``.
    """
    hits = {
        "below_ma_long": not above_ma_long,
        "ma_long_down": ma_long_down,
        "macd_death_cross": death_cross,
        "trend_down": not trend_up,
        "ma10_latch_bear": ma10_latch_bear,
        "macd_latch_bear": macd_latch_bear,
    }
    return [name for name in MARKET_GATE_BLOCKER_ORDER if hits[name]]


def summarize_market_gate_history(
    observations: Iterable[Mapping[str, Any]],
    *,
    window_days: int = 120,
) -> dict[str, Any]:
    """Aggregate recorded daily gate observations into a "is the gate too strict" view.

    ``observations`` are ``{"date", "allows_entries", "blocked_by", "regime"}``
    rows in chronological order (oldest first); only the last ``window_days``
    are summarized. Hit counts are per trading day, not per symbol: the market
    gate is an index-level decision evaluated once per day, so ``ma_long_down:
    118/120`` means it fired on 118 of the 120 recorded days.
    """
    rows = [row for row in observations if isinstance(row, Mapping)]
    window = rows[-max(int(window_days), 1):] if rows else []
    total = len(window)
    hits = {name: 0 for name in MARKET_GATE_BLOCKER_ORDER}
    allowed_days = 0
    unknown: Counter = Counter()
    for row in window:
        if bool(row.get("allows_entries")):
            allowed_days += 1
        for name in row.get("blocked_by") or []:
            if name in hits:
                hits[name] += 1
            else:
                unknown[str(name)] += 1
    blocked_days = total - allowed_days
    return {
        "window_days": max(int(window_days), 1),
        "days_recorded": total,
        "window_start": str(window[0]["date"]) if total else None,
        "window_end": str(window[-1]["date"]) if total else None,
        "allowed_days": allowed_days,
        "blocked_days": blocked_days,
        "allowed_ratio": (allowed_days / total) if total else None,
        "hits": {
            name: {
                "days": hits[name],
                "ratio": (hits[name] / total) if total else None,
                "display": f"{hits[name]}/{total}",
            }
            for name in MARKET_GATE_BLOCKER_ORDER
        },
        "other_blockers": dict(unknown),
        "blocked_by_daily": [
            {"date": str(row.get("date")), "blocked_by": list(row.get("blocked_by") or [])}
            for row in window
        ],
    }
