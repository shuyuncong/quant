"""Build the long-history ETF cache used by ``etf_optimization_study.py``.

The runtime cache (``cache/etf_rotation``) only keeps about 1,200 bars per ETF
and has no money-market or Nasdaq ETF.  This script writes a separate research
cache from Eastmoney forward-adjusted daily bars and never touches the runtime
one.

Usage
-----
    python fetch_etf_research_cache.py --out cache/etf_research_long
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from etf_extended_study import BOND, GOLD, MARKET, SATELLITE
from etf_optimization_study import CASH_ETF, NASDAQ_ETF

COLUMNS = {"日期": "datetime", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
           "成交量": "volume", "成交额": "amount"}
SYMBOLS = [*SATELLITE, MARKET, GOLD, BOND, CASH_ETF, NASDAQ_ETF]


def normalise(raw: pd.DataFrame) -> pd.DataFrame:
    """Eastmoney ETF history in the cache schema, oldest first, one row per day."""
    frame = raw.rename(columns=COLUMNS)[list(COLUMNS.values())].copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    for column in frame.columns.drop("datetime"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    frame = frame.dropna(subset=["close"]).drop_duplicates("datetime", keep="last")
    frame = frame.sort_values("datetime").reset_index(drop=True)
    frame["is_closed"] = True
    frame.attrs.update(source="akshare_em_qfq", adjust="qfq", timeframe="1d")
    return frame


def fetch(symbol: str, start: str, end: str, retries: int = 3) -> pd.DataFrame:
    import akshare as ak

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            raw = ak.fund_etf_hist_em(symbol=symbol, period="daily", start_date=start,
                                      end_date=end, adjust="qfq")
            if raw is None or raw.empty:
                raise ValueError("empty history")
            return normalise(raw)
        except Exception as exc:  # network, block page, malformed payload
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--out", default="cache/etf_research_long")
    parser.add_argument("--start", default="20100101")
    parser.add_argument("--end", default=pd.Timestamp.now().strftime("%Y%m%d"))
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for symbol in SYMBOLS:
        try:
            frame = fetch(symbol, args.start, args.end)
        except RuntimeError as exc:
            failed[symbol] = str(exc)
            continue
        frame.to_pickle(out / f"{symbol}_qfq.pkl")
        report[symbol] = {
            "rows": int(len(frame)),
            "first": str(frame["datetime"].iloc[0].date()),
            "last": str(frame["datetime"].iloc[-1].date()),
            "min_close": float(frame["close"].min()),
        }
    (out / "fetch_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"written={len(report)} failed={len(failed)} {failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
