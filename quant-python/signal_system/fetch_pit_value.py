"""Fetch full-A annual earnings for a point-in-time value factor.

Current-listed names come from Eastmoney's annual performance report
(``stock_yjbb_em``), which returns every stock for one report date in a few
requests.  Delisted names are absent from that endpoint, so they are fetched
one by one from ``stock_profit_sheet_by_report_delisted_em``; that statement
also carries ``NOTICE_DATE``, which is what makes the panel PIT-safe.

Outputs (under ``cache/stock_pit`` by default):

* ``financials/yjbb/{year}.csv``: raw current-listed annual report rows;
* ``financials/profit_delisted/{symbol}.csv``: raw delisted profit sheets;
* ``financials/value_listed_annual.csv``: normalized current-listed panel;
* ``financials/value_delisted_annual.csv``: normalized delisted panel;
* ``financials/value_annual.csv``: combined panel used by the study.

The panel deliberately contains annual reports only.  A later TTM version can
be built from quarterly statements without changing this file's schema.

Usage
-----
    python fetch_pit_value.py --years 2018-2025
    python fetch_pit_value.py --skip-listed --workers 4
    python fetch_pit_value.py --normalize-only
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

from stock_pit_data import _date, _em_symbol, _save, _symbol


DEFAULT_OUT = Path("cache/stock_pit")
LISTED_COLUMNS = {
    "股票代码": "symbol",
    "净利润-净利润": "net_profit",
    "营业总收入-营业总收入": "revenue",
    "每股收益": "eps",
    "净资产收益率": "roe",
    "最新公告日期": "notice_date",
}
VALUE_COLUMNS = [
    "symbol",
    "report_year",
    "net_profit",
    "revenue",
    "eps",
    "roe",
    "available_date",
    "pit_available",
    "listed_status",
    "source",
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"symbol": str, "股票代码": str})


def normalize_listed(raw: pd.DataFrame, year: int) -> pd.DataFrame:
    """Normalize one ``stock_yjbb_em`` annual response."""

    frame = raw.rename(columns=LISTED_COLUMNS).copy()
    frame["symbol"] = frame["symbol"].map(_symbol)
    frame["report_year"] = int(year)
    for column in ("net_profit", "revenue", "eps", "roe"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        else:
            frame[column] = float("nan")
    if "notice_date" in frame.columns:
        frame["available_date"] = frame["notice_date"].map(_date)
    else:
        frame["available_date"] = pd.NaT
    frame = frame.dropna(subset=["symbol"])
    # First disclosed annual row wins; a later revision is not PIT knowledge.
    frame = frame.sort_values(["symbol", "available_date"])
    frame = frame.drop_duplicates(["symbol", "report_year"], keep="first")
    frame["listed_status"] = "current"
    frame["source"] = "stock_yjbb_em"
    frame["pit_available"] = (
        frame["available_date"].notna() & frame["net_profit"].notna()
    )
    return frame[VALUE_COLUMNS]


def normalize_delisted(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize one delisted profit sheet to annual PIT rows."""

    if raw is None or raw.empty:
        return pd.DataFrame(columns=VALUE_COLUMNS)
    frame = raw.copy()
    if "REPORT_DATE" not in frame.columns:
        return pd.DataFrame(columns=VALUE_COLUMNS)
    frame["report_date"] = pd.to_datetime(frame["REPORT_DATE"], errors="coerce")
    frame = frame[
        frame["report_date"].dt.month.eq(12)
        & frame["report_date"].dt.day.eq(31)
    ].copy()
    if frame.empty:
        return pd.DataFrame(columns=VALUE_COLUMNS)
    if "SECURITY_CODE" not in frame.columns:
        return pd.DataFrame(columns=VALUE_COLUMNS)
    frame["symbol"] = frame["SECURITY_CODE"].map(_symbol)
    frame["report_year"] = frame["report_date"].dt.year.astype("Int64")
    frame["net_profit"] = pd.to_numeric(frame.get("PARENT_NETPROFIT"), errors="coerce")
    frame["revenue"] = pd.to_numeric(frame.get("TOTAL_OPERATE_INCOME"), errors="coerce")
    frame["eps"] = pd.to_numeric(frame.get("BASIC_EPS"), errors="coerce")
    frame["roe"] = float("nan")
    frame["available_date"] = pd.to_datetime(frame.get("NOTICE_DATE"), errors="coerce")
    frame = frame.dropna(subset=["symbol", "report_year"])
    frame = frame.sort_values(["symbol", "report_year", "available_date"])
    frame = frame.drop_duplicates(["symbol", "report_year"], keep="first")
    frame["report_year"] = frame["report_year"].astype(int)
    frame["listed_status"] = "delisted"
    frame["source"] = "stock_profit_sheet_by_report_delisted_em"
    frame["pit_available"] = (
        frame["available_date"].notna() & frame["net_profit"].notna()
    )
    return frame[VALUE_COLUMNS]


