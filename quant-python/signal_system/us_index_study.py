"""Nasdaq index / QQQ trend study.

Research-only.  This module answers three questions:

* can the existing stack obtain US index history without a paid feed?
* how would a simple, pre-registered trend rule have behaved on the Nasdaq?
* does the tradable proxy (QQQ) behave like the index after costs?

Data sources
------------
* Yahoo Finance chart API: QQQ (adjusted total return) and ^IXIC / ^NDX.
* FRED ``NASDAQCOM`` / ``NASDAQ100``: independent validation of the index
  closes when Yahoo is reachable; also a close-only fallback.

Execution model
---------------
Daily close signal, next trading day open fill, sells before buys (one asset
makes this implicit), whole/fractional shares, 5 bp slippage, zero commission,
cash earns 0%.  QQQ prices are adjusted for dividends/splits (total return);
index series are price returns only.  No withholding tax, FX, or financing is
modelled.

This is deliberately a small, fixed set of classic rules, not a parameter
sweep.  The main rule is the 200-day moving average; the other variants are
diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE = BASE_DIR / "cache" / "us_index"
DEFAULT_OUT = BASE_DIR.parents[1] / "docs" / "backtest-results" / "2026-11-08-nasdaq-index"

DEFAULT_START = "2004-01-02"
INITIAL = 250_000.0
SLIPPAGE = 0.0005
COMMISSION = 0.0
TRADING_DAYS = 252
SEED = 20261108

YAHOO_URL = (
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1={period1}&period2={period2}&interval=1d&events=div%2Csplits"
)
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
FRED_SERIES = {"^IXIC": "NASDAQCOM", "^NDX": "NASDAQ100"}
SYMBOLS = ("QQQ", "^IXIC", "^NDX")


def _http_get(url: str, timeout: int = 30) -> bytes:
    """HTTP GET with requests when available, urllib as the dependency-free fallback."""
    try:
        import requests

        response = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        return response.content
    except ImportError:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_name(symbol: str) -> str:
    return symbol.replace("^", "") + ".csv"


def cache_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / _cache_name(symbol)


def standardize_yahoo(payload: dict[str, Any], symbol: str) -> pd.DataFrame:
    """Convert a Yahoo chart payload to an adjusted OHLCV frame."""
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    timestamps = result["timestamp"]
    index = pd.to_datetime(
        [datetime.fromtimestamp(int(ts), tz=timezone.utc).date() for ts in timestamps]
    )
    frame = pd.DataFrame(
        {
            "open": quote["open"],
            "high": quote["high"],
            "low": quote["low"],
            "close": quote["close"],
            "volume": quote["volume"],
        },
        index=index,
    )
    adjclose = result["indicators"].get("adjclose")
    if adjclose and adjclose[0].get("adjclose"):
        frame["adjclose"] = adjclose[0]["adjclose"]
        factor: pd.Series = frame["adjclose"] / frame["close"]
        for column in ("open", "high", "low", "close"):
            frame[column] = frame[column] * factor
    else:
        frame["adjclose"] = frame["close"]
    frame.index.name = "date"
    frame = frame.dropna(subset=["open", "close"]).sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    return frame


def fetch_yahoo(symbol: str, start: str = DEFAULT_START,
                end: str | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    start_ts = int(pd.Timestamp(start, tz="UTC").timestamp())
    end_day = pd.Timestamp(end) if end else pd.Timestamp(datetime.now(timezone.utc).date())
    end_ts = int((end_day + pd.Timedelta(days=1)).timestamp())
    url = YAHOO_URL.format(symbol=symbol, period1=start_ts, period2=end_ts)
    payload = json.loads(_http_get(url))
    frame = standardize_yahoo(payload, symbol)
    return frame, {
        "symbol": symbol,
        "source": "yahoo_chart",
        "url": url,
        "rows": int(len(frame)),
        "first_date": frame.index.min().strftime("%Y-%m-%d"),
        "last_date": frame.index.max().strftime("%Y-%m-%d"),
    }


def fetch_fred(series: str, cache_dir: Path | None = None,
               refresh: bool = False) -> pd.DataFrame:
    path = cache_dir / "fred" / f"{series}.csv" if cache_dir else None
    if path is not None and path.exists() and not refresh:
        raw = path.read_bytes()
    else:
        raw = _http_get(FRED_URL.format(series=series))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    frame = pd.read_csv(pd.io.common.BytesIO(raw))
    frame.columns = ["date", "close"]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna().set_index("date").sort_index()
    for column in ("open", "high", "low", "adjclose"):
        frame[column] = frame["close"]
    frame["volume"] = np.nan
    frame.index.name = "date"
    return frame


def load_series(symbol: str, cache_dir: Path = DEFAULT_CACHE,
                start: str = DEFAULT_START, end: str | None = None,
                refresh: bool = False) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load a cached series, fetching it only when the cache is unusable."""
    path = cache_path(cache_dir, symbol)
    if path.exists() and not refresh:
        frame = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
        frame = frame.loc[pd.Timestamp(start):pd.Timestamp(end) if end else frame.index[-1]]
        return frame, {
            "symbol": symbol,
            "source": "cache",
            "path": str(path),
            "rows": int(len(frame)),
            "first_date": frame.index.min().strftime("%Y-%m-%d"),
            "last_date": frame.index.max().strftime("%Y-%m-%d"),
            "sha256": _sha256(path),
        }

    try:
        frame, meta = fetch_yahoo(symbol, start=start, end=end)
    except Exception as exc:  # network/API failure: fall back for index series
        if symbol not in FRED_SERIES:
            raise RuntimeError(
                f"cannot fetch {symbol} from Yahoo and no cache exists: {exc}"
            ) from exc
        frame = fetch_fred(FRED_SERIES[symbol])
        frame = frame.loc[pd.Timestamp(start):pd.Timestamp(end) if end else frame.index[-1]]
        meta = {
            "symbol": symbol,
            "source": "fred_fallback",
            "series": FRED_SERIES[symbol],
            "rows": int(len(frame)),
            "first_date": frame.index.min().strftime("%Y-%m-%d"),
            "last_date": frame.index.max().strftime("%Y-%m-%d"),
            "warning": f"Yahoo failed: {type(exc).__name__}: {exc}",
        }

    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    frame.to_csv(temporary, index_label="date")
    temporary.replace(path)
    meta["path"] = str(path)
    meta["sha256"] = _sha256(path)
    return frame, meta


