from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import macd_divergence_audit as audit  # noqa: E402


def _frame(hist, close, *, low=None, high=None):
    count = len(hist)
    close_series = list(map(float, close))
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2026-01-01", periods=count),
            "close": close_series,
            "low": list(map(float, low or close_series)),
            "high": list(map(float, high or close_series)),
            "hist": list(map(float, hist)),
        }
    )


def test_bottom_area_divergence_uses_two_completed_negative_cycles():
    frame = _frame(
        [-2, -3, -1, 1, 1, -1, -1, 0.5],
        [10, 9, 8, 9, 9, 8, 7, 8],
        low=[9.8, 8.8, 7.8, 8.8, 8.9, 7.8, 6.8, 7.8],
    )
    result = audit._area_divergence_features(frame, 7)
    assert result["bottom_comparable"] is True
    assert result["bottom_area_ratio"] == 2 / 6
    assert result["bottom_price_new_low_pct"] < 0
    assert result["bullish_divergence"] is True
    assert result["bottom_confirmation_wait_bars"] == 1


def test_top_area_divergence_is_symmetric():
    frame = _frame(
        [1, 2, 1, -1, -1, 0.5, 0.5, -0.2],
        [10, 11, 12, 11, 10, 12, 13, 12],
        high=[10.2, 11.2, 12.2, 11.2, 10.2, 12.2, 13.2, 12.2],
    )
    result = audit._area_divergence_features(frame, 7)
    assert result["top_comparable"] is True
    assert result["top_area_ratio"] == 1 / 4
    assert result["top_price_new_high_pct"] > 0
    assert result["bearish_divergence"] is True
    assert result["top_confirmation_wait_bars"] == 1


def test_incomplete_current_cycle_is_not_used_as_confirmed_divergence():
    frame = _frame(
        [-2, -2, 1, 1, -0.5, -0.5],
        [10, 9, 10, 10, 8, 7],
    )
    result = audit._area_divergence_features(frame, 5)
    assert result["bottom_comparable"] is False
    assert result["bullish_divergence"] is False


def test_factor_has_no_lookahead_from_later_bars():
    base = _frame(
        [-2, -3, -1, 1, 1, -1, -1, 0.5],
        [10, 9, 8, 9, 9, 8, 7, 8],
    )
    extended = pd.concat(
        [
            base,
            _frame([-100, 100], [1, 100]).assign(
                datetime=pd.bdate_range("2026-02-01", periods=2)
            ),
        ],
        ignore_index=True,
    )
    assert audit._area_divergence_features(base, 7) == audit._area_divergence_features(
        extended, 7
    )


def test_primary_variant_requires_confirmed_bullish_divergence():
    rows = [
        {"candidate_id": "a", "bullish_divergence": True, "bearish_divergence": False},
        {"candidate_id": "b", "bullish_divergence": False, "bearish_divergence": False},
        {"candidate_id": "c", "bullish_divergence": True, "bearish_divergence": True},
    ]
    retained = audit._apply_variant(rows, "require_bullish_divergence")
    assert [row["candidate_id"] for row in retained] == ["a", "c"]


def test_alternate_variant_only_excludes_confirmed_bearish_divergence():
    rows = [
        {"candidate_id": "a", "bullish_divergence": False, "bearish_divergence": False},
        {"candidate_id": "b", "bullish_divergence": False, "bearish_divergence": True},
    ]
    retained = audit._apply_variant(rows, "exclude_bearish_divergence")
    assert [row["candidate_id"] for row in retained] == ["a"]


def test_cluster_bootstrap_is_deterministic():
    rows = [
        {"symbol": "000001", "variant_included": True, "future_40d": 5.0},
        {"symbol": "000001", "variant_included": False, "future_40d": 1.0},
        {"symbol": "000002", "variant_included": True, "future_40d": 4.0},
        {"symbol": "000002", "variant_included": False, "future_40d": 0.0},
    ]
    first = audit._cluster_bootstrap_delta(
        rows, "future_40d", reps=100, seed=7
    )
    second = audit._cluster_bootstrap_delta(
        rows, "future_40d", reps=100, seed=7
    )
    assert first == second
    assert first["mean_delta"] == 4.0
    assert first["ci95_low"] is not None


def test_daily_rank_ic_orients_smaller_area_ratio_as_better():
    rows = [
        {"signal_day": "2026-01-02", "bottom_area_ratio": 0.2, "future_40d": 5.0},
        {"signal_day": "2026-01-02", "bottom_area_ratio": 0.5, "future_40d": 3.0},
        {"signal_day": "2026-01-02", "bottom_area_ratio": 0.9, "future_40d": 1.0},
    ]
    result = audit._daily_rank_ic(
        rows, factor="bottom_area_ratio", outcome="future_40d", ascending_good=False
    )
    assert result["days"] == 1
    assert result["median"] == 1.0


def test_variant_factor_specs_do_not_mix_bottom_and_top_area_reports():
    bottom = audit._variant_factor_spec("require_bullish_divergence")
    top = audit._variant_factor_spec("exclude_bearish_divergence")

    assert bottom == {
        "comparable_key": "bottom_comparable",
        "factor": "bottom_area_ratio",
        "smaller_is_better": True,
    }
    assert top == {
        "comparable_key": "top_comparable",
        "factor": "top_area_ratio",
        "smaller_is_better": False,
    }
