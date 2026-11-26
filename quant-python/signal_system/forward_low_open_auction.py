"""首板低开 9:25 判信号：给观察名单判当天的信号并推送，再记下 9:30 后实际的成交价。

"低开 3%–4%"要等 9:25 集合竞价撮合完才知道，前一晚的账本只能给到观察名单
（``ledger["watchlist"]``）。这个脚本在 9:25 取名单里每只股票的开盘价，开盘 ÷ 昨收
落在 ``LOW_OPEN`` 里的就是当天的信号，连同账本会买的股数推送到手机。每个交易日
推一条，没有信号也推；休市日（行情日期都不是今天）不推。

和账本对得上靠的是同一套算式：区间判断与 ``build_signals`` 相同，股数直接调用账本
的买入代码。昨收用行情里的：遇除权除息它是除权参考价，和账本晚上用的前复权昨收
一致。多数股票的昨收和名单对不上时，名单不是给今天用的，报错不判。

推送之后继续盯信号股到 9:35，每 3 秒记一次盘口，用来回答"收到推送再下单，实际
能买到什么价"：账本按开盘价 + 10bp 记账，而历史上开盘头 5 分钟均价平均比开盘价
高 0.81%。记录按天写在 ``<账本目录>/auction/<日期>.json``，不改账本。

晚上账本推进之后用 ``--action reconcile`` 对账：当天没有早盘记录，或推送的信号、
股数和账本记的不一致，就以非 0 退出（由 ledger_cron.sh 报警）。对账的输出里带
``fills``：全部早盘记录里，9:30 后的各个价格平均比开盘价高多少。

用法::

    python forward_low_open_auction.py --action check            # 9:25，cron
    python forward_low_open_auction.py --action check --dry-run  # 不推送
    python forward_low_open_auction.py --action reconcile        # 账本推进之后
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, time as clock_time
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np
import pandas as pd

import first_board_four_strategies_study as four
import forward_low_open as forward
import ledger_alert
from utils.helpers import load_config
from utils.time_utils import now_shanghai

Quotes = dict[str, dict[str, Any]]
Fetch = Callable[[list[str]], Quotes]
Notify = Callable[[str, str], dict[str, str]]

# 两只几乎不停牌的大盘股：名单为空或全部停牌时，靠它们判断今天开没开市。
CLOCK = ("600519", "000001")
OPEN_AT = "09:25:00"  # 集合竞价撮合完成；这之前行情里是虚拟撮合价，不能当开盘价
CONTINUOUS = "09:30:00"  # 连续竞价开始
POLL = 1.0  # 等开盘价时的轮询间隔（秒）
GRACE = 10.0  # 第一只出开盘价后，最多再等其余股票这么久（停牌、竞价没成交的不会有）
WAIT = 90.0  # 一只都没有开盘价时最多等这么久
SAMPLE_FROM, SAMPLE_TO, SAMPLE_EVERY = clock_time(9, 29, 55), clock_time(9, 35, 5), 3.0
SNAPSHOT = ("time", "last", "bid", "ask", "volume", "amount", "source")
HTTP_TIMEOUT = 4.0
HALF_TICK = 0.005

# 行情端点不走系统代理（同 config 的 data.disable_system_proxy）。
_OPENER = build_opener(ProxyHandler({}))


# ---------------------------------------------------------------------------
# 实时行情
# ---------------------------------------------------------------------------


def _number(text: str) -> float:
    return float(text) if text.strip() else 0.0


def parse_sina(text: str) -> Quotes:
    """``hq.sinajs.cn``：成交量是股、成交额是元，时间是这只股票最后一个快照的时间。"""

    quotes: Quotes = {}
    for symbol, body in re.findall(r'hq_str_s[hz](\d{6})="([^"]*)"', text):
        field = body.split(",")
        if len(field) < 32:
            continue
        quotes[symbol] = {
            "name": field[0],
            "open": _number(field[1]),
            "prev_close": _number(field[2]),
            "last": _number(field[3]),
            "bid": _number(field[6]),
            "ask": _number(field[7]),
            "volume": _number(field[8]),
            "amount": _number(field[9]),
            "date": field[30],
            "time": field[31],
            "source": "sina",
        }
    return quotes


def parse_tencent(text: str) -> Quotes:
    """``qt.gtimg.cn``：成交量是手（换成股），时间是腾讯刷新这条行情的时间。"""

    quotes: Quotes = {}
    for symbol, body in re.findall(r'v_s[hz](\d{6})="([^"]*)"', text):
        field = body.split("~")
        if len(field) < 38:
            continue
        stamp, deal = field[30], field[35].split("/")
        quotes[symbol] = {
            "name": field[1],
            "open": _number(field[5]),
            "prev_close": _number(field[4]),
            "last": _number(field[3]),
            "bid": _number(field[9]),
            "ask": _number(field[19]),
            "volume": _number(field[6]) * 100.0,
            "amount": _number(deal[2]) if len(deal) == 3 else _number(field[37]) * 1e4,
            "date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}",
            "time": f"{stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}",
            "source": "tencent",
        }
    return quotes


SOURCES = (
    ("https://hq.sinajs.cn/list={codes}", "https://finance.sina.com.cn", parse_sina),
    ("https://qt.gtimg.cn/q={codes}", "https://gu.qq.com", parse_tencent),
)


def fetch_quotes(symbols: list[str]) -> Quotes:
    """按 6 位代码取实时行情：先新浪，取不到或缺的用腾讯补。两个源都没数据抛 LedgerError。"""

    quotes: Quotes = {}
    errors: list[str] = []
    for url, referer, parse in SOURCES:
        missing = [symbol for symbol in symbols if symbol not in quotes]
        if not missing:
            break
        codes = ",".join(f"{'sh' if symbol[0] == '6' else 'sz'}{symbol}" for symbol in missing)
        request = Request(url.format(codes=codes), headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
        try:
            with _OPENER.open(request, timeout=HTTP_TIMEOUT) as response:
                quotes.update(parse(response.read().decode("gbk", "replace")))
        except Exception as exc:  # noqa: BLE001 - 一个源出错就换下一个
            errors.append(f"{parse.__name__}: {type(exc).__name__}: {exc}")
    if not quotes:
        raise forward.LedgerError("取不到实时行情：" + ("；".join(errors) or "两个源都没有返回数据"))
    return quotes


def _opened(quote: dict[str, Any] | None, today: str) -> bool:
    """这条行情里的开盘价是不是今天竞价撮合出来的。"""

    return (
        quote is not None
        and quote["date"] == today
        and quote["time"] >= OPEN_AT
        and quote["open"] > 0
        and quote["prev_close"] > 0
    )


def wait_for_open(
    listed: list[str], today: str, fetch: Fetch, clock: Callable[[], datetime], sleep: Callable[[float], None]
) -> Quotes | None:
    """轮询行情，直到名单里的股票都出了开盘价。今天没开市返回 None。

    停牌或竞价没成交的股票不会有开盘价：第一只出价后最多再等 ``GRACE`` 秒就返回。
    """

    symbols = list(dict.fromkeys([*listed, *CLOCK]))
    target = listed or list(CLOCK)
    start = clock()
    first: datetime | None = None
    quotes: Quotes = {}
    while True:
        error: forward.LedgerError | None = None
        try:
            quotes = fetch(symbols)
        except forward.LedgerError as exc:
            error = exc
        now = clock()
        opened = sum(_opened(quotes.get(symbol), today) for symbol in target)
        if opened == len(target):
            return quotes
        if opened and first is None:
            first = now
        if first is not None and (now - first).total_seconds() >= GRACE:
            return quotes
        if (now - start).total_seconds() >= WAIT:
            if error is not None:
                raise error
            return quotes if any(quote["date"] == today for quote in quotes.values()) else None
        sleep(POLL)


# ---------------------------------------------------------------------------
# 判信号
# ---------------------------------------------------------------------------


def hits(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in record["rows"] if row.get("signal")]


def ledger_buys(cash: float, signals: list[dict[str, Any]], today: str) -> dict[str, int]:
    """账本空仓、现金 ``cash`` 时对这些信号各买多少股。

    用昨收、今开两根 K 线拼一个最小的面板去调账本的买入代码，所以滑点、手续费、
    按 100 股取整都和账本晚上记的一样。
    """

    signals = sorted(signals, key=lambda row: row["symbol"])  # 账本按代码顺序买
    index = pd.DatetimeIndex([pd.Timestamp(today) - pd.Timedelta(days=1), pd.Timestamp(today)])
    prices = pd.DataFrame(
        [[row["prev_close"] for row in signals], [row["open"] for row in signals]],
        index=index,
        columns=[row["symbol"] for row in signals],
    )
    panels = {field: prices for field in ("open", "high", "low", "close")}
    panels["volume"] = panels["amount"] = pd.DataFrame(1.0, index=index, columns=prices.columns)
    panels["turnover_rate"] = pd.DataFrame(np.nan, index=index, columns=prices.columns)
    portfolio = four.StrategyPortfolio(forward.STRATEGY, max_weight=forward.MAX_WEIGHT)
    portfolio.cash = cash
    portfolio.buy_signals(four.bundle_from_panels(panels), 1, np.arange(len(signals)))
    return {symbol: position.qty for symbol, position in portfolio.positions.items()}


def judge(ledger: dict[str, Any], quotes: Quotes, today: str) -> dict[str, Any]:
    """观察名单 + 竞价结束后的行情 → 当天的早盘记录。

    ``rows`` 每只一行：有 9:25 之后行情的带开盘价、``signal``（是否落在低开区间）和
    ``moved``（行情里的昨收是否和名单不同）；账本空仓时，信号行还带账本会买的 ``qty``。
    没有开盘价的股票不判。
    """

    watch = ledger["watchlist"]
    low, high = forward.LOW_OPEN
    rows = []
    for item in watch["rows"]:
        quote = quotes.get(item["symbol"])
        row = {"symbol": item["symbol"], "name": (quote or item).get("name", "")}
        if _opened(quote, today):
            ratio = quote["open"] / quote["prev_close"]
            row.update(
                {key: quote[key] for key in ("open", "prev_close", "bid", "ask", "volume", "amount", "time", "source")},
                open_ratio=ratio,
                signal=low <= ratio <= high,
                moved=abs(quote["prev_close"] - item["prev_close"]) > HALF_TICK,
            )
        rows.append(row)

    seen = [row for row in rows if "open" in row]
    if rows and not seen:
        raise forward.LedgerError(f"名单里的 {len(rows)} 只股票都没有今天 {OPEN_AT} 之后的开盘价")
    moved = [row for row in seen if row["moved"]]
    if len(moved) * 2 > len(seen):
        listed = {item["symbol"]: item["prev_close"] for item in watch["rows"]}
        detail = "、".join(
            f"{row['symbol']} 名单 {listed[row['symbol']]:.2f} 行情 {row['prev_close']:.2f}" for row in moved[:3]
        )
        raise forward.LedgerError(
            f"名单依据 {watch['as_of']} 收盘，但 {len(moved)}/{len(seen)} 只的昨收和行情对不上（{detail}）："
            "名单不是给今天用的"
        )

    flat = not ledger["positions"]
    signals = [row for row in seen if row["signal"]]
    if flat and signals:
        buys = ledger_buys(float(ledger["cash"]), signals, today)
        for row in signals:
            row["qty"] = buys.get(row["symbol"])
    return {"session": today, "as_of": watch["as_of"], "flat": flat, "rows": rows}


def _label(row: dict[str, Any]) -> str:
    return f"{row['name']} {row['symbol']}".strip()


def message(record: dict[str, Any]) -> tuple[str, str]:
    """推送的标题和正文：信号在最前面，然后是没判成的股票。"""

    rows, signals = record["rows"], hits(record)
    day = record["session"][5:]
    if not signals:
        title = f"首板低开 {day}：无信号"
    elif record["flat"]:
        title = f"首板低开 {day}：{len(signals)} 个信号，9:30 买"
    else:
        title = f"首板低开 {day}：{len(signals)} 个信号，账本有持仓不买"

    lines = []
    for row in signals:
        line = f"{_label(row)}：开盘 {row['open']:.2f}，低开 {(1.0 - row['open_ratio']) * 100:.2f}%"
        if row.get("qty"):
            line += f"，账本买 {row['qty']} 股"
        lines.append(line)
    if signals:
        weight = Fraction(min(1.0 / len(signals), forward.MAX_WEIGHT)).limit_denominator(100)
        lines.append(f"规则：空仓才买，每只占总资产 {weight}，按 100 股取整。")
        if any("ST" in row["name"] for row in signals):
            lines.append("带 ST 的建议跳过。")
    seen = [row for row in rows if "open" in row]
    if seen and not signals:
        middle = sum(forward.LOW_OPEN) / 2
        nearest = sorted(seen, key=lambda row: abs(row["open_ratio"] - middle))[:3]
        lines.append(
            "最接近的：" + "、".join(f"{row['name']} {(row['open_ratio'] - 1.0) * 100:+.2f}%" for row in nearest)
        )
    missing = [row for row in rows if "open" not in row]
    if missing:
        lines.append("没取到开盘价，自己看一眼：" + "、".join(_label(row) for row in missing))
    moved = [row for row in seen if row["moved"]]
    if moved:
        lines.append("昨收和名单不同（除权？），按行情里的昨收判断：" + "、".join(_label(row) for row in moved))
    if seen and max(row["time"] for row in seen) >= CONTINUOUS:
        lines.append(f"补判：行情时间 {max(row['time'] for row in seen)}，连续竞价已经开始。")
    lines.append(f"名单 {len(rows)} 只，依据 {record['as_of']} 收盘。")
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# 9:30 后的成交价
# ---------------------------------------------------------------------------


def sample(
    symbols: list[str], today: str, fetch: Fetch, clock: Callable[[], datetime], sleep: Callable[[float], None]
) -> dict[str, list[dict[str, Any]]]:
    """``SAMPLE_FROM`` 到 ``SAMPLE_TO`` 每 ``SAMPLE_EVERY`` 秒记一次盘口。

    行情时间戳没变的快照不重复记；某一次取不到行情就跳过这一次。
    """

    samples: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    while True:
        now = clock()
        if now.time() > SAMPLE_TO:
            return samples
        if now.time() < SAMPLE_FROM:
            sleep((datetime.combine(now.date(), SAMPLE_FROM) - now).total_seconds())
            continue
        try:
            quotes = fetch(symbols)
        except forward.LedgerError:
            quotes = {}
        for symbol, rows in samples.items():
            quote = quotes.get(symbol)
            if quote and quote["date"] == today and (not rows or rows[-1]["time"] != quote["time"]):
                rows.append({key: quote[key] for key in SNAPSHOT})
        sleep(SAMPLE_EVERY)


def fill(auction: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    """一只信号股 9:30 后能买到什么价（都是当日实际价，不复权）。

    ``auction`` 是 9:25 的快照，它的成交量、成交额就是竞价那一笔。

    - ``book_ask``：9:30 前最后一次看到的卖一。
    - ``first``：连续竞价开始后第一个有新成交的快照；``vwap`` 是这段新成交的均价，
      即（累计成交额 − 竞价成交额）÷（累计成交量 − 竞价成交量）。
    - ``vwap_1m`` / ``vwap_5m``：到 9:31 / 9:35 为止的同口径均价，不含竞价那一笔。

    只用和 ``auction`` 同一个行情源的快照：腾讯的成交量按手取整，和新浪的相减会
    凭空多出或少掉几十股。
    """

    def vwap(row: dict[str, Any]) -> float:
        return (row["amount"] - auction["amount"]) / (row["volume"] - auction["volume"])

    samples = [row for row in samples if row["source"] == auction["source"]]
    before = [row for row in samples if row["time"] < CONTINUOUS]
    after = [row for row in samples if row["time"] >= CONTINUOUS and row["volume"] > auction["volume"]]
    result: dict[str, Any] = {"open": auction["open"], "book_ask": (before[-1] if before else auction)["ask"] or None}
    if after:
        result["first"] = {"time": after[0]["time"], "vwap": vwap(after[0]), "ask": after[0]["ask"] or None}
        for key, until in (("vwap_1m", "09:31:00"), ("vwap_5m", "09:35:00")):
            rows = [row for row in after if row["time"] <= until]
            result[key] = vwap(rows[-1]) if rows else None
    return result


# ---------------------------------------------------------------------------
# 早盘任务、夜间对账
# ---------------------------------------------------------------------------


def check(
    state_dir: Path,
    notify: Notify,
    fetch: Fetch = fetch_quotes,
    clock: Callable[[], datetime] = now_shanghai,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    ledger = forward.load_ledger(state_dir)
    watch = ledger.get("watchlist")
    if watch is None or watch["as_of"] != ledger["last_session"]:
        raise forward.LedgerError(f"账本到 {ledger['last_session']}，但没有这一天收盘算出来的观察名单")
    today = clock().date().isoformat()
    path = state_dir / "auction" / f"{today}.json"
    if path.exists():
        raise forward.LedgerError(f"今天已经判过，记录在 {path}")

    quotes = wait_for_open([row["symbol"] for row in watch["rows"]], today, fetch, clock, sleep)
    if quotes is None:
        return {"session": today, "skipped": "休市：行情日期都不是今天"}
    record = judge(ledger, quotes, today)
    record["checked_at"] = clock().isoformat(timespec="seconds")
    title, body = message(record)
    try:
        channels = notify(title, body)
    except Exception as exc:  # noqa: BLE001 - 推送失败也要把 9:30 后的价格记下来
        channels = {"error": f"{type(exc).__name__}: {exc}"}
    record["push"] = {"title": title, "body": body, "channels": channels}

    def save() -> None:
        forward.write_atomic(path, json.dumps(record, ensure_ascii=False, indent=1))

    save()
    # 9:30 之后才判的（手工补跑）已经不是竞价快照，算不出 9:30 后的成交价。
    signals = [row for row in hits(record) if row["time"] < CONTINUOUS]
    if signals:
        record["samples"] = sample([row["symbol"] for row in signals], today, fetch, clock, sleep)
        record["fills"] = {row["symbol"]: fill(row, record["samples"][row["symbol"]]) for row in signals}
        save()
    if "ok" not in channels.values():
        raise forward.LedgerError(f"{title}（没推送出去：{channels}）。记录在 {path}")
    return {
        "session": today,
        "as_of": record["as_of"],
        "flat": record["flat"],
        "listed": len(record["rows"]),
        "push": record["push"],
        "samples": {symbol: len(rows) for symbol, rows in record.get("samples", {}).items()},
        "fills": record.get("fills", {}),
        "record": str(path),
    }


def reconcile(state_dir: Path, today: str) -> dict[str, Any]:
    """账本推进到今天之后：早盘推送的信号、股数和账本记的是否一致。不一致抛 LedgerError。"""

    ledger = forward.load_ledger(state_dir)
    day = ledger["last_session"]
    if day != today:
        return {"session": day, "skipped": "账本今天没有推进，不用对"}
    path = state_dir / "auction" / f"{day}.json"
    if not path.exists():
        raise forward.LedgerError(f"{day} 账本推进了，但没有早盘记录：9:25 的任务没跑，或没跑到推送那一步")
    record = json.loads(path.read_text(encoding="utf-8"))
    pushed = {row["symbol"]: row.get("qty") for row in hits(record)}
    booked: dict[str, Any] = {row["symbol"]: None for row in ledger["signals"] if row["date"] == day}
    booked.update({row["symbol"]: row["qty"] for row in ledger["positions"] if row["entry_date"] == day})
    if pushed != booked:
        raise forward.LedgerError(
            f"{day} 早盘推送和账本不一致（代码: 买入股数，没买为 null）："
            f"推送 {json.dumps(pushed, sort_keys=True)}，账本 {json.dumps(booked, sort_keys=True)}"
        )
    return {"session": day, "signals": pushed}


def fill_stats(state_dir: Path) -> dict[str, Any]:
    """全部早盘记录里，信号股 9:30 后的各个价格平均比开盘价高多少（%）。"""

    fills = [
        item
        for path in sorted((state_dir / "auction").glob("*.json"))
        for item in json.loads(path.read_text(encoding="utf-8")).get("fills", {}).values()
    ]
    prices = {
        "book_ask": [item["book_ask"] for item in fills],
        "first": [item.get("first", {}).get("vwap") for item in fills],
        "vwap_1m": [item.get("vwap_1m") for item in fills],
        "vwap_5m": [item.get("vwap_5m") for item in fills],
    }
    stats: dict[str, Any] = {"signals": len(fills)}
    for key, values in prices.items():
        ratios = [value / item["open"] - 1.0 for value, item in zip(values, fills) if value]
        stats[f"{key}_checked"] = len(ratios)
        stats[f"mean_{key}_vs_open_pct"] = round(float(np.mean(ratios)) * 100, 3) if ratios else None
    return stats


def _notifier(config_path: str, dry_run: bool) -> Notify:
    """先把通知配置读好（要查数据库），到推送时不再等。"""

    config = load_config(config_path)
    if config is None:
        raise forward.LedgerError(f"配置文件加载失败: {config_path}")
    settings = ledger_alert.apply_settings(config, ledger_alert.read_db_settings())
    if dry_run:
        return lambda title, body: {"dry-run": "ok"}
    return lambda title, body: ledger_alert.push(title, body, settings, "low-open-auction")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="首板低开 9:25 判信号、推送，记 9:30 后的成交价")
    parser.add_argument("--action", choices=["check", "reconcile"], required=True)
    parser.add_argument(
        "--state-dir", default=str(forward.DEFAULT_STATE), help="账本目录；早盘记录写在它的 auction/ 下"
    )
    parser.add_argument("--config", default=str(forward.BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="check 时不推送")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    try:
        if args.action == "check":
            data = check(state_dir, _notifier(args.config, args.dry_run))
        else:
            data = {**reconcile(state_dir, now_shanghai().date().isoformat()), "fills": fill_stats(state_dir)}
        print(json.dumps({"ok": True, "data": {"action": args.action, **data}}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
