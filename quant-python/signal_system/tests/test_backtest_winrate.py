from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import (  # noqa: E402
    _apply_p5b_cross_sectional_ranks,
    _execution_values,
    build_market_gate,
    daily_price_limits,
    find_signals,
    load_backtest_history,
    price_limit_rate,
    run_portfolio,
    simulate_signal_mode,
    summarize_holding_periods,
)


def bars(
    opens: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    closes: list[float] | None = None,
    start: str = "2026-01-05",
) -> pd.DataFrame:
    count = len(opens)
    closes = closes or list(opens)
    highs = highs or [max(o, c) for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) for o, c in zip(opens, closes)]
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range(start, periods=count),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000.0] * count,
            "is_closed": [True] * count,
        }
    )


def buy(day: str, signal_type: str = "buy_1") -> dict:
    return {
        "day": day,
        "signal_type": signal_type,
        "side": "buy",
        "price": 10.0,
        "confirmed_at": day,
    }


def sell(day: str, signal_type: str = "sell_1") -> dict:
    return {
        "day": day,
        "signal_type": signal_type,
        "side": "sell",
        "price": 10.0,
        "confirmed_at": day,
    }


def execution(**overrides) -> dict:
    result = {
        "commission_pct": 0.0,
        "stamp_tax_pct": 0.0,
        "slippage_pct": 0.0,
        "lot_size": 100,
        "t_plus_one": True,
        "minimum_commission": 0.0,
        "price_limit_model": "none",
        "stop_loss_pct": 0.08,
        "take_profit_pct": 0.30,
        "intrabar_conflict": "stop_first",
        "chan_zero_axis": {"max_holding_bars": 3},
    }
    result.update(overrides)
    return result


