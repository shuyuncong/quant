import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import first_board_four_strategies_study as four
import forward_low_open as forward

DAYS = pd.bdate_range("2023-01-02", periods=330)
BOARD = 300  # 首板日；次日低开买入
START = DAYS[BOARD - 5]


def set_bar(panels, day, symbol, open_, close):
    panels["open"].loc[DAYS[day], symbol] = open_
    panels["close"].loc[DAYS[day], symbol] = close
    panels["high"].loc[DAYS[day], symbol] = max(open_, close)
    panels["low"].loc[DAYS[day], symbol] = min(open_, close)


def study_panels() -> dict[str, pd.DataFrame]:
    """000001：首板→低开买→次日卖；000002：同日买，次日涨停多拿一天；
    600000：晚一天出信号，那时账户还有持仓。"""
    symbols = ["000001", "000002", "600000"]
    panels = {
        field: pd.DataFrame(9.5, index=DAYS, columns=symbols)
        for field in ("open", "high", "low", "close")
    }
    panels["volume"] = pd.DataFrame(1e6, index=DAYS, columns=symbols)
    panels["amount"] = pd.DataFrame(9.5e6, index=DAYS, columns=symbols)
    panels["turnover_rate"] = pd.DataFrame(np.nan, index=DAYS, columns=symbols)
    # 60 日区间 9–12，首板收盘 10.45 落在下半区。
    panels["high"].loc[DAYS[BOARD - 30]] = 12.0
    panels["low"].loc[DAYS[BOARD - 30]] = 9.0

    low_open = 10.45 * 0.965
    for symbol, board in (("000001", BOARD), ("000002", BOARD), ("600000", BOARD + 1)):
        set_bar(panels, board, symbol, 9.5, 10.45)
        set_bar(panels, board + 1, symbol, low_open, 10.2)
    set_bar(panels, BOARD + 2, "000001", 10.2, 10.4)
    set_bar(panels, BOARD + 2, "000002", 10.2, 11.22)  # 收盘涨停，不卖
    set_bar(panels, BOARD + 3, "000002", 11.22, 11.0)
    for symbol, last in (("000001", 10.4), ("000002", 11.0), ("600000", 10.2)):
        first = BOARD + (4 if symbol == "000002" else 3)
        for field in ("open", "high", "low", "close"):
            panels[field].loc[DAYS[first]:, symbol] = last
    return panels


def as_of(panels, day, scale=1.0):
    cut = {field: frame.loc[:day] for field, frame in panels.items()}
    for field in ("open", "high", "low", "close"):
        cut[field] = cut[field] * scale
    return four.bundle_from_panels(cut)


def run_daily(panels, days, scale_from=None):
    """每个交易日运行一次，账本每次都经过 JSON 落盘再读回。"""
    ledger = forward.new_ledger(four.INITIAL_CAPITAL, START.strftime("%Y-%m-%d"))
    for day in days:
        scale = 0.5 if scale_from is not None and day >= scale_from else 1.0
        forward.advance(ledger, as_of(panels, day, scale), day.to_pydatetime())
        ledger = json.loads(json.dumps(ledger))
    return ledger


def keys(rows, *names):
    return [tuple(row[name] for name in names) for row in rows]


