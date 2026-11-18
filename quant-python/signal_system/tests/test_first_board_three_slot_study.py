import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import first_board_three_slot_study as study

SYMBOLS = ("000001", "000002", "000003", "000004")
DAYS = pd.bdate_range("2024-01-02", periods=6)
START, END = str(DAYS[0].date()), str(DAYS[-1].date())
FILL = 10.0 * (1.0 + study.four.SLIPPAGE)
STOP_PRICE = FILL * (1.0 - study.HARD_STOP)


def flat_market() -> study.Market:
    """Every symbol trades at 10.00 all day, every day."""
    shape = (len(DAYS), len(SYMBOLS))
    return study.Market(
        dates=DAYS,
        symbols=np.array(SYMBOLS),
        open=np.full(shape, 10.0),
        high=np.full(shape, 10.0),
        low=np.full(shape, 10.0),
        close=np.full(shape, 10.0),
        volume=np.full(shape, 1_000_000.0),
        rate=np.full(shape, 0.10),
        prev_close=np.full(shape, 10.0),
    )


def set_bar(market, day, open_, high, low, close, column=0):
    market.open[day, column] = open_
    market.high[day, column] = high
    market.low[day, column] = low
    market.close[day, column] = close
    if day + 1 < len(DAYS):
        market.prev_close[day + 1, column] = close


def hold(market, index, position):
    return None


class ThreeSlotAccountTests(unittest.TestCase):
    def test_fourth_same_day_candidate_is_dropped_for_good(self):
        book = study.run_book(
            flat_market(), {0: [0, 1, 2, 3]}, study.low_open_exit, START, END
        )
        # Slots free up on day 1, but an abandoned candidate is never bought later.
        self.assertEqual(["000001", "000002", "000003"], sorted(book.trades["symbol"]))
        self.assertEqual(1, book.counts["dropped"])
        weights = book.trades["entry_cost"] / study.four.INITIAL_CAPITAL
        self.assertTrue(((weights > 0.33) & (weights <= 1 / 3 + 1e-3)).all(), weights.tolist())

    def test_single_name_is_capped_at_forty_percent_and_third_gets_leftover_cash(self):
        book = study.run_book(flat_market(), {0: [0], 1: [1], 2: [2], 3: [3]}, hold, START, END)
        qty = dict(zip(book.trades["symbol"], book.trades["qty"]))
        self.assertEqual(39_900, qty["000001"])  # floor(400,000 / 10.01) in round lots
        self.assertEqual(39_900, qty["000002"])
        self.assertEqual(20_000, qty["000003"])  # only ~20% cash left, no leverage
        self.assertNotIn("000004", qty)  # book is full

    def test_hard_stop_fills_at_the_stop_price_when_touched_intraday(self):
        market = flat_market()
        set_bar(market, 1, open_=10.0, high=10.0, low=9.0, close=9.8)
        book = study.run_book(market, {0: [0]}, hold, START, END)
        trade = book.trades.iloc[0]
        self.assertEqual("hard_stop", trade["exit_reason"])
        self.assertEqual(DAYS[1], trade["exit_date"])
        self.assertAlmostEqual(STOP_PRICE * (1.0 - study.four.SLIPPAGE), trade["exit_price"])

    def test_hard_stop_fills_at_the_open_when_it_gaps_through_the_stop(self):
        market = flat_market()
        set_bar(market, 1, open_=9.1, high=9.3, low=9.1, close=9.2)
        book = study.run_book(market, {0: [0]}, hold, START, END)
        trade = book.trades.iloc[0]
        self.assertEqual("hard_stop", trade["exit_reason"])
        self.assertAlmostEqual(9.1 * (1.0 - study.four.SLIPPAGE), trade["exit_price"])

    def test_entry_day_crash_cannot_be_sold_until_the_next_session(self):
        market = flat_market()
        set_bar(market, 0, open_=10.0, high=10.0, low=9.05, close=9.1)
        set_bar(market, 1, open_=9.1, high=9.1, low=9.1, close=9.1)
        book = study.run_book(market, {0: [0]}, hold, START, END)
        trade = book.trades.iloc[0]
        self.assertEqual(DAYS[1], trade["exit_date"])
        self.assertAlmostEqual(9.1 * (1.0 - study.four.SLIPPAGE), trade["exit_price"])

    def test_locked_limit_down_defers_the_stop_to_the_next_tradable_open(self):
        market = flat_market()
        set_bar(market, 1, open_=9.0, high=9.0, low=9.0, close=9.0)
        set_bar(market, 2, open_=8.5, high=8.6, low=8.4, close=8.5)
        book = study.run_book(market, {0: [0]}, study.low_open_exit, START, END)
        trade = book.trades.iloc[0]
        self.assertEqual("hard_stop", trade["exit_reason"])
        self.assertEqual(DAYS[2], trade["exit_date"])
        self.assertAlmostEqual(8.5 * (1.0 - study.four.SLIPPAGE), trade["exit_price"])


if __name__ == "__main__":
    unittest.main()
