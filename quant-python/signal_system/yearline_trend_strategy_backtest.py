"""年线(250日)趋势策略回测.

策略定义 (自然语言 -> 可执行规则, 全部只使用信号日收盘及更早数据, 无前视):
  1. 标的池: 长期向好的趋势票
     - MA250 上行: MA250[t] > MA250[t-20]
     - close[t] > MA250[t] (站上年线)
  2. 入场 A (放量站上年线): 当日收盘上穿 MA250 且当日量 >= vol_mult×MA5(量)
  3. 入场 B (回踩年线不破): 前一日已在线上方, 当日最低 <= MA250×(1+tol) 且
     收盘 >= MA250 (不破), 且收盘未明显转弱
  4. 出场: 止损(5%/8%/动态ATR截断到[5%,8%]+浮盈8%后移保本上方2%) 或
     收盘跌破 MA250 (趋势破坏)

数据边界: 仅本地缓存 `cache/daily_history/<code>_qfq.pkl`, 不连生产库/holdout。
回测窗口: 需250根做MA250热启动, 实际信号窗口约 2024-06 -> 2026-09。
已知偏差: 当前股票清单用于历史窗口存在幸存者偏差 (开发用, 不能称历史时点成分股)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
HISTORY_DIR = BASE_DIR / "cache" / "daily_history"
UNIVERSE_FILE = BASE_DIR / "cache" / "49c74bcce8953772e779e483af108c878820d52b5b8425db964fddb98a07f2b6.pkl"
INDEX_FILE = BASE_DIR / "cache" / "index_000001_sh.pkl"

MA_YEAR = 250
MA_SLOPE_LOOKBACK = 20
VOL_MA = 5
STOP_MODES = ["fixed5", "fixed8", "dynamic"]


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """给单只股票 QFQ 历史追加不产生前视的指标列."""
    df = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["datetime"] = pd.to_datetime(df["datetime"])
    close = df["close"]
    high = df["high"]
    low = df["low"]
    df["ma250"] = close.rolling(MA_YEAR).mean()
    df["ma60"] = close.rolling(60).mean()
    df["ma120"] = close.rolling(120).mean()
    df["vol_ma5"] = df["volume"].rolling(VOL_MA).mean()
    df["ma250_slope"] = df["ma250"] - df["ma250"].shift(MA_SLOPE_LOOKBACK)
    # 稳定上涨刻画
    df["above250_frac60"] = (close > df["ma250"]).rolling(60).mean()  # 近60日收盘站上年线比例
    df["high120_close"] = close.rolling(120).max()                     # 近120日收盘最高
    df["ret20"] = close / close.shift(20) - 1
    df["ret60"] = close / close.shift(60) - 1
    # ATR14 (Wilder)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean()
    df["atr_pct"] = df["atr14"] / close
    return df


# --------------------------------------------------------------------------- #
# 信号生成
# --------------------------------------------------------------------------- #
@dataclass
class Signal:
    symbol: str
    name: str
    entry_type: str  # "A_breakout" | "B_pullback"
    entry_idx: int
    entry_date: pd.Timestamp
    entry_price: float
    vol_ratio: float  # 当日量 / vol_ma5
    atr_pct: float


def generate_signals(
    symbol: str,
    name: str,
    df: pd.DataFrame,
    vol_mult: float,
    tol: float,
    cooldown: int = 20,
    filters: Optional[list[str]] = None,
) -> list[Signal]:
    """按因果规则生成入场信号; cooldown 防止同一段回踩重复计数.

    filters 中的"稳定上涨"条件(全部用信号日及以前数据):
      - align: 多头排列 close > ma60 > ma120
      - freq : 近60日收盘站上年线比例 >= 80%
      - dd   : 距120日收盘高点回撤 <= 15% (close >= 0.85*high120)
      - mom  : 20日与60日收益均为正
    """
    if len(df) < MA_YEAR + MA_SLOPE_LOOKBACK + 5:
        return []
    filters = set(filters or [])
    close = df["close"].values
    low = df["low"].values
    ma250 = df["ma250"].values
    vol = df["volume"].values
    vol_ma5 = df["vol_ma5"].values
    slope = df["ma250_slope"].values
    atr_pct = df["atr_pct"].values

    signals: list[Signal] = []
    last_entry_idx = -10**9
    start = MA_YEAR + MA_SLOPE_LOOKBACK  # 热启动
    for i in range(start, len(df)):
        if np.isnan(close[i]) or np.isnan(low[i]) or np.isnan(ma250[i]) or np.isnan(ma250[i - 1]):
            continue
        if np.isnan(vol_ma5[i]) or vol_ma5[i] <= 0:
            continue
        # 基础趋势池: MA250 上行 + 价格在年线上方
        if not (slope[i] > 0 and close[i] > ma250[i]):
            continue
        # 稳定上涨过滤
        if filters - {"align", "freq", "dd", "mom"}:
            raise ValueError(f"unknown filters: {filters}")
        if "align" in filters:
            ma60 = df["ma60"].values[i]
            ma120 = df["ma120"].values[i]
            if np.isnan(ma60) or np.isnan(ma120) or not (close[i] > ma60 > ma120):
                continue
        if "freq" in filters:
            f = df["above250_frac60"].values[i]
            if np.isnan(f) or f < 0.8:
                continue
        if "dd" in filters:
            h120 = df["high120_close"].values[i]
            if np.isnan(h120) or close[i] < 0.85 * h120:
                continue
        if "mom" in filters:
            r20 = df["ret20"].values[i]
            r60 = df["ret60"].values[i]
            if np.isnan(r20) or np.isnan(r60) or r20 <= 0 or r60 <= 0:
                continue
        prev_close = close[i - 1]
        prev_ma = ma250[i - 1]
        vol_ratio = vol[i] / vol_ma5[i]
        if np.isnan(vol_ratio):
            vol_ratio = 0.0

        # 入场 A: 放量站上年线 (收盘上穿)
        if prev_close <= prev_ma and close[i] > ma250[i] and vol_ratio >= vol_mult:
            if i - last_entry_idx >= cooldown:
                signals.append(
                    Signal(symbol, name, "A_breakout", i, df["datetime"].iloc[i],
                           float(close[i]), float(vol_ratio), float(atr_pct[i]))
                )
                last_entry_idx = i
                continue

        # 入场 B: 回踩年线不破 (前一日在线, 当日最低触线但收盘守住)
        if prev_close > prev_ma and close[i] >= ma250[i] and low[i] <= ma250[i] * (1 + tol):
            if close[i] >= close[i - 1]:
                if i - last_entry_idx >= cooldown:
                    signals.append(
                        Signal(symbol, name, "B_pullback", i, df["datetime"].iloc[i],
                               float(close[i]), float(vol_ratio), float(atr_pct[i]))
                    )
                    last_entry_idx = i

    return signals


# --------------------------------------------------------------------------- #
# 单笔独立模拟
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    symbol: str
    name: str
    entry_type: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: Optional[pd.Timestamp]
    exit_price: float
    exit_reason: str
    return_gross: float
    return_net: float
    holding_days: int


def _stop_pct(stop_mode: str, atr_pct: float) -> float:
    if stop_mode == "fixed5":
        return 0.05
    if stop_mode == "fixed8":
        return 0.08
    # dynamic: 2×ATR14% 截断到 [5%, 8%]
    return float(np.clip(2.0 * atr_pct, 0.05, 0.08))


def simulate_trade(
    df: pd.DataFrame,
    signal: Signal,
    stop_mode: str,
    cost_bps_buy: float,
    cost_bps_sell: float,
    max_holding: Optional[int] = None,
    exit_mode: str = "stop_trend",
) -> Optional[Trade]:
    """独立模拟一笔交易: 入场日收盘买入, 次日盯止损/趋势."""
    n = len(df)
    entry_i = signal.entry_idx
    entry = signal.entry_price
    if np.isnan(entry) or entry <= 0:
        return None

    atr_pct = signal.atr_pct if not np.isnan(signal.atr_pct) else 0.06
    stop = entry * (1 - _stop_pct(stop_mode, atr_pct))

    low_arr = df["low"].values
    high_arr = df["high"].values
    open_arr = df["open"].values
    close_arr = df["close"].values
    ma_arr = df["ma250"].values

    horizon = min(n, entry_i + 1 + (max_holding if max_holding else n))
    exit_price: Optional[float] = None
    exit_i = n - 1
    reason = "eod"
    for j in range(entry_i + 1, horizon):
        c = close_arr[j]
        if np.isnan(c):
            continue
        # 动态止损: 浮盈>=8% 后把止损抬到保本上方2%
        if stop_mode == "dynamic" and c >= entry * 1.08:
            stop = max(stop, entry * 1.02)
        # 止损触发 (跳空低开按开盘价, 否则按止损价)
        if open_arr[j] <= stop:
            exit_price, reason = float(open_arr[j]), "stop_loss"
            exit_i = j
            break
        if low_arr[j] <= stop:
            exit_price, reason = float(stop), "stop_loss"
            exit_i = j
            break
        # 收盘跌破年线 (趋势破坏离场) — 可选
        if exit_mode == "stop_trend" and not np.isnan(ma_arr[j]) and c < ma_arr[j]:
            exit_price, reason = float(c), "trend_break"
            exit_i = j
            break
    else:
        # 未触发 -> 持有到回测期结束或最大持有
        exit_i = min(n - 1, horizon - 1)
        exit_price = float(close_arr[exit_i])
        reason = "eod" if exit_i == n - 1 else "max_holding"

    exit_day = df["datetime"].iloc[exit_i]
    gross = (exit_price - entry) / entry
    net = (1 + gross) * (1 - cost_bps_buy - cost_bps_sell) - 1
    return Trade(
        signal.symbol, signal.name, signal.entry_type, signal.entry_date,
        entry, exit_day, exit_price, reason, float(gross), float(net),
        int(exit_i - entry_i),
    )


# --------------------------------------------------------------------------- #
# 组合模拟: 等权 + 最多 K 个并发仓位, 逐日盯市
# --------------------------------------------------------------------------- #
def run_portfolio(
    trades: list[Trade],
    global_calendar: list[pd.Timestamp],
    close_by_symbol: dict[str, dict],
    max_positions: int,
    initial_capital: float = 1.0,
) -> dict:
    """按入场日顺序等权开仓 (同标的不重复), 仓位数 <= max_positions. 逐日盯市."""
    active = sorted(
        (t for t in trades if t.exit_date is not None),
        key=lambda t: (t.entry_date, t.symbol),
    )
    by_day: dict[pd.Timestamp, list[Trade]] = {}
    for t in active:
        by_day.setdefault(t.entry_date, []).append(t)
    exits: dict[tuple[str, pd.Timestamp], Trade] = {}
    for t in active:
        exits[(t.symbol, t.entry_date)] = t

    cash = initial_capital
    per_slot = initial_capital / max_positions
    open_positions: list[dict] = []
    equity_curve: list[dict] = []
    closed: list[dict] = []

    for day in global_calendar:
        # 1) 离场 (按离场日 <= 当日结算)
        still_open: list[dict] = []
        for p in open_positions:
            key = (p["symbol"], p["entry_day"])
            tr = exits.get(key)
            if tr is not None and tr.exit_date <= day:
                proceeds = p["shares"] * tr.exit_price
                cash += proceeds
                closed.append({
                    "symbol": p["symbol"],
                    "entry_type": tr.entry_type,
                    "entry_date": str(tr.entry_date.date()),
                    "exit_date": str(tr.exit_date.date()),
                    "return_net": tr.return_net,
                    "exit_reason": tr.exit_reason,
                })
            else:
                still_open.append(p)
        open_positions = still_open
        # 2) 入场
        held = {p["symbol"] for p in open_positions}
        for t in by_day.get(day, []):
            if len(open_positions) >= max_positions or t.symbol in held:
                continue
            cap = per_slot
            if cash < cap:
                cap = max(cash, 0.0)
                if cap <= 0:
                    break
            shares = cap * (1 - 0.0003) / t.entry_price
            cash -= cap
            open_positions.append({
                "symbol": t.symbol,
                "entry_price": t.entry_price,
                "shares": shares,
                "entry_day": day,
            })
            held.add(t.symbol)
        # 3) 盯市 (以当日收盘价估值)
        nav = cash + sum(
            p["shares"] * close_by_symbol[p["symbol"]].get(day, p["entry_price"])
            for p in open_positions
        )
        equity_curve.append({"date": str(day.date()), "nav": float(nav)})

    # 回测期结束, 剩余持仓按最后估值了结
    final = equity_curve[-1]["nav"] if equity_curve else initial_capital
    max_drawdown = _max_drawdown([e["nav"] for e in equity_curve])
    return {
        "equity_curve": equity_curve,
        "closed_trades": closed,
        "final_nav": final,
        "total_return": final / initial_capital - 1,
        "max_drawdown_pct": max_drawdown,
        "open_at_end": len(open_positions),
    }


def _max_drawdown(navs: list[float]) -> float:
    peak = -np.inf
    mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return float(mdd)


# --------------------------------------------------------------------------- #
# 报告工具
# --------------------------------------------------------------------------- #
def summarize_trades(trades: list[Trade]) -> dict:
    if not trades:
        return {"count": 0}
    nets = np.array([t.return_net for t in trades])
    gross = np.array([t.return_gross for t in trades])
    wins = nets > 0
    return {
        "count": len(trades),
        "win_rate": float(wins.mean()),
        "avg_return_net": float(nets.mean()),
        "median_return_net": float(np.median(nets)),
        "avg_return_gross": float(gross.mean()),
        "best": float(nets.max()),
        "worst": float(nets.min()),
        "p10": float(np.percentile(nets, 10)),
        "p25": float(np.percentile(nets, 25)),
        "p75": float(np.percentile(nets, 75)),
        "p90": float(np.percentile(nets, 90)),
        "avg_holding_days": float(np.mean([t.holding_days for t in trades])),
        "std_return_net": float(nets.std()),
        "buckets_pct": {
            "gt50": float(np.mean(nets > 0.5)),
            "10_50": float(np.mean((nets > 0.10) & (nets <= 0.50))),
            "0_10": float(np.mean((nets > 0) & (nets <= 0.10))),
            "-5_0": float(np.mean((nets <= 0) & (nets > -0.05))),
            "lt-5": float(np.mean(nets <= -0.05)),
        },
        "by_reason": _bucket_by_reason(trades),
        "by_year": _bucket_by_year(trades),
    }


def _bucket_by_year(trades: list[Trade]) -> dict:
    out: dict[str, dict] = {}
    for year in sorted({t.entry_date.year for t in trades}):
        sub = [t for t in trades if t.entry_date.year == year]
        nets = np.array([t.return_net for t in sub])
        out[str(year)] = {
            "count": len(sub),
            "win_rate": float(np.mean(nets > 0)),
            "avg_return_net": float(nets.mean()),
            "median_return_net": float(np.median(nets)),
        }
    return out


def _bucket_by_reason(trades: list[Trade]) -> dict:
    out: dict[str, dict] = {}
    for reason in sorted({t.exit_reason for t in trades}):
        sub = [t for t in trades if t.exit_reason == reason]
        out[reason] = {
            "count": len(sub),
            "win_rate": float(np.mean([t.return_net > 0 for t in sub])),
            "avg_return_net": float(np.mean([t.return_net for t in sub])),
        }
    return out


def run_equal_weight_all(
    trades: list[Trade],
    global_calendar: list[pd.Timestamp],
    close_by_symbol: dict[str, dict],
    initial_capital: float = 1.0,
) -> dict:
    """等权持有所有并发信号 (无仓位上限), 每个信号等权, 直接检验单信号边际是否可复利."""
    active = sorted(
        (t for t in trades if t.exit_date is not None),
        key=lambda t: (t.entry_date, t.symbol),
    )
    by_day: dict[pd.Timestamp, list[Trade]] = {}
    for t in active:
        by_day.setdefault(t.entry_date, []).append(t)
    exits: dict[tuple[str, pd.Timestamp], Trade] = {}
    for t in active:
        exits[(t.symbol, t.entry_date)] = t

    # 每个并发仓位等权: 按当前 NAV / 同时持仓数分配
    open_positions: list[dict] = []
    equity_curve: list[dict] = []
    cash = initial_capital
    for day in global_calendar:
        # 离场
        still_open: list[dict] = []
        for p in open_positions:
            tr = exits.get((p["symbol"], p["entry_day"]))
            if tr is not None and tr.exit_date <= day:
                cash += p["shares"] * tr.exit_price
            else:
                still_open.append(p)
        open_positions = still_open
        # 入场 (每个信号等权: 用 NAV/enqueue 数)
        pending = [t for t in by_day.get(day, [])]
        if pending and len(open_positions) + len(pending) > 0:
            nav = cash + sum(p["shares"] * close_by_symbol[p["symbol"]].get(day, p["entry_price"])
                             for p in open_positions)
            count = len(open_positions) + len(pending)
            cap = nav / count
            held = {p["symbol"] for p in open_positions}
            for t in pending:
                if t.symbol in held:
                    continue
                if cash <= 0:
                    break
                shares = min(cap, cash) * (1 - 0.0003) / t.entry_price
                cost = shares * t.entry_price
                cash -= cost
                open_positions.append({
                    "symbol": t.symbol, "entry_day": t.entry_date,
                    "entry_price": t.entry_price, "shares": shares,
                })
                held.add(t.symbol)
        nav = cash + sum(p["shares"] * close_by_symbol[p["symbol"]].get(day, p["entry_price"])
                         for p in open_positions)
        equity_curve.append({"date": str(day.date()), "nav": float(nav)})

    final = equity_curve[-1]["nav"] if equity_curve else initial_capital
    return {
        "equity_curve": equity_curve,
        "final_nav": final,
        "total_return": final / initial_capital - 1,
        "max_drawdown_pct": _max_drawdown([e["nav"] for e in equity_curve]),
        "open_at_end": len(open_positions),
    }


def _max_concurrent(equity_curve, open_positions, close_by_symbol) -> int:
    return len(open_positions)


def index_stats(index_df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    idx = index_df[(index_df["datetime"] >= start) & (index_df["datetime"] <= end)]
    if idx.empty:
        return {}
    first = float(idx["close"].iloc[0])
    last = float(idx["close"].iloc[-1])
    years = (idx["datetime"].iloc[-1] - idx["datetime"].iloc[0]).days / 365.25
    return {
        "start": str(idx["datetime"].iloc[0].date()),
        "end": str(idx["datetime"].iloc[-1].date()),
        "buy_hold_return": last / first - 1,
        "annualized": (last / first) ** (1 / years) - 1 if years > 0 else None,
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 稳定上涨过滤对照研究
# --------------------------------------------------------------------------- #
FILTER_STUDY = {
    "baseline": [],
    "align": ["align"],
    "freq": ["freq"],
    "dd": ["dd"],
    "mom": ["mom"],
    "stable_all": ["align", "freq", "dd"],
    "stable_all_mom": ["align", "freq", "dd", "mom"],
}


def stable_mask(df: pd.DataFrame, filters: set[str]) -> np.ndarray:
    """对整张表计算'稳定上涨'过滤掩码 (因果, 只用当日及以前)."""
    n = len(df)
    mask = np.ones(n, dtype=bool)
    close = df["close"].values
    ma250 = df["ma250"].values
    slope = df["ma250_slope"].values
    if "align" in filters:
        mask &= close > df["ma60"].values
        mask &= df["ma60"].values > df["ma120"].values
    if "freq" in filters:
        mask &= df["above250_frac60"].values >= 0.8
    if "dd" in filters:
        mask &= close >= 0.85 * df["high120_close"].values
    if "mom" in filters:
        mask &= df["ret20"].values > 0
        mask &= df["ret60"].values > 0
    mask &= ~np.isnan(close)
    mask &= ~np.isnan(ma250)
    mask &= ~np.isnan(slope)
    return mask


def run_filter_study(out: str, exit_mode: str = "stop_trend",
                     only_freq: bool = False) -> None:
    vol_mult, tol = 1.5, 0.025
    cost_bps_buy = 0.0003
    cost_bps_sell = 0.0008

    universe = pd.read_pickle(UNIVERSE_FILE)
    index_df = pd.read_pickle(INDEX_FILE)
    index_df["datetime"] = pd.to_datetime(index_df["datetime"])
    global_calendar = list(pd.to_datetime(index_df["datetime"]))
    close_by_symbol: dict[str, dict] = {}

    # 每只股票 load/指标一次; 基础信号生成一次, 各过滤器以掩码筛选后分别模拟
    trades_by_filter: dict[str, list[Trade]] = {k: [] for k in FILTER_STUDY}
    study_names = ["freq"] if only_freq else list(FILTER_STUDY)
    usable = 0
    bars_total = 0
    filter_sets = {name: set(flt) for name, flt in FILTER_STUDY.items()}
    for row in universe.itertuples(index=False):
        code = str(row.code)
        name = str(row.name)
        path = HISTORY_DIR / f"{code}_qfq.pkl"
        if not path.exists():
            continue
        try:
            df = pd.read_pickle(path)
        except Exception:
            continue
        if len(df) < MA_YEAR + MA_SLOPE_LOOKBACK + 5:
            continue
        df = add_indicators(df)
        usable += 1
        bars_total += len(df)
        close_by_symbol[code] = dict(
            zip(pd.to_datetime(df["datetime"]),
                pd.to_numeric(df["close"], errors="coerce").tolist())
        )
        masks = {fname: stable_mask(df, fs) for fname, fs in filter_sets.items()}
        base_sigs = generate_signals(code, name, df, vol_mult=vol_mult, tol=tol)
        for fname in study_names:
            m = masks[fname]
            for s in base_sigs:
                if not m[s.entry_idx]:
                    continue
                t = simulate_trade(df, s, "dynamic", cost_bps_buy, cost_bps_sell,
                                   max_holding=None, exit_mode=exit_mode)
                if t is not None:
                    trades_by_filter[fname].append(t)

    report_cells = {k: summarize_trades(v) for k, v in trades_by_filter.items()}

    # 基准与"稳定上涨"组合口径
    # 基准窗口 (优先 baseline, 缺失则用 freq)
    ref_trades = trades_by_filter["baseline"] or trades_by_filter["freq"]
    start = min(t.entry_date for t in ref_trades)
    end = max(t.exit_date for t in ref_trades if t.exit_date is not None)
    bh = index_stats(index_df, start, end)
    portfolios = {}
    for key in ["baseline", "freq", "stable_all"]:
        portfolios[key] = {
            "10slot": {k: v for k, v in run_portfolio(
                trades_by_filter[key], global_calendar, close_by_symbol, 10).items()
                if k not in ("equity_curve", "closed_trades")},
            "eq_all": {k: v for k, v in run_equal_weight_all(
                trades_by_filter[key], global_calendar, close_by_symbol).items()
                if k not in ("equity_curve",)},
        }

    report = {
        "params": {"vol_mult": vol_mult, "tol": tol, "stop_mode": "dynamic",
                   "exit_mode": exit_mode, "cost_bps_buy": 3.0, "cost_bps_sell": 8.0},
        "data": {"universe_size": int(len(universe)), "usable_stocks": usable,
                 "avg_bars": round(bars_total / usable, 1) if usable else 0,
                 "note": "对照不同'稳定上涨'过滤对策略单笔/组合的影响; 幸存者偏差. filters: align=close>ma60>ma120, freq=近60日80%收盘站上年线, dd=距120日高点回撤<=15%, mom=20/60日收益为正."},
        "index_buy_hold": bh,
        "cells": report_cells,
        "portfolios": portfolios,
    }
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="年线(250日)趋势策略回测")
    parser.add_argument("--main-only", action="store_true",
                        help="只跑主配置 vm1.5/tol0.025, 跳过参数网格")
    parser.add_argument("--out", default=str(BASE_DIR / "yearline_trend_strategy_report.json"))
    parser.add_argument("--exit-mode", default="stop_trend",
                        choices=["stop_trend", "stop_only"],
                        help="stop_trend=止损+跌破年线离场; stop_only=仅止损离场")
    parser.add_argument("--filter-study", action="store_true",
                        help="运行'稳定上涨'过滤对照研究")
    parser.add_argument("--only-freq", action="store_true",
                        help="过滤研究只跑 freq 过滤器 (配合 --exit-mode)")
    args = parser.parse_args()

    if args.filter_study:
        run_filter_study(args.out, exit_mode=args.exit_mode, only_freq=args.only_freq)
        return

    if args.main_only:
        vol_mults = [1.5]
        tols = [0.025]
    else:
        vol_mults = [1.5, 1.0, 2.0]
        tols = [0.025, 0.02, 0.03]
    cost_bps_buy = 0.0003   # 佣金
    cost_bps_sell = 0.0008  # 佣金 + 印花税0.05% + 滑点

    universe = pd.read_pickle(UNIVERSE_FILE)
    index_df = pd.read_pickle(INDEX_FILE)
    index_df["datetime"] = pd.to_datetime(index_df["datetime"])
    global_calendar = list(pd.to_datetime(index_df["datetime"]))

    # 按股票池 load 一次, 对每个 (vol_mult,tol) 生成信号, 对每个止损模式模拟交易
    results: dict[str, dict] = {}
    close_by_symbol: dict[str, dict] = {}
    stats_by_cell: dict = {}

    usable = 0
    bars_total = 0
    for row in universe.itertuples(index=False):
        code = str(row.code)
        name = str(row.name)
        path = HISTORY_DIR / f"{code}_qfq.pkl"
        if not path.exists():
            continue
        try:
            df = pd.read_pickle(path)
        except Exception:
            continue
        if len(df) < MA_YEAR + MA_SLOPE_LOOKBACK + 5:
            continue
        df = add_indicators(df)
        usable += 1
        bars_total += len(df)
        close_by_symbol[code] = dict(
            zip(pd.to_datetime(df["datetime"]),
                pd.to_numeric(df["close"], errors="coerce").tolist())
        )

        # 每个参数格
        for vm in vol_mults:
            for tol in tols:
                cell = f"vm{vm}_tol{tol}"
                sigs = generate_signals(code, name, df, vol_mult=vm, tol=tol)
                if not sigs:
                    continue
                cell_trades = stats_by_cell.setdefault(cell, [])
                for s in sigs:
                    for sm in STOP_MODES:
                        t = simulate_trade(
                            df, s, sm, cost_bps_buy, cost_bps_sell,
                            max_holding=None, exit_mode=args.exit_mode,
                        )
                        if t is not None:
                            cell_trades.append((sm, t))

    # 汇总
    report_cells = {}
    for cell, trades in stats_by_cell.items():
        by_stop: dict[str, list[Trade]] = {sm: [] for sm in STOP_MODES}
        for sm, t in trades:
            by_stop[sm].append(t)
        report_cells[cell] = {
            sm: summarize_trades(sub) for sm, sub in by_stop.items()
        }

    # 基准与主配置 (vm1.5_tol0.025 dynamic)
    main_cell = "vm1.5_tol0.025"
    main_trades = [t for sm, t in stats_by_cell[main_cell] if sm == "dynamic"]
    if main_trades:
        start = min(t.entry_date for t in main_trades)
        end = max(t.exit_date for t in main_trades if t.exit_date is not None)
        bh = index_stats(index_df, start, end)
    else:
        bh = {}

    # 组合模拟 (主配置): 10 仓顺序分配 + 等权全信号
    portfolio = run_portfolio(
        main_trades, global_calendar, close_by_symbol, max_positions=10,
    )
    eq_all = run_equal_weight_all(main_trades, global_calendar, close_by_symbol)

    # 按年指数 buy&hold 对比
    yearly_index = {}
    idx_index = pd.DatetimeIndex(pd.to_datetime(index_df["datetime"]))
    idx_close = index_df["close"].astype(float).values
    for year in sorted({t.entry_date.year for t in main_trades}):
        y_start = idx_index[idx_index >= pd.Timestamp(year, 1, 1)]
        y_end = idx_index[idx_index < pd.Timestamp(year + 1, 1, 1)]
        if len(y_start) and len(y_end):
            s0 = float(idx_close[idx_index.get_loc(y_start[0])])
            s1 = float(idx_close[idx_index.get_loc(y_end[-1])])
            yearly_index[str(year)] = {"index_buy_hold": s1 / s0 - 1}

    report = {
        "params": {
            "ma": MA_YEAR,
            "vol_mults": vol_mults,
            "tols": tols,
            "stop_modes": STOP_MODES,
            "cost_bps_buy": 3.0,
            "cost_bps_sell": 8.0,
            "max_positions": 10,
            "max_holding": None,
        },
        "data": {
            "universe_size": int(len(universe)),
            "usable_stocks": usable,
            "avg_bars": round(bars_total / usable, 1) if usable else 0,
            "note": "当前成分股票清单(幸存者偏差); 信号窗口约2024-06起; 无最大持有上限, trend_break/止损/eod 三种离场.",
        },
        "index_buy_hold": bh,
        "index_buy_hold_by_year": yearly_index,
        "config_summary": report_cells,
        "portfolio_10slot": {
            **{k: v for k, v in portfolio.items() if k != "equity_curve" and k != "closed_trades"},
            "equity_curve_len": len(portfolio["equity_curve"]),
        },
        "portfolio_equal_weight_all": {
            **{k: v for k, v in eq_all.items() if k != "equity_curve"},
            "equity_curve_len": len(eq_all["equity_curve"]),
        },
        "main_config_trade_stats": summarize_trades(main_trades),
    }

    out = Path(args.out)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()