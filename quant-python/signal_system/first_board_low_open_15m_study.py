"""首板低开：日线信号 + 15m 小级别进场验证。

预注册口径（首次运行前固定，禁止事后调参）：
- 信号：完全复用 ``first_board_four_strategies_study`` 的 s1 首板低开信号，
  2020-01-02 .. 2026-10-30；不加任何新选股条件。
- 基线 B0：信号日 T 开盘价买入。
- B1：T 日 15m 收盘价首次 > T 日开盘价（收复低开），下一根 15m 开盘买。
- B2：T 日 15m 收盘价首次 > 第一根 15m（09:30-09:45）最高价，下一根 15m 开盘买。
- B3：B2 + 确认根成交量 >= 1.5 × 第一根 15m 成交量，下一根 15m 开盘买。
- 确认截止 11:15；11:15 之后不再开仓。未确认则当天放弃。
- 出场统一为 T+1 日 14:45 的 15m 收盘价，隔离“进场”变量；
  另给出源码式 11:30 有浮盈先卖的近似口径。
- 成本：佣金 3bp/边、卖出印花税 10bp、滑点 10bp/边。
- 数据：BaoStock 15m 前复权，按信号日/次日抓取，缓存到 cache/first_board_15m。
- 不接生产、不自动交易。15m 收盘确认后按下一根开盘成交，避免收盘价成交的未来函数。

这是研究脚本，输出事件级比较（每笔独立），不做组合曲线。
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from first_board_four_strategies_study import build_signals, load_bundle

BASE_DIR = pathlib.Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache" / "first_board_15m"
OUT_DIR = BASE_DIR.parent.parent / "docs" / "backtest-results" / "2026-11-16-first-board-low-open-15m"
COMMISSION = 0.0003
STAMP_TAX = 0.0010
SLIPPAGE = 0.0010
LOT = 100
CONFIRM_CUTOFF = "11:15"
EXIT_TIME = "14:45"
EARLY_EXIT_TIME = "11:30"


@dataclass(frozen=True)
class Variant:
    name: str
    rule: str


VARIANTS = [
    Variant("B0", "T 日开盘价直接买"),
    Variant("B1", "15m 收盘收复 T 日开盘价，下一根开盘买"),
    Variant("B2", "15m 收盘突破第一根 15m 最高价，下一根开盘买"),
    Variant("B3", "B2 + 确认根量 >= 1.5×第一根量"),
]


def _code(symbol: str) -> str:
    symbol = str(symbol).zfill(6)
    if symbol.startswith(("6", "9")):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _time_of(value: str) -> pd.Timestamp:
    return pd.to_datetime(str(value)[:14], format="%Y%m%d%H%M%S")


def fetch_bars(
    code: str,
    start: str,
    end: str,
    refresh: bool = False,
    *,
    adjustflag: str = "2",
    cache_dir: pathlib.Path = CACHE_DIR,
) -> pd.DataFrame:
    """BaoStock 15m K 线；``adjustflag`` 2=前复权（研究默认）、3=不复权。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{code.replace('.', '_')}_{start}.pkl"
    if cache.exists() and not refresh:
        return pd.read_pickle(cache)

    import baostock as bs

    last_error = None
    for _ in range(3):
        try:
            result = bs.query_history_k_data_plus(
                code,
                "date,time,open,high,low,close,volume,amount",
                start_date=start,
                end_date=end,
                frequency="15",
                adjustflag=adjustflag,
            )
            if result.error_code != "0":
                last_error = result.error_msg
                continue
            rows = []
            while result.next():
                rows.append(result.get_row_data())
            if not rows:
                frame = pd.DataFrame()
            else:
                frame = pd.DataFrame(rows, columns=result.fields)
                frame["datetime"] = frame["time"].map(_time_of)
                frame["date"] = pd.to_datetime(frame["date"])
                for column in ("open", "high", "low", "close", "volume", "amount"):
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
                frame = frame.dropna(subset=["open", "high", "low", "close"])
                frame = frame[frame["date"].dt.strftime("%Y-%m-%d").between(start, end)]
                frame = frame.sort_values("datetime").reset_index(drop=True)
            frame.to_pickle(cache)
            return frame
        except Exception as exc:  # network/SDK failure
            last_error = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"BaoStock 15m failed for {code} {start}..{end}: {last_error}")


