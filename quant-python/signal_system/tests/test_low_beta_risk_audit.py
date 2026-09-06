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

import low_beta_risk_audit as audit  # noqa: E402


def _prices_from_returns(start: float, returns: list[float]) -> list[float]:
    values = [start]
    for value in returns:
        values.append(values[-1] * (1.0 + value))
    return values


def _stock_frame(
    days: pd.DatetimeIndex,
    closes: list[float],
    *,
    adjust: str = "qfq",
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "datetime": days,
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(days),
            "amount": [100000.0] * len(days),
            "is_closed": [True] * len(days),
        }
    )
    frame.attrs.update({"adjust": adjust, "timeframe": "1d"})
    return frame


def _index_frame(
    days: pd.DatetimeIndex,
    closes: list[float],
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "datetime": days,
            "close": closes,
            "is_closed": [True] * len(days),
        }
    )
    frame.attrs.update({"adjust": "none", "timeframe": "1d"})
    return frame


def _source(
    symbol: str = "000001",
    day: str = "2026-03-30",
    regime: str = "bull",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "signal_day": day,
        "signal_type": "macd_above",
        "regime": regime,
    }


def _row(symbol: str, day: str, beta: float, regime: str = "bull") -> dict[str, object]:
    return {
        **_source(symbol, day, regime),
        "candidate_id": f"{symbol}|{day}|macd_above",
        "beta60": beta,
        "market_correlation_60": beta,
        "downside_beta_60": beta,
        "idiosyncratic_volatility_60": abs(beta),
        "factor_assignment_available": True,
        "future_20d": -beta,
        "future_40d": -beta,
        "trade_pnl_pct": -beta,
        "normalized_regime": regime,
    }


def _valid_source_match(count: int) -> dict[str, object]:
    return {
        "source_rows": count,
        "eligible_rows": count,
        "replayed_rows": count,
        "policy_skips": {},
        "baseline_replay_complete": True,
        "entry_signal_policy_validated": True,
    }


def _valid_factor_meta() -> dict[str, object]:
    return {
        "history_manifest_sha256": "history-hash",
        "history_file_count": 20,
        "history_symbol_count": 10,
        "history_input_safe": True,
        "history_input_failures": [],
        "replay_history_coverage_safe": True,
        "replay_history_coverage_failures": [],
        "qfq_none_window_consistency_safe": True,
        "qfq_none_window_consistency_failures": [],
        "factor_errors": {},
        "diagnostic_unavailable_counts": {
            "market_correlation_60": 0,
            "downside_beta_60": 0,
            "idiosyncratic_volatility_60": 0,
        },
    }


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
            "cluster_bootstrap_low_minus_high": {
                "future_40d": {"mean_delta": future_40d},
                "trade_pnl_pct": {"mean_delta": trade_pnl_pct},
            },
        }
    }