def _apply_disclosure_dates(panel: pd.DataFrame, out: Path) -> pd.DataFrame:
    """Prefer the first actual annual-report disclosure date for current names."""

    path = out / "disclosure" / "disclosure_annual.csv"
    if panel.empty or not path.exists():
        return panel
    disclosure = pd.read_csv(path, dtype={"symbol": str})
    if disclosure.empty:
        return panel
    disclosure["symbol"] = disclosure["symbol"].map(_symbol)
    disclosure["report_year"] = pd.to_numeric(disclosure["report_year"], errors="coerce")
    disclosure["actual_disclosure"] = pd.to_datetime(
        disclosure["actual_disclosure"], errors="coerce"
    )
    disclosure = (
        disclosure.dropna(subset=["actual_disclosure"])
        .sort_values(["symbol", "report_year", "actual_disclosure"])
        .drop_duplicates(["symbol", "report_year"], keep="first")
        [["symbol", "report_year", "actual_disclosure"]]
    )
    merged = panel.merge(disclosure, on=["symbol", "report_year"], how="left")
    # The disclosure table is the exchange-published first actual date.  Keep
    # the endpoint date only when the disclosure table does not cover a name.
    merged["available_date"] = merged["actual_disclosure"].fillna(merged["available_date"])
    merged = merged.drop(columns=["actual_disclosure"])
    merged["pit_available"] = (
        merged["available_date"].notna() & merged["net_profit"].notna()
    )
    return merged


def _fetch_yjbb_year(year: int, attempts: int = 4) -> pd.DataFrame:
    import akshare as ak

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return ak.stock_yjbb_em(date=f"{year}1231")
        except Exception as exc:  # noqa: BLE001 - Eastmoney truncates page streams
            last = exc
            time.sleep(1.5 + attempt * 2)
    raise RuntimeError(f"yjbb {year} failed after {attempts} attempts: {last}")


def fetch_listed(out: Path, years: list[int], force: bool) -> pd.DataFrame:
    raw_dir = out / "financials" / "yjbb"
    raw_dir.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    failures: list[dict[str, str]] = []
    for year in years:
        path = raw_dir / f"{year}.csv"
        if path.exists() and path.stat().st_size > 100 and not force:
            raw = pd.read_csv(path, dtype={"股票代码": str})
        else:
            print(f"[listed] fetch {year}", flush=True)
            try:
                raw = _fetch_yjbb_year(year)
            except Exception as exc:  # noqa: BLE001 - keep other years moving
                failures.append({"year": str(year), "error": str(exc)})
                print(f"[listed] {year}: FAILED {exc}", flush=True)
                continue
            if raw is None or raw.empty:
                failures.append({"year": str(year), "error": "empty result"})
                print(f"[listed] {year}: empty", flush=True)
                continue
            _save(raw, path, "stock_yjbb_em")
        frames.append(normalize_listed(raw, year))
        print(f"[listed] {year}: rows={len(raw)}", flush=True)
        time.sleep(0.2)
    if failures:
        (out / "financials" / "value_listed_errors.json").write_text(
            json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if not frames:
        return pd.DataFrame(columns=VALUE_COLUMNS)
    return _apply_disclosure_dates(
        pd.concat(frames, ignore_index=True), out
    ).drop_duplicates(["symbol", "report_year"], keep="first")


def _delisted_symbols(out: Path) -> list[str]:
    symbols: set[str] = set()
    price_dir = out / "prices_delisted"
    if price_dir.exists():
        symbols.update(
            path.stem for path in price_dir.glob("*.csv") if path.name != "prices_index.csv"
        )
    path = out / "universe" / "delisted.csv"
    if path.exists():
        frame = pd.read_csv(path, dtype={"symbol": str})
        frame["delist_date"] = pd.to_datetime(frame["delist_date"], errors="coerce")
        frame = frame[frame["delist_date"] >= pd.Timestamp("2019-01-01")]
        symbols.update(_symbol(value) for value in frame["symbol"])
    return sorted(symbols)


def _fetch_one_delisted(symbol: str, raw_dir: Path, force: bool) -> pd.DataFrame:
    import akshare as ak

    path = raw_dir / f"{symbol}.csv"
    if path.exists() and path.stat().st_size > 100 and not force:
        raw = pd.read_csv(path, dtype={"SECURITY_CODE": str})
    else:
        last: Exception | None = None
        raw = None
        for attempt in range(4):
            try:
                raw = ak.stock_profit_sheet_by_report_delisted_em(
                    symbol=_em_symbol(symbol)
                )
                break
            except Exception as exc:  # noqa: BLE001 - transient endpoint failures
                last = exc
                time.sleep(0.8 + attempt)
        if raw is None or raw.empty:
            raise RuntimeError(
                f"delisted profit fetch failed for {symbol}: {last or 'empty'}"
            )
        raw.to_csv(path, index=False, encoding="utf-8-sig")
    return normalize_delisted(raw)


def fetch_delisted(out: Path, workers: int, force: bool) -> pd.DataFrame:
    raw_dir = out / "financials" / "profit_delisted"
    raw_dir.mkdir(parents=True, exist_ok=True)
    symbols = _delisted_symbols(out)
    if not symbols:
        print("[delisted] no symbols", flush=True)
        return pd.DataFrame(columns=VALUE_COLUMNS)
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_fetch_one_delisted, symbol, raw_dir, force): symbol
            for symbol in symbols
        }
        for index, future in enumerate(as_completed(futures), 1):
            symbol = futures[future]
            try:
                frame = future.result()
                if not frame.empty:
                    frames.append(frame)
            except Exception as exc:  # noqa: BLE001 - record and continue
                failures.append(f"{symbol}: {exc}")
            if index % 25 == 0 or index == len(symbols):
                print(
                    f"[delisted] {index}/{len(symbols)} ok={len(frames)} "
                    f"fail={len(failures)}",
                    flush=True,
                )
    if failures:
        (out / "financials" / "value_delisted_errors.json").write_text(
            json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if not frames:
        return pd.DataFrame(columns=VALUE_COLUMNS)
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        ["symbol", "report_year"], keep="first"
    )


