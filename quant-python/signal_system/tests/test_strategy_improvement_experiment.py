import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_improvement_experiment import (  # noqa: E402
    META_FEATURE_NAMES,
    META_OUTCOME_FIELDS,
    _guard_development_path,
    _load_baseline_splits,
    align_equity_to_benchmark,
    apply_preprocessor,
    extract_meta_feature_dict,
    fit_logistic_regression,
    fit_preprocessor,
    latch_risk_off,
    meta_training_plan,
    predict_logistic,
    value_filter_rows,
)


def _candidate(symbol: str, pnl: float = 1.0) -> dict:
    return {
        "symbol": symbol,
        "signal_day": "2025-01-10",
        "signal_type": "macd_golden_cross_pullback_confirmed_above",
        "entry_day": "2025-01-13",
        "exit_day": "2025-01-20",
        "entry_price": 10.0,
        "exit_price": 10.2,
        "exit_session": "open",
        "holding_days": 7,
        "pnl_pct": pnl,
        "trade_pnl_pct": pnl,
        "regime": "bull",
        "_mark_prices": {"2025-01-13": 10.0, "2025-01-20": 10.2},
    }


def _fundamental(symbol: str, pe: float, market_cap: float) -> dict:
    return {
        "symbol": symbol,
        "signal_day": "2025-01-10",
        "signal_type": "macd_golden_cross_pullback_confirmed_above",
        "ann_date": "2024-12-20",
        "ann_date_estimated": False,
        "period": "20240930",
        "pe": pe,
        "market_cap": market_cap,
    }


def _history(rows: int = 300) -> pd.DataFrame:
    close = np.linspace(10.0, 15.0, rows)
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2023-01-02", periods=rows),
            "open": close - 0.05,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": np.linspace(1000.0, 1500.0, rows),
            "is_closed": True,
        }
    )


def test_holdout_paths_are_blocked_by_default(tmp_path):
    with pytest.raises(ValueError, match="Holdout path is blocked"):
        _guard_development_path(tmp_path / "blind_holdout" / "candidates.jsonl", False)
    resolved = _guard_development_path(tmp_path / "blind_holdout", True)
    assert resolved.name == "blind_holdout"


def test_unified_runner_rejects_duplicate_baseline_candidate_ids(tmp_path):
    duplicate = _candidate("000001")
    for split in ("train", "val", "test"):
        rows = [duplicate, dict(duplicate)] if split == "train" else [_candidate(split)]
        (tmp_path / f"candidates_{split}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="duplicate candidate ids"):
        _load_baseline_splits(tmp_path, allow_holdout=False)


def test_risk_off_latch_recovers_only_after_three_consecutive_sessions():
    state = latch_risk_off(
        [False, True, False, False, False, False, False, False],
        [False, False, True, True, False, True, True, True],
    )
    assert state == [False, True, True, True, True, True, True, False]


def test_benchmark_alignment_adds_anchor_and_forward_fills():
    index = pd.DataFrame(
        {
            "datetime": pd.bdate_range("2025-01-06", periods=4),
            "open": [100, 101, 102, 103],
            "high": [101, 102, 103, 104],
            "low": [99, 100, 101, 102],
            "close": [100, 101, 102, 103],
            "is_closed": True,
        }
    )
    curve = [
        {"day": "2025-01-07", "cash": 50.0, "market_value": 51.0, "equity": 101.0},
        {"day": "2025-01-09", "cash": 102.0, "market_value": 0.0, "equity": 102.0},
    ]
    aligned = align_equity_to_benchmark(curve, index, 100.0)
    assert [row["day"] for row in aligned] == [
        "2025-01-06",
        "2025-01-07",
        "2025-01-08",
        "2025-01-09",
    ]
    assert aligned[0]["equity"] == 100.0
    assert aligned[2]["equity"] == 101.0
    assert aligned[2]["cash"] == 50.0


def test_value_filter_separates_nonpositive_pe_and_ranks_by_signal_day():
    baseline = [_candidate(symbol, pnl=float(index - 2)) for index, symbol in enumerate("abcde")]
    fundamentals = [
        _fundamental("a", 40.0, 10.0),
        _fundamental("b", 20.0, 20.0),
        _fundamental("c", 10.0, 30.0),
        _fundamental("d", 5.0, 40.0),
        _fundamental("e", -5.0, 50.0),
    ]
    eligible, selected, audit = value_filter_rows(baseline, fundamentals)
    assert len(eligible) == 5
    assert {_candidate["symbol"] for _candidate in selected} == {"c", "d"}
    assert audit["nonpositive_pe_candidates"] == 1
    assert audit["positive_pe_candidates"] == 4
    assert audit["daily_positive_pe_group_sizes"] == {4: 1}


def test_meta_feature_schema_excludes_outcomes_and_ignores_their_values():
    candidate = _candidate("000001")
    candidate["market_cap"] = 100.0
    history = _history()
    index_features = {
        "index_return_5d": 0.01,
        "index_ma20_distance": 0.02,
        "index_downside_volatility_20d": 0.01,
        "index_risk_off": 0.0,
    }
    first = extract_meta_feature_dict(candidate, history, index_features)
    changed = dict(candidate)
    for field in META_OUTCOME_FIELDS:
        changed[field] = 999999
    second = extract_meta_feature_dict(changed, history, index_features)
    assert set(META_FEATURE_NAMES).isdisjoint(META_OUTCOME_FIELDS)
    assert first == second


def test_meta_training_plan_never_fits_the_evaluation_split():
    plan = meta_training_plan()
    assert plan["validation"] == {"fit_splits": ("train",), "evaluate_split": "val"}
    assert plan["viewed_test"] == {
        "fit_splits": ("train", "val"),
        "evaluate_split": "test",
    }
    for stage in plan.values():
        assert stage["evaluate_split"] not in stage["fit_splits"]


def test_numpy_logistic_model_is_deterministic():
    matrix = np.asarray(
        [[-2.0, 0.0], [-1.0, 0.5], [0.0, -0.5], [1.0, 0.5], [2.0, 1.0]],
        dtype=float,
    )
    labels = np.asarray([0.0, 0.0, 0.0, 1.0, 1.0])
    state = fit_preprocessor(matrix)
    transformed = apply_preprocessor(matrix, state)
    first = fit_logistic_regression(transformed, labels)
    second = fit_logistic_regression(transformed, labels)
    assert first["intercept"] == second["intercept"]
    np.testing.assert_array_equal(first["coefficients"], second["coefficients"])
    np.testing.assert_array_equal(
        predict_logistic(first, transformed), predict_logistic(second, transformed)
    )