class ExecutionTests(unittest.TestCase):
    def test_missing_config_fails_with_actionable_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-config.yaml"
            result = subprocess.run(
                [sys.executable, str(BASE_DIR / "backtest_winrate.py"), "--config", str(missing)],
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(2, result.returncode)
        self.assertIn("unable to load config", result.stderr)

    def test_p5b_missing_features_are_neutral_not_best(self):
        candidates = [
            {
                "entry_day": "2026-01-05",
                "_p5b_features": {"ma60_dist": None},
            },
            {
                "entry_day": "2026-01-05",
                "_p5b_features": {"ma60_dist": 0.10},
            },
            {
                "entry_day": "2026-01-05",
                "_p5b_features": {"ma60_dist": 0.20},
            },
        ]

        _apply_p5b_cross_sectional_ranks(candidates)

        self.assertEqual(0.5, candidates[0]["_p5b_pct"]["ma60_dist"])
        self.assertEqual(0.0, candidates[1]["_p5b_pct"]["ma60_dist"])
        self.assertEqual(1.0, candidates[2]["_p5b_pct"]["ma60_dist"])

    def test_sell_signal_on_entry_bar_exits_next_open(self):
        frame = bars([10.0, 10.0, 9.5, 9.4, 9.3, 9.2])
        days = [item.date().isoformat() for item in frame["datetime"]]
        result = simulate_signal_mode(
            "TEST",
            frame,
            {"buy": [buy(days[0])], "sell": [sell(days[1])]},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(),
        )
        self.assertEqual(1, len(result["trades"]))
        trade = result["trades"][0]
        self.assertEqual(days[1], trade["entry_day"])
        self.assertEqual(days[1], trade["exit_trigger_day"])
        self.assertEqual(days[2], trade["exit_day"])
        self.assertEqual(9.5, trade["exit_price"])
        self.assertEqual("sell_1", trade["exit_reason"])

    def test_stop_loss_and_take_profit_are_executed(self):
        stop_frame = bars(
            [10.0, 10.0, 10.0, 10.0, 10.0],
            highs=[10.0, 10.2, 10.1, 10.1, 10.1],
            lows=[10.0, 9.8, 9.1, 9.9, 9.9],
        )
        days = [item.date().isoformat() for item in stop_frame["datetime"]]
        stopped = simulate_signal_mode(
            "STOP",
            stop_frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(),
        )["trades"][0]
        self.assertEqual("stop_loss", stopped["exit_reason"])
        self.assertEqual(9.2, stopped["exit_price"])
        self.assertEqual(days[2], stopped["exit_day"])

        take_frame = bars(
            [10.0, 10.0, 10.0, 10.0, 10.0],
            highs=[10.0, 10.2, 13.5, 10.1, 10.1],
            lows=[10.0, 9.8, 9.9, 9.9, 9.9],
        )
        taken = simulate_signal_mode(
            "TAKE",
            take_frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(),
        )["trades"][0]
        self.assertEqual("take_profit", taken["exit_reason"])
        self.assertEqual(13.0, taken["exit_price"])

    def test_same_bar_conflict_is_stop_first(self):
        frame = bars(
            [10.0, 10.0, 10.0, 10.0, 10.0],
            highs=[10.0, 10.2, 13.5, 10.1, 10.1],
            lows=[10.0, 9.8, 9.0, 9.9, 9.9],
        )
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "BOTH",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(),
        )["trades"][0]
        self.assertEqual("stop_loss", trade["exit_reason"])
        self.assertEqual(9.2, trade["exit_price"])

    def test_entry_bar_stop_is_delayed_by_t_plus_one(self):
        frame = bars(
            [10.0, 10.0, 8.8, 9.0, 9.0],
            highs=[10.0, 10.1, 9.0, 9.1, 9.1],
            lows=[10.0, 9.0, 8.7, 8.9, 8.9],
        )
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "T1",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(),
        )["trades"][0]
        self.assertEqual("stop_loss", trade["exit_reason"])
        self.assertEqual(days[1], trade["exit_trigger_day"])
        self.assertEqual(days[2], trade["exit_day"])
        self.assertEqual(8.8, trade["exit_price"])

    def test_incomplete_horizon_is_skipped_by_default(self):
        frame = bars([10.0, 10.0, 10.0])
        days = [item.date().isoformat() for item in frame["datetime"]]
        strict = simulate_signal_mode(
            "SHORT",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(),
        )
        self.assertEqual([], strict["trades"])
        self.assertEqual(1, strict["skipped"]["incomplete_horizon"])

        loose = simulate_signal_mode(
            "SHORT",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(),
            allow_incomplete=True,
        )
        self.assertEqual("window_end", loose["trades"][0]["exit_reason"])

    def test_signal_mode_keeps_overlapping_trades(self):
        frame = bars([10.0] * 8)
        days = [item.date().isoformat() for item in frame["datetime"]]
        result = simulate_signal_mode(
            "OVERLAP",
            frame,
            {"buy": [buy(days[0], "buy_1"), buy(days[1], "buy_2")], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(),
        )
        self.assertEqual(2, len(result["trades"]))

    def test_signal_trade_keeps_stock_pool_metrics(self):
        frame = bars([10.0] * 5)
        days = [item.date().isoformat() for item in frame["datetime"]]
        event = buy(days[0])
        event["stock_pool_metrics"] = {
            "market_cap": 100.0,
            "avg_amount": 2.0,
            "avg_turnover_rate": 1.5,
        }
        trade = simulate_signal_mode(
            "POOL",
            frame,
            {"buy": [event], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(),
        )["trades"][0]
        self.assertEqual(100.0, trade["stock_pool_metrics"]["market_cap"])

    def test_minimum_commission_is_charged_on_both_sides(self):
        frame = bars([10.0] * 5)
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "600001",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(
                minimum_commission=5.0,
                price_limit_model="none",
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
            ),
        )["trades"][0]
        self.assertEqual(5.0, trade["entry_commission_cash"])
        self.assertEqual(5.0, trade["exit_commission_cash"])
        self.assertAlmostEqual(-0.995, trade["pnl_pct"], places=3)

    def test_limit_up_entry_is_skipped(self):
        frame = bars(
            [10.0, 11.0, 10.5, 10.4, 10.3],
            highs=[10.0, 11.0, 10.6, 10.5, 10.4],
            lows=[10.0, 11.0, 10.4, 10.3, 10.2],
            closes=[10.0, 11.0, 10.5, 10.4, 10.3],
        )
        days = [item.date().isoformat() for item in frame["datetime"]]
        result = simulate_signal_mode(
            "600001",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(price_limit_model="conservative"),
        )
        self.assertEqual([], result["trades"])
        self.assertEqual(1, result["skipped"]["entry_limit_up"])

    def test_locked_limit_down_defers_sell_until_next_tradable_day(self):
        frame = bars(
            [10.0, 10.0, 9.0, 9.2, 9.3, 9.4],
            highs=[10.0, 10.0, 9.0, 9.5, 9.5, 9.5],
            lows=[10.0, 10.0, 9.0, 9.0, 9.1, 9.2],
            closes=[10.0, 10.0, 9.0, 9.3, 9.4, 9.4],
        )
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "600001",
            frame,
            {"buy": [buy(days[0])], "sell": [sell(days[1])]},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(price_limit_model="conservative", chan_zero_axis={"max_holding_bars": 4}),
        )["trades"][0]
        self.assertEqual(days[1], trade["exit_trigger_day"])
        self.assertEqual(days[3], trade["exit_day"])
        self.assertEqual(9.2, trade["exit_price"])
        self.assertEqual(1, trade["price_limit_deferred_bars"])

    def test_limit_down_open_that_unlocks_can_fill_at_limit_price(self):
        frame = bars(
            [10.0, 10.0, 9.0, 9.2, 9.3],
            highs=[10.0, 10.0, 9.2, 9.4, 9.4],
            lows=[10.0, 10.0, 9.0, 9.0, 9.1],
            closes=[10.0, 10.0, 9.1, 9.3, 9.3],
        )
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "600001",
            frame,
            {"buy": [buy(days[0])], "sell": [sell(days[1])]},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(price_limit_model="conservative"),
        )["trades"][0]
        self.assertEqual(days[2], trade["exit_day"])
        self.assertEqual(9.0, trade["exit_price"])
        self.assertEqual("intraday", trade["exit_session"])

    def test_timeout_ma_break_exits_next_open_after_close_confirmation(self):
        frame = bars(
            [10.0, 10.0, 10.0, 10.0, 10.0, 8.5, 8.5, 8.5],
            closes=[10.0, 10.0, 10.0, 10.0, 9.0, 8.5, 8.5, 8.5],
        )
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "MA_BREAK",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                chan_zero_axis={
                    "max_holding_bars": 3,
                    "timeout_exit_mode": "ma_break",
                    "timeout_ma_period": 2,
                    "timeout_ma_confirm_bars": 1,
                    "timeout_hard_cap_bars": 6,
                },
            ),
        )["trades"][0]
        self.assertEqual(days[4], trade["exit_trigger_day"])
        self.assertEqual(days[5], trade["exit_day"])
        self.assertEqual("timeout_ma_break", trade["exit_reason"])

    def test_timeout_ma_break_requires_two_closes_when_configured(self):
        frame = bars(
            [10.0, 10.0, 10.0, 10.0, 9.0, 8.5, 8.5, 8.5, 8.5],
            closes=[10.0, 10.0, 10.0, 10.0, 9.0, 8.5, 8.5, 8.5, 8.5],
        )
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "MA_BREAK_2",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                chan_zero_axis={
                    "max_holding_bars": 3,
                    "timeout_exit_mode": "ma_break",
                    "timeout_ma_period": 2,
                    "timeout_ma_confirm_bars": 2,
                    "timeout_hard_cap_bars": 6,
                },
            ),
        )["trades"][0]
        self.assertEqual(days[5], trade["exit_trigger_day"])
        self.assertEqual(days[6], trade["exit_day"])
        self.assertEqual("timeout_ma_break", trade["exit_reason"])

    def test_timeout_ma_confirmation_streak_starts_at_timeout_threshold(self):
        frame = bars(
            [10.0, 10.0, 10.0, 9.0, 8.5, 8.0, 8.0, 8.0, 8.0],
            closes=[10.0, 10.0, 10.0, 9.0, 8.5, 8.0, 8.0, 8.0, 8.0],
        )
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "MA_THRESHOLD",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                chan_zero_axis={
                    "max_holding_bars": 3,
                    "timeout_exit_mode": "ma_break",
                    "timeout_ma_period": 2,
                    "timeout_ma_confirm_bars": 2,
                    "timeout_hard_cap_bars": 6,
                },
            ),
        )["trades"][0]
        self.assertEqual(days[5], trade["exit_trigger_day"])
        self.assertEqual(days[6], trade["exit_day"])

    def test_explicit_fixed_timeout_matches_default_policy(self):
        frame = bars([10.0] * 8)
        days = [item.date().isoformat() for item in frame["datetime"]]
        baseline = simulate_signal_mode(
            "FIXED_DEFAULT",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(stop_loss_pct=0.0, take_profit_pct=0.0),
        )["trades"][0]
        explicit = simulate_signal_mode(
            "FIXED_EXPLICIT",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                chan_zero_axis={
                    "max_holding_bars": 3,
                    "timeout_exit_mode": "fixed",
                },
            ),
        )["trades"][0]
        for key in ("exit_trigger_day", "exit_day", "exit_reason", "exit_price"):
            self.assertEqual(baseline[key], explicit[key])

    def test_timeout_ma_break_uses_hard_cap_when_trend_never_breaks(self):
        frame = bars([10.0] * 8)
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "MA_CAP",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                chan_zero_axis={
                    "max_holding_bars": 3,
                    "timeout_exit_mode": "ma_break",
                    "timeout_ma_period": 2,
                    "timeout_ma_confirm_bars": 1,
                    "timeout_hard_cap_bars": 6,
                },
            ),
        )["trades"][0]
        self.assertEqual(days[7], trade["exit_day"])
        self.assertEqual("timeout_hard_cap", trade["exit_reason"])

    def test_timeout_hard_cap_records_locked_limit_down_deferral(self):
        frame = bars(
            [10.0] * 7 + [9.0, 9.3],
            highs=[10.0] * 7 + [9.0, 9.5],
            lows=[10.0] * 7 + [9.0, 9.1],
            closes=[10.0] * 7 + [9.0, 9.3],
        )
        days = [item.date().isoformat() for item in frame["datetime"]]
        trade = simulate_signal_mode(
            "600001",
            frame,
            {"buy": [buy(days[0])], "sell": []},
            date.fromisoformat(days[0]),
            date.fromisoformat(days[-1]),
            execution(
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                price_limit_model="conservative",
                chan_zero_axis={
                    "max_holding_bars": 3,
                    "timeout_exit_mode": "ma_break",
                    "timeout_ma_period": 2,
                    "timeout_ma_confirm_bars": 1,
                    "timeout_hard_cap_bars": 6,
                },
            ),
        )["trades"][0]
        self.assertEqual(days[7], trade["exit_trigger_day"])
        self.assertEqual(days[8], trade["exit_day"])
        self.assertEqual("timeout_hard_cap", trade["exit_reason"])
        self.assertEqual(1, trade["price_limit_deferred_bars"])

    def test_timeout_ma_break_defaults_to_sixty_bar_hard_cap(self):
        resolved = _execution_values(
            {
                "chan_zero_axis": {
                    "max_holding_bars": 40,
                    "timeout_exit_mode": "ma_break",
                }
            }
        )
        self.assertEqual(60, resolved["timeout_hard_cap_bars"])

    def test_zero_axis_exit_confirmation_delays_event_without_lookahead(self):
        frame = bars([10.0] * 80)
        fake_macd = pd.DataFrame(
            {
                "dif": [float("nan")] * 80,
                "dea": [float("nan")] * 80,
                "hist": [float("nan")] * 80,
            }
        )
        fake_macd.loc[10, ["dif", "dea"]] = [0.1, 0.05]
        fake_macd.loc[11, ["dif", "dea"]] = [-0.001, 0.0]
        fake_macd.loc[12, ["dif", "dea"]] = [-0.002, 0.0]
        config = {
            "backtest": {
                "chan_zero_axis": {"zero_axis_exit_confirmation_bars": 2}
            },
            "signal_strategy": {"macd": {"zero_axis_tolerance": 0.005}},
        }
        with patch("backtest_winrate.calculate_macd", return_value=fake_macd), \
             patch("backtest_winrate.analyze_chan", return_value={"signals": []}), \
             patch("backtest_winrate.find_golden_cross_entries", return_value=[]):
            events = find_signals(frame, config)
        self.assertEqual(1, len(events["sell"]))
        self.assertEqual(
            frame["datetime"].iloc[12].date().isoformat(), events["sell"][0]["day"]
        )
        self.assertEqual(
            frame["datetime"].iloc[11].date().isoformat(), events["sell"][0]["trigger_day"]
        )
        self.assertEqual(2, events["sell"][0]["confirmation_bars"])

    def test_zero_axis_exit_confirmation_cancels_after_recross(self):
        frame = bars([10.0] * 80)
        fake_macd = pd.DataFrame(
            {
                "dif": [float("nan")] * 80,
                "dea": [float("nan")] * 80,
                "hist": [float("nan")] * 80,
            }
        )
        fake_macd.loc[10, ["dif", "dea"]] = [0.1, 0.05]
        fake_macd.loc[11, ["dif", "dea"]] = [-0.001, 0.0]
        fake_macd.loc[12, ["dif", "dea"]] = [0.001, 0.0]
        config = {
            "backtest": {
                "chan_zero_axis": {"zero_axis_exit_confirmation_bars": 2}
            },
            "signal_strategy": {"macd": {"zero_axis_tolerance": 0.005}},
        }
        with patch("backtest_winrate.calculate_macd", return_value=fake_macd), \
             patch("backtest_winrate.analyze_chan", return_value={"signals": []}), \
             patch("backtest_winrate.find_golden_cross_entries", return_value=[]):
            events = find_signals(frame, config)
        self.assertEqual([], events["sell"])


