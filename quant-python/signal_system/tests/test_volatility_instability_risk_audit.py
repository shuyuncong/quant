from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import volatility_instability_risk_audit as audit  # noqa: E402


def _source(
    symbol: str = "000001",
    day: str = "2026-04-01",
    signal_type: str = "macd_above",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "signal_day": day,
        "signal_type": signal_type,
        "regime": "bull",
    }


def _close_series(
    returns: np.ndarray,
    *,
    start: str = "2025-12-01",
    drop: set[int] | None = None,
) -> pd.Series:
    closes = 100.0 * np.cumprod(np.r_[1.0, 1.0 + returns])
    index = pd.bdate_range(start, periods=len(closes))
    series = pd.Series(closes, index=index, name="close")
    if drop:
        series = series.drop(series.index[list(sorted(drop))])
    return series


def _frame(series: pd.Series, *, adjust: str = "qfq") -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "datetime": series.index,
            "close": series.to_numpy(),
            "is_closed": [True] * len(series),
        }
    )
    frame.attrs.update({"adjust": adjust, "timeframe": "1d"})
    return frame


def _row(
    symbol: str,
    day: str,
    low: bool | None,
    outcome: float | None,
) -> dict[str, object]:
    row = {
        **_source(symbol, day),
        "candidate_id": f"{symbol}|{day}|macd_above",
        audit.PRIMARY_FACTOR: 0.1 if low else 0.8,
        "factor_assignment_available": True,
        "primary_low_volatility_instability_risk": low,
        "normalized_regime": "bull",
    }
    for field in audit.FIELDS:
        row[field] = outcome
    return row


def _valid_source_match(count: int) -> dict[str, object]:
    return {
        "source_rows": count,
        "common_eligible_rows": count,
        "simulated_rows": count,
        "source_pnl_abs_diff": {},
    }


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "input_dir": str(audit.CANONICAL_INPUT_DIR),
        "output_dir": str(audit.FORMAL_OUTPUT_DIR),
        "config": str(audit.CONFIG_PATH),
        "label": audit.PREREGISTERED_LABEL,
        "splits": list(audit.SPLITS),
        "seed": audit.PREREGISTERED_SEED,
        "bootstrap_reps": audit.PREREGISTERED_BOOTSTRAP_REPS,
        "expected_script_sha256": audit.file_sha256(Path(audit.__file__)),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_contract_constants_are_frozen():
    assert audit.PRIMARY_FACTOR == "volatility_instability_cv_6x10"
    assert audit.RETURN_OBSERVATIONS == 60
    assert audit.BLOCK_COUNT == 6
    assert audit.BLOCK_RETURN_OBSERVATIONS == 10
    assert audit.CLOSES_REQUIRED == 61
    assert audit.PREREGISTERED_SEED == 20260905
    assert audit.PREREGISTERED_BOOTSTRAP_REPS == 2000
    assert audit.MIN_ASSIGNMENT_COVERAGE == pytest.approx(0.90)
    assert audit.MIN_AFFECTED_CANDIDATES == 30
    assert audit.MIN_AFFECTED_SYMBOLS == 10
    assert audit.MIN_AFFECTED_SIGNAL_DAYS == 10
    assert audit.MIN_OUTCOME_CLUSTERS == 10
    assert audit.FACTOR_SPECS[audit.PRIMARY_FACTOR] == {
        "larger_is_better": False,
        "role": "primary",
    }
    assert audit.FACTOR_SPECS["block_rms_trend_slope_60"] == {
        "larger_is_better": None,
        "role": "diagnostic",
    }
    assert all(
        spec["role"] == "diagnostic"
        for name, spec in audit.FACTOR_SPECS.items()
        if name != audit.PRIMARY_FACTOR
    )


@pytest.mark.parametrize("value", ["1", "00001", "000001.SZ", "ABC001", ""])
def test_symbol_must_be_full_six_digits(value: str):
    with pytest.raises(RuntimeError, match="six digits"):
        audit._normalize_symbol(value)


def test_signal_day_must_be_canonical_iso():
    assert audit._normalize_signal_day("2026-04-01") == "2026-04-01"
    with pytest.raises(RuntimeError, match="invalid signal_day"):
        audit._normalize_signal_day("2026/04/01")


def test_symbol_day_duplicate_fails_even_when_signal_type_differs():
    rows = [
        _source("000001", signal_type="above"),
        _source("000001", signal_type="buy_1"),
    ]
    with pytest.raises(RuntimeError, match="symbol x signal_day"):
        audit._validate_symbol_day_uniqueness(rows)