def _args(input_dir: Path, output_dir: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "config": str(audit.BASE_DIR / "config" / "config.yaml"),
        "index_data": str(audit.DEFAULT_INDEX_DATA),
        "label": audit.PREREGISTERED_LABEL,
        "splits": list(audit.SPLITS),
        "seed": audit.PREREGISTERED_SEED,
        "bootstrap_reps": audit.PREREGISTERED_BOOTSTRAP_REPS,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _synthetic_pair(
    market_returns: list[float],
    stock_returns: list[float],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    days = pd.bdate_range("2026-01-02", periods=61)
    index = _index_frame(days, _prices_from_returns(100.0, market_returns))
    qfq = _stock_frame(days, _prices_from_returns(50.0, stock_returns))
    none = _stock_frame(
        days,
        _prices_from_returns(80.0, stock_returns),
        adjust="none",
    )
    return qfq, none, index, days[-1].date().isoformat()


def test_primary_factor_and_all_diagnostics_are_fixed():
    assert audit.PRIMARY_FACTOR == "beta60"
    assert audit.FACTOR_SPECS["beta60"] == {
        "larger_is_better": False,
        "role": "primary",
    }
    for factor in (
        "market_correlation_60",
        "downside_beta_60",
        "idiosyncratic_volatility_60",
    ):
        assert audit.FACTOR_SPECS[factor]["role"] == "diagnostic"
    assert audit.BETA_RETURN_OBSERVATIONS == 60
    assert audit.MIN_ACTUAL_STOCK_SESSIONS == 50


def test_beta60_uses_exact_arithmetic_return_ols_slope():
    market_returns = [(-1) ** index * (0.002 + index * 0.0001) for index in range(60)]
    stock_returns = [0.001 + 1.75 * value for value in market_returns]
    qfq, none, index, signal_day = _synthetic_pair(market_returns, stock_returns)

    result, failure = audit._beta_features(
        qfq, none, index, pd.Timestamp(signal_day).date()
    )

    assert failure is None
    assert result["factor_assignment_available"] is True
    assert result["beta60"] == pytest.approx(1.75)
    assert result["market_correlation_60"] == pytest.approx(1.0)
    assert result["idiosyncratic_volatility_60"] == pytest.approx(0.0, abs=1e-14)
    assert result["actual_stock_sessions"] == 61
    assert result["forward_filled_stock_sessions"] == 0


def test_correlation_and_residual_volatility_match_nonperfect_manual_values():
    market_returns = [(-1) ** index * (0.002 + index * 0.0001) for index in range(60)]
    noise = [((index % 5) - 2) * 0.0007 for index in range(60)]
    stock_returns = [
        0.001 + 1.4 * market + residual
        for market, residual in zip(market_returns, noise)
    ]
    qfq, none, index, signal_day = _synthetic_pair(market_returns, stock_returns)

    result, _ = audit._beta_features(qfq, none, index, pd.Timestamp(signal_day).date())

    market = np.asarray(market_returns, dtype=float)
    stock = np.asarray(stock_returns, dtype=float)
    expected_beta = float(
        np.dot(market - market.mean(), stock - stock.mean())
        / np.dot(market - market.mean(), market - market.mean())
    )
    expected_alpha = float(stock.mean() - expected_beta * market.mean())
    expected_residuals = stock - (expected_alpha + expected_beta * market)
    assert result["beta60"] == pytest.approx(expected_beta)
    assert result["market_correlation_60"] == pytest.approx(
        float(np.corrcoef(stock, market)[0, 1])
    )
    assert result["idiosyncratic_volatility_60"] == pytest.approx(
        float(np.std(expected_residuals, ddof=1))
    )


def test_downside_beta_recomputes_intercept_ols_on_negative_market_days():
    market_returns = [(-0.01 if index % 2 == 0 else 0.008) for index in range(60)]
    stock_returns = [0.003 + 2.25 * value for value in market_returns]
    qfq, none, index, signal_day = _synthetic_pair(market_returns, stock_returns)

    result, _ = audit._beta_features(qfq, none, index, pd.Timestamp(signal_day).date())

    assert result["downside_observations"] == 30
    assert result["downside_beta_60"] is None

    varied_market = [-(0.002 + index * 0.0001) if index % 2 == 0 else 0.008 for index in range(60)]
    varied_stock = [0.003 + 2.25 * value for value in varied_market]
    qfq, none, index, signal_day = _synthetic_pair(varied_market, varied_stock)
    result, _ = audit._beta_features(qfq, none, index, pd.Timestamp(signal_day).date())
    assert result["downside_beta_60"] == pytest.approx(2.25)


def test_downside_beta_is_unavailable_below_ten_negative_market_days():
    market_returns = [-0.01] * 9 + [0.005 + index * 0.0001 for index in range(51)]
    stock_returns = [0.001 + value for value in market_returns]
    qfq, none, index, signal_day = _synthetic_pair(market_returns, stock_returns)
    result, _ = audit._beta_features(qfq, none, index, pd.Timestamp(signal_day).date())
    assert result["factor_assignment_available"] is True
    assert result["downside_observations"] == 9
    assert result["downside_beta_60"] is None


def test_t0_can_use_latest_prior_real_close_as_uncounted_seed():
    market_returns = [0.001 + index * 0.00001 for index in range(60)]
    stock_returns = [0.002 + 1.2 * value for value in market_returns]
    qfq, none, index, signal_day = _synthetic_pair(market_returns, stock_returns)
    prior_day = pd.Timestamp(index.loc[0, "datetime"]) - pd.offsets.BDay(1)
    qfq = pd.concat(
        [
            _stock_frame(pd.DatetimeIndex([prior_day]), [qfq.loc[0, "close"]]),
            qfq.iloc[1:].copy(),
        ],
        ignore_index=True,
    )
    qfq.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    none = pd.concat(
        [
            _stock_frame(
                pd.DatetimeIndex([prior_day]),
                [none.loc[0, "close"]],
                adjust="none",
            ),
            none.iloc[1:].copy(),
        ],
        ignore_index=True,
    )
    none.attrs.update({"adjust": "none", "timeframe": "1d"})

    result, failure = audit._beta_features(
        qfq, none, index, pd.Timestamp(signal_day).date()
    )

    assert failure is None
    assert result["factor_assignment_available"] is True
    assert result["actual_stock_sessions"] == 60
    assert result["forward_filled_stock_sessions"] == 1


def test_missing_t0_and_no_prior_seed_is_unavailable():
    market_returns = [0.001 + index * 0.00001 for index in range(60)]
    stock_returns = [0.002 + value for value in market_returns]
    qfq, none, index, signal_day = _synthetic_pair(market_returns, stock_returns)
    qfq = qfq.iloc[1:].reset_index(drop=True)
    qfq.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    none = none.iloc[1:].reset_index(drop=True)
    none.attrs.update({"adjust": "none", "timeframe": "1d"})
    result, _ = audit._beta_features(qfq, none, index, pd.Timestamp(signal_day).date())
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "missing_pre_window_stock_seed"


def test_matching_suspension_days_are_forward_filled_causally():
    market_returns = [(-1) ** index * (0.003 + index * 0.00001) for index in range(60)]
    stock_returns = [0.001 + 0.8 * value for value in market_returns]
    qfq, none, index, signal_day = _synthetic_pair(market_returns, stock_returns)
    removed = [10, 20, 30, 40, 50]
    qfq = qfq.drop(index=removed).reset_index(drop=True)
    qfq.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    none = none.drop(index=removed).reset_index(drop=True)
    none.attrs.update({"adjust": "none", "timeframe": "1d"})

    result, failure = audit._beta_features(
        qfq, none, index, pd.Timestamp(signal_day).date()
    )

    assert failure is None
    assert result["factor_assignment_available"] is True
    assert result["actual_stock_sessions"] == 56
    assert result["forward_filled_stock_sessions"] == 5
    days = list(pd.to_datetime(index["datetime"]).dt.date)
    aligned = pd.Series(
        qfq["close"].to_numpy(dtype=float),
        index=pd.Index(pd.to_datetime(qfq["datetime"]).dt.date),
    ).reindex(days, method="ffill")
    aligned_returns = aligned.pct_change(fill_method=None).iloc[1:]
    market_values = index["close"].pct_change(fill_method=None).iloc[1:]
    for removed_index in removed:
        assert aligned_returns.iloc[removed_index - 1] == 0.0
        expected_reopen = aligned.iloc[removed_index + 1] / aligned.iloc[removed_index - 1] - 1.0
        assert aligned_returns.iloc[removed_index] == pytest.approx(expected_reopen)
    expected_beta = audit._ols_slope(
        aligned_returns.to_numpy(dtype=float),
        market_values.to_numpy(dtype=float),
    )
    assert result["beta60"] == pytest.approx(expected_beta)


def test_qfq_none_window_date_mismatch_is_a_safety_failure():
    returns = [(-1) ** index * 0.005 for index in range(60)]
    qfq, none, index, signal_day = _synthetic_pair(returns, returns)
    none = none.drop(index=20).reset_index(drop=True)
    none.attrs.update({"adjust": "none", "timeframe": "1d"})

    result, failure = audit._beta_features(
        qfq, none, index, pd.Timestamp(signal_day).date()
    )

    assert result["factor_error"] == "qfq_none_window_date_mismatch"
    assert failure is not None
    assert len(failure["qfq_only_dates"]) == 1


def test_fewer_than_fifty_real_stock_sessions_is_unavailable():
    returns = [(-1) ** index * 0.005 for index in range(60)]
    qfq, none, index, signal_day = _synthetic_pair(returns, returns)
    removed = list(range(1, 13))
    qfq = qfq.drop(index=removed).reset_index(drop=True)
    qfq.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    none = none.drop(index=removed).reset_index(drop=True)
    none.attrs.update({"adjust": "none", "timeframe": "1d"})
    result, _ = audit._beta_features(qfq, none, index, pd.Timestamp(signal_day).date())
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "insufficient_actual_stock_sessions"
    assert result["actual_stock_sessions"] == 49


def test_zero_market_variance_is_unavailable():
    returns = [0.0] * 60
    qfq, none, index, signal_day = _synthetic_pair(returns, [0.01] * 60)
    result, _ = audit._beta_features(qfq, none, index, pd.Timestamp(signal_day).date())
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "zero_or_invalid_market_variance"


def test_features_do_not_use_bars_after_signal_day():
    market_returns = [(-1) ** index * (0.002 + index * 0.0001) for index in range(60)]
    stock_returns = [0.001 + 1.3 * value for value in market_returns]
    qfq, none, index, signal_day = _synthetic_pair(market_returns, stock_returns)
    expected, _ = audit._beta_features(qfq, none, index, pd.Timestamp(signal_day).date())
    future_days = pd.bdate_range(pd.Timestamp(signal_day) + pd.offsets.BDay(1), periods=3)
    qfq_ext = pd.concat([qfq, _stock_frame(future_days, [9999.0] * 3)], ignore_index=True)
    qfq_ext.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    none_ext = pd.concat(
        [none, _stock_frame(future_days, [8888.0] * 3, adjust="none")],
        ignore_index=True,
    )
    none_ext.attrs.update({"adjust": "none", "timeframe": "1d"})
    index_ext = pd.concat(
        [index, _index_frame(future_days, [7777.0] * 3)], ignore_index=True
    )
    index_ext.attrs.update({"adjust": "none", "timeframe": "1d"})
    actual, _ = audit._beta_features(
        qfq_ext, none_ext, index_ext, pd.Timestamp(signal_day).date()
    )
    assert actual == expected


def test_same_day_median_ties_enter_low_group():
    rows = [
        _row("000001", "2026-01-02", 1.0),
        _row("000002", "2026-01-02", 2.0),
        _row("000003", "2026-01-02", 2.0),
        _row("000004", "2026-01-02", 3.0),
        _row("000005", "2026-01-03", 4.0),
        _row("000006", "2026-01-03", 4.0),
        _row("000007", "2026-01-04", 5.0),
    ]
    audit._assign_primary_halves(rows)
    assert [row["primary_low_beta"] for row in rows[:4]] == [True, True, True, False]
    assert [row["variant_included"] for row in rows[:4]] == [True, True, True, False]
    assert all(row["primary_low_beta"] is None for row in rows[4:])


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("non_qfq", "not qfq"),
        ("wrong_timeframe", "not 1d"),
        ("missing_closed", "missing columns"),
        ("unsorted", "not sorted"),
        ("duplicate_day", "duplicate trading dates"),
        ("invalid_high", "invalid OHLC"),
        ("invalid_datetime", "invalid datetime"),
        ("no_closed", "no closed bars"),
    ],
)
def test_stock_history_validation_fails_closed(mutation: str, message: str):
    days = pd.bdate_range("2026-01-02", periods=61)
    frame = _stock_frame(days, [100.0 + index for index in range(61)])
    if mutation == "non_qfq":
        frame.attrs["adjust"] = "none"
    elif mutation == "wrong_timeframe":
        frame.attrs["timeframe"] = "1h"
    elif mutation == "missing_closed":
        frame = frame.drop(columns=["is_closed"])
    elif mutation == "unsorted":
        frame.loc[[0, 1], "datetime"] = frame.loc[[1, 0], "datetime"].to_numpy()
    elif mutation == "duplicate_day":
        frame.loc[1, "datetime"] = frame.loc[0, "datetime"]
    elif mutation == "invalid_high":
        frame.loc[1, "high"] = frame.loc[1, "close"] - 1.0
    elif mutation == "invalid_datetime":
        frame["datetime"] = frame["datetime"].astype(object)
        frame.loc[1, "datetime"] = "bad"
    elif mutation == "no_closed":
        frame["is_closed"] = False
    with pytest.raises(RuntimeError, match=message):
        audit._validate_stock_history_frame(frame, expected_adjust="qfq")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("non_none", "not none"),
        ("wrong_timeframe", "not 1d"),
        ("missing_closed", "missing columns"),
        ("unsorted", "not sorted"),
        ("duplicate", "duplicate trading dates"),
        ("invalid_close", "invalid close"),
        ("unclosed", "unclosed"),
    ],
)
def test_index_validation_fails_closed(mutation: str, message: str):
    days = pd.bdate_range("2026-01-02", periods=61)
    frame = _index_frame(days, [100.0 + index for index in range(61)])
    if mutation == "non_none":
        frame.attrs["adjust"] = "qfq"
    elif mutation == "wrong_timeframe":
        frame.attrs["timeframe"] = "1h"
    elif mutation == "missing_closed":
        frame = frame.drop(columns=["is_closed"])
    elif mutation == "unsorted":
        frame.loc[[0, 1], "datetime"] = frame.loc[[1, 0], "datetime"].to_numpy()
    elif mutation == "duplicate":
        frame.loc[1, "datetime"] = frame.loc[0, "datetime"]
    elif mutation == "invalid_close":
        frame.loc[1, "close"] = 0.0
    elif mutation == "unclosed":
        frame.loc[1, "is_closed"] = False
    with pytest.raises(RuntimeError, match=message):
        audit._validate_index_frame(frame)


