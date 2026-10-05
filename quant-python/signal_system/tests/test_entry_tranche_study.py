"""分批建仓 (entry tranche) 实验的正确性护栏。

这里的两个 bug 都是**静默**的: 数字看着合理, 但口径错了。
1) 用收盘价 (_mark_prices) 而非开盘价当批次买入价 -> 买入价系统性偏高 (涨停日尤其离谱);
2) 取 cache 里 `_none` (不复权) 而非 `_qfq` -> 买入价与引擎口径差几个百分点。
因此测试直接钉住"tranches=1 必须逐笔复现引擎结果"这一等价关系。
"""

from __future__ import annotations

import pickle
import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backtest_winrate as bt  # noqa: E402
import entry_tranche_study as ets  # noqa: E402
from utils.helpers import load_config  # noqa: E402

CACHE = BASE_DIR / "rankcache" / "diverge.pkl"


def _bars() -> pd.DataFrame:
    """20 根日线, 开盘价与收盘价刻意不同, 用于暴露价格基准错误。"""
    dates = pd.bdate_range("2025-01-02", periods=20)
    close = np.linspace(10.0, 12.0, 20)
    return pd.DataFrame(
        {
            "datetime": dates,
            "open": close * 0.90,  # 开盘价明显低于收盘价
            "high": close * 1.02,
            "low": close * 0.88,
            "close": close,
            "volume": np.full(20, 1_000_000.0),
            "is_closed": True,
        }
    )


class TrancheBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(str(BASE_DIR / "config" / "config.yaml"))
        self.costs = {
            **bt._resolve_execution_config(config),
            "exit_mode": "fixed",
            "exit_rules_scope": [],
        }
        ets.COSTS_CACHE.clear()
        ets.COSTS_CACHE.update(self.costs)
        self.bars = {"000001": _bars()}

    def _trade(self, **overrides) -> dict:
        trade = {
            "symbol": "000001",
            "signal_day": "2025-01-02",
            "entry_day": "2025-01-03",
            "exit_day": "2025-01-28",
            "entry_price": 9.09,
            "exit_price": 11.5,
            "signal_type": "macd_divergence_bottom",
            "exit_session": "close",
        }
        trade.update(overrides)
        return trade

    def test_single_tranche_uses_the_engine_open_price(self):
        """tranches=1 必须逐笔等于引擎的 entry_day/entry_price (价格基准护栏)。"""
        built = ets.build_fractional_trades([self._trade()], self.bars, 4, 1)
        self.assertEqual(len(built), 1)
        frame = self.bars["000001"]
        dates = [item.date() for item in pd.to_datetime(frame["datetime"])]
        index = dates.index(date(2025, 1, 3))
        self.assertEqual(built[0]["entry_day"], "2025-01-03")
        self.assertAlmostEqual(built[0]["entry_price"], float(frame.iloc[index]["open"]))
        # 开盘价 != 收盘价, 用收盘价会立刻失败。
        self.assertNotAlmostEqual(built[0]["entry_price"], float(frame.iloc[index]["close"]))
        self.assertEqual(built[0]["symbol"], "000001")

    def test_three_tranches_are_five_bars_apart_and_share_one_exit(self):
        built = ets.build_fractional_trades([self._trade()], self.bars, 4, 3)
        self.assertEqual(len(built), 3)
        frame = self.bars["000001"]
        dates = [item.date() for item in pd.to_datetime(frame["datetime"])]
        indices = [dates.index(date.fromisoformat(item["entry_day"])) for item in built]
        self.assertEqual(indices[1] - indices[0], ets.TRANCHE_GAP)
        self.assertEqual(indices[2] - indices[1], ets.TRANCHE_GAP)
        # 卖出规则不变: 三批共用同一个出场日。
        self.assertEqual({item["exit_day"] for item in built}, {"2025-01-28"})
        # 批次是独立的持仓槽位。
        self.assertEqual([item["symbol"] for item in built], ["000001", "000001T1", "000001T2"])

    def test_tranches_stop_when_the_position_exits_first(self):
        """快速止损的单子: 只建了第一批, 不会凭空补出后续批次。"""
        built = ets.build_fractional_trades(
            [self._trade(exit_day="2025-01-06")], self.bars, 4, 3
        )
        self.assertEqual(len(built), 1)

    def test_missing_bars_yields_nothing(self):
        built = ets.build_fractional_trades([self._trade(symbol="999999")], self.bars, 4, 3)
        self.assertEqual(built, [])

    def test_two_tranche_weights_are_two_thirds_then_one_third(self):
        """加测档: 首日买 2/3, +5 个交易日补 1/3 (不是等权 1/2)。"""
        self.assertEqual(ets._weights_for(2), (2.0 / 3.0, 1.0 / 3.0))
        self.assertEqual(ets._weights_for(1), (1.0,))
        self.assertEqual(ets._weights_for(3), (1 / 3, 1 / 3, 1 / 3))
        built = ets.build_fractional_trades([self._trade()], self.bars, 4, 2)
        self.assertEqual(len(built), 2)
        self.assertAlmostEqual(built[0]["_tranche_weight"], 2.0 / 3.0)
        self.assertAlmostEqual(built[1]["_tranche_weight"], 1.0 / 3.0)
        # 权重之和 = 1 (一只股票的总仓位不因分批而改变)。
        self.assertAlmostEqual(sum(item["_tranche_weight"] for item in built), 1.0)

    def test_unbuilt_tranches_leave_capital_unused(self):
        """未建成的批次不摊到已建批次上 (否则分批会退化成一次买满)。"""
        built = ets.build_fractional_trades(
            [self._trade(exit_day="2025-01-08")], self.bars, 4, 3
        )
        self.assertEqual(len(built), 1)
        # 只建了首批 -> 权重就是 1/3, 不是被放大到 1.0。
        self.assertAlmostEqual(built[0]["_tranche_weight"], 1.0 / 3.0)


class TrancheCacheBasisTests(unittest.TestCase):
    """口径护栏: 必须用前复权缓存, 否则买入价与引擎不一致。"""

    @unittest.skipUnless(CACHE.exists(), "需要先跑 ranking_study --build-cache")
    def test_single_tranche_reproduces_the_engine_trade(self):
        import entry_tranche_study as study

        config = load_config(str(BASE_DIR / "config" / "config.yaml"))
        costs = {
            **bt._resolve_execution_config(config),
            "exit_mode": "fixed",
            "exit_rules_scope": [],
        }
        study.COSTS_CACHE.clear()
        study.COSTS_CACHE.update(costs)
        store = pickle.loads(CACHE.read_bytes())["diverge"]
        bars = study.load_bars({str(trade["symbol"]) for trade in store})
        mismatched = 0
        checked = 0
        for trade in store:
            built = study.build_fractional_trades([trade], bars, 5, 1)
            if not built:
                mismatched += 1
                continue
            checked += 1
            if (
                abs(built[0]["entry_price"] - trade["entry_price"]) > 1e-9
                or built[0]["entry_day"] != trade["entry_day"]
            ):
                mismatched += 1
        # 任何一个不一致都说明取错了复权口径或价格基准。
        self.assertEqual(mismatched, 0)
        self.assertGreater(checked, 400)


if __name__ == "__main__":
    unittest.main()