def validate_index(frame: pd.DataFrame, series: str,
                   cache_dir: Path | None = None,
                   refresh: bool = False) -> dict[str, Any]:
    """Compare one index close series against FRED on overlapping dates."""
    fred = fetch_fred(series, cache_dir=cache_dir, refresh=refresh)
    common = frame.index.intersection(fred.index)
    if len(common) == 0:
        return {"series": series, "common_days": 0}
    left = frame.loc[common, "close"].astype(float)
    right = fred.loc[common, "close"].astype(float)
    diff = (left - right).abs()
    relative = diff / right.abs().replace(0, np.nan)
    return {
        "series": series,
        "common_days": int(len(common)),
        "max_abs_diff": round(float(diff.max()), 6),
        "max_rel_diff_pct": round(float(relative.max()) * 100, 6),
        "p99_rel_diff_pct": round(float(relative.quantile(0.99)) * 100, 6),
    }


def signal_buy_hold(close: pd.Series, index: int) -> float:
    return 1.0


def signal_ma(close: pd.Series, index: int, window: int = 200) -> float:
    if index < window:
        return 0.0
    return 1.0 if close.iloc[index] > close.iloc[index - window + 1:index + 1].mean() else 0.0


def signal_momentum(close: pd.Series, index: int) -> float:
    if index < 252:
        return 0.0
    return 1.0 if close.iloc[index - 21] / close.iloc[index - 252] - 1.0 > 0 else 0.0


