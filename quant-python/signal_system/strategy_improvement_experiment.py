"""Deterministic research runner for the strategy-improvement workstreams.

The runner consumes completed, fixed-entry baseline trades and local cached
market data.  It never regenerates signals, writes business data, or connects
to the production database.  Development commands reject Holdout paths unless
an explicit, separately authorized override is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import (  # noqa: E402
    DEFAULT_SIGNAL_PRIORITY,
    HISTORY_DIR,
    _resolve_execution_config,
    prepare_closed_bars,
    run_portfolio,
    summarize,
)
from candidate_integrity import normalize_candidate_rows  # noqa: E402
from utils.helpers import load_config  # noqa: E402


SPLITS = ("train", "val", "test")
DEFAULT_BASELINE_DIR = Path(r"D:\tmp\exit_experiments_final_prod\baseline")
DEFAULT_FUNDAMENTAL_DIR = Path(r"D:\tmp\candidates_canonical")
DEFAULT_OUTPUT_DIR = Path(r"D:\tmp\strategy_improvement")
DEFAULT_INDEX_PATH = BASE_DIR / "cache" / "index_000001_sh.pkl"
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
DEFAULT_SEED = 20260830
PORTFOLIO_CONFIG = {
    "initial_cash": 100000.0,
    "max_positions": 4,
    "position_size_pct": 0.25,
    "signal_priority": list(DEFAULT_SIGNAL_PRIORITY),
    "score_mode": "P0",
    "tie_break": "symbol_asc",
    "seed": DEFAULT_SEED,
}
VALUE_COVERAGE_GATE_PCT = 70.0
VALUE_MIN_ELIGIBLE = 30
VALUE_MIN_SELECTED = 15


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _candidate_id(row: dict[str, Any]) -> str:
    return str(
        row.get("candidate_id")
        or f"{row.get('symbol', '')}|{row.get('signal_day', '')}|{row.get('signal_type', '')}"
    )


def _manifest_sha256(rows: Iterable[dict[str, Any]]) -> str:
    payload = "\n".join(sorted(_candidate_id(row) for row in rows)).encode("utf-8")
    return _sha256_bytes(payload)


def _guard_development_path(path: Path, allow_holdout: bool) -> Path:
    resolved = path.expanduser().resolve()
    if not allow_holdout and any("holdout" in part.lower() for part in resolved.parts):
        raise ValueError(
            f"Holdout path is blocked for development experiments: {resolved}. "
            "Use only after the experiment is frozen and separately authorized."
        )
    return resolved


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in materialized:
            handle.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")
    return {"path": str(path), "sha256": _sha256_file(path), "rows": len(materialized)}


def _write_json(path: Path, value: Any) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    return {"path": str(path), "sha256": _sha256_file(path)}


def _load_baseline_splits(
    baseline_dir: Path,
    *,
    allow_holdout: bool,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    root = _guard_development_path(baseline_dir, allow_holdout)
    rows: dict[str, list[dict[str, Any]]] = {}
    inputs: dict[str, Any] = {}
    seen: set[str] = set()
    for split in SPLITS:
        path = _guard_development_path(root / f"candidates_{split}.jsonl", allow_holdout)
        if not path.exists():
            raise FileNotFoundError(f"baseline split does not exist: {path}")
        split_rows = _read_jsonl(path)
        normalized_rows, integrity = normalize_candidate_rows(split_rows)
        if integrity["removed_exact_duplicate_rows"]:
            raise ValueError(
                f"baseline split contains duplicate candidate ids ({split}); "
                "regenerate it through exit_experiment.py"
            )
        split_rows = normalized_rows
        ids = {_candidate_id(row) for row in split_rows}
        overlap = seen & ids
        if overlap:
            sample = sorted(overlap)[:3]
            raise ValueError(f"candidate leakage across splits ({split}): {sample}")
        seen.update(ids)
        rows[split] = split_rows
        inputs[split] = {
            "path": str(path),
            "sha256": _sha256_file(path),
            "rows": len(split_rows),
            "candidate_manifest_sha256": _manifest_sha256(split_rows),
            "candidate_integrity": integrity,
        }
    return rows, inputs


def _load_index(path: Path, allow_holdout: bool) -> tuple[pd.DataFrame, dict[str, Any]]:
    resolved = _guard_development_path(path, allow_holdout)
    if not resolved.exists():
        raise FileNotFoundError(f"index cache does not exist: {resolved}")
    suffix = resolved.suffix.lower()
    if suffix in {".pkl", ".pickle"}:
        raw = pd.read_pickle(resolved)
    elif suffix == ".csv":
        raw = pd.read_csv(resolved)
    elif suffix in {".json", ".jsonl"}:
        raw = pd.read_json(resolved, lines=suffix == ".jsonl")
    else:
        raise ValueError("index data must be PKL, CSV, JSON or JSONL")
    closed = prepare_closed_bars(raw)
    if closed.empty:
        raise ValueError(f"index cache has no usable closed bars: {resolved}")
    return closed, {
        "path": str(resolved),
        "sha256": _sha256_file(resolved),
        "rows": len(closed),
        "first_day": str(closed.iloc[0]["datetime"].date()),
        "last_day": str(closed.iloc[-1]["datetime"].date()),
    }


def _load_costs(config_path: Path, allow_holdout: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = _guard_development_path(config_path, allow_holdout)
    config = load_config(str(resolved))
    if config is None:
        raise RuntimeError(f"unable to load config: {resolved}")
    costs = _resolve_execution_config(config)
    costs["profit_protection"] = {"mode": "none"}
    return costs, {
        "path": str(resolved),
        "sha256": _sha256_file(resolved),
        "resolved_execution_sha256": _sha256_bytes(_canonical_json(costs).encode("utf-8")),
    }


def _research_config(command: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    value = {
        "version": "strategy_improvement.v2",
        "command": command,
        "seed": DEFAULT_SEED,
        "portfolio": PORTFOLIO_CONFIG,
    }
    if extra:
        value.update(extra)
    return value


def _candidate_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {**summarize(rows), "candidate_manifest_sha256": _manifest_sha256(rows)}


def _run_portfolio_artifacts(
    rows: list[dict[str, Any]],
    costs: dict[str, Any],
    output_dir: Path,
    label: str,
) -> dict[str, Any]:
    result = run_portfolio(rows, costs, dict(PORTFOLIO_CONFIG))
    return {
        "summary": result["summary"],
        "attribution": result["attribution"],
        "rejection_reasons": result["rejection_reasons"],
        "artifacts": {
            "accepted_entries": _write_jsonl(
                output_dir / f"{label}_accepted_entries.jsonl",
                result["accepted_entries"],
            ),
            "trades": _write_jsonl(output_dir / f"{label}_portfolio_trades.jsonl", result["trades"]),
            "rejections": _write_jsonl(
                output_dir / f"{label}_portfolio_rejections.jsonl", result["rejections"]
            ),
            "equity_curve": _write_jsonl(
                output_dir / f"{label}_equity_curve.jsonl", result["equity_curve"]
            ),
        },
        "_equity_curve": result["equity_curve"],
    }


def align_equity_to_benchmark(
    equity_curve: list[dict[str, Any]],
    index_history: pd.DataFrame,
    initial_cash: float,
) -> list[dict[str, Any]]:
    """Align equity to index sessions, including a prior-session capital anchor."""
    if not equity_curve:
        return []
    index = prepare_closed_bars(index_history)
    points = {str(point["day"]): point for point in equity_curve}
    first_day = min(points)
    last_day = max(points)
    index_days = [str(value.date()) for value in index["datetime"]]
    eligible = [position for position, day in enumerate(index_days) if day < first_day]
    start_position = eligible[-1] if eligible else next(
        (position for position, day in enumerate(index_days) if day >= first_day), 0
    )
    end_positions = [position for position, day in enumerate(index_days) if day <= last_day]
    if not end_positions:
        return []
    end_position = end_positions[-1]
    if end_position < start_position:
        return []
    cash = float(initial_cash)
    equity = float(initial_cash)
    market_value = 0.0
    aligned: list[dict[str, Any]] = []
    for position in range(start_position, end_position + 1):
        day = index_days[position]
        point = points.get(day)
        if point is not None:
            cash = float(point["cash"])
            market_value = float(point["market_value"])
            equity = float(point["equity"])
        aligned.append(
            {
                "day": day,
                "cash": round(cash, 2),
                "market_value": round(market_value, 2),
                "equity": round(equity, 2),
                "benchmark_close": float(index.iloc[position]["close"]),
                "is_initial_anchor": day < first_day,
            }
        )
    return aligned


def _max_drawdown(values: np.ndarray) -> float | None:
    if values.size == 0:
        return None
    peaks = np.maximum.accumulate(values)
    valid = peaks > 0
    if not valid.any():
        return None
    drawdowns = np.zeros_like(values, dtype=float)
    drawdowns[valid] = values[valid] / peaks[valid] - 1.0
    return float(drawdowns.min())


def benchmark_metrics(aligned: list[dict[str, Any]]) -> dict[str, Any]:
    if len(aligned) < 2:
        return {"observations": len(aligned), "status": "insufficient_data"}
    equity = np.asarray([float(row["equity"]) for row in aligned], dtype=float)
    benchmark = np.asarray([float(row["benchmark_close"]) for row in aligned], dtype=float)
    strategy_returns = equity[1:] / equity[:-1] - 1.0
    benchmark_returns = benchmark[1:] / benchmark[:-1] - 1.0
    observations = len(strategy_returns)
    strategy_total = equity[-1] / equity[0] - 1.0
    benchmark_total = benchmark[-1] / benchmark[0] - 1.0
    periods = max(observations, 1)

    def annualized(total: float) -> float | None:
        if total <= -1.0:
            return None
        return (1.0 + total) ** (252.0 / periods) - 1.0

    strategy_vol = float(np.std(strategy_returns, ddof=1) * math.sqrt(252.0)) if observations > 1 else None
    benchmark_vol = float(np.std(benchmark_returns, ddof=1) * math.sqrt(252.0)) if observations > 1 else None
    strategy_std = float(np.std(strategy_returns, ddof=1)) if observations > 1 else 0.0
    sharpe = (
        float(np.mean(strategy_returns) / strategy_std * math.sqrt(252.0))
        if strategy_std > 0
        else None
    )
    benchmark_variance = float(np.var(benchmark_returns, ddof=1)) if observations > 1 else 0.0
    beta = None
    alpha = None
    if benchmark_variance > 0:
        beta = float(np.cov(strategy_returns, benchmark_returns, ddof=1)[0, 1] / benchmark_variance)
        alpha = float((np.mean(strategy_returns) - beta * np.mean(benchmark_returns)) * 252.0)
    active = strategy_returns - benchmark_returns
    active_std = float(np.std(active, ddof=1)) if observations > 1 else 0.0
    tracking_error = active_std * math.sqrt(252.0) if active_std > 0 else None
    information_ratio = (
        float(np.mean(active) / active_std * math.sqrt(252.0)) if active_std > 0 else None
    )
    return {
        "status": "ok",
        "start_day": aligned[0]["day"],
        "end_day": aligned[-1]["day"],
        "observations": observations,
        "strategy_return_pct": round(strategy_total * 100.0, 4),
        "benchmark_return_pct": round(benchmark_total * 100.0, 4),
        "compounded_excess_return_pct": round(
            ((1.0 + strategy_total) / (1.0 + benchmark_total) - 1.0) * 100.0,
            4,
        ),
        "strategy_annualized_return_pct": (
            round(float(annualized(strategy_total)) * 100.0, 4)
            if annualized(strategy_total) is not None
            else None
        ),
        "benchmark_annualized_return_pct": (
            round(float(annualized(benchmark_total)) * 100.0, 4)
            if annualized(benchmark_total) is not None
            else None
        ),
        "strategy_annualized_volatility_pct": round(strategy_vol * 100.0, 4) if strategy_vol is not None else None,
        "benchmark_annualized_volatility_pct": round(benchmark_vol * 100.0, 4) if benchmark_vol is not None else None,
        "sharpe_zero_rf": round(sharpe, 6) if sharpe is not None else None,
        "strategy_max_drawdown_pct": round(float(_max_drawdown(equity)) * 100.0, 4),
        "benchmark_max_drawdown_pct": round(float(_max_drawdown(benchmark)) * 100.0, 4),
        "beta": round(beta, 6) if beta is not None else None,
        "annualized_alpha_pct": round(alpha * 100.0, 4) if alpha is not None else None,
        "tracking_error_pct": round(tracking_error * 100.0, 4) if tracking_error is not None else None,
        "information_ratio": round(information_ratio, 6) if information_ratio is not None else None,
    }


def build_composite_risk_off(index_history: pd.DataFrame) -> pd.DataFrame:
    """Build the preregistered causal composite_risk_off_v1 state series."""
    frame = prepare_closed_bars(index_history)[["datetime", "close"]].copy()
    close = frame["close"].astype(float)
    daily_return = close.pct_change()
    frame["ma10"] = close.rolling(10, min_periods=10).mean()
    frame["ma20"] = close.rolling(20, min_periods=20).mean()
    frame["return_5d"] = close.pct_change(5)
    frame["downside_volatility_20d"] = (
        daily_return.clip(upper=0.0).rolling(20, min_periods=20).std(ddof=0)
    )
    trigger = (
        ((close < frame["ma20"]) & (frame["return_5d"] < 0.0))
        | (frame["return_5d"] <= -0.03)
        | (frame["downside_volatility_20d"] >= 0.025)
    ).fillna(False)
    recovery = ((close > frame["ma10"]) & (frame["return_5d"] > 0.0)).fillna(False)
    frame["risk_trigger"] = trigger.astype(bool)
    frame["recovery_condition"] = recovery.astype(bool)
    frame["risk_off"] = latch_risk_off(trigger.tolist(), recovery.tolist())
    frame["regime_version"] = "composite_risk_off_v1"
    frame["day"] = frame["datetime"].dt.date.astype(str)
    return frame


def latch_risk_off(trigger: list[bool], recovery: list[bool]) -> list[bool]:
    """Latch risk-off immediately and recover after three consecutive sessions."""
    if len(trigger) != len(recovery):
        raise ValueError("trigger and recovery lengths differ")
    active = False
    recovery_streak = 0
    result: list[bool] = []
    for is_trigger, can_recover in zip(trigger, recovery):
        if bool(is_trigger):
            active = True
            recovery_streak = 0
        elif active:
            recovery_streak = recovery_streak + 1 if bool(can_recover) else 0
            if recovery_streak >= 3:
                active = False
                recovery_streak = 0
        result.append(active)
    return result


def _regime_filter(
    rows: list[dict[str, Any]],
    regime_frame: pd.DataFrame,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    by_day = {str(row["day"]): bool(row["risk_off"]) for _, row in regime_frame.iterrows()}
    eligible: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    missing = 0
    rejected = 0
    for row in rows:
        day = str(row.get("signal_day", ""))
        if day not in by_day:
            missing += 1
            continue
        copy = dict(row)
        copy["research_regime"] = "risk_off" if by_day[day] else "risk_on"
        eligible.append(copy)
        if by_day[day]:
            rejected += 1
        else:
            selected.append(copy)
    return eligible, selected, {
        "source_candidates": len(rows),
        "eligible_candidates": len(eligible),
        "selected_candidates": len(selected),
        "missing_index_day": missing,
        "risk_off_rejections": rejected,
        "eligible_manifest_sha256": _manifest_sha256(eligible),
        "selected_manifest_sha256": _manifest_sha256(selected),
    }


def _load_fundamental_splits(
    fundamental_dir: Path,
    *,
    allow_holdout: bool,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    root = _guard_development_path(fundamental_dir, allow_holdout)
    result: dict[str, list[dict[str, Any]]] = {}
    inputs: dict[str, Any] = {}
    for split in SPLITS:
        path = _guard_development_path(root / f"candidates_{split}.jsonl", allow_holdout)
        if not path.exists():
            raise FileNotFoundError(f"fundamental split does not exist: {path}")
        rows = _read_jsonl(path)
        result[split] = rows
        inputs[split] = {
            "path": str(path),
            "sha256": _sha256_file(path),
            "rows": len(rows),
            "candidate_manifest_sha256": _manifest_sha256(rows),
        }
    return result, inputs


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fundamental_by_candidate(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_candidate_id(row)].append(row)
    result: dict[str, dict[str, Any]] = {}
    for key, candidates in grouped.items():
        result[key] = sorted(
            candidates,
            key=lambda row: (str(row.get("ann_date", "")), str(row.get("period", ""))),
            reverse=True,
        )[0]
    return result


def percentile_rank(values: list[float], position: int) -> float:
    """Candidate-relative percentile rank with a neutral singleton value."""
    if not values:
        raise ValueError("values must not be empty")
    if len(values) == 1:
        return 0.5
    target = values[position]
    below = sum(1 for value in values if value < target)
    equal_others = sum(1 for index, value in enumerate(values) if index != position and value == target)
    return (below + 0.5 * equal_others) / (len(values) - 1)


def value_filter_rows(
    baseline_rows: list[dict[str, Any]],
    fundamental_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Apply exact PIT join, candidate-relative size, and earnings-yield ranks."""
    lookup = _fundamental_by_candidate(fundamental_rows)
    eligible: list[dict[str, Any]] = []
    positive_pe: list[dict[str, Any]] = []
    nonpositive_pe: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for baseline in baseline_rows:
        fundamental = lookup.get(_candidate_id(baseline))
        if fundamental is None:
            reasons["no_exact_join"] += 1
            continue
        signal_day = str(baseline.get("signal_day", ""))
        announcement_day = str(fundamental.get("ann_date", ""))
        if not announcement_day or announcement_day > signal_day:
            reasons["not_point_in_time"] += 1
            continue
        if bool(fundamental.get("ann_date_estimated", False)):
            reasons["estimated_announcement_date"] += 1
            continue
        market_cap = _finite_number(fundamental.get("market_cap"))
        pe = _finite_number(fundamental.get("pe"))
        if market_cap is None or market_cap <= 0:
            reasons["invalid_market_cap"] += 1
            continue
        if pe is None:
            reasons["invalid_pe"] += 1
            continue
        row = dict(baseline)
        row["market_cap"] = market_cap
        row["pe"] = pe
        row["fundamental_ann_date"] = announcement_day
        row["fundamental_period"] = fundamental.get("period")
        if pe > 0:
            row["earnings_yield"] = 1.0 / pe
            row["earnings_yield_status"] = "available"
            positive_pe.append(row)
        else:
            row["earnings_yield"] = None
            row["earnings_yield_status"] = "nonpositive_pe_loss_or_unavailable"
            nonpositive_pe.append(row)
        eligible.append(row)

    selected: list[dict[str, Any]] = []
    daily_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in positive_pe:
        daily_groups[str(row["signal_day"])].append(row)
    daily_group_sizes: Counter[int] = Counter()
    size_rejections = 0
    value_rejections = 0
    for day in sorted(daily_groups):
        group = sorted(daily_groups[day], key=_candidate_id)
        daily_group_sizes[len(group)] += 1
        caps = [float(row["market_cap"]) for row in group]
        yields = [float(row["earnings_yield"]) for row in group]
        for position, source in enumerate(group):
            row = dict(source)
            row["candidate_relative_market_cap_percentile"] = percentile_rank(caps, position)
            row["candidate_relative_earnings_yield_percentile"] = percentile_rank(yields, position)
            keep_size = row["candidate_relative_market_cap_percentile"] >= 0.30
            keep_value = row["candidate_relative_earnings_yield_percentile"] >= 0.50
            if not keep_size:
                size_rejections += 1
            elif not keep_value:
                value_rejections += 1
            else:
                selected.append(row)

    coverage = len(eligible) / len(baseline_rows) * 100.0 if baseline_rows else 0.0
    gates = {
        "exact_pit_coverage_at_least_70pct": coverage >= VALUE_COVERAGE_GATE_PCT,
        "eligible_candidates_at_least_30": len(eligible) >= VALUE_MIN_ELIGIBLE,
        "selected_candidates_at_least_15": len(selected) >= VALUE_MIN_SELECTED,
    }
    return eligible, selected, {
        "source_candidates": len(baseline_rows),
        "fundamental_rows": len(fundamental_rows),
        "eligible_candidates": len(eligible),
        "exact_pit_coverage_pct": round(coverage, 2),
        "positive_pe_candidates": len(positive_pe),
        "nonpositive_pe_candidates": len(nonpositive_pe),
        "selected_candidates": len(selected),
        "size_bottom_30pct_rejections": size_rejections,
        "earnings_yield_lower_half_rejections": value_rejections,
        "join_or_validity_rejections": dict(reasons),
        "daily_positive_pe_group_sizes": dict(sorted(daily_group_sizes.items())),
        "days_with_at_least_two_rankable_candidates": sum(
            count for size, count in daily_group_sizes.items() if size >= 2
        ),
        "ranking_scope": (
            "candidate-relative within each signal day; this is not a full-market "
            "bottom-30-percent market-cap exclusion"
        ),
        "gates": gates,
        "passes_research_coverage_gates": all(gates.values()),
        "eligible_manifest_sha256": _manifest_sha256(eligible),
        "selected_manifest_sha256": _manifest_sha256(selected),
        "nonpositive_pe_stats": _candidate_summary(nonpositive_pe),
    }


