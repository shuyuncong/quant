"""Build a point-in-time (PIT) stock data layer from free AkShare endpoints.

The builder deliberately separates what is available from what is missing:

* available: current CSI 1000 list, delisted lists, annual-report disclosure
  dates, annual earnings forecasts, pledge ratios by date, margin detail by
  date, and per-symbol financial statements (with NOTICE_DATE);
* not available here: historical CSI 1000 membership, audit-opinion text,
  regulatory-investigation records, historical minute/tick/order-book data.

Every output file carries a ``source`` and ``fetched_at`` column.  Raw
per-period files are kept so that a later run can be audited or re-normalized
without re-downloading.

Usage
-----
    python stock_pit_data.py --action fast
    python stock_pit_data.py --action financials --symbols 000001,600000 --limit 10
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd


DEFAULT_OUT = Path("cache/stock_pit")
YEAR_RANGE = range(2019, 2025)
SOURCE = "akshare"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _symbol(value: Any) -> str:
    text = str(value).strip()
    for prefix in ("SH", "SZ", "BJ", "sh", "sz", "bj"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.split(".")[0]
    return text.zfill(6)


def _date(value: Any) -> pd.Timestamp | pd.NaT:
    return pd.to_datetime(value, errors="coerce")


def _save(df: pd.DataFrame, path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["source"] = source
    out["fetched_at"] = _now()
    out.to_csv(path, index=False, encoding="utf-8-sig")


def _safe(name: str, fn: Callable[[], pd.DataFrame], errors: list[dict[str, str]]) -> pd.DataFrame | None:
    try:
        df = fn()
        if df is None or df.empty:
            errors.append({"step": name, "error": "empty result"})
            return None
        return df
    except Exception as exc:  # network/API failures are expected; record and continue
        errors.append({"step": name, "error": f"{type(exc).__name__}: {exc}"})
        return None


def fetch_universe(out: Path, errors: list[dict[str, str]]) -> None:
    index = _safe(
        "csi1000_current",
        lambda: __import__("akshare").index_stock_cons_csindex(symbol="000852"),
        errors,
    )
    if index is not None:
        index = index.rename(
            columns={
                "成分券代码": "symbol",
                "成分券名称": "name",
                "日期": "snapshot_date",
            }
        )
        index["symbol"] = index["symbol"].map(_symbol)
        index["snapshot_date"] = index["snapshot_date"].map(_date)
        index = index[["symbol", "name", "snapshot_date"]].drop_duplicates("symbol")
        _save(index, out / "universe" / "csi1000_current.csv", "index_stock_cons_csindex")

    sh = _safe("sh_delist", lambda: __import__("akshare").stock_info_sh_delist(), errors)
    sz = _safe("sz_delist", lambda: __import__("akshare").stock_info_sz_delist(), errors)
    frames: list[pd.DataFrame] = []
    if sh is not None:
        sh = sh.rename(columns={"公司代码": "symbol", "公司简称": "name", "上市日期": "list_date", "暂停上市日期": "delist_date"})
        sh["exchange"] = "SSE"
        frames.append(sh[["symbol", "name", "list_date", "delist_date", "exchange"]])
    if sz is not None:
        sz = sz.rename(columns={"证券代码": "symbol", "证券简称": "name", "上市日期": "list_date", "终止上市日期": "delist_date"})
        sz["exchange"] = "SZSE"
        frames.append(sz[["symbol", "name", "list_date", "delist_date", "exchange"]])
    if frames:
        delisted = pd.concat(frames, ignore_index=True)
        delisted["symbol"] = delisted["symbol"].map(_symbol)
        delisted["list_date"] = delisted["list_date"].map(_date)
        delisted["delist_date"] = delisted["delist_date"].map(_date)
        delisted = delisted.drop_duplicates("symbol").sort_values("symbol")
        _save(delisted, out / "universe" / "delisted.csv", "stock_info_sh_delist+sz_delist")

    universe = []
    if index is not None:
        current = index.copy()
        current["status"] = "current"
        current["in_csi1000"] = True
        universe.append(current)
    if frames:
        old = delisted.copy()
        old["status"] = "delisted"
        old["in_csi1000"] = False
        universe.append(old)
    if universe:
        merged = pd.concat(universe, ignore_index=True).drop_duplicates("symbol", keep="first")
        _save(merged, out / "universe" / "universe.csv", "csi1000_current+delisted")


def fetch_disclosure(out: Path, years: list[int], errors: list[dict[str, str]]) -> None:
    import akshare as ak

    frames: list[pd.DataFrame] = []
    for year in years:
        period = f"{year}年报"
        raw = _safe(f"disclosure_{period}", lambda p=period: ak.stock_report_disclosure(market="沪深京", period=p), errors)
        if raw is None:
            continue
        raw["report_year"] = year
        _save(raw, out / "disclosure" / f"annual_{year}.csv", "stock_report_disclosure")
        normalized = raw.rename(
            columns={
                "股票代码": "symbol",
                "股票简称": "name",
                "首次预约": "first_appointment",
                "实际披露": "actual_disclosure",
            }
        )
        keep = [c for c in ["symbol", "name", "first_appointment", "actual_disclosure", "report_year"] if c in normalized.columns]
        normalized = normalized[keep].copy()
        normalized["symbol"] = normalized["symbol"].map(_symbol)
        for column in ("first_appointment", "actual_disclosure"):
            if column in normalized.columns:
                normalized[column] = normalized[column].map(_date)
        frames.append(normalized)
    if frames:
        combined = pd.concat(frames, ignore_index=True).drop_duplicates(["symbol", "report_year"], keep="last")
        _save(combined, out / "disclosure" / "disclosure_annual.csv", "stock_report_disclosure")


def fetch_forecast(out: Path, years: list[int], errors: list[dict[str, str]]) -> None:
    import akshare as ak

    frames: list[pd.DataFrame] = []
    for year in years:
        raw = _safe(
            f"yjyg_{year}",
            lambda y=year: ak.stock_yjyg_em(date=f"{y}1231"),
            errors,
        )
        if raw is None:
            continue
        raw["report_year"] = year
        _save(raw, out / "forecast" / f"yjyg_{year}.csv", "stock_yjyg_em")
        normalized = raw.rename(
            columns={
                "股票代码": "symbol",
                "股票简称": "name",
                "预测指标": "indicator",
                "业绩变动": "change_text",
                "预告类型": "forecast_type",
                "公告日期": "announcement_date",
            }
        )
        keep = [c for c in ["symbol", "name", "indicator", "change_text", "forecast_type", "announcement_date", "report_year"] if c in normalized.columns]
        normalized = normalized[keep].copy()
        normalized["symbol"] = normalized["symbol"].map(_symbol)
        if "announcement_date" in normalized.columns:
            normalized["announcement_date"] = normalized["announcement_date"].map(_date)
        frames.append(normalized)
    if frames:
        combined = pd.concat(frames, ignore_index=True).drop_duplicates(
            ["symbol", "report_year", "indicator", "announcement_date"], keep="last"
        )
        _save(combined, out / "forecast" / "forecasts_annual.csv", "stock_yjyg_em")


def _quarter_ends(years: list[int]) -> list[str]:
    dates: list[str] = []
    for year in years:
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            stamp = pd.Timestamp(year=year, month=month, day=day)
            while stamp.weekday() >= 5:
                stamp -= pd.Timedelta(days=1)
            dates.append(stamp.strftime("%Y-%m-%d"))
    return dates


def fetch_pledge(out: Path, years: list[int], errors: list[dict[str, str]]) -> None:
    import akshare as ak

    frames: list[pd.DataFrame] = []
    for date in _quarter_ends(years):
        raw = _safe(f"pledge_{date}", lambda d=date: ak.stock_gpzy_pledge_ratio_em(date=d), errors)
        if raw is None:
            continue
        raw["trade_date"] = date
        _save(raw, out / "pledge" / f"pledge_{date}.csv", "stock_gpzy_pledge_ratio_em")
        normalized = raw.rename(
            columns={
                "股票代码": "symbol",
                "股票简称": "name",
                "交易日期": "trade_date",
                "质押比例": "pledge_ratio",
                "质押股数": "pledged_shares",
            }
        )
        keep = [c for c in ["symbol", "name", "trade_date", "pledge_ratio", "pledged_shares"] if c in normalized.columns]
        normalized = normalized[keep].copy()
        normalized["symbol"] = normalized["symbol"].map(_symbol)
        frames.append(normalized)
        time.sleep(0.2)
    if frames:
        combined = pd.concat(frames, ignore_index=True).drop_duplicates(["symbol", "trade_date"], keep="last")
        _save(combined, out / "pledge" / "pledge.csv", "stock_gpzy_pledge_ratio_em")


def _margin_year_end_dates(years: list[int]) -> list[str]:
    # 2022-12-31 and 2023-12-31 are weekends; use the last weekday of the year.
    return [date for date in _quarter_ends(years) if date.startswith(tuple(f"{y}-12" for y in years))]


def _normalize_margin(frame: pd.DataFrame) -> pd.DataFrame:
    columns = {
        "标的证券代码": "symbol",
        "标的证券简称": "name",
        "证券代码": "symbol",
        "证券简称": "name",
        "信用交易日期": "raw_date",
    }
    out = frame.rename(columns=columns).copy()
    if "symbol" not in out.columns:
        return out
    out["symbol"] = out["symbol"].map(_symbol)
    common = [
        "symbol",
        "name",
        "trade_date",
        "exchange",
        "融资余额",
        "融资买入额",
        "融资偿还额",
        "融券余量",
        "融券卖出量",
        "融券偿还量",
        "融券余额",
        "融资融券余额",
    ]
    return out[[column for column in common if column in out.columns]]


def fetch_margin(out: Path, years: list[int], errors: list[dict[str, str]]) -> None:
    import akshare as ak

    frames: list[pd.DataFrame] = []
    for date in _margin_year_end_dates(years):
        for market, fn in (("SSE", ak.stock_margin_detail_sse), ("SZSE", ak.stock_margin_detail_szse)):
            raw = _safe(f"margin_{market}_{date}", lambda f=fn, d=date: f(date=d.replace("-", "")), errors)
            if raw is None:
                continue
            raw["trade_date"] = date
            raw["exchange"] = market
            _save(raw, out / "margin" / f"margin_{market}_{date}.csv", f"stock_margin_detail_{market.lower()}")
            frames.append(_normalize_margin(raw))
            time.sleep(0.2)
    if frames:
        combined = pd.concat(frames, ignore_index=True, sort=False)
        _save(combined, out / "margin" / "margin.csv", "stock_margin_detail_sse+szse")


def _sina_symbol(symbol: str) -> str:
    code = _symbol(symbol)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def fetch_sina_prices(
    out: Path,
    symbols: list[str],
    start: str,
    end: str,
    errors: list[dict[str, str]],
) -> None:
    """Fetch 2025-2026 daily bars with outstanding shares and turnover.

    Sina's daily endpoint is the only free source found here that returns
    historical turnover/outstanding shares for the extension period.
    """

    import akshare as ak

    price_dir = out / "prices"
    price_dir.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        code = _symbol(symbol)
        path = price_dir / f"{code}.csv"
        if path.exists() and path.stat().st_size > 100:
            raw = pd.read_csv(path)
        else:
            raw = _safe(
                f"prices_{code}",
                lambda c=code: ak.stock_zh_a_daily(
                    symbol=_sina_symbol(c), start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""), adjust="",
                ),
                errors,
            )
            if raw is None:
                continue
            raw.to_csv(path, index=False, encoding="utf-8-sig")
        if raw.empty:
            continue
        date_column = "date" if "date" in raw.columns else raw.columns[0]
        dates = pd.to_datetime(raw[date_column], errors="coerce")
        index_rows.append(
            {
                "symbol": code,
                "path": str(path.relative_to(out)),
                "rows": len(raw),
                "start_date": dates.min().strftime("%Y-%m-%d") if dates.notna().any() else "",
                "end_date": dates.max().strftime("%Y-%m-%d") if dates.notna().any() else "",
                "has_turnover": "turnover" in raw.columns,
                "has_outstanding_share": "outstanding_share" in raw.columns,
                "fetched_at": _now(),
                "source": "stock_zh_a_daily",
            }
        )
        time.sleep(0.05)
    if index_rows:
        _save(pd.DataFrame(index_rows), out / "prices" / "prices_index.csv", "stock_zh_a_daily")


def fetch_delisted_prices(
    out: Path,
    start: str,
    end: str,
    errors: list[dict[str, str]],
) -> None:
    """Fetch delisted A-share bars from Eastmoney with proxy env cleared.

    Eastmoney is reachable in this environment only when the corporate proxy
    variables are removed; otherwise every delisted request fails with
    ProxyError.  The list itself comes from the already-built delisted.csv.
    """

    import os

    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    import akshare as ak

    delisted_path = out / "universe" / "delisted.csv"
    if not delisted_path.exists():
        errors.append({"step": "delisted_prices", "error": "delisted.csv missing"})
        return
    delisted = pd.read_csv(delisted_path, dtype={"symbol": str})
    delisted["delist_date"] = pd.to_datetime(delisted["delist_date"], errors="coerce")
    delisted = delisted[delisted["delist_date"].notna()]
    delisted = delisted[delisted["delist_date"] >= pd.Timestamp(start)]
    price_dir = out / "prices_delisted"
    price_dir.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, Any]] = []
    for _, row in delisted.iterrows():
        code = _symbol(row["symbol"])
        path = price_dir / f"{code}.csv"
        if not code.startswith(("0", "3", "6")):
            continue
        if path.exists() and path.stat().st_size > 100:
            raw = pd.read_csv(path)
        else:
            end_date = min(pd.Timestamp(end), row["delist_date"]).strftime("%Y%m%d")
            raw = None
            last_error = None
            for attempt in range(4):
                try:
                    raw = ak.stock_zh_a_hist(
                        symbol=code, start_date=start.replace("-", ""), end_date=end_date,
                        adjust="", timeout=20,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    time.sleep(1.0 + attempt)
            if raw is None or raw.empty:
                errors.append({
                    "step": f"delisted_{code}",
                    "error": f"{type(last_error).__name__}: {last_error}" if last_error else "empty result",
                })
                continue
            raw.to_csv(path, index=False, encoding="utf-8-sig")
        if raw.empty:
            continue
        date_column = "日期" if "日期" in raw.columns else raw.columns[0]
        dates = pd.to_datetime(raw[date_column], errors="coerce")
        index_rows.append(
            {
                "symbol": code,
                "path": str(path.relative_to(out)),
                "rows": len(raw),
                "start_date": dates.min().strftime("%Y-%m-%d") if dates.notna().any() else "",
                "end_date": dates.max().strftime("%Y-%m-%d") if dates.notna().any() else "",
                "has_turnover": "换手率" in raw.columns,
                "has_amount": "成交额" in raw.columns,
                "fetched_at": _now(),
                "source": "stock_zh_a_hist",
            }
        )
        time.sleep(0.1)
    if index_rows:
        _save(pd.DataFrame(index_rows), out / "prices_delisted" / "prices_index.csv", "stock_zh_a_hist")


def fetch_financial_abstract(
    out: Path, symbols: list[str], errors: list[dict[str, str]]
) -> None:
    """Fetch the wide key-indicator table: revenue, net profit, goodwill, ROE.

    This endpoint is much faster than the three full statements (about 2–4
    seconds per symbol).  Period columns are report periods, not announcement
    dates; join annual periods to ``disclosure_annual.csv`` before using them.
    """

    import akshare as ak

    abstract_dir = out / "financials" / "abstract"
    abstract_dir.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        code = _symbol(symbol)
        raw = _safe(f"financial_abstract_{code}", lambda c=code: ak.stock_financial_abstract(symbol=c), errors)
        if raw is None:
            continue
        path = abstract_dir / f"{code}.csv"
        raw.to_csv(path, index=False, encoding="utf-8-sig")
        periods = [str(c) for c in raw.columns if str(c).isdigit() and len(str(c)) == 8]
        index_rows.append(
            {
                "symbol": code,
                "path": str(path.relative_to(out)),
                "rows": len(raw),
                "periods": len(periods),
                "start_period": min(periods) if periods else "",
                "end_period": max(periods) if periods else "",
                "fetched_at": _now(),
                "source": "stock_financial_abstract",
            }
        )
        time.sleep(0.2)
    if index_rows:
        _save(pd.DataFrame(index_rows), out / "financials" / "abstract_index.csv", "stock_financial_abstract")


ABSTRACT_INDICATORS = [
    "归母净利润",
    "营业总收入",
    "扣非净利润",
    "股东权益合计(净资产)",
    "商誉",
    "经营现金流量净额",
    "净资产收益率(ROE)",
    "总资产报酬率(ROA)",
    "资产负债率",
]


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


def normalize_financial_abstract(out: Path, errors: list[dict[str, str]]) -> None:
    """Turn the wide abstract files into a long annual panel.

    The raw period columns are report periods.  Annual rows are joined to the
    actual report-disclosure date; a row without an actual date is marked
    ``pit_available=False`` rather than being silently treated as available.
    """

    abstract_dir = out / "financials" / "abstract"
    if not abstract_dir.exists():
        return
    disclosure_path = out / "disclosure" / "disclosure_annual.csv"
    disclosure = (
        pd.read_csv(disclosure_path, dtype={"symbol": str})
        if disclosure_path.exists()
        else pd.DataFrame(columns=["symbol", "report_year", "actual_disclosure"])
    )
    if not disclosure.empty:
        disclosure["symbol"] = disclosure["symbol"].map(_symbol)
        disclosure["report_year"] = pd.to_numeric(disclosure["report_year"], errors="coerce")
        disclosure["actual_disclosure"] = disclosure["actual_disclosure"].map(_date)
        disclosure = disclosure[["symbol", "report_year", "actual_disclosure"]].drop_duplicates(
            ["symbol", "report_year"], keep="last"
        )

    rows: list[pd.DataFrame] = []
    for path in sorted(abstract_dir.glob("*.csv")):
        try:
            raw = pd.read_csv(path, dtype={"指标": str})
        except Exception as exc:
            errors.append({"step": f"normalize_{path.stem}", "error": f"{type(exc).__name__}: {exc}"})
            continue
        if "指标" not in raw.columns:
            continue
        date_columns = [str(c) for c in raw.columns if str(c).isdigit() and len(str(c)) == 8 and str(c).endswith("1231")]
        if not date_columns:
            continue
        subset = raw[raw["指标"].isin(ABSTRACT_INDICATORS)]
        for _, item in subset.iterrows():
            for period in date_columns:
                value = _number(item.get(period))
                if value is None:
                    continue
                rows.append(
                    pd.DataFrame(
                        [
                            {
                                "symbol": path.stem,
                                "indicator": item["指标"],
                                "report_period": period,
                                "report_year": int(period[:4]),
                                "value": value,
                            }
                        ]
                    )
                )
    if not rows:
        return
    panel = pd.concat(rows, ignore_index=True)
    panel["symbol"] = panel["symbol"].map(_symbol)
    panel = panel.drop_duplicates(["symbol", "indicator", "report_period"], keep="first")
    panel = panel.merge(disclosure, on=["symbol", "report_year"], how="left")
    panel["statutory_deadline"] = pd.to_datetime(
        (panel["report_year"] + 1).astype("Int64").astype(str) + "-04-30", errors="coerce"
    )
    panel["pit_available"] = panel["actual_disclosure"].notna()
    panel["available_date"] = panel["actual_disclosure"]
    _save(panel, out / "financials" / "abstract_annual.csv", "stock_financial_abstract")


def _em_symbol(symbol: str) -> str:
    code = _symbol(symbol)
    if code.startswith(("6", "9")):
        return f"SH{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SZ{code}"


def fetch_financials(out: Path, symbols: list[str], errors: list[dict[str, str]]) -> None:
    import akshare as ak

    statements = {
        "balance": ak.stock_balance_sheet_by_report_em,
        "profit": ak.stock_profit_sheet_by_report_em,
        "cashflow": ak.stock_cash_flow_sheet_by_report_em,
    }
    raw_dir = out / "financials" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        code = _symbol(symbol)
        em = _em_symbol(code)
        for name, fn in statements.items():
            raw = _safe(f"financial_{name}_{code}", lambda f=fn, s=em: f(symbol=s), errors)
            if raw is None:
                continue
            path = out / "financials" / "raw" / f"{code}_{name}.csv"
            raw.to_csv(path, index=False, encoding="utf-8-sig")
            index_rows.append(
                {
                    "symbol": code,
                    "statement": name,
                    "path": str(path.relative_to(out)),
                    "rows": len(raw),
                    "columns": len(raw.columns),
                    "has_notice_date": "NOTICE_DATE" in raw.columns,
                    "fetched_at": _now(),
                    "source": "stock_financial_report_em",
                }
            )
            time.sleep(0.2)
    if index_rows:
        _save(pd.DataFrame(index_rows), out / "financials" / "index.csv", "stock_financial_report_em")


def build_manifest(out: Path, errors: list[dict[str, str]]) -> None:
    files = sorted(path for path in out.rglob("*") if path.is_file() and path.name != "manifest.json")
    entries: list[dict[str, Any]] = []
    for path in files:
        data = path.read_bytes()
        entries.append(
            {
                "path": str(path.relative_to(out)),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "generated_at": _now(),
        "root": str(out),
        "files": entries,
        "errors": errors,
        "coverage": {
            "current_csi1000": (out / "universe" / "csi1000_current.csv").exists(),
            "delisted": (out / "universe" / "delisted.csv").exists(),
            "disclosure": (out / "disclosure" / "disclosure_annual.csv").exists(),
            "forecast": (out / "forecast" / "forecasts_annual.csv").exists(),
            "pledge": (out / "pledge" / "pledge.csv").exists(),
            "margin": (out / "margin" / "margin.csv").exists(),
            "financials": (out / "financials" / "index.csv").exists(),
            "financial_abstract": (out / "financials" / "abstract_index.csv").exists(),
            "financial_abstract_annual": (out / "financials" / "abstract_annual.csv").exists(),
            "prices_2025_2026": (out / "prices" / "prices_index.csv").exists(),
            "prices_delisted": (out / "prices_delisted" / "prices_index.csv").exists(),
        },
        "unavailable": [
            "historical PIT CSI 1000 constituents",
            "structured audit opinions (standard/unqualified vs qualified)",
            "regulatory investigation / filing records",
            "historical minute/tick/order-book data",
            "historical order-book and partial-fill data",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--action",
        choices=["fast", "universe", "disclosure", "forecast", "pledge", "margin", "financials", "financial_abstract", "normalize", "prices", "prices_delisted", "all"],
        default="fast",
    )
    parser.add_argument("--years", default="2019-2024")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--all-current", action="store_true", help="fetch every current CSI 1000 symbol")
    parser.add_argument("--all-cache", action="store_true", help="fetch every symbol in the local baostock cache")
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--end-date", default="2026-10-30")
    args = parser.parse_args()

    start, end = args.years.split("-")
    years = list(range(int(start), int(end) + 1))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, str]] = []

    if args.action in ("fast", "universe", "all"):
        fetch_universe(out, errors)
    if args.action in ("fast", "disclosure", "all"):
        fetch_disclosure(out, years, errors)
    if args.action in ("fast", "forecast", "all"):
        fetch_forecast(out, years, errors)
    if args.action in ("pledge", "all"):
        fetch_pledge(out, years, errors)
    if args.action in ("fast", "margin", "all"):
        fetch_margin(out, years, errors)
    if args.action in ("financials", "financial_abstract", "all"):
        symbols = [s for s in args.symbols.split(",") if s.strip()]
        if not symbols:
            current = out / "universe" / "csi1000_current.csv"
            if current.exists():
                codes = pd.read_csv(current, dtype={"symbol": str})["symbol"].tolist()
                symbols = codes if args.all_current else codes[: args.limit]
        if symbols and args.action == "financials":
            fetch_financials(out, symbols, errors)
        if symbols and args.action == "financial_abstract":
            fetch_financial_abstract(out, symbols, errors)
            normalize_financial_abstract(out, errors)
    if args.action in ("prices", "all"):
        symbols = [s for s in args.symbols.split(",") if s.strip()]
        if not symbols and args.all_cache:
            cache_dir = Path("cache/baostock_history_20261026")
            symbols = sorted(path.name.split("_")[0] for path in cache_dir.glob("*_qfq.pkl"))
        if not symbols:
            current = out / "universe" / "csi1000_current.csv"
            if current.exists():
                codes = pd.read_csv(current, dtype={"symbol": str})["symbol"].tolist()
                symbols = codes if args.all_current else codes[: args.limit]
        if symbols:
            fetch_sina_prices(out, symbols, args.start_date, args.end_date, errors)
    if args.action in ("prices_delisted", "all"):
        fetch_delisted_prices(out, args.start_date, args.end_date, errors)
    if args.action == "normalize":
        normalize_financial_abstract(out, errors)
    build_manifest(out, errors)
    print(f"wrote {out}; errors={len(errors)}")
    for error in errors[:10]:
        print("ERROR", error["step"], error["error"])


if __name__ == "__main__":
    main()
