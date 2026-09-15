import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_pairwise_ranking_experiment import (  # noqa: E402
    MODEL_FEATURE_NAMES,
    _guard_development_path,
    _research_screen,
    _score_rows,
    _technical_features_at_signal_day,
    build_cross_sectional_feature_map,
    build_pairwise_training_data,
    extract_ranking_raw_features,
    fit_pairwise_logistic,
)


def _row(
    candidate_id: str,
    day: str,
    signal_type: str,
    atr_ratio: float,
    outcome: float,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "symbol": candidate_id,
        "signal_day": day,
        "entry_day": day,
        "signal_type": signal_type,
        "confirmation_count": 2,
        "_p5a_features": {"dif_dea_gap": 0.01, "zero_dist": 0.02},
        "_p5b_features": {
            "ma60_dist": 0.1,
            "ma60_slope": 0.01,
            "ma250_dist": 0.2,
            "ma250_slope": 0.005,
            "atr_ratio": atr_ratio,
            "recent_return": 0.03,
        },
        "trade_pnl_pct": outcome,
    }


def test_guard_blocks_holdout_paths_by_default(tmp_path):
    path = tmp_path / "reserved_holdout" / "report.json"
    with pytest.raises(ValueError, match="Holdout path is blocked"):
        _guard_development_path(path)
    assert _guard_development_path(path, True).name == "report.json"


def test_feature_extractor_does_not_read_outcomes():
    first = _row("a", "2025-01-02", "buy_1", 0.02, -10.0)
    second = dict(first)
    second.update(
        {
            "exit_day": "2099-01-01",
            "exit_reason": "future_magic",
            "trade_pnl_pct": 999.0,
            "mfe": 999.0,
            "future_20d": 999.0,
        }
    )
    assert extract_ranking_raw_features(first) == extract_ranking_raw_features(second)


def test_history_features_ignore_bars_after_signal_day():
    days = pd.bdate_range("2024-01-02", periods=300)
    close = np.linspace(10.0, 20.0, 300)
    history = pd.DataFrame(
        {
            "datetime": days,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
        }
    )
    signal_index = 279
    signal_day = str(days[signal_index].date())
    causal = history.iloc[: signal_index + 1].copy()
    changed_future = history.copy()
    changed_future.loc[signal_index + 1 :, ["open", "high", "low", "close"]] = 9999.0

    expected = _technical_features_at_signal_day(causal, signal_day)
    actual = _technical_features_at_signal_day(changed_future, signal_day)

    assert actual == expected


def test_pairwise_model_learns_within_bucket_feature_direction():
    rows = []
    for index in range(3):
        day = f"2025-01-0{index + 2}"
        rows.extend(
            [
                _row(f"low-{index}", day, "buy_1", 0.01, -2.0),
                _row(f"high-{index}", day, "buy_1", 0.05, 3.0),
            ]
        )
    feature_map, _ = build_cross_sectional_feature_map(rows)
    matrix, labels, sample_weights, audit = build_pairwise_training_data(
        rows, feature_map
    )
    model = fit_pairwise_logistic(matrix, labels, sample_weights)
    atr_index = list(MODEL_FEATURE_NAMES).index("rank_atr_ratio")
    assert audit["entry_days_with_pairs"] == 3
    assert model["weights"][atr_index] > 0


def test_scoring_never_crosses_frozen_p0_priority():
    rows = [
        _row(
            "above",
            "2025-01-02",
            "macd_golden_cross_pullback_confirmed_above",
            0.01,
            0.0,
        ),
        _row(
            "near-low",
            "2025-01-02",
            "macd_golden_cross_pullback_confirmed_near",
            0.01,
            0.0,
        ),
        _row(
            "near-high",
            "2025-01-02",
            "macd_golden_cross_pullback_confirmed_near",
            0.10,
            0.0,
        ),
    ]
    weights = np.zeros(len(MODEL_FEATURE_NAMES), dtype=float)
    weights[list(MODEL_FEATURE_NAMES).index("rank_atr_ratio")] = 10000.0
    model = {
        "weights": weights,
        "l2": 1.0,
        "iterations": 1,
        "converged": True,
        "objective_weight_sum": 1.0,
    }
    scored, _, _ = _score_rows(rows, model)
    scores = {row["candidate_id"]: row["_portfolio_rank_score"] for row in scored}
    assert scores["above"] > scores["near-high"]
    assert scores["near-high"] > scores["near-low"]


def test_research_screen_uses_only_walk_forward_and_validation():
    walk_forward = {
        "model_evaluated_days": 5,
        "pairwise_metrics_on_model_evaluated_days": {
            "day_weighted_pairwise_accuracy": 0.51
        },
    }
    validation = {
        "evaluation": {
            "pairwise_metrics": {"day_weighted_pairwise_accuracy": 0.52}
        },
        "portfolio_comparison": {
            "pairwise_ranked": {
                "summary": {"total_return_pct": 12.0, "max_drawdown_pct": 9.0}
            },
            "random_order_reference": {
                "total_return_pct": {"median": 10.0},
                "max_drawdown_pct": {"median": 10.0},
            },
            "ranked_return_random_order_percentile": 75.0,
        },
    }
    result = _research_screen(walk_forward, validation)
    assert result["passes_research_screen"] is True
    assert result["viewed_test_used"] is False
    assert result["production_eligible"] is False
