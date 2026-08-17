import unittest
from unittest.mock import patch

import pandas as pd

from backtest.strategies.chan_zero_axis_bt import ChanZeroAxisBacktestStrategy


class ChanZeroAxisBacktestStrategyTest(unittest.TestCase):
    def _frame(self):
        close = [10.0] * 48 + [9.0, 11.0, 11.5]
        return pd.DataFrame(
            {
                "datetime": pd.date_range("2025-01-01", periods=len(close), freq="D"),
                "open": close,
                "high": [value + 0.2 for value in close],
                "low": [value - 0.2 for value in close],
                "close": close,
                "volume": [1000.0] * 49 + [1500.0, 1400.0],
                "is_closed": True,
            }
        )

    def test_enters_only_when_chan_point_and_qualified_cross_align(self):
        frame = self._frame()
        macd = pd.DataFrame(
            {
                "dif": [0.2] * 49 + [0.30, 0.35],
                "dea": [0.2] * 49 + [0.25, 0.30],
                "hist": [0.05] * 48 + [0.10, 0.20, 0.30],
            }
        )
        chan = {
            "signals": [
                {
                    "signal_type": "buy_2",
                    "side": "buy",
                    "confirmed_at": frame["datetime"].iloc[49].isoformat(),
                }
            ]
        }
        strategy = ChanZeroAxisBacktestStrategy(
            {"backtest": {"chan_zero_axis": {"min_confirmations": 2}}}
        )
        with patch("backtest.strategies.chan_zero_axis_bt.calculate_macd", return_value=macd), patch(
            "backtest.strategies.chan_zero_axis_bt.analyze_chan", return_value=chan
        ):
            signals = strategy.generate_signals(frame)
        self.assertEqual("BUY", signals[0]["action"])
        self.assertIn("buy_2", signals[0]["reason"])
        self.assertIn("above金叉", signals[0]["reason"])

    def test_below_zero_cross_is_excluded_by_default(self):
        frame = self._frame()
        macd = pd.DataFrame(
            {
                "dif": [-0.3] * 49 + [-0.10, -0.05],
                "dea": [-0.2] * 49 + [-0.15, -0.10],
                "hist": [0.05] * 48 + [0.10, 0.20, 0.30],
            }
        )
        chan = {
            "signals": [
                {
                    "signal_type": "buy_1",
                    "side": "buy",
                    "confirmed_at": frame["datetime"].iloc[49].isoformat(),
                }
            ]
        }
        strategy = ChanZeroAxisBacktestStrategy(
            {"backtest": {"chan_zero_axis": {"min_confirmations": 0}}}
        )
        with patch("backtest.strategies.chan_zero_axis_bt.calculate_macd", return_value=macd), patch(
            "backtest.strategies.chan_zero_axis_bt.analyze_chan", return_value=chan
        ):
            self.assertEqual([], strategy.generate_signals(frame))


if __name__ == "__main__":
    unittest.main()
