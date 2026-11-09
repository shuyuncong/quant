"""Execution stress knobs must be opt-in and must change fills as documented."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from etf_rotation.backtest import run_panels


def _panels(days: int = 80):
    dates = pd.bdate_range("2024-01-01", periods=days)
    trend = np.linspace(10.0, 11.0, days)
    close = pd.DataFrame({"AAA": trend, "BBB": trend * 0.5}, index=dates)
    open_ = close * 1.001
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=close.columns)
    return close, open_, volume


def _signal(close, volume, as_of):
    return {"target_weights": {"AAA": 1.0}}


class ExecutionStressTests(unittest.TestCase):
    def test_defaults_preserve_baseline(self) -> None:
        close, open_, volume = _panels()
        first = run_panels(close, open_, volume, start=str(close.index[0].date()),
                           end=str(close.index[-1].date()), signal_fn=_signal)
        second = run_panels(close, open_, volume, start=str(close.index[0].date()),
                            end=str(close.index[-1].date()), signal_fn=_signal)
        self.assertEqual(first["total_return_pct"], second["total_return_pct"])
        self.assertEqual(first["execution"]["capped_orders"], 0)
        self.assertEqual(first["execution"]["partial_fills"], 0)

    def test_partial_fill_reduces_execution(self) -> None:
        close, open_, volume = _panels()
        baseline = run_panels(close, open_, volume, start=str(close.index[0].date()),
                              end=str(close.index[-1].date()), signal_fn=_signal)
        partial = run_panels(close, open_, volume, start=str(close.index[0].date()),
                             end=str(close.index[-1].date()), signal_fn=_signal,
                             fill_ratio=0.5)
        self.assertGreater(partial["execution"]["partial_fills"], 0)
        self.assertGreater(partial["execution"]["unfilled_notional"], 0.0)
        self.assertLess(partial["avg_exposure"], baseline["avg_exposure"])

    def test_adv_cap_binds_when_tiny(self) -> None:
        close, open_, volume = _panels()
        result = run_panels(close, open_, volume, start=str(close.index[30].date()),
                            end=str(close.index[-1].date()), signal_fn=_signal,
                            max_participation=0.000001)
        self.assertGreater(result["execution"]["capped_orders"], 0)
        self.assertGreater(result["execution"]["unfilled_notional"], 0.0)

    def test_premium_and_discount_reduce_equity(self) -> None:
        close, open_, volume = _panels()
        baseline = run_panels(close, open_, volume, start=str(close.index[0].date()),
                              end=str(close.index[-1].date()), signal_fn=_signal)
        stressed = run_panels(close, open_, volume, start=str(close.index[0].date()),
                              end=str(close.index[-1].date()), signal_fn=_signal,
                              buy_premium_bps=100, sell_discount_bps=100)
        self.assertLess(stressed["curve"][-1]["equity"], baseline["curve"][-1]["equity"])

    def test_empirical_spread_panel_reduces_equity(self) -> None:
        close, open_, volume = _panels()
        baseline = run_panels(close, open_, volume, start=str(close.index[0].date()),
                              end=str(close.index[-1].date()), signal_fn=_signal)
        spread = pd.DataFrame(0.005, index=close.index, columns=close.columns)
        stressed = run_panels(close, open_, volume, start=str(close.index[0].date()),
                              end=str(close.index[-1].date()), signal_fn=_signal,
                              spread_panel=spread)
        self.assertTrue(stressed["execution"]["spread_panel"])
        self.assertLess(stressed["curve"][-1]["equity"], baseline["curve"][-1]["equity"])

    def test_extra_slippage_reduces_equity(self) -> None:
        close, open_, volume = _panels()
        baseline = run_panels(close, open_, volume, start=str(close.index[0].date()),
                              end=str(close.index[-1].date()), signal_fn=_signal)
        stressed = run_panels(close, open_, volume, start=str(close.index[0].date()),
                              end=str(close.index[-1].date()), signal_fn=_signal,
                              extra_slippage_bps=20)
        self.assertEqual(stressed["execution"]["effective_slippage_bp"], 25)
        self.assertLess(stressed["curve"][-1]["equity"], baseline["curve"][-1]["equity"])

    def test_invalid_fill_ratio_fails_closed(self) -> None:
        close, open_, volume = _panels()
        with self.assertRaises(ValueError):
            run_panels(close, open_, volume, start=str(close.index[0].date()),
                       end=str(close.index[-1].date()), signal_fn=_signal, fill_ratio=0.0)


if __name__ == "__main__":
    unittest.main()
