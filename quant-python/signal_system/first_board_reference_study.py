"""Mechanical shell of an external first-board pullback strategy.

Reference: 华安证券金融工程, 2026-03-18, "涨停板背后的Alpha：首板回调策略的
系统化探索与实证".  The public report describes the final strategy as:

* first board (换手板, one-word boards excluded), prior 20 days without a limit;
* T-1 pullback close versus the limit-day close in [-7%, +3%];
* T-1 close position in its daily range below 0.8;
* a 0-15 score (market cap, sector heat, pullback quality, sentiment) >= 6;
* buy at T open, skip a limit-up open;
* sell at the next open unless the entry day closes limit-up, then ride until
  the first non-limit close; max five trading days; -2% to -5% stage-adaptive
  stop; limit-down exits deferred.

This script tests only the mechanical shell that is reproducible from local
daily bars.  It does NOT reproduce the score (no PIT industry/sentiment/封单
data), the stage-adaptive stop, or the stage-adaptive position sizing.  The
results therefore measure the hard filters alone; they are not a replication
and can differ from the report in either direction.

Costs and execution follow first_board_pullback_study.py: 3bp commission per
side, 10bp sell stamp tax, 10bp one-way slippage, next-open execution,
limit-down deferral, and no cross-splice returns.
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

import first_board_pullback_study as base

BASE_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_OUT = BASE_DIR / "first_board_reference_results"
REPORT_COMMISSION = 0.00025
REPORT_STAMP_TAX = 0.0005
REPORT_SLIPPAGE = 0.0010


@dataclass(frozen=True)
class ReferenceConfig:
    start: str = "2019-01-01"
    end: str = "2026-10-30"
    min_history: int = 250
    min_price: float = 2.0
    min_adv20: float = 20_000_000.0
    first_board_lookback: int = 20
    pullback_min: float = -0.07
    pullback_max: float = 0.03
    close_position_max: float = 0.8
    hold_days: int = 5
    stop_loss: float = 0.05
    max_exit_defer: int = 5
    exclude_one_word: bool = True


def _is_one_word_at(open_: float, low: float, prev: float, rate: float) -> bool:
    """True when the board opened and stayed at the limit all day."""
    if not (prev > 0 and math.isfinite(prev)):
        return False
    return bool(
        open_ / prev - 1.0 >= rate - 0.005
        and low / prev - 1.0 >= rate - 0.005
    )


def _simulate_exit(
    bars: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    rates: np.ndarray,
    limit_up: np.ndarray,
    cfg: ReferenceConfig,
) -> dict[str, Any] | None:
    """Next-open exit unless the entry day seals the limit; then ride.

    ``hold_days`` counts close checks including the entry day, matching the
    original study's maximum-holding-period convention.
    """
    n = len(bars)
    open_ = bars["open"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    stop = entry_price * (1.0 - cfg.stop_loss)
    last_check = min(entry_idx + cfg.hold_days - 1, n - 1)
    trigger_idx: int | None = None
    reason = "timeout"

    for j in range(entry_idx, last_check + 1):
        if close[j] <= stop:
            trigger_idx = j + 1
            reason = "stop"
            break
        if j == entry_idx:
            if not bool(limit_up[j]):
                trigger_idx = j + 1
                reason = "one_day"
                break
            # Entry day sealed the limit: ride.
            continue
        if not bool(limit_up[j]):
            trigger_idx = j + 1
            reason = "ride_end"
            break

    if trigger_idx is None:
        trigger_idx = entry_idx + cfg.hold_days
        reason = "timeout"
    if trigger_idx >= n:
        return None

    deferred = 0
    while trigger_idx < n and base._is_limit_down_open(
        open_[trigger_idx], close[trigger_idx - 1], rates[trigger_idx]
    ):
        trigger_idx += 1
        deferred += 1
        if deferred > cfg.max_exit_defer:
            return None
    if trigger_idx >= n:
        return None
    exit_price = float(open_[trigger_idx])
    if not math.isfinite(exit_price) or exit_price <= 0:
        return None
    return {
        "exit_idx": int(trigger_idx),
        "exit_date": pd.Timestamp(bars["datetime"].iloc[trigger_idx]),
        "exit_price": exit_price,
        "exit_reason": reason,
        "hold_days": int(trigger_idx - entry_idx),
        "deferred_exit_days": int(deferred),
    }


def detect_candidates(
    symbol: str,
    bars: pd.DataFrame,
    cfg: ReferenceConfig,
) -> list[dict[str, Any]]:
    """Return one trade per qualifying first board.

    The signal is observed at T-1 close.  Entry is T open; no same-bar
    information is used.
    """
    if len(bars) < cfg.min_history + cfg.hold_days + cfg.max_exit_defer:
        return []
    dates = bars["datetime"]
    close = bars["close"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    open_ = bars["open"].to_numpy(dtype=float)
    amount = bars["amount"].to_numpy(dtype=float)
    volume = bars["volume"].to_numpy(dtype=float)
    if not np.isfinite(amount).all() or (amount <= 0).any():
        fallback = volume * close
        amount = np.where(np.isfinite(amount) & (amount > 0), amount, fallback)
    if not np.isfinite(amount).all() or (amount <= 0).any():
        return []

    rates = base.limit_rate_series(symbol, dates)
    prev_close = np.r_[np.nan, close[:-1]]
    limit_up = base._is_limit_up_at(close, high, prev_close, rates)
    limit_up[0] = False
    if "splice_break" in bars.columns:
        limit_up[bars["splice_break"].fillna(False).to_numpy(dtype=bool)] = False

    amount_s = pd.Series(amount)
    avg20 = amount_s.shift(1).rolling(20, min_periods=20).mean().to_numpy()

    first_board = np.zeros(len(bars), dtype=bool)
    for t in np.flatnonzero(limit_up):
        if t < cfg.min_history:
            continue
        first_board[t] = base._candidate_after_quiet_prior(
            limit_up, t, cfg.first_board_lookback
        )

    out: list[dict[str, Any]] = []
    for t in np.flatnonzero(first_board):
        if t < cfg.min_history:
            continue
        if cfg.min_adv20 > 0 and (
            not np.isfinite(avg20[t]) or avg20[t] < cfg.min_adv20
        ):
            continue
        if close[t] < cfg.min_price:
            continue
        if cfg.exclude_one_word and _is_one_word_at(
            float(open_[t]), float(low[t]), float(prev_close[t]), float(rates[t])
        ):
            continue

        j = t + 1
        entry_idx = j + 1
        if entry_idx >= len(bars):
            continue
        pullback = float(close[j] / close[t] - 1.0)
        if pullback < cfg.pullback_min or pullback > cfg.pullback_max:
            continue
        day_range = float(high[j] - low[j])
        close_position = (
            float((close[j] - low[j]) / day_range) if day_range > 0 else 1.0
        )
        if close_position >= cfg.close_position_max:
            continue

        entry_open = float(open_[entry_idx])
        if not math.isfinite(entry_open) or entry_open <= 0:
            continue
        if entry_open / close[j] - 1.0 >= rates[entry_idx] - 0.005:
            # Opening at the upper limit: no realistic fill.
            continue
        exit_info = _simulate_exit(
            bars, entry_idx, entry_open, rates, limit_up, cfg
        )
        if exit_info is None:
            continue
        entry_date = pd.Timestamp(dates.iloc[entry_idx])
        if entry_date <= base.SPLICE_DATE < exit_info["exit_date"]:
            continue

        out.append(
            {
                "symbol": symbol,
                "limit_date": pd.Timestamp(dates.iloc[t]),
                "signal_date": pd.Timestamp(dates.iloc[j]),
                "entry_date": entry_date,
                "limit_close": float(close[t]),
                "limit_amount": float(amount[t]),
                "limit_amount_ratio": float(amount[t] / avg20[t])
                if np.isfinite(avg20[t]) and avg20[t] > 0
                else None,
                "pullback_pct": pullback,
                "pullback_amount_ratio": float(amount[j] / amount[t]),
                "close_position": close_position,
                "entry_gap": float(entry_open / close[j] - 1.0),
                "entry_open": entry_open,
                **exit_info,
            }
        )
    return out


def _cost_return(
    trade: dict[str, Any],
    commission: float,
    stamp_tax: float,
    slippage: float,
) -> float:
    entry_cost = float(trade["entry_open"]) * (1.0 + slippage) * (1.0 + commission)
    exit_net = float(trade["exit_price"]) * (1.0 - slippage) * (
        1.0 - commission - stamp_tax
    )
    return exit_net / entry_cost - 1.0


def _summary_from_returns(returns: np.ndarray) -> dict[str, Any]:
    if len(returns) == 0:
        return {"trades": 0}
    mean = float(returns.mean())
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    t_stat = mean / (std / math.sqrt(len(returns))) if std > 0 else 0.0
    return {
        "trades": int(len(returns)),
        "mean_pct": round(mean * 100, 2),
        "median_pct": round(float(np.median(returns)) * 100, 2),
        "t_stat": round(t_stat, 2),
        "win_rate_pct": round(float((returns > 0).mean()) * 100, 2),
    }


def report_cost_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    returns = np.array(
        [
            _cost_return(t, REPORT_COMMISSION, REPORT_STAMP_TAX, REPORT_SLIPPAGE)
            for t in trades
        ],
        dtype=float,
    )
    return _summary_from_returns(returns)


def yearly_report_cost_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not trades:
        return out
    frame = pd.DataFrame(
        {
            "year": [t["entry_date"].year for t in trades],
            "ret": [
                _cost_return(t, REPORT_COMMISSION, REPORT_STAMP_TAX, REPORT_SLIPPAGE)
                for t in trades
            ],
        }
    )
    for year, group in frame.groupby("year"):
        out[str(int(year))] = {
            "trades": int(len(group)),
            "mean_pct": round(float(group["ret"].mean()) * 100, 2),
            "win_rate_pct": round(float((group["ret"] > 0).mean()) * 100, 2),
        }
    return out


def bucket_stats(
    trades: list[dict[str, Any]], key: str, bins: list[float]
) -> dict[str, Any]:
    if not trades:
        return {}
    frame = pd.DataFrame(
        {
            "bucket": pd.cut(
                [float(t[key]) for t in trades], bins, right=False
            ).astype(str),
            "gross": [
                float(t["exit_price"]) / float(t["entry_open"]) - 1.0
                for t in trades
            ],
            "net": [base.net_trade_return(t, base.BASE_SLIPPAGE) for t in trades],
            "report_net": [
                _cost_return(t, REPORT_COMMISSION, REPORT_STAMP_TAX, REPORT_SLIPPAGE)
                for t in trades
            ],
        }
    )
    out: dict[str, Any] = {}
    for bucket, group in frame.groupby("bucket", sort=True):
        out[bucket] = {
            "trades": int(len(group)),
            "gross_mean_pct": round(float(group["gross"].mean()) * 100, 2),
            "net_mean_pct": round(float(group["net"].mean()) * 100, 2),
            "report_cost_mean_pct": round(float(group["report_net"].mean()) * 100, 2),
            "median_pct": round(float(group["net"].median()) * 100, 2),
            "win_rate_pct": round(float((group["net"] > 0).mean()) * 100, 2),
        }
    return out


def _subset_period(
    trades: list[dict[str, Any]], start: str, end: str
) -> list[dict[str, Any]]:
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end)
    return [t for t in trades if lo <= t["entry_date"] <= hi]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--start", default=ReferenceConfig.start)
    parser.add_argument("--end", default=ReferenceConfig.end)
    parser.add_argument("--min-adv20", type=float, default=ReferenceConfig.min_adv20)
    parser.add_argument("--pullback-min", type=float, default=ReferenceConfig.pullback_min)
    parser.add_argument("--pullback-max", type=float, default=ReferenceConfig.pullback_max)
    parser.add_argument(
        "--close-position-max", type=float, default=ReferenceConfig.close_position_max
    )
    parser.add_argument("--stop-loss", type=float, default=ReferenceConfig.stop_loss)
    parser.add_argument("--hold-days", type=int, default=ReferenceConfig.hold_days)
    parser.add_argument(
        "--include-one-word",
        action="store_true",
        help="do not exclude one-word limit-up boards",
    )
    args = parser.parse_args()

    if not -1 < args.pullback_min < args.pullback_max < 1:
        raise SystemExit("pullback bounds must satisfy -1 < min < max < 1")
    if not 0 < args.close_position_max <= 1:
        raise SystemExit("close-position-max must be in (0, 1]")
    if not 0 < args.stop_loss < 1:
        raise SystemExit("stop-loss must be a fraction in (0, 1)")
    if args.hold_days < 1:
        raise SystemExit("hold-days must be >= 1")

    cfg = ReferenceConfig(
        start=args.start,
        end=args.end,
        min_adv20=args.min_adv20,
        pullback_min=args.pullback_min,
        pullback_max=args.pullback_max,
        close_position_max=args.close_position_max,
        stop_loss=args.stop_loss,
        hold_days=args.hold_days,
        exclude_one_word=not args.include_one_word,
    )

    print("loading bars ...", flush=True)
    bars = base.load_bars()
    print(f"loaded {len(bars)} symbols", flush=True)

    all_trades: list[dict[str, Any]] = []
    for i, (symbol, frame) in enumerate(bars.items(), 1):
        all_trades.extend(detect_candidates(symbol, frame, cfg))
        if i % 1000 == 0:
            print(
                f"  scanned {i}/{len(bars)} symbols, {len(all_trades)} trades",
                flush=True,
            )
    all_trades.sort(key=lambda t: (t["entry_date"], t["symbol"]))
    all_trades = _subset_period(all_trades, cfg.start, cfg.end)
    print(f"events in {cfg.start}..{cfg.end}: {len(all_trades)}", flush=True)

    report_period = _subset_period(all_trades, "2021-01-01", "2025-12-31")
    design = _subset_period(all_trades, "2021-01-01", "2023-12-31")
    holdout = _subset_period(all_trades, "2024-01-01", "2025-12-31")
    latest = _subset_period(all_trades, "2026-01-01", cfg.end)

    summary: dict[str, Any] = {
        "strategy": "external first-board pullback mechanical shell",
        "source": (
            "华安证券金融工程 2026-03-18 涨停板背后的Alpha："
            "首板回调策略的系统化探索与实证"
        ),
        "config": asdict(cfg),
        "costs": {
            "base_commission_per_side": base.COMMISSION,
            "base_stamp_tax_sell": base.STAMP_TAX,
            "base_slippage": base.BASE_SLIPPAGE,
            "report_commission_per_side": REPORT_COMMISSION,
            "report_stamp_tax_sell": REPORT_STAMP_TAX,
            "report_slippage": REPORT_SLIPPAGE,
        },
        "data": {
            "symbols": len(bars),
            "source": "BaoStock qfq 2016-2024 + local qfq 2025-2026; BSE excluded",
            "limitations": [
                "no PIT industry, sector-heat, 封单, 炸板 or sentiment score",
                "no historical ST list; ST 5% boards are effectively not detected",
                "qfq prices; dividends not separately modeled",
                "2024-12-31 splice; cross-splice entries/exits are dropped",
                "event trades overlap; t-stat is not an independent-sample test",
            ],
        },
        "event_full": base.event_stats(all_trades, base.BASE_SLIPPAGE),
        "event_full_report_cost": report_cost_stats(all_trades),
        "event_report_period_2021_2025": base.event_stats(
            report_period, base.BASE_SLIPPAGE
        ),
        "event_report_period_report_cost": report_cost_stats(report_period),
        "event_design_2021_2023": base.event_stats(design, base.BASE_SLIPPAGE),
        "event_holdout_2024_2025": base.event_stats(holdout, base.BASE_SLIPPAGE),
        "event_latest_2026": base.event_stats(latest, base.BASE_SLIPPAGE),
        "event_yearly_base": base.yearly_event_stats(
            all_trades, base.BASE_SLIPPAGE
        ),
        "event_yearly_report_cost": yearly_report_cost_stats(all_trades),
        "buckets_report_period": {
            "pullback": bucket_stats(
                report_period,
                "pullback_pct",
                [-0.07, -0.05, -0.03, -0.01, 0.01, 0.03],
            ),
            "close_position": bucket_stats(
                report_period,
                "close_position",
                [0.0, 0.2, 0.4, 0.6, 0.8],
            ),
            "pullback_amount_ratio": bucket_stats(
                report_period,
                "pullback_amount_ratio",
                [0.0, 0.3, 0.5, 0.75, 1.0, 1.5, np.inf],
            ),
            "limit_amount_ratio": bucket_stats(
                report_period,
                "limit_amount_ratio",
                [0.0, 1.0, 1.5, 2.0, 3.0, np.inf],
            ),
        },
        "notes": [
            "Score >= 6 and the five-stage sentiment overlay are not replicated; "
            "this is a hard-filter shell, not the report's final strategy.",
            "No portfolio curve is produced because position sizing and ranking "
            "in the report depend on the unavailable score.",
        ],
    }

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(all_trades)
    frame["net_return_pct"] = [
        round(base.net_trade_return(t, base.BASE_SLIPPAGE) * 100, 3)
        for t in all_trades
    ]
    frame["report_net_return_pct"] = [
        round(
            _cost_return(t, REPORT_COMMISSION, REPORT_STAMP_TAX, REPORT_SLIPPAGE)
            * 100,
            3,
        )
        for t in all_trades
    ]
    frame.to_csv(out / "trades.csv", index=False)
    (out / "summary.json").write_text(
        json.dumps(_jsonable(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            _jsonable(
                {
                    "events": len(all_trades),
                    "report_period": summary["event_report_period_2021_2025"],
                    "report_period_report_cost": summary[
                        "event_report_period_report_cost"
                    ],
                    "design": summary["event_design_2021_2023"],
                    "holdout": summary["event_holdout_2024_2025"],
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