def test_history_frame_validation_accepts_strict_qfq_daily_close():
    series = _close_series(np.linspace(-0.01, 0.01, 60))
    result = audit._validate_history_frame(_frame(series), Path("history.pkl"))
    pd.testing.assert_series_equal(
        result, series, check_names=False, check_freq=False
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda frame: frame.attrs.update({"adjust": "none"}), "adjust"),
        (lambda frame: frame.attrs.update({"timeframe": "5m"}), "timeframe"),
        (
            lambda frame: frame.__setitem__("close", [0.0] * len(frame)),
            "positive",
        ),
        (
            lambda frame: frame.__setitem__("is_closed", [1] * len(frame)),
            "non-bool",
        ),
        (
            lambda frame: frame.__setitem__(
                "close", ["1.0"] * len(frame)
            ),
            "non-numeric",
        ),
        (
            lambda frame: frame.__setitem__(
                "close", [True] * len(frame)
            ),
            "non-numeric",
        ),
        (
            lambda frame: frame.__setitem__(
                "close", [math.nan] + [1.0] * (len(frame) - 1)
            ),
            "finite",
        ),
        (
            lambda frame: frame.__setitem__(
                "close", [math.inf] + [1.0] * (len(frame) - 1)
            ),
            "finite",
        ),
    ],
)
def test_history_frame_corruption_fails_closed(mutate, message):
    frame = _frame(_close_series(np.linspace(-0.01, 0.01, 60)))
    mutate(frame)
    with pytest.raises(RuntimeError, match=message):
        audit._validate_history_frame(frame, Path("history.pkl"))


def test_history_duplicate_or_non_normalized_date_fails_closed():
    series = _close_series(np.linspace(-0.01, 0.01, 60))
    duplicate = _frame(series)
    duplicate.loc[1, "datetime"] = duplicate.loc[0, "datetime"]
    with pytest.raises(RuntimeError, match="duplicate"):
        audit._validate_history_frame(duplicate, Path("history.pkl"))

    intraday = _frame(series)
    intraday.loc[0, "datetime"] = intraday.loc[0, "datetime"] + pd.Timedelta(hours=1)
    with pytest.raises(RuntimeError, match="normalized"):
        audit._validate_history_frame(intraday, Path("history.pkl"))


def test_history_validates_unused_and_unclosed_rows_before_filtering():
    series = _close_series(np.linspace(-0.01, 0.01, 61))
    bad_close = _frame(series)
    bad_close.loc[len(bad_close) - 1, "is_closed"] = False
    bad_close["close"] = bad_close["close"].astype(object)
    bad_close.loc[len(bad_close) - 1, "close"] = "broken"
    with pytest.raises(RuntimeError, match="non-numeric"):
        audit._validate_history_frame(bad_close, Path("history.pkl"))

    bad_date = _frame(series)
    bad_date.loc[len(bad_date) - 1, "is_closed"] = False
    bad_date["datetime"] = bad_date["datetime"].astype(object)
    bad_date.loc[len(bad_date) - 1, "datetime"] = "not-a-date"
    with pytest.raises(RuntimeError, match="invalid datetime"):
        audit._validate_history_frame(bad_date, Path("history.pkl"))


def test_history_missing_column_or_descending_dates_fails_closed():
    series = _close_series(np.linspace(-0.01, 0.01, 60))
    missing = _frame(series).drop(columns=["close"])
    with pytest.raises(RuntimeError, match="missing columns"):
        audit._validate_history_frame(missing, Path("history.pkl"))

    descending = _frame(series).iloc[::-1].reset_index(drop=True)
    descending.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    with pytest.raises(RuntimeError, match="strictly increasing"):
        audit._validate_history_frame(descending, Path("history.pkl"))


def test_volatility_instability_matches_six_known_rms_blocks():
    amplitudes = np.asarray([0.01, 0.02, 0.03, 0.04, 0.05, 0.06])
    returns = np.repeat(amplitudes, 10)
    series = _close_series(returns)
    result = audit._volatility_instability_features(
        series, series.index[-1].date().isoformat()
    )
    assert result is not None
    assert result[audit.PRIMARY_FACTOR] == pytest.approx(
        np.std(amplitudes, ddof=0) / np.mean(amplitudes)
    )
    assert result["mean_block_rms_10_60"] == pytest.approx(0.035)
    assert result["max_block_rms_10_60"] == pytest.approx(0.06)
    assert result["block_rms_range_60"] == pytest.approx(0.05)
    assert result["latest_block_rms_10"] == pytest.approx(0.06)
    assert result["block_rms_trend_slope_60"] == pytest.approx(0.01)
    assert result["block_count"] == 6
    assert result["block_return_observations"] == 10


def test_blocks_are_oldest_to_newest_nonoverlapping_ten_return_slices():
    block_rms = np.asarray([0.06, 0.01, 0.05, 0.02, 0.04, 0.03])
    returns = np.repeat(block_rms, 10)
    series = _close_series(returns)
    result = audit._volatility_instability_features(
        series, series.index[-1].date().isoformat()
    )
    assert result is not None
    assert result["latest_block_rms_10"] == pytest.approx(block_rms[-1])
    positions = np.arange(6, dtype=float)
    expected_slope = float(
        np.sum((positions - positions.mean()) * (block_rms - block_rms.mean()))
        / np.sum(np.square(positions - positions.mean()))
    )
    assert result["block_rms_trend_slope_60"] == pytest.approx(expected_slope)


