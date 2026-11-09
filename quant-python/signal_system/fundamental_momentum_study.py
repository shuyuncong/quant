"""Fundamental + momentum research study (development-only).

This script builds a point-in-time (PIT) monthly panel from a local 300-stock
fundamental snapshot and the repository price cache, then evaluates:

* factor Rank IC / ICIR / quintile spreads;
* long-only monthly portfolios with A-share costs, lot size and T+1;
* a research long-short earnings-surprise spread (not an executable strategy).

It deliberately does not touch ``config/config.yaml``, production strategy code,
the broker, or the web app.  Results are written to a new output directory and
must not be treated as a production authorization.

The target requested for this study was Sharpe > 1.5 and win rate > 60%.  The
script records whether each evaluated variant meets it; it does not tune
parameters until the target is met.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

VERSION = "fundamental_momentum_study.v1"
TARGET_SHARPE = 1.5
TARGET_WIN_RATE = 0.60

COMMISSION_PCT = 0.0003
MIN_COMMISSION = 5.0
STAMP_TAX_PCT = 0.001
SLIPPAGE_PCT = 0.0005
LOT_SIZE = 100
INITIAL_CASH = 250_000.0
LIMIT_UP_PCT = 0.095

DEFAULT_DATA_DIR = Path("D:/tmp/fund300")
DEFAULT_PRICE_DIR = Path(__file__).resolve().parent / "cache" / "daily_history"
DEFAULT_FINANCIAL = "financial_real.jsonl"
DEFAULT_MARKET = "market.jsonl"
DEFAULT_SYMBOLS = "fund300_symbols.json"
DEFAULT_INDUSTRY = "D:/tmp/fund300_industry.json"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _sha256(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _price_manifest(price_dir: Path, symbols: list[str]) -> dict[str, Any]:
    entries: list[str] = []
    total_bytes = 0
    missing: list[str] = []
    for symbol in symbols:
        path = price_dir / f"{symbol}_qfq.pkl"
        if not path.exists():
            missing.append(symbol)
            continue
        stat = path.stat()
        total_bytes += stat.st_size
        entries.append(f"{symbol}|{stat.st_size}|{stat.st_mtime_ns}")
    return {
        "files": len(entries),
        "missing": missing,
        "total_bytes": total_bytes,
        "manifest_sha256": hashlib.sha256("\n".join(entries).encode()).hexdigest(),
    }


def statutory_deadline(period_date: pd.Timestamp) -> pd.Timestamp:
    """Latest statutory disclosure date for an A-share report period.

    Q1: April 30; H1: August 31; Q3: October 31; annual: April 30 next year.
    For an unknown quarter we conservatively add four months.
    """

    if pd.isna(period_date):
        return pd.NaT
    year, month = int(period_date.year), int(period_date.month)
    if month == 3:
        return pd.Timestamp(year, 4, 30)
    if month == 6:
        return pd.Timestamp(year, 8, 31)
    if month == 9:
        return pd.Timestamp(year, 10, 31)
    if month == 12:
        return pd.Timestamp(year + 1, 4, 30)
    return period_date + pd.Timedelta(days=120)


def report_availability(
    period_date: pd.Timestamp,
    ann_date: pd.Timestamp,
    estimated: bool,
) -> pd.Timestamp:
    """Return the first date on which a report may be used.

    Exact announcement dates are used directly.  Estimated dates are pushed to
    the statutory deadline when the estimate is earlier than the deadline.  A
    report never becomes available on or before its period end.
    """

    if pd.isna(period_date):
        return pd.NaT
    deadline = statutory_deadline(period_date)
    if estimated or pd.isna(ann_date):
        available = deadline if pd.isna(ann_date) else max(ann_date, deadline)
    else:
        available = ann_date
    available = pd.Timestamp(available)
    if available <= period_date:
        available = period_date + pd.Timedelta(days=1)
    return available


def load_financials(path: Path) -> pd.DataFrame:
    raw = pd.DataFrame(_read_jsonl(path))
    if raw.empty:
        raise ValueError(f"financial file is empty: {path}")
    raw["symbol"] = raw["symbol"].astype(str).str.zfill(6)
    raw["period_date"] = pd.to_datetime(
        raw["period"].astype(str).str.replace("-", ""), errors="coerce"
    )
    raw["ann_date"] = pd.to_datetime(raw["ann_date"], errors="coerce")
    raw["estimated"] = raw["ann_date_estimated"].astype(bool)
    raw = raw.dropna(subset=["period_date"]).copy()
    raw["available_date"] = [
        report_availability(p, a, e)
        for p, a, e in zip(raw["period_date"], raw["ann_date"], raw["estimated"])
    ]
    raw = raw.dropna(subset=["available_date"])
    raw = raw.sort_values(["symbol", "available_date", "period_date"])
    raw = raw.drop_duplicates(["symbol", "period_date"], keep="last")

    # Same-quarter previous-year ROE, used as a simple earnings-surprise proxy.
    previous = raw[["symbol", "period_date", "roe", "debt_ratio"]].rename(
        columns={
            "period_date": "previous_period",
            "roe": "roe_previous",
            "debt_ratio": "debt_previous",
        }
    )
    raw["previous_period"] = raw["period_date"] - pd.DateOffset(years=1)
    raw = raw.merge(previous, on=["symbol", "previous_period"], how="left")
    raw["roe_yoy"] = raw["roe"] - raw["roe_previous"]
    raw["debt_yoy"] = raw["debt_ratio"] - raw["debt_previous"]
    return raw


def load_prices(
    price_dir: Path,
    symbols: list[str],
    *,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    missing: list[str] = []
    for symbol in symbols:
        path = price_dir / f"{symbol}_qfq.pkl"
        if not path.exists():
            missing.append(symbol)
            continue
        frame = pd.read_pickle(path)
        if "datetime" not in frame.columns:
            missing.append(symbol)
            continue
        frame = frame.copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        frame = frame.sort_values("datetime").drop_duplicates("datetime")
        frame["symbol"] = symbol
        frames.append(
            frame[["datetime", "symbol", "open", "close", "volume", "amount"]]
        )
    if not frames:
        raise ValueError(f"no price files found under {price_dir}")
    prices = pd.concat(frames, ignore_index=True)
    prices["date"] = prices["datetime"].dt.normalize()
    prices = prices[prices["date"] <= end]

    def pivot(value: str) -> pd.DataFrame:
        return (
            prices.pivot_table(index="date", columns="symbol", values=value, aggfunc="last")
            .sort_index()
            .astype(float)
        )

    close = pivot("close")
    open_ = pivot("open").reindex(index=close.index, columns=close.columns)
    volume = pivot("volume").reindex(index=close.index, columns=close.columns)
    amount = pivot("amount").reindex(index=close.index, columns=close.columns)
    if missing:
        close = close.drop(columns=[s for s in missing if s in close.columns], errors="ignore")
        open_ = open_.drop(columns=[s for s in missing if s in open_.columns], errors="ignore")
        volume = volume.drop(columns=[s for s in missing if s in volume.columns], errors="ignore")
        amount = amount.drop(columns=[s for s in missing if s in amount.columns], errors="ignore")
    return close, open_, volume, amount


def load_market(
    path: Path,
    *,
    signal_dates: pd.DatetimeIndex,
    symbols: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = pd.DataFrame(_read_jsonl(path))
    if raw.empty:
        raise ValueError(f"market file is empty: {path}")
    raw["symbol"] = raw["symbol"].astype(str).str.zfill(6)
    raw["date"] = pd.to_datetime(raw["trade_date"])
    raw = raw[raw["symbol"].isin(symbols)]
    pe = raw.pivot_table(index="date", columns="symbol", values="pe", aggfunc="last").sort_index()
    mv = raw.pivot_table(index="date", columns="symbol", values="circ_mv", aggfunc="last").sort_index()
    pe = pe.reindex(pe.index.union(signal_dates)).sort_index().ffill(limit=5).reindex(signal_dates)
    mv = mv.reindex(mv.index.union(signal_dates)).sort_index().ffill(limit=5).reindex(signal_dates)
    return pe, mv


def month_end_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    series = pd.Series(index, index=index)
    ends = series.groupby([index.year, index.month]).max()
    return pd.DatetimeIndex(ends.values).sort_values()


def build_panel(
    financial: pd.DataFrame,
    close: pd.DataFrame,
    open_: pd.DataFrame,
    volume: pd.DataFrame,
    pe: pd.DataFrame,
    mv: pd.DataFrame,
    symbols: list[str],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    signal_dates = month_end_dates(close.index)
    signal_dates = signal_dates[(signal_dates >= start) & (signal_dates <= end)]
    if len(signal_dates) < 24:
        raise ValueError("not enough month-end signal dates")

    grid = pd.MultiIndex.from_product(
        [signal_dates, symbols], names=["date", "symbol"]
    ).to_frame(index=False)
    right = financial[
        [
            "symbol",
            "period_date",
            "available_date",
            "roe",
            "debt_ratio",
            "roe_yoy",
            "debt_yoy",
            "estimated",
        ]
    ].sort_values(["available_date", "symbol"])
    panel = pd.merge_asof(
        grid.sort_values("date"),
        right,
        left_on="date",
        right_on="available_date",
        by="symbol",
        direction="backward",
        allow_exact_matches=False,
    )
    panel["report_age_days"] = (panel["date"] - panel["period_date"]).dt.days

    position = {date: i for i, date in enumerate(close.index)}
    factor_frames: list[pd.DataFrame] = []
    for date in signal_dates:
        i = position[date]
        price = close.iloc[i]
        if i >= 252:
            mom_12_1 = close.iloc[i - 21] / close.iloc[i - 252] - 1
            mom_6_1 = close.iloc[i - 21] / close.iloc[i - 126] - 1
            mom_3_1 = close.iloc[i - 21] / close.iloc[i - 63] - 1
        else:
            nan = pd.Series(np.nan, index=close.columns)
            mom_12_1, mom_6_1, mom_3_1 = nan, nan, nan
        mom_1 = close.iloc[i] / close.iloc[i - 21] - 1 if i >= 21 else pd.Series(
            np.nan, index=close.columns
        )
        daily = close.iloc[max(0, i - 60) : i + 1].pct_change().iloc[1:]
        vol_60 = daily.std() * math.sqrt(252)
        vol_20 = volume.iloc[max(0, i - 19) : i + 1].mean()
        factor_frames.append(
            pd.DataFrame(
                {
                    "date": date,
                    "symbol": close.columns,
                    "price": price.values,
                    "mom_12_1": mom_12_1.values,
                    "mom_6_1": mom_6_1.values,
                    "mom_3_1": mom_3_1.values,
                    "mom_1": mom_1.values,
                    "vol_60": vol_60.values,
                    "vol_20": vol_20.values,
                    "pe": pe.loc[date].reindex(close.columns).values,
                    "market_cap": mv.loc[date].reindex(close.columns).values,
                }
            )
        )
    factors = pd.concat(factor_frames, ignore_index=True)
    panel = panel.merge(factors, on=["date", "symbol"], how="left")

    panel["earnings_yield"] = np.where(
        panel["pe"].notna() & (panel["pe"] != 0), 1.0 / panel["pe"], np.nan
    )
    panel["earnings_yield"] = panel["earnings_yield"].clip(-0.5, 0.5)
    panel["log_market_cap"] = np.log(panel["market_cap"].where(panel["market_cap"] > 0))
    panel["days_since_announcement"] = (
        panel["date"] - panel["available_date"]
    ).dt.days
    panel["earnings_surprise_90"] = np.where(
        panel["days_since_announcement"] <= 90, panel["roe_yoy"], np.nan
    )
    panel["earnings_surprise_75"] = np.where(
        panel["days_since_announcement"] <= 75, panel["roe_yoy"], np.nan
    )
    panel["earnings_surprise_120"] = np.where(
        panel["days_since_announcement"] <= 120, panel["roe_yoy"], np.nan
    )

    def rank(column: str, *, ascending: bool = True) -> pd.Series:
        return panel.groupby("date")[column].rank(pct=True, ascending=ascending)

    panel["mom_12_1_r"] = rank("mom_12_1")
    panel["mom_6_1_r"] = rank("mom_6_1")
    panel["mom_3_1_r"] = rank("mom_3_1")
    panel["mom_1_r"] = rank("mom_1")
    panel["vol_60_r"] = rank("vol_60", ascending=False)
    panel["earnings_yield_r"] = rank("earnings_yield")
    panel["roe_r"] = rank("roe")
    panel["debt_ratio_r"] = rank("debt_ratio", ascending=False)
    panel["roe_yoy_r"] = rank("roe_yoy")
    panel["log_market_cap_r"] = rank("log_market_cap", ascending=False)
    for window in (75, 90, 120):
        panel[f"earnings_surprise_{window}_r"] = rank(
            f"earnings_surprise_{window}"
        )

    panel["quality"] = 0.5 * panel["roe_r"] + 0.5 * panel["debt_ratio_r"]
    panel["value"] = panel["earnings_yield_r"]
    panel["momentum"] = panel["mom_12_1_r"]
    panel["low_vol"] = panel["vol_60_r"]
    panel["qvm_lv"] = (
        0.4 * panel["momentum"]
        + 0.2 * panel["quality"]
        + 0.2 * panel["value"]
        + 0.2 * panel["low_vol"]
    )
    panel["eligible"] = (
        (panel["price"] >= 1.0)
        & (panel["market_cap"] >= 2e9)
        & panel["mom_12_1"].notna()
        & panel["earnings_yield"].notna()
        & panel["roe"].notna()
        & panel["debt_ratio"].notna()
        & (panel["report_age_days"] <= 270)
    )
    return panel, signal_dates


def add_forward_returns(
    panel: pd.DataFrame,
    close: pd.DataFrame,
    signal_dates: pd.DatetimeIndex,
    *,
    horizons: tuple[int, ...] = (1, 3, 6),
) -> pd.DataFrame:
    for horizon in horizons:
        frames: list[pd.DataFrame] = []
        for i, date in enumerate(signal_dates):
            if i + horizon >= len(signal_dates):
                continue
            future = close.loc[signal_dates[i + horizon]] / close.loc[date] - 1.0
            frames.append(
                pd.DataFrame(
                    {
                        "date": date,
                        "symbol": close.columns,
                        f"fwd_{horizon}m": future.values,
                    }
                )
            )
        if frames:
            panel = panel.merge(
                pd.concat(frames, ignore_index=True),
                on=["date", "symbol"],
                how="left",
            )
    return panel


def rank_ic(panel: pd.DataFrame, column: str) -> dict[str, Any]:
    rows: list[tuple[float, float]] = []
    for _, group in panel[panel["eligible"]].groupby("date"):
        sample = group.dropna(subset=[column, "fwd_1m", "fwd_3m"])
        if len(sample) < 20:
            continue
        ic_1 = sample[column].corr(sample["fwd_1m"], method="spearman")
        ic_3 = sample[column].corr(sample["fwd_3m"], method="spearman")
        if pd.notna(ic_1) and pd.notna(ic_3):
            rows.append((float(ic_1), float(ic_3)))
    if not rows:
        return {"days": 0}
    values = pd.DataFrame(rows, columns=["ic_1m", "ic_3m"])
    return {
        "days": int(len(values)),
        "ic_1m_mean": round(float(values["ic_1m"].mean()), 4),
        "ic_1m_median": round(float(values["ic_1m"].median()), 4),
        "ic_1m_positive_pct": round(float((values["ic_1m"] > 0).mean() * 100), 1),
        "ic_1m_icir": round(
            float(values["ic_1m"].mean() / values["ic_1m"].std()), 3
        )
        if values["ic_1m"].std() > 0
        else None,
        "ic_3m_mean": round(float(values["ic_3m"].mean()), 4),
        "ic_3m_positive_pct": round(float((values["ic_3m"] > 0).mean() * 100), 1),
    }


def quantile_spread(
    panel: pd.DataFrame,
    column: str,
    *,
    quantiles: int = 5,
    min_names: int = 60,
) -> dict[str, Any]:
    monthly: list[float] = []
    active = 0
    for _, group in panel[panel["eligible"]].groupby("date"):
        sample = group.dropna(subset=[column, "fwd_1m"])
        if len(sample) < min_names:
            monthly.append(0.0)
            continue
        ranked = sample[column].rank(method="first")
        try:
            buckets = pd.qcut(ranked, quantiles, labels=False, duplicates="drop")
        except ValueError:
            monthly.append(0.0)
            continue
        means = sample.groupby(buckets)["fwd_1m"].mean()
        if len(means) < quantiles:
            monthly.append(0.0)
            continue
        monthly.append(float(means.iloc[-1] - means.iloc[0]))
        active += 1
    series = pd.Series(monthly, dtype=float)
    if series.empty:
        return {"months": 0}
    active_values = series[series != 0]
    return {
        "months": int(len(series)),
        "active_months": int(active),
        "mean_monthly_pct": round(float(series.mean() * 100), 3),
        "annualized_pct": round(float(series.mean() * 12 * 100), 2),
        "sharpe_full": round(
            float(series.mean() / series.std() * math.sqrt(12)), 3
        )
        if series.std() > 0
        else None,
        "win_rate_full_pct": round(float((series > 0).mean() * 100), 1),
        "active_mean_monthly_pct": round(float(active_values.mean() * 100), 3)
        if len(active_values)
        else None,
        "active_sharpe": round(
            float(active_values.mean() / active_values.std() * math.sqrt(12)), 3
        )
        if len(active_values) > 1 and active_values.std() > 0
        else None,
        "active_win_rate_pct": round(float((active_values > 0).mean() * 100), 1)
        if len(active_values)
        else None,
    }


def _commission(gross: float) -> float:
    return max(MIN_COMMISSION, gross * COMMISSION_PCT)


def _metrics(equity: pd.Series, trades: pd.DataFrame, exposure: list[float]) -> dict[str, Any]:
    returns = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 else None
    sharpe = (
        float(returns.mean() / returns.std() * math.sqrt(252))
        if returns.std() > 0
        else None
    )
    drawdown = float((equity / equity.cummax() - 1).min())
    monthly = equity.resample("ME").last().pct_change().dropna()
    monthly_win = float((monthly > 0).mean()) if len(monthly) else None
    trade_win = float((trades["pnl"] > 0).mean()) if len(trades) else None
    return {
        "start": equity.index[0].date().isoformat(),
        "end": equity.index[-1].date().isoformat(),
        "total_return_pct": round(float(equity.iloc[-1] / equity.iloc[0] - 1) * 100, 2),
        "cagr_pct": round(float(cagr) * 100, 2) if cagr is not None else None,
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "max_drawdown_pct": round(drawdown * 100, 2),
        "monthly_win_rate_pct": round(monthly_win * 100, 1)
        if monthly_win is not None
        else None,
        "trade_win_rate_pct": round(trade_win * 100, 1)
        if trade_win is not None
        else None,
        "trades": int(len(trades)),
        "avg_holding_days": round(float(trades["days"].mean()), 1)
        if len(trades)
        else None,
        "avg_exposure_pct": round(float(np.mean(exposure)) * 100, 1)
        if exposure
        else None,
    }


def simulate_long_only(
    panel: pd.DataFrame,
    close: pd.DataFrame,
    open_: pd.DataFrame,
    signal_dates: pd.DatetimeIndex,
    *,
    score_column: str,
    top_n: int,
    market_ma: str | None = None,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    close_m = close.values.astype(float)
    open_m = open_.values.astype(float)
    previous_close_m = close.shift(1).values.astype(float)
    columns = list(close.columns)
    column_index = {symbol: i for i, symbol in enumerate(columns)}
    date_index = {date: i for i, date in enumerate(close.index)}

    execution: dict[pd.Timestamp, pd.Timestamp] = {}
    for date in signal_dates:
        position = close.index.searchsorted(date, side="right")
        if position < len(close.index):
            execution[date] = close.index[position]

    ew_index = close.mean(axis=1)
    moving_average = {
        "ma200": ew_index.rolling(200, min_periods=200).mean(),
        "ma60": ew_index.rolling(60, min_periods=60).mean(),
    }

    targets: dict[pd.Timestamp, list[int]] = {}
    for date in signal_dates:
        if date < start or date > end or date not in execution:
            continue
        if market_ma is not None:
            ma = moving_average[market_ma]
            if date not in ma.index or pd.isna(ma.loc[date]) or ew_index.loc[date] <= ma.loc[date]:
                targets[date] = []
                continue
        sample = panel[
            (panel["date"] == date) & panel["eligible"]
        ].dropna(subset=[score_column])
        selected = sample.nlargest(top_n, score_column)["symbol"].tolist()
        targets[date] = [column_index[s] for s in selected if s in column_index]

    execution_to_signal = {execution[date]: date for date in targets}
    start_position = date_index[pd.Timestamp(start)]
    cash = INITIAL_CASH
    shares = np.zeros(len(columns))
    last_close = np.zeros(len(columns))
    basis = np.zeros(len(columns))
    entry_position = np.full(len(columns), -1, dtype=int)
    equity = np.full(len(close_m), np.nan)
    exposure: list[float] = []
    trades: list[dict[str, Any]] = []
    traded_notional = 0.0
    min_cash = INITIAL_CASH

    for position in range(start_position, len(close_m)):
        date = close.index[position]
        if date in execution_to_signal:
            signal_date = execution_to_signal[date]
            target_list = targets.get(signal_date, [])
            target_set = set(target_list)

            for j in list(np.nonzero(shares)[0]):
                if j in target_set:
                    continue
                price = open_m[position, j]
                if price != price:
                    continue
                previous = previous_close_m[position, j]
                if previous == previous and price / previous - 1 <= -LIMIT_UP_PCT:
                    continue
                quantity = shares[j]
                sell_price = price * (1 - SLIPPAGE_PCT)
                gross = quantity * sell_price
                commission = _commission(gross)
                stamp = gross * STAMP_TAX_PCT
                cash += gross - commission - stamp
                min_cash = min(min_cash, cash)
                traded_notional += gross
                pnl = (gross - commission - stamp) - basis[j]
                trades.append(
                    {
                        "symbol": columns[j],
                        "entry": close.index[entry_position[j]].date().isoformat()
                        if entry_position[j] >= 0
                        else None,
                        "exit": date.date().isoformat(),
                        "quantity": int(quantity),
                        "pnl": float(pnl),
                        "ret": float(pnl / basis[j]) if basis[j] > 0 else None,
                        "days": int(position - entry_position[j])
                        if entry_position[j] >= 0
                        else None,
                    }
                )
                shares[j] = 0.0
                basis[j] = 0.0
                entry_position[j] = -1

            if target_list:
                portfolio_value = cash
                for j in np.nonzero(shares)[0]:
                    price = open_m[position, j]
                    portfolio_value += shares[j] * (
                        price if price == price else last_close[j]
                    )
                weight = 1.0 / len(target_list)
                for j in target_list:
                    price = open_m[position, j]
                    if price != price:
                        continue
                    previous = previous_close_m[position, j]
                    if previous == previous and price / previous - 1 >= LIMIT_UP_PCT:
                        continue
                    delta = portfolio_value * weight - shares[j] * price
                    if delta > 0:
                        buy_price = price * (1 + SLIPPAGE_PCT)
                        quantity = int(delta / (buy_price * LOT_SIZE)) * LOT_SIZE
                        if quantity <= 0:
                            continue
                        gross = quantity * buy_price
                        commission = _commission(gross)
                        if gross + commission > cash:
                            quantity = (
                                int(max(0.0, cash - MIN_COMMISSION) / (buy_price * LOT_SIZE))
                                * LOT_SIZE
                            )
                            gross = quantity * buy_price
                            commission = _commission(gross)
                        if quantity <= 0 or gross + commission > cash:
                            continue
                        cash -= gross + commission
                        min_cash = min(min_cash, cash)
                        traded_notional += gross
                        if shares[j] <= 0:
                            entry_position[j] = position
                        shares[j] += quantity
                        basis[j] += gross + commission
                    elif delta < 0:
                        quantity = min(
                            int((-delta) / price / LOT_SIZE) * LOT_SIZE,
                            int(shares[j]),
                        )
                        if quantity <= 0:
                            continue
                        previous = previous_close_m[position, j]
                        if previous == previous and price / previous - 1 <= -LIMIT_UP_PCT:
                            continue
                        sell_price = price * (1 - SLIPPAGE_PCT)
                        gross = quantity * sell_price
                        commission = _commission(gross)
                        stamp = gross * STAMP_TAX_PCT
                        cash += gross - commission - stamp
                        min_cash = min(min_cash, cash)
                        traded_notional += gross
                        total = shares[j]
                        shares[j] -= quantity
                        basis[j] = basis[j] * (shares[j] / total) if total > 0 else 0.0
                        if shares[j] <= 0:
                            pnl = (gross - commission - stamp) - basis[j]
                            trades.append(
                                {
                                    "symbol": columns[j],
                                    "entry": close.index[entry_position[j]].date().isoformat()
                                    if entry_position[j] >= 0
                                    else None,
                                    "exit": date.date().isoformat(),
                                    "quantity": int(quantity),
                                    "pnl": float(pnl),
                                    "ret": float(pnl / basis[j]) if basis[j] > 0 else None,
                                    "days": int(position - entry_position[j])
                                    if entry_position[j] >= 0
                                    else None,
                                }
                            )
                            basis[j] = 0.0
                            entry_position[j] = -1

        value = cash
        invested = 0.0
        for j in np.nonzero(shares)[0]:
            price = close_m[position, j]
            if price == price:
                last_close[j] = price
            else:
                price = last_close[j]
            value += shares[j] * price
            invested += shares[j] * price
        equity[position] = value
        exposure.append(invested / value if value > 0 else 0.0)

    equity_series = pd.Series(
        equity[start_position:], index=close.index[start_position:]
    ).dropna()
    trade_frame = pd.DataFrame(trades)
    if trade_frame.empty:
        trade_frame = pd.DataFrame(columns=["symbol", "entry", "exit", "pnl", "ret", "days"])
    result = _metrics(equity_series, trade_frame, exposure)
    result["score"] = score_column
    result["top_n"] = top_n
    result["market_ma"] = market_ma
    result["turnover_x"] = round(
        traded_notional / float(equity_series.mean()) / max(years_between(equity_series), 1e-9),
        2,
    ) if len(equity_series) else None
    result["min_cash"] = round(float(min_cash), 2)
    return {"equity": equity_series, "trades": trade_frame, "metrics": result}


def years_between(equity: pd.Series) -> float:
    return (equity.index[-1] - equity.index[0]).days / 365.25


def simulate_long_short(
    panel: pd.DataFrame,
    *,
    column: str,
    quantiles: int = 10,
    min_names: int = 60,
    cost_per_active_month: float = 0.0,
) -> dict[str, Any]:
    dates = sorted(panel["date"].unique())
    monthly: list[float] = []
    active_flags: list[bool] = []
    for date in dates:
        sample = panel[
            (panel["date"] == date) & panel["eligible"]
        ].dropna(subset=[column, "fwd_1m"])
        if len(sample) < min_names:
            monthly.append(0.0)
            active_flags.append(False)
            continue
        ranked = sample[column].rank(method="first")
        try:
            buckets = pd.qcut(ranked, quantiles, labels=False, duplicates="drop")
        except ValueError:
            monthly.append(0.0)
            active_flags.append(False)
            continue
        means = sample.groupby(buckets)["fwd_1m"].mean()
        if len(means) < quantiles:
            monthly.append(0.0)
            active_flags.append(False)
            continue
        monthly.append(float(means.iloc[-1] - means.iloc[0]))
        active_flags.append(True)
    series = pd.Series(monthly, index=pd.DatetimeIndex(dates), dtype=float)
    active = series[series.index.isin(series.index[active_flags])]
    net = series.copy()
    if cost_per_active_month:
        net.loc[active.index] = net.loc[active.index] - cost_per_active_month
    return {
        "column": column,
        "quantiles": quantiles,
        "months": int(len(series)),
        "active_months": int(sum(active_flags)),
        "gross_annualized_pct": round(float(series.mean() * 12 * 100), 2),
        "gross_sharpe_full": round(
            float(series.mean() / series.std() * math.sqrt(12)), 3
        )
        if series.std() > 0
        else None,
        "gross_win_rate_full_pct": round(float((series > 0).mean() * 100), 1),
        "gross_sharpe_active": round(
            float(active.mean() / active.std() * math.sqrt(12)), 3
        )
        if len(active) > 1 and active.std() > 0
        else None,
        "gross_win_rate_active_pct": round(float((active > 0).mean() * 100), 1)
        if len(active)
        else None,
        "net_annualized_pct": round(float(net.mean() * 12 * 100), 2),
        "net_sharpe_full": round(
            float(net.mean() / net.std() * math.sqrt(12)), 3
        )
        if net.std() > 0
        else None,
        "net_win_rate_full_pct": round(float((net > 0).mean() * 100), 1),
        "monthly_series": series,
    }


def _target_met(metrics: dict[str, Any]) -> bool:
    sharpe = metrics.get("sharpe")
    win = metrics.get("trade_win_rate_pct")
    return bool(
        sharpe is not None
        and win is not None
        and sharpe > TARGET_SHARPE
        and win > TARGET_WIN_RATE * 100
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, pd.Series):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return value


def write_report(
    output_dir: Path,
    *,
    meta: dict[str, Any],
    factor_ic: dict[str, Any],
    long_only: dict[str, Any],
    long_short: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(
            _json_safe(
                {
                    "meta": meta,
                    "factor_ic": factor_ic,
                    "long_only": long_only,
                    "long_short": long_short,
                }
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append("# Fundamental + momentum research study")
    lines.append("")
    lines.append(f"- Version: `{VERSION}`")
    lines.append(f"- Sample: {meta['sample_start']} → {meta['sample_end']}")
    lines.append(f"- Universe: {meta['universe_symbols']} symbols")
    lines.append(
        f"- Target: Sharpe > {TARGET_SHARPE:.1f} and trade win rate > {TARGET_WIN_RATE * 100:.0f}%"
    )
    lines.append(
        "- Costs: commission 3bp (min 5 CNY), stamp tax 10bp on sells, slippage 5bp, lot 100"
    )
    lines.append("")
    lines.append("## Factor IC (monthly, eligible universe)")
    lines.append("")
    lines.append("| Factor | IC 1M | ICIR 1M | Positive days | IC 3M |")
    lines.append("|---|---:|---:|---:|---:|")
    for name, row in factor_ic.items():
        lines.append(
            f"| `{name}` | {row.get('ic_1m_mean')} | {row.get('ic_1m_icir')} | "
            f"{row.get('ic_1m_positive_pct')}% | {row.get('ic_3m_mean')} |"
        )
    lines.append("")
    lines.append("## Long-only portfolios")
    lines.append("")
    lines.append(
        "| Variant | CAGR | Sharpe | Max DD | Monthly win | Trade win | Trades | Exposure |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, row in long_only.items():
        metrics = row["metrics"]
        lines.append(
            f"| `{name}` | {metrics.get('cagr_pct')}% | {metrics.get('sharpe')} | "
            f"{metrics.get('max_drawdown_pct')}% | {metrics.get('monthly_win_rate_pct')}% | "
            f"{metrics.get('trade_win_rate_pct')}% | {metrics.get('trades')} | "
            f"{metrics.get('avg_exposure_pct')}% |"
        )
    lines.append("")
    lines.append("## Long-short research spread (not executable in A-share retail)")
    lines.append("")
    lines.append(
        "| Variant | Active months | Gross annual | Gross Sharpe (full) | Gross win (full) | "
        "Net annual | Net Sharpe (full) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for name, row in long_short.items():
        lines.append(
            f"| `{name}` | {row['active_months']}/{row['months']} | "
            f"{row['gross_annualized_pct']}% | {row['gross_sharpe_full']} | "
            f"{row['gross_win_rate_full_pct']}% | {row['net_annualized_pct']}% | "
            f"{row['net_sharpe_full']} |"
        )
    lines.append("")
    best_long = max(
        long_only.values(),
        key=lambda item: item["metrics"].get("sharpe") or -999,
    )["metrics"]
    lines.append("## Verdict")
    lines.append("")
    lines.append(
        f"- Best long-only: Sharpe {best_long.get('sharpe')}, trade win "
        f"{best_long.get('trade_win_rate_pct')}%, max drawdown "
        f"{best_long.get('max_drawdown_pct')}%."
    )
    lines.append(
        f"- Target met by a long-only variant: "
        f"{'yes' if _target_met(best_long) else 'no'}."
    )
    lines.append(
        "- The long-short earnings-surprise spread is a research diagnostic only. "
        "It requires shorting, borrow availability and financing costs that are not "
        "available to the retail constraints of this repository."
    )
    lines.append(
        "- No parameter was tuned to force the target. Production decision: none."
    )
    lines.append("")
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR)
    parser.add_argument("--financial-file", default=DEFAULT_FINANCIAL)
    parser.add_argument("--market-file", default=DEFAULT_MARKET)
    parser.add_argument("--symbols-file", default=DEFAULT_SYMBOLS)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", default="2018-01-31")
    parser.add_argument("--end", default="2026-08-31")
    parser.add_argument("--price-end", default="2026-10-30")
    args = parser.parse_args(argv)

    def resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else args.data_dir / path

    financial_path = resolve(args.financial_file)
    market_path = resolve(args.market_file)
    symbols_path = resolve(args.symbols_file)
    if not financial_path.exists():
        raise FileNotFoundError(financial_path)
    if not market_path.exists():
        raise FileNotFoundError(market_path)
    if not symbols_path.exists():
        raise FileNotFoundError(symbols_path)

    symbols = [str(symbol).zfill(6) for symbol in json.loads(symbols_path.read_text(encoding="utf-8"))]
    financial = load_financials(financial_path)
    financial = financial[financial["symbol"].isin(symbols)]
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    price_end = pd.Timestamp(args.price_end)

    close, open_, volume, amount = load_prices(
        args.price_dir, symbols, end=price_end
    )
    available_symbols = [symbol for symbol in symbols if symbol in close.columns]
    signal_dates = month_end_dates(close.index)
    signal_dates = signal_dates[(signal_dates >= start) & (signal_dates <= end)]
    pe, mv = load_market(
        market_path, signal_dates=signal_dates, symbols=available_symbols
    )
    panel, signal_dates = build_panel(
        financial,
        close,
        open_,
        volume,
        pe,
        mv,
        available_symbols,
        start=start,
        end=end,
    )
    panel = add_forward_returns(panel, close, signal_dates)

    factor_columns = [
        "mom_12_1",
        "mom_6_1",
        "mom_3_1",
        "mom_1",
        "vol_60",
        "earnings_yield",
        "roe",
        "debt_ratio",
        "roe_yoy",
        "earnings_surprise_75",
        "earnings_surprise_90",
        "earnings_surprise_120",
        "quality",
        "value",
        "momentum",
        "qvm_lv",
    ]
    factor_ic = {column: rank_ic(panel, column) for column in factor_columns}

    long_only_specs = [
        ("qvm_lv_top20", "qvm_lv", 20, None),
        ("qvm_lv_top30", "qvm_lv", 30, None),
        ("qvm_lv_top50", "qvm_lv", 50, None),
        ("quality_top20", "quality", 20, None),
        ("value_top20", "value", 20, None),
        ("earnings_surprise_75_top20", "earnings_surprise_75_r", 20, None),
        ("earnings_surprise_90_top20", "earnings_surprise_90_r", 20, None),
        ("qvm_lv_top20_ma200", "qvm_lv", 20, "ma200"),
        ("qvm_lv_top30_ma200", "qvm_lv", 30, "ma200"),
        ("qvm_lv_top50_ma200", "qvm_lv", 50, "ma200"),
        ("qvm_lv_top30_ma60", "qvm_lv", 30, "ma60"),
    ]
    long_only_results: dict[str, Any] = {}
    for name, column, top_n, market_ma in long_only_specs:
        result = simulate_long_only(
            panel,
            close,
            open_,
            signal_dates,
            score_column=column,
            top_n=top_n,
            market_ma=market_ma,
            start=start,
            end=end,
        )
        long_only_results[name] = {
            "metrics": result["metrics"],
            "equity_last": round(float(result["equity"].iloc[-1]), 2),
            "equity_dates": [
                result["equity"].index[0].date().isoformat(),
                result["equity"].index[-1].date().isoformat(),
            ],
        }

    long_short_results: dict[str, Any] = {}
    for name, column, quantiles in [
        ("earnings_surprise_75_q5", "earnings_surprise_75_r", 5),
        ("earnings_surprise_75_q10", "earnings_surprise_75_r", 10),
        ("earnings_surprise_90_q5", "earnings_surprise_90_r", 5),
        ("earnings_surprise_90_q10", "earnings_surprise_90_r", 10),
        ("earnings_surprise_120_q10", "earnings_surprise_120_r", 10),
    ]:
        result = simulate_long_short(
            panel,
            column=column,
            quantiles=quantiles,
            min_names=60,
            cost_per_active_month=0.0015,
        )
        result.pop("monthly_series", None)
        long_short_results[name] = result

    meta = {
        "version": VERSION,
        "sample_start": start.date().isoformat(),
        "sample_end": end.date().isoformat(),
        "price_end": price_end.date().isoformat(),
        "universe_symbols": len(available_symbols),
        "requested_symbols": len(symbols),
        "signal_dates": len(signal_dates),
        "rows": len(panel),
        "eligible_rows": int(panel["eligible"].sum()),
        "data_files": {
            "financial": {
                "path": str(financial_path),
                "sha256": _sha256(financial_path),
            },
            "market": {"path": str(market_path), "sha256": _sha256(market_path)},
            "symbols": {"path": str(symbols_path), "sha256": _sha256(symbols_path)},
            "prices": _price_manifest(args.price_dir, available_symbols),
        },
        "pit_rule": (
            "exact announcement date when available; otherwise statutory deadline; "
            "report must be available strictly before the signal date"
        ),
        "target": {
            "sharpe": TARGET_SHARPE,
            "trade_win_rate_pct": TARGET_WIN_RATE * 100,
        },
        "costs": {
            "commission_pct": COMMISSION_PCT,
            "minimum_commission": MIN_COMMISSION,
            "stamp_tax_pct": STAMP_TAX_PCT,
            "slippage_pct": SLIPPAGE_PCT,
            "lot_size": LOT_SIZE,
            "initial_cash": INITIAL_CASH,
        },
    }
    write_report(
        args.out,
        meta=meta,
        factor_ic=factor_ic,
        long_only=long_only_results,
        long_short=long_short_results,
    )
    print(json.dumps(_json_safe(meta), ensure_ascii=False, indent=2))
    print(f"wrote {args.out / 'summary.json'}")
    print(f"wrote {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
