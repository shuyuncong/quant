from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import volume_price_audit as audit  # noqa: E402


def _frame(current_close=11.0, current_volume=200.0, count=30):
    close = [10.0] * count
    close[-1] = current_close
    volume = [100.0] * count
    volume[-1] = current_volume
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2026-01-01", periods=count),
            "open": close,
            "high": [value + 0.2 for value in close],
            "low": [value - 0.2 for value in close],
            "close": close,
            "volume": volume,
            "amount": [value * 1_000_000 for value in volume],
        }
    )


def test_primary_factor_is_fixed_and_quadrant_fields_are_diagnostic():
    assert audit.PRIMARY_FACTOR == "up_volume_confirmation"
    assert audit.FACTOR_SPECS[audit.PRIMARY_FACTOR]["role"] == "primary"
    for factor in (
        "signal_day_return",
        "volume_ratio_20",
        "amount_ratio_20",
        "volume_ratio_3_vs_20",
        "price_location_20",
        "turnover_value_proxy",
        "signed_volume_impulse",
    ):
        assert audit.FACTOR_SPECS[factor]["role"] == "diagnostic"


def test_up_expanding_is_the_only_primary_confirmed_quadrant():
    result = audit._volume_price_features(_frame(), 29, 100.0)
    assert result["factor_assignment_available"] is True
    assert result["signal_day_return"] > 0
    assert result["volume_ratio_20"] == 2.0
    assert result["up_volume_confirmation"] == 1
    assert result["volume_price_quadrant"] == "up_expanding"


def test_four_quadrants_are_classified_from_fixed_thresholds():
    cases = [
        (11.0, 200.0, "up_expanding", 1),
        (11.0, 50.0, "up_contracting", 0),
        (9.0, 200.0, "down_or_flat_expanding", 0),
        (9.0, 50.0, "down_or_flat_contracting", 0),
    ]
    for close, volume, expected, primary in cases:
        result = audit._volume_price_features(
            _frame(current_close=close, current_volume=volume), 29, 100.0
        )
        assert result["volume_price_quadrant"] == expected
        assert result["up_volume_confirmation"] == primary


def test_prior_20_volume_baseline_excludes_signal_day():
    result = audit._volume_price_features(
        _frame(current_volume=10_000.0), 29, 100.0
    )
    assert result["volume_ratio_20"] == 100.0
    assert result["baseline_excludes_signal_day"] is True


def test_features_have_no_lookahead_from_later_bars():
    frame = _frame()
    expected = audit._volume_price_features(frame, 29, 100.0)
    extended = pd.concat(
        [frame, _frame(current_close=1000.0, current_volume=999999.0, count=2)],
        ignore_index=True,
    )
    assert expected == audit._volume_price_features(extended, 29, 100.0)


def test_turnover_is_explicit_value_proxy_not_exchange_turnover():
    result = audit._volume_price_features(_frame(), 29, 100.0)
    expected = 200.0 * 1_000_000 / (100.0 * 100_000_000)
    assert result["turnover_value_proxy"] == expected


def test_non_positive_cached_amount_is_reported_as_unavailable():
    frame = _frame()
    frame["amount"] = 0.0
    result = audit._volume_price_features(frame, 29, 100.0)
    assert result["factor_assignment_available"] is True
    assert result["amount_ratio_20"] is None
    assert result["turnover_value_proxy"] is None


def test_zero_signal_day_volume_does_not_break_primary_assignment():
    result = audit._volume_price_features(
        _frame(current_close=11.0, current_volume=0.0), 29, 100.0
    )
    assert result["factor_assignment_available"] is True
    assert result["volume_ratio_20"] == 0.0
    assert result["volume_price_quadrant"] == "up_contracting"
    assert result["up_volume_confirmation"] == 0
    assert result["signed_volume_impulse"] is None


def test_insufficient_history_is_rejected():
    result = audit._volume_price_features(_frame(count=20), 19, 100.0)
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "insufficient_volume_price_history"
