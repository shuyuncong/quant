"""退出提醒：有持仓且卖出条件触发时，必须真的推送出去。

覆盖三个易错点：
1. 没有持仓时永远不提醒（否则会提示卖一只你没持有的股票）。
2. 提醒的"当天"必须按分析所属交易日算，不能用 K 线时间戳——
   节假日前最后一个交易日收盘后跑分析，用 K 线时间戳会被时效检查丢掉。
3. 同一交易日只提醒一次（重复跑分析不该重复轰炸）。
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import date

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.dirname(BASE_DIR))

from monitor.service import EXIT_RULE_PRIORITY, SignalMonitor
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
            "holdings": [{"symbol": "600519", "name": "贵州茅台", "shares": 100,
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


class ExitNotificationStoreTests(unittest.TestCase):
    def test_last_exit_notification_day_round_trips(self):
        directory = tempfile.mkdtemp(prefix="exit_store_")
        store = SignalStore(os.path.join(directory, "signals.db"))
        self.assertIsNone(store.last_exit_notification_day("600519", "*"))
        store.mark_exit_notified("600519", "*", "2026-10-05")
        self.assertEqual(store.last_exit_notification_day("600519", "*"), "2026-10-05")
        # 事件表与 outbox 都不该被这枚状态污染
        self.assertEqual(store.event_count(), 0)


if __name__ == "__main__":
    unittest.main()
