"""Daily-bar approximation of a small-cap / CSI 1000 rotation strategy.

This is a research harness, not a production strategy.  The source strategy
description uses intraday checks and a nine-point annual-report audit; neither
is available in the local daily-bar dataset.  The harness therefore tests the
price-based core and labels every omitted component explicitly in the output.

Key approximation choices
-------------------------
* Universe: PIT float-market-cap rank 801-1800 as a CSI 1000 proxy.  The true
  index constituents are not available historically.
* Signal: previous close; execution: next open (Tuesday rebalance).
* Stops / take-profit: daily open-low-high bar; stop wins if both stop and
  take-profit are touched on the same bar.
* MACD divergence: index 399101, price high within the last 5 sessions, DIF not
  at a 60-day high, histogram turns negative.
* Microcap breadth: smallest 5% by float market cap, prior-day return
  consistency, 120-day upper band; skipped in a bull tape.
* Volume exit: prior-day turnover > 10% and > 2x its 20-day mean, sell next open.
* No fundamentals, no audit opinion / goodwill / pledge / regulatory data, no
  intraday 9:31/10:00/10:30/14:00/14:50 sequencing.
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quality_filter_study import extend_delisted_panels, extend_panels

# Study artifacts live next to the code (``cache/`` is gitignored); the old
# ``D:/tmp/...`` defaults only resolved on the Windows research machine.
STUDY_DIR = Path(__file__).resolve().parent / "cache" / "smallcap_study"


@dataclass
class Config:
    initial: float = 250_000.0
    start: str = "2020-01-02"
    end: str = "2024-12-31"
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    slippage: float = 0.0005
    lot: int = 100
    min_history: int = 250

    rank_low: int = 801
    rank_high: int = 1800
    max_names: int = 30

    stop_loss: float = 0.09
    atr_window: int = 14
    atr_mult: float = 2.0
    take_profit: float = 1.0
    protect_1: float = 0.15
    protect_2: float = 0.30
    protect_2_stop: float = 0.10

    pause_days: int = 10
    cooldown_days: int = 2
    macd_lookback: int = 60
    macd_recent: int = 5
    micro_pct: float = 0.05
    micro_lookback: int = 120
    volume_turnover: float = 10.0
    volume_mult: float = 2.0


@dataclass
class Position:
    shares: int
    entry_price: float
    entry_date: pd.Timestamp
    highest: float
    stop_price: float
    atr_stop: float
    last_price: float
    buy_commission: float
    entry_reason: str = "weekly"


@dataclass
class Variant:
    name: str
    dynamic_names: bool
    use_stops: bool
    use_macd: bool
    use_micro: bool
    use_volume: bool
    static_names: int = 30
    pool_size: int = 150


VARIANTS = [
    Variant("v1_static30", dynamic_names=False, use_stops=False, use_macd=False,
            use_micro=False, use_volume=False, static_names=30, pool_size=150),
    Variant("v1_static30_strict50", dynamic_names=False, use_stops=False, use_macd=False,
            use_micro=False, use_volume=False, static_names=30, pool_size=50),
    Variant("v1_dynamic", dynamic_names=True, use_stops=False, use_macd=False,
            use_micro=False, use_volume=False),
    Variant("stops_only", dynamic_names=True, use_stops=True, use_macd=False,
            use_micro=False, use_volume=False),
    Variant("price_risk", dynamic_names=True, use_stops=True, use_macd=True,
            use_micro=True, use_volume=True),
    Variant("stops_only_strict50", dynamic_names=True, use_stops=True, use_macd=False,
            use_micro=False, use_volume=False, pool_size=50),
    Variant("price_risk_strict50", dynamic_names=True, use_stops=True, use_macd=True,
            use_micro=True, use_volume=True, pool_size=50),
]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_industry(path: Path) -> dict[str, str]:
    industry: dict[str, str] = {}
    for row in _read_jsonl(path):
        for code in row.get("codes", []):
            industry.setdefault(str(code).zfill(6), str(row.get("name", "")))
    return industry


def load_panels(data_dir: Path, cache: Path | None, cfg: Config) -> dict[str, pd.DataFrame]:
    if cache and cache.exists():
        with cache.open("rb") as handle:
            return pickle.load(handle)

    files = sorted(data_dir.glob("*_qfq.pkl"))
    if not files:
        raise FileNotFoundError(f"no qfq files under {data_dir}")
    frames: list[pd.DataFrame] = []
    for index, path in enumerate(files, 1):
        symbol = path.name.split("_")[0]
        frame = pd.read_pickle(path)
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        frame = frame[
            (frame["datetime"] >= "2018-07-01")
            & (frame["datetime"] <= cfg.end)
        ]
        if frame.empty:
            continue
        frame["symbol"] = symbol
        frames.append(
            frame[
                [
                    "datetime", "symbol", "open", "high", "low", "close",
                    "volume", "amount", "turnover_rate",
                ]
            ]
        )
        if index % 1000 == 0:
            print(f"loaded {index}/{len(files)}", flush=True)
    long = pd.concat(frames, ignore_index=True)
    panels: dict[str, pd.DataFrame] = {}
    for column in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        panels[column] = (
            long.pivot(index="datetime", columns="symbol", values=column)
            .astype("float32")
            .sort_index()
        )
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with cache.open("wb") as handle:
            pickle.dump(panels, handle, protocol=5)
    return panels


def build_features(panels: dict[str, pd.DataFrame], index_close: pd.Series,
                   cfg: Config) -> dict[str, Any]:
    close = panels["close"]
    open_ = panels["open"]
    high = panels["high"]
    low = panels["low"]
    amount = panels["amount"]
    turnover = panels["turnover_rate"]

    dates = close.index
    idx = index_close.reindex(dates).ffill()

    # PIT float market cap: amount / turnover.  This is the traded value divided
    # by the fraction of float that traded, so it does not depend on qfq price.
    mv = amount / (turnover / 100.0)
    mv = mv.replace([np.inf, -np.inf], np.nan)
    mv_ffill = mv.ffill(limit=5)

    history = close.notna().cumsum()
    traded = (amount > 0) & (turnover > 0) & close.notna() & (close > 0)
    valid = traded & (history >= cfg.min_history) & mv_ffill.notna()
    rank = mv_ffill.where(valid).rank(axis=1, ascending=True)

    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        keys=["hl", "hc", "lc"],
    ).groupby(level=1).max()
    atr = true_range.rolling(cfg.atr_window, min_periods=cfg.atr_window).mean()
    turnover_mean20 = turnover.rolling(20, min_periods=20).mean()

    # Index features.
    ma10 = idx.rolling(10, min_periods=10).mean()
    ma20 = idx.rolling(20, min_periods=20).mean()
    dev = idx - ma10
    bull = (idx > ma20) & (ma20 > ma20.shift(5))
    ema12 = idx.ewm(span=12, adjust=False).mean()
    ema26 = idx.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = dif - dea
    price_high = idx == idx.rolling(cfg.macd_lookback, min_periods=cfg.macd_lookback).max()
    recent_high = price_high.rolling(cfg.macd_recent, min_periods=1).max().astype(bool)
    dif_not_high = dif < dif.rolling(cfg.macd_lookback, min_periods=cfg.macd_lookback).max()
    hist_cross_down = (hist < 0) & (hist.shift(1) >= 0)
    macd_trigger = recent_high & dif_not_high & hist_cross_down

    # Microcap breadth: smallest 5% of the valid universe.
    pct_rank = mv_ffill.where(valid).rank(axis=1, pct=True)
    small = pct_rank <= cfg.micro_pct
    prior_return = close.pct_change().shift(1)
    sub = prior_return.where(small)
    mean_ret = sub.mean(axis=1)
    std_ret = sub.std(axis=1)
    median_ret = sub.median(axis=1)
    within = sub.ge(mean_ret, axis=0) & sub.le(mean_ret + std_ret, axis=0)
    consistency = within.sum(axis=1) / sub.notna().sum(axis=1)
    upper = (
        consistency.rolling(cfg.micro_lookback, min_periods=cfg.micro_lookback // 2).mean()
        + 2.0
        * consistency.rolling(
            cfg.micro_lookback, min_periods=cfg.micro_lookback // 2
        ).std()
    )
    micro_trigger = (median_ret < -0.02) & (consistency > upper) & (~bull)

    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "prev_close": prev_close,
        "amount": amount,
        "turnover": turnover,
        "mv": mv_ffill,
        "valid": valid,
        "rank": rank,
        "atr": atr,
        "atr_prev": atr.shift(1),
        "turnover_mean20": turnover_mean20,
        "idx": idx,
        "ma10": ma10,
        "ma20": ma20,
        "dev": dev,
        "bull": bull,
        "macd_trigger": macd_trigger.fillna(False),
        "micro_trigger": micro_trigger.fillna(False),
        "consistency": consistency,
    }


def dynamic_target_count(dev: float) -> int:
    if not np.isfinite(dev):
        return 4
    if dev >= 200:
        return 3
    if dev >= -200:
        return 4
    if dev >= -500:
        return 5
    return 6


def select_targets(
    features: dict[str, Any],
    industry: dict[str, str],
    signal_date: pd.Timestamp,
    cfg: Config,
    limit: int,
    pool_size: int,
) -> list[str]:
    mv = features["mv"].loc[signal_date]
    valid = features["valid"].loc[signal_date]
    rank = features["rank"].loc[signal_date]
    pool = mv[(rank >= cfg.rank_low) & (rank <= cfg.rank_high) & valid]
    if pool.empty:
        return []
    small = pool.nsmallest(pool_size)
    # The description takes 30 of the smallest 50, so largest first among the
    # 50 smallest; industry cap is one per secondary industry.
    selected: list[str] = []
    seen: set[str] = set()
    for symbol in small.sort_values(ascending=False).index:
        sector = industry.get(symbol)
        if not sector:
            continue
        if sector in seen:
            continue
        seen.add(sector)
        selected.append(symbol)
        if len(selected) >= cfg.max_names:
            break
    return selected[:limit]


def limit_pct(symbol: str) -> float:
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("4", "8")):
        return 0.30
    return 0.10


def _commission(notional: float, cfg: Config) -> float:
    return max(cfg.min_commission, notional * cfg.commission_rate)


class Backtest:
    def __init__(self, features: dict[str, Any], industry: dict[str, str],
                 cfg: Config, variant: Variant):
        self.f = features
        self.industry = industry
        self.cfg = cfg
        self.variant = variant
        self.cash = cfg.initial
        self.positions: dict[str, Position] = {}
        self.trades: list[dict[str, Any]] = []
        self.equity: list[tuple[pd.Timestamp, float]] = []
        self.exposure: list[float] = []
        self.pause_until = -1
        self.stopped_at: dict[str, int] = {}
        self._limit_cache: dict[str, float] = {}

    def _limit(self, symbol: str) -> float:
        if symbol not in self._limit_cache:
            self._limit_cache[symbol] = limit_pct(symbol)
        return self._limit_cache[symbol]

    def _price(self, frame: str, date: pd.Timestamp, symbol: str) -> float:
        value = self.f[frame].at[date, symbol]
        return float(value) if pd.notna(value) else float("nan")

    def _equity(self, date: pd.Timestamp, price_frame: str = "close") -> float:
        value = self.cash
        for symbol, pos in self.positions.items():
            price = self._price(price_frame, date, symbol)
            if np.isfinite(price) and price > 0:
                if price_frame == "close":
                    pos.last_price = price
                value += pos.shares * price
            else:
                value += pos.shares * pos.last_price
        return value

    def _sell(self, date: pd.Timestamp, symbol: str, raw_price: float,
              reason: str, apply_slippage: bool = True) -> bool:
        pos = self.positions.get(symbol)
        if pos is None or pos.shares <= 0 or not np.isfinite(raw_price):
            return False
        price = raw_price * (1 - self.cfg.slippage) if apply_slippage else raw_price
        if price <= 0:
            return False
        notional = pos.shares * price
        commission = _commission(notional, self.cfg)
        cash = notional - commission - notional * self.cfg.stamp_tax
        self.cash += cash
        pnl = (
            (price - pos.entry_price) * pos.shares
            - commission
            - notional * self.cfg.stamp_tax
            - pos.buy_commission
        )
        self.trades.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "symbol": symbol,
                "side": "sell",
                "shares": pos.shares,
                "price": round(price, 4),
                "notional": round(notional, 2),
                "commission": round(commission, 2),
                "stamp_tax": round(notional * self.cfg.stamp_tax, 2),
                "pnl": round(pnl, 2),
                "reason": reason,
                "entry_date": pos.entry_date.strftime("%Y-%m-%d"),
                "holding_days": int((date - pos.entry_date).days),
            }
        )
        if reason.startswith("stop") and self.variant.use_stops:
            self.stopped_at[symbol] = self._date_index[date]
        del self.positions[symbol]
        return True

    def _buy(self, date: pd.Timestamp, symbol: str, raw_price: float,
             target_value: float) -> bool:
        if not np.isfinite(raw_price) or raw_price <= 0:
            return False
        price = raw_price * (1 + self.cfg.slippage)
        budget = min(target_value, self.cash)
        if budget <= 0:
            return False
        lots = int(budget // (price * self.cfg.lot))
        while lots > 0:
            shares = lots * self.cfg.lot
            notional = shares * price
            commission = _commission(notional, self.cfg)
            if notional + commission <= self.cash:
                break
            lots -= 1
        if lots <= 0:
            return False
        shares = lots * self.cfg.lot
        notional = shares * price
        commission = _commission(notional, self.cfg)
        self.cash -= notional + commission
        atr_value = self._price("atr_prev", date, symbol)
        stop = price * (1 - self.cfg.stop_loss)
        if np.isfinite(atr_value) and atr_value > 0:
            stop = max(stop, price - self.cfg.atr_mult * atr_value)
            atr_stop = price - self.cfg.atr_mult * atr_value
        else:
            atr_stop = stop
        self.positions[symbol] = Position(
            shares=shares,
            entry_price=price,
            entry_date=date,
            highest=price,
            stop_price=max(stop, atr_stop),
            atr_stop=atr_stop,
            last_price=price,
            buy_commission=commission,
            entry_reason="weekly",
        )
        self.trades.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "symbol": symbol,
                "side": "buy",
                "shares": shares,
                "price": round(price, 4),
                "notional": round(notional, 2),
                "commission": round(commission, 2),
                "stamp_tax": 0.0,
                "pnl": 0.0,
                "reason": "weekly_target",
                "entry_date": date.strftime("%Y-%m-%d"),
                "holding_days": 0,
            }
        )
        return True

    def _update_stop(self, date: pd.Timestamp, symbol: str) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return
        high = self._price("high", date, symbol)
        close = self._price("close", date, symbol)
        atr_value = self._price("atr", date, symbol)
        if np.isfinite(high):
            pos.highest = max(pos.highest, high)
        if np.isfinite(close) and close > 0:
            pos.last_price = close
        profit = pos.highest / pos.entry_price - 1.0
        fixed = pos.entry_price * (1 - self.cfg.stop_loss)
        if profit >= self.cfg.protect_2:
            fixed = max(fixed, pos.entry_price * (1 + self.cfg.protect_2_stop))
        elif profit >= self.cfg.protect_1:
            fixed = max(fixed, pos.entry_price)
        pos.stop_price = max(pos.stop_price, fixed)
        if np.isfinite(atr_value) and atr_value > 0:
            pos.atr_stop = max(pos.atr_stop, pos.highest - self.cfg.atr_mult * atr_value)
            pos.stop_price = max(pos.stop_price, pos.atr_stop)

    def _check_protective_exit(self, date: pd.Timestamp, symbol: str) -> bool:
        pos = self.positions.get(symbol)
        if pos is None or pos.entry_date >= date:
            return False
        open_ = self._price("open", date, symbol)
        high = self._price("high", date, symbol)
        low = self._price("low", date, symbol)
        if not all(np.isfinite(x) for x in (open_, high, low)):
            return False
        if self._limit_down_blocked(date, symbol):
            return False
        take = pos.entry_price * (1 + self.cfg.take_profit)
        stop = pos.stop_price
        # Conservative: if both take-profit and stop are touched, stop wins.
        if open_ <= stop:
            return self._sell(date, symbol, open_, "stop_gap")
        if low <= stop:
            return self._sell(date, symbol, stop, "stop")
        if high >= take:
            return self._sell(date, symbol, max(open_, take), "take_profit")
        return False

    def _limit_down_blocked(self, date: pd.Timestamp, symbol: str) -> bool:
        # Uses prior close; the panel has no limit price, so board-specific
        # percentage is the only available approximation.
        prev_close = self.f["prev_close"].at[date, symbol]
        open_ = self._price("open", date, symbol)
        if not np.isfinite(prev_close) or not np.isfinite(open_) or prev_close <= 0:
            return False
        return open_ <= prev_close * (1 - self._limit(symbol) + 0.002)

    def _limit_up_blocked(self, date: pd.Timestamp, symbol: str) -> bool:
        prev_close = self.f["prev_close"].at[date, symbol]
        open_ = self._price("open", date, symbol)
        if not np.isfinite(prev_close) or not np.isfinite(open_) or prev_close <= 0:
            return False
        return open_ >= prev_close * (1 + self._limit(symbol) - 0.002)

    def run(self) -> dict[str, Any]:
        cfg = self.cfg
        dates = self.f["close"].index
        start = pd.Timestamp(cfg.start)
        active = [i for i, d in enumerate(dates) if d >= start]
        if not active:
            raise ValueError("start date outside panel")
        first = max(active[0], 1)
        self._date_index = {d: i for i, d in enumerate(dates)}
        last_equity = cfg.initial
        peak = cfg.initial
        max_dd = 0.0
        daily_returns: list[float] = []

        for i in range(first, len(dates)):
            date = dates[i]
            prev = dates[i - 1]

            exit_all = False
            if self.variant.use_macd and bool(self.f["macd_trigger"].at[prev]):
                exit_all = True
                self.pause_until = max(self.pause_until, i + cfg.pause_days)
            if self.variant.use_micro and bool(self.f["micro_trigger"].at[prev]):
                exit_all = True

            sold_symbols: set[str] = set()
            if exit_all:
                for symbol in list(self.positions):
                    if self._limit_down_blocked(date, symbol):
                        continue
                    if not self._sell(date, symbol, self._price("open", date, symbol),
                                      "macd_exit" if self.f["macd_trigger"].at[prev] else "micro_exit"):
                        continue
                    sold_symbols.add(symbol)

            if self.variant.use_volume:
                for symbol in list(self.positions):
                    if symbol in sold_symbols:
                        continue
                    turn = self.f["turnover"].at[prev, symbol]
                    avg = self.f["turnover_mean20"].at[prev, symbol]
                    if (np.isfinite(turn) and np.isfinite(avg) and turn > cfg.volume_turnover
                            and turn > cfg.volume_mult * avg):
                        if self._limit_down_blocked(date, symbol):
                            continue
                        if self._sell(date, symbol, self._price("open", date, symbol),
                                      "volume_exit"):
                            sold_symbols.add(symbol)

            # Weekly rebalance: Tuesday, signal from Monday close.
            if date.weekday() == 1:
                signal = prev
                n_target = (
                    dynamic_target_count(float(self.f["dev"].at[signal]))
                    if self.variant.dynamic_names else self.variant.static_names
                )
                target = select_targets(
                    self.f, self.industry, signal, cfg, n_target, self.variant.pool_size
                )
                can_buy = not (self.variant.use_macd and i < self.pause_until)
                if not can_buy:
                    target = []

                for symbol in list(self.positions):
                    if symbol in sold_symbols:
                        continue
                    if symbol in target:
                        continue
                    if self._limit_down_blocked(date, symbol):
                        continue
                    if self._sell(date, symbol, self._price("open", date, symbol),
                                  "weekly_rebalance_out"):
                        sold_symbols.add(symbol)

                if target:
                    equity = self._equity(date, price_frame="open")
                    target_value = equity / max(1, n_target)
                    for symbol in target:
                        if symbol in self.positions:
                            continue
                        if self.variant.use_stops:
                            last_stop = self.stopped_at.get(symbol)
                            if last_stop is not None and i <= last_stop + cfg.cooldown_days:
                                continue
                        if self._limit_up_blocked(date, symbol):
                            continue
                        self._buy(date, symbol, self._price("open", date, symbol), target_value)

            if self.variant.use_stops:
                for symbol in list(self.positions):
                    if symbol in sold_symbols:
                        continue
                    if self._check_protective_exit(date, symbol):
                        sold_symbols.add(symbol)

            for symbol in list(self.positions):
                self._update_stop(date, symbol)

            equity = self._equity(date)
            invested = equity - self.cash
            self.exposure.append(invested / equity if equity > 0 else 0.0)
            self.equity.append((date, equity))
            daily_returns.append(equity / last_equity - 1.0 if last_equity > 0 else 0.0)
            peak = max(peak, equity)
            max_dd = min(max_dd, equity / peak - 1.0)
            last_equity = equity

        return self._result(daily_returns, max_dd)

    def _result(self, daily_returns: list[float], max_dd: float) -> dict[str, Any]:
        curve = pd.Series(
            [value for _, value in self.equity],
            index=pd.DatetimeIndex([date for date, _ in self.equity]),
        )
        years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
        total = curve.iloc[-1] / curve.iloc[0] - 1.0
        cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1.0
        returns = pd.Series(daily_returns)
        sharpe = float(returns.mean() / returns.std() * math.sqrt(252)) if returns.std() > 0 else 0.0
        sells = [t for t in self.trades if t["side"] == "sell"]
        wins = [t for t in sells if t["pnl"] > 0]
        losses = [t for t in sells if t["pnl"] <= 0]
        avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0.0
        avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0.0
        turnover = sum(t["notional"] for t in self.trades)
        avg_equity = float(curve.mean())
        drawdown = curve / curve.cummax() - 1.0
        dd_end = drawdown.idxmin()
        dd_start = curve.loc[:dd_end].idxmax()
        yearly = curve.resample("YE").last()
        yearly_returns = yearly.pct_change()
        if len(yearly) > 0:
            yearly_returns.iloc[0] = yearly.iloc[0] / curve.iloc[0] - 1.0
        return {
            "final_equity": round(float(curve.iloc[-1]), 2),
            "total_return_pct": round(total * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "sharpe": round(sharpe, 3),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "daily_win_rate_pct": round(float((returns > 0).mean() * 100), 2),
            "closed_trades": len(sells),
            "trade_win_rate_pct": round(len(wins) / len(sells) * 100, 2) if sells else 0.0,
            "payoff": round(avg_win / abs(avg_loss), 3) if avg_loss else None,
            "avg_exposure_pct": round(float(np.mean(self.exposure) * 100), 2),
            "turnover_x": round(turnover / avg_equity, 2) if avg_equity else 0.0,
            "turnover_annual_x": round(turnover / avg_equity / years, 2) if avg_equity else 0.0,
            "trades": len(self.trades),
            "max_drawdown_start": dd_start.strftime("%Y-%m-%d"),
            "max_drawdown_end": dd_end.strftime("%Y-%m-%d"),
            "yearly_returns_pct": {
                str(index.year): round(float(value) * 100, 2)
                for index, value in yearly_returns.items()
                if pd.notna(value)
            },
        }


def benchmark_metrics(index_close: pd.Series, cfg: Config) -> dict[str, Any]:
    curve = index_close.loc[cfg.start:cfg.end].dropna()
    if curve.empty:
        return {}
    total = curve.iloc[-1] / curve.iloc[0] - 1.0
    years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1.0
    returns = curve.pct_change().dropna()
    sharpe = float(returns.mean() / returns.std() * math.sqrt(252)) if returns.std() > 0 else 0.0
    dd = float((curve / curve.cummax() - 1).min())
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(dd * 100, 2),
        "daily_win_rate_pct": round(float((returns > 0).mean() * 100), 2),
    }


def run_study(panels: dict[str, pd.DataFrame], index_close: pd.Series,
              industry: dict[str, str], cfg: Config) -> dict[str, Any]:
    features = build_features(panels, index_close, cfg)
    dates = features["close"].index
    valid_counts = features["valid"].sum(axis=1).loc[cfg.start:cfg.end]
    result: dict[str, Any] = {
        "config": asdict(cfg),
        "data": {
            "symbols": int(features["close"].shape[1]),
            "dates": [dates.min().strftime("%Y-%m-%d"), dates.max().strftime("%Y-%m-%d")],
            "backtest_start": cfg.start,
            "backtest_end": cfg.end,
            "universe_mean": round(float(valid_counts.mean()), 1),
            "universe_min": int(valid_counts.min()),
            "universe_max": int(valid_counts.max()),
            "industry_symbols": len(industry),
            "industry_missing_in_panel": int(sum(1 for s in features["close"].columns if s not in industry)),
        },
        "triggers": {
            "macd": int(features["macd_trigger"].sum()),
            "micro": int(features["micro_trigger"].sum()),
            "macd_dates": [d.strftime("%Y-%m-%d") for d in features["macd_trigger"].index[features["macd_trigger"]]],
            "micro_dates": [d.strftime("%Y-%m-%d") for d in features["micro_trigger"].index[features["micro_trigger"]]],
        },
        "benchmark_399101": benchmark_metrics(features["idx"], cfg),
        "variants": {},
    }
    curves: dict[str, pd.Series] = {}
    for variant in VARIANTS:
        started = time.time()
        engine = Backtest(features, industry, cfg, variant)
        metrics = engine.run()
        result["variants"][variant.name] = {
            **metrics,
            "variant": asdict(variant),
            "elapsed_seconds": round(time.time() - started, 1),
        }
        curves[variant.name] = pd.Series(
            [v for _, v in engine.equity],
            index=pd.DatetimeIndex([d for d, _ in engine.equity]),
        )
        if variant.name == "price_risk":
            result["price_risk_trades"] = engine.trades
        print(f"{variant.name}: {metrics}", flush=True)
    result["curves"] = (
        pd.DataFrame({"benchmark_399101": features["idx"], **curves})
        .dropna(how="all")
        .loc[cfg.start:cfg.end]
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="cache/baostock_history_20261026")
    parser.add_argument("--panel-cache", default=str(STUDY_DIR / "panels_2018_2024.pkl"))
    parser.add_argument("--index-file", default=str(STUDY_DIR / "index_399101.csv"))
    parser.add_argument("--industry-file", default=str(STUDY_DIR / "sina_all.jsonl"))
    parser.add_argument("--out", default=str(STUDY_DIR / "out"))
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--initial", type=float, default=250_000.0)
    parser.add_argument("--extension-dir", default="cache/stock_pit/prices")
    args = parser.parse_args()

    cfg = Config(start=args.start, end=args.end, initial=args.initial)
    data_dir = Path(args.data_dir)
    panels = load_panels(data_dir, Path(args.panel_cache), cfg)
    if pd.Timestamp(args.end) > pd.Timestamp("2024-12-31"):
        panels = extend_panels(
            panels, Path(args.extension_dir), list(panels["close"].columns), args.start
        )
    delisted_dir = Path("cache/stock_pit/prices_delisted")
    if delisted_dir.exists():
        panels = extend_delisted_panels(panels, delisted_dir)
    index = pd.read_csv(args.index_file, parse_dates=["date"]).set_index("date")["close"]
    industry = load_industry(Path(args.industry_file))
    delisted_path = Path("cache/stock_pit/universe/delisted.csv")
    if delisted_path.exists():
        delisted = pd.read_csv(delisted_path, dtype={"symbol": str})
        for code in delisted["symbol"]:
            industry.setdefault(str(code).zfill(6), f"__delisted_{code}")
    result = run_study(panels, index, industry, cfg)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    curves = result.pop("curves")
    curves.to_csv(out / "curves.csv", float_format="%.2f")
    trades = result.pop("price_risk_trades", [])
    pd.DataFrame(trades).to_csv(out / "price_risk_trades.csv", index=False)
    limitations = [
        "No PIT CSI 1000 constituents; rank 801-1800 by PIT float market cap is the proxy.",
        "No total market cap; float market cap is used for the 50 -> 30 sort.",
        "No v2/v3 fundamentals or nine-point annual-report audit.",
        "No intraday bars; stops/take-profit use daily open-low-high and stop wins if both touched.",
        "Industry mapping is current Sina classification, not PIT.",
        "No ST/name data; limit-up/down uses board percentages only.",
        "Delisted stocks are absent from the baostock cache; survivorship bias remains.",
        "MACD divergence and microcap breadth are reconstructions; the source code is not available.",
    ]
    result["limitations"] = limitations
    (out / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