class MarketGateTests(unittest.TestCase):
    def test_gate_uses_only_same_day_history(self):
        dates = pd.bdate_range("2024-01-02", periods=280)
        closes = [100.0 + index * 0.1 for index in range(len(dates))]
        frame = bars(
            closes,
            highs=[value + 0.2 for value in closes],
            lows=[value - 0.2 for value in closes],
            closes=closes,
            start="2024-01-02",
        )
        config = {
            "entry_filters": {"market_gate_enabled": True},
            "signal_strategy": {"macd": {"long_ma_period": 250}},
        }
        original = build_market_gate(frame, config)
        target_day = dates[265].date().isoformat()
        self.assertTrue(original[target_day]["allows_entries"])

        changed = frame.copy()
        changed.loc[270:, "close"] = 1.0
        changed.loc[270:, "open"] = 1.0
        changed_gate = build_market_gate(changed, config)
        self.assertEqual(original[target_day], changed_gate[target_day])

    def test_gate_blocks_below_or_falling_long_ma(self):
        up = [100.0 + index * 0.1 for index in range(270)]
        below = list(up)
        below[-1] = 50.0
        below_frame = bars(below, closes=below, start="2024-01-02")
        config = {
            "entry_filters": {"market_gate_enabled": True},
            "signal_strategy": {"macd": {"long_ma_period": 250}},
        }
        below_gate = build_market_gate(below_frame, config)
        last_day = below_frame["datetime"].iloc[-1].date().isoformat()
        self.assertFalse(below_gate[last_day]["allows_entries"])
        self.assertIn("below_ma_long", below_gate[last_day]["blocked_by"])

        falling = [200.0] * 250 + [200.0 - index * 5.0 for index in range(20)]
        falling_frame = bars(falling, closes=falling, start="2024-01-02")
        falling_gate = build_market_gate(falling_frame, config)
        last_day = falling_frame["datetime"].iloc[-1].date().isoformat()
        self.assertFalse(falling_gate[last_day]["allows_entries"])
        self.assertIn("ma_long_down", falling_gate[last_day]["blocked_by"])

    def test_gate_blocks_a_same_day_macd_death_cross(self):
        closes = [100.0 + index * 0.1 for index in range(270)]
        frame = bars(closes, closes=closes, start="2024-01-02")
        dif = pd.Series([1.0] * 269 + [-1.0])
        dea = pd.Series([0.0] * 270)
        fake_macd = pd.DataFrame({"dif": dif, "dea": dea, "hist": (dif - dea) * 2})
        config = {
            "entry_filters": {"market_gate_enabled": True},
            "signal_strategy": {"macd": {"long_ma_period": 250}},
        }
        with patch("backtest_winrate.calculate_macd", return_value=fake_macd):
            gate = build_market_gate(frame, config)
        last_day = frame["datetime"].iloc[-1].date().isoformat()
        self.assertFalse(gate[last_day]["allows_entries"])
        self.assertIn("macd_death_cross", gate[last_day]["blocked_by"])


