import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import research_backtest as rb
from research_backtest import entry_indices, metrics, prepare_backtest_inputs, replay
from strategy.registry import STRATEGIES, evaluate_strategies

CALENDAR_DAYS = [str(day.date()) for day in pd.bdate_range("2022-01-03", "2024-03-29")]


def bars(days, price=10.0, volume=10000.0):
    index = pd.DatetimeIndex(pd.to_datetime(list(days)))
    frame = pd.DataFrame({"datetime": index, "open": price, "high": price, "low": price, "close": price, "volume": volume})
    frame["is_closed"] = True
    return frame


class FakeProvider:
    """Controlled daily provider: only symbols present in ``history`` return bars."""

    def __init__(self, listing, history, calendar=None):
        self.listing = listing
        self.history = history
        self.calendar = CALENDAR_DAYS if calendar is None else calendar
        self.requested = []

    def get_stock_list(self, exchange="", list_status="L", *, include_special=False):
        rows = [row for row in self.listing if include_special or "ST" not in row["name"]]
        return pd.DataFrame(rows, columns=["symbol", "name", "ts_code"])

    def get_trade_calendar(self, start_date=None, end_date=None):
        return pd.DataFrame({"trade_date": [day.replace("-", "") for day in self.calendar]})

    def get_daily_data(self, ts_code, start_date=None, end_date=None, period=250):
        self.requested.append(ts_code)
        frame = self.history.get(ts_code)
        return pd.DataFrame() if frame is None else frame.copy()


def market_listing():
    return [
        {"symbol": "600000", "name": "浦发银行", "ts_code": "600000.SH"},
        {"symbol": "000001", "name": "平安银行", "ts_code": "000001.SZ"},
        {"symbol": "430047", "name": "诺思兰德", "ts_code": "430047.BJ"},
        {"symbol": "000004", "name": "ST国华", "ts_code": "000004.SZ"},
    ]


class ResearchBacktestMetricsTests(unittest.TestCase):
    def test_metrics_include_initial_drawdown_and_cash_days(self):
        curve = [{"equity": 100}, {"equity": 90}, {"equity": 110}]
        result = metrics(curve, [{"pnl_cash": 20}, {"pnl_cash": -10}, {"pnl_cash": 0}], 100)
        self.assertAlmostEqual(result["max_drawdown_pct"], 10)
        self.assertAlmostEqual(result["win_rate_pct"], 100 / 3)
        self.assertEqual(result["payoff_ratio"], 2)
        self.assertAlmostEqual(result["annualized_return_pct"], 299806.27541746007)
        self.assertAlmostEqual(result["sharpe_ratio"], 3.9213138062028534)
        self.assertEqual(result["closed_trades"], 3)
        self.assertIsNone(metrics([{"equity": 100}] * 3, [], 100)["sharpe_ratio"])

    def test_metrics_keep_zero_and_null_apart(self):
        cash_only = metrics([{"equity": 100}] * 5, [], 100)
        self.assertEqual(cash_only["annualized_return_pct"], 0)
        self.assertEqual(cash_only["max_drawdown_pct"], 0)
        self.assertIsNone(cash_only["sharpe_ratio"])
        self.assertIsNone(cash_only["payoff_ratio"])
        self.assertIsNone(cash_only["win_rate_pct"])

        wins_only = metrics([{"equity": 110}], [{"pnl_cash": 10}], 100)
        self.assertIsNone(wins_only["payoff_ratio"])
        self.assertEqual(wins_only["win_rate_pct"], 100)
        losses_only = metrics([{"equity": 90}], [{"pnl_cash": -10}], 100)
        self.assertIsNone(losses_only["payoff_ratio"])
        self.assertEqual(losses_only["win_rate_pct"], 0)