def signal_table(start: str, end: str) -> pd.DataFrame:
    bundle = load_bundle()
    signals = build_signals(bundle)["s1_first_board_low_open"]
    rows = []
    for index, columns in signals.items():
        if index + 1 >= len(bundle.dates):
            continue
        day = bundle.dates[index]
        if day < pd.Timestamp(start) or day > pd.Timestamp(end):
            continue
        for column in columns:
            rows.append(
                {
                    "symbol": str(bundle.symbols[column]),
                    "signal_date": day.strftime("%Y-%m-%d"),
                    "next_date": bundle.dates[index + 1].strftime("%Y-%m-%d"),
                    "open": float(bundle.open[index, column]),
                    "close": float(bundle.close[index, column]),
                    "next_close": float(bundle.close[index + 1, column]),
                    "high_limit": float(bundle.high_limit_price[index, column]),
                    "next_high_limit": float(bundle.high_limit_price[index + 1, column]),
                    "gap": float(bundle.open_ratio[index, column]),
                }
            )
    return pd.DataFrame(rows).drop_duplicates(["symbol", "signal_date"])


def _scale_day(day: pd.DataFrame, daily_open: float) -> tuple[pd.DataFrame, bool, float]:
    if day.empty:
        return day, False, 1.0
    first_open = float(day.iloc[0]["open"])
    if not math.isfinite(first_open) or first_open <= 0:
        return day.iloc[0:0], False, 1.0
    ratio = daily_open / first_open
    if abs(ratio - 1.0) <= 0.005:
        return day, False, 1.0
    day = day.copy()
    for column in ("open", "high", "low", "close"):
        day[column] = day[column] * ratio
    return day, True, ratio


def _bar_at(day: pd.DataFrame, hhmm: str) -> pd.Series | None:
    target = pd.Timestamp(hhmm).time()
    for _, bar in day.iterrows():
        if bar["datetime"].time() <= target:
            candidate = bar
        else:
            break
    return candidate if "candidate" in locals() else None


def _entry_price(
    variant: str,
    day: pd.DataFrame,
    daily_open: float,
    high_limit: float,
) -> tuple[float, pd.Timestamp, float, bool] | None:
    if variant == "B0":
        return daily_open, day.iloc[0]["datetime"] - pd.Timedelta(minutes=15), 0.0, True

    first = day.iloc[0]
    first_high = float(first["high"])
    first_volume = float(first["volume"])
    cutoff = pd.Timestamp(CONFIRM_CUTOFF).time()
    for i in range(0, len(day)):
        bar = day.iloc[i]
        if bar["datetime"].time() > cutoff:
            break
        confirmed = False
        if variant == "B1":
            confirmed = float(bar["close"]) > daily_open
        elif variant in ("B2", "B3"):
            confirmed = float(bar["close"]) > first_high
            if variant == "B3" and confirmed:
                confirmed = float(bar["volume"]) >= 1.5 * first_volume
        if not confirmed:
            continue
        if i + 1 >= len(day):
            return None
        entry_bar = day.iloc[i + 1]
        entry = float(entry_bar["open"])
        if not math.isfinite(entry) or entry <= 0:
            return None
        if entry >= high_limit - 0.005:
            return None
        premium = entry / daily_open - 1.0
        return entry, entry_bar["datetime"], premium, True
    return None


def _exit_price(day: pd.DataFrame, mode: str, entry: float) -> tuple[float, pd.Timestamp]:
    exit_bar = _bar_at(day, EXIT_TIME)
    early_bar = _bar_at(day, EARLY_EXIT_TIME)
    if exit_bar is None:
        raise ValueError("no exit bar")
    if mode == "source" and early_bar is not None:
        if float(early_bar["close"]) > entry * (1.0 + COMMISSION):
            return float(early_bar["close"]), early_bar["datetime"]
    return float(exit_bar["close"]), exit_bar["datetime"]


def net_return(entry: float, exit_: float) -> float:
    entry_cost = entry * (1.0 + SLIPPAGE) * (1.0 + COMMISSION)
    exit_net = exit_ * (1.0 - SLIPPAGE) * (1.0 - COMMISSION - STAMP_TAX)
    return exit_net / entry_cost - 1.0


