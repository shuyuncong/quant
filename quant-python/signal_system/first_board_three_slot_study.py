"""首板低开 vs 首板缩量回调 V1：同一账户规则下各自回测（研究脚本，不接生产）。

账户规则（两条策略完全相同，各自一个独立的 100 万账户）：
- 最多同时持有 3 只；当日可买候选超过空余仓位的直接放弃，不排队、不补买；
- 单票买入金额 = min(40% × 买入时总资产, 可用现金 / 当日买入只数)，不加杠杆，
  持有期不再平衡；
- 浮亏 8% 硬止损：买入次日起（T+1），最低价触及 成本价 × 0.92 即卖出，
  成交价取 min(开盘价, 止损价)；全天封死跌停卖不掉，顺延到下一个不跌停的开盘；
- 100 股整手；佣金 3bp/边（最低 5 元）、卖出印花税 10bp、滑点 10bp/边。

选股和策略自带的卖出规则不变：
- 首板低开（first_board_four_strategies_study 的 s1）：开盘买，次日收盘卖；
  收盘涨停继续持有，收盘跌停卖不掉。源码只在空仓时买，这里改成有空余仓位就买，
  源码口径作为 flat_only 对照保留。
- 首板缩量回调 V1（first_board_pullback_cycle_study）：开盘买；收盘 -5% 止损 /
  +10% 止盈 / 第 5 根收盘到期，均次日开盘卖，开盘跌停顺延。

同日候选多于空余仓位时的取舍顺序：V1 按首板放量倍数降序（原研究口径）；
首板低开源码没有排序，按代码升序。其它顺序作为敏感性输出。
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

import first_board_four_strategies_study as four
import first_board_pullback_cycle_study as cycle
import first_board_pullback_study as base

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = (
    BASE_DIR.parent.parent
    / "docs"
    / "backtest-results"
    / "2026-11-18-first-board-three-slot-stop8"
)
SLOTS = 3
MAX_WEIGHT = 0.40
HARD_STOP = 0.08
STALE_LIMIT = 60
LIMIT_TOLERANCE = 0.005
PULLBACK_EXIT = base.ExitConfig()
SHUFFLE_RUNS = 200
SHUFFLE_SEED = 20261118

Entry = tuple[int, int, float]  # (交易日下标, 股票列, 同日排序分)


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------


@dataclass
class Market:
    dates: pd.DatetimeIndex
    symbols: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    rate: np.ndarray
    prev_close: np.ndarray
    # 复权口径拼接点之前的最后一根 K 线；持仓不能跨过去。
    last_before_break: np.ndarray | None = None

    def traded(self, index: int, column: int) -> bool:
        close = self.close[index, column]
        volume = self.volume[index, column]
        return bool(math.isfinite(close) and math.isfinite(volume) and volume > 0)

    def move(self, price: float, index: int, column: int) -> float:
        previous = self.prev_close[index, column]
        return float(price / previous - 1.0) if previous > 0 else math.nan

    def limit_up_close(self, index: int, column: int) -> bool:
        """收盘封住涨停：涨幅到了涨停幅度，且收盘价就是当天最高价（只容浮点误差）。"""
        close = self.close[index, column]
        return bool(
            self.move(close, index, column) >= self.rate[index, column] - LIMIT_TOLERANCE
            and math.isclose(close, self.high[index, column], rel_tol=1e-9)
        )

    def limit_down(self, price: float, index: int, column: int) -> bool:
        return bool(
            self.move(price, index, column) <= -self.rate[index, column] + LIMIT_TOLERANCE
        )

    def breaks_after(self, index: int, column: int) -> bool:
        return bool(
            self.last_before_break is not None and self.last_before_break[index, column]
        )


def load_low_open() -> tuple[Market, list[Entry]]:
    bundle = four.load_bundle()
    market = Market(
        dates=bundle.dates,
        symbols=bundle.symbols,
        open=bundle.open,
        high=bundle.high,
        low=bundle.low,
        close=bundle.close,
        volume=bundle.volume,
        rate=bundle.rate,
        prev_close=bundle.close_prev,
    )
    signals = four.build_signals(bundle)["s1_first_board_low_open"]
    entries = [
        (int(index), int(column), 0.0)
        for index, columns in signals.items()
        for column in columns
    ]
    return market, entries


def _entry_only(
    symbol: str,
    bars: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    rates: np.ndarray,
    exit_cfg: base.ExitConfig,
) -> dict[str, Any]:
    """保留每一个能开盘买入的 V1 信号；怎么卖由账户回放决定，不让事件模型先筛掉。"""
    return {"exit_date": pd.Timestamp(bars["datetime"].iloc[entry_idx])}


def load_pullback(start: str, end: str) -> tuple[Market, list[Entry]]:
    bars = base.load_bars()
    variant = next(item for item in cycle.VARIANTS if item.name == "V1")
    found: list[dict[str, Any]] = []
    for symbol, frame in bars.items():
        found.extend(
            cycle.detect(symbol, frame, variant, PULLBACK_EXIT, None, _entry_only)
        )
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    found = [item for item in found if lo <= item["entry_date"] <= hi]

    dates = pd.DatetimeIndex(
        np.unique(np.concatenate([frame["datetime"].to_numpy() for frame in bars.values()]))
    )
    dates = dates[(dates >= lo) & (dates <= hi)]
    symbols = np.array(sorted({item["symbol"] for item in found}))
    shape = (len(dates), len(symbols))
    fields = {
        name: np.full(shape, np.nan)
        for name in ("open", "high", "low", "close", "volume", "prev_close")
    }
    rate = np.empty(shape)
    last_before_break = np.zeros(shape, dtype=bool)
    for column, symbol in enumerate(symbols):
        frame = bars[symbol]
        rows = dates.get_indexer(frame["datetime"])
        keep = rows >= 0
        for name in ("open", "high", "low", "close", "volume"):
            fields[name][rows[keep], column] = frame[name].to_numpy(dtype=float)[keep]
        close = frame["close"].to_numpy(dtype=float)
        fields["prev_close"][rows[keep], column] = np.r_[np.nan, close[:-1]][keep]
        rate[:, column] = base.limit_rate_series(symbol, dates)
        if "splice_break" in frame.columns:
            breaks = frame["splice_break"].fillna(False).to_numpy(dtype=bool)
            last_before_break[rows[keep & np.r_[breaks[1:], False]], column] = True

    column_of = {symbol: column for column, symbol in enumerate(symbols)}
    entries = [
        (
            int(dates.get_loc(item["entry_date"])),
            column_of[item["symbol"]],
            float(item["volume_multiple"]),
        )
        for item in found
    ]
    market = Market(
        dates=dates,
        symbols=symbols,
        rate=rate,
        last_before_break=last_before_break,
        **fields,
    )
    return market, entries


def ranked(market: Market, entries: list[Entry], order: str) -> dict[int, list[int]]:
    """同日候选的取舍顺序。多余的会被放弃，所以顺序本身是规则的一部分。"""
    ordered = sorted(
        entries,
        key=lambda item: str(market.symbols[item[1]]),
        reverse=order == "symbol_desc",
    )
    if order == "score_desc":
        ordered.sort(key=lambda item: -item[2])
    elif order == "score_asc":
        ordered.sort(key=lambda item: item[2])
    elif order not in ("symbol_asc", "symbol_desc"):
        raise ValueError(f"unknown order: {order}")
    by_day: dict[int, list[int]] = {}
    for index, column, _ in ordered:
        by_day.setdefault(index, []).append(column)
    return by_day


# ---------------------------------------------------------------------------
# 账户回放
# ---------------------------------------------------------------------------


@dataclass
class Position:
    column: int
    qty: int
    entry_index: int
    entry_fill: float  # 含滑点的买入价；浮亏以它为成本
    entry_cost: float
    entry_weight: float  # 买入金额 / 买入时总资产
    last_price: float
    bars_held: int = 0  # 持有期内有成交的 K 线数，含买入当日
    stale_days: int = 0
    pending: str | None = None  # 已触发、等下一个能卖的开盘


@dataclass
class Book:
    equity: pd.Series
    exposure: pd.Series
    trades: pd.DataFrame
    counts: Counter


ExitRule = Callable[[Market, int, Position], "tuple[str, str] | None"]


def low_open_exit(market: Market, index: int, position: Position) -> tuple[str, str] | None:
    """次日起收盘清仓；收盘涨停继续持有，收盘跌停卖不掉。"""
    column = position.column
    if index <= position.entry_index:
        return None
    if market.limit_up_close(index, column) or market.limit_down(
        market.close[index, column], index, column
    ):
        return None
    return "close", "signal"


def pullback_exit(market: Market, index: int, position: Position) -> tuple[str, str] | None:
    """V1 冻结出场：收盘 -5% / +10% / 第 5 根收盘到期，均次日开盘卖。"""
    close = market.close[index, position.column]
    reference = market.open[position.entry_index, position.column]
    if close <= reference * (1.0 - PULLBACK_EXIT.stop_loss):
        return "next_open", "stop"
    if close >= reference * (1.0 + PULLBACK_EXIT.take_profit):
        return "next_open", "take_profit"
    if position.bars_held >= PULLBACK_EXIT.hold_days:
        return "next_open", "timeout"
    return None


def _buy_fill(market: Market, index: int, column: int) -> float | None:
    """开盘成交价（含滑点）；开盘涨停、无有效报价、或下一根就换复权口径时放弃。"""
    price = market.open[index, column]
    move = market.move(price, index, column)
    if not (math.isfinite(price) and price > 0 and math.isfinite(move)):
        return None
    if move >= market.rate[index, column] - LIMIT_TOLERANCE:
        return None
    if market.breaks_after(index, column):
        return None
    return float(price) * (1.0 + four.SLIPPAGE)


def run_book(
    market: Market,
    entries: dict[int, list[int]],
    exit_rule: ExitRule,
    start: str,
    end: str,
    slots: int = SLOTS,
    max_weight: float = MAX_WEIGHT,
    hard_stop: float | None = HARD_STOP,
    flat_only: bool = False,
) -> Book:
    first = int(market.dates.searchsorted(pd.Timestamp(start)))
    last = int(market.dates.searchsorted(pd.Timestamp(end), side="right") - 1)
    if last <= first:
        raise ValueError("end date must be after start date")

    cash = four.INITIAL_CAPITAL
    positions: dict[int, Position] = {}
    trades: list[dict[str, Any]] = []
    counts: Counter = Counter()
    equities: list[float] = []
    exposures: list[float] = []

    def sell(position: Position, index: int, raw_price: float, reason: str) -> None:
        nonlocal cash
        price = float(raw_price) * (1.0 - four.SLIPPAGE)
        gross = price * position.qty
        proceeds = gross - four._fee(gross, sell=True)
        cash += proceeds
        trades.append(
            {
                "symbol": str(market.symbols[position.column]),
                "entry_date": market.dates[position.entry_index],
                "exit_date": market.dates[index],
                "entry_price": position.entry_fill,
                "exit_price": price,
                "qty": position.qty,
                "entry_cost": position.entry_cost,
                "entry_weight": position.entry_weight,
                "net_pnl": proceeds - position.entry_cost,
                "net_return": proceeds / position.entry_cost - 1.0,
                "hold_days": index - position.entry_index,
                "exit_reason": reason,
            }
        )
        del positions[position.column]

    for index in range(first, last + 1):
        # 1. 开盘：已触发的卖出；开盘跌停卖不掉，顺延。
        for position in list(positions.values()):
            column = position.column
            price = market.open[index, column]
            if (
                position.pending
                and market.traded(index, column)
                and math.isfinite(price)
                and price > 0
                and not market.limit_down(price, index, column)
            ):
                sell(position, index, price, position.pending)

        # 2. 开盘：买入。最后一个交易日不开新仓。
        if index < last:
            buyable: list[tuple[int, float]] = []
            for column in entries.get(index, ()):
                if column in positions:
                    continue
                fill = _buy_fill(market, index, column)
                if fill is not None:
                    buyable.append((column, fill))
            free = 0 if flat_only and positions else slots - len(positions)
            taken = buyable[: max(free, 0)]
            counts["signals"] += len(buyable)
            counts["dropped"] += len(buyable) - len(taken)
            if taken:
                equity = cash + sum(p.qty * p.last_price for p in positions.values())
                target = min(max_weight * equity, cash / len(taken))
                for column, fill in taken:
                    qty = int(min(target, cash) / fill // four.LOT) * four.LOT
                    while qty >= four.LOT and fill * qty + four._fee(fill * qty) > cash:
                        qty -= four.LOT
                    if qty < four.LOT:
                        counts["unfilled"] += 1
                        continue
                    cost = fill * qty + four._fee(fill * qty)
                    cash -= cost
                    positions[column] = Position(
                        column, qty, index, fill, cost, fill * qty / equity, fill
                    )

        # 3. 盘中：浮亏硬止损（T+1 起）。
        if hard_stop is not None:
            for position in list(positions.values()):
                column = position.column
                if index <= position.entry_index or not market.traded(index, column):
                    continue
                stop_price = position.entry_fill * (1.0 - hard_stop)
                if market.low[index, column] > stop_price:
                    continue
                if market.limit_down(market.high[index, column], index, column):
                    position.pending = position.pending or "hard_stop"
                else:
                    sell(
                        position,
                        index,
                        min(market.open[index, column], stop_price),
                        "hard_stop",
                    )

        # 4. 收盘：策略自带的卖出规则。
        for position in list(positions.values()):
            column = position.column
            if not market.traded(index, column):
                position.stale_days += 1
                if position.stale_days >= STALE_LIMIT:
                    sell(position, index, position.last_price, "stale")
                continue
            close = float(market.close[index, column])
            position.stale_days = 0
            position.last_price = close
            position.bars_held += 1
            if market.breaks_after(index, column):
                sell(position, index, close, "basis_break")
                continue
            if position.pending:
                continue
            action = exit_rule(market, index, position)
            if action is None:
                continue
            when, reason = action
            if when == "close":
                sell(position, index, close, reason)
            else:
                position.pending = reason

        invested = sum(p.qty * p.last_price for p in positions.values())
        equities.append(cash + invested)
        exposures.append(invested / (cash + invested))

    for position in list(positions.values()):
        sell(position, last, position.last_price, "end_of_sample")
    equities[-1] = cash
    exposures[-1] = 0.0

    days = market.dates[first : last + 1]
    return Book(
        equity=pd.Series(equities, index=days),
        exposure=pd.Series(exposures, index=days),
        trades=pd.DataFrame(trades),
        counts=counts,
    )


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def book_stats(book: Book) -> dict[str, Any]:
    equity = book.equity
    returns = equity.pct_change().dropna()
    deviation = float(returns.std(ddof=1))
    pnl = book.trades["net_pnl"]
    gains = float(pnl[pnl > 0].sum())
    losses = float(-pnl[pnl < 0].sum())
    stopped = book.trades.loc[book.trades["exit_reason"] == "hard_stop", "net_return"]
    return {
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1.0),
        "annualized_return": four._annualized(equity),
        "sharpe": float(returns.mean() / deviation * math.sqrt(252.0)) if deviation > 0 else None,
        "max_drawdown": four._drawdown(equity),
        "final_equity": float(equity.iloc[-1]),
        "average_exposure": float(book.exposure.mean()),
        "trades": int(len(pnl)),
        "win_rate": float((pnl > 0).mean()),
        "profit_factor": gains / losses if losses > 0 else None,
        "average_trade_return": float(book.trades["net_return"].mean()),
        "worst_trade_return": float(book.trades["net_return"].min()),
        # 按止损价成交时净收益约 -8.2%；比它低的部分是跳空低开/跌停造成的。
        "hard_stop_average_return": float(stopped.mean()) if len(stopped) else None,
        "average_hold_days": float(book.trades["hold_days"].mean()),
        "signals": int(book.counts["signals"]),
        "dropped": int(book.counts["dropped"]),
        "unfilled": int(book.counts["unfilled"]),
        "exit_reasons": {
            str(reason): int(count)
            for reason, count in book.trades["exit_reason"].value_counts().items()
        },
        "yearly_returns": four._yearly(equity),
    }


def run_variants(
    market: Market,
    entries: list[Entry],
    exit_rule: ExitRule,
    start: str,
    end: str,
    orders: tuple[str, ...],
    extra: dict[str, dict[str, Any]],
) -> dict[str, Book]:
    def run(order: str = "score_desc", **overrides: Any) -> Book:
        return run_book(market, ranked(market, entries, order), exit_rule, start, end, **overrides)

    books = {"main": run(), "no_hard_stop": run(hard_stop=None)}
    books.update({f"order_{order}": run(order) for order in orders})
    books.update({name: run(**overrides) for name, overrides in extra.items()})
    return books


def shuffle_stats(
    market: Market, entries: list[Entry], exit_rule: ExitRule, start: str, end: str
) -> dict[str, Any]:
    """随机打乱同日取舍顺序重跑：放弃谁是任意的，看结果和 8% 止损的作用对它有多敏感。"""
    rng = np.random.default_rng(SHUFFLE_SEED)
    keys = ("total_return", "annualized_return", "sharpe", "max_drawdown")
    rows = []
    for _ in range(SHUFFLE_RUNS):
        shuffled = [(index, column, float(rng.random())) for index, column, _ in entries]
        order = ranked(market, shuffled, "score_desc")
        stopped = book_stats(run_book(market, order, exit_rule, start, end))
        unstopped = book_stats(run_book(market, order, exit_rule, start, end, hard_stop=None))
        rows.append(
            {key: stopped[key] for key in keys}
            | {f"hard_stop_effect_{key}": stopped[key] - unstopped[key] for key in keys}
        )
    frame = pd.DataFrame(rows)
    return {
        "runs": SHUFFLE_RUNS,
        "seed": SHUFFLE_SEED,
        "hard_stop_raised_total_return_share": float(
            (frame["hard_stop_effect_total_return"] > 0).mean()
        ),
        **{
            column: {f"p{q:02d}": float(frame[column].quantile(q / 100.0)) for q in (5, 50, 95)}
            for column in frame.columns
        },
    }


def best_trade_dependence(
    market: Market,
    entries: list[Entry],
    exit_rule: ExitRule,
    start: str,
    end: str,
    book: Book,
    counts: tuple[int, ...] = (3, 5, 10),
) -> dict[str, Any]:
    """去掉贡献最大的几笔信号后重跑账户：单票 40% 时结果有多依赖少数几笔交易。"""
    contribution = (book.trades["entry_weight"] * book.trades["net_return"]).sort_values(
        ascending=False
    )
    column_of = {str(symbol): column for column, symbol in enumerate(market.symbols)}
    result: dict[str, Any] = {
        "top5_share_of_summed_contribution": float(
            contribution.head(5).sum() / contribution.sum()
        )
    }
    for count in counts:
        best = book.trades.loc[contribution.index[:count]]
        removed = {
            (int(market.dates.get_loc(day)), column_of[symbol])
            for day, symbol in zip(best["entry_date"], best["symbol"])
        }
        kept = [item for item in entries if (item[0], item[1]) not in removed]
        stats = book_stats(
            run_book(market, ranked(market, kept, "score_desc"), exit_rule, start, end)
        )
        result[f"without_best_{count}"] = {
            key: stats[key]
            for key in ("total_return", "annualized_return", "sharpe", "max_drawdown")
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def evaluate(
        market: Market,
        entries: list[Entry],
        exit_rule: ExitRule,
        orders: tuple[str, ...],
        extra: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Book], dict[str, Any], dict[str, Any]]:
        books = run_variants(market, entries, exit_rule, args.start, args.end, orders, extra)
        return (
            books,
            shuffle_stats(market, entries, exit_rule, args.start, args.end),
            best_trade_dependence(
                market, entries, exit_rule, args.start, args.end, books["main"]
            ),
        )

    results: dict[str, dict[str, Book]] = {}
    shuffles: dict[str, dict[str, Any]] = {}
    dependence: dict[str, dict[str, Any]] = {}
    print("low open ...", flush=True)
    results["low_open"], shuffles["low_open"], dependence["low_open"] = evaluate(
        *load_low_open(), low_open_exit, ("symbol_desc",), {"flat_only": {"flat_only": True}}
    )
    print("pullback V1 ...", flush=True)
    results["pullback_v1"], shuffles["pullback_v1"], dependence["pullback_v1"] = evaluate(
        *load_pullback(args.start, args.end),
        pullback_exit,
        ("score_asc", "symbol_asc", "symbol_desc"),
        {},
    )

    stats = {
        strategy: {variant: book_stats(book) for variant, book in books.items()}
        for strategy, books in results.items()
    }
    pd.concat(
        [
            books["main"].equity.rename("equity").rename_axis("date").reset_index().assign(strategy=strategy)
            for strategy, books in results.items()
        ]
    )[["date", "strategy", "equity"]].to_csv(out / "curves.csv", index=False)
    pd.concat(
        [books["main"].trades.assign(strategy=strategy) for strategy, books in results.items()]
    ).to_csv(out / "trades.csv", index=False)
    rows = [
        {"strategy": strategy, "variant": variant, **item}
        for strategy, variants in stats.items()
        for variant, item in variants.items()
    ]
    pd.DataFrame(rows).drop(columns=["exit_reasons", "yearly_returns"]).to_csv(
        out / "metrics.csv", index=False
    )
    summary = {
        "period": {"start": args.start, "end": args.end},
        "rules": {
            "initial_capital": four.INITIAL_CAPITAL,
            "slots": SLOTS,
            "max_weight": MAX_WEIGHT,
            "hard_stop": HARD_STOP,
            "sizing": "min(max_weight * equity, cash / buys_today)",
            "costs": "commission 3bp/side (min 5), stamp tax 10bp on sells, slippage 10bp/side",
        },
        "results": stats,
        "random_order": shuffles,
        "best_trade_dependence": dependence,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for strategy, variants in stats.items():
        for variant, item in variants.items():
            print(
                f"{strategy:12s} {variant:18s} total={item['total_return']:+.2%} "
                f"ann={item['annualized_return']:+.2%} sharpe={item['sharpe']:.3f} "
                f"mdd={item['max_drawdown']:.2%} win={item['win_rate']:.2%} "
                f"trades={item['trades']} signals={item['signals']} dropped={item['dropped']} "
                f"exits={item['exit_reasons']}",
                flush=True,
            )
    for strategy, item in shuffles.items():

        def band(key: str, spec: str) -> str:
            return " / ".join(format(item[key][q], spec) for q in ("p05", "p50", "p95"))

        print(
            f"{strategy:12s} random order x{item['runs']} p05/p50/p95: "
            f"total {band('total_return', '+.2%')} | sharpe {band('sharpe', '.3f')} | "
            f"mdd {band('max_drawdown', '.2%')} | hard-stop effect on total "
            f"{band('hard_stop_effect_total_return', '+.2%')}, on mdd "
            f"{band('hard_stop_effect_max_drawdown', '+.2%')}, raised total in "
            f"{item['hard_stop_raised_total_return_share']:.0%} of runs",
            flush=True,
        )
    for strategy, item in dependence.items():
        print(
            f"{strategy:12s} top-5 trades = {item['top5_share_of_summed_contribution']:.0%} "
            "of summed contribution; total without best "
            + ", ".join(
                f"{count}: {item[f'without_best_{count}']['total_return']:+.2%}"
                for count in (3, 5, 10)
            ),
            flush=True,
        )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
