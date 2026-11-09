"""Pre-registered "optimization v1" for the PIT all-A small-cap baseline.

Design is fixed, not tuned:

* universe: PIT all-A + available delisted stocks;
* filters: price >= 2, 250-day history, ADV20 >= 50M CNY;
* selection: PIT float-market-cap rank 801-1800, 50 names, sector <=25%;
* monthly rebalance, next open;
* no-trade band 5%;
* 10bp slippage, 1% ADV participation cap;
* 10% annualized vol target, portfolio drawdown de-risk at -15% / -25%;
* no individual stop-loss.

2020-2024 is the design period; 2025-2026 is a time holdout.  This is not a
pristine out-of-sample test because the holdout was observed earlier.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quality_filter_study import (
    _rebalance_map,
    extend_delisted_panels,
    extend_panels,
)
from smallcap_strategy_study import (
    Config,
    build_features,
    load_industry,
    load_panels,
)

# Study artifacts live next to the code (``cache/`` is gitignored); the old
# ``D:/tmp/...`` defaults only resolved on the Windows research machine.
STUDY_DIR = Path(__file__).resolve().parent / "cache" / "smallcap_study"


@dataclass
class OptConfig:
    start: str = "2020-01-02"
    end: str = "2026-10-30"
    initial: float = 250_000.0
    rank_low: int = 801
    rank_high: int = 1800
    rank_pct_low: float = 0.0
    rank_pct_high: float = 1.0
    max_names: int = 50
    min_price: float = 2.0
    min_adv: float = 50_000_000.0
    max_sector_weight: float = 0.25
    band: float = 0.05
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    slippage: float = 0.0010
    lot: int = 100
    target_vol: float = 0.10
    dd_derisk_1: float = -0.15
    dd_derisk_2: float = -0.25
    participation: float = 0.01


def _limit_pct(symbol: str) -> float:
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("4", "8")):
        return 0.30
    return 0.10


def _commission(notional: float, cfg: OptConfig) -> float:
    return max(cfg.min_commission, notional * cfg.commission_rate)


class OptimizedBacktest:
    def __init__(
        self,
        features: dict[str, Any],
        adv20: pd.DataFrame,
        industry: dict[str, str],
        cfg: OptConfig,
    ):
        self.f = features
        self.adv20 = adv20
        self.industry = industry
        self.cfg = cfg
        self.cash = cfg.initial
        self.positions: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.equity: list[tuple[pd.Timestamp, float]] = []
        self.exposure: list[float] = []
        self.position_counts: list[int] = []
        self.selections: list[dict[str, Any]] = []

    def _price(self, frame: str, date: pd.Timestamp, symbol: str) -> float:
        value = self.f[frame].at[date, symbol]
        return float(value) if pd.notna(value) else float("nan")

    def _equity(self, date: pd.Timestamp, frame: str = "close") -> float:
        value = self.cash
        for symbol, pos in self.positions.items():
            price = self._price(frame, date, symbol)
            if not np.isfinite(price) or price <= 0:
                price = pos["last_price"]
            if frame == "close" and np.isfinite(price) and price > 0:
                pos["last_price"] = price
            value += pos["shares"] * price
        return value

    def _limit_down(self, date: pd.Timestamp, symbol: str) -> bool:
        prev = self._price("prev_close", date, symbol)
        open_ = self._price("open", date, symbol)
        if not np.isfinite(prev) or not np.isfinite(open_) or prev <= 0:
            return False
        return open_ <= prev * (1 - _limit_pct(symbol) + 0.002)

    def _limit_up(self, date: pd.Timestamp, symbol: str) -> bool:
        prev = self._price("prev_close", date, symbol)
        open_ = self._price("open", date, symbol)
        if not np.isfinite(prev) or not np.isfinite(open_) or prev <= 0:
            return False
        return open_ >= prev * (1 + _limit_pct(symbol) - 0.002)

    def _adv_shares(self, signal: pd.Timestamp, symbol: str, price: float) -> int:
        if not np.isfinite(price) or price <= 0:
            return 0
        adv = self.adv20.at[signal, symbol]
        if not np.isfinite(adv) or adv <= 0:
            return 0
        return int((adv * self.cfg.participation / price) // self.cfg.lot) * self.cfg.lot

    def _sell(self, date: pd.Timestamp, signal: pd.Timestamp, symbol: str,
              raw_price: float, shares: int, reason: str) -> int:
        pos = self.positions.get(symbol)
        if pos is None or shares <= 0 or not np.isfinite(raw_price) or raw_price <= 0:
            return 0
        shares = min(shares, pos["shares"])
        cap = self._adv_shares(signal, symbol, raw_price)
        if cap > 0:
            shares = min(shares, cap)
        if shares <= 0:
            return 0
        price = raw_price * (1 - self.cfg.slippage)
        notional = shares * price
        commission = _commission(notional, self.cfg)
        stamp = notional * self.cfg.stamp_tax
        self.cash += notional - commission - stamp
        pnl = (
            (price - pos["entry_price"]) * shares
            - commission
            - stamp
            - pos["buy_commission"] * shares / max(1, pos["shares"])
        )
        self.trades.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "symbol": symbol,
                "side": "sell",
                "shares": shares,
                "price": round(price, 4),
                "notional": round(notional, 2),
                "pnl": round(pnl, 2),
                "reason": reason,
            }
        )
        pos["shares"] -= shares
        if pos["shares"] <= 0:
            del self.positions[symbol]
        return shares

    def _buy(self, date: pd.Timestamp, signal: pd.Timestamp, symbol: str,
             raw_price: float, target_value: float) -> int:
        if not np.isfinite(raw_price) or raw_price <= 0:
            return 0
        price = raw_price * (1 + self.cfg.slippage)
        budget = min(target_value, self.cash)
        if budget <= 0:
            return 0
        cap = self._adv_shares(signal, symbol, price)
        lots = int(budget // (price * self.cfg.lot))
        if cap > 0:
            lots = min(lots, cap // self.cfg.lot)
        while lots > 0:
            shares = lots * self.cfg.lot
            notional = shares * price
            commission = _commission(notional, self.cfg)
            if notional + commission <= self.cash:
                break
            lots -= 1
        if lots <= 0:
            return 0
        shares = lots * self.cfg.lot
        notional = shares * price
        commission = _commission(notional, self.cfg)
        self.cash -= notional + commission
        if symbol in self.positions:
            pos = self.positions[symbol]
            total = pos["shares"] + shares
            pos["entry_price"] = (pos["entry_price"] * pos["shares"] + price * shares) / total
            pos["buy_commission"] += commission
            pos["shares"] = total
            pos["last_price"] = price
        else:
            self.positions[symbol] = {
                "shares": shares,
                "entry_price": price,
                "buy_commission": commission,
                "last_price": price,
            }
        self.trades.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "symbol": symbol,
                "side": "buy",
                "shares": shares,
                "price": round(price, 4),
                "notional": round(notional, 2),
                "pnl": 0.0,
                "reason": "monthly_rebalance",
            }
        )
        return shares

    def _select(self, signal: pd.Timestamp) -> tuple[list[str], int]:
        valid = self.f["valid"].loc[signal]
        close = self.f["close"].loc[signal]
        rank = self.f["rank"].loc[signal]
        mv = self.f["mv"].loc[signal]
        adv = self.adv20.loc[signal]
        valid_count = max(1, int(valid.sum()))
        rank_pct = rank / valid_count
        mask = (
            valid
            & (close >= self.cfg.min_price)
            & (adv >= self.cfg.min_adv)
            & (rank >= self.cfg.rank_low)
            & (rank <= self.cfg.rank_high)
            & (rank_pct >= self.cfg.rank_pct_low)
            & (rank_pct <= self.cfg.rank_pct_high)
        )
        pool = mv[mask].dropna().sort_values().head(self.cfg.max_names * 2)
        max_per_sector = max(1, int(math.ceil(self.cfg.max_sector_weight * self.cfg.max_names)))
        selected: list[str] = []
        counts: dict[str, int] = {}
        for symbol in pool.index:
            sector = self.industry.get(symbol, f"__{symbol}")
            if counts.get(sector, 0) >= max_per_sector:
                continue
            counts[sector] = counts.get(sector, 0) + 1
            selected.append(symbol)
            if len(selected) >= self.cfg.max_names:
                break
        self.selections.append(
            {
                "signal": signal.strftime("%Y-%m-%d"),
                "pool": int(mask.sum()),
                "target": len(selected),
                "max_sector": max_per_sector,
            }
        )
        return selected, len(selected)

    def _gross_exposure(self, equity: float) -> float:
        curve = pd.Series([v for _, v in self.equity])
        if len(curve) < 20:
            gross = 1.0
        else:
            returns = curve.pct_change().dropna().tail(60)
            vol = float(returns.std() * math.sqrt(252)) if len(returns) > 2 else 0.0
            gross = min(1.0, self.cfg.target_vol / vol) if vol > 0 else 1.0
        peak = max([v for _, v in self.equity] + [equity])
        dd = equity / peak - 1.0 if peak > 0 else 0.0
        if dd <= self.cfg.dd_derisk_2:
            # Do not go to permanent cash: with no positions the drawdown can
            # never recover.  Keep a 25% risk sleeve instead.
            gross = min(gross, 0.25)
        elif dd <= self.cfg.dd_derisk_1:
            gross *= 0.5
        return max(0.0, min(1.0, gross))

    def run(self, execution_map: dict[pd.Timestamp, pd.Timestamp]) -> dict[str, Any]:
        dates = self.f["close"].index
        start = pd.Timestamp(self.cfg.start)
        last_equity = self.cfg.initial
        peak = self.cfg.initial
        max_dd = 0.0
        daily_returns: list[float] = []

        for date in dates:
            if date < start:
                continue
            if date in execution_map:
                signal = execution_map[date]
                target, n_target = self._select(signal)
                target_set = set(target)
                equity = self._equity(date, "open")
                gross = self._gross_exposure(equity)
                target_value = equity * gross / max(1, n_target)

                # Sells: non-target first, then overweight target positions.
                for symbol in list(self.positions):
                    if self._limit_down(date, symbol):
                        continue
                    raw = self._price("open", date, symbol)
                    if not np.isfinite(raw):
                        raw = self.positions[symbol]["last_price"]
                    if symbol not in target_set:
                        self._sell(date, signal, symbol, raw, self.positions[symbol]["shares"], "rebalance_out")
                        continue
                    current = self.positions[symbol]["shares"] * raw
                    if current > target_value * (1 + self.cfg.band):
                        excess = int((current - target_value) / raw // self.cfg.lot) * self.cfg.lot
                        self._sell(date, signal, symbol, raw, excess, "rebalance_trim")

                # Buys: new names and underweight target names.
                for symbol in target:
                    if self._limit_up(date, symbol):
                        continue
                    raw = self._price("open", date, symbol)
                    if not np.isfinite(raw):
                        continue
                    current = self.positions.get(symbol, {}).get("shares", 0) * raw
                    if symbol not in self.positions:
                        self._buy(date, signal, symbol, raw, target_value)
                    elif current < target_value * (1 - self.cfg.band):
                        self._buy(date, signal, symbol, raw, target_value - current)

            equity = self._equity(date, "close")
            invested = equity - self.cash
            self.exposure.append(invested / equity if equity > 0 else 0.0)
            self.position_counts.append(len(self.positions))
            self.equity.append((date, equity))
            daily_returns.append(equity / last_equity - 1.0 if last_equity > 0 else 0.0)
            peak = max(peak, equity)
            max_dd = min(max_dd, equity / peak - 1.0)
            last_equity = equity

        return self._result(daily_returns, max_dd)

    def _result(self, daily_returns: list[float], max_dd: float) -> dict[str, Any]:
        curve = pd.Series(
            [v for _, v in self.equity],
            index=pd.DatetimeIndex([d for d, _ in self.equity]),
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
        if len(yearly):
            yearly_returns.iloc[0] = yearly.iloc[0] / curve.iloc[0] - 1.0
        return {
            "final_equity": round(float(curve.iloc[-1]), 2),
            "total_return_pct": round(total * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "sharpe": round(sharpe, 3),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "max_drawdown_start": dd_start.strftime("%Y-%m-%d"),
            "max_drawdown_end": dd_end.strftime("%Y-%m-%d"),
            "daily_win_rate_pct": round(float((returns > 0).mean() * 100), 2),
            "closed_trades": len(sells),
            "trade_win_rate_pct": round(len(wins) / len(sells) * 100, 2) if sells else 0.0,
            "payoff": round(avg_win / abs(avg_loss), 3) if avg_loss else None,
            "avg_exposure_pct": round(float(np.mean(self.exposure) * 100), 2),
            "avg_positions": round(float(np.mean(self.position_counts)), 2),
            "turnover_x": round(turnover / avg_equity, 2) if avg_equity else 0.0,
            "turnover_annual_x": round(turnover / avg_equity / years, 2) if avg_equity else 0.0,
            "trades": len(self.trades),
            "yearly_returns_pct": {
                str(index.year): round(float(value) * 100, 2)
                for index, value in yearly_returns.items()
                if pd.notna(value)
            },
        }


def period_metrics(curve: pd.Series, start: str, end: str) -> dict[str, Any]:
    part = curve.loc[start:end]
    if len(part) < 2:
        return {}
    returns = part.pct_change().dropna()
    years = max((part.index[-1] - part.index[0]).days / 365.25, 1e-9)
    cagr = (part.iloc[-1] / part.iloc[0]) ** (1 / years) - 1.0
    dd = float((part / part.cummax() - 1).min())
    sharpe = float(returns.mean() / returns.std() * math.sqrt(252)) if returns.std() > 0 else 0.0
    return {
        "cagr_pct": round(cagr * 100, 2),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(dd * 100, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panels", default=str(STUDY_DIR / "panels_2018_2024.pkl"))
    parser.add_argument("--data-dir", default="cache/baostock_history_20261026")
    parser.add_argument("--index-file", default=str(STUDY_DIR / "index_399101.csv"))
    parser.add_argument("--industry-file", default=str(STUDY_DIR / "sina_all.jsonl"))
    parser.add_argument("--extension-dir", default="cache/stock_pit/prices")
    parser.add_argument("--delisted-dir", default="cache/stock_pit/prices_delisted")
    parser.add_argument("--out", default=str(STUDY_DIR / "optimized"))
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--initial", type=float, default=250_000.0)
    parser.add_argument("--rank-low", type=int, default=801)
    parser.add_argument("--rank-high", type=int, default=1800)
    parser.add_argument("--rank-pct-low", type=float, default=0.0)
    parser.add_argument("--rank-pct-high", type=float, default=1.0)
    args = parser.parse_args()

    cfg = OptConfig(
        start=args.start,
        end=args.end,
        initial=args.initial,
        rank_low=args.rank_low,
        rank_high=args.rank_high,
        rank_pct_low=args.rank_pct_low,
        rank_pct_high=args.rank_pct_high,
    )
    panels = load_panels(Path(args.data_dir), Path(args.panels), Config(start=args.start, end=args.end))
    panels = extend_panels(panels, Path(args.extension_dir), list(panels["close"].columns), args.start)
    panels = extend_delisted_panels(panels, Path(args.delisted_dir))
    index = pd.read_csv(args.index_file, parse_dates=["date"]).set_index("date")["close"]
    industry = load_industry(Path(args.industry_file))
    delisted_path = Path("cache/stock_pit/universe/delisted.csv")
    if delisted_path.exists():
        delisted = pd.read_csv(delisted_path, dtype={"symbol": str})
        for code in delisted["symbol"]:
            industry.setdefault(str(code).zfill(6), f"__delisted_{code}")
    current_all = list(panels["close"].columns)
    base_cfg = Config(start=args.start, end=args.end, min_history=250)
    features = build_features(panels, index, base_cfg)
    adv20 = panels["amount"].rolling(20, min_periods=20).mean()
    execution_map = _rebalance_map(features["close"].index, base_cfg)

    engine = OptimizedBacktest(features, adv20, industry, cfg)
    metrics = engine.run(execution_map)
    curve = pd.Series(
        [v for _, v in engine.equity],
        index=pd.DatetimeIndex([d for d, _ in engine.equity]),
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "config": asdict(cfg),
        "data": {
            "symbols": len(current_all),
            "signals": len(execution_map),
            "start": str(curve.index.min().date()),
            "end": str(curve.index.max().date()),
        },
        "metrics": metrics,
        "periods": {
            "train_2020_2024": period_metrics(curve, "2020-01-02", "2024-12-31"),
            "holdout_2025_2026": period_metrics(curve, "2025-01-01", "2026-10-30"),
        },
        "selections": engine.selections,
        "limitations": [
            "Historical PIT CSI 1000 membership is still unavailable; this uses PIT all-A rank 801-1800.",
            "ST history is not available; price/ADV/history filters are used as a proxy.",
            "237/367 delisted stocks have bars; remaining delisted names are missing.",
            "2025-2026 prices are Sina unadjusted, scaled at the 2024 splice.",
            "2025-2026 was observed earlier, so it is a time holdout, not pristine OOS.",
        ],
    }
    (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    curve.to_frame("equity").to_csv(out / "curves.csv")
    pd.DataFrame(engine.trades).to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
    print(metrics, flush=True)
    print("wrote", out, flush=True)


if __name__ == "__main__":
    main()
