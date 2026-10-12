import unittest
import numpy as np
import pandas as pd
from strategy.registry import (BREAKEVEN_STOP_SOURCE, DEFAULT_STOP_LOSS_PCT, STRATEGIES, VERSION,
                               evaluate_strategies, evaluate_exits, exit_parameters,
                               exit_plan_advice, plan_progress_from_trades, resolve_exit_plan,
                               resolve_stop_loss)
from strategy.yearline import add_yearline_indicators, pullback_signal_at


class StrategyRegistryTests(unittest.TestCase):
    def frame(self):
        prices = np.linspace(10, 25, 320) + np.sin(np.arange(320) / 7)
        return pd.DataFrame({"datetime": pd.bdate_range("2024-01-01", periods=320), "open": prices,
                             "high": prices + .3, "low": prices - .3, "close": prices + .1,
                             "volume": np.full(320, 10000), "is_closed": True})

    def test_exit_plan_replaces_the_fixed_take_profit_level(self):
        holding = {"shares": 100, "cost_price": 100, "opened_on": "2024-01-02", "strategy_id": "macd_zero_axis"}
        base = exit_parameters({}, "macd_zero_axis", holding)
        self.assertEqual(base["take_profit_pct"], 0.30)
        self.assertEqual(base["exit_plan"], {})
        planned = exit_parameters({"risk": {"exit_plan": {"initial_fraction": .5, "first_gain": .15}}},
                                  "macd_zero_axis", holding)
        self.assertIsNone(planned["take_profit_pct"])
        self.assertEqual(planned["exit_plan"]["levels"], [{"gain": .15, "fraction": .5}])

    def test_ladder_rule_reports_informational_never_triggers(self):
        frame = self.frame()
        frame.loc[319, "close"] = 500  # far beyond any take-profit level
        holding = {"shares": 100, "cost_price": 100, "opened_on": "2024-01-02", "strategy_id": "macd_zero_axis"}
        config = {"risk": {"exit_plan": {"initial_fraction": .5, "first_gain": .15}},
                  "backtest": {"chan_zero_axis": {"max_holding_bars": 10_000}}}
        checks, _ = evaluate_exits(frame, config, "macd_zero_axis", holding)
        self.assertNotIn("固定止盈", [row["name"] for row in checks])
        ladder = next(row for row in checks if row["name"] == "分批止盈")
        # The plan owner executes ladder exits on the bar high; the rule layer must not
        # mark it met and dump the whole tail at the next opening.
        self.assertIsNone(ladder["met"])

    def test_trailing_only_plan_reports_the_trailing_rule(self):
        frame = self.frame()
        holding = {"shares": 100, "cost_price": float(frame["close"].iloc[319]), "opened_on": "2024-01-02",
                   "strategy_id": "macd_zero_axis"}
        config = {"risk": {"exit_plan": {"trailing_activation_gain": .15, "trailing_pct": .08}}}
        checks, _ = evaluate_exits(frame, config, "macd_zero_axis", holding)
        names = [row["name"] for row in checks]
        self.assertIn("移动止盈", names)
        self.assertNotIn("固定止盈", names)

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

    def test_breakeven_tail_stop_moves_to_cost_after_first_level(self):
        holding = {"shares": 500, "cost_price": 100, "opened_on": "2024-01-02", "strategy_id": "macd_zero_axis"}
        config = {"risk": {"stop_loss_pct": .08, "exit_plan": {"initial_fraction": .5, "first_gain": .15,
                                                              "protect_tail": "breakeven"}}}
        # 首档未执行时仍是普通 8% 止损。
        self.assertEqual(exit_parameters(config, "macd_zero_axis", holding)["stop_loss_source"], "global")
        # 首档成交后，实盘尾仓止损上移到成本价；回测的 replay 自己维护档位进度，传不到这里。
        taken = {**holding, "exit_levels_taken": 1}
        rules = exit_parameters(config, "macd_zero_axis", taken)
        self.assertEqual(rules["stop_loss_pct"], 0.0)
        self.assertEqual(rules["stop_loss_source"], "plan_breakeven")
        # 未开启保本时，即使档位已执行，止损口径也不变。
        no_be = {"risk": {"stop_loss_pct": .08, "exit_plan": {"initial_fraction": .5, "first_gain": .15}}}
        self.assertEqual(exit_parameters(no_be, "macd_zero_axis", taken)["stop_loss_pct"], .08)

    def test_advice_gives_the_next_level_price_and_share_count(self):
        advice = exit_plan_advice(self.plan_config(), "macd_zero_axis", self.plan_holding(), self.uptrend_frame())
        self.assertEqual(advice["next"]["trigger_price"], 115.0)
        self.assertEqual(advice["next"]["quantity"], 500)
        self.assertEqual(advice["original_shares"], 1000)
        self.assertTrue(advice["triggered_today"])
        self.assertIn("卖出 500 股", advice["summary"])

    def test_advice_infers_original_shares_from_levels_already_taken(self):
        # 已执行 50% 后持仓剩 500 股：下一档基数必须是原始 1000 股，而不是按 500 股算成 250 股。
        advice = exit_plan_advice(self.plan_config(), "macd_zero_axis",
                                  {**self.plan_holding(), "shares": 500, "exit_levels_taken": 1}, self.uptrend_frame())
        self.assertEqual(advice["original_shares"], 1000)
        self.assertEqual(advice["breakeven_stop_price"], 100.0)
        self.assertIn("保本止损已生效", advice["summary"])

    def test_opted_out_strategy_and_missing_holding_have_no_advice(self):
        self.assertIsNone(exit_plan_advice(self.plan_config(), "yearline_pullback",
                                          {**self.plan_holding(), "strategy_id": "yearline_pullback"}, self.uptrend_frame()))
        self.assertIsNone(exit_plan_advice(self.plan_config(), "macd_zero_axis", None, self.uptrend_frame()))

    def test_evaluate_exits_keeps_plan_rows_informational_for_the_engine(self):
        holding = {"shares": 1000, "cost_price": 10.0, "opened_on": "2024-01-02", "strategy_id": "macd_zero_axis"}
        config = {"risk": {"stop_loss_pct": .08, "exit_plan": {"initial_fraction": .5, "first_gain": .15}}}
        conditions, _ = evaluate_exits(self.uptrend_frame(), config, "macd_zero_axis", holding)
        row = next(item for item in conditions if item["name"] == "分批止盈")
        # 回测口径由 replay 维护档位：规则层不能把 met 标成 True，否则尾仓会被一次性卖光。
        self.assertIsNone(row["met"])
        self.assertEqual(row["details"]["next"]["trigger_price"], 11.5)

    def test_live_layer_marks_a_triggered_reduction_level_as_met(self):
        frame = self.uptrend_frame()
        holding = {"shares": 1000, "cost_price": 10.0, "opened_on": "2024-01-02", "strategy_id": "macd_zero_axis"}
        config = {"risk": {"stop_loss_pct": .08, "exit_plan": {"initial_fraction": .5, "first_gain": .15}}}
        rows = evaluate_strategies(frame, config, holding)
        item = next(row for row in rows if row["strategy_id"] == "macd_zero_axis")
        plan_row = next(cond for cond in item["sell_conditions"] if cond["name"] == "分批止盈")
        self.assertIs(plan_row["met"], True)
        self.assertEqual(plan_row["trigger_basis"], "日线最高价")
        # 档位触发属于"有卖出信号"，但只卖一档，不是整仓离场。
        self.assertTrue(item["sell"])
        self.assertEqual(item["exit_plan_advice"]["next"]["quantity"], 500)

    def plan_config(self):
        """共用配置：日线零轴金叉开启半仓+保本，年线趋势显式关闭，底背离继承全局。"""
        return {"risk": {"stop_loss_pct": .08, "stop_profit_pct": .30,
                         "strategy_exit_plan": {
                             "macd_zero_axis": {"initial_fraction": .5, "first_gain": .15, "protect_tail": "breakeven"},
                             "yearline_pullback": {},
                             "macd_divergence": None}},
                "backtest": {"chan_zero_axis": {"max_holding_bars": 40}}}

    def plan_holding(self):
        return {"symbol": "600000.SH", "shares": 1000, "cost_price": 100.0,
                "opened_on": "2024-01-02", "strategy_id": "macd_zero_axis"}

    def uptrend_frame(self):
        """成本 100、涨到 120：越过 +15% 首档（115），未到 +30%。"""
        prices = np.linspace(100, 120, 320)
        return pd.DataFrame({"datetime": pd.bdate_range("2024-01-01", periods=320), "open": prices,
                             "high": prices * 1.002, "low": prices * .998, "close": prices,
                             "volume": np.full(320, 10000), "is_closed": True})

    def test_progress_is_reconstructed_from_trade_records(self):
        """持仓台账没有档位字段，成交记录必须能反推出"已经减过一档"。"""
        trades = [{"side": "buy", "quantity": 1000, "price": 10.0, "traded_at": "2024-01-02"},
                  {"side": "sell", "quantity": 500, "price": 11.5, "traded_at": "2024-06-03"}]
        levels = [{"fraction": .5}, {"fraction": .5}]
        progress = plan_progress_from_trades({"trades": trades}, levels)
        self.assertEqual(progress["taken"], 1)
        self.assertEqual(progress["original_shares"], 1000)
        self.assertEqual(progress["sold_fraction"], 0.5)
        # 一个档位都还没卖时反推为 0 档，原始仓位就是累计买入。
        progress_none = plan_progress_from_trades({"trades": trades[:1]}, levels)
        self.assertEqual(progress_none["taken"], 0)
        self.assertEqual(progress_none["original_shares"], 1000)

    def test_advice_uses_trades_instead_of_assuming_zero_levels(self):
        """实盘最容易出错的场景：已经卖过第一档，再提醒就该是"卖第二档"而不是重复第一档。"""
        config = {"risk": {"stop_loss_pct": .08, "exit_plan": {
            "first_gain": .15, "initial_fraction": .5, "protect_tail": "breakeven",
            "tiers": [{"gain": .30, "fraction": .5}]}}}
        holding = {"symbol": "600000.SH", "shares": 500, "cost_price": 10.0, "opened_on": "2024-01-02",
                   "history_complete": True,
                   "trades": [{"side": "buy", "quantity": 1000, "price": 10.0, "traded_at": "2024-01-02"},
                              {"side": "sell", "quantity": 500, "price": 11.5, "traded_at": "2024-06-03"}]}
        advice = exit_plan_advice(config, "macd_zero_axis", holding, self.uptrend_frame())
        self.assertEqual(advice["taken"], 1)
        self.assertEqual(advice["original_shares"], 1000)
        self.assertTrue(advice["breakeven_active"])
        self.assertEqual(advice["breakeven_stop_price"], 10.0)
        self.assertEqual(advice["next"]["gain"], .30)
        # 第二档按原始 1000 股的一半 = 500 股，而不是按剩余 500 股的一半 = 250 股。
        self.assertEqual(advice["next"]["quantity"], 500)
        self.assertEqual(advice["basis"], "按成交记录还原的已执行档位")

    def test_incomplete_history_is_disclosed_as_an_estimate(self):
        """成交记录可能不完整：反推结果必须标明是估算，不能装作精确。"""
        config = {"risk": {"exit_plan": {"first_gain": .15, "initial_fraction": .5}}}
        holding = {"symbol": "600000.SH", "shares": 500, "cost_price": 10.0, "history_complete": False,
                   "trades": [{"side": "buy", "quantity": 1000, "price": 10.0},
                              {"side": "sell", "quantity": 500, "price": 11.5}]}
        advice = exit_plan_advice(config, "macd_zero_axis", holding, self.uptrend_frame())
        self.assertEqual(advice["basis"], "按成交记录估算的已执行档位（成交记录可能不完整）")
        self.assertIn("估算", advice["summary"])

    def test_open_position_without_buy_history_is_left_unknown(self):
        """期初持仓没有买入记录（卖出多于已知买入）时不能瞎猜原始仓位。"""
        partially_known = {"trades": [{"side": "sell", "quantity": 500, "price": 11.5}]}
        self.assertIsNone(plan_progress_from_trades(partially_known, [{"fraction": .5}]))
        self.assertIsNone(plan_progress_from_trades({}, [{"fraction": .5}]))
        self.assertIsNone(plan_progress_from_trades({"trades": []}, [{"fraction": .5}]))

    def test_progress_never_regresses_below_the_ledger_value(self):
        """台账将来带上档位字段时，成交记录只能把它往前推，不能倒退。"""
        config = {"risk": {"exit_plan": {"first_gain": .15, "initial_fraction": .5,
                                         "tiers": [{"gain": .30, "fraction": .5}]}}}
        # 台账说已执行 2 档，成交记录只显示 1 档（样本不全）→ 保留 2 档。
        holding = {"symbol": "600000.SH", "shares": 500, "cost_price": 10.0, "exit_levels_taken": 1,
                   "history_complete": True,
                   "trades": [{"side": "buy", "quantity": 1000, "price": 10.0},
                              {"side": "sell", "quantity": 500, "price": 11.5}]}
        advice = exit_plan_advice(config, "macd_zero_axis", holding, self.uptrend_frame())
        self.assertEqual(advice["taken"], 1)
        self.assertEqual(advice["basis"], "已执行档位")

    def test_replay_shaped_holding_keeps_the_plain_stop(self):
        """回放的持仓是引擎自己拼的合成持仓（只有价格/数量/建仓日，没有成交记录）。

        档位进度由 replay 内部维护，所以规则层绝不能凭合成持仓就把止损移到成本价，
        否则回测口径会和归档结果分叉。
        """
        config = {"risk": {"stop_loss_pct": .08, "exit_plan": {
            "first_gain": .15, "initial_fraction": .5, "protect_tail": "breakeven"}}}
        synthetic = {"shares": 500, "cost_price": 10.0, "opened_on": "2024-03-04", "strategy_id": "macd_zero_axis"}
        rules = exit_parameters(config, "macd_zero_axis", synthetic)
        self.assertEqual(rules["stop_loss_pct"], .08)
        self.assertNotEqual(rules["stop_loss_source"], BREAKEVEN_STOP_SOURCE)
        # 建议层也不该凭合成持仓宣称"保本已生效"。
        advice = exit_plan_advice(config, "macd_zero_axis", synthetic, self.uptrend_frame())
        self.assertFalse(advice["breakeven_active"])
        self.assertEqual(advice["taken"], 0)
        self.assertEqual(advice["basis"], "未记录已执行档位，按当前仓位视为原始仓位")

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

    def test_shipped_defaults_keep_the_single_take_profit_exit(self):
        from copy import deepcopy
        from utils.helpers import DEFAULT_CONFIG
        config = deepcopy(DEFAULT_CONFIG)
        # 未配置计划时，三策略都必须维持旧的固定止盈口径，不能被新字段静默改成"无止盈"。
        self.assertIsNone(config["risk"]["exit_plan"])
        for strategy_id in STRATEGIES:
            rules = exit_parameters(config, strategy_id)
            self.assertEqual(rules["exit_plan"], {})
            self.assertEqual(rules["take_profit_pct"], 0.30)

    def test_one_exit_plan_owner_decides_the_take_profit_side(self):
        holding = {"shares": 100, "cost_price": 100, "opened_on": "2024-01-02", "strategy_id": "macd_zero_axis"}
        config = {"risk": {"stop_loss_pct": .08, "stop_profit_pct": .30,
                           "exit_plan": {"initial_fraction": .5, "first_gain": .15}}}
        # A global plan turns off the fixed take-profit for the owning strategy...
        self.assertIsNone(exit_parameters(config, "macd_zero_axis", holding)["take_profit_pct"])
        # ...unless that strategy explicitly opts out with an empty plan.
        config["risk"]["strategy_exit_plan"] = {"macd_zero_axis": {}}
        self.assertEqual(exit_parameters(config, "macd_zero_axis", holding)["take_profit_pct"], 0.30)
        # The plan follows the position's owning strategy, exactly like the stop; asking
        # for a different strategy id with the same holding must not swap the ruleset.
        self.assertEqual(exit_parameters(config, "macd_divergence", holding)["take_profit_pct"], 0.30)
        divergence_holding = {**holding, "strategy_id": "macd_divergence"}
        self.assertIsNone(exit_parameters(config, "macd_divergence", divergence_holding)["take_profit_pct"])

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
