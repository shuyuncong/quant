"""Pure ETF core-satellite signal engine.

Reads the local qfq cache, evaluates the frozen 65/35 + backfill35 rules, and
returns a target portfolio. No network, database, orders, or UI concerns here.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

MARKET = "510300"
CORE = {"518880": "黄金", "511260": "十年国债"}
SATELLITE = {
    "159611": "电力", "159647": "中药", "159745": "建材", "159766": "旅游",
    "159770": "机器人", "159825": "农业", "159865": "养殖", "159869": "游戏",
    "159870": "化工", "159886": "机械", "159915": "创业板", "159928": "消费",
    "159967": "成长", "159992": "创新药", "159996": "家电", "159997": "电子",
    "512010": "医药", "512040": "价值", "512100": "中证1000", "512170": "医疗",
    "512200": "房地产", "512480": "半导体", "512580": "环保", "512660": "军工",
    "512720": "计算机", "512760": "芯片", "512800": "银行", "512880": "证券",
    "512890": "低波", "512980": "传媒", "515000": "科技", "515050": "5G",
    "515070": "人工智能", "515170": "食品", "515210": "钢铁", "515230": "软件",
    "515790": "光伏", "515880": "通信", "516110": "汽车", "516160": "新能源",
    "516510": "云计算", "516780": "稀土", "516970": "基建", "588000": "科创50",
}
NAMES = {**SATELLITE, **CORE, MARKET: "沪深300"}
CORE_WEIGHT = 0.65
SATELLITE_MAX = 0.35
BACKFILL_WEIGHT = 0.35
BACKFILL_GOLD = 0.40
BACKFILL_BOND = 0.60
TARGET_VOL = 0.10
ADV_MIN = 50_000_000.0
LOT = 100


class EtfDataError(RuntimeError):
    """Raised when the local ETF qfq cache is missing or incomplete."""


def _data_dir() -> Path:
    override = os.getenv("ETF_ROTATION_DATA_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "cache" / "etf_rotation"


def _read_frame(data_dir: Path, symbol: str) -> pd.DataFrame:
    path = data_dir / f"{symbol}_qfq.pkl"
    if not path.exists():
        raise EtfDataError(f"缺少 ETF 行情缓存: {path}")
    frame = pd.read_pickle(path)
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise EtfDataError(f"ETF 行情为空: {path}")
    frame = frame[frame["close"].gt(0)].copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    return frame.set_index("datetime").sort_index()


def load_panel(data_dir: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return aligned close/volume panels indexed by the market calendar."""
    root = data_dir or _data_dir()
    symbols = [*SATELLITE, MARKET, *CORE]
    frames = {symbol: _read_frame(root, symbol) for symbol in symbols}
    master = frames[MARKET].index
    close = pd.DataFrame({symbol: frame["close"].reindex(master).ffill() for symbol, frame in frames.items()})
    volume = pd.DataFrame({symbol: frame["volume"].reindex(master).ffill() for symbol, frame in frames.items()})
    return close, volume


def _last_finite(panel: pd.DataFrame, symbol: str, day: pd.Timestamp) -> float:
    value = float(panel[symbol].loc[day])
    return value if math.isfinite(value) else float("nan")