META_SIGNAL_TYPES = (*DEFAULT_SIGNAL_PRIORITY, "other")
META_REGIMES = ("bull", "bear", "range", "sideways", "unknown")
META_FEATURE_NAMES = (
    "stock_return_5d",
    "stock_return_20d",
    "stock_return_60d",
    "stock_volatility_20d",
    "stock_downside_volatility_20d",
    "stock_atr14_pct",
    "stock_ma20_distance",
    "stock_ma60_distance",
    "stock_ma250_distance",
    "stock_ma20_slope_5d",
    "stock_volume_ratio_20d",
    "log_market_cap",
    "index_return_5d",
    "index_ma20_distance",
    "index_downside_volatility_20d",
    "index_risk_off",
    *(f"signal_type_{name}" for name in META_SIGNAL_TYPES),
    *(f"source_regime_{name}" for name in META_REGIMES),
)
META_OUTCOME_FIELDS = {
    "entry_day",
    "entry_price",
    "exit_day",
    "exit_price",
    "exit_reason",
    "future_5d",
    "future_20d",
    "future_40d",
    "holding_days",
    "holding_bars",
    "mae",
    "mfe",
    "pnl_pct",
    "post_exit_5d",
    "post_exit_20d",
    "trade_pnl_pct",
}


def _return_over(close: pd.Series, periods: int) -> float | None:
    if len(close) <= periods:
        return None
    earlier = float(close.iloc[-periods - 1])
    latest = float(close.iloc[-1])
    return latest / earlier - 1.0 if earlier > 0 else None


