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

import overnight_variance_share_risk_audit as audit  # noqa: E402


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


def _ohlc_frame(
    overnight_returns: np.ndarray,
    intraday_returns: np.ndarray,
    *,
    start: str = "2025-12-01",
    adjust: str = "qfq",
) -> pd.DataFrame:
    overnight = np.asarray(overnight_returns, dtype=float)
    intraday = np.asarray(intraday_returns, dtype=float)
    assert overnight.shape == intraday.shape
    assert np.all(overnight > -1.0)
    assert np.all(intraday > -1.0)
    opens = [100.0]
    closes = [100.0]
    for gap, move in zip(overnight, intraday):
        current_open = closes[-1] * (1.0 + gap)
        current_close = current_open * (1.0 + move)
        opens.append(float(current_open))
        closes.append(float(current_close))
    index = pd.bdate_range(start, periods=len(closes))
    open_values = np.asarray(opens, dtype=float)
    close_values = np.asarray(closes, dtype=float)
    margin = np.maximum(np.maximum(open_values, close_values) * 0.01, 0.01)
    frame = pd.DataFrame(
        {
            "datetime": index,
            "open": open_values,
            "high": np.maximum(open_values, close_values) + margin,
            "low": np.minimum(open_values, close_values) - margin,
            "close": close_values,
            "is_closed": [True] * len(index),
        }
    )
    assert (frame["low"] > 0).all()
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
        "primary_low_overnight_variance_share_risk": low,
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
    assert audit.PRIMARY_FACTOR == "overnight_variance_share_60"
    assert audit.MOVE_OBSERVATIONS == 60
    assert audit.HISTORY_BARS_REQUIRED == 61
    assert audit.PREREGISTERED_SEED == 20260906
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
    assert audit.FACTOR_SPECS["mean_overnight_gap_60"] == {
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


def test_history_frame_validation_accepts_strict_qfq_daily_ohlc():
    frame = _ohlc_frame(
        np.linspace(-0.01, 0.01, 60),
        np.linspace(0.02, -0.02, 60),
    )
    result = audit._validate_history_frame(frame, Path("history.pkl"))
    expected = frame.set_index("datetime")[["open", "high", "low", "close"]]
    pd.testing.assert_frame_equal(
        result, expected, check_freq=False, check_dtype=False
    )


def test_history_must_be_a_dataframe():
    with pytest.raises(RuntimeError, match="not a DataFrame"):
        audit._validate_history_frame(pd.Series([1.0]), Path("history.pkl"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda frame: frame.attrs.update({"adjust": "none"}), "adjust"),
        (lambda frame: frame.attrs.update({"adjust": "QFQ"}), "adjust"),
        (lambda frame: frame.attrs.update({"timeframe": "5m"}), "timeframe"),
        (lambda frame: frame.attrs.update({"timeframe": "1D"}), "timeframe"),
        (
            lambda frame: frame.__setitem__("open", [0.0] * len(frame)),
            "positive",
        ),
        (
            lambda frame: frame.__setitem__("is_closed", [1] * len(frame)),
            "non-bool",
        ),
        (
            lambda frame: frame.__setitem__(
                "high", ["1.0"] * len(frame)
            ),
            "non-numeric",
        ),
        (
            lambda frame: frame.__setitem__(
                "low", [True] * len(frame)
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
                "open", [math.inf] + [1.0] * (len(frame) - 1)
            ),
            "finite",
        ),
        (
            lambda frame: frame.__setitem__("high", frame["close"] - 0.01),
            "relationships",
        ),
        (
            lambda frame: frame.__setitem__("low", frame["close"] + 0.01),
            "relationships",
        ),
    ],
)
def test_history_frame_corruption_fails_closed(mutate, message):
    frame = _ohlc_frame(np.linspace(-0.01, 0.01, 60), np.zeros(60))
    mutate(frame)
    with pytest.raises(RuntimeError, match=message):
        audit._validate_history_frame(frame, Path("history.pkl"))


