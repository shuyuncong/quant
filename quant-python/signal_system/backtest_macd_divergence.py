"""回测 macd_divergence 研究池策略 (独立, 不修改生产配置与生产回测脚本)。

信号源: strategy/macd_divergence.py 的四条件 AND (零轴金叉 + 底背离 + 放量 + 年线以上)。
成交/风控: 复用 backtest_winrate 的同一套执行语义 (次日开盘买入、T+1、涨跌停、
佣金/印花税/滑点、固定止损止盈、持仓超时), 因此结果与既有回测可比。

用法:
    python backtest_macd_divergence.py --limit 300 --start 2023-01-01
    python backtest_macd_divergence.py --min-volume-ratio 1.0 --out report_v1.json

输出: 逐笔信号、组合资金曲线、与逐条件放宽的对比 (volume/背离 是否加门槛)。
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(BASE_DIR.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import backtest_winrate as bt  # noqa: E402
from strategy.macd_divergence import (  # noqa: E402
    MIN_BARS,
    add_divergence_indicators,
    prepare_divergence_bars,
    resolve_divergence_config,
)
from utils.helpers import load_config  # noqa: E402


def _completed_cycles(hist: np.ndarray, low: np.ndarray, min_bars: int) -> list[tuple[int, float, float]]:
    """Completed negative histogram runs as (end_index, abs_area, lowest_low)."""
    negative = np.isfinite(hist) & (hist < 0)
    if not negative.any():
        return []
    changes = np.flatnonzero(np.diff(negative.astype(np.int8)) != 0) + 1
    bounds = np.concatenate(([0], changes, [len(negative)]))
    cycles: list[tuple[int, float, float]] = []
    for start, stop in zip(bounds[:-1], bounds[1:]):
        if not negative[start]:
            continue
        if stop - start < min_bars:
            continue
        segment = hist[start:stop]
        prices = low[start:stop]
        cycles.append((stop - 1, float(np.abs(segment).sum()), float(np.nanmin(prices))))
    return cycles


def _divergence_flags(
    hist: np.ndarray,
    low: np.ndarray,
    min_bars: int,
) -> np.ndarray:
    """Per-bar boolean: two latest completed negative cycles show bottom divergence.

    Fast equivalent of strategy.macd_divergence.bottom_divergence_at, verified
    against it in tests. Only cycles closed strictly before the bar count.
    """
    cycles = _completed_cycles(hist, low, min_bars)
    flags = np.zeros(len(hist), dtype=bool)
    if len(cycles) < 2:
        return flags
    index = 2
    for position in range(1, len(cycles)):
        end_prev, area_prev, low_prev = cycles[position - 1]
        end_cur, area_cur, low_cur = cycles[position]
        if area_prev <= 0 or low_prev == 0:
            continue
        divergent = bool(low_cur < low_prev and area_cur < area_prev)
        start = max(end_cur + 1, index)
        if start >= len(flags):
            break
        if divergent:
            flags[start:] = True
        index = start
        if not divergent:
            flags[start:] = False
    return flags


def build_entries(
    symbol: str,
    closed: pd.DataFrame,
    settings: dict[str, Any],
    min_volume_ratio: float | None = None,
) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Return (buy records, per-bar all-four-condition mask)."""
    frame = add_divergence_indicators(closed, settings)
    if frame.empty or len(frame) < MIN_BARS:
        return [], np.zeros(0, dtype=bool)
    dif = pd.to_numeric(frame["dif"], errors="coerce").to_numpy(dtype=float)
    dea = pd.to_numeric(frame["dea"], errors="coerce").to_numpy(dtype=float)
    hist = pd.to_numeric(frame["hist"], errors="coerce").to_numpy(dtype=float)
    low = pd.to_numeric(frame["low"], errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    ma_long = pd.to_numeric(frame["ma_long"], errors="coerce").to_numpy(dtype=float)
    ma_long_prev = pd.to_numeric(frame["ma_long_prev"], errors="coerce").to_numpy(dtype=float)
    volume_ratio = pd.to_numeric(frame["volume_ratio"], errors="coerce").to_numpy(dtype=float)
    cross = frame["golden_cross"].to_numpy(dtype=bool)
    tolerance = float(settings["zero_axis_tolerance"])
    threshold = float(
        settings["min_volume_ratio"] if min_volume_ratio is None else min_volume_ratio
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        distance = np.maximum(np.abs(dif), np.abs(dea)) / np.maximum(np.abs(close), 1e-12)
        near = distance <= tolerance
        zone_ok = near | (dif * dea <= 0) | ((dif > 0) & (dea > 0))
    volume_ok = volume_ratio >= threshold
    above = (close > ma_long) & (ma_long > ma_long_prev)
    divergence = _divergence_flags(hist, low, int(settings["min_macd_segment_bars"]))
    mask = (
        np.nan_to_num(cross, nan=False).astype(bool)
        & np.nan_to_num(zone_ok, nan=False).astype(bool)
        & np.nan_to_num(volume_ok, nan=False).astype(bool)
        & np.nan_to_num(above, nan=False).astype(bool)
        & divergence
    )
    valid = np.arange(len(frame)) >= (MIN_BARS - 1)
    mask &= valid
    buys: list[dict[str, Any]] = []
    dates = pd.to_datetime(frame["datetime"])
    for index in np.flatnonzero(mask):
        index = int(index)
        zone = "above" if (dif[index] > 0 and dea[index] > 0) else "near"
        buys.append(
            {
                "symbol": symbol,
                "day": str(dates.iloc[index].date()),
                "signal_type": "macd_divergence_bottom",
                "side": "buy",
                "price": float(close[index]),
                "confirmed_at": str(dates.iloc[index]),
                "zone": zone,
                "volume_ratio": float(volume_ratio[index]),
                "area_ratio": None,
            }
        )
    return buys, mask


def build_production_entries(
    symbol: str,
    closed: pd.DataFrame,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Production MACD pullback-confirmed entries, for a head-to-head baseline.

    Delegates to backtest_winrate.find_signals so the baseline uses the exact
    production signal definition (no re-implementation drift).
    """
    found = bt.find_signals(closed, config)
    buys: list[dict[str, Any]] = []
    for record in found.get("buy", []):
        if not str(record.get("signal_type", "")).startswith(
            "macd_golden_cross_pullback_confirmed_"
        ):
            continue
        buys.append({"symbol": symbol, **record})
    return buys


def build_yearline_entries(
    symbol: str,
    closed: pd.DataFrame,
    vol_mult: float = 1.5,
    tolerance: float = 0.025,
    cooldown: int = 20,
) -> list[dict[str, Any]]:
    """Yearline-pool entries, mirrored from yearline_trend_strategy_backtest.

    Covers both production entry types (A_breakout: volume close above the
    yearline; B_pullback: touch-and-hold of the yearline). Indicators are the
    same causal definitions, so the comparison runs on shared execution.
    """
    import yearline_trend_strategy_backtest as yl

    frame = yl.add_indicators(closed)
    if len(frame) < yl.MA_YEAR + yl.MA_SLOPE_LOOKBACK + 5:
        return []
    signals = yl.generate_signals(
        symbol,
        "",
        frame,
        vol_mult=vol_mult,
        tol=tolerance,
        cooldown=cooldown,
    )
    return [
        {
            "symbol": symbol,
            "day": str(pd.Timestamp(signal.entry_date).date()),
            "signal_type": f"yearline_{signal.entry_type}",
            "side": "buy",
            "price": float(signal.entry_price),
            "confirmed_at": str(pd.Timestamp(signal.entry_date)),
            "volume_ratio": float(signal.vol_ratio),
        }
        for signal in signals
    ]


def run_arm(
    args: argparse.Namespace,
    signal_name: str,
    settings: dict[str, Any] | None,
    config: dict[str, Any],
    costs: dict[str, Any],
    portfolio_config: dict[str, Any],
    paths: list[Path],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Run one arm (divergence variant or the production baseline)."""
    trades: list[dict[str, Any]] = []
    rejections: Counter = Counter()
    stats = {
        "symbols_with_history": 0,
        "symbols_evaluated": 0,
        "signal_count": 0,
        "insufficient_history": 0,
    }
    for path in paths:
        symbol = path.name.split("_")[0]
        try:
            raw = pd.read_pickle(path)
        except Exception:
            continue
        closed = bt.prepare_closed_bars(raw)
        if closed is None or len(closed) < MIN_BARS:
            stats["insufficient_history"] += 1
            continue
        stats["symbols_with_history"] += 1
        if signal_name == "production_macd_pullback":
            buys = build_production_entries(symbol, closed, config)
        elif signal_name.startswith("yearline_"):
            buys = build_yearline_entries(symbol, closed)
        else:
            assert settings is not None
            buys, _ = build_entries(symbol, closed, settings)
        dates = [item.date() for item in pd.to_datetime(closed["datetime"])]
        window = [b for b in buys if start <= date.fromisoformat(str(b["day"])) <= end]
        if not window:
            continue
        stats["symbols_evaluated"] += 1
        stats["signal_count"] += len(window)
        for buy in window:
            trade, reason = bt.simulate_single_trade(
                symbol, closed, dates, buy, {}, costs
            )
            if trade is None:
                rejections[reason or "unknown"] += 1
                continue
            trades.append(trade)
    arm: dict[str, Any] = {
        "signal": signal_name,
        "stats": stats,
        "execution_rejections": dict(rejections),
        "trade_count": len(trades),
    }
    if trades:
        arm["single_trade_summary"] = bt.summarize(trades)
        portfolio_result = bt.run_portfolio(
            copy.deepcopy(trades), costs, copy.deepcopy(portfolio_config)
        )
        arm["portfolio_summary"] = portfolio_result["summary"]
        arm["portfolio_rejections"] = portfolio_result["rejection_reasons"]
    return arm


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(str(Path(args.config).expanduser().resolve()))
    if config is None:
        raise RuntimeError("unable to load config")
    settings = resolve_divergence_config(config)
    costs = bt._resolve_execution_config(config)
    position = config.get("position", {}) or {}
    portfolio_config = {
        "initial_cash": float(costs.get("initial_cash", 100000.0)),
        "max_positions": int(position.get("max_stocks", 4)),
        "position_size_pct": float(position.get("base_position_per_stock", 0.25)),
        "lot_size": int(costs.get("lot_size", 100)),
        "signal_priority": costs.get("signal_priority"),
        "score_mode": "P0",
        "tie_break": "symbol_asc",
        "seed": int(args.seed),
    }

    paths = sorted((BASE_DIR / "cache" / "daily_history").glob("*_qfq.pkl"))
    if args.symbols:
        wanted = {item.strip().zfill(6) for item in args.symbols.split(",") if item.strip()}
        paths = [p for p in paths if p.name.split("_")[0] in wanted]
    if args.limit:
        paths = paths[: args.limit]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    # Arms: the four-condition AND plus threshold sweeps and the production
    # signal on identical data/execution, so differences come from the signal.
    # Selectable so the expensive production (chan) arm can run in parallel.
    arm_specs: list[tuple[str, dict[str, Any] | None]] = []
    for token in [item.strip() for item in args.arms.split(",") if item.strip()]:
        if token == "v1":
            arm_specs.append(("divergence_v1", copy.deepcopy(settings)))
        elif token.startswith("vol:"):
            ratio = float(token.split(":", 1)[1])
            variant = copy.deepcopy(settings)
            variant["min_volume_ratio"] = ratio
            arm_specs.append((f"divergence_vol_{ratio}", variant))
        elif token == "no_div":
            variant = copy.deepcopy(settings)
            variant["_skip_divergence"] = True
            arm_specs.append(("divergence_no_bottom_divergence", variant))
        elif token == "baseline":
            arm_specs.append(("production_macd_pullback", None))
        elif token == "yearline":
            arm_specs.append(("yearline_all", None))
        else:
            raise SystemExit(f"unknown arm: {token}")
    if args.min_volume_ratio is not None:
        for _, spec in arm_specs:
            if spec is not None:
                spec["min_volume_ratio"] = float(args.min_volume_ratio)

    arms: dict[str, Any] = {}
    for name, arm_settings in arm_specs:
        if arm_settings is not None and arm_settings.pop("_skip_divergence", False):
            arms[name] = _run_skip_divergence_arm(
                args, name, arm_settings, costs, portfolio_config, paths, start, end
            )
            continue
        arms[name] = run_arm(
            args, name, arm_settings, config, costs, portfolio_config, paths, start, end
        )

    result = {
        "window": {"start": args.start, "end": args.end},
        "universe": {
            "history_dir": str(BASE_DIR / "cache" / "daily_history"),
            "symbols_considered": len(paths),
            "adjust": "qfq",
        },
        "execution": {
            "stop_loss_pct": costs.get("stop_loss_pct"),
            "take_profit_pct": costs.get("take_profit_pct"),
            "max_holding_bars": costs.get("chan_zero_axis", {}).get("max_holding_bars"),
            "commission_pct": costs.get("commission_pct"),
            "stamp_tax_pct": costs.get("stamp_tax_pct"),
            "slippage_pct": costs.get("slippage_pct"),
            "t_plus_one": costs.get("t_plus_one"),
            "price_limit_model": costs.get("price_limit_model"),
            "portfolio": portfolio_config,
        },
        "gates_applied": {
            "market_gate": False,
            "stock_pool": False,
            "note": "两条臂都不加市场/股票池闸门, 差异只来自信号本身",
        },
        "arms": arms,
    }
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = BASE_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        result["output_file"] = str(out_path)
    return result


def _run_skip_divergence_arm(
    args: argparse.Namespace,
    name: str,
    settings: dict[str, Any],
    costs: dict[str, Any],
    portfolio_config: dict[str, Any],
    paths: list[Path],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Ablation: same three gates, bottom-divergence requirement removed."""
    trades: list[dict[str, Any]] = []
    rejections: Counter = Counter()
    stats = {"symbols_with_history": 0, "symbols_evaluated": 0, "signal_count": 0}
    for path in paths:
        symbol = path.name.split("_")[0]
        try:
            raw = pd.read_pickle(path)
        except Exception:
            continue
        closed = bt.prepare_closed_bars(raw)
        if closed is None or len(closed) < MIN_BARS:
            continue
        stats["symbols_with_history"] += 1
        buys = _build_entries_no_divergence(symbol, closed, settings)
        dates = [item.date() for item in pd.to_datetime(closed["datetime"])]
        window = [b for b in buys if start <= date.fromisoformat(str(b["day"])) <= end]
        if not window:
            continue
        stats["symbols_evaluated"] += 1
        stats["signal_count"] += len(window)
        for buy in window:
            trade, reason = bt.simulate_single_trade(
                symbol, closed, dates, buy, {}, costs
            )
            if trade is None:
                rejections[reason or "unknown"] += 1
                continue
            trades.append(trade)
    arm: dict[str, Any] = {
        "signal": name,
        "stats": stats,
        "execution_rejections": dict(rejections),
        "trade_count": len(trades),
    }
    if trades:
        arm["single_trade_summary"] = bt.summarize(trades)
        portfolio_result = bt.run_portfolio(
            copy.deepcopy(trades), costs, copy.deepcopy(portfolio_config)
        )
        arm["portfolio_summary"] = portfolio_result["summary"]
        arm["portfolio_rejections"] = portfolio_result["rejection_reasons"]
    del args
    return arm


def _build_entries_no_divergence(
    symbol: str,
    closed: pd.DataFrame,
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Golden cross + volume + yearline, without the divergence requirement."""
    frame = add_divergence_indicators(closed, settings)
    if frame.empty or len(frame) < MIN_BARS:
        return []
    dif = pd.to_numeric(frame["dif"], errors="coerce").to_numpy(dtype=float)
    dea = pd.to_numeric(frame["dea"], errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    ma_long = pd.to_numeric(frame["ma_long"], errors="coerce").to_numpy(dtype=float)
    ma_long_prev = pd.to_numeric(frame["ma_long_prev"], errors="coerce").to_numpy(dtype=float)
    volume_ratio = pd.to_numeric(frame["volume_ratio"], errors="coerce").to_numpy(dtype=float)
    tolerance = float(settings["zero_axis_tolerance"])
    with np.errstate(invalid="ignore", divide="ignore"):
        distance = np.maximum(np.abs(dif), np.abs(dea)) / np.maximum(np.abs(close), 1e-12)
        zone_ok = (distance <= tolerance) | (dif * dea <= 0) | ((dif > 0) & (dea > 0))
    mask = (
        np.nan_to_num(frame["golden_cross"].to_numpy(dtype=bool), nan=False).astype(bool)
        & np.nan_to_num(zone_ok, nan=False).astype(bool)
        & np.nan_to_num(volume_ratio >= float(settings["min_volume_ratio"]), nan=False).astype(bool)
        & np.nan_to_num((close > ma_long) & (ma_long > ma_long_prev), nan=False).astype(bool)
        & (np.arange(len(frame)) >= (MIN_BARS - 1))
    )
    dates = pd.to_datetime(frame["datetime"])
    return [
        {
            "symbol": symbol,
            "day": str(dates.iloc[int(index)].date()),
            "signal_type": "macd_divergence_no_divergence",
            "side": "buy",
            "price": float(close[int(index)]),
            "confirmed_at": str(dates.iloc[int(index)]),
        }
        for index in np.flatnonzero(mask)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest the macd_divergence research pool")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--symbols", type=str, default=None, help="comma separated codes")
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--min-volume-ratio", type=float, default=None)
    parser.add_argument(
        "--arms",
        default="v1",
        help="comma list: v1,vol:1.0,vol:1.2,vol:2.0,no_div,baseline",
    )
    parser.add_argument("--mode", choices=("signal", "portfolio"), default="portfolio")
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--out", default="divergence_backtest_report.json")
    args = parser.parse_args()
    result = run(args)
    compact = {
        "window": result["window"],
        "universe": result["universe"],
        "execution": result["execution"],
        "arms": {
            name: {
                "signal_count": arm["stats"]["signal_count"],
                "trade_count": arm["trade_count"],
                "win_rate": arm.get("single_trade_summary", {}).get("win_rate"),
                "avg_pnl_pct": arm.get("single_trade_summary", {}).get("avg_pnl_pct"),
                "profit_factor": arm.get("single_trade_summary", {}).get("profit_factor"),
                "portfolio": {
                    key: arm.get("portfolio_summary", {}).get(key)
                    for key in (
                        "count",
                        "win_rate",
                        "avg_pnl_pct",
                        "profit_factor",
                        "total_return_pct",
                        "max_drawdown_pct",
                        "final_equity",
                        "avg_holding_days",
                    )
                },
            }
            for name, arm in result["arms"].items()
        },
        "output_file": result.get("output_file"),
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
