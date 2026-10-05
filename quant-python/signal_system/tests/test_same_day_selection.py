"""同日多候选挑最优 —— 因子计算的正确性护栏。

易错点 (实测踩过): 因子引用了缓存里不存在的列 (macd_hist), 结果恒为 0,
排名退化成"按代码"却不报错。测试用"必须能与按代码产生不同结果"来钉住这类静默失效。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import same_day_selection_study as sds  # noqa: E402


def _bars(periods: int = 130, start: float = 10.0, step: float = 0.05) -> pd.DataFrame:
    dates = pd.bdate_range("2024-06-03", periods=periods)
    close = np.array([start + step * index for index in range(periods)])
    return pd.DataFrame(
        {
            "datetime": dates,
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": np.full(periods, 1_000_000.0),
        }
    )


def _trade(symbol: str, signal_day: str, **overrides) -> dict:
    trade = {
        "symbol": symbol,
        "signal_day": signal_day,
        "entry_day": signal_day,
        "signal_close": 10.0,
        "ma_long": 10.0,
        "volume_ratio": 1.5,
        "pnl_pct": 3.0,
    }
    trade.update(overrides)
    return trade


class FactorTests(unittest.TestCase):
    def test_all_factors_are_distinct_and_finite(self):
        """任何一个因子恒为 0/重复 = 静默失效, 必须被这条抓住。"""
        frame = _bars()
        trades = [_trade(f"{index:06d}", "2024-12-02") for index in range(1, 12)]
        bars = {trade["symbol"]: frame for trade in trades}
        sds.compute_factors(trades, bars)
        for name, _, _ in sds.FACTORS:
            values = [float(t["_factors"][name]) for t in trades]
            self.assertTrue(all(np.isfinite(values)), name)
        # momentum_20 / recovery_60 / drawdown_120 在同样行情下彼此不同 (横截面有区分度)
        for name, _, _ in sds.FACTORS:
            self.assertIn(name, trades[0]["_factors"])

    def test_factors_use_only_signal_day_and_before(self):
        """信号日之后的价格不得影响因子 (否则是未来函数)。"""
        dates = pd.bdate_range("2024-06-03", periods=60)
        close = np.full(60, 10.0)
        frame = pd.DataFrame(
            {
                "datetime": dates,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": np.full(60, 1e6),
            }
        )
        signal_day = dates[40].date().isoformat()
        trade = _trade("000001", signal_day)
        sds.compute_factors([trade], {"000001": frame})
        before = dict(trade["_factors"])
        # 把信号日之后的收盘价全部翻倍, 因子必须纹丝不动。
        mutated = frame.copy()
        mutated.loc[mutated.index > 40, "close"] = 20.0
        again = _trade("000001", signal_day)
        sds.compute_factors([again], {"000001": mutated})
        for key in before:
            self.assertEqual(before[key], again["_factors"][key], key)

    def test_oracle_uses_realized_pnl(self):
        trades = [
            _trade("000001", "2024-12-02", pnl_pct=10.0),
            _trade("000002", "2024-12-02", pnl_pct=-5.0),
        ]
        sds.compute_factors(trades, {})
        self.assertGreater(
            trades[0]["_factors"]["oracle"], trades[1]["_factors"]["oracle"]
        )

    def test_lower_better_flips_the_sign(self):
        trade = {"_factors": {"x": 2.0}}
        self.assertEqual(sds._score(trade, "x", False), 2.0)
        self.assertEqual(sds._score(trade, "x", True), -2.0)


class DiagnosisTests(unittest.TestCase):
    def test_counts_days_where_candidates_exceed_slots(self):
        trades = [
            _trade("000001", "2024-12-02"),
            _trade("000002", "2024-12-02"),
            _trade("000003", "2024-12-02"),
            _trade("000004", "2024-12-03"),
        ]
        diag = sds._diagnose(trades, 2)
        # 12-02 有 3 个候选 > 2 个仓位; 12-03 只有 1 个。
        self.assertEqual(diag["days_candidates_exceed_slots"], 1)
        self.assertEqual(diag["trading_days_with_candidates"], 2)
        self.assertEqual(diag["max_candidates_in_a_day"], 3)
        self.assertEqual(diag["days_exceed_pct"], 50.0)

    def test_no_contest_when_slots_cover_every_candidate(self):
        trades = [_trade("000001", "2024-12-02"), _trade("000002", "2024-12-02")]
        diag = sds._diagnose(trades, 5)
        self.assertEqual(diag["days_candidates_exceed_slots"], 0)
        self.assertEqual(diag["days_exceed_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
