import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import forward_low_open as forward
import forward_low_open_auction as auction
from test_forward_low_open import BOARD, DAYS, START, as_of, run_daily, study_panels

TODAY = str(DAYS[BOARD + 1].date())  # 000001、000002 首板的次日
# 2026-11-25 收盘后两个源返回的同一只股票（盘口只留前几档的原样，其余照抄）。
SINA = (
    'var hq_str_sh603725="天安新材,9.760,10.090,9.080,10.020,9.080,0.000,9.080,28091072,268606287.000,'
    "0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,569800,9.080,18799,9.090,45700,9.100,21100,9.120,6600,9.130,"
    '2026-11-25,15:34:59,00,D|7900|71732.00";\nvar hq_str_sz999999="";\n'
)
TENCENT = (
    'v_sh603725="1~天安新材~603725~9.08~10.09~9.76~280911~111252~169658~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~'
    "9.08~5698~9.09~188~9.10~457~9.12~211~9.13~66~~20261125161459~-1.01~-10.01~10.02~9.08~"
    '9.08/280911/268606287~280911~26861~9.21~54.34";\n'
)


def quote(open_, prev_close, time="09:25:03", date=TODAY, **extra):
    return {
        "name": "测试", "open": open_, "prev_close": prev_close, "last": open_, "bid": open_, "ask": open_ + 0.01,
        "volume": 50_000.0, "amount": 50_000.0 * open_, "date": date, "time": time, "source": "sina", **extra,
    }


def ledger_until(day):
    """每天收盘后推进一次、并算好次日名单的账本。"""
    panels = study_panels()
    ledger = run_daily(panels, DAYS[(DAYS > START) & (DAYS <= DAYS[day])])
    ledger["watchlist"] = forward.watchlist({field: frame.loc[:DAYS[day]] for field, frame in panels.items()})
    return ledger, panels


def panel_quotes(panels, day, symbols=("000001", "000002", "600000")):
    return {
        symbol: quote(float(panels["open"].loc[DAYS[day], symbol]), float(panels["close"].loc[DAYS[day - 1], symbol]))
        for symbol in symbols
    }


class Clock:
    """假时钟：sleep 就是拨表。"""

    def __init__(self, start):
        self.now = datetime.fromisoformat(f"{TODAY}T{start}")

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class QuoteParsingTests(unittest.TestCase):
    def test_both_sources_give_the_same_quote_in_shares_and_yuan(self):
        sina = auction.parse_sina(SINA)["603725"]
        tencent = auction.parse_tencent(TENCENT)["603725"]
        self.assertEqual(list(auction.parse_sina(SINA)), ["603725"])  # 查不到的代码返回空串，不算一条行情
        for key, value in {"name": "天安新材", "open": 9.76, "prev_close": 10.09, "last": 9.08,
                           "bid": 0.0, "ask": 9.08, "amount": 268606287.0, "date": "2026-11-25"}.items():
            self.assertEqual((sina[key], tencent[key]), (value, value), key)
        self.assertEqual((sina["volume"], tencent["volume"]), (28091072.0, 28091100.0))  # 腾讯按手报
        self.assertEqual((sina["time"], tencent["time"]), ("15:34:59", "16:14:59"))


