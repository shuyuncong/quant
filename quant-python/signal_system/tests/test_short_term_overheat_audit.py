from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import short_term_overheat_audit as audit  # noqa: E402


def _frame(count: int = 30) -> pd.DataFrame:
    close = [100.0 + index for index in range(count)]
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2026-01-01", periods=count),
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
        "pre_entry_20d_return": value,
    }


def _split_report(
    *, eligible: bool, future_40d: float, trade_pnl_pct: float, ci_positive: bool
) -> dict[str, object]:
    low = 0.1 if ci_positive else -0.1
    return {
        "candidate_report": {
            "gate": {
                "eligible_for_cross_split": eligible,
                "primary_cluster_bootstrap_ci95_positive": ci_positive,
            },
            "cluster_bootstrap_cooler_minus_hotter": {
                "future_40d": {"mean_delta": future_40d, "ci95_low": low},
                "trade_pnl_pct": {"mean_delta": trade_pnl_pct, "ci95_low": low},
            },
        }
    }


def test_primary_factor_is_fixed_and_other_fields_are_diagnostic():
    assert audit.PRIMARY_FACTOR == "pre_entry_20d_return"
    assert audit.FACTOR_SPECS[audit.PRIMARY_FACTOR] == {
        "larger_is_better": False,
        "role": "primary",
    }
    for factor in (
        "pre_entry_5d_return",
        "distance_to_20d_high",
        "signal_day_gap",
    ):
        assert audit.FACTOR_SPECS[factor]["role"] == "diagnostic"


def test_overheat_features_follow_exact_preregistered_windows():
    frame = _frame()
    result = audit._overheat_features(frame, 29)
    assert result["factor_assignment_available"] is True
    assert result["pre_entry_20d_return"] == pytest.approx(129.0 / 109.0 - 1.0)
    assert result["pre_entry_5d_return"] == pytest.approx(129.0 / 124.0 - 1.0)
    assert result["distance_to_20d_high"] == pytest.approx(129.0 / 130.0 - 1.0)
    assert result["signal_day_gap"] == pytest.approx(129.0 / 128.0 - 1.0)
    assert result["twenty_day_high_includes_signal_day"] is True


def test_features_do_not_use_bars_after_signal_day():
    frame = _frame()
    expected = audit._overheat_features(frame, 29)
    later = _frame(3)
    later[["open", "high", "low", "close"]] = 10_000.0
    extended = pd.concat([frame, later], ignore_index=True)
    assert audit._overheat_features(extended, 29) == expected


def test_insufficient_20_session_return_history_is_rejected():
    result = audit._overheat_features(_frame(20), 19)
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "insufficient_20_session_return_history"


def test_invalid_primary_price_is_rejected():
    frame = _frame()
    frame.loc[9, "close"] = 0.0
    result = audit._overheat_features(frame, 29)
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "invalid_pre_entry_20d_return"


def test_same_day_halves_include_cooler_candidates_only():
    rows = [
        _row("000001", "2026-01-02", 0.10),
        _row("000002", "2026-01-02", 0.30),
        _row("000003", "2026-01-03", 0.20),
        _row("000004", "2026-01-03", 0.20),
        _row("000005", "2026-01-04", 0.05),
    ]
    audit._assign_primary_halves(rows)
    assert {row["symbol"] for row in rows if row["variant_included"]} == {
        "000001"
    }
    assert rows[0]["primary_cooler"] is True
    assert rows[1]["primary_cooler"] is False
    assert rows[2]["primary_cooler"] is None
    assert rows[3]["primary_cooler"] is None
    assert rows[4]["primary_cooler"] is None


def test_cross_split_gate_requires_consistent_positive_supported_direction():
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


def test_holdout_split_is_rejected_before_any_input_is_read():
    with pytest.raises(RuntimeError, match="cannot consume holdout"):
        audit.run(argparse.Namespace(splits=["holdout"]))

