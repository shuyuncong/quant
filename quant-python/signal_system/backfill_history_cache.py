"""Prepare historical daily bars without changing production strategy settings.

BaoStock uses an independent, single-source research cache: do not stitch its
share-volume/QFQ series onto legacy caches with unknown units or adjustment dates.
The original AkShare backfill remains available for existing research workflows.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

from data.providers.akshare_provider import AkshareDailyProvider
from data.providers.baostock_provider import BaostockDailyProvider
from data.symbols import normalize_ts_code
from utils.helpers import load_config

BACKFILL_START = "2018-01-01"
BACKFILL_END = "2023-05-19"
CACHE_DIR = BASE / "cache" / "daily_history"
BAR_COLUMNS = ("datetime", "open", "high", "low", "close", "volume", "amount", "is_closed")
# A successful query with zero bars is a determinate result (e.g. listing after the
# requested range); only transport failures are retried on the next run.
DONE_PREFIXES = frozenset({"added", "refreshed", "already", "no_data"})


def marks_done(result: str) -> bool:
    return result.split(":", 1)[0] in DONE_PREFIXES


def progress_path(start: str, end: str, provider: str = "akshare", *,
                  cache_dir: Path = CACHE_DIR, overwrite_amount: bool = False) -> Path:
    operation = "liquidity" if overwrite_amount else "history"
    return cache_dir / (
        f"_backfill_{provider}_{start.replace('-', '')}_{end.replace('-', '')}_{operation}_progress.json"
    )


def to_standard(frame: pd.DataFrame) -> pd.DataFrame:
    """Retain historical turnover rates instead of deriving them from volume."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    out = frame.loc[:, ["datetime", "open", "high", "low", "close", "volume"]].copy()
    out["datetime"] = pd.to_datetime(out["datetime"])
    for column in ("open", "high", "low", "close", "volume"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out["amount"] = pd.to_numeric(frame["amount"], errors="coerce") if "amount" in frame else 0.0
    out["is_closed"] = frame["is_closed"].astype(bool) if "is_closed" in frame else True
    if "turnover_rate" in frame:
        out["turnover_rate"] = pd.to_numeric(frame["turnover_rate"], errors="coerce")
    out = out.loc[out["is_closed"]].sort_values("datetime").reset_index(drop=True)
    if out.empty:
        return out
    prices = out[["open", "high", "low", "close"]].to_numpy(dtype=float)
    volumes = out["volume"].to_numpy(dtype=float)
    if (out["datetime"].isna().any() or out["datetime"].duplicated().any()
            or not np.isfinite(prices).all() or (prices <= 0).any()
            or not np.isfinite(volumes).all() or (volumes < 0).any()
            or (out["high"] < out[["open", "close", "low"]].max(axis=1)).any()
            or (out["low"] > out[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("历史行情包含非法日期、重复日期、价格或成交量")
    out.attrs = dict(frame.attrs)
    return out


def load_progress(path: Path) -> set[str]:
    if path.exists():
        return set(json.loads(path.read_text(encoding="utf-8")).get("done", []))
    return set()


def save_progress(path: Path, done: set[str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"done": sorted(done)}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_symbol_list(path: Path) -> list[str]:
    """Read newline/comma separated codes; ``#`` starts a comment line."""
    tokens: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        tokens.extend(token for token in line.replace(",", " ").split() if token)
    return tokens


def overlap_drift(existing: pd.DataFrame, new_rows: pd.DataFrame) -> tuple[int, float]:
    """Maximum relative OHLC difference; a median can hide a bad splice boundary."""
    if existing.empty or new_rows.empty:
        return 0, 0.0
    columns = ["datetime", "open", "high", "low", "close"]
    overlap = existing[columns].merge(new_rows[columns], on="datetime", suffixes=("_old", "_new"))
    if overlap.empty:
        return 0, 0.0
    old = overlap[[f"{c}_old" for c in columns[1:]]].to_numpy(dtype=float)
    new = overlap[[f"{c}_new" for c in columns[1:]]].to_numpy(dtype=float)
    if not np.isfinite(old).all() or not np.isfinite(new).all() or (old <= 0).any():
        raise ValueError("重叠区间存在非法 OHLC")
    return len(overlap), float(np.max(np.abs(new / old - 1)))


def backfill_one(provider: AkshareDailyProvider | BaostockDailyProvider, symbol: str,
                 path: Path, start: str, end: str, *, max_overlap_drift: float = 0.005,
                 overwrite_amount: bool = False) -> str:
    existing = to_standard(pd.read_pickle(path)) if path.exists() else pd.DataFrame()
    source = provider.name
    if source == "baostock" and not existing.empty and (
        existing.attrs.get("source") != "baostock"
        or existing.attrs.get("volume_unit") != "shares"
        or existing.attrs.get("adjust") != "qfq"
    ):
        raise ValueError("BaoStock 必须使用独立研究缓存；请指定新的 --cache-dir，不能拼接不同来源/单位的历史")

    # Only a completed query can establish coverage; long suspensions and IPOs
    # must not be mistaken for missing data based on calendar-day gap heuristics.
    attrs = existing.attrs
    if (not existing.empty and attrs.get("backfill_provider") == source
            and attrs.get("requested_start", "9999") <= start
            and attrs.get("requested_end", "") >= end
            and (not overwrite_amount or attrs.get("liquidity_enriched") is True)):
        return "already"

    fetch_start, fetch_end = start, end
    if source == "baostock" and not existing.empty:
        # Re-fetch the entire union using one adjustment snapshot. Keeping old
        # rows after a dividend would create an artificial jump at the splice.
        fetch_start = min(start, existing["datetime"].min().strftime("%Y-%m-%d"))
        fetch_end = max(end, existing["datetime"].max().strftime("%Y-%m-%d"))
    ts_code = normalize_ts_code(symbol)
    fetched = provider.get_daily_data(ts_code, start_date=fetch_start.replace("-", ""),
                                      end_date=fetch_end.replace("-", ""))
    new_rows = to_standard(fetched)
    if new_rows.empty:
        return "no_data"
    if (new_rows["datetime"].min() < pd.Timestamp(fetch_start)
            or new_rows["datetime"].max() > pd.Timestamp(fetch_end)):
        raise ValueError("数据源返回了请求区间以外的日线")

    if source == "baostock":
        combined = new_rows
        if not existing.empty:
            missing = existing["datetime"].isin(new_rows["datetime"])
            if not missing.all():
                raise ValueError("完整重抓缺失既有交易日，拒绝替换；检查数据源覆盖")
    else:
        overlap_days, drift = overlap_drift(existing, new_rows)
        if overlap_days and drift > max_overlap_drift:
            raise ValueError(f"复权口径漂移：{overlap_days} 个重叠日，最大相对 OHLC 差异 {drift:.4%}")
        combined = pd.concat([existing, new_rows], ignore_index=True)
        combined = combined.drop_duplicates("datetime", keep="first").set_index("datetime")
        incoming = new_rows.set_index("datetime")
        if overwrite_amount:
            amount = pd.to_numeric(combined["amount"], errors="coerce")
            replacement = incoming["amount"].reindex(combined.index)
            fill = (amount.isna() | amount.eq(0)) & replacement.gt(0)
            combined.loc[fill, "amount"] = replacement.loc[fill]
        if "turnover_rate" in incoming:
            if "turnover_rate" not in combined:
                combined["turnover_rate"] = np.nan
            replacement = incoming["turnover_rate"].reindex(combined.index)
            fill = combined["turnover_rate"].isna() & replacement.gt(0)
            combined.loc[fill, "turnover_rate"] = replacement.loc[fill]
        combined = combined.reset_index().sort_values("datetime").reset_index(drop=True)
        combined.attrs = dict(existing.attrs or new_rows.attrs)
        combined.attrs.setdefault("source", source)

    combined.attrs.update(
        backfill_provider=source, requested_start=fetch_start, requested_end=fetch_end,
        liquidity_enriched=overwrite_amount, timeframe="1d", adjust="qfq",
        timezone="Asia/Shanghai", timestamp_semantics="end",
    )
    if source == "baostock":
        combined.attrs.update(source="baostock", volume_unit="shares")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        combined.to_pickle(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    added = len(combined) - len(existing)
    return f"added:{added}" if added > 0 else "refreshed"


def main() -> int:
    parser = argparse.ArgumentParser(description="准备历史日线；BaoStock 使用独立研究目录，不改生产配置")
    parser.add_argument("--provider", default="akshare", choices=("akshare", "baostock"))
    parser.add_argument("--start", default=BACKFILL_START)
    parser.add_argument("--end", default=BACKFILL_END)
    parser.add_argument("--symbols", nargs="+", help="指定股票代码；不指定则读 universe-dir 中的 qfq 文件名")
    parser.add_argument("--symbols-file", type=Path,
                        help="从文本文件读取代码（换行/逗号分隔，# 注释）；顺序即处理顺序，用于分层交错的全量任务")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR, help="输出缓存目录")
    parser.add_argument("--universe-dir", type=Path, default=CACHE_DIR, help="仅用于读取股票代码，不修改其文件")
    parser.add_argument("--limit", type=int, default=0, help="0=全部；正数限制本轮股票数")
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--max-cache-start-year", type=int, default=None)
    parser.add_argument("--max-overlap-drift", type=float, default=0.005,
                        help="AkShare 拼接时允许的最大相对 OHLC 差异，0.005=0.5%%")
    parser.add_argument("--overwrite-amount", action="store_true",
                        help="补齐重叠旧行的缺失成交额；始终保留数据源提供的历史换手率")
    args = parser.parse_args()
    try:
        start = pd.Timestamp(args.start).strftime("%Y-%m-%d")
        end = pd.Timestamp(args.end).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        parser.error("--start/--end 必须是有效日期")
    if start > end:
        parser.error("--start 不能晚于 --end")
    if args.limit < 0 or not math.isfinite(args.max_overlap_drift) or args.max_overlap_drift < 0:
        parser.error("--limit 与 --max-overlap-drift 必须是非负有限数")
    config = load_config(BASE / "config/config.yaml")
    interval = args.interval if args.interval is not None else float(
        config.get("market_data", {}).get("request_interval_seconds", 0.35)
    )
    if not math.isfinite(interval) or interval < 0:
        parser.error("--interval 必须是非负有限数")
    cache_dir = args.cache_dir.expanduser().resolve()
    universe_dir = args.universe_dir.expanduser().resolve()
    if args.provider == "baostock" and cache_dir == CACHE_DIR.resolve():
        parser.error("BaoStock 请指定独立的 --cache-dir（例如 cache/baostock_2016_2024）")
    raw_symbols = list(args.symbols or [])
    if args.symbols_file is not None:
        if not args.symbols_file.is_file():
            parser.error(f"--symbols-file 不存在: {args.symbols_file}")
        raw_symbols = read_symbol_list(args.symbols_file) + raw_symbols
    if not raw_symbols:
        raw_symbols = sorted(p.name[:-8] for p in universe_dir.glob("*_qfq.pkl"))
    codes = [normalize_ts_code(s) for s in raw_symbols]
    if any(len(s.split(".")[0]) != 6 or not s.split(".")[0].isdigit() for s in codes):
        parser.error("股票代码必须包含 6 位数字")
    unsupported = sorted({s for s in codes if s.endswith(".BJ")}) if args.provider == "baostock" else []
    symbols = list(dict.fromkeys(s.split(".")[0] for s in codes if s not in unsupported))
    if args.max_cache_start_year is not None:
        kept = []
        for symbol in symbols:
            path = universe_dir / f"{symbol}_qfq.pkl"
            if path.exists():
                frame = pd.read_pickle(path)
                if pd.to_datetime(frame["datetime"]).min().year <= args.max_cache_start_year:
                    kept.append(symbol)
        symbols = kept
    if args.limit:
        symbols = symbols[:args.limit]
    if not symbols:
        parser.error("没有可处理股票；检查 --symbols/--universe-dir")
    cache_dir.mkdir(parents=True, exist_ok=True)
    progress = progress_path(start, end, args.provider, cache_dir=cache_dir,
                             overwrite_amount=args.overwrite_amount)
    done = load_progress(progress)
    pending = [s for s in symbols if s not in done]
    provider = BaostockDailyProvider(config=config) if args.provider == "baostock" else AkshareDailyProvider(config=config)
    print(f"来源 {args.provider} | 区间 {start} ~ {end} | 缓存 {cache_dir}", flush=True)
    print(f"本轮 {len(symbols)} 只，续跑跳过 {len(symbols)-len(pending)}，不支持 {len(unsupported)} 只", flush=True)
    results: dict[str, str] = {}
    began = time.monotonic()
    try:
        for index, symbol in enumerate(pending, start=1):
            try:
                result = backfill_one(provider, symbol, cache_dir / f"{symbol}_qfq.pkl", start, end,
                                      max_overlap_drift=args.max_overlap_drift,
                                      overwrite_amount=args.overwrite_amount)
            except (OSError, EOFError, RuntimeError, ValueError, KeyError, TypeError, ImportError) as error:
                result = f"error:{type(error).__name__}: {error}"
            results[symbol] = result
            if marks_done(result):
                done.add(symbol)
            save_progress(progress, done)
            print(f"{index}/{len(pending)} {symbol} {result}", flush=True)
            if index < len(pending):
                time.sleep(interval)
    finally:
        if isinstance(provider, BaostockDailyProvider):
            provider.close()
        save_progress(progress, done)
    summary = {
        "provider": args.provider, "start": start, "end": end, "cache_dir": str(cache_dir),
        "requested": len(symbols), "completed": sum(s in done for s in symbols),
        "unsupported": unsupported, "results": results, "elapsed_seconds": round(time.monotonic()-began, 2),
    }
    report = progress.with_name(progress.stem.removesuffix("_progress") + "_report.json")
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, ensure_ascii=False), flush=True)
    print(f"报告 {report}", flush=True)
    return 0 if summary["completed"] == len(symbols) else 1


if __name__ == "__main__":
    raise SystemExit(main())
