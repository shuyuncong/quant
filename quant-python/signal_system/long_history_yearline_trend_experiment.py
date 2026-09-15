"""Causal development backtest for a long-term yearline trend strategy.

The experiment is deliberately parameter-frozen: it does not search entry or
stop thresholds, does not use Holdout data, and never writes production config.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import (  # noqa: E402
    HISTORY_DIR,
    _resolve_execution_config,
    prepare_closed_bars,
    run_portfolio,
    simulate_single_trade,
)
from utils.helpers import load_config  # noqa: E402


VERSION = "long_history_yearline_trend_experiment.v1"
DEFAULT_UNIVERSE = (
    BASE_DIR
    / "cache"
    / "49c74bcce8953772e779e483af108c878820d52b5b8425db964fddb98a07f2b6.pkl"
)
DEFAULT_INDEX = BASE_DIR / "cache" / "index_000001_sh.pkl"
DEFAULT_CONFIG = BASE_DIR / "config" / "config.yaml"

MA_YEAR = 250
MA_SLOPE_LOOKBACK = 20
VOLUME_LOOKBACK = 20
BREAKOUT_VOLUME_RATIO = 1.5
PULLBACK_ABOVE_TOLERANCE = 0.02
PULLBACK_BELOW_TOLERANCE = 0.005
SIGNAL_COOLDOWN_BARS = 20
MAX_HOLDING_BARS = 120
ATR_MULTIPLE = 2.0
MIN_STOP_LOSS_PCT = 0.05
MAX_STOP_LOSS_PCT = 0.08
STOP_MODES = ("fixed_sl8", "fixed_sl5", "dynamic_sl5_sl8")
ROUTES = ("volume_breakout", "yearline_pullback", "combined")
SPLITS = {
    "train": (date(2024, 7, 1), date(2024, 12, 31)),
    "val": (date(2025, 1, 1), date(2025, 8, 31)),
    "viewed_test": (date(2025, 9, 1), date(2026, 2, 27)),
}
BOOTSTRAP_SEED = 20260914


@dataclass(frozen=True)
class Signal:
    symbol: str
    name: str
    signal_type: str
    index: int
    signal_day: str
    signal_close: float
    ma250: float
    ma250_slope_20: float
    ma60: float
    ma120: float
    volume_ratio_20: float
    atr14_pct: float

    @property
    def candidate_id(self) -> str:
        return f"{self.symbol}|{self.signal_day}|{self.signal_type}"


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise ValueError(f"Holdout path is blocked: {resolved}")
    return resolved


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    result = prepare_closed_bars(frame).copy()
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce").astype(float)
    close = result["close"]
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["ma60"] = close.rolling(60, min_periods=60).mean()
    result["ma120"] = close.rolling(120, min_periods=120).mean()
    result["ma250"] = close.rolling(MA_YEAR, min_periods=MA_YEAR).mean()
    result["ma250_slope_20"] = (
        result["ma250"] / result["ma250"].shift(MA_SLOPE_LOOKBACK) - 1.0
    )
    result["prior_volume_mean20"] = (
        result["volume"]
        .shift(1)
        .rolling(VOLUME_LOOKBACK, min_periods=VOLUME_LOOKBACK)
        .mean()
    )
    result["atr14"] = true_range.ewm(
        alpha=1.0 / 14.0,
        adjust=False,
        min_periods=14,
    ).mean()
    return result


def _long_term_trend(row: pd.Series) -> bool:
    values = [
        row["ma60"],
        row["ma120"],
        row["ma250"],
        row["ma250_slope_20"],
    ]
    return bool(
        all(math.isfinite(float(value)) for value in values)
        and float(row["ma250_slope_20"]) > 0.0
        and float(row["ma60"]) > float(row["ma120"]) > float(row["ma250"])
    )


def _raw_signals(symbol: str, name: str, frame: pd.DataFrame) -> dict[str, list[Signal]]:
    signals: dict[str, list[Signal]] = {
        "volume_breakout": [],
        "yearline_pullback": [],
    }
    last_by_type = {key: -10**9 for key in signals}
    start = MA_YEAR + MA_SLOPE_LOOKBACK
    for index in range(start, len(frame)):
        row = frame.iloc[index]
        previous = frame.iloc[index - 1]
        if not _long_term_trend(row):
            continue
        required = (
            row["close"],
            row["open"],
            row["low"],
            row["volume"],
            row["prior_volume_mean20"],
            row["atr14"],
            previous["close"],
            previous["ma250"],
        )
        if not all(math.isfinite(float(value)) for value in required):
            continue
        prior_volume = float(row["prior_volume_mean20"])
        if prior_volume <= 0.0 or float(row["atr14"]) <= 0.0:
            continue
        volume_ratio = float(row["volume"]) / prior_volume
        common = {
            "symbol": symbol,
            "name": name,
            "index": index,
            "signal_day": pd.Timestamp(row["datetime"]).date().isoformat(),
            "signal_close": float(row["close"]),
            "ma250": float(row["ma250"]),
            "ma250_slope_20": float(row["ma250_slope_20"]),
            "ma60": float(row["ma60"]),
            "ma120": float(row["ma120"]),
            "volume_ratio_20": volume_ratio,
            "atr14_pct": float(row["atr14"]) / float(row["close"]),
        }
        breakout = (
            float(previous["close"]) <= float(previous["ma250"])
            and float(row["close"]) > float(row["ma250"])
            and volume_ratio >= BREAKOUT_VOLUME_RATIO
        )
        if breakout and index - last_by_type["volume_breakout"] >= SIGNAL_COOLDOWN_BARS:
            signals["volume_breakout"].append(
                Signal(signal_type="yearline_volume_breakout", **common)
            )
            last_by_type["volume_breakout"] = index

        lower = float(row["ma250"]) * (1.0 - PULLBACK_BELOW_TOLERANCE)
        upper = float(row["ma250"]) * (1.0 + PULLBACK_ABOVE_TOLERANCE)
        pullback = (
            float(previous["close"]) > float(previous["ma250"])
            and lower <= float(row["low"]) <= upper
            and float(row["close"]) >= float(row["ma250"])
            and float(row["close"]) >= float(row["open"])
        )
        if pullback and index - last_by_type["yearline_pullback"] >= SIGNAL_COOLDOWN_BARS:
            signals["yearline_pullback"].append(
                Signal(signal_type="yearline_pullback_hold", **common)
            )
            last_by_type["yearline_pullback"] = index
    return signals


def _combined_signals(by_route: dict[str, list[Signal]]) -> list[Signal]:
    ordered = sorted(
        [*by_route["volume_breakout"], *by_route["yearline_pullback"]],
        key=lambda signal: (
            signal.index,
            0 if signal.signal_type == "yearline_volume_breakout" else 1,
        ),
    )
    result: list[Signal] = []
    last_index = -10**9
    for signal in ordered:
        if signal.index - last_index < SIGNAL_COOLDOWN_BARS:
            continue
        result.append(signal)
        last_index = signal.index
    return result


def _stop_loss_pct(signal: Signal, mode: str) -> float:
    if mode == "fixed_sl8":
        return MAX_STOP_LOSS_PCT
    if mode == "fixed_sl5":
        return MIN_STOP_LOSS_PCT
    if mode != "dynamic_sl5_sl8":
        raise ValueError(f"unknown stop mode: {mode}")
    return float(
        np.clip(
            ATR_MULTIPLE * signal.atr14_pct,
            MIN_STOP_LOSS_PCT,
            MAX_STOP_LOSS_PCT,
        )
    )


def _trend_breaks(frame: pd.DataFrame) -> dict[int, str]:
    return {
        index: "yearline_trend_break"
        for index in range(len(frame))
        if pd.notna(frame.iloc[index]["ma250"])
        and float(frame.iloc[index]["close"]) < float(frame.iloc[index]["ma250"])
    }


def _simulate(
    signal: Signal,
    frame: pd.DataFrame,
    dates: list[date],
    trend_breaks: dict[int, str],
    execution: dict[str, Any],
    stop_mode: str,
) -> tuple[dict[str, Any] | None, str | None]:
    costs = copy.deepcopy(execution)
    stop_pct = _stop_loss_pct(signal, stop_mode)
    costs["stop_loss_pct"] = stop_pct
    trade, reason = simulate_single_trade(
        signal.symbol,
        frame,
        dates,
        {
            "day": signal.signal_day,
            "side": "buy",
            "signal_type": signal.signal_type,
            "price": signal.signal_close,
            "confirmed_at": signal.signal_day,
        },
        trend_breaks,
        costs,
        allow_incomplete=False,
    )
    if trade is None:
        return None, reason
    trade.update(
        {
            "candidate_id": signal.candidate_id,
            "signal_day": signal.signal_day,
            "signal_close": round(signal.signal_close, 6),
            "ma250": round(signal.ma250, 6),
            "ma250_slope_20": round(signal.ma250_slope_20, 8),
            "ma60": round(signal.ma60, 6),
            "ma120": round(signal.ma120, 6),
            "volume_ratio_20": round(signal.volume_ratio_20, 6),
            "atr14_pct": round(signal.atr14_pct, 8),
            "stop_mode": stop_mode,
            "stop_loss_pct": round(stop_pct, 8),
            "trade_pnl_pct": trade["pnl_pct"],
        }
    )
    return trade, None


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = np.asarray([float(row["trade_pnl_pct"]) for row in rows], dtype=float)
    if values.size == 0:
        return {"count": 0}
    return {
        "count": int(values.size),
        "mean_pnl_pct": round(float(values.mean()), 4),
        "median_pnl_pct": round(float(np.median(values)), 4),
        "win_rate_pct": round(float((values > 0).mean() * 100.0), 2),
        "p10_pnl_pct": round(float(np.quantile(values, 0.10)), 4),
        "worst_pnl_pct": round(float(values.min()), 4),
        "mean_holding_bars": round(
            float(np.mean([row["holding_bars"] for row in rows])), 2
        ),
        "exit_reason_counts": dict(
            sorted(Counter(str(row["exit_reason"]) for row in rows).items())
        ),
    }


def _paired_delta(
    baseline: list[dict[str, Any]],
    variant: list[dict[str, Any]],
) -> dict[str, Any]:
    base = {str(row["candidate_id"]): row for row in baseline}
    other = {str(row["candidate_id"]): row for row in variant}
    if set(base) != set(other):
        raise RuntimeError("stop variants do not contain identical candidate IDs")
    values = np.asarray(
        [
            float(other[key]["trade_pnl_pct"])
            - float(base[key]["trade_pnl_pct"])
            for key in sorted(base)
        ],
        dtype=float,
    )
    if values.size == 0:
        return {"n": 0}
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    if values.size == 1:
        samples = values
    else:
        samples = rng.choice(values, size=(5000, values.size), replace=True).mean(axis=1)
    low, high = np.quantile(samples, [0.025, 0.975])
    return {
        "n": int(values.size),
        "mean_delta_pp": round(float(values.mean()), 4),
        "median_delta_pp": round(float(np.median(values)), 4),
        "positive_pct": round(float((values > 0).mean() * 100.0), 2),
        "changed_count": int((np.abs(values) > 1e-9).sum()),
        "iid_bootstrap_ci95_low": round(float(low), 4),
        "iid_bootstrap_ci95_high": round(float(high), 4),
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: str(row["candidate_id"]))
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in ordered:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {"path": str(path), "rows": len(ordered), "sha256": _sha256(path)}


def _index_return(index_frame: pd.DataFrame, start: str, end: str) -> float | None:
    values = index_frame[
        (index_frame["datetime"] >= pd.Timestamp(start))
        & (index_frame["datetime"] <= pd.Timestamp(end))
    ]
    if len(values) < 2:
        return None
    return round(
        (float(values["close"].iloc[-1]) / float(values["close"].iloc[0]) - 1.0)
        * 100.0,
        4,
    )


def _portfolio(
    rows: list[dict[str, Any]],
    execution: dict[str, Any],
    index_frame: pd.DataFrame,
) -> dict[str, Any]:
    result = run_portfolio(
        rows,
        execution,
        {
            "initial_cash": 100000.0,
            "max_positions": 4,
            "position_size_pct": 0.25,
            "lot_size": 100,
            "signal_priority": [
                "yearline_volume_breakout",
                "yearline_pullback_hold",
            ],
            "score_mode": "P0",
            "tie_break": "symbol_asc",
            "seed": 20260830,
        },
    )
    summary = dict(result["summary"])
    curve = result["equity_curve"]
    summary["index_return_same_calendar_pct"] = (
        _index_return(index_frame, curve[0]["day"], curve[-1]["day"])
        if curve
        else None
    )
    return {
        "summary": summary,
        "attribution": result["attribution"],
        "rejection_reasons": result["rejection_reasons"],
    }


def _screen(route_report: dict[str, Any]) -> dict[str, Any]:
    train = route_report["splits"]["train"]
    val = route_report["splits"]["val"]
    train_dynamic = train["profiles"]["dynamic_sl5_sl8"]["candidate_summary"]
    val_dynamic = val["profiles"]["dynamic_sl5_sl8"]["candidate_summary"]
    train_delta = train["paired_vs_fixed_sl8"]["dynamic_sl5_sl8"]
    val_delta = val["paired_vs_fixed_sl8"]["dynamic_sl5_sl8"]
    val_portfolio = val["profiles"]["dynamic_sl5_sl8"]["portfolio"]["summary"]
    val_baseline_portfolio = val["profiles"]["fixed_sl8"]["portfolio"]["summary"]
    checks = {
        "train_dynamic_candidate_mean_positive": train_dynamic.get("mean_pnl_pct", -1) > 0,
        "validation_dynamic_candidate_mean_positive": val_dynamic.get("mean_pnl_pct", -1) > 0,
        "train_dynamic_not_worse_than_fixed_sl8": train_delta.get("mean_delta_pp", -1) >= 0,
        "validation_dynamic_not_worse_than_fixed_sl8": val_delta.get("mean_delta_pp", -1) >= 0,
        "validation_dynamic_delta_ci_low_non_negative": val_delta.get(
            "iid_bootstrap_ci95_low", -1
        ) >= 0,
        "validation_portfolio_return_positive": val_portfolio.get(
            "total_return_pct", -1
        ) > 0,
        "validation_portfolio_not_worse_than_fixed_sl8": val_portfolio.get(
            "total_return_pct", -1
        ) >= val_baseline_portfolio.get("total_return_pct", math.inf),
        "validation_candidates_at_least_100": val_dynamic.get("count", 0) >= 100,
    }
    return {
        "checks": checks,
        "passes_research_screen": all(checks.values()),
        "production_eligible": False,
        "holdout_used": False,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    universe_path = _guard_development_path(Path(args.universe))
    index_path = _guard_development_path(Path(args.index_data))
    config_path = _guard_development_path(Path(args.config))
    output_dir = _guard_development_path(Path(args.output_dir))
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite output directory: {output_dir}")
    for path in (universe_path, index_path, config_path):
        if not path.exists():
            raise FileNotFoundError(path)
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")
    execution = _resolve_execution_config(config)
    execution["chan_zero_axis"] = {
        "max_holding_bars": MAX_HOLDING_BARS,
        "timeout_exit_mode": "fixed",
    }
    execution["profit_protection"] = {"mode": "none"}
    execution["take_profit_pct"] = 0.30

    universe = pd.read_pickle(universe_path)
    if not {"code", "name"}.issubset(universe.columns):
        raise ValueError("universe requires code and name columns")
    universe_rows = sorted(
        [
            (str(row.code).zfill(6), str(row.name))
            for row in universe.itertuples(index=False)
        ]
    )
    if len({symbol for symbol, _ in universe_rows}) != len(universe_rows):
        raise ValueError("universe contains duplicate symbols")
    index_frame = prepare_closed_bars(pd.read_pickle(index_path))
    index_frame["datetime"] = pd.to_datetime(index_frame["datetime"])

    rows_by_route_stop_split: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
        route: {
            stop: {split: [] for split in SPLITS}
            for stop in STOP_MODES
        }
        for route in ROUTES
    }
    skipped: Counter[str] = Counter()
    history_entries: list[str] = []
    usable_symbols = 0
    signal_counts: Counter[str] = Counter()

    for symbol, name in universe_rows:
        history_path = HISTORY_DIR / f"{symbol}_qfq.pkl"
        if not history_path.exists():
            raise FileNotFoundError(f"missing frozen QFQ history: {history_path}")
        history_entries.append(f"{symbol}|{_sha256(history_path)}")
        frame = _add_indicators(pd.read_pickle(history_path))
        if len(frame) < MA_YEAR + MA_SLOPE_LOOKBACK + MAX_HOLDING_BARS + 2:
            skipped["short_history_symbol"] += 1
            continue
        usable_symbols += 1
        dates = [pd.Timestamp(value).date() for value in frame["datetime"]]
        breaks = _trend_breaks(frame)
        raw = _raw_signals(symbol, name, frame)
        signals_by_route = {
            "volume_breakout": raw["volume_breakout"],
            "yearline_pullback": raw["yearline_pullback"],
            "combined": _combined_signals(raw),
        }
        for route, signals in signals_by_route.items():
            for signal in signals:
                signal_day = date.fromisoformat(signal.signal_day)
                split = next(
                    (
                        label
                        for label, (start, end) in SPLITS.items()
                        if start <= signal_day <= end
                    ),
                    None,
                )
                if split is None:
                    continue
                signal_counts[f"{route}|{split}"] += 1
                for stop_mode in STOP_MODES:
                    trade, reason = _simulate(
                        signal,
                        frame,
                        dates,
                        breaks,
                        execution,
                        stop_mode,
                    )
                    if trade is None:
                        skipped[f"{route}|{split}|{reason or 'unknown'}"] += 1
                        continue
                    rows_by_route_stop_split[route][stop_mode][split].append(trade)

    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts: dict[str, Any] = {}
    routes_report: dict[str, Any] = {}
    for route in ROUTES:
        route_report: dict[str, Any] = {"splits": {}}
        for split in SPLITS:
            profiles: dict[str, Any] = {}
            for stop_mode in STOP_MODES:
                rows = rows_by_route_stop_split[route][stop_mode][split]
                artifact_name = f"{route}_{stop_mode}_{split}.jsonl"
                artifacts[artifact_name] = _write_jsonl(output_dir / artifact_name, rows)
                profiles[stop_mode] = {
                    "candidate_summary": _summary(rows),
                    "stop_loss_pct_distribution": {
                        "min": round(min((row["stop_loss_pct"] for row in rows), default=0), 6),
                        "mean": round(
                            float(np.mean([row["stop_loss_pct"] for row in rows]))
                            if rows
                            else 0.0,
                            6,
                        ),
                        "max": round(max((row["stop_loss_pct"] for row in rows), default=0), 6),
                    },
                    "portfolio": _portfolio(rows, execution, index_frame),
                }
            route_report["splits"][split] = {
                "profiles": profiles,
                "paired_vs_fixed_sl8": {
                    stop_mode: _paired_delta(
                        rows_by_route_stop_split[route]["fixed_sl8"][split],
                        rows_by_route_stop_split[route][stop_mode][split],
                    )
                    for stop_mode in ("fixed_sl5", "dynamic_sl5_sl8")
                },
            }
        route_report["screen"] = _screen(route_report)
        routes_report[route] = route_report

    report: dict[str, Any] = {
        "version": VERSION,
        "status": "viewed_development_backtest",
        "inputs": {
            "universe": {"path": str(universe_path), "sha256": _sha256(universe_path)},
            "index": {"path": str(index_path), "sha256": _sha256(index_path)},
            "config": {"path": str(config_path), "sha256": _sha256(config_path)},
            "history_manifest_sha256": hashlib.sha256(
                "\n".join(history_entries).encode("utf-8")
            ).hexdigest(),
            "history_files": len(history_entries),
        },
        "frozen_design": {
            "trend": "MA250[t] > MA250[t-20] and MA60[t] > MA120[t] > MA250[t]",
            "volume_breakout": (
                "close crosses from <=MA250 to >MA250 and volume >= "
                "1.5 * mean(volume[t-20:t-1])"
            ),
            "yearline_pullback": (
                "prior close > prior MA250; low within [-0.5%, +2%] of MA250; "
                "close >= MA250 and close >= open"
            ),
            "entry": "signal confirmed at close t; buy at next tradable open t+1",
            "exit": (
                "SL first, TP30, next-open exit after close below MA250, or "
                "120-bar hard timeout; T+1 and conservative price limits"
            ),
            "dynamic_stop": "clip(2 * ATR14 / signal_close, 5%, 8%) fixed at entry",
            "cooldown_bars": SIGNAL_COOLDOWN_BARS,
            "parameter_search": False,
        },
        "splits": {
            label: {"signal_start": start.isoformat(), "signal_end": end.isoformat()}
            for label, (start, end) in SPLITS.items()
        },
        "data": {
            "universe_symbols": len(universe_rows),
            "usable_symbols": usable_symbols,
            "signal_counts_before_execution": dict(sorted(signal_counts.items())),
            "skipped": dict(sorted(skipped.items())),
            "survivorship_bias": True,
        },
        "routes": routes_report,
        "artifacts": artifacts,
        "policy": {
            "local_data_only": True,
            "database_used": False,
            "sql_executed": False,
            "network_used": False,
            "holdout_used": False,
            "production_config_modified": False,
            "production_eligible": False,
            "factor_or_threshold_selection_performed": False,
        },
    }
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    report["report_sha256"] = _sha256(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--index-data", default=str(DEFAULT_INDEX))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
