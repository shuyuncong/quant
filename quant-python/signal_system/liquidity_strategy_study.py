"""Liquidity (30-day turnover) overlay on the three existing entry signals.

Hypothesis (docs/量化交易/quantTrading.md):
    近 30 交易日平均换手率 1%-5%（主力吸筹区间；<1% 是死水，过度高换手是分歧）。

This is a **research-only** add-on study.  It does not modify production config,
strategies or the frozen sample.  It reuses:

* ``research_backtest.replay`` for next-open entries, costs, stops and exits;
* ``strategy.registry.STRATEGIES`` and ``exit_parameters`` for the three entries;
* the prepared sample layout written by ``scale_out_take_profit_study.build_cache_sample``
  (``inputs/*.pkl`` + ``*.signals.json`` + ``prepared_metadata.json``).

Three arms, identical engine settings, only the entry candidate sets differ:

    all_signals          every executable original signal, including candidates whose
                         turnover is unknown; comparing to it also includes the
                         effect of missing-data selection, not only the turnover rule
    available_baseline   signals whose 30-bar turnover is evaluable (any value)
    turnover_1_to_5      available_baseline restricted to mean turnover in [1, 5] %

The real comparison is ``available_baseline`` vs ``turnover_1_to_5``.  Unknown
turnover is *not* treated as a pass or as zero: it is removed from both comparison
arms and counted under an explicit reason.

Causality:
* turnover is the arithmetic mean of ``history.turnover_rate`` (percentage points)
  over the signal bar and the preceding 29 closed bars -- never a later bar;
* zero, negative, NaN/non-finite, a missing column, or fewer than 30 bars make the
  candidate unavailable;
* the signal-day bar is the decision bar; replay still buys at the next open;
* sample frames are truncated at the window end before any signal/turnover work, so
  no sealed future holdout can leak in.

Input validation (reject rather than silently repair):
* pickles must carry ``attrs.adjust == "qfq"`` and ``attrs.timeframe == "1d"``;
* datetimes must parse, be strictly increasing and unique;
* every signal index must be a real integer inside its own symbol's frame;
* ``prepared_metadata.calendar`` must cover every in-window bar date.

Usage::

    python liquidity_strategy_study.py \\
        --sample output/backtests/_gate_samples/sample-2024-01-01-2024-06-30 \\
        --out output/backtests/liquidity-turnover-2024h1/liquidity_study.json

The window defaults to ``prepared_metadata.start/end``.  ``--start/--end`` are only
accepted when they equal that metadata window.  The engine config comes from
``prepared_metadata.config_snapshot``; only when that key is absent may an explicit
``--config`` YAML be given (a config passed while a snapshot exists is recorded as
ignored, never applied).  There is deliberately no fallback to a historical
``comparison.json``: parameters can never be inherited silently.

Output schema (JSON)::

    study/version/generated_at/script_sha256/label
    hypothesis
    sample {path, window, metadata, exclusion_reasons, hashes, validation,
            config_source}
    parameters {replay_options, engine_config, engine_config_sha256, exit_rules,
                cost_rules, arm_definitions}
    strategies {<id>: {name, candidates, availability, turnover, arms, comparison,
                       sufficiency, notes}}
    sufficiency {status, flags, minimums}
    warnings
    conclusion
    scope {offline, network, database, future_holdout_used_in_calculations,
           frames_truncated_at_window_end, production_config_changed,
           broker_integration}

Every arm embeds the real replay ``metrics``, all closed ``trades``, the number of
still-open positions at the window end, plus an equity-curve digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
for _path in (BASE, BASE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import numpy as np
import pandas as pd

from research_backtest import (
    CONFIG_KEYS,
    WARNING_LIMITS,
    WARNING_SIGNAL_PARITY,
    WARNING_SURVIVORSHIP,
    _illegal_history_rows,
    file_sha256,
    replay,
    write_json,
)
from strategy.registry import STRATEGIES, VERSION, exit_parameters
from utils.helpers import load_config
from utils.time_utils import now_shanghai

TURNOVER_WINDOW = 30
TURNOVER_MIN_PCT = 1.0
TURNOVER_MAX_PCT = 5.0
TURNOVER_COLUMN = "turnover_rate"

ARMS = ("all_signals", "available_baseline", "turnover_1_to_5")
ARM_DEFINITIONS = {
    "all_signals": "所有窗口内可执行的原信号；保留换手率不可评估的候选，仅作背景基线，不参与效果比较",
    "available_baseline": "只保留信号日截至当日 30 根已收盘 bar 换手率可评估的候选（不限数值）",
    "turnover_1_to_5": "在 available_baseline 上保留 30 日均换手率落在 [1%, 5%] 的候选",
}
MIN_COVERAGE_PCT = 90.0
MIN_UNIQUE_CANDIDATES = 30
MIN_UNIQUE_SYMBOLS = 10
MIN_UNIQUE_DAYS = 10

REASON_MISSING_COLUMN = "turnover_column_missing"
REASON_SHORT_HISTORY = "insufficient_closed_bars"
REASON_UNCLOSED = "unclosed_bar_in_window"
REASON_NAN = "nan_or_nonfinite_turnover"
REASON_NONPOSITIVE = "zero_or_negative_turnover"

DEFAULT_MAX_POSITIONS = 4
DEFAULT_INITIAL_CASH = 250_000.0
DELTA_FIELDS = (
    "total_return_pct",
    "annualized_return_pct",
    "max_drawdown_pct",
    "sharpe_ratio",
    "payoff_ratio",
    "win_rate_pct",
    "closed_trades",
    "final_equity",
)
COST_KEYS = (
    "commission_pct",
    "minimum_commission",
    "slippage_pct",
    "stamp_tax_pct",
    "lot_size",
    "t_plus_one",
    "price_limit_model",
    "intrabar_conflict",
    "adjustment",
)


class SampleValidationError(ValueError):
    """The prepared sample is not safe to replay; never repair it silently."""


# --------------------------------------------------------------------------- #
# Turnover availability
# --------------------------------------------------------------------------- #


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "mean_turnover_pct": None, "reason": reason, "passes": None}


def _turnover_values(frame: pd.DataFrame) -> np.ndarray | None:
    if TURNOVER_COLUMN not in frame.columns:
        return None
    return pd.to_numeric(frame[TURNOVER_COLUMN], errors="coerce").to_numpy(dtype=float)


def _closed_flags(frame: pd.DataFrame) -> np.ndarray | None:
    if "is_closed" not in frame.columns:
        # Prepared daily samples without the flag are already closed-bar series.
        return None
    return frame["is_closed"].fillna(False).astype(bool).to_numpy()


def _assess_window(
    values: np.ndarray | None,
    closed: np.ndarray | None,
    index: int,
    window: int,
    minimum: float,
    maximum: float,
) -> dict[str, Any]:
    if values is None:
        return _unavailable(REASON_MISSING_COLUMN)
    if index + 1 < window:
        return _unavailable(REASON_SHORT_HISTORY)
    first = index - window + 1
    if closed is not None and not bool(closed[first:index + 1].all()):
        return _unavailable(REASON_UNCLOSED)
    sample = values[first:index + 1]
    if not np.isfinite(sample).all():
        return _unavailable(REASON_NAN)
    if (sample <= 0).any():
        return _unavailable(REASON_NONPOSITIVE)
    mean = float(sample.mean())
    return {
        "available": True,
        "mean_turnover_pct": mean,
        "reason": None,
        "passes": bool(minimum <= mean <= maximum),
    }


def evaluate_turnover(
    frame: pd.DataFrame,
    index: int,
    window: int = TURNOVER_WINDOW,
    minimum: float = TURNOVER_MIN_PCT,
    maximum: float = TURNOVER_MAX_PCT,
) -> dict[str, Any]:
    """Turnover availability *as of* ``index`` using only bars ``index-window+1 .. index``.

    ``turnover_rate`` is a percentage number (0.34 means 0.34 %).  Returns a dict with
    ``available``, ``mean_turnover_pct``, ``reason`` and ``passes``.  ``passes`` is
    ``None`` when unavailable so a missing value can never be read as pass or zero.
    """
    if isinstance(index, bool) or not isinstance(index, (int, np.integer)):
        raise TypeError("signal index must be an integer")
    index = int(index)
    if index < 0 or index >= len(frame):
        raise IndexError(f"signal index {index} out of range for {len(frame)} bars")
    return _assess_window(
        _turnover_values(frame), _closed_flags(frame), index, int(window), float(minimum), float(maximum)
    )


def _signal_day(frame: pd.DataFrame, index: int) -> str:
    return pd.Timestamp(frame["datetime"].iloc[index]).strftime("%Y-%m-%d")


def _records_for_frame(
    symbol: str,
    frame: pd.DataFrame,
    indices: set[int] | list[int],
    start: str,
    end: str,
    window: int = TURNOVER_WINDOW,
    minimum: float = TURNOVER_MIN_PCT,
    maximum: float = TURNOVER_MAX_PCT,
) -> list[dict[str, Any]]:
    values = _turnover_values(frame)
    closed = _closed_flags(frame)
    records: list[dict[str, Any]] = []
    for index in sorted(int(value) for value in indices):
        if index < 0:
            raise ValueError(f"{symbol}: 信号索引 {index} 不能为负")
        if index >= len(frame):
            # The index is legal in the original file but points after the research
            # window; truncation removed those bars, so the signal is simply ignored.
            continue
        day = _signal_day(frame, index)
        if not (start <= day <= end):
            continue
        outcome = _assess_window(values, closed, index, int(window), float(minimum), float(maximum))
        records.append(
            {
                "symbol": symbol,
                "index": index,
                "date": day,
                "executable": bool(index + 1 < len(frame)),
                **outcome,
            }
        )
    return records


def classify_sample(
    histories: dict[str, pd.DataFrame],
    signals: dict[str, dict[str, set[int]]],
    start: str,
    end: str,
    window: int = TURNOVER_WINDOW,
    minimum: float = TURNOVER_MIN_PCT,
    maximum: float = TURNOVER_MAX_PCT,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Evaluate every in-window signal with that symbol's own bars (no cross-symbol dates)."""
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for symbol in sorted(histories):
        frame = histories[symbol]
        result[symbol] = {
            strategy: _records_for_frame(
                symbol, frame, signals[symbol][strategy], start, end, window, minimum, maximum
            )
            for strategy in STRATEGIES
        }
    return result