def stats(trades: pd.DataFrame, signal_count: int) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "fill_rate_pct": 0.0}
    returns = trades["net_return"].to_numpy(dtype=float)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    by_year = {}
    for year, group in trades.groupby(trades["signal_date"].str[:4]):
        r = group["net_return"].to_numpy(dtype=float)
        by_year[str(year)] = {
            "trades": int(len(group)),
            "mean_pct": round(float(r.mean()) * 100, 2),
            "win_rate_pct": round(float((r > 0).mean()) * 100, 2),
        }
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    return {
        "trades": int(len(returns)),
        "signals": int(signal_count),
        "fill_rate_pct": round(len(returns) / signal_count * 100, 2) if signal_count else 0.0,
        "mean_pct": round(float(returns.mean()) * 100, 3),
        "median_pct": round(float(np.median(returns)) * 100, 3),
        "win_rate_pct": round(float((returns > 0).mean()) * 100, 2),
        "avg_win_pct": round(float(wins.mean()) * 100, 3) if len(wins) else 0.0,
        "avg_loss_pct": round(float(losses.mean()) * 100, 3) if len(losses) else 0.0,
        "payoff": round(float(wins.mean() / abs(losses.mean())), 2) if len(wins) and len(losses) else None,
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if len(losses) and len(wins) else None,
        "t_stat": round(float(returns.mean() / (std / math.sqrt(len(returns)))), 2) if std > 0 else 0.0,
        "best_pct": round(float(returns.max()) * 100, 2),
        "worst_pct": round(float(returns.min()) * 100, 2),
        "avg_entry_premium_pct": round(float(trades["entry_premium"].mean()) * 100, 3),
        "by_year": by_year,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    signals = signal_table(args.start, args.end)
    if args.limit:
        signals = signals.head(args.limit).copy()
    print(f"signals: {len(signals)}", flush=True)

    import baostock as bs
    print(bs.login().error_msg, flush=True)
    records: list[dict[str, Any]] = []
    scaled = 0
    missing = 0
    try:
        for number, row in enumerate(signals.itertuples(index=False), 1):
            try:
                bars = fetch_bars(
                    _code(row.symbol), row.signal_date, row.next_date, args.refresh
                )
            except Exception as exc:
                print(f"  [{number}/{len(signals)}] {row.symbol} {row.signal_date} fetch failed: {exc}", flush=True)
                missing += 1
                continue
            day = bars[bars["date"] == pd.Timestamp(row.signal_date)].sort_values("datetime")
            next_day = bars[bars["date"] == pd.Timestamp(row.next_date)].sort_values("datetime")
            if day.empty or next_day.empty:
                missing += 1
                continue
            day, was_scaled, ratio = _scale_day(day, float(row.open))
            scaled += int(was_scaled)
            if day.empty:
                missing += 1
                continue
            if abs(ratio - 1.0) > 0.005:
                next_day = next_day.copy()
                for column in ("open", "high", "low", "close"):
                    next_day[column] = next_day[column] * ratio
            for variant in VARIANTS:
                entry = _entry_price(
                    variant.name, day, float(row.open), float(row.high_limit)
                )
                if entry is None:
                    continue
                entry_price, entry_time, premium, _ = entry
                for exit_mode in ("fixed", "source"):
                    try:
                        exit_price, exit_time = _exit_price(
                            next_day, exit_mode, entry_price
                        )
                    except ValueError:
                        continue
                    records.append(
                        {
                            "variant": variant.name,
                            "exit_mode": exit_mode,
                            "symbol": row.symbol,
                            "signal_date": row.signal_date,
                            "next_date": row.next_date,
                            "entry_time": entry_time.strftime("%H:%M"),
                            "entry_price": round(entry_price, 4),
                            "entry_premium": premium,
                            "exit_price": round(exit_price, 4),
                            "exit_time": exit_time.strftime("%H:%M"),
                            "net_return": net_return(entry_price, exit_price),
                            "gap": row.gap,
                            "scaled": was_scaled,
                        }
                    )
            if number % 50 == 0:
                print(f"  scanned {number}/{len(signals)}", flush=True)
    finally:
        bs.logout()

    trades = pd.DataFrame(records)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    signals.to_csv(out / "signals.csv", index=False)
    trades.to_csv(out / "trades.csv", index=False)

    summary = {
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "period": {"start": args.start, "end": args.end},
        "signal_count": int(len(signals)),
        "scaled_days": int(scaled),
        "missing_days": int(missing),
        "costs": {
            "commission_per_side": COMMISSION,
            "stamp_tax_sell": STAMP_TAX,
            "slippage_per_side": SLIPPAGE,
        },
        "variants": {v.name: {"rule": v.rule} for v in VARIANTS},
        "results": {},
    }
    for variant in VARIANTS:
        for mode in ("fixed", "source"):
            subset = trades[(trades["variant"] == variant.name) & (trades["exit_mode"] == mode)] if not trades.empty else trades
            summary["results"][f"{variant.name}_{mode}"] = stats(subset, len(signals))
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for key, value in summary["results"].items():
        print(
            f"{key}: trades={value.get('trades')} fill={value.get('fill_rate_pct')}% "
            f"mean={value.get('mean_pct')}% win={value.get('win_rate_pct')}% "
            f"payoff={value.get('payoff')} pf={value.get('profit_factor')} t={value.get('t_stat')} "
            f"premium={value.get('avg_entry_premium_pct')}%",
            flush=True,
        )
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
