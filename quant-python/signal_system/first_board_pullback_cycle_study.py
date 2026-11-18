"""首板缩量回调 V1–V5：按用户规则做结构增量验证（预注册，不调参）。

V0 基线：首板放量涨停后 1–3 日缩量回调 ≤8%，回调量 ≤60% 首板量且 ≤20 日均额，次日开盘买；
         -5% 止损 / +10% 止盈 / 最多持 5 日。
V1：回调 10%–25%，不破首板日最低价；首板量缩 ≤60%；1–10 日内首次满足，次日开盘买。
V2：V1 + 量缩至首板日 50% 以下 + 至少 2 个连续地量日 + 之后首次放量（≥1.5×前一日）收阳，次日开盘买。
V3：V2 + 放量转折日收盘距 MA20 ≤3%，且 MA20 五日斜率 ≥0（走平/上翘）。
V4：V3 + 放量转折日收盘 > MA20（放量站上 MA20）。
V5：V4 + 首板日处于「冰点/启动期」：最高连板 ≤2 且涨停家数 ≤ 前 20 日中位数。

所有变体共用：首板定义、2000 万 ADV、2 元价格、-5%/+10%/5 日出场、成本 3bp/10bp/10bp。
情绪周期统计只用当日及以前数据；首板日 T 的周期状态在收盘后确定，之后才可能入场。
研究脚本，不接生产、不自动交易。
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

import first_board_pullback_study as base

BASE_DIR = pathlib.Path(__file__).resolve().parent
OUT_DIR = BASE_DIR.parent.parent / "docs" / "backtest-results" / "2026-11-16-first-board-pullback-v1-v5"


@dataclass(frozen=True)
class VariantConfig:
    name: str
    min_pullback: float
    max_pullback: float
    shrink_ratio: float
    stabilize: bool
    ma20_near: bool
    ma20_slope: bool
    reclaim_ma20: bool
    ice_only: bool
    max_wait: int


VARIANTS = [
    VariantConfig("V0", 0.0, 0.08, 0.60, False, False, False, False, False, 3),
    VariantConfig("V1", 0.10, 0.25, 0.60, False, False, False, False, False, 10),
    VariantConfig("V2", 0.10, 0.25, 0.50, True, False, False, False, False, 10),
    VariantConfig("V3", 0.10, 0.25, 0.50, True, True, True, False, False, 10),
    VariantConfig("V4", 0.10, 0.25, 0.50, True, True, True, True, False, 10),
    VariantConfig("V5", 0.10, 0.25, 0.50, True, True, True, True, True, 10),
]


def market_cycle(bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Daily limit-up ladder / breadth, computed only from each day's close."""
    stats: dict[pd.Timestamp, dict[str, int]] = defaultdict(lambda: {"lu": 0, "height": 0, "first": 0, "broken": 0})
    for symbol, frame in bars.items():
        if len(frame) < 2:
            continue
        dates = pd.to_datetime(frame["datetime"])
        close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
        high = pd.to_numeric(frame["high"], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(close).all() or not np.isfinite(high).all():
            continue
        rates = base.limit_rate_series(symbol, dates)
        prev = np.r_[np.nan, close[:-1]]
        limit_up = base._is_limit_up_at(close, high, prev, rates)
        limit_up[0] = False
        if "splice_break" in frame.columns:
            limit_up[frame["splice_break"].fillna(False).to_numpy(dtype=bool)] = False
        touched = (high / prev - 1.0 >= rates - 0.005) & ~limit_up
        consecutive = np.zeros(len(frame), dtype=int)
        for i in range(1, len(frame)):
            consecutive[i] = consecutive[i - 1] + 1 if limit_up[i] else 0
        for i in np.flatnonzero(limit_up):
            day = pd.Timestamp(dates.iloc[i])
            bucket = stats[day]
            bucket["lu"] += 1
            bucket["height"] = max(bucket["height"], int(consecutive[i]))
            if consecutive[i] == 1:
                bucket["first"] += 1
        for i in np.flatnonzero(touched):
            stats[pd.Timestamp(dates.iloc[i])]["broken"] += 1
    frame = pd.DataFrame.from_dict(stats, orient="index").sort_index()
    frame.index.name = "date"
    frame["lu_median20"] = frame["lu"].shift(1).rolling(20, min_periods=10).median()
    frame["ice"] = (frame["height"] <= 2) & (frame["lu"] <= frame["lu_median20"])
    return frame


def _low_volume_day(
    j: int,
    close: np.ndarray,
    amount: np.ndarray,
    first_low: float,
    limit_close: float,
    limit_amount: float,
    avg20: float,
    cfg: VariantConfig,
) -> bool:
    if j >= len(close):
        return False
    c = float(close[j])
    a = float(amount[j])
    if not (math.isfinite(c) and math.isfinite(a)):
        return False
    if c >= limit_close or c < limit_close * (1.0 - cfg.max_pullback) or c > limit_close * (1.0 - cfg.min_pullback):
        return False
    if c < first_low:
        return False
    if a > cfg.shrink_ratio * limit_amount or a > avg20:
        return False
    return True


def _trade(
    symbol: str,
    bars: pd.DataFrame,
    t: int,
    j: int,
    entry_idx: int,
    exit_cfg: base.ExitConfig,
    cfg: VariantConfig,
    first_board_low: float,
    limit_close: float,
    limit_amount: float,
    volume_multiple: float,
    simulate_exit: Callable[..., dict[str, Any] | None],
) -> dict[str, Any] | None:
    dates = bars["datetime"]
    open_ = bars["open"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    rates = base.limit_rate_series(symbol, dates)
    if entry_idx >= len(bars):
        return None
    entry_open = float(open_[entry_idx])
    if not math.isfinite(entry_open) or entry_open <= 0:
        return None
    if entry_open / close[j] - 1.0 >= rates[entry_idx] - 0.005:
        return None
    exit_info = simulate_exit(symbol, bars, entry_idx, entry_open, rates, exit_cfg)
    if exit_info is None:
        return None
    entry_date = pd.Timestamp(dates.iloc[entry_idx])
    if entry_date <= base.SPLICE_DATE < exit_info["exit_date"]:
        return None
    return {
        "symbol": symbol,
        "limit_date": pd.Timestamp(dates.iloc[t]),
        "signal_date": pd.Timestamp(dates.iloc[j]),
        "entry_date": entry_date,
        "limit_close": limit_close,
        "limit_amount": limit_amount,
        "volume_multiple": volume_multiple,
        "first_board_low": first_board_low,
        "pullback_pct": float(close[j] / limit_close - 1.0),
        "entry_open": entry_open,
        **exit_info,
    }


def detect(
    symbol: str,
    bars: pd.DataFrame,
    cfg: VariantConfig,
    exit_cfg: base.ExitConfig,
    ice_days: set[pd.Timestamp] | None,
    simulate_exit: Callable[..., dict[str, Any] | None] = base._simulate_exit,
) -> list[dict[str, Any]]:
    if len(bars) < 250 + cfg.max_wait + exit_cfg.hold_days:
        return []
    dates = bars["datetime"]
    close = bars["close"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    open_ = bars["open"].to_numpy(dtype=float)
    amount = bars["amount"].to_numpy(dtype=float)
    volume = bars["volume"].to_numpy(dtype=float)
    if not np.isfinite(amount).all() or (amount <= 0).any():
        fallback = volume * close
        amount = np.where(np.isfinite(amount) & (amount > 0), amount, fallback)
    if not np.isfinite(amount).all() or (amount <= 0).any():
        return []

    ma20 = pd.Series(close).rolling(20, min_periods=20).mean().to_numpy(dtype=float)
    slope5 = pd.Series(ma20).pct_change(5).to_numpy(dtype=float)
    rates = base.limit_rate_series(symbol, dates)
    prev = np.r_[np.nan, close[:-1]]
    limit_up = base._is_limit_up_at(close, high, prev, rates)
    limit_up[0] = False
    if "splice_break" in bars.columns:
        limit_up[bars["splice_break"].fillna(False).to_numpy(dtype=bool)] = False
    avg20 = pd.Series(amount).shift(1).rolling(20, min_periods=20).mean().to_numpy(dtype=float)
    volume_multiple = amount / avg20
    first_board = np.zeros(len(bars), dtype=bool)
    for t in np.flatnonzero(limit_up):
        if t >= 250:
            first_board[t] = base._candidate_after_quiet_prior(limit_up, t, 20)

    out: list[dict[str, Any]] = []
    for t in np.flatnonzero(first_board):
        if not math.isfinite(avg20[t]) or avg20[t] < 20_000_000.0:
            continue
        if close[t] < 2.0 or volume_multiple[t] < 2.0:
            continue
        if cfg.ice_only and ice_days is not None and pd.Timestamp(dates.iloc[t]) not in ice_days:
            continue
        limit_close = float(close[t])
        limit_amount = float(amount[t])
        first_low = float(low[t])
        entry = None

        if not cfg.stabilize:
            for j in range(t + 1, t + 1 + cfg.max_wait):
                if j + 1 >= len(bars):
                    break
                if _low_volume_day(j, close, amount, first_low, limit_close, limit_amount, avg20[t], cfg):
                    entry = _trade(symbol, bars, t, j, j + 1, exit_cfg, cfg, first_low, limit_close, limit_amount, float(volume_multiple[t]), simulate_exit)
                    break
        else:
            for start in range(t + 1, t + 1 + cfg.max_wait - 1):
                if start + 1 >= len(bars):
                    break
                if not _low_volume_day(start, close, amount, first_low, limit_close, limit_amount, avg20[t], cfg):
                    continue
                if not _low_volume_day(start + 1, close, amount, first_low, limit_close, limit_amount, avg20[t], cfg):
                    continue
                for k in range(start + 2, min(t + 1 + cfg.max_wait, len(bars) - 1)):
                    if float(close[k]) >= limit_close or float(close[k]) < first_low:
                        continue
                    if float(amount[k]) < 1.5 * float(amount[k - 1]) or float(close[k]) <= float(close[k - 1]):
                        continue
                    if cfg.ma20_near:
                        if not math.isfinite(ma20[k]) or abs(float(close[k]) - float(ma20[k])) / float(ma20[k]) > 0.03:
                            continue
                    if cfg.ma20_slope:
                        if not math.isfinite(slope5[k]) or slope5[k] < 0:
                            continue
                    if cfg.reclaim_ma20:
                        if not math.isfinite(ma20[k]) or float(close[k]) <= float(ma20[k]):
                            continue
                    entry = _trade(symbol, bars, t, k, k + 1, exit_cfg, cfg, first_low, limit_close, limit_amount, float(volume_multiple[t]), simulate_exit)
                    if entry is not None:
                        break
                if entry is not None:
                    break
        if entry is not None:
            out.append(entry)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()

    print("loading bars ...", flush=True)
    bars = base.load_bars()
    print(f"loaded {len(bars)} symbols", flush=True)
    print("building market cycle ...", flush=True)
    cycle = market_cycle(bars)
    ice_days = set(cycle.index[cycle["ice"].fillna(False)])
    print(f"cycle days {len(cycle)}, ice days {len(ice_days)}", flush=True)

    signal_cfg = base.SignalConfig(start=args.start, end=args.end)
    exit_cfg = base.ExitConfig()
    portfolio_cfg = base.PortfolioConfig()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cycle.reset_index().to_csv(out / "market_cycle.csv", index=False)
    summary: dict[str, Any] = {
        "period": {"start": args.start, "end": args.end},
        "variants": {v.name: asdict(v) for v in VARIANTS},
        "cycle": {
            "ice_days": len(ice_days),
            "definition": "height<=2 and limit_up_count<=previous 20-day median",
        },
        "results": {},
    }
    for cfg in VARIANTS:
        print(f"scanning {cfg.name} ...", flush=True)
        trades: list[dict[str, Any]] = []
        for i, (symbol, frame) in enumerate(bars.items(), 1):
            trades.extend(detect(symbol, frame, cfg, exit_cfg, ice_days))
            if i % 1000 == 0:
                print(f"  {cfg.name} {i}/{len(bars)}", flush=True)
        trades.sort(key=lambda x: (x["entry_date"], x["symbol"]))
        trades = [x for x in trades if pd.Timestamp(args.start) <= x["entry_date"] <= pd.Timestamp(args.end)]
        trades_frame = pd.DataFrame(trades)
        if not trades_frame.empty:
            trades_frame["net_return_pct"] = [round(base.net_trade_return(x, base.BASE_SLIPPAGE) * 100, 3) for x in trades]
            trades_frame.to_csv(out / f"trades_{cfg.name}.csv", index=False)
        event = base.event_stats(trades, base.BASE_SLIPPAGE)
        design = base._subset_period(trades, args.start, "2024-12-31")
        holdout = base._subset_period(trades, "2025-01-01", args.end)
        curve, portfolio, accepted = base.simulate_portfolio(trades, bars, signal_cfg, portfolio_cfg, base.BASE_SLIPPAGE)
        if not curve.empty:
            curve.to_csv(out / f"portfolio_{cfg.name}.csv", index=False)
        summary["results"][cfg.name] = {
            "event": event,
            "event_design_2020_2024": base.event_stats(design, base.BASE_SLIPPAGE),
            "event_holdout_2025_2026": base.event_stats(holdout, base.BASE_SLIPPAGE),
            "portfolio": portfolio,
            "trades": len(trades),
        }
        print(
            f"{cfg.name}: trades={len(trades)} mean={event.get('mean_pct')}% "
            f"win={event.get('win_rate_pct')}% pf={event.get('profit_factor')} "
            f"portfolio={portfolio.get('total_return_pct')}%",
            flush=True,
        )
    (out / "summary.json").write_text(json.dumps(base._jsonable(summary), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