def test_block_rms_is_not_demeaned_and_cv_uses_population_std():
    amplitudes = np.asarray([0.01, 0.02, 0.04, 0.08, 0.16, 0.32])
    returns = np.concatenate(
        [np.tile(np.asarray([value, -value]), 5) for value in amplitudes]
    )
    series = _close_series(returns)
    result = audit._volatility_instability_features(
        series, series.index[-1].date().isoformat()
    )
    assert result is not None
    expected = float(np.std(amplitudes, ddof=0) / np.mean(amplitudes))
    sample_cv = float(np.std(amplitudes, ddof=1) / np.mean(amplitudes))
    assert result["mean_block_rms_10_60"] == pytest.approx(
        np.mean(amplitudes)
    )
    assert result[audit.PRIMARY_FACTOR] == pytest.approx(expected)
    assert result[audit.PRIMARY_FACTOR] != pytest.approx(sample_cv)


def test_diagnostics_match_manual_rms_and_ols_formulas():
    returns = np.sin(np.arange(60) / 4.0) * 0.025
    series = _close_series(returns)
    result = audit._volatility_instability_features(
        series, series.index[-1].date().isoformat()
    )
    assert result is not None
    close_values = series.to_numpy(dtype=float)
    actual_returns = close_values[1:] / close_values[:-1] - 1.0
    blocks = actual_returns.reshape(6, 10)
    rms = np.sqrt(np.mean(np.square(blocks), axis=1))
    positions = np.arange(6, dtype=float)
    centered = positions - positions.mean()
    expected_slope = float(
        np.sum(centered * (rms - rms.mean())) / np.sum(np.square(centered))
    )
    assert result["mean_block_rms_10_60"] == pytest.approx(np.mean(rms))
    assert result["max_block_rms_10_60"] == pytest.approx(np.max(rms))
    assert result["block_rms_range_60"] == pytest.approx(
        np.max(rms) - np.min(rms)
    )
    assert result["latest_block_rms_10"] == pytest.approx(rms[-1])
    assert result["block_rms_trend_slope_60"] == pytest.approx(expected_slope)


def test_all_zero_returns_are_unavailable():
    series = _close_series(np.zeros(60))
    assert audit._volatility_instability_features(
        series, series.index[-1].date().isoformat()
    ) is None


def test_equal_nonzero_block_rms_is_available_with_zero_instability():
    series = _close_series(np.ones(60))
    result = audit._volatility_instability_features(
        series, series.index[-1].date().isoformat()
    )
    assert result is not None
    assert result["mean_block_rms_10_60"] == pytest.approx(1.0)
    assert result[audit.PRIMARY_FACTOR] == 0.0


def test_tiny_positive_mean_rms_is_not_floored_or_marked_unavailable():
    amplitudes = np.asarray([1, 2, 3, 4, 5, 6], dtype=float) * (2.0**-42)
    series = _close_series(np.repeat(amplitudes, 10))
    result = audit._volatility_instability_features(
        series, series.index[-1].date().isoformat()
    )
    assert result is not None
    assert 0.0 < result["mean_block_rms_10_60"] < 1e-10
    assert result[audit.PRIMARY_FACTOR] == pytest.approx(
        np.std(amplitudes, ddof=0) / np.mean(amplitudes), rel=1e-3
    )


def test_features_are_scale_invariant_and_ignore_post_signal_suffix():
    returns = np.sin(np.arange(80) / 3.0) * 0.02
    series = _close_series(returns)
    day = series.index[65].date().isoformat()
    expected = audit._volatility_instability_features(series, day)
    scaled = audit._volatility_instability_features(series * 8.0, day)
    modified = series.copy()
    modified.iloc[70:] *= np.linspace(2.0, 9.0, len(modified.iloc[70:]))
    suffix_changed = audit._volatility_instability_features(modified, day)
    assert scaled == pytest.approx(expected)
    assert suffix_changed == pytest.approx(expected)


def test_features_are_unavailable_with_only_60_closes():
    series = _close_series(np.linspace(-0.01, 0.01, 59))
    assert audit._volatility_instability_features(
        series, series.index[-1].date().isoformat()
    ) is None


def _feature_cache(block_profiles: dict[str, list[float]]) -> tuple[
    list[dict[str, object]],
    dict[str, pd.Series],
    dict[str, dict[str, object]],
]:
    day = pd.bdate_range("2025-12-01", periods=61)[-1].date().isoformat()
    sources = [_source(symbol, day) for symbol in block_profiles]
    cache = {
        symbol: _close_series(np.repeat(np.asarray(profile) * 0.001, 10))
        for symbol, profile in block_profiles.items()
    }
    snapshot = {
        f"qfq:{symbol}": {
            "path": f"{symbol}.pkl",
            "size_bytes": 1,
            "sha256": symbol,
        }
        for symbol in block_profiles
    }
    return sources, cache, snapshot


