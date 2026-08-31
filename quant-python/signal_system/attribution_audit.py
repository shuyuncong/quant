"""P0 交易生命周期与退出效率审计 (attribution_audit.py v2.0.0).

v2 从冻结候选集（frozen candidates）按生产执行语义重放每笔交易，再做三维归因：

生产语义（production_risk 默认档）：
- 真实止损/止盈：risk.stop_loss_pct (SL8) / risk.stop_profit_pct (TP30)
- T+1、佣金/印花税/滑点、保守涨跌停模型（封板延迟成交）、缠论卖点、timeout
  均由 backtest_winrate.simulate_single_trade 按 config 执行
- 用真实前复权日线重算 future_5/20/40、MFE/MAE、post_exit_5/20 生命周期字段

三维归因：signal_type × regime × exit_category，每个格子给
n / avg_pnl / median_pnl / win_rate / pnl_sum_pp / exit_efficiency / avg_holding_bars。
另附二维交叉表（signal_type×regime、signal_type×exit_category、regime×exit_category）
与各维度的 pnl_sum 贡献。

判定逻辑（v1 保留）：future/MFE 好但 trade 差 → 退出问题；
future/MFE 差 → 入场问题；退出后继续涨 → 退出泄漏。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

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


AUDIT_VERSION = "2.0.0"
FIELDS = ["future_5d", "future_20d", "future_40d", "mfe", "mae", "trade_pnl_pct",
          "post_exit_5d", "post_exit_20d"]

# These labels mirror the simulator's actual exit reasons.  In particular,
# sell_1/sell_2/sell_3 are Chan sell signals, not risk stop-loss exits.
EXIT_REASON_SEMANTICS = {
    "stop_loss": "risk_stop_loss",
    "take_profit": "risk_take_profit",
    "sell_1": "chan_sell_1",
    "sell_2": "chan_sell_2",
    "sell_3": "chan_sell_3",
    "zero_axis_death_cross": "macd_zero_axis_death_cross",
    "timeout": "time_limit",
    "timeout_ma_break": "time_limit_ma_break",
    "timeout_hard_cap": "time_limit_hard_cap",
    "window_end": "right_censored_window_end",
}

# Buy fields the simulator consumes when replaying a frozen candidate.
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

# Source candidate fields carried into the replayed lifecycle output.
_PASSTHROUGH_KEYS = (
    "regime",
    "market_cap",
    "roe",
    "debt_ratio",
    "pe",
    "period",
    "ann_date",
    "ann_date_estimated",
)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "avg": round(sum(vals) / len(vals), 3),
        "median": round(float(pd.Series(vals).median()), 3),
        "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
    }


def group_stats(rows, group_key):
    groups = defaultdict(list)
    for r in rows:
        groups[r.get(group_key, "unknown")].append(r)
    out = {}
    for g, grp in sorted(groups.items()):
        entry = {"n": len(grp)}
        for f in FIELDS:
            entry[f] = stats([r.get(f) for r in grp])
        # 退出效率：实际收益 / MFE（理想 = 吃满 MFE）
        eff = []
        for r in grp:
            mfe = r.get("mfe")
            pnl = r.get("trade_pnl_pct")
            if mfe is not None and pnl is not None and mfe > 0:
                eff.append(pnl / mfe)
        entry["exit_efficiency"] = stats(eff) if eff else {"n": 0}
        out[g] = entry
    return out


def exit_reason_category(reason):
    """Return an unambiguous simulator-semantic category for an exit reason."""
    return EXIT_REASON_SEMANTICS.get(str(reason), "unknown")


def judge(rows):
    """按用户设定规则判断入场 vs 退出问题。"""
    f40 = [r.get("future_40d") for r in rows if r.get("future_40d") is not None]
    mfe = [r.get("mfe") for r in rows if r.get("mfe") is not None]
    pnl = [r.get("trade_pnl_pct") for r in rows if r.get("trade_pnl_pct") is not None]
    post20 = [r.get("post_exit_20d") for r in rows if r.get("post_exit_20d") is not None]
    if not f40 or not pnl:
        return "insufficient"
    med_f40 = float(pd.Series(f40).median())
    med_mfe = float(pd.Series(mfe).median()) if mfe else None
    med_pnl = float(pd.Series(pnl).median())
    med_post = float(pd.Series(post20).median()) if post20 else None
    # 判定
    entry_weak = med_f40 <= 0 or (med_mfe is not None and med_mfe <= 0)
    exit_weak = med_f40 > 0 and med_pnl <= 0
    exit_leak = med_post is not None and med_post > 0 and med_pnl <= 0
    if exit_leak:
        return "exit_problem (future好、退出后继续涨)"
    if exit_weak:
        return "exit_problem (future好、trade差)"
    if entry_weak:
        return "entry_problem (future/MFE差)"
    return "both_ok (入场退出都不明显差)"


def _frozen_candidate_costs(config: dict[str, Any]) -> dict[str, Any]:
    """Reproduce the frozen dataset's execution semantics (no risk SL/TP).

    The frozen candidate files were generated without risk stop/take-profit
    exits; keep that baseline while nesting the resolved research timeout
    fields so simulate_single_trade keeps the fixed timeout behaviour.
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
    return costs


