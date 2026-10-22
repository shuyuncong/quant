from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from strategy.indicators import TechnicalIndicators


class TechnicalIndicatorsTests(unittest.TestCase):
    def test_moving_average_matches_pandas_rolling(self):
        values = pd.Series(np.arange(1.0, 11.0))

        actual = TechnicalIndicators.calculate_ma(values, period=3)
        expected = values.rolling(3, min_periods=3).mean().to_numpy()

        np.testing.assert_allclose(actual, expected, equal_nan=True)

    def test_macd_uses_ema_and_histogram_is_macd_minus_signal(self):
        values = pd.Series(np.linspace(10.0, 25.0, 80))

        actual_macd, actual_signal, actual_hist = TechnicalIndicators.calculate_macd(
            values, fast=12, slow=26, signal=9
        )
        ema_fast = values.ewm(span=12, adjust=False, min_periods=12).mean()
        ema_slow = values.ewm(span=26, adjust=False, min_periods=26).mean()
        expected_macd = ema_fast - ema_slow
        expected_signal = expected_macd.ewm(span=9, adjust=False, min_periods=9).mean()

        np.testing.assert_allclose(actual_macd, expected_macd.to_numpy(), equal_nan=True)
        np.testing.assert_allclose(actual_signal, expected_signal.to_numpy(), equal_nan=True)
        np.testing.assert_allclose(
            actual_hist, (expected_macd - expected_signal).to_numpy(), equal_nan=True
        )

    def test_rsi_of_strictly_rising_series_reaches_100(self):
        values = pd.Series(np.arange(1.0, 40.0))

        actual = TechnicalIndicators.calculate_rsi(values, period=14)

        self.assertTrue(np.isfinite(actual[14:]).all())
        np.testing.assert_allclose(actual[14:], 100.0)

    def test_analyze_stock_technical_does_not_require_talib(self):
        close = np.linspace(10.0, 20.0, 300)
        frame = pd.DataFrame({"close": close, "volume": np.full(300, 10000.0)})

        result = TechnicalIndicators.analyze_stock_technical(frame, ma_period=250)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(result["current_price"], 20.0)
        self.assertGreater(result["ma250"], 10.0)
        self.assertEqual(result["volume_ratio"], 1.0)


if __name__ == "__main__":
    unittest.main()
