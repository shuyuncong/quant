"""Contract tests for the US index research script."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from us_index_study import (
    fetch_fred,
    load_series,
    metrics,
    run_strategy,
    signal_buy_hold,
    signal_ma,
    signal_momentum,
    standardize_yahoo,
    validate_index,
)


def _frame(days: int = 60, start: str = "2022-01-03") -> pd.DataFrame:
    index = pd.bdate_range(start, periods=days)
    close = np.linspace(100.0, 160.0, days)
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "adjclose": close,
            "volume": 1_000_000,
        },
        index=index,
    )


class YahooPayloadTests(unittest.TestCase):
    def test_adjusted_ohlc_uses_adjclose_factor(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1704153600, 1704240000],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [90.0, 100.0],
                                    "high": [101.0, 111.0],
                                    "low": [89.0, 99.0],
                                    "close": [100.0, 110.0],
                                    "volume": [1000, 2000],
                                }
                            ],
                            "adjclose": [{"adjclose": [50.0, 55.0]}],
                        },
                    }
                ]
            }
        }
        frame = standardize_yahoo(payload, "TEST")
        self.assertEqual(len(frame), 2)
        self.assertAlmostEqual(float(frame["open"].iloc[0]), 45.0)
        self.assertAlmostEqual(float(frame["close"].iloc[1]), 55.0)
        self.assertAlmostEqual(float(frame["adjclose"].iloc[1]), 55.0)

    def test_null_quote_rows_are_dropped(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1704153600, 1704240000],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [None, 100.0],
                                    "high": [None, 111.0],
                                    "low": [None, 99.0],
                                    "close": [None, 110.0],
                                    "volume": [None, 2000],
                                }
                            ]
                        },
                    }
                ]
            }
        }
        frame = standardize_yahoo(payload, "TEST")
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.index[0].date(), datetime(2024, 1, 3, tzinfo=timezone.utc).date())


class ExecutionTests(unittest.TestCase):
    def test_signal_at_close_fills_at_next_open(self) -> None:
        frame = _frame(40)
        dates = frame.index

        def signal(close: pd.Series, index: int) -> float:
            return 1.0 if index == 10 else 0.0

        result = run_strategy(frame, signal, start="2022-01-03", initial=100_000.0,
                              slippage=0.0, commission=0.0)
        trades = result["trades_detail"]
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(trades[0]["date"], dates[11].strftime("%Y-%m-%d"))
        self.assertAlmostEqual(trades[0]["price"], float(frame["open"].iloc[11]), places=5)
        self.assertEqual(trades[0]["side"], "buy")

    def test_buy_and_hold_matches_close_ratio(self) -> None:
        frame = _frame(40)
        result = run_strategy(frame, signal_buy_hold, start="2022-01-03",
                              initial=100_000.0, slippage=0.0, commission=0.0)
        expected = 100_000.0 * float(frame["close"].iloc[-1]) / float(frame["open"].iloc[1])
        self.assertAlmostEqual(result["final_equity"], expected, places=2)

    def test_costs_reduce_final_equity(self) -> None:
        frame = _frame(60)
        free = run_strategy(frame, signal_buy_hold, start="2022-01-03",
                            initial=100_000.0, slippage=0.0, commission=0.0)
        costly = run_strategy(frame, signal_buy_hold, start="2022-01-03",
                              initial=100_000.0, slippage=0.002, commission=0.0)
        self.assertLess(costly["final_equity"], free["final_equity"])

    def test_short_window_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_strategy(_frame(20), signal_buy_hold, start="2022-01-03")

    def test_future_bars_do_not_change_prefix_equity_or_trades(self) -> None:
        frame = _frame(80)
        dates = frame.index

        def signal(close: pd.Series, index: int) -> float:
            return 1.0 if 10 <= index < 20 else 0.0

        base = run_strategy(frame, signal, start="2022-01-03", initial=100_000.0,
                            slippage=0.0, commission=0.0)
        mutated = frame.copy()
        mutated.iloc[41:, mutated.columns.get_indexer(["open", "high", "low", "close"])] *= 2.0
        future = run_strategy(mutated, signal, start="2022-01-03", initial=100_000.0,
                              slippage=0.0, commission=0.0)

        pd.testing.assert_series_equal(
            base["curve"].iloc[:41], future["curve"].iloc[:41], check_names=False
        )
        self.assertEqual(
            [trade for trade in base["trades_detail"] if trade["date"] < str(dates[41].date())],
            [trade for trade in future["trades_detail"] if trade["date"] < str(dates[41].date())],
        )


class SignalTests(unittest.TestCase):
    def test_ma_signal_warms_up_and_reacts(self) -> None:
        close = pd.Series([100.0] * 200 + [101.0])
        self.assertEqual(signal_ma(close, 199), 0.0)
        self.assertEqual(signal_ma(close, 200), 1.0)

        below = pd.Series([100.0] * 200 + [99.0])
        self.assertEqual(signal_ma(below, 200), 0.0)

    def test_momentum_skips_the_most_recent_month(self) -> None:
        close = pd.Series([100.0] * 300)
        close.iloc[252:] = 110.0
        self.assertEqual(signal_momentum(close, 252), 0.0)
        self.assertEqual(signal_momentum(close, 273), 1.0)

        close.iloc[273] = 50.0
        self.assertEqual(signal_momentum(close, 294), 0.0)


class MetricsTests(unittest.TestCase):
    def test_max_drawdown_uses_peak_to_trough(self) -> None:
        equity = pd.Series(
            [100.0, 120.0, 60.0, 90.0],
            index=pd.bdate_range("2022-01-03", periods=4),
        )
        result = metrics(equity, initial=100.0, trades=[], exposure=[1.0] * 4)
        self.assertAlmostEqual(result["max_drawdown_pct"], -50.0)


class CacheTests(unittest.TestCase):
    def test_cache_roundtrip_uses_local_file(self) -> None:
        frame = _frame(10)
        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            frame.to_csv(cache_dir / "TEST.csv", index_label="date")
            loaded, meta = load_series("TEST", cache_dir=cache_dir,
                                       start="2022-01-03", end="2022-01-14")
        self.assertEqual(len(loaded), 10)
        self.assertEqual(meta["source"], "cache")
        self.assertEqual(meta["rows"], 10)

    def test_fred_snapshot_is_parsed_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            path = cache_dir / "fred" / "NASDAQCOM.csv"
            path.parent.mkdir(parents=True)
            path.write_text(
                "observation_date,NASDAQCOM\n2020-01-02,100.0\n2020-01-03,101.5\n",
                encoding="utf-8",
            )
            frame = fetch_fred("NASDAQCOM", cache_dir=cache_dir)
        self.assertEqual(len(frame), 2)
        self.assertAlmostEqual(float(frame["close"].iloc[1]), 101.5)
        self.assertAlmostEqual(float(frame["open"].iloc[1]), 101.5)

    def test_validate_index_compares_cached_fred(self) -> None:
        index = pd.DatetimeIndex(["2020-01-02", "2020-01-03"])
        frame = pd.DataFrame({"close": [100.0, 101.5]}, index=index)
        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            path = cache_dir / "fred" / "NASDAQCOM.csv"
            path.parent.mkdir(parents=True)
            path.write_text(
                "observation_date,NASDAQCOM\n2020-01-02,100.0\n2020-01-03,101.5\n",
                encoding="utf-8",
            )
            result = validate_index(frame, "NASDAQCOM", cache_dir=cache_dir)
        self.assertEqual(result["common_days"], 2)
        self.assertAlmostEqual(result["max_abs_diff"], 0.0)


if __name__ == "__main__":
    unittest.main()
