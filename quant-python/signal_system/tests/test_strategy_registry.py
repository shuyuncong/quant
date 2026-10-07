import unittest
import numpy as np
import pandas as pd
from strategy.registry import (DEFAULT_STOP_LOSS_PCT, STRATEGIES, VERSION, evaluate_strategies,
                               evaluate_exits, exit_parameters, resolve_stop_loss)
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

    def test_shipped_defaults_follow_a_changed_global_stop(self):
        from copy import deepcopy
        from utils.helpers import DEFAULT_CONFIG
        config = deepcopy(DEFAULT_CONFIG)
        config["risk"]["stop_loss_pct"] = 0.07
        for strategy_id in STRATEGIES:
            self.assertEqual(resolve_stop_loss(config, strategy_id), (0.07, "global", None))

    def test_later_fetch_cannot_add_the_close_of_an_intraday_snapshot(self):
        frame = self.frame()
        cutoff = str(frame.datetime.iloc[-1] + pd.Timedelta(hours=10))
        self.assertEqual(evaluate_strategies(frame.iloc[:-1], {}), evaluate_strategies(frame, {}, as_of=cutoff))

    def test_strategy_stops_stay_independent_under_one_global_value(self):
        config = {"risk": {"stop_loss_pct": 0.08, "strategy_stop_loss_pct": {
            "macd_zero_axis": 0.05, "yearline_pullback": None, "macd_divergence": 0.12}}}
        stops = {row["strategy_id"]: row["stop_loss"] for row in evaluate_strategies(self.frame(), config)}
        # Without a position every assessment is hypothetical, so it uses its own stop.
        self.assertEqual(stops["macd_zero_axis"]["pct"], 0.05)
        self.assertEqual(stops["yearline_pullback"]["pct"], 0.08)
        self.assertEqual(stops["macd_divergence"]["pct"], 0.12)
        self.assertEqual({stop["source"] for stop in stops.values()}, {"strategy", "global"})
        rules = exit_parameters(config, "yearline_pullback", {"shares": 100, "strategy_id": "macd_zero_axis"})
        self.assertEqual((rules["stop_loss_pct"], rules["stop_loss_owner"], rules["stop_loss_source"]), (0.05, "macd_zero_axis", "strategy"))

    def test_holding_attribution_ignores_the_assessed_strategy(self):
        config = {"risk": {"stop_loss_pct": 0.08, "strategy_stop_loss_pct": {
            "macd_zero_axis": 0.05, "yearline_pullback": 0.04, "macd_divergence": 0.12}}}
        tagged = {"shares": 100, "cost_price": 100, "strategy_id": "macd_divergence"}
        for assessed in ("macd_zero_axis", "yearline_pullback", "macd_divergence"):
            self.assertEqual(resolve_stop_loss(config, assessed, tagged), (0.12, "strategy", "macd_divergence"), assessed)
            rules = exit_parameters(config, assessed, tagged)
            self.assertEqual((rules["stop_loss_pct"], rules["stop_loss_owner"]), (0.12, "macd_divergence"))
        untagged = {"shares": 100, "cost_price": 100}
        for assessed in ("macd_zero_axis", "yearline_pullback", "macd_divergence"):
            self.assertEqual(resolve_stop_loss(config, assessed, untagged), (0.08, "global", None), assessed)
        # An unrecognised owner never borrows another strategy's stop.
        wrong = {"shares": 100, "cost_price": 100, "strategy_id": "buy_1_pullback"}
        self.assertEqual(resolve_stop_loss(config, "macd_divergence", wrong), (0.08, "global", None))
        # Zero shares is a hypothetical assessment, not a held position.
        flat = {"shares": 0, "cost_price": 100, "strategy_id": "macd_divergence"}
        self.assertEqual(resolve_stop_loss(config, "macd_zero_axis", flat), (0.05, "strategy", "macd_zero_axis"))

    def test_null_override_inherits_and_follows_global_updates(self):
        for inherited in ({}, {"macd_zero_axis": None}, {"macd_zero_axis": None, "yearline_pullback": 0.2}):
            config = {"risk": {"stop_loss_pct": 0.08, "strategy_stop_loss_pct": inherited}}
            self.assertEqual(resolve_stop_loss(config, "macd_zero_axis")[:2], (0.08, "global"), inherited)
            config["risk"]["stop_loss_pct"] = 0.03
            self.assertEqual(resolve_stop_loss(config, "macd_zero_axis")[:2], (0.03, "global"), inherited)
            self.assertEqual(resolve_stop_loss(config, "yearline_pullback")[0], inherited.get("yearline_pullback") or 0.03)
        # A null override on a held position inherits the same way.
        config = {"risk": {"stop_loss_pct": 0.06, "strategy_stop_loss_pct": {"macd_divergence": None}}}
        self.assertEqual(resolve_stop_loss(config, "macd_zero_axis", {"shares": 1, "strategy_id": "macd_divergence"}), (0.06, "global", None))
        # The shipped seeds must not shadow a changed global: every strategy that only
        # inherits moves with stop_loss_pct, and an explicit override does not.
        config = {"risk": {"stop_loss_pct": 0.06, "strategy_stop_loss_pct": {"macd_zero_axis": 0.05}}}
        for strategy_id in ("yearline_pullback", "macd_divergence"):
            self.assertEqual(resolve_stop_loss(config, strategy_id), (0.06, "global", None), strategy_id)
        self.assertEqual(resolve_stop_loss(config, "macd_zero_axis"), (0.05, "strategy", "macd_zero_axis"))
        # Nothing configured, or an explicit null global, yields the built-in stop.
        for risk in ({}, {"stop_loss_pct": None}):
            self.assertEqual(resolve_stop_loss({"risk": risk}, "macd_zero_axis"), (DEFAULT_STOP_LOSS_PCT, "default", None), risk)
        self.assertEqual(resolve_stop_loss({}, None), (DEFAULT_STOP_LOSS_PCT, "default", None))
        # A malformed non-null global is a config error, not a silent fallback.
        for invalid in (0.0, float("nan"), "0.05", True):
            with self.assertRaises(ValueError, msg=invalid) as caught:
                resolve_stop_loss({"risk": {"stop_loss_pct": invalid}}, "macd_zero_axis")
            self.assertIn("risk.stop_loss_pct", str(caught.exception))

    def test_invalid_override_raises_instead_of_silently_disabling_risk(self):
        for invalid in (0.0, -0.05, 1.0, float("inf"), float("nan"), "0.05", True):
            config = {"risk": {"stop_loss_pct": 0.08, "strategy_stop_loss_pct": {"macd_zero_axis": invalid}}}
            with self.assertRaises(ValueError, msg=invalid) as caught:
                resolve_stop_loss(config, "macd_zero_axis")
            self.assertIn("strategy_stop_loss_pct.macd_zero_axis", str(caught.exception))
            with self.assertRaises(ValueError, msg=invalid):
                exit_parameters(config, "macd_zero_axis")
            with self.assertRaises(ValueError, msg=invalid) as caught:
                evaluate_strategies(self.frame(), config)
            self.assertIn("strategy_stop_loss_pct.macd_zero_axis", str(caught.exception))
        # An unknown strategy key belongs to no decision and must not break another one.
        self.assertEqual(resolve_stop_loss({"risk": {"stop_loss_pct": 0.08, "strategy_stop_loss_pct": {"unknown": 0.5}}}, "macd_zero_axis"), (0.08, "global", None))
        # A non-null map that is not an object is malformed, not "no overrides".
        with self.assertRaises(ValueError) as caught:
            resolve_stop_loss({"risk": {"stop_loss_pct": 0.08, "strategy_stop_loss_pct": "0.5"}}, "macd_zero_axis")
        self.assertIn("strategy_stop_loss_pct", str(caught.exception))
        # A malformed override owned by a real position is rejected against the owner.
        owned = {"shares": 100, "strategy_id": "macd_divergence"}
        config = {"risk": {"stop_loss_pct": 0.08, "strategy_stop_loss_pct": {"macd_divergence": 0.0}}}
        with self.assertRaises(ValueError) as caught:
            exit_parameters(config, "macd_zero_axis", owned)
        self.assertIn("macd_divergence", str(caught.exception))
        # The same malformed value on an unrelated strategy cannot hurt a held position
        # that is attributed elsewhere, because only the owner's key is consulted.
        config = {"risk": {"stop_loss_pct": 0.08, "strategy_stop_loss_pct": {"yearline_pullback": 0.0}}}
        self.assertEqual(resolve_stop_loss(config, "macd_zero_axis", owned), (0.08, "global", None))