@pytest.mark.parametrize("case", ["missing", "corrupt", "non_qfq", "missing_none"])
def test_history_pair_input_failures_abort(monkeypatch, tmp_path: Path, case: str):
    days = pd.bdate_range("2026-01-02", periods=61)
    qfq_path = tmp_path / "000001_qfq.pkl"
    none_path = tmp_path / "000001_none.pkl"
    if case == "corrupt":
        qfq_path.write_bytes(b"bad")
        _stock_frame(days, [100.0] * 61, adjust="none").to_pickle(none_path)
    elif case == "non_qfq":
        _stock_frame(days, [100.0] * 61, adjust="none").to_pickle(qfq_path)
        _stock_frame(days, [100.0] * 61, adjust="none").to_pickle(none_path)
    elif case == "missing_none":
        _stock_frame(days, [100.0] * 61).to_pickle(qfq_path)
    monkeypatch.setattr(audit, "HISTORY_DIR", tmp_path)
    cache = {}
    failures = []
    assert audit._history_pair("000001", cache, {}, failures) is None
    assert len(failures) == 1


def test_factor_source_regime_is_normalized_and_unknown_is_retained(monkeypatch, tmp_path: Path):
    market_returns = [(-1) ** index * (0.002 + index * 0.0001) for index in range(60)]
    stock_returns = [0.001 + value for value in market_returns]
    qfq, none, index, signal_day = _synthetic_pair(market_returns, stock_returns)
    qfq.to_pickle(tmp_path / "000001_qfq.pkl")
    none.to_pickle(tmp_path / "000001_none.pkl")
    monkeypatch.setattr(audit, "HISTORY_DIR", tmp_path)
    sources = [_source(day=signal_day, regime=" SURPRISE ")]
    features, meta = audit._factor_features_for_sources(sources, index, "index-hash")
    identifier = audit.candidate_id(sources[0])
    assert features[identifier]["normalized_regime"] == "unknown"
    assert features[identifier]["factor_assignment_available"] is True
    assert meta["history_input_safe"] is True


