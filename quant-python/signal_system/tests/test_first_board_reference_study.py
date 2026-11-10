import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import first_board_reference_study as study


def make_bars() -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-01", periods=300)
    close = np.full(300, 10.0)
    open_ = np.full(300, 10.0)
    high = np.full(300, 10.1)
    low = np.full(300, 9.9)
    amount = np.full(300, 50_000_000.0)
    volume = np.full(300, 5_000_000.0)

    t = 260
    close[t] = 11.0
    open_[t] = 10.5
    high[t] = 11.0
    low[t] = 10.4
    amount[t] = 150_000_000.0

    close[t + 1] = 10.8
    open_[t + 1] = 10.9
    high[t + 1] = 11.0
    low[t + 1] = 10.7
    amount[t + 1] = 30_000_000.0

    close[t + 2] = 10.85
    open_[t + 2] = 10.9
    high[t + 2] = 10.95
    low[t + 2] = 10.8
    amount[t + 2] = 40_000_000.0

    for i in range(t + 3, 300):
        close[i] = 10.9
        open_[i] = 10.9
        high[i] = 11.0
        low[i] = 10.8

    return pd.DataFrame(
        {
            "datetime": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


def make_ride_bars() -> pd.DataFrame:
    bars = make_bars()
    t = 260
    # T-1 closes at -7% versus the limit close; T then seals a 10% limit.
    bars.loc[t + 1, "close"] = 10.23
    bars.loc[t + 1, "open"] = 10.5
    bars.loc[t + 1, "high"] = 10.5
    bars.loc[t + 1, "low"] = 10.0
    bars.loc[t + 1, "amount"] = 30_000_000.0

    bars.loc[t + 2, "open"] = 10.3
    bars.loc[t + 2, "high"] = 11.253
    bars.loc[t + 2, "low"] = 10.3
    bars.loc[t + 2, "close"] = 11.253
    bars.loc[t + 2, "amount"] = 60_000_000.0

    bars.loc[t + 3, "open"] = 11.5
    bars.loc[t + 3, "high"] = 12.3783
    bars.loc[t + 3, "low"] = 11.4
    bars.loc[t + 3, "close"] = 12.3783
    bars.loc[t + 3, "amount"] = 60_000_000.0

    bars.loc[t + 4, "open"] = 12.2
    bars.loc[t + 4, "high"] = 12.3
    bars.loc[t + 4, "low"] = 11.9
    bars.loc[t + 4, "close"] = 12.0
    bars.loc[t + 4, "amount"] = 60_000_000.0

    bars.loc[t + 5, "open"] = 12.1
    bars.loc[t + 5, "high"] = 12.2
    bars.loc[t + 5, "low"] = 12.0
    bars.loc[t + 5, "close"] = 12.1
    return bars


class FirstBoardReferenceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = study.ReferenceConfig()
        self.bars = make_bars()

    def detect(self, bars=None, cfg=None):
        return study.detect_candidates(
            "600000",
            bars if bars is not None else self.bars,
            cfg if cfg is not None else self.cfg,
        )

    def test_detects_external_pullback_and_exits_next_open(self):
        trades = self.detect()
        self.assertEqual(1, len(trades))
        trade = trades[0]
        self.assertEqual(pd.Timestamp("2020-12-30"), trade["limit_date"])
        self.assertEqual(pd.Timestamp("2020-12-31"), trade["signal_date"])
        self.assertEqual(pd.Timestamp("2021-01-01"), trade["entry_date"])
        self.assertEqual(pd.Timestamp("2021-01-04"), trade["exit_date"])
        self.assertAlmostEqual(10.9, trade["entry_open"])
        self.assertAlmostEqual(-0.0181818, trade["pullback_pct"], places=6)
        self.assertEqual("one_day", trade["exit_reason"])
        self.assertEqual(1, trade["hold_days"])

    def test_prior_limit_up_blocks_first_board(self):
        bars = self.bars.copy()
        t = 255
        bars.loc[t, "close"] = 11.0
        bars.loc[t, "high"] = 11.0
        bars.loc[t, "open"] = 10.5
        bars.loc[t, "amount"] = 150_000_000.0
        self.assertEqual([], self.detect(bars))

    def test_pullback_above_plus_three_percent_is_rejected(self):
        bars = self.bars.copy()
        bars.loc[261, "close"] = 11.4
        bars.loc[261, "high"] = 11.5
        bars.loc[261, "low"] = 10.0
        cfg = study.ReferenceConfig(close_position_max=1.0)
        self.assertEqual([], self.detect(bars, cfg))

    def test_pullback_below_minus_seven_percent_is_rejected(self):
        bars = self.bars.copy()
        bars.loc[261, "close"] = 10.2
        bars.loc[261, "high"] = 10.8
        bars.loc[261, "low"] = 10.0
        self.assertEqual([], self.detect(bars))

    def test_high_close_position_is_rejected(self):
        bars = self.bars.copy()
        bars.loc[261, "close"] = 10.95
        bars.loc[261, "high"] = 11.0
        bars.loc[261, "low"] = 10.7
        self.assertEqual([], self.detect(bars))

    def test_one_word_board_is_excluded_by_default(self):
        bars = self.bars.copy()
        t = 260
        bars.loc[t, "open"] = 11.0
        bars.loc[t, "high"] = 11.0
        bars.loc[t, "low"] = 11.0
        bars.loc[t, "close"] = 11.0
        self.assertEqual([], self.detect(bars))
        cfg = study.ReferenceConfig(exclude_one_word=False)
        self.assertEqual(1, len(self.detect(bars, cfg)))

    def test_ride_after_limit_up_close(self):
        trades = self.detect(make_ride_bars())
        self.assertEqual(1, len(trades))
        trade = trades[0]
        self.assertEqual(pd.Timestamp("2021-01-06"), trade["exit_date"])
        self.assertEqual("ride_end", trade["exit_reason"])
        self.assertEqual(3, trade["hold_days"])
        self.assertAlmostEqual(12.1, trade["exit_price"])

    def test_stop_loss_exits_next_open(self):
        bars = self.bars.copy()
        bars.loc[262, "close"] = 10.3
        bars.loc[262, "high"] = 10.4
        bars.loc[262, "low"] = 10.2
        trades = self.detect(bars)
        self.assertEqual(1, len(trades))
        self.assertEqual("stop", trades[0]["exit_reason"])
        self.assertEqual(1, trades[0]["hold_days"])


if __name__ == "__main__":
    unittest.main()
