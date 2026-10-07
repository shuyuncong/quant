"""Funded daily replay of the versioned analysis strategies.

Market data is fetched through the configured daily provider for the requested
window, frozen under ``output_dir/inputs`` on first run, and reused on retry so
a resumed task compares exactly the same sample.  No signal store, notification
or real holdings writes happen here. Each strategy is replayed independently
under its own resolved stop loss.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from strategy.registry import (
    STRATEGIES,
    VERSION,
    evaluate_strategies,
    evaluate_exits,
    exit_parameters,
)
from strategy.macd import calculate_macd, find_golden_cross_entries
from strategy.macd_divergence import add_divergence_indicators, resolve_divergence_config
from strategy.signal_policy import effective_signal_execution_mode, resolve_signal_execution_policy
from strategy.yearline import prepare_yearline_bars, add_yearline_indicators
from backtest_winrate import _execution_values, _resolve_execution_config, _buy_cash, _sell_cash, _bar_price_limits, _resolve_sell_fill
from data.symbols import normalize_ts_code
from data.providers.akshare_provider import AkshareDailyProvider
from utils.time_utils import now_shanghai

# Rules persisted with a task, mirroring freezeEngineConfig() on the web side.
CONFIG_KEYS = (
    "signal_strategy", "macd_divergence", "risk", "backtest", "position",
    "trading_limits", "stock_pool", "scan", "candidate_pool", "yearline",
    "monitor", "entry_filters", "market_regime", "regime",
)
# MA250 + slope window + margin, matching yearline.MIN_BARS.
MIN_WARMUP_BARS = 270
MAX_CALENDAR_LOOKBACK = 540
DEFAULT_REQUEST_INTERVAL = 0.35
# How many fetched symbols may sit in memory before the ledger is flushed to disk.
LEDGER_FLUSH_SYMBOLS = 25
ILLEGAL_HISTORY_REASON = "历史行情存在非法价格或成交量"
WARNING_SURVIVORSHIP = (
    "当前在市全 A 股历史回放；不含完整历史退市样本及历史 ST 状态，存在幸存者偏差。"
)
WARNING_LIMITS = (
    "采用前复权价格进行理论成交，分红送转与真实股份换算未逐笔复原；费用含佣金、滑点及按日期适用的印花税。"
)
WARNING_SIGNAL_PARITY = (
    "MACD 实时通知使用不复权行情，本回测统一用前复权历史；除权附近的信号日期可能不同。年线与底背离指标池也使用前复权。"
)
WARNING_GATE_SCOPE = (
    "三策略比较使用统一技术入场条件；实时入池的市值、流动性及市场闸门不属于本次回放的历史筛选范围。停牌或缺少当日日线时不成交，持仓按最后已知收盘估值。"
)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str), encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_config(config: dict) -> dict:
    """Persist only engine rules: credentials and connection details never reach disk."""
    # Deep copy so a later mutation of the live config (e.g. an override merge) cannot
    # change the fingerprint or the snapshot a retry compares against.
    return deepcopy({key: config[key] for key in CONFIG_KEYS if key in config})


def _public_options(options: dict) -> dict:
    """The request as sent by the client: the server-side output directory stays internal."""
    return {key: value for key, value in options.items() if key != "output_dir"}


def _config_hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _effective_trading_days(calendar_dates: list[str], start: str, end: str, today: date | None = None) -> list[str]:
    """Real trading days inside the requested window, trimmed for an unfinished session."""
    days = [day for day in calendar_dates if start <= day <= end]
    now = now_shanghai()
    if (today or now.date()).isoformat() in days and now.hour < 15:
        # Today's daily bar is not published yet, so it cannot be replayed.
        days = [day for day in days if day != (today or now.date()).isoformat()]
    return days


def _illegal_history_rows(frame: pd.DataFrame) -> int:
    ohlc = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    volume = frame["volume"].to_numpy(dtype=float)
    bad_ohlc = ~np.isfinite(ohlc).all(axis=1) | (ohlc <= 0).any(axis=1)
    bad_volume = ~np.isfinite(volume) | (volume < 0)
    return int((bad_ohlc | bad_volume).sum())


def prepare_backtest_inputs(config: dict, options: dict) -> dict:
    """Fetch, freeze and describe the exact sample one backtest runs on.

    Returns histories/calendar plus the report metadata (universe, exclusions,
    manifest, rules snapshot).  Everything is cached under ``output_dir/inputs``
    so a retry after a crash replays the same frozen sample.
    """
    start, end = str(options["start"]), str(options["end"])
    mode = str(options.get("mode", "stock"))
    task_dir = Path(options["output_dir"])
    inputs_dir = task_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    progress_file = task_dir / "progress.json"
    data_config = config.get("market_data", {})
    interval = float(data_config.get("request_interval_seconds", DEFAULT_REQUEST_INTERVAL))

    snapshot = _snapshot_config(config)
    config_hash = _config_hash(snapshot)
    rules_file = inputs_dir / "rules.json"
    if not rules_file.exists():
        write_json(rules_file, snapshot)

    def report(stage: str, processed: int, total: int, excluded: int) -> None:
        write_json(progress_file, {"stage": stage, "processed": processed, "total": total, "excluded": excluded})

    def provider() -> AkshareDailyProvider:
        # A short-lived provider per symbol keeps one run from retaining every
        # stock's frame in the provider's in-memory cache.
        return AkshareDailyProvider(config=config)

    # ---- 1. stock list -------------------------------------------------------
    report("准备股票名单", 0, 0, 0)
    universe_file = inputs_dir / "universe.json"
    if universe_file.exists():
        universe = read_json(universe_file)
        names = {row["symbol"]: row["name"] for row in universe["symbols"]}
    else:
        listing = provider().get_stock_list(include_special=True)
        if listing is None or listing.empty:
            raise ValueError("无法获取当前在市 A 股名单，回测任务失败")
        available = {
            normalize_ts_code(str(row["ts_code"] or row["symbol"])): str(row.get("name") or "")
            for _, row in listing.iterrows()
        }
        if mode == "market":
            symbols = sorted(symbol for symbol in available if symbol)
            scope = "current_listed_a"
        else:
            requested = [normalize_ts_code(str(item)) for item in options.get("symbols") or []]
            symbols = [symbol for symbol in requested if symbol]
            scope = "stock"
        if not symbols:
            raise ValueError("没有可回测的股票代码")
        names = {symbol: (available.get(symbol) or symbol) for symbol in symbols}
        universe = {
            "as_of": datetime.now().strftime("%Y-%m-%d"),
            "scope": scope,
            "symbols": [{"symbol": symbol, "name": names[symbol]} for symbol in symbols],
        }
        write_json(universe_file, universe)

    # ---- 2. calendar and warmup span ----------------------------------------
    calendar_file = inputs_dir / "calendar.json"
    if calendar_file.exists():
        # A retry must replay the frozen sample, not whatever the source returns today.
        calendar_days = sorted(str(day) for day in read_json(calendar_file))
    else:
        full_calendar = provider().get_trade_calendar()
        if full_calendar is None or full_calendar.empty:
            raise ValueError("无法获取交易日历，回测任务失败")
        calendar_days = sorted({str(pd.Timestamp(value).date()) for value in full_calendar["trade_date"]})
        if not calendar_days:
            raise ValueError("交易日历为空，回测任务失败")
        write_json(calendar_file, calendar_days)
    effective = _effective_trading_days(calendar_days, start, end)
    if not effective:
        raise ValueError("所选区间内没有交易日")
    effective_start, effective_end = effective[0], effective[-1]
    warmup_candidates = [day for day in calendar_days if day < effective_start]
    if len(warmup_candidates) < MIN_WARMUP_BARS:
        raise ValueError(f"交易日历前史不足 {MIN_WARMUP_BARS} 个交易日，无法计算年线等指标")
    # Up to MAX_CALENDAR_LOOKBACK calendar trading days of history, so suspensions still leave 270 warmup bars.
    # A shorter calendar is used as-is: the guard above already proved 270 bars exist.
    fetch_start = warmup_candidates[max(0, len(warmup_candidates) - MAX_CALENDAR_LOOKBACK)]

    # ---- 3. per-symbol history ----------------------------------------------
    ledger_file = inputs_dir / "prepared.json"
    ledger: dict[str, Any] = read_json(ledger_file) if ledger_file.exists() else {"symbols": {}}
    ledger["symbols"] = ledger.get("symbols") or {}
    manifest_file = inputs_dir / "input_manifest.json"
    frozen_files: dict[str, Any] = read_json(manifest_file).get("files", {}) if manifest_file.exists() else {}
    histories: dict[str, pd.DataFrame] = {}
    excluded: list[dict] = []
    total = len(universe["symbols"])
    pending_writes = 0

    def flush(force: bool = False) -> None:
        """Record finished symbols on disk; a restart then reuses them instead of refetching."""
        nonlocal pending_writes
        if not force and pending_writes < LEDGER_FLUSH_SYMBOLS:
            return
        write_json(ledger_file, ledger)
        pending_writes = 0

    def exclude(symbol: str, reason: str) -> None:
        ledger["symbols"][symbol] = {"reason": reason}
        excluded.append({"symbol": symbol, "reason": reason})
        # Exclusions are small and decide coverage, so never lose them.
        flush(True)

    for number, row in enumerate(universe["symbols"]):
        symbol = row["symbol"]
        record = ledger["symbols"].get(symbol)
        data_file = inputs_dir / f"{symbol}.pkl"
        if isinstance(record, dict) and record.get("reason"):
            excluded.append({"symbol": symbol, "reason": record["reason"]})
            report("准备历史行情", number + 1, total, len(excluded))
            continue
        if isinstance(record, dict) and record.get("file") and data_file.exists():
            digest = file_sha256(data_file)
            if digest != record.get("sha256"):
                raise ValueError("回测输入已变化，请新建任务")
            if frozen_files and frozen_files.get(symbol, {}).get("sha256") not in (None, digest):
                raise ValueError("回测输入已变化，请新建任务")
            frame = pd.read_pickle(data_file)
            if len(frame) >= MIN_WARMUP_BARS:
                histories[symbol] = frame
                report("准备历史行情", number + 1, total, len(excluded))
                continue
        # A frozen file listed in the manifest must not be refetched: reuse it or fail loudly.
        if frozen_files.get(symbol):
            raise ValueError("回测输入已变化，请新建任务")
        try:
            raw = provider().get_daily_data(
                symbol,
                start_date=fetch_start.replace("-", ""),
                end_date=effective_end.replace("-", ""),
            )
        except Exception as exc:  # noqa: BLE001 - one symbol must not fail the whole market run
            exclude(symbol, f"行情获取失败：{exc}")
            report("准备历史行情", number + 1, total, len(excluded))
            continue
        frame = prepare_yearline_bars(raw)
        if frame.empty:
            exclude(symbol, "区间内无历史行情")
            report("准备历史行情", number + 1, total, len(excluded))
            continue
        frame = frame[pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d") <= effective_end].reset_index(drop=True)
        if frame.empty:
            reason = "区间内无历史行情"
        elif _illegal_history_rows(frame) > 0:
            reason = ILLEGAL_HISTORY_REASON
        else:
            in_window = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d")
            warmup = int((in_window < effective_start).sum())
            reason = None if warmup >= MIN_WARMUP_BARS and len(frame) > warmup else f"首个交易日前不足 {MIN_WARMUP_BARS} 根已收盘日线"
        if reason is not None:
            exclude(symbol, reason)
            report("准备历史行情", number + 1, total, len(excluded))
            continue
        # Keep only the warmup MA window plus the replay window.
        keep = frame.tail(len(frame) - warmup + MIN_WARMUP_BARS).reset_index(drop=True)
        write_pickle(data_file, keep)
        ledger["symbols"][symbol] = {"file": str(data_file.name), "sha256": file_sha256(data_file)}
        pending_writes += 1
        flush()
        histories[symbol] = keep
        report("准备历史行情", number + 1, total, len(excluded))
        if interval > 0:
            # Serial, throttled fetching: the market run must not open unbounded sockets.
            time.sleep(interval)
    flush(True)
    if mode == "stock" and excluded:
        # A single-symbol request has no partial-coverage story: exclude nothing, fail loudly.
        raise ValueError(f"{excluded[0]['symbol']} 无法回测：{excluded[0]['reason']}")
    if not histories:
        raise ValueError("所有股票都缺少可用历史行情，回测任务失败")

    # ---- 4. signals with checkpoints ----------------------------------------
    signals: dict[str, dict[str, set[int]]] = {}
    surviving: dict[str, pd.DataFrame] = {}
    for number, (symbol, frame) in enumerate(histories.items()):
        fingerprint = _history_hash(frame)
        checkpoint = task_dir / f"{symbol}.signals.json"
        key = f"{VERSION}:{config_hash}:{fingerprint}:{effective_end}"
        cached = None
        if checkpoint.exists():
            try:
                cached = read_json(checkpoint)
            except (ValueError, OSError):
                cached = None
        if isinstance(cached, dict) and cached.get("key") == key:
            signals[symbol] = {name: set(indices) for name, indices in cached["signals"].items()}
        else:
            try:
                signals[symbol] = entry_indices(frame, config)
            except Exception as exc:  # noqa: BLE001 - drop the symbol, never fake a curve
                excluded.append({"symbol": symbol, "reason": f"策略计算失败：{exc}"})
                ledger["symbols"][symbol] = {"reason": f"策略计算失败：{exc}"}
                report("策略计算", number + 1, len(histories), len(excluded))
                continue
            write_json(checkpoint, {"key": key, "signals": {name: sorted(indices) for name, indices in signals[symbol].items()}})
        surviving[symbol] = frame
        report("策略计算", number + 1, len(histories), len(excluded))
    flush(True)
    if not surviving:
        raise ValueError("所有股票的策略计算都失败，回测任务失败")

    # The manifest fingerprints the same files the ledger tracks, so a retry can compare like with like.
    frozen_files.update({symbol: {"sha256": file_sha256(inputs_dir / f"{symbol}.pkl")} for symbol in surviving})
    manifest = {
        "version": VERSION,
        "config_hash": config_hash,
        "effective_start": effective_start,
        "effective_end": effective_end,
        "fetch_start": fetch_start,
        "files": frozen_files,
        "ledger": ledger_file.name,
        "ledger_sha256": file_sha256(ledger_file),
        "universe_sha256": read_json(manifest_file).get("universe_sha256") if manifest_file.exists() else file_sha256(universe_file),
        "calendar_sha256": read_json(manifest_file).get("calendar_sha256") if manifest_file.exists() else file_sha256(calendar_file),
        "rules_sha256": file_sha256(rules_file),
    }
    write_json(manifest_file, manifest)
    return {
        "histories": surviving,
        "signals": signals,
        "calendar": effective,
        "excluded": excluded,
        "manifest": manifest,
        "universe": universe,
        "config_snapshot": snapshot,
    }


def _history_hash(frame: pd.DataFrame) -> str:
    values = np.ascontiguousarray(frame[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float))
    return hashlib.sha256(values.tobytes()).hexdigest()[:32]


def write_pickle(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_pickle(temporary)
    temporary.replace(path)


def metrics(curve: list[dict], closed_trades: list[dict], initial: float) -> dict:
    equities = np.array([initial] + [float(point["equity"]) for point in curve], dtype=float)
    daily = equities[1:] / equities[:-1] - 1 if len(equities) > 1 else np.array([])
    peak = np.maximum.accumulate(equities)
    deviation = float(np.std(daily, ddof=1)) if len(daily) > 1 else 0.0
    profits = [float(trade["pnl_cash"]) for trade in closed_trades]
    wins, losses = [x for x in profits if x > 0], [x for x in profits if x < 0]
    final = float(equities[-1])
    # No position ever opened: annualised return and drawdown are exactly zero, the
    # ratios that need closed trades or a dispersion estimate stay null.
    return {
        "total_return_pct": (final / initial - 1) * 100,
        "annualized_return_pct": ((final / initial) ** (252 / len(daily)) - 1) * 100 if len(daily) and final > 0 else None,
        "max_drawdown_pct": float(np.max((peak - equities) / peak)) * 100,
        "sharpe_ratio": float(np.mean(daily)) / deviation * math.sqrt(252) if deviation > 1e-12 else None,
        "payoff_ratio": float(np.mean(wins)) / abs(float(np.mean(losses))) if wins and losses else None,
        "win_rate_pct": len(wins) / len(profits) * 100 if profits else None,
        "closed_trades": len(profits),
        "final_equity": final,
    }


def entry_indices(frame: pd.DataFrame, config: dict) -> dict[str, set[int]]:
    """Preselect possible trigger days, then verify with the same live predicate."""
    possibilities = {key: set() for key in STRATEGIES}
    macd = config.get("signal_strategy", {}).get("macd", {})
    enriched = frame.join(calculate_macd(frame["close"], fast=macd.get("fast", 12), slow=macd.get("slow", 26), signal=macd.get("signal", 9)))
    for item in find_golden_cross_entries(enriched, fast=macd.get("fast", 12), slow=macd.get("slow", 26), signal=macd.get("signal", 9), zero_axis_tolerance=macd.get("zero_axis_tolerance", .005), confirmation_bars=macd.get("pullback_confirmation_bars", 5)):
        possibilities["macd_zero_axis"].add(int(item["confirmation_index"]))
    yearline = add_yearline_indicators(frame)
    if len(yearline):
        mask = yearline["ma250_up"] & yearline["ma_align"] & yearline["above250_prev"] & yearline["low_in_zone"] & yearline["close_hold"] & yearline["close_ge_open"]
        possibilities["yearline_pullback"] = set(np.flatnonzero(mask))
    divergence = add_divergence_indicators(frame, resolve_divergence_config(config))
    if len(divergence):
        possibilities["macd_divergence"] = set(np.flatnonzero(divergence["golden_cross"]))
    confirmed = {key: set() for key in STRATEGIES}
    for strategy, indices in possibilities.items():
        for index in sorted(indices):
            result = evaluate_strategies(frame.iloc[:index + 1], config, strategy_ids=[strategy])[0]
            if result["status"] == "error":
                raise ValueError(f"{strategy} 策略计算异常：{'; '.join(result.get('warnings') or [])}")
            if result["status"] == "ok" and result["buy"]:
                confirmed[strategy].add(int(index))
    return confirmed


def strategy_warnings(config: dict, strategy: str) -> list[str]:
    """Disclose a strategy that the live configuration disables or only observes."""
    policy = resolve_signal_execution_policy(config)
    if strategy == "macd_divergence":
        if not resolve_divergence_config(config).get("enabled", True):
            return ["策略配置已禁用：零轴＋底背离未启用，资金保持现金"]
        return []
    if strategy == "macd_zero_axis":
        modes = [
            effective_signal_execution_mode([f"macd_golden_cross_pullback_confirmed_{zone}"], policy)
            for zone in ("above", "near")
        ]
        if not any(mode == "enabled" for mode in modes):
            return ["策略入场已禁用或仅观察：日线零轴金叉在配置中均非 enabled，资金保持现金"]
        if not all(mode == "enabled" for mode in modes):
            return ["受限信号：零轴金叉部分执行状态非 enabled，仅按 enabled 的分支交易"]
    return []


def replay(histories: dict[str, pd.DataFrame], signals: dict[str, dict[str, set[int]]], calendar: list[str], config: dict, options: dict, strategy: str) -> dict:
    initial = float(options.get("initial_cash", 100000))
    max_positions = int(options.get("max_positions", 4))
    allocation = float(options.get("position_size_pct", .25))
    execution = _execution_values(_resolve_execution_config(config))
    rules = exit_parameters(config, strategy)
    locations = {symbol: {str(pd.Timestamp(value).date()): index for index, value in enumerate(frame.datetime)} for symbol, frame in histories.items()}
    candidates: dict[str, list[tuple[str, int]]] = {}
    for symbol, frame in histories.items():
        for index in signals[symbol][strategy]:
            if index + 1 < len(frame):
                day = str(pd.Timestamp(frame.datetime.iloc[index + 1]).date())
                # Signals formed before the selected start date may not open a position.
                if str(pd.Timestamp(frame.datetime.iloc[index]).date()) >= options["start"]:
                    candidates.setdefault(day, []).append((symbol, index + 1))
    cash = initial
    positions: dict[str, dict] = {}
    trades, curve, rejected = [], [], []

    def sell(symbol, index, price, reason, day, session):
        nonlocal cash
        position = positions[symbol]
        resolved = _resolve_sell_fill(symbol, histories[symbol], index, price, session, execution)
        if resolved is None:
            position["pending_exit"] = reason
            return False
        costs = {**execution, "stamp_tax_pct": .0005 if day >= "2023-08-28" else .001}
        received = _sell_cash(resolved[0], position["quantity"], costs)["total"]
        cash += received
        trades.append({"symbol": symbol, "entry_day": position["entry_day"], "exit_day": day, "entry_price": position["entry_price"], "exit_price": resolved[0], "quantity": position["quantity"], "pnl_cash": received - position["entry_cash"], "exit_reason": reason})
        del positions[symbol]
        return True

    for day in calendar:
        # Only opening exits release cash for orders executed at this opening.
        for symbol, position in list(positions.items()):
            index = locations[symbol].get(day)
            if index is not None and position.get("pending_exit") and day > position["entry_day"]:
                sell(symbol, index, float(histories[symbol].iloc[index].open), position["pending_exit"], day, "open")
        for symbol, index in sorted(candidates.get(day, [])):
            if symbol in positions or len(positions) >= max_positions:
                continue
            frame = histories[symbol]
            row = frame.iloc[index]
            price = float(row.open)
            if float(row.volume) <= 0 or price <= 0:
                continue
            limits = _bar_price_limits(symbol, frame, index, execution)
            if limits and price >= limits[0] - .0001:
                rejected.append({"symbol": symbol, "date": day, "reason": "涨停无法买入"})
                continue
            equity = cash + sum(position["quantity"] * position["mark"] for position in positions.values())
            minimum = 200 if symbol.startswith(("688", "689")) else 100
            step = 1 if minimum == 200 or symbol.endswith(".BJ") else 100
            budget = min(cash, equity * allocation)
            quantity = int(budget / price / step) * step
            while quantity >= minimum and _buy_cash(price, quantity, execution)["total"] > budget:
                quantity -= step
            if quantity < minimum:
                continue
            spent = _buy_cash(price, quantity, execution)["total"]
            cash -= spent
            positions[symbol] = {"quantity": quantity, "entry_price": price, "entry_cash": spent, "entry_day": day, "entry_index": index, "mark": price, "pending_exit": None}
        for symbol, position in list(positions.items()):
            index = locations[symbol].get(day)
            if index is None:
                continue
            frame = histories[symbol]
            row = frame.iloc[index]
            position["mark"] = float(row.close)
            if day > position["entry_day"] and float(row.volume) > 0:
                stop = position["entry_price"] * (1 - rules["stop_loss_pct"])
                take = position["entry_price"] * (1 + rules["take_profit_pct"]) if rules["take_profit_pct"] is not None else None
                if float(row.low) <= stop:
                    if sell(symbol, index, min(float(row.open), stop), "stop_loss", day, "intraday"):
                        continue
                elif take is not None and float(row.high) >= take:
                    if sell(symbol, index, max(float(row.open), take), "take_profit", day, "intraday"):
                        continue
            # Attribute the synthetic holding to the replay strategy so a real holding's
            # strategy override applies here; without the tag the stop would fall back to
            # the global one and silently neutralize the backtest.
            checks, _ = evaluate_exits(frame.iloc[:index + 1], config, strategy, {"shares": position["quantity"], "cost_price": position["entry_price"], "opened_on": position["entry_day"], "strategy_id": strategy})
            triggers = [check["name"] for check in checks if check["met"] is True]
            if triggers:
                position["pending_exit"] = " / ".join(triggers)
        equity = cash + sum(position["quantity"] * position["mark"] for position in positions.values())
        curve.append({"date": day, "equity": round(equity, 6), "cash": round(cash, 6), "positions": len(positions), "return_pct": (equity / initial - 1) * 100})
    opened = len(positions)
    return {"strategy_id": strategy, "name": STRATEGIES[strategy], "version": VERSION, "metrics": metrics(curve, trades, initial), "equity_curve": curve, "trades": trades, "open_positions": opened, "rejected": rejected, "warnings": strategy_warnings(config, strategy)}


def run_backtest(config: dict, options: dict) -> dict:
    """Prepare frozen inputs, replay all three strategies, and write the report."""
    start, end = date.fromisoformat(str(options["start"])), date.fromisoformat(str(options["end"]))
    if start >= end:
        raise ValueError("回测结束日期必须晚于开始日期")
    task_dir = Path(options["output_dir"])
    task_dir.mkdir(parents=True, exist_ok=True)
    prepared = prepare_backtest_inputs(config, options)
    histories, signals, calendar = prepared["histories"], prepared["signals"], prepared["calendar"]
    universe = prepared["universe"]
    results = []
    for strategy in STRATEGIES:
        results.append(replay(histories, signals, calendar, config, options, strategy))
        write_json(task_dir / "progress.json", {"stage": "组合回放", "processed": len(results), "total": len(STRATEGIES), "excluded": len(prepared["excluded"])})
    included = len(histories)
    report = {
        "schema_version": 1,
        "kind": "backtest",
        "options": _public_options(options),
        "strategy_version": VERSION,
        "data_manifest": prepared["manifest"],
        "universe": {
            "scope": universe["scope"],
            "as_of": universe["as_of"],
            "total": included + len(prepared["excluded"]),
            "included": included,
            "excluded": len(prepared["excluded"]),
        },
        "effective_start": calendar[0],
        "effective_end": calendar[-1],
        "config_snapshot": prepared["config_snapshot"],
        "excluded": prepared["excluded"],
        "calendar_days": len(calendar),
        "results": results,
        "warnings": [WARNING_SURVIVORSHIP, WARNING_LIMITS, WARNING_SIGNAL_PARITY, WARNING_GATE_SCOPE],
    }
    output = task_dir / "report.json"
    write_json(output, report)
    write_json(task_dir / "progress.json", {"stage": "完成", "processed": len(STRATEGIES), "total": len(STRATEGIES), "excluded": len(prepared["excluded"])})
    return {"output_file": str(output.resolve()), "strategies": len(results), "stocks": included}