def test_qfq_none_consistency_failure_aborts_before_replay(monkeypatch, tmp_path: Path):
    returns = [(-1) ** index * 0.005 for index in range(60)]
    qfq, none, index, signal_day = _synthetic_pair(returns, returns)
    none = none.drop(index=20).reset_index(drop=True)
    none.attrs.update({"adjust": "none", "timeframe": "1d"})
    qfq.to_pickle(tmp_path / "000001_qfq.pkl")
    none.to_pickle(tmp_path / "000001_none.pkl")
    monkeypatch.setattr(audit, "HISTORY_DIR", tmp_path)
    _, meta = audit._factor_features_for_sources(
        [_source(day=signal_day)], index, "index-hash"
    )
    assert meta["qfq_none_window_consistency_safe"] is False
    with pytest.raises(RuntimeError, match="history safety gate failed before replay"):
        audit._assert_history_safe(meta)


def test_replay_integrity_accepts_exact_mapping_and_rejects_failures():
    sources = [_source("000001"), _source("000002")]
    features = {
        audit.candidate_id(row): {"candidate_id": audit.candidate_id(row)}
        for row in sources
    }
    checks = audit._assert_replay_integrity(
        sources,
        features,
        [dict(row) for row in sources],
        Counter(),
        _valid_source_match(2),
    )
    assert checks["all_pass"] is True

    with pytest.raises(RuntimeError, match="replay integrity validation failed"):
        audit._assert_replay_integrity(
            sources,
            features,
            [dict(sources[0])],
            Counter({"missing_entry_bar": 1}),
            _valid_source_match(2),
        )