def classification_pools(
    classifications: dict[str, dict[str, list[dict[str, Any]]]], strategy: str
) -> dict[str, list[dict[str, Any]]]:
    """Executable candidate pools for the three arms (nested by construction)."""
    records = [
        record
        for symbol in sorted(classifications)
        for record in classifications[symbol][strategy]
        if record["executable"]
    ]
    available = [record for record in records if record["available"]]
    turnover = [record for record in available if record["passes"]]
    return {"all_signals": records, "available_baseline": available, "turnover_1_to_5": turnover}


def arm_signal_maps(
    signals: dict[str, dict[str, set[int]]],
    classifications: dict[str, dict[str, list[dict[str, Any]]]],
    strategy: str,
) -> dict[str, dict[str, dict[str, set[int]]]]:
    """Build the three signal maps.  Every non-entry field and other strategy is copied."""
    base = {
        symbol: {name: set(values) for name, values in per_strategy.items()}
        for symbol, per_strategy in signals.items()
    }
    pools = classification_pools(classifications, strategy)
    arms: dict[str, dict[str, dict[str, set[int]]]] = {}
    for arm in ARMS:
        arm_map = deepcopy(base)
        indices = {record["symbol"]: set() for record in pools[arm]}
        for record in pools[arm]:
            indices[record["symbol"]].add(int(record["index"]))
        for symbol in arm_map:
            arm_map[symbol][strategy] = indices.get(symbol, set())
        arms[arm] = arm_map
    return arms