def signal_golden_cross(close: pd.Series, index: int) -> float:
    if index < 200:
        return 0.0
    fast = close.iloc[index - 49:index + 1].mean()
    slow = close.iloc[index - 199:index + 1].mean()
    return 1.0 if fast > slow else 0.0


def signal_ma_vol_target(close: pd.Series, index: int, window: int = 200,
                         target_vol: float = 0.15) -> float:
    if index < max(window, 20):
        return 0.0
    if close.iloc[index] <= close.iloc[index - window + 1:index + 1].mean():
        return 0.0
    realised = close.pct_change().iloc[index - 19:index + 1].std() * math.sqrt(TRADING_DAYS)
    if not np.isfinite(realised) or realised <= 0:
        return 0.0
    return float(min(1.0, target_vol / realised))


def run_strategy(frame: pd.DataFrame, signal: Callable[[pd.Series, int], float], *,
                 start: str = DEFAULT_START, end: str | None = None,
                 initial: float = INITIAL, slippage: float = SLIPPAGE,
                 commission: float = COMMISSION, daily_rebalance: bool = False,
                 band: float = 0.0) -> dict[str, Any]:
    """Run one long/cash strategy with next-open execution.

    Binary signals only trade when the target flips.  A continuous signal
    (volatility target) trades only when the current weight differs from the
    target by more than ``band``; this prevents daily micro-rebalances.
    """
    data = frame.loc[pd.Timestamp(start):pd.Timestamp(end) if end else frame.index[-1]]
    if len(data) < 30:
        raise ValueError(f"backtest window too short: {len(data)} rows")
    dates = data.index
    close = data["close"].astype(float)
    open_ = data["open"].astype(float)

    cash = float(initial)
    units = 0.0
    last_target = 0.0
    pending: float | None = None
    equity: list[float] = []
    exposure: list[float] = []
    trades: list[dict[str, Any]] = []
    turnover = 0.0

    for i, day in enumerate(dates):
        price_open = float(open_.iloc[i])
        price_close = float(close.iloc[i])
        if pending is not None and np.isfinite(price_open) and price_open > 0:
            target = float(pending)
            equity_open = cash + units * price_open
            target_units = (equity_open * target) / price_open
            delta = target_units - units
            notional = abs(delta) * price_open
            if notional > 1e-9:
                cost = notional * (slippage + commission)
                if delta > 0:
                    cash -= notional + cost
                else:
                    cash += notional - cost
                trades.append({
                    "date": day.strftime("%Y-%m-%d"),
                    "side": "buy" if delta > 0 else "sell",
                    "price": round(price_open, 6),
                    "notional": round(notional, 2),
                    "cost": round(cost, 2),
                    "target_weight": round(target, 6),
                })
                turnover += notional / initial
                units = target_units
            last_target = target
            pending = None

        value = cash + units * price_close
        equity.append(value)
        exposure.append((units * price_close) / value if value > 0 else 0.0)

        if i + 1 < len(dates):
            target = float(signal(close, i))
            if daily_rebalance:
                current = (units * price_close) / value if value > 0 else 0.0
                if abs(target - current) > band:
                    pending = target
            elif abs(target - last_target) > 1e-12:
                pending = target

    series = pd.Series(equity, index=dates, name="equity")
    result = metrics(series, initial=initial, trades=trades, exposure=exposure)
    result["turnover_x"] = round(turnover, 2)
    result["trades_detail"] = trades
    result["curve"] = series
    return result


