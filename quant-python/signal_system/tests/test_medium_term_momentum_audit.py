from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import medium_term_momentum_audit as audit  # noqa: E402


def _frame(count: int = 80) -> pd.DataFrame:
    close = [100.0 + index for index in range(count)]
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2025-01-01", periods=count),
            "open": close,
            "high": [value + 1.0 for value in close],
            "low": [value - 1.0 for value in close],
            "close": close,
            "volume": [1000.0] * count,
        }
    )


def _row(symbol: str, day: str, value: float) -> dict[str, object]:
    return {
        "candidate_id": f"{symbol}|{day}|macd_above",
        "symbol": symbol,
        "signal_day": day,
        "signal_type": "macd_above",
        "pre_entry_60d_return": value,
    }


def _split_report(
    *, eligible: bool, future_40d: float, trade_pnl_pct: float, ci_positive: bool
) -> dict[str, object]:
    return {
        "candidate_report": {
            "gate": {
                "eligible_for_cross_split": eligible,
                "primary_cluster_bootstrap_ci95_positive": ci_positive,
            },
            "cluster_bootstrap_high_minus_low": {
                "future_40d": {"mean_delta": future_40d},
                "trade_pnl_pct": {"mean_delta": trade_pnl_pct},
            },
        }
    }


def test_primary_factor_is_fixed_and_diagnostics_cannot_gate():
    assert audit.PRIMARY_FACTOR == "pre_entry_60d_return"
    assert audit.FACTOR_SPECS[audit.PRIMARY_FACTOR] == {
        "larger_is_better": True,
        "role": "primary",
    }
    for factor in (
        "pre_entry_20d_return",
        "momentum_acceleration_20_vs_60",
        "positive_return_ratio_60",
    ):
        assert audit.FACTOR_SPECS[factor]["role"] == "diagnostic"


def test_momentum_features_follow_exact_preregistered_formulas():
    frame = _frame()
    result = audit._momentum_features(frame, 79)
    expected_60 = 179.0 / 119.0 - 1.0
    expected_20 = 179.0 / 159.0 - 1.0
    expected_acceleration = math.log(179.0 / 159.0) / 20.0 - math.log(
        179.0 / 119.0
    ) / 60.0
    assert result["factor_assignment_available"] is True
    assert result["pre_entry_60d_return"] == pytest.approx(expected_60)
    assert result["pre_entry_20d_return"] == pytest.approx(expected_20)
    assert result["momentum_acceleration_20_vs_60"] == pytest.approx(
        expected_acceleration
    )
    assert result["positive_return_ratio_60"] == 1.0


def test_features_do_not_use_bars_after_signal_day():
    frame = _frame()
    expected = audit._momentum_features(frame, 79)
    later = _frame(3)
    later[["open", "high", "low", "close"]] = 10_000.0
    extended = pd.concat([frame, later], ignore_index=True)
    assert audit._momentum_features(extended, 79) == expected


def test_sixty_session_history_is_required():
    result = audit._momentum_features(_frame(60), 59)
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "insufficient_60_session_return_history"


def test_invalid_sixty_day_base_price_is_rejected():
    frame = _frame()
    frame.loc[19, "close"] = 0.0
    result = audit._momentum_features(frame, 79)
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "invalid_pre_entry_60d_return"


def test_same_day_halves_include_high_momentum_candidates_only():
    rows = [
        _row("000001", "2026-01-02", 0.10),
        _row("000002", "2026-01-02", 0.30),
        _row("000003", "2026-01-03", 0.20),
        _row("000004", "2026-01-03", 0.20),
        _row("000005", "2026-01-04", 0.50),
    ]
    audit._assign_primary_halves(rows)
    assert {row["symbol"] for row in rows if row["variant_included"]} == {
        "000002"
    }
    assert rows[0]["primary_high_momentum"] is False
    assert rows[1]["primary_high_momentum"] is True
    assert rows[2]["primary_high_momentum"] is None
    assert rows[3]["primary_high_momentum"] is None
    assert rows[4]["primary_high_momentum"] is None


def test_cross_split_gate_requires_all_eligible_splits_to_be_positive():
    reports = {
        "train": _split_report(
            eligible=True, future_40d=1.0, trade_pnl_pct=0.5, ci_positive=True
        ),
        "val": _split_report(
            eligible=True, future_40d=0.8, trade_pnl_pct=0.2, ci_positive=True
        ),
        "test": _split_report(
            eligible=False, future_40d=-9.0, trade_pnl_pct=-9.0, ci_positive=False
        ),
    }
    assert audit._cross_split_gate(reports)["pass"] is True
    reports["val"] = _split_report(
        eligible=True, future_40d=-0.1, trade_pnl_pct=0.2, ci_positive=True
    )
    assert audit._cross_split_gate(reports)["pass"] is False


def test_holdout_split_is_rejected_before_input_access():
    with pytest.raises(RuntimeError, match="cannot consume holdout"):
        audit.run(argparse.Namespace(splits=["holdout"]))

