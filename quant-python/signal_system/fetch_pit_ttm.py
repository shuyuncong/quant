"""Build a point-in-time TTM earnings panel for the EP factor.

The annual panel in ``value_annual.csv`` is already PIT-safe.  This script adds
the three interim reports and constructs trailing-twelve-month parent net
profit:

    TTM = current YTD + prior annual - prior-year same YTD

Only reports with an announcement/notice date are used.  A TTM row is dated by
the *latest* component's availability, so it never uses a later report before
the market could have seen it.

Current-listed interim reports come from Eastmoney's ``stock_yjbb_em``.
Delisted interim reports come from the already-downloaded delisted profit
sheets.  Annual Q4 rows are taken from ``value_annual.csv`` (their disclosure
dates are the actual first disclosure dates).

Usage
-----
    python fetch_pit_ttm.py --years 2018-2025
    python fetch_pit_ttm.py --normalize-only
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from stock_pit_data import _save, _symbol


DEFAULT_OUT = Path("cache/stock_pit")
QUARTER_COLUMNS = [
    "symbol",
    "period",
    "report_year",
    "quarter",
    "net_profit_cum",
    "revenue_cum",
    "eps",
    "roe",
    "available_date",
    "listed_status",
    "source",
]
TTM_COLUMNS = [
    "symbol",
    "period",
    "report_year",
    "quarter",
    "net_profit",
    "revenue",
    "eps",
    "roe",
    "available_date",
    "listed_status",
    "source",
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quarter_ends(years: list[int]) -> list[pd.Timestamp]:
    dates: list[pd.Timestamp] = []
    for year in years:
        for month, day in ((3, 31), (6, 30), (9, 30)):
            dates.append(pd.Timestamp(year=year, month=month, day=day))
    return dates


def normalize_listed_quarter(raw: pd.DataFrame, period: pd.Timestamp) -> pd.DataFrame:
    frame = raw.rename(
        columns={
            "股票代码": "symbol",
            "净利润-净利润": "net_profit_cum",
            "营业总收入-营业总收入": "revenue_cum",
            "每股收益": "eps",
            "净资产收益率": "roe",
            "最新公告日期": "notice_date",
        }
    ).copy()
    frame["symbol"] = frame["symbol"].map(_symbol)
    frame["period"] = period
    frame["report_year"] = period.year
    frame["quarter"] = period.quarter
    for column in ("net_profit_cum", "revenue_cum", "eps", "roe"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    frame["available_date"] = pd.to_datetime(frame.get("notice_date"), errors="coerce")
    frame = frame.dropna(subset=["symbol", "available_date", "net_profit_cum"])
    frame = (
        frame.sort_values(["symbol", "period", "available_date"])
        .drop_duplicates(["symbol", "period"], keep="first")
    )
    frame["listed_status"] = "current"
    frame["source"] = "stock_yjbb_em"
    return frame[QUARTER_COLUMNS]


def _fetch_one_quarter(period: pd.Timestamp, raw_dir: Path, force: bool) -> pd.DataFrame:
    import akshare as ak

    path = raw_dir / f"{period.strftime('%Y%m%d')}.csv"
    if path.exists() and path.stat().st_size > 100 and not force:
        raw = pd.read_csv(path, dtype={"股票代码": str})
    else:
        last: Exception | None = None
        raw = None
        for attempt in range(4):
            try:
                raw = ak.stock_yjbb_em(date=period.strftime("%Y%m%d"))
                break
            except Exception as exc:  # noqa: BLE001 - transient page-stream failures
                last = exc
                time.sleep(1.0 + attempt * 1.5)
        if raw is None or raw.empty:
            raise RuntimeError(f"quarterly yjbb failed for {period.date()}: {last or 'empty'}")
        _save(raw, path, "stock_yjbb_em")
    return normalize_listed_quarter(raw, period)


def fetch_listed_quarters(
    out: Path, years: list[int], workers: int, force: bool
) -> pd.DataFrame:
    raw_dir = out / "financials" / "yjbb_quarterly"
    raw_dir.mkdir(parents=True, exist_ok=True)
    periods = quarter_ends(years)
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_fetch_one_quarter, period, raw_dir, force): period
            for period in periods
        }
        for index, future in enumerate(as_completed(futures), 1):
            period = futures[future]
            try:
                frame = future.result()
                if not frame.empty:
                    frames.append(frame)
            except Exception as exc:  # noqa: BLE001 - record and continue
                failures.append(f"{period.date()}: {exc}")
            if index % 4 == 0 or index == len(periods):
                print(
                    f"[quarters] {index}/{len(periods)} ok={len(frames)} fail={len(failures)}",
                    flush=True,
                )
    if failures:
        (out / "financials" / "value_ttm_errors.json").write_text(
            json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if not frames:
        return pd.DataFrame(columns=QUARTER_COLUMNS)
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values(["symbol", "period", "available_date"])
        .drop_duplicates(["symbol", "period"], keep="first")
    )


def normalize_delisted_quarters(out: Path) -> pd.DataFrame:
    raw_dir = out / "financials" / "profit_delisted"
    if not raw_dir.exists():
        return pd.DataFrame(columns=QUARTER_COLUMNS)
    frames: list[pd.DataFrame] = []
    for path in sorted(raw_dir.glob("*.csv")):
        try:
            raw = pd.read_csv(path, dtype={"SECURITY_CODE": str})
        except Exception:
            continue
        if "REPORT_DATE" not in raw.columns or "SECURITY_CODE" not in raw.columns:
            continue
        frame = raw.copy()
        frame["period"] = pd.to_datetime(frame["REPORT_DATE"], errors="coerce")
        frame = frame[frame["period"].dt.month.isin([3, 6, 9])].copy()
        if frame.empty:
            continue
        frame["symbol"] = frame["SECURITY_CODE"].map(_symbol)
        frame["report_year"] = frame["period"].dt.year
        frame["quarter"] = frame["period"].dt.quarter
        frame["net_profit_cum"] = pd.to_numeric(frame.get("PARENT_NETPROFIT"), errors="coerce")
        frame["revenue_cum"] = pd.to_numeric(frame.get("TOTAL_OPERATE_INCOME"), errors="coerce")
        frame["eps"] = pd.to_numeric(frame.get("BASIC_EPS"), errors="coerce")
        frame["roe"] = float("nan")
        frame["available_date"] = pd.to_datetime(frame.get("NOTICE_DATE"), errors="coerce")
        frame = frame.dropna(subset=["symbol", "period", "available_date", "net_profit_cum"])
        frame["listed_status"] = "delisted"
        frame["source"] = "stock_profit_sheet_by_report_delisted_em"
        frames.append(frame[QUARTER_COLUMNS])
    if not frames:
        return pd.DataFrame(columns=QUARTER_COLUMNS)
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values(["symbol", "period", "available_date"])
        .drop_duplicates(["symbol", "period"], keep="first")
    )


def fetch_quarterly_disclosure(out: Path, years: list[int], force: bool) -> pd.DataFrame:
    """Actual first-disclosure dates for Q1-Q3, from CNInfo's booking table."""

    import akshare as ak

    raw_dir = out / "disclosure"
    raw_dir.mkdir(parents=True, exist_ok=True)
    labels = {1: "一季", 2: "半年报", 3: "三季"}
    frames: list[pd.DataFrame] = []
    for year in years:
        for quarter, label in labels.items():
            period = pd.Timestamp(
                year=year,
                month={1: 3, 2: 6, 3: 9}[quarter],
                day={1: 31, 2: 30, 3: 30}[quarter],
            )
            path = raw_dir / f"quarterly_{year}Q{quarter}.csv"
            if path.exists() and path.stat().st_size > 100 and not force:
                raw = pd.read_csv(path, dtype={"股票代码": str})
            else:
                last: Exception | None = None
                raw = None
                for attempt in range(4):
                    try:
                        raw = ak.stock_report_disclosure(
                            market="沪深京", period=f"{year}{label}"
                        )
                        break
                    except Exception as exc:  # noqa: BLE001 - transient endpoint failure
                        last = exc
                        time.sleep(1.0 + attempt * 1.5)
                if raw is None or raw.empty:
                    raise RuntimeError(
                        f"quarterly disclosure failed for {year}Q{quarter}: {last or 'empty'}"
                    )
                _save(raw, path, "stock_report_disclosure")
            frame = raw.rename(
                columns={
                    "股票代码": "symbol",
                    "实际披露": "actual_disclosure",
                }
            ).copy()
            frame["symbol"] = frame["symbol"].map(_symbol)
            frame["period"] = period
            frame["report_year"] = year
            frame["quarter"] = quarter
            frame["actual_disclosure"] = pd.to_datetime(
                frame["actual_disclosure"], errors="coerce"
            )
            frame = frame.dropna(subset=["symbol", "actual_disclosure"])
            frames.append(
                frame[
                    ["symbol", "period", "report_year", "quarter", "actual_disclosure"]
                ]
            )
    if not frames:
        return pd.DataFrame(
            columns=["symbol", "period", "report_year", "quarter", "actual_disclosure"]
        )
    combined = (
        pd.concat(frames, ignore_index=True)
        .sort_values(["symbol", "period", "actual_disclosure"])
        .drop_duplicates(["symbol", "period"], keep="first")
    )
    _save(combined, out / "disclosure" / "disclosure_quarterly.csv", "stock_report_disclosure")
    return combined


