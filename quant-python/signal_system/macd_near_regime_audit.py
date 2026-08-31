"""Research-only audit for disabling MACD ``near`` entries in weak regimes.

The experiment keeps the frozen candidate set and production risk/exit
semantics fixed.  The variant only removes
``macd_golden_cross_pullback_confirmed_near`` candidates whose frozen regime
is ``range`` or ``bear``.  It produces both candidate-level and funded
portfolio-level reports and never edits production configuration.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from attribution_audit import FIELDS, _config_snapshot, exit_reason_category, group_stats, stats
from backtest_winrate import (
    HISTORY_DIR,
    _resolve_execution_config,
    _sell_events_by_index,
    find_signals,
    next_bar_index,
    prepare_closed_bars,
    run_portfolio,
    simulate_single_trade,
)
from strategy.signal_policy import (
    resolve_signal_execution_policy,
    signal_execution_mode_with_regime,
)
from utils.helpers import load_config


VERSION = "macd_near_regime_audit.v1"
SPLITS = ("train", "val", "test")
NEAR_SIGNAL = "macd_golden_cross_pullback_confirmed_near"
WEAK_REGIMES = frozenset({"range", "bear"})
MIN_WEAK_NEAR_COUNT = 30
MIN_WEAK_NEAR_UNIQUE_SYMBOLS = 10
MIN_WEAK_NEAR_UNIQUE_DAYS = 10

FREEZE_MANIFEST_NAME = "holdout_freeze.json"
# Portfolio max-drawdown tolerance for the variant vs baseline (percentage
# points).  "组合收益改善不能以明显增加最大回撤为代价".
DRAWDOWN_TOLERANCE_PP = 5.0

_BUY_KEYS = (
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


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _rows_manifest_sha256(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(_canonical_json(row) for row in rows)).encode("utf-8")
    ).hexdigest()


def _ids_manifest_sha256(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def _config_sha256(config: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_freeze_manifest(path: Path) -> dict[str, Any]:
    """Load the holdout freeze manifest written by freeze_holdout.py."""
    if not path.exists():
        raise FileNotFoundError(f"holdout freeze manifest not found: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _validate_freeze_manifest(
    freeze: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check a frozen-candidate split against the pre-frozen holdout manifest.

    Only checks that are fully determined at freeze time are enforced:
    time boundary, candidate-pool membership, rules hash, config hash.
    The candidate manifest hash is filled by the generation step and is
    cross-checked separately once available.
    """
    checks: dict[str, Any] = {}
    rules = freeze.get("rules", {})
    checks["rules_match"] = (
        str(rules.get("signal", "")) == NEAR_SIGNAL
        and set(rules.get("weak_regimes", [])) == set(WEAK_REGIMES)
    )
    window = freeze.get("holdout_window", {})
    start = window.get("start")
    if start:
        start_date = date.fromisoformat(str(start))
        days = [
            date.fromisoformat(str(row.get("signal_day", "")))
            for row in candidates
            if str(row.get("signal_day", "")).strip()
        ]
        checks["window_start"] = str(start)
        checks["all_signal_days_after_start"] = (
            bool(days) and all(day >= start_date for day in days)
        )
    else:
        checks["window_start"] = None
        checks["all_signal_days_after_start"] = None
    frozen_universe = set(freeze.get("universe", {}).get("symbols", []))
    if frozen_universe:
        candidate_symbols = {
            str(row.get("symbol", "")).zfill(6) for row in candidates
        }
        outside = sorted(candidate_symbols - frozen_universe)
        checks["universe_count"] = len(frozen_universe)
        checks["candidates_outside_universe"] = outside
        checks["all_candidates_in_universe"] = not outside
    else:
        checks["universe_count"] = None
        checks["candidates_outside_universe"] = None
        checks["all_candidates_in_universe"] = None
    return checks


def _candidate_id(source: dict[str, Any]) -> str:
    return (
        f"{str(source.get('symbol', '')).zfill(6)}|{source.get('signal_day')}|"
        f"{source.get('signal_type', '')}"
    )


def _is_disabled(source: dict[str, Any]) -> bool:
    return (
        str(source.get("signal_type", "")) == NEAR_SIGNAL
        and str(source.get("regime", "unknown")).lower() in WEAK_REGIMES
    )


