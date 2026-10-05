"""顶背离：回测引擎与实盘退出提醒必须用同一套判定。

原实现里这两条路径是两份不同的代码：
  - 回测 `_build_trend_exit_flags`：正柱周期切换，比较"最高价"是否创新高、面积是否收缩。
  - 实盘 `registry.evaluate_exits`：调用 `bottom_divergence_at(-hist, -high, ...)`，
    "取负镜像"复用底背离函数——但取负之后"创更高价"变成 price_change_pct > 0，
    而底背离判定要求 < 0，方向被反转，实盘实际判的是"价格未创新高"。
两份不一致 => 页面/推送提示的卖点，不是回测里验证过的那个卖点。
"""
import os
import sys
import random
import unittest

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.dirname(BASE_DIR))

from backtest_winrate import _build_trend_exit_flags
from strategy.macd_divergence import top_divergence_flags
from strategy.registry import evaluate_exits


def _random_bars(seed: int, bars: int = 400) -> pd.DataFrame:
    rng = random.Random(seed)
    closes = [10.0]
    for _ in range(bars - 1):
        closes.append(max(0.5, closes[-1] * (1 + rng.gauss(0.0005, 0.02))))
    return pd.DataFrame({
        "datetime": pd.date_range("2023-01-02", periods=len(closes), freq="B"),
        "open": closes,
        "high": [value * 1.01 for value in closes],
        "low": [value * 0.99 for value in closes],
        "close": closes,
        "volume": [1e6] * len(closes),
    })


def _top_divergence_from_highs(hist: pd.Series, high: pd.Series) -> list[int]:
    """独立参照实现：正柱周期最高价创新高 + 面积收缩。"""
    values = hist.fillna(0.0).to_numpy(dtype=float)
    peaks = high.to_numpy(dtype=float)
    cycles = []
    start = None
    for index, value in enumerate(values > 0):
        if value and start is None:
            start = index
        if not value and start is not None:
            cycles.append((index, float(values[start:index].sum()), float(np.nanmax(peaks[start:index]))))
            start = None
    hits = []
    for position in range(1, len(cycles)):
        trigger, area, peak = cycles[position]
        _, prior_area, prior_peak = cycles[position - 1]
        if peak > prior_peak and area < prior_area:
            hits.append(trigger)
    return hits


class TopDivergenceSharedDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.execution = {
            "exit_rules_config": {"mode": "divergence_trend", "top_divergence": True,
                                  "below_yearline": True, "ma_long_period": 250},
            "ma_long_period": 250, "max_holding_bars": 40, "stop_loss_pct": 0.08,
        }
        self.config = {
            "risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.30},
            "backtest": {"chan_zero_axis": {"max_holding_bars": 40}, "exit_rules": {
                "mode": "divergence_trend", "top_divergence": True, "below_yearline": True,
                "ma_long_period": 250, "apply_to_signal_types": ["macd_divergence_bottom"]}},
            "signal_strategy": {"macd": {"long_ma_period": 250}},
        }

    def _engine_flags(self, frame):
        return _build_trend_exit_flags(frame, self.execution)["top_divergence_flags"]

    def _shared_flags(self, frame):
        from strategy.macd import calculate_macd
        macd = calculate_macd(frame["close"], fast=12, slow=26, signal=9)
        return top_divergence_flags(macd["hist"], frame["close"])[0]

    def test_engine_and_shared_helper_agree_everywhere(self):
        for seed in range(12):
            frame = _random_bars(seed)
            self.assertTrue((self._engine_flags(frame) == self._shared_flags(frame)).all(), f"seed={seed}")

    def test_engine_matches_an_independent_reference(self):
        """防止"两边一起改错"：用独立实现校验创新高 + 面积收缩。"""
        hit_total = 0
        for seed in range(12):
            frame = _random_bars(seed)
            from strategy.macd_divergence import add_divergence_indicators, resolve_divergence_config
            enriched = add_divergence_indicators(frame, resolve_divergence_config(self.config))
            expected = _top_divergence_from_highs(enriched["hist"], enriched["high"])
            actual = np.flatnonzero(self._engine_flags(frame)).tolist()
            self.assertEqual(actual, expected, f"seed={seed}")
            hit_total += len(expected)
        self.assertGreater(hit_total, 0, "样本里必须有顶背离触发，否则测试是空转")

    def test_live_exit_fires_on_exactly_the_engine_bars(self):
        """实盘退出提醒必须在回测标记的同一根 K 线上触发（含历史时点回看）。"""
        hit_total = 0
        for seed in (3, 11, 12, 21, 27):
            frame = _random_bars(seed)
            engine = self._engine_flags(frame)
            for index in range(260, len(frame)):
                conditions, _ = evaluate_exits(frame.iloc[: index + 1], self.config, "macd_divergence", None)
                live = {item["name"]: item["met"] for item in conditions}.get("已确认日线顶背离") is True
                self.assertEqual(live, bool(engine[index]), f"seed={seed} bar={index}")
            hit_total += int(engine.sum())
        self.assertGreater(hit_total, 0, "样本里必须有顶背离触发，否则测试是空转")

    def test_live_exit_does_not_fire_without_a_holding(self):
        """没有持仓时顶背离条件仍会算，但不会变成"卖出"结论。"""
        frame = _random_bars(11)
        engine = self._engine_flags(frame)
        index = int(np.flatnonzero(engine)[-1])
        conditions, sell = evaluate_exits(frame.iloc[: index + 1], self.config, "macd_divergence", None)
        self.assertIsNone(sell)
        self.assertTrue({item["name"]: item["met"] for item in conditions}["已确认日线顶背离"])

    def test_yearline_requires_a_full_period_of_history(self):
        """历史不足 250 根时年线是"未知"（None），不是"没跌破"（False）。"""
        frame = _random_bars(5, bars=120)
        conditions, _ = evaluate_exits(frame, self.config, "macd_divergence", None)
        met = {item["name"]: item["met"] for item in conditions}
        self.assertIsNone(met["跌破年线"])


if __name__ == "__main__":
    unittest.main()