@pytest.mark.parametrize(
    "failure",
    ["duplicate_replay", "reordered_replay", "extra_feature", "missing_feature", "policy_skip"],
)
def test_replay_integrity_rejects_id_or_policy_failures(failure: str):
    sources = [_source("000001"), _source("000002")]
    replay = [dict(row) for row in sources]
    features = {
        audit.candidate_id(row): {"candidate_id": audit.candidate_id(row)}
        for row in sources
    }
    source_match = _valid_source_match(2)
    if failure == "duplicate_replay":
        replay[1] = dict(replay[0])
    elif failure == "reordered_replay":
        replay.reverse()
    elif failure == "extra_feature":
        features["999999|2026-03-30|macd_above"] = {}
    elif failure == "missing_feature":
        features.pop(audit.candidate_id(sources[1]))
    elif failure == "policy_skip":
        source_match["policy_skips"] = {"policy_disabled": 1}
    with pytest.raises(RuntimeError, match="replay integrity validation failed"):
        audit._assert_replay_integrity(
            sources, features, replay, Counter(), source_match
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"splits": ["holdout"]}, "cannot consume holdout"),
        ({"splits": ["train", "val"]}, "all required splits"),
        ({"splits": ["val", "train", "test"]}, "all required splits"),
        ({"label": "different"}, "pre-registered label"),
        ({"seed": 1}, "pre-registered seed"),
        ({"bootstrap_reps": 1999}, "pre-registered value"),
    ],
)
def test_unregistered_arguments_are_rejected_before_input_access(
    tmp_path: Path, overrides: dict[str, object], message: str
):
    with pytest.raises(RuntimeError, match=message):
        audit.run(_args(tmp_path / "missing", tmp_path / "output", **overrides))


