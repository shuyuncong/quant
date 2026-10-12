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
    top_divergence_flags,
)
from strategy.yearline import add_yearline_indicators, pullback_signal_at, prepare_yearline_bars
from strategy.market_gate import resolve_min_confirmations
from strategy.signal_policy import effective_signal_execution_mode, resolve_signal_execution_policy

STRATEGIES = {
    "macd_zero_axis": "日线零轴金叉",
    "yearline_pullback": "年线趋势",
    "macd_divergence": "零轴＋底背离",
}
VERSION = "2026-10-11.1"
# Risk defaults live here so a missing YAML value can never leave a position without a stop.
DEFAULT_STOP_LOSS_PCT = 0.08
STOP_LOSS_FLOOR = 0.001
STOP_LOSS_CEIL = 0.99


def _finite_fraction(value: Any, key: str) -> float:
    """Return a finite int/float, rejecting strings, bools, NaN and infinity.

    Range is checked by the caller.  A quoted YAML value or a stray boolean is a
    type error here on purpose: it must not be silently coerced into a risk level.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} 必须是数值，实际为 {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{key} 必须是有限数值，实际为 {value!r}")
    return number


def _take_profit_tiers(value: Any, key: str) -> list[dict]:
    """Parse ladder take-profit tiers into a normalized, ascending, gap-free list."""
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} 必须是数组，实际为 {value!r}")
    parsed: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{key} 的每一项必须是对象，实际为 {item!r}")
        gain = _finite_fraction(item.get("gain"), f"{key}[].gain")
        fraction = _finite_fraction(item.get("fraction"), f"{key}[].fraction")
        if gain <= 0 or gain > 10:
            raise ValueError(f"{key}[].gain 必须位于 0..10，实际为 {gain!r}")
        if fraction <= 0 or fraction > 1:
            raise ValueError(f"{key}[].fraction 必须位于 0..1，实际为 {fraction!r}")
        parsed.append({"gain": gain, "fraction": fraction})
    parsed.sort(key=lambda item: item["gain"])
    seen: set[float] = set()
    for item in parsed:
        if item["gain"] in seen:
            raise ValueError(f"{key} 存在重复的 gain={item['gain']}")
        seen.add(item["gain"])
    return parsed


def resolve_exit_plan(config: dict, strategy_id: str | None = None,
                      holding: dict | None = None) -> dict:
    """Scale-out take-profit plan: ladder levels plus an optional trailing tail.

    Mirrors :func:`resolve_stop_loss`: an absent plan keeps the historical behaviour
    (one ``take_profit_pct`` all-out level) and a *malformed* configured plan raises
    instead of quietly changing the exit.  A plan owns every take-profit level:
    ``levels`` are absolute gains over the entry price, each selling ``fraction`` of
    the *original* position.  With ``protect_tail="breakeven"`` the stop for the
    remainder moves to the entry price after the first level fills; with a trailing
    pair the remainder exits when the bar drops ``pct`` under the highest price
    reached while held.

    An explicit empty object on ``risk.strategy_exit_plan.<id>`` means "this strategy
    keeps the legacy single take-profit exit" even while a global plan is configured;
    ``null`` (or a missing key) inherits the global plan.

    Intraday precedence (a daily bar cannot order its own path, so the engine fixes one):
    stop loss → 尾仓保护 (trailing) → 下一档止盈.  A bar that breaches the tail floor
    closes the remainder, so pending ladder levels become moot; at most one ladder level
    fills per day.
    """
    risk = config.get("risk") or {}
    overrides = risk.get("strategy_exit_plan")
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError(f"risk.strategy_exit_plan 必须是对象或 null，实际为 {overrides!r}")
    raw = None
    if isinstance(overrides, dict) and strategy_id is not None:
        raw = overrides.get(strategy_id, None)
        if raw == {}:
            return {}
    plan = raw if raw is not None else risk.get("exit_plan")
    if plan is None:
        return {}
    if not isinstance(plan, dict):
        raise ValueError(f"risk.exit_plan 必须是对象或 null，实际为 {plan!r}")
    levels: list[dict] = []
    initial = _finite_fraction(plan.get("initial_fraction", 0.0), "risk.exit_plan.initial_fraction")
    if not 0.0 <= initial <= 1.0:
        raise ValueError(f"risk.exit_plan.initial_fraction 必须位于 0..1，实际为 {initial!r}")
    first = plan.get("first_gain")
    if initial > 0:
        if first is None:
            raise ValueError("risk.exit_plan.initial_fraction 大于 0 时必须提供 first_gain")
        first_gain = _finite_fraction(first, "risk.exit_plan.first_gain")
        if not 0.0 < first_gain <= 10.0:
            raise ValueError(f"risk.exit_plan.first_gain 必须位于 0..10，实际为 {first!r}")
        levels.append({"gain": first_gain, "fraction": initial})
    for tier in _take_profit_tiers(plan.get("tiers", []), "risk.exit_plan.tiers"):
        if levels and tier["gain"] <= levels[-1]["gain"]:
            raise ValueError(f"risk.exit_plan.tiers 的 gain 必须严格大于前一档，实际为 {tier['gain']!r}")
        levels.append(tier)
    protection = str(plan.get("protect_tail", "none")).strip().lower()
    if protection not in {"none", "breakeven"}:
        raise ValueError("risk.exit_plan.protect_tail 必须是 'none' 或 'breakeven'，"
                         f"实际为 {plan.get('protect_tail')!r}")
    activation = plan.get("trailing_activation_gain")
    trailing = plan.get("trailing_pct")
    if (activation is None) != (trailing is None):
        raise ValueError("risk.exit_plan.trailing_activation_gain 与 trailing_pct 必须同时提供")
    trail: dict | None = None
    if activation is not None:
        activation_gain = _finite_fraction(activation, "risk.exit_plan.trailing_activation_gain")
        trail_pct = _finite_fraction(trailing, "risk.exit_plan.trailing_pct")
        if not 0.0 < activation_gain <= 10.0:
            raise ValueError(f"risk.exit_plan.trailing_activation_gain 必须位于 0..10，实际为 {activation!r}")
        if not 0.0 < trail_pct < 1.0:
            raise ValueError(f"risk.exit_plan.trailing_pct 必须位于 0..1，实际为 {trailing!r}")
        trail = {"activation_gain": activation_gain, "pct": trail_pct}
    if not levels and trail is None:
        raise ValueError("risk.exit_plan 至少要有一个减仓档位或一组移动止盈参数")
    total = sum(level["fraction"] for level in levels)
    if total > 1 + 1e-9:
        raise ValueError(f"risk.exit_plan 各档 fraction 之和不能超过 1，实际为 {total!r}")
    return {"levels": levels, "breakeven": protection == "breakeven", "trailing": trail}


def resolve_plan_progress(holding: dict | None, levels: list[dict]) -> tuple[int, float, str]:
    """统一的"已执行几档 / 原始股数 / 基数来源"口径，供建议与止损两处共用。

    持仓台账目前没有档位字段，成交记录是唯一的进度来源；两者都拿不到时明确按 0 档处理，
    并要求调用方在文案里说明是"按当前仓位视为原始仓位"，不要假装知道真实进度。
    """
    if not holding or not levels:
        return 0, 0.0, "unknown"
    taken = max(0, min(int(holding.get("exit_levels_taken") or 0), len(levels)))
    sold_fraction = sum(float(level["fraction"]) for level in levels[:taken])
    progress = plan_progress_from_trades(holding, levels)
    if progress and progress["taken"] > taken:
        taken = progress["taken"]
        sold_fraction = sum(float(level["fraction"]) for level in levels[:taken])
        origin = "trades_estimated" if progress["estimated"] else "trades"
    elif taken:
        origin = "ledger"
    else:
        origin = "assumed_zero"
    if sold_fraction >= 1 - 1e-9:
        original = float(holding.get("shares") or 0)
    else:
        try:
            original = float(holding.get("shares") or 0) / (1 - sold_fraction)
        except (TypeError, ValueError):
            return taken, 0.0, origin
    return taken, original, origin


def plan_progress_from_trades(holding: dict | None, levels: list[dict]) -> dict | None:
    """用成交记录反推"已执行几档、原始股数多少"，替代持仓表缺失的档位字段。

    持仓台账没有 `exit_levels_taken`，只靠它会让已减仓的持仓被当成"一档都没卖"，
    于是提醒里报出重复减仓的建议。成交记录（`holding_trades`）里买/卖都在，
    按"累计卖出 ÷ 累计买入"就能还原比例，再对照计划档位比例算出已执行到第几档。

    返回 None 表示无法判断（没有成交记录），调用方必须保持"未知"而不是当成 0。
    样本不完整（`history_complete` 为假）时 `estimated` 为真，提醒里要写明是估算。
    """
    if not holding or not levels:
        return None
    trades = holding.get("trades")
    if not isinstance(trades, list) or not trades:
        return None
    bought = sold = 0.0
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        try:
            quantity = float(trade.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        # 卖出席位以 side 为准；缺失时按"有已实现盈亏"兜底，避免把卖出当成买入反向累计。
        side = str(trade.get("side") or "").strip().lower()
        is_sell = side == "sell" or (not side and trade.get("realized_pnl") not in (None, 0))
        if is_sell:
            sold += quantity
        else:
            bought += quantity
    if bought <= 0:
        return None
    # 期初持仓没有买入记录，卖出可能超过已知买入：此时无法确定原始仓位。
    if sold > bought + 1e-9:
        return None
    sold_fraction = sold / bought
    taken = 0
    cumulative = 0.0
    for level in levels:
        cumulative += float(level["fraction"])
        if sold_fraction >= cumulative - 1e-9:
            taken += 1
    return {"original_shares": bought, "sold_shares": sold, "sold_fraction": sold_fraction,
            "taken": taken, "estimated": not bool(holding.get("history_complete"))}


def exit_plan_advice(config: dict, strategy_id: str, holding: dict | None,
                     frame: pd.DataFrame | None = None) -> dict | None:
    """把分批减仓计划翻译成"今天该怎么卖"的可执行建议。

    返回 None 表示这只票当前没有可执行的分批计划（未持仓 / 未配置计划 / 计划只有移动止盈）。

    档位比例是相对**最初买入股数**，而实盘持仓表只存当前股数与已执行档位，所以原始股数按
    ``当前股数 ÷ (1 − 已执行各档比例之和)`` 反推；已执行档位缺失时按 0 处理，并在 ``basis``
    里说明基数，避免把"按当前仓位的一档"误当成"按原始仓位的一档"。
    带成交记录时优先用它反推（见 `plan_progress_from_trades`），这样已经减过仓的持仓
    不会再收到"再卖一次第一档"的提醒。
    """
    if not holding:
        return None
    plan = resolve_exit_plan(config, stop_owner(strategy_id, holding), holding)
    if not plan or not plan.get("levels"):
        return None
    try:
        cost = float(holding.get("cost_price") or 0)
        shares = float(holding.get("shares") or 0)
    except (TypeError, ValueError):
        return None
    if cost <= 0 or shares <= 0:
        return None
    symbol = str(holding.get("symbol") or "")
    lot = 1 if symbol.endswith(".BJ") else 100
    levels = plan["levels"]
    # 台账若已带档位字段就以它为准；没有（当前 schema）时交给成交记录反推。
    taken, original, origin = resolve_plan_progress(holding, levels)
    if original <= 0:
        return None
    basis = {
        "ledger": "已执行档位",
        "trades": "按成交记录还原的已执行档位",
        "trades_estimated": "按成交记录估算的已执行档位（成交记录可能不完整）",
    }.get(origin, "未记录已执行档位，按当前仓位视为原始仓位")
    rows = []
    for index, level in enumerate(levels):
        trigger = cost * (1 + level["gain"])
        quantity = 0 if index < taken else int(original * level["fraction"] / lot) * lot
        rows.append({"index": index + 1, "gain": level["gain"], "fraction": level["fraction"],
                     "trigger_price": round(trigger, 4), "quantity": quantity, "taken": index < taken})
    high = float(frame["high"].iloc[-1]) if frame is not None and len(frame) else None
    close = float(frame["close"].iloc[-1]) if frame is not None and len(frame) else None
    watch = next((row for row in rows if not row["taken"]), None)
    trailing = plan.get("trailing")
    advice = {
        "symbol": symbol, "strategy_id": strategy_id, "cost_price": round(cost, 4),
        "shares": shares, "original_shares": round(original, 2), "basis": basis,
        "levels": rows, "taken": taken, "pending": len(rows) - taken,
        "next": watch,
        "triggered_today": bool(watch and high is not None and high >= watch["trigger_price"]),
        "breakeven_active": bool(plan["breakeven"] and taken > 0),
        "breakeven_stop_price": round(cost, 4) if plan["breakeven"] and taken > 0 else None,
        "last_high": None if high is None else round(high, 4),
        "last_close": None if close is None else round(close, 4),
    }
    if trailing:
        peak, peak_basis = None, "frame"
        if frame is not None and len(frame) and "close" in frame:
            closes = frame
            opened_on = str(holding.get("opened_on") or "").strip()
            if opened_on and "datetime" in frame:
                dates = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d")
                held = frame[dates >= opened_on]
                if len(held):
                    closes, peak_basis = held, "since_opened_on"
            peak = float(closes["close"].max())
        advice["trailing"] = {"activation_gain": trailing["activation_gain"],
                              "pct": trailing["pct"], "peak_close": None if peak is None else round(peak, 4),
                              "peak_basis": peak_basis,
                              "activation_price": round(cost * (1 + trailing["activation_gain"]), 4)}
        if peak is not None:
            armed = peak >= cost * (1 + trailing["activation_gain"])
            advice["trailing"]["floor_price"] = round(peak * (1 - trailing["pct"]), 4)
            advice["trailing"]["armed"] = armed
            advice["trailing"]["triggered_today"] = armed and close is not None and close <= peak * (1 - trailing["pct"])
    advice["summary"] = _plan_summary(advice)
    return advice


def _plan_summary(advice: dict) -> str:
    """一句话说清：剩几档、下一档在哪、卖多少、保本止损到没到。"""
    # 进度是照成交记录反推的（持仓台账还没有档位字段），样本可能不完整——必须写在
    # 用户真正会读的那句话里，否则"1/2 档已执行"会被当成精确事实。
    estimated = "估算" in str(advice.get("basis") or "")
    parts = [f"分批计划 {advice['taken']}/{len(advice['levels'])} 档已执行"
             + ("（按已知成交估算）" if estimated else "")]
    watch = advice["next"]
    if watch:
        parts.append(f"下一档 +{watch['gain']:.0%} 触发价 {watch['trigger_price']:.3f}，卖出 {watch['quantity']} 股"
                     f"（原始仓位 {advice['original_shares']:.0f} 股的 {watch['fraction']:.0%}）")
    else:
        parts.append("各档均已执行，尾仓按止损/超时等规则管理")
    if advice["breakeven_active"]:
        parts.append(f"保本止损已生效：跌破成本价 {advice['breakeven_stop_price']:.3f} 卖出尾仓")
    if advice.get("trailing"):
        parts.append(f"尾仓移动止盈：激活价 {advice['trailing']['activation_price']:.3f}，"
                     f"从最高点回撤 {advice['trailing']['pct']:.0%} 清仓")
    if advice["triggered_today"] and watch:
        parts.append(f"今日日线最高 {advice['last_high']:.3f} 已触及该档")
    return "；".join(parts) + "。"


def stop_owner(strategy_id: str | None, holding: dict | None) -> str | None:
    """Which strategy owns the stop, never inferred from signals or from the assessed strategy.

    A held position is attributed to ``holding["strategy_id"]`` alone: an unknown owner
    reads as None so the global stop is used instead of borrowing the stop of whichever
    strategy happens to be under assessment.  Without a position the decision is
    hypothetical, so the strategy being assessed owns the stop.
    """
    if holding and float(holding.get("shares", 0) or 0) > 0:
        owner = holding.get("strategy_id")
        return owner if owner in STRATEGIES else None
    return strategy_id if strategy_id in STRATEGIES else None


def resolve_stop_loss(config: dict, strategy_id: str | None = None,
                      holding: dict | None = None) -> tuple[float, str, str | None]:
    """Resolve one stop loss: owning strategy override > global risk > built-in default.

    A missing global falls back to ``DEFAULT_STOP_LOSS_PCT``; a *malformed* non-null global
    or strategy override raises ``ValueError`` instead, because a typo must not silently
    change the risk a position is carried under.  A missing or explicitly null override
    inherits the global stop.  ``source`` names the winning layer and the third value is
    the owning strategy (None when no override applies), so a report can explain the stop.
    """
    risk = config.get("risk") or {}
    owner = stop_owner(strategy_id, holding)
    overrides = risk.get("strategy_stop_loss_pct")
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError(f"risk.strategy_stop_loss_pct 必须是对象或 null，实际为 {overrides!r}")
    raw = overrides.get(owner) if overrides is not None and owner is not None else None
    if raw is not None:
        value = _finite_fraction(raw, f"risk.strategy_stop_loss_pct.{owner}")
        if not STOP_LOSS_FLOOR <= value <= STOP_LOSS_CEIL:
            raise ValueError(f"risk.strategy_stop_loss_pct.{owner} 必须位于 {STOP_LOSS_FLOOR}..{STOP_LOSS_CEIL}，实际为 {raw!r}")
        return value, "strategy", owner
    configured = risk.get("stop_loss_pct")
    if configured is None:
        return DEFAULT_STOP_LOSS_PCT, "default", None
    value = _finite_fraction(configured, "risk.stop_loss_pct")
    if not STOP_LOSS_FLOOR <= value <= STOP_LOSS_CEIL:
        raise ValueError(f"risk.stop_loss_pct 必须位于 {STOP_LOSS_FLOOR}..{STOP_LOSS_CEIL}，实际为 {configured!r}")
    return value, "global", None


def condition(name, actual, expected, met):
    if isinstance(actual, float) and not math.isfinite(actual):
        actual = None
    if hasattr(actual, "item"):
        actual = actual.item()
    return {"name": name, "actual": actual, "expected": expected, "met": None if met is None else bool(met)}


def rule_parameters(config: dict, strategy_id: str, holding: dict | None = None) -> dict:
    signal = config.get("signal_strategy", {})
    pct, source, _ = resolve_stop_loss(config, strategy_id, holding)
    return {
        "macd": signal.get("macd", {}),
        "min_confirmations": resolve_min_confirmations(config),
        "divergence": resolve_divergence_config(config),
        "risk": config.get("risk", {}),
        "stop_loss": {"pct": pct, "source": source},
        "exit_rules": config.get("backtest", {}).get("exit_rules", {}),
        "max_holding_bars": config.get("backtest", {}).get("chan_zero_axis", {}).get("max_holding_bars", 40),
        "execution_policy": resolve_signal_execution_policy(config),
        "strategy_id": strategy_id,
    }


BREAKEVEN_STOP_SOURCE = "plan_breakeven"


def exit_parameters(config: dict, strategy_id: str, holding: dict | None = None) -> dict:
    risk = config.get("risk", {})
    settings = config.get("backtest", {})
    rules = settings.get("exit_rules", {})
    signal_type = "macd_divergence_bottom" if strategy_id == "macd_divergence" else strategy_id
    scope = rules.get("apply_to_signal_types", [])
    trend = rules.get("mode") == "divergence_trend" and (not scope or signal_type in scope)
    stop_loss_pct, stop_loss_source, stop_loss_owner = resolve_stop_loss(config, strategy_id, holding)
    plan = resolve_exit_plan(config, stop_owner(strategy_id, holding), holding)
    # 首档成交后尾仓止损上移到成本价：实盘持仓自己记录已执行档位，止损口径随之改变。
    # 台账没有档位字段时由成交记录反推（见 resolve_plan_progress）；
    # 回测由 replay 内部维护档位进度，不会把进度传进来，口径不受影响。
    if plan.get("breakeven") and holding is not None:
        taken, _, _ = resolve_plan_progress(holding, plan["levels"])
        if taken > 0:
            stop_loss_pct, stop_loss_source = 0.0, BREAKEVEN_STOP_SOURCE
    return {"mode": "divergence_trend" if trend else "fixed", "stop_loss_pct": stop_loss_pct,
            "stop_loss_source": stop_loss_source, "stop_loss_owner": stop_loss_owner,
            # A configured scale-out plan replaces the single take-profit level entirely.
            "take_profit_pct": None if (trend or plan) else float(risk.get("stop_profit_pct", 0.30)),
            "exit_plan": plan,
            "ma_long_period": int(rules.get("ma_long_period", 250)),
            "max_holding_bars": int(settings.get("chan_zero_axis", {}).get("max_holding_bars", 40)),
            "top_divergence": trend and rules.get("top_divergence", True),
            "below_yearline": trend and rules.get("below_yearline", True)}


def evaluate_exits(frame: pd.DataFrame, config: dict, strategy_id: str, holding: dict | None) -> tuple[list[dict], bool | None]:
    rules = exit_parameters(config, strategy_id, holding)
    current = float(frame["close"].iloc[-1]) if not frame.empty else None
    held = bool(holding and float(holding.get("shares", 0)) > 0)
    cost = float(holding.get("cost_price", 0)) if held else 0
    cost_known = cost > 0 and current is not None
    # With a scale-out plan the tail stop can move to the entry price after the first
    # level; the plan owner knows that flag, the daily rule evaluator does not, so it
    # must not invent a second fixed-take-profit exit that the plan already replaced.
    plan = rules["exit_plan"]
    conditions = [condition("持仓成本止损", current, f"成本价 × {1-rules['stop_loss_pct']:.4f}" + (f" = {cost*(1-rules['stop_loss_pct']):.3f}" if cost_known else "（成本未知）"), current <= cost*(1-rules["stop_loss_pct"]) if cost_known else None)]
    plan_advice = exit_plan_advice(config, strategy_id, holding, frame)
    if rules["take_profit_pct"] is not None:
        conditions.append(condition("固定止盈", current, f"成本价 × {1+rules['take_profit_pct']:.4f}" + (f" = {cost*(1+rules['take_profit_pct']):.3f}" if cost_known else "（成本未知）"), current >= cost*(1+rules["take_profit_pct"]) if cost_known else None))
    elif plan.get("levels"):
        # 分批减仓按"档位触发"，每个交易日最多一档；提醒层用一句话说清下一档在哪、卖多少，
        # 明细放进 details 让 AI/前端能读出具体价位与股数。met=None：它不是一次性卖出的布尔条件。
        first_gain = plan["levels"][0]["gain"]
        condition_row = condition("分批止盈", current, f"成本价 × {1+first_gain:.4f}" + (f" = {cost*(1+first_gain):.3f}" if cost_known else "（成本未知）"), None)
        if plan_advice:
            condition_row["details"] = plan_advice
            condition_row["expected"] = plan_advice["summary"]
        conditions.append(condition_row)
    elif plan.get("trailing"):
        conditions.append(condition("移动止盈", current, f"激活 +{plan['trailing']['activation_gain']:.2%}，回撤 {plan['trailing']['pct']:.2%} 清仓", None))
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
        # min_periods=period: 历史不足 250 根时 MA 视为未知, 与回测口径一致
        # (回测用 min_periods=period; 不设 min_periods 会用不足 250 根算出年线)。
        period = int(rules["ma_long_period"])
        ma = frame["close"].rolling(period, min_periods=period).mean().iloc[-1] if len(frame) else float("nan")
        known = math.isfinite(float(ma))
        conditions.append(condition("跌破年线", current, f"收盘价 < MA{period} ({ma:.3f})" if known else f"需要 {period} 根日线", current < ma if known else None))
    if rules["top_divergence"]:
        div_config = resolve_divergence_config(config)
        enriched = add_divergence_indicators(frame, div_config)
        flag = None
        if len(enriched) >= 2:
            flags, _ = top_divergence_flags(enriched["hist"], enriched["close"])
            flag = bool(flags[-1])
        conditions.append(condition("已确认日线顶背离", flag, "正柱周期完成、价格创新高且动能面积收缩", flag))
    return conditions, (any(item["met"] is True for item in conditions) if held else None)


def build_sell_signal(strategy_id: str, name: str, sell_conditions: list[dict], holding: dict | None,
                      *, as_of: str | None = None, price: float | None = None) -> dict | None:
    """把"有持仓且退出条件触发"翻译成一条可推送的卖出事件。

    只在真正触发时返回 (met is True), 且必须有持仓——没有持仓就不该提示卖出。
    返回 dict 而非 SignalEvent: 本模块是无副作用的纯决策层, 构造/投递事件由
    monitor 负责, 免得 strategy 反向依赖 models 与通知链路。
    """
    if not holding or float(holding.get("shares", 0) or 0) <= 0:
        return None
    triggered = [item for item in sell_conditions if item.get("met") is True]
    if not triggered:
        return None
    return {
        "symbol": str(holding.get("symbol", "") or ""),
        "name": str(holding.get("name") or name or ""),
        "strategy_id": strategy_id,
        "strategy_name": name,
        "primary_rule": str(triggered[0]["name"]),
        "rules": [str(item["name"]) for item in triggered],
        "signal_type": f"{strategy_id}_exit_{triggered[0]['name']}",
        "reasons": [f"{item['name']}：{item['expected']}（实际 {item['actual']}）" for item in triggered],
        "components": [f"{strategy_id}_exit_{item['name']}" for item in triggered],
        "price": float(price or 0.0),
        "as_of": str(as_of or ""),
    }


def _apply_plan_met(conditions: list[dict], close: float | None) -> None:
    """Lift the plan rows out of "informational" for the live path only.

    ``evaluate_exits`` deliberately leaves 分批止盈/移动止盈 rows unset so the replay engine
    keeps owning its own ladder; the live report knows the position's progress instead, so
    it fills ``met`` from the advice.  A scale-out level needs the bar **high** to trade, so
    a close-only comparison understates it; the row records which basis it used.
    """
    for row in conditions:
        details = row.get("details") or {}
        if row.get("name") == "分批止盈":
            row["met"] = bool(details.get("triggered_today"))
            row["trigger_basis"] = "日线最高价" if details.get("last_high") is not None else "未取得当日日线"
        elif row.get("name") == "移动止盈":
            trailing = details.get("trailing")
            if trailing and not trailing.get("armed"):
                row["met"] = False
                row["trigger_basis"] = "未激活：尚未达到激活价"
            else:
                row["met"] = bool(trailing and trailing.get("triggered_today"))
                row["trigger_basis"] = "按最高收盘价与回撤阈值比较"


def evaluate_strategies(raw: pd.DataFrame, config: dict, holding: dict | None = None, *, as_of: str | None = None, strategy_ids: list[str] | None = None) -> list[dict]:
    if as_of:
        raw = closed_bars_as_of(raw, "1d", as_of)
    frame = prepare_yearline_bars(raw)
    result = []
    for strategy_id, name in STRATEGIES.items():
        if strategy_ids is not None and strategy_id not in strategy_ids:
            continue
        parameters = rule_parameters(config, strategy_id, holding)
        exit_rules = exit_parameters(config, strategy_id, holding)
        fingerprint = hashlib.sha256(json.dumps(parameters, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        item: dict[str, Any] = {"strategy_id": strategy_id, "name": name, "version": f"{VERSION}:{fingerprint}",
            "status": "ok", "as_of": str(frame["datetime"].iloc[-1]) if len(frame) else None,
            "buy": False, "sell": None, "buy_conditions": [], "sell_conditions": [],
            "reference_price": float(frame["close"].iloc[-1]) if len(frame) else None,
            "exit_rule": exit_rules["mode"], "stop_loss": {"pct": exit_rules["stop_loss_pct"], "source": exit_rules["stop_loss_source"], "strategy_id": exit_rules["stop_loss_owner"]},
            "warnings": [], "parameters": parameters}
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
            _apply_plan_met(item["sell_conditions"], item.get("reference_price"))
            item["exit_plan_advice"] = exit_plan_advice(config, strategy_id, holding, frame)
            # 分批档位触发也是"有卖出信号"，只是卖一部分；条件明细里已写明卖多少股。
            item["sell"] = bool(item["sell"]) or any(row.get("met") is True for row in item["sell_conditions"])
            item["sell_signal"] = build_sell_signal(
                strategy_id, name, item["sell_conditions"], holding,
                as_of=item.get("as_of"), price=item.get("reference_price"),
            )
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