def test_history_duplicate_or_non_normalized_date_fails_closed():
    frame = _ohlc_frame(np.linspace(-0.01, 0.01, 60), np.zeros(60))
    duplicate = frame.copy()
    duplicate.attrs.update(frame.attrs)
    duplicate.loc[1, "datetime"] = duplicate.loc[0, "datetime"]
    with pytest.raises(RuntimeError, match="duplicate"):
        audit._validate_history_frame(duplicate, Path("history.pkl"))

    intraday = frame.copy()
    intraday.attrs.update(frame.attrs)
    intraday.loc[0, "datetime"] = intraday.loc[0, "datetime"] + pd.Timedelta(hours=1)
    with pytest.raises(RuntimeError, match="normalized"):
        audit._validate_history_frame(intraday, Path("history.pkl"))

    timezone = frame.copy()
    timezone.attrs.update(frame.attrs)
    timezone["datetime"] = timezone["datetime"].dt.tz_localize("Asia/Shanghai")
    with pytest.raises(RuntimeError, match="timezone-naive"):
        audit._validate_history_frame(timezone, Path("history.pkl"))


def test_history_validates_unused_and_unclosed_rows_before_filtering():
    frame = _ohlc_frame(np.linspace(-0.01, 0.01, 61), np.zeros(61))
    bad_close = frame.copy()
    bad_close.attrs.update(frame.attrs)
    bad_close.loc[len(bad_close) - 1, "is_closed"] = False
    bad_close["close"] = bad_close["close"].astype(object)
    bad_close.loc[len(bad_close) - 1, "close"] = "broken"
    with pytest.raises(RuntimeError, match="non-numeric"):
        audit._validate_history_frame(bad_close, Path("history.pkl"))

    bad_date = frame.copy()
    bad_date.attrs.update(frame.attrs)
    bad_date.loc[len(bad_date) - 1, "is_closed"] = False
    bad_date["datetime"] = bad_date["datetime"].astype(object)
    bad_date.loc[len(bad_date) - 1, "datetime"] = "not-a-date"
    with pytest.raises(RuntimeError, match="invalid datetime"):
        audit._validate_history_frame(bad_date, Path("history.pkl"))


def test_history_missing_column_or_descending_dates_fails_closed():
    frame = _ohlc_frame(np.linspace(-0.01, 0.01, 60), np.zeros(60))
    missing = frame.drop(columns=["open"])
    missing.attrs.update(frame.attrs)
    with pytest.raises(RuntimeError, match="missing columns"):
        audit._validate_history_frame(missing, Path("history.pkl"))

    descending = frame.iloc[::-1].reset_index(drop=True)
    descending.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    with pytest.raises(RuntimeError, match="strictly increasing"):
        audit._validate_history_frame(descending, Path("history.pkl"))


def test_primary_and_diagnostics_match_manual_gap_and_intraday_formulas():
    overnight = np.asarray([-0.04, -0.02, 0.0, 0.01, 0.03] * 12)
    intraday = np.asarray([0.02, -0.01, 0.03, 0.0, -0.02] * 12)
    frame = _ohlc_frame(overnight, intraday)
    day = frame.iloc[-1]["datetime"].date().isoformat()

    result = audit._overnight_variance_share_features(
        frame.set_index("datetime")[["open", "high", "low", "close"]],
        day,
    )

    assert result is not None
    overnight_sum = float(np.sum(np.square(overnight)))
    intraday_sum = float(np.sum(np.square(intraday)))
    assert result[audit.PRIMARY_FACTOR] == pytest.approx(
        overnight_sum / (overnight_sum + intraday_sum)
    )
    assert result["overnight_rms_60"] == pytest.approx(
        np.sqrt(np.mean(np.square(overnight)))
    )
    assert result["intraday_rms_60"] == pytest.approx(
        np.sqrt(np.mean(np.square(intraday)))
    )
    assert result["mean_absolute_overnight_gap_60"] == pytest.approx(
        np.mean(np.abs(overnight))
    )
    assert result["negative_overnight_gap_fraction_60"] == pytest.approx(
        np.mean(overnight < 0.0)
    )
    assert result["mean_overnight_gap_60"] == pytest.approx(
        np.mean(overnight)
    )
    assert result["overnight_squared_move_sum"] == pytest.approx(overnight_sum)
    assert result["intraday_squared_move_sum"] == pytest.approx(intraday_sum)
    assert result["move_observations"] == 60
    assert result["history_bars_required"] == 61


def test_open_is_current_bar_and_gap_denominator_is_prior_close():
    overnight = np.zeros(60)
    intraday = np.zeros(60)
    overnight[-1] = 0.20
    intraday[-1] = -0.10
    frame = _ohlc_frame(overnight, intraday)
    indexed = frame.set_index("datetime")[["open", "high", "low", "close"]]
    day = indexed.index[-1].date().isoformat()
    result = audit._overnight_variance_share_features(indexed, day)
    assert result is not None
    assert result["overnight_squared_move_sum"] == pytest.approx(0.20**2)
    assert result["intraday_squared_move_sum"] == pytest.approx(0.10**2)
    assert result[audit.PRIMARY_FACTOR] == pytest.approx(0.8)