def compute_signal(close: pd.DataFrame, volume: pd.DataFrame, as_of: pd.Timestamp | None = None) -> dict[str, Any]:
    """Compute the current signal and target weights.

    Decision is made on the last available close; execution is intentionally
    outside this function.
    """
    if MARKET not in close.columns:
        raise EtfDataError("行情面板缺少沪深300")
    day = pd.Timestamp(as_of) if as_of is not None else close.index[-1]
    if day not in close.index:
        day = close.index[close.index <= day].max()
    if pd.isna(day):
        raise EtfDataError("没有可用的行情日期")

    csi = close[MARKET]
    csi_ma200 = csi.rolling(200).mean()
    csi_ma20 = csi.rolling(20).mean()
    csi_high252 = csi.rolling(252).max()
    gate = bool(math.isfinite(float(csi_ma200.loc[day])) and float(csi.loc[day]) > float(csi_ma200.loc[day]))
    high = float(csi_high252.loc[day]) if math.isfinite(float(csi_high252.loc[day])) else 0.0
    drawdown = float(csi.loc[day]) / high - 1.0 if high > 0 else 0.0
    crash = bool(drawdown <= -0.20 and math.isfinite(float(csi_ma20.loc[day])) and float(csi.loc[day]) > float(csi_ma20.loc[day]))

    returns = close.pct_change()
    ma60 = close.rolling(60).mean()
    ma200 = close.rolling(200).mean()
    ma200_prev = ma200.shift(20)
    adv20 = (close * volume * 100.0).rolling(20).mean()
    momentum = close.shift(21) / close.shift(252) - 1.0
    vol60 = returns.rolling(60).std() * math.sqrt(252.0)
    ret60 = close / close.shift(60) - 1.0
    valid = close.notna().cumsum()

    core_momentum: dict[str, float] = {}
    for symbol in CORE:
        value = float(momentum[symbol].loc[day])
        if math.isfinite(value) and value > 0:
            core_momentum[symbol] = value
    core = max(core_momentum, key=core_momentum.get) if core_momentum else None

    candidates: list[dict[str, Any]] = []
    if gate or crash:
        for symbol in SATELLITE:
            if int(valid[symbol].loc[day]) < 253:
                continue
            adv = float(adv20[symbol].loc[day])
            if not math.isfinite(adv) or adv < ADV_MIN:
                continue
            price = float(close[symbol].loc[day])
            ma60_value = float(ma60[symbol].loc[day])
            ma200_value = float(ma200[symbol].loc[day])
            ma200_prev_value = float(ma200_prev[symbol].loc[day])
            ret60_value = float(ret60[symbol].loc[day])
            if not (
                math.isfinite(ma60_value) and math.isfinite(ma200_value)
                and math.isfinite(ma200_prev_value) and math.isfinite(ret60_value)
                and price > ma200_value and ma200_value > ma200_prev_value
                and price > ma60_value and ma60_value > ma200_value and ret60_value > 0
            ):
                continue
            mom = float(momentum[symbol].loc[day])
            vol = float(vol60[symbol].loc[day])
            if not math.isfinite(mom) or not math.isfinite(vol) or mom <= 0 or vol <= 0:
                continue
            candidates.append({
                "symbol": symbol,
                "name": SATELLITE[symbol],
                "score": mom / vol,
                "momentum_12_1": mom,
                "vol_60": vol,
                "price": price,
            })
    candidates.sort(key=lambda item: (-item["score"], item["symbol"]))
    picks = candidates[:1]
    n_trend = len(candidates)
    satellite_budget = SATELLITE_MAX * min(1.0, n_trend / 2.0)

    weights: dict[str, float] = {}
    if core:
        weights[core] = CORE_WEIGHT
    if picks and satellite_budget > 0:
        weights[picks[0]["symbol"]] = weights.get(picks[0]["symbol"], 0.0) + satellite_budget
    else:
        weights["518880"] = weights.get("518880", 0.0) + BACKFILL_WEIGHT * BACKFILL_GOLD
        weights["511260"] = weights.get("511260", 0.0) + BACKFILL_WEIGHT * BACKFILL_BOND

    raw_weights = dict(weights)
    scale = 1.0
    if weights:
        cols = list(weights)
        sample = returns.loc[:day].tail(60)[cols].dropna()
        if len(sample) >= 20:
            vector = np.array([weights[symbol] for symbol in cols], dtype=float)
            covariance = sample.cov().to_numpy() * 252.0
            portfolio_vol = float(np.sqrt(max(vector @ covariance @ vector, 0.0)))
            if math.isfinite(portfolio_vol) and portfolio_vol > TARGET_VOL:
                scale = float(TARGET_VOL / portfolio_vol)
                weights = {symbol: value * scale for symbol, value in weights.items()}

    total = sum(weights.values())
    target = [
        {
            "symbol": symbol,
            "name": NAMES.get(symbol, symbol),
            "weight": round(float(weight), 6),
            "price": round(float(close[symbol].loc[day]), 6),
        }
        for symbol, weight in sorted(weights.items())
        if weight > 0
    ]
    cash_weight = max(0.0, 1.0 - total)
    target.append({"symbol": "CASH", "name": "现金", "weight": round(cash_weight, 6), "price": 1.0})
    return {
        "as_of": day.strftime("%Y-%m-%d"),
        "gate": gate,
        "gate_reason": "沪深300收盘价高于MA200" if gate else "沪深300收盘价低于MA200",
        "crash": crash,
        "core": core,
        "core_name": CORE.get(core, "") if core else "",
        "core_momentum": {symbol: round(value, 6) for symbol, value in core_momentum.items()},
        "picks": [item["symbol"] for item in picks],
        "candidates": candidates[:10],
        "n_trend": n_trend,
        "satellite_budget": round(satellite_budget, 6),
        "target_weights": {symbol: round(float(weight), 6) for symbol, weight in weights.items()},
        "cash_weight": round(cash_weight, 6),
        "vol_scale": round(scale, 6),
        "raw_weights": {symbol: round(float(weight), 6) for symbol, weight in raw_weights.items()},
        "reason": (
            f"闸门{'开启' if gate else '关闭'}，"
            + (f"核心 {core}，卫星 {picks[0]['symbol']}" if picks else "无卫星候选，回填黄金/国债")
        ),
        "target": target,
    }
