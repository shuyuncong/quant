"""Causal daily-bar backtest with independent-signal and funded-portfolio modes.

The engine deliberately separates signal discovery, one-trade simulation, and
portfolio allocation. Buy/sell signals are confirmed at the close and their
orders fill at the next trading-day open. Intraday risk orders may fill on the
trigger day once T+1 permits selling.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
QUANT_ROOT = BASE_DIR.parent
if str(QUANT_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANT_ROOT))

from strategy.chan import analyze_chan
from strategy.macd import calculate_macd, classify_zero_axis_zone, find_golden_cross_entries
from strategy.market_gate import (
    calculate_strict_regime,
    calculate_trend_gate,
    resolve_market_gate_settings,
    resolve_min_confirmations,
)
from strategy.signal_policy import (
    partition_entry_signals,
    resolve_signal_execution_policy,
)
from strategy.stock_pool import filter_buy_events, resolve_stock_pool_config
from core.selector.fundamental import (
    filter_buy_events_by_fundamental,
    load_fundamental_history,
    resolve_fundamental_config,
)
from core.selector.fundamental_history import history_coverage_report
from core.strategy.framework import build_config_snapshot, resolve_strategy_framework
from utils.helpers import load_config


logger = logging.getLogger(__name__)
HISTORY_DIR = BASE_DIR / "cache" / "daily_history"
SELL_TYPES = {"sell_1", "sell_2", "sell_3", "zero_axis_death_cross"}
DEFAULT_SIGNAL_PRIORITY = (
    "macd_golden_cross_pullback_confirmed_above",
    "macd_golden_cross_pullback_confirmed_near",
    "buy_1",
    "buy_2",
    "buy_3",
)


def _zero_axis_exit_confirmation_bars(config: dict[str, Any]) -> int:
    """Return the research-only confirmation length for zero-axis exits.

    The default of one bar is the historical P0 behaviour.  Values greater
    than one delay the sell event until DIF has remained below DEA for the
    requested number of closed bars.
    """
    value = (
        config.get("backtest", {})
        .get("chan_zero_axis", {})
        .get("zero_axis_exit_confirmation_bars", 1)
    )
    return max(int(value), 1)
_BACKTEST_CLIENTS = threading.local()


def _day(value: Any) -> date:
    return pd.Timestamp(value).date()


def prepare_closed_bars(history: pd.DataFrame) -> pd.DataFrame:
    """Return sorted, numeric, closed bars without changing the input."""
    if history is None or history.empty or "datetime" not in history.columns:
        return pd.DataFrame()
    result = history.copy()
    if "is_closed" in result.columns:
        result = result[result["is_closed"].fillna(False).astype(bool)]
    result["datetime"] = pd.to_datetime(result["datetime"], errors="coerce")
    result = result.dropna(subset=["datetime"])
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    if "close" not in result.columns:
        return pd.DataFrame()
    for column in ("open", "high", "low"):
        if column not in result.columns:
            result[column] = result["close"]
        else:
            result[column] = result[column].fillna(result["close"])
    if "volume" not in result.columns:
        result["volume"] = 0.0
    result = result.dropna(subset=["open", "high", "low", "close"])
    return (
        result.sort_values("datetime")
        .drop_duplicates("datetime", keep="last")
        .reset_index(drop=True)
    )


def _symbol_code(symbol: str) -> str:
    raw = str(symbol).upper().split(".")[0]
    digits = "".join(character for character in raw if character.isdigit())
    return digits[-6:].zfill(6)


def price_limit_rate(
    symbol: str,
    trade_day: date,
    st_symbols: set[str] | list[str] | tuple[str, ...] | None = None,
) -> float:
    """Return the configured A-share daily price-limit rate."""
    code = _symbol_code(symbol)
    normalized_st = {_symbol_code(item) for item in (st_symbols or ())}
    if code in normalized_st:
        return 0.05
    if code.startswith(("4", "8", "92")):
        return 0.30
    if code.startswith(("688", "689")):
        return 0.20
    if code.startswith(("300", "301")) and trade_day >= date(2020, 8, 24):
        return 0.20
    return 0.10


def _round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def daily_price_limits(
    symbol: str,
    previous_close: float,
    trade_day: date,
    st_symbols: set[str] | list[str] | tuple[str, ...] | None = None,
) -> tuple[float, float]:
    rate = price_limit_rate(symbol, trade_day, st_symbols)
    return (
        _round_price(previous_close * (1 + rate)),
        _round_price(previous_close * (1 - rate)),
    )


def _default_adjusted_fetcher(
    symbol: str,
    limit: int,
    adjustment: str,
    config: dict[str, Any],
) -> pd.DataFrame:
    """Fetch a complete adjusted daily frame without accepting a short cache."""
    clients = getattr(_BACKTEST_CLIENTS, "clients", None)
    if clients is None:
        clients = {}
        _BACKTEST_CLIENTS.clients = clients
    if adjustment not in clients:
        from data.market_data import MarketDataClient

        client_config = copy.deepcopy(config)
        market_data = client_config.setdefault("market_data", {})
        market_data["adjust"] = adjustment
        market_data["cache_dir"] = str(BASE_DIR / "cache")
        clients[adjustment] = MarketDataClient(client_config)
    client = clients[adjustment]
    if client.provider == "tushare":
        frame = client._fetch_tushare_daily(symbol, limit)
    elif client.provider == "akshare":
        frame = client._fetch_akshare(symbol, "1d", limit)
    elif client.provider == "eastmoney":
        frame = client._fetch_eastmoney(symbol, "1d", limit)
    else:
        frame = client._fetch_tencent(symbol, "1d", limit)
    return frame.tail(limit).reset_index(drop=True)


def load_backtest_history(
    symbol: str,
    *,
    adjustment: str,
    config: dict[str, Any],
    history_bars: int,
    end: date,
    fetch_missing: bool,
    history_dir: Path = HISTORY_DIR,
    fetcher: Callable[[str, int, str], pd.DataFrame] | None = None,
    local_only: bool = False,
) -> tuple[pd.DataFrame, str]:
    """Load one adjustment-consistent history and never fall back to `none`."""
    adjustment = str(adjustment).lower().strip() or "qfq"
    path = history_dir / f"{_symbol_code(symbol)}_{adjustment}.pkl"
    cached = pd.DataFrame()
    if path.exists():
        cached = prepare_closed_bars(pd.read_pickle(path))
        cached_adjustment = str(cached.attrs.get("adjust", adjustment)).lower()
        last_day = _day(cached["datetime"].iloc[-1]) if not cached.empty else None
        fresh_enough = last_day is not None and last_day >= end - timedelta(days=7)
        if local_only and cached_adjustment == adjustment and not cached.empty:
            cached.attrs["adjust"] = adjustment
            return cached.tail(history_bars).reset_index(drop=True), f"cached_{adjustment}_local_only"
        if (
            cached_adjustment == adjustment
            and len(cached) >= history_bars
            and fresh_enough
        ):
            cached.attrs["adjust"] = adjustment
            return cached.tail(history_bars).reset_index(drop=True), f"cached_{adjustment}"
    if not fetch_missing:
        if path.exists():
            raise RuntimeError(
                f"{symbol} {adjustment} history is stale or shorter than {history_bars} bars"
            )
        raise FileNotFoundError(f"missing adjusted history: {path}")
    actual_fetcher = fetcher or (
        lambda code, limit, adjust: _default_adjusted_fetcher(
            code, limit, adjust, config
        )
    )
    fetched = prepare_closed_bars(actual_fetcher(symbol, history_bars, adjustment))
    fetched_adjustment = str(fetched.attrs.get("adjust", adjustment)).lower()
    if fetched.empty or fetched_adjustment != adjustment:
        raise RuntimeError(f"{symbol} returned invalid {adjustment} history")
    if len(fetched) < history_bars:
        raise RuntimeError(
            f"{symbol} returned only {len(fetched)}/{history_bars} {adjustment} bars"
        )
    fetched.attrs["adjust"] = adjustment
    history_dir.mkdir(parents=True, exist_ok=True)
    fetched.to_pickle(path)
    return fetched.tail(history_bars).reset_index(drop=True), f"fetched_{adjustment}"


def load_stock_pool_history(
    symbol: str,
    *,
    config: dict[str, Any],
    history_bars: int,
    end: date,
    local_only: bool = False,
) -> pd.DataFrame:
    """Load unadjusted, signal-day liquidity metrics for stock-pool filtering."""
    if local_only:
        path = HISTORY_DIR / f"{_symbol_code(symbol)}_none.pkl"
        if not path.exists():
            raise FileNotFoundError(f"local stock-pool history not found: {path}")
        frame = prepare_closed_bars(pd.read_pickle(path))
        frame = frame[pd.to_datetime(frame["datetime"]).dt.date <= end]
        return frame.tail(max(int(history_bars), 1)).reset_index(drop=True)
    clients = getattr(_BACKTEST_CLIENTS, "clients", None)
    if clients is None:
        clients = {}
        _BACKTEST_CLIENTS.clients = clients
    key = "stock_pool_history"
    if key not in clients:
        from data.market_data import MarketDataClient

        client_config = copy.deepcopy(config)
        market_data = client_config.setdefault("market_data", {})
        market_data["adjust"] = "none"
        market_data["cache_dir"] = str(BASE_DIR / "cache")
        clients[key] = MarketDataClient(client_config)
    return clients[key].get_stock_pool_history(
        symbol,
        limit=history_bars,
        end=end,
    )


def _confirmation_details(
    enriched: pd.DataFrame,
    index: int,
    macd_config: dict[str, Any],
) -> tuple[list[str], int]:
    """Calculate the same three MACD confirmations used by live scans."""
    if index < 2:
        return [], 0
    frame = enriched.iloc[: index + 1]
    current = frame.iloc[-1]
    previous = frame.iloc[-2]
    volume_period = int(macd_config.get("volume_period", 20))
    average_volume = frame["volume"].rolling(
        volume_period,
        min_periods=max(3, volume_period // 2),
    ).mean().iloc[-1]
    volume_ratio = (
        float(current["volume"] / average_volume)
        if pd.notna(average_volume) and float(average_volume) > 0
        else 0.0
    )
    moderate_volume = bool(
        float(macd_config.get("moderate_volume_min", 1.0))
        <= volume_ratio
        <= float(macd_config.get("moderate_volume_max", 2.0))
    )
    ma5 = frame["close"].rolling(5, min_periods=5).mean()
    ma10 = frame["close"].rolling(10, min_periods=10).mean()
    price_breakout = False
    values = (ma5.iloc[-1], ma10.iloc[-1], ma5.iloc[-2], ma10.iloc[-2])
    if all(pd.notna(value) for value in values):
        price_breakout = bool(
            float(current["close"]) > max(float(ma5.iloc[-1]), float(ma10.iloc[-1]))
            and float(previous["close"]) <= max(float(ma5.iloc[-2]), float(ma10.iloc[-2]))
        )
    hist = frame["hist"]
    hist_expanding = bool(
        len(hist) >= 3
        and all(pd.notna(value) for value in hist.iloc[-3:])
        and 0 < float(hist.iloc[-3]) < float(hist.iloc[-2]) < float(hist.iloc[-1])
    )
    items = [
        label
        for enabled, label in (
            (moderate_volume, "moderate_volume"),
            (price_breakout, "price_breakout_ma5_ma10"),
            (hist_expanding, "hist_expanding"),
        )
        if enabled
    ]
    return items, len(items)


def find_signals(history: pd.DataFrame, config: dict[str, Any]) -> dict[str, list[dict]]:
    """Discover Chan signals and pullback-confirmed MACD entries causally."""
    closed = prepare_closed_bars(history)
    if closed.empty or len(closed) < 60:
        return {"buy": [], "sell": []}
    strategy = config.get("signal_strategy", {})
    chan = strategy.get("chan", {})
    macd_config = strategy.get("macd", {})
    zero_tol = float(macd_config.get("zero_axis_tolerance", 0.005))
    macd_frame = calculate_macd(
        closed["close"],
        fast=int(macd_config.get("fast", 12)),
        slow=int(macd_config.get("slow", 26)),
        signal=int(macd_config.get("signal", 9)),
    )
    enriched = closed.join(macd_frame)
    chan_result = analyze_chan(
        enriched,
        min_bi_bars=int(chan.get("min_bi_bars", 4)),
        divergence_ratio=float(chan.get("divergence_ratio", 0.9)),
    )
    buys: list[dict] = []
    sells: list[dict] = []
    for signal in chan_result.get("signals", []):
        confirmed_day = _day(signal["confirmed_at"])
        record = {
            "day": confirmed_day.isoformat(),
            "signal_type": signal["signal_type"],
            "side": signal["side"],
            "price": float(signal["price"]),
            "confirmed_at": str(signal["confirmed_at"]),
        }
        if signal["side"] == "buy":
            buys.append(record)
        elif signal["signal_type"] in SELL_TYPES:
            sells.append(record)

    backtest_macd = config.get("backtest", {}).get("chan_zero_axis", {})
    allowed_zones = tuple(
        str(item).lower()
        for item in backtest_macd.get("allowed_zones", ["above", "near"])
    )
    confirmation_bars = int(
        backtest_macd.get(
            "cross_window_bars",
            macd_config.get("pullback_confirmation_bars", 5),
        )
    )
    min_confirmations = resolve_min_confirmations(config)
    entries = find_golden_cross_entries(
        closed,
        fast=int(macd_config.get("fast", 12)),
        slow=int(macd_config.get("slow", 26)),
        signal=int(macd_config.get("signal", 9)),
        zero_axis_tolerance=zero_tol,
        confirmation_bars=confirmation_bars,
        allowed_zones=allowed_zones,
    )
    dates = pd.to_datetime(closed["datetime"])
    # Quality filters for macd_above entries (backtest.profile.tighten_*)
    profile = config.get("backtest", {}).get("profile", {})
    require_confirmations = max(int(profile.get("require_confirmations", 0)), 0)
    require_weekly_strong = bool(profile.get("require_weekly_strong", False))
    reject_top_divergence = bool(profile.get("reject_top_divergence", False))
    require_moderate_volume = bool(profile.get("require_moderate_volume", False))
    for entry in entries:
        confirmation_index = int(entry["confirmation_index"])
        confirmation_items, confirmation_count = _confirmation_details(
            enriched, confirmation_index, macd_config
        )
        if confirmation_count < max(min_confirmations, require_confirmations):
            continue
        apply_quality = bool(require_weekly_strong or reject_top_divergence or require_moderate_volume)
        if apply_quality and entry["zone"] in {"above", "near"}:
            if require_weekly_strong and not _weekly_strong(
                enriched, confirmation_index, macd_config
            ):
                continue
            if reject_top_divergence and _top_divergence_risk(
                enriched, confirmation_index, macd_config
            ):
                continue
            if require_moderate_volume and not _moderate_volume_ok(confirmation_items):
                continue
        raw_date = dates.iloc[confirmation_index]
        cross_date = dates.iloc[int(entry["cross_index"])]
        # P5a features: signal-day close only (no lookahead)
        row_close = float(closed["close"].iloc[confirmation_index])
        row_dif = float(macd_frame["dif"].iloc[confirmation_index]) if pd.notna(macd_frame["dif"].iloc[confirmation_index]) else 0.0
        row_dea = float(macd_frame["dea"].iloc[confirmation_index]) if pd.notna(macd_frame["dea"].iloc[confirmation_index]) else 0.0
        p5a_features = None
        p5b_features = None
        if row_close > 0:
            p5a_features = {
                # normalized MACD strength: separation of DIF/DEA relative to price
                "dif_dea_gap": abs(row_dif - row_dea) / row_close,
                # normalized distance from zero axis (higher = farther from zero)
                "zero_dist": abs(row_dif) / row_close,
            }
            # P5b features: trend/risk/volatility at signal-day close (causal).
            close_series = closed["close"]
            high_series = closed["high"] if "high" in closed.columns else close_series
            low_series = closed["low"] if "low" in closed.columns else close_series
            idx = confirmation_index
            c_now = float(close_series.iloc[idx])
            ma60 = float(close_series.iloc[max(0, idx - 59): idx + 1].mean()) if idx >= 59 else float("nan")
            ma250 = float(close_series.iloc[max(0, idx - 249): idx + 1].mean()) if idx >= 249 else float("nan")
            ma60_prev = float(close_series.iloc[max(0, idx - 60 - 4): idx - 4 + 1].mean()) if idx >= 64 else float("nan")
            ma250_prev = float(close_series.iloc[max(0, idx - 250 - 4): idx - 4 + 1].mean()) if idx >= 254 else float("nan")
            prev_close = float(close_series.iloc[idx - 1]) if idx >= 1 else float("nan")
            prev_close_20 = float(close_series.iloc[max(0, idx - 20)]) if idx >= 20 else float("nan")
            tr_vals = []
            for k in range(max(1, idx - 13), idx + 1):
                h = float(high_series.iloc[k])
                l = float(low_series.iloc[k])
                pc = float(close_series.iloc[k - 1])
                tr_vals.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr = float(sum(tr_vals) / len(tr_vals)) if tr_vals else float("nan")
            p5b_features = {
                # distance from MAs (fraction of close); positive = above
                "ma60_dist": (c_now / ma60 - 1.0) if ma60 == ma60 else None,
                "ma250_dist": (c_now / ma250 - 1.0) if ma250 == ma250 else None,
                # 5-bar MA slopes
                "ma60_slope": (ma60 / ma60_prev - 1.0) if ma60 == ma60 and ma60_prev == ma60_prev else None,
                "ma250_slope": (ma250 / ma250_prev - 1.0) if ma250 == ma250 and ma250_prev == ma250_prev else None,
                # volatility / close
                "atr_ratio": (atr / c_now) if atr == atr else None,
                # recent 20-bar return (high-position risk)
                "recent_return": (c_now / prev_close_20 - 1.0) if prev_close_20 == prev_close_20 else None,
            }
        buys.append(
            {
                "day": raw_date.date().isoformat(),
                "cross_day": cross_date.date().isoformat(),
                "signal_type": f"macd_golden_cross_pullback_confirmed_{entry['zone']}",
                "side": "buy",
                "price": float(entry["confirmation_price"]),
                "confirmed_at": str(raw_date),
                "confirmation_bars": int(entry["confirmation_bars"]),
                "confirmation_items": confirmation_items,
                "confirmation_count": confirmation_count,
                "_p5a_features": p5a_features,
                "_p5b_features": p5b_features,
            }
        )

    dif = macd_frame["dif"]
    dea = macd_frame["dea"]
    previous_dif = dif.shift(1)
    previous_dea = dea.shift(1)
    death_cross_confirmation_bars = _zero_axis_exit_confirmation_bars(config)
    for index in range(2, len(closed)):
        if pd.isna(dif.iloc[index]) or pd.isna(dea.iloc[index]):
            continue
        death_cross = bool(
            dif.iloc[index] < dea.iloc[index]
            and previous_dif.iloc[index] >= previous_dea.iloc[index]
        )
        if not death_cross:
            continue
        zone = classify_zero_axis_zone(
            float(dif.iloc[index]),
            float(dea.iloc[index]),
            float(closed["close"].iloc[index]),
            zero_tol,
        )
        if zone == "near":
            confirmation_index = index + death_cross_confirmation_bars - 1
            if confirmation_index >= len(closed):
                # A confirmation that would extend past the available
                # history is not a causal event and must not be backfilled.
                continue
            confirmed = all(
                pd.notna(dif.iloc[probe])
                and pd.notna(dea.iloc[probe])
                and float(dif.iloc[probe]) < float(dea.iloc[probe])
                for probe in range(index, confirmation_index + 1)
            )
            if not confirmed:
                continue
            raw_date = dates.iloc[confirmation_index]
            sells.append(
                {
                    "day": raw_date.date().isoformat(),
                    "signal_type": "zero_axis_death_cross",
                    "side": "sell",
                    "price": float(closed["close"].iloc[index]),
                    "confirmed_at": str(raw_date),
                    "trigger_day": dates.iloc[index].date().isoformat(),
                    "confirmation_bars": death_cross_confirmation_bars,
                }
            )
    return {"buy": buys, "sell": sells}


def _weekly_strong(
    enriched: pd.DataFrame,
    confirmation_index: int,
    macd_config: dict[str, Any],
) -> bool:
    """Return True when the closed week containing the confirmation is in a
    strong MACD regime on the weekly frame (dif > 0 and dif > dea).

    No-lookahead: only bars up to and including the confirmation are used.
    """
    if confirmation_index < 0:
        return False
    frame = enriched.iloc[: confirmation_index + 1].copy()
    if frame.empty or "datetime" not in frame.columns:
        return False
    frame["_dt"] = pd.to_datetime(frame["datetime"])
    frame["_week"] = frame["_dt"].dt.to_period("W").apply(lambda p: p.start_time)
    weekly = frame.groupby("_week", as_index=False)["close"].last()
    if len(weekly) < 60:
        return False
    wmacd = calculate_macd(
        weekly["close"],
        fast=int(macd_config.get("fast", 12)),
        slow=int(macd_config.get("slow", 26)),
        signal=int(macd_config.get("signal", 9)),
    )
    dif = wmacd["dif"]
    dea = wmacd["dea"]
    if pd.isna(dif.iloc[-1]) or pd.isna(dea.iloc[-1]):
        return False
    return bool(float(dif.iloc[-1]) > 0 and float(dif.iloc[-1]) > float(dea.iloc[-1]))


def _top_divergence_risk(
    enriched: pd.DataFrame,
    confirmation_index: int,
    macd_config: dict[str, Any],
) -> bool:
    """Detect a MACD area top divergence using consecutive cross cycles.

    A top divergence exists when price makes a higher high while the positive
    MACD hist area of the latest up-cycle is smaller than the prior one.
    Only data up to the confirmation bar is used (no lookahead).
    """
    if confirmation_index < 3:
        return False
    frame = enriched.iloc[: confirmation_index + 1].reset_index(drop=True)
    dif = frame["dif"]
    dea = frame["dea"]
    hist = frame["hist"].fillna(0.0)
    closes = frame["close"]

    # Collect golden/death cross indices
    crosses: list[tuple[int, str]] = []
    for i in range(1, len(frame)):
        if pd.isna(dif.iloc[i]) or pd.isna(dea.iloc[i]):
            continue
        if dif.iloc[i] > dea.iloc[i] and dif.iloc[i - 1] <= dea.iloc[i - 1]:
            crosses.append((i, "golden"))
        elif dif.iloc[i] < dea.iloc[i] and dif.iloc[i - 1] >= dea.iloc[i - 1]:
            crosses.append((i, "death"))

    # Split into up-cycles: golden→death spans, incomplete final uses confirmation
    cycles: list[dict[str, float]] = []
    for j in range(len(crosses) - 1):
        if crosses[j][1] != "golden":
            continue
        start = crosses[j][0]
        end = crosses[j + 1][0]
        if crosses[j + 1][1] != "death":
            end = confirmation_index
            if end <= start:
                continue
        segment_hist = hist.iloc[start:end]
        positive_area = float(segment_hist[segment_hist > 0].sum())
        if positive_area <= 0:
            continue
        segment_high = float(closes.iloc[start:end].max())
        cycles.append({"high": segment_high, "area": positive_area})

    if len(cycles) < 2:
        return False
    latest = cycles[-1]
    prior = cycles[-2]
    high_raised = bool(latest["high"] > prior["high"])
    area_shrunk = bool(latest["area"] < prior["area"])
    return bool(high_raised and area_shrunk)


def _moderate_volume_ok(confirmation_items: list[str]) -> bool:
    """Return True when a moderate-volume confirmation is present."""
    return "moderate_volume" in (confirmation_items or [])


def _long_ma_period(config: dict[str, Any]) -> int:
    return max(
        int(
            config.get("signal_strategy", {})
            .get("macd", {})
            .get("long_ma_period", config.get("regime", {}).get("ma_long", 250))
        ),
        2,
    )


def build_market_gate(
    index_history: pd.DataFrame,
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build one no-lookahead market decision for each index trading day."""
    closed = prepare_closed_bars(index_history)
    if closed.empty:
        return {}
    gate_settings = resolve_market_gate_settings(config)
    macd_settings = gate_settings["macd"]
    ma_period = _long_ma_period(config)
    slope_bars = max(int(config.get("backtest", {}).get("market_gate_slope_bars", 5)), 1)
    macd = calculate_macd(
        closed["close"],
        fast=macd_settings["fast"],
        slow=macd_settings["slow"],
        signal=macd_settings["signal"],
    )
    ma_long = closed["close"].rolling(ma_period, min_periods=ma_period).mean()
    ma_previous = ma_long.shift(slope_bars)
    trend_gate_enabled = gate_settings["trend_gate_enabled"]
    trend_up_by_day = calculate_trend_gate(
        closed["close"],
        trend_gate_enabled,
        gate_settings["trend_fast_ma"],
        gate_settings["trend_slow_ma"],
    )
    death_cross = (macd["dif"] < macd["dea"]) & (
        macd["dif"].shift(1) >= macd["dea"].shift(1)
    )
    golden_cross = (macd["dif"] > macd["dea"]) & (
        macd["dif"].shift(1) <= macd["dea"].shift(1)
    )
    # Strict MA20/MA10 regime (independent of blocked_by, used for by_regime).
    # Always calculate MA10 so the regime remains valid even when the optional
    # stateful fast latch is disabled.
    strict_regime_by_day = calculate_strict_regime(
        closed["close"], fast_period=10, slow_period=20
    )
    ma10 = closed["close"].rolling(10, min_periods=10).mean()
    ma10_prev = ma10.shift(1)
    # Fast stateful latch: ma10_latch | macd_death_latch | any_latch
    fast_gate_mode = gate_settings["fast_gate_mode"]
    # Latch state: initialized before loop, mutated per-iteration (no future)
    ma10_latch_bear = False
    macd_latch_bear = False
    result: dict[str, dict[str, Any]] = {}
    for index, row in closed.iterrows():
        day_key = _day(row["datetime"]).isoformat()
        current_ma = ma_long.iloc[index]
        previous_ma = ma_previous.iloc[index]
        if pd.isna(current_ma) or pd.isna(previous_ma):
            result[day_key] = {
                "allows_entries": False,
                "regime": "unknown",
                "blocked_by": ["insufficient_history"],
                "above_ma_long": False,
                "ma_long_up": False,
                "death_cross": bool(death_cross.iloc[index]),
                "trend_up": False,
                "ma10_latch_bear": False,
                "macd_latch_bear": False,
            }
            continue
        above_ma = bool(float(row["close"]) > float(current_ma))
        ma_up = bool(float(current_ma) > float(previous_ma))
        ma_down = bool(float(current_ma) < float(previous_ma))
        is_death_cross = bool(death_cross.iloc[index])
        trend_up = bool(trend_up_by_day.iloc[index])
        blocked_by: list[str] = []
        if not above_ma:
            blocked_by.append("below_ma_long")
        if ma_down:
            blocked_by.append("ma_long_down")
        if is_death_cross:
            blocked_by.append("macd_death_cross")
        if not trend_up:
            blocked_by.append("trend_down")
        # Fast gate latch updates (stateful, persists across days)
        if fast_gate_mode in ("ma10_latch", "any_latch"):
            cur_ma10 = ma10.iloc[index]
            pre_ma10 = ma10_prev.iloc[index]
            if pd.notna(cur_ma10) and pd.notna(pre_ma10):
                close_above = float(row["close"]) > float(cur_ma10)
                ma10_rising = float(cur_ma10) > float(pre_ma10)
                if not close_above and not ma10_rising:
                    ma10_latch_bear = True
                elif close_above and ma10_rising:
                    ma10_latch_bear = False
                # otherwise state unchanged
            if ma10_latch_bear:
                blocked_by.append("ma10_latch_bear")
        if fast_gate_mode in ("macd_death_latch", "any_latch"):
            if bool(golden_cross.iloc[index]):
                macd_latch_bear = False
            elif bool(death_cross.iloc[index]):
                macd_latch_bear = True
            # else state unchanged
            if macd_latch_bear:
                blocked_by.append("macd_latch_bear")
        strict_regime = str(strict_regime_by_day.iloc[index])
        result[day_key] = {
            "allows_entries": not blocked_by,
            "regime": strict_regime,
            "blocked_by": blocked_by,
            "above_ma_long": above_ma,
            "ma_long_up": ma_up,
            "death_cross": is_death_cross,
            "trend_up": trend_up,
            "ma10_latch_bear": ma10_latch_bear,
            "macd_latch_bear": macd_latch_bear,
            "ma_long": float(current_ma),
            "close": float(row["close"]),
        }
    return result