def test_exactly_sixty_moves_include_signal_day_and_drop_older_move():
    overnight = np.r_[0.75, np.zeros(59), 0.25]
    intraday = np.r_[0.50, np.full(59, 0.01), 0.0]
    frame = _ohlc_frame(overnight, intraday)
    indexed = frame.set_index("datetime")[["open", "high", "low", "close"]]
    day = indexed.index[-1].date().isoformat()
    result = audit._overnight_variance_share_features(indexed, day)
    assert result is not None
    assert result["overnight_squared_move_sum"] == pytest.approx(0.25**2)
    assert result["intraday_squared_move_sum"] == pytest.approx(59 * 0.01**2)


def test_zero_total_squared_move_is_unavailable():
    frame = _ohlc_frame(np.zeros(60), np.zeros(60))
    indexed = frame.set_index("datetime")[["open", "high", "low", "close"]]
    assert audit._overnight_variance_share_features(
        indexed, indexed.index[-1].date().isoformat()
    ) is None


def test_pure_overnight_and_pure_intraday_boundaries_are_available():
    overnight_only = _ohlc_frame(np.full(60, 0.01), np.zeros(60))
    intraday_only = _ohlc_frame(np.zeros(60), np.full(60, 0.01))
    overnight_indexed = overnight_only.set_index("datetime")[["open", "high", "low", "close"]]
    intraday_indexed = intraday_only.set_index("datetime")[["open", "high", "low", "close"]]
    overnight_result = audit._overnight_variance_share_features(
        overnight_indexed, overnight_indexed.index[-1].date().isoformat()
    )
    intraday_result = audit._overnight_variance_share_features(
        intraday_indexed, intraday_indexed.index[-1].date().isoformat()
    )
    assert overnight_result is not None
    assert intraday_result is not None
    assert overnight_result[audit.PRIMARY_FACTOR] == pytest.approx(1.0)
    assert intraday_result[audit.PRIMARY_FACTOR] == pytest.approx(0.0)


def test_tiny_positive_denominator_is_not_floored():
    overnight = np.full(60, 2.0**-30)
    intraday = np.full(60, 2.0**-31)
    frame = _ohlc_frame(overnight, intraday)
    indexed = frame.set_index("datetime")[["open", "high", "low", "close"]]
    result = audit._overnight_variance_share_features(
        indexed, indexed.index[-1].date().isoformat()
    )
    assert result is not None
    assert 0.0 < (
        result["overnight_squared_move_sum"]
        + result["intraday_squared_move_sum"]
    ) < 1e-15
    assert result[audit.PRIMARY_FACTOR] == pytest.approx(0.8, rel=1e-6)


def test_features_are_uniform_scale_invariant_and_ignore_post_signal_suffix():
    overnight = np.sin(np.arange(80) / 3.0) * 0.02
    intraday = np.cos(np.arange(80) / 5.0) * 0.015
    frame = _ohlc_frame(overnight, intraday)
    indexed = frame.set_index("datetime")[["open", "high", "low", "close"]]
    day = indexed.index[65].date().isoformat()
    expected = audit._overnight_variance_share_features(indexed, day)
    scaled = indexed * 8.0
    modified = indexed.copy()
    modified.loc[modified.index > pd.Timestamp(day), ["open", "high", "low", "close"]] *= 9.0
    assert audit._overnight_variance_share_features(scaled, day) == pytest.approx(expected)
    assert audit._overnight_variance_share_features(modified, day) == pytest.approx(expected)


def test_features_are_unavailable_with_only_sixty_bars():
    frame = _ohlc_frame(np.zeros(59), np.full(59, 0.01))
    indexed = frame.set_index("datetime")[["open", "high", "low", "close"]]
    assert audit._overnight_variance_share_features(
        indexed, indexed.index[-1].date().isoformat()
    ) is None


def _feature_cache(overnight_levels: dict[str, float]) -> tuple[
    list[dict[str, object]],
    dict[str, pd.DataFrame],
    dict[str, dict[str, object]],
]:
    day = pd.bdate_range("2025-12-01", periods=61)[-1].date().isoformat()
    sources = [_source(symbol, day) for symbol in overnight_levels]
    cache = {
        symbol: _ohlc_frame(
            np.full(60, level),
            np.full(60, 0.01),
        ).set_index("datetime")[["open", "high", "low", "close"]]
        for symbol, level in overnight_levels.items()
    }
    snapshot = {
        f"qfq:{symbol}": {
            "path": f"{symbol}.pkl",
            "size_bytes": 1,
            "sha256": symbol,
        }
        for symbol in overnight_levels
    }
    return sources, cache, snapshot


