"""退出提醒：有持仓且卖出条件触发时，必须真的推送出去。

覆盖三个易错点：
1. 没有持仓时永远不提醒（否则会提示卖一只你没持有的股票）。
2. 提醒的"当天"必须按分析所属交易日算，不能用 K 线时间戳——
   节假日前最后一个交易日收盘后跑分析，用 K 线时间戳会被时效检查丢掉。
3. 同一交易日只提醒一次（重复跑分析不该重复轰炸）。
"""
import copy
import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.dirname(BASE_DIR))

from monitor.service import EXIT_RULE_PRIORITY, SignalMonitor
import pandas as pd
from models import SignalEvent
from notification.signal_notifier import SignalNotifier
from storage.signal_store import SignalStore


def _condition(name, expected, actual, met):
    return {"name": name, "actual": actual, "expected": expected, "met": met}


def _strategy(strategy_id, name, sell_conditions, price=10.0, as_of="2026-09-30"):
    triggered = [item for item in sell_conditions if item.get("met") is True]
    return {
        "strategy_id": strategy_id,
        "name": name,
        "exit_rule": "divergence_trend",
        "as_of": as_of,
        "reference_price": price,
        "sell": bool(triggered),
        "sell_conditions": sell_conditions,
        "sell_signal": None if not triggered else {
            "symbol": "600519",
            "name": "贵州茅台",
            "strategy_id": strategy_id,
            "strategy_name": name,
            "primary_rule": triggered[0]["name"],
            "rules": [item["name"] for item in triggered],
            "signal_type": f"{strategy_id}_exit_{triggered[0]['name']}",
            "reasons": [f"{item['name']}：{item['expected']}（实际 {item['actual']}）" for item in triggered],
            "components": [f"{strategy_id}_exit_{item['name']}" for item in triggered],
            "price": price,
            "as_of": as_of,
        },
    }


class ExitNotificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="exit_notify_")
        self.config = {
            "market_data": {"cache_dir": os.path.join(self.directory, "cache")},
            "runtime": {
                "database_path": os.path.join(self.directory, "signals.db"),
                "output_dir": os.path.join(self.directory, "output"),
            },
            "monitor": {"watchlist": []},
        }
        self.monitor = SignalMonitor(copy.deepcopy(self.config))
        self.monitor.analysis_version = 2
        self.monitor.analysis_cutoff = "2026-10-05T15:30:00"
        self.monitor.portfolio_context = {
            "holdings": [{"symbol": "600519.SH", "name": "贵州茅台", "shares": 100,
                          "cost_price": 12.0, "opened_on": "2026-08-19"}],
            "total_capital": 250000.0,
        }

    def tearDown(self):
        self.monitor.store.set_state("exit_notify:600519:*", "")

    def _events(self, analysis):
        return self.monitor._exit_notification_events(analysis, "600519", "贵州茅台")

    def test_no_alert_without_a_holding(self):
        self.monitor.portfolio_context = {"holdings": [], "total_capital": 250000.0}
        analysis = {"strategies": [_strategy("macd_divergence", "零轴＋底背离",
                     [_condition("持仓成本止损", "成本价 × 0.9200", 8.0, True)])]}
        self.assertEqual(self._events(analysis), [])

    def test_holding_lookup_matches_exchange_suffix(self):
        """分析循环传 6 位代码，Web 快照带 .SH/.SZ；必须规范化后再匹配。"""
        self.assertEqual(self.monitor._holding_for_symbol("600519")["cost_price"], 12.0)
        self.assertEqual(self.monitor._holding_for_symbol("600519.SH")["cost_price"], 12.0)
        self.assertIsNone(self.monitor._holding_for_symbol("000001"))

    def test_alert_requires_a_triggered_condition(self):
        """未触发的条件（met=False）和无法判断的条件（met=None）都不该提醒。"""
        analysis = {"strategies": [_strategy("macd_divergence", "零轴＋底背离", [
            _condition("持仓成本止损", "成本价 × 0.9200", 13.0, False),
            _condition("持有满 40 根日线", ">= 40", None, None),
        ])]}
        self.assertEqual(self._events(analysis), [])

    def test_alert_is_emitted_once_per_trading_day(self):
        analysis = {"strategies": [_strategy("macd_divergence", "零轴＋底背离",
                     [_condition("持仓成本止损", "成本价 × 0.9200", 8.0, True)])]}
        first = self._events(analysis)
        second = self._events(analysis)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(first[0].side, "sell")
        self.assertEqual(first[0].evidence["cost_price"], 12.0)

    def test_confirmed_at_uses_the_analysis_day_not_the_bar_timestamp(self):
        """K 线时间戳可能是节假日前最后一个交易日；用它会被时效检查当过期丢掉。"""
        events = self._events({"strategies": [_strategy(
            "macd_divergence", "零轴＋底背离",
            [_condition("持仓成本止损", "成本价 × 0.9200", 8.0, True)],
            as_of="2026-09-30",
        )]})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].confirmed_at, "2026-10-05")

    def test_multiple_strategies_collapse_into_one_message(self):
        conditions = [_condition("持仓成本止损", "成本价 × 0.9200", 8.0, True),
                      _condition("跌破年线", "收盘价 < MA250", 8.0, True)]
        analysis = {"strategies": [
            _strategy("macd_divergence", "零轴＋底背离", conditions),
            _strategy("macd_zero_axis", "日线零轴金叉", conditions),
        ]}
        events = self._events(analysis)
        self.assertEqual(len(events), 1)
        # 同一条规则被两个策略标出时只报一次
        self.assertEqual(events[0].evidence["score_reasons"].count(
            [item for item in events[0].evidence["score_reasons"] if item.startswith("持仓成本止损")][0]), 1)
        self.assertEqual(events[0].evidence["strategy_names"], ["日线零轴金叉", "零轴＋底背离"])

    def test_stop_loss_outranks_take_profit_as_the_headline_rule(self):
        analysis = {"strategies": [_strategy("macd_zero_axis", "日线零轴金叉", [
            _condition("固定止盈", "成本价 × 1.3000", 20.0, True),
            _condition("持仓成本止损", "成本价 × 0.9200", 8.0, True),
        ])]}
        events = self._events(analysis)
        self.assertEqual(events[0].evidence["rules"][0], "持仓成本止损")

    def test_event_carries_cost_and_pnl_for_the_message(self):
        events = self._events({"strategies": [_strategy(
            "macd_zero_axis", "日线零轴金叉",
            [_condition("持仓成本止损", "成本价 × 0.9200", 8.0, True)], price=8.0,
        )]})
        evidence = events[0].evidence
        self.assertEqual(evidence["cost_price"], 12.0)
        self.assertEqual(evidence["pnl_pct"], -33.33)
        self.assertEqual(evidence["notification_kind"], "strategy_exit")
        self.assertTrue(evidence["actionable"])

    def test_message_renders_cost_and_triggers(self):
        events = self._events({"strategies": [_strategy(
            "macd_zero_axis", "日线零轴金叉",
            [_condition("持仓成本止损", "成本价 × 0.9200", 8.0, True)], price=8.0,
        )]})
        text = SignalNotifier._markdown(events[0].to_payload())
        self.assertIn("策略退出提醒", text)
        self.assertIn("600519", text)
        self.assertIn("12.000", text)
        self.assertIn("-33.33%", text)
        self.assertIn("持仓成本止损", text)

    def test_message_says_unknown_instead_of_zero_when_cost_is_missing(self):
        """成本未知时印 +0.00% 会让人以为"不亏不赚"，必须显式写未知。"""
        event = SignalEvent(
            symbol="600519", name="贵州茅台", timeframe="1d",
            signal_type="macd_zero_axis_exit_跌破年线", side="sell", price=10.0,
            structure_time="2026-10-05", confirmed_at="2026-10-05", score=0,
            evidence={"notification_kind": "strategy_exit", "score_reasons": ["跌破年线"],
                      "cost_price": None, "pnl_pct": None, "strategy_name": "日线零轴金叉"},
        )
        text = SignalNotifier._markdown(event.to_payload())
        self.assertIn("成本价：未知", text)
        self.assertNotIn("+0.00%", text)

    def test_exit_rule_priority_covers_every_produced_rule(self):
        """所有 registry 产出的退出条件名都必须有优先级，否则会掉到 99 的兜底。"""
        for rule in ("持仓成本止损", "固定止盈", "持仓超时", "跌破年线", "已确认日线顶背离"):
            self.assertIn(rule, EXIT_RULE_PRIORITY)

    def test_holding_is_enriched_with_this_symbols_trades_only(self):
        """进度反推依赖成交记录：注入持仓时必须只带本股票的成交，并带上样本完整度。"""
        self.monitor.portfolio_context = {
            "holdings": [{"symbol": "600519", "name": "贵州茅台", "shares": 500,
                          "cost_price": 12.0, "opened_on": "2026-08-19"}],
            "trades": [
                {"symbol": "600519", "side": "buy", "quantity": 1000, "price": 12.0, "traded_at": "2026-08-19"},
                {"symbol": "600519", "side": "sell", "quantity": 500, "price": 13.0, "traded_at": "2026-09-20"},
                # 别只股票的成交绝不能混进来，否则会算错原始仓位。
                {"symbol": "000001", "side": "buy", "quantity": 9999, "price": 9.0, "traded_at": "2026-08-19"},
            ],
            "history_complete": True,
        }
        enriched = self.monitor._holding_with_trade_history(self.monitor.portfolio_context["holdings"][0])
        self.assertEqual([row["symbol"] for row in enriched["trades"]], ["600519", "600519"])
        self.assertTrue(enriched["history_complete"])
        # 不修改原对象：快照仍被其他调用方共享。
        self.assertNotIn("trades", self.monitor.portfolio_context["holdings"][0])

    def test_holding_without_trades_is_passed_through_unchanged(self):
        """没有成交记录时不要塞空列表：规则层要能区分"没记录"与"记录为空"。"""
        holding = {"symbol": "600519", "shares": 100, "cost_price": 12.0}
        self.monitor.portfolio_context = {"holdings": [holding], "trades": []}
        self.assertIs(self.monitor._holding_with_trade_history(holding), holding)

    def test_holding_gains_an_open_date_from_the_first_buy(self):
        """持仓表没有建仓日，规则却要用它算持有天数；取最早一笔买入补上。"""
        holding = {"symbol": "600519", "shares": 500, "cost_price": 12.0}
        self.monitor.portfolio_context = {
            "holdings": [holding], "history_complete": True,
            "trades": [
                {"symbol": "600519", "side": "sell", "quantity": 100, "traded_at": "2026-09-20"},
                {"symbol": "600519", "side": "buy", "quantity": 600, "traded_at": "2026-08-19 10:00:00"},
                {"symbol": "600519", "side": "buy", "quantity": 400, "traded_at": "2026-07-01 10:00:00"},
            ],
        }
        enriched = self.monitor._holding_with_trade_history(holding)
        self.assertEqual(enriched["opened_on"], "2026-07-01")

    def test_existing_open_date_is_never_overwritten(self):
        """台账将来带上建仓日时，成交记录只能补空，不能覆盖。"""
        holding = {"symbol": "600519", "shares": 100, "cost_price": 12.0, "opened_on": "2026-09-01"}
        self.monitor.portfolio_context = {
            "holdings": [holding], "history_complete": True,
            "trades": [{"symbol": "600519", "side": "buy", "quantity": 100, "traded_at": "2026-01-01"}],
        }
        self.assertEqual(self.monitor._holding_with_trade_history(holding)["opened_on"], "2026-09-01")

    def test_plan_reduction_never_outranks_a_full_exit(self):
        """分批止盈只是"卖一部分"，头条规则必须是整仓离场（止损/顶背离）而不是它。"""
        self.assertLess(EXIT_RULE_PRIORITY["持仓成本止损"], EXIT_RULE_PRIORITY["分批止盈"])
        self.assertLess(EXIT_RULE_PRIORITY["已确认日线顶背离"], EXIT_RULE_PRIORITY["分批止盈"])
        self.assertLess(EXIT_RULE_PRIORITY["跌破年线"], EXIT_RULE_PRIORITY["分批止盈"])
        self.assertLess(EXIT_RULE_PRIORITY["持仓超时"], EXIT_RULE_PRIORITY["分批止盈"])

    PLAN_ADVICE = {"taken": 0, "pending": 1, "breakeven_active": False,
                   "next": {"trigger_price": 11.5, "quantity": 500},
                   "summary": "下一档 +15% 触发价 11.500，卖出 500 股"}

    def _plan_strategy(self):
        """真实引擎输出的形状：分批止盈条件 + 策略级 exit_plan_advice。"""
        strategy = _strategy("macd_zero_axis", "日线零轴金叉", [
            {**_condition("分批止盈", "分批计划 0/1 档已执行", 11.6, True),
             "details": self.PLAN_ADVICE, "trigger_basis": "日线最高价"},
        ])
        strategy["exit_plan_advice"] = self.PLAN_ADVICE
        return strategy

    def test_plan_reduction_event_carries_the_executable_numbers(self):
        """推送里必须带上"卖多少股、什么价触发"，否则用户只能看到"该减仓了"。"""
        events = self._events({"strategies": [self._plan_strategy()]})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].evidence["rules"][0], "分批止盈")
        self.assertEqual(events[0].evidence["exit_plan_advice"]["next"]["quantity"], 500)
        self.assertIn("11.500", events[0].evidence["exit_plan_advice"]["summary"])

    def test_plan_reduction_message_survives_rendering(self):
        """分批减仓的文案必须能落进推送正文，含触发价、股数与尾仓止损位。"""
        event = self._plan_event()
        text = SignalNotifier._markdown(event.to_payload())
        self.assertIn("策略退出提醒", text)
        self.assertIn("分批止盈", text)
        self.assertIn("分批减仓：下一档 +15% 触发价 11.500，卖出 500 股", text)

    def test_plan_summary_is_not_printed_twice_when_it_is_the_headline_rule(self):
        """分批止盈当头条规则时摘要已出现在"触发条件"里，不能再单独印一遍。"""
        event = self._plan_event()
        event.evidence["score_reasons"] = [f"分批止盈：{self.PLAN_ADVICE['summary']}（实际 11.6）"]
        text = SignalNotifier._markdown(event.to_payload())
        self.assertEqual(text.count(self.PLAN_ADVICE["summary"]), 1)

    def test_plan_summary_still_shown_when_another_rule_is_the_headline(self):
        """止损才是头条规则时，分批进度仍须单独印出来，否则用户看不到该减仓。"""
        event = self._plan_event()
        event.evidence["score_reasons"] = ["持仓成本止损：成本价 × 1.0000（实际 10.0）"]
        event.evidence["rules"] = ["持仓成本止损"]
        text = SignalNotifier._markdown(event.to_payload())
        self.assertIn("分批减仓：下一档 +15% 触发价 11.500，卖出 500 股", text)

    def test_breakeven_message_states_the_tail_stop_price(self):
        """保本生效时必须写出尾仓止损价，否则"上移到成本价"无从执行。"""
        event = self._plan_event()
        event.evidence["exit_plan_advice"] = {
            "summary": "分批计划 1/1 档已执行；保本止损已生效。",
            "breakeven_active": True, "breakeven_stop_price": 10.0,
        }
        text = SignalNotifier._markdown(event.to_payload())
        self.assertIn("尾仓止损：已上移到成本价 10.000", text)

    def _plan_event(self):
        return self._events({"strategies": [self._plan_strategy()]})[0]