class PriceLimitTests(unittest.TestCase):
    def test_board_rates_and_price_rounding(self):
        trade_day = date(2026, 1, 5)
        self.assertEqual(0.10, price_limit_rate("600001", trade_day))
        self.assertEqual(0.20, price_limit_rate("300001", trade_day))
        self.assertEqual(0.20, price_limit_rate("688001", trade_day))
        self.assertEqual(0.30, price_limit_rate("830001", trade_day))
        self.assertEqual(0.05, price_limit_rate("600001", trade_day, {"600001"}))
        upper, lower = daily_price_limits("600001", 10.03, trade_day)
        self.assertEqual(11.03, upper)
        self.assertEqual(9.03, lower)


class AdjustedHistoryTests(unittest.TestCase):
    def test_qfq_missing_does_not_fall_back_to_none(self):
        with tempfile.TemporaryDirectory() as directory:
            history_dir = Path(directory)
            bars([10.0] * 20).to_pickle(history_dir / "600001_none.pkl")
            with self.assertRaises(FileNotFoundError):
                load_backtest_history(
                    "600001",
                    adjustment="qfq",
                    config={},
                    history_bars=20,
                    end=date(2026, 1, 30),
                    fetch_missing=False,
                    history_dir=history_dir,
                )

    def test_qfq_fetcher_is_used_and_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            history_dir = Path(directory)
            fetched = bars([10.0] * 30)
            fetched.attrs["adjust"] = "qfq"
            calls = []

            def fetcher(symbol, limit, adjustment):
                calls.append((symbol, limit, adjustment))
                return fetched

            loaded, source = load_backtest_history(
                "600001",
                adjustment="qfq",
                config={},
                history_bars=20,
                end=date(2026, 1, 30),
                fetch_missing=True,
                history_dir=history_dir,
                fetcher=fetcher,
            )
            self.assertEqual("fetched_qfq", source)
            self.assertEqual([("600001", 20, "qfq")], calls)
            self.assertEqual("qfq", loaded.attrs["adjust"])
            self.assertTrue((history_dir / "600001_qfq.pkl").exists())