def test_factor_features_compute_instability_and_same_day_groups():
    sources, cache, snapshot = _feature_cache(
        {
            "000001": [1, 1, 1, 1, 1, 1],
            "000002": [1, 1, 1, 1, 1, 1.2],
            "000003": [1, 1, 1, 1, 1, 2],
            "000004": [1, 1, 1, 1, 1, 4],
        }
    )
    features, meta = audit._factor_features_for_sources(
        sources, cache, snapshot
    )
    assert list(features) == [audit.candidate_id(row) for row in sources]
    assert all(item["factor_assignment_available"] for item in features.values())
    assert sum(
        item["primary_low_volatility_instability_risk"] is True for item in features.values()
    ) == 2
    assert sum(
        item["primary_low_volatility_instability_risk"] is False for item in features.values()
    ) == 2
    assert meta["return_observations"] == 60
    assert meta["block_count"] == 6
    assert meta["block_return_observations"] == 10
    assert meta["block_order"] == "oldest_to_newest"
    assert meta["blocks_overlap"] is False
    assert meta["block_rms_demeaned"] is False
    assert meta["block_rms_annualized"] is False
    assert meta["cv_std_ddof"] == 0
    assert meta["mean_rms_floor_used"] is False
    assert meta["qfq_uniform_scale_invariant"] is True
    assert meta["time_varying_adjustment_invariant"] is False
    assert meta["primary_unavailable_candidates"] == 0


def test_factor_features_ignore_source_legacy_outcomes():
    sources, cache, snapshot = _feature_cache(
        {
            "000001": [1, 1, 1, 1, 1, 1],
            "000002": [1, 1, 1, 1, 1, 2],
            "000003": [1, 1, 1, 1, 1, 4],
        }
    )
    changed = [
        {**row, "future_40d": index * 999.0, "trade_pnl_pct": -index * 777.0}
        for index, row in enumerate(sources, start=1)
    ]
    first, _ = audit._factor_features_for_sources(sources, cache, snapshot)
    second, _ = audit._factor_features_for_sources(changed, cache, snapshot)
    assert first == second


def test_median_tie_that_leaves_one_side_empty_is_not_comparable():
    sources, cache, snapshot = _feature_cache(
        {
            "000001": [1, 1, 1, 1, 1, 1],
            "000002": [1, 1, 1, 1, 1, 1],
            "000003": [1, 1, 1, 1, 1, 1],
        }
    )
    features, meta = audit._factor_features_for_sources(
        sources, cache, snapshot
    )
    assert all(
        item["factor_assignment_available"] for item in features.values()
    )
    assert all(
        item["primary_low_volatility_instability_risk"] is None for item in features.values()
    )
    assert meta["excluded_single_group_signal_days"] == 1


def test_zero_mean_block_rms_candidate_is_unavailable_and_counted():
    sources, cache, snapshot = _feature_cache(
        {
            "000001": [0, 0, 0, 0, 0, 0],
            "000002": [1, 1, 1, 1, 1, 2],
            "000003": [1, 1, 1, 1, 1, 4],
        }
    )
    features, meta = audit._factor_features_for_sources(
        sources, cache, snapshot
    )
    first = features[audit.candidate_id(sources[0])]
    assert first["factor_assignment_available"] is False
    assert first[audit.PRIMARY_FACTOR] is None
    assert meta["zero_mean_block_rms_candidates"] == 1
    assert meta["insufficient_history_candidates"] == 0


def test_insufficient_history_is_unavailable_but_missing_signal_day_fails():
    day = pd.bdate_range("2025-12-01", periods=60)[-1].date().isoformat()
    sources = [_source("000001", day), _source("000002", day)]
    cache = {
        "000001": _close_series(np.linspace(-0.01, 0.01, 59)),
        "000002": _close_series(np.linspace(-0.02, 0.02, 59)),
    }
    snapshot = {
        f"qfq:{symbol}": {
            "path": f"{symbol}.pkl",
            "size_bytes": 1,
            "sha256": symbol,
        }
        for symbol in cache
    }
    features, meta = audit._factor_features_for_sources(
        sources, cache, snapshot
    )
    assert all(
        item[audit.PRIMARY_FACTOR] is None for item in features.values()
    )
    assert meta["insufficient_history_candidates"] == 2

    missing_day_sources = [_source("000001", "2026-12-31")]
    with pytest.raises(RuntimeError, match="signal day is absent"):
        audit._factor_features_for_sources(
            missing_day_sources, cache, snapshot
        )


def test_block_rms_trend_slope_is_unoriented_and_has_no_directional_reports():
    rows = [
        {
            **_row("000001", "2026-04-01", True, 1.0),
            "block_rms_trend_slope_60": 0.09,
        },
        {
            **_row("000002", "2026-04-01", False, -1.0),
            "block_rms_trend_slope_60": 0.01,
        },
    ]
    report = audit._factor_report(
        rows,
        "block_rms_trend_slope_60",
        audit.FACTOR_SPECS["block_rms_trend_slope_60"][
            "larger_is_better"
        ],
    )
    assert report["larger_is_better"] is None
    assert report["directional_analysis_performed"] is False
    assert report["non_gating_diagnostic"] is True
    assert all(
        item["computed"] is False for item in report["rank_ic"].values()
    )
    assert all(
        item["best_minus_worst_pp"] is None
        for item in report["quantiles"].values()
    )