def test_wrong_index_path_is_rejected(tmp_path: Path):
    with pytest.raises(RuntimeError, match="index path must remain"):
        audit.run(
            _args(
                tmp_path / "missing",
                tmp_path / "output",
                index_data=str(tmp_path / "index.pkl"),
            )
        )


def test_config_and_index_hashes_are_hard_gates(monkeypatch, tmp_path: Path):
    original = audit.file_sha256

    def bad_config(path: Path) -> str:
        if Path(path).name == "config.yaml":
            return "0" * 64
        return original(Path(path))

    monkeypatch.setattr(audit, "file_sha256", bad_config)
    with pytest.raises(RuntimeError, match="config hash differs"):
        audit.run(_args(tmp_path / "missing", tmp_path / "output1"))

    def bad_index(path: Path) -> str:
        if Path(path).name == "index_000001_sh.pkl":
            return "0" * 64
        return original(Path(path))

    monkeypatch.setattr(audit, "file_sha256", bad_index)
    with pytest.raises(RuntimeError, match="index hash differs"):
        audit.run(_args(tmp_path / "missing", tmp_path / "output2"))


def test_cross_split_gate_requires_all_splits_directions_and_ci():
    reports = {split: _split_report() for split in audit.SPLITS}
    assert audit._cross_split_gate(reports)["pass"] is True
    out_of_order = {"test": reports["test"], "train": reports["train"], "val": reports["val"]}
    assert audit._cross_split_gate(out_of_order)["pass"] is True
    for changed in (
        {"test": _split_report(eligible=False)},
        {"test": _split_report(future_40d=-0.1)},
        {"test": _split_report(contract_ok=False)},
        {"test": _split_report(ci_positive=False)},
    ):
        candidate = dict(reports)
        candidate.update(changed)
        assert audit._cross_split_gate(candidate)["pass"] is False
    assert audit._cross_split_gate({"train": reports["train"]})["pass"] is False


