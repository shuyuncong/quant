"""首板放量涨停后的缩量回调 —— 事件研究 + 资金曲线回测。

预注册口径 (首次运行前固定):
- 股票池: 本地 qfq 日线, 排除北交所 (4/8/92); 上市/历史 >=250 根 K 线;
  涨停日收盘价 >=2 元, 涨停日前 20 日日均成交额 >=2000 万元。
- 首板: 涨停日之前 20 个交易日内没有涨停。
- 放量: 涨停日成交额 >= 前 20 个交易日日均成交额的 2.0 倍。
- 缩量回调: 涨停后第 1~3 个交易日内, 首次出现
  close < 涨停日 close, 回撤不超过 8%, 且
  amount <= 0.6 * 涨停日 amount, amount <= 涨停日前 20 日均额。
- 入场: 回调日收盘确认, 次一交易日开盘买入; 开盘涨停则放弃。
- 出场: 买入后最多持有 5 个交易日; 收盘跌破 -5% 止损,
  收盘涨破 +10% 止盈, 均于次日开盘卖出; 到期于第 5 个收盘后的
  下一开盘卖出; 跌停无法卖出则顺延, 最多 5 个交易日。
- 成本: 佣金 3bp/边, 卖出印花税 10bp, 滑点 10bp/边 (压力 20bp/边)。
- 组合: 100 万初始资金, 单票 5%, 最多 20 只, 同日候选按放量倍数排序;
  不追加杠杆, 现金不计息。

这是研究脚本, 不接生产、不自动交易。
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
import pandas as pd

import backtest_winrate as bt

BASE_DIR = pathlib.Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache" / "daily_history"
HISTORY_DIR = BASE_DIR / "cache" / "baostock_history_20261026"
INITIAL_CAPITAL = 1_000_000.0
LOT = 100
COMMISSION = 0.0003
STAMP_TAX = 0.0010
BASE_SLIPPAGE = 0.0010
STRESS_SLIPPAGE = 0.0020
SPLICE_DATE = pd.Timestamp("2024-12-31")


@dataclass(frozen=True)
class SignalConfig:
    start: str = "2019-01-01"
    end: str = "2026-10-30"
    min_history: int = 250
    min_price: float = 2.0
    min_adv20: float = 20_000_000.0
    first_board_lookback: int = 20
    volume_multiple: float = 2.0
    max_pullback: float = 0.08
    shrink_ratio: float = 0.6
    max_wait_days: int = 3


@dataclass(frozen=True)
class ExitConfig:
    hold_days: int = 5
    stop_loss: float = 0.05
    take_profit: float = 0.10
    max_exit_defer: int = 5


@dataclass(frozen=True)
class PortfolioConfig:
    initial_capital: float = INITIAL_CAPITAL
    max_positions: int = 20
    position_pct: float = 0.05


def _repair_amount(frame: pd.DataFrame) -> pd.DataFrame:
    """Recent Sina rows sometimes have amount=0; volume is in lots there."""
    if frame.empty:
        return frame
    frame = frame.copy()
    if "amount" not in frame.columns:
        frame["amount"] = np.nan
    amount = pd.to_numeric(frame["amount"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    fallback = volume * pd.to_numeric(frame["close"], errors="coerce") * 100.0
    frame["amount"] = amount.where(np.isfinite(amount) & (amount > 0), fallback)
    return frame


def load_bars() -> dict[str, pd.DataFrame]:
    """Merge BaoStock 2016-2024 qfq bars with the recent 2025-2026 cache.

    The smallcap panel and BaoStock cache are both adjusted to the same
    2024-12-31 basis; recent bars are appended only strictly after the
    historical tail, so no return is computed across the splice.
    """
    if not HISTORY_DIR.exists() and not CACHE_DIR.exists():
        raise FileNotFoundError("missing both historical and recent daily caches")

    bars: dict[str, pd.DataFrame] = {}
    for path in sorted(HISTORY_DIR.glob("*_qfq.pkl")):
        symbol = path.name.split("_", 1)[0]
        code = bt._symbol_code(symbol)
        if code.startswith(("4", "8", "92")):
            continue
        try:
            historical = bt.prepare_closed_bars(pd.read_pickle(path))
        except Exception:
            continue
        if historical.empty:
            continue
        recent_path = CACHE_DIR / f"{symbol}_qfq.pkl"
        if recent_path.exists():
            try:
                recent = bt.prepare_closed_bars(pd.read_pickle(recent_path))
            except Exception:
                recent = pd.DataFrame()
            if not recent.empty:
                recent = _repair_amount(recent)
                recent = recent[recent["datetime"] > historical["datetime"].max()]
                if not recent.empty:
                    # Keep the first recent bar so the following bar has a
                    # valid same-basis prior close, but never signal on the
                    # spliced return itself.
                    recent = recent.copy()
                    recent["splice_break"] = False
                    recent.iloc[0, recent.columns.get_loc("splice_break")] = True
                    historical = pd.concat([historical, recent], ignore_index=True)
        bars[code] = historical.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)

    # Symbols that listed after the BaoStock backfill only have recent bars.
    for path in sorted(CACHE_DIR.glob("*_qfq.pkl")):
        symbol = path.name.split("_", 1)[0]
        code = bt._symbol_code(symbol)
        if code in bars or code.startswith(("4", "8", "92")):
            continue
        try:
            recent = bt.prepare_closed_bars(pd.read_pickle(path))
        except Exception:
            continue
        if recent.empty:
            continue
        recent = _repair_amount(recent)
        bars[code] = recent.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    return bars


def limit_rate_series(symbol: str, dates: pd.Series) -> np.ndarray:
    """Daily price-limit rate, vectorized; ST history is unavailable."""
    code = bt._symbol_code(symbol)
    rates = np.full(len(dates), 0.10, dtype=float)
    if code.startswith(("688", "689")):
        rates[:] = 0.20
    elif code.startswith(("300", "301")):
        rates[:] = 0.10
        rates[dates >= pd.Timestamp("2020-08-24")] = 0.20
    return rates


def _is_limit_up_at(close: np.ndarray, high: np.ndarray, prev: np.ndarray, rates: np.ndarray) -> np.ndarray:
    # high == close means the close was sealed at the upper limit; qfq rounding
    # makes an exact equality check unreliable, so allow 0.5 cents.
    return (close / prev - 1.0 >= rates - 0.005) & (high >= close - 0.005)


def _is_limit_down_open(open_: float, prev_close: float, rate: float) -> bool:
    return bool(prev_close > 0 and open_ / prev_close - 1.0 <= -rate + 0.005)


def _candidate_after_quiet_prior(limit_up: np.ndarray, t: int, lookback: int) -> bool:
    start = max(0, t - lookback)
    return not bool(limit_up[start:t].any())


def _simulate_exit(
    symbol: str,
    bars: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    rates: np.ndarray,
    exit_cfg: ExitConfig,
) -> dict[str, Any] | None:
    n = len(bars)
    open_ = bars["open"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    stop = entry_price * (1.0 - exit_cfg.stop_loss)
    take = entry_price * (1.0 + exit_cfg.take_profit)
    trigger_idx: int | None = None
    reason = "timeout"

    last_check = min(entry_idx + exit_cfg.hold_days, n)
    for j in range(entry_idx, last_check):
        if close[j] <= stop:
            trigger_idx = j + 1
            reason = "stop"
            break
        if close[j] >= take:
            trigger_idx = j + 1
            reason = "take_profit"
            break
    if trigger_idx is None:
        trigger_idx = entry_idx + exit_cfg.hold_days
        reason = "timeout"
    if trigger_idx >= n:
        return None

    # A-share T+1 and limit-down: cannot sell at an open that is locked down.
    deferred = 0
    while trigger_idx < n and _is_limit_down_open(open_[trigger_idx], close[trigger_idx - 1], rates[trigger_idx]):
        trigger_idx += 1
        deferred += 1
        if deferred > exit_cfg.max_exit_defer:
            return None
    if trigger_idx >= n:
        return None
    exit_price = float(open_[trigger_idx])
    if not math.isfinite(exit_price) or exit_price <= 0:
        return None
    return {
        "exit_idx": int(trigger_idx),
        "exit_date": pd.Timestamp(bars["datetime"].iloc[trigger_idx]),
        "exit_price": exit_price,
        "exit_reason": reason,
        "hold_days": int(trigger_idx - entry_idx),
        "deferred_exit_days": int(deferred),
    }


def detect_candidates(
    symbol: str,
    bars: pd.DataFrame,
    signal_cfg: SignalConfig,
    exit_cfg: ExitConfig,
) -> list[dict[str, Any]]:
    """Return independent trades for every qualifying first-board pullback."""
    if len(bars) < signal_cfg.min_history + signal_cfg.max_wait_days + exit_cfg.hold_days:
        return []
    dates = bars["datetime"]
    close = bars["close"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    open_ = bars["open"].to_numpy(dtype=float)
    amount = bars["amount"].to_numpy(dtype=float)
    volume = bars["volume"].to_numpy(dtype=float)
    if not np.isfinite(amount).all() or (amount <= 0).any():
        # A few old rows have no turnover value; volume*close is a usable proxy.
        fallback = volume * close
        amount = np.where(np.isfinite(amount) & (amount > 0), amount, fallback)
    if not np.isfinite(amount).all() or (amount <= 0).any():
        return []

    rates = limit_rate_series(symbol, dates)
    prev_close = np.r_[np.nan, close[:-1]]
    returns = close / prev_close - 1.0
    limit_up = _is_limit_up_at(close, high, prev_close, rates)
    limit_up[0] = False
    if "splice_break" in bars.columns:
        limit_up[bars["splice_break"].fillna(False).to_numpy(dtype=bool)] = False

    amount_s = pd.Series(amount)
    avg20 = amount_s.shift(1).rolling(20, min_periods=20).mean().to_numpy()
    vol_multiple = amount / avg20
    with np.errstate(invalid="ignore"):
        first_board = np.zeros(len(bars), dtype=bool)
        for t in np.flatnonzero(limit_up):
            if t < signal_cfg.min_history:
                continue
            first_board[t] = _candidate_after_quiet_prior(
                limit_up, t, signal_cfg.first_board_lookback
            )

    out: list[dict[str, Any]] = []
    for t in np.flatnonzero(first_board):
        if not np.isfinite(avg20[t]) or avg20[t] < signal_cfg.min_adv20:
            continue
        if close[t] < signal_cfg.min_price:
            continue
        if vol_multiple[t] < signal_cfg.volume_multiple:
            continue

        limit_close = float(close[t])
        limit_amount = float(amount[t])
        limit_date = pd.Timestamp(dates.iloc[t])
        for k in range(1, signal_cfg.max_wait_days + 1):
            j = t + k
            if j + 1 >= len(bars):
                break
            pullback_close = float(close[j])
            if pullback_close >= limit_close:
                continue
            if pullback_close < limit_close * (1.0 - signal_cfg.max_pullback):
                break
            pullback_amount = float(amount[j])
            if pullback_amount > signal_cfg.shrink_ratio * limit_amount:
                continue
            if pullback_amount > avg20[t]:
                continue

            entry_idx = j + 1
            entry_open = float(open_[entry_idx])
            if not math.isfinite(entry_open) or entry_open <= 0:
                break
            if entry_open / close[j] - 1.0 >= rates[entry_idx] - 0.005:
                # Opening at the upper limit: no realistic fill; skip this event.
                break
            exit_info = _simulate_exit(symbol, bars, entry_idx, entry_open, rates, exit_cfg)
            if exit_info is None:
                break
            entry_date = pd.Timestamp(dates.iloc[entry_idx])
            if entry_date <= SPLICE_DATE < exit_info["exit_date"]:
                # Entry and exit sit on different qfq bases; do not treat the
                # splice as a tradable return.
                break
            out.append(
                {
                    "symbol": symbol,
                    "limit_date": limit_date,
                    "signal_date": pd.Timestamp(dates.iloc[j]),
                    "entry_date": entry_date,
                    "limit_close": limit_close,
                    "limit_amount": limit_amount,
                    "volume_multiple": float(vol_multiple[t]),
                    "pullback_pct": float(pullback_close / limit_close - 1.0),
                    "pullback_volume_ratio": float(pullback_amount / limit_amount),
                    "pullback_wait_days": int(k),
                    "entry_open": entry_open,
                    **exit_info,
                }
            )
            break
    return out


def net_trade_return(trade: dict[str, Any], slippage: float) -> float:
    entry_cost = float(trade["entry_open"]) * (1.0 + slippage) * (1.0 + COMMISSION)
    exit_net = float(trade["exit_price"]) * (1.0 - slippage) * (1.0 - COMMISSION - STAMP_TAX)
    return exit_net / entry_cost - 1.0


def event_stats(trades: list[dict[str, Any]], slippage: float) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    returns = np.array([net_trade_return(t, slippage) for t in trades], dtype=float)
    gross = np.array(
        [float(t["exit_price"]) / float(t["entry_open"]) - 1.0 for t in trades],
        dtype=float,
    )
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    mean = float(returns.mean())
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    t_stat = mean / (std / math.sqrt(len(returns))) if std > 0 else 0.0
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    by_reason: dict[str, Any] = {}
    for trade, ret in zip(trades, returns):
        reason = str(trade.get("exit_reason", "unknown"))
        bucket = by_reason.setdefault(reason, {"trades": 0, "sum_ret": 0.0, "wins": 0})
        bucket["trades"] += 1
        bucket["sum_ret"] += float(ret)
        bucket["wins"] += int(ret > 0)
    for bucket in by_reason.values():
        bucket["mean_pct"] = round(bucket.pop("sum_ret") / bucket["trades"] * 100, 2)
        bucket["win_rate_pct"] = round(bucket.pop("wins") / bucket["trades"] * 100, 2)
    return {
        "trades": int(len(returns)),
        "mean_pct": round(mean * 100, 2),
        "gross_mean_pct": round(float(gross.mean()) * 100, 2),
        "median_pct": round(float(np.median(returns)) * 100, 2),
        "std_pct": round(std * 100, 2),
        "t_stat": round(t_stat, 2),
        "win_rate_pct": round(float((returns > 0).mean()) * 100, 2),
        "gross_win_rate_pct": round(float((gross > 0).mean()) * 100, 2),
        "avg_win_pct": round(float(wins.mean()) * 100, 2) if len(wins) else 0.0,
        "avg_loss_pct": round(float(losses.mean()) * 100, 2) if len(losses) else 0.0,
        "payoff": round(float(wins.mean() / abs(losses.mean())), 2) if len(wins) and len(losses) else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "best_pct": round(float(returns.max()) * 100, 2),
        "worst_pct": round(float(returns.min()) * 100, 2),
        "by_exit_reason": by_reason,
    }


def yearly_event_stats(trades: list[dict[str, Any]], slippage: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not trades:
        return out
    frame = pd.DataFrame(
        {
            "year": [t["entry_date"].year for t in trades],
            "ret": [net_trade_return(t, slippage) for t in trades],
        }
    )
    for year, group in frame.groupby("year"):
        out[str(int(year))] = {
            "trades": int(len(group)),
            "mean_pct": round(float(group["ret"].mean()) * 100, 2),
            "win_rate_pct": round(float((group["ret"] > 0).mean()) * 100, 2),
        }
    return out


def _subset_period(trades: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end)
    return [t for t in trades if lo <= t["entry_date"] <= hi]


def _portfolio_stats(equity: pd.Series, exposure: pd.Series, positions: pd.Series) -> dict[str, Any]:
    if len(equity) < 2:
        return {}
    daily = equity.pct_change().dropna()
    n = len(daily)
    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (252.0 / n) - 1.0)
    vol = float(daily.std(ddof=1) * math.sqrt(252))
    sharpe = float(daily.mean() / daily.std(ddof=1) * math.sqrt(252)) if daily.std(ddof=1) > 0 else 0.0
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "annual_vol_pct": round(vol * 100, 2),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(float(drawdown.min()) * 100, 2),
        "avg_exposure_pct": round(float(exposure.mean()), 2),
        "max_positions_held": int(positions.max()),
        "days": int(len(daily)),
    }


def simulate_portfolio(
    trades: list[dict[str, Any]],
    bars: dict[str, pd.DataFrame],
    signal_cfg: SignalConfig,
    portfolio_cfg: PortfolioConfig,
    slippage: float,
) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    """Capital-constrained daily portfolio using precomputed candidate exits."""
    lo = pd.Timestamp(signal_cfg.start)
    hi = pd.Timestamp(signal_cfg.end)
    candidates = [
        t
        for t in trades
        if lo <= t["entry_date"] <= hi and t["exit_date"] <= hi
    ]
    if not candidates:
        return pd.DataFrame(), {}, []

    # Use the longest local series as the market calendar, then add candidate dates.
    calendar_symbol = max(bars, key=lambda s: len(bars[s]))
    dates = pd.DatetimeIndex(
        sorted(
            set(pd.to_datetime(bars[calendar_symbol]["datetime"]))
            | set(t["entry_date"] for t in candidates)
            | set(t["exit_date"] for t in candidates)
        )
    )
    dates = dates[(dates >= lo) & (dates <= hi)]

    close_maps: dict[str, pd.Series] = {}
    for symbol in {t["symbol"] for t in candidates}:
        close_maps[symbol] = (
            bars[symbol].set_index("datetime")["close"].reindex(dates).ffill()
        )

    by_entry: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    for trade in candidates:
        by_entry.setdefault(trade["entry_date"], []).append(trade)
    for values in by_entry.values():
        values.sort(key=lambda t: (-t["volume_multiple"], t["symbol"]))

    cash = float(portfolio_cfg.initial_capital)
    positions: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    skipped_slots = 0
    skipped_cash = 0
    skipped_symbol = 0

    for day in dates:
        # Exits first: proceeds are available for the same open.
        still: list[dict[str, Any]] = []
        for pos in positions:
            trade = pos["trade"]
            if trade["exit_date"] == day:
                qty = pos["qty"]
                proceeds = qty * float(trade["exit_price"]) * (1.0 - slippage) * (
                    1.0 - COMMISSION - STAMP_TAX
                )
                cash += proceeds
                accepted.append(
                    {
                        **trade,
                        "qty": qty,
                        "entry_cost": pos["entry_cost"],
                        "exit_proceeds": proceeds,
                        "net_return": proceeds / pos["entry_cost"] - 1.0,
                    }
                )
            else:
                still.append(pos)
        positions = still

        held = {pos["trade"]["symbol"] for pos in positions}
        for trade in by_entry.get(day, []):
            if len(positions) >= portfolio_cfg.max_positions:
                skipped_slots += 1
                continue
            symbol = trade["symbol"]
            if symbol in held:
                skipped_symbol += 1
                continue
            fill = float(trade["entry_open"]) * (1.0 + slippage)
            target = portfolio_cfg.initial_capital * portfolio_cfg.position_pct
            qty = int(target // (fill * LOT)) * LOT
            if qty <= 0:
                skipped_cash += 1
                continue
            cost = qty * fill * (1.0 + COMMISSION)
            if cost > cash:
                skipped_cash += 1
                continue
            cash -= cost
            positions.append(
                {
                    "trade": trade,
                    "qty": qty,
                    "entry_cost": cost,
                    "entry_fill": fill,
                }
            )
            held.add(symbol)

        invested = 0.0
        for pos in positions:
            close = close_maps[pos["trade"]["symbol"]].get(day, np.nan)
            if not math.isfinite(float(close)):
                close = pos["entry_fill"]
            invested += pos["qty"] * float(close)
        equity = cash + invested
        curve.append(
            {
                "date": day,
                "equity": equity,
                "cash": cash,
                "invested": invested,
                "positions": len(positions),
                "exposure_pct": (invested / equity * 100.0) if equity > 0 else 0.0,
            }
        )

    frame = pd.DataFrame(curve)
    stats = _portfolio_stats(
        frame.set_index("date")["equity"],
        frame.set_index("date")["exposure_pct"],
        frame.set_index("date")["positions"],
    )
    stats.update(
        {
            "accepted_trades": int(len(accepted)),
            "skipped_slots": int(skipped_slots),
            "skipped_cash": int(skipped_cash),
            "skipped_symbol_already_held": int(skipped_symbol),
            "slippage": slippage,
        }
    )
    return frame, stats, accepted


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(BASE_DIR / "first_board_pullback_results"))
    parser.add_argument("--start", default=SignalConfig.start)
    parser.add_argument("--end", default=SignalConfig.end)
    parser.add_argument("--hold-days", type=int, default=ExitConfig.hold_days)
    parser.add_argument("--stop-loss", type=float, default=ExitConfig.stop_loss)
    parser.add_argument("--take-profit", type=float, default=ExitConfig.take_profit)
    parser.add_argument("--volume-multiple", type=float, default=SignalConfig.volume_multiple)
    parser.add_argument("--shrink-ratio", type=float, default=SignalConfig.shrink_ratio)
    parser.add_argument("--max-pullback", type=float, default=SignalConfig.max_pullback)
    parser.add_argument("--max-positions", type=int, default=PortfolioConfig.max_positions)
    parser.add_argument("--position-pct", type=float, default=PortfolioConfig.position_pct)
    args = parser.parse_args()

    if not 0 < args.stop_loss < 1 or not 0 < args.take_profit < 1:
        raise SystemExit("stop-loss/take-profit must be fractions in (0, 1)")
    signal_cfg = SignalConfig(
        start=args.start,
        end=args.end,
        volume_multiple=args.volume_multiple,
        shrink_ratio=args.shrink_ratio,
        max_pullback=args.max_pullback,
    )
    exit_cfg = ExitConfig(
        hold_days=args.hold_days,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
    )
    portfolio_cfg = PortfolioConfig(
        max_positions=args.max_positions,
        position_pct=args.position_pct,
    )

    print("loading bars ...", flush=True)
    bars = load_bars()
    print(f"loaded {len(bars)} symbols", flush=True)

    all_trades: list[dict[str, Any]] = []
    for i, (symbol, frame) in enumerate(bars.items(), 1):
        all_trades.extend(detect_candidates(symbol, frame, signal_cfg, exit_cfg))
        if i % 1000 == 0:
            print(f"  scanned {i}/{len(bars)} symbols, {len(all_trades)} trades", flush=True)
    all_trades.sort(key=lambda t: (t["entry_date"], t["symbol"]))
    all_trades = [
        t
        for t in all_trades
        if pd.Timestamp(signal_cfg.start) <= t["entry_date"] <= pd.Timestamp(signal_cfg.end)
    ]

    base_event = event_stats(all_trades, BASE_SLIPPAGE)
    stress_event = event_stats(all_trades, STRESS_SLIPPAGE)
    design = _subset_period(all_trades, signal_cfg.start, "2024-12-31")
    holdout = _subset_period(all_trades, "2025-01-01", signal_cfg.end)

    print("simulating base portfolio ...", flush=True)
    base_curve, base_port, accepted = simulate_portfolio(
        all_trades, bars, signal_cfg, portfolio_cfg, BASE_SLIPPAGE
    )
    print("simulating stress portfolio ...", flush=True)
    stress_curve, stress_port, _ = simulate_portfolio(
        all_trades, bars, signal_cfg, portfolio_cfg, STRESS_SLIPPAGE
    )

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    trades_frame = pd.DataFrame(all_trades)
    trades_frame["net_return_pct"] = [
        round(net_trade_return(t, BASE_SLIPPAGE) * 100, 3) for t in all_trades
    ]
    trades_frame["stress_net_return_pct"] = [
        round(net_trade_return(t, STRESS_SLIPPAGE) * 100, 3) for t in all_trades
    ]
    trades_frame.to_csv(out / "trades.csv", index=False)
    base_curve.to_csv(out / "portfolio_curve.csv", index=False)
    stress_curve.to_csv(out / "portfolio_curve_20bp.csv", index=False)

    summary: dict[str, Any] = {
        "strategy": "first-board high-volume limit-up, shrinking-volume pullback",
        "signal": asdict(signal_cfg),
        "exit": asdict(exit_cfg),
        "portfolio": asdict(portfolio_cfg),
        "costs": {
            "commission_per_side": COMMISSION,
            "stamp_tax_sell": STAMP_TAX,
            "slippage_base": BASE_SLIPPAGE,
            "slippage_stress": STRESS_SLIPPAGE,
        },
        "data": {
            "symbols": len(bars),
            "start": signal_cfg.start,
            "end": signal_cfg.end,
            "source": "BaoStock qfq 2016-2024 + local qfq 2025-2026; BSE excluded",
            "limitations": [
                "no historical ST list; ST 5% boards are effectively not detected",
                "no historical index constituent list; full local A-share cache",
                "qfq prices; dividends not separately modeled",
                "delisted symbols are included only if local bars exist",
                "2024-12-31 data splice; cross-splice entries/exits are dropped",
            ],
        },
        "event_base": base_event,
        "event_stress_20bp": stress_event,
        "event_design_2019_2024": event_stats(design, BASE_SLIPPAGE),
        "event_holdout_2025_2026": event_stats(holdout, BASE_SLIPPAGE),
        "event_yearly": yearly_event_stats(all_trades, BASE_SLIPPAGE),
        "portfolio_base": base_port,
        "portfolio_stress_20bp": stress_port,
        "accepted_trades": len(accepted),
    }
    (out / "summary.json").write_text(
        json.dumps(_jsonable(summary), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(_jsonable({
        "trades": len(all_trades),
        "event_base": base_event,
        "event_design": summary["event_design_2019_2024"],
        "event_holdout": summary["event_holdout_2025_2026"],
        "portfolio_base": base_port,
        "portfolio_stress_20bp": stress_port,
    }), ensure_ascii=False, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