def _atr_percentage(history: pd.DataFrame, period: int = 14) -> float | None:
    if len(history) < period + 1:
        return None
    high = history["high"].astype(float)
    low = history["low"].astype(float)
    close = history["close"].astype(float)
    previous = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1)
    atr = float(true_range.iloc[-period:].mean())
    latest = float(close.iloc[-1])
    return atr / latest if latest > 0 else None


def _distance_to_average(close: pd.Series, period: int) -> float | None:
    if len(close) < period:
        return None
    average = float(close.iloc[-period:].mean())
    return float(close.iloc[-1]) / average - 1.0 if average > 0 else None


def _stock_signal_features(history: pd.DataFrame) -> dict[str, float | None]:
    close = history["close"].astype(float)
    returns = close.pct_change().dropna()
    recent = returns.iloc[-20:]
    volume = history["volume"].astype(float) if "volume" in history else pd.Series(dtype=float)
    volume_mean = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else 0.0
    ma20_current = float(close.iloc[-20:].mean()) if len(close) >= 20 else None
    ma20_previous = float(close.iloc[-25:-5].mean()) if len(close) >= 25 else None
    return {
        "stock_return_5d": _return_over(close, 5),
        "stock_return_20d": _return_over(close, 20),
        "stock_return_60d": _return_over(close, 60),
        "stock_volatility_20d": float(recent.std(ddof=0)) if len(recent) == 20 else None,
        "stock_downside_volatility_20d": (
            float(recent.clip(upper=0.0).std(ddof=0)) if len(recent) == 20 else None
        ),
        "stock_atr14_pct": _atr_percentage(history, 14),
        "stock_ma20_distance": _distance_to_average(close, 20),
        "stock_ma60_distance": _distance_to_average(close, 60),
        "stock_ma250_distance": _distance_to_average(close, 250),
        "stock_ma20_slope_5d": (
            ma20_current / ma20_previous - 1.0
            if ma20_current is not None and ma20_previous not in {None, 0.0}
            else None
        ),
        "stock_volume_ratio_20d": (
            float(volume.iloc[-1]) / volume_mean
            if len(volume) >= 20 and volume_mean > 0
            else None
        ),
    }