def test_authoritative_outcomes_require_every_field_and_finite_numeric_values():
    row = _row("000001", "2026-04-01", True, 1.0)
    audit._validate_authoritative_outcomes([row])
    missing = dict(row)
    missing.pop(audit.FIELDS[0])
    with pytest.raises(RuntimeError, match="field is missing"):
        audit._validate_authoritative_outcomes([missing])
    for value in (True, "1.0", math.nan, math.inf):
        invalid = dict(row)
        invalid["future_40d"] = value
        with pytest.raises(RuntimeError, match="not numeric|not finite"):
            audit._validate_authoritative_outcomes([invalid])


def test_valid_fe_rows_drop_none_and_days_without_both_groups():
    rows = [
        _row("000001", "2026-04-01", True, 4.0),
        _row("000002", "2026-04-01", False, 1.0),
        _row("000003", "2026-04-02", True, 2.0),
        _row("000004", "2026-04-02", False, None),
    ]
    valid = audit._valid_fe_rows(rows, "future_40d")
    assert [row["candidate_id"] for row in valid] == [
        "000001|2026-04-01|macd_above",
        "000002|2026-04-01|macd_above",
    ]


def test_fixed_effect_beta_is_day_controlled_not_pooled_mean():
    rows = [
        _row("000001", "2026-04-01", True, 4.0),
        _row("000002", "2026-04-01", True, 2.0),
        _row("000003", "2026-04-01", False, 0.0),
        _row("000004", "2026-04-02", True, 2.0),
        _row("000005", "2026-04-02", False, 0.0),
        _row("000006", "2026-04-02", False, 0.0),
    ]
    valid = audit._valid_fe_rows(rows, "future_40d")
    assert audit._fixed_effect_beta(valid, "future_40d") == pytest.approx(2.5)
    pooled = np.mean([4.0, 2.0, 2.0]) - np.mean([0.0, 0.0, 0.0])
    assert pooled != pytest.approx(2.5)


def test_weighted_fixed_effect_beta_uses_symbol_multiplicity():
    rows = [
        _row("000001", "2026-04-01", True, 4.0),
        _row("000002", "2026-04-01", False, 1.0),
        _row("000003", "2026-04-02", True, 8.0),
        _row("000004", "2026-04-02", False, 2.0),
    ]
    valid = audit._valid_fe_rows(rows, "future_40d")
    equal = audit._fixed_effect_beta(valid, "future_40d")
    weighted = audit._fixed_effect_beta(
        valid,
        "future_40d",
        {"000001": 3, "000002": 1, "000003": 1, "000004": 1},
    )
    assert equal == pytest.approx(4.5)
    assert weighted == pytest.approx(4.2)


def test_cluster_bootstrap_is_deterministic_and_uses_fixed_seed():
    rows = []
    for index in range(12):
        day = f"2026-04-{index + 1:02d}"
        rows.extend(
            [
                _row(f"{index + 1:06d}", day, True, 3.0 + index),
                _row(f"{index + 101:06d}", day, False, 1.0 + index),
            ]
        )
    first = audit._cluster_bootstrap_fe_low_minus_high(
        rows, "future_40d", reps=100, seed=audit.PREREGISTERED_SEED
    )
    second = audit._cluster_bootstrap_fe_low_minus_high(
        rows, "future_40d", reps=100, seed=audit.PREREGISTERED_SEED
    )
    assert first == second
    assert first["fixed_effect_beta"] == pytest.approx(2.0)
    assert first["reps_valid"] == 100
    assert first["cluster_count"] == 24
    assert first["seed"] == audit.PREREGISTERED_SEED


def test_fe_and_bootstrap_are_invariant_to_input_order():
    rows = [
        _row("000003", "2026-04-02", True, 8.0),
        _row("000001", "2026-04-01", True, 4.0),
        _row("000004", "2026-04-02", False, 2.0),
        _row("000002", "2026-04-01", False, 1.0),
    ]
    ordered = audit._valid_fe_rows(rows, "future_40d")
    assert [row["candidate_id"] for row in ordered] == [
        "000001|2026-04-01|macd_above",
        "000002|2026-04-01|macd_above",
        "000003|2026-04-02|macd_above",
        "000004|2026-04-02|macd_above",
    ]
    first = audit._cluster_bootstrap_fe_low_minus_high(
        rows, "future_40d", reps=50, seed=audit.PREREGISTERED_SEED
    )
    second = audit._cluster_bootstrap_fe_low_minus_high(
        list(reversed(rows)),
        "future_40d",
        reps=50,
        seed=audit.PREREGISTERED_SEED,
    )
    assert first == second


def test_cluster_bootstrap_percentile_linear_ci_and_invalid_replicates():
    rows = [
        _row("000001", "2026-04-01", True, 3.0),
        _row("000002", "2026-04-01", False, 1.0),
        _row("000003", "2026-04-02", True, 8.0),
        _row("000004", "2026-04-02", False, 2.0),
    ]
    reps = 40
    seed = 7
    report = audit._cluster_bootstrap_fe_low_minus_high(
        rows, "future_40d", reps=reps, seed=seed
    )

    rng = np.random.default_rng(seed)
    expected_samples = []
    day_differences = (2.0, 6.0)
    for _ in range(reps):
        selected = rng.integers(0, 4, size=4)
        counts = np.bincount(selected, minlength=4)
        numerator = 0.0
        denominator = 0.0
        for day_index, difference in enumerate(day_differences):
            low_weight = float(counts[day_index * 2])
            high_weight = float(counts[day_index * 2 + 1])
            if low_weight <= 0 or high_weight <= 0:
                continue
            day_weight = low_weight * high_weight / (low_weight + high_weight)
            numerator += day_weight * difference
            denominator += day_weight
        if denominator > 0:
            expected_samples.append(numerator / denominator)

    expected_ci = np.percentile(
        np.asarray(expected_samples), [2.5, 97.5], method="linear"
    )
    assert 0 < len(expected_samples) < reps
    assert report["reps_valid"] == len(expected_samples)
    assert report["ci95_low"] == pytest.approx(expected_ci[0])
    assert report["ci95_high"] == pytest.approx(expected_ci[1])