class ForwardLowOpenTests(unittest.TestCase):
    def test_daily_runs_reproduce_the_backtest_trades(self):
        panels = study_panels()
        bundle = four.bundle_from_panels(panels)
        portfolio, equity, _, _ = four.simulate(
            bundle, four.build_signals(bundle), forward.STRATEGY,
            str(START.date()), str(DAYS[-1].date()), max_weight=forward.MAX_WEIGHT,
        )
        expected = [
            (t.symbol, str(t.entry_date.date()), str(t.exit_date.date()), t.qty, t.net_pnl)
            for t in portfolio.trades
        ]
        self.assertEqual([row[0] for row in expected], ["000001", "000002"])

        ledger = run_daily(panels, DAYS[DAYS > START])

        self.assertEqual(
            keys(ledger["trades"], "symbol", "entry_date", "exit_date", "qty", "net_pnl"), expected
        )
        self.assertEqual(ledger["trades"][1]["hold_days"], 2)
        self.assertAlmostEqual(ledger["nav_history"][-1]["equity"], float(equity.iloc[-1]), places=2)
        self.assertEqual(ledger["positions"], [])
        self.assertEqual(
            keys(ledger["signals"], "symbol", "action"),
            [("000001", "bought"), ("000002", "bought"), ("600000", "holding")],
        )
        self.assertFalse(any(row["backfilled"] for row in ledger["trades"]))

    def test_position_is_marked_at_the_close_on_the_buy_day(self):
        # 开盘 10.08 买入、当天收盘 10.2：持仓的现价是收盘价，和权益用的是同一个价。
        ledger = run_daily(study_panels(), DAYS[(DAYS > START) & (DAYS <= DAYS[BOARD + 1])])

        self.assertEqual(
            keys(ledger["positions"], "symbol", "mark_price"), [("000001", 10.2), ("000002", 10.2)]
        )
        self.assertAlmostEqual(
            ledger["cash"] + sum(row["qty"] * row["mark_price"] for row in ledger["positions"]),
            ledger["nav_history"][-1]["equity"],
            delta=0.01,
        )

    def test_one_catch_up_run_equals_daily_runs(self):
        panels = study_panels()
        daily = run_daily(panels, DAYS[DAYS > START])

        late = forward.new_ledger(four.INITIAL_CAPITAL, START.strftime("%Y-%m-%d"))
        forward.advance(late, four.bundle_from_panels(panels), datetime(2024, 6, 1))

        for table, names in (
            ("trades", ("symbol", "entry_date", "exit_date", "qty", "net_pnl")),
            ("nav_history", ("date", "cash", "equity", "positions")),
            ("signals", ("date", "symbol", "action")),
        ):
            self.assertEqual(keys(late[table], *names), keys(daily[table], *names), table)
        self.assertTrue(all(row["backfilled"] for row in late["nav_history"]))

    def test_restated_history_does_not_change_the_holding_return(self):
        panels = study_panels()
        plain = run_daily(panels, DAYS[DAYS > START])
        # 买入次日 1 拆 2：从那天起缓存里的全部历史价都减半。
        split = run_daily(panels, DAYS[DAYS > START], scale_from=DAYS[BOARD + 2])

        self.assertEqual(keys(split["trades"], "symbol", "exit_date"), keys(plain["trades"], "symbol", "exit_date"))
        for got, want in zip(split["trades"], plain["trades"]):
            self.assertAlmostEqual(got["net_return"], want["net_return"], places=9)
            self.assertAlmostEqual(got["qty"], want["qty"] * 2)
        self.assertAlmostEqual(
            split["nav_history"][-1]["equity"], plain["nav_history"][-1]["equity"], places=2
        )

    def test_half_refreshed_cache_is_rejected(self):
        panels = study_panels()
        for field in ("open", "high", "low", "close"):
            panels[field].loc[DAYS[-1], ["000002", "600000"]] = np.nan
        with self.assertRaises(forward.LedgerError):
            forward.check_complete(four.bundle_from_panels(panels))
        forward.check_complete(four.bundle_from_panels(study_panels()))

    def test_watchlist_band_is_exactly_where_the_next_open_signals(self):
        # 首板收盘 10.45：区间两端都不在价位上；11.00：两端（10.56、10.67）正好在价位上。
        for board_close, band in ((10.45, (10.04, 10.13)), (11.0, (10.56, 10.67))):
            panels = study_panels()
            panels["high"].loc[DAYS[BOARD - 30]] = 13.5  # 两种收盘价都落在 60 日区间的下半区
            for symbol in ("000001", "000002"):
                set_bar(panels, BOARD, symbol, 9.5, board_close)

            def until(day):
                return {field: frame.loc[:DAYS[day]] for field, frame in panels.items()}

            def signals(open_):
                panels["open"].loc[DAYS[BOARD + 1], "000001"] = open_
                bundle = four.bundle_from_panels(until(BOARD + 1))
                columns = four.build_signals(bundle)[forward.STRATEGY].get(BOARD + 1, np.empty(0, dtype=int))
                return "000001" in bundle.symbols[columns]

            watch = forward.watchlist(until(BOARD))
            self.assertEqual(
                (watch["as_of"], watch["session"]), (str(DAYS[BOARD].date()), str(DAYS[BOARD + 1].date()))
            )
            self.assertEqual([row["symbol"] for row in watch["rows"]], ["000001", "000002"])
            # 次日 600000 才首板；前两只低开后没再涨停，不再进名单。
            self.assertEqual([row["symbol"] for row in forward.watchlist(until(BOARD + 1))["rows"]], ["600000"])

            row = watch["rows"][0]
            self.assertEqual((row["prev_close"], row["low"], row["high"]), (board_close, *band))
            self.assertTrue(signals(row["low"]) and signals(row["high"]))
            self.assertFalse(signals(round(row["low"] - 0.01, 2)) or signals(round(row["high"] + 0.01, 2)))

    def test_loader_keeps_closed_stock_bars_only(self):
        frame = pd.DataFrame({
            "datetime": ["2026-11-18 00:00:00", "2026-11-19 00:00:00", "2026-11-20 00:00:00"],
            "open": ["10.0", "10.1", "10.2"], "high": [10.2, 10.3, 10.4],
            "low": [9.9, 10.0, 10.1], "close": [10.1, 10.2, 10.3],
            "volume": [1e6, 1e6, 1e6], "amount": [1e7, 1e7, 1e7],
            "is_closed": [True, True, False],
        })
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("000001_qfq.pkl", "510300_qfq.pkl", "200002_qfq.pkl", "600000_none.pkl"):
                frame.to_pickle(root / name)
            panels = forward.load_panels(root)
        self.assertEqual(list(panels["close"].columns), ["000001"])
        self.assertEqual(list(panels["close"].index), list(pd.to_datetime(["2026-11-18", "2026-11-19"])))
        self.assertEqual(panels["open"].iloc[-1, 0], 10.1)

    def test_source_exit_follows_the_1128_and_1450_rules(self):
        day0, day1, day2 = (pd.Timestamp(day) for day in ("2026-11-18", "2026-11-19", "2026-11-20"))

        def bars(rows):
            rows = [(day0, "15:00", 10.0), *rows]  # 买入日收盘 10.00 → 次日涨停价 11.00
            return pd.DataFrame(
                [(day, pd.Timestamp(f"{day.date()} {hhmm}"), price) for day, hhmm, price in rows],
                columns=["date", "datetime", "close"],
            )

        # 11:30 有浮盈、未涨停：上午就卖。
        morning = forward.source_exit(bars([(day1, "11:30", 10.3), (day1, "14:45", 9.0)]), 10.0, 0.10)
        self.assertEqual((morning["time"], morning["price"]), ("11:30", 10.3))
        # 11:30 浮亏不卖，14:45 清仓。
        afternoon = forward.source_exit(bars([(day1, "11:30", 9.8), (day1, "14:45", 9.6)]), 10.0, 0.10)
        self.assertEqual((afternoon["time"], afternoon["price"]), ("14:45", 9.6))
        # 全天封在涨停不卖；第二天涨停价 12.10，上午浮盈卖出。
        sealed = [(day1, "11:30", 11.0), (day1, "14:45", 11.0)]
        held = forward.source_exit(bars([*sealed, (day2, "11:30", 11.5)]), 10.0, 0.10)
        self.assertEqual((held["date"], held["time"]), ("2026-11-20", "11:30"))
        self.assertIsNone(forward.source_exit(bars(sealed), 10.0, 0.10))
        # 同样的价格在 20% 涨跌幅的票上不算涨停，上午就卖。
        self.assertEqual(forward.source_exit(bars(sealed), 10.0, 0.20)["time"], "11:30")

    def test_first_5m_fill_reprices_the_entry_without_touching_the_ledger_return(self):
        gross_entry, gross_exit = 100_000.0, 103_000.0
        exit_fee = four._fee(gross_exit, sell=True)
        net = (gross_exit - exit_fee) / (gross_entry + four._fee(gross_entry)) - 1.0
        row = {"gross_entry": gross_entry, "gross_exit": gross_exit, "exit_fee": exit_fee, "net_return": net}

        # 均价等于开盘价：重算结果回到账本的净收益。
        same = dict(row)
        forward.first_5m_fill(same, {"open": 10.0, "high": 10.0, "volume": 1e5, "amount": 1e6})
        self.assertAlmostEqual(same["vwap_net_return"], net)
        self.assertAlmostEqual(same["first_5m"]["participation"], 0.1)

        # 均价比开盘高 2%：3% 的毛利剩不到 1%。价格基准不同（除权换算）不影响结果。
        for scale in (1.0, 0.5):
            dearer = dict(row)
            bar = {"open": 10.0 * scale, "high": 10.5 * scale, "volume": 1e6 / scale, "amount": 10.2e6}
            forward.first_5m_fill(dearer, bar)
            self.assertAlmostEqual(dearer["first_5m"]["vwap"], 10.2 * scale)
            self.assertAlmostEqual(dearer["vwap_net_return"], (gross_exit - exit_fee) / (102_000.0 * 1.0003) - 1.0)
            self.assertEqual(dearer["net_return"], net)


if __name__ == "__main__":
    unittest.main()
