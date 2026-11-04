"""ETF v5.1 extended research: gate ablation, vol-8 top2, risk diagnostics, public controls.

Research only. Reads a local qfq ETF cache, never touches production config,
never places orders. One variable changes per experiment.

Experiments
-----------
A  market-gate ablation          baseline (gate on) vs gate off, all else equal
B  vol-8 top2                   8% vol target, max_picks 1 vs 2
C  concentration / correlation  per-rebalance HHI, group HHI, pairwise corr
D  candidate crowding           candidate breadth, score gap, rank overlap
E  public controls              Faber-, GEM-, VAA-, DAA-style, raw and vol-matched

Usage
-----
    python etf_extended_study.py --data-dir D:/tmp/etf_universe_data \
        --out ../../docs/backtest-results/2026-11-04-etf-extended-study

Parity gate: the baseline variants must reproduce the published v5.1 numbers
(§8.5.3 of docs/plans/2026-11-01-行业ETF核心卫星轮动方案.md) within 0.02pp.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

INITIAL = 250_000.0
COMMISSION = 0.0003
SLIPPAGE = 0.0005
MIN_COMMISSION = 5.0
LOT = 100
ADV_MIN = 50_000_000.0
CORE_W = 0.40
SAT_MAX = 0.35
BACKFILL_WEIGHT = 0.35
BACKFILL_GOLD = 0.40
BACKFILL_BOND = 0.60
GOLD, BOND, MARKET = "518880", "511260", "510300"
DEFAULT_START, DEFAULT_END = "2019-01-01", "2026-10-30"
DESIGN_END = "2023-12-31"
OOS_START = "2024-01-01"

# Frozen satellite pool: every sector/theme ETF except the core legs, the gate,
# and the qfq series flagged as anomalous by the plan (§8.5.3).
SECTOR = {
    "512880": "证券", "512170": "医疗", "512010": "医药", "512660": "军工",
    "588000": "科创50", "512480": "半导体", "512760": "芯片", "518880": "黄金",
    "159869": "游戏", "512800": "银行", "159928": "消费", "512980": "传媒",
    "515790": "光伏", "516160": "新能源", "159825": "农业", "159611": "电力",
    "515210": "钢铁", "512200": "房地产", "512580": "环保", "515170": "食品",
    "159996": "家电", "516110": "汽车", "515070": "人工智能", "159770": "机器人",
    "516510": "云计算", "515880": "通信", "515050": "5G", "512720": "计算机",
    "515230": "软件", "515000": "科技", "159997": "电子", "159992": "创新药",
    "159647": "中药", "159865": "养殖", "159766": "旅游", "516970": "基建",
    "159745": "建材", "159870": "化工", "159886": "机械", "516780": "稀土",
    "510880": "红利", "512890": "低波", "512040": "价值", "159967": "成长",
    "159915": "创业板", "512100": "中证1000",
}
EXCLUDE = {GOLD, BOND, MARKET, "510880", "512690", "515220", "512400"}
SATELLITE = {s: n for s, n in SECTOR.items() if s not in EXCLUDE}

# Coarse industry map: versioned, fixed before any result was inspected.
COARSE_GROUP = {
    "512480": "科技硬件", "512760": "科技硬件", "159997": "科技硬件",
    "512720": "软件互联网", "515230": "软件互联网", "516510": "软件互联网",
    "515070": "软件互联网", "159869": "软件互联网", "512980": "软件互联网",
    "515880": "通信", "515050": "通信",
    "512010": "医药", "512170": "医药", "159992": "医药", "159647": "医药",
    "159928": "消费", "515170": "消费", "159996": "消费", "159766": "消费",
    "159865": "消费", "159825": "消费",
    "512800": "金融地产", "512880": "金融地产", "512200": "金融地产",
    "515210": "周期资源", "159870": "周期资源", "516780": "周期资源",
    "512660": "工业制造", "159886": "工业制造", "159770": "工业制造",
    "516970": "工业制造", "159745": "工业制造",
    "515790": "新能源", "516160": "新能源", "159611": "新能源",
    "516110": "新能源",
    "512580": "工业制造",
    "588000": "宽基与风格", "159915": "宽基与风格", "512100": "宽基与风格",
    "512890": "宽基与风格", "512040": "宽基与风格", "159967": "宽基与风格",
    "515000": "宽基与风格",
}

PARITY = {
    # label: (total_return_pct, sharpe, max_drawdown_pct)
    "v5.1_top1_breadth_vol12": (97.49, 1.07, -11.13),
    "v5.1_top2_breadth_vol12": (72.63, 0.88, -12.98),
    "v5.1_top1_breadth_vol10": (92.14, 1.13, -9.91),
    "v5.1_top2_breadth_vol10": (69.73, 0.92, -11.50),
    "v5.1_top1_breadth_vol8": (84.13, 1.20, -8.65),
}
PARITY_TOL = 0.02


class StudyError(RuntimeError):
    """Raised when the local cache or a study precondition is unusable."""


# --------------------------------------------------------------------------- data


def load_panels(data_dir: Path) -> tuple[dict[str, str], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not data_dir.exists():
        raise StudyError(f"数据目录不存在: {data_dir}")
    symbols = [*SATELLITE, MARKET, GOLD, BOND]
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = data_dir / f"{symbol}_qfq.pkl"
        if not path.exists():
            raise StudyError(f"缺少行情缓存: {path}")
        frame = pd.read_pickle(path)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise StudyError(f"行情为空: {path}")
        frame = frame[frame["close"].gt(0)].copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        frames[symbol] = frame.set_index("datetime").sort_index()
    master = frames[MARKET].index
    close = pd.DataFrame({s: f["close"].reindex(master).ffill() for s, f in frames.items()})
    open_ = pd.DataFrame({s: f["open"].reindex(master).ffill() for s, f in frames.items()})
    volume = pd.DataFrame({s: f["volume"].reindex(master).ffill() for s, f in frames.items()})
    return dict(SATELLITE), close, open_, volume


def build_panels(close: pd.DataFrame, volume: pd.DataFrame) -> dict[str, pd.DataFrame]:
    csi = close[MARKET]
    rets = close.pct_change()
    ma60 = close.rolling(60).mean()
    ma200 = close.rolling(200).mean()
    return {
        "close": close,
        "rets": rets,
        "ma60": ma60,
        "ma200": ma200,
        "ma200_prev": ma200.shift(20),
        "adv20": (close * volume * 100.0).rolling(20).mean(),
        "mom": close.shift(21) / close.shift(252) - 1.0,
        "vol60": rets.rolling(60).std() * math.sqrt(252.0),
        "ret60": close / close.shift(60) - 1.0,
        "valid": close.notna().cumsum(),
        "csi_ma20": csi.rolling(20).mean(),
        "csi_ma200": csi.rolling(200).mean(),
        "csi_high252": csi.rolling(252).max(),
    }


def build_signals(close: pd.DataFrame, satellite: dict[str, str],
                  panels: dict[str, pd.DataFrame], dates: pd.Index) -> dict[pd.Timestamp, dict[str, Any]]:
    """Day-level state. Candidates are always computed; gating happens at target time."""
    cache: dict[pd.Timestamp, dict[str, Any]] = {}
    for day in dates:
        csi_close = float(close[MARKET].loc[day])
        csi_ma200 = float(panels["csi_ma200"].loc[day])
        gate = bool(np.isfinite(csi_ma200) and csi_close > csi_ma200)

        high = float(panels["csi_high252"].loc[day])
        csi_ma20 = float(panels["csi_ma20"].loc[day])
        dd = csi_close / high - 1.0 if np.isfinite(high) and high > 0 else 0.0
        crash = bool(dd <= -0.20 and np.isfinite(csi_ma20) and csi_close > csi_ma20)

        core_mom: dict[str, float] = {}
        for symbol in (GOLD, BOND):
            mom = float(panels["mom"][symbol].loc[day])
            if np.isfinite(mom) and mom > 0:
                core_mom[symbol] = mom

        candidates: list[tuple[float, str, float, float]] = []
        for symbol in satellite:
            if int(panels["valid"][symbol].loc[day]) < 253:
                continue
            adv = float(panels["adv20"][symbol].loc[day])
            if not np.isfinite(adv) or adv < ADV_MIN:
                continue
            price = float(close[symbol].loc[day])
            ma60 = float(panels["ma60"][symbol].loc[day])
            ma200 = float(panels["ma200"][symbol].loc[day])
            ma200_prev = float(panels["ma200_prev"][symbol].loc[day])
            ret60 = float(panels["ret60"][symbol].loc[day])
            if not (
                np.isfinite(ma60) and np.isfinite(ma200) and np.isfinite(ma200_prev)
                and np.isfinite(ret60) and price > ma200 and ma200 > ma200_prev
                and price > ma60 and ma60 > ma200 and ret60 > 0
            ):
                continue
            mom = float(panels["mom"][symbol].loc[day])
            vol = float(panels["vol60"][symbol].loc[day])
            if not np.isfinite(mom) or not np.isfinite(vol) or mom <= 0 or vol <= 0:
                continue
            candidates.append((mom / vol, symbol, mom, vol))
        candidates.sort(key=lambda row: (-row[0], row[1]))
        cache[day] = {
            "gate": gate,
            "crash": crash,
            "drawdown": dd,
            "core_mom": core_mom,
            "candidates": candidates,
        }
    return cache


# ---------------------------------------------------------------------- targeting


def make_target(sig: dict[str, Any], panels: dict[str, pd.DataFrame], day: pd.Timestamp, *,
                max_picks: int, target_vol: float | None, breadth: bool,
                crash_overlay: bool, use_market_gate: bool = True,
                sat_max: float = SAT_MAX, core_mode: str = "winner",
                core_w: float = CORE_W, backfill: bool = False) -> tuple[dict[str, float], dict[str, Any]]:
    gate = bool(sig["gate"])
    crash = bool(sig["crash"] and crash_overlay)
    core_mom = sig["core_mom"]
    core = max(core_mom, key=core_mom.get) if core_mom else None

    allowed = use_market_gate and not (gate or crash)
    candidates = [] if allowed else sig["candidates"]
    n_trend = len(candidates)
    picks = [row[1] for row in candidates[:max_picks]]

    if crash:
        core_budget = 0.30 if core else 0.0
        sat_budget = 0.70 * (min(1.0, n_trend / 2.0) if breadth else 1.0)
    elif gate or not use_market_gate:
        core_budget = core_w if core else 0.0
        sat_budget = sat_max * (min(1.0, n_trend / 2.0) if breadth else 1.0)
    else:
        core_budget = core_w if core else 0.0
        sat_budget = 0.0

    weights: dict[str, float] = {}
    if core:
        if core_mode == "equal" and len(core_mom) > 1:
            for symbol in core_mom:
                weights[symbol] = core_budget / len(core_mom)
        else:
            weights[core] = core_budget
    if sat_budget > 0 and picks:
        for symbol in picks:
            weights[symbol] = weights.get(symbol, 0.0) + sat_budget / len(picks)
    elif backfill:
        weights[GOLD] = weights.get(GOLD, 0.0) + BACKFILL_WEIGHT * BACKFILL_GOLD
        weights[BOND] = weights.get(BOND, 0.0) + BACKFILL_WEIGHT * BACKFILL_BOND

    raw_weights = dict(weights)
    scale = 1.0
    if target_vol is not None and weights and not crash:
        scale = vol_scale_for(panels, day, weights, target_vol)
        if scale < 1.0:
            weights = {s: w * scale for s, w in weights.items()}

    return weights, {
        "gate": gate,
        "crash": crash,
        "core": core,
        "core_mom": {s: round(v, 6) for s, v in core_mom.items()},
        "n_trend": n_trend,
        "satellite_budget": round(float(sat_budget), 6),
        "candidates": candidates,
        "picks": picks,
        "n_picks": len(picks),
        "raw_weights": {s: round(w, 6) for s, w in raw_weights.items()},
        "weights": {s: round(w, 6) for s, w in weights.items()},
        "vol_scale": round(scale, 6),
    }


def vol_scale_for(panels: dict[str, pd.DataFrame], day: pd.Timestamp,
                  weights: dict[str, float], target_vol: float) -> float:
    cols = list(weights)
    sample = panels["rets"].loc[:day].tail(60)[cols].dropna()
    if len(sample) < 20:
        return 1.0
    vector = np.array([weights[s] for s in cols], dtype=float)
    covariance = sample.cov().to_numpy() * 252.0
    portfolio_vol = float(np.sqrt(max(vector @ covariance @ vector, 0.0)))
    if not np.isfinite(portfolio_vol) or portfolio_vol <= target_vol:
        return 1.0
    return float(target_vol / portfolio_vol)


# ---------------------------------------------------------------------- execution


def fee_for(notional: float, slippage: float) -> float:
    return max(notional * COMMISSION, MIN_COMMISSION) + notional * slippage


@dataclass
class RunResult:
    label: str
    start: str
    end: str
    curve: pd.Series
    exposures: pd.Series
    trace: list[dict[str, Any]]
    trades: int
    turnover: float
    slippage: float

    def metrics(self) -> dict[str, Any]:
        series = self.curve
        rets = series.pct_change().dropna()
        years = (series.index[-1] - series.index[0]).days / 365.25
        total = series.iloc[-1] / INITIAL - 1.0
        cagr = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 else float("nan")
        std = float(rets.std())
        sharpe = float(rets.mean()) / std * math.sqrt(252) if std > 0 else float("nan")
        maxdd = float((series / series.cummax() - 1.0).min())
        yearly_series = series.resample("YE").last()
        yearly = yearly_series.pct_change()
        if len(yearly):
            yearly.iloc[0] = yearly_series.iloc[0] / INITIAL - 1.0
        return {
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "total_return_pct": round(total * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "vol_pct": round(std * math.sqrt(252) * 100, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(maxdd * 100, 2),
            "avg_exposure": round(float(self.exposures.mean()), 2),
            "trades": self.trades,
            "turnover_x": round(self.turnover, 2),
            "slippage_bp": int(round(self.slippage * 10000)),
            "yearly": {str(int(getattr(k, "year", k))): round(v * 100, 2) for k, v in yearly.items()},
        }


def rebalance_days(dates: pd.Index, rebalance: str) -> set[pd.Timestamp]:
    period = "W" if rebalance == "W" else "M"
    marks = pd.Series(dates, index=dates).groupby(dates.to_period(period)).max()
    return set(marks.tolist())


def execute(close: pd.DataFrame, open_: pd.DataFrame, *, dates: pd.Index,
            target_fn: Callable[[pd.Timestamp], dict[str, float]],
            rebalance: str, slippage: float, label: str,
            trace_fn: Callable[[pd.Timestamp], dict[str, Any]] | None = None) -> RunResult:
    """Daily mark-to-market with next-open execution, sells before buys."""
    if len(dates) < 30:
        raise StudyError(f"{label}: 样本过短 ({len(dates)} 天)")
    marks = rebalance_days(dates, rebalance)
    positions: dict[str, dict[str, Any]] = {}
    cash = INITIAL
    pending: dict[str, float] | None = None
    curve: list[tuple[pd.Timestamp, float]] = []
    exposures: list[float] = []
    trace: list[dict[str, Any]] = []
    trades = 0
    turnover = 0.0

    for index, day in enumerate(dates):
        if pending is not None:
            equity_open = cash + sum(p["qty"] * float(open_[s].loc[day]) for s, p in positions.items())
            targets = {s: equity_open * w for s, w in pending.items()}
            for symbol in sorted(list(positions)):
                price = float(open_[symbol].loc[day])
                if not np.isfinite(price) or price <= 0:
                    continue
                current = positions[symbol]["qty"]
                target_value = targets.get(symbol, 0.0)
                if current * price <= target_value:
                    continue
                qty = int((current * price - target_value) / price / LOT) * LOT
                if target_value <= 0:
                    qty = current
                qty = min(qty, current)
                if qty <= 0:
                    continue
                notional = qty * price
                cash += notional - fee_for(notional, slippage)
                positions[symbol]["qty"] -= qty
                trades += 1
                turnover += notional / INITIAL
                if positions[symbol]["qty"] <= 0:
                    del positions[symbol]
            for symbol, target_value in sorted(targets.items()):
                price = float(open_[symbol].loc[day])
                if not np.isfinite(price) or price <= 0:
                    continue
                current = positions.get(symbol, {}).get("qty", 0)
                delta = target_value - current * price
                if delta <= price * LOT * 0.5:
                    continue
                qty = int(delta / price / LOT) * LOT
                if qty <= 0:
                    continue
                qty = min(qty, int(cash / price / LOT) * LOT)
                while qty > 0 and qty * price + fee_for(qty * price, slippage) > cash:
                    qty -= LOT
                if qty <= 0:
                    continue
                notional = qty * price
                old = positions.get(symbol)
                if old:
                    old["entry_price"] = (old["entry_price"] * old["qty"] + price * qty) / (old["qty"] + qty)
                    old["qty"] += qty
                else:
                    positions[symbol] = {"qty": qty, "entry_price": price, "entry_day": day}
                cash -= notional + fee_for(notional, slippage)
                trades += 1
                turnover += notional / INITIAL
            pending = None

        equity = cash + sum(p["qty"] * float(close[s].loc[day]) for s, p in positions.items())
        invested = sum(p["qty"] * float(close[s].loc[day]) for s, p in positions.items())
        curve.append((day, equity))
        exposures.append(invested / equity if equity > 0 else 0.0)

        if day in marks:
            weights = target_fn(day)
            if trace_fn is not None:
                trace.append(trace_fn(day))
            if index + 1 < len(dates):
                pending = weights

    series = pd.Series(dict(curve)).sort_index()
    return RunResult(
        label=label, start=str(dates[0].date()), end=str(dates[-1].date()),
        curve=series, exposures=pd.Series(exposures, index=dates), trace=trace,
        trades=trades, turnover=turnover, slippage=slippage,
    )


def run_v5(close: pd.DataFrame, open_: pd.DataFrame, panels: dict[str, pd.DataFrame],
           signals: dict[pd.Timestamp, dict[str, Any]], *, label: str,
           start: str, end: str, max_picks: int, target_vol: float | None,
           breadth: bool, crash_overlay: bool = False, use_market_gate: bool = True,
           sat_max: float = SAT_MAX, core_mode: str = "winner",
           core_w: float = CORE_W, backfill: bool = False,
           slippage: float = SLIPPAGE, rebalance: str = "W",
           keep_trace: bool = False) -> RunResult:
    dates = close.loc[pd.Timestamp(start):pd.Timestamp(end)].index
    memo: dict[pd.Timestamp, dict[str, Any]] = {}

    def target(day: pd.Timestamp) -> dict[str, float]:
        weights, meta = make_target(
            signals[day], panels, day, max_picks=max_picks, target_vol=target_vol,
            breadth=breadth, crash_overlay=crash_overlay, use_market_gate=use_market_gate,
            sat_max=sat_max, core_mode=core_mode, core_w=core_w, backfill=backfill,
        )
        memo[day] = {"day": day, **meta}
        return weights

    def trace(day: pd.Timestamp) -> dict[str, Any]:
        return memo[day]

    return execute(close, open_, dates=dates, target_fn=target, rebalance=rebalance,
                   slippage=slippage, label=label,
                   trace_fn=trace if keep_trace else None)


# -------------------------------------------------------------------- diagnostics


def _weights_of(row: dict[str, Any]) -> dict[str, float]:
    return {s: float(w) for s, w in (row.get("weights") or {}).items() if float(w) > 0}


def _hhi(weights: dict[str, float]) -> float:
    total = sum(weights.values())
    if total <= 0:
        return float("nan")
    return sum((w / total) ** 2 for w in weights.values())


def _forward_return(close: pd.DataFrame, symbol: str, day: pd.Timestamp, horizon: int) -> float | None:
    index = close.index
    position = index.get_loc(day)
    target = position + horizon
    if target >= len(index):
        return None
    start = float(close[symbol].iloc[position])
    end = float(close[symbol].iloc[target])
    if not np.isfinite(start) or not np.isfinite(end) or start <= 0:
        return None
    return end / start - 1.0


def _pairwise(rets: pd.DataFrame) -> tuple[float, float]:
    if rets.shape[1] < 2:
        return float("nan"), float("nan")
    corr = rets.corr().to_numpy()
    upper = corr[np.triu_indices_from(corr, k=1)]
    upper = upper[np.isfinite(upper)]
    if upper.size == 0:
        return float("nan"), float("nan")
    return float(upper.mean()), float(upper.max())


def _portfolio_vol(panels: dict[str, pd.DataFrame], day: pd.Timestamp,
                   weights: dict[str, float]) -> float:
    cols = list(weights)
    sample = panels["rets"].loc[:day].tail(60)[cols].dropna()
    if len(sample) < 20:
        return float("nan")
    vector = np.array([weights[s] for s in cols], dtype=float)
    covariance = sample.cov().to_numpy() * 252.0
    return float(np.sqrt(max(vector @ covariance @ vector, 0.0)))


def _forward_vol(panels: dict[str, pd.DataFrame], day: pd.Timestamp,
                 weights: dict[str, float], horizon: int) -> float:
    """Realised annualised vol of the target basket over the next `horizon` days."""
    cols = list(weights)
    index = panels["rets"].index
    position = index.get_loc(day)
    window = panels["rets"].iloc[position + 1:position + 1 + horizon][cols].dropna()
    if len(window) < max(5, horizon // 4):
        return float("nan")
    vector = np.array([weights[s] for s in cols], dtype=float)
    return float((window.to_numpy() @ vector).std() * math.sqrt(252.0))


def diagnostics(close: pd.DataFrame, panels: dict[str, pd.DataFrame],
                trace: list[dict[str, Any]], *, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_candidates: set[str] | None = None
    for row in trace:
        day = row["day"]
        weights = _weights_of(row)
        risky = {s: w for s, w in weights.items()}
        hhi = _hhi(risky)
        effective = 1.0 / hhi if np.isfinite(hhi) and hhi > 0 else float("nan")
        total = sum(risky.values())
        core_w = sum(w for s, w in risky.items() if s in (GOLD, BOND))
        sat_w = sum(w for s, w in risky.items() if s in SATELLITE)
        ranked = sorted(risky.values(), reverse=True)
        group_weights: dict[str, float] = {}
        for symbol, weight in risky.items():
            group = COARSE_GROUP.get(symbol, "核心与防守" if symbol in (GOLD, BOND) else "其他")
            group_weights[group] = group_weights.get(group, 0.0) + weight
        group_hhi = _hhi(group_weights)
        group_ranked = sorted(group_weights.values(), reverse=True)

        held = sorted(risky)
        sample_cols = held + ([MARKET] if MARKET not in held else [])
        sample = panels["rets"].loc[:day].tail(60)[sample_cols].dropna() if sample_cols else pd.DataFrame()
        avg_corr, max_corr = _pairwise(sample[held] if held else pd.DataFrame())
        market_corr = float("nan")
        if held and len(sample) >= 20 and MARKET in sample.columns:
            market = sample[MARKET]
            weighted = 0.0
            weight_sum = 0.0
            for symbol in held:
                pair = pd.concat([sample[symbol], market], axis=1).dropna()
                if len(pair) < 20:
                    continue
                value = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
                if np.isfinite(value):
                    weighted += value * risky[symbol]
                    weight_sum += risky[symbol]
            if weight_sum > 0:
                market_corr = weighted / weight_sum

        picks = list(row.get("picks") or [])
        pick_groups = {COARSE_GROUP.get(symbol, "其他") for symbol in picks}
        candidates = list(row.get("candidates") or [])
        candidate_symbols = [item[1] for item in candidates]
        candidate_set = set(candidate_symbols)
        overlap = float("nan")
        if previous_candidates is not None:
            union = candidate_set | previous_candidates
            overlap = len(candidate_set & previous_candidates) / len(union) if union else float("nan")
        previous_candidates = candidate_set

        candidate_corr = float("nan")
        high_corr_share = float("nan")
        if len(candidate_symbols) >= 2:
            cand_sample = panels["rets"].loc[:day].tail(60)[candidate_symbols].dropna()
            if len(cand_sample) >= 20:
                corr = cand_sample.corr().to_numpy()
                upper = corr[np.triu_indices_from(corr, k=1)]
                upper = upper[np.isfinite(upper)]
                if upper.size:
                    candidate_corr = float(upper.mean())
                    high_corr_share = float((upper >= 0.7).mean())

        forward = [value for value in (_forward_return(close, s, day, 20) for s in picks) if value is not None]
        basket_forward = float(np.mean(forward)) if forward else None
        forward_5 = [value for value in (_forward_return(close, s, day, 5) for s in picks) if value is not None]
        basket_forward_5 = float(np.mean(forward_5)) if forward_5 else None

        predicted_vol = _portfolio_vol(panels, day, risky) if risky else float("nan")
        realized_20 = _forward_vol(panels, day, risky, 20) if risky else float("nan")
        realized_60 = _forward_vol(panels, day, risky, 60) if risky else float("nan")
        hhi_with_cash = _hhi({**risky, "CASH": max(0.0, 1.0 - total)})

        rows.append({
            "variant": label,
            "day": str(day.date()),
            "gate": bool(row.get("gate")),
            "crash": bool(row.get("crash")),
            "core": row.get("core"),
            "picks": " ".join(picks),
            "n_picks": len(picks),
            "exposure": round(total, 4),
            "cash": round(max(0.0, 1.0 - total), 4),
            "core_weight": round(core_w, 4),
            "satellite_weight": round(sat_w, 4),
            "asset_hhi": round(hhi, 4) if np.isfinite(hhi) else None,
            "asset_hhi_with_cash": round(hhi_with_cash, 4) if np.isfinite(hhi_with_cash) else None,
            "predicted_vol": round(predicted_vol, 4) if np.isfinite(predicted_vol) else None,
            "realized_vol_20d": round(realized_20, 4) if np.isfinite(realized_20) else None,
            "realized_vol_60d": round(realized_60, 4) if np.isfinite(realized_60) else None,
            "vol_realisation_ratio": (
                round(realized_20 / predicted_vol, 3)
                if np.isfinite(realized_20) and np.isfinite(predicted_vol) and predicted_vol > 0 else None
            ),
            "effective_assets": round(effective, 3) if np.isfinite(effective) else None,
            "max_weight": round(ranked[0], 4) if ranked else 0.0,
            "top2_weight": round(sum(ranked[:2]), 4),
            "group_hhi": round(group_hhi, 4) if np.isfinite(group_hhi) else None,
            "max_group_weight": round(group_ranked[0], 4) if group_ranked else None,
            "top2_group_weight": round(sum(group_ranked[:2]), 4) if group_ranked else None,
            "distinct_pick_groups": len(pick_groups) if picks else 0,
            "picks_same_group": bool(len(pick_groups) == 1) if len(picks) >= 2 else None,
            "avg_holding_corr": round(avg_corr, 4) if np.isfinite(avg_corr) else None,
            "max_holding_corr": round(max_corr, 4) if np.isfinite(max_corr) else None,
            "market_corr": round(market_corr, 4) if np.isfinite(market_corr) else None,
            "vol_scale": row.get("vol_scale"),
            "n_candidates": len(candidates),
            "candidate_ratio": round(len(candidates) / len(SATELLITE), 4),
            "candidate_corr": round(candidate_corr, 4) if np.isfinite(candidate_corr) else None,
            "high_corr_share": round(high_corr_share, 4) if np.isfinite(high_corr_share) else None,
            "top_score_gap": round(float(candidates[0][0] - candidates[1][0]), 4) if len(candidates) >= 2 else None,
            "rank_overlap": round(overlap, 4) if np.isfinite(overlap) else None,
            "basket_forward_20d_pct": round(basket_forward * 100, 3) if basket_forward is not None else None,
            "basket_forward_5d_pct": round(basket_forward_5 * 100, 3) if basket_forward_5 is not None else None,
        })
    return rows


def summarise_diagnostics(rows: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return {"variant": label, "points": 0}

    def stat(column: str) -> dict[str, Any]:
        series = pd.to_numeric(frame[column], errors="coerce").dropna()
        if series.empty:
            return {"mean": None, "p05": None, "p95": None}
        return {
            "mean": round(float(series.mean()), 4),
            "p05": round(float(series.quantile(0.05)), 4),
            "p95": round(float(series.quantile(0.95)), 4),
        }

    satellite = pd.to_numeric(frame["satellite_weight"], errors="coerce")
    return {
        "variant": label,
        "points": int(len(frame)),
        "share_days_gate_open": round(float(frame["gate"].mean()), 4),
        "share_days_satellite": round(float((satellite > 0).mean()), 4),
        "asset_hhi": stat("asset_hhi"),
        "effective_assets": stat("effective_assets"),
        "max_weight": stat("max_weight"),
        "group_hhi": stat("group_hhi"),
        "max_group_weight": stat("max_group_weight"),
        "avg_holding_corr": stat("avg_holding_corr"),
        "max_holding_corr": stat("max_holding_corr"),
        "market_corr": stat("market_corr"),
        "predicted_vol": stat("predicted_vol"),
        "realized_vol_20d": stat("realized_vol_20d"),
        "realized_vol_60d": stat("realized_vol_60d"),
        "vol_realisation_ratio": stat("vol_realisation_ratio"),
        "candidate_count": stat("n_candidates"),
        "candidate_corr": stat("candidate_corr"),
        "high_corr_share": stat("high_corr_share"),
        "rank_overlap": stat("rank_overlap"),
        "basket_forward_20d_pct": stat("basket_forward_20d_pct"),
        "top2_same_group_rate": (
            round(float(frame["picks_same_group"].dropna().mean()), 4)
            if frame["picks_same_group"].notna().any() else None
        ),
    }


# ---------------------------------------------------------------------- public arms


def aggregate_score(close: pd.DataFrame, day: pd.Timestamp, symbols: list[str]) -> dict[str, float]:
    """VAA/DAA 13612W momentum: equal-weight mean of 1/3/6/12 month returns."""
    scores: dict[str, float] = {}
    for symbol in symbols:
        values = []
        for window in (21, 63, 126, 252):
            series = close[symbol]
            position = series.index.get_loc(day)
            if position - window < 0:
                values = []
                break
            past = float(series.iloc[position - window])
            now = float(series.iloc[position])
            if not np.isfinite(past) or not np.isfinite(now) or past <= 0:
                values = []
                break
            values.append(now / past - 1.0)
        if values:
            scores[symbol] = float(np.mean(values))
    return scores


def faber_targets(close: pd.DataFrame, panels: dict[str, pd.DataFrame],
                  universe: list[str], *, vol_target: float | None,
                  rebalance: str = "M") -> Callable[[pd.Timestamp], dict[str, float]]:
    def target(day: pd.Timestamp) -> dict[str, float]:
        passed = []
        for symbol in universe:
            price = float(close[symbol].loc[day])
            ma = float(panels["ma200"][symbol].loc[day])
            if np.isfinite(price) and np.isfinite(ma) and price > ma:
                passed.append(symbol)
        weights = {symbol: 1.0 / len(passed) for symbol in passed} if passed else {}
        if vol_target is not None and weights:
            scale = vol_scale_for(panels, day, weights, vol_target)
            if scale < 1.0:
                weights = {s: w * scale for s, w in weights.items()}
        return weights
    return target


def gem_targets(close: pd.DataFrame, panels: dict[str, pd.DataFrame], *,
                vol_target: float | None) -> Callable[[pd.Timestamp], dict[str, float]]:
    def target(day: pd.Timestamp) -> dict[str, float]:
        risk = float(panels["mom"][MARKET].loc[day])
        safe = float(panels["mom"][BOND].loc[day])
        if np.isfinite(risk) and np.isfinite(safe) and risk > 0 and risk > safe:
            weights = {MARKET: 1.0}
        elif np.isfinite(safe) and safe > 0:
            weights = {BOND: 1.0}
        else:
            weights = {}
        if vol_target is not None and weights:
            scale = vol_scale_for(panels, day, weights, vol_target)
            if scale < 1.0:
                weights = {s: w * scale for s, w in weights.items()}
        return weights
    return target


def vaa_targets(close: pd.DataFrame, panels: dict[str, pd.DataFrame], *,
                top_n: int = 4, vol_target: float | None,
                breadth_canary: bool) -> Callable[[pd.Timestamp], dict[str, float]]:
    aggressive = [*SATELLITE, MARKET]
    defensive = [GOLD, BOND]

    def target(day: pd.Timestamp) -> dict[str, float]:
        scores = aggregate_score(close, day, aggressive)
        picked: list[str] = []
        if scores:
            ranked = sorted(scores.items(), key=lambda item: -item[1])
            positive_share = sum(1 for value in scores.values() if value > 0) / len(scores)
            if breadth_canary:
                if positive_share >= 0.5:
                    picked = [symbol for symbol, _ in ranked[:top_n] if scores[symbol] > 0]
            else:
                canary = scores.get(MARKET)
                if canary is not None and canary > 0:
                    picked = [symbol for symbol, _ in ranked[:top_n] if symbol != MARKET]
                    picked = picked[:top_n]
        if picked:
            weights = {symbol: 1.0 / len(picked) for symbol in picked}
        else:
            defensive_scores = aggregate_score(close, day, defensive)
            positive = [symbol for symbol in defensive if defensive_scores.get(symbol, -1.0) > 0]
            bucket = positive or []
            weights = {symbol: 1.0 / len(bucket) for symbol in bucket} if bucket else {}
        if vol_target is not None and weights:
            scale = vol_scale_for(panels, day, weights, vol_target)
            if scale < 1.0:
                weights = {s: w * scale for s, w in weights.items()}
        return weights
    return target


# ------------------------------------------------------------------------- helpers


def block_bootstrap(diff: np.ndarray, *, block: int = 5, reps: int = 2000,
                    seed: int = 20261104) -> dict[str, Any]:
    diff = np.asarray(diff, dtype=float)
    diff = diff[np.isfinite(diff)]
    if diff.size < block * 4:
        return {"n": int(diff.size), "mean_bp": None, "ci_low_bp": None, "ci_high_bp": None}
    rng = np.random.default_rng(seed)
    blocks = diff.size // block
    means = np.empty(reps)
    for i in range(reps):
        starts = rng.integers(0, diff.size - block + 1, blocks)
        means[i] = np.concatenate([diff[s:s + block] for s in starts]).mean()
    return {
        "n": int(diff.size),
        "mean_bp": round(float(diff.mean()) * 10000, 2),
        "ci_low_bp": round(float(np.percentile(means, 2.5)) * 10000, 2),
        "ci_high_bp": round(float(np.percentile(means, 97.5)) * 10000, 2),
        "p_value_two_sided": round(float(min(1.0, 2 * min((means <= 0).mean(), (means >= 0).mean()))), 4),
    }


def compare(a: RunResult, b: RunResult, *, block: int = 5) -> dict[str, Any]:
    aligned = pd.concat([a.curve.pct_change(), b.curve.pct_change()], axis=1, join="inner").dropna()
    if aligned.empty:
        return {"n": 0}
    diff = (aligned.iloc[:, 0] - aligned.iloc[:, 1]).to_numpy()
    stats = block_bootstrap(diff, block=block)
    return {
        "label_a": a.label,
        "label_b": b.label,
        "n": stats["n"],
        "mean_diff_bp": stats["mean_bp"],
        "ci_low_bp": stats["ci_low_bp"],
        "ci_high_bp": stats["ci_high_bp"],
        "p_value_two_sided": stats.get("p_value_two_sided"),
        "annualised_diff_pct": round(float(stats["mean_bp"] or 0.0) * 252 / 100, 2),
    }


def periods() -> list[tuple[str, str, str]]:
    return [
        ("full", DEFAULT_START, DEFAULT_END),
        ("design", DEFAULT_START, DESIGN_END),
        ("oos", OOS_START, DEFAULT_END),
    ]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def data_manifest(data_dir: Path) -> dict[str, str]:
    return {path.name: file_sha256(path) for path in sorted(data_dir.glob("*_qfq.pkl"))}


# ---------------------------------------------------------------------------- main


def crowding_groups(rows: list[dict[str, Any]], *, label: str) -> list[dict[str, Any]]:
    """Bucket rebalances by candidate count; descriptive only, never a selector."""
    frame = pd.DataFrame(rows)
    if frame.empty:
        return []
    buckets = pd.cut(frame["n_candidates"], bins=[-1, 0, 1, 4, 9, 1000],
                     labels=["0", "1", "2-4", "5-9", "10+"])
    out: list[dict[str, Any]] = []
    for bucket, part in frame.groupby(buckets, observed=True):
        fwd20 = pd.to_numeric(part["basket_forward_20d_pct"], errors="coerce").dropna()
        fwd5 = pd.to_numeric(part["basket_forward_5d_pct"], errors="coerce").dropna()
        corr = pd.to_numeric(part["candidate_corr"], errors="coerce").dropna()
        out.append({
            "variant": label,
            "bucket": str(bucket),
            "rebalances": int(len(part)),
            "mean_candidates": round(float(part["n_candidates"].mean()), 2),
            "mean_satellite_weight": round(float(pd.to_numeric(part["satellite_weight"]).mean()), 3),
            "mean_candidate_corr": round(float(corr.mean()), 3) if len(corr) else None,
            "basket_forward_5d_pct": round(float(fwd5.mean()), 2) if len(fwd5) else None,
            "basket_forward_20d_pct": round(float(fwd20.mean()), 2) if len(fwd20) else None,
            "win_rate_20d": round(float((fwd20 > 0).mean()), 3) if len(fwd20) else None,
        })
    return out


def write_tables(out_dir: Path, gate_rows: list[dict[str, Any]], top2_rows: list[dict[str, Any]],
                 control_rows: list[dict[str, Any]], cost_rows: list[dict[str, Any]],
                 diag_rows: list[dict[str, Any]], closed_rows: list[dict[str, Any]],
                 gate_runs: dict[str, RunResult], top2_runs: dict[str, RunResult]) -> None:
    columns = ["experiment", "label", "period", "total_return_pct", "cagr_pct", "vol_pct",
               "sharpe", "max_drawdown_pct", "avg_exposure", "trades", "turnover_x"]
    records: list[dict[str, Any]] = []
    for experiment, rows in (("A_gate_ablation", gate_rows), ("B_top2_vol8", top2_rows),
                             ("E_public_controls", control_rows)):
        for row in rows:
            records.append({"experiment": experiment, **{k: row.get(k) for k in columns if k != "experiment"}})
    for row in cost_rows:
        records.append({"experiment": "cost_stress", "label": f"{row['base']}_slip{row['slippage_bp']}bp",
                        "period": "full", **{k: row.get(k) for k in columns if k not in {"experiment", "label", "period"}}})
    pd.DataFrame(records)[columns].to_csv(out_dir / "metrics.csv", index=False)

    curves = {
        "A_gate": gate_runs["A_gate_full"].curve,
        "A_nogate": gate_runs["A_nogate_full"].curve,
        "B_top1_vol8": top2_runs["B_top1_vol8|full"].curve,
        "B_top2_vol8": top2_runs["B_top2_vol8|full"].curve,
    }
    curve_frame = pd.DataFrame(curves)
    curve_frame.index.name = "date"
    curve_frame.to_csv(out_dir / "curves.csv")

    pd.DataFrame(diag_rows).to_csv(out_dir / "diagnostics.csv", index=False)
    pd.DataFrame(closed_rows).to_csv(out_dir / "gate_closed_days.csv", index=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ETF extended research study")
    parser.add_argument("--data-dir", default="D:/tmp/etf_universe_data")
    parser.add_argument("--out", default="D:/tmp/etf_extended_study.json")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--skip-parity", action="store_true")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    satellite, close, open_, volume = load_panels(data_dir)
    panels = build_panels(close, volume)
    signals = build_signals(close, satellite, panels, close.index)
    print(f"satellite={len(satellite)} gate={MARKET} core={GOLD}/{BOND} days={len(close.index)}")

    def run(label: str, start: str, end: str, *, keep_trace: bool = False, **params: Any) -> RunResult:
        return run_v5(close, open_, panels, signals, label=label, start=start, end=end,
                      keep_trace=keep_trace, **params)

    # ---- parity against the published v5.1 table
    parity_rows: list[dict[str, Any]] = []
    parity_ok = True
    parity_specs = {
        "v5.1_top1_breadth_vol12": dict(max_picks=1, target_vol=0.12, breadth=True),
        "v5.1_top2_breadth_vol12": dict(max_picks=2, target_vol=0.12, breadth=True),
        "v5.1_top1_breadth_vol10": dict(max_picks=1, target_vol=0.10, breadth=True),
        "v5.1_top2_breadth_vol10": dict(max_picks=2, target_vol=0.10, breadth=True),
        "v5.1_top1_breadth_vol8": dict(max_picks=1, target_vol=0.08, breadth=True),
    }
    baseline_runs: dict[str, RunResult] = {}
    for label, params in parity_specs.items():
        result = run(label, args.start, args.end, crash_overlay=False, keep_trace=True, **params)
        baseline_runs[label] = result
        metrics = result.metrics()
        expected = PARITY[label]
        deltas = (
            round(metrics["total_return_pct"] - expected[0], 3),
            round(metrics["sharpe"] - expected[1], 3),
            round(metrics["max_drawdown_pct"] - expected[2], 3),
        )
        within = all(abs(delta) <= PARITY_TOL for delta in deltas)
        parity_ok = parity_ok and within
        parity_rows.append({
            "label": label, "expected": expected, "actual": [
                metrics["total_return_pct"], metrics["sharpe"], metrics["max_drawdown_pct"],
            ], "delta": deltas, "within_tolerance": within,
        })
        print(f"parity {label:<26} {metrics['total_return_pct']:>7.2f}% "
              f"{metrics['sharpe']:>5.2f} {metrics['max_drawdown_pct']:>7.2f}% "
              f"delta={deltas} {'OK' if within else 'MISMATCH'}")

    # ---- runtime parity: the canonical rule set the page and paper ledger use.
    # Independent expectation: docs/backtest-results/2026-11-02-etf-forward-test
    # froze backfill 10% at +90.10% / Sharpe 1.20 / -10.19% over 2020-01-02..2026-10-30.
    runtime_expected = (90.10, 1.20, -10.19)
    runtime_result = run("runtime_backfill_40_35_vol10", "2020-01-02", args.end,
                         max_picks=1, target_vol=0.10, breadth=True, crash_overlay=False,
                         core_w=0.40, sat_max=0.35, backfill=True)
    runtime_metrics = runtime_result.metrics()
    runtime_actual = (runtime_metrics["total_return_pct"], runtime_metrics["sharpe"],
                      runtime_metrics["max_drawdown_pct"])
    runtime_deltas = tuple(round(a - e, 3) for a, e in zip(runtime_actual, runtime_expected))
    runtime_within = all(abs(delta) <= PARITY_TOL for delta in runtime_deltas)
    parity_ok = parity_ok and runtime_within
    runtime_parity_row = {
        "label": "runtime_backfill_40_35_vol10",
        "expected": runtime_expected,
        "actual": list(runtime_actual),
        "delta": runtime_deltas,
        "within_tolerance": runtime_within,
        "window": ["2020-01-02", args.end],
    }
    print(f"parity {'runtime_backfill_40_35_vol10':<26} {runtime_actual[0]:>7.2f}% "
          f"{runtime_actual[1]:>5.2f} {runtime_actual[2]:>7.2f}% "
          f"delta={runtime_deltas} {'OK' if runtime_within else 'MISMATCH'}")

    # ---- A: market-gate ablation
    gate_rows: list[dict[str, Any]] = []
    gate_runs: dict[str, RunResult] = {}
    for pname, start, end in periods():
        for use_gate in (True, False):
            label = f"A_{'gate' if use_gate else 'nogate'}_{pname}"
            result = run(label, start, end, max_picks=1, target_vol=0.12, breadth=True,
                         crash_overlay=False, use_market_gate=use_gate, keep_trace=True)
            gate_runs[label] = result
            row = result.metrics() | {"period": pname, "use_market_gate": use_gate}
            gate_rows.append(row)
            print(f"{label:<22} ret {row['total_return_pct']:>7.2f}%  cagr {row['cagr_pct']:>5.2f}%  "
                  f"sharpe {row['sharpe']:>5.2f}  dd {row['max_drawdown_pct']:>7.2f}%  "
                  f"exp {row['avg_exposure']:>4.2f}  trades {row['trades']:>4}")

    gate_stats = {}
    gate_yearly = {}
    for pname, _, _ in periods():
        gate_stats[pname] = compare(gate_runs[f"A_nogate_{pname}"], gate_runs[f"A_gate_{pname}"])
        nogate_yearly = gate_runs[f"A_nogate_{pname}"].metrics()["yearly"]
        gate_yearly_ = gate_runs[f"A_gate_{pname}"].metrics()["yearly"]
        gate_yearly[pname] = {
            year: round(nogate_yearly[year] - gate_yearly_[year], 2)
            for year in sorted(gate_yearly_)
            if year in nogate_yearly
        }

    # gate-closed days opened by the ablation: how much satellite they add, and forward returns
    closed_rows: list[dict[str, Any]] = []
    gate_trace = gate_runs["A_gate_full"].trace
    nogate_trace = gate_runs["A_nogate_full"].trace
    for gate_row, nogate_row in zip(gate_trace, nogate_trace):
        if gate_row["gate"]:
            continue
        opened = _weights_of(nogate_row)
        added = {s: w for s, w in opened.items() if s in SATELLITE and w > 0}
        forward = [value for value in (_forward_return(close, s, gate_row["day"], 20) for s in added)
                   if value is not None]
        closed_rows.append({
            "day": str(gate_row["day"].date()),
            "satellite_added": " ".join(sorted(added)),
            "satellite_weight": round(sum(added.values()), 4),
            "n_candidates": len(nogate_row.get("candidates") or []),
            "added_forward_20d_pct": round(float(np.mean(forward)) * 100, 3) if forward else None,
            "csi_forward_20d_pct": (
                round(_forward_return(close, MARKET, gate_row["day"], 20) * 100, 3)
                if _forward_return(close, MARKET, gate_row["day"], 20) is not None else None
            ),
        })
    closed_frame = pd.DataFrame(closed_rows)
    if not closed_frame.empty:
        opened_trade = closed_frame[closed_frame["satellite_weight"] > 0]["added_forward_20d_pct"].dropna()
        gate_closed_summary = {
            "closed_days": int(len(closed_frame)),
            "closed_days_with_candidate": int((closed_frame["n_candidates"] > 0).sum()),
            "closed_days_with_satellite": int((closed_frame["satellite_weight"] > 0).sum()),
            "mean_satellite_weight": round(float(closed_frame["satellite_weight"].mean()), 4),
            "opened_forward_20d_mean_pct": round(float(opened_trade.mean()), 2) if len(opened_trade) else None,
            "opened_forward_20d_win_rate": round(float((opened_trade > 0).mean()), 3) if len(opened_trade) else None,
            "csi_forward_20d_mean_pct": round(float(closed_frame["csi_forward_20d_pct"].dropna().mean()), 2)
            if closed_frame["csi_forward_20d_pct"].notna().any() else None,
        }
    else:
        gate_closed_summary = {}

    # ---- B: vol-8 top2
    top2_rows: list[dict[str, Any]] = []
    top2_runs: dict[str, RunResult] = {}
    top2_specs = {
        "B_top1_vol8": dict(max_picks=1, target_vol=0.08),
        "B_top2_vol8": dict(max_picks=2, target_vol=0.08),
        "B_top1_vol10": dict(max_picks=1, target_vol=0.10),
        "B_top2_vol10": dict(max_picks=2, target_vol=0.10),
        "B_top1_vol12": dict(max_picks=1, target_vol=0.12),
        "B_top2_vol12": dict(max_picks=2, target_vol=0.12),
    }
    for pname, start, end in periods():
        for label, params in top2_specs.items():
            result = run(f"{label}_{pname}", start, end, breadth=True, crash_overlay=False,
                         keep_trace=label.startswith("B_top2_vol8"), **params)
            top2_runs[f"{label}|{pname}"] = result
            row = result.metrics() | {"period": pname, "max_picks": params["max_picks"],
                                      "target_vol": params["target_vol"]}
            top2_rows.append(row)
            print(f"{label}_{pname:<12} ret {row['total_return_pct']:>7.2f}%  "
                  f"sharpe {row['sharpe']:>5.2f}  dd {row['max_drawdown_pct']:>7.2f}%  "
                  f"vol {row['vol_pct']:>5.2f}%  exp {row['avg_exposure']:>4.2f}  turn {row['turnover_x']:>5}")

    top2_compare = {
        pname: compare(top2_runs[f"B_top2_vol8|{pname}"], top2_runs[f"B_top1_vol8|{pname}"])
        for pname, _, _ in periods()
    }

    # ---- C/D: diagnostics on baseline and top2-vol8
    diag_rows: list[dict[str, Any]] = []
    diag_summary: list[dict[str, Any]] = []
    crowding_summary: list[dict[str, Any]] = []
    for label, result in (("baseline_top1_vol12", baseline_runs["v5.1_top1_breadth_vol12"]),
                          ("top2_vol8", top2_runs["B_top2_vol8|full"]),
                          ("top2_vol12", baseline_runs["v5.1_top2_breadth_vol12"])):
        rows = diagnostics(close, panels, result.trace, label=label)
        diag_rows.extend(rows)
        diag_summary.append(summarise_diagnostics(rows, label=label))
        crowding_summary.extend(crowding_groups(rows, label=label))
        print("diagnostics", label, "points", len(rows))

    # ---- E: public controls
    control_rows: list[dict[str, Any]] = []
    control_runs: dict[str, RunResult] = {}
    universe = [*SATELLITE, GOLD, BOND]
    control_specs: dict[str, Callable[[pd.Timestamp], dict[str, float]]] = {
        "E_faber_universe": faber_targets(close, panels, universe, vol_target=None),
        "E_faber_universe_vt12": faber_targets(close, panels, universe, vol_target=0.12),
        "E_faber_3asset": faber_targets(close, panels, [MARKET, GOLD, BOND], vol_target=None),
        "E_gem": gem_targets(close, panels, vol_target=None),
        "E_gem_vt12": gem_targets(close, panels, vol_target=0.12),
        "E_vaa_style": vaa_targets(close, panels, top_n=4, vol_target=None, breadth_canary=True),
        "E_vaa_style_vt12": vaa_targets(close, panels, top_n=4, vol_target=0.12, breadth_canary=True),
        "E_daa_style": vaa_targets(close, panels, top_n=4, vol_target=None, breadth_canary=False),
        "E_daa_style_vt12": vaa_targets(close, panels, top_n=4, vol_target=0.12, breadth_canary=False),
        "E_100pct_bond": lambda day: {BOND: 1.0},
        "E_100pct_gold": lambda day: {GOLD: 1.0},
    }
    for pname, start, end in periods():
        dates = close.loc[pd.Timestamp(start):pd.Timestamp(end)].index
        for label, target in control_specs.items():
            result = execute(close, open_, dates=dates, target_fn=target, rebalance="M",
                             slippage=SLIPPAGE, label=f"{label}_{pname}")
            control_runs[f"{label}|{pname}"] = result
            row = result.metrics() | {"period": pname}
            control_rows.append(row)
            print(f"{label}_{pname:<10} ret {row['total_return_pct']:>7.2f}%  "
                  f"sharpe {row['sharpe']:>5.2f}  dd {row['max_drawdown_pct']:>7.2f}%  "
                  f"exp {row['avg_exposure']:>4.2f}")

    control_compare = {
        name: compare(control_runs[f"{name}|full"], control_runs["E_vaa_style_vt12|full"])
        for name in control_specs
        if name != "E_vaa_style_vt12"
    }
    control_vs_baseline = {
        name: compare(control_runs[f"{name}|full"], baseline_runs["v5.1_top1_breadth_vol12"])
        for name in control_specs
    }

    # ---- cost stress on the two headline arms
    cost_rows: list[dict[str, Any]] = []
    for label, params in (("A_gate_top1_vol12", dict(max_picks=1, target_vol=0.12)),
                          ("A_nogate_top1_vol12", dict(max_picks=1, target_vol=0.12, use_market_gate=False)),
                          ("B_top1_vol8", dict(max_picks=1, target_vol=0.08)),
                          ("B_top2_vol8", dict(max_picks=2, target_vol=0.08))):
        for slip in (0.0005, 0.0010, 0.0020):
            result = run(f"{label}_slip{int(slip * 10000)}", args.start, args.end,
                         breadth=True, crash_overlay=False, slippage=slip, **params)
            cost_rows.append(result.metrics() | {"base": label})

    payload = {
        "meta": {
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "data_dir": str(data_dir),
            "start": args.start,
            "end": args.end,
            "initial": INITIAL,
            "commission": COMMISSION,
            "slippage": SLIPPAGE,
            "min_commission": MIN_COMMISSION,
            "lot": LOT,
            "adv_min": ADV_MIN,
            "satellite_count": len(satellite),
            "satellite": satellite,
            "excluded": sorted(EXCLUDE),
            "core": [GOLD, BOND],
            "gate": MARKET,
            "coarse_group": COARSE_GROUP,
            "coarse_group_sha256": hashlib.sha256(
                json.dumps(COARSE_GROUP, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "data_sha256": data_manifest(data_dir),
        },
        "parity": {"ok": parity_ok, "tolerance": PARITY_TOL, "rows": parity_rows,
                   "runtime": runtime_parity_row},
        "gate_ablation": {
            "rows": gate_rows,
            "daily_compare": gate_stats,
            "yearly_diff_nogate_minus_gate": gate_yearly,
            "closed_days": gate_closed_summary,
            "trace": closed_rows,
        },
        "top2_vol8": {"rows": top2_rows, "daily_compare": top2_compare},
        "diagnostics": {"summary": diag_summary, "crowding": crowding_summary, "rows": diag_rows},
        "controls": {"rows": control_rows, "compare": control_compare,
                     "vs_baseline": control_vs_baseline},
        "cost_stress": cost_rows,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    write_tables(out_path.parent, gate_rows, top2_rows, control_rows, cost_rows,
                 diag_rows, closed_rows, gate_runs, top2_runs)
    print("wrote", out_path)

    if not parity_ok and not args.skip_parity:
        print("PARITY MISMATCH: published v5.1 numbers are not reproduced")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