@pytest.mark.parametrize(
    "override",
    [
        {"reps_valid": audit.PREREGISTERED_BOOTSTRAP_REPS - 1},
        {"cluster_count": audit.MIN_OUTCOME_CLUSTERS - 1},
        {"fixed_effect_beta": None},
    ],
)
def test_bootstrap_contract_rejects_incomplete_or_small_cluster_reports(override):
    report = {
        "fixed_effect_beta": 1.0,
        "reps_requested": audit.PREREGISTERED_BOOTSTRAP_REPS,
        "reps_valid": audit.PREREGISTERED_BOOTSTRAP_REPS,
        "cluster_count": audit.MIN_OUTCOME_CLUSTERS,
    }
    report.update(override)
    assert audit._bootstrap_contract_ok(report) is False


def test_candidate_report_does_not_support_gate_when_bootstrap_contract_fails(
    monkeypatch,
):
    monkeypatch.setattr(audit, "MIN_AFFECTED_CANDIDATES", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SYMBOLS", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SIGNAL_DAYS", 2)
    rows = [
        _row("000001", "2026-04-01", True, 3.0),
        _row("000002", "2026-04-01", False, 1.0),
        _row("000003", "2026-04-02", True, 4.0),
        _row("000004", "2026-04-02", False, 2.0),
    ]

    def incomplete_bootstrap(*args, **kwargs):
        return {
            "fixed_effect_beta": 1.0,
            "ci95_low": 0.1,
            "ci95_high": 1.9,
            "reps_requested": audit.PREREGISTERED_BOOTSTRAP_REPS,
            "reps_valid": audit.PREREGISTERED_BOOTSTRAP_REPS - 1,
            "cluster_count": audit.MIN_OUTCOME_CLUSTERS,
            "signal_day_count": 2,
            "seed": audit.PREREGISTERED_SEED,
        }

    monkeypatch.setattr(
        audit, "_cluster_bootstrap_fe_low_minus_high", incomplete_bootstrap
    )
    report = audit._candidate_report(
        rows,
        _valid_source_match(len(rows)),
        {"all_pass": True},
        {},
        audit.PREREGISTERED_SEED,
    )
    assert report["gate"]["sample_sufficient"] is True
    assert report["gate"]["outcome_samples_sufficient"] is True
    assert report["gate"]["primary_bootstrap_contract_ok"] is False
    assert report["gate"]["primary_cluster_bootstrap_ci95_positive"] is False


def test_candidate_report_assignment_coverage_boundary_is_inclusive(monkeypatch):
    monkeypatch.setattr(audit, "MIN_AFFECTED_CANDIDATES", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SYMBOLS", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SIGNAL_DAYS", 2)

    def supported_bootstrap(*args, **kwargs):
        return {
            "fixed_effect_beta": 1.0,
            "ci95_low": 0.1,
            "ci95_high": 1.9,
            "reps_requested": audit.PREREGISTERED_BOOTSTRAP_REPS,
            "reps_valid": audit.PREREGISTERED_BOOTSTRAP_REPS,
            "cluster_count": audit.MIN_OUTCOME_CLUSTERS,
            "signal_day_count": 4,
            "seed": audit.PREREGISTERED_SEED,
        }

    monkeypatch.setattr(
        audit, "_cluster_bootstrap_fe_low_minus_high", supported_bootstrap
    )
    comparable = []
    for day_index in range(4):
        day = f"2026-04-{day_index + 1:02d}"
        comparable.extend(
            [
                _row(f"{day_index * 2 + 1:06d}", day, True, 2.0),
                _row(f"{day_index * 2 + 2:06d}", day, False, 1.0),
            ]
        )
    assigned_without_group = _row("000009", "2026-04-05", None, 1.0)
    unavailable = {
        **_row("000010", "2026-04-06", None, 1.0),
        "factor_assignment_available": False,
    }
    rows = comparable + [assigned_without_group, unavailable]
    boundary = audit._candidate_report(
        rows,
        _valid_source_match(len(rows)),
        {"all_pass": True},
        {},
        audit.PREREGISTERED_SEED,
    )
    assert boundary["assignment_coverage"] == pytest.approx(0.90)
    assert boundary["gate"]["assignment_coverage_ok"] is True
    assert boundary["gate"]["eligible_for_cross_split"] is True

    rows[-2]["factor_assignment_available"] = False
    below = audit._candidate_report(
        rows,
        _valid_source_match(len(rows)),
        {"all_pass": True},
        {},
        audit.PREREGISTERED_SEED,
    )
    assert below["assignment_coverage"] == pytest.approx(0.80)
    assert below["gate"]["assignment_coverage_ok"] is False
    assert below["gate"]["eligible_for_cross_split"] is False


