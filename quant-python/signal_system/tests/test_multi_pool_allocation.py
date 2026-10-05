"""多池资金分配实验的护栏。

易错点 (实测踩过, 已修): 把多个子账户的权益曲线"按当天有数据的日子相加",
会让子账户在缺失日被当成 0, 曲线出现锯齿, 最大回撤被算成 80% 这种荒唐值。
正确做法是按全部日期的并集推进, 缺失日子账户沿用上一日权益。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import multi_pool_allocation_study as mp  # noqa: E402


class CombineEquityTests(unittest.TestCase):
    def test_missing_days_carry_forward_instead_of_dropping_to_zero(self):
        # 子账户 A 只在第 1、3 天有数据点; B 只在第 2、3 天有。
        a = [{"day": "2025-01-01", "equity": 100.0}, {"day": "2025-01-03", "equity": 110.0}]
        b = [{"day": "2025-01-02", "equity": 50.0}, {"day": "2025-01-03", "equity": 60.0}]
        combined = mp._combine_equity([a, b], [100.0, 50.0])
        days = [point["day"] for point in combined]
        self.assertEqual(days, ["2025-01-01", "2025-01-02", "2025-01-03"])
        # B 在第 1 天还没建仓, 但它的 50 元现金仍属于账户 -> 第 1 天合计 150;
        # 第 2 天 A 沿用 100 (不是 0), 合计 150; 第 3 天 110+60=170。
        self.assertEqual([point["equity"] for point in combined], [150.0, 150.0, 170.0])

    def test_combined_drawdown_is_not_inflated_by_date_misalignment(self):
        a = [{"day": "2025-01-01", "equity": 100.0}, {"day": "2025-01-03", "equity": 100.0}]
        b = [{"day": "2025-01-02", "equity": 100.0}, {"day": "2025-01-03", "equity": 100.0}]
        combined = mp._combine_equity([a, b], [100.0, 100.0])
        # 两边都持平 (含各自未投入的现金) -> 组合不亏, 回撤必须是 0
        # (错算法把缺失日当 0, 会给出约 50%)。
        self.assertEqual([point["equity"] for point in combined], [200.0, 200.0, 200.0])
        self.assertEqual(mp._combined_max_drawdown(combined), 0.0)

    def test_drawdown_measures_a_real_peak_to_trough(self):
        curve = [
            {"day": "2025-01-01", "equity": 100.0},
            {"day": "2025-01-02", "equity": 120.0},
            {"day": "2025-01-03", "equity": 90.0},
            {"day": "2025-01-04", "equity": 110.0},
        ]
        # 峰值 120 -> 谷底 90 = 25%。
        self.assertEqual(mp._combined_max_drawdown(curve), 25.0)


class PoolTypesTests(unittest.TestCase):
    def test_every_pool_maps_to_at_least_one_signal_type(self):
        for pool, types in mp.POOL_TYPES.items():
            self.assertTrue(types, pool)
            self.assertTrue(all(isinstance(item, str) for item in types), pool)

    def test_types_for_flattens_in_priority_order(self):
        flattened = mp._types_for(("diverge", "yearline"))
        self.assertEqual(flattened[0], "macd_divergence_bottom")
        self.assertIn("yearline_A_breakout", flattened)
        self.assertNotIn("macd_golden_cross_pullback_confirmed_above", flattened)


if __name__ == "__main__":
    unittest.main()