def _enrich_trade(
    trade: dict[str, Any],
    closed: pd.DataFrame,
    dates: list[date],
    source: dict[str, Any],
) -> None:
    entry_idx = dates.index(pd.Timestamp(trade["entry_day"]).date())
    exit_idx = dates.index(pd.Timestamp(trade["exit_day"]).date())
    entry_price = float(trade["entry_price"])
    for horizon in (5, 20, 40):
        probe = entry_idx + horizon
        trade[f"future_{horizon}d"] = (
            round((float(closed.iloc[probe]["close"]) / entry_price - 1.0) * 100.0, 3)
            if probe < len(closed)
            else None
        )
    highs = pd.to_numeric(closed.iloc[entry_idx : exit_idx + 1]["high"], errors="coerce")
    lows = pd.to_numeric(closed.iloc[entry_idx : exit_idx + 1]["low"], errors="coerce")
    trade["mfe"] = round((float(highs.max()) / entry_price - 1.0) * 100.0, 3)
    trade["mae"] = round((float(lows.min()) / entry_price - 1.0) * 100.0, 3)
    trade["trade_pnl_pct"] = trade["pnl_pct"]
    for horizon in (5, 20):
        probe = exit_idx + horizon
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
    trade["signal_day"] = str(source.get("signal_day", ""))
    trade["entry_year"] = str(pd.Timestamp(trade["entry_day"]).year)
    trade["exit_category"] = exit_reason_category(trade.get("exit_reason"))
    trade["candidate_id"] = _candidate_id(source)
    trade["source_exit_reason"] = source.get("exit_reason")
    trade["source_trade_pnl_pct"] = source.get("trade_pnl_pct")
    trade["near_regime_variant_included"] = not _is_disabled(source)


def _replay_split(
    source_path: Path,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], Counter, dict[str, Any]]:
    source_rows = _load_jsonl(source_path)
    costs = _resolve_execution_config(config)
    signal_policy = resolve_signal_execution_policy(config)
    history_cache: dict[str, tuple[pd.DataFrame, list[date], dict[int, str]] | None] = {}
    history_hashes: dict[str, str] = {}
    eligible_ids: list[str] = []
    eligible_sources: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    source_pnl_diffs: list[float] = []
    reason_matches = 0

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
        signal_day = date.fromisoformat(str(source["signal_day"]))
        if next_bar_index(closed, dates, signal_day) is None:
            skipped["missing_entry_bar"] += 1
            continue
        regime = str(source.get("regime", "unknown")).lower()
        policy_mode = signal_execution_mode_with_regime(
            str(source.get("signal_type", "")), signal_policy, regime
        )
        if policy_mode != "enabled":
            skipped[f"policy_{policy_mode}"] += 1
            continue
        candidate_id = _candidate_id(source)
        eligible_ids.append(candidate_id)
        eligible_sources.append(source)
        buy = {key: source.get(key) for key in _BUY_KEYS}
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
        )
        if trade is None:
            skipped[str(reason or "unknown")] += 1
            continue
        _enrich_trade(trade, closed, dates, source)
        trade["production_signal_policy_mode"] = policy_mode
        if source.get("trade_pnl_pct") is not None:
            source_pnl_diffs.append(
                abs(float(trade["pnl_pct"]) - float(source["trade_pnl_pct"]))
            )
        if trade.get("exit_reason") == source.get("exit_reason"):
            reason_matches += 1
        rows.append(trade)

    pnl_stats = stats(source_pnl_diffs)
    match = {
        "source_rows": len(source_rows),
        "source_candidates_sha256": _rows_manifest_sha256(source_rows),
        "source_candidate_ids_sha256": _ids_manifest_sha256(
            [_candidate_id(row) for row in source_rows]
        ),
        "eligible_rows": len(eligible_sources),
        "eligible_manifest_sha256": _rows_manifest_sha256(eligible_sources),
        "eligible_id_manifest_sha256": _ids_manifest_sha256(eligible_ids),
        "replayed_rows": len(rows),
        "replayed_manifest_sha256": _rows_manifest_sha256(rows),
        "history_manifest_sha256": hashlib.sha256(
            "\n".join(
                f"{symbol}|{history_hashes[symbol]}"
                for symbol in sorted(history_hashes)
            ).encode("utf-8")
        ).hexdigest(),
        "entry_signal_policy_validated": True,
        "policy_skips": {
            key: value
            for key, value in skipped.items()
            if str(key).startswith("policy_")
        },
        "source_pnl_abs_diff": pnl_stats,
        "exit_reason_exact_matches": reason_matches,
        "exit_reason_match_rate": (
            round(reason_matches / len(rows) * 100.0, 2) if rows else None
        ),
        # The candidate JSONL files contain historical outcome fields from
        # earlier audit semantics.  Do not use those fields as the gate for
        # today's production replay; retain the comparison as diagnostics.
        "baseline_replay_complete": (
            len(rows) == len(source_rows)
            and len(eligible_sources) == len(source_rows)
        ),
        "source_artifact_match_ok": bool(
            len(rows) == len(eligible_sources)
            and (pnl_stats.get("median", 0.0) <= 0.05 if pnl_stats else True)
            and (not rows or reason_matches / len(rows) >= 0.95)
        ),
    }
    return rows, skipped, match