def apply_stock_position_gate(
    closed: pd.DataFrame,
    events: dict[str, list[dict]],
    config: dict[str, Any],
) -> tuple[dict[str, list[dict]], Counter]:
    """Apply the live candidate rule: above a rising long moving average."""
    if not config.get("entry_filters", {}).get("position_gate_enabled", False):
        return events, Counter()
    ma_period = _long_ma_period(config)
    slope_bars = max(int(config.get("backtest", {}).get("market_gate_slope_bars", 5)), 1)
    ma_long = closed["close"].rolling(ma_period, min_periods=ma_period).mean()
    ma_previous = ma_long.shift(slope_bars)
    index_by_day = {
        _day(value).isoformat(): index for index, value in enumerate(closed["datetime"])
    }
    accepted: list[dict] = []
    skipped: Counter = Counter()
    for event in events.get("buy", []):
        index = index_by_day.get(str(event["day"]))
        if index is None or pd.isna(ma_long.iloc[index]) or pd.isna(ma_previous.iloc[index]):
            skipped["position_gate_insufficient_history"] += 1
            continue
        if not (
            float(closed.iloc[index]["close"]) > float(ma_long.iloc[index])
            and float(ma_long.iloc[index]) > float(ma_previous.iloc[index])
        ):
            skipped["position_gate_blocked"] += 1
            continue
        accepted.append(event)
    return {"buy": accepted, "sell": list(events.get("sell", []))}, skipped