def build_panel(out: Path, listed: pd.DataFrame, delisted: pd.DataFrame) -> pd.DataFrame:
    frames = [frame for frame in (listed, delisted) if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=VALUE_COLUMNS)
    panel = pd.concat(frames, ignore_index=True)
    panel["symbol"] = panel["symbol"].map(_symbol)
    panel["report_year"] = pd.to_numeric(panel["report_year"], errors="coerce")
    panel["available_date"] = pd.to_datetime(panel["available_date"], errors="coerce")
    panel = panel.dropna(subset=["symbol", "report_year", "net_profit"])
    panel["report_year"] = panel["report_year"].astype(int)
    panel = panel.sort_values(["symbol", "report_year", "available_date"])
    panel = panel.drop_duplicates(["symbol", "report_year"], keep="first")
    panel = panel[panel["available_date"].notna()].copy()
    panel["pit_available"] = True
    return panel[VALUE_COLUMNS].sort_values(["symbol", "report_year"])


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
        "years": {
            str(int(year)): int(count)
            for year, count in panel["report_year"].value_counts().sort_index().items()
        },
        "available_date_min": str(panel["available_date"].min().date()),
        "available_date_max": str(panel["available_date"].max().date()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--years", default="2018-2025")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-listed", action="store_true")
    parser.add_argument("--skip-delisted", action="store_true")
    parser.add_argument("--normalize-only", action="store_true")
    args = parser.parse_args()

    start, end = args.years.split("-")
    years = list(range(int(start), int(end) + 1))
    out = args.out
    (out / "financials").mkdir(parents=True, exist_ok=True)

    listed_path = out / "financials" / "value_listed_annual.csv"
    delisted_path = out / "financials" / "value_delisted_annual.csv"

    if args.normalize_only:
        listed = pd.read_csv(listed_path, dtype={"symbol": str}) if listed_path.exists() else pd.DataFrame(columns=VALUE_COLUMNS)
        delisted = pd.read_csv(delisted_path, dtype={"symbol": str}) if delisted_path.exists() else pd.DataFrame(columns=VALUE_COLUMNS)
    else:
        listed = (
            pd.DataFrame(columns=VALUE_COLUMNS)
            if args.skip_listed
            else fetch_listed(out, years, args.force)
        )
        delisted = (
            pd.DataFrame(columns=VALUE_COLUMNS)
            if args.skip_delisted
            else fetch_delisted(out, args.workers, args.force)
        )
        if not listed.empty:
            listed.to_csv(listed_path, index=False, encoding="utf-8-sig")
        if not delisted.empty:
            delisted.to_csv(delisted_path, index=False, encoding="utf-8-sig")

    panel = build_panel(out, listed, delisted)
    if panel.empty:
        raise SystemExit("no PIT value rows produced")
    path = out / "financials" / "value_annual.csv"
    panel.to_csv(path, index=False, encoding="utf-8-sig")
    summary = {
        "generated_at": _now(),
        "path": str(path),
        "coverage": coverage(panel),
        "notes": [
            "Annual reports only; EP uses parent net profit divided by float market cap.",
            "Current-listed dates prefer disclosure_annual.csv actual disclosure dates.",
            "Delisted dates come from the report's NOTICE_DATE.",
            "Rows without an announcement date are dropped; no statutory-deadline fallback.",
        ],
    }
    (out / "financials" / "value_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
