import unittest
import numpy as np
import pandas as pd
from strategy.registry import evaluate_strategies, evaluate_exits
from strategy.yearline import add_yearline_indicators, pullback_signal_at


class StrategyRegistryTests(unittest.TestCase):
    def frame(self):
        prices = np.linspace(10, 25, 320) + np.sin(np.arange(320) / 7)
        return pd.DataFrame({"datetime": pd.bdate_range("2024-01-01", periods=320), "open": prices,
                             "high": prices + .3, "low": prices - .3, "close": prices + .1,
                             "volume": np.full(320, 10000), "is_closed": True})

    def test_prefix_matches_as_of_and_future_changes_cannot_change_decision(self):
        frame = self.frame()
        cutoff = str(frame.datetime.iloc[289] + pd.Timedelta(hours=15))
        config = {"macd_divergence": {"enabled": True}}
        expected = evaluate_strategies(frame.iloc[:290], config)
        frame.loc[290:, "close"] = 1000
        self.assertEqual(expected, evaluate_strategies(frame, config, as_of=cutoff))
        self.assertTrue(all(row["status"] == "ok" for row in expected), expected)

    def test_yearline_is_same_rule_as_candidate_pool(self):
        frame = self.frame()
        report = evaluate_strategies(frame, {})[1]
        self.assertEqual(report["buy"], pullback_signal_at(add_yearline_indicators(frame), len(frame)-1) is not None)

    def test_missing_holding_does_not_invent_exit_and_cost_stop_is_reported(self):
        frame = self.frame()
        self.assertIsNone(evaluate_exits(frame, {}, "macd_zero_axis", None)[1])
        checks, sell = evaluate_exits(frame, {}, "macd_zero_axis", {"shares": 100, "cost_price": 100})
        self.assertTrue(sell)
        self.assertIsNone(next(row for row in checks if row["name"] == "持仓超时")["met"])

    def test_unclosed_daily_bar_is_excluded(self):
        frame = self.frame()
        frame.loc[319, "is_closed"] = False
        self.assertEqual(evaluate_strategies(frame, {}), evaluate_strategies(frame.iloc[:-1], {}))

    def test_later_fetch_cannot_add_the_close_of_an_intraday_snapshot(self):
        frame = self.frame()
        cutoff = str(frame.datetime.iloc[-1] + pd.Timedelta(hours=10))
        self.assertEqual(evaluate_strategies(frame.iloc[:-1], {}), evaluate_strategies(frame, {}, as_of=cutoff))
