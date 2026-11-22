import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import first_board_four_strategies_study as four
import first_board_low_open_fill_study as study


def trade(symbol: str, gross_entry: float, gross_exit: float) -> dict:
    entry_fee, exit_fee = four._fee(gross_entry), four._fee(gross_exit, sell=True)
    return {
        "symbol": symbol,
        "entry_date": pd.Timestamp("2024-03-04"),
        "gross_entry": gross_entry,
        "gross_exit": gross_exit,
        "exit_fee": exit_fee,
        "net_return": (gross_exit - exit_fee) / (gross_entry + entry_fee) - 1.0,
    }


TRADES = pd.DataFrame(
    [
        trade("000001", 100_000.0, 103_000.0),  # 头 5 分钟均价比开盘高 2%
        trade("000002", 100_000.0, 103_000.0),  # 头 5 分钟只成交 50 万
        trade("000003", 100_000.0, 103_000.0),  # 没有分钟线
    ]
)
BARS = {
    "000001": {"open": 10.0, "high": 10.5, "low": 10.0, "volume": 1e6, "amount": 10.2e6},
    "000002": {"open": 10.0, "high": 10.0, "low": 10.0, "volume": 5e4, "amount": 5e5},
    "000003": None,
}


class FillTableTests(unittest.TestCase):
    def setUp(self):
        self.table = study.fill_table(TRADES, lambda symbol, day: BARS[symbol]).set_index("symbol")

    def test_buying_above_the_open_lowers_the_return_and_missing_bars_stay_unknown(self):
        table = self.table
        # 买贵 2%：3% 的毛利剩不到 1%；买贵 5% 则亏损。
        self.assertAlmostEqual(table.loc["000001", "vwap_vs_open"], 0.02)
        self.assertLess(table.loc["000001", "net_return_vwap"], table.loc["000001", "net_return"] - 0.019)
        self.assertGreater(table.loc["000001", "net_return_vwap"], 0)
        self.assertLess(table.loc["000001", "net_return_high"], 0)
        # 均价等于开盘价时，重算结果必须回到原来的净收益。
        self.assertAlmostEqual(table.loc["000002", "net_return_vwap"], table.loc["000002", "net_return"])
        self.assertEqual(table["unknown"].tolist(), [False, False, True])
        self.assertTrue(pd.isna(table.loc["000003", "net_return_vwap"]))

    def test_capacity_counts_orders_above_a_tenth_of_the_first_five_minutes(self):
        table = self.table.reset_index()
        # 10 万对 1020 万是 1%，对 50 万是 20%（受限，只能买到 5 万）；查不到的不计入。
        self.assertEqual(study.capacity(table, 1), {
            "limited_share": 0.5,
            "fillable_amount_share": 0.75,
            "median_participation": round((100_000 / 10.2e6 + 0.2) / 2, 5),
        })
        # 放大 20 倍：200 万对 1020 万也超过 10%。
        self.assertEqual(study.capacity(table, 20)["limited_share"], 1.0)


if __name__ == "__main__":
    unittest.main()