def test_symbol_cluster_bootstrap_is_deterministic_and_meets_contract():
    rows: list[dict[str, object]] = []
    for index in range(10):
        symbol = f"{index + 1:06d}"
        rows.extend(
            [
                {"symbol": symbol, "variant_included": True, "future_40d": 2.0},
                {"symbol": symbol, "variant_included": False, "future_40d": 1.0},
            ]
        )
    first = audit._cluster_bootstrap_delta(
        rows, "future_40d", reps=2000, seed=audit.PREREGISTERED_SEED
    )
    second = audit._cluster_bootstrap_delta(
        rows, "future_40d", reps=2000, seed=audit.PREREGISTERED_SEED
    )
    assert first == second
    assert first["mean_delta"] == 1.0
    assert first["reps_valid"] == first["reps_requested"] == 2000
    assert first["cluster_count"] == 10
    assert audit._bootstrap_contract_ok(first) is True


def test_bootstrap_contract_rejects_too_few_clusters_or_replicates():
    base = {
        "mean_delta": 1.0,
        "reps_requested": 2000,
        "reps_valid": 2000,
        "cluster_count": 10,
    }
    assert audit._bootstrap_contract_ok({**base, "cluster_count": 9}) is False
    assert audit._bootstrap_contract_ok({**base, "reps_valid": 1999}) is False
    assert audit._bootstrap_contract_ok({**base, "reps_requested": 1999}) is False


def test_diagnostics_and_regime_results_cannot_rescue_primary_gate(monkeypatch):
    rows = [
        _row(
            f"{symbol:06d}",
            f"2026-01-{day:02d}",
            float(symbol),
            regime="range" if day % 2 else "bear",
        )
        for day in range(2, 12)
        for symbol in range(1, 11)
    ]
    audit._assign_primary_halves(rows)

    def negative_primary(*args, **kwargs):
        return {
            "mean_delta": -1.0,
            "ci95_low": -2.0,
            "ci95_high": -0.5,
            "reps_requested": 2000,
            "reps_valid": 2000,
            "cluster_count": 10,
        }

    monkeypatch.setattr(audit, "_cluster_bootstrap_delta", negative_primary)
    report = audit._candidate_report(
        rows,
        _valid_source_match(len(rows)),
        {"all_pass": True},
        _valid_factor_meta(),
        audit.PREREGISTERED_SEED,
    )
    assert report["regime_diagnostics"]["range"]["n"] > 0
    assert report["factors"]["downside_beta_60"]["n"] == len(rows)
    assert report["gate"]["primary_direction_positive"] is False
    assert report["gate"]["primary_cluster_bootstrap_ci95_positive"] is False