def test_outcome_sample_gate_reapplies_after_none_filter(monkeypatch):
    monkeypatch.setattr(audit, "MIN_AFFECTED_CANDIDATES", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SYMBOLS", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SIGNAL_DAYS", 2)
    rows = [
        _row("000001", "2026-04-01", True, 3.0),
        _row("000002", "2026-04-01", False, 1.0),
        _row("000003", "2026-04-02", True, 3.0),
        _row("000004", "2026-04-02", False, None),
    ]
    report = audit._outcome_sample_gate(rows, "future_40d")
    assert report["valid_signal_days_with_both_groups"] == 1
    assert report["sufficient"] is False


def test_replay_integrity_checks_explicit_id_order_and_completeness():
    sources = [_source("000001"), _source("000002")]
    features = {
        audit.candidate_id(row): {"candidate_id": audit.candidate_id(row)}
        for row in sources
    }
    replay = [
        {**row, "candidate_id": audit.candidate_id(row)} for row in sources
    ]
    checks, source_match = audit._assert_replay_integrity(
        sources,
        features,
        replay,
        Counter(),
        _valid_source_match(2),
    )
    assert checks["all_pass"] is True
    assert source_match["source_outcomes_used"] is False
    replay[0]["candidate_id"] = "tampered"
    with pytest.raises(RuntimeError, match="replay integrity"):
        audit._assert_replay_integrity(
            sources,
            features,
            replay,
            Counter(),
            _valid_source_match(2),
        )


def _split_report(
    *,
    eligible: bool = True,
    future_40d: float = 1.0,
    trade_pnl_pct: float = 0.5,
    contract_ok: bool = True,
    ci_positive: bool = True,
) -> dict[str, object]:
    return {
        "candidate_report": {
            "gate": {
                "eligible_for_cross_split": eligible,
                "primary_bootstrap_contract_ok": contract_ok,
                "primary_cluster_bootstrap_ci95_positive": ci_positive,
            },
            "fixed_effect_cluster_bootstrap_low_minus_high": {
                "future_40d": {"fixed_effect_beta": future_40d},
                "trade_pnl_pct": {"fixed_effect_beta": trade_pnl_pct},
            },
        }
    }


def test_cross_split_gate_requires_all_positive_and_supported():
    reports = {split: _split_report() for split in audit.SPLITS}
    assert audit._cross_split_gate(reports)["pass"] is True
    reports["test"] = _split_report(trade_pnl_pct=-0.1)
    result = audit._cross_split_gate(reports)
    assert result["direction_consistent"] is False
    assert result["pass"] is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"label": "changed"}, "label"),
        ({"seed": 1}, "seed"),
        ({"bootstrap_reps": 100}, "bootstrap reps"),
        ({"splits": ["train"]}, "required splits"),
        ({"expected_script_sha256": "0" * 64}, "script SHA"),
    ],
)
def test_preregistered_args_fail_closed(override, message):
    with pytest.raises(RuntimeError, match=message):
        audit._validate_preregistered_args(_args(**override))


def test_output_directory_refuses_final_or_staging(tmp_path: Path):
    final = tmp_path / "formal"
    final.mkdir()
    with pytest.raises(RuntimeError, match="overwrite"):
        audit._assert_output_directory_unused(final)
    final.rmdir()
    Path(str(final) + ".tmp").mkdir()
    with pytest.raises(RuntimeError, match="overwrite"):
        audit._assert_output_directory_unused(final)


def test_atomic_writer_publishes_and_records_prepublish_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    final = tmp_path / "formal"
    staging = tmp_path / "formal.tmp"
    rows = {split: [{"candidate_id": split}] for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}
    initial = {"x": {"path": "x", "size_bytes": 1, "sha256": "a"}}
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: (value, []))
    monkeypatch.setattr(audit, "_snapshot_manifest", lambda value: "static")
    monkeypatch.setattr(audit, "_history_manifest", lambda value: "history")

    audit._write_outputs_atomically(
        final, staging, rows, result, initial, initial
    )

    assert final.is_dir()
    assert not staging.exists()
    assert result["pre_publish_input_stability"]["all_unchanged"] is True
    assert {path.name for path in final.iterdir()} == {
        "volatility_instability_risk_audit.json",
        "volatility_instability_risk_train.jsonl",
        "volatility_instability_risk_val.jsonl",
        "volatility_instability_risk_test.jsonl",
    }


def test_atomic_writer_keeps_staging_and_does_not_publish_on_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    final = tmp_path / "formal"
    staging = tmp_path / "formal.tmp"
    rows = {split: [{"candidate_id": split}] for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}
    initial = {"x": {"path": "x", "size_bytes": 1, "sha256": "a"}}
    calls = iter([(initial, [{"name": "x"}]), (initial, [])])
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: next(calls))

    with pytest.raises(RuntimeError, match="before publish"):
        audit._write_outputs_atomically(
            final, staging, rows, result, initial, initial
        )

    assert not final.exists()
    assert staging.is_dir()


