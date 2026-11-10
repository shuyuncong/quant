"""Offline invariants for the PIT quality-value study."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from pit_quality_value_study import (
    QualityValueBacktest,
    build_cap_weighted_index,
    latest_annual_asof,
    market_gate_on,
    select_quality_value_targets,
)
from smallcap_optimized_study import OptConfig


def _features(days: pd.DatetimeIndex, symbols: list[str]) -> dict[str, object]:
    close = pd.DataFrame(10.0, index=days, columns=symbols)
    mv = pd.DataFrame(
        {symbol: 1_000_000_000.0 + index * 1_000_000_000.0 for index, symbol in enumerate(symbols)},
        index=days,
    )
    rank = mv.rank(axis=1, ascending=True)
    return {
        "close": close,
        "open": close.copy(),
        "prev_close": close.copy(),
        "high": close.copy(),
        "low": close.copy(),
        "amount": pd.DataFrame(1e9, index=days, columns=symbols),
        "turnover": pd.DataFrame(1.0, index=days, columns=symbols),
        "mv": mv,
        "valid": pd.DataFrame(True, index=days, columns=symbols),
        "rank": rank,
    }


class QualityValueTests(unittest.TestCase):
    def test_latest_annual_is_asof(self) -> None:
        value = pd.DataFrame(
            [
                {
                    "symbol": "000001",
                    "period": pd.Timestamp("2019-12-31"),
                    "net_profit": 10.0,
                    "revenue": 100.0,
                    "roe": 8.0,
                    "available_date": pd.Timestamp("2020-04-20"),
                },
                {
                    "symbol": "000001",
                    "period": pd.Timestamp("2020-12-31"),
                    "net_profit": 20.0,
                    "revenue": 200.0,
                    "roe": 10.0,
                    "available_date": pd.Timestamp("2021-04-20"),
                },
            ]
        )
        before = latest_annual_asof(value, pd.Timestamp("2021-04-19"))
        after = latest_annual_asof(value, pd.Timestamp("2021-04-20"))
        self.assertEqual(float(before.loc["000001", "net_profit"]), 10.0)
        self.assertEqual(float(after.loc["000001", "net_profit"]), 20.0)

    def test_market_gate_uses_200_day_average(self) -> None:
        days = pd.bdate_range("2020-01-01", periods=320)
        index = pd.Series(
            np.concatenate([np.linspace(100.0, 120.0, 260), np.linspace(120.0, 90.0, 60)]),
            index=days,
        )
        gate = market_gate_on(index, window=200)
        self.assertFalse(bool(gate.iloc[150]))
        self.assertTrue(bool(gate.iloc[250]))
        self.assertFalse(bool(gate.iloc[-1]))

    def test_cap_weighted_index_uses_previous_weights(self) -> None:
        days = pd.bdate_range("2020-01-01", periods=3)
        symbols = ["000001", "000002"]
        features = _features(days, symbols)
        features["close"] = pd.DataFrame(
            [[10.0, 20.0], [11.0, 20.0], [11.0, 22.0]],
            index=days,
            columns=symbols,
        )
        # The larger stock doubles; cap weighting must put more weight on it.
        features["mv"] = pd.DataFrame(
            [[1.0, 3.0], [1.0, 3.0], [1.0, 3.0]],
            index=days,
            columns=symbols,
        )
        index = build_cap_weighted_index(features)
        self.assertAlmostEqual(float(index.iloc[1] / index.iloc[0] - 1), 0.025)
        self.assertAlmostEqual(float(index.iloc[2] / index.iloc[1] - 1), 0.075)

    def test_quality_value_selection_excludes_losses_and_respects_sector_cap(self) -> None:
        days = pd.bdate_range("2020-01-06", periods=2)
        symbols = [f"00000{index}" for index in range(1, 7)]
        features = _features(days, symbols)
        value = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "period": pd.Timestamp("2018-12-31"),
                    "net_profit": profit,
                    "revenue": 1000.0,
                    "roe": roe,
                    "available_date": pd.Timestamp("2019-04-20"),
                }
                for symbol, profit, roe in zip(
                    symbols,
                    [100.0, 200.0, 300.0, -10.0, 400.0, 500.0],
                    [5.0, 10.0, 15.0, 20.0, 25.0, 30.0],
                )
            ]
        )
        adv20 = pd.DataFrame(1e9, index=days, columns=symbols)
        cfg = OptConfig(
            max_names=2,
            rank_low=1,
            rank_high=999999,
            rank_pct_low=0.0,
            rank_pct_high=1.0,
            max_sector_weight=0.5,
            min_adv=50_000_000.0,
        )
        industry = {
            "000001": "bank",
            "000002": "bank",
            "000003": "bank",
            "000004": "tech",
            "000005": "health",
            "000006": "bank",
        }
        selected, stats = select_quality_value_targets(
            features, adv20, industry, value, cfg, days[0], "qv"
        )
        self.assertEqual(len(selected), 2)
        self.assertNotIn("000004", selected)
        self.assertEqual(sum(industry[symbol] == "bank" for symbol in selected), 1)
        self.assertEqual(stats["fundamental_names"], 5)

    def test_size_control_selects_largest_names(self) -> None:
        days = pd.bdate_range("2020-01-06", periods=2)
        symbols = [f"00000{index}" for index in range(1, 7)]
        features = _features(days, symbols)
        value = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "period": pd.Timestamp("2018-12-31"),
                    "net_profit": 100.0,
                    "revenue": 1000.0,
                    "roe": 10.0,
                    "available_date": pd.Timestamp("2019-04-20"),
                }
                for symbol in symbols
            ]
        )
        adv20 = pd.DataFrame(1e9, index=days, columns=symbols)
        cfg = OptConfig(
            max_names=2,
            rank_low=1,
            rank_high=999999,
            rank_pct_low=0.0,
            rank_pct_high=1.0,
            max_sector_weight=1.0,
        )
        industry = {symbol: symbol for symbol in symbols}
        selected, _ = select_quality_value_targets(
            features, adv20, industry, value, cfg, days[0], "size_top50"
        )
        self.assertEqual(selected, ["000006", "000005"])

    def test_gate_off_returns_empty_target(self) -> None:
        days = pd.bdate_range("2020-01-06", periods=2)
        symbols = [f"00000{index}" for index in range(1, 4)]
        features = _features(days, symbols)
        value = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "period": pd.Timestamp("2018-12-31"),
                    "net_profit": 100.0,
                    "revenue": 1000.0,
                    "roe": 10.0,
                    "available_date": pd.Timestamp("2019-04-20"),
                }
                for symbol in symbols
            ]
        )
        adv20 = pd.DataFrame(1e9, index=days, columns=symbols)
        cfg = OptConfig(
            max_names=2,
            rank_low=1,
            rank_high=999999,
            rank_pct_low=0.0,
            rank_pct_high=1.0,
            max_sector_weight=1.0,
        )
        gate = pd.Series(False, index=days)
        engine = QualityValueBacktest(
            features,
            adv20,
            {symbol: symbol for symbol in symbols},
            cfg,
            value=value,
            gate=gate,
            use_gate=True,
            mode="qv",
        )
        selected, count = engine._select(days[0])
        self.assertEqual(selected, [])
        self.assertEqual(count, 0)
        self.assertFalse(engine.selections[-1]["gate"])


if __name__ == "__main__":
    unittest.main()