def metrics(equity: pd.Series, *, initial: float,
            trades: list[dict[str, Any]], exposure: list[float]) -> dict[str, Any]:
    returns = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = float(equity.iloc[-1] / initial - 1.0)
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 else float("nan")
    std = float(returns.std())
    sharpe = float(returns.mean() / std * math.sqrt(TRADING_DAYS)) if std > 0 else None
    drawdown = equity / equity.cummax() - 1.0
    max_drawdown = float(drawdown.min())
    calmar = float(cagr / abs(max_drawdown)) if max_drawdown < 0 else None

    year_end = equity.resample("YE").last()
    yearly = year_end.pct_change()
    if len(yearly):
        yearly.iloc[0] = year_end.iloc[0] / initial - 1.0

    return {
        "start": equity.index[0].strftime("%Y-%m-%d"),
        "end": equity.index[-1].strftime("%Y-%m-%d"),
        "years": round(years, 2),
        "initial": initial,
        "final_equity": round(float(equity.iloc[-1]), 2),
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "vol_pct": round(std * math.sqrt(TRADING_DAYS) * 100, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "calmar": round(calmar, 2) if calmar is not None else None,
        "daily_win_rate_pct": round(float((returns > 0).mean()) * 100, 2),
        "avg_exposure": round(float(np.mean(exposure)), 2),
        "trades": len(trades),
        "yearly": {
            str(getattr(index, "year", index)): round(float(value) * 100, 2)
            for index, value in yearly.items()
        },
    }


def _public(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key not in {"curve", "trades_detail"}}


def _returns(result: dict[str, Any]) -> pd.Series:
    return result["curve"].pct_change().dropna()


def run_study(cache_dir: Path = DEFAULT_CACHE, start: str = DEFAULT_START,
              end: str | None = None, out: Path = DEFAULT_OUT,
              refresh: bool = False, bootstrap: bool = True) -> dict[str, Any]:
    frames: dict[str, pd.DataFrame] = {}
    manifest: dict[str, Any] = {}
    for symbol in SYMBOLS:
        frames[symbol], manifest[symbol] = load_series(
            symbol, cache_dir=cache_dir, start=start, end=end, refresh=refresh
        )

    validation = {}
    for symbol, series in (("^IXIC", "NASDAQCOM"), ("^NDX", "NASDAQ100")):
        try:
            validation[symbol] = validate_index(
                frames[symbol], series, cache_dir=cache_dir, refresh=refresh
            )
        except Exception as exc:
            validation[symbol] = {"series": series, "error": f"{type(exc).__name__}: {exc}"}

    strategies: dict[str, Callable[..., float]] = {
        "buy_hold": signal_buy_hold,
        "ma200": signal_ma,
        "mom_12_1": signal_momentum,
        "golden_cross_50_200": signal_golden_cross,
        "ma200_vol15": signal_ma_vol_target,
    }
    results: dict[str, Any] = {}
    for symbol, frame in frames.items():
        results[symbol] = {}
        for name, signal in strategies.items():
            results[symbol][name] = run_strategy(
                frame, signal, start=start, end=end,
                daily_rebalance=(name == "ma200_vol15"),
                band=0.05 if name == "ma200_vol15" else 0.0,
            )

    qqq = results["QQQ"]
    splits = {
        "2004-2015": ("2004-01-02", "2015-12-31"),
        "2016-2026": ("2016-01-01", str(frames["QQQ"].index.max().date())),
    }
    split_results: dict[str, Any] = {}
    for label, (split_start, split_end) in splits.items():
        split_results[label] = {
            name: _public(run_strategy(
                frames["QQQ"], signal, start=split_start, end=split_end,
                daily_rebalance=(name == "ma200_vol15"),
                band=0.05 if name == "ma200_vol15" else 0.0,
            ))
            for name, signal in strategies.items()
        }

    cost_sensitivity = {}
    for bps in (0, 5, 10, 20):
        result = run_strategy(frames["QQQ"], signal_ma, start=start, end=end,
                              slippage=bps / 10000.0)
        cost_sensitivity[f"{bps}bp"] = _public(result)

    bootstrap_result: dict[str, Any] = {}
    if bootstrap:
        from strategy_statistics import (
            bootstrap_sharpe_ci,
            deflated_sharpe,
            paired_sharpe_diff,
        )

        ma_returns = _returns(qqq["ma200"])
        bh_returns = _returns(qqq["buy_hold"])
        trial_sharpes = [qqq[name]["sharpe"] for name in strategies if qqq[name]["sharpe"]]
        bootstrap_result = {
            "ma200": bootstrap_sharpe_ci(ma_returns, seed=SEED),
            "buy_hold": bootstrap_sharpe_ci(bh_returns, seed=SEED),
            "paired_ma200_minus_buy_hold": paired_sharpe_diff(ma_returns, bh_returns, seed=SEED),
            "deflated_sharpe_ma200": deflated_sharpe(
                ma_returns, n_trials=len(strategies), trial_sharpes=trial_sharpes
            ),
        }

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": {"start": start, "end": str(frames["QQQ"].index.max().date())},
        "execution": {
            "signal": "daily close",
            "fill": "next trading day open",
            "slippage_bp": int(round(SLIPPAGE * 10000)),
            "commission_bp": int(round(COMMISSION * 10000)),
            "cash_return": "0%",
            "qqq_prices": "adjusted total return",
            "index_prices": "price return",
        },
        "data": manifest,
        "validation": validation,
        "results": {symbol: {name: _public(result) for name, result in per.items()}
                    for symbol, per in results.items()},
        "split_qqq": split_results,
        "cost_sensitivity_qqq_ma200": cost_sensitivity,
        "bootstrap": bootstrap_result,
        "notes": [
            "Research only; no production configuration changed.",
            "QQQ adjusted OHLC includes dividends/splits; withholding tax is not modelled.",
            "Index series are price returns; index timing is hypothetical and cost-free.",
            "Cash earns 0%; using T-bills would raise the cash-only variants.",
        ],
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    curves = pd.DataFrame({
        "qqq_buy_hold": qqq["buy_hold"]["curve"],
        "qqq_ma200": qqq["ma200"]["curve"],
        "qqq_mom_12_1": qqq["mom_12_1"]["curve"],
        "qqq_golden_cross": qqq["golden_cross_50_200"]["curve"],
        "qqq_ma200_vol15": qqq["ma200_vol15"]["curve"],
        "ixic_buy_hold": results["^IXIC"]["buy_hold"]["curve"],
        "ixic_ma200": results["^IXIC"]["ma200"]["curve"],
        "ndx_buy_hold": results["^NDX"]["buy_hold"]["curve"],
        "ndx_ma200": results["^NDX"]["ma200"]["curve"],
    })
    curves.index.name = "date"
    curves.to_csv(out / "curves.csv")

    yearly = pd.DataFrame({
        "qqq_buy_hold": qqq["buy_hold"]["yearly"],
        "qqq_ma200": qqq["ma200"]["yearly"],
        "qqq_ma200_vol15": qqq["ma200_vol15"]["yearly"],
    })
    yearly.index.name = "year"
    yearly.to_csv(out / "yearly.csv")

    pd.DataFrame(qqq["ma200"]["trades_detail"]).to_csv(
        out / "trades_qqq_ma200.csv", index=False
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--no-bootstrap", action="store_true")
    args = parser.parse_args(argv)

    summary = run_study(
        cache_dir=Path(args.cache_dir),
        start=args.start,
        end=args.end,
        out=Path(args.out),
        refresh=args.refresh,
        bootstrap=not args.no_bootstrap,
    )
    print(json.dumps({
        "period": summary["period"],
        "qqq_ma200": summary["results"]["QQQ"]["ma200"],
        "qqq_buy_hold": summary["results"]["QQQ"]["buy_hold"],
        "ixic_ma200": summary["results"]["^IXIC"]["ma200"],
        "ixic_buy_hold": summary["results"]["^IXIC"]["buy_hold"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