def _config_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    risk = config.get("risk", {})
    backtest = config.get("backtest", {})
    chan_zero_axis = backtest.get("chan_zero_axis", {}) or {}
    return {
        "stop_loss_pct": risk.get("stop_loss_pct"),
        "stop_profit_pct": risk.get("stop_profit_pct"),
        "commission_pct": backtest.get("commission_pct"),
        "minimum_commission": backtest.get("minimum_commission"),
        "stamp_tax_pct": backtest.get("stamp_tax_pct"),
        "slippage_pct": backtest.get("slippage_pct"),
        "lot_size": backtest.get("lot_size"),
        "t_plus_one": backtest.get("t_plus_one"),
        "price_limit_model": backtest.get("price_limit_model"),
        "intrabar_conflict": backtest.get("intrabar_conflict"),
        "max_holding_bars": chan_zero_axis.get("max_holding_bars"),
        "timeout_exit_mode": chan_zero_axis.get("timeout_exit_mode", "fixed"),
        "adjustment": backtest.get("adjustment", "qfq"),
    }


def _history_manifest_sha256(history_hashes: dict[str, str]) -> str:
    return hashlib.sha256(
        "\n".join(
            f"{symbol}|{history_hashes[symbol]}"
            for symbol in sorted(history_hashes)
        ).encode("utf-8")
    ).hexdigest()


def _manifest_sha256(items: list[str]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(items)).encode("utf-8")
    ).hexdigest()


def _cell_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [r.get("trade_pnl_pct") for r in rows if r.get("trade_pnl_pct") is not None]
    eff = [
        r["trade_pnl_pct"] / r["mfe"]
        for r in rows
        if r.get("mfe") is not None and r.get("mfe", 0) > 0
        and r.get("trade_pnl_pct") is not None
    ]
    holdings = [r.get("holding_bars") for r in rows if r.get("holding_bars") is not None]
    cell = {"n": len(rows)}
    if pnls:
        cell["avg_pnl"] = round(sum(pnls) / len(pnls), 3)
        cell["median_pnl"] = round(float(pd.Series(pnls).median()), 3)
        cell["win_rate"] = round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1)
        cell["pnl_sum_pp"] = round(sum(pnls), 2)
    else:
        cell["avg_pnl"] = None
        cell["median_pnl"] = None
        cell["win_rate"] = None
        cell["pnl_sum_pp"] = None
    cell["exit_efficiency"] = stats(eff) if eff else {"n": 0}
    cell["avg_holding_bars"] = (
        round(sum(holdings) / len(holdings), 1) if holdings else None
    )
    return cell


