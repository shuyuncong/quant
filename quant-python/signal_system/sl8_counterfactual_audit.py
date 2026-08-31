"""SL8 因果反事实审计 (sl8_counterfactual_audit.py v1.0.0).

固定所有入场候选与除止损外的全部退出规则，对照两个执行档：

- baseline: 生产 P0 语义 SL8/TP30（production_risk，即 attribution_audit v2 基线）
- variant:  无止损诊断档（仅禁用 stop_loss，TP30/timeout/缠论卖点/死叉全部保留）

仅对 baseline 中触发 stop_loss 的交易配对比较：
- 最终配对 PnL（variant − baseline）
- 止损触发日之后 5/20/40 根K线恢复程度（若未止损，持仓在那些时点的 PnL，相对入场价）
- baseline 退出后 5/20 日恢复（相对止损成交价，来源 baseline post_exit 字段）
- MAE、P10、尾部最大损失（min）
- train/val/test 跨窗口稳定性

预冻结判定（全部通过才认为 SL8 过紧，进入第二阶段 SL10 单变量实验）：
- train: 平均配对 delta >= -0.5pp（不恶化）
- val:   平均 delta > 0 且 bootstrap CI95 下界 > 0
- test:  平均 delta >= -0.5pp（不显著恶化）
- 尾部:  val 与 test 的 variant P10 >= baseline P10 - 5pp，
         且 variant min >= baseline min - 10pp（尾部损失不明显扩大）

无止损档仅用于诊断，不作为生产候选。任一闸门失败 → SL8 在保护组合，
停止退出调参，转向验证 bear/range 下 macd_near 降级为 observe_only。
生产配置（P0 / SL8 / TP30 / C4）本脚本不修改。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import (
    HISTORY_DIR,
    _resolve_execution_config,
    _sell_events_by_index,
    find_signals,
    next_bar_index,
    prepare_closed_bars,
    simulate_single_trade,
)
from attribution_audit import _config_snapshot, exit_reason_category
from utils.helpers import load_config


VERSION = "sl8_counterfactual.v1"
SPLITS = ("train", "val", "test")
SL_EXIT = "stop_loss"

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

# Source candidate fields carried into the paired output.
_PASSTHROUGH_KEYS = ("regime", "market_cap")

# Pre-frozen gate tolerances (pp).
GATE_TOLERANCES = {
    "train_delta_pp": -0.5,
    "test_delta_pp": -0.5,
    "p10_delta_pp": -5.0,
    "min_delta_pp": -10.0,
}

# Research guardrails are frozen in the report so a future small sample cannot
# accidentally pass the "SL8 too tight" decision gate on a degenerate CI.
MIN_SL_CUT_COUNTS = {
    "train": 5,
    "val": 30,
    "test": 10,
}
BOOTSTRAP_RESAMPLES = 5000


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


def _manifest_sha256(items: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(items)).encode("utf-8")).hexdigest()


def _rows_manifest_sha256(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(_canonical_json(row) for row in rows)).encode("utf-8")
    ).hexdigest()


def _config_sha256(config: dict[str, Any]) -> str:
    """Hash the full effective config used by signal discovery and replay."""
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()


def _bootstrap_mean_ci(values: list[float], seed: int) -> dict[str, Any]:
    clean_values: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(number):
            clean_values.append(number)
    clean = np.asarray(clean_values, dtype=float)
    if clean.size == 0:
        return {"n": 0}
    if clean.size == 1:
        only = float(clean[0])
        return {"n": 1, "mean": only, "median": only, "p10": only, "p90": only,
                "min": only, "max": only, "positive_pct": 100.0 if only > 0 else 0.0,
                "ci95_low": only, "ci95_high": only}
    rng = np.random.default_rng(seed)
    samples = rng.choice(
        clean, size=(BOOTSTRAP_RESAMPLES, clean.size), replace=True
    ).mean(axis=1)
    low, high = np.quantile(samples, [0.025, 0.975])
    return {
        "n": int(clean.size),
        "mean": round(float(clean.mean()), 4),
        "median": round(float(np.median(clean)), 4),
        "p10": round(float(np.quantile(clean, 0.10)), 4),
        "p90": round(float(np.quantile(clean, 0.90)), 4),
        "min": round(float(clean.min()), 4),
        "max": round(float(clean.max()), 4),
        "positive_pct": round(float((clean > 0).mean() * 100.0), 2),
        "ci95_low": round(float(low), 4),
        "ci95_high": round(float(high), 4),
    }


def _lifecycle(
    trade: dict[str, Any],
    closed: pd.DataFrame,
    dates: list[date],
    source: dict[str, Any],
) -> None:
    """Fill lifecycle fields on a replayed trade (mirrors attribution_audit v2)."""
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
    highs = pd.to_numeric(
        closed.iloc[entry_idx : exit_idx + 1]["high"], errors="coerce"
    )
    lows = pd.to_numeric(
        closed.iloc[entry_idx : exit_idx + 1]["low"], errors="coerce"
    )
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
    for key in _PASSTHROUGH_KEYS:
        trade[key] = source.get(key)
    trade["signal_day"] = str(source.get("signal_day", ""))
    trade["entry_year"] = str(pd.Timestamp(trade["entry_day"]).year)
    trade["exit_category"] = exit_reason_category(trade.get("exit_reason"))
    trade["candidate_id"] = (
        f"{str(source.get('symbol', '')).zfill(6)}|{source['signal_day']}"
        f"|{source.get('signal_type', '')}"
    )


def _run_split(
    source_path: Path,
    config: dict[str, Any],
    history_dir: Path,
    seed: int,
) -> dict[str, Any]:
    source_rows = _load_jsonl(source_path)
    baseline_costs = _resolve_execution_config(config)
    variant_costs = copy.deepcopy(baseline_costs)
    variant_costs["stop_loss_pct"] = None  # diagnostic arm: SL disabled only

    history_cache: dict[str, tuple[pd.DataFrame, list, dict[int, str]] | None] = {}
    history_hashes: dict[str, str] = {}
    paired_rows: list[dict[str, Any]] = []
    sl_cut_rows: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    eligible_ids: list[str] = []
    eligible_source_rows: list[dict[str, Any]] = []

    for source in source_rows:
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
        candidate_id = (
            f"{symbol}|{source['signal_day']}|{source.get('signal_type', '')}"
        )
        eligible_ids.append(candidate_id)
        eligible_source_rows.append(source)
        buy = {key: source.get(key) for key in _BUY_KEYS}
        buy["day"] = source["signal_day"]
        buy["side"] = "buy"
        trade_b, reason_b = simulate_single_trade(
            symbol, closed, dates, buy, sells_by_index, baseline_costs,
            allow_incomplete=False, market_context=None,
        )
        if trade_b is None:
            skipped[f"baseline_{reason_b}"] += 1
            continue
        trade_v, reason_v = simulate_single_trade(
            symbol, closed, dates, buy, sells_by_index, variant_costs,
            allow_incomplete=False, market_context=None,
        )
        if trade_v is None:
            skipped[f"variant_{reason_v}"] += 1
            continue
        _lifecycle(trade_b, closed, dates, source)
        _lifecycle(trade_v, closed, dates, source)
        pair = {
            "candidate_id": trade_b["candidate_id"],
            "symbol": symbol,
            "signal_day": str(source.get("signal_day", "")),
            "signal_type": str(source.get("signal_type", "")),
            "regime": str(source.get("regime", "unknown")),
            "entry_day": trade_b["entry_day"],
            "baseline": {
                "pnl_pct": trade_b["trade_pnl_pct"],
                "exit_reason": trade_b["exit_reason"],
                "exit_category": trade_b["exit_category"],
                "exit_day": trade_b["exit_day"],
                "holding_bars": trade_b["holding_bars"],
                "mfe": trade_b["mfe"],
                "mae": trade_b["mae"],
                "post_exit_5d": trade_b.get("post_exit_5d"),
                "post_exit_20d": trade_b.get("post_exit_20d"),
                "future_40d": trade_b.get("future_40d"),
            },
            "variant": {
                "pnl_pct": trade_v["trade_pnl_pct"],
                "exit_reason": trade_v["exit_reason"],
                "exit_category": trade_v["exit_category"],
                "exit_day": trade_v["exit_day"],
                "holding_bars": trade_v["holding_bars"],
                "mfe": trade_v["mfe"],
                "mae": trade_v["mae"],
                "future_40d": trade_v.get("future_40d"),
            },
        }
        if trade_b["exit_reason"] == SL_EXIT:
            sl_exit_idx = dates.index(pd.Timestamp(trade_b["exit_day"]).date())
            entry_price = float(trade_b["entry_price"])
            for horizon in (5, 20, 40):
                probe = sl_exit_idx + horizon
                pair[f"recovery_pnl_pp_plus_{horizon}d"] = (
                    round((float(closed.iloc[probe]["close"]) / entry_price - 1.0) * 100.0, 3)
                    if probe < len(closed)
                    else None
                )
            sl_cut_rows.append(pair)
        paired_rows.append(pair)

    deltas = [
        float(pair["variant"]["pnl_pct"]) - float(pair["baseline"]["pnl_pct"])
        for pair in sl_cut_rows
    ]
    mae_baseline = [float(pair["baseline"]["mae"]) for pair in sl_cut_rows]
    mae_variant = [float(pair["variant"]["mae"]) for pair in sl_cut_rows]
    pnl_baseline = [float(pair["baseline"]["pnl_pct"]) for pair in sl_cut_rows]
    pnl_variant = [float(pair["variant"]["pnl_pct"]) for pair in sl_cut_rows]
    exit_transitions = Counter(
        f"{pair['baseline']['exit_reason']}->{pair['variant']['exit_reason']}"
        for pair in sl_cut_rows
    )
    group_deltas: dict[str, list[float]] = defaultdict(list)
    for pair in sl_cut_rows:
        group = f"{pair['regime']}|{pair['signal_type']}"
        group_deltas[group].append(
            float(pair["variant"]["pnl_pct"]) - float(pair["baseline"]["pnl_pct"])
        )
    recovery = {}
    for horizon in (5, 20, 40):
        key = f"recovery_pnl_pp_plus_{horizon}d"
        recovery[f"plus_{horizon}d"] = _bootstrap_mean_ci(
            [pair[key] for pair in sl_cut_rows if pair.get(key) is not None], seed
        )
    return {
        "source_rows": len(source_rows),
        "eligible_count": len(eligible_ids),
        "paired_count": len(paired_rows),
        "paired_coverage_pct": round(
            len(paired_rows) / len(eligible_ids) * 100.0, 2
        ) if eligible_ids else None,
        "sl_cut_count": len(sl_cut_rows),
        "skipped": dict(skipped),
        # Keep both ID-only and canonical-row manifests.  The latter covers
        # source metadata (e.g. regime/market_cap) used in attribution.
        "source_candidates_sha256": _rows_manifest_sha256(source_rows),
        "source_candidate_ids_sha256": _manifest_sha256(
            [
                f"{str(r.get('symbol', '')).zfill(6)}|{r.get('signal_day')}"
                f"|{r.get('signal_type', '')}"
                for r in source_rows
            ]
        ),
        "eligible_manifest_sha256": _rows_manifest_sha256(eligible_source_rows),
        "eligible_id_manifest_sha256": _manifest_sha256(eligible_ids),
        "paired_manifest_sha256": _rows_manifest_sha256(paired_rows),
        "paired_id_manifest_sha256": _manifest_sha256(
            [str(pair["candidate_id"]) for pair in paired_rows]
        ),
        "history_manifest_sha256": hashlib.sha256(
            "\n".join(
                f"{symbol}|{history_hashes[symbol]}"
                for symbol in sorted(history_hashes)
            ).encode("utf-8")
        ).hexdigest(),
        "sl_cut": {
            "n": len(sl_cut_rows),
            "baseline_pnl_pp": _bootstrap_mean_ci(pnl_baseline, seed),
            "variant_pnl_pp": _bootstrap_mean_ci(pnl_variant, seed),
            "paired_delta_pp": _bootstrap_mean_ci(deltas, seed),
            "mae_baseline_pp": _bootstrap_mean_ci(mae_baseline, seed),
            "mae_variant_pp": _bootstrap_mean_ci(mae_variant, seed),
            "mae_delta_pp": _bootstrap_mean_ci(
                [variant - base for variant, base in zip(mae_variant, mae_baseline)], seed
            ),
            "tail": {
                "p10_delta_pp": (
                    round(float(np.quantile(pnl_variant, 0.10)) - float(np.quantile(pnl_baseline, 0.10)), 4)
                    if pnl_variant else None
                ),
                "min_delta_pp": (
                    round(float(np.min(pnl_variant)) - float(np.min(pnl_baseline)), 4)
                    if pnl_variant else None
                ),
            },
            "recovery": recovery,
            "exit_reason_transitions": dict(exit_transitions),
            "delta_by_regime_signal": {
                key: _bootstrap_mean_ci(values, seed)
                for key, values in sorted(group_deltas.items())
            },
        },
    }


def _gates(report: dict[str, Any], tolerances: dict[str, float]) -> dict[str, Any]:
    train = report["splits"]["train"]["sl_cut"]
    val = report["splits"]["val"]["sl_cut"]
    test = report["splits"]["test"]["sl_cut"]

    def tail_ok(split_sl_cut: dict[str, Any]) -> bool:
        tail = split_sl_cut["tail"]
        if tail.get("p10_delta_pp") is None or tail.get("min_delta_pp") is None:
            return False
        return (
            tail["p10_delta_pp"] >= tolerances["p10_delta_pp"]
            and tail["min_delta_pp"] >= tolerances["min_delta_pp"]
        )

    checks = {
        "paired_coverage_at_least_99pct": all(
            report["splits"][split].get("paired_coverage_pct", 0) is not None
            and report["splits"][split]["paired_coverage_pct"] >= 99.0
            for split in SPLITS
        ),
        "minimum_sl_cut_sample_counts": all(
            report["splits"][split]["sl_cut"].get("n", 0)
            >= MIN_SL_CUT_COUNTS[split]
            for split in SPLITS
        ),
        "train_not_worse": train["paired_delta_pp"].get("mean", float("-inf")) >= tolerances["train_delta_pp"],
        "val_mean_gain": val["paired_delta_pp"].get("mean", float("-inf")) > 0,
        "val_ci95_low_positive": val["paired_delta_pp"].get("ci95_low", float("-inf")) > 0,
        # This is a mean-delta tolerance check, not a statistical-significance
        # test; the name is intentionally explicit.
        "test_mean_not_below_tolerance": test["paired_delta_pp"].get("mean", float("-inf")) >= tolerances["test_delta_pp"],
        "val_tail_not_expanded": tail_ok(val),
        "test_tail_not_expanded": tail_ok(test),
    }
    return {
        "tolerances": tolerances,
        "checks": checks,
        "all_pass": all(checks.values()),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    if config is None:
        raise RuntimeError(f"unable to load config: {args.config}")
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir = Path(args.history_dir).expanduser().resolve()
    result: dict[str, Any] = {
        "version": VERSION,
        "label": args.label,
        "design": {
            "fixed_entries": True,
            "fixed_other_exits": True,
            "baseline": "production_SL8_TP30",
            "variant": "no_stop_loss_diagnostic_only",
            "variant_is_production_eligible": False,
            "scope": "trades whose baseline exit_reason == stop_loss",
            "production_unchanged": True,
        },
        "config_snapshot": _config_snapshot(config),
        "seed": args.seed,
        "bootstrap": {
            "method": "iid_transaction_resample",
            "resamples": BOOTSTRAP_RESAMPLES,
            "confidence_level": 0.95,
        },
        "minimum_sl_cut_counts": MIN_SL_CUT_COUNTS,
        "gates": GATE_TOLERANCES,
        "splits": {},
    }
    result["config_snapshot_sha256"] = hashlib.sha256(
        json.dumps(result["config_snapshot"], sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    result["effective_config_sha256"] = _config_sha256(config)
    for split in SPLITS:
        source_path = input_dir / f"candidates_{split}.jsonl"
        split_result = _run_split(source_path, config, history_dir, args.seed)
        split_result["seed"] = args.seed
        split_result["config_snapshot_sha256"] = result["config_snapshot_sha256"]
        split_result["effective_config_sha256"] = result["effective_config_sha256"]
        result["splits"][split] = split_result
    result["gate_evaluation"] = _gates(result, GATE_TOLERANCES)
    if result["gate_evaluation"]["all_pass"]:
        result["verdict"] = (
            "SL8_too_tight_evidence: 无止损在所有窗口不恶化且 val 显著增益、"
            "尾部不扩大 → 进入第二阶段 SL10 单变量实验（仍为实验，生产不动）"
        )
    else:
        result["verdict"] = (
            "SL8_protective: 无止损未能通过全部闸门（train/val/test 或尾部扩大）"
            "→ 停止退出调参，转向验证 bear/range 下 macd_near 降级 observe_only；"
            "生产保持 P0/SL8/TP30/C4"
        )
    result_path = output_dir / "sl8_counterfactual_audit.json"
    with result_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=r"D:\tmp\candidates", help="frozen candidate JSONL dir")
    parser.add_argument("--output-dir", default=r"D:\tmp\sl8_counterfactual")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--history-dir", default=str(HISTORY_DIR))
    parser.add_argument("--label", default="sl8-counterfactual")
    parser.add_argument("--seed", type=int, default=20260830)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps(
        {
            "version": result["version"],
            "gate_evaluation": result["gate_evaluation"],
            "verdict": result["verdict"],
            "per_split": {
                split: {
                    "paired": result["splits"][split]["paired_count"],
                    "eligible": result["splits"][split]["eligible_count"],
                    "paired_coverage_pct": result["splits"][split]["paired_coverage_pct"],
                    "sl_cut": result["splits"][split]["sl_cut_count"],
                    "delta_mean_pp": result["splits"][split]["sl_cut"]["paired_delta_pp"].get("mean"),
                    "delta_ci95": [
                        result["splits"][split]["sl_cut"]["paired_delta_pp"].get("ci95_low"),
                        result["splits"][split]["sl_cut"]["paired_delta_pp"].get("ci95_high"),
                    ],
                    "p10_delta_pp": result["splits"][split]["sl_cut"]["tail"].get("p10_delta_pp"),
                    "min_delta_pp": result["splits"][split]["sl_cut"]["tail"].get("min_delta_pp"),
                    "eligible_manifest_sha256": result["splits"][split]["eligible_manifest_sha256"],
                    "paired_manifest_sha256": result["splits"][split]["paired_manifest_sha256"],
                }
                for split in SPLITS
            },
        },
        ensure_ascii=False, indent=1,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