def test_factor_features_compute_share_and_same_day_groups():
    sources, cache, snapshot = _feature_cache(
        {
            "000001": 0.001,
            "000002": 0.002,
            "000003": 0.004,
            "000004": 0.008,
        }
    )
    features, meta = audit._factor_features_for_sources(
        sources, cache, snapshot
    )
    assert list(features) == [audit.candidate_id(row) for row in sources]
    assert all(item["factor_assignment_available"] for item in features.values())
    assert sum(
        item["primary_low_overnight_variance_share_risk"] is True
        for item in features.values()
    ) == 2
    assert sum(
        item["primary_low_overnight_variance_share_risk"] is False
        for item in features.values()
    ) == 2
    assert meta["move_observations"] == 60
    assert meta["history_bars_required"] == 61
    assert meta["overnight_return_definition"] == "open_t_div_prior_close_minus_1"
    assert meta["intraday_return_definition"] == "close_t_div_open_t_minus_1"
    assert meta["exact_variance_decomposition_claimed"] is False
    assert meta["total_squared_move_floor_used"] is False
    assert meta["qfq_uniform_scale_invariant"] is True
    assert meta["time_varying_adjustment_invariant"] is False
    assert meta["primary_unavailable_candidates"] == 0


def test_factor_features_ignore_source_legacy_outcomes():
    sources, cache, snapshot = _feature_cache(
        {
            "000001": 0.001,
            "000002": 0.002,
            "000003": 0.004,
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
            "000001": 0.001,
            "000002": 0.001,
            "000003": 0.001,
        }
    )
    features, meta = audit._factor_features_for_sources(
        sources, cache, snapshot
    )
    assert all(
        item["factor_assignment_available"] for item in features.values()
    )
    assert all(
        item["primary_low_overnight_variance_share_risk"] is None
        for item in features.values()
    )
    assert meta["excluded_single_group_signal_days"] == 1


def test_zero_total_squared_move_candidate_is_unavailable_and_counted():
    sources, cache, snapshot = _feature_cache(
        {
            "000001": 0.0,
            "000002": 0.002,
            "000003": 0.004,
        }
    )
    cache["000001"] = _ohlc_frame(
        np.zeros(60), np.zeros(60)
    ).set_index("datetime")[["open", "high", "low", "close"]]
    features, meta = audit._factor_features_for_sources(
        sources, cache, snapshot
    )
    first = features[audit.candidate_id(sources[0])]
    assert first["factor_assignment_available"] is False
    assert first[audit.PRIMARY_FACTOR] is None
    assert meta["zero_total_squared_move_candidates"] == 1
    assert meta["insufficient_history_candidates"] == 0


def test_insufficient_history_is_unavailable_but_missing_signal_day_fails():
    day = pd.bdate_range("2025-12-01", periods=60)[-1].date().isoformat()
    sources = [_source("000001", day), _source("000002", day)]
    cache = {
        "000001": _ohlc_frame(
            np.zeros(59), np.full(59, 0.01)
        ).set_index("datetime")[["open", "high", "low", "close"]],
        "000002": _ohlc_frame(
            np.zeros(59), np.full(59, 0.02)
        ).set_index("datetime")[["open", "high", "low", "close"]],
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


def test_mean_overnight_gap_is_unoriented_and_has_no_directional_reports():
    rows = [
        {
            **_row("000001", "2026-04-01", True, 1.0),
            "mean_overnight_gap_60": 0.09,
        },
        {
            **_row("000002", "2026-04-01", False, -1.0),
            "mean_overnight_gap_60": 0.01,
        },
    ]
    report = audit._factor_report(
        rows,
        "mean_overnight_gap_60",
        audit.FACTOR_SPECS["mean_overnight_gap_60"][
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
        ({"input_dir": "D:/tmp/other"}, "input dir"),
        ({"output_dir": "D:/tmp/other"}, "output dir"),
        ({"config": "D:/tmp/other.yaml"}, "config path"),
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
        "overnight_variance_share_risk_audit.json",
        "overnight_variance_share_risk_train.jsonl",
        "overnight_variance_share_risk_val.jsonl",
        "overnight_variance_share_risk_test.jsonl",
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
                    "primary_low_overnight_variance_share_risk": True,
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
