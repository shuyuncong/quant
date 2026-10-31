#!/usr/bin/env python
"""Pre-registered time-holdout audit for the yearline-pullback + MA20 variant.

The development study (``ma20_entry_filter_study.py``) used 2018H2..2024Q4.
This script evaluates the *fixed* variant on a later, untouched-for-this-variant
window set: 2025H1, 2025H2 and 2026H1.  The rule, gates and decision rule are
written to a sealed pre-registration before the one official run.

Modes
-----
``--write-preregistration``
    Freeze the protocol, code hashes, config snapshot and local QFQ data
    manifest.  Writes ``yearline_ma20_holdout_preregistration.json`` and
    ``.seal``.  Do this before any official outcome run.
``--official``
    Verify the seal, code/config/data hashes, then run the three holdout
    windows exactly once.  Refuses to overwrite an existing report.
``--smoke-window 2024H2``
    Development-only parity smoke on a frozen sample.  It never writes the
    official report and is not part of the holdout.

Variant
-------
Entry: the shipped ``yearline_pullback`` mask, plus signal-day close strictly
above the stock's 20-day simple moving average.  Exits are the production
defaults (8% stop, 30% take-profit, 40-bar timeout, T+1, fees, slippage and
price limits).  Entries are only in the holdout window; exits may run through
the local data cutoff.  The market gate is off so the comparison isolates the
stock-level MA20 rule.

This is a time holdout for this variant, not a globally blind project holdout:
the 2025-2026 period has been used by other research tracks.  The sealed
2026-09+ MACD-near holdout remains untouched.  No production config is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import strategy_effectiveness_report as eff  # noqa: E402
from backtest_winrate import HISTORY_DIR, prepare_closed_bars  # noqa: E402
from research_backtest import _config_hash, _snapshot_config, replay  # noqa: E402
from strategy.yearline import add_yearline_indicators  # noqa: E402

INDEX_DATA = BASE / "cache" / "index_000001_sh.pkl"
DEFAULT_OUT = BASE / "output" / "backtests" / "yearline-ma20-holdout-2025-2026"
DEFAULT_PREREG = DEFAULT_OUT / "yearline_ma20_holdout_preregistration.json"
SNAPSHOT_DIR = DEFAULT_OUT / "inputs"
DATA_CUTOFF = "2026-09-01"
INITIAL_CASH = 250_000.0
MAX_POSITIONS = 4
STRATEGY = "yearline_pullback"
VARIANT = "yearline_pullback + MA20(20)"
PREREG_VERSION = "yearline_ma20_holdout.v1"

HOLDOUT_WINDOWS: tuple[dict[str, str], ...] = (
    {"name": "2025H1", "start": "2025-01-01", "end": "2025-06-30"},
    {"name": "2025H2", "start": "2025-07-01", "end": "2025-12-31"},
    {"name": "2026H1", "start": "2026-01-01", "end": "2026-06-30"},
)

SAMPLE_GATES = {
    "min_variant_candidates": 30,
    "min_variant_symbols": 10,
    "min_variant_signal_days": 10,
    "counting": "unique symbol|signal_day across the three holdout windows",
}
PRIMARY_ENDPOINT = {
    "metric": "per-window portfolio total_return_pct, variant minus baseline",
    "min_positive_windows": 2,
    "median_delta_return_pct_gt": 0.0,
    "median_delta_max_drawdown_pct_lte": 2.0,
    "note": "max drawdown delta is variant minus baseline; positive = worse",
}
SECONDARY_ENDPOINT = {
    "metric": "per-window average closed-trade PnL pct, variant minus baseline",
    "min_positive_windows": 2,
    "note": "reported but only required for strong_pass",
}
CODE_FILES = (
    "yearline_ma20_holdout.py",
    "strategy/yearline.py",
    "strategy/registry.py",
    "research_backtest.py",
    "backtest_winrate.py",
    "holdout_integrity.py",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _num(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _production_config() -> dict[str, Any]:
    return eff.production_config()


def _config_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    return _snapshot_config(config)


def _config_fingerprint(config: dict[str, Any]) -> str:
    return _config_hash(_config_snapshot(config))


def _build_snapshot(snapshot_dir: Path = SNAPSHOT_DIR) -> dict[str, Any]:
    """Freeze QFQ histories truncated to the holdout data cutoff.

    The live cache may be updated by another process; the holdout must consume
    one immutable snapshot.  Files are read atomically by pandas; a transient
    read failure is retried and then reported, never silently treated as empty.
    """
    cutoff = pd.Timestamp(DATA_CUTOFF)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    failures: list[dict[str, str]] = []
    for path in sorted(HISTORY_DIR.glob("*_qfq.pkl")):
        symbol = path.name.split("_")[0].zfill(6)
        raw = None
        last_error = ""
        for _ in range(3):
            try:
                raw = pd.read_pickle(path)
                break
            except Exception as exc:  # noqa: BLE001 - transient writer race
                last_error = f"{type(exc).__name__}: {exc}"
                time_sleep(0.5)
        if raw is None or raw.empty:
            failures.append({"symbol": symbol, "reason": last_error or "empty"})
            continue
        frame = raw.copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
        frame = frame.dropna(subset=["datetime"])
        frame = frame[frame["datetime"] <= cutoff]
        if frame.empty:
            failures.append({"symbol": symbol, "reason": "no rows <= data cutoff"})
            continue
        frame = (
            frame.sort_values("datetime", kind="mergesort")
            .drop_duplicates("datetime", keep="last")
            .reset_index(drop=True)
        )
        frame.to_pickle(snapshot_dir / f"{symbol}_qfq.pkl")
        written += 1
    meta = {
        "source": str(HISTORY_DIR),
        "snapshot": str(snapshot_dir),
        "data_cutoff": DATA_CUTOFF,
        "file_count": written,
        "failures": failures,
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (snapshot_dir / "snapshot_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def time_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _data_manifest() -> dict[str, Any]:
    paths = sorted(SNAPSHOT_DIR.glob("*_qfq.pkl"))
    rows = [f"{path.name.split('_')[0].zfill(6)}|{_file_sha256(path)}" for path in paths]
    return {
        "history_dir": str(SNAPSHOT_DIR),
        "file_count": len(rows),
        "manifest_sha256": _sha256_text("\n".join(rows)),
    }


def _code_hashes() -> dict[str, str]:
    return {
        name: _file_sha256(BASE / name)
        for name in CODE_FILES
        if (BASE / name).exists()
    }


def _protocol() -> dict[str, Any]:
    config = _production_config()
    return {
        "version": PREREG_VERSION,
        "label": "yearline-pullback-ma20-holdout-2025-2026",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hypothesis": (
            "Adding 'signal-day close > MA20(20)' to yearline_pullback improves "
            "the portfolio outcome without materially worsening drawdown."
        ),
        "variant": VARIANT,
        "strategy": STRATEGY,
        "holdout_windows": list(HOLDOUT_WINDOWS),
        "universe": {
            "rule": (
                "frozen snapshot of cache/daily_history/*_qfq.pkl truncated to "
                "the data cutoff; symbols with at least 270 closed bars before "
                "the window start and data through the window end; no "
                "price/outcome-based selection"
            ),
            "data_cutoff": DATA_CUTOFF,
            "survivorship_bias": True,
        },
        "execution": {
            "entries": "signal-day close confirmed; next bar open; only inside the holdout window",
            "exits": f"through local data cutoff {DATA_CUTOFF}",
            "stop_loss_pct": 0.08,
            "take_profit_pct": 0.30,
            "max_holding_bars": 40,
            "t_plus_one": True,
            "fees_slippage_price_limits": True,
            "initial_cash": INITIAL_CASH,
            "max_positions": MAX_POSITIONS,
            "position_size": "25% of equity per position",
            "market_gate": "off (isolates the stock-level MA20 rule)",
        },
        "sample_gates": SAMPLE_GATES,
        "primary_endpoint": PRIMARY_ENDPOINT,
        "secondary_endpoint": SECONDARY_ENDPOINT,
        "decision_rule": {
            "insufficient_sample": "sample gates not met; no return conclusion",
            "pass_primary": (
                "sample gates met AND >=2/3 windows delta return > 0 AND "
                "median delta return > 0 AND median delta max drawdown <= 2.0pp"
            ),
            "pass_strong": "pass_primary AND >=2/3 windows avg trade PnL delta > 0",
            "fail": "otherwise",
            "production_action": "none; any pass only authorizes a separate production proposal",
        },
        "data": _data_manifest(),
        "config": {
            "file": str(BASE / "config" / "config.yaml"),
            "file_sha256": _file_sha256(BASE / "config" / "config.yaml"),
            "snapshot": _config_snapshot(config),
            "snapshot_sha256": _config_fingerprint(config),
        },
        "code_hashes": _code_hashes(),
        "caveat": (
            "This is a time holdout for this variant, not a globally blind project "
            "holdout: 2025-2026 has been used by other research tracks. The sealed "
            "2026-09+ MACD-near holdout remains untouched."
        ),
        "seal": None,
    }


def _compute_seal(payload: dict[str, Any]) -> str:
    clean = dict(payload)
    clean["seal"] = None
    return _sha256_text(_canonical_json(clean))


def _verify_preregistration(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"pre-registration not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    seal_path = path.with_suffix(".seal")
    if not seal_path.exists():
        raise SystemExit(f"pre-registration seal not found: {seal_path}")
    external = seal_path.read_text(encoding="utf-8").strip()
    computed = _compute_seal(payload)
    if not external or external != computed or payload.get("seal") != computed:
        raise SystemExit("pre-registration seal mismatch; refusing to run")
    if payload.get("version") != PREREG_VERSION:
        raise SystemExit("pre-registration version mismatch")
    if payload.get("code_hashes") != _code_hashes():
        raise SystemExit("code hash mismatch; the frozen rules changed")
    if payload.get("data") != _data_manifest():
        raise SystemExit("data manifest mismatch; the frozen history cache changed")
    config = _production_config()
    if payload.get("config", {}).get("snapshot_sha256") != _config_fingerprint(config):
        raise SystemExit("config snapshot mismatch; the frozen rules changed")
    return payload


def _write_preregistration(path: Path) -> dict[str, Any]:
    payload = _protocol()
    seal = _compute_seal(payload)
    payload["seal"] = seal
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    path.with_suffix(".seal").write_text(seal, encoding="utf-8")
    return {"manifest": str(path), "seal": seal, "data": payload["data"]}


def _yearline_indices(enriched: pd.DataFrame) -> np.ndarray:
    mask = (
        enriched["ma250_up"].fillna(False).astype(bool)
        & enriched["ma_align"].fillna(False).astype(bool)
        & enriched["above250_prev"].fillna(False).astype(bool)
        & enriched["low_in_zone"].fillna(False).astype(bool)
        & enriched["close_hold"].fillna(False).astype(bool)
        & enriched["close_ge_open"].fillna(False).astype(bool)
    )
    return np.flatnonzero(mask.to_numpy(dtype=bool))


def _prepare_symbol(
    raw: pd.DataFrame,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, list[str], np.ndarray] | None:
    if raw is None or raw.empty:
        return None
    enriched = add_yearline_indicators(raw)
    if enriched.empty or len(enriched) < 270:
        return None
    enriched = enriched.reset_index(drop=True)
    indices = _yearline_indices(enriched)
    frame = enriched[["datetime", "open", "high", "low", "close", "volume"]].copy()
    frame = frame.reset_index(drop=True)
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    ma20 = pd.Series(close).rolling(20, min_periods=20).mean().to_numpy(dtype=float)
    dates = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d").tolist()
    return frame, close, ma20, dates, indices


def _actionable(dates: list[str], index: int, start: str, end: str) -> bool:
    return index + 1 < len(dates) and start <= dates[index] <= end and dates[index + 1] <= end


def _window_signals(
    indicators: dict[str, tuple[np.ndarray, np.ndarray, list[str], np.ndarray]],
    start: str,
    end: str,
) -> tuple[dict[str, set[int]], dict[str, set[int]], dict[str, Any]]:
    """Return (baseline sets, variant sets, variant sample details)."""
    baseline: dict[str, set[int]] = {}
    variant: dict[str, set[int]] = {}
    symbols: set[str] = set()
    days: set[str] = set()
    candidate_ids: set[str] = set()
    for symbol, (close, ma20, dates, indices) in indicators.items():
        base: set[int] = set()
        above: set[int] = set()
        for index in indices:
            index = int(index)
            if not _actionable(dates, index, start, end):
                continue
            base.add(index)
            if (
                np.isfinite(ma20[index])
                and float(ma20[index]) > 0
                and float(close[index]) > float(ma20[index])
            ):
                above.add(index)
                candidate_ids.add(f"{symbol}|{dates[index]}")
                symbols.add(symbol)
                days.add(dates[index])
        baseline[symbol] = base
        variant[symbol] = above
    return baseline, variant, {
        "candidate_ids": sorted(candidate_ids),
        "symbols": sorted(symbols),
        "signal_days": sorted(days),
        "candidates": len(candidate_ids),
        "symbol_count": len(symbols),
        "signal_day_count": len(days),
    }


def _load_cache_histories() -> tuple[
    dict[str, pd.DataFrame],
    dict[str, tuple[np.ndarray, np.ndarray, list[str], np.ndarray]],
]:
    histories: dict[str, pd.DataFrame] = {}
    indicators: dict[str, tuple[np.ndarray, np.ndarray, list[str], np.ndarray]] = {}
    for path in sorted(SNAPSHOT_DIR.glob("*_qfq.pkl")):
        symbol = path.name.split("_")[0].zfill(6)
        try:
            raw = pd.read_pickle(path)
        except Exception:
            continue
        prepared = _prepare_symbol(raw)
        if prepared is None:
            continue
        frame, close, ma20, dates, indices = prepared
        histories[symbol] = frame
        indicators[symbol] = (close, ma20, dates, indices)
    return histories, indicators


def _arm_metrics(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") or {}
    trades = result.get("trades") or []
    trade_returns: list[float] = []
    for trade in trades:
        basis = float(trade.get("entry_price") or 0.0) * int(trade.get("quantity") or 0)
        if basis > 0:
            trade_returns.append(float(trade.get("pnl_cash") or 0.0) / basis * 100.0)
    open_positions = result.get("open_positions") or 0
    if isinstance(open_positions, (list, tuple, set, dict)):
        open_count = len(open_positions)
    else:
        open_count = int(open_positions or 0)
    return {
        "closed_trades": int(metrics.get("closed_trades") or 0),
        "total_return_pct": _num(metrics.get("total_return_pct")),
        "annualized_return_pct": _num(metrics.get("annualized_return_pct")),
        "max_drawdown_pct": _num(metrics.get("max_drawdown_pct")),
        "sharpe_ratio": _num(metrics.get("sharpe_ratio"), 3),
        "win_rate_pct": _num(metrics.get("win_rate_pct")),
        "payoff_ratio": _num(metrics.get("payoff_ratio"), 3),
        "avg_trade_pnl_pct": _num(float(np.mean(trade_returns))) if trade_returns else None,
        "open_positions": open_count,
    }


def _run_window(
    histories: dict[str, pd.DataFrame],
    indicators: dict[str, tuple[np.ndarray, np.ndarray, list[str], np.ndarray]],
    calendar_all: list[str],
    config: dict[str, Any],
    window: dict[str, str],
) -> dict[str, Any]:
    start, end = window["start"], window["end"]
    baseline, variant, variant_sample = _window_signals(indicators, start, end)
    included = [
        symbol
        for symbol, frame in histories.items()
        if len(frame) >= 270
        and str(pd.Timestamp(frame["datetime"].iloc[0]).date()) <= start
        and str(pd.Timestamp(frame["datetime"].iloc[-1]).date()) >= end
    ]
    calendar = [day for day in calendar_all if start <= day <= DATA_CUTOFF]
    options = {
        "start": start,
        "end": DATA_CUTOFF,
        "max_positions": MAX_POSITIONS,
        "initial_cash": INITIAL_CASH,
        "history_bars": 300,
    }
    rows: dict[str, Any] = {}
    for arm, sets in (("baseline", baseline), ("ma20", variant)):
        signals = {symbol: {STRATEGY: sets[symbol]} for symbol in histories}
        result = replay(histories, signals, calendar, config, options, STRATEGY)
        rows[arm] = _arm_metrics(result)
    baseline_row, variant_row = rows["baseline"], rows["ma20"]
    return {
        "window": window["name"],
        "start": start,
        "end": end,
        "included_symbols": len(included),
        "calendar_days": len(calendar),
        "variant_sample": variant_sample,
        "baseline_candidates": sum(len(values) for values in baseline.values()),
        "variant_candidates": sum(len(values) for values in variant.values()),
        "baseline": baseline_row,
        "ma20": variant_row,
        "delta_return_pct": _num(
            (variant_row["total_return_pct"] or 0.0)
            - (baseline_row["total_return_pct"] or 0.0)
        ),
        "delta_max_drawdown_pct": _num(
            (variant_row["max_drawdown_pct"] or 0.0)
            - (baseline_row["max_drawdown_pct"] or 0.0)
        ),
        "delta_sharpe": _num(
            (variant_row["sharpe_ratio"] or 0.0)
            - (baseline_row["sharpe_ratio"] or 0.0),
            3,
        ),
        "delta_avg_trade_pnl_pct": _num(
            (variant_row["avg_trade_pnl_pct"] or 0.0)
            - (baseline_row["avg_trade_pnl_pct"] or 0.0)
        ),
    }


def _decide(
    window_rows: list[dict[str, Any]],
    sample: dict[str, Any],
) -> dict[str, Any]:
    gates = SAMPLE_GATES
    sample_ok = (
        sample["candidates"] >= gates["min_variant_candidates"]
        and sample["symbol_count"] >= gates["min_variant_symbols"]
        and sample["signal_day_count"] >= gates["min_variant_signal_days"]
    )
    if not sample_ok:
        return {
            "decision": "insufficient_sample",
            "sample_gate_passed": False,
            "reason": "sample gates not met; do not interpret returns",
            "production_eligible": False,
            "production_action": "none; a pass only authorizes a separate production proposal",
        }
    deltas = [float(row["delta_return_pct"] or 0.0) for row in window_rows]
    drawdowns = [float(row["delta_max_drawdown_pct"] or 0.0) for row in window_rows]
    trade_deltas = [
        float(row["delta_avg_trade_pnl_pct"] or 0.0) for row in window_rows
    ]
    positive = sum(1 for value in deltas if value > 0)
    primary = (
        positive >= PRIMARY_ENDPOINT["min_positive_windows"]
        and float(np.median(deltas)) > PRIMARY_ENDPOINT["median_delta_return_pct_gt"]
        and float(np.median(drawdowns))
        <= PRIMARY_ENDPOINT["median_delta_max_drawdown_pct_lte"]
    )
    strong = (
        primary
        and sum(1 for value in trade_deltas if value > 0)
        >= SECONDARY_ENDPOINT["min_positive_windows"]
    )
    return {
        "decision": "pass_strong" if strong else "pass_primary" if primary else "fail",
        "sample_gate_passed": True,
        "positive_windows": positive,
        "median_delta_return_pct": _num(float(np.median(deltas))),
        "median_delta_max_drawdown_pct": _num(float(np.median(drawdowns))),
        "positive_avg_trade_windows": sum(1 for value in trade_deltas if value > 0),
        "median_delta_avg_trade_pnl_pct": _num(float(np.median(trade_deltas))),
        "production_eligible": False,
        "production_action": "none; a pass only authorizes a separate production proposal",
    }


def _markdown(
    pre: dict[str, Any],
    window_rows: list[dict[str, Any]],
    sample: dict[str, Any],
    decision: dict[str, Any],
    run_meta: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# 年线趋势 + MA20 独立 holdout 复核")
    lines.append("")
    lines.append(f"运行时间：{run_meta['run_at_utc']}")
    lines.append("")
    lines.append("## 预注册")
    lines.append("")
    lines.append(f"- 预注册：`{pre.get('label')}`，seal `{run_meta['preregistration_seal']}`")
    lines.append(f"- 变体：`{VARIANT}`：年线回踩信号 + 信号日收盘价严格高于该股 MA20(20)。")
    lines.append("- 执行：8% 止损、30% 止盈、40 根超时、T+1、费用/滑点/涨跌停；25 万、最多 4 仓、单仓 25%；市场闸门关闭。")
    lines.append(f"- 窗口：{', '.join(window['name'] for window in HOLDOUT_WINDOWS)}；只在窗口内开仓，退出可运行到数据截止日 {DATA_CUTOFF}。")
    lines.append("- 这是本变体的时间外 holdout；2025-2026 被其他研究线使用过，因此不是全项目盲测。2026-09+ 的 MACD-near sealed holdout 仍未触碰。")
    lines.append("")
    lines.append("## 样本门")
    lines.append("")
    lines.append("| 指标 | 实际 | 要求 |")
    lines.append("|---|---:|---:|")
    lines.append(f"| 唯一候选 symbol|signal_day | {sample['candidates']} | ≥{SAMPLE_GATES['min_variant_candidates']} |")
    lines.append(f"| 唯一股票 | {sample['symbol_count']} | ≥{SAMPLE_GATES['min_variant_symbols']} |")
    lines.append(f"| 唯一信号日 | {sample['signal_day_count']} | ≥{SAMPLE_GATES['min_variant_signal_days']} |")
    lines.append("")
    lines.append("## 逐窗口结果")
    lines.append("")
    lines.append("| 窗口 | 入选股票 | 候选 基线→MA20 | 成交 基线→MA20 | 收益% 基线→MA20 | Δpp | 回撤% 基线→MA20 | 单笔均收益% 基线→MA20 |")
    lines.append("|---|---:|---:|---:|---|---:|---|---|")
    for row in window_rows:
        b, m = row["baseline"], row["ma20"]
        lines.append(
            f"| {row['window']} | {row['included_symbols']} | "
            f"{row['baseline_candidates']} → {row['variant_candidates']} | "
            f"{b['closed_trades']} → {m['closed_trades']} | "
            f"{b['total_return_pct']} → {m['total_return_pct']} | "
            f"{row['delta_return_pct']:+.2f} | "
            f"{b['max_drawdown_pct']} → {m['max_drawdown_pct']} | "
            f"{b['avg_trade_pnl_pct']} → {m['avg_trade_pnl_pct']} |"
        )
    lines.append("")
    lines.append("## 预注册判决")
    lines.append("")
    lines.append(f"- 判决：`{decision['decision']}`")
    lines.append(f"- 样本门通过：`{decision['sample_gate_passed']}`")
    if decision.get("positive_windows") is not None:
        lines.append(f"- 收益改善窗口：{decision['positive_windows']}/3；收益Δ中位数 {decision['median_delta_return_pct']}pp；回撤Δ中位数 {decision['median_delta_max_drawdown_pct']}pp。")
        lines.append(f"- 单笔均收益改善窗口：{decision['positive_avg_trade_windows']}/3；单笔均收益Δ中位数 {decision['median_delta_avg_trade_pnl_pct']}pp。")
    lines.append(f"- 生产动作：{decision['production_action']}")
    lines.append("")
    lines.append("## 口径与限制")
    lines.append("")
    lines.append("- 股票池为本地 QFQ 缓存中的当前上市股票，存在幸存者偏差；不是 PIT 成分股。")
    lines.append("- 2025-2026 已被其他研究线使用；本报告只把这段时期作为 **本变体** 的时间外 holdout，不宣称全项目盲测。")
    lines.append("- 每窗口交易数有限；判决是预注册的描述性门槛，不是统计显著性检验。")
    lines.append("- 不修改生产 `config.yaml`；即使通过，也只授权另行提出生产方案，不直接上线。")
    lines.append("")
    return "\n".join(lines)


def _smoke(window_name: str, out_dir: Path) -> dict[str, Any]:
    """Development parity smoke on a frozen sample; never writes the official report."""
    window = next((item for item in eff.WINDOWS if item["name"] == window_name), None)
    if window is None:
        raise SystemExit(f"unknown smoke window: {window_name}")
    raw_histories, _signals, calendar, _metadata, _coverage = eff.load_sample(
        eff.BASE / window["root"]
    )
    histories: dict[str, pd.DataFrame] = {}
    indicators: dict[str, tuple[np.ndarray, np.ndarray, list[str], np.ndarray]] = {}
    for symbol, frame in raw_histories.items():
        prepared = _prepare_symbol(frame)
        if prepared is None:
            continue
        slim, close, ma20, dates, indices = prepared
        histories[symbol] = slim
        indicators[symbol] = (close, ma20, dates, indices)
    config = _production_config()
    start, end = window["start"], window["end"]
    baseline, variant, stats = _window_signals(indicators, start, end)
    options = {
        "start": start,
        "end": end,
        "max_positions": MAX_POSITIONS,
        "initial_cash": INITIAL_CASH,
        "history_bars": 300,
    }
    rows: dict[str, Any] = {}
    for arm, sets in (("baseline", baseline), ("ma20", variant)):
        signals = {symbol: {STRATEGY: sets[symbol]} for symbol in histories}
        result = replay(histories, signals, calendar, config, options, STRATEGY)
        rows[arm] = _arm_metrics(result)
    payload = {
        "window": window_name,
        "variant_sample": stats,
        "baseline_candidates": sum(len(values) for values in baseline.values()),
        "variant_candidates": sum(len(values) for values in variant.values()),
        "baseline": rows["baseline"],
        "ma20": rows["ma20"],
        "delta_return_pct": _num(
            (rows["ma20"]["total_return_pct"] or 0.0)
            - (rows["baseline"]["total_return_pct"] or 0.0)
        ),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "smoke.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-snapshot", action="store_true")
    parser.add_argument("--write-preregistration", action="store_true")
    parser.add_argument("--official", action="store_true")
    parser.add_argument("--preregistration", type=Path, default=DEFAULT_PREREG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--smoke-window", default=None)
    args = parser.parse_args()

    if args.smoke_window:
        result = _smoke(args.smoke_window, Path("D:/tmp/yearline-ma20-smoke"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.build_snapshot:
        summary = _build_snapshot()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.write_preregistration:
        summary = _write_preregistration(args.preregistration)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if not args.official:
        parser.error("choose --write-preregistration, --official, or --smoke-window")

    prereg = _verify_preregistration(args.preregistration)
    report_path = args.out / "report.json"
    if report_path.exists():
        raise SystemExit(f"official holdout report already exists: {report_path}")
    config = _production_config()
    histories, indicators = _load_cache_histories()
    if not histories:
        raise SystemExit("no QFQ histories found")
    index_frame = pd.read_pickle(INDEX_DATA)
    index_closed = prepare_closed_bars(index_frame)
    calendar_all = [
        str(pd.Timestamp(value).date()) for value in index_closed["datetime"]
    ]
    if not calendar_all or calendar_all[-1] < DATA_CUTOFF:
        raise SystemExit(
            f"index calendar ends {calendar_all[-1] if calendar_all else 'empty'}, "
            f"before the frozen data cutoff {DATA_CUTOFF}"
        )
    window_rows: list[dict[str, Any]] = []
    for window in HOLDOUT_WINDOWS:
        window_rows.append(
            _run_window(histories, indicators, calendar_all, config, window)
        )
        print(f"finished {window['name']}", flush=True)
    candidate_union: set[str] = set()
    symbol_union: set[str] = set()
    day_union: set[str] = set()
    for row in window_rows:
        candidate_union.update(row["variant_sample"]["candidate_ids"])
        symbol_union.update(row["variant_sample"]["symbols"])
        day_union.update(row["variant_sample"]["signal_days"])
    sample = {
        "candidates": len(candidate_union),
        "symbol_count": len(symbol_union),
        "signal_day_count": len(day_union),
    }
    decision = _decide(window_rows, sample)
    run_meta = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preregistration_seal": prereg["seal"],
        "data_manifest_sha256": prereg["data"]["manifest_sha256"],
        "config_snapshot_sha256": prereg["config"]["snapshot_sha256"],
        "code_hashes": prereg["code_hashes"],
        "holdout_used": True,
        "production_database_connected": False,
        "production_config_written": False,
    }
    payload = {
        "kind": "yearline_ma20_holdout_report",
        "version": PREREG_VERSION,
        "preregistration": prereg,
        "run": run_meta,
        "windows": window_rows,
        "sample": sample,
        "decision": decision,
        "production_eligible": False,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (args.out / "report.md").write_text(
        _markdown(prereg, window_rows, sample, decision, run_meta),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"output_dir": str(args.out), "decision": decision},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