def _pool_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "unique_candidates": len(records),
        "unique_symbols": len({record["symbol"] for record in records}),
        "unique_signal_days": len({record["date"] for record in records}),
    }


def summarize_classifications(
    classifications: dict[str, dict[str, list[dict[str, Any]]]],
    strategy: str,
    signals: dict[str, dict[str, set[int]]] | None = None,
) -> dict[str, Any]:
    pools = classification_pools(classifications, strategy)
    all_records = [
        record
        for symbol in classifications
        for record in classifications[symbol][strategy]
    ]
    candidates = pools["all_signals"]
    available = pools["available_baseline"]
    turnover = pools["turnover_1_to_5"]
    reasons = Counter(record["reason"] for record in candidates if not record["available"])
    means = [float(record["mean_turnover_pct"]) for record in available]
    total = (
        sum(len(signals[symbol][strategy]) for symbol in signals)
        if signals is not None
        else len(all_records)
    )
    candidate_count = len(candidates)
    coverage = 100.0 * len(available) / candidate_count if candidate_count else 0.0
    summary = {
        "signals_total": total,
        "signals_in_window": len(all_records),
        "signals_outside_window": total - len(all_records),
        "non_executable_signals": len(all_records) - candidate_count,
        "candidates": _pool_counts(candidates),
        "available": _pool_counts(available),
        "turnover_pool": _pool_counts(turnover),
        "available_count": len(available),
        "unavailable_count": candidate_count - len(available),
        "coverage_pct": coverage,
        "by_reason": dict(sorted(reasons.items())),
        "turnover_reject_count": len(available) - len(turnover),
        "turnover_means": {
            "evaluable": len(means),
            "min_pct": min(means) if means else None,
            "max_pct": max(means) if means else None,
            "mean_pct": (sum(means) / len(means)) if means else None,
        },
    }
    summary["sufficiency"] = sufficiency(available_count=len(available), candidate_count=candidate_count, candidate_symbols=summary["candidates"]["unique_symbols"], candidate_days=summary["candidates"]["unique_signal_days"], turnover_count=len(turnover), turnover_symbols=summary["turnover_pool"]["unique_symbols"], turnover_days=summary["turnover_pool"]["unique_signal_days"])
    return summary