def _pivot_3d(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """signal_type × regime × exit_category attribution table."""
    cells: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for row in rows:
        signal_type = str(row.get("signal_type", "unknown"))
        regime = str(row.get("regime", "unknown"))
        category = str(row.get("exit_category", "unknown"))
        cells[signal_type][regime][category].append(row)
    out = {}
    for signal_type, by_regime in sorted(cells.items()):
        regimes = {}
        for regime, by_category in sorted(by_regime.items()):
            regimes[regime] = {
                category: _cell_stats(group)
                for category, group in sorted(by_category.items())
            }
        out[signal_type] = regimes
    return out


def _cross_2d(rows: list[dict[str, Any]], key_a: str, key_b: str) -> dict[str, Any]:
    cells: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        cells[str(row.get(key_a, "unknown"))][str(row.get(key_b, "unknown"))].append(row)
    return {
        a: {
            b: _cell_stats(group)
            for b, group in sorted(by_b.items())
        }
        for a, by_b in sorted(cells.items())
    }


def _contribution(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups = defaultdict(list)
    for row in rows:
        groups[str(row.get(key, "unknown"))].append(row)
    out = {}
    for g, grp in sorted(groups.items()):
        pnls = [r.get("trade_pnl_pct") for r in grp if r.get("trade_pnl_pct") is not None]
        out[g] = {
            "n": len(grp),
            "pnl_sum_pp": round(sum(pnls), 2) if pnls else None,
            "avg_pnl": round(sum(pnls) / len(pnls), 3) if pnls else None,
        }
    return out


def _replay_split(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    profile: str,
    history_dir: Path,
) -> tuple[list[dict[str, Any]], Counter, dict[str, Any]]:
    costs = (
        _frozen_candidate_costs(config)
        if profile == "frozen_source"
        else _resolve_execution_config(config)
    )
    history_cache: dict[str, tuple[pd.DataFrame, list, dict[int, str]] | None] = {}
    history_hashes: dict[str, str] = {}
    output_rows: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    eligible_ids: list[str] = []
    pnl_diffs: list[float] = []
    reason_matches = 0

    for source in rows:
        symbol = str(source.get("symbol", "")).zfill(6)
        if symbol not in history_cache:
            path = history_dir / f"{symbol}_qfq.pkl"
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
            closed, dates, date.fromisoformat(str(source["signal_day"]))
        )
        if entry_idx is None:
            skipped["missing_entry_bar"] += 1
            continue
        eligible_ids.append(
            f"{symbol}|{source['signal_day']}|{source.get('signal_type', '')}"
        )
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
        trade_entry_idx = dates.index(pd.Timestamp(trade["entry_day"]).date())
        trade_exit_idx = dates.index(pd.Timestamp(trade["exit_day"]).date())
        entry_price = float(trade["entry_price"])
        for horizon in (5, 20, 40):
            probe = trade_entry_idx + horizon
            trade[f"future_{horizon}d"] = (
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
        for key in _PASSTHROUGH_KEYS:
            trade[key] = source.get(key)
        trade["signal_day"] = str(source.get("signal_day", ""))
        trade["entry_year"] = str(pd.Timestamp(trade["entry_day"]).year)
        trade["exit_category"] = exit_reason_category(trade.get("exit_reason"))
        trade["candidate_id"] = (
            f"{symbol}|{source['signal_day']}|{source.get('signal_type', '')}"
        )
        trade["source_trade_pnl_pct"] = source.get("trade_pnl_pct")
        trade["source_exit_reason"] = source.get("exit_reason")
        if source.get("trade_pnl_pct") is not None:
            pnl_diffs.append(
                abs(float(trade["pnl_pct"]) - float(source["trade_pnl_pct"]))
            )
        if trade.get("exit_reason") == source.get("exit_reason"):
            reason_matches += 1
        output_rows.append(trade)

    match: dict[str, Any] = {
        "source_rows": len(rows),
        "source_candidates_sha256": _manifest_sha256(
            f"{str(r.get('symbol', '')).zfill(6)}|{r.get('signal_day')}|{r.get('signal_type', '')}"
            for r in rows
        ),
        "common_eligible_rows": len(eligible_ids),
        "common_eligible_manifest_sha256": _manifest_sha256(eligible_ids),
        "history_manifest_sha256": _history_manifest_sha256(history_hashes),
        "simulated_rows": len(output_rows),
        "completed_manifest_sha256": _manifest_sha256(
            [str(r["candidate_id"]) for r in output_rows]
        ),
        "source_pnl_abs_diff": stats(pnl_diffs),
        "exit_reason_exact_matches": reason_matches,
        "exit_reason_match_rate": (
            round(reason_matches / len(output_rows) * 100.0, 2)
            if output_rows
            else None
        ),
    }
    if profile == "frozen_source":
        pnl_median = (match["source_pnl_abs_diff"] or {}).get("median")
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


def _audit(rows: list[dict[str, Any]], label: str, skipped: Counter, profile: str) -> dict[str, Any]:
    categorized = [
        {**row, "exit_category": exit_reason_category(row.get("exit_reason", "unknown"))}
        for row in rows
    ]
    return {
        "version": AUDIT_VERSION,
        "label": label,
        "n": len(rows),
        "execution_profile": profile,
        "overall": {f: stats([r.get(f) for r in rows]) for f in FIELDS},
        "overall_exit_efficiency": stats(
            [r["trade_pnl_pct"] / r["mfe"] for r in rows
             if r.get("mfe") and r.get("mfe", 0) > 0 and r.get("trade_pnl_pct") is not None]
        ),
        "by_regime": group_stats(rows, "regime"),
        "by_signal_type": group_stats(rows, "signal_type"),
        "by_exit_reason": group_stats(rows, "exit_reason"),
        "by_exit_category": group_stats(categorized, "exit_category"),
        "by_entry_year": group_stats(rows, "entry_year"),
        "cross_signal_type_x_regime": _cross_2d(rows, "signal_type", "regime"),
        "cross_signal_type_x_exit_category": _cross_2d(categorized, "signal_type", "exit_category"),
        "cross_regime_x_exit_category": _cross_2d(categorized, "regime", "exit_category"),
        "pivot_3d": _pivot_3d(categorized),
        "contribution_by_signal_type": _contribution(rows, "signal_type"),
        "contribution_by_regime": _contribution(rows, "regime"),
        "contribution_by_exit_category": _contribution(categorized, "exit_category"),
        "contribution_by_exit_reason": _contribution(rows, "exit_reason"),
        "overall_judgement": judge(rows),
        "exit_reason_counts": dict(Counter(r.get("exit_reason", "unknown") for r in rows)),
        "skipped": dict(skipped),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", required=True, help="frozen candidate JSONL (one split)")
    ap.add_argument("--output", required=True, help="audit JSON output")
    ap.add_argument("--label", default="unknown")
    ap.add_argument(
        "--config",
        default=str(BASE_DIR / "config" / "config.yaml"),
        help="strategy config YAML (source of production SL8/TP30 + execution costs)",
    )
    ap.add_argument(
        "--execution-profile",
        choices=("production_risk", "frozen_source"),
        default="production_risk",
        help="production_risk: real SL8/TP30 + fees/slippage/T+1/price-limit; "
        "frozen_source: reproduce the frozen dataset (no risk exits) as a replay check",
    )
    ap.add_argument(
        "--replay-output",
        default=None,
        help="optional regenerated lifecycle JSONL (production-semantics candidates)",
    )
    ap.add_argument("--history-dir", default=str(HISTORY_DIR), help="qfq daily-history pkl dir")
    args = ap.parse_args()

    config = load_config(args.config)
    if config is None:
        ap.error(f"unable to load config: {args.config}")
    rows = load(args.candidate)
    output_rows, skipped, match = _replay_split(
        rows, config, args.execution_profile, Path(args.history_dir).expanduser()
    )
    result = _audit(output_rows, args.label, skipped, args.execution_profile)
    result["source_match"] = match
    result["config_snapshot"] = _config_snapshot(config)
    result["config_snapshot_sha256"] = hashlib.sha256(
        json.dumps(result["config_snapshot"], sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    result["exit_reason_semantics"] = EXIT_REASON_SEMANTICS

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    if args.replay_output:
        os.makedirs(os.path.dirname(args.replay_output), exist_ok=True)
        with open(args.replay_output, "w", encoding="utf-8") as f:
            for row in output_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"saved {args.output}", flush=True)
    print(f"label={args.label} profile={args.execution_profile} n={len(output_rows)} "
          f"skipped={sum(skipped.values())}", flush=True)
    print(f"overall: f40={result['overall']['future_40d']} mfe={result['overall']['mfe']} "
          f"pnl={result['overall']['trade_pnl_pct']} post20={result['overall']['post_exit_20d']} "
          f"eff={result['overall_exit_efficiency']}", flush=True)
    print(f"judgement: {result['overall_judgement']}", flush=True)
    return 0


if __name__ == "__main__":
    main()