class PortfolioTests(unittest.TestCase):
    @staticmethod
    def candidate(symbol: str, signal_day: str, entry_day: str, exit_day: str) -> dict:
        return {
            "symbol": symbol,
            "signal_day": signal_day,
            "entry_day": entry_day,
            "exit_day": exit_day,
            "exit_trigger_day": exit_day,
            "signal_type": "buy_1",
            "signal_types": ["buy_1"],
            "entry_price": 10.0,
            "exit_price": 11.0,
            "exit_reason": "timeout",
            "exit_session": "open",
            "pnl_pct": 10.0,
            "holding_days": 3,
            "_mark_prices": {
                entry_day: 10.0,
                "2026-01-07": 10.5,
                exit_day: 11.0,
            },
        }

    def test_portfolio_enforces_limits_and_conserves_cash(self):
        candidates = [
            self.candidate("AAA", "2026-01-05", "2026-01-06", "2026-01-08"),
            self.candidate("BBB", "2026-01-05", "2026-01-06", "2026-01-08"),
            self.candidate("CCC", "2026-01-05", "2026-01-06", "2026-01-08"),
            self.candidate("AAA", "2026-01-06", "2026-01-07", "2026-01-09"),
        ]
        result = run_portfolio(
            candidates,
            execution(commission_pct=0.0, stamp_tax_pct=0.0, slippage_pct=0.0),
            {
                "initial_cash": 10000.0,
                "max_positions": 2,
                "position_size_pct": 0.5,
                "lot_size": 100,
            },
        )
        self.assertEqual(2, len(result["trades"]))
        self.assertEqual(11000.0, result["summary"]["final_equity"])
        self.assertEqual(10.0, result["summary"]["total_return_pct"])
        self.assertEqual(1, result["rejection_reasons"]["max_positions"])
        self.assertEqual(1, result["rejection_reasons"]["symbol_already_held"])
        self.assertLessEqual(result["summary"]["max_positions_used"], 2)

    def test_same_day_same_symbol_signals_are_merged(self):
        first = self.candidate("AAA", "2026-01-05", "2026-01-06", "2026-01-08")
        second = dict(first)
        second["signal_type"] = "buy_2"
        second["signal_types"] = ["buy_2"]
        result = run_portfolio(
            [first, second],
            execution(),
            {
                "initial_cash": 100000.0,
                "max_positions": 4,
                "position_size_pct": 0.25,
                "lot_size": 100,
            },
        )
        self.assertEqual(1, len(result["trades"]))
        self.assertEqual(["buy_1", "buy_2"], result["trades"][0]["signal_types"])

    def test_portfolio_recalculates_minimum_commission_for_actual_quantity(self):
        candidate = self.candidate("AAA", "2026-01-05", "2026-01-06", "2026-01-08")
        result = run_portfolio(
            [candidate],
            execution(minimum_commission=5.0),
            {
                "initial_cash": 10000.0,
                "max_positions": 1,
                "position_size_pct": 0.5,
                "lot_size": 100,
            },
        )
        self.assertEqual(10390.0, result["summary"]["final_equity"])
        self.assertEqual(390.0, result["summary"]["total_pnl_cash"])
        self.assertEqual(400, result["trades"][0]["quantity"])


if __name__ == "__main__":
    unittest.main()