class JudgeTests(unittest.TestCase):
    def test_push_matches_what_the_ledger_books_that_night(self):
        ledger, panels = ledger_until(BOARD)
        record = auction.judge(ledger, panel_quotes(panels, BOARD + 1), TODAY)

        self.assertTrue(record["flat"])
        self.assertEqual([row["symbol"] for row in auction.hits(record)], ["000001", "000002"])
        title, body = auction.message(record)
        self.assertIn("2 个信号，9:30 买", title)

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            forward.write_atomic(state / "auction" / f"{TODAY}.json", json.dumps(record))
            forward.advance(ledger, as_of(panels, DAYS[BOARD + 1]), DAYS[BOARD + 1].to_pydatetime())
            forward.save_ledger(state, ledger)
            booked = {row["symbol"]: row["qty"] for row in ledger["positions"]}
            self.assertEqual(auction.reconcile(state, TODAY)["signals"], booked)
            for symbol, qty in booked.items():
                self.assertIn(f"{symbol}：开盘 10.08，低开 3.50%，账本买 {qty} 股", body)

            # 推送的股数和账本不同、当天没有早盘记录：都要报出来。
            record["rows"][0]["qty"] += 100
            forward.write_atomic(state / "auction" / f"{TODAY}.json", json.dumps(record))
            with self.assertRaisesRegex(forward.LedgerError, "不一致"):
                auction.reconcile(state, TODAY)
            (state / "auction" / f"{TODAY}.json").unlink()
            with self.assertRaisesRegex(forward.LedgerError, "没有早盘记录"):
                auction.reconcile(state, TODAY)
            # 账本今天没推进（休市、行情没刷完）：没什么可对的。
            self.assertIn("skipped", auction.reconcile(state, str(DAYS[BOARD + 2].date())))

    def test_signal_while_holding_is_pushed_without_a_quantity(self):
        ledger, panels = ledger_until(BOARD + 1)  # 已持有 000001、000002；600000 今天低开
        today = str(DAYS[BOARD + 2].date())
        quotes = {"600000": quote(float(panels["open"].loc[DAYS[BOARD + 2], "600000"]), 10.45, date=today)}
        record = auction.judge(ledger, quotes, today)

        self.assertEqual([(row["symbol"], row.get("qty")) for row in auction.hits(record)], [("600000", None)])
        title, body = auction.message(record)
        self.assertIn("账本有持仓不买", title)
        self.assertNotIn("账本买", body)

    def test_band_ends_are_signals_and_one_tick_outside_is_not(self):
        ledger, _ = ledger_until(BOARD)
        row = ledger["watchlist"]["rows"][0]  # 昨收 10.45，买入区间 10.04 – 10.13
        for open_, expected in ((10.03, False), (10.04, True), (10.13, True), (10.14, False)):
            record = auction.judge(ledger, {row["symbol"]: quote(open_, row["prev_close"])}, TODAY)
            self.assertEqual(record["rows"][0]["signal"], expected, open_)

    def test_unopened_stocks_are_listed_for_a_manual_look(self):
        ledger, panels = ledger_until(BOARD)
        quotes = panel_quotes(panels, BOARD + 1)
        quotes["000001"] = quote(9.9, 10.45)  # 低开 5.3%，不是信号
        quotes["000002"] = quote(0.0, 10.45, time="09:25:00")  # 停牌：没有开盘价
        record = auction.judge(ledger, quotes, TODAY)

        self.assertEqual(auction.hits(record), [])
        title, body = auction.message(record)
        self.assertIn("无信号", title)
        self.assertIn("最接近的：测试 -5.26%", body)
        self.assertIn("没取到开盘价，自己看一眼：测试 000002", body)
        # 9:25 之前行情里是虚拟撮合价，不能当开盘价判。
        early = {symbol: quote(10.08, 10.45, time="09:24:57") for symbol in ("000001", "000002")}
        with self.assertRaisesRegex(forward.LedgerError, "都没有今天"):
            auction.judge(ledger, early, TODAY)

    def test_list_from_another_day_is_refused_but_one_ex_dividend_stock_is_not(self):
        ledger, panels = ledger_until(BOARD)
        quotes = panel_quotes(panels, BOARD + 1)
        # 一只除权：昨收变成 10.25，按行情里的昨收判（10.08 ÷ 10.25 只低开 1.7%）。
        quotes["000002"] = quote(10.08, 10.25)
        ledger["watchlist"]["rows"].append({"symbol": "600000", "prev_close": 9.5, "low": 9.12, "high": 9.21, "rp60": 0.2})
        record = auction.judge(ledger, quotes, TODAY)
        self.assertEqual([row["symbol"] for row in auction.hits(record)], ["000001"])
        self.assertIn("昨收和名单不同", auction.message(record)[1])
        # 多数对不上：名单是别的日子的。
        quotes["000001"] = quote(10.08, 10.25)
        with self.assertRaisesRegex(forward.LedgerError, "名单不是给今天用的"):
            auction.judge(ledger, quotes, TODAY)


