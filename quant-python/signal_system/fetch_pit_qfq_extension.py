"""Rebuild the 2025+ price extension with dividend/split-adjusted prices.

``cache/stock_pit/prices/*.csv`` are unadjusted Sina bars starting 2025-01-02.
Scaling each symbol once at the 2024-12-31 splice turns every later
ex-dividend or split day into a real loss (BYD's 2025-07-29 split reads as
-67%) and zeroes the return of the first 2025 session.

This script keeps the Sina rows as the master, prepends the last unadjusted
2024 bar as an overlap anchor, and multiplies open/high/low/close by Sina's own
cumulative adjustment factor, normalised to the latest factor:

    adjusted = raw * hfq_factor(date) / hfq_factor(last bar)

The factor is multiplicative, so returns are exact total returns.  Tencent's
qfq series is not used because it subtracts cash dividends from history, which
distorts percentage returns.  Amount and turnover are left untouched, so the
PIT float-cap proxy ``amount / turnover`` is unchanged.

``extend_panels`` aligns on the anchor bar, so the result does not depend on
when either side was downloaded: a dividend paid after the baostock snapshot
shifts the whole adjusted series, and the anchor ratio takes it out again.

The raw directory is never modified.  Pass the output through
``--extension-dir``.

Usage
-----
    python fetch_pit_qfq_extension.py --workers 4
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent / "cache" / "stock_pit"
PRICE_COLUMNS = ["open", "high", "low", "close"]
ANCHOR_WINDOW = ("20241201", "20241231")


def sina_code(symbol: str) -> str:
    if symbol.startswith(("6", "9")):
        return f"sh{symbol}"
    if symbol.startswith(("0", "2", "3")):
        return f"sz{symbol}"
    raise ValueError(f"unsupported exchange for {symbol}")


def _with_retries(symbol: str, call: Callable[[], Any], *, retries: int = 3, pause: float = 0.1) -> Any:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            result = call()
            time.sleep(pause)
            return result
        except Exception as exc:  # network, block page, malformed payload
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}")


def fetch_factor(symbol: str) -> pd.Series:
    """Sina cumulative adjustment factor, indexed by its effective date."""

    import akshare as ak

    def call() -> pd.Series:
        frame = ak.stock_zh_a_daily(symbol=sina_code(symbol), adjust="hfq-factor")
        if frame is None or frame.empty:
            raise ValueError("empty factor table")
        return pd.Series(
            pd.to_numeric(frame["hfq_factor"], errors="coerce").to_numpy(dtype=float),
            index=pd.to_datetime(frame["date"]),
            name="hfq_factor",
        ).dropna().sort_index()

    return _with_retries(symbol, call)


def fetch_anchor(symbol: str) -> pd.DataFrame:
    """Last unadjusted Sina bar of 2024; empty for names listed or resumed later."""

    import akshare as ak

    def call() -> pd.DataFrame:
        frame = ak.stock_zh_a_daily(
            symbol=sina_code(symbol), start_date=ANCHOR_WINDOW[0], end_date=ANCHOR_WINDOW[1], adjust=""
        )
        if frame is None or frame.empty:
            return pd.DataFrame()
        frame = frame.assign(date=pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d"))
        return frame.sort_values("date").tail(1)

    return _with_retries(symbol, call)


def with_anchor(raw: pd.DataFrame, anchor: pd.DataFrame) -> pd.DataFrame:
    """Prepend the anchor bar, keeping the source schema and one row per date."""

    if anchor.empty:
        return raw
    anchor = anchor.reindex(columns=raw.columns)
    anchor = anchor[~anchor["date"].isin(raw["date"])]
    return pd.concat([anchor, raw], ignore_index=True).sort_values("date").reset_index(drop=True)


def adjust_frame(raw: pd.DataFrame, factor: pd.Series) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Scale OHLC by the factor in force on each bar, normalised to the last bar.

    ``factor`` is a step function: each entry applies from its date until the
    next entry.  Bars before the first entry use the first known factor.
    """

    if factor.empty:
        raise ValueError("empty factor series")
    frame = raw.copy()
    dates = pd.DatetimeIndex(pd.to_datetime(frame["date"]))
    factor = factor[factor > 0].sort_index()
    position = factor.index.searchsorted(dates, side="right") - 1
    in_force = factor.to_numpy(dtype=float)[np.clip(position, 0, len(factor) - 1)]
    scale = in_force / in_force[-1]
    for column in PRICE_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float) * scale
    return frame, {
        "rows": int(len(frame)),
        "corporate_actions": int((np.diff(in_force) != 0).sum()),
        "scale_first": round(float(scale[0]), 6),
    }


def process(symbol: str, source: Path, target: Path) -> dict[str, Any]:
    raw = pd.read_csv(source / f"{symbol}.csv")
    try:
        anchor = fetch_anchor(symbol)
        adjusted, stats = adjust_frame(with_anchor(raw, anchor), fetch_factor(symbol))
    except Exception as exc:
        return {"symbol": symbol, "status": "failed", "error": str(exc)[:200]}
    adjusted.to_csv(target / f"{symbol}.csv", index=False)
    return {"symbol": symbol, "status": "ok", "anchored": not anchor.empty, **stats}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--source", default=str(ROOT / "prices"))
    parser.add_argument("--out", default=str(ROOT / "prices_qfq"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    source, target = Path(args.source), Path(args.out)
    target.mkdir(parents=True, exist_ok=True)
    symbols = sorted(
        path.stem for path in source.glob("*.csv") if path.stem.isdigit() and len(path.stem) == 6
    )
    if args.limit:
        symbols = symbols[: args.limit]
    pending = [symbol for symbol in symbols if not (target / f"{symbol}.csv").exists()]
    print(f"symbols={len(symbols)} pending={len(pending)}", flush=True)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for index, row in enumerate(pool.map(lambda s: process(s, source, target), pending), 1):
            results.append(row)
            if index % 500 == 0:
                failed = sum(1 for item in results if item["status"] != "ok")
                print(f"{index}/{len(pending)} failed={failed}", flush=True)

    ok = [row for row in results if row["status"] == "ok"]
    failed = [row for row in results if row["status"] != "ok"]
    written = sum(1 for symbol in symbols if (target / f"{symbol}.csv").exists())
    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": str(source),
        "adjust_source": "sina hfq_factor (akshare stock_zh_a_daily, adjust='hfq-factor')",
        "formula": "raw * hfq_factor(date) / hfq_factor(last bar)",
        "anchor": f"last unadjusted Sina bar in {ANCHOR_WINDOW[0]}-{ANCHOR_WINDOW[1]}",
        "symbols": len(symbols),
        "written_total": written,
        "written_this_run": len(ok),
        "anchored_this_run": sum(1 for row in ok if row["anchored"]),
        "failed": [{"symbol": row["symbol"], "error": row["error"]} for row in failed],
        "symbols_with_corporate_action_this_run": sum(1 for row in ok if row["corporate_actions"] > 0),
        "corporate_actions_this_run": sum(row["corporate_actions"] for row in ok),
    }
    (target / "qfq_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in manifest.items() if k != "failed"}, ensure_ascii=False))
    print("failed", len(failed), [row["symbol"] for row in failed[:20]])


if __name__ == "__main__":
    main()
