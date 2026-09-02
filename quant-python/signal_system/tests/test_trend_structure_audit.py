from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trend_structure_audit as audit  # noqa: E402


def _frame(count=320, start=10.0, step=0.1):
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2024-01-01", periods=count),
            "close": [start + index * step for index in range(count)],
        }
    )


def _row(symbol, day, slope):
    return {
        "candidate_id": f"{symbol}|{day}|macd_above",
        "symbol": symbol,
        "signal_day": day,
        "signal_type": "macd_above",
        "ma250_slope_20": slope,
    }


def test_primary_factor_is_predeclared_and_other_trend_fields_are_diagnostic():
    assert audit.PRIMARY_FACTOR == "ma250_slope_20"
    assert audit.FACTOR_SPECS["ma250_slope_20"]["role"] == "primary"
    for factor in (
        "ma60_slope_20",
        "ma20_slope_5",
        "close_ma20_distance",
        "close_ma60_distance",
        "close_ma250_distance",
        "bullish_alignment",
    ):
        assert audit.FACTOR_SPECS[factor]["role"] == "diagnostic"


def test_trend_features_use_only_signal_day_and_prior_bars():
    frame = _frame()
    result = audit._trend_features(frame, 299)
    extended = pd.concat(
        [frame, _frame(5, start=1000.0, step=100.0)], ignore_index=True
    )
    assert result == audit._trend_features(extended, 299)
    assert result["factor_assignment_available"] is True
    assert result["ma250_slope_20"] > 0
    assert result["ma60_slope_20"] > 0
    assert result["ma20_slope_5"] > 0
    assert result["bullish_alignment"] == 1


def test_trend_features_require_full_ma250_slope_history():
    result = audit._trend_features(_frame(269), 268)
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "insufficient_ma250_slope_history"


def test_flat_history_has_zero_primary_slope_and_no_bullish_alignment():
    result = audit._trend_features(_frame(step=0.0), 299)
    assert result["ma250_slope_20"] == 0.0
    assert result["bullish_alignment"] == 0


def test_primary_halves_require_same_day_variation_and_multiple_candidates():
    rows = [
        _row("000001", "2026-01-02", 0.01),
        _row("000002", "2026-01-02", 0.03),
        _row("000003", "2026-01-03", 0.02),
        _row("000004", "2026-01-04", 0.01),
        _row("000005", "2026-01-04", 0.01),
    ]
    audit._assign_primary_halves(rows)
    assert {row["symbol"] for row in rows if row["variant_included"]} == {
        "000002"
    }
    assert rows[2]["primary_high_quality"] is None
    assert rows[3]["primary_high_quality"] is None
    assert rows[4]["primary_high_quality"] is None