class WaitForOpenTests(unittest.TestCase):
    def run_wait(self, listed, feed):
        """feed(clock 时间) → 行情。返回 (结果, 结束时间)。"""
        clock = Clock("09:25:00")
        result = auction.wait_for_open(listed, TODAY, lambda symbols: feed(clock.now.strftime("%H:%M:%S")), clock, clock.sleep)
        return result, clock.now.strftime("%H:%M:%S")

    def test_returns_as_soon_as_every_listed_stock_has_opened(self):
        def feed(now):
            stamp = "09:25:03" if now >= "09:25:04" else "09:24:58"
            return {symbol: quote(10.08, 10.45, time=stamp) for symbol in ("000001", "000002", *auction.CLOCK)}

        quotes, ended = self.run_wait(["000001", "000002"], feed)
        self.assertEqual(ended, "09:25:04")
        self.assertEqual(quotes["000001"]["time"], "09:25:03")

    def test_suspended_stock_only_holds_the_push_for_the_grace_period(self):
        def feed(now):
            return {"000001": quote(10.08, 10.45), "000002": quote(0.0, 10.45, time="09:25:00")}

        quotes, ended = self.run_wait(["000001", "000002"], feed)
        self.assertEqual(ended, "09:25:10")
        self.assertEqual(quotes["000002"]["open"], 0.0)

    def test_market_holiday_is_skipped_and_a_dead_feed_is_an_error(self):
        yesterday = str(DAYS[BOARD].date())
        stale = {symbol: quote(10.45, 10.0, time="15:00:00", date=yesterday) for symbol in ("000001", *auction.CLOCK)}
        self.assertEqual(self.run_wait(["000001"], lambda now: stale), (None, "09:26:30"))
        # 名单是空的：靠两只大盘股判断开没开市。
        self.assertIsNone(self.run_wait([], lambda now: stale)[0])
        opened = {symbol: quote(10.0, 10.0) for symbol in auction.CLOCK}
        self.assertEqual(self.run_wait([], lambda now: opened), (opened, "09:25:00"))

        def dead(now):
            raise forward.LedgerError("取不到实时行情")

        with self.assertRaisesRegex(forward.LedgerError, "取不到实时行情"):
            self.run_wait(["000001"], dead)


class FillTests(unittest.TestCase):
    def test_fill_prices_exclude_the_auction_print(self):
        at_auction = quote(10.0, 10.36)  # 竞价成交 5 万股、50 万元
        samples = [
            {"time": "09:29:58", "ask": 10.02, "volume": 50_000.0, "amount": 500_000.0},
            # 9:30:00 的快照还只有竞价那一笔：不算连续竞价的成交。
            {"time": "09:30:00", "ask": 10.03, "volume": 50_000.0, "amount": 500_000.0},
            {"time": "09:30:03", "ask": 10.06, "volume": 60_000.0, "amount": 600_500.0},
            {"time": "09:30:57", "ask": 10.10, "volume": 150_000.0, "amount": 1_508_000.0},
            {"time": "09:34:58", "ask": 10.20, "volume": 450_000.0, "amount": 4_560_000.0},
        ]
        samples = [{**row, "source": "sina"} for row in samples]
        # 中途换到腾讯补的一次快照：成交量按手取整，和新浪的不是一个口径，不能相减。
        samples.insert(2, {"time": "09:30:01", "ask": 10.04, "volume": 50_100.0, "amount": 501_400.0, "source": "tencent"})
        got = auction.fill(at_auction, samples)

        self.assertEqual((got["open"], got["book_ask"]), (10.0, 10.02))
        self.assertEqual(got["first"]["time"], "09:30:03")
        self.assertAlmostEqual(got["first"]["vwap"], 10.05)
        self.assertAlmostEqual(got["vwap_1m"], 10.08)
        self.assertAlmostEqual(got["vwap_5m"], 10.15)
        # 9:30 后一笔都没成交：只有盘口。
        self.assertEqual(auction.fill(at_auction, samples[:2]), {"open": 10.0, "book_ask": 10.02})


