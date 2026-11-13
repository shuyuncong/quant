"""Build a point-in-time ETF universe from exchange scale snapshots.

The runtime ETF cache only contains current-listed symbols, so backtests using
it are exposed to survivorship bias.  The exchanges publish historical scale
tables that still contain funds which later delisted or liquidated.  This
script samples month-end snapshots, records first/last seen dates for every
code, and writes a reusable universe file for research backtests.

Data sources
------------
SSE: ``akshare.fund_etf_scale_sse(date="YYYYMMDD")``
SZSE: ``akshare.fund_scale_daily_szse(start_date, end_date, symbol="ETF")``

Usage
-----
python fetch_etf_pit_universe.py --out cache/etf_pit
python fetch_etf_pit_universe.py --out cache/etf_pit --start 2020-01-01 --end 2026-10-30
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd

COLUMNS = ["date", "code", "name", "exchange", "shares"]


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def month_ends(start: date, end: date) -> list[date]:
    """Month-end dates in ``[start, end]`` plus the requested end date."""
    stamps = pd.date_range(start=start, end=end, freq="ME").date.tolist()
    if not stamps or stamps[-1] != end:
        stamps.append(end)
    return [stamp for stamp in stamps if start <= stamp <= end]


def snapshot_dates(start: date, end: date, freq: str) -> list[date]:
    """Snapshot calendar: month-end (``M``), quarter-end (``Q``) or annual (``A``)."""
    if freq == "M":
        return month_ends(start, end)
    if freq == "Q":
        stamps = pd.date_range(start=start, end=end, freq="QE").date.tolist()
    elif freq == "A":
        stamps = pd.date_range(start=start, end=end, freq="YE").date.tolist()
    else:
        raise ValueError("freq must be 'M', 'Q' or 'A'")
    if not stamps or stamps[-1] != end:
        stamps.append(end)
    return [stamp for stamp in stamps if start <= stamp <= end]


def _snapshot_sse(month_end: date) -> tuple[date, pd.DataFrame]:
    """SSE snapshot at the latest available day in the last five days."""
    last_error: Exception | None = None
    for offset in range(5):
        query_date = month_end - timedelta(days=offset)
        try:
            frame = ak.fund_etf_scale_sse(date=query_date.strftime("%Y%m%d"))
        except Exception as error:  # network/API failures are retried once
            last_error = error
            time.sleep(1.0)
            try:
                frame = ak.fund_etf_scale_sse(date=query_date.strftime("%Y%m%d"))
            except Exception as retry_error:
                last_error = retry_error
                continue
        if frame is not None and not frame.empty:
            normalised = pd.DataFrame(
                {
                    "date": pd.to_datetime(frame["统计日期"]).dt.date,
                    "code": frame["基金代码"].astype(str).str.zfill(6),
                    "name": frame["基金简称"].astype(str),
                    "exchange": "SSE",
                    "shares": pd.to_numeric(frame["基金份额"], errors="coerce"),
                }
            )
            return query_date, normalised
    if last_error is not None:
        raise last_error
    return month_end, pd.DataFrame(columns=COLUMNS)


def _snapshot_szse(month_end: date) -> tuple[date, pd.DataFrame]:
    """SZSE snapshot at the latest available day in the last five days."""
    last_error: Exception | None = None
    for offset in range(5):
        query_date = month_end - timedelta(days=offset)
        try:
            frame = ak.fund_scale_daily_szse(
                start_date=query_date.strftime("%Y%m%d"),
                end_date=query_date.strftime("%Y%m%d"),
                symbol="ETF",
            )
        except Exception as error:
            last_error = error
            time.sleep(1.0)
            try:
                frame = ak.fund_scale_daily_szse(
                    start_date=query_date.strftime("%Y%m%d"),
                    end_date=query_date.strftime("%Y%m%d"),
                    symbol="ETF",
                )
            except Exception as retry_error:
                last_error = retry_error
                continue
        if frame is not None and not frame.empty:
            normalised = pd.DataFrame(
                {
                    "date": pd.to_datetime(frame["日期"]).dt.date,
                    "code": frame["基金代码"].astype(str).str.zfill(6),
                    "name": frame["基金简称"].astype(str),
                    "exchange": "SZSE",
                    "shares": pd.to_numeric(frame["基金份额"], errors="coerce"),
                }
            )
            return query_date, normalised
    if last_error is not None:
        raise last_error
    return month_end, pd.DataFrame(columns=COLUMNS)


def fetch_szse_snapshots(stamps: list[date]) -> tuple[pd.DataFrame, list[str]]:
    """Sample one SZSE snapshot per requested month/quarter end."""
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for stamp in stamps:
        print(f"SZSE snapshot {stamp} ...", flush=True)
        try:
            _, frame = _snapshot_szse(stamp)
            if not frame.empty:
                frames.append(frame)
        except Exception as error:
            errors.append(f"SZSE {stamp}: {type(error).__name__}: {error}")
        time.sleep(0.25)
    if not frames:
        return pd.DataFrame(columns=COLUMNS), errors
    return pd.concat(frames, ignore_index=True), errors


def build_universe(snapshots: pd.DataFrame, end: date) -> pd.DataFrame:
    """Aggregate snapshots into one row per ETF code."""
    if snapshots.empty:
        return pd.DataFrame(
            columns=[
                "code",
                "name",
                "exchange",
                "first_date",
                "last_date",
                "snapshots",
                "max_shares",
                "last_shares",
                "status",
            ]
        )
    latest_date = snapshots["date"].max()
    grouped = (
        snapshots.sort_values("date")
        .groupby("code", as_index=False)
        .agg(
            name=("name", "last"),
            exchange=("exchange", "last"),
            first_date=("date", "min"),
            last_date=("date", "max"),
            snapshots=("date", "nunique"),
            max_shares=("shares", "max"),
            last_shares=("shares", "last"),
        )
    )
    grouped["status"] = grouped["last_date"].map(
        lambda value: "current" if value >= latest_date - timedelta(days=45) else "delisted_or_stale"
    )
    return grouped.sort_values(["exchange", "code"]).reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("cache/etf_pit"))
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--refresh", action="store_true", help="ignore cached snapshots")
    parser.add_argument("--freq", choices=("M", "Q", "A"), default="A",
                        help="snapshot frequency: month/quarter/year end (default: A)")
    parser.add_argument("--sse-only", action="store_true")
    parser.add_argument("--szse-only", action="store_true")
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end)
    if start > end:
        raise SystemExit("--start must not be after --end")
    args.out.mkdir(parents=True, exist_ok=True)
    snapshots_path = args.out / "snapshots.csv"
    universe_path = args.out / "universe.csv"
    stamps = snapshot_dates(start, end, args.freq)

    if snapshots_path.exists() and not args.refresh:
        snapshots = pd.read_csv(snapshots_path, parse_dates=["date"])
        snapshots["date"] = snapshots["date"].dt.date
    else:
        frames: list[pd.DataFrame] = []
        errors: list[str] = []
        if not args.szse_only:
            for stamp in stamps:
                print(f"SSE snapshot {stamp} ...", flush=True)
                try:
                    _, frame = _snapshot_sse(stamp)
                    if not frame.empty:
                        frames.append(frame)
                except Exception as error:
                    errors.append(f"SSE {stamp}: {type(error).__name__}: {error}")
                time.sleep(0.25)
        if not args.sse_only:
            szse_frame, szse_errors = fetch_szse_snapshots(stamps)
            errors.extend(szse_errors)
            if not szse_frame.empty:
                frames.append(szse_frame)
        snapshots = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
        snapshots = snapshots.drop_duplicates(subset=["date", "code"], keep="last")
        snapshots.sort_values(["date", "exchange", "code"]).to_csv(snapshots_path, index=False)
        if errors:
            (args.out / "errors.txt").write_text("\n".join(errors), encoding="utf-8")

    snapshots = snapshots[COLUMNS]
    universe = build_universe(snapshots, end)
    universe.to_csv(universe_path, index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "freq": args.freq,
        "snapshots": int(len(snapshots)),
        "codes": int(len(universe)),
        "current": int((universe["status"] == "current").sum()),
        "delisted_or_stale": int((universe["status"] == "delisted_or_stale").sum()),
        "sse": int((universe["exchange"] == "SSE").sum()),
        "szse": int((universe["exchange"] == "SZSE").sum()),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