def sufficiency(
    *,
    available_count: int,
    candidate_count: int,
    candidate_symbols: int,
    candidate_days: int,
    turnover_count: int,
    turnover_symbols: int,
    turnover_days: int,
) -> dict[str, Any]:
    """Sample-size gates.  Exact boundaries pass; anything below is insufficient."""
    coverage = 100.0 * available_count / candidate_count if candidate_count else 0.0
    flags: list[str] = []
    if coverage < MIN_COVERAGE_PCT:
        flags.append("coverage_below_90pct")
    if candidate_count < MIN_UNIQUE_CANDIDATES:
        flags.append("unique_candidates_below_30")
    if candidate_symbols < MIN_UNIQUE_SYMBOLS:
        flags.append("unique_symbols_below_10")
    if candidate_days < MIN_UNIQUE_DAYS:
        flags.append("unique_signal_days_below_10")
    if turnover_count < MIN_UNIQUE_CANDIDATES:
        flags.append("turnover_pool_unique_candidates_below_30")
    if turnover_symbols < MIN_UNIQUE_SYMBOLS:
        flags.append("turnover_pool_unique_symbols_below_10")
    if turnover_days < MIN_UNIQUE_DAYS:
        flags.append("turnover_pool_unique_signal_days_below_10")
    return {
        "status": "insufficient_sample" if flags else "sufficient",
        "flags": flags,
        "coverage_pct": coverage,
        "minimums": {
            "coverage_pct": MIN_COVERAGE_PCT,
            "unique_candidates": MIN_UNIQUE_CANDIDATES,
            "unique_symbols": MIN_UNIQUE_SYMBOLS,
            "unique_signal_days": MIN_UNIQUE_DAYS,
        },
        "turnover_pool": {
            "unique_candidates": turnover_count,
            "unique_symbols": turnover_symbols,
            "unique_signal_days": turnover_days,
        },
    }


# --------------------------------------------------------------------------- #
# Sample loading / validation / hashing
# --------------------------------------------------------------------------- #


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (BASE / path)


def _truncate(frame: pd.DataFrame, end: str) -> pd.DataFrame:
    """Drop bars after the research window before any signal/turnover computation."""
    if "datetime" not in frame.columns:
        return frame
    dates = pd.to_datetime(frame["datetime"], errors="coerce")
    if dates.isna().any():
        raise SampleValidationError("历史行情包含无法解析的 datetime")
    if dates.duplicated().any():
        raise SampleValidationError("历史行情日期重复（含窗口外未来行）")
    if not dates.is_monotonic_increasing:
        raise SampleValidationError("历史行情日期不是单调递增（含窗口外未来行）")
    keep = dates <= pd.Timestamp(end)
    truncated = frame.loc[keep].copy()
    truncated.attrs = dict(frame.attrs)
    return truncated.reset_index(drop=True)


