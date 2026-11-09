"""ETF premium/discount and high-low spread proxy data closure.

V5 needs ETF premium/discount, bid-ask and partial-fill data.  The local cache
has none.  This script fetches what is actually obtainable:

* raw daily OHLCV from Sina (`fund_etf_hist_sina`);
* historical unit NAV from Eastmoney (`fund_etf_fund_info_em`);
* premium/discount = raw close / unit NAV - 1;
* Corwin-Schultz high-low spread proxy from raw high/low.

It does **not** invent bid-ask quotes or partial fills.  The spread proxy is an
estimator, not a quote; partial-fill and queue data remain unavailable.

Cached under ``D:/tmp/etf_microstructure`` by default.  Research-only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from etf_rotation.engine import CORE, MARKET, SATELLITE

SYMBOLS = [MARKET, *CORE, *SATELLITE]
DEFAULT_OUT = Path("D:/tmp/etf_microstructure")
START = "20190101"
END = "20261105"


def _sina_symbol(symbol: str) -> str:
    return ("sh" if symbol.startswith(("5", "6", "9")) else "sz") + symbol


def _safe_name(symbol: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", symbol)


def _fetch_raw(symbol: str, out: Path) -> pd.DataFrame:
    path = out / "raw" / f"{_safe_name(symbol)}.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["date"])
    import akshare as ak

    frame = ak.fund_etf_hist_sina(symbol=_sina_symbol(symbol))
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return frame


def _fetch_nav(symbol: str, out: Path) -> pd.DataFrame:
    path = out / "nav" / f"{_safe_name(symbol)}.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["date"])
    import akshare as ak

    frame = ak.fund_etf_fund_info_em(fund=symbol, start_date=START, end_date=END)
    frame = frame.rename(columns={"净值日期": "date", "单位净值": "nav"})
    frame = frame[["date", "nav"]].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["nav"] = pd.to_numeric(frame["nav"], errors="coerce")
    frame = frame.dropna(subset=["date", "nav"]).sort_values("date")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return frame


def _corwin_schultz(high: pd.Series, low: pd.Series) -> pd.Series:
    """Daily high-low spread estimator (Corwin & Schultz, 2012)."""
    high = high.astype(float)
    low = low.astype(float)
    valid = (high > 0) & (low > 0) & (high >= low)
    high = high.where(valid)
    low = low.where(valid)
    hl = np.log(high / low)
    beta = hl ** 2 + hl.shift(-1) ** 2
    gamma = np.log(
        high.combine(high.shift(-1), max) / low.combine(low.shift(-1), min)
    ) ** 2
    alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / (3.0 - 2.0 * np.sqrt(2.0)) - np.sqrt(
        gamma / (3.0 - 2.0 * np.sqrt(2.0))
    )
    spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    return spread.clip(lower=0.0, upper=0.05)


def _summarize(values: pd.Series) -> dict[str, Any]:
    clean = values.replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return {"count": 0}
    return {
        "count": int(len(clean)),
        "mean": round(float(clean.mean()), 6),
        "std": round(float(clean.std(ddof=1)), 6),
        "p01": round(float(clean.quantile(0.01)), 6),
        "p05": round(float(clean.quantile(0.05)), 6),
        "p50": round(float(clean.quantile(0.50)), 6),
        "p95": round(float(clean.quantile(0.95)), 6),
        "p99": round(float(clean.quantile(0.99)), 6),
        "share_abs_gt_1pct": round(float((clean.abs() > 0.01).mean()), 4),
        "share_abs_gt_2pct": round(float((clean.abs() > 0.02).mean()), 4),
    }


def fetch_all(out: Path, *, max_workers: int = 4, cached_only: bool = False) -> dict[str, Any]:
    raw: dict[str, pd.DataFrame] = {}
    nav: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}

    def fetch_one(kind: str, symbol: str) -> pd.DataFrame | None:
        path = out / kind / f"{_safe_name(symbol)}.csv"
        if cached_only and not path.exists():
            errors[f"{kind}:{symbol}"] = "not cached (cached-only mode)"
            return None
        return _fetch_raw(symbol, out) if kind == "raw" else _fetch_nav(symbol, out)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        raw_jobs = {pool.submit(fetch_one, "raw", symbol): symbol for symbol in SYMBOLS}
        for future in as_completed(raw_jobs):
            symbol = raw_jobs[future]
            try:
                frame = future.result()
                if frame is not None:
                    raw[symbol] = frame
            except Exception as exc:  # noqa: BLE001
                errors[f"raw:{symbol}"] = f"{type(exc).__name__}: {exc}"
        nav_jobs = {pool.submit(fetch_one, "nav", symbol): symbol for symbol in SYMBOLS}
        for future in as_completed(nav_jobs):
            symbol = nav_jobs[future]
            try:
                frame = future.result()
                if frame is not None:
                    nav[symbol] = frame
            except Exception as exc:  # noqa: BLE001
                errors[f"nav:{symbol}"] = f"{type(exc).__name__}: {exc}"

    close = pd.DataFrame({symbol: frame.set_index("date")["close"] for symbol, frame in raw.items()})
    high = pd.DataFrame({symbol: frame.set_index("date")["high"] for symbol, frame in raw.items()})
    low = pd.DataFrame({symbol: frame.set_index("date")["low"] for symbol, frame in raw.items()})
    nav_panel = pd.DataFrame({symbol: frame.set_index("date")["nav"] for symbol, frame in nav.items()})
    close = close.sort_index()
    high = high.reindex(close.index)
    low = low.reindex(close.index)
    nav_panel = nav_panel.reindex(close.index).ffill()

    premium = close / nav_panel - 1.0
    premium = premium.where(premium.abs() <= 0.20)  # split/distribution artefacts
    spread = pd.DataFrame({symbol: _corwin_schultz(high[symbol], low[symbol]) for symbol in high.columns})

    out.mkdir(parents=True, exist_ok=True)
    premium.to_csv(out / "premium_discount.csv")
    spread.to_csv(out / "spread_proxy.csv")
    (out / "half_spread.csv").write_text(
        spread.div(2.0).to_csv(), encoding="utf-8",
    )

    premium_values = premium.stack()
    spread_values = spread.stack()
    summary = {
        "period": {"start": str(close.index.min().date()), "end": str(close.index.max().date())},
        "symbols_requested": len(SYMBOLS),
        "raw_symbols": len(raw),
        "nav_symbols": len(nav),
        "errors": errors,
        "premium_discount": _summarize(premium_values),
        "spread_proxy_round_trip": _summarize(spread_values),
        "spread_proxy_half": _summarize(spread_values / 2.0),
        "data_status": {
            "premium_discount": "historical NAV + raw close; usable as diagnostic, not a transaction cost",
            "bid_ask": "high-low Corwin-Schultz proxy; not a quote",
            "partial_fill": "unavailable; no queue/order-book data",
            "pit_delisted_universe": "SSE historical scale is available; SZSE historical list is not; current 47 are current-listed",
        },
        "notes": [
            "Premium/discount uses raw close and unit NAV; distributions can create artefacts and are clipped to +/-20%.",
            "The spread proxy is an estimator from daily high/low; it is not a quoted bid-ask spread.",
            "Use premium/discount to diagnose entry valuation, not as an extra constant cost on top of market prices.",
        ],
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--cached-only", action="store_true",
                        help="build from existing cache; do not fetch missing symbols")
    args = parser.parse_args()
    summary = fetch_all(
        Path(args.out), max_workers=args.max_workers, cached_only=args.cached_only,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