def next_bar_index(bars: pd.DataFrame, dates: list[date], day: date) -> int | None:
    del bars
    for index, bar_day in enumerate(dates):
        if bar_day > day:
            return index
    return None


def _sell_events_by_index(sells: list[dict], dates: list[date]) -> dict[int, str]:
    index_by_day = {bar_day: index for index, bar_day in enumerate(dates)}
    rank = {
        name: index
        for index, name in enumerate(("sell_1", "sell_2", "sell_3", "zero_axis_death_cross"))
    }
    grouped: dict[int, list[str]] = defaultdict(list)
    for event in sells:
        index = index_by_day.get(date.fromisoformat(str(event["day"])))
        if index is not None:
            grouped[index].append(str(event.get("signal_type") or "chan_sell"))
    return {
        index: sorted(types, key=lambda name: (rank.get(name, 999), name))[0]
        for index, types in grouped.items()
    }


def _risk_trigger(
    bar: pd.Series,
    stop_price: float | None,
    take_price: float | None,
    conflict: str,
) -> tuple[str, float, str] | None:
    open_price = float(bar["open"])
    stop_hit = stop_price is not None and float(bar["low"]) <= stop_price
    take_hit = take_price is not None and float(bar["high"]) >= take_price
    if not stop_hit and not take_hit:
        return None
    if stop_hit and take_hit:
        reason = "take_profit" if conflict == "take_first" else "stop_loss"
    elif stop_hit:
        reason = "stop_loss"
    else:
        reason = "take_profit"
    if reason == "stop_loss":
        assert stop_price is not None
        return (reason, open_price, "open") if open_price <= stop_price else (reason, float(stop_price), "intraday")
    assert take_price is not None
    return (reason, open_price, "open") if open_price >= take_price else (reason, float(take_price), "intraday")


def _buy_cash(price: float, quantity: int, execution: dict[str, Any]) -> dict[str, float]:
    notional = float(price) * int(quantity)
    commission = max(
        notional * execution["commission_pct"],
        execution["minimum_commission"],
    )
    slippage = notional * execution["slippage_pct"]
    return {
        "notional": notional,
        "commission": commission,
        "slippage": slippage,
        "total": notional + commission + slippage,
    }


def _sell_cash(price: float, quantity: int, execution: dict[str, Any]) -> dict[str, float]:
    notional = float(price) * int(quantity)
    commission = max(
        notional * execution["commission_pct"],
        execution["minimum_commission"],
    )
    stamp_tax = notional * execution["stamp_tax_pct"]
    slippage = notional * execution["slippage_pct"]
    return {
        "notional": notional,
        "commission": commission,
        "stamp_tax": stamp_tax,
        "slippage": slippage,
        "total": notional - commission - stamp_tax - slippage,
    }


def _bar_price_limits(
    symbol: str,
    closed: pd.DataFrame,
    index: int,
    execution: dict[str, Any],
) -> tuple[float, float] | None:
    if execution["price_limit_model"] != "conservative" or index <= 0:
        return None
    return daily_price_limits(
        symbol,
        float(closed.iloc[index - 1]["close"]),
        _day(closed.iloc[index]["datetime"]),
        execution["st_symbols"],
    )


def _resolve_sell_fill(
    symbol: str,
    closed: pd.DataFrame,
    index: int,
    desired_price: float,
    desired_session: str,
    execution: dict[str, Any],
) -> tuple[float, str] | None:
    limits = _bar_price_limits(symbol, closed, index, execution)
    if limits is None:
        return float(desired_price), desired_session
    _, limit_down = limits
    bar = closed.iloc[index]
    epsilon = 0.0001
    opens_at_limit_down = float(bar["open"]) <= limit_down + epsilon
    locked_limit_down = opens_at_limit_down and float(bar["high"]) <= limit_down + epsilon
    if locked_limit_down:
        return None
    if opens_at_limit_down:
        return limit_down, "intraday"
    return max(float(desired_price), limit_down), desired_session


def _execution_values(costs: dict[str, Any]) -> dict[str, Any]:
    risk = costs.get("risk", {})
    chan_zero_axis = costs.get("chan_zero_axis", {}) or {}
    max_holding_bars = max(int(chan_zero_axis.get("max_holding_bars", 40)), 1)
    timeout_exit_mode = str(
        chan_zero_axis.get("timeout_exit_mode", "fixed")
    ).lower()
    if timeout_exit_mode not in {"fixed", "ma_break"}:
        raise ValueError(
            "timeout_exit_mode must be 'fixed' or 'ma_break', "
            f"got {timeout_exit_mode!r}"
        )
    stop_loss = costs.get("stop_loss_pct", risk.get("stop_loss_pct"))
    take_profit = costs.get(
        "take_profit_pct",
        costs.get("stop_profit_pct", risk.get("stop_profit_pct")),
    )
    return {
        "commission_pct": float(costs.get("commission_pct", 0.0003)),
        "minimum_commission": max(float(costs.get("minimum_commission", 5.0)), 0.0),
        "stamp_tax_pct": float(costs.get("stamp_tax_pct", 0.001)),
        "slippage_pct": float(costs.get("slippage_pct", 0.0005)),
        "lot_size": max(int(costs.get("lot_size", 100)), 1),
        "t_plus_one": bool(costs.get("t_plus_one", True)),
        "price_limit_model": str(costs.get("price_limit_model", "conservative")).lower(),
        "st_symbols": tuple(str(item) for item in costs.get("st_symbols", [])),
        "stop_loss_pct": None if stop_loss is None else max(float(stop_loss), 0.0),
        "take_profit_pct": None if take_profit is None else max(float(take_profit), 0.0),
        "intrabar_conflict": str(costs.get("intrabar_conflict", "stop_first")),
        "max_holding_bars": max_holding_bars,
        "timeout_exit_mode": timeout_exit_mode,
        "timeout_ma_period": max(
            int(chan_zero_axis.get("timeout_ma_period", 20)), 2
        ),
        "timeout_ma_confirm_bars": max(
            int(chan_zero_axis.get("timeout_ma_confirm_bars", 1)), 1
        ),
        "timeout_hard_cap_bars": max(
            int(
                chan_zero_axis.get(
                    "timeout_hard_cap_bars",
                    (
                        max_holding_bars + 20
                        if timeout_exit_mode == "ma_break"
                        else max_holding_bars
                    ),
                )
            ),
            max_holding_bars,
            1,
        ),
    }


def _timeout_ma_break_confirmed(
    closed: pd.DataFrame,
    index: int,
    period: int,
    confirm_bars: int,
    start_index: int = 0,
) -> bool:
    """Return whether the close is below its MA for consecutive closed bars."""
    first_probe = index - confirm_bars + 1
    if index < period - 1 or first_probe < max(start_index, 0):
        return False
    closes = pd.to_numeric(closed["close"], errors="coerce")
    ma = closes.rolling(period, min_periods=period).mean()
    for probe in range(first_probe, index + 1):
        if pd.isna(closes.iloc[probe]) or pd.isna(ma.iloc[probe]):
            return False
        if float(closes.iloc[probe]) >= float(ma.iloc[probe]):
            return False
    return True


