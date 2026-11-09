"""Invariants for the small-cap daily-bar research harness."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from smallcap_strategy_study import (
    Backtest,
    Config,
    Variant,
    _commission,
    dynamic_target_count,
    limit_pct,
    select_targets,
)


def _features(days: pd.DatetimeIndex, symbols: list[str]) -> dict[str, object]:
    close = pd.DataFrame(10.0, index=days, columns=symbols)
    open_ = close.copy()
    high = close + 0.2
    low = close - 0.2
    prev_close = close.shift(1).bfill()
    atr = pd.DataFrame(0.2, index=days, columns=symbols)
    turnover = pd.DataFrame(1.0, index=days, columns=symbols)
    mv = pd.DataFrame(
        {symbol: 1_000_000_000.0 + index for index, symbol in enumerate(symbols)},
        index=days,
    )
    valid = pd.DataFrame(True, index=days, columns=symbols)
    rank = pd.DataFrame(
        {symbol: 801.0 + index for index, symbol in enumerate(symbols)},
        index=days,
    )
    zeros = pd.DataFrame(0.0, index=days, columns=symbols)
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "prev_close": prev_close,
        "atr": atr,
        "atr_prev": atr,
        "turnover": turnover,
        "turnover_mean20": turnover,
        "mv": mv,
        "valid": valid,
        "rank": rank,
        "dev": pd.Series(0.0, index=days),
        "macd_trigger": pd.Series(False, index=days),
        "micro_trigger": pd.Series(False, index=days),
        "idx": pd.Series(1000.0, index=days),
        "amount": zeros,
    }


class SmallCapStudyTests(unittest.TestCase):
    def test_dynamic_target_count_thresholds(self) -> None:
        self.assertEqual(dynamic_target_count(200), 3)
        self.assertEqual(dynamic_target_count(0), 4)
        self.assertEqual(dynamic_target_count(-500), 5)
        self.assertEqual(dynamic_target_count(-500.1), 6)
        self.assertEqual(dynamic_target_count(float("nan")), 4)

    def test_limit_percent_by_board(self) -> None:
        self.assertAlmostEqual(limit_pct("300001"), 0.20)
        self.assertAlmostEqual(limit_pct("688001"), 0.20)
        self.assertAlmostEqual(limit_pct("830001"), 0.30)
        self.assertAlmostEqual(limit_pct("600000"), 0.10)

    def test_industry_cap_and_market_cap_rank(self) -> None:
        days = pd.bdate_range("2020-01-06", periods=3)
        features = _features(days, ["000001", "000002", "000003"])
        industry = {"000001": "bank", "000002": "bank", "000003": "tech"}
        cfg = Config()
        selected = select_targets(
            features, industry, days[1], cfg, limit=3, pool_size=150
        )
        self.assertEqual(len(selected), 2)
        self.assertIn("000003", selected)
        self.assertEqual(
            len([s for s in selected if industry[s] == "bank"]), 1
        )
        # A stock outside the 801-1800 rank band must not be selected.
        features["rank"].loc[days[1], "000003"] = 800
        selected = select_targets(
            features, industry, days[1], cfg, limit=3, pool_size=150
        )
        self.assertNotIn("000003", selected)

    def test_lot_cash_and_t_plus_one(self) -> None:
        days = pd.bdate_range("2020-01-06", periods=10)
        symbols = ["000001", "000003"]
        features = _features(days, symbols)
        industry = {"000001": "bank", "000003": "tech"}
        cfg = Config(initial=100_000.0, start=str(days[0].date()))
        variant = Variant(
            "test", dynamic_names=False, use_stops=True, use_macd=False,
            use_micro=False, use_volume=False, static_names=2, pool_size=150,
        )
        engine = Backtest(features, industry, cfg, variant)
        result = engine.run()

        buys = [t for t in engine.trades if t["side"] == "buy"]
        self.assertTrue(buys)
        self.assertTrue(all(t["shares"] % cfg.lot == 0 for t in buys))
        self.assertGreaterEqual(engine.cash, 0.0)
        self.assertAlmostEqual(
            result["final_equity"],
            engine.cash
            + sum(pos.shares * pos.last_price for pos in engine.positions.values()),
            places=2,
        )

        # Gap down after entry: the stop must not fire on the entry day (T+1).
        entry_day = days[1]  # Tuesday
        self.assertTrue(any(t["date"] == entry_day.strftime("%Y-%m-%d") for t in buys))
        # Entry day is normal; the next session gaps through the stop.  Open
        # 9.50 is above the 10% limit-down price (9.02), so the sell is
        # tradable and the T+1 rule is what keeps the entry day quiet.
        gap_day = days[2]
        features["open"].loc[gap_day, :] = 9.50
        features["high"].loc[gap_day, :] = 9.60
        features["low"].loc[gap_day, :] = 7.90
        features["close"].loc[gap_day, :] = 8.00
        engine = Backtest(features, industry, cfg, variant)
        engine.run()
        entry_sells = [
            t for t in engine.trades
            if t["side"] == "sell" and t["date"] == entry_day.strftime("%Y-%m-%d")
        ]
        self.assertEqual(entry_sells, [])
        later_sells = [
            t for t in engine.trades
            if t["side"] == "sell" and t["reason"].startswith("stop")
        ]
        self.assertTrue(later_sells)

    def test_min_commission(self) -> None:
        cfg = Config()
        self.assertEqual(_commission(100.0, cfg), cfg.min_commission)
        self.assertAlmostEqual(_commission(100_000.0, cfg), 30.0)


if __name__ == "__main__":
    unittest.main()
