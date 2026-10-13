"""把 2018-2022 日线回填进 cache/daily_history/<symbol>_qfq.pkl（供熊市抽样回测使用）。

为什么需要这个脚本：三策略回测的样本构建器（scale_out_take_profit_study._out_of_sample_job）
只读本地缓存 cache/daily_history/*_qfq.pkl，不联网；而该缓存起点中位 2023-05-18，
0 只覆盖 2018/2022。现成的 load_backtest_history 也帮不上忙 —— 它是**向前**抓
（取最近 N 根）并覆盖式写盘，无法向 2018 年回填。

本脚本：
1. 读现有 <symbol>_qfq.pkl（可能不存在），
2. 用 AkshareDailyProvider.get_daily_data(start_date, end_date) 抓 2018-01-01 ~ 2023-05-19
   （终点定为 2023-05-19 而非 2022-12-31：缓存原有起点中位 2023-05-18，
   只补到 2022-12-31 会留下 2023-01~2023-05 的约 139 天空洞），
   该接口 adjust 固定 qfq，与缓存口径一致，
3. 归一化成项目的 STANDARD_COLUMNS（datetime/open/high/low/close/volume/amount/is_closed），
4. **合并**（按 datetime 去重、排序）后写回，绝不丢掉已有的 2023+ 数据。

可中断续跑：已在 progress 文件里记为完成的标的会跳过。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(os.path.abspath(__file__)).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

import pandas as pd  # noqa: E402

from data.providers.akshare_provider import AkshareDailyProvider  # noqa: E402
from utils.helpers import load_config  # noqa: E402

BACKFILL_START = "2018-01-01"
# 终止日必须覆盖到既有缓存起点之前，否则会留下空洞：
# 原缓存起点中位 2023-05-18，若只回填到 2022-12-31，则 2023-01 ~ 2023-05 整段缺失，
# 而预热（270 根）与信号计算会跨过这个断裂 → 结果不可信。故统一回填到 2023-05-19。
BACKFILL_END = "2023-05-19"
CACHE_DIR = BASE / "cache" / "daily_history"
PROGRESS = CACHE_DIR / "_backfill_2018_2022_progress.json"


def to_standard(frame: pd.DataFrame) -> pd.DataFrame:
    """把 provider 输出归一化成缓存使用的 8 列标准结构。"""
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "datetime": pd.to_datetime(frame["datetime"]),
        "open": pd.to_numeric(frame["open"], errors="coerce"),
        "high": pd.to_numeric(frame["high"], errors="coerce"),
        "low": pd.to_numeric(frame["low"], errors="coerce"),
        "close": pd.to_numeric(frame["close"], errors="coerce"),
        "volume": pd.to_numeric(frame["volume"], errors="coerce"),
        "amount": pd.to_numeric(frame.get("amount"), errors="coerce") if "amount" in frame else 0.0,
        # 缓存里的历史行全部标记为已收盘；回填的都是历史数据，恒为 True。
        "is_closed": True,
    })
    return out


def load_progress() -> set[str]:
    if PROGRESS.exists():
        return set(json.loads(PROGRESS.read_text(encoding="utf-8")).get("done", []))
    return set()


def save_progress(done: set[str]) -> None:
    PROGRESS.write_text(json.dumps({"done": sorted(done)}, ensure_ascii=False), encoding="utf-8")


def backfill_one(provider: AkshareDailyProvider, symbol: str, path: Path) -> str:
    """返回结果码：added / no_data / already / error。"""
    existing = pd.DataFrame()
    if path.exists():
        try:
            existing = pd.read_pickle(path)
            existing = existing.drop(columns=[c for c in ("trade_date", "vol") if c in existing.columns])
        except Exception as error:  # noqa: BLE001 - 单只损坏不应中断整轮
            return f"error: 读取现有缓存失败 {type(error).__name__}"

    dates = pd.to_datetime(existing["datetime"]) if not existing.empty and "datetime" in existing else pd.Series(dtype="datetime64[ns]")
    if not dates.empty:
        # "已完成"的判据是**起点足够早且中段无空洞**，不是"存在旧数据"：
        # 只回填到 2022 就跳过的写法会漏掉 2023-01 ~ 2023-05 的断裂。
        early = int((dates < BACKFILL_START).sum())
        has_gap = ((dates.sort_values().diff().dt.days > 30).sum() > 0)
        if early > 200 and not has_gap:
            return "already"

    ts_code = symbol + (".SH" if symbol.startswith("6") else ".SZ" if symbol.startswith(("0", "3")) else ".BJ")
    fetched = provider.get_daily_data(ts_code, start_date=BACKFILL_START.replace("-", ""), end_date=BACKFILL_END.replace("-", ""))
    new_rows = to_standard(fetched)
    if new_rows.empty:
        return "no_data"

    combined = pd.concat([existing, new_rows], ignore_index=True) if not existing.empty else new_rows
    combined = combined.drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    # pd.concat / reset_index 会丢掉 DataFrame.attrs，而下游审计（atr_volatility_audit、
    # canonical_replay_semantics_audit 等）用 frame.attrs["adjust"] == "qfq" 判定口径。
    # 必须显式还原，否则整批缓存会被判为"非 qfq"而拒绝。
    combined.attrs = dict(existing.attrs) if existing.attrs else {}
    combined.attrs.setdefault("source", "tencent")
    combined.attrs["timeframe"] = "1d"
    combined.attrs["adjust"] = "qfq"
    combined.attrs.setdefault("timezone", "Asia/Shanghai")
    combined.attrs.setdefault("timestamp_semantics", "end")
    combined.to_pickle(path)
    return f"added:{len(new_rows)}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"回填日线进 cache/daily_history（默认 {BACKFILL_START} ~ {BACKFILL_END}）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部），用于冒烟")
    parser.add_argument("--symbols", nargs="*", default=None, help="指定标的（不带后缀）")
    parser.add_argument("--interval", type=float, default=None, help="请求间隔秒，默认取 config")
    args = parser.parse_args()

    config = load_config(BASE / "config/config.yaml")
    interval = args.interval if args.interval is not None else float(
        config.get("market_data", {}).get("request_interval_seconds", 0.35)
    )
    provider = AkshareDailyProvider(config=config)
    provider._get_akshare()      # 预热，排除首次 import 开销

    if args.symbols:
        symbols = list(args.symbols)
    else:
        symbols = sorted(p.name[: -len("_qfq.pkl")] for p in CACHE_DIR.glob("*_qfq.pkl"))
    if args.limit:
        symbols = symbols[: args.limit]

    done = load_progress()
    todo = [s for s in symbols if s not in done]
    print(f"待处理 {len(todo)} / {len(symbols)}（已完成 {len(symbols) - len(todo)}），间隔 {interval}s", flush=True)

    counts: dict[str, int] = {}
    started = time.time()
    for index, symbol in enumerate(todo, start=1):
        path = CACHE_DIR / f"{symbol}_qfq.pkl"
        try:
            result = backfill_one(provider, symbol, path)
        except Exception as error:  # noqa: BLE001 - 单只失败继续
            result = f"error: {type(error).__name__}: {error}"
        key = result.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
        if key in ("added", "no_data", "already"):
            done.add(symbol)
        if index % 25 == 0 or index == len(todo):
            elapsed = time.time() - started
            rate = elapsed / index
            remaining = rate * (len(todo) - index)
            print(f"  {index}/{len(todo)}  {counts}  用时 {elapsed/60:.1f}m  预计剩余 {remaining/60:.1f}m",
                  flush=True)
            save_progress(done)
        time.sleep(interval)

    save_progress(done)
    print(f"\n完成：{counts}")
    print(f"总用时 {(time.time() - started)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