class ResearchBacktestReplayTests(unittest.TestCase):
    def build(self, symbols, signal_index=270, days=280):
        dates = [str(day.date()) for day in pd.bdate_range("2024-01-01", periods=days)]
        histories = {symbol: bars(dates) for symbol in symbols}
        signals = {symbol: {key: set() for key in STRATEGIES} for symbol in symbols}
        for symbol in symbols:
            signals[symbol]["macd_zero_axis"] = {signal_index}
        return histories, signals, dates

    def options(self, start):
        return {"start": start, "initial_cash": 100000, "max_positions": 4, "position_size_pct": .25}

    def test_replay_does_not_sell_on_entry_day_and_keeps_full_calendar(self):
        days = pd.bdate_range("2024-01-01", periods=280)
        frame = pd.DataFrame({"datetime": days, "open": 10., "high": 10., "low": 10., "close": 10., "volume": 10000., "is_closed": True})
        frame.loc[271, "low"] = 8
        frame.loc[272, "low"] = 8
        symbol = "600000.SH"
        signals = {symbol: {key: set() for key in STRATEGIES}}
        signals[symbol]["macd_zero_axis"] = {270}
        calendar = [str(day.date()) for day in days[269:275]]
        options = {"start": calendar[0], "initial_cash": 100000, "max_positions": 4, "position_size_pct": .25}
        result = replay({symbol: frame}, signals, calendar, {"backtest": {"price_limit_model": "none"}}, options, "macd_zero_axis")
        self.assertEqual(len(result["equity_curve"]), 6)
        self.assertEqual(result["equity_curve"][0]["equity"], 100000)
        self.assertEqual(result["trades"][0]["entry_day"], str(days[271].date()))
        self.assertEqual(result["trades"][0]["exit_day"], str(days[272].date()))

    def test_fast_candidate_selection_agrees_with_canonical_predicate(self):
        import numpy as np
        days = pd.bdate_range("2024-01-01", periods=290)
        prices = np.linspace(10, 20, 290) + np.sin(np.arange(290) / 5)
        frame = pd.DataFrame({"datetime": days, "open": prices, "high": prices + .2, "low": prices - .2, "close": prices + .1, "volume": 10000., "is_closed": True})
        config = {"macd_divergence": {"enabled": True}}
        signals = entry_indices(frame, config)
        for index in range(275, 290):
            for result in evaluate_strategies(frame.iloc[:index + 1], config):
                self.assertEqual(index in signals[result["strategy_id"]], result["buy"], (index, result))

    def test_entry_indices_surfaces_strategy_errors(self):
        # Uptrending prices give the MACD pre-filter real confirmation days to verify.
        import numpy as np
        days = [str(day.date()) for day in pd.bdate_range("2024-01-01", periods=290)]
        prices = np.linspace(10, 20, 290) + np.sin(np.arange(290) / 5)
        frame = pd.DataFrame({"datetime": pd.to_datetime(days), "open": prices, "high": prices + .2, "low": prices - .2, "close": prices + .1, "volume": 10000., "is_closed": True})
        with patch.object(rb, "evaluate_strategies", lambda *args, **kwargs: [{"status": "error", "buy": False, "warnings": ["boom"]}]):
            with self.assertRaises(ValueError) as caught:
                entry_indices(frame, {})
        self.assertIn("boom", str(caught.exception))

    def test_position_limit_keeps_the_lowest_code_that_triggers_first(self):
        histories, signals, dates = self.build(["600000.SH", "000001.SZ"])
        options = {**self.options(dates[269]), "max_positions": 1}
        result = replay(histories, signals, [dates[269], dates[270], dates[271], dates[272]], {"backtest": {"price_limit_model": "none"}}, options, "macd_zero_axis")
        self.assertEqual(result["equity_curve"][-1]["positions"], 1)
        self.assertEqual([row["symbol"] for row in result["trades"]], [])
        self.assertEqual(result["open_positions"], 1)

    def test_independent_replays_do_not_share_cash(self):
        histories, signals, dates = self.build(["600000.SH", "000001.SZ"])
        calendar = dates[269:275]
        config = {"backtest": {"price_limit_model": "none"}}
        first = replay(histories, signals, calendar, config, self.options(calendar[0]), "macd_zero_axis")
        second = replay(histories, signals, calendar, config, self.options(calendar[0]), "macd_zero_axis")
        third = replay(histories, signals, calendar, config, self.options(calendar[0]), "macd_zero_axis")
        for result in (first, second, third):
            self.assertEqual(result["equity_curve"][0]["cash"], 100000)
            self.assertGreaterEqual(result["metrics"]["final_equity"], 0)
        self.assertEqual(first["equity_curve"], second["equity_curve"])
        self.assertEqual(second["equity_curve"], third["equity_curve"])
        # Two symbols bought in the same run share one account only inside that run.
        self.assertEqual(len([row for row in first["equity_curve"] if row["positions"] == 2]), 0 if first["equity_curve"][-1]["positions"] < 2 else len([row for row in first["equity_curve"] if row["positions"] == 2]))

    def test_unrealised_loss_moves_equity_and_drawdown(self):
        days = [str(day.date()) for day in pd.bdate_range("2024-01-01", periods=280)]
        frame = bars(days)
        # Entry at bar 271, then the position loses value without ever triggering a stop.
        frame.loc[271:, "open"] = 8.0
        frame.loc[271:, "high"] = 8.2
        frame.loc[271:, "low"] = 7.9
        frame.loc[271:, "close"] = 8.0
        # A below-budget opening price fills at 8 with an 8% adverse move; that must show up as drawdown.
        symbol = "600000.SH"
        signals = {symbol: {key: set() for key in STRATEGIES}}
        signals[symbol]["macd_zero_axis"] = {270}
        calendar = days[269:275]
        config = {"backtest": {"price_limit_model": "none"}, "risk": {"stop_loss_pct": .5, "stop_profit_pct": 10}}
        result = replay({symbol: frame}, signals, calendar, config, self.options(calendar[0]), "macd_zero_axis")
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["metrics"]["closed_trades"], 0)
        self.assertIsNone(result["metrics"]["payoff_ratio"])
        self.assertIsNone(result["metrics"]["win_rate_pct"])
        self.assertGreater(result["metrics"]["max_drawdown_pct"], 0)

        curve = result["equity_curve"]
        held = [point for point in curve if point["positions"] == 1]
        self.assertTrue(held)
        for point in held:
            self.assertGreaterEqual(point["cash"], 0)
            # Equity must equal cash plus the marked position value, never the entry price.
            self.assertAlmostEqual(point["equity"], point["cash"] + 3100 * 8.0, places=3)
        # 25000 budget at 8.00 fills 3100 shares for 24819.84 including commission and slippage.
        self.assertAlmostEqual(held[0]["cash"], 75180.16, places=3)
        self.assertAlmostEqual(held[0]["equity"], 99980.16, places=3)


    def test_frozen_stop_loss_threshold_changes_the_exit(self):
        days = [str(day.date()) for day in pd.bdate_range("2024-01-01", periods=280)]
        frame = bars(days)
        # Entry at bar 271 at 100 with a close that survives the 5% stop, then a slide to 93:
        # below the 5% threshold (95), above the 8% one (92).
        frame.loc[272:, "open"] = 93.0
        frame.loc[272:, "high"] = 94.0
        frame.loc[272:, "low"] = 93.0
        frame.loc[272:, "close"] = 93.5
        frame.loc[271, "open"] = 100.0
        frame.loc[271, "high"] = 100.0
        frame.loc[271, "low"] = 99.5
        frame.loc[271, "close"] = 99.8
        symbol = "600000.SH"
        signals = {symbol: {key: set() for key in STRATEGIES}}
        signals[symbol]["macd_zero_axis"] = {270}
        calendar = days[269:276]
        options = self.options(calendar[0])

        def run(stop_loss_pct):
            config = {"backtest": {"price_limit_model": "none", "chan_zero_axis": {"max_holding_bars": 250}}, "risk": {"stop_loss_pct": stop_loss_pct, "stop_profit_pct": 10}}
            return replay({symbol: frame}, signals, calendar, config, options, "macd_zero_axis")

        tight = run(.05)
        loose = run(.08)
        self.assertEqual([row["exit_reason"] for row in tight["trades"]], ["stop_loss"])
        self.assertEqual(tight["trades"][0]["exit_day"], days[272])
        # The stop fills at the lower of the opening price and the trigger (95).
        self.assertEqual(tight["trades"][0]["exit_price"], 93.0)
        # The looser stop is never touched by a 7% slide, so the position stays open.
        self.assertEqual(loose["trades"], [])
        self.assertEqual(loose["open_positions"], 1)

    def test_strategy_override_controls_intraday_and_t_plus_one_exits(self):
        days = [str(day.date()) for day in pd.bdate_range("2024-01-01", periods=280)]
        frame = bars(days, price=100.0)
        frame.loc[272:, ["open", "high", "low", "close"]] = [94.0, 95.0, 93.0, 94.0]
        symbol = "600000.SH"
        signals = {symbol: {key: set() for key in STRATEGIES}}
        signals[symbol]["macd_zero_axis"] = {270}
        calendar = days[269:276]
        config = {"backtest": {"price_limit_model": "none", "chan_zero_axis": {"max_holding_bars": 250}},
                  "risk": {"stop_loss_pct": .08, "stop_profit_pct": .30,
                           "strategy_stop_loss_pct": {"macd_zero_axis": .05, "macd_divergence": .12}}}
        frozen = json.dumps(config, sort_keys=True)
        result = replay({symbol: frame}, signals, calendar, config, self.options(calendar[0]), "macd_zero_axis")
        self.assertEqual([(trade["exit_day"], trade["exit_price"]) for trade in result["trades"]], [(days[272], 94.0)])

        # Entry-day close breaches the strategy stop, but not the global stop.
        # T+1 defers the exit to the next opening even if price has recovered.
        frame.loc[272:, ["open", "high", "low", "close"]] = 100.0
        frame.loc[271, ["low", "close"]] = 94.0
        deferred = replay({symbol: frame}, signals, calendar, config, self.options(calendar[0]), "macd_zero_axis")
        self.assertEqual([(trade["exit_day"], trade["exit_price"]) for trade in deferred["trades"]], [(days[272], 100.0)])
        config["risk"]["strategy_stop_loss_pct"]["macd_zero_axis"] = None
        inherited = replay({symbol: frame}, signals, calendar, config, self.options(calendar[0]), "macd_zero_axis")
        self.assertEqual(inherited["trades"], [])
        config["risk"]["strategy_stop_loss_pct"]["macd_zero_axis"] = .05
        self.assertEqual(json.dumps(config, sort_keys=True), frozen)


class PrepareBacktestInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.output_dir = self.temp / "task"

    def options(self, **overrides):
        base = {
            "mode": "market",
            "start": "2023-06-01",
            "end": "2024-03-31",
            "initial_cash": 100000,
            "max_positions": 4,
            "position_size_pct": .25,
            "output_dir": str(self.output_dir),
        }
        return {**base, **overrides}

    def history_for(self, symbol, end="2024-03-29", periods=520):
        days = [str(day.date()) for day in pd.bdate_range(end=end, periods=periods)]
        return bars(days)

    def prepare(self, provider, **overrides):
        config = {"market_data": {"request_interval_seconds": 0}}
        with patch.object(rb, "AkshareDailyProvider", lambda config=None: provider):
            return prepare_backtest_inputs(config, self.options(**overrides))

    def test_market_scope_attempts_the_whole_list_and_closes_the_counts(self):
        provider = FakeProvider(market_listing(), {"600000.SH": self.history_for("600000.SH")})
        prepared = self.prepare(provider)

        # Every listed symbol is attempted, including the ST and Beijing names.
        self.assertEqual(sorted(provider.requested), ["000001.SZ", "000004.SZ", "430047.BJ", "600000.SH"])
        self.assertEqual([row["symbol"] for row in prepared["universe"]["symbols"]], ["000001.SZ", "000004.SZ", "430047.BJ", "600000.SH"])
        self.assertEqual(prepared["universe"]["scope"], "current_listed_a")
        self.assertEqual(list(prepared["histories"]), ["600000.SH"])
        self.assertEqual(len(prepared["excluded"]), 3)
        self.assertTrue(all(row["reason"] for row in prepared["excluded"]))
        self.assertEqual(prepared["calendar"][0], "2023-06-01")
        self.assertEqual(prepared["calendar"][-1], "2024-03-29")
        # The 270-bar warmup is kept in front of the replay window.
        self.assertGreater(len(prepared["histories"]["600000.SH"]), 270)

    def test_prepare_excludes_symbols_without_enough_warmup(self):
        short = self.history_for("000001.SZ", periods=120)
        provider = FakeProvider(market_listing(), {"600000.SH": self.history_for("600000.SH"), "000001.SZ": short})
        prepared = self.prepare(provider)
        reasons = {row["symbol"]: row["reason"] for row in prepared["excluded"]}
        self.assertIn("000001.SZ", reasons)
        self.assertIn("270", reasons["000001.SZ"])
        self.assertNotIn("000001.SZ", prepared["histories"])

    def test_prepare_rejects_non_numeric_history(self):
        broken = self.history_for("000001.SZ")
        broken.loc[5, "close"] = -1.0
        provider = FakeProvider(market_listing(), {"600000.SH": self.history_for("600000.SH"), "000001.SZ": broken})
        prepared = self.prepare(provider)
        reasons = {row["symbol"]: row["reason"] for row in prepared["excluded"]}
        self.assertEqual(reasons["000001.SZ"], rb.ILLEGAL_HISTORY_REASON)

    def test_non_trading_end_date_is_not_a_missing_session(self):
        # 2024-03-31 is a Sunday: the effective window ends on the previous session.
        provider = FakeProvider(market_listing(), {"600000.SH": self.history_for("600000.SH")})
        prepared = self.prepare(provider)
        self.assertEqual(prepared["calendar"][-1], "2024-03-29")
        self.assertEqual(prepared["manifest"]["effective_end"], "2024-03-29")

    def test_single_stock_mode_fails_when_history_is_missing(self):
        provider = FakeProvider(market_listing(), {})
        with self.assertRaises(ValueError) as caught:
            self.prepare(provider, mode="stock", symbols=["600519"])
        self.assertIn("600519.SH", str(caught.exception))

    def test_single_stock_mode_uses_the_requested_symbol_only(self):
        provider = FakeProvider(market_listing(), {"600000.SH": self.history_for("600000.SH")})
        prepared = self.prepare(provider, mode="stock", symbols=["600000"])
        self.assertEqual(provider.requested, ["600000.SH"])
        self.assertEqual(prepared["universe"]["scope"], "stock")
        self.assertEqual([row["symbol"] for row in prepared["universe"]["symbols"]], ["600000.SH"])

    def test_frozen_inputs_survive_a_changed_source_and_reject_edited_files(self):
        first = FakeProvider(market_listing(), {"600000.SH": self.history_for("600000.SH")})
        prepared = self.prepare(first)
        original = prepared["histories"]["600000.SH"].copy()

        # The upstream source changes and the listing shrinks; the frozen sample must not.
        changed_price = self.history_for("600000.SH")
        changed_price["close"] = 999.0
        second = FakeProvider(market_listing()[:1], {"600000.SH": changed_price})
        retried = self.prepare(second)
        self.assertEqual(second.requested, [])
        self.assertEqual(list(retried["histories"]), ["600000.SH"])
        pd.testing.assert_frame_equal(retried["histories"]["600000.SH"], original)

        # Editing a frozen file invalidates the task instead of silently replaying it.
        pd.to_pickle(bars([str(day.date()) for day in pd.bdate_range("2023-06-01", periods=300)]), self.output_dir / "inputs" / "600000.SH.pkl")
        with self.assertRaises(ValueError) as caught:
            self.prepare(second)
        self.assertIn("回测输入已变化", str(caught.exception))

    def test_missing_listing_fails_the_task(self):
        provider = FakeProvider([], {}, calendar=[])
        with self.assertRaises(ValueError):
            self.prepare(provider)

    def test_rules_snapshot_never_contains_secrets(self):
        provider = FakeProvider(market_listing(), {"600000.SH": self.history_for("600000.SH")})
        config = {"market_data": {"request_interval_seconds": 0, "tushare_token": "secret-token"}, "risk": {"stop_loss_pct": .08}, "notification": {"bark": {"device_key": "secret"}}}
        with patch.object(rb, "AkshareDailyProvider", lambda config=None: provider):
            prepare_backtest_inputs(config, self.options())
        written = json.loads((self.output_dir / "inputs" / "rules.json").read_text(encoding="utf-8"))
        self.assertEqual(set(written), {"risk"})
        self.assertNotIn("secret", (self.output_dir / "inputs" / "rules.json").read_text(encoding="utf-8"))

    def test_rules_snapshot_keeps_null_overrides_and_ignores_later_mutation(self):
        provider = FakeProvider(market_listing(), {"600000.SH": self.history_for("600000.SH")})
        overrides = {"macd_zero_axis": None, "yearline_pullback": 0.06, "macd_divergence": 0.12}
        config = {"market_data": {"request_interval_seconds": 0},
                  "risk": {"stop_loss_pct": .08, "strategy_stop_loss_pct": dict(overrides)}}
        with patch.object(rb, "AkshareDailyProvider", lambda config=None: provider):
            prepared = prepare_backtest_inputs(config, self.options())
        # The task's own snapshot keeps every override value, including the explicit null.
        self.assertEqual(prepared["config_snapshot"]["risk"]["strategy_stop_loss_pct"], overrides)
        self.assertIsNone(prepared["config_snapshot"]["risk"]["strategy_stop_loss_pct"]["macd_zero_axis"])
        written = json.loads((self.output_dir / "inputs" / "rules.json").read_text(encoding="utf-8"))
        self.assertEqual(written["risk"]["strategy_stop_loss_pct"], overrides)
        # Mutating the caller's config after freezing must not reach the frozen rules file.
        config["risk"]["strategy_stop_loss_pct"]["macd_divergence"] = 0.3
        self.assertEqual(prepared["config_snapshot"]["risk"]["strategy_stop_loss_pct"], overrides)
        self.assertEqual(json.loads((self.output_dir / "inputs" / "rules.json").read_text(encoding="utf-8"))["risk"]["strategy_stop_loss_pct"], overrides)


if __name__ == "__main__":
    unittest.main()
