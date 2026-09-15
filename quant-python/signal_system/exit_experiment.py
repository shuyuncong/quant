"""Run fixed-entry, single-variable exit experiments on frozen candidates.

This research runner deliberately starts from the already frozen candidate
JSONL files.  It does not regenerate or filter entries, so baseline and
variants differ only in the requested exit policy.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from attribution_audit import FIELDS, exit_reason_category, group_stats, judge, stats
from candidate_integrity import normalize_candidate_rows
from backtest_winrate import (
    HISTORY_DIR,
    _execution_values,
    _resolve_execution_config,
    _sell_events_by_index,
    find_signals,
    next_bar_index,
    prepare_closed_bars,
    simulate_single_trade,
)
from utils.helpers import load_config


DEFAULT_INPUT_DIR = Path(r"D:\tmp\candidates_exit")
DEFAULT_OUTPUT_DIR = Path(r"D:\tmp\exit_experiments")
DEFAULT_CONFIG = BASE_DIR / "config" / "config.yaml"
DEFAULT_INDEX_DATA = BASE_DIR / "cache" / "index_000001_sh.pkl"
LOW_OPEN_RESEARCH_VARIANTS = (
    "fixed_sl5",
    "regime_sl5_sl8",
    "weak_market_low_open",
)


def _guard_development_path(path: Path, allow_holdout: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if not allow_holdout and any("holdout" in part.lower() for part in resolved.parts):
        raise ValueError(
            f"Holdout path is blocked for development experiments: {resolved}. "
            "Use only after the experiment is frozen and separately authorized."
        )
    return resolved


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _variant_config(base_config: dict[str, Any], variant: str) -> dict[str, Any]:
    config = copy.deepcopy(base_config)
    backtest = config.setdefault("backtest", {})
    chan = backtest.setdefault("chan_zero_axis", {})
    chan["zero_axis_exit_confirmation_bars"] = 1
    chan["timeout_exit_mode"] = "fixed"
    backtest["profit_protection"] = {"mode": "none"}
    backtest["stop_loss_policy"] = {"mode": "fixed"}
    chan.pop("timeout_hard_cap_bars", None)
    chan.pop("timeout_ma_period", None)
    chan.pop("timeout_ma_confirm_bars", None)
    if variant == "zero_axis_confirm_2":
        chan["zero_axis_exit_confirmation_bars"] = 2
    elif variant == "timeout_ma_break":
        chan["timeout_exit_mode"] = "ma_break"
        chan["timeout_ma_period"] = 20
        chan["timeout_ma_confirm_bars"] = 1
        chan["timeout_hard_cap_bars"] = 60
    elif variant == "mfe_profit_lock":
        backtest["profit_protection"] = {
            "mode": "mfe_lock",
            "activation_pct": 0.08,
            "lock_pct": 0.02,
        }
    elif variant == "atr_trailing":
        backtest["profit_protection"] = {
            "mode": "atr_trailing",
            "activation_pct": 0.08,
            "floor_pct": 0.0,
            "atr_period": 14,
            "atr_multiple": 2.5,
        }
    elif variant == "fixed_sl5":
        config.setdefault("risk", {})["stop_loss_pct"] = 0.05
    elif variant == "regime_sl5_sl8":
        backtest["stop_loss_policy"] = {
            "mode": "market_regime",
            "weak_market_stop_loss_pct": 0.05,
        }
    elif variant == "weak_market_low_open":
        backtest["stop_loss_policy"] = {
            "mode": "fixed",
            "weak_market_low_open_exit_pct": 0.015,
        }
    elif variant != "baseline":
        raise ValueError(f"unknown experiment variant: {variant}")
    return config


def _frozen_candidate_costs(config: dict[str, Any]) -> dict[str, Any]:
    """Reproduce the execution semantics of the frozen attribution dataset.

    The source candidate files contain no risk stop/take-profit exits.  Keep
    that frozen baseline while nesting the resolved research timeout fields
    so simulate_single_trade does not lose the variant on its own resolver.
    """
    resolved = _execution_values(config.get("backtest", {}))
    costs = dict(resolved)
    costs["chan_zero_axis"] = {
        key: resolved[key]
        for key in (
            "max_holding_bars",
            "timeout_exit_mode",
            "timeout_ma_period",
            "timeout_ma_confirm_bars",
            "timeout_hard_cap_bars",
        )
    }
    costs["profit_protection"] = dict(
        config.get("backtest", {}).get("profit_protection", {}) or {}
    )
    costs["stop_loss_policy"] = dict(
        config.get("backtest", {}).get("stop_loss_policy", {}) or {}
    )
    return costs


def _load_market_weak_by_day(
    path: Path,
    *,
    ma_period: int = 20,
) -> tuple[dict[str, bool], dict[str, Any]]:
    """Build target-day states using only the preceding index trading day."""
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"index data does not exist: {resolved}")
    frame = prepare_closed_bars(pd.read_pickle(resolved))
    if frame.empty:
        raise ValueError(f"index data is empty: {resolved}")
    frame = frame.sort_values("datetime", kind="mergesort").reset_index(drop=True)
    target_days = [pd.Timestamp(value).date() for value in frame["datetime"]]
    if len(target_days) != len(set(target_days)):
        raise ValueError("index data contains duplicate trading days")
    closes = pd.to_numeric(frame["close"], errors="coerce").astype(float)
    previous_close = closes.shift(1)
    previous_ma = closes.rolling(ma_period, min_periods=ma_period).mean().shift(1)
    mapping = {
        target_days[index].isoformat(): bool(
            previous_close.iloc[index] < previous_ma.iloc[index]
        )
        for index in range(len(frame))
        if pd.notna(previous_close.iloc[index]) and pd.notna(previous_ma.iloc[index])
    }
    weak_days = sum(mapping.values())
    return mapping, {
        "path": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        "definition": (
            f"target trading day t is weak iff index close(t-1) < MA{ma_period}(t-1); "
            "the current-day index bar is never used"
        ),
        "ma_period": ma_period,
        "index_rows": len(frame),
        "mapped_target_days": len(mapping),
        "weak_target_days": weak_days,
        "strong_target_days": len(mapping) - weak_days,
        "first_mapped_target_day": min(mapping) if mapping else None,
        "last_mapped_target_day": max(mapping) if mapping else None,
        "all_cutoffs_before_target_day": True,
    }


def _audit(rows: list[dict[str, Any]], label: str, skipped: Counter) -> dict[str, Any]:
    common_window_rows = [
        {**row, "mfe": row.get("mfe_common_60"), "mae": row.get("mae_common_60")}
        for row in rows
    ]
    common_efficiency = [
        row["trade_pnl_pct"] / row["mfe_common_60"]
        for row in rows
        if row.get("mfe_common_60") is not None
        and row.get("mfe_common_60", 0) > 0
        and row.get("trade_pnl_pct") is not None
    ]
    result = {
        "version": "exit_experiment.v3",
        "label": label,
        "n": len(rows),
        "overall": {field: stats([row.get(field) for row in rows]) for field in FIELDS},
        "held_window_exit_efficiency": stats(
            [
                row["trade_pnl_pct"] / row["mfe"]
                for row in rows
                if row.get("mfe") is not None
                and row.get("mfe", 0) > 0
                and row.get("trade_pnl_pct") is not None
            ]
        ),
        "common_60d_window": {
            "mfe": stats([row.get("mfe_common_60") for row in rows]),
            "mae": stats([row.get("mae_common_60") for row in rows]),
            "exit_efficiency": stats(common_efficiency),
        },
        "by_regime": group_stats(rows, "regime"),
        "by_signal_type": group_stats(rows, "signal_type"),
        "by_exit_reason": group_stats(rows, "exit_reason"),
        "by_exit_category": group_stats(
            [
                {**row, "exit_category": exit_reason_category(row.get("exit_reason"))}
                for row in rows
            ],
            "exit_category",
        ),
        # The ordinary MFE/MAE horizon ends at each policy's own exit, so this
        # judgement is useful for diagnosis but is not a fair variant ranking.
        "held_window_judgement_exploratory": judge(rows),
        "common_60d_judgement": judge(common_window_rows),
        "exit_reason_counts": dict(Counter(row.get("exit_reason") for row in rows)),
        "skipped": dict(skipped),
    }
    return result


def _run_split(
    source_path: Path,
    config: dict[str, Any],
    label: str,
    execution_profile: str,
    market_weak_by_day: dict[str, bool] | None = None,
) -> tuple[list[dict[str, Any]], Counter, dict[str, Any]]:
    raw_source_rows = _load_jsonl(source_path)
    source_rows, integrity = normalize_candidate_rows(raw_source_rows)
    costs = (
        _frozen_candidate_costs(config)
        if execution_profile == "frozen_source"
        else _resolve_execution_config(config)
    )
    history_cache: dict[str, tuple[pd.DataFrame, list, dict[int, str]] | None] = {}
    output_rows: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    pnl_diffs: list[float] = []
    reason_matches = 0
    common_horizon_bars = 80
    policy_horizon_bars = 60
    eligible_ids: list[str] = []
    history_hashes: dict[str, str] = {}

    for source in source_rows:
        symbol = str(source.get("symbol", "")).zfill(6)
        if symbol not in history_cache:
            path = HISTORY_DIR / f"{symbol}_qfq.pkl"
            if not path.exists():
                history_cache[symbol] = None
            else:
                history_hashes[symbol] = hashlib.sha256(path.read_bytes()).hexdigest()
                closed = prepare_closed_bars(pd.read_pickle(path))
                dates = [pd.Timestamp(value).date() for value in closed["datetime"]]
                events = find_signals(closed, config)
                sells_by_index = _sell_events_by_index(events.get("sell", []), dates)
                history_cache[symbol] = (closed, dates, sells_by_index)
        cached = history_cache[symbol]
        if cached is None:
            skipped["missing_history"] += 1
            continue
        closed, dates, sells_by_index = cached
        entry_idx = next_bar_index(
            closed, dates, pd.Timestamp(source["signal_day"]).date()
        )
        if entry_idx is None or entry_idx + common_horizon_bars >= len(closed):
            # Keep paired comparisons fair: every variant must have enough
            # bars for the longest 60-bar policy plus 20 post-exit bars.
            skipped["ineligible_common_horizon"] += 1
            continue
        eligible_ids.append(f"{symbol}|{source['signal_day']}|{source.get('signal_type', '')}")
        buy = {
            key: source.get(key)
            for key in (
                "day",
                "signal_type",
                "side",
                "price",
                "confirmed_at",
                "cross_day",
                "confirmation_bars",
                "confirmation_count",
                "confirmation_items",
                "_p5a_features",
                "_p5b_features",
                "stock_pool_metrics",
                "stock_pool_warnings",
                "fundamental_status",
                "fundamental_metrics",
                "fundamental_warnings",
            )
        }
        buy["day"] = source["signal_day"]
        buy["side"] = "buy"
        trade, reason = simulate_single_trade(
            symbol,
            closed,
            dates,
            buy,
            sells_by_index,
            costs,
            allow_incomplete=False,
            market_context=None,
            market_weak_by_day=market_weak_by_day,
        )
        if trade is None:
            skipped[str(reason or "unknown")] += 1
            continue
        trade_entry_idx = dates.index(pd.Timestamp(trade["entry_day"]).date())
        trade_exit_idx = dates.index(pd.Timestamp(trade["exit_day"]).date())
        entry_price = float(trade["entry_price"])
        future = {}
        for horizon in (5, 20, 40):
            probe = trade_entry_idx + horizon
            future[f"future_{horizon}d"] = (
                round((float(closed.iloc[probe]["close"]) / entry_price - 1.0) * 100.0, 3)
                if probe < len(closed)
                else None
            )
        highs = pd.to_numeric(
            closed.iloc[trade_entry_idx : trade_exit_idx + 1]["high"], errors="coerce"
        )
        lows = pd.to_numeric(
            closed.iloc[trade_entry_idx : trade_exit_idx + 1]["low"], errors="coerce"
        )
        trade["mfe"] = round((float(highs.max()) / entry_price - 1.0) * 100.0, 3)
        trade["mae"] = round((float(lows.min()) / entry_price - 1.0) * 100.0, 3)
        common_end = min(trade_entry_idx + policy_horizon_bars, len(closed) - 1)
        common_highs = pd.to_numeric(
            closed.iloc[trade_entry_idx : common_end + 1]["high"], errors="coerce"
        )
        common_lows = pd.to_numeric(
            closed.iloc[trade_entry_idx : common_end + 1]["low"], errors="coerce"
        )
        trade["mfe_common_60"] = round(
            (float(common_highs.max()) / entry_price - 1.0) * 100.0, 3
        )
        trade["mae_common_60"] = round(
            (float(common_lows.min()) / entry_price - 1.0) * 100.0, 3
        )
        trade.update(future)
        trade["trade_pnl_pct"] = trade["pnl_pct"]
        for horizon in (5, 20):
            probe = trade_exit_idx + horizon
            trade[f"post_exit_{horizon}d"] = (
                round(
                    (float(closed.iloc[probe]["close"]) / float(trade["exit_price"]) - 1.0)
                    * 100.0,
                    3,
                )
                if probe < len(closed)
                else None
            )
        trade["regime"] = source.get("regime", "unknown")
        trade["market_cap"] = source.get("market_cap")
        trade["source_exit_reason"] = source.get("exit_reason")
        trade["source_trade_pnl_pct"] = source.get("trade_pnl_pct")
        trade["experiment_label"] = label
        trade["entry_market_weak"] = (
            market_weak_by_day.get(str(trade["entry_day"]))
            if market_weak_by_day is not None
            else None
        )
        trade["exit_market_weak"] = (
            market_weak_by_day.get(str(trade["exit_day"]))
            if market_weak_by_day is not None
            else None
        )
        previous_exit_close = (
            float(closed.iloc[trade_exit_idx - 1]["close"])
            if trade_exit_idx > 0
            else None
        )
        trade["exit_open_gap_pct"] = (
            round(
                (float(closed.iloc[trade_exit_idx]["open"]) / previous_exit_close - 1.0)
                * 100.0,
                4,
            )
            if previous_exit_close is not None and previous_exit_close > 0
            else None
        )
        trade["candidate_id"] = (
            f"{symbol}|{source['signal_day']}|{source.get('signal_type', '')}"
        )
        if source.get("trade_pnl_pct") is not None:
            pnl_diffs.append(
                abs(float(trade["pnl_pct"]) - float(source["trade_pnl_pct"]))
            )
        if trade.get("exit_reason") == source.get("exit_reason"):
            reason_matches += 1
        output_rows.append(trade)

    match = {
        "source_rows": len(raw_source_rows),
        "normalized_source_rows": len(source_rows),
        "candidate_integrity": integrity,
        "source_candidates_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "common_eligible_rows": len(eligible_ids),
        "common_eligible_manifest_sha256": hashlib.sha256(
            "\n".join(sorted(eligible_ids)).encode("utf-8")
        ).hexdigest(),
        "history_manifest_sha256": hashlib.sha256(
            "\n".join(
                f"{symbol}|{history_hashes[symbol]}"
                for symbol in sorted(history_hashes)
            ).encode("utf-8")
        ).hexdigest(),
        "simulated_rows": len(output_rows),
        "completed_manifest_sha256": hashlib.sha256(
            "\n".join(
                sorted(str(row["candidate_id"]) for row in output_rows)
            ).encode("utf-8")
        ).hexdigest(),
        "source_pnl_abs_diff": stats(pnl_diffs),
        "exit_reason_exact_matches": reason_matches,
        "exit_reason_match_rate": (
            round(reason_matches / len(output_rows) * 100.0, 2)
            if output_rows
            else None
        ),
    }
    pnl_median = (match["source_pnl_abs_diff"] or {}).get("median")
    if label == "baseline" and execution_profile == "frozen_source":
        match["baseline_reproduction_ok"] = bool(
            len(output_rows) == len(eligible_ids)
            and (pnl_median is None or pnl_median <= 0.05)
            and (
                not output_rows
                or match["exit_reason_match_rate"] is None
                or match["exit_reason_match_rate"] >= 95.0
            )
        )
    else:
        match["baseline_reproduction_ok"] = None
    return output_rows, skipped, match


def run(args: argparse.Namespace) -> dict[str, Any]:
    allow_holdout = bool(getattr(args, "allow_holdout", False))
    config_path = _guard_development_path(Path(args.config), allow_holdout)
    base_config = load_config(str(config_path))
    if base_config is None:
        raise RuntimeError(f"unable to load config: {config_path}")
    input_dir = _guard_development_path(Path(args.input_dir), allow_holdout)
    output_dir = _guard_development_path(Path(args.output_dir), allow_holdout) / args.variant
    if args.variant in LOW_OPEN_RESEARCH_VARIANTS and args.execution_profile != "production_risk":
        raise ValueError(
            "5%-8% stop research variants require --execution-profile production_risk"
        )
    index_data_arg = getattr(args, "index_data", None)
    market_weak_by_day: dict[str, bool] | None = None
    market_regime_input: dict[str, Any] | None = None
    if index_data_arg:
        index_path = _guard_development_path(Path(index_data_arg), allow_holdout)
        market_weak_by_day, market_regime_input = _load_market_weak_by_day(index_path)
    if args.variant in {"regime_sl5_sl8", "weak_market_low_open"} and market_weak_by_day is None:
        raise ValueError(f"{args.variant} requires --index-data")
    config = _variant_config(base_config, args.variant)
    if output_dir.exists():
        raise FileExistsError(
            f"refusing to overwrite an existing experiment directory: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {
        "version": "exit_experiment.v3",
        "variant": args.variant,
        "execution_profile": args.execution_profile,
        "market_regime_input": market_regime_input,
        "input_config": {
            "path": str(config_path),
            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "resolved_variant_config_sha256": hashlib.sha256(
                json.dumps(
                    config,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        },
        "config": {
            "zero_axis_exit_confirmation_bars": config["backtest"]["chan_zero_axis"].get(
                "zero_axis_exit_confirmation_bars", 1
            ),
            "timeout_exit_mode": config["backtest"]["chan_zero_axis"].get(
                "timeout_exit_mode", "fixed"
            ),
            "timeout_ma_period": config["backtest"]["chan_zero_axis"].get(
                "timeout_ma_period", 20
            ),
            "timeout_ma_confirm_bars": config["backtest"]["chan_zero_axis"].get(
                "timeout_ma_confirm_bars", 1
            ),
            "timeout_hard_cap_bars": _execution_values(
                config.get("backtest", {})
            )["timeout_hard_cap_bars"],
            "profit_protection": dict(
                config.get("backtest", {}).get("profit_protection", {}) or {}
            ),
            "stop_loss_pct": _execution_values(
                _resolve_execution_config(config)
            )["stop_loss_pct"],
            "take_profit_pct": _execution_values(
                _resolve_execution_config(config)
            )["take_profit_pct"],
            "stop_loss_policy": dict(
                config.get("backtest", {}).get("stop_loss_policy", {}) or {}
            ),
        },
        "comparison_scope": {
            "fixed_entry_candidates": True,
            "analysis_mode": "signal_paired",
            "common_horizon_bars": 60,
            "common_tail_bars_for_post_exit_20": 20,
            "ineligible_candidates_excluded_from_all_variants": True,
            "history_adjustment": "qfq",
            "baseline_match_tolerance_pnl_pp": 0.05,
            "baseline_match_tolerance_reason_rate_pct": 95.0,
            "risk_exit_semantics": (
                "disabled_in_source_candidates"
                if args.execution_profile == "frozen_source"
                else "production_risk_with_frozen_stop_variant"
            ),
        },
        "splits": {},
    }
    for split in ("train", "val", "test"):
        source_path = input_dir / f"candidates_{split}.jsonl"
        jsonl_path = output_dir / f"candidates_{split}.jsonl"
        json_path = output_dir / f"attribution_{split}.json"
        rows, skipped, match = _run_split(
            source_path,
            config,
            args.variant,
            args.execution_profile,
            market_weak_by_day,
        )
        _write_jsonl(jsonl_path, rows)
        audit = _audit(rows, f"{args.variant}:{split}", skipped)
        audit["source_match"] = match
        with json_path.open("w", encoding="utf-8") as handle:
            json.dump(audit, handle, ensure_ascii=False, indent=1)
        result["splits"][split] = {
            "source": str(source_path),
            "candidates": str(jsonl_path),
            "audit": str(json_path),
            "n": len(rows),
            "skipped": dict(skipped),
            "source_match": match,
            "overall": audit["overall"],
            "by_exit_category": audit["by_exit_category"],
        }
    if args.variant == "baseline" and args.execution_profile == "frozen_source":
        result["baseline_reproduction_ok"] = all(
            bool(split["source_match"].get("baseline_reproduction_ok"))
            for split in result["splits"].values()
        )
    else:
        result["interpretation_gate"] = (
            "Compare only against a baseline generated with the same "
            "execution_profile and candidate/history manifests."
        )
    result_path = output_dir / "experiment.json"
    with result_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        choices=(
            "baseline",
            "zero_axis_confirm_2",
            "timeout_ma_break",
            "mfe_profit_lock",
            "atr_trailing",
            *LOW_OPEN_RESEARCH_VARIANTS,
        ),
        required=True,
    )
    parser.add_argument(
        "--execution-profile",
        choices=("frozen_source", "production_risk"),
        default="frozen_source",
    )
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument(
        "--index-data",
        default=None,
        help=(
            "local index PKL used to construct causal previous-day MA20 states; "
            f"recommended: {DEFAULT_INDEX_DATA}"
        ),
    )
    parser.add_argument(
        "--allow-holdout",
        action="store_true",
        help="explicit override; do not use during strategy development",
    )
    args = parser.parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
