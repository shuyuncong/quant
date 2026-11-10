import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import first_board_pullback_study as study


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
    volume[t] = 12_000_000.0

    close[t + 1] = 10.8
    open_[t + 1] = 11.0
    high[t + 1] = 11.0
    low[t + 1] = 10.7
    amount[t + 1] = 30_000_000.0

    close[t + 2] = 10.9
    open_[t + 2] = 10.9
    for i in range(t + 3, 300):
        close[i] = 11.0
        open_[i] = 11.0
        high[i] = 11.1
        low[i] = 10.9

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


class FirstBoardPullbackTests(unittest.TestCase):
    def setUp(self):
        self.signal_cfg = study.SignalConfig()
        self.exit_cfg = study.ExitConfig()
        self.bars = make_bars()

    def detect(self, bars=None):
        return study.detect_candidates(
            "600000", bars if bars is not None else self.bars, self.signal_cfg, self.exit_cfg
        )

    def test_detects_shrinking_pullback_and_enters_next_open(self):
        trades = self.detect()
        self.assertEqual(1, len(trades))
        trade = trades[0]
        self.assertEqual(pd.Timestamp("2020-12-30"), trade["limit_date"])
        self.assertEqual(pd.Timestamp("2021-01-01"), trade["entry_date"])
        self.assertAlmostEqual(10.9, trade["entry_open"])
        self.assertEqual("timeout", trade["exit_reason"])
        self.assertEqual(5, trade["hold_days"])

    def test_prior_limit_up_blocks_first_board(self):
        bars = self.bars.copy()
        t = 255
        bars.loc[t, "close"] = 11.0
        bars.loc[t, "high"] = 11.0
        bars.loc[t, "open"] = 10.0
        bars.loc[t, "amount"] = 150_000_000.0
        self.assertEqual([], self.detect(bars))

    def test_pullback_must_shrink_volume(self):
        bars = self.bars.copy()
        bars.loc[261, "amount"] = 100_000_000.0
        bars.loc[262, "amount"] = 100_000_000.0
        self.assertEqual([], self.detect(bars))

    def test_too_deep_pullback_is_rejected(self):
        bars = self.bars.copy()
        bars.loc[261, "close"] = 10.0
        self.assertEqual([], self.detect(bars))

    def test_stop_loss_exits_next_open(self):
        bars = self.bars.copy()
        bars.loc[262, "close"] = 10.0  # entry 10.9; -5% = 10.355
        trades = self.detect(bars)
        self.assertEqual(1, len(trades))
        self.assertEqual("stop", trades[0]["exit_reason"])
        self.assertEqual(1, trades[0]["hold_days"])


if __name__ == "__main__":
    unittest.main()