def test_existing_formal_or_temporary_output_is_never_overwritten(tmp_path: Path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    target = output_dir / "low_beta_risk_train.jsonl"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_targets_unused(output_dir)
    target.unlink()
    target.with_name(target.name + ".tmp").write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_targets_unused(output_dir)


def test_atomic_writer_records_hashes_and_leaves_no_temporary_files(tmp_path: Path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    enriched = {split: [{"split": split}] for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}
    audit._write_outputs_atomically(output_dir, enriched, result)
    for split in audit.SPLITS:
        target = output_dir / f"low_beta_risk_{split}.jsonl"
        assert target.exists()
        assert result["splits"][split]["enriched_sha256"] == audit.file_sha256(target)
        assert not target.with_name(target.name + ".tmp").exists()
    written = json.loads((output_dir / "low_beta_risk_audit.json").read_text(encoding="utf-8"))
    assert written["splits"] == result["splits"]


def test_run_emits_fixed_provenance_safety_and_input_hashes(monkeypatch, tmp_path: Path):
    input_dir = tmp_path / "canonical"
    output_dir = tmp_path / "formal"
    input_dir.mkdir()
    rows = [
        _row(f"{symbol:06d}", f"2026-01-{day:02d}", float(symbol))
        for day in range(2, 12)
        for symbol in range(1, 11)
    ]
    split_records: dict[str, object] = {}
    for split in audit.SPLITS:
        path = input_dir / f"candidates_{split}.jsonl"
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        split_records[split] = {
            "output_file": str(path.resolve()),
            "output_sha256": audit.file_sha256(path),
            "output_rows": len(rows),
            "candidate_ids_sha256": audit.candidate_ids_sha256(rows),
        }
    manifest = {
        "version": audit.INTEGRITY_VERSION,
        "output_dir": str(input_dir.resolve()),
        "splits": split_records,
    }
    (input_dir / "candidate_integrity_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    def factor_features(sources, index_frame, index_sha256):
        return (
            {
                audit.candidate_id(row): {
                    "candidate_id": audit.candidate_id(row),
                    "normalized_regime": row["regime"],
                    "factor_assignment_available": True,
                    "factor_error": None,
                    "beta60": float(row["beta60"]),
                    "market_correlation_60": float(row["market_correlation_60"]),
                    "downside_beta_60": float(row["downside_beta_60"]),
                    "idiosyncratic_volatility_60": float(
                        row["idiosyncratic_volatility_60"]
                    ),
                }
                for row in sources
            },
            _valid_factor_meta(),
        )

    def replay_split(path, config):
        replayed = audit.load_jsonl(path)
        return replayed, Counter(), _valid_source_match(len(replayed))

    def positive_bootstrap(*args, **kwargs):
        return {
            "mean_delta": 1.0,
            "ci95_low": 0.1,
            "ci95_high": 2.0,
            "reps_requested": 2000,
            "reps_valid": 2000,
            "cluster_count": 10,
        }

    days = pd.bdate_range("2026-01-02", periods=61)
    fake_index = _index_frame(days, [100.0 + index for index in range(61)])
    monkeypatch.setattr(audit, "_load_index_frame", lambda path: fake_index)
    monkeypatch.setattr(audit, "_factor_features_for_sources", factor_features)
    monkeypatch.setattr(audit, "_replay_split", replay_split)
    monkeypatch.setattr(audit, "_cluster_bootstrap_delta", positive_bootstrap)
    monkeypatch.setattr(audit, "load_config", lambda path: {})
    monkeypatch.setattr(audit, "_config_snapshot", lambda config: {})

    result = audit.run(_args(input_dir, output_dir))

    assert result["research_provenance"]["original_scope"] == "range_bear_only"
    assert result["research_provenance"]["revised_scope"] == "all_canonical"
    assert result["research_provenance"]["scope_changed_before_implementation"] is True
    assert result["research_provenance"]["weak_regime_precheck_counts"] == {
        "train": {"candidates": 2, "signal_days": 2},
        "val": {"candidates": 53, "signal_days": 11},
        "test": {"candidates": 17, "signal_days": 7},
    }
    assert result["design"]["regime_diagnostic_only"] is True
    assert result["design"]["source_outcomes_used"] is False
    assert result["design"]["holdout_consumed"] is False
    assert result["design"]["portfolio_layer_implemented"] is False
    assert result["design"]["production_eligible"] is False
    assert result["production_database_connected"] is False
    assert result["config_file_unchanged"] is True
    assert result["index_file_unchanged"] is True
    assert result["config_file_expected_sha256"] == audit.PREREGISTERED_CONFIG_SHA256
    assert result["index_file_expected_sha256"] == audit.PREREGISTERED_INDEX_SHA256
    assert result["production_decision"] == "unchanged_P0"
    assert result["candidate_gate"]["pass"] is True
    assert (output_dir / "low_beta_risk_audit.json").exists()