def _index_feature_lookup(regime_frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for _, row in regime_frame.iterrows():
        close = _finite_number(row.get("close"))
        ma20 = _finite_number(row.get("ma20"))
        result[str(row["day"])] = {
            "index_return_5d": _finite_number(row.get("return_5d")),
            "index_ma20_distance": close / ma20 - 1.0 if close and ma20 else None,
            "index_downside_volatility_20d": _finite_number(
                row.get("downside_volatility_20d")
            ),
            "index_risk_off": 1.0 if bool(row.get("risk_off")) else 0.0,
        }
    return result


def extract_meta_feature_dict(
    candidate: dict[str, Any],
    stock_history: pd.DataFrame,
    index_features: dict[str, Any],
    fundamental: dict[str, Any] | None = None,
) -> dict[str, float | None]:
    """Return signal-day-only meta features; outcome fields are never read."""
    features: dict[str, float | None] = _stock_signal_features(stock_history)
    features.update({key: _finite_number(index_features.get(key)) for key in (
        "index_return_5d",
        "index_ma20_distance",
        "index_downside_volatility_20d",
        "index_risk_off",
    )})
    market_cap = _finite_number(candidate.get("market_cap"))
    if (market_cap is None or market_cap <= 0) and fundamental is not None:
        market_cap = _finite_number(fundamental.get("market_cap"))
    features["log_market_cap"] = math.log(market_cap) if market_cap and market_cap > 0 else None
    signal_type = str(candidate.get("signal_type", ""))
    normalized_signal = signal_type if signal_type in DEFAULT_SIGNAL_PRIORITY else "other"
    for name in META_SIGNAL_TYPES:
        features[f"signal_type_{name}"] = 1.0 if name == normalized_signal else 0.0
    regime = str(candidate.get("regime", "unknown")).lower()
    if regime not in META_REGIMES:
        regime = "unknown"
    for name in META_REGIMES:
        features[f"source_regime_{name}"] = 1.0 if name == regime else 0.0
    if set(features) != set(META_FEATURE_NAMES):
        missing = [name for name in META_FEATURE_NAMES if name not in features]
        extra = [name for name in features if name not in META_FEATURE_NAMES]
        raise AssertionError(f"meta feature schema mismatch; missing={missing}, extra={extra}")
    return {name: features[name] for name in META_FEATURE_NAMES}


def _build_meta_split(
    rows: list[dict[str, Any]],
    index_lookup: dict[str, dict[str, Any]],
    fundamental_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], np.ndarray, np.ndarray, dict[str, Any]]:
    fundamentals = _fundamental_by_candidate(fundamental_rows)
    history_cache: dict[str, pd.DataFrame | None] = {}
    eligible: list[dict[str, Any]] = []
    matrix: list[list[float]] = []
    labels: list[float] = []
    skipped: Counter[str] = Counter()
    history_hashes: dict[str, str] = {}
    for candidate in rows:
        signal_day = str(candidate.get("signal_day", ""))
        index_features = index_lookup.get(signal_day)
        if index_features is None:
            skipped["missing_index_day"] += 1
            continue
        symbol = str(candidate.get("symbol", ""))
        if symbol not in history_cache:
            path = HISTORY_DIR / f"{symbol}_qfq.pkl"
            if not path.exists():
                history_cache[symbol] = None
            else:
                history_cache[symbol] = prepare_closed_bars(pd.read_pickle(path))
                history_hashes[symbol] = _sha256_file(path)
        history = history_cache[symbol]
        if history is None or history.empty:
            skipped["missing_stock_history"] += 1
            continue
        signal_timestamp = pd.Timestamp(signal_day)
        causal = history[history["datetime"] <= signal_timestamp].copy()
        if causal.empty or str(causal.iloc[-1]["datetime"].date()) != signal_day:
            skipped["stock_signal_day_not_in_cache"] += 1
            continue
        if len(causal) < 61:
            skipped["stock_history_shorter_than_61_bars"] += 1
            continue
        feature_dict = extract_meta_feature_dict(
            candidate,
            causal,
            index_features,
            fundamentals.get(_candidate_id(candidate)),
        )
        outcome = _finite_number(candidate.get("trade_pnl_pct", candidate.get("pnl_pct")))
        if outcome is None:
            skipped["missing_label"] += 1
            continue
        copy = dict(candidate)
        copy["candidate_id"] = _candidate_id(candidate)
        eligible.append(copy)
        matrix.append(
            [float(feature_dict[name]) if feature_dict[name] is not None else float("nan") for name in META_FEATURE_NAMES]
        )
        labels.append(1.0 if outcome > 0 else 0.0)
    history_manifest = "\n".join(
        f"{symbol}|{history_hashes[symbol]}" for symbol in sorted(history_hashes)
    )
    return eligible, np.asarray(matrix, dtype=float), np.asarray(labels, dtype=float), {
        "source_candidates": len(rows),
        "eligible_candidates": len(eligible),
        "skipped": dict(skipped),
        "candidate_manifest_sha256": _manifest_sha256(eligible),
        "history_manifest_sha256": _sha256_bytes(history_manifest.encode("utf-8")),
        "history_symbols": len(history_hashes),
        "positive_labels": int(sum(labels)),
        "negative_labels": int(len(labels) - sum(labels)),
    }


