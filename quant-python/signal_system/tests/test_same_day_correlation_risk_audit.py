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

import same_day_correlation_risk_audit as audit  # noqa: E402


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
        "primary_low_correlation_risk": low,
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
    assert audit.PRIMARY_FACTOR == "same_day_mean_pairwise_correlation_60"
    assert audit.RETURN_OBSERVATIONS == 60
    assert audit.COMMON_CLOSES_REQUIRED == 61
    assert audit.MIN_VALID_PEERS == 2
    assert audit.PREREGISTERED_SEED == 20260903
    assert audit.PREREGISTERED_BOOTSTRAP_REPS == 2000
    assert audit.FACTOR_SPECS[audit.PRIMARY_FACTOR] == {
        "larger_is_better": False,
        "role": "primary",
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
    pd.testing.assert_series_equal(result, series, check_names=False, check_freq=False)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda frame: frame.attrs.update({"adjust": "none"}), "adjust"),
        (lambda frame: frame.attrs.update({"timeframe": "5m"}), "timeframe"),
        (lambda frame: frame.__setitem__("close", [0.0] * len(frame)), "positive"),
        (lambda frame: frame.__setitem__("is_closed", [1] * len(frame)), "non-bool"),
    ],
)
def test_history_frame_corruption_fails_closed(mutate, message):
    frame = _frame(_close_series(np.linspace(-0.01, 0.01, 60)))
    mutate(frame)
    with pytest.raises(RuntimeError, match=message):
        audit._validate_history_frame(frame, Path("history.pkl"))


def test_history_duplicate_date_fails_closed():
    series = _close_series(np.linspace(-0.01, 0.01, 60))
    frame = _frame(series)
    frame.loc[1, "datetime"] = frame.loc[0, "datetime"]
    with pytest.raises(RuntimeError, match="duplicate"):
        audit._validate_history_frame(frame, Path("history.pkl"))


def test_pair_correlation_uses_last_61_common_closes_and_matches_manual():
    base = np.linspace(-0.02, 0.03, 70)
    left = _close_series(base)
    right_returns = 0.75 * base + np.sin(np.arange(70)) * 0.002
    right = _close_series(right_returns)
    day = left.index[-1].date().isoformat()

    correlation, common = audit._pair_correlation(left, right, day)

    joined = pd.concat([left, right], axis=1).tail(61)
    expected = joined.pct_change(fill_method=None).iloc[1:].iloc[:, 0].corr(
        joined.pct_change(fill_method=None).iloc[1:].iloc[:, 1]
    )
    assert common == 61
    assert correlation == pytest.approx(expected)


def test_pair_correlation_joins_closes_before_returns_for_missing_day():
    returns = np.linspace(-0.01, 0.015, 70)
    left = _close_series(returns)
    right = _close_series(returns * 0.5 + 0.001, drop={5})
    day = left.index[-1].date().isoformat()

    correlation, common = audit._pair_correlation(left, right, day)

    joined = pd.concat([left, right], axis=1, join="inner").dropna().tail(61)
    expected_returns = joined.pct_change(fill_method=None).iloc[1:]
    expected = expected_returns.iloc[:, 0].corr(expected_returns.iloc[:, 1])
    assert common == 61
    assert correlation == pytest.approx(expected)


def test_pair_correlation_is_unavailable_with_only_60_common_closes():
    returns = np.linspace(-0.01, 0.01, 59)
    left = _close_series(returns)
    right = _close_series(returns * 0.8)
    correlation, common = audit._pair_correlation(
        left, right, left.index[-1].date().isoformat()
    )
    assert correlation is None
    assert common == 60


def test_pair_correlation_ignores_post_signal_suffix():
    returns = np.sin(np.arange(80)) * 0.01
    left = _close_series(returns)
    right = _close_series(returns * 0.8 + np.cos(np.arange(80)) * 0.002)
    day = left.index[65].date().isoformat()
    first = audit._pair_correlation(left, right, day)[0]
    left.iloc[70:] *= 8
    right.iloc[70:] *= 0.2
    second = audit._pair_correlation(left, right, day)[0]
    assert first == pytest.approx(second)


def test_pair_constant_return_is_unavailable():
    left = _close_series(np.zeros(60))
    right = _close_series(np.linspace(-0.01, 0.01, 60))
    assert audit._pair_correlation(
        left, right, left.index[-1].date().isoformat()
    )[0] is None


def test_factor_features_compute_peer_statistics_and_same_day_groups():
    day = pd.bdate_range("2025-12-01", periods=70)[-1].date().isoformat()
    grid = np.arange(69)
    sequences = {
        "000001": np.sin(grid / 5) * 0.01,
        "000002": np.sin(grid / 5) * 0.01 + np.cos(grid) * 0.001,
        "000003": -np.sin(grid / 5) * 0.01 + np.cos(grid / 3) * 0.002,
        "000004": np.cos(grid / 4) * 0.008,
    }
    sources = [_source(symbol, day) for symbol in sequences]
    cache = {symbol: _close_series(values) for symbol, values in sequences.items()}
    snapshot = {
        f"qfq:{symbol}": {
            "path": f"{symbol}.pkl",
            "size_bytes": 1,
            "sha256": symbol,
        }
        for symbol in sequences
    }

    features, meta = audit._factor_features_for_sources(sources, cache, snapshot)

    assert list(features) == [audit.candidate_id(row) for row in sources]
    assert all(item["factor_assignment_available"] for item in features.values())
    assert all(item["same_day_valid_peer_count"] == 3 for item in features.values())
    assert sum(item["primary_low_correlation_risk"] is True for item in features.values()) == 2
    assert sum(item["primary_low_correlation_risk"] is False for item in features.values()) == 2
    assert meta["valid_pair_count"] == 6
    assert meta["valid_common_close_count_min"] == 61


def test_factor_requires_two_distinct_valid_peer_symbols():
    day = pd.bdate_range("2025-12-01", periods=61)[-1].date().isoformat()
    sources = [_source("000001", day), _source("000002", day)]
    cache = {
        "000001": _close_series(np.linspace(-0.01, 0.01, 60)),
        "000002": _close_series(np.linspace(-0.02, 0.02, 60)),
    }
    snapshot = {
        f"qfq:{symbol}": {"path": f"{symbol}.pkl", "size_bytes": 1, "sha256": symbol}
        for symbol in cache
    }
    features, meta = audit._factor_features_for_sources(sources, cache, snapshot)
    assert all(item[audit.PRIMARY_FACTOR] is None for item in features.values())
    assert meta["primary_unavailable_candidates"] == 2


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
        rows, "future_40d", reps=100, seed=20260903
    )
    second = audit._cluster_bootstrap_fe_low_minus_high(
        rows, "future_40d", reps=100, seed=20260903
    )
    assert first == second
    assert first["fixed_effect_beta"] == pytest.approx(2.0)
    assert first["reps_valid"] == 100
    assert first["cluster_count"] == 24
    assert first["seed"] == 20260903


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
        rows, "future_40d", reps=50, seed=20260903
    )
    second = audit._cluster_bootstrap_fe_low_minus_high(
        list(reversed(rows)), "future_40d", reps=50, seed=20260903
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
    replay[0]["candidate_id"] = "stale"
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
    assert len(list(final.iterdir())) == 4


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
                    "primary_low_correlation_risk": True,
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
