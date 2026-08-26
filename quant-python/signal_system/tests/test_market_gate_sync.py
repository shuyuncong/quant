from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import build_market_gate  # noqa: E402
from monitor.service import SignalMonitor, _fast_gate_latch_state  # noqa: E402
from strategy.market_gate import (  # noqa: E402
    calculate_strict_regime,
    calculate_trend_gate,
    resolve_market_gate_settings,
)


def index_bars(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2024-01-02", periods=len(closes)),
            "open": closes,
            "high": [value + 0.2 for value in closes],
            "low": [value - 0.2 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
            "is_closed": [True] * len(closes),
        }
    )


class MarketGateConfigTests(unittest.TestCase):
    def test_resolves_effective_gate_settings(self):
        settings = resolve_market_gate_settings(
            {
                "entry_filters": {
                    "trend_gate_enabled": True,
                    "trend_fast_ma": 8,
                    "trend_slow_ma": 5,
                    "fast_gate_mode": "ANY_LATCH",
                },
                "signal_strategy": {
                    "macd": {"fast": 5, "slow": 13, "signal": 4}
                },
            }
        )

        self.assertTrue(settings["trend_gate_enabled"])
        self.assertEqual(8, settings["trend_fast_ma"])
        self.assertEqual(9, settings["trend_slow_ma"])
        self.assertEqual("any_latch", settings["fast_gate_mode"])
        self.assertEqual(
            {"fast": 5, "slow": 13, "signal": 4},
            settings["macd"],
        )

    def test_invalid_fast_gate_mode_falls_back_to_none(self):
        settings = resolve_market_gate_settings(
            {"entry_filters": {"fast_gate_mode": "unexpected"}}
        )
        self.assertEqual("none", settings["fast_gate_mode"])

    def test_non_default_trend_periods_are_calculated_from_closed_bars(self):
        rising = pd.Series([10.0, 10.0, 10.0, 10.0, 11.0, 12.0, 13.0])
        falling = pd.Series([13.0, 12.0, 11.0, 10.0, 10.0, 10.0, 10.0])

        self.assertTrue(bool(calculate_trend_gate(rising, True, 3, 6).iloc[-1]))
        self.assertFalse(bool(calculate_trend_gate(falling, True, 3, 6).iloc[-1]))

    def test_strict_regime_is_available_when_fast_latch_is_disabled(self):
        rising = pd.Series([100.0] * 10 + [101.0] * 10 + [102.0] * 5)
        regimes = calculate_strict_regime(rising)
        self.assertEqual("bull", regimes.iloc[-1])

        frame = index_bars(rising.tolist())
        gate = build_market_gate(
            frame,
            {
                "entry_filters": {
                    "trend_gate_enabled": False,
                    "fast_gate_mode": "none",
                },
                "signal_strategy": {
                    "macd": {"long_ma_period": 2},
                },
                "backtest": {"market_gate_slope_bars": 1},
            },
        )
        last_day = frame.iloc[-1]["datetime"].date().isoformat()
        self.assertEqual("bull", gate[last_day]["regime"])