def fit_preprocessor(matrix: np.ndarray) -> dict[str, np.ndarray]:
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise ValueError("preprocessor requires a non-empty two-dimensional matrix")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        medians = np.nanmedian(matrix, axis=0)
    medians = np.where(np.isfinite(medians), medians, 0.0)
    imputed = np.where(np.isfinite(matrix), matrix, medians)
    means = np.mean(imputed, axis=0)
    scales = np.std(imputed, axis=0, ddof=0)
    scales = np.where(scales > 1e-12, scales, 1.0)
    return {"medians": medians, "means": means, "scales": scales}


def apply_preprocessor(matrix: np.ndarray, state: dict[str, np.ndarray]) -> np.ndarray:
    if matrix.ndim != 2:
        raise ValueError("matrix must be two-dimensional")
    imputed = np.where(np.isfinite(matrix), matrix, state["medians"])
    return (imputed - state["means"]) / state["scales"]


def _sigmoid(value: np.ndarray) -> np.ndarray:
    clipped = np.clip(value, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def fit_logistic_regression(
    matrix: np.ndarray,
    labels: np.ndarray,
    *,
    l2: float = 1.0,
    max_iterations: int = 100,
    tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Fit a deterministic pure-NumPy L2 logistic regression by Newton steps."""
    if matrix.ndim != 2 or len(matrix) != len(labels) or len(labels) == 0:
        raise ValueError("invalid logistic training data")
    unique = np.unique(labels)
    if len(unique) < 2:
        raise ValueError("logistic training labels require both classes")
    design = np.column_stack([np.ones(len(matrix)), matrix])
    weights = np.zeros(design.shape[1], dtype=float)
    converged = False
    iteration = 0
    for iteration in range(1, max_iterations + 1):
        probabilities = _sigmoid(design @ weights)
        gradient = design.T @ (probabilities - labels) / len(labels)
        gradient[1:] += l2 * weights[1:] / len(labels)
        curvature = probabilities * (1.0 - probabilities)
        hessian = (design.T * curvature) @ design / len(labels)
        regularization = np.zeros(design.shape[1], dtype=float)
        regularization[1:] = l2 / len(labels)
        hessian += np.diag(regularization)
        step = np.linalg.pinv(hessian, rcond=1e-12) @ gradient
        weights -= step
        if float(np.max(np.abs(step))) < tolerance:
            converged = True
            break
    return {
        "intercept": float(weights[0]),
        "coefficients": weights[1:].copy(),
        "l2": float(l2),
        "iterations": iteration,
        "converged": converged,
    }


def predict_logistic(model: dict[str, Any], matrix: np.ndarray) -> np.ndarray:
    return _sigmoid(float(model["intercept"]) + matrix @ np.asarray(model["coefficients"], dtype=float))


def _roc_auc(labels: np.ndarray, probabilities: np.ndarray) -> float | None:
    positives = int(np.sum(labels == 1))
    negatives = int(np.sum(labels == 0))
    if positives == 0 or negatives == 0:
        return None
    order = np.argsort(probabilities, kind="mergesort")
    ranks = np.empty(len(probabilities), dtype=float)
    position = 0
    while position < len(order):
        end = position + 1
        while end < len(order) and probabilities[order[end]] == probabilities[order[position]]:
            end += 1
        average_rank = (position + 1 + end) / 2.0
        ranks[order[position:end]] = average_rank
        position = end
    positive_rank_sum = float(np.sum(ranks[labels == 1]))
    return (positive_rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def classification_metrics(
    labels: np.ndarray,
    probabilities: np.ndarray,
    threshold: float = 0.5,
) -> dict[str, Any]:
    if len(labels) == 0:
        return {"count": 0, "status": "insufficient_data"}
    predicted = probabilities >= threshold
    actual = labels >= 0.5
    true_positive = int(np.sum(predicted & actual))
    true_negative = int(np.sum(~predicted & ~actual))
    false_positive = int(np.sum(predicted & ~actual))
    false_negative = int(np.sum(~predicted & actual))
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else None
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else None
    specificity = true_negative / (true_negative + false_positive) if true_negative + false_positive else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None
        and recall is not None
        and precision + recall > 0
        else 0.0
    )
    clipped = np.clip(probabilities, 1e-12, 1.0 - 1e-12)
    auc = _roc_auc(labels, probabilities)
    return {
        "status": "ok",
        "count": len(labels),
        "threshold": threshold,
        "selected": int(np.sum(predicted)),
        "accuracy": round((true_positive + true_negative) / len(labels), 6),
        "balanced_accuracy": (
            round((recall + specificity) / 2.0, 6)
            if recall is not None and specificity is not None
            else None
        ),
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "specificity": round(specificity, 6) if specificity is not None else None,
        "f1": round(f1, 6) if f1 is not None else None,
        "brier_score": round(float(np.mean((probabilities - labels) ** 2)), 8),
        "log_loss": round(float(-np.mean(labels * np.log(clipped) + (1.0 - labels) * np.log(1.0 - clipped))), 8),
        "roc_auc": round(float(auc), 6) if auc is not None else None,
        "confusion_matrix": {
            "true_positive": true_positive,
            "true_negative": true_negative,
            "false_positive": false_positive,
            "false_negative": false_negative,
        },
    }


def meta_training_plan() -> dict[str, dict[str, Any]]:
    return {
        "validation": {"fit_splits": ("train",), "evaluate_split": "val"},
        "viewed_test": {"fit_splits": ("train", "val"), "evaluate_split": "test"},
    }


def _fit_meta_stage(
    stage: str,
    fit_splits: tuple[str, ...],
    evaluate_split: str,
    datasets: dict[str, tuple[list[dict[str, Any]], np.ndarray, np.ndarray]],
    costs: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    fit_rows = [row for split in fit_splits for row in datasets[split][0]]
    fit_matrix = np.vstack([datasets[split][1] for split in fit_splits])
    fit_labels = np.concatenate([datasets[split][2] for split in fit_splits])
    evaluation_rows, evaluation_matrix, evaluation_labels = datasets[evaluate_split]
    preprocessor = fit_preprocessor(fit_matrix)
    model = fit_logistic_regression(
        apply_preprocessor(fit_matrix, preprocessor), fit_labels, l2=1.0
    )
    fit_probabilities = predict_logistic(
        model, apply_preprocessor(fit_matrix, preprocessor)
    )
    evaluation_probabilities = predict_logistic(
        model, apply_preprocessor(evaluation_matrix, preprocessor)
    )
    selected: list[dict[str, Any]] = []
    scored: list[dict[str, Any]] = []
    for row, probability in zip(evaluation_rows, evaluation_probabilities):
        copy = dict(row)
        copy["meta_probability"] = round(float(probability), 10)
        copy["meta_selected"] = bool(probability >= 0.5)
        scored.append(copy)
        if probability >= 0.5:
            selected.append(copy)
    stage_dir = output_dir / stage
    baseline_portfolio = _run_portfolio_artifacts(
        evaluation_rows, costs, stage_dir, f"{evaluate_split}_eligible_baseline"
    )
    selected_portfolio = _run_portfolio_artifacts(
        selected, costs, stage_dir, f"{evaluate_split}_meta_selected"
    )
    baseline_portfolio.pop("_equity_curve", None)
    selected_portfolio.pop("_equity_curve", None)
    model_payload = {
        "feature_names": list(META_FEATURE_NAMES),
        "preprocessor": {
            key: [float(value) for value in values]
            for key, values in preprocessor.items()
        },
        "intercept": model["intercept"],
        "coefficients": [float(value) for value in model["coefficients"]],
        "l2": model["l2"],
        "iterations": model["iterations"],
        "converged": model["converged"],
        "threshold": 0.5,
    }
    return {
        "fit_splits": list(fit_splits),
        "evaluate_split": evaluate_split,
        "fit_candidates": len(fit_rows),
        "fit_candidate_manifest_sha256": _manifest_sha256(fit_rows),
        "evaluation_candidates": len(evaluation_rows),
        "evaluation_candidate_manifest_sha256": _manifest_sha256(evaluation_rows),
        "selected_candidates": len(selected),
        "selected_candidate_manifest_sha256": _manifest_sha256(selected),
        "fit_metrics": classification_metrics(fit_labels, fit_probabilities),
        "evaluation_metrics": classification_metrics(evaluation_labels, evaluation_probabilities),
        "candidate_comparison": {
            "eligible_baseline": _candidate_summary(evaluation_rows),
            "meta_selected": _candidate_summary(selected),
        },
        "portfolio_comparison": {
            "eligible_baseline": baseline_portfolio,
            "meta_selected": selected_portfolio,
        },
        "artifacts": {
            "scored_candidates": _write_jsonl(
                stage_dir / f"{evaluate_split}_scored_candidates.jsonl", scored
            ),
            "selected_candidates": _write_jsonl(
                stage_dir / f"{evaluate_split}_selected_candidates.jsonl", selected
            ),
            "model": _write_json(stage_dir / "model.json", model_payload),
        },
    }


def _common_context(args: argparse.Namespace) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, Any],
    dict[str, Any],
    Path,
]:
    baseline, baseline_inputs = _load_baseline_splits(
        Path(args.baseline_dir), allow_holdout=args.allow_holdout
    )
    costs, config_input = _load_costs(
        Path(args.config), allow_holdout=args.allow_holdout
    )
    output_root = _guard_development_path(Path(args.output_dir), args.allow_holdout)
    context = {
        "baseline": baseline_inputs,
        "execution_config": config_input,
    }
    return baseline, costs, context, output_root


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    baseline, costs, inputs, output_root = _common_context(args)
    index, index_input = _load_index(Path(args.index), args.allow_holdout)
    inputs["index"] = index_input
    command_dir = output_root / "benchmark"
    research_config = _research_config(
        "benchmark",
        {"benchmark": "000001.SH", "alignment": "prior-index-session anchor then forward-fill"},
    )
    report: dict[str, Any] = {
        "version": "strategy_improvement.v2",
        "command": "benchmark",
        "development_only": True,
        "holdout_used": args.allow_holdout,
        "inputs": inputs,
        "research_config": research_config,
        "research_config_sha256": _sha256_bytes(_canonical_json(research_config).encode("utf-8")),
        "splits": {},
    }
    for split in SPLITS:
        split_dir = command_dir / split
        portfolio = _run_portfolio_artifacts(
            baseline[split], costs, split_dir, f"{split}_baseline"
        )
        aligned = align_equity_to_benchmark(
            portfolio.pop("_equity_curve"), index, PORTFOLIO_CONFIG["initial_cash"]
        )
        report["splits"][split] = {
            "candidate_stats": _candidate_summary(baseline[split]),
            "portfolio": portfolio,
            "benchmark_metrics": benchmark_metrics(aligned),
            "aligned_curve": _write_jsonl(
                split_dir / f"{split}_aligned_benchmark_curve.jsonl", aligned
            ),
        }
    report_artifact = _write_json(command_dir / "report.json", report)
    report["report_artifact"] = report_artifact
    return report


def run_regime(args: argparse.Namespace) -> dict[str, Any]:
    baseline, costs, inputs, output_root = _common_context(args)
    index, index_input = _load_index(Path(args.index), args.allow_holdout)
    inputs["index"] = index_input
    command_dir = output_root / "regime"
    regime = build_composite_risk_off(index)
    research_config = _research_config(
        "regime",
        {
            "variant": "composite_risk_off_v1",
            "trigger": (
                "(close<MA20 and return_5d<0) or return_5d<=-3% or "
                "20d downside-volatility>=2.5%"
            ),
            "recovery": "three consecutive closes above MA10 with positive 5d return",
            "feature_timing": "signal-day close and earlier only",
        },
    )
    report: dict[str, Any] = {
        "version": "strategy_improvement.v2",
        "command": "regime",
        "development_only": True,
        "holdout_used": args.allow_holdout,
        "inputs": inputs,
        "research_config": research_config,
        "research_config_sha256": _sha256_bytes(_canonical_json(research_config).encode("utf-8")),
        "regime_series": _write_jsonl(
            command_dir / "composite_risk_off_v1.jsonl",
            regime[
                [
                    "day",
                    "close",
                    "ma10",
                    "ma20",
                    "return_5d",
                    "downside_volatility_20d",
                    "risk_trigger",
                    "recovery_condition",
                    "risk_off",
                    "regime_version",
                ]
            ].to_dict("records"),
        ),
        "splits": {},
    }
    for split in SPLITS:
        split_dir = command_dir / split
        eligible, selected, audit = _regime_filter(baseline[split], regime)
        baseline_portfolio = _run_portfolio_artifacts(
            eligible, costs, split_dir, f"{split}_eligible_baseline"
        )
        selected_portfolio = _run_portfolio_artifacts(
            selected, costs, split_dir, f"{split}_risk_on_selected"
        )
        baseline_portfolio.pop("_equity_curve", None)
        selected_portfolio.pop("_equity_curve", None)
        report["splits"][split] = {
            "audit": audit,
            "candidate_comparison": {
                "eligible_baseline": _candidate_summary(eligible),
                "risk_on_selected": _candidate_summary(selected),
            },
            "portfolio_comparison": {
                "eligible_baseline": baseline_portfolio,
                "risk_on_selected": selected_portfolio,
            },
            "artifacts": {
                "eligible_candidates": _write_jsonl(
                    split_dir / f"{split}_eligible_candidates.jsonl", eligible
                ),
                "selected_candidates": _write_jsonl(
                    split_dir / f"{split}_risk_on_selected.jsonl", selected
                ),
            },
        }
    report_artifact = _write_json(command_dir / "report.json", report)
    report["report_artifact"] = report_artifact
    return report


def run_value(args: argparse.Namespace) -> dict[str, Any]:
    baseline, costs, inputs, output_root = _common_context(args)
    fundamentals, fundamental_inputs = _load_fundamental_splits(
        Path(args.fundamental_dir), allow_holdout=args.allow_holdout
    )
    inputs["fundamentals"] = fundamental_inputs
    command_dir = output_root / "value"
    research_config = _research_config(
        "value",
        {
            "variant": "earnings_yield_size_v1",
            "pit_join": "symbol|signal_day|signal_type; exact announcement date required",
            "earnings_yield": "1/PE only for PE>0",
            "size_filter": "exclude candidate-relative signal-day market-cap percentile <30%",
            "value_filter": "retain candidate-relative signal-day earnings-yield percentile >=50%",
            "coverage_gate_pct": VALUE_COVERAGE_GATE_PCT,
            "minimum_eligible": VALUE_MIN_ELIGIBLE,
            "minimum_selected": VALUE_MIN_SELECTED,
        },
    )
    report: dict[str, Any] = {
        "version": "strategy_improvement.v2",
        "command": "value",
        "development_only": True,
        "holdout_used": args.allow_holdout,
        "inputs": inputs,
        "research_config": research_config,
        "research_config_sha256": _sha256_bytes(_canonical_json(research_config).encode("utf-8")),
        "splits": {},
    }
    for split in SPLITS:
        split_dir = command_dir / split
        eligible, selected, audit = value_filter_rows(
            baseline[split], fundamentals[split]
        )
        baseline_portfolio = _run_portfolio_artifacts(
            eligible, costs, split_dir, f"{split}_eligible_baseline"
        )
        selected_portfolio = _run_portfolio_artifacts(
            selected, costs, split_dir, f"{split}_value_selected"
        )
        baseline_portfolio.pop("_equity_curve", None)
        selected_portfolio.pop("_equity_curve", None)
        report["splits"][split] = {
            "audit": audit,
            "candidate_comparison": {
                "eligible_baseline": _candidate_summary(eligible),
                "value_selected": _candidate_summary(selected),
            },
            "portfolio_comparison": {
                "eligible_baseline": baseline_portfolio,
                "value_selected": selected_portfolio,
            },
            "artifacts": {
                "eligible_candidates": _write_jsonl(
                    split_dir / f"{split}_eligible_candidates.jsonl", eligible
                ),
                "selected_candidates": _write_jsonl(
                    split_dir / f"{split}_value_selected.jsonl", selected
                ),
            },
        }
    report["all_splits_pass_research_coverage_gates"] = all(
        split["audit"]["passes_research_coverage_gates"]
        for split in report["splits"].values()
    )
    report_artifact = _write_json(command_dir / "report.json", report)
    report["report_artifact"] = report_artifact
    return report


def run_meta(args: argparse.Namespace) -> dict[str, Any]:
    baseline, costs, inputs, output_root = _common_context(args)
    index, index_input = _load_index(Path(args.index), args.allow_holdout)
    fundamentals, fundamental_inputs = _load_fundamental_splits(
        Path(args.fundamental_dir), allow_holdout=args.allow_holdout
    )
    inputs["index"] = index_input
    inputs["fundamentals"] = fundamental_inputs
    command_dir = output_root / "meta"
    regime = build_composite_risk_off(index)
    index_lookup = _index_feature_lookup(regime)
    datasets: dict[str, tuple[list[dict[str, Any]], np.ndarray, np.ndarray]] = {}
    dataset_audits: dict[str, Any] = {}
    for split in SPLITS:
        rows, matrix, labels, audit = _build_meta_split(
            baseline[split], index_lookup, fundamentals[split]
        )
        if len(rows) == 0:
            raise RuntimeError(f"meta split {split} has no feature-eligible candidates")
        datasets[split] = (rows, matrix, labels)
        dataset_audits[split] = audit
    research_config = _research_config(
        "meta",
        {
            "variant": "numpy_logistic_meta_v1",
            "feature_names": list(META_FEATURE_NAMES),
            "label": "trade_pnl_pct > 0",
            "threshold": 0.5,
            "l2": 1.0,
            "validation_fit": ["train"],
            "validation_evaluate": "val",
            "viewed_test_fit": ["train", "val"],
            "viewed_test_evaluate": "test",
            "forbidden_features": sorted(META_OUTCOME_FIELDS),
        },
    )
    report: dict[str, Any] = {
        "version": "strategy_improvement.v2",
        "command": "meta",
        "development_only": True,
        "holdout_used": args.allow_holdout,
        "inputs": inputs,
        "research_config": research_config,
        "research_config_sha256": _sha256_bytes(_canonical_json(research_config).encode("utf-8")),
        "dataset_audits": dataset_audits,
        "stages": {},
    }
    for stage, plan in meta_training_plan().items():
        report["stages"][stage] = _fit_meta_stage(
            stage,
            tuple(plan["fit_splits"]),
            str(plan["evaluate_split"]),
            datasets,
            costs,
            command_dir,
        )
    report_artifact = _write_json(command_dir / "report.json", report)
    report["report_artifact"] = report_artifact
    return report


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--baseline-dir", default=str(DEFAULT_BASELINE_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument(
        "--allow-holdout",
        action="store_true",
        help="explicit override; do not use during strategy development",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    benchmark = commands.add_parser("benchmark", help="report index-relative portfolio metrics")
    _add_common_arguments(benchmark)
    benchmark.add_argument("--index", default=str(DEFAULT_INDEX_PATH))

    regime = commands.add_parser("regime", help="test composite_risk_off_v1")
    _add_common_arguments(regime)
    regime.add_argument("--index", default=str(DEFAULT_INDEX_PATH))

    value = commands.add_parser("value", help="test earnings-yield and size filters")
    _add_common_arguments(value)
    value.add_argument("--fundamental-dir", default=str(DEFAULT_FUNDAMENTAL_DIR))

    meta = commands.add_parser("meta", help="test the causal NumPy logistic meta-label")
    _add_common_arguments(meta)
    meta.add_argument("--index", default=str(DEFAULT_INDEX_PATH))
    meta.add_argument("--fundamental-dir", default=str(DEFAULT_FUNDAMENTAL_DIR))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runners = {
        "benchmark": run_benchmark,
        "regime": run_regime,
        "value": run_value,
        "meta": run_meta,
    }
    report = runners[args.command](args)
    print(
        json.dumps(
            {
                "status": "complete",
                "command": args.command,
                "report": report.get("report_artifact"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