def _build_trade(
    symbol: str,
    closed: pd.DataFrame,
    dates: list[date],
    buy: dict,
    entry_idx: int,
    exit_idx: int,
    exit_price: float,
    exit_reason: str,
    exit_trigger_idx: int,
    exit_session: str,
    execution: dict[str, Any],
    market_context: dict[str, Any] | None,
    price_limit_deferred_bars: int = 0,
) -> dict[str, Any]:
    entry_price = float(closed.iloc[entry_idx]["open"])
    reference_quantity = execution["lot_size"]
    entry_cash = _buy_cash(entry_price, reference_quantity, execution)
    exit_cash = _sell_cash(exit_price, reference_quantity, execution)
    entry_unit_cost = entry_cash["total"] / reference_quantity
    exit_unit_gain = exit_cash["total"] / reference_quantity
    pnl_pct = (exit_cash["total"] - entry_cash["total"]) / entry_cash["total"] * 100.0
    marks = {
        dates[index].isoformat(): float(closed.iloc[index]["close"])
        for index in range(entry_idx, exit_idx + 1)
    }
    return {
        "symbol": symbol,
        "signal_day": str(buy["day"]),
        "entry_day": dates[entry_idx].isoformat(),
        "exit_trigger_day": dates[exit_trigger_idx].isoformat(),
        "exit_day": dates[exit_idx].isoformat(),
        "signal_type": str(buy["signal_type"]),
        "signal_types": [str(buy["signal_type"])],
        "cross_day": buy.get("cross_day"),
        "confirmation_bars": buy.get("confirmation_bars"),
        "confirmation_count": buy.get("confirmation_count"),
        "confirmation_items": buy.get("confirmation_items"),
        "_p5a_features": buy.get("_p5a_features"),
        "_p5b_features": buy.get("_p5b_features"),
        "stock_pool_metrics": buy.get("stock_pool_metrics"),
        "stock_pool_warnings": buy.get("stock_pool_warnings", []),
        "fundamental_status": buy.get("fundamental_status"),
        "fundamental_metrics": buy.get("fundamental_metrics"),
        "fundamental_warnings": buy.get("fundamental_warnings", []),
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "entry_unit_cost": entry_unit_cost,
        "exit_unit_gain": exit_unit_gain,
        "reference_quantity": reference_quantity,
        "entry_commission_cash": round(entry_cash["commission"], 2),
        "exit_commission_cash": round(exit_cash["commission"], 2),
        "stamp_tax_cash": round(exit_cash["stamp_tax"], 2),
        "slippage_cash": round(entry_cash["slippage"] + exit_cash["slippage"], 2),
        "pnl_pct": round(pnl_pct, 4),
        "holding_days": max((dates[exit_idx] - dates[entry_idx]).days, 0),
        "holding_bars": max(exit_idx - entry_idx, 0),
        "exit_reason": exit_reason,
        "exit_session": exit_session,
        "price_limit_deferred_bars": price_limit_deferred_bars,
        "market_context": market_context,
        "_mark_prices": marks,
    }


