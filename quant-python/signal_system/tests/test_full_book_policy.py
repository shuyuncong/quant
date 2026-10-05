"""仓位满时的处置决策 —— 统计口径护栏。

易错点: "被丢弃的候选"、"轮换要放弃的剩余收益" 这类统计必须**从原始 trade 记录
回查** (rejections 只带 symbol/signal_day, 没有 pnl), 否则会静默得到 0 条数据
(实测踩到过 ZeroDivisionError: 拒绝记录里没有 pnl_pct)。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import full_book_policy_study as fbs  # noqa: E402


def _trade(symbol: str, signal_day: str, pnl: float, **overrides) -> dict:
    trade = {
        "symbol": symbol,
        "signal_day": signal_day,
        "entry_day": signal_day,
        "exit_day": "2025-02-28",
        "exit_price": 11.0,
        "pnl_pct": pnl,
        "_mark_prices": {"2025-01-10": 10.0, "2025-01-20": 12.0},
    }
    trade.update(overrides)
    return trade


class DescribeTests(unittest.TestCase):
    def test_describe_computes_mean_winrate_and_profit_factor(self):
        block = fbs._describe([_trade("000001", "2025-01-02", 10.0), _trade("000002", "2025-01-02", -5.0)])
        self.assertEqual(block["count"], 2)
        self.assertEqual(block["mean_pnl_pct"], 2.5)
        self.assertEqual(block["win_rate"], 50.0)
        self.assertEqual(block["profit_factor"], 2.0)

    def test_describe_tolerates_empty_input(self):
        # 拒绝记录里没有 pnl 时会筛出空列表 -> 必须是 count=0 而不是崩溃。
        self.assertEqual(fbs._describe([])[ "count"], 0)
        self.assertEqual(fbs._describe([]), {"count": 0})


class ForgoneUpsideTests(unittest.TestCase):
    def test_measures_upside_after_the_new_signal_day(self):
        held = _trade("000001", "2025-01-02", 5.0, entry_day="2025-01-02")
        result = {
            "accepted_entries": [{"symbol": "000001", "signal_day": "2025-01-02", "entry_day": "2025-01-02"}],
            "rejections": [{"symbol": "000009", "signal_day": "2025-01-10", "entry_day": "2025-01-10",
                            "reason": "max_positions"}],
        }
        block = fbs._forgone_upside(result, {("000001", "2025-01-02"): held})
        # 信号日 01-10 收盘 10.0 -> 最终 11.0 = +10%。
        self.assertEqual(block["observations"], 1)
        self.assertEqual(block["mean_remaining_pct"], 10.0)

    def test_ignores_rejections_that_are_not_full_book(self):
        held = _trade("000001", "2025-01-02", 5.0)
        result = {
            "accepted_entries": [{"symbol": "000001", "signal_day": "2025-01-02", "entry_day": "2025-01-02"}],
            "rejections": [{"symbol": "000009", "signal_day": "2025-01-10", "entry_day": "2025-01-10",
                            "reason": "symbol_already_held"}],
        }
        block = fbs._forgone_upside(result, {("000001", "2025-01-02"): held})
        self.assertEqual(block["observations"], 0)

    def test_ignores_new_signals_after_the_held_position_already_exited(self):
        held = _trade("000001", "2025-01-02", 5.0, exit_day="2025-01-15")
        result = {
            "accepted_entries": [{"symbol": "000001", "signal_day": "2025-01-02", "entry_day": "2025-01-02"}],
            "rejections": [{"symbol": "000009", "signal_day": "2025-01-20", "entry_day": "2025-01-20",
                            "reason": "max_positions"}],
        }
        # 持仓 01-15 已平, 01-20 的新信号来临时空仓, 不存在"放弃的收益"。
        block = fbs._forgone_upside(result, {("000001", "2025-01-02"): held})
        self.assertEqual(block["observations"], 0)


if __name__ == "__main__":
    unittest.main()