def apply_quarterly_disclosure(
    interims: pd.DataFrame, disclosure: pd.DataFrame
) -> pd.DataFrame:
    """Replace endpoint dates with actual first-disclosure dates where covered.

    Current-listed rows without a CNInfo disclosure record are dropped: using
    the endpoint's "latest announcement" date for old reports is often years
    late and would silently destroy the signal.  Delisted rows keep the profit
    sheet's notice date, which is already a PIT announcement date.
    """

    if interims.empty or disclosure.empty:
        return interims
    frame = interims.copy()
    frame["period"] = pd.to_datetime(frame["period"], errors="coerce")
    frame["raw_available_date"] = pd.to_datetime(frame["available_date"], errors="coerce")
    merged = frame.merge(
        disclosure[["symbol", "period", "actual_disclosure"]],
        on=["symbol", "period"],
        how="left",
    )
    merged["available_date"] = merged["actual_disclosure"]
    missing = merged["available_date"].isna()
    # Keep the profit sheet's notice date for delisted names; current names
    # without an exchange disclosure record are not used.
    delisted_missing = missing & merged["listed_status"].eq("delisted")
    merged.loc[delisted_missing, "available_date"] = merged.loc[
        delisted_missing, "raw_available_date"
    ]
    merged = merged.dropna(subset=["available_date"])
    return merged.drop(columns=["actual_disclosure", "raw_available_date"])


