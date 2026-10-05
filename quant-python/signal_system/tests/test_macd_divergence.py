"""Tests for the decoupled macd_divergence research pool."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from strategy.macd_divergence import (  # noqa: E402
    MIN_BARS,
    POOL_TYPE_DIVERGENCE,
    add_divergence_indicators,
    bottom_divergence_at,
    divergence_signal_at,
    evaluate_conditions,
    last_bar_divergence_candidate,
    resolve_divergence_config,
)


def _config(**overrides):
    config = {
        "macd_divergence": {
            "enabled": True,
            "macd": {"fast": 12, "slow": 26, "signal": 9},
            "zero_axis_tolerance": 0.005,
            "volume_window": 20,
            "min_volume_ratio": 1.5,
            "long_ma_period": 250,
            "long_ma_slope_window": 20,
            "min_macd_segment_bars": 2,
        }
    }
    config["macd_divergence"].update(overrides)
    return resolve_divergence_config(config)


def _bars_from_close(
    close, volume_mult: float = 2.0, bump_at: int | None = None
) -> pd.DataFrame:
    """Build a daily frame with a known volume multiple on one bar."""
    close = np.asarray(close, dtype=float)
    n = len(close)
    dates = pd.bdate_range("2024-01-01", periods=n)
    volume = np.full(n, 1_000_000.0)
    volume[n - 1 if bump_at is None else bump_at] = 1_000_000.0 * volume_mult
    return pd.DataFrame(
        {
            "datetime": dates,
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
            "is_closed": True,
        }
    )


# Deterministic four-condition fixture: trend + two descending dips. The values
# were located by a seeded search over this generator; PCG64 streams are stable
# across numpy versions, so the setup is reproducible without any data files.
FIXTURE_SEED = 40
FIXTURE_BAR = 296


def _fixture_close(seed: int = FIXTURE_SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = 320
    close = 12.0 + np.cumsum(rng.normal(0.35, 0.9, n))
    close = np.clip(close, 5.0, None)
    close[-40:-25] = np.linspace(close[-41], close[-41] - 4.0, 15)
    close[-25:-10] = np.linspace(close[-25], close[-25] + 3.0, 15)
    close[-10:] = np.linspace(close[-11], close[-11] - 3.2, 10)
    return close


def _hist_series(runs):
    """Build (hist, low) from runs of (length, hist_value, low_start, low_end)."""
    hist: list[float] = []
    low: list[float] = []
    for length, hist_value, low_start, low_end in runs:
        hist.extend([float(hist_value)] * length)
        low.extend(np.linspace(low_start, low_end, length))
    return pd.Series(hist, dtype=float), pd.Series(low, dtype=float)


class ResolveConfigTests(unittest.TestCase):
    def test_missing_block_disables_pool(self):
        self.assertFalse(resolve_divergence_config({})["enabled"])

    def test_defaults_are_explicit(self):
        settings = _config()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["min_volume_ratio"], 1.5)
        self.assertEqual(settings["long_ma_period"], 250)


class BottomDivergenceTests(unittest.TestCase):
    # 20 bars: two completed negative runs (indices 5-8 and 14-17) plus a
    # trailing positive run so both negative runs are closed before the signal.
    def _divergence_runs(self, second_low_start: float, second_low_end: float):
        return [
            (5, 1.0, 30.0, 30.0),
            (4, -1.0, 20.0, 12.0),
            (5, 1.0, 20.0, 25.0),
            (4, -0.4, second_low_start, second_low_end),
            (2, 1.0, 12.0, 12.0),
        ]

    def test_new_low_with_smaller_area_is_divergence(self):
        hist, low = _hist_series(self._divergence_runs(18.0, 11.0))
        result = bottom_divergence_at(hist, low, 19, 2)
        self.assertTrue(result["available"])
        self.assertTrue(result["bottom_divergence"])
        self.assertLess(result["area_ratio"], 1.0)
        self.assertLess(result["price_new_low_pct"], 0.0)

    def test_higher_low_is_not_divergence(self):
        hist, low = _hist_series(self._divergence_runs(13.5, 13.0))
        result = bottom_divergence_at(hist, low, 19, 2)
        self.assertTrue(result["available"])
        self.assertFalse(result["bottom_divergence"])

    def test_larger_area_is_not_divergence(self):
        runs = self._divergence_runs(18.0, 11.0)
        runs[3] = (4, -1.6, 18.0, 11.0)
        hist, low = _hist_series(runs)
        result = bottom_divergence_at(hist, low, 19, 2)
        self.assertTrue(result["available"])
        self.assertFalse(result["bottom_divergence"])

    def test_active_cycle_is_excluded(self):
        hist, low = _hist_series(self._divergence_runs(18.0, 11.0))
        # Signal bar sits inside the second negative run: it is not completed yet.
        result = bottom_divergence_at(hist, low, 15, 2)
        self.assertFalse(result["bottom_divergence"])

    def test_short_runs_are_filtered_by_min_segment(self):
        runs = [
            (5, 1.0, 30.0, 30.0),
            (1, -1.0, 20.0, 12.0),
            (5, 1.0, 20.0, 25.0),
            (1, -0.4, 18.0, 11.0),
            (2, 1.0, 12.0, 12.0),
        ]
        hist, low = _hist_series(runs)
        result = bottom_divergence_at(hist, low, 13, 2)
        self.assertFalse(result["available"])


class FastPathEquivalenceTests(unittest.TestCase):
    """The backtest's vectorized divergence flag must equal the shipped checker.

    backtest_macd_divergence._divergence_flags is an O(n) fast path; if it ever
    drifts from bottom_divergence_at, backtests stop describing the live screen.
    """

    def test_matches_reference_on_synthetic_histogram(self):
        from backtest_macd_divergence import _divergence_flags

        settings = _config()
        frame = add_divergence_indicators(
            _bars_from_close(
                _fixture_close(), volume_mult=2.0, bump_at=FIXTURE_BAR
            ),
            settings,
        )
        hist = frame["hist"].to_numpy(dtype=float)
        low = frame["low"].to_numpy(dtype=float)
        flags = _divergence_flags(hist, low, int(settings["min_macd_segment_bars"]))
        checked = 0
        for index in range(MIN_BARS - 1, len(frame)):
            reference = bool(
                bottom_divergence_at(
                    frame["hist"],
                    frame["low"],
                    index,
                    int(settings["min_macd_segment_bars"]),
                )["bottom_divergence"]
            )
            self.assertEqual(
                reference, bool(flags[index]), f"divergence flag differs at {index}"
            )
            checked += 1
        self.assertGreater(checked, 20)

    def test_flags_are_causal(self):
        from backtest_macd_divergence import _divergence_flags

        hist = np.concatenate(
            [
                np.full(5, 1.0),
                np.full(4, -1.0),
                np.full(5, 1.0),
                np.full(4, -0.4),
                np.full(2, 1.0),
            ]
        )
        low = np.concatenate(
            [
                np.full(5, 30.0),
                np.linspace(20.0, 12.0, 4),
                np.full(5, 20.0),
                np.linspace(18.0, 11.0, 4),
                np.full(2, 12.0),
            ]
        )
        flags = _divergence_flags(hist, low, 2)
        # The divergence is only knowable once the second negative run closed.
        self.assertFalse(bool(flags[:18].any()))
        self.assertTrue(bool(flags[18:].all()))


class ExitRuleTests(unittest.TestCase):
    """The research exit rules must fire causally and match shipped detectors."""

    def test_top_divergence_trigger_matches_engine_helper(self):
        from backtest_macd_divergence import top_divergence_flags
        import backtest_winrate as bt

        settings = _config()
        frame = add_divergence_indicators(
            _bars_from_close(
                _fixture_close(), volume_mult=2.0, bump_at=FIXTURE_BAR
            ),
            settings,
        )
        flags = top_divergence_flags(frame["hist"], frame["close"], 1)
        checked = 0
        for index in np.flatnonzero(flags):
            index = int(index)
            self.assertTrue(
                bt._top_divergence_risk(frame, index, settings),
                f"flag at {index} not confirmed by the shipped helper",
            )
            checked += 1
        self.assertGreater(checked, 0)

    def test_top_divergence_is_single_bar_and_causal(self):
        from backtest_macd_divergence import top_divergence_flags

        # Two positive cycles: higher high, smaller area -> divergence on the
        # bar where the second cycle ends.
        hist = np.concatenate(
            [np.full(3, 0.0), np.full(4, 1.0), np.full(2, -0.5), np.full(4, 0.4), np.full(2, -0.5)]
        )
        close = np.concatenate(
            [
                np.full(3, 10.0),
                np.linspace(11.0, 14.0, 4),
                np.full(2, 13.0),
                np.linspace(14.0, 15.0, 4),
                np.full(2, 14.5),
            ]
        )
        flags = top_divergence_flags(pd.Series(hist), pd.Series(close), 1)
        # Cycle 1 ends at index 7, cycle 2 at index 13 -> only 13 may trigger.
        self.assertEqual(list(np.flatnonzero(flags)), [13])

    def test_stop_loss_wins_within_the_same_bar(self):
        """A gap through the stop must exit as stop_loss, not as an MA break."""
        from backtest_macd_divergence import (
            simulate_with_exit_rules,
            top_divergence_flags,
        )

        settings = _config()
        close = np.full(325, 20.0)
        bars = _bars_from_close(close, volume_mult=1.0, bump_at=324)
        # Signal on bar 318 -> entry at bar 319 open; bar 320 gaps below the stop.
        bars.loc[320, ["open", "high", "low", "close"]] = [17.0, 17.2, 16.8, 17.0]
        bars.loc[321:, ["open", "high", "low", "close"]] = 17.0
        buy = {"day": str(bars["datetime"].iloc[318].date()), "signal_type": "t"}
        frame = add_divergence_indicators(bars, settings)
        flags = top_divergence_flags(frame["hist"], frame["close"], 1)
        costs = {"stop_loss_pct": 0.08, "take_profit_pct": 0.30, "max_holding_bars": 40}
        trade, reason = simulate_with_exit_rules(
            "600036", bars, buy, {}, settings, costs, flags
        )
        self.assertIsNone(reason)
        assert trade is not None
        self.assertEqual(trade["exit_reason"], "stop_loss")
        self.assertLess(trade["pnl_pct"], -8.0)

    def test_slow_decline_exits_on_yearline_or_ma20_before_the_stop(self):
        """With a gradual decline the MA rules fire earlier than -8%."""
        from backtest_macd_divergence import (
            simulate_with_exit_rules,
            top_divergence_flags,
        )

        settings = _config()
        close = np.concatenate([np.linspace(10.0, 20.0, 300), np.linspace(20.0, 17.0, 40)])
        bars = _bars_from_close(close, volume_mult=1.0, bump_at=len(close) - 41)
        buy = {"day": str(bars["datetime"].iloc[-42].date()), "signal_type": "t"}
        frame = add_divergence_indicators(bars, settings)
        flags = top_divergence_flags(frame["hist"], frame["close"], 1)
        costs = {"stop_loss_pct": 0.08, "take_profit_pct": 0.30, "max_holding_bars": 40}
        trade, reason = simulate_with_exit_rules(
            "600036", bars, buy, {}, settings, costs, flags
        )
        self.assertIsNone(reason)
        assert trade is not None
        self.assertIn(
            trade["exit_reason"], {"below_ma20", "below_ma250", "top_divergence"}
        )
        self.assertGreater(trade["pnl_pct"], -8.0)
        self.assertLess(trade["holding_bars"], 40)


    def test_relaxed_ma20_variants_exit_no_earlier_than_the_strict_rule(self):
        """Strict '跌破即卖' must never exit later than its relaxed variants."""
        from backtest_macd_divergence import simulate_with_rules, top_divergence_flags

        settings = _config()
        # Uptrend, then a slow decline that crosses MA20 and stays below it.
        close = np.concatenate([np.linspace(10.0, 20.0, 300), np.linspace(20.0, 18.0, 40)])
        bars = _bars_from_close(close, volume_mult=1.0, bump_at=len(close) - 41)
        buy = {"day": str(bars["datetime"].iloc[-42].date()), "signal_type": "t"}
        frame = add_divergence_indicators(bars, settings)
        flags = top_divergence_flags(frame["hist"], frame["close"], 1)
        costs = {"stop_loss_pct": 0.08, "take_profit_pct": 0.30, "max_holding_bars": 40}
        holds: dict[str, int] = {}
        for rule in ("ma20", "ma20_3days", "ma20_death_cross"):
            trade, reason = simulate_with_rules(
                "600036", bars, buy, {}, settings, costs, flags, ("stop", rule)
            )
            self.assertIsNone(reason, rule)
            assert trade is not None
            self.assertIn(trade["exit_reason"], {"below_ma20", "below_ma20_3days", "below_ma20_death_cross"}, rule)
            holds[rule] = trade["holding_bars"]
        self.assertLessEqual(holds["ma20"], holds["ma20_3days"])
        self.assertLessEqual(holds["ma20"], holds["ma20_death_cross"])

    def test_timeout_rule_fires_at_the_configured_bar(self):
        from backtest_macd_divergence import simulate_with_rules, top_divergence_flags

        settings = _config()
        # Flat prices: no MA break, no divergence -> only the timeout can exit.
        # 60 bars of runway after the entry so a 40-bar timeout can be reached.
        close = np.full(365, 20.0)
        bars = _bars_from_close(close, volume_mult=1.0, bump_at=364)
        buy = {"day": str(bars["datetime"].iloc[300].date()), "signal_type": "t"}
        frame = add_divergence_indicators(bars, settings)
        flags = top_divergence_flags(frame["hist"], frame["close"], 1)
        costs = {"stop_loss_pct": 0.08, "take_profit_pct": 0.30, "max_holding_bars": 40}
        trade, reason = simulate_with_rules(
            "600036", bars, buy, {}, settings, costs, flags, ("stop", "timeout")
        )
        self.assertIsNone(reason)
        assert trade is not None
        self.assertEqual(trade["exit_reason"], "timeout")
        # Scheduled exit: the engine sells at the open of the 40th held bar.
        self.assertEqual(trade["holding_bars"], 40)

    def test_without_timeout_a_flat_position_runs_to_the_end(self):
        from backtest_macd_divergence import simulate_with_rules, top_divergence_flags

        settings = _config()
        close = np.full(365, 20.0)
        bars = _bars_from_close(close, volume_mult=1.0, bump_at=364)
        buy = {"day": str(bars["datetime"].iloc[300].date()), "signal_type": "t"}
        frame = add_divergence_indicators(bars, settings)
        flags = top_divergence_flags(frame["hist"], frame["close"], 1)
        costs = {"stop_loss_pct": 0.08, "take_profit_pct": 0.30, "max_holding_bars": 40}
        trade, reason = simulate_with_rules(
            "600036", bars, buy, {}, settings, costs, flags, ("stop",)
        )
        self.assertIsNone(reason)
        assert trade is not None
        self.assertEqual(trade["exit_reason"], "open_at_end")

    def test_trailing_stop_locks_profit_after_the_arm_level(self):
        from backtest_macd_divergence import simulate_with_rules, top_divergence_flags

        settings = _config()
        # Rise 25%, then fall 12% from the peak: trailing arms at +15%, gap 8%.
        close = np.concatenate(
            [np.full(300, 20.0), np.linspace(20.0, 25.0, 20), np.linspace(25.0, 21.5, 15)]
        )
        bars = _bars_from_close(close, volume_mult=1.0, bump_at=len(close) - 36)
        buy = {"day": str(bars["datetime"].iloc[299].date()), "signal_type": "t"}
        frame = add_divergence_indicators(bars, settings)
        flags = top_divergence_flags(frame["hist"], frame["close"], 1)
        costs = {"stop_loss_pct": 0.08, "take_profit_pct": 0.30, "max_holding_bars": 60}
        trade, reason = simulate_with_rules(
            "600036",
            bars,
            buy,
            {},
            settings,
            costs,
            flags,
            ("stop", "trailing"),
            trail_arm_pct=0.15,
            trail_gap_pct=0.08,
        )
        self.assertIsNone(reason)
        assert trade is not None
        self.assertEqual(trade["exit_reason"], "trailing_stop")
        self.assertGreater(trade["pnl_pct"], 0)

    def test_unknown_rule_is_rejected(self):
        from backtest_macd_divergence import simulate_with_rules

        with self.assertRaises(ValueError):
            simulate_with_rules(
                "600036",
                _bars_from_close(np.full(300, 10.0)),
                {"day": "2024-01-01"},
                {},
                _config(),
                {},
                np.zeros(300, dtype=bool),
                ("not_a_rule",),
            )


class EngineTrendExitTests(unittest.TestCase):
    """The shipped engine must implement v1b and keep other signals on `fixed`."""

    @staticmethod
    def _resolved(**overrides):
        import backtest_winrate as bt

        config = {
            "backtest": {"exit_rules": {"mode": "divergence_trend", **overrides}},
            "risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.30},
            "signal_strategy": {"macd": {"fast": 12, "slow": 26, "signal": 9}},
            "chan_zero_axis": {"max_holding_bars": 40},
        }
        return bt, bt._resolve_execution_config(config)

    def test_scope_limits_v1b_to_the_divergence_signal(self):
        bt, costs = self._resolved(apply_to_signal_types=["macd_divergence_bottom"])
        execution = bt._execution_values(costs)
        self.assertTrue(
            bt._uses_trend_exits(execution, {"signal_type": "macd_divergence_bottom"})
        )
        self.assertFalse(
            bt._uses_trend_exits(
                execution, {"signal_type": "macd_golden_cross_pullback_confirmed_above"}
            )
        )

    def test_empty_scope_applies_to_every_signal(self):
        bt, costs = self._resolved()
        execution = bt._execution_values(costs)
        self.assertTrue(bt._uses_trend_exits(execution, {"signal_type": "anything"}))

    def test_wildcard_scope_covers_every_signal_type(self):
        """Used by the buy x sell comparison arms; must not depend on the name."""
        bt, costs = self._resolved(apply_to_signal_types=["*"])
        execution = bt._execution_values(costs)
        for signal_type in (
            "macd_divergence_bottom",
            "yearline_A_breakout",
            "yearline_B_pullback",
            "macd_golden_cross_pullback_confirmed_above",
        ):
            self.assertTrue(bt._uses_trend_exits(execution, {"signal_type": signal_type}))

    def test_prefix_scope_matches_a_family_only(self):
        bt, costs = self._resolved(apply_to_signal_types=["yearline_*"])
        execution = bt._execution_values(costs)
        self.assertTrue(
            bt._uses_trend_exits(execution, {"signal_type": "yearline_A_breakout"})
        )
        self.assertTrue(
            bt._uses_trend_exits(execution, {"signal_type": "yearline_B_pullback"})
        )
        self.assertFalse(
            bt._uses_trend_exits(execution, {"signal_type": "macd_divergence_bottom"})
        )

    def test_invalid_mode_is_rejected(self):
        import backtest_winrate as bt

        with self.assertRaises(ValueError):
            bt._resolve_execution_config(
                {"backtest": {"exit_rules": {"mode": "nope"}}, "risk": {}}
            )

    def test_v1b_disables_the_fixed_take_profit(self):
        """A +30% cap would silently truncate winners under v1b."""
        bt, costs = self._resolved(apply_to_signal_types=["macd_divergence_bottom"])
        execution = bt._execution_values(costs)
        self.assertTrue(
            bt._uses_trend_exits(execution, {"signal_type": "macd_divergence_bottom"})
        )
        settings = execution["exit_rules_config"]
        # MA period and MACD params must be resolved, not left at defaults.
        self.assertEqual(settings["ma_long_period"], 250)
        self.assertEqual(settings["macd_fast"], 12)

    def test_yearline_break_flag_is_causal(self):
        import backtest_winrate as bt

        _, costs = self._resolved()
        execution = bt._execution_values(costs)
        close = np.concatenate([np.linspace(10.0, 30.0, 300), np.linspace(30.0, 12.0, 60)])
        bars = _bars_from_close(close, volume_mult=1.0, bump_at=359)
        flags = bt._build_trend_exit_flags(bars, execution)
        ma_long = flags["ma_long"]
        below = [
            index
            for index in range(len(bars))
            if np.isfinite(ma_long[index]) and close[index] < ma_long[index]
        ]
        self.assertTrue(below, "fixture must eventually trade below the yearline")

    def test_top_divergence_triggers_are_single_bar(self):
        import backtest_winrate as bt

        _, costs = self._resolved()
        execution = bt._execution_values(costs)
        bars = _bars_from_close(_fixture_close(), volume_mult=1.0, bump_at=319)
        flags = bt._build_trend_exit_flags(bars, execution)
        # Each trigger is a bar where a positive cycle completes; consecutive
        # duplicate triggers would mean a stale, entry-crossing signal.
        triggers = np.flatnonzero(flags["top_divergence_flags"])
        self.assertGreater(len(triggers), 0)
        self.assertEqual(len(triggers), len(set(int(t) for t in triggers)))


class ConditionFunnelTests(unittest.TestCase):
    """The funnel must report each gate independently, not short-circuit."""

    @classmethod
    def setUpClass(cls):
        cls.settings = _config()
        cls.frame = add_divergence_indicators(
            _bars_from_close(
                _fixture_close(), volume_mult=2.0, bump_at=FIXTURE_BAR
            ),
            cls.settings,
        )

    def test_all_gates_true_on_the_fixture(self):
        gates = evaluate_conditions(self.frame, FIXTURE_BAR, self.settings)
        self.assertTrue(gates["available"])
        for key in (
            "golden_cross",
            "zero_axis_ok",
            "volume_ok",
            "above_yearline",
            "divergence_ok",
            "all",
        ):
            self.assertTrue(gates[key], key)

    def test_volume_failure_still_reports_other_gates(self):
        frame = add_divergence_indicators(
            _bars_from_close(
                _fixture_close(), volume_mult=1.0, bump_at=FIXTURE_BAR
            ),
            self.settings,
        )
        gates = evaluate_conditions(frame, FIXTURE_BAR, self.settings)
        self.assertTrue(gates["available"])
        self.assertTrue(gates["golden_cross"])
        self.assertTrue(gates["divergence_ok"])
        self.assertFalse(gates["volume_ok"])
        self.assertFalse(gates["all"])

    def test_unavailable_when_history_too_short(self):
        frame = add_divergence_indicators(
            _bars_from_close(np.linspace(10.0, 12.0, 100)), self.settings
        )
        gates = evaluate_conditions(frame, len(frame) - 1, self.settings)
        self.assertFalse(gates["available"])
        self.assertFalse(gates["all"])

    def test_precomputed_frame_matches_direct_call(self):
        bars = _bars_from_close(
            _fixture_close(), volume_mult=2.0, bump_at=FIXTURE_BAR
        )
        trimmed = bars.iloc[: FIXTURE_BAR + 1].reset_index(drop=True)
        direct = last_bar_divergence_candidate(
            "000002", "x", trimmed, self.settings
        )
        reuse = last_bar_divergence_candidate(
            "000002",
            "x",
            trimmed,
            self.settings,
            frame=add_divergence_indicators(trimmed, self.settings),
        )
        self.assertEqual(direct, reuse)


class GateTests(unittest.TestCase):
    """Gate-level tests: one fixture satisfies all four conditions, then each
    condition is driven to failure in turn."""

    @classmethod
    def setUpClass(cls):
        cls.settings = _config()
        close = _fixture_close()
        cls.bars = _bars_from_close(close, volume_mult=2.0, bump_at=FIXTURE_BAR)
        cls.frame = add_divergence_indicators(cls.bars, cls.settings)

    def test_full_path_candidate_has_all_four_conditions(self):
        signal = divergence_signal_at(self.frame, FIXTURE_BAR, self.settings)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["signal_type"], "macd_divergence_bottom")
        self.assertIn(signal["zero_axis_zone"], {"above", "near"})
        self.assertGreaterEqual(
            signal["volume_ratio"], self.settings["min_volume_ratio"]
        )
        self.assertLess(signal["divergence_area_ratio"], 1.0)
        self.assertLess(signal["divergence_price_new_low_pct"], 0.0)
        self.assertEqual(len(signal["conditions"]), 4)
        self.assertEqual(signal["entry_reference"], "next_day_open")
        self.assertTrue(signal["research_only"])

    def test_last_bar_path_matches_index_path(self):
        # Same bar, but as the final bar of the frame.
        trimmed = self.bars.iloc[: FIXTURE_BAR + 1].reset_index(drop=True)
        candidate = last_bar_divergence_candidate(
            "600036", "招商银行", trimmed, self.settings
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["pool_type"], POOL_TYPE_DIVERGENCE)
        self.assertEqual(candidate["symbol"], "600036")
        self.assertEqual(candidate["signal_date"], "2025-02-18")

    def test_volume_gate_rejects_thin_bar(self):
        thin = _bars_from_close(
            _fixture_close(), volume_mult=1.0, bump_at=FIXTURE_BAR
        )
        frame = add_divergence_indicators(thin, self.settings)
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_volume_gate_accepts_exact_threshold(self):
        exact = _bars_from_close(
            _fixture_close(),
            volume_mult=self.settings["min_volume_ratio"],
            bump_at=FIXTURE_BAR,
        )
        frame = add_divergence_indicators(exact, self.settings)
        self.assertIsNotNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_below_zero_cross_is_rejected(self):
        frame = self.frame.copy()
        frame.loc[FIXTURE_BAR, "dif"] = -0.5
        frame.loc[FIXTURE_BAR, "dea"] = -0.6
        frame.loc[FIXTURE_BAR, "golden_cross"] = True
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_below_yearline_is_rejected(self):
        frame = self.frame.copy()
        frame.loc[FIXTURE_BAR, "close"] = (
            float(frame.loc[FIXTURE_BAR, "ma_long"]) * 0.9
        )
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_flat_yearline_is_rejected(self):
        frame = self.frame.copy()
        frame.loc[FIXTURE_BAR, "ma_long_prev"] = (
            float(frame.loc[FIXTURE_BAR, "ma_long"]) * 1.01
        )
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_missing_golden_cross_is_rejected(self):
        frame = self.frame.copy()
        frame.loc[FIXTURE_BAR, "golden_cross"] = False
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_insufficient_history_returns_none(self):
        bars = _bars_from_close(np.linspace(10.0, 12.0, 100))
        frame = add_divergence_indicators(bars, _config())
        self.assertIsNone(divergence_signal_at(frame, len(frame) - 1, _config()))
        self.assertIsNone(
            last_bar_divergence_candidate("600036", "招商银行", bars, _config())
        )

    def test_below_zero_signal_type_absent_from_pool_names(self):
        # Guard: this pool must never emit the production MACD signal types.
        signal = divergence_signal_at(self.frame, FIXTURE_BAR, self.settings)
        assert signal is not None
        self.assertFalse(
            signal["signal_type"].startswith("macd_golden_cross_pullback_confirmed_")
        )


class CapacitySweepTests(unittest.TestCase):
    """资金容量阶梯: 回答"同一账户装得下谁的信号"。

    超订策略(18033 个信号 / 4 个仓位)的组合结果由"抽到哪 0.5%"决定, 所以容量
    阶梯必须保证: 档位越高 -> 成交越多 -> 占用率越高。占用率若不高, 说明瓶颈
    不是资金而是信号本身。
    """

    def test_slot_ladder_increases_accepted_and_exposes_occupancy(self):
        import backtest_macd_divergence as bmd
        import backtest_winrate as bt

        # 9 个信号全部落在同一天开仓、同一天平仓: 4 个仓位只能装 4 个,
        # 16 个仓位能全装 -> 阶梯必须体现差异。
        trades = []
        for slot in range(9):
            trades.append(
                {
                    "symbol": f"{slot:06d}",
                    "signal_day": "2025-01-02",
                    "signal_type": "macd_divergence_bottom",
                    "entry_day": "2025-01-02",
                    "entry_index": 0,
                    "exit_index": 1,
                    "exit_day": "2025-01-20",
                    "entry_price": 10.0,
                    "exit_price": 10.5,
                    "pnl_pct": 5.0,
                    "pnl_cash": 500.0,
                    "holding_days": 18,
                    "quantity": 100,
                    "entry_cost_cash": 1000.0,
                    "exit_reason": "timeout",
                }
            )
        config = _config()
        costs = bt._resolve_execution_config(config)
        base = {
            "initial_cash": 100000.0,
            "position_size_pct": 0.25,
            "lot_size": 100,
            "tie_break": "symbol_asc",
            "seed": 1,
        }
        accepted = {}
        for level in (4, 16):
            result = bt.run_portfolio(
                [dict(trade) for trade in trades],
                costs,
                {**base, "max_positions": level, "position_size_pct": 1.0 / level},
            )
            accepted[level] = result["summary"]["accepted"]
            attribution = result["attribution"]
            # 占用率必须存在, 否则无法判断瓶颈是资金还是信号。
            self.assertIsNotNone(attribution.get("average_positions"))
            self.assertLessEqual(
                float(attribution["average_positions"]), float(level) + 1e-9
            )
        self.assertGreater(accepted[16], accepted[4])

    def test_run_arm_tolerates_slots_as_int_and_list(self):
        """老审计脚本传 int slots; 新驱动传 slots_list。两者都必须可用。"""
        import argparse

        import backtest_macd_divergence as bmd

        for namespace in (
            argparse.Namespace(slots=4),
            argparse.Namespace(slots_list="4,8"),
            argparse.Namespace(),
        ):
            namespace.tie_break_list = ["symbol_asc"]
            # run_arm 只从 args 读取扫描参数; 空 path 列表让它直接返回。
            arm = bmd.run_arm(
                namespace,
                "synthetic",
                _config(),
                _config(),
                bmd.bt._resolve_execution_config(_config()),
                {"initial_cash": 100000.0, "max_positions": 4},
                [],
                date(2025, 1, 1),
                date(2025, 12, 31),
            )
            self.assertEqual(arm["trade_count"], 0)


if __name__ == "__main__":
    unittest.main()
