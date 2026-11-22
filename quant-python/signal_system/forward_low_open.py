"""首板低开·单票≤1/3 的前向纸面账本（不接券商、不下单）。

规则冻结为 2026-11-15 归档、逐笔复现过的版本：选股和账户直接调用回测用的
``first_board_four_strategies_study``（``build_signals`` / ``StrategyPortfolio``），
本文件只负责读每日行情缓存、逐个交易日推进、把信号/成交/净值追加到账本。
回测样本止于 2026-10-30，之后的每一笔都是样本外。

成交价用当日缓存里的前复权价。前复权以最新交易日为基准，所以当日的前复权
开盘/收盘价就是实际成交价；持仓期间遇到除权，历史价被整体重述，下次运行时
把持仓换算到新的价格基准（成本不变）。

附带三项尽力而为的诊断，失败不影响账本，下次运行自动补：
- 股票名称与 ST 标记（回测没有历史 ST 名单）；
- 按聚宽源码 11:28 止盈 / 14:50 清仓规则、用 15 分钟线重放的卖出价
  （回测用收盘价近似）；
- 买入日第一根 5 分钟线的均价、成交额，以及按该均价买入重算的单笔净收益
  （账本按开盘价成交，但低开幅度要等竞价结束才知道，最早 9:30 才能下单）。

用法::

    python forward_low_open.py --action init
    python forward_low_open.py --action cycle --data-dir /opt/docker/quant/cache/daily_history
    python forward_low_open.py --action status
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import first_board_four_strategies_study as four
from first_board_low_open_15m_study import fetch_bars, first_5m_bar, repriced_net_return

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE = BASE_DIR / "state" / "forward_low_open_1m"
DEFAULT_DATA = BASE_DIR / "cache" / "daily_history"
INTRADAY_CACHE = BASE_DIR / "cache" / "forward_low_open_15m"
FILL_CACHE = BASE_DIR / "cache" / "forward_low_open_5m"
STRATEGY = "s1_first_board_low_open"
STRATEGY_ID = "first_board_low_open_maxweight33"
MAX_WEIGHT = 0.3333333333  # 与归档回测的 --max-weight 一致
SAMPLE_END = "2026-10-30"  # 回测样本终点；账本从下一个交易日开始
HISTORY_BARS = 400  # 60 日相对位置 + 上市满 250 个自然日
COVERAGE = 0.98
REBASE_TOLERANCE = 1e-3
FIELDS = ("open", "high", "low", "close", "volume", "amount")


class LedgerError(RuntimeError):
    """账本或行情不满足推进条件；不改动账本。"""


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------


def load_panels(data_dir: Path) -> dict[str, pd.DataFrame]:
    """读 ``<代码>_qfq.pkl`` 日线缓存，返回最近 ``HISTORY_BARS`` 个交易日的面板。

    缓存里还有 ETF 和 B 股；它们不在回测面板里，按代码首位排除，
    否则会落进首板低开的选股范围。
    """

    series: dict[str, dict[str, pd.Series]] = {field: {} for field in FIELDS}
    for path in sorted(data_dir.glob("*_qfq.pkl")):
        symbol = path.name.split("_")[0]
        if symbol[0] not in "036":
            continue
        frame = pd.read_pickle(path)
        if "is_closed" in frame.columns:
            frame = frame[frame["is_closed"].astype(bool)]
        frame = frame.tail(HISTORY_BARS)
        if frame.empty:
            continue
        index = pd.DatetimeIndex(pd.to_datetime(frame["datetime"])).normalize()
        keep = ~index.duplicated(keep="last")
        for field in FIELDS:
            values = pd.to_numeric(frame[field], errors="coerce").to_numpy("float64")
            series[field][symbol] = pd.Series(values[keep], index=index[keep])
    if not series["close"]:
        raise LedgerError(f"没有日线缓存: {data_dir}")
    panels = {
        field: pd.DataFrame(columns).sort_index().iloc[-HISTORY_BARS:]
        for field, columns in series.items()
    }
    close = panels["close"]
    panels["turnover_rate"] = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    return panels


def check_complete(bundle: four.Bundle) -> None:
    """最新交易日的行情必须已全部落盘；刷新到一半时会漏信号。"""

    previous, latest = np.isfinite(bundle.close[-2:]).sum(axis=1)
    if latest < COVERAGE * previous:
        raise LedgerError(
            f"{bundle.dates[-1].date()} 的日线只有 {latest} 只，上一交易日 {previous} 只；"
            "行情缓存还没刷新完"
        )


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


def new_ledger(capital: float, start: str) -> dict[str, Any]:
    return {
        "strategy_id": STRATEGY_ID,
        "rule": {
            "signal": "昨日首板（非连板）、今日低开 3%–4%、60 日相对位置≤50%、上市≥250 日",
            "buy": "空仓时开盘买入当日全部候选，单票 = min(总资产/候选数, 总资产×max_weight)",
            "sell": "次日起收盘卖出；收盘涨停继续持有，收盘跌停卖不掉；停牌满 60 日按最后价强平",
            "max_weight": MAX_WEIGHT,
            "commission": four.COMMISSION,
            "min_commission": four.MIN_COMMISSION,
            "stamp_tax": four.STAMP_TAX,
            "slippage": four.SLIPPAGE,
        },
        "capital": capital,
        "cash": capital,
        "positions": [],
        "last_session": start,
        "signals": [],
        "trades": [],
        "nav_history": [],
        "updated_at": None,
    }


def load_ledger(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "ledger.json"
    if not path.exists():
        raise LedgerError(f"账本不存在: {path}（先运行 --action init）")
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if ledger.get("strategy_id") != STRATEGY_ID:
        raise LedgerError(f"账本来自其它规则版本: {ledger.get('strategy_id')}")
    return ledger


def save_ledger(state_dir: Path, ledger: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "ledger.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


def _restore(
    ledger: dict[str, Any], bundle: four.Bundle
) -> tuple[four.StrategyPortfolio, list[dict[str, Any]]]:
    portfolio = four.StrategyPortfolio(STRATEGY, max_weight=MAX_WEIGHT)
    portfolio.cash = float(ledger["cash"])
    column_of = {str(symbol): column for column, symbol in enumerate(bundle.symbols)}
    rebased: list[dict[str, Any]] = []
    for row in ledger["positions"]:
        symbol = row["symbol"]
        entry_date = pd.Timestamp(row["entry_date"])
        if symbol not in column_of or entry_date not in bundle.dates:
            raise LedgerError(f"持仓 {symbol}（{row['entry_date']} 买入）不在当前行情里")
        column = column_of[symbol]
        entry_index = int(bundle.dates.get_loc(entry_date))
        basis = float(bundle.open[entry_index, column]) * (1.0 + four.SLIPPAGE)
        if not math.isfinite(basis) or basis <= 0:
            raise LedgerError(f"持仓 {symbol} 买入日 {row['entry_date']} 的开盘价缺失")
        factor = basis / float(row["entry_price"])
        if abs(factor - 1.0) <= REBASE_TOLERANCE:
            factor = 1.0
        else:
            rebased.append({"symbol": symbol, "factor": factor})
        portfolio.positions[symbol] = four.Position(
            symbol=symbol,
            column=column,
            qty=row["qty"] if factor == 1.0 else row["qty"] / factor,
            entry_index=entry_index,
            entry_date=entry_date,
            entry_price=float(row["entry_price"]) * factor,
            entry_cost=float(row["entry_cost"]),
            last_price=float(row["last_price"]) * factor,
            stale_days=int(row["stale_days"]),
        )
    return portfolio, rebased


def _tick(price: float) -> float:
    return math.floor(price * 100.0 + 0.5) / 100.0


def advance(ledger: dict[str, Any], bundle: four.Bundle, now: datetime) -> dict[str, Any]:
    """把账本逐个交易日推进到行情的最新一天；每天的顺序与回测相同（先买后卖再盯市）。"""

    last = pd.Timestamp(ledger["last_session"])
    if last not in bundle.dates:
        raise LedgerError(f"账本停在 {ledger['last_session']}，不在当前行情的交易日里")
    signals = four.build_signals(bundle)[STRATEGY]
    portfolio, rebased = _restore(ledger, bundle)
    stamp = now.isoformat(timespec="seconds")
    sessions: list[dict[str, Any]] = []

    for index in range(int(bundle.dates.get_loc(last)) + 1, len(bundle.dates)):
        day = bundle.dates[index]
        key = day.strftime("%Y-%m-%d")
        record = {"recorded_at": stamp, "backfilled": now.date() > day.date()}
        flat = not portfolio.positions
        columns = signals.get(index, np.empty(0, dtype=int))
        portfolio.buy_signals(bundle, index, columns)
        for column in columns:
            symbol = str(bundle.symbols[column])
            position = portfolio.positions.get(symbol)
            bought = position is not None and position.entry_index == index
            limit = _tick(float(bundle.close[index - 2, column]) * (1.0 + float(bundle.rate[index - 1, column])))
            ledger["signals"].append({
                "date": key,
                "symbol": symbol,
                "open": float(bundle.open[index, column]),
                "prev_close": float(bundle.close_prev[index, column]),
                "open_ratio": float(bundle.open_ratio[index, column]),
                "rp60": float(bundle.rp60[index, column]),
                # 回测用 0.5% 容差认定涨停；这里记下昨收是否正好等于涨停价。
                "prev_close_at_limit": abs(float(bundle.close_prev[index, column]) - limit) < 0.005,
                "action": "bought" if bought else ("holding" if not flat else "unfilled"),
                **record,
            })
        closed = len(portfolio.trades)
        portfolio.sell_positions(bundle, index)
        for trade in portfolio.trades[closed:]:
            row = asdict(trade)
            del row["strategy"]
            row["entry_date"] = trade.entry_date.strftime("%Y-%m-%d")
            row["exit_date"] = trade.exit_date.strftime("%Y-%m-%d")
            ledger["trades"].append({**row, **record})
        equity = portfolio._equity(bundle, index, mark="close")
        ledger["nav_history"].append({
            "date": key,
            "cash": round(portfolio.cash, 2),
            "equity": round(equity, 2),
            "positions": len(portfolio.positions),
            **record,
        })
        ledger["last_session"] = key
        sessions.append({
            "session": key,
            "signals": [str(bundle.symbols[column]) for column in columns],
            "bought": [p.symbol for p in portfolio.positions.values() if p.entry_index == index],
            "sold": [trade.symbol for trade in portfolio.trades[closed:]],
            "equity": round(equity, 2),
        })

    ledger["cash"] = portfolio.cash
    ledger["positions"] = [
        {
            "symbol": position.symbol,
            "qty": position.qty,
            "entry_date": position.entry_date.strftime("%Y-%m-%d"),
            "entry_price": position.entry_price,
            "entry_cost": position.entry_cost,
            "last_price": position.last_price,
            "stale_days": position.stale_days,
        }
        for position in portfolio.positions.values()
    ]
    ledger["updated_at"] = stamp
    return {"sessions": sessions, "rebased": rebased}


# ---------------------------------------------------------------------------
# 诊断：名称 / ST、源码盘中卖点
# ---------------------------------------------------------------------------


def source_exit(bars: pd.DataFrame, avg_cost: float, rate: float) -> dict[str, Any] | None:
    """聚宽源码的卖出规则：11:28 有浮盈且未涨停就卖，14:50 未涨停就卖。

    ``bars`` 是买入日到纸面账本卖出日的不复权 15 分钟线；用 11:30、14:45 两根的
    收盘价近似，涨停价取前一日最后一根收盘价 ×(1+rate)。到账本卖出日仍未触发
    返回 None。
    """

    days = sorted(bars["date"].unique())
    clock = bars["datetime"].dt.strftime("%H:%M")
    for previous, day in zip(days, days[1:]):
        limit = _tick(float(bars.loc[bars["date"] == previous, "close"].iloc[-1]) * (1.0 + rate))
        for hhmm, need_profit in (("11:30", True), ("14:45", False)):
            bar = bars[(bars["date"] == day) & (clock == hhmm)]
            if bar.empty:
                continue
            price = float(bar["close"].iloc[0])
            if price < limit - 0.001 and (price > avg_cost or not need_profit):
                return {"date": pd.Timestamp(day).strftime("%Y-%m-%d"), "time": hhmm, "price": price}
    return None


def _annotate_trade(row: dict[str, Any], bundle: four.Bundle, cache_dir: Path) -> bool:
    if row["exit_reason"] != "signal":
        row["source_exit"] = None
        return True
    symbol = row["symbol"]
    code = f"{'sh' if symbol[0] == '6' else 'sz'}.{symbol}"
    bars = fetch_bars(
        code, row["entry_date"], row["exit_date"], refresh=True, adjustflag="3", cache_dir=cache_dir
    )
    entry_day, exit_day = pd.Timestamp(row["entry_date"]), pd.Timestamp(row["exit_date"])
    if bars.empty or not ((bars["date"] == entry_day).any() and (bars["date"] == exit_day).any()):
        return False  # 分钟线还没入库，下次再补
    first_open = float(bars.loc[bars["date"] == entry_day, "open"].iloc[0])
    row["open_15m"] = first_open
    if abs(first_open * (1.0 + four.SLIPPAGE) / row["entry_price"] - 1.0) > 0.005:
        row["source_exit"] = None  # 除权换算过的持仓，或分钟线与日线不一致
        return True
    column = int(np.flatnonzero(bundle.symbols == symbol)[0])
    exit_ = source_exit(bars, row["entry_price"], float(bundle.rate[-1, column]))
    row["source_exit"] = exit_
    if exit_ is not None:
        gross = exit_["price"] * (1.0 - four.SLIPPAGE) * row["qty"]
        cost = row["gross_entry"] + row["entry_fee"]
        row["source_net_return"] = (gross - four._fee(gross, sell=True)) / cost - 1.0
    return True


def first_5m_fill(row: dict[str, Any], bar: dict[str, float]) -> None:
    """把买入日第一根 5 分钟线记到成交上，并按它的均价重算单笔净收益。

    只用均价 ÷ 第一根开盘价这个比例去调整买入金额，所以持仓被除权换算过也成立。
    账本净值不受影响。
    """

    vwap = bar["amount"] / bar["volume"]
    row["first_5m"] = {
        "open": bar["open"],
        "vwap": vwap,
        "high": bar["high"],
        "amount": bar["amount"],
        "participation": row["gross_entry"] / bar["amount"],
    }
    row["vwap_net_return"] = repriced_net_return(
        row["gross_entry"], row["gross_exit"] - row["exit_fee"], vwap / bar["open"]
    )


def annotate(ledger: dict[str, Any], bundle: four.Bundle, cache_dir: Path = INTRADAY_CACHE) -> dict[str, int]:
    signals = [row for row in ledger["signals"] if "name" not in row]
    trades = [row for row in ledger["trades"] if "source_exit" not in row]
    fills = [row for row in ledger["trades"] if "first_5m" not in row]
    done = {"names": 0, "intraday": 0, "fills": 0, "pending": len(signals) + len(trades) + len(fills)}
    if not done["pending"]:
        return done
    import baostock as bs

    with contextlib.redirect_stdout(io.StringIO()):  # baostock 往 stdout 打登录信息
        login = bs.login()
        if login.error_code != "0":
            raise LedgerError(f"BaoStock 登录失败: {login.error_msg}")
        try:
            for row in signals:
                symbol = row["symbol"]
                result = bs.query_stock_basic(code=f"{'sh' if symbol[0] == '6' else 'sz'}.{symbol}")
                if result.error_code == "0" and result.next():
                    row["name"] = result.get_row_data()[1]
                    row["is_st"] = "ST" in row["name"]
                    done["names"] += 1
            for row in trades:
                done["intraday"] += _annotate_trade(row, bundle, cache_dir)
            for row in fills:
                bar = first_5m_bar(row["symbol"], row["entry_date"], FILL_CACHE, refresh=True)
                if bar is not None:  # 否则分钟线还没入库，下次再补
                    first_5m_fill(row, bar)
                    done["fills"] += 1
        finally:
            bs.logout()
    done["pending"] -= done["names"] + done["intraday"] + done["fills"]
    return done


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _mean_pct(values: list[float]) -> float | None:
    return round(float(np.mean(values)) * 100, 3) if values else None


def status(ledger: dict[str, Any]) -> dict[str, Any]:
    nav = ledger["nav_history"]
    equity = nav[-1]["equity"] if nav else ledger["cash"]
    returns = [row["net_return"] for row in ledger["trades"]]
    paired = [row for row in ledger["trades"] if row.get("source_net_return") is not None]
    filled = [row for row in ledger["trades"] if "first_5m" in row]
    return {
        "last_session": ledger["last_session"],
        "cash": round(ledger["cash"], 2),
        "equity": equity,
        "pnl_pct": round((equity / ledger["capital"] - 1.0) * 100, 2),
        "positions": ledger["positions"],
        "signals": len(ledger["signals"]),
        "trades": len(returns),
        "backfilled_trades": sum(1 for row in ledger["trades"] if row["backfilled"]),
        "win_rate_pct": _mean_pct([float(value > 0) for value in returns]),
        "mean_net_return_pct": _mean_pct(returns),
        "intraday_checked": len(paired),
        "mean_net_return_close_pct": _mean_pct([row["net_return"] for row in paired]),
        "mean_net_return_source_pct": _mean_pct([row["source_net_return"] for row in paired]),
        "fill_checked": len(filled),
        "mean_net_return_open_pct": _mean_pct([row["net_return"] for row in filled]),
        "mean_net_return_vwap_5m_pct": _mean_pct([row["vwap_net_return"] for row in filled]),
        "mean_vwap_5m_vs_open_pct": _mean_pct(
            [row["first_5m"]["vwap"] / row["first_5m"]["open"] - 1.0 for row in filled]
        ),
    }


def _expected_session(config: str) -> date:
    from data.market_data import MarketDataClient
    from utils.helpers import load_config

    return MarketDataClient(load_config(config)).latest_expected_trade_date()


def cycle(state_dir: Path, data_dir: Path, config: str, diagnostics: bool) -> dict[str, Any]:
    ledger = load_ledger(state_dir)
    bundle = four.bundle_from_panels(load_panels(data_dir))
    expected = _expected_session(config)
    if bundle.dates[-1].date() < expected:
        raise LedgerError(f"行情缓存只到 {bundle.dates[-1].date()}，应到 {expected}")
    check_complete(bundle)
    result = advance(ledger, bundle, datetime.now())
    save_ledger(state_dir, ledger)
    if diagnostics:
        try:
            result["diagnostics"] = annotate(ledger, bundle)
        except Exception as exc:  # noqa: BLE001 - 诊断失败不影响账本，下次运行再补
            result["diagnostics"] = {"error": f"{type(exc).__name__}: {exc}"}
        save_ledger(state_dir, ledger)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="首板低开前向纸面账本")
    parser.add_argument("--action", choices=["init", "cycle", "status"], default="status")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA), help="<代码>_qfq.pkl 日线缓存目录")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--capital", type=float, default=four.INITIAL_CAPITAL)
    parser.add_argument("--start", default=SAMPLE_END, help="账本起点（该交易日收盘后空仓）")
    parser.add_argument("--no-diagnostics", action="store_true")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    try:
        payload: dict[str, Any] = {"action": args.action, "state_dir": str(state_dir)}
        if args.action == "init":
            if (state_dir / "ledger.json").exists():
                raise LedgerError(f"账本已存在: {state_dir / 'ledger.json'}")
            save_ledger(state_dir, new_ledger(args.capital, args.start))
        elif args.action == "cycle":
            payload.update(cycle(state_dir, Path(args.data_dir), args.config, not args.no_diagnostics))
        payload["ledger"] = status(load_ledger(state_dir))
        print(json.dumps({"ok": True, "data": payload}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