class ExitNotificationStoreTests(unittest.TestCase):
    def test_last_exit_notification_day_round_trips(self):
        directory = tempfile.mkdtemp(prefix="exit_store_")
        store = SignalStore(os.path.join(directory, "signals.db"))
        self.assertIsNone(store.last_exit_notification_day("600519", "*"))
        store.mark_exit_notified("600519", "*", "2026-10-05")
        self.assertEqual(store.last_exit_notification_day("600519", "*"), "2026-10-05")
        # 事件表与 outbox 都不该被这枚状态污染
        self.assertEqual(store.event_count(), 0)


class HoldingAttributionTests(unittest.TestCase):
    """持仓归属策略的端到端链路：portfolio_context.holdings → 规则层 → 推送建议。

    这条链路此前是断的：quant.holdings 没有 strategy_id，stop_owner 只认
    holding["strategy_id"]，所以线上持仓一律落到全局口径，按策略的分批计划形同虚设。
    """
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="holding_owner_")
        self.monitor = SignalMonitor({"market_data": {"cache_dir": os.path.join(self.directory, "cache")},
                                      "runtime": {"database_path": os.path.join(self.directory, "signals.db"),
                                                  "output_dir": os.path.join(self.directory, "output")},
                                      "monitor": {"watchlist": []}})
        self.monitor.analysis_cutoff = "2026-10-05T15:30:00"

    def _plan(self, holding):
        """用真实引擎跑一遍：成本 100、收 120（越 +15% 首档，未到 +30%）。"""
        from strategy.registry import evaluate_strategies
        self.monitor.portfolio_context = {"holdings": [holding], "trades": []}
        enriched = self.monitor._holding_with_trade_history(holding)
        frame = pd.DataFrame({"datetime": pd.bdate_range("2026-01-05", periods=120),
                              "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0})
        frame.loc[frame.index[-1], ["high", "close"]] = 120.0
        config = {"risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.30,
                           "strategy_exit_plan": {"macd_zero_axis": {"initial_fraction": .5, "first_gain": .15}},
                           "strategy_stop_loss_pct": {"macd_zero_axis": 0.10}}}
        return {row["strategy_id"]: row for row in evaluate_strategies(frame, config, enriched)}

    def test_tagged_holding_uses_its_own_strategy_plan_and_stop(self):
        rows = self._plan({"symbol": "600519", "name": "贵州茅台", "shares": 1000,
                           "cost_price": 100.0, "opened_on": "2026-01-05",
                           "strategy_id": "macd_zero_axis"})
        zero_axis = rows["macd_zero_axis"]
        self.assertEqual(zero_axis["stop_loss"]["pct"], 0.10)
        self.assertEqual(zero_axis["stop_loss"]["strategy_id"], "macd_zero_axis")
        advice = zero_axis["exit_plan_advice"]
        self.assertEqual(advice["next"]["quantity"], 500)
        self.assertEqual(advice["next"]["trigger_price"], 115.0)
        # 计划生效即接管止盈侧：分批止盈取代固定 30% 止盈，后者不再产生离场条件。
        names = [row["name"] for row in zero_axis["sell_conditions"]]
        self.assertIn("分批止盈", names)
        self.assertNotIn("固定止盈", names)
        # 归属是"这一笔持仓"的属性：别的策略评估同一笔持仓时，也必须用归属策略的
        # 止损与计划，绝不各自换成自己的口径。
        divergence = rows["macd_divergence"]
        self.assertEqual(divergence["stop_loss"]["pct"], 0.10)
        self.assertEqual(divergence["stop_loss"]["strategy_id"], "macd_zero_axis")
        self.assertEqual(divergence["exit_plan_advice"]["next"]["quantity"], 500)
        self.assertIn("分批止盈", [row["name"] for row in divergence["sell_conditions"]])

    def test_untagged_holding_never_borrows_the_assessed_strategy(self):
        """未归属（空串/缺失）必须回到全局口径，绝不借用当天被评估策略的计划。"""
        for unowned in ("", None):
            rows = self._plan({"symbol": "600519", "shares": 1000, "cost_price": 100.0,
                               "opened_on": "2026-01-05", "strategy_id": unowned})
            zero_axis = rows["macd_zero_axis"]
            self.assertIsNone(zero_axis["exit_plan_advice"], unowned)
            self.assertEqual(zero_axis["stop_loss"]["pct"], 0.08, unowned)
            self.assertEqual(zero_axis["stop_loss"]["strategy_id"], None, unowned)
            names = [row["name"] for row in zero_axis["sell_conditions"]]
            self.assertIn("固定止盈", names, unowned)
            self.assertNotIn("分批止盈", names, unowned)


if __name__ == "__main__":
    unittest.main()
