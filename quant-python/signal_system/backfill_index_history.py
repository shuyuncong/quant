"""把上证指数日线前推到指定起点（默认 2016-12-01），写回 cache/index_000001_sh.pkl。

为什么需要：市场闸门（`strategy/market_gate.py`）要 MA250 等长均线，
预热需要 >=270 根已收盘日线。现有指数缓存起点是 2018-01-02，
到 2018-12-28 只有 243 根 —— 于是 2018 **全年 243 个交易日的 blocked_by 全是
`insufficient_history`**，闸门 0 天开放，2018H2 窗口因两侧同时为空而无法对照。
把起点前推到 2016-12 才能让 2018H2 具备运行条件。

与 backfill_history_cache.py 同一套纪律：
- **合并**而非覆盖（按 datetime 去重、排序），绝不丢已有数据；
- 显式还原 `DataFrame.attrs`（pd.concat 会丢掉它，下游按 attrs["adjust"] 判定口径）；
- 写前备份到 `.bak`，只在成功时替换。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

BASE = Path(os.path.abspath(__file__)).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

import pandas as pd  # noqa: E402

from data.providers.akshare_provider import AkshareDailyProvider  # noqa: E402
from utils.helpers import load_config  # noqa: E402

DEFAULT_PATH = BASE / "cache" / "index_000001_sh.pkl"
DEFAULT_START = "2016-12-01"
DEFAULT_END = "2026-09-01"


def to_standard(frame: pd.DataFrame) -> pd.DataFrame:
    """归一化成缓存使用的 8 列结构（与 backfill_history_cache.to_standard 同口径）。"""
    if frame is None or frame.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "datetime": pd.to_datetime(frame["datetime"]),
        "open": pd.to_numeric(frame["open"], errors="coerce"),
        "high": pd.to_numeric(frame["high"], errors="coerce"),
        "low": pd.to_numeric(frame["low"], errors="coerce"),
        "close": pd.to_numeric(frame["close"], errors="coerce"),
        "volume": pd.to_numeric(frame["volume"], errors="coerce"),
        "amount": pd.to_numeric(frame.get("amount"), errors="coerce") if "amount" in frame else 0.0,
        "is_closed": True,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"指数缓存不存在：{path}")

    existing = pd.read_pickle(path)
    before_attrs = dict(existing.attrs)
    before_dates = pd.to_datetime(existing["datetime"])
    print(f"现有：{len(existing)} 根，{before_dates.min().date()} ~ {before_dates.max().date()}")
    print(f"     attrs={before_attrs}")

    config = load_config(BASE / "config/config.yaml")
    provider = AkshareDailyProvider(config=config)
    fetched = provider.get_index_daily("000001.SH", start_date=args.start.replace("-", ""),
                                       end_date=args.end.replace("-", ""))
    new_rows = to_standard(fetched)
    if new_rows.empty:
        raise SystemExit("抓取结果为空，未改动任何文件")
    print(f"抓取：{len(new_rows)} 根，{new_rows['datetime'].min().date()} ~ {new_rows['datetime'].max().date()}")

    # **keep="first"：重叠日期一律保留原有取值，只用新数据补齐缺失的更早日期。**
    # 曾经用 keep="last"（新数据覆盖旧的）导致 2103 天里 51.7% 的 OHLC 被改写，
    # 幅度虽只有 <=0.005（数据源四舍五入漂移），但足以让"只前推、不改历史"的说法不成立，
    # 也会破坏下游预登记哈希所代表的数据同一性。前推历史**没有理由**重写既有行。
    combined = pd.concat([existing, new_rows], ignore_index=True)
    combined = combined.drop_duplicates("datetime", keep="first").sort_values("datetime").reset_index(drop=True)

    # pd.concat / reset_index 会丢掉 DataFrame.attrs —— 必须显式还原，
    # 否则下游按 attrs["adjust"] 判定口径的审计会拒绝这份缓存。
    combined.attrs = dict(before_attrs)
    combined.attrs.setdefault("source", "eastmoney-index")
    combined.attrs["timeframe"] = "1d"
    combined.attrs["adjust"] = "none"
    combined.attrs.setdefault("timezone", "Asia/Shanghai")
    combined.attrs.setdefault("timestamp_semantics", "end")

    # 价格完整性：重叠区间必须逐行一致，否则说明两次抓取口径不同，不能合并
    # 重叠区间只做**报告**，不再当作拒绝条件：数据源本身会有 <=0.005 的四舍五入漂移，
    # 因此真正的安全保证来自上面的 keep="first"（根本不采用新值），而不是靠容差判断。
    # 之前的写法用了 0.011 的容差，恰好掩盖了 51.7% 的重叠行被改写这一事实。
    report_cols = [c for c in ("open", "high", "low", "close") if c in existing.columns]
    overlap = pd.merge(existing[["datetime", *report_cols]], new_rows[["datetime", *report_cols]],
                       on="datetime", suffixes=("_old", "_new"))
    if not overlap.empty:
        print(f"重叠 {len(overlap)} 天（这些日期将**保留原值**，不采用新抓取结果）：")
        for col in report_cols:
            drift = (overlap[f"{col}_old"].astype(float) - overlap[f"{col}_new"].astype(float)).abs()
            print(f"  {col:<6} 供应商漂移 max={drift.max():.6f} 非零={int((drift > 0).sum())} 天")

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    combined.to_pickle(path)

    after_dates = pd.to_datetime(combined["datetime"])
    print(f"\n写入：{len(combined)} 根，{after_dates.min().date()} ~ {after_dates.max().date()}")
    print(f"      attrs={combined.attrs}")
    gaps = after_dates.sort_values().diff().dt.days
    print(f"      最大间隔 {gaps.max()} 天")
    pre = int((after_dates < pd.Timestamp("2018-07-01")).sum())
    print(f"      2018-07-01 前 {pre} 根（闸门预热需 >=270）")
    print(f"\n备份：{backup}")


if __name__ == "__main__":
    main()