def _install_mocked_run_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    change_schedule: list[list[dict[str, object]]] | None = None,
) -> dict[str, object]:
    calls: dict[str, object] = {"profiles": [], "seeds": [], "published": 0}
    sources_by_name = {
        f"candidates_{split}.jsonl": [
            _source(f"{index + 1:06d}", f"2026-04-0{index + 1}")
        ]
        for index, split in enumerate(audit.SPLITS)
    }
    schedule = iter(change_schedule or [[], [], [], []])
    monkeypatch.setattr(audit, "_validate_preregistered_args", lambda args: None)
    monkeypatch.setattr(
        audit,
        "_assert_output_directory_unused",
        lambda output: tmp_path / "formal.tmp",
    )

    def fake_snapshot(paths):
        return {
            name: {"path": str(path), "size_bytes": 1, "sha256": f"h-{name}"}
            for name, path in paths.items()
        }

    monkeypatch.setattr(audit, "_snapshot_named_paths", fake_snapshot)
    monkeypatch.setattr(
        audit,
        "_validate_expected_snapshot",
        lambda snapshot, expected: {"all": {"match": True}},
    )
    monkeypatch.setattr(audit, "load_config", lambda path: {"risk": {}})
    monkeypatch.setattr(
        audit,
        "load_jsonl",
        lambda path: [dict(row) for row in sources_by_name[Path(path).name]],
    )
    monkeypatch.setattr(
        audit,
        "_validate_integrity_manifest",
        lambda *args: {"all_pass": True},
    )
    monkeypatch.setattr(
        audit,
        "_history_manifest",
        lambda snapshot: audit.PREREGISTERED_HISTORY_MANIFEST_SHA256,
    )

    def fake_features(sources, cache, history_snapshot):
        return (
            {
                audit.candidate_id(row): {
                    "candidate_id": audit.candidate_id(row),
                    audit.PRIMARY_FACTOR: 0.1,
                    "factor_assignment_available": True,
                    "primary_low_volatility_instability_risk": True,
                }
                for row in sources
            },
            {"history_input_safe": True},
        )

    monkeypatch.setattr(audit, "_factor_features_for_sources", fake_features)

    def fake_changes(initial):
        return initial, next(schedule)

    monkeypatch.setattr(audit, "_snapshot_changes", fake_changes)

    def fake_replay(sources, config, profile, history_dir):
        assert profile == "production_risk"
        calls["profiles"].append(profile)
        replay = []
        for source in sources:
            row = {**source, "candidate_id": audit.candidate_id(source)}
            row.update({field: 1.0 for field in audit.FIELDS})
            replay.append(row)
        return replay, Counter(), _valid_source_match(len(sources))

    monkeypatch.setattr(audit, "_production_replay_split", fake_replay)

    def fake_report(rows, source_match, replay_integrity, factor_meta, seed):
        calls["seeds"].append(seed)
        return {
            "source_match": source_match,
            "gate": {
                "eligible_for_cross_split": True,
                "primary_bootstrap_contract_ok": True,
                "primary_cluster_bootstrap_ci95_positive": True,
            },
            "fixed_effect_cluster_bootstrap_low_minus_high": {
                "future_40d": {"fixed_effect_beta": 1.0},
                "trade_pnl_pct": {"fixed_effect_beta": 1.0},
            },
        }

    monkeypatch.setattr(audit, "_candidate_report", fake_report)

    def fake_publish(*args, **kwargs):
        calls["published"] = int(calls["published"]) + 1

    monkeypatch.setattr(audit, "_write_outputs_atomically", fake_publish)
    return calls


def test_mocked_run_uses_three_production_replays_and_one_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = _install_mocked_run_harness(monkeypatch, tmp_path)
    result = audit.run(_args(output_dir=str(tmp_path / "formal")))
    assert calls["profiles"] == ["production_risk"] * 3
    assert calls["seeds"] == [audit.PREREGISTERED_SEED] * 3
    assert calls["published"] == 1
    assert result["pre_replay_input_stability"]["all_unchanged"] is True
    assert result["protected_inputs"]["all_unchanged"] is True


def test_mocked_run_pre_replay_mutation_stops_before_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = _install_mocked_run_harness(
        monkeypatch,
        tmp_path,
        change_schedule=[[{"name": "config"}], []],
    )
    with pytest.raises(RuntimeError, match="changed before replay"):
        audit.run(_args(output_dir=str(tmp_path / "formal")))
    assert calls["profiles"] == []
    assert calls["published"] == 0


def test_mocked_run_post_replay_mutation_stops_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = _install_mocked_run_harness(
        monkeypatch,
        tmp_path,
        change_schedule=[[], [], [{"name": "config"}], []],
    )
    with pytest.raises(RuntimeError, match="changed during audit"):
        audit.run(_args(output_dir=str(tmp_path / "formal")))
    assert calls["profiles"] == ["production_risk"] * 3
    assert calls["published"] == 0
