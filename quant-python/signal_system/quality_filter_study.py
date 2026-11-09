"""Quality-filter A/B on the current CSI 1000 pool.

This is a narrow, documented experiment, not a claim of a PIT CSI 1000
backtest:

* universe: current CSI 1000 snapshot (2026-11-05), fixed for 2020-2024;
* selection: 30 smallest PIT float-market-cap names from the 100 smallest
  current-CSI1000 names;
* baseline: no quality filter;
* quality: annual-report PIT filter; only reports whose actual disclosure date
  is on or before the signal date are used.

The current-snapshot universe is selected in 2026, so absolute returns are
biased.  The A/B is still useful for measuring the marginal effect of a
quality filter on the *same* universe, costs and execution.
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

# Study artifacts live next to the code (``cache/`` is gitignored); the old
# ``D:/tmp/...`` defaults only resolved on the Windows research machine.
STUDY_DIR = Path(__file__).resolve().parent / "cache" / "smallcap_study"


@dataclass
class Config:
    start: str = "2020-01-02"
    end: str = "2024-12-31"
    initial: float = 250_000.0
    pool_size: int = 100
    max_names: int = 30
    min_history: int = 250
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    slippage: float = 0.0005
    lot: int = 100


@dataclass
class Variant:
    name: str
    quality: str  # none / loose / strict
    roe_min: float = 0.0
    roa_min: float = 0.0


VARIANTS = [
    Variant("baseline", "none"),
    Variant("quality_loose", "loose", roe_min=8.0, roa_min=4.0),
    Variant("quality_strict", "strict", roe_min=15.0, roa_min=10.0),
]


def _load_panels(path: Path) -> dict[str, pd.DataFrame]:
    with path.open("rb") as handle:
        return pickle.load(handle)


def extend_panels(
    panels: dict[str, pd.DataFrame],
    price_dir: Path,
    symbols: list[str],
    start: str = "2025-01-01",
) -> dict[str, pd.DataFrame]:
    """Append Sina 2025-2026 bars to the baostock 2020-2024 panels.

    Sina files are unadjusted.  Each symbol is scaled so its first 2025 close
    equals the last 2024 baostock close; this preserves return continuity at
    the splice.  Market-cap ranking uses amount/turnover, which is unaffected
    by price scaling.
    """

    if not price_dir.exists():
        return panels
    frames: list[pd.DataFrame] = []
    for symbol in symbols:
        path = price_dir / f"{symbol}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        date_column = "date" if "date" in frame.columns else frame.columns[0]
        frame["datetime"] = pd.to_datetime(frame[date_column], errors="coerce")
        frame = frame[frame["datetime"] >= pd.Timestamp(start)].copy()
        if frame.empty:
            continue
        for column in ["open", "high", "low", "close", "volume", "amount"]:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if "turnover" in frame.columns:
            frame["turnover_rate"] = pd.to_numeric(frame["turnover"], errors="coerce") * 100.0
        else:
            frame["turnover_rate"] = np.nan
        last_close = panels["close"].get(symbol)
        if last_close is not None:
            last_close = last_close.dropna()
            last_close = last_close.loc[: pd.Timestamp("2024-12-31")]
        if last_close is not None and not last_close.empty:
            first_close = frame["close"].dropna()
            if not first_close.empty and first_close.iloc[0] > 0:
                scale = float(last_close.iloc[-1]) / float(first_close.iloc[0])
                for column in ["open", "high", "low", "close"]:
                    frame[column] = frame[column] * scale
        frame["symbol"] = symbol
        frames.append(frame[["datetime", "symbol", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]])
    if not frames:
        return panels
    long = pd.concat(frames, ignore_index=True)
    extended = dict(panels)
    for column in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        extra = long.pivot(index="datetime", columns="symbol", values=column)
        extra = extra.reindex(columns=panels[column].columns)
        extended[column] = pd.concat([panels[column], extra]).sort_index()
    return extended


def extend_delisted_panels(
    panels: dict[str, pd.DataFrame], price_dir: Path
) -> dict[str, pd.DataFrame]:
    """Merge Eastmoney delisted bars (Chinese columns) into the panels."""

    if not price_dir.exists():
        return panels
    frames: list[pd.DataFrame] = []
    for path in sorted(price_dir.glob("*.csv")):
        if path.name == "prices_index.csv":
            continue
        raw = pd.read_csv(path)
        if "日期" not in raw.columns or "收盘" not in raw.columns:
            continue
        frame = pd.DataFrame(
            {
                "datetime": pd.to_datetime(raw["日期"], errors="coerce"),
                "symbol": path.stem,
                "open": pd.to_numeric(raw.get("开盘"), errors="coerce"),
                "high": pd.to_numeric(raw.get("最高"), errors="coerce"),
                "low": pd.to_numeric(raw.get("最低"), errors="coerce"),
                "close": pd.to_numeric(raw.get("收盘"), errors="coerce"),
                "volume": pd.to_numeric(raw.get("成交量"), errors="coerce"),
                "amount": pd.to_numeric(raw.get("成交额"), errors="coerce"),
                "turnover_rate": pd.to_numeric(raw.get("换手率"), errors="coerce"),
            }
        )
        frames.append(frame.dropna(subset=["datetime", "close"]))
    if not frames:
        return panels
    long = pd.concat(frames, ignore_index=True)
    extended = dict(panels)
    for column in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        extra = long.pivot_table(index="datetime", columns="symbol", values=column, aggfunc="last")
        # Add delisted symbols that are not in the current panels.
        missing = [s for s in extra.columns if s not in panels[column].columns]
        extra = extra.reindex(columns=list(panels[column].columns) + missing)
        extended[column] = panels[column].combine_first(extra).sort_index()
    return extended


def _load_current(path: Path) -> list[str]:
    frame = pd.read_csv(path, dtype={"symbol": str})
    return [str(value).zfill(6) for value in frame["symbol"]]


def _number(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in {"", "--", "-", "nan", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_quality(path: Path) -> pd.DataFrame:
    """Return one row per symbol/report-year with the nine indicators."""

    frame = pd.read_csv(path, dtype={"symbol": str})
    frame["symbol"] = frame["symbol"].map(lambda value: str(value).zfill(6))
    frame["report_year"] = pd.to_numeric(frame["report_year"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame["available_date"] = pd.to_datetime(frame["available_date"], errors="coerce")
    frame["pit_available"] = frame["pit_available"].astype(str).str.lower().isin({"true", "1"})
    frame = frame[frame["pit_available"] & frame["available_date"].notna()]
    frame = frame.dropna(subset=["report_year", "value"])
    pivot = (
        frame.pivot_table(
            index=["symbol", "report_year"],
            columns="indicator",
            values="value",
            aggfunc="first",
        )
        .reset_index()
    )
    meta = (
        frame.groupby(["symbol", "report_year"], as_index=False)["available_date"]
        .max()
        .rename(columns={"available_date": "quality_date"})
    )
    return pivot.merge(meta, on=["symbol", "report_year"], how="left")


def latest_quality(quality: pd.DataFrame, signal: pd.Timestamp) -> pd.DataFrame:
    available = quality[quality["quality_date"] <= signal]
    if available.empty:
        return available
    return (
        available.sort_values(["symbol", "report_year"])
        .groupby("symbol", as_index=False)
        .tail(1)
        .set_index("symbol")
    )


def quality_pass(row: pd.Series, roe_min: float, roa_min: float) -> bool:
    """PIT annual quality gate used in the A/B.

    Missing values fail rather than pass.  Goodwill missing is treated as zero
    because the abstract source omits zero/non-applicable line items; this is
    documented in the report.
    """

    required = [
        "归母净利润",
        "扣非净利润",
        "经营现金流量净额",
        "营业总收入",
        "净资产收益率(ROE)",
        "总资产报酬率(ROA)",
        "资产负债率",
    ]
    values = {name: _number(row.get(name)) for name in required}
    if any(value is None for value in values.values()):
        return False
    if values["归母净利润"] <= 0 or values["扣非净利润"] <= 0:
        return False
    if values["经营现金流量净额"] <= 0:
        return False
    if values["营业总收入"] < 1e8:
        return False
    if values["净资产收益率(ROE)"] < roe_min:
        return False
    if values["总资产报酬率(ROA)"] < roa_min:
        return False
    if values["资产负债率"] > 70.0:
        return False
    equity = _number(row.get("股东权益合计(净资产)"))
    goodwill = _number(row.get("商誉"))
    goodwill = 0.0 if goodwill is None else goodwill
    if equity is None or equity <= 0:
        return False
    if goodwill / equity > 0.30:
        return False
    return True


def _limit_pct(symbol: str) -> float:
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("4", "8")):
        return 0.30
    return 0.10


def _commission(notional: float, cfg: Config) -> float:
    return max(cfg.min_commission, notional * cfg.commission_rate)


def _rebalance_map(dates: pd.DatetimeIndex, cfg: Config) -> dict[pd.Timestamp, pd.Timestamp]:
    month_ends = pd.Series(dates, index=dates).groupby(dates.to_period("M")).max()
    mapping: dict[pd.Timestamp, pd.Timestamp] = {}
    positions = {date: index for index, date in enumerate(dates)}
    for signal in month_ends:
        index = positions.get(signal)
        if index is None or index + 1 >= len(dates):
            continue
        execution = dates[index + 1]
        if pd.Timestamp(cfg.start) <= execution <= pd.Timestamp(cfg.end):
            mapping[execution] = signal
    return mapping


def build_features(panels: dict[str, pd.DataFrame], current: list[str], cfg: Config) -> dict[str, Any]:
    close = panels["close"]
    open_ = panels["open"]
    amount = panels["amount"]
    turnover = panels["turnover_rate"]
    mv = (amount / (turnover / 100.0)).replace([np.inf, -np.inf], np.nan)
    mv = mv.ffill(limit=5)
    history = close.notna().cumsum()
    valid = (
        close.notna()
        & (close > 0)
        & (amount > 0)
        & (turnover > 0)
        & (history >= cfg.min_history)
        & mv.notna()
    )
    # Restrict to the current CSI 1000 names that actually have local prices.
    allowed = [symbol for symbol in current if symbol in close.columns]
    valid = valid[allowed]
    return {
        "open": open_[allowed],
        "close": close[allowed],
        "prev_close": close[allowed].shift(1),
        "amount": amount[allowed],
        "turnover": turnover[allowed],
        "mv": mv[allowed],
        "valid": valid,
    }


def select_targets(
    signal: pd.Timestamp,
    features: dict[str, Any],
    quality: pd.DataFrame,
    variant: Variant,
    cfg: Config,
    stats: list[dict[str, Any]],
) -> list[str]:
    valid = features["valid"].loc[signal]
    mv = features["mv"].loc[signal]
    symbols = [symbol for symbol in valid.index if bool(valid.get(symbol, False))]
    pool = mv.reindex(symbols).dropna().sort_values().head(cfg.pool_size)
    quality_names = 0
    if variant.quality == "none":
        selected = pool
    else:
        q = latest_quality(quality, signal)
        if q.empty:
            selected = pool.iloc[0:0]
        else:
            flags = q.apply(
                lambda row: quality_pass(row, variant.roe_min, variant.roa_min),
                axis=1,
            )
            allowed = set(flags[flags].index)
            quality_names = len(set(pool.index) & allowed)
            selected = pool[pool.index.isin(allowed)]
    target = list(selected.head(cfg.max_names).index)
    stats.append(
        {
            "signal": signal.strftime("%Y-%m-%d"),
            "variant": variant.name,
            "pool": len(pool),
            "quality_pass": quality_names,
            "target": len(target),
        }
    )
    return target


class Backtest:
    def __init__(
        self,
        features: dict[str, Any],
        cfg: Config,
        variant: Variant,
        target_fn: Callable[[pd.Timestamp], list[str]],
    ):
        self.f = features
        self.cfg = cfg
        self.variant = variant
        self.target_fn = target_fn
        self.cash = cfg.initial
        self.positions: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.equity: list[tuple[pd.Timestamp, float]] = []
        self.exposure: list[float] = []
        self.position_counts: list[int] = []

    def _price(self, frame: str, date: pd.Timestamp, symbol: str) -> float:
        value = self.f[frame].at[date, symbol]
        return float(value) if pd.notna(value) else float("nan")

    def _equity(self, date: pd.Timestamp, frame: str = "close") -> float:
        value = self.cash
        for symbol, position in self.positions.items():
            price = self._price(frame, date, symbol)
            if not np.isfinite(price) or price <= 0:
                price = position["last_price"]
            if frame == "close" and np.isfinite(price) and price > 0:
                position["last_price"] = price
            value += position["shares"] * price
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

    def _sell(self, date: pd.Timestamp, symbol: str, raw_price: float, reason: str) -> bool:
        position = self.positions.get(symbol)
        if position is None or not np.isfinite(raw_price) or raw_price <= 0:
            return False
        price = raw_price * (1 - self.cfg.slippage)
        notional = position["shares"] * price
        commission = _commission(notional, self.cfg)
        stamp = notional * self.cfg.stamp_tax
        self.cash += notional - commission - stamp
        pnl = (
            (price - position["entry_price"]) * position["shares"]
            - commission
            - stamp
            - position["buy_commission"]
        )
        self.trades.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "symbol": symbol,
                "side": "sell",
                "shares": position["shares"],
                "price": round(price, 4),
                "notional": round(notional, 2),
                "pnl": round(pnl, 2),
                "reason": reason,
            }
        )
        del self.positions[symbol]
        return True

    def _buy(self, date: pd.Timestamp, symbol: str, raw_price: float, target_value: float) -> bool:
        if not np.isfinite(raw_price) or raw_price <= 0:
            return False
        price = raw_price * (1 + self.cfg.slippage)
        budget = min(target_value, self.cash)
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
        return True

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
                target = self.target_fn(execution_map[date])
                target_set = set(target)
                for symbol in list(self.positions):
                    if symbol in target_set:
                        continue
                    if self._limit_down(date, symbol):
                        continue
                    raw_price = self._price("open", date, symbol)
                    if not np.isfinite(raw_price) or raw_price <= 0:
                        raw_price = self.positions[symbol]["last_price"]
                    self._sell(date, symbol, raw_price, "monthly_out")
                if target:
                    equity = self._equity(date, frame="open")
                    target_value = equity / len(target)
                    for symbol in target:
                        if symbol in self.positions:
                            continue
                        if self._limit_up(date, symbol):
                            continue
                        self._buy(date, symbol, self._price("open", date, symbol), target_value)

            equity = self._equity(date, frame="close")
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
            [value for _, value in self.equity],
            index=pd.DatetimeIndex([date for date, _ in self.equity]),
        )
        years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
        total = curve.iloc[-1] / curve.iloc[0] - 1.0
        cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1.0
        returns = pd.Series(daily_returns)
        sharpe = float(returns.mean() / returns.std() * math.sqrt(252)) if returns.std() > 0 else 0.0
        sells = [trade for trade in self.trades if trade["side"] == "sell"]
        wins = [trade for trade in sells if trade["pnl"] > 0]
        losses = [trade for trade in sells if trade["pnl"] <= 0]
        avg_win = np.mean([trade["pnl"] for trade in wins]) if wins else 0.0
        avg_loss = np.mean([trade["pnl"] for trade in losses]) if losses else 0.0
        turnover = sum(trade["notional"] for trade in self.trades)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panels", default=str(STUDY_DIR / "panels_2018_2024.pkl"))
    parser.add_argument("--current", default="cache/stock_pit/universe/csi1000_current.csv")
    parser.add_argument("--quality", default="cache/stock_pit/financials/abstract_annual.csv")
    parser.add_argument("--index-file", default=str(STUDY_DIR / "index_399101.csv"))
    parser.add_argument("--out", default=str(STUDY_DIR / "quality_filter"))
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--initial", type=float, default=250_000.0)
    parser.add_argument("--extension-dir", default="cache/stock_pit/prices")
    args = parser.parse_args()

    cfg = Config(start=args.start, end=args.end, initial=args.initial)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    panels = _load_panels(Path(args.panels))
    current_all = _load_current(Path(args.current))
    if pd.Timestamp(args.end) > pd.Timestamp("2024-12-31"):
        panels = extend_panels(panels, Path(args.extension_dir), current_all, args.start)
    quality = load_quality(Path(args.quality))
    features = build_features(panels, current_all, cfg)
    current_with_prices = list(features["close"].columns)
    execution_map = _rebalance_map(features["close"].index, cfg)

    result: dict[str, Any] = {
        "config": asdict(cfg),
        "data": {
            "current_csi1000": len(current_all),
            "current_with_price": len(current_with_prices),
            "current_missing_price": len(set(current_all) - set(current_with_prices)),
            "quality_symbols": int(quality["symbol"].nunique()),
            "quality_report_years": [int(quality["report_year"].min()), int(quality["report_year"].max())],
            "signals": len(execution_map),
        },
        "benchmark_399101": benchmark_metrics(
            pd.read_csv(args.index_file, parse_dates=["date"]).set_index("date")["close"],
            cfg,
        ),
        "variants": {},
    }
    curves: dict[str, pd.Series] = {}
    all_stats: list[dict[str, Any]] = []
    for variant in VARIANTS:
        stats: list[dict[str, Any]] = []
        engine = Backtest(
            features,
            cfg,
            variant,
            lambda signal, v=variant, s=stats: select_targets(signal, features, quality, v, cfg, s),
        )
        metrics = engine.run(execution_map)
        result["variants"][variant.name] = {**metrics, "variant": asdict(variant)}
        curves[variant.name] = pd.Series(
            [value for _, value in engine.equity],
            index=pd.DatetimeIndex([date for date, _ in engine.equity]),
        )
        all_stats.extend(stats)
        pd.DataFrame(engine.trades).to_csv(
            Path(args.out) / f"trades_{variant.name}.csv", index=False, encoding="utf-8-sig"
        )
        print(variant.name, metrics, flush=True)

    result["curves"] = pd.DataFrame({"benchmark_399101": pd.read_csv(args.index_file, parse_dates=["date"]).set_index("date")["close"], **curves}).dropna(how="all").loc[cfg.start:cfg.end]
    result["quality_counts"] = all_stats
    result["limitations"] = [
        "Universe is the 2026-11-05 CSI 1000 snapshot, not historical PIT membership; absolute returns are biased.",
        "The A/B uses the same fixed universe, so the marginal filter effect is more informative than the level.",
        "Quality is annual-report data joined to actual disclosure dates; missing actual dates fail the gate.",
        "Goodwill missing is treated as zero; the abstract source omits some zero/non-applicable items.",
        "No delisted stocks, no historical minute/tick, no intraday stop sequencing.",
        "No parameter tuning: loose=ROE 8/ROA 4, strict=ROE 15/ROA 10, plus common balance-sheet/cash-flow gates.",
    ]
    (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    result["curves"].to_csv(out / "curves.csv", float_format="%.2f")
    pd.DataFrame(all_stats).to_csv(out / "quality_counts.csv", index=False, encoding="utf-8-sig")
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
