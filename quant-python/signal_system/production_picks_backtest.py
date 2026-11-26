"""生产环境选出来的股票，按当时的策略规则回放（研究脚本；只读生产数据，不下单）。

选股来源是生产每日扫描落盘的报告（``/opt/docker/quant/output``）：

    scan_<时间>.json                  日线零轴金叉   macd_zero_axis
    scan_yearline_<时间>.json         年线趋势       yearline_pullback
    scan_macd_divergence_<时间>.json  零轴＋底背离   macd_divergence

同一只票同一个信号日只算一次，第一次出现在报告里的时间记为"发布时间"。

回放直接调用 Web 本地回测的 ``research_backtest.replay``：退出规则来自
``strategy.registry``，和生产卖出提醒同源；这里只把"重新算出来的信号"换成
"生产当时真的选出来的票"。每个策略出两组数：

    逐票    每只票单独用一个仓位（账户 × 单票目标仓位）回放，互不占用名额。
            回答"选出来的票各自表现如何"。
    组合    25 万、最多 4 只、单票 25% 的账户按发布顺序回放。名额满了的票直接
            放弃，同一天的候选按代码升序占名额（replay 的既有规则，不按评分）。

每只票配两个同期基准，都是买入日开盘到卖出日（还拿着的算到最新）收盘：沪深300ETF，
和全 A 等权（沪深 A 股里两头都有价格的股票的平均收益，不含费用）。选股范围是
全 A，所以全 A 等权才是"同一段时间随便买"的对照。

买入日（``--entry``）：

    published  发布之后的第一个开盘价。开盘价在 09:25 集合竞价结束时定下，
               之后才扫出来的票只能等下一个交易日。默认。
    signal     信号日的下一个交易日开盘，不管当时报告出没出来（策略的理论口径）。

零轴金叉的候选带执行状态，按买入前最后一次扫描的状态分组：``enabled`` 是生产
认定可入场的；``position_gate_not_ready`` / ``market_gate_closed`` 是被闸门降为
仅观察的；``legacy`` 是 2026-08-21 闸门字段上线前的报告。年线和底背离是研究池，
不分组。

首板低开已有逐日推进的前向纸面账本，这里只读账本汇总，不重算。

用法::

    python production_picks_backtest.py
    python production_picks_backtest.py --entry signal --out /tmp/picks-signal
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

import forward_low_open as low_open
import research_backtest as rb
from strategy.registry import STRATEGIES
from utils.helpers import load_config

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parents[1]
PRODUCTION = Path("/opt/docker/quant")
DEFAULT_OUT = ROOT / "docs" / "backtest-results" / "2026-11-23-production-picks"
ACCOUNT_CASH = 250_000.0  # 每个策略独立 25 万，与生产设置 holdings.total_capital 一致
BENCHMARK = "510300"
AUCTION_CUTOFF = "09:25"
ZERO_AXIS = "macd_zero_axis"
POOLS = {
    ZERO_AXIS: ("scan_[0-9]*.json", "confirmed_at"),
    "yearline_pullback": ("scan_yearline_*.json", "signal_date"),
    "macd_divergence": ("scan_macd_divergence_*.json", "signal_date"),
}


def load_picks(reports_dir: Path) -> list[dict[str, Any]]:
    """每个（策略、代码、信号日）一条；``states`` 是它在各次扫描里的执行状态。"""

    picks: dict[tuple[str, str, str], dict[str, Any]] = {}
    for strategy, (pattern, field) in POOLS.items():
        for path in sorted(reports_dir.glob(pattern)):
            report = json.loads(path.read_text(encoding="utf-8"))
            stamp = str(report["scanned_at"])
            for item in report.get("candidates") or []:
                symbol, signal_date = str(item["symbol"]), str(item[field])[:10]
                pick = picks.setdefault((strategy, symbol, signal_date), {
                    "strategy": strategy,
                    "symbol": symbol,
                    "name": str(item.get("name") or ""),
                    "signal_date": signal_date,
                    "published_at": stamp,
                    "states": [],
                })
                pick["published_at"] = min(pick["published_at"], stamp)
                pick["states"].append((stamp, item.get("execution_mode"), item.get("observe_reason")))
    return sorted(picks.values(), key=lambda pick: (pick["strategy"], pick["published_at"], pick["symbol"]))


def entry_bar(dates: list[str], signal_date: str, published_at: str, mode: str) -> int | None:
    """买入那根日线的下标；None 表示还没有可以买的交易日。"""

    day, clock = published_at[:10], published_at[11:16]
    for index, bar in enumerate(dates):
        if bar <= signal_date:
            continue
        if mode == "signal" or bar > day or (bar == day and clock < AUCTION_CUTOFF):
            return index
    return None


def gate_group(pick: dict[str, Any], entry_day: str) -> str:
    if pick["strategy"] != ZERO_AXIS:
        return "pool"
    cutoff = f"{entry_day}T{AUCTION_CUTOFF}"
    known = [state for state in pick["states"] if state[0] < cutoff] or pick["states"]
    _, mode, reason = max(known, key=lambda state: state[0])
    if mode is None:
        return "legacy"
    return "enabled" if mode == "enabled" else str(reason or mode)


class Market:
    """生产日线缓存（前复权）；沪深300ETF 的交易日当日历。

    ETF 的日线 15:35 就刷新，股票的要到晚上。所有日线都截到最后一个股票行情已经
    刷完的交易日，否则基准会比持仓多算一天，刷到一半时持仓有的按今天、有的按昨天。
    """

    def __init__(self, data_dir: Path, symbols: list[str]):
        self.frames = {symbol: self._read(data_dir / f"{symbol}_qfq.pkl") for symbol in [BENCHMARK, *symbols]}
        if self.frames[BENCHMARK] is None:
            raise SystemExit(f"缺少基准 {BENCHMARK} 的日线: {data_dir}")
        end = self._settled_day(symbols)
        panels = low_open.load_panels(data_dir)
        self.universe_open, self.universe_close = panels["open"].loc[:end], panels["close"].loc[:end]
        self.frames = {
            symbol: None if frame is None else frame[frame["datetime"] <= end].reset_index(drop=True)
            for symbol, frame in self.frames.items()
        }
        self.dates = {
            symbol: frame["datetime"].dt.strftime("%Y-%m-%d").tolist()
            for symbol, frame in self.frames.items()
            if frame is not None
        }
        benchmark = self.frames[BENCHMARK]
        self.calendar = self.dates[BENCHMARK]
        self.benchmark_open = dict(zip(self.calendar, benchmark["open"]))
        self.benchmark_close = dict(zip(self.calendar, benchmark["close"]))

    @staticmethod
    def _read(path: Path) -> pd.DataFrame | None:
        frame = rb.prepare_yearline_bars(pd.read_pickle(path)) if path.exists() else pd.DataFrame()
        return None if frame.empty else frame

    def _settled_day(self, symbols: list[str]) -> pd.Timestamp:
        """当天有日线的入选股票不少于上一交易日的 98%（与首板低开账本同一条规则）。"""

        days = self.frames[BENCHMARK]["datetime"].tolist()
        listed = [set(frame["datetime"].iloc[-20:]) for symbol in symbols if (frame := self.frames[symbol]) is not None]
        for day, previous in zip(reversed(days), reversed(days[:-1])):
            count = sum(day in bars for bars in listed)
            if count and count >= low_open.COVERAGE * sum(previous in bars for bars in listed):
                return day
        raise SystemExit("入选股票最近 20 个交易日都没有刷完的日线")

    def frame(self, symbol: str) -> pd.DataFrame | None:
        return self.frames[symbol]

    def benchmark_return(self, entry_day: str, exit_day: str) -> float | None:
        start, end = self.benchmark_open.get(entry_day), self.benchmark_close.get(exit_day)
        return None if start is None or end is None else (end / start - 1.0) * 100.0

    def universe_return(self, entry_day: str, exit_day: str) -> float | None:
        """全 A 等权：两头都有价格的股票的平均收益（%）。"""

        start, end = pd.Timestamp(entry_day), pd.Timestamp(exit_day)
        if start not in self.universe_open.index or end not in self.universe_close.index:
            return None
        return float((self.universe_close.loc[end] / self.universe_open.loc[start] - 1.0).mean() * 100.0)


def replay_pick(market: Market, config: dict, pick: dict[str, Any], index: int, slot_cash: float) -> dict[str, Any]:
    """一只票单独占一个仓位回放；收益率按实际买入花掉的钱算，不受整手剩余现金影响。"""

    symbol, strategy = pick["symbol"], pick["strategy"]
    dates = market.dates[symbol]
    entry_day = dates[index]
    calendar = [day for day in market.calendar if day >= entry_day]
    options = {"initial_cash": slot_cash, "max_positions": 1, "position_size_pct": 1.0, "start": ""}
    result = rb.replay(
        {symbol: market.frame(symbol)}, {symbol: {strategy: {index - 1}}}, calendar, config, options, strategy
    )
    curve = result["equity_curve"]
    spent = slot_cash - curve[0]["cash"]
    if spent <= 0:
        rejected = result["rejected"]
        return {"status": "unfilled", "exit_reason": rejected[0]["reason"] if rejected else "停牌或买不起一手"}
    row = {"entry_day": entry_day, "entry_price": float(market.frame(symbol)["open"].iloc[index])}
    if result["trades"]:
        trade = result["trades"][0]
        row.update(status="closed", exit_day=trade["exit_day"], exit_price=trade["exit_price"],
                   exit_reason=trade["exit_reason"])
    else:
        # 还拿着：按最后一根收盘价盯市，没有扣卖出费用。
        row.update(status="open", exit_day=dates[-1], exit_price=float(market.frame(symbol)["close"].iloc[-1]),
                   exit_reason="持有中")
    row["bars_held"] = dates.index(row["exit_day"]) - index
    row["return_pct"] = (curve[-1]["equity"] - slot_cash) / spent * 100.0
    row["benchmark_pct"] = market.benchmark_return(entry_day, row["exit_day"])
    row["universe_pct"] = market.universe_return(entry_day, row["exit_day"])
    return row


def replay_account(market: Market, config: dict, strategy: str, rows: pd.DataFrame, options: dict) -> dict[str, Any]:
    """这一组票共用一个账户回放。"""

    signals: dict[str, dict[str, set[int]]] = {}
    for row in rows.itertuples():
        signals.setdefault(row.symbol, {strategy: set()})[strategy].add(int(row.entry_index) - 1)
    histories = {symbol: market.frame(symbol) for symbol in signals}
    calendar = [day for day in market.calendar if day >= rows["entry_day_planned"].min()]
    result = rb.replay(histories, signals, calendar, config, options, strategy)
    metrics = result["metrics"]
    curve = result["equity_curve"]
    return {
        "start": calendar[0],
        "end": calendar[-1],
        "sessions": len(calendar),
        "total_return_pct": metrics["total_return_pct"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "final_equity": metrics["final_equity"],
        "closed_trades": metrics["closed_trades"],
        "win_rate_pct": metrics["win_rate_pct"],
        "open_positions": result["open_positions"],
        "average_positions": sum(point["positions"] for point in curve) / len(curve),
        "benchmark_pct": market.benchmark_return(calendar[0], calendar[-1]),
        "universe_pct": market.universe_return(calendar[0], calendar[-1]),
        "trades": result["trades"],
    }


def describe(rows: pd.DataFrame) -> dict[str, Any]:
    filled = rows[rows["status"].isin(["closed", "open"])]
    closed = filled[filled["status"] == "closed"]
    returns = filled["return_pct"]
    return {
        "picks": len(rows),
        "pending": int((rows["status"] == "pending").sum()),
        "unfilled": int((rows["status"] == "unfilled").sum()),
        "filled": len(filled),
        "closed": len(closed),
        "open": len(filled) - len(closed),
        "mean_return_pct": returns.mean(),
        "median_return_pct": returns.median(),
        "win_rate_pct": (returns > 0).mean() * 100.0,
        "closed_mean_return_pct": closed["return_pct"].mean(),
        "closed_win_rate_pct": (closed["return_pct"] > 0).mean() * 100.0,
        "mean_benchmark_pct": filled["benchmark_pct"].mean(),
        "mean_excess_pct": (returns - filled["benchmark_pct"]).mean(),
        "mean_universe_pct": filled["universe_pct"].mean(),
        "mean_excess_universe_pct": (returns - filled["universe_pct"]).mean(),
        "mean_bars_held": filled["bars_held"].mean(),
        "best_pct": returns.max(),
        "worst_pct": returns.min(),
        "exit_reasons": filled["exit_reason"].value_counts().to_dict(),
    }


def low_open_summary(state_dir: Path) -> dict[str, Any]:
    """首板低开前向账本：净值和持仓浮盈都按账本自己的收盘盯市价。"""

    ledger = low_open.load_ledger(state_dir)
    summary = low_open.status(ledger)
    names = {row["symbol"]: row.get("name", "") for row in ledger["signals"]}
    positions = [
        {
            "symbol": row["symbol"],
            "name": names.get(row["symbol"], ""),
            "entry_date": row["entry_date"],
            "return_pct": (row["qty"] * row["mark_price"] / row["entry_cost"] - 1.0) * 100.0,
        }
        for row in summary.pop("positions")
    ]
    return {
        **summary,
        "capital": ledger["capital"],
        "first_session": ledger["nav_history"][0]["date"],
        "open_positions": positions,
        "trades": [
            {key: row[key] for key in ("symbol", "entry_date", "exit_date", "net_return", "backfilled")}
            for row in ledger["trades"]
        ],
    }


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, float):
        return None if value != value else round(value, 4)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--reports-dir", default=str(PRODUCTION / "output"))
    parser.add_argument("--data-dir", default=str(PRODUCTION / "cache" / "daily_history"))
    parser.add_argument("--ledger-dir", default=str(low_open.DEFAULT_STATE))
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--entry", choices=("published", "signal"), default="published")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config is None:
        raise SystemExit(f"配置读不出来: {args.config}")
    position = config["position"]
    account = {
        "initial_cash": ACCOUNT_CASH,
        "max_positions": int(position["max_stocks"]),
        "position_size_pct": float(position["base_position_per_stock"]),
        "start": "",
    }
    slot_cash = ACCOUNT_CASH * account["position_size_pct"]
    picks = load_picks(Path(args.reports_dir))
    market = Market(Path(args.data_dir), sorted({pick["symbol"] for pick in picks}))

    rows = []
    for pick in picks:
        row = {key: pick[key] for key in ("strategy", "symbol", "name", "signal_date", "published_at")}
        frame = market.frame(pick["symbol"])
        index = None if frame is None else entry_bar(
            market.dates[pick["symbol"]], pick["signal_date"], pick["published_at"], args.entry
        )
        if index is None:
            # 没有日线，或者发布之后还没有开过盘。
            row.update(group=gate_group(pick, "9999-12-31"), status="pending")
        else:
            planned = market.dates[pick["symbol"]][index]
            row.update(group=gate_group(pick, planned), entry_index=index, entry_day_planned=planned)
            row.update(replay_pick(market, config, pick, index, slot_cash))
        rows.append(row)
    table = pd.DataFrame(rows)

    summary: dict[str, Any] = {
        "entry": args.entry,
        "data_end": market.calendar[-1],
        "account": {key: account[key] for key in ("initial_cash", "max_positions", "position_size_pct")},
        "groups": [],
    }
    summary["strategies"] = [
        {"strategy": strategy, "name": STRATEGIES[strategy], **describe(part)}
        for strategy, part in table.groupby("strategy", sort=False)
    ]
    for (strategy, group), part in table.groupby(["strategy", "group"], sort=False):
        item = {"strategy": strategy, "name": STRATEGIES[strategy], "group": group, **describe(part)}
        entered = part[part["status"] != "pending"]
        if len(entered):
            item["account"] = replay_account(market, config, strategy, entered, account)
        summary["groups"].append(item)
    summary["first_board_low_open"] = low_open_summary(Path(args.ledger_dir))

    summary = _clean(summary)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    table.drop(columns=["entry_index"]).to_csv(out_dir / "picks.csv", index=False, float_format="%.4f")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"entry={args.entry}  data_end={summary['data_end']}  out={out_dir}")
    header = ("strategy", "group", "picks", "filled", "closed", "open", "mean%", "median%", "win%",
              "bench%", "excess%", "all_a%", "vs_all_a%", "acct%", "acct_mdd%", "acct_trades", "acct_bench%", "acct_all_a%")
    print("  ".join(header))
    for item in summary["groups"]:
        book = item.get("account") or {}
        cells = [item["strategy"], item["group"], item["picks"], item["filled"], item["closed"], item["open"]]
        cells += [item[key] for key in ("mean_return_pct", "median_return_pct", "win_rate_pct", "mean_benchmark_pct",
                                        "mean_excess_pct", "mean_universe_pct", "mean_excess_universe_pct")]
        cells += [book.get("total_return_pct"), book.get("max_drawdown_pct"),
                  None if not book else book["closed_trades"] + book["open_positions"], book.get("benchmark_pct"),
                  book.get("universe_pct")]
        print("  ".join("-" if cell is None else f"{cell:.2f}" if isinstance(cell, float) else str(cell)
                        for cell in cells))
    ledger = summary["first_board_low_open"]
    print(f"first_board_low_open  equity={ledger['equity']:.2f}  pnl={ledger['pnl_pct']:.2f}%  "
          f"closed={len(ledger['trades'])}  open={len(ledger['open_positions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