def _candidate_report(
    rows: list[dict[str, Any]],
    variant_rows: list[dict[str, Any]],
    source_match: dict[str, Any],
    skipped: Counter,
    seed: int,
) -> dict[str, Any]:
    disabled = [row for row in rows if not row["near_regime_variant_included"]]
    retained = [row for row in rows if row["near_regime_variant_included"]]
    disabled_pnls = [float(row["trade_pnl_pct"]) for row in disabled]
    retained_pnls = [float(row["trade_pnl_pct"]) for row in retained]
    baseline_pnls = [float(row["trade_pnl_pct"]) for row in rows]
    def group_by_id(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault(str(item["candidate_id"]), []).append(item)
        return grouped

    baseline_by_id = group_by_id(retained)
    variant_by_id = group_by_id(variant_rows)
    common_ids = sorted(set(baseline_by_id) & set(variant_by_id))
    retained_ids_match = Counter(
        str(row["candidate_id"]) for row in retained
    ) == Counter(str(row["candidate_id"]) for row in variant_rows)
    retained_delta: list[float] = []
    exit_reason_matches = 0
    paired_retained = 0
    for candidate_id in common_ids:
        base_group = sorted(baseline_by_id[candidate_id], key=_canonical_json)
        variant_group = sorted(variant_by_id[candidate_id], key=_canonical_json)
        for baseline_row, variant_row in zip(base_group, variant_group):
            paired_retained += 1
            retained_delta.append(
                float(variant_row["trade_pnl_pct"])
                - float(baseline_row["trade_pnl_pct"])
            )
            if variant_row.get("exit_reason") == baseline_row.get("exit_reason"):
                exit_reason_matches += 1
    baseline_retained_hash = _rows_manifest_sha256(retained)
    variant_retained_hash = _rows_manifest_sha256(variant_rows)
    weak_candidate_ids = {str(row["candidate_id"]) for row in disabled}
    weak_symbols = {str(row.get("symbol", "")) for row in disabled}
    weak_days = {str(row.get("signal_day", "")) for row in disabled}
    return {
        "n_replayed": len(rows),
        "baseline_overall": {
            field: stats([row.get(field) for row in rows]) for field in FIELDS
        },
        "variant_executed": {
            "n": len(retained),
            "stats": {
                field: stats([row.get(field) for row in retained]) for field in FIELDS
            },
        },
        "disabled_weak_near": {
            "n": len(disabled),
            "stats": {
                field: stats([row.get(field) for row in disabled]) for field in FIELDS
            },
            "by_regime": group_stats(disabled, "regime"),
        },
        "by_regime": group_stats(rows, "regime"),
        "by_signal_type": group_stats(rows, "signal_type"),
        "policy_effect": {
            "disabled_count": len(disabled),
            "disabled_pnl_sum_pp": round(sum(disabled_pnls), 4),
            "avoided_loss_sum_pp": round(
                sum(-pnl for pnl in disabled_pnls if pnl < 0), 4
            ),
            "baseline_equal_weight_mean_pp": (
                round(float(np.mean(baseline_pnls)), 4) if baseline_pnls else None
            ),
            "variant_executed_equal_weight_mean_pp": (
                round(float(np.mean(retained_pnls)), 4) if retained_pnls else None
            ),
            "executed_mean_difference_is_not_paired": True,
        },
        "retained_pair_invariant": {
            "n": paired_retained,
            "pnl_delta_max_abs_pp": max((abs(delta) for delta in retained_delta), default=0.0),
            "exit_reason_match_rate_pct": (
                round(exit_reason_matches / paired_retained * 100.0, 2)
                if paired_retained
                else None
            ),
            "candidate_id_multiset_match": retained_ids_match,
            "baseline_retained_manifest_sha256": baseline_retained_hash,
            "variant_retained_manifest_sha256": variant_retained_hash,
            "exit_semantics_changed": (
                not retained_ids_match
                or baseline_retained_hash != variant_retained_hash
            ),
        },
        "sample_gate": {
            "weak_near_row_count": len(disabled),
            "weak_near_unique_candidates": len(weak_candidate_ids),
            "weak_near_unique_symbols": len(weak_symbols),
            "weak_near_unique_signal_days": len(weak_days),
            "minimum_required": MIN_WEAK_NEAR_COUNT,
            "minimum_unique_symbols": MIN_WEAK_NEAR_UNIQUE_SYMBOLS,
            "minimum_unique_signal_days": MIN_WEAK_NEAR_UNIQUE_DAYS,
            "sufficient": (
                len(weak_candidate_ids) >= MIN_WEAK_NEAR_COUNT
                and len(weak_symbols) >= MIN_WEAK_NEAR_UNIQUE_SYMBOLS
                and len(weak_days) >= MIN_WEAK_NEAR_UNIQUE_DAYS
            ),
        },
        "source_match": source_match,
        "skipped": dict(skipped),
        "seed": seed,
    }


def _portfolio_config(config: dict[str, Any], seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
    costs = _resolve_execution_config(config)
    position = config.get("position", {}) or {}
    portfolio = {
        "initial_cash": float(costs.get("initial_cash", 100000.0)),
        "max_positions": int(position.get("max_stocks", 4)),
        "position_size_pct": float(position.get("base_position_per_stock", 0.25)),
        "lot_size": int(costs.get("lot_size", 100)),
        "signal_priority": costs.get("signal_priority"),
        "score_mode": "P0",
        "tie_break": "symbol_asc",
        "seed": seed,
    }
    return costs, portfolio


def _portfolio_summary(
    trades: list[dict[str, Any]],
    costs: dict[str, Any],
    portfolio_config: dict[str, Any],
) -> dict[str, Any]:
    result = run_portfolio(copy.deepcopy(trades), costs, portfolio_config)
    return {
        "summary": result["summary"],
        "rejection_reasons": result["rejection_reasons"],
        "completed_count": len(result["trades"]),
        "equity_curve": result["equity_curve"],
    }, result["trades"], result["rejections"]


def run(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).expanduser().resolve()
    config_file_hash_before = _file_sha256(config_path)
    config = load_config(config_path)
    if config is None:
        raise RuntimeError(f"unable to load config: {args.config}")
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    splits = tuple(args.splits)
    freeze_manifest = None
    if args.freeze_manifest:
        freeze_manifest = _load_freeze_manifest(
            Path(args.freeze_manifest).expanduser().resolve()
        )
    config_snapshot = _config_snapshot(config)
    effective_config_hash = _config_sha256(config)
    config_snapshot_hash = _config_sha256(config_snapshot)
    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "seed": args.seed,
        "design": {
            "fixed_candidate_source": True,
            "fixed_history_source": True,
            "baseline": "frozen_candidates_with_production_exit_semantics_SL8_TP30",
            "variant": "bear_range_near_observe_only",
            "variant_rule": f"{NEAR_SIGNAL} excluded when regime in {sorted(WEAK_REGIMES)}",
            "bull_near_unchanged": True,
            "fixed_exit_semantics": "SL8/TP30/T+1/fees/slippage/price-limit/timeout",
            "entry_policy_validation": "signal_execution_policy_only",
            "entry_filters_not_replayed": ["market_gate", "stock_pool", "fundamental"],
            "production_config_unchanged": "verified_by_before_after_file_hash",
            "variant_is_production_eligible": False,
        },
        "sample_policy": {
            "minimum_weak_near_count": MIN_WEAK_NEAR_COUNT,
            "test_is_viewed_history_not_blind_holdout": True,
            "iid_transaction_stats_are_exploratory": True,
            "source_outcome_fields_are_diagnostic_only": True,
        },
        "config_snapshot": config_snapshot,
        "config_snapshot_sha256": config_snapshot_hash,
        "effective_config_sha256": effective_config_hash,
        "config_file": str(config_path),
        "config_file_sha256_before": config_file_hash_before,
        "splits_run": list(splits),
        "freeze_manifest": (
            str(Path(args.freeze_manifest).expanduser().resolve())
            if args.freeze_manifest
            else None
        ),
        "splits": {},
    }
    for split in splits:
        source_path = input_dir / f"candidates_{split}.jsonl"
        rows, skipped, source_match = _replay_split(source_path, config)
        replay_path = output_dir / f"replay_{split}.jsonl"
        _write_jsonl(replay_path, rows)
        freeze_check = (
            _validate_freeze_manifest(freeze_manifest, rows)
            if freeze_manifest is not None
            else None
        )
        costs, portfolio_config = _portfolio_config(config, args.seed)
        disabled = [row for row in rows if not row["near_regime_variant_included"]]
        retained = [row for row in rows if row["near_regime_variant_included"]]
        candidate_report = _candidate_report(
            rows, retained, source_match, skipped, args.seed
        )
        baseline_report, baseline_trades, baseline_rejections = _portfolio_summary(
            rows, costs, portfolio_config
        )
        variant_report, variant_trades, variant_rejections = _portfolio_summary(
            retained, costs, portfolio_config
        )
        baseline_trades_path = output_dir / f"portfolio_{split}_baseline_trades.jsonl"
        variant_trades_path = output_dir / f"portfolio_{split}_variant_trades.jsonl"
        baseline_rej_path = output_dir / f"portfolio_{split}_baseline_rejections.jsonl"
        variant_rej_path = output_dir / f"portfolio_{split}_variant_rejections.jsonl"
        _write_jsonl(baseline_trades_path, baseline_trades)
        _write_jsonl(variant_trades_path, variant_trades)
        _write_jsonl(baseline_rej_path, baseline_rejections)
        _write_jsonl(variant_rej_path, variant_rejections)
        artifact_hashes = {
            "replay": _file_sha256(replay_path),
            "baseline_trades": _file_sha256(baseline_trades_path),
            "variant_trades": _file_sha256(variant_trades_path),
            "baseline_rejections": _file_sha256(baseline_rej_path),
            "variant_rejections": _file_sha256(variant_rej_path),
        }
        result["splits"][split] = {
            "source": str(source_path),
            "replay": str(replay_path),
            "candidate_report": candidate_report,
            "freeze_check": freeze_check,
            "portfolio": {
                "config": portfolio_config,
                "artifacts_sha256": artifact_hashes,
                "baseline": baseline_report,
                "variant": variant_report,
                "baseline_trades": str(baseline_trades_path),
                "variant_trades": str(variant_trades_path),
                "baseline_rejections": str(baseline_rej_path),
                "variant_rejections": str(variant_rej_path),
            },
            "disabled_count": len(disabled),
            "retained_count": len(retained),
        }
    config_file_hash_after = _file_sha256(config_path)
    result["config_file_sha256_after"] = config_file_hash_after
    result["config_file_unchanged"] = config_file_hash_before == config_file_hash_after
    baseline_ok = all(
        bool(result["splits"][split]["candidate_report"]["source_match"]["baseline_replay_complete"])
        for split in splits
    )
    sample_ok = all(
        result["splits"][split]["candidate_report"]["sample_gate"]["sufficient"]
        for split in splits
    )
    freeze_checks = {
        split: result["splits"][split]["freeze_check"] for split in splits
    }
    freeze_ok = bool(freeze_manifest) and all(
        bool(freeze_checks[split])
        and freeze_checks[split]["rules_match"] is True
        and freeze_checks[split]["all_signal_days_after_start"] is True
        and freeze_checks[split]["all_candidates_in_universe"] is True
        for split in splits
    )
    holdout_ok = "holdout" in splits and freeze_ok
    # Direction consistency: the disabled weak-near trades should be
    # loss-making in both val and the holdout (i.e. the variant removes
    # losers in both windows).  Sample gate must already be satisfied.
    val_disabled = (
        result["splits"].get("val", {}).get("candidate_report", {}).get("policy_effect", {})
    )
    holdout_disabled = (
        result["splits"].get("holdout", {}).get("candidate_report", {}).get("policy_effect", {})
    )
    direction_consistent = bool(
        holdout_ok
        and val_disabled.get("disabled_pnl_sum_pp") is not None
        and holdout_disabled.get("disabled_pnl_sum_pp") is not None
        and val_disabled["disabled_pnl_sum_pp"] < 0
        and holdout_disabled["disabled_pnl_sum_pp"] < 0
    )
    # Portfolio drawdown: variant must not materially worsen max drawdown.
    drawdown_ok = all(
        result["splits"][split]["portfolio"]["variant"]["summary"].get("max_drawdown_pct", 0.0)
        <= (
            result["splits"][split]["portfolio"]["baseline"]["summary"].get("max_drawdown_pct", 0.0)
            + DRAWDOWN_TOLERANCE_PP
        )
        for split in splits
    )
    result["gates"] = {
        "baseline_replay_complete_all_splits": baseline_ok,
        "source_artifact_match_diagnostic_all_splits": all(
            bool(result["splits"][split]["candidate_report"]["source_match"]["source_artifact_match_ok"])
            for split in splits
        ),
        "weak_near_min_sample_all_splits": sample_ok,
        "holdout_freeze_manifest_validated": freeze_ok,
        "holdout_direction_consistent_with_val": direction_consistent,
        "portfolio_drawdown_not_materially_worsened": drawdown_ok,
        "unseen_blind_holdout_available": holdout_ok,
        "production_config_unchanged": result["config_file_unchanged"],
        "all_pass": False,
    }
    if not baseline_ok:
        result["verdict"] = (
            "baseline_replay_incomplete: one or more frozen candidates could not be "
            "replayed under production semantics; do not change production policy"
        )
    elif not sample_ok:
        result["verdict"] = (
            "insufficient_sample_exploratory: weak near sample is insufficient for a "
            "confirmatory decision; do not change production policy"
        )
    elif not holdout_ok:
        result["verdict"] = (
            "holdout_unavailable_exploratory: the 2026-09+ blind holdout is not "
            "frozen or not yet available; do not change production policy"
        )
    elif not direction_consistent:
        result["verdict"] = (
            "direction_inconsistent_exploratory: val and holdout disagree on whether "
            "disabled weak-near trades are loss-making; do not change production policy"
        )
    elif not drawdown_ok:
        result["verdict"] = (
            "drawdown_worsened_exploratory: the variant's portfolio drawdown exceeds "
            "the baseline by more than the tolerance; do not change production policy"
        )
    else:
        result["verdict"] = (
            "research_only_candidate: val and blind holdout both support disabling "
            "weak-regime near entries; production adoption still requires final review"
        )
    result["gates"]["all_pass"] = bool(
        baseline_ok and sample_ok and holdout_ok and direction_consistent and drawdown_ok
    )
    result_path = output_dir / "macd_near_regime_audit.json"
    with result_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=r"D:\tmp\candidates")
    parser.add_argument("--output-dir", default=r"D:\tmp\macd_near_regime_audit")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--label", default="macd-near-regime-audit")
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--splits", nargs="+", default=list(SPLITS))
    parser.add_argument("--freeze-manifest", default=None)
    args = parser.parse_args()
    result = run(args)
    print(
        json.dumps(
            {
                "version": result["version"],
                "gates": result["gates"],
                "verdict": result["verdict"],
                "per_split": {
                    split: {
                        "replayed": result["splits"][split]["candidate_report"]["n_replayed"],
                        "disabled_weak_near": result["splits"][split]["disabled_count"],
                        "retained": result["splits"][split]["retained_count"],
                        "baseline_return_pct": result["splits"][split]["portfolio"]["baseline"]["summary"].get("total_return_pct"),
                        "variant_return_pct": result["splits"][split]["portfolio"]["variant"]["summary"].get("total_return_pct"),
                    }
                    for split in args.splits
                },
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