def simulate_single_trade(
    symbol: str,
    closed: pd.DataFrame,
    dates: list[date],
    buy: dict,
    sells_by_index: dict[int, str],
    costs: dict[str, Any],
    *,
    allow_incomplete: bool = False,
    market_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Simulate one signal without considering any other position."""
    execution = _execution_values(costs)
    signal_day = date.fromisoformat(str(buy["day"]))
    entry_idx = next_bar_index(closed, dates, signal_day)
    if entry_idx is None:
        return None, "missing_entry_bar"
    entry_price = float(closed.iloc[entry_idx]["open"])
    if entry_price <= 0:
        return None, "invalid_entry_price"
    entry_limits = _bar_price_limits(symbol, closed, entry_idx, execution)
    if entry_limits is not None and entry_price >= entry_limits[0] - 0.0001:
        return None, "entry_limit_up"
    timeout_mode = execution["timeout_exit_mode"]
    timeout_idx = entry_idx + execution["max_holding_bars"]
    hard_cap_idx = entry_idx + (
        execution["timeout_hard_cap_bars"]
        if timeout_mode == "ma_break"
        else execution["max_holding_bars"]
    )
    if hard_cap_idx >= len(closed) and not allow_incomplete:
        return None, "incomplete_horizon"
    stop_price = (
        entry_price * (1 - execution["stop_loss_pct"])
        if execution["stop_loss_pct"] is not None and execution["stop_loss_pct"] > 0
        else None
    )
    take_price = (
        entry_price * (1 + execution["take_profit_pct"])
        if execution["take_profit_pct"] is not None and execution["take_profit_pct"] > 0
        else None
    )
    pending_exit: tuple[str, int] | None = None
    price_limit_deferred_bars = 0
    for index in range(entry_idx, len(closed)):
        if index > entry_idx and pending_exit is not None:
            reason, trigger_idx = pending_exit
            desired_price = float(closed.iloc[index]["open"])
            if desired_price <= 0:
                return None, "invalid_exit_price"
            resolved = _resolve_sell_fill(
                symbol, closed, index, desired_price, "open", execution
            )
            if resolved is None:
                price_limit_deferred_bars += 1
                continue
            exit_price, exit_session = resolved
            return _build_trade(
                symbol, closed, dates, buy, entry_idx, index, exit_price, reason,
                trigger_idx, exit_session, execution, market_context,
                price_limit_deferred_bars
            ), None
        if timeout_mode == "fixed" and index == timeout_idx:
            desired_price = float(closed.iloc[index]["open"])
            if desired_price <= 0:
                return None, "invalid_exit_price"
            resolved = _resolve_sell_fill(
                symbol, closed, index, desired_price, "open", execution
            )
            if resolved is None:
                pending_exit = ("timeout", index)
                price_limit_deferred_bars += 1
                continue
            exit_price, exit_session = resolved
            return _build_trade(
                symbol, closed, dates, buy, entry_idx, index, exit_price, "timeout",
                index, exit_session, execution, market_context,
                price_limit_deferred_bars
            ), None
        if timeout_mode == "fixed" and index > timeout_idx:
            continue
        if timeout_mode == "ma_break" and index == hard_cap_idx:
            desired_price = float(closed.iloc[index]["open"])
            if desired_price <= 0:
                return None, "invalid_exit_price"
            resolved = _resolve_sell_fill(
                symbol, closed, index, desired_price, "open", execution
            )
            if resolved is None:
                pending_exit = ("timeout_hard_cap", index)
                price_limit_deferred_bars += 1
                continue
            exit_price, exit_session = resolved
            return _build_trade(
                symbol, closed, dates, buy, entry_idx, index, exit_price,
                "timeout_hard_cap", index, exit_session, execution, market_context,
                price_limit_deferred_bars
            ), None
        if timeout_mode == "ma_break" and index > hard_cap_idx:
            continue
        risk = _risk_trigger(
            closed.iloc[index], stop_price, take_price, execution["intrabar_conflict"]
        )
        if risk is not None:
            reason, risk_price, session = risk
            if index == entry_idx and execution["t_plus_one"]:
                pending_exit = (reason, index)
            else:
                resolved = _resolve_sell_fill(
                    symbol, closed, index, risk_price, session, execution
                )
                if resolved is None:
                    pending_exit = (reason, index)
                    price_limit_deferred_bars += 1
                    continue
                exit_price, exit_session = resolved
                return _build_trade(
                    symbol, closed, dates, buy, entry_idx, index, exit_price, reason,
                    index, exit_session, execution, market_context,
                    price_limit_deferred_bars
                ), None
        if pending_exit is None and index in sells_by_index and index >= entry_idx:
            pending_exit = (sells_by_index[index], index)
        if (
            timeout_mode == "ma_break"
            and pending_exit is None
            and index >= timeout_idx
            and _timeout_ma_break_confirmed(
                closed,
                index,
                execution["timeout_ma_period"],
                execution["timeout_ma_confirm_bars"],
                timeout_idx,
            )
        ):
            # The condition is observed at today's close and therefore fills
            # at the next trading day's open via pending_exit.
            pending_exit = ("timeout_ma_break", index)
    if pending_exit is not None:
        return None, "unresolved_limit_down"
    if not allow_incomplete:
        return None, "incomplete_horizon"
    exit_idx = len(closed) - 1
    exit_price = float(closed.iloc[exit_idx]["close"])
    if exit_idx < entry_idx or exit_price <= 0:
        return None, "missing_exit_bar"
    resolved = _resolve_sell_fill(
        symbol, closed, exit_idx, exit_price, "close", execution
    )
    if resolved is None:
        return None, "unresolved_limit_down"
    exit_price, exit_session = resolved
    return _build_trade(
        symbol, closed, dates, buy, entry_idx, exit_idx, exit_price, "window_end",
        exit_idx, exit_session, execution, market_context,
        price_limit_deferred_bars
    ), None


def simulate_signal_mode(
    symbol: str,
    closed: pd.DataFrame,
    events: dict[str, list[dict]],
    start: date,
    end: date,
    costs: dict[str, Any],
    *,
    market_gate: dict[str, dict[str, Any]] | None = None,
    market_gate_enabled: bool = False,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    """Independently simulate every buy signal, including overlapping ones."""
    closed = prepare_closed_bars(closed)
    dates = [_day(value) for value in closed["datetime"]]
    sells_by_index = _sell_events_by_index(events.get("sell", []), dates)
    trades: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    buys = sorted(
        events.get("buy", []),
        key=lambda item: (str(item["day"]), str(item.get("signal_type", ""))),
    )
    for buy in buys:
        signal_day = date.fromisoformat(str(buy["day"]))
        if signal_day < start or signal_day > end:
            skipped["outside_window"] += 1
            continue
        context = None
        if market_gate_enabled:
            context = (market_gate or {}).get(signal_day.isoformat())
            if context is None:
                skipped["market_gate_missing_day"] += 1
                continue
            if not context.get("allows_entries", False):
                skipped["market_gate_blocked"] += 1
                continue
        trade, reason = simulate_single_trade(
            symbol, closed, dates, buy, sells_by_index, costs,
            allow_incomplete=allow_incomplete, market_context=context
        )
        if trade is None:
            skipped[str(reason or "unknown")] += 1
        else:
            trades.append(trade)
    return {"trades": trades, "skipped": dict(skipped)}


def simulate(
    symbol: str,
    closed: pd.DataFrame,
    events: dict[str, list[dict]],
    start: date,
    end: date,
    costs: dict[str, Any],
) -> list[dict]:
    """Compatibility wrapper for callers that expected a trade list."""
    return simulate_signal_mode(symbol, closed, events, start, end, costs)["trades"]


def _public_trade(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in trade.items()
        if not key.startswith("_") and key not in {"entry_unit_cost", "exit_unit_gain"}
    }


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize signal returns without fixed-share price weighting."""
    if not trades:
        return {
            "count": 0,
            "win_rate": None,
            "avg_pnl_pct": None,
            "median_pnl_pct": None,
            "gross_profit_pct": 0.0,
            "gross_loss_pct": 0.0,
            "avg_win_loss_ratio": None,
            "profit_factor": None,
            "total_pnl_cash": 0.0,
            "cash_profit_factor": None,
        }
    pnls = [float(trade["pnl_pct"]) for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit_pct = sum(value for value in pnls if value > 0)
    gross_loss_pct = sum(value for value in pnls if value < 0)
    cash_values = [float(trade.get("pnl_cash", 0.0)) for trade in trades]
    gross_profit_cash = sum(value for value in cash_values if value > 0)
    gross_loss_cash = sum(value for value in cash_values if value < 0)
    return {
        "count": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 2),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 2),
        "median_pnl_pct": round(float(pd.Series(pnls).median()), 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "gross_profit_pct": round(gross_profit_pct, 2),
        "gross_loss_pct": round(gross_loss_pct, 2),
        "avg_win_loss_ratio": round(abs(avg_win / avg_loss), 2) if avg_loss else None,
        "profit_factor": round(gross_profit_pct / abs(gross_loss_pct), 2) if gross_loss_pct else None,
        "total_pnl_cash": round(sum(cash_values), 2),
        "cash_profit_factor": round(gross_profit_cash / abs(gross_loss_cash), 2) if gross_loss_cash else None,
        "avg_holding_days": round(
            sum(float(trade["holding_days"]) for trade in trades) / len(trades), 1
        ),
    }


def summarize_holding_periods(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Summarize trades by stable holding-period buckets for audit reports."""

    buckets = (
        (0, 5, "0-5d"),
        (6, 20, "6-20d"),
        (21, 40, "21-40d"),
        (41, None, "41d+"),
    )
    result: dict[str, dict[str, Any]] = {}
    for lower, upper, name in buckets:
        selected = []
        for trade in trades:
            try:
                days = float(trade.get("holding_days", 0))
            except (TypeError, ValueError):
                continue
            if days >= lower and (upper is None or days <= upper):
                selected.append(trade)
        result[name] = summarize(selected)
    return result


_ZONE_SCORE = {
    "macd_golden_cross_pullback_confirmed_above": 300,
    "macd_golden_cross_pullback_confirmed_near": 200,
    "buy_1": 100,
    "buy_2": 90,
    "buy_3": 80,
}


_P5A_VARIANTS = {"C", "G", "Z", "CG", "CZ", "CGZ"}


# P5b: trend/risk/volatility feature groups. Each mode maps to a set of
# _p5b_features keys plus a direction sign (+1 high better, -1 low better).
# Directions come from the three-window feature probe (cross-consistent):
#   atr_ratio high better (strong breakouts), ma60/ma250 distance LOW better
#   (close to MAs = not overheated), slopes weakly negative, recent_return
#   directionless (dropped from score, kept in risk diagnostics).
_P5B_MODES: dict[str, dict[str, float]] = {
    "P5b-MA60": {"ma60_dist": -1.0, "ma60_slope": -1.0},
    "P5b-MA250": {"ma250_dist": -1.0, "ma250_slope": -1.0},
    "P5b-ATR": {"atr_ratio": 1.0},
    "P5b-Risk": {"recent_return": 0.0},  # directionless; kept as observation
    "P5b-MA": {"ma60_dist": -1.0, "ma60_slope": -1.0, "ma250_dist": -1.0, "ma250_slope": -1.0},
    "P5b-All": {
        "ma60_dist": -1.0, "ma60_slope": -1.0, "ma250_dist": -1.0, "ma250_slope": -1.0,
        "atr_ratio": 1.0,
    },
}


def _is_p5b_mode(mode: str) -> bool:
    return mode in _P5B_MODES


def _p5b_mode_directions(mode: str) -> dict[str, float]:
    return _P5B_MODES.get(mode, {})


def _apply_p5b_cross_sectional_ranks(
    candidates: list[dict[str, Any]],
) -> None:
    """Compute per entry-day percentile ranks for P5b features (in-place).

    Each candidate gets ``_p5b_pct`` = {feature_key: rank 0..1}, where rank is
    the fraction of same-day candidates strictly below. Direction handling is
    left to the score function (sign applied there).
    """
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        entry_day = candidate.get("entry_day") or candidate.get("signal_day")
        if entry_day:
            by_day[str(entry_day)].append(candidate)
    for day, group in by_day.items():
        if len(group) < 2:
            for candidate in group:
                candidate["_p5b_pct"] = {key: 0.5 for key in (
                    "ma60_dist", "ma60_slope", "ma250_dist", "ma250_slope",
                    "atr_ratio", "recent_return")}
            continue
        for key in (
            "ma60_dist", "ma60_slope", "ma250_dist", "ma250_slope",
            "atr_ratio", "recent_return",
        ):
            valid_values = []
            candidate_values = []
            for candidate in group:
                features = candidate.get("_p5b_features") or {}
                value = features.get(key)
                numeric_value = None
                if value is not None:
                    try:
                        numeric_value = float(value)
                    except (TypeError, ValueError):
                        numeric_value = None
                if numeric_value is not None and math.isfinite(numeric_value):
                    valid_values.append((candidate, numeric_value))
                    candidate_values.append((candidate, numeric_value))
                else:
                    # Missing features carry no ranking information. Keep them
                    # neutral instead of treating them as the lowest value.
                    candidate_values.append((candidate, None))
            denominator = max(len(valid_values) - 1, 1)
            for candidate, value in candidate_values:
                if value is None or len(valid_values) < 2:
                    rank = 0.5
                else:
                    rank = sum(1 for _, other in valid_values if other < value) / denominator
                candidate.setdefault("_p5b_pct", {})[key] = rank


def _p5b_mode_score(
    candidate: dict[str, Any],
    mode: str,
    rank: dict[str, int],
) -> float:
    """P5b score: base + signed percentile contributions of the mode's features.
    buy_1/2/3 have no P5b features; fall back to base score."""
    signal_type = str(candidate.get("signal_type", ""))
    base = float(_ZONE_SCORE.get(signal_type, rank.get(signal_type, 999) * 10.0))
    confirm_count = float(candidate.get("confirmation_count") or 0)
    if not signal_type.startswith("macd"):
        return base + confirm_count * 10.0
    directions = _p5b_mode_directions(mode)
    pct = candidate.get("_p5b_pct") or {}
    score = base + confirm_count * 10.0
    for key, sign in directions.items():
        r = float(pct.get(key, 0.5))
        # rank 0..1; apply sign so higher score = better under that direction
        contribution = (r - 0.5) * 2.0 * sign
        score += contribution * 20.0
    return score


def _is_p5a_variant(mode: str) -> bool:
    return mode == "P5a" or (mode.startswith("P5a-") and mode[4:] in _P5A_VARIANTS)


def _p5a_variant_components(mode: str) -> set[str]:
    """Return the feature components enabled by a P5a variant mode."""
    if mode == "P5a":
        return {"C", "G", "Z"}
    suffix = mode[4:]
    return set(suffix)


def _apply_p5a_cross_sectional_ranks(
    candidates: list[dict[str, Any]],
) -> None:
    """Replace raw P5a feature values with same-day cross-sectional percentile
    ranks (0..1), computed per entry day across all symbols' candidates.

    In-place: each candidate gets ``_p5a_pct`` = {"C": .., "G": .., "Z": ..}.
    Percentile uses a high-is-better formulation over the raw feature.
    For ``dif_dea_gap`` and ``confirmation_count`` higher is better; for
    ``zero_dist`` lower is better (nearer the zero axis). Rank = fraction of
    candidates strictly below within the same entry-day group.
    """
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        entry_day = candidate.get("entry_day") or candidate.get("signal_day")
        if entry_day:
            by_day[str(entry_day)].append(candidate)
    for day, group in by_day.items():
        if len(group) < 2:
            for candidate in group:
                candidate["_p5a_pct"] = {"C": 0.5, "G": 0.5, "Z": 0.5}
            continue
        for key, better, raw in (
            ("C", True, "confirmation_count"),
            ("G", True, "dif_dea_gap"),
            ("Z", False, "zero_dist"),
        ):
            values = []
            for candidate in group:
                if key == "C":
                    value = float(candidate.get("confirmation_count") or 0)
                else:
                    features = candidate.get("_p5a_features") or {}
                    value = float(features.get(raw) or 0.0)
                values.append((candidate, value))
            # high-is-better rank: fraction strictly below
            for candidate, value in values:
                below = sum(1 for _, other in values if (other < value) if not (other == value and candidate is candidate))
                rank = below / (len(group) - 1) if len(group) > 1 else 0.5
                if not better:
                    rank = 1.0 - rank
                candidate.setdefault("_p5a_pct", {})[key] = rank


def _p5a_variant_score(
    candidate: dict[str, Any],
    mode: str,
    rank: dict[str, int],
    *,
    use_percentile: bool,
) -> float:
    """P5a family score. P5a/C/G/Z/CG/CZ/CGZ with optional cross-sectional
    percentile normalization. Falls back to the base P1 score for non-MACD
    signal types (buy_1/2/3 carry no MACD features)."""
    signal_type = str(candidate.get("signal_type", ""))
    base = float(_ZONE_SCORE.get(signal_type, rank.get(signal_type, 999) * 10.0))
    confirm_count = float(candidate.get("confirmation_count") or 0)
    if not signal_type.startswith("macd"):
        # buy_1/2/3 have no MACD features; keep the P1 base score
        return base + confirm_count * 10.0
    components = _p5a_variant_components(mode)
    if use_percentile:
        pct = candidate.get("_p5a_pct") or {}
        score = base
        if "C" in components:
            score += float(pct.get("C", 0.5)) * 30.0
        if "G" in components:
            score += float(pct.get("G", 0.5)) * 30.0
        if "Z" in components:
            score += float(pct.get("Z", 0.5)) * 20.0
        return score
    features = candidate.get("_p5a_features") or {}
    gap = float(features.get("dif_dea_gap") or 0.0)
    zdist = float(features.get("zero_dist") or 0.0)
    score = base + confirm_count * 10.0
    if "G" in components:
        score += min(gap / 0.05, 1.0) * 30.0
    if "Z" in components:
        score += (1.0 - min(zdist / 0.10, 1.0)) * 20.0
    return score


def _candidate_score(
    candidate: dict[str, Any],
    mode: str,
    rank: dict[str, int],
) -> float:
    """Compute an orderable score using only information known at entry time."""
    if _is_p5b_mode(mode):
        return _p5b_mode_score(candidate, mode, rank)
    if _is_p5a_variant(mode):
        # P5a family: bare "P5a" uses raw clip normalization; P5a-C/G/Z/CG/CZ/CGZ
        # use cross-sectional percentile ranks (set in _merge_portfolio_candidates).
        use_pct = mode != "P5a"
        return _p5a_variant_score(candidate, mode, rank, use_percentile=use_pct)
    signal_type = str(candidate.get("signal_type", ""))
    base = float(_ZONE_SCORE.get(signal_type, rank.get(signal_type, 999) * 10.0))
    confirm_count = float(candidate.get("confirmation_count") or 0)
    total = base + confirm_count * 10.0
    if mode in {"P2", "P3"}:
        bars = candidate.get("confirmation_bars")
        freshness = 0.0
        if isinstance(bars, (int, float)) and bars is not None:
            freshness = max(0.0, 5.0 - float(bars))
        total += freshness * 5.0
    if mode == "P3":
        context = candidate.get("market_context") or {}
        risk = 0.0
        ma_long = context.get("ma_long")
        close = context.get("close")
        if isinstance(ma_long, (int, float)) and isinstance(close, (int, float)) and ma_long:
            distance = abs(float(close) - float(ma_long)) / float(ma_long)
            if distance > 0.35:
                risk += 50.0
        if context.get("death_cross"):
            risk += 30.0
        if context.get("ma10_latch_bear") or context.get("macd_latch_bear"):
            risk += 40.0
        total -= risk
    return total


def _merge_portfolio_candidates(
    candidates: list[dict[str, Any]],
    signal_priority: list[str] | tuple[str, ...],
    seed: int | None = None,
    score_mode: str = "P0",
    tie_break: str = "symbol_asc",
) -> list[dict[str, Any]]:
    rank = {name: index for index, name in enumerate(signal_priority)}
    if _is_p5b_mode(score_mode):
        _apply_p5b_cross_sectional_ranks(candidates)
    elif _is_p5a_variant(score_mode) and score_mode != "P5a":
        _apply_p5a_cross_sectional_ranks(candidates)
    rng = None
    if seed is not None:
        import random as _random

        rng = _random.Random(seed)

    def order_bucket(bucket: list[dict[str, Any]], score_key) -> list[dict[str, Any]]:
        """Order candidates within an equal-priority / equal-score bucket."""
        if tie_break == "symbol_asc":
            return sorted(bucket, key=lambda item: (str(item["symbol"]), str(item["signal_day"])))
        if tie_break == "symbol_desc":
            return sorted(
                bucket,
                key=lambda item: (str(item["symbol"]), str(item["signal_day"])),
                reverse=True,
            )
        if tie_break == "hash":
            import hashlib

            return sorted(
                bucket,
                key=lambda item: hashlib.sha256(
                    f"{item['symbol']}|{item['signal_day']}|fixed-seed".encode()
                ).hexdigest(),
            )
        if tie_break == "rotate":
            rotated = sorted(
                bucket,
                key=lambda item: (str(item["symbol"]), str(item["signal_day"])),
            )
            if rotated:
                day = str(bucket[0].get("entry_day", ""))
                offset = sum(ord(char) for char in day) % len(rotated)
                rotated = rotated[offset:] + rotated[:offset]
            return rotated
        # random (uses rng when seeded; deterministic fallback symbol_asc)
        if rng is not None:
            shuffled = list(bucket)
            rng.shuffle(shuffled)
            return shuffled
        return sorted(bucket, key=lambda item: (str(item["symbol"]), str(item["signal_day"])))

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        grouped[(str(candidate["symbol"]), str(candidate["signal_day"]))].append(candidate)
    merged: list[dict[str, Any]] = []
    for items in grouped.values():
        if score_mode == "P0":
            ordered = sorted(
                items,
                key=lambda item: (
                    rank.get(str(item["signal_type"]), 999),
                    str(item["signal_type"]),
                ),
            )
            # Reorder within equal-priority buckets by tie_break
            from itertools import groupby

            ordered = [
                candidate
                for _, bucket in groupby(
                    ordered,
                    key=lambda item: (
                        rank.get(str(item["signal_type"]), 999),
                        str(item["signal_type"]),
                    ),
                )
                for candidate in order_bucket(list(bucket), None)
            ]
        else:
            ordered = sorted(
                items,
                key=lambda item: (
                    -_candidate_score(item, score_mode, rank),
                    str(item["symbol"]),
                ),
            )
            from itertools import groupby

            ordered = [
                candidate
                for _, bucket in groupby(
                    ordered,
                    key=lambda item: -_candidate_score(item, score_mode, rank),
                )
                for candidate in order_bucket(list(bucket), None)
            ]
        primary = dict(ordered[0])
        signal_types = sorted(
            {str(item["signal_type"]) for item in items},
            key=lambda name: (rank.get(name, 999), name),
        )
        primary["signal_types"] = signal_types
        primary["signal_type"] = signal_types[0]
        primary["_rank_score"] = _candidate_score(primary, score_mode, rank)
        merged.append(primary)
    if score_mode == "P0":
        order_key = lambda item: (
            str(item["entry_day"]),
            rank.get(str(item["signal_type"]), 999),
        )
        sort_key = lambda item: (
            str(item["entry_day"]),
            rank.get(str(item["signal_type"]), 999),
            str(item["symbol"]),
            str(item["signal_day"]),
        )
        final_order = sorted(merged, key=sort_key)
    else:
        order_key = lambda item: (
            str(item["entry_day"]),
            -_candidate_score(item, score_mode, rank),
        )
        final_order = sorted(
            merged,
            key=lambda item: (
                str(item["entry_day"]),
                -_candidate_score(item, score_mode, rank),
                str(item["symbol"]),
                str(item["signal_day"]),
            ),
        )
    # Group final order by (entry_day, score/priority) and apply tie_break inside.
    from itertools import groupby

    grouped_final: list[dict[str, Any]] = []
    for _, bucket in groupby(final_order, key=order_key):
        bucket_list = list(bucket)
        grouped_final.extend(order_bucket(bucket_list, None))
    final_order = grouped_final
    if score_mode == "P4":
        # P4: fair rotation per day
        entries_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in final_order:
            entries_by_day[str(item["entry_day"])].append(item)
        rotated: list[dict[str, Any]] = []
        for day in sorted(entries_by_day):
            day_items = list(entries_by_day[day])
            offset = sum(ord(char) for char in day) % max(len(day_items), 1)
            sorted_day = day_items[offset:] + day_items[:offset] if day_items else day_items
            sorted_day.sort(key=lambda item: rank.get(str(item["signal_type"]), 999))
            rotated.extend(sorted_day)
        return rotated
    if rng is not None and tie_break in {"random", "rotate", "hash"}:
        # Final same-day tie randomization for seeded sweeps.
        entries_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in final_order:
            entries_by_day[str(item["entry_day"])].append(item)
        result: list[dict[str, Any]] = []
        for day in sorted(entries_by_day):
            day_items = list(entries_by_day[day])
            bucket_map: dict[Any, list[dict[str, Any]]] = defaultdict(list)
            for item in day_items:
                if score_mode == "P0":
                    bucket_map[(rank.get(str(item["signal_type"]), 999), str(item["signal_type"]))].append(item)
                else:
                    bucket_map[round(-_candidate_score(item, score_mode, rank), 6)].append(item)
            flat: list[dict[str, Any]] = []
            if score_mode == "P0":
                for key in sorted(bucket_map):
                    bucket = list(bucket_map[key])
                    if tie_break == "random":
                        rng.shuffle(bucket)
                    else:
                        bucket = order_bucket(bucket, None)
                    flat.extend(bucket)
            else:
                # bucket keys are -score; ascending iter gives highest score first.
                for key in sorted(bucket_map):
                    bucket = list(bucket_map[key])
                    if tie_break == "random":
                        rng.shuffle(bucket)
                    else:
                        bucket = order_bucket(bucket, None)
                    flat.extend(bucket)
            result.extend(flat)
        return result
    return final_order


def _group_by_key(
    items: list[dict[str, Any]],
    key_fn,
):
    grouped = defaultdict(list)
    for item in items:
        grouped[key_fn(item)].append(item)
    return grouped.items()


def _mark_price(trade: dict[str, Any], day_key: str) -> float:
    marks = trade.get("_mark_prices", {})
    if day_key in marks:
        return float(marks[day_key])
    eligible = [key for key in marks if key <= day_key]
    return float(marks[max(eligible)]) if eligible else float(trade["entry_price"])


def _max_affordable_quantity(
    price: float,
    budget: float,
    lot_size: int,
    execution: dict[str, Any],
) -> int:
    quantity = math.floor(float(budget) / float(price) / lot_size) * lot_size
    while quantity >= lot_size and _buy_cash(price, quantity, execution)["total"] > budget + 1e-8:
        quantity -= lot_size
    return max(quantity, 0)


def run_portfolio(
    candidates: list[dict[str, Any]],
    costs: dict[str, Any],
    portfolio_config: dict[str, Any],
) -> dict[str, Any]:
    """Allocate independent candidates into a funded long-only portfolio."""
    execution = _execution_values(costs)
    seed = portfolio_config.get("seed")
    initial_cash = float(portfolio_config.get("initial_cash", costs.get("initial_cash", 100000.0)))
    max_positions = max(int(portfolio_config.get("max_positions", 4)), 1)
    position_size_pct = max(float(portfolio_config.get("position_size_pct", 0.25)), 0.0)
    lot_size = max(int(portfolio_config.get("lot_size", execution["lot_size"])), 1)
    priority = portfolio_config.get("signal_priority", list(DEFAULT_SIGNAL_PRIORITY))
    score_mode = str(portfolio_config.get("score_mode", "P0"))
    tie_break = str(portfolio_config.get("tie_break", "symbol_asc"))
    merged = _merge_portfolio_candidates(candidates, priority, seed, score_mode, tie_break)
    entries_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_days: set[str] = set()
    for candidate in merged:
        entries_by_day[str(candidate["entry_day"])].append(candidate)
        all_days.update((str(candidate["entry_day"]), str(candidate["exit_day"])))
        all_days.update(str(key) for key in candidate.get("_mark_prices", {}))

    cash = initial_cash
    positions: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    max_positions_used = 0

    def reject(candidate: dict[str, Any], reason: str) -> None:
        rejections.append(
            {
                "symbol": candidate["symbol"],
                "signal_day": candidate["signal_day"],
                "entry_day": candidate["entry_day"],
                "signal_types": candidate.get("signal_types", [candidate["signal_type"]]),
                "reason": reason,
            }
        )

    def close_position(symbol: str) -> None:
        nonlocal cash
        trade = positions.pop(symbol)
        quantity = int(trade["quantity"])
        exit_cash = _sell_cash(float(trade["exit_price"]), quantity, execution)
        proceeds = exit_cash["total"]
        cash += proceeds
        entry_cost_exact = float(trade["_entry_cost_cash_exact"])
        pnl_cash_exact = proceeds - entry_cost_exact
        trade["exit_proceeds_cash"] = round(proceeds, 2)
        trade["exit_commission_cash"] = round(exit_cash["commission"], 2)
        trade["stamp_tax_cash"] = round(exit_cash["stamp_tax"], 2)
        trade["exit_slippage_cash"] = round(exit_cash["slippage"], 2)
        trade["pnl_cash"] = round(pnl_cash_exact, 2)
        trade["pnl_pct"] = round(
            pnl_cash_exact
            / entry_cost_exact
            * 100.0,
            4,
        )
        completed.append(trade)

    for day_key in sorted(all_days):
        exited_today: set[str] = set()
        for symbol, trade in list(positions.items()):
            if trade["exit_day"] == day_key and trade.get("exit_session") == "open":
                close_position(symbol)
                exited_today.add(symbol)
        for candidate in entries_by_day.get(day_key, []):
            symbol = str(candidate["symbol"])
            if symbol in positions:
                reject(candidate, "symbol_already_held")
                continue
            if symbol in exited_today:
                reject(candidate, "same_day_reentry")
                continue
            if len(positions) >= max_positions:
                reject(candidate, "max_positions")
                continue
            available_budget = min(initial_cash * position_size_pct, cash)
            quantity = _max_affordable_quantity(
                float(candidate["entry_price"]),
                available_budget,
                lot_size,
                execution,
            )
            if quantity < lot_size:
                reject(candidate, "insufficient_cash")
                continue
            entry_cash = _buy_cash(float(candidate["entry_price"]), quantity, execution)
            entry_cost = entry_cash["total"]
            if entry_cost > cash + 1e-8:
                reject(candidate, "insufficient_cash")
                continue
            accepted = dict(candidate)
            accepted["quantity"] = quantity
            accepted["entry_cost_cash"] = round(entry_cost, 2)
            accepted["_entry_cost_cash_exact"] = entry_cost
            accepted["entry_commission_cash"] = round(entry_cash["commission"], 2)
            accepted["entry_slippage_cash"] = round(entry_cash["slippage"], 2)
            cash -= entry_cost
            positions[symbol] = accepted
            max_positions_used = max(max_positions_used, len(positions))
        for symbol, trade in list(positions.items()):
            if trade["exit_day"] == day_key and trade.get("exit_session") != "open":
                close_position(symbol)
                exited_today.add(symbol)
        market_value = sum(
            _mark_price(trade, day_key) * int(trade["quantity"])
            for trade in positions.values()
        )
        equity_curve.append(
            {
                "day": day_key,
                "cash": round(cash, 2),
                "market_value": round(market_value, 2),
                "equity": round(cash + market_value, 2),
                "positions": len(positions),
            }
        )

    final_day = max(all_days) if all_days else "9999-12-31"
    final_equity = cash + sum(
        _mark_price(trade, final_day) * int(trade["quantity"])
        for trade in positions.values()
    )
    peak = initial_cash
    max_drawdown = 0.0
    for point in equity_curve:
        equity = float(point["equity"])
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    public_completed = [_public_trade(trade) for trade in completed]
    trade_summary = summarize(public_completed)
    trade_summary["total_pnl_cash"] = round(final_equity - initial_cash, 2)
    summary = {
        **trade_summary,
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity / initial_cash - 1) * 100.0, 2) if initial_cash > 0 else None,
        "max_drawdown_pct": round(max_drawdown * 100.0, 2),
        "max_positions_used": max_positions_used,
        "open_positions": len(positions),
        "candidates": len(merged),
        "accepted": len(completed) + len(positions),
        "rejected": len(rejections),
    }
    return {
        "summary": summary,
        "trades": public_completed,
        "rejections": rejections,
        "rejection_reasons": dict(Counter(item["reason"] for item in rejections)),
        "equity_curve": equity_curve,
    }


def _resolve_execution_config(config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config.get("backtest", {}))
    risk = config.get("risk", {})
    result["stop_loss_pct"] = float(risk.get("stop_loss_pct", 0.08))
    result["take_profit_pct"] = float(risk.get("stop_profit_pct", 0.30))
    result.setdefault("intrabar_conflict", "stop_first")
    return result


def _load_index_history(path_text: str | None, config: dict[str, Any], limit: int) -> pd.DataFrame:
    if path_text:
        path = Path(path_text).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"index data does not exist: {path}")
        if path.suffix.lower() in {".pkl", ".pickle"}:
            return pd.read_pickle(path)
        if path.suffix.lower() == ".csv":
            return pd.read_csv(path)
        if path.suffix.lower() in {".json", ".jsonl"}:
            return pd.read_json(path, lines=path.suffix.lower() == ".jsonl")
        raise ValueError("index data must be CSV, JSON, JSONL or PKL")
    from data.market_data import MarketDataClient

    filters = config.get("entry_filters", {})
    index_code = str(
        filters.get("market_index_code")
        or config.get("regime", {}).get("index_code", "000001.SH")
    )
    return MarketDataClient(config).get_index_bars(index_code, limit=limit)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest daily signal quality and portfolio returns")
    parser.add_argument(
        "--config",
        type=str,
        default=str(BASE_DIR / "config" / "config.yaml"),
        help="strategy config YAML; use a copied research config to keep production isolated",
    )
    parser.add_argument("--experiment-id", type=str, default=None, help="label for this research run")
    parser.add_argument(
        "--dataset-role",
        choices=("baseline", "train", "validation", "test", "full"),
        default=None,
        help="dataset role recorded in the strategy framework snapshot",
    )
    parser.add_argument("--limit", type=int, default=0, help="only first N symbols")
    parser.add_argument("--start", type=str, default="2026-02-21", help="signal window start")
    parser.add_argument("--end", type=str, default=date.today().isoformat(), help="backtest data end")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", type=str, default="backtest_report_v2.json")
    parser.add_argument("--mode", choices=("signal", "portfolio", "both"), default="both")
    parser.add_argument("--adjust", choices=("qfq", "none"), default=None)
    parser.add_argument("--history-bars", type=int, default=None)
    parser.add_argument(
        "--fetch-missing-adjusted",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="fetch missing/short adjusted histories; never falls back to none",
    )
    parser.add_argument(
        "--local-data-only",
        action="store_true",
        help="research safety mode: use local history/cache only and never fetch stock-pool history",
    )
    parser.add_argument("--index-data", type=str, default=None, help="historical index CSV/JSON/PKL")
    parser.add_argument("--index-limit", type=int, default=1200)
    parser.add_argument(
        "--fundamental-data",
        type=str,
        default=None,
        help="causal historical fundamentals JSON/JSONL",
    )
    parser.add_argument(
        "--enable-fundamental",
        action="store_true",
        help="enable historical fundamental filtering for this run only (does not edit config)",
    )
    parser.add_argument(
        "--symbols-file",
        type=str,
        default=None,
        help="JSON file with list of symbols to analyze (filters the directory scan)",
    )
    parser.add_argument(
        "--fundamental-missing-data-policy",
        choices=("reject", "unavailable"),
        default=None,
        help="override historical fundamental missing-data policy for this run",
    )
    parser.add_argument(
        "--fundamental-roe-min",
        type=float,
        default=None,
        help="override the historical fundamental ROE threshold for this run "
        "(does not edit config)",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="include right-censored trades and exit them at the last close",
    )
    parser.add_argument(
        "--stop-loss-pct", type=float, default=None,
        help="override stop-loss threshold (e.g. 0.10)",
    )
    parser.add_argument(
        "--take-profit-pct", type=float, default=None,
        help="override take-profit threshold (e.g. 0.20)",
    )
    parser.add_argument(
        "--zero-axis-exit-confirm-bars", type=int, default=None,
        help="research override: require this many closed bars below DEA before zero-axis exit",
    )
    parser.add_argument(
        "--timeout-exit-mode", choices=("fixed", "ma_break"), default=None,
        help="research override: fixed timeout or MA-break conditioned timeout",
    )
    parser.add_argument(
        "--timeout-ma-period", type=int, default=None,
        help="research override: moving-average period for conditioned timeout",
    )
    parser.add_argument(
        "--timeout-ma-confirm-bars", type=int, default=None,
        help="research override: consecutive closes below timeout MA",
    )
    parser.add_argument(
        "--timeout-hard-cap-bars", type=int, default=None,
        help="research override: hard holding-bar cap for ma_break timeout",
    )
    parser.add_argument(
        "--portfolio-max-positions", type=int, default=None,
        help="override portfolio max concurrent positions",
    )
    parser.add_argument(
        "--position-size-pct", type=float, default=None,
        help="override per-position budget as fraction of initial cash",
    )
    parser.add_argument(
        "--portfolio-seed", type=int, default=None,
        help="seed for randomizing same-priority candidate order (unset = deterministic)",
    )
    parser.add_argument(
        "--portfolio-seed-sweep", type=int, default=0,
        help="run N random-seed portfolio sweeps and append distribution to report",
    )
    parser.add_argument(
        "--portfolio-score-mode",
        choices=("P0", "P1", "P2", "P3", "P4", "P5a",
                 "P5a-C", "P5a-G", "P5a-Z", "P5a-CG", "P5a-CZ", "P5a-CGZ",
                 "P5b-MA60", "P5b-MA250", "P5b-ATR", "P5b-Risk", "P5b-MA", "P5b-All"),
        default="P0",
        help="candidate ordering within same day: P0 priority, P1 score, P2 +freshness, "
        "P3 +risk penalty, P4 fair rotation, P5a +continuous MACD quality; "
        "P5a-C/G/Z/CG/CZ/CGZ use cross-sectional percentile normalization "
        "(C=confirmation, G=DIF/DEA gap, Z=zero-axis distance); "
        "P5b-MA60/MA250/ATR/Risk/MA/All use trend/risk/volatility percentiles",
    )
    parser.add_argument(
        "--portfolio-tie-break", choices=("symbol_asc", "symbol_desc", "hash", "rotate", "random"),
        default="symbol_asc",
        help="tie-break for equal-priority/equal-score candidates",
    )
    parser.add_argument(
        "--portfolio-multi-modes", type=str, default="",
        help="comma list of score_modes to run in one pass (e.g. P0,P1hash,P1random); "
        "each mode writes its own report and portfolio outputs",
    )
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        parser.error("--end must be on or after --start")
    config = load_config(args.config)
    if config is None:
        parser.error(f"unable to load config: {args.config}")
    if args.enable_fundamental or getattr(args, "fundamental_missing_data_policy", None):
        historical_fundamental = config.setdefault("backtest", {}).setdefault("fundamental", {})
        if args.enable_fundamental:
            historical_fundamental["enabled"] = True
        if getattr(args, "fundamental_missing_data_policy", None):
            historical_fundamental["missing_data_policy"] = args.fundamental_missing_data_policy
    if args.fundamental_roe_min is not None:
        cfg = config.setdefault("strategy", {}).setdefault("fundamental", {})
        cfg["min_roe"] = args.fundamental_roe_min
    framework_config = config.setdefault("strategy", {}).setdefault("framework", {})
    if args.experiment_id is not None:
        framework_config["experiment_id"] = args.experiment_id
    if args.dataset_role is not None:
        framework_config["dataset_role"] = args.dataset_role
    strategy_framework = resolve_strategy_framework(config)
    chan_zero_axis_config = config.setdefault("backtest", {}).setdefault(
        "chan_zero_axis", {}
    )
    if args.zero_axis_exit_confirm_bars is not None:
        chan_zero_axis_config["zero_axis_exit_confirmation_bars"] = max(
            int(args.zero_axis_exit_confirm_bars), 1
        )
    if args.timeout_exit_mode is not None:
        chan_zero_axis_config["timeout_exit_mode"] = args.timeout_exit_mode
    if args.timeout_ma_period is not None:
        chan_zero_axis_config["timeout_ma_period"] = max(
            int(args.timeout_ma_period), 2
        )
    if args.timeout_ma_confirm_bars is not None:
        chan_zero_axis_config["timeout_ma_confirm_bars"] = max(
            int(args.timeout_ma_confirm_bars), 1
        )
    if args.timeout_hard_cap_bars is not None:
        chan_zero_axis_config["timeout_hard_cap_bars"] = max(
            int(args.timeout_hard_cap_bars), 1
        )
    execution = _resolve_execution_config(config)
    if args.stop_loss_pct is not None:
        execution["stop_loss_pct"] = max(float(args.stop_loss_pct), 0.0)
    if args.take_profit_pct is not None:
        execution["take_profit_pct"] = max(float(args.take_profit_pct), 0.0)
    backtest_config = config.get("backtest", {})
    adjustment = str(args.adjust or backtest_config.get("adjustment", "qfq")).lower()
    history_bars = max(int(args.history_bars or backtest_config.get("history_bars", 800)), 300)
    fetch_missing_adjusted = (
        bool(backtest_config.get("fetch_missing_adjusted", True))
        if args.fetch_missing_adjusted is None
        else bool(args.fetch_missing_adjusted)
    )
    entry_filters = config.get("entry_filters", {})
    stock_pool_settings = resolve_stock_pool_config(config)
    fundamental_settings = resolve_fundamental_config(config, context="historical")
    fundamental_data_path = (
        str(Path(args.fundamental_data).expanduser().resolve())
        if args.fundamental_data
        else (config.get("backtest", {}).get("fundamental", {}) or {}).get("data_path")
    )
    fundamental_history: dict[str, list[dict[str, Any]]] = {}
    if fundamental_settings["enabled"]:
        if not fundamental_data_path:
            raise RuntimeError(
                "historical fundamental filtering is enabled but no fundamental data file was provided"
            )
        fundamental_history = load_fundamental_history(fundamental_data_path)
    fundamental_coverage = history_coverage_report(fundamental_history)
    market_gate_settings = resolve_market_gate_settings(config)
    signal_execution_policy = resolve_signal_execution_policy(config)
    market_gate_enabled = bool(entry_filters.get("market_gate_enabled", False))
    market_gate: dict[str, dict[str, Any]] | None = None
    market_gate_meta: dict[str, Any] = {"enabled": market_gate_enabled}
    if market_gate_enabled:
        index_history = _load_index_history(args.index_data, config, args.index_limit)
        market_gate = build_market_gate(index_history, config)
        if not market_gate:
            raise RuntimeError("market gate is enabled but historical index data is unavailable")
        usable_days = [
            day_key
            for day_key, context in market_gate.items()
            if "insufficient_history" not in context.get("blocked_by", [])
        ]
        if not usable_days:
            raise RuntimeError("market gate index history is too short for the long moving average")
        market_gate_meta.update(
            {
                "first_day": min(market_gate),
                "last_day": max(market_gate),
                "first_usable_day": min(usable_days),
                "usable_days": len(usable_days),
                "allowed_days": sum(
                    1 for context in market_gate.values() if context.get("allows_entries", False)
                ),
            }
        )

    files = sorted(HISTORY_DIR.glob("*_none.pkl"))
    if args.symbols_file:
        with open(Path(args.symbols_file), encoding="utf-8") as symbols_handle:
            symbol_list = json.load(symbols_handle)
        wanted = {str(item).zfill(6) for item in symbol_list}
        files = [
            path for path in files
            if str(path.name.split("_")[0]).zfill(6) in wanted
        ]
    if args.limit:
        files = files[: args.limit]
    logger.info(
        "Backtesting %s symbols, window %s -> %s, mode=%s, adjust=%s, bars=%s",
        len(files), start, end, args.mode, adjustment, history_bars,
    )
    # Day → market regime for regime-aware signal policy (causal: the regime
    # of the signal day's close, used only for that day's signal disposition).
    regime_lookup: dict[str, str] | None = None
    if market_gate:
        regime_lookup = {
            day_key: str(context.get("regime", ""))
            for day_key, context in market_gate.items()
            if context.get("regime") in {"bull", "range", "bear"}
        }
    all_candidates: list[dict[str, Any]] = []
    observed_signals: list[dict[str, Any]] = []
    signal_policy_counts: Counter = Counter()
    stock_pool_rejection_details: list[dict[str, Any]] = []
    fundamental_filter_details: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    history_sources: Counter = Counter()
    errors = 0

    def analyse_one(path: Path) -> dict[str, Any]:
        symbol = path.name.split("_")[0]
        closed, history_source = load_backtest_history(
            symbol,
            adjustment=adjustment,
            config=config,
            history_bars=history_bars,
            end=end,
            fetch_missing=fetch_missing_adjusted,
            local_only=bool(args.local_data_only),
        )
        closed = closed[pd.to_datetime(closed["datetime"]).dt.date <= end].reset_index(drop=True)
        if closed.empty or len(closed) < 60:
            return {
                "trades": [],
                "skipped": {"insufficient_history": 1},
                "history_source": history_source,
                "stock_pool_rejections": [],
                "fundamental_rejections": [],
                "observed_signals": [],
                "signal_policy_counts": {},
            }
        events = find_signals(closed, config)
        events = {
            "buy": [
                event
                for event in events.get("buy", [])
                if start <= date.fromisoformat(str(event["day"])) <= end
            ],
            "sell": list(events.get("sell", [])),
        }
        executable_buys, observed_buys, disabled_buys = partition_entry_signals(
            events["buy"],
            signal_execution_policy,
            regime_lookup=regime_lookup,
        )
        policy_counts = Counter(
            f"{event['execution_mode']}:{event['signal_type']}"
            for event in executable_buys + observed_buys + disabled_buys
        )
        observed_records = [
            {
                "symbol": symbol,
                "day": event.get("day"),
                "confirmed_at": event.get("confirmed_at"),
                "signal_type": event.get("signal_type"),
                "execution_mode": event.get("execution_mode"),
                "observation_stage": "detected_before_entry_filters",
                "price": event.get("price"),
                "cross_day": event.get("cross_day"),
                "confirmation_bars": event.get("confirmation_bars"),
                "confirmation_count": event.get("confirmation_count"),
                "confirmation_items": event.get("confirmation_items"),
            }
            for event in observed_buys
        ]
        events["buy"] = executable_buys
        events, position_skipped = apply_stock_position_gate(closed, events, config)
        stock_pool_skipped: Counter = Counter()
        stock_pool_details: list[dict[str, Any]] = []
        fundamental_skipped: Counter = Counter()
        fundamental_details: list[dict[str, Any]] = []
        stock_pool_fetch_failed = False
        if stock_pool_settings["enabled"] and events.get("buy"):
            try:
                stock_pool_history = load_stock_pool_history(
                    symbol,
                    config=config,
                    history_bars=history_bars,
                    end=end,
                    local_only=bool(args.local_data_only),
                )
            except Exception as exc:
                stock_pool_fetch_failed = True
                logger.warning("stock-pool history failed for %s: %s", symbol, exc)
                stock_pool_history = pd.DataFrame()
            events, stock_pool_skipped, stock_pool_details = filter_buy_events(
                stock_pool_history,
                events,
                config,
            )
            for detail in stock_pool_details:
                detail["symbol"] = symbol
                if stock_pool_fetch_failed:
                    detail["data_error"] = "stock_pool_history_fetch_failed"
        if fundamental_settings["enabled"] and events.get("buy"):
            events, fundamental_skipped, fundamental_details = filter_buy_events_by_fundamental(
                symbol,
                events,
                fundamental_history,
                config,
            )
        result = simulate_signal_mode(
            symbol, closed, events, start, end, execution,
            market_gate=market_gate,
            market_gate_enabled=market_gate_enabled,
            allow_incomplete=args.allow_incomplete,
        )
        combined = Counter(result["skipped"])
        combined.update(position_skipped)
        combined.update(stock_pool_skipped)
        combined.update(fundamental_skipped)
        if observed_buys:
            combined["signal_policy_observe_only"] += len(observed_buys)
        if disabled_buys:
            combined["signal_policy_disabled"] += len(disabled_buys)
        if stock_pool_fetch_failed:
            combined["stock_pool_history_fetch_failed"] += 1
        return {
            "trades": result["trades"],
            "skipped": dict(combined),
            "history_source": history_source,
            "stock_pool_rejections": stock_pool_details,
            "fundamental_rejections": fundamental_details,
            "observed_signals": observed_records,
            "signal_policy_counts": dict(policy_counts),
        }

    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as pool:
        futures = [pool.submit(analyse_one, path) for path in files]
        for index, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
                all_candidates.extend(result["trades"])
                observed_signals.extend(result["observed_signals"])
                signal_policy_counts.update(result["signal_policy_counts"])
                stock_pool_rejection_details.extend(result["stock_pool_rejections"])
                fundamental_filter_details.extend(result["fundamental_rejections"])
                skipped.update(result["skipped"])
                history_sources[result["history_source"]] += 1
            except Exception:
                errors += 1
                logger.exception("symbol analysis failed")
            if index % 200 == 0:
                logger.info("processed %d/%d, candidates %d", index, len(files), len(all_candidates))

    effective_config = copy.deepcopy(config)
    effective_backtest = effective_config.setdefault("backtest", {})
    effective_backtest["adjustment"] = adjustment
    effective_backtest["history_bars"] = history_bars
    effective_backtest["fetch_missing_adjusted"] = fetch_missing_adjusted
    effective_backtest["execution_effective"] = copy.deepcopy(execution)
    effective_backtest["portfolio_overrides"] = {
        "max_positions": args.portfolio_max_positions,
        "position_size_pct": args.position_size_pct,
        "score_mode": args.portfolio_score_mode,
        "tie_break": args.portfolio_tie_break,
        "seed": args.portfolio_seed,
        "seed_sweep": args.portfolio_seed_sweep,
        "multi_modes": args.portfolio_multi_modes or None,
    }
    if args.fundamental_data:
        effective_backtest.setdefault("fundamental", {})["data_path"] = str(
            Path(args.fundamental_data).expanduser().resolve()
        )

    report: dict[str, Any] = {
        "report_version": 2,
        "strategy_framework": strategy_framework,
        "config_source": config.get("_config_path"),
        "config_snapshot": build_config_snapshot(effective_config),
        "experiment": {
            "id": strategy_framework["experiment_id"],
            "dataset_role": strategy_framework["dataset_role"],
            "window": {"start": start.isoformat(), "end": end.isoformat()},
        },
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "mode": args.mode,
        "symbols_requested": len(files),
        "symbols_succeeded": max(len(files) - errors, 0),
        "symbols_failed": errors,
        # Deprecated alias retained for consumers of report_version 2. It now
        # means symbols with a completed analysis, not requested symbols.
        "symbols_analyzed": max(len(files) - errors, 0),
        "data_adjustment": adjustment,
        "history_bars_requested": history_bars,
        "history_sources": dict(history_sources),
        "allow_incomplete": args.allow_incomplete,
        "execution": execution,
        "model_capabilities": {
            "price_limit_model_applied": execution["price_limit_model"] == "conservative",
            "adjusted_prices": adjustment == "qfq",
            "minimum_commission_applied": execution["minimum_commission"] > 0,
        },
        "model_limitations": {
            "historical_st_status_automatic": False,
            "historical_total_market_cap_available": False,
            "ipo_no_limit_period_modeled": False,
            "limit_queue_depth_modeled": False,
        },
        "filters": {
            "fetch_missing_adjusted": fetch_missing_adjusted,
            "local_data_only": bool(args.local_data_only),
            "market_gate_enabled": market_gate_enabled,
            "market_index_code": entry_filters.get("market_index_code"),
            "market_gate_history": market_gate_meta,
            "market_gate_settings": market_gate_settings,
            "signal_execution_policy": signal_execution_policy,
            "position_gate_enabled": bool(entry_filters.get("position_gate_enabled", False)),
            "stock_pool": stock_pool_settings,
            "stock_pool_rejections": dict(
                Counter(
                    reason
                    for detail in stock_pool_rejection_details
                    for reason in detail.get("reasons", [])
                )
            ),
            "stock_pool_rejected_candidates": len(stock_pool_rejection_details),
            "fundamental": {
                "enabled": fundamental_settings["enabled"],
                "context": "historical",
                "missing_data_policy": fundamental_settings["missing_data_policy"],
                "data_path": str(fundamental_data_path) if fundamental_data_path else None,
                "symbols_with_snapshots": len(fundamental_history),
                "coverage": fundamental_coverage,
                "thresholds": {
                    key: fundamental_settings[key]
                    for key in (
                        "roe_min",
                        "debt_ratio_max",
                        "pe_max",
                        "market_cap_min",
                        "market_cap_max",
                    )
                },
                "rejected_candidates": sum(
                    1
                    for detail in fundamental_filter_details
                    if detail.get("status") == "rejected"
                ),
                "unavailable_candidates": sum(
                    1
                    for detail in fundamental_filter_details
                    if detail.get("status") == "unavailable"
                ),
            },
            "min_confirmations": resolve_min_confirmations(config),
            "manual_st_symbols": len(execution["st_symbols"]),
        },
        "artifacts": {},
    }
    observed_signals.sort(
        key=lambda event: (
            str(event.get("day", "")),
            str(event.get("signal_type", "")),
            str(event.get("symbol", "")),
        )
    )
    policy_by_mode: dict[str, dict[str, int]] = defaultdict(dict)
    for key, count in sorted(signal_policy_counts.items()):
        mode, signal_type = key.split(":", 1)
        policy_by_mode[mode][signal_type] = count
    report["signal_policy"] = {
        "by_mode": dict(policy_by_mode),
        "observed_count": len(observed_signals),
        "observation_stage": "detected_before_entry_filters",
    }
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = BASE_DIR / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if stock_pool_rejection_details:
        stock_pool_rejections_path = out_path.with_name(
            out_path.stem + "_stock_pool_rejections.jsonl"
        )
        _write_jsonl(stock_pool_rejections_path, stock_pool_rejection_details)
        report["artifacts"]["stock_pool_rejections"] = str(stock_pool_rejections_path)

    if fundamental_filter_details:
        fundamental_details_path = out_path.with_name(
            out_path.stem + "_fundamental_filter.jsonl"
        )
        _write_jsonl(fundamental_details_path, fundamental_filter_details)
        report["artifacts"]["fundamental_filter"] = str(fundamental_details_path)

    if observed_signals:
        observed_signals_path = out_path.with_name(
            out_path.stem + "_observed_signals.jsonl"
        )
        _write_jsonl(observed_signals_path, observed_signals)
        report["artifacts"]["observed_signals"] = str(observed_signals_path)

    if args.mode in {"signal", "both"}:
        public_candidates = [_public_trade(trade) for trade in all_candidates]
        by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in public_candidates:
            by_signal[str(trade["signal_type"])].append(trade)
        signal_report = {
            "summary": summarize(public_candidates),
            "by_signal_type": {name: summarize(trades) for name, trades in sorted(by_signal.items())},
            "exit_reasons": dict(Counter(trade["exit_reason"] for trade in public_candidates)),
            "skipped": dict(skipped),
        }
        # Regime × signal_type breakdown from executed trades (by signal day)
        by_regime_sig: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in public_candidates:
            day = trade.get("signal_day", "")
            r = (regime_lookup or {}).get(day, "unknown")
            key = f"{r}:{trade['signal_type']}"
            by_regime_sig[key].append(trade)
        signal_report["by_regime_signal_type"] = {
            key: summarize(trades) for key, trades in sorted(by_regime_sig.items())
        }
        signal_report["by_holding_period"] = summarize_holding_periods(public_candidates)
        report["signal"] = signal_report
        report["summary"] = signal_report["summary"]
        report["by_signal_type"] = signal_report["by_signal_type"]
        report["exit_reasons"] = signal_report["exit_reasons"]
        signal_trades_path = out_path.with_name(out_path.stem + "_signal_trades.jsonl")
        _write_jsonl(signal_trades_path, public_candidates)
        report["artifacts"]["signal_trades"] = str(signal_trades_path)

    if args.mode in {"portfolio", "both"}:
        position = config.get("position", {})
        multi_modes_raw = str(args.portfolio_multi_modes or "").strip()
        if multi_modes_raw:
            parsed_modes: list[tuple[str, str, str]] = []
            for item in multi_modes_raw.split(","):
                tag = item.strip()
                if not tag:
                    continue
                if tag == "P0":
                    parsed_modes.append((tag, "P0", "symbol_asc"))
                elif tag == "P1hash":
                    parsed_modes.append((tag, "P1", "hash"))
                elif tag == "P1random":
                    parsed_modes.append((tag, "P1", "random"))
                elif tag.startswith("P5a-") and tag.endswith("random"):
                    base_mode = tag[: -len("random")]
                    parsed_modes.append((tag, base_mode, "random"))
                elif tag.startswith("P5a-") and tag.endswith("hash"):
                    base_mode = tag[: -len("hash")]
                    parsed_modes.append((tag, base_mode, "hash"))
                elif tag in ("P5a", "P5a-C", "P5a-G", "P5a-Z", "P5a-CG", "P5a-CZ", "P5a-CGZ"):
                    parsed_modes.append((tag, tag, "hash"))
                elif tag in ("P5b-MA60", "P5b-MA250", "P5b-ATR", "P5b-Risk", "P5b-MA", "P5b-All"):
                    parsed_modes.append((tag, tag, "hash"))
                elif tag.endswith("hash") and tag[:-4] in (
                    "P5b-MA60", "P5b-MA250", "P5b-ATR", "P5b-Risk", "P5b-MA", "P5b-All",
                ):
                    parsed_modes.append((tag, tag[:-4], "hash"))
                elif tag.endswith("random") and tag[:-6] in (
                    "P5b-MA60", "P5b-MA250", "P5b-ATR", "P5b-Risk", "P5b-MA", "P5b-All",
                ):
                    parsed_modes.append((tag, tag[:-6], "random"))
                else:
                    parsed_modes.append((tag, args.portfolio_score_mode, args.portfolio_tie_break))
            modes_to_run = parsed_modes
        else:
            modes_to_run = [
                ("default", args.portfolio_score_mode, args.portfolio_tie_break)
            ]

        def run_one_mode(mode_tag: str, score_mode: str, tie_break: str, mode_out: Path) -> dict[str, Any]:
            base_cfg = {
                "initial_cash": execution.get("initial_cash", 100000),
                "max_positions": (
                    args.portfolio_max_positions
                    if args.portfolio_max_positions is not None
                    else position.get("max_stocks", 4)
                ),
                "position_size_pct": (
                    args.position_size_pct
                    if args.position_size_pct is not None
                    else position.get("base_position_per_stock", 0.25)
                ),
                "lot_size": execution.get("lot_size", 100),
                "signal_priority": execution.get("signal_priority", list(DEFAULT_SIGNAL_PRIORITY)),
                "score_mode": score_mode,
                "tie_break": tie_break,
            }
            if score_mode == "P0":
                base_cfg["tie_break"] = "symbol_asc"
            if tie_break == "random":
                base_cfg["seed"] = None  # deterministic single run first
            portfolio = run_portfolio(all_candidates, execution, base_cfg)
            mode_report = {
                "score_mode": score_mode,
                "tie_break": tie_break,
                "summary": portfolio["summary"],
                "by_holding_period": summarize_holding_periods(portfolio["trades"]),
                "rejection_reasons": portfolio["rejection_reasons"],
                "equity_curve": portfolio["equity_curve"],
            }
            trades_path = mode_out.with_name(mode_out.stem + f"_{mode_tag}_portfolio_trades.jsonl")
            rej_path = mode_out.with_name(mode_out.stem + f"_{mode_tag}_portfolio_rejections.jsonl")
            _write_jsonl(trades_path, portfolio["trades"])
            _write_jsonl(rej_path, portfolio["rejections"])
            mode_report["trades_file"] = str(trades_path)
            mode_report["rejections_file"] = str(rej_path)
            # seed sweep only for random tie-break modes
            if args.portfolio_seed_sweep > 0 and tie_break == "random":
                sweep_results: list[dict[str, Any]] = []
                for seed in range(args.portfolio_seed_sweep):
                    cfg = dict(base_cfg)
                    cfg["seed"] = seed
                    result = run_portfolio(all_candidates, execution, cfg)
                    sweep_results.append(
                        {
                            "seed": seed,
                            "total_return_pct": result["summary"]["total_return_pct"],
                            "max_drawdown_pct": result["summary"]["max_drawdown_pct"],
                            "final_equity": result["summary"]["final_equity"],
                        }
                    )
                rets = sorted(float(item["total_return_pct"]) for item in sweep_results)
                n = len(rets)
                mode_report["seed_sweep"] = {
                    "count": n,
                    "median_return_pct": float(rets[n // 2]),
                    "p10_return_pct": float(rets[max(0, int(0.10 * (n - 1)))]),
                    "p90_return_pct": float(rets[min(n - 1, int(0.90 * (n - 1)))]),
                    "win_probability_pct": round(
                        sum(1 for value in rets if value > 0) / n * 100, 2
                    ),
                    "min_return_pct": float(rets[0]),
                    "max_return_pct": float(rets[-1]),
                    "mean_return_pct": round(sum(rets) / n, 4),
                    "all": rets,
                }
            return mode_report

        report["portfolio"] = {}
        report["portfolio_modes"] = {}
        for mode_tag, score_mode, tie_break in modes_to_run:
            mode_report = run_one_mode(mode_tag, score_mode, tie_break, out_path)
            report["portfolio_modes"][mode_tag] = mode_report
        if "default" in report["portfolio_modes"]:
            report["portfolio"] = report["portfolio_modes"]["default"]

    report["artifacts"]["report"] = str(out_path)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