def load_sample(
    root: Path, *, end: str | None = None
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, set[int]]], dict[str, Any], dict[str, int]]:
    """Read a prepared sample.  ``original_lengths`` keeps pre-truncation index legality."""
    root = _resolve_path(root)
    metadata_path = root / "prepared_metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"样本缺少 prepared_metadata.json: {root}")
    inputs = root / "inputs"
    if not inputs.is_dir():
        raise FileNotFoundError(f"样本缺少 inputs/ 目录: {root}")
    meta = json.loads(metadata_path.read_text(encoding="utf-8"))

    histories: dict[str, pd.DataFrame] = {}
    original_lengths: dict[str, int] = {}
    for path in sorted(inputs.glob("*.pkl")):
        frame = pd.read_pickle(path)
        if not isinstance(frame, pd.DataFrame):
            raise SampleValidationError(f"{path.name} 不是 DataFrame")
        symbol = path.stem
        original_lengths[symbol] = len(frame)
        histories[symbol] = _truncate(frame, end) if end is not None else frame

    signals: dict[str, dict[str, set[int]]] = {}
    for path in sorted(root.glob("*.signals.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "signals" not in payload:
            raise SampleValidationError(f"{path.name} 缺少 signals 字段")
        signals[path.name[: -len(".signals.json")]] = {
            name: set(values) for name, values in payload["signals"].items()
        }
    return histories, signals, meta, original_lengths


def resolve_window(
    meta: dict[str, Any], start: str | None, end: str | None
) -> tuple[str, str, str]:
    """Metadata window wins; explicit dates must reproduce it exactly."""
    meta_start, meta_end = meta.get("start"), meta.get("end")
    if meta_start and meta_end:
        if (start or end) and (start != meta_start or end != meta_end):
            raise ValueError(
                f"显式窗口 {start!r}~{end!r} 与 metadata 窗口 {meta_start}~{meta_end} 不一致；"
                "本脚本只允许用显式参数复述同一窗口"
            )
        start, end, source = str(meta_start), str(meta_end), "prepared_metadata"
    else:
        if not (start and end):
            raise ValueError("metadata 缺少 start/end，必须显式给出 --start 与 --end")
        source = "explicit_arguments"
    try:
        start_date, end_date = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    except ValueError as error:
        raise ValueError(f"窗口日期必须为 YYYY-MM-DD: {start!r}~{end!r}") from error
    if start_date >= end_date:
        raise ValueError("窗口起始日必须早于结束日")
    return str(start), str(end), source


def _validate_frame(symbol: str, frame: pd.DataFrame) -> None:
    attrs = frame.attrs or {}
    if attrs.get("adjust") != "qfq":
        raise SampleValidationError(
            f"{symbol}: 样本 attrs.adjust={attrs.get('adjust')!r}，必须是 'qfq'（拒绝未标注复权口径的数据）"
        )
    if attrs.get("timeframe") != "1d":
        raise SampleValidationError(
            f"{symbol}: 样本 attrs.timeframe={attrs.get('timeframe')!r}，必须是 '1d'"
        )
    required = {"datetime", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise SampleValidationError(f"{symbol}: 缺少列 {missing}")
    dates = pd.to_datetime(frame["datetime"], errors="coerce")
    if dates.isna().any():
        raise SampleValidationError(f"{symbol}: datetime 含空值/非法值")
    if not dates.is_monotonic_increasing:
        raise SampleValidationError(f"{symbol}: 日期不是单调递增")
    if dates.duplicated().any():
        raise SampleValidationError(f"{symbol}: 日期重复")
    illegal = _illegal_history_rows(frame)
    if illegal:
        raise SampleValidationError(f"{symbol}: 有 {illegal} 根价格/成交量非法的 bar")


def resolve_calendar(
    meta: dict[str, Any], histories: dict[str, pd.DataFrame], start: str, end: str
) -> tuple[list[str], int]:
    raw = meta.get("calendar")
    if not isinstance(raw, list) or not raw:
        raise SampleValidationError("prepared_metadata.calendar 缺失或为空")
    days: list[str] = []
    ignored = 0
    for value in raw:
        day = str(value)
        try:
            date.fromisoformat(day)
        except ValueError as error:
            raise SampleValidationError(f"calendar 日期非法: {day!r}") from error
        if not (start <= day <= end):
            ignored += 1
            continue
        days.append(day)
    if not days:
        raise SampleValidationError("窗口内没有交易日")
    if len(set(days)) != len(days):
        raise SampleValidationError("calendar 含重复交易日")
    if days != sorted(days):
        raise SampleValidationError("calendar 未按日期排序")
    calendar_set = set(days)
    for symbol, frame in histories.items():
        dates = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d")
        inside = dates[(dates >= start) & (dates <= end)]
        if inside.empty:
            raise SampleValidationError(f"{symbol}: 窗口内没有任何 bar")
        unknown = sorted(set(inside) - calendar_set)
        if unknown:
            raise SampleValidationError(
                f"{symbol}: {len(unknown)} 个交易日期不在 metadata.calendar（例如 {unknown[:3]}）"
            )
    return days, ignored


def validate_sample(
    histories: dict[str, pd.DataFrame],
    signals: dict[str, dict[str, set[int]]],
    meta: dict[str, Any],
    start: str,
    end: str,
    original_lengths: dict[str, int],
) -> dict[str, Any]:
    if not histories:
        raise SampleValidationError("样本没有 inputs/*.pkl")
    missing_signals = sorted(set(histories) - set(signals))
    extra_signals = sorted(set(signals) - set(histories))
    if missing_signals or extra_signals:
        raise SampleValidationError(
            f"行情与信号文件不配对: 缺信号 {missing_signals[:5]}，多信号 {extra_signals[:5]}"
        )
    expected = set(STRATEGIES)
    for symbol, frame in histories.items():
        _validate_frame(symbol, frame)
        per_strategy = signals[symbol]
        if set(per_strategy) != expected:
            raise SampleValidationError(
                f"{symbol}: 策略集合 {sorted(per_strategy)} 与 registry {sorted(expected)} 不一致"
            )
        for strategy, indices in per_strategy.items():
            if not isinstance(indices, (set, list, tuple)):
                raise SampleValidationError(f"{symbol}/{strategy}: 信号索引不是列表")
            for index in indices:
                if isinstance(index, bool) or not isinstance(index, int):
                    raise SampleValidationError(f"{symbol}/{strategy}: 非法索引 {index!r}（必须是整数）")
                if index < 0 or index >= original_lengths[symbol]:
                    raise SampleValidationError(
                        f"{symbol}/{strategy}: 索引 {index} 超出行情长度 {original_lengths[symbol]}"
                    )
    return {
        "symbols": len(histories),
        "signal_files": len(signals),
        "strategies": list(STRATEGIES),
        "attrs": {"adjust": "qfq", "timeframe": "1d"},
        "signal_index_check": "ok",
    }


def _sha_manifest(rows: list[tuple[str, str]]) -> str:
    text = "".join(f"{name}\t{digest}\n" for name, digest in sorted(rows))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_sample_files(root: Path) -> dict[str, Any]:
    """SHA256 over the real file bytes plus deterministic per-group manifests."""
    root = _resolve_path(root)
    metadata = root / "prepared_metadata.json"
    if not metadata.is_file():
        raise FileNotFoundError(f"样本缺少 prepared_metadata.json: {root}")
    input_paths = sorted((root / "inputs").glob("*.pkl"))
    signal_paths = sorted(root.glob("*.signals.json"))
    if not input_paths:
        raise SampleValidationError("样本没有 inputs/*.pkl")
    if not signal_paths:
        raise SampleValidationError("样本没有 *.signals.json")
    input_rows = [(path.name, file_sha256(path)) for path in input_paths]
    signal_rows = [(path.name, file_sha256(path)) for path in signal_paths]
    metadata_digest = file_sha256(metadata)
    combined = _sha_manifest(
        [("inputs/" + name, digest) for name, digest in input_rows]
        + [("prepared_metadata.json", metadata_digest)]
        + [(name, digest) for name, digest in signal_rows]
    )
    return {
        "prepared_metadata_sha256": metadata_digest,
        "inputs": {"count": len(input_rows), "manifest_sha256": _sha_manifest(input_rows)},
        "signals": {"count": len(signal_rows), "manifest_sha256": _sha_manifest(signal_rows)},
        "manifest_sha256": combined,
        "files_sha256": {
            "prepared_metadata.json": metadata_digest,
            **{f"inputs/{name}": digest for name, digest in input_rows},
            **{name: digest for name, digest in signal_rows},
        },
    }


# --------------------------------------------------------------------------- #
# Replay / report
# --------------------------------------------------------------------------- #


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()




def execute_arm(
    histories: dict[str, pd.DataFrame],
    signals: dict[str, dict[str, set[int]]],
    calendar: list[str],
    config: dict[str, Any],
    options: dict[str, Any],
    strategy: str,
) -> dict[str, Any]:
    result = replay(histories, signals, calendar, deepcopy(config), dict(options), strategy)
    curve = result.get("equity_curve") or []
    rejected = Counter(item.get("reason", "unknown") for item in result.get("rejected") or [])
    return {
        "metrics": deepcopy(result["metrics"]),
        "trades": deepcopy(result.get("trades") or []),
        "open_positions": int(result.get("open_positions") or 0),
        "final_positions": int(curve[-1]["positions"]) if curve else 0,
        "equity_curve": {
            "points": len(curve),
            "sha256": canonical_sha(curve),
        },
        "warnings": list(result.get("warnings") or []),
        "rejected_orders": {"count": sum(rejected.values()), "by_reason": dict(sorted(rejected.items()))},
    }


def _delta(base: Any, filtered: Any) -> Any:
    if base is None or filtered is None:
        return None
    return float(filtered) - float(base)


def _comparison(arms: dict[str, Any]) -> dict[str, Any]:
    baseline = arms["available_baseline"]["metrics"]
    filtered = arms["turnover_1_to_5"]["metrics"]
    return {
        "baseline_arm": "available_baseline",
        "filtered_arm": "turnover_1_to_5",
        "baseline_metrics": deepcopy(baseline),
        "filtered_metrics": deepcopy(filtered),
        "all_signals_role": "context_only_not_an_effect_estimate",
        "deltas": {f"{field}_delta": _delta(baseline.get(field), filtered.get(field)) for field in DELTA_FIELDS},
    }


def _report_config(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    snapshot = {key: deepcopy(config[key]) for key in CONFIG_KEYS if key in config}
    extra = sorted(
        str(key) for key in config if key not in CONFIG_KEYS and not str(key).startswith("_")
    )
    return snapshot, extra


def _exclusion_reasons(meta: dict[str, Any]) -> dict[str, int]:
    rows = meta.get("exclusions") or []
    return dict(sorted(Counter(str(row.get("reason", "unknown")) for row in rows if isinstance(row, dict)).items()))


def build_report(
    sample_dir: Path | str,
    config: dict[str, Any],
    *,
    config_source: dict[str, Any],
    start: str,
    end: str,
    options: dict[str, Any],
    label: str = "",
    window_source: str = "prepared_metadata",
) -> dict[str, Any]:
    root = _resolve_path(sample_dir)
    histories, signals, meta, original_lengths = load_sample(root, end=end)
    validation = validate_sample(histories, signals, meta, start, end, original_lengths)
    calendar, ignored_calendar = resolve_calendar(meta, histories, start, end)
    validation["calendar_days"] = len(calendar)
    validation["ignored_calendar_dates"] = ignored_calendar
    validation["frame_truncated_at"] = end

    classifications = classify_sample(histories, signals, start, end)
    snapshot, extra_config_keys = _report_config(config)
    exit_rules = {strategy: exit_parameters(config, strategy) for strategy in STRATEGIES}
    engine_sha = canonical_sha({"config": snapshot, "options": options, "exit_rules": exit_rules})
    hashes = hash_sample_files(root)

    strategies_report: dict[str, Any] = {}
    overall_flags: list[str] = []
    for strategy in STRATEGIES:
        summary = summarize_classifications(classifications, strategy, signals)
        arms_maps = arm_signal_maps(signals, classifications, strategy)
        pools = classification_pools(classifications, strategy)
        arm_results: dict[str, Any] = {}
        for arm in ARMS:
            result = execute_arm(histories, arms_maps[arm], calendar, config, options, strategy)
            counts = _pool_counts(pools[arm])
            counts["dropped_vs_all"] = summary["candidates"]["unique_candidates"] - counts["unique_candidates"]
            result["signals"] = counts
            result["definition"] = ARM_DEFINITIONS[arm]
            arm_results[arm] = result

        suff = summary["sufficiency"]
        if suff["status"] != "sufficient":
            overall_flags.extend(f"{strategy}:{flag}" for flag in suff["flags"])

        strategies_report[strategy] = {
            "name": STRATEGIES[strategy],
            "candidates": {
                "signals_total": summary["signals_total"],
                "signals_in_window": summary["signals_in_window"],
                "signals_outside_window": summary["signals_outside_window"],
                "non_executable_signals": summary["non_executable_signals"],
                **summary["candidates"],
            },
            "availability": {
                "available": summary["available_count"],
                "unavailable": summary["unavailable_count"],
                "coverage_pct": summary["coverage_pct"],
                "by_reason": summary["by_reason"],
                "available_pool": summary["available"],
            },
            "turnover": {
                "pass": summary["turnover_pool"]["unique_candidates"],
                "reject": summary["turnover_reject_count"],
                "pool": summary["turnover_pool"],
                **summary["turnover_means"],
            },
            "arms": arm_results,
            "comparison": _comparison(arm_results),
            "sufficiency": suff,
            "notes": [
                "all_signals 含换手率不可评估的候选，只作背景；真实对照是 available_baseline vs turnover_1_to_5",
                "未知换手率不按通过、也不按 0 处理，直接从比较臂剔除并按原因计数",
            ],
        }

    status = "insufficient_sample" if overall_flags else "sufficient"
    sufficiency_block = {
        "status": status,
        "flags": overall_flags,
        "minimums": {
            "coverage_pct": MIN_COVERAGE_PCT,
            "unique_candidates": MIN_UNIQUE_CANDIDATES,
            "unique_symbols": MIN_UNIQUE_SYMBOLS,
            "unique_signal_days": MIN_UNIQUE_DAYS,
        },
        "scope": "每个策略的候选池与 turnover 1-5% 池都必须达标",
    }

    meta_summary = {
        key: meta.get(key)
        for key in ("start", "end", "source", "version", "cache_dir")
        if key in meta
    }
    meta_summary["exclusions"] = len(meta.get("exclusions") or [])
    meta_summary["universe"] = deepcopy(meta.get("universe"))
    warnings = [
        WARNING_SURVIVORSHIP,
        "当前样本只覆盖快照时点在市的股票，不含完整退市/历史 ST 状态，存在幸存者偏差；不是完整 PIT 数据。",
        "换手率只取信号 bar 及之前 29 根已收盘日线，不使用未来 bar；次日开盘成交仍由 research_backtest.replay 负责。",
        "turnover_rate 按百分数读取（0.34 = 0.34%），不做单位推断或 volume/amount 代算；0/NaN/负值一律不可评估。",
        "1%-5% 为文档给定的固定阈值，本轮不做参数网格/调优；结论不能外推为投产参数。",
        "all_signals 是原信号的背景收益；它与过滤组的差异同时包含缺失数据筛选，不能单独归因于换手率规则。",
        WARNING_LIMITS,
        WARNING_SIGNAL_PARITY,
        "本报告是离线历史研究，不宣称实盘改善，不改生产 config/strategy，不接券商、数据库或通知。",
    ]
    if config_source.get("ignored_config"):
        warnings.insert(
            0,
            f"样本自带 config_snapshot，已忽略 --config {config_source['ignored_config']}；参数一律以样本快照为准。",
        )
    if overall_flags:
        warnings.append("样本不足（insufficient_sample）：" + "；".join(overall_flags))

    report = {
        "study": "liquidity_turnover_filter",
        "version": VERSION,
        "generated_at": now_shanghai().isoformat(),
        "script_sha256": file_sha256(Path(__file__).resolve()),
        "label": label or f"{start}~{end}",
        "hypothesis": {
            "source": "docs/量化交易/quantTrading.md",
            "rule": "近 30 交易日平均换手率 1%-5%",
            "window_bars": TURNOVER_WINDOW,
            "min_pct": TURNOVER_MIN_PCT,
            "max_pct": TURNOVER_MAX_PCT,
            "inclusive_bounds": True,
            "thresholds_fixed": True,
            "parameter_grid": False,
        },
        "sample": {
            "path": str(root),
            "window": {
                "start": start,
                "end": end,
                "source": window_source,
                "trading_days": len(calendar),
                "first_day": calendar[0],
                "last_day": calendar[-1],
            },
            "metadata": meta_summary,
            "exclusion_reasons": _exclusion_reasons(meta),
            "hashes": hashes,
            "validation": validation,
            "config_source": config_source,
        },
        "parameters": {
            "replay_options": dict(options),
            "engine_config": snapshot,
            "engine_config_sha256": engine_sha,
            "engine_config_omitted_keys": extra_config_keys,
            "exit_rules": exit_rules,
            "cost_rules": {
                key: (config.get("backtest") or {}).get(key)
                for key in COST_KEYS
                if (config.get("backtest") or {}).get(key) is not None
            },
            "arm_definitions": {arm: ARM_DEFINITIONS[arm] for arm in ARMS},
        },
        "strategies": strategies_report,
        "sufficiency": sufficiency_block,
        "warnings": warnings,
        "conclusion": {
            "status": "research_only",
            "statement": "离线历史对照，不构成实盘收益改善结论；阈值固定未调优。",
            "comparison_basis": "available_baseline vs turnover_1_to_5",
            "all_signals_role": "仅背景基线，不参与效果比较",
            "production_change": "none",
        },
        "scope": {
            "offline": True,
            "network": False,
            "database": False,
            "future_holdout_used_in_calculations": False,
            "frames_truncated_at_window_end": True,
            "production_config_changed": False,
            "broker_integration": False,
        },
    }
    return report


def _print_summary(report: dict[str, Any], out: Path) -> None:
    window = report["sample"]["window"]
    print(f"样本 {report['sample']['path']} | 窗口 {window['start']}~{window['end']} ({window['trading_days']} 交易日)")
    print(f"{'策略':<18}{'候选':>6}{'可评估':>8}{'覆盖%':>8}{'1-5%':>7}"
          f"{'baseline收益%':>15}{'filtered收益%':>16}{'状态':>18}")
    for strategy, payload in report["strategies"].items():
        availability = payload["availability"]
        baseline = payload["arms"]["available_baseline"]["metrics"].get("total_return_pct")
        filtered = payload["arms"]["turnover_1_to_5"]["metrics"].get("total_return_pct")

        def fmt(value: Any) -> str:
            return "—" if value is None else f"{value:.2f}"

        print(
            f"{strategy:<18}{payload['candidates']['unique_candidates']:>6}"
            f"{availability['available']:>8}{availability['coverage_pct']:>8.2f}"
            f"{payload['turnover']['pass']:>7}{fmt(baseline):>15}{fmt(filtered):>16}"
            f"{payload['sufficiency']['status']:>18}"
        )
    print(f"\n样本状态: {report['sufficiency']['status']}")
    for warning in report["warnings"]:
        print(f"  ! {warning}")
    print(f"\n写出 {out}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="30 日平均换手率 1%-5% 过滤的三策略离线对照研究")
    parser.add_argument("--sample", required=True, help="prepared 样本目录（inputs/*.pkl + *.signals.json + prepared_metadata.json）")
    parser.add_argument("--out", required=True, help="输出 JSON 路径（相对路径按仓库根解析）")
    parser.add_argument("--config", default=None, help="仅当 metadata 没有 config_snapshot 时使用的显式 YAML")
    parser.add_argument("--start", default=None, help="可选；必须与 metadata.start 相同")
    parser.add_argument("--end", default=None, help="可选；必须与 metadata.end 相同")
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_INITIAL_CASH)
    parser.add_argument("--max-positions", type=int, default=DEFAULT_MAX_POSITIONS)
    parser.add_argument("--label", default="")
    return parser.parse_args(argv)


def _config_for_sample(
    meta: dict[str, Any], config_path: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = meta.get("config_snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, dict) or not snapshot:
            raise SampleValidationError("config_snapshot 存在但为空或不是对象")
        source = {
            "type": "prepared_metadata.config_snapshot",
            "path": None,
            "file_sha256": None,
            "snapshot_sha256": canonical_sha(snapshot),
        }
        if config_path:
            # Still snapshot-first, but never silent: the override attempt is recorded.
            source["ignored_config"] = str(_resolve_path(config_path))
        return deepcopy(snapshot), source
    if not config_path:
        raise ValueError(
            "样本没有 config_snapshot，且未提供 --config；拒绝从历史 comparison 静默取参数"
        )
    path = _resolve_path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    loaded = load_config(str(path))
    if not loaded:
        raise ValueError(f"读取配置失败: {path}")
    config = {key: value for key, value in loaded.items() if not str(key).startswith("_")}
    return config, {
        "type": "explicit_yaml",
        "path": str(path),
        "file_sha256": file_sha256(path),
        "snapshot_sha256": canonical_sha(_report_config(config)[0]),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if not math.isfinite(args.initial_cash) or args.initial_cash <= 0:
            raise ValueError("--initial-cash 必须为正的有限数值")
        if args.max_positions <= 0:
            raise ValueError("--max-positions 必须为正整数")
        sample_dir = _resolve_path(args.sample)
        # The window must be known before loading so frames can be truncated at `end`.
        metadata_path = sample_dir / "prepared_metadata.json"
        if not metadata_path.is_file():
            raise FileNotFoundError(f"样本缺少 prepared_metadata.json: {sample_dir}")
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        start, end, window_source = resolve_window(meta, args.start, args.end)
        config, config_source = _config_for_sample(meta, args.config)
        options = {
            "start": start,
            "end": end,
            "max_positions": int(args.max_positions),
            "initial_cash": float(args.initial_cash),
        }
        report = build_report(
            sample_dir,
            config,
            config_source=config_source,
            start=start,
            end=end,
            options=options,
            label=args.label,
            window_source=window_source,
        )
        out = _resolve_path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        write_json(out, report)
    except (SampleValidationError, FileNotFoundError, ValueError, KeyError) as error:
        print(f"[拒绝] {error}", file=sys.stderr)
        return 2
    _print_summary(report, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