def _annual_quarters(out: Path) -> pd.DataFrame:
    path = out / "financials" / "value_annual.csv"
    if not path.exists():
        return pd.DataFrame(columns=QUARTER_COLUMNS)
    annual = pd.read_csv(path, dtype={"symbol": str})
    annual["period"] = pd.to_datetime(
        annual["report_year"].astype(int).astype(str) + "1231", errors="coerce"
    )
    annual["quarter"] = 4
    annual["net_profit_cum"] = pd.to_numeric(annual["net_profit"], errors="coerce")
    annual["revenue_cum"] = pd.to_numeric(annual["revenue"], errors="coerce")
    annual["available_date"] = pd.to_datetime(annual["available_date"], errors="coerce")
    annual["source"] = annual.get("source", "value_annual")
    return annual[
        ["symbol", "period", "report_year", "quarter", "net_profit_cum",
         "revenue_cum", "eps", "roe", "available_date", "listed_status", "source"]
    ]


def build_ttm(interims: pd.DataFrame, annual: pd.DataFrame) -> pd.DataFrame:
    """Combine Q4 annual and Q1-Q3 interim rows into a TTM panel."""

    frames = [frame for frame in (interims, annual) if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=TTM_COLUMNS)
    panel = pd.concat(frames, ignore_index=True)
    panel["symbol"] = panel["symbol"].map(_symbol)
    panel["period"] = pd.to_datetime(panel["period"], errors="coerce")
    panel["available_date"] = pd.to_datetime(panel["available_date"], errors="coerce")
    panel["net_profit_cum"] = pd.to_numeric(panel["net_profit_cum"], errors="coerce")
    panel = panel.dropna(subset=["symbol", "period", "net_profit_cum", "available_date"])
    panel = (
        panel.sort_values(["symbol", "period", "available_date"])
        .drop_duplicates(["symbol", "period"], keep="first")
        .reset_index(drop=True)
    )

    annual_map = panel[panel["period"].dt.month.eq(12)][
        ["symbol", "period", "net_profit_cum", "available_date"]
    ].rename(
        columns={
            "period": "prior_annual_period",
            "net_profit_cum": "prior_annual_profit",
            "available_date": "prior_annual_date",
        }
    )
    same_map = panel[["symbol", "period", "net_profit_cum", "available_date"]].rename(
        columns={
            "period": "prior_same_period",
            "net_profit_cum": "prior_same_profit",
            "available_date": "prior_same_date",
        }
    )
    panel["prior_annual_period"] = pd.to_datetime(
        (panel["period"].dt.year - 1).astype(str) + "1231", errors="coerce"
    )
    panel["prior_same_period"] = panel["period"] - pd.DateOffset(years=1)
    panel = panel.merge(annual_map, on=["symbol", "prior_annual_period"], how="left")
    panel = panel.merge(same_map, on=["symbol", "prior_same_period"], how="left")

    is_q4 = panel["period"].dt.month.eq(12)
    ttm = panel["net_profit_cum"].copy()
    interim = ~is_q4
    ttm.loc[interim] = (
        panel.loc[interim, "net_profit_cum"]
        + panel.loc[interim, "prior_annual_profit"]
        - panel.loc[interim, "prior_same_profit"]
    )
    panel["net_profit"] = ttm
    panel["revenue"] = panel["revenue_cum"]
    dates = panel[["available_date", "prior_annual_date", "prior_same_date"]].max(axis=1)
    panel["available_date"] = dates
    panel["report_year"] = panel["period"].dt.year
    panel["quarter"] = panel["period"].dt.quarter
    panel = panel.dropna(subset=["net_profit", "available_date"])
    panel["source"] = panel["source"].fillna("ttm")
    return panel[TTM_COLUMNS].sort_values(["symbol", "period"]).reset_index(drop=True)


