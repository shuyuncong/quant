"""Tests for the decoupled macd_divergence research pool."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from strategy.macd_divergence import (  # noqa: E402
    MIN_BARS,
    POOL_TYPE_DIVERGENCE,
    add_divergence_indicators,
    bottom_divergence_at,
    divergence_signal_at,
    evaluate_conditions,
    last_bar_divergence_candidate,
    resolve_divergence_config,
)


def _config(**overrides):
    config = {
        "macd_divergence": {
            "enabled": True,
            "macd": {"fast": 12, "slow": 26, "signal": 9},
            "zero_axis_tolerance": 0.005,
            "volume_window": 20,
            "min_volume_ratio": 1.5,
            "long_ma_period": 250,
            "long_ma_slope_window": 20,
            "min_macd_segment_bars": 2,
        }
    }
    config["macd_divergence"].update(overrides)
    return resolve_divergence_config(config)


def _bars_from_close(
    close, volume_mult: float = 2.0, bump_at: int | None = None
) -> pd.DataFrame:
    """Build a daily frame with a known volume multiple on one bar."""
    close = np.asarray(close, dtype=float)
    n = len(close)
    dates = pd.bdate_range("2024-01-01", periods=n)
    volume = np.full(n, 1_000_000.0)
    volume[n - 1 if bump_at is None else bump_at] = 1_000_000.0 * volume_mult
    return pd.DataFrame(
        {
            "datetime": dates,
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
            "is_closed": True,
        }
    )


# Deterministic four-condition fixture: trend + two descending dips. The values
# were located by a seeded search over this generator; PCG64 streams are stable
# across numpy versions, so the setup is reproducible without any data files.
FIXTURE_SEED = 40
FIXTURE_BAR = 296


def _fixture_close(seed: int = FIXTURE_SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = 320
    close = 12.0 + np.cumsum(rng.normal(0.35, 0.9, n))
    close = np.clip(close, 5.0, None)
    close[-40:-25] = np.linspace(close[-41], close[-41] - 4.0, 15)
    close[-25:-10] = np.linspace(close[-25], close[-25] + 3.0, 15)
    close[-10:] = np.linspace(close[-11], close[-11] - 3.2, 10)
    return close


def _hist_series(runs):
    """Build (hist, low) from runs of (length, hist_value, low_start, low_end)."""
    hist: list[float] = []
    low: list[float] = []
    for length, hist_value, low_start, low_end in runs:
        hist.extend([float(hist_value)] * length)
        low.extend(np.linspace(low_start, low_end, length))
    return pd.Series(hist, dtype=float), pd.Series(low, dtype=float)


class ResolveConfigTests(unittest.TestCase):
    def test_missing_block_disables_pool(self):
        self.assertFalse(resolve_divergence_config({})["enabled"])

    def test_defaults_are_explicit(self):
        settings = _config()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["min_volume_ratio"], 1.5)
        self.assertEqual(settings["long_ma_period"], 250)


class BottomDivergenceTests(unittest.TestCase):
    # 20 bars: two completed negative runs (indices 5-8 and 14-17) plus a
    # trailing positive run so both negative runs are closed before the signal.
    def _divergence_runs(self, second_low_start: float, second_low_end: float):
        return [
            (5, 1.0, 30.0, 30.0),
            (4, -1.0, 20.0, 12.0),
            (5, 1.0, 20.0, 25.0),
            (4, -0.4, second_low_start, second_low_end),
            (2, 1.0, 12.0, 12.0),
        ]

    def test_new_low_with_smaller_area_is_divergence(self):
        hist, low = _hist_series(self._divergence_runs(18.0, 11.0))
        result = bottom_divergence_at(hist, low, 19, 2)
        self.assertTrue(result["available"])
        self.assertTrue(result["bottom_divergence"])
        self.assertLess(result["area_ratio"], 1.0)
        self.assertLess(result["price_new_low_pct"], 0.0)

    def test_higher_low_is_not_divergence(self):
        hist, low = _hist_series(self._divergence_runs(13.5, 13.0))
        result = bottom_divergence_at(hist, low, 19, 2)
        self.assertTrue(result["available"])
        self.assertFalse(result["bottom_divergence"])

    def test_larger_area_is_not_divergence(self):
        runs = self._divergence_runs(18.0, 11.0)
        runs[3] = (4, -1.6, 18.0, 11.0)
        hist, low = _hist_series(runs)
        result = bottom_divergence_at(hist, low, 19, 2)
        self.assertTrue(result["available"])
        self.assertFalse(result["bottom_divergence"])

    def test_active_cycle_is_excluded(self):
        hist, low = _hist_series(self._divergence_runs(18.0, 11.0))
        # Signal bar sits inside the second negative run: it is not completed yet.
        result = bottom_divergence_at(hist, low, 15, 2)
        self.assertFalse(result["bottom_divergence"])

    def test_short_runs_are_filtered_by_min_segment(self):
        runs = [
            (5, 1.0, 30.0, 30.0),
            (1, -1.0, 20.0, 12.0),
            (5, 1.0, 20.0, 25.0),
            (1, -0.4, 18.0, 11.0),
            (2, 1.0, 12.0, 12.0),
        ]
        hist, low = _hist_series(runs)
        result = bottom_divergence_at(hist, low, 13, 2)
        self.assertFalse(result["available"])


class FastPathEquivalenceTests(unittest.TestCase):
    """The backtest's vectorized divergence flag must equal the shipped checker.

    backtest_macd_divergence._divergence_flags is an O(n) fast path; if it ever
    drifts from bottom_divergence_at, backtests stop describing the live screen.
    """

    def test_matches_reference_on_synthetic_histogram(self):
        from backtest_macd_divergence import _divergence_flags

        settings = _config()
        frame = add_divergence_indicators(
            _bars_from_close(
                _fixture_close(), volume_mult=2.0, bump_at=FIXTURE_BAR
            ),
            settings,
        )
        hist = frame["hist"].to_numpy(dtype=float)
        low = frame["low"].to_numpy(dtype=float)
        flags = _divergence_flags(hist, low, int(settings["min_macd_segment_bars"]))
        checked = 0
        for index in range(MIN_BARS - 1, len(frame)):
            reference = bool(
                bottom_divergence_at(
                    frame["hist"],
                    frame["low"],
                    index,
                    int(settings["min_macd_segment_bars"]),
                )["bottom_divergence"]
            )
            self.assertEqual(
                reference, bool(flags[index]), f"divergence flag differs at {index}"
            )
            checked += 1
        self.assertGreater(checked, 20)

    def test_flags_are_causal(self):
        from backtest_macd_divergence import _divergence_flags

        hist = np.concatenate(
            [
                np.full(5, 1.0),
                np.full(4, -1.0),
                np.full(5, 1.0),
                np.full(4, -0.4),
                np.full(2, 1.0),
            ]
        )
        low = np.concatenate(
            [
                np.full(5, 30.0),
                np.linspace(20.0, 12.0, 4),
                np.full(5, 20.0),
                np.linspace(18.0, 11.0, 4),
                np.full(2, 12.0),
            ]
        )
        flags = _divergence_flags(hist, low, 2)
        # The divergence is only knowable once the second negative run closed.
        self.assertFalse(bool(flags[:18].any()))
        self.assertTrue(bool(flags[18:].all()))


class ConditionFunnelTests(unittest.TestCase):
    """The funnel must report each gate independently, not short-circuit."""

    @classmethod
    def setUpClass(cls):
        cls.settings = _config()
        cls.frame = add_divergence_indicators(
            _bars_from_close(
                _fixture_close(), volume_mult=2.0, bump_at=FIXTURE_BAR
            ),
            cls.settings,
        )

    def test_all_gates_true_on_the_fixture(self):
        gates = evaluate_conditions(self.frame, FIXTURE_BAR, self.settings)
        self.assertTrue(gates["available"])
        for key in (
            "golden_cross",
            "zero_axis_ok",
            "volume_ok",
            "above_yearline",
            "divergence_ok",
            "all",
        ):
            self.assertTrue(gates[key], key)

    def test_volume_failure_still_reports_other_gates(self):
        frame = add_divergence_indicators(
            _bars_from_close(
                _fixture_close(), volume_mult=1.0, bump_at=FIXTURE_BAR
            ),
            self.settings,
        )
        gates = evaluate_conditions(frame, FIXTURE_BAR, self.settings)
        self.assertTrue(gates["available"])
        self.assertTrue(gates["golden_cross"])
        self.assertTrue(gates["divergence_ok"])
        self.assertFalse(gates["volume_ok"])
        self.assertFalse(gates["all"])

    def test_unavailable_when_history_too_short(self):
        frame = add_divergence_indicators(
            _bars_from_close(np.linspace(10.0, 12.0, 100)), self.settings
        )
        gates = evaluate_conditions(frame, len(frame) - 1, self.settings)
        self.assertFalse(gates["available"])
        self.assertFalse(gates["all"])

    def test_precomputed_frame_matches_direct_call(self):
        bars = _bars_from_close(
            _fixture_close(), volume_mult=2.0, bump_at=FIXTURE_BAR
        )
        trimmed = bars.iloc[: FIXTURE_BAR + 1].reset_index(drop=True)
        direct = last_bar_divergence_candidate(
            "000002", "x", trimmed, self.settings
        )
        reuse = last_bar_divergence_candidate(
            "000002",
            "x",
            trimmed,
            self.settings,
            frame=add_divergence_indicators(trimmed, self.settings),
        )
        self.assertEqual(direct, reuse)


class GateTests(unittest.TestCase):
    """Gate-level tests: one fixture satisfies all four conditions, then each
    condition is driven to failure in turn."""

    @classmethod
    def setUpClass(cls):
        cls.settings = _config()
        close = _fixture_close()
        cls.bars = _bars_from_close(close, volume_mult=2.0, bump_at=FIXTURE_BAR)
        cls.frame = add_divergence_indicators(cls.bars, cls.settings)

    def test_full_path_candidate_has_all_four_conditions(self):
        signal = divergence_signal_at(self.frame, FIXTURE_BAR, self.settings)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["signal_type"], "macd_divergence_bottom")
        self.assertIn(signal["zero_axis_zone"], {"above", "near"})
        self.assertGreaterEqual(
            signal["volume_ratio"], self.settings["min_volume_ratio"]
        )
        self.assertLess(signal["divergence_area_ratio"], 1.0)
        self.assertLess(signal["divergence_price_new_low_pct"], 0.0)
        self.assertEqual(len(signal["conditions"]), 4)
        self.assertEqual(signal["entry_reference"], "next_day_open")
        self.assertTrue(signal["research_only"])

    def test_last_bar_path_matches_index_path(self):
        # Same bar, but as the final bar of the frame.
        trimmed = self.bars.iloc[: FIXTURE_BAR + 1].reset_index(drop=True)
        candidate = last_bar_divergence_candidate(
            "600036", "招商银行", trimmed, self.settings
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["pool_type"], POOL_TYPE_DIVERGENCE)
        self.assertEqual(candidate["symbol"], "600036")
        self.assertEqual(candidate["signal_date"], "2025-02-18")

    def test_volume_gate_rejects_thin_bar(self):
        thin = _bars_from_close(
            _fixture_close(), volume_mult=1.0, bump_at=FIXTURE_BAR
        )
        frame = add_divergence_indicators(thin, self.settings)
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_volume_gate_accepts_exact_threshold(self):
        exact = _bars_from_close(
            _fixture_close(),
            volume_mult=self.settings["min_volume_ratio"],
            bump_at=FIXTURE_BAR,
        )
        frame = add_divergence_indicators(exact, self.settings)
        self.assertIsNotNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_below_zero_cross_is_rejected(self):
        frame = self.frame.copy()
        frame.loc[FIXTURE_BAR, "dif"] = -0.5
        frame.loc[FIXTURE_BAR, "dea"] = -0.6
        frame.loc[FIXTURE_BAR, "golden_cross"] = True
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_below_yearline_is_rejected(self):
        frame = self.frame.copy()
        frame.loc[FIXTURE_BAR, "close"] = (
            float(frame.loc[FIXTURE_BAR, "ma_long"]) * 0.9
        )
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_flat_yearline_is_rejected(self):
        frame = self.frame.copy()
        frame.loc[FIXTURE_BAR, "ma_long_prev"] = (
            float(frame.loc[FIXTURE_BAR, "ma_long"]) * 1.01
        )
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_missing_golden_cross_is_rejected(self):
        frame = self.frame.copy()
        frame.loc[FIXTURE_BAR, "golden_cross"] = False
        self.assertIsNone(divergence_signal_at(frame, FIXTURE_BAR, self.settings))

    def test_insufficient_history_returns_none(self):
        bars = _bars_from_close(np.linspace(10.0, 12.0, 100))
        frame = add_divergence_indicators(bars, _config())
        self.assertIsNone(divergence_signal_at(frame, len(frame) - 1, _config()))
        self.assertIsNone(
            last_bar_divergence_candidate("600036", "招商银行", bars, _config())
        )

    def test_below_zero_signal_type_absent_from_pool_names(self):
        # Guard: this pool must never emit the production MACD signal types.
        signal = divergence_signal_at(self.frame, FIXTURE_BAR, self.settings)
        assert signal is not None
        self.assertFalse(
            signal["signal_type"].startswith("macd_golden_cross_pullback_confirmed_")
        )


if __name__ == "__main__":
    unittest.main()
