"""Fetch qfq histories for the point-in-time ETF universe.

Reads ``cache/etf_pit/universe.csv`` produced by ``fetch_etf_pit_universe.py``
and downloads daily forward-adjusted bars for domestic equity ETFs.  Bond,
money-market, commodity and cross-border funds are excluded by name because
the production 40/35 sleeve only selects domestic equity themes.

Usage
-----
python fetch_etf_pit_prices.py --universe cache/etf_pit/universe.csv \
    --out cache/etf_pit/prices --workers 4
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd

COLUMNS = {"日期": "datetime", "开盘": "open", "最高": "high", "最低": "low",
           "收盘": "close", "成交量": "volume", "成交额": "amount"}

EXCLUDE_KEYWORDS = (
    "债", "国债", "政金", "信用", "短融", "转债", "货币", "现金", "快线", "保证金",
    "黄金", "白银", "有色", "原油", "商品", "豆粕", "能源化工",
    "纳指", "纳斯达克", "标普", "恒生", "港股", "H股", "AH", "德国", "法国", "日经",
    "美国", "海外", "中概", "跨境", "QDII", "REIT",
)
ANCHORS = ("510300", "518880", "511260", "511880", "513100")


def is_domestic_equity(name: str) -> bool:
    """False for funds the 40/35 sleeve can never select."""
    text = str(name)
    return not any(keyword in text for keyword in EXCLUDE_KEYWORDS)


def normalise(raw: pd.DataFrame) -> pd.DataFrame:
    frame = raw.rename(columns=COLUMNS)[list(COLUMNS.values())].copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    for column in frame.columns.drop("datetime"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    frame = frame.dropna(subset=["close"]).drop_duplicates("datetime", keep="last")
    frame = frame.sort_values("datetime").reset_index(drop=True)
    frame["is_closed"] = True
    frame.attrs.update(source="akshare_em_qfq", adjust="qfq", timeframe="1d")
    return frame


def normalise_sina(raw: pd.DataFrame) -> pd.DataFrame:
    """Sina returns raw prices and share volume; convert volume to 100-share lots."""
    frame = pd.DataFrame({
        "datetime": pd.to_datetime(raw["date"]),
        "open": pd.to_numeric(raw["open"], errors="coerce"),
        "high": pd.to_numeric(raw["high"], errors="coerce"),
        "low": pd.to_numeric(raw["low"], errors="coerce"),
        "close": pd.to_numeric(raw["close"], errors="coerce"),
        "volume": pd.to_numeric(raw["volume"], errors="coerce") / 100.0,
        "amount": pd.to_numeric(raw.get("amount"), errors="coerce"),
    })
    frame = frame.dropna(subset=["close"]).drop_duplicates("datetime", keep="last")
    frame = frame.sort_values("datetime").reset_index(drop=True)
    frame["is_closed"] = True
    frame.attrs.update(source="akshare_sina", adjust="none", timeframe="1d")
    return frame


def fetch_one(code: str, start: str, end: str, retries: int = 3) -> pd.DataFrame:
    import akshare as ak

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            raw = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start,
                                      end_date=end, adjust="qfq")
            if raw is None or raw.empty:
                raise ValueError("empty history")
            return normalise(raw)
        except Exception as error:  # network, block page, delisted symbol
            last_error = error
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{code}: {last_error}")


def fetch_one_sina(code: str, start: str, end: str, retries: int = 3) -> pd.DataFrame:
    import akshare as ak

    symbol = f"sh{code}" if code.startswith("5") else f"sz{code}"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            raw = ak.fund_etf_hist_sina(symbol=symbol)
            if raw is None or raw.empty:
                raise ValueError("empty history")
            frame = normalise_sina(raw)
            frame = frame[
                (frame["datetime"] >= pd.Timestamp(start)) & (frame["datetime"] <= pd.Timestamp(end))
            ].reset_index(drop=True)
            if frame.empty:
                raise ValueError("empty history in requested range")
            return frame
        except Exception as error:
            last_error = error
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{code}: {last_error}")


def _fetch_job(code: str, out: Path, start: str, end: str, source: str) -> dict[str, Any]:
    frame = fetch_one(code, start, end) if source == "em" else fetch_one_sina(code, start, end)
    frame.to_pickle(out / f"{code}_qfq.pkl")
    return {
        "code": code,
        "rows": int(len(frame)),
        "first": str(frame["datetime"].iloc[0].date()),
        "last": str(frame["datetime"].iloc[-1].date()),
        "source": source,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=Path("cache/etf_pit/universe.csv"))
    parser.add_argument("--out", type=Path, default=Path("cache/etf_pit/prices"))
    parser.add_argument("--existing-dir", type=Path, default=Path("cache/etf_research_long"),
                        help="skip codes already cached here")
    parser.add_argument("--start", default="20100101")
    parser.add_argument("--end", default="20261030")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--source", choices=("em", "sina"), default="em")
    parser.add_argument("--codes", default=None,
                        help="comma-separated codes; bypasses the universe filter")
    args = parser.parse_args()

    universe = pd.read_csv(args.universe, dtype={"code": str})
    universe["code"] = universe["code"].str.zfill(6)
    anchors = universe[universe["code"].isin(ANCHORS)]
    if args.codes:
        wanted = {code.strip().zfill(6) for code in args.codes.split(",") if code.strip()}
        candidates = universe[universe["code"].isin(wanted)].copy()
    else:
        candidates = pd.concat(
            [universe[universe["name"].map(is_domestic_equity)], anchors],
            ignore_index=True,
        ).drop_duplicates("code")
    skipped = 0
    if args.existing_dir.exists() and not args.limit and not args.codes:
        existing = {
            path.name.removesuffix("_qfq.pkl")
            for path in args.existing_dir.glob("*_qfq.pkl")
        }
        before = len(candidates)
        candidates = candidates[~candidates["code"].isin(existing)]
        skipped = before - len(candidates)
    if args.limit:
        candidates = candidates.head(args.limit)
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"universe={len(universe)} candidates={len(candidates)} skipped_existing={skipped}", flush=True)

    report: dict[str, Any] = {}
    failed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(_fetch_job, row.code, args.out, args.start, args.end, args.source): row.code
            for row in candidates.itertuples()
        }
        for done, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                report[code] = future.result()
            except Exception as error:
                failed[code] = str(error)
            if done % 50 == 0:
                print(f"{done}/{len(futures)} fetched, failures={len(failed)}", flush=True)

    payload = {
        "universe": str(args.universe),
        "start": args.start,
        "end": args.end,
        "candidates": len(candidates),
        "written": len(report),
        "failed": len(failed),
        "prices": dict(sorted(report.items())),
        "errors": dict(sorted(failed.items())),
    }
    (args.out.parent / "price_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"written={len(report)} failed={len(failed)}", flush=True)
    return 0 if report else 1


if __name__ == "__main__":
    raise SystemExit(main())