class FastGateReplayTests(unittest.TestCase):
    def test_ma10_latch_sets_holds_and_recovers(self):
        falling = index_bars([100.0] * 10 + [99.0, 98.0, 97.0])
        self.assertTrue(
            _fast_gate_latch_state(falling, "ma10_latch")["ma10_latch_bear"]
        )

        mixed = index_bars([100.0] * 10 + [99.0, 98.0, 97.0, 98.0])
        self.assertTrue(
            _fast_gate_latch_state(mixed, "ma10_latch")["ma10_latch_bear"]
        )

        recovered = index_bars(
            [100.0] * 10 + [99.0, 98.0, 97.0, 98.0, 99.0, 100.0, 101.0]
        )
        self.assertFalse(
            _fast_gate_latch_state(recovered, "ma10_latch")["ma10_latch_bear"]
        )

    def test_macd_latch_uses_configured_periods(self):
        frame = index_bars([100.0] * 12)
        dif = pd.Series([1.0] * 11 + [-1.0])
        dea = pd.Series([0.0] * 12)
        fake_macd = pd.DataFrame(
            {"dif": dif, "dea": dea, "hist": (dif - dea) * 2.0}
        )

        with patch("monitor.service.calculate_macd", return_value=fake_macd) as mocked:
            state = _fast_gate_latch_state(
                frame,
                "macd_death_latch",
                macd_fast=5,
                macd_slow=13,
                macd_signal=4,
            )

        mocked.assert_called_once()
        _, kwargs = mocked.call_args
        self.assertEqual(
            {"fast": 5, "slow": 13, "signal": 4},
            kwargs,
        )
        self.assertTrue(state["macd_latch_bear"])

    def test_live_replay_matches_backtest_latches(self):
        closes = (
            [100.0 + index * 0.15 for index in range(80)]
            + [112.0 - index * 0.3 for index in range(35)]
            + [101.5 + index * 0.25 for index in range(45)]
        )
        frame = index_bars(closes)
        for mode in ("ma10_latch", "macd_death_latch", "any_latch"):
            with self.subTest(mode=mode):
                config = {
                    "entry_filters": {
                        "trend_gate_enabled": True,
                        "trend_fast_ma": 8,
                        "trend_slow_ma": 21,
                        "fast_gate_mode": mode,
                    },
                    "signal_strategy": {
                        "macd": {
                            "fast": 5,
                            "slow": 13,
                            "signal": 4,
                            "long_ma_period": 30,
                        }
                    },
                    "backtest": {"market_gate_slope_bars": 5},
                }
                gate = build_market_gate(frame, config)
                for index in range(35, len(frame)):
                    day = frame.iloc[index]["datetime"].date().isoformat()
                    live = _fast_gate_latch_state(
                        frame.iloc[: index + 1],
                        mode,
                        macd_fast=5,
                        macd_slow=13,
                        macd_signal=4,
                    )
                    self.assertEqual(
                        gate[day]["ma10_latch_bear"],
                        live["ma10_latch_bear"],
                        day,
                    )
                    self.assertEqual(
                        gate[day]["macd_latch_bear"],
                        live["macd_latch_bear"],
                        day,
                    )


class LiveMarketContextTests(unittest.TestCase):
    def test_chan_sell_context_does_not_become_a_live_only_death_cross_gate(self):
        frame = index_bars([100.0 + index * 0.1 for index in range(300)])
        monitor = SignalMonitor.__new__(SignalMonitor)
        monitor.config = {
            "entry_filters": {
                "market_gate_enabled": True,
                "market_gate_fail_open": False,
                "market_index_code": "000001.SH",
                "trend_gate_enabled": True,
                "trend_fast_ma": 20,
                "trend_slow_ma": 60,
                "fast_gate_mode": "none",
            },
            "signal_strategy": {
                "macd": {"fast": 12, "slow": 26, "signal": 9}
            },
        }
        monitor.market = MagicMock()
        monitor.market.get_index_bars.return_value = frame
        monitor.analyzer = MagicMock()
        monitor.analyzer.analyze.return_value = {
            "timeframes": {
                "1d": {
                    "status": "ok",
                    "indicators": {
                        "ma_long": 110.0,
                        "death_cross": False,
                        "ma_long_down": False,
                        "above_ma_long": True,
                        "ma_long_up": True,
                    },
                    "chan": {
                        "fresh_signals": [
                            {"side": "sell", "signal_type": "sell_1"}
                        ]
                    },
                }
            }
        }

        context = monitor._market_entry_context()

        self.assertTrue(context["allows_entries"])
        self.assertFalse(context["death_cross"])
        self.assertTrue(context["bearish_structure"])


if __name__ == "__main__":
    unittest.main()