def coverage(panel: pd.DataFrame) -> dict[str, Any]:
    if panel.empty:
        return {"rows": 0, "symbols": 0}
    return {
        "rows": len(panel),
        "symbols": int(panel["symbol"].nunique()),
        "current_symbols": int(
            panel.loc[panel["listed_status"] == "current", "symbol"].nunique()
        ),
        "delisted_symbols": int(
            panel.loc[panel["listed_status"] == "delisted", "symbol"].nunique()
        ),
        "periods": int(panel["period"].nunique()),
        "min_period": str(panel["period"].min().date()),
        "max_period": str(panel["period"].max().date()),
        "min_available_date": str(panel["available_date"].min().date()),
        "max_available_date": str(panel["available_date"].max().date()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--years", default="2018-2025")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--normalize-only", action="store_true")
    args = parser.parse_args()

    start, end = args.years.split("-")
    years = list(range(int(start), int(end) + 1))
    out = args.out
    interim_path = out / "financials" / "value_quarterly.csv"
    if args.normalize_only:
        if not interim_path.exists():
            raise SystemExit(f"missing {interim_path}; run without --normalize-only")
        interims = pd.read_csv(interim_path, dtype={"symbol": str})
        disclosure_path = out / "disclosure" / "disclosure_quarterly.csv"
        if disclosure_path.exists():
            disclosure = pd.read_csv(disclosure_path, dtype={"symbol": str})
            disclosure["period"] = pd.to_datetime(disclosure["period"], errors="coerce")
            disclosure["actual_disclosure"] = pd.to_datetime(
                disclosure["actual_disclosure"], errors="coerce"
            )
            interims = apply_quarterly_disclosure(interims, disclosure)
    else:
        listed = fetch_listed_quarters(out, years, args.workers, args.force)
        delisted = normalize_delisted_quarters(out)
        interims = pd.concat([frame for frame in (listed, delisted) if not frame.empty], ignore_index=True)
        if not interims.empty:
            interims = (
                interims.sort_values(["symbol", "period", "available_date"])
                .drop_duplicates(["symbol", "period"], keep="first")
            )
            disclosure = fetch_quarterly_disclosure(out, years, args.force)
            interims = apply_quarterly_disclosure(interims, disclosure)
            interims.to_csv(interim_path, index=False, encoding="utf-8-sig")
    annual = _annual_quarters(out)
    panel = build_ttm(interims, annual)
    if panel.empty:
        raise SystemExit("no TTM rows produced")
    path = out / "financials" / "value_ttm.csv"
    panel.to_csv(path, index=False, encoding="utf-8-sig")
    summary = {
        "generated_at": _now(),
        "path": str(path),
        "coverage": coverage(panel),
        "definition": "TTM parent net profit = current YTD + prior annual - prior-year same YTD; Q4 = annual.",
        "notes": [
            "Current-listed interim dates use CNInfo's actual first-disclosure table; rows without a record are dropped.",
            "Delisted interim dates come from the profit sheet's NOTICE_DATE.",
            "Annual Q4 rows reuse value_annual.csv and its actual-disclosure dates.",
            "A TTM row is available only after the latest component announcement date.",
        ],
    }
    (out / "financials" / "value_ttm_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