class CheckTests(unittest.TestCase):
    def run_check(self, state, start, feed, channels=None):
        clock, pushes = Clock(start), []

        def notify(title, body):
            pushes.append((title, body))
            return channels or {"bark": "ok"}

        result = auction.check(state, notify, lambda symbols: feed(clock.now.strftime("%H:%M:%S"), symbols), clock, clock.sleep)
        return result, pushes, clock.now.strftime("%H:%M:%S")

    def feed(self, panels):
        base = panel_quotes(panels, BOARD + 1, ("000001", "000002"))

        def at(now, symbols):
            traded = max(0, (datetime.fromisoformat(f"{TODAY}T{now}") - datetime.fromisoformat(f"{TODAY}T09:30:00")).seconds // 3)
            if now < "09:30:00":
                traded = 0
            rows = {}
            for symbol in symbols:
                row = dict(base.get(symbol) or quote(10.0, 10.0))
                if now >= "09:26:00":  # 连续竞价：每 3 秒成交 1000 股，价格比开盘价高 1%
                    row.update(time=now, volume=row["volume"] + 1000.0 * traded,
                               amount=row["amount"] + 1000.0 * traded * row["open"] * 1.01)
                rows[symbol] = row
            return rows

        return at

    def test_pushes_once_then_records_what_the_signals_traded_at(self):
        ledger, panels = ledger_until(BOARD)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            forward.save_ledger(state, ledger)
            result, pushes, ended = self.run_check(state, "09:25:01", self.feed(panels))

            self.assertEqual(len(pushes), 1)
            self.assertIn("2 个信号，9:30 买", pushes[0][0])
            self.assertGreater(ended, "09:35:00")
            record = json.loads((state / "auction" / f"{TODAY}.json").read_text(encoding="utf-8"))
            self.assertEqual(record["checked_at"], f"{TODAY}T09:25:01")  # 行情一到就推，不等 9:30
            for symbol in ("000001", "000002"):
                fill = record["fills"][symbol]
                self.assertAlmostEqual(fill["vwap_5m"] / fill["open"], 1.01)
                self.assertEqual(fill["first"]["time"][:5], "09:30")
            self.assertEqual(result["fills"], record["fills"])
            self.assertEqual(auction.fill_stats(state)["signals"], 2)
            self.assertAlmostEqual(auction.fill_stats(state)["mean_vwap_5m_vs_open_pct"], 1.0)
            # 一天只判一次。
            with self.assertRaisesRegex(forward.LedgerError, "今天已经判过"):
                self.run_check(state, "09:40:00", self.feed(panels))
            self.assertEqual(json.loads(json.dumps(forward.load_ledger(state))), json.loads(json.dumps(ledger)))

    def test_failed_push_is_an_error_but_the_record_is_kept(self):
        ledger, panels = ledger_until(BOARD)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            forward.save_ledger(state, ledger)
            with self.assertRaisesRegex(forward.LedgerError, "没推送出去"):
                self.run_check(state, "09:25:01", self.feed(panels), channels={"bark": "Bark 错误 code=500"})
            record = json.loads((state / "auction" / f"{TODAY}.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(record["fills"]), ["000001", "000002"])

    def test_holiday_and_stale_watchlist(self):
        ledger, panels = ledger_until(BOARD)
        yesterday = str(DAYS[BOARD].date())
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            forward.save_ledger(state, ledger)
            closed = lambda now, symbols: {s: quote(10.45, 10.0, time="15:00:00", date=yesterday) for s in symbols}
            result, pushes, _ = self.run_check(state, "09:25:01", closed)
            self.assertIn("skipped", result)
            self.assertEqual((pushes, list((state / "auction").glob("*.json"))), ([], []))

            # 前一晚账本推进了但名单没算出来（或账本是旧版本写的）。
            ledger["watchlist"]["as_of"] = str(DAYS[BOARD - 1].date())
            forward.save_ledger(state, ledger)
            with self.assertRaisesRegex(forward.LedgerError, "没有这一天收盘算出来的观察名单"):
                self.run_check(state, "09:25:01", self.feed(panels))


if __name__ == "__main__":
    unittest.main()
