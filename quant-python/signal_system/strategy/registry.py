"""Versioned, side-effect-free strategy decisions shared by analysis and replay."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import pandas as pd
from data.snapshot import closed_bars_as_of

from strategy.macd import analyze_macd
from strategy.macd_divergence import (
    add_divergence_indicators, evaluate_conditions, resolve_divergence_config,
    bottom_divergence_at,
)
from strategy.yearline import add_yearline_indicators, pullback_signal_at, prepare_yearline_bars
from strategy.market_gate import resolve_min_confirmations
from strategy.signal_policy import effective_signal_execution_mode, resolve_signal_execution_policy

STRATEGIES = {
    "macd_zero_axis": "日线零轴金叉",
    "yearline_pullback": "年线趋势",
    "macd_divergence": "零轴＋底背离",
}
VERSION = "2026-10-05.1"


def condition(name, actual, expected, met):
    if isinstance(actual, float) and not math.isfinite(actual):
        actual = None
    if hasattr(actual, "item"):
        actual = actual.item()
    return {"name": name, "actual": actual, "expected": expected, "met": None if met is None else bool(met)}


def rule_parameters(config: dict, strategy_id: str) -> dict:
    signal = config.get("signal_strategy", {})
    return {
        "macd": signal.get("macd", {}),
        "min_confirmations": resolve_min_confirmations(config),
        "divergence": resolve_divergence_config(config),
        "risk": config.get("risk", {}),
        "exit_rules": config.get("backtest", {}).get("exit_rules", {}),
        "max_holding_bars": config.get("backtest", {}).get("chan_zero_axis", {}).get("max_holding_bars", 40),
        "execution_policy": resolve_signal_execution_policy(config),
        "strategy_id": strategy_id,
    }


def exit_parameters(config: dict, strategy_id: str) -> dict:
    risk = config.get("risk", {})
    settings = config.get("backtest", {})
    rules = settings.get("exit_rules", {})
    signal_type = "macd_divergence_bottom" if strategy_id == "macd_divergence" else strategy_id
    scope = rules.get("apply_to_signal_types", [])
    trend = rules.get("mode") == "divergence_trend" and (not scope or signal_type in scope)
    return {"mode": "divergence_trend" if trend else "fixed", "stop_loss_pct": float(risk.get("stop_loss_pct", 0.08)),
            "take_profit_pct": None if trend else float(risk.get("stop_profit_pct", 0.30)),
            "max_holding_bars": int(settings.get("chan_zero_axis", {}).get("max_holding_bars", 40)),
            "top_divergence": trend and rules.get("top_divergence", True),
            "below_yearline": trend and rules.get("below_yearline", True)}


def evaluate_exits(frame: pd.DataFrame, config: dict, strategy_id: str, holding: dict | None) -> tuple[list[dict], bool | None]:
    rules = exit_parameters(config, strategy_id)
    current = float(frame["close"].iloc[-1]) if not frame.empty else None
    held = bool(holding and float(holding.get("shares", 0)) > 0)
    cost = float(holding.get("cost_price", 0)) if held else 0
    cost_known = cost > 0 and current is not None
    conditions = [condition("持仓成本止损", current, f"成本价 × {1-rules['stop_loss_pct']:.4f}" + (f" = {cost*(1-rules['stop_loss_pct']):.3f}" if cost_known else "（成本未知）"), current <= cost*(1-rules["stop_loss_pct"]) if cost_known else None)]
    if rules["take_profit_pct"] is not None:
        conditions.append(condition("固定止盈", current, f"成本价 × {1+rules['take_profit_pct']:.4f}" + (f" = {cost*(1+rules['take_profit_pct']):.3f}" if cost_known else "（成本未知）"), current >= cost*(1+rules["take_profit_pct"]) if cost_known else None))
    opened = (holding or {}).get("opened_on")
    days = None
    if opened and not frame.empty:
        dates = pd.to_datetime(frame["datetime"]).dt.date
        opened_date = pd.Timestamp(opened).date()
        # A truncated history cannot establish the holding age.
        if dates.iloc[0] <= opened_date:
            days = int((dates > opened_date).sum())
    conditions.append(condition("持仓超时", days, f"持有满 {rules['max_holding_bars']} 根日线", days >= rules["max_holding_bars"] if days is not None else None))
    if rules["below_yearline"]:
        ma = frame["close"].rolling(250).mean().iloc[-1] if len(frame) else float("nan")
        known = math.isfinite(float(ma))
        conditions.append(condition("跌破年线", current, f"收盘价 < MA250 ({ma:.3f})" if known else "需要 250 根日线", current < ma if known else None))
    if rules["top_divergence"]:
        div_config = resolve_divergence_config(config)
        enriched = add_divergence_indicators(frame, div_config)
        flag = None
        if len(enriched) >= 2:
            hist = enriched["hist"]
            completed_now = hist.iloc[-1] <= 0 < hist.iloc[-2]
            evidence = bottom_divergence_at(-hist, -enriched["high"], len(enriched)-1, div_config["min_macd_segment_bars"])
            flag = bool(completed_now and evidence["bottom_divergence"])
        conditions.append(condition("已确认日线顶背离", flag, "正柱周期完成、价格创新高且动能面积收缩", flag))
    return conditions, (any(item["met"] is True for item in conditions) if held else None)


def evaluate_strategies(raw: pd.DataFrame, config: dict, holding: dict | None = None, *, as_of: str | None = None, strategy_ids: list[str] | None = None) -> list[dict]:
    if as_of:
        raw = closed_bars_as_of(raw, "1d", as_of)
    frame = prepare_yearline_bars(raw)
    result = []
    for strategy_id, name in STRATEGIES.items():
        if strategy_ids is not None and strategy_id not in strategy_ids:
            continue
        parameters = rule_parameters(config, strategy_id)
        fingerprint = hashlib.sha256(json.dumps(parameters, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        item: dict[str, Any] = {"strategy_id": strategy_id, "name": name, "version": f"{VERSION}:{fingerprint}",
            "status": "ok", "as_of": str(frame["datetime"].iloc[-1]) if len(frame) else None,
            "buy": False, "sell": None, "buy_conditions": [], "sell_conditions": [],
            "reference_price": float(frame["close"].iloc[-1]) if len(frame) else None,
            "exit_rule": exit_parameters(config, strategy_id)["mode"], "warnings": [], "parameters": parameters}
        try:
            minimum = 60 if strategy_id == "macd_zero_axis" else 270
            if len(frame) < minimum:
                item["status"] = "insufficient_data"
                item["warnings"].append(f"至少需要 {minimum} 根已收盘日线，目前 {len(frame)} 根")
            elif strategy_id == "macd_zero_axis":
                settings = config.get("signal_strategy", {}).get("macd", {})
                keys = ("fast", "slow", "signal", "zero_axis_tolerance", "moderate_volume_min", "moderate_volume_max", "pullback_confirmation_bars", "long_ma_period", "position_lookback", "max_long_ma_distance", "max_recent_return", "high_position_volume_ratio")
                _, indicators = analyze_macd(frame, **{key: settings[key] for key in keys if key in settings})
                zone = indicators.get("golden_cross_entry_zone") or indicators.get("golden_cross_zone")
                count = int(indicators.get("confirmation_count", len(indicators.get("confirmations", []))))
                signal_type = f"macd_golden_cross_pullback_confirmed_{zone}"
                mode = effective_signal_execution_mode([signal_type], resolve_signal_execution_policy(config))
                item["buy_conditions"] = [
                    condition("金叉后回落确认", indicators.get("golden_cross_entry_ready", False), "最近收盘日线首次确认回落", indicators.get("golden_cross_entry_ready", False)),
                    condition("零轴位置", zone, "above 或 near", zone in {"above", "near"}),
                    condition("辅助确认数", count, f">= {resolve_min_confirmations(config)}", count >= resolve_min_confirmations(config)),
                    condition("策略执行状态", mode, "enabled", mode == "enabled"),
                ]
                item["buy"] = all(row["met"] for row in item["buy_conditions"])
            elif strategy_id == "yearline_pullback":
                enriched = add_yearline_indicators(frame)
                row = enriched.iloc[-1]
                fields = {"ma250_up": "年线上行", "ma_align": "MA60 > MA120 > MA250", "above250_prev": "前一日收盘在年线上方", "low_in_zone": "最低价进入年线 -0.5% 至 +2% 区间", "close_hold": "收盘守住年线", "close_ge_open": "收阳或收平"}
                item["buy_conditions"] = [condition(label, bool(row[key]), "满足", bool(row[key])) for key, label in fields.items()]
                item["buy"] = pullback_signal_at(enriched, len(enriched)-1) is not None
            else:
                settings = resolve_divergence_config(config)
                enriched = add_divergence_indicators(frame, settings)
                conditions = evaluate_conditions(enriched, len(enriched)-1, settings)
                fields = {"golden_cross": "当前日线金叉", "zero_axis_ok": "零轴附近或上方", "divergence_ok": "已完成负柱周期底背离", "volume_ok": f"量比 >= {settings['min_volume_ratio']}", "above_yearline": "年线上方且年线上行"}
                item["buy_conditions"] = [condition(label, conditions[key], "满足", conditions[key]) for key, label in fields.items()]
                item["buy"] = bool(conditions["all"])
                if not settings["enabled"]:
                    item["status"] = "disabled"
                    item["buy"] = False
            item["sell_conditions"], item["sell"] = evaluate_exits(frame, config, strategy_id, holding)
            if not holding:
                item["warnings"].append("未持仓：卖出条件仅作建仓后的退出规则参考")
            elif not holding.get("opened_on"):
                item["warnings"].append("缺少完整买入日期，持仓超时状态未知")
            item["warnings"].append("日线收盘确认；候选入池及通知还须通过资金、市场环境和风险约束")
        except Exception as exc:
            item.update(status="error", buy=False, sell=None)
            item["warnings"].append(str(exc))
        result.append(item)
    return result
