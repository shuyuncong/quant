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

import downside_gap_risk_audit as audit  # noqa: E402


def _frame(
    days: pd.DatetimeIndex,
    opens: list[float],
    closes: list[float],
    *,
    adjust: str = "qfq",
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "datetime": days,
            "open": opens,
            "high": [max(open_, close) + 1.0 for open_, close in zip(opens, closes)],
            "low": [min(open_, close) - 1.0 for open_, close in zip(opens, closes)],
            "close": closes,
            "volume": [1000.0] * len(days),
            "amount": [100000.0] * len(days),
            "is_closed": [True] * len(days),
        }
    )
    frame.attrs.update({"adjust": adjust, "timeframe": "1d"})
    return frame


def _frame_from_gaps(
    gaps: list[float],
    *,
    adjust: str = "qfq",
    start: str = "2026-01-02",
    closes: list[float] | None = None,
) -> pd.DataFrame:
    if closes is None:
        closes = [100.0] * (len(gaps) + 1)
    assert len(closes) == len(gaps) + 1
    opens = [closes[0]] + [
        closes[index] * (1.0 + gap) for index, gap in enumerate(gaps)
    ]
    days = pd.bdate_range(start, periods=len(closes))
    return _frame(days, opens, closes, adjust=adjust)


def _source(
    symbol: str = "000001",
    day: str = "2026-03-26",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "signal_day": day,
        "signal_type": "macd_above",
        "regime": "bull",
    }


def _row(
    symbol: str,
    day: str,
    risk: float,
    outcome: float | None = None,
) -> dict[str, object]:
    return {
        **_source(symbol, day),
        "candidate_id": f"{symbol}|{day}|macd_above",
        audit.PRIMARY_FACTOR: risk,
        "factor_assignment_available": True,
        "primary_low_gap_risk": None,
        "variant_included": False,
        "future_20d": outcome,
        "future_40d": outcome,
        "trade_pnl_pct": outcome,
        "normalized_regime": "bull",
    }


def _valid_source_match(count: int) -> dict[str, object]:
    return {
        "source_rows": count,
        "common_eligible_rows": count,
        "simulated_rows": count,
        "source_pnl_abs_diff": {},
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


def test_primary_factor_direction_window_and_diagnostics_are_fixed():
    assert audit.PRIMARY_FACTOR == "downside_gap_semideviation_60"
    assert audit.FACTOR_SPECS[audit.PRIMARY_FACTOR] == {
        "larger_is_better": False,
        "role": "primary",
    }
    assert audit.GAP_OBSERVATIONS == 60
    assert audit.MAIN_WINDOW_BARS == 61
    assert audit.CHASE_WINDOW_BARS == 62
    assert audit.LARGE_DOWN_GAP_THRESHOLD == -0.03
    assert audit.LIMIT_DOWN_PROXY_THRESHOLD == -0.095
    assert audit.LARGE_UP_DAY_THRESHOLD == 0.095
    assert all(
        spec["role"] == "diagnostic"
        for name, spec in audit.FACTOR_SPECS.items()
        if name != audit.PRIMARY_FACTOR
    )


def test_gap_features_match_manual_semideviation_frequency_and_worst_gap():
    gaps = [-0.10, -0.02] + [0.01] * 58
    qfq = _frame_from_gaps(gaps)
    none = _frame_from_gaps(gaps, adjust="none")

    result, failure = audit._gap_features(
        qfq,
        none,
        qfq.iloc[-1]["datetime"].date(),
    )

    assert failure is None
    assert result["factor_assignment_available"] is True
    assert result[audit.PRIMARY_FACTOR] == pytest.approx(
        math.sqrt((0.10**2 + 0.02**2) / 60.0)
    )
    assert result["large_down_gap_frequency_60"] == pytest.approx(1.0 / 60.0)
    assert result["worst_down_gap_60"] == pytest.approx(0.10)
    assert result["none_downside_gap_semideviation_60"] == pytest.approx(
        result[audit.PRIMARY_FACTOR]
    )


def test_all_positive_gaps_have_zero_primary_downside_risk():
    qfq = _frame_from_gaps([0.01] * 60)
    none = _frame_from_gaps([0.02] * 60, adjust="none")

    result, _ = audit._gap_features(qfq, none, qfq.iloc[-1]["datetime"].date())

    assert result[audit.PRIMARY_FACTOR] == 0.0
    assert result["worst_down_gap_60"] == 0.0
    assert result["large_down_gap_frequency_60"] == 0.0


def test_none_mechanical_gap_is_diagnostic_and_does_not_change_qfq_primary():
    qfq = _frame_from_gaps([0.0] * 60)
    none_gaps = [-0.10] + [0.0] * 59
    none = _frame_from_gaps(none_gaps, adjust="none")

    result, _ = audit._gap_features(qfq, none, qfq.iloc[-1]["datetime"].date())

    assert result[audit.PRIMARY_FACTOR] == 0.0
    assert result["none_downside_gap_semideviation_60"] == pytest.approx(
        math.sqrt(0.10**2 / 60.0)
    )
    assert result["open_limit_down_proxy_frequency_60"] == pytest.approx(
        1.0 / 60.0
    )


def test_open_limit_down_proxy_boundary_includes_equal_threshold():
    qfq = _frame_from_gaps([0.0] * 60)
    none = _frame_from_gaps(
        [audit.LIMIT_DOWN_PROXY_THRESHOLD] + [0.0] * 59,
        adjust="none",
    )

    result, _ = audit._gap_features(qfq, none, qfq.iloc[-1]["datetime"].date())

    assert result["open_limit_down_proxy_frequency_60"] == pytest.approx(
        1.0 / 60.0
    )


def test_chase_proxy_uses_all_qualifying_days_and_positive_gap_clip():
    closes = [100.0, 110.0] + [100.0] * 60
    gaps = [0.0, 0.02] + [0.0] * 59
    qfq = _frame_from_gaps(gaps, closes=closes)
    none = _frame_from_gaps(gaps, closes=closes, adjust="none")

    result, _ = audit._gap_features(qfq, none, qfq.iloc[-1]["datetime"].date())

    assert result["chase_history_available"] is True
    assert result["chase_qualifying_days"] == 1
    assert result["post_large_up_day_chase_gap_mean_60"] == pytest.approx(0.02)


def test_chase_proxy_is_zero_when_qualifying_day_has_no_positive_gap():
    closes = [100.0, 110.0] + [100.0] * 60
    gaps = [0.0, -0.02] + [0.0] * 59
    qfq = _frame_from_gaps(gaps, closes=closes)
    none = _frame_from_gaps(gaps, closes=closes, adjust="none")

    result, _ = audit._gap_features(qfq, none, qfq.iloc[-1]["datetime"].date())

    assert result["chase_qualifying_days"] == 1
    assert result["post_large_up_day_chase_gap_mean_60"] == 0.0


def test_chase_proxy_is_unavailable_without_qualifying_day():
    qfq = _frame_from_gaps([0.0] * 61)
    none = _frame_from_gaps([0.0] * 61, adjust="none")

    result, _ = audit._gap_features(qfq, none, qfq.iloc[-1]["datetime"].date())

    assert result["chase_history_available"] is True
    assert result["chase_qualifying_days"] == 0
    assert result["post_large_up_day_chase_gap_mean_60"] is None


def test_exactly_sixty_one_bars_keeps_primary_but_not_chase_diagnostic():
    qfq = _frame_from_gaps([-0.01] * 60)
    none = _frame_from_gaps([-0.01] * 60, adjust="none")

    result, _ = audit._gap_features(qfq, none, qfq.iloc[-1]["datetime"].date())

    assert result["factor_assignment_available"] is True
    assert result[audit.PRIMARY_FACTOR] == pytest.approx(0.01)
    assert result["chase_history_available"] is False
    assert result["post_large_up_day_chase_gap_mean_60"] is None


def test_fewer_than_sixty_one_bars_is_factor_unavailable():
    qfq = _frame_from_gaps([-0.01] * 59)
    none = _frame_from_gaps([-0.01] * 59, adjust="none")

    result, failure = audit._gap_features(
        qfq,
        none,
        qfq.iloc[-1]["datetime"].date(),
    )

    assert failure is None
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "insufficient_60_gap_history"


def test_qfq_none_main_window_date_mismatch_is_safety_failure():
    qfq = _frame_from_gaps([0.0] * 60)
    none = _frame_from_gaps([0.0] * 60, adjust="none")
    none.loc[0, "datetime"] = none.loc[0, "datetime"] - pd.Timedelta(days=1)

    result, failure = audit._gap_features(qfq, none, qfq.iloc[-1]["datetime"].date())

    assert result["factor_assignment_available"] is False
    assert failure is not None
    assert failure["error"] == "qfq_none_window_date_mismatch"


def test_features_do_not_use_bars_after_signal_day():
    qfq = _frame_from_gaps([-0.01] * 61)
    none = _frame_from_gaps([-0.02] * 61, adjust="none")
    signal_day = qfq.iloc[-2]["datetime"].date()
    before, _ = audit._gap_features(qfq, none, signal_day)
    qfq.loc[qfq.index[-1], ["open", "high", "low", "close"]] = [999, 1000, 998, 999]
    none.loc[none.index[-1], ["open", "high", "low", "close"]] = [888, 889, 887, 888]

    after, _ = audit._gap_features(qfq, none, signal_day)

    assert before == after


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("adjust", "adjustment"),
        ("timeframe", "timeframe"),
        ("duplicate", "duplicate"),
        ("unsorted", "unsorted"),
        ("nan", "invalid OHLC"),
        ("negative", "invalid OHLC"),
        ("bad_high", "invalid OHLC"),
    ],
)
def test_history_validation_fails_closed(mutation: str, message: str):
    frame = _frame_from_gaps([0.0] * 60)
    if mutation == "adjust":
        frame.attrs["adjust"] = "none"
    elif mutation == "timeframe":
        frame.attrs["timeframe"] = "1h"
    elif mutation == "duplicate":
        frame.loc[1, "datetime"] = frame.loc[0, "datetime"]
    elif mutation == "unsorted":
        frame.loc[[0, 1], "datetime"] = frame.loc[[1, 0], "datetime"].to_numpy()
    elif mutation == "nan":
        frame.loc[0, "open"] = np.nan
    elif mutation == "negative":
        frame.loc[0, "close"] = -1.0
    elif mutation == "bad_high":
        frame.loc[0, "high"] = 1.0

    with pytest.raises(RuntimeError, match=message):
        audit._validate_history_frame(frame, expected_adjust="qfq")


def test_missing_required_history_column_fails_closed():
    frame = _frame_from_gaps([0.0] * 60).drop(columns=["open"])
    with pytest.raises(RuntimeError, match="missing columns"):
        audit._validate_history_frame(frame, expected_adjust="qfq")


def test_same_day_median_ties_enter_low_group_and_all_tie_day_is_excluded():
    rows = [
        _row("000001", "2026-03-01", 0.01),
        _row("000002", "2026-03-01", 0.02),
        _row("000003", "2026-03-01", 0.02),
        _row("000004", "2026-03-01", 0.04),
        _row("000005", "2026-03-02", 0.03),
        _row("000006", "2026-03-02", 0.03),
    ]

    audit._assign_primary_halves(rows)

    assert [row["primary_low_gap_risk"] for row in rows[:4]] == [
        True,
        True,
        True,
        False,
    ]
    assert rows[4]["primary_low_gap_risk"] is None
    assert rows[5]["primary_low_gap_risk"] is None


def test_outcome_sample_gate_reapplies_minima_after_missing_values(monkeypatch):
    monkeypatch.setattr(audit, "MIN_AFFECTED_CANDIDATES", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SYMBOLS", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SIGNAL_DAYS", 2)
    rows = [
        _row("000001", "2026-03-01", 0.01, 2.0),
        _row("000002", "2026-03-01", 0.02, 1.0),
        _row("000003", "2026-03-02", 0.03, None),
        _row("000004", "2026-03-02", 0.04, None),
    ]
    audit._assign_primary_halves(rows)

    report = audit._outcome_sample_gate(rows, "future_40d")

    assert report["valid_candidates"] == 2
    assert report["sufficient"] is False


def test_cluster_bootstrap_is_deterministic_and_uses_fixed_labels(monkeypatch):
    monkeypatch.setattr(audit, "MIN_OUTCOME_CLUSTERS", 2)
    rows = [
        _row("000001", "2026-03-01", 0.01, 4.0),
        _row("000002", "2026-03-01", 0.04, 1.0),
        _row("000003", "2026-03-02", 0.02, 3.0),
        _row("000004", "2026-03-02", 0.05, 0.0),
    ]
    audit._assign_primary_halves(rows)

    first = audit._cluster_bootstrap_low_minus_high(
        rows, "future_40d", reps=50, seed=123
    )
    second = audit._cluster_bootstrap_low_minus_high(
        rows, "future_40d", reps=50, seed=123
    )

    assert first == second
    assert first["mean_delta"] == pytest.approx(3.0)
    assert first["cluster_count"] == 4
    assert first["reps_requested"] == 50
    assert 0 < first["reps_valid"] <= 50


def test_repeated_selected_cluster_repeats_whole_candidate_weight(monkeypatch):
    class FakeRng:
        def integers(self, low: int, high: int, size: int) -> np.ndarray:
            assert (low, high, size) == (0, 2, 2)
            return np.asarray([0, 0])

    monkeypatch.setattr(np.random, "default_rng", lambda seed: FakeRng())
    rows = [
        _row("000001", "2026-03-01", 0.01, 4.0),
        _row("000001", "2026-03-02", 0.04, 1.0),
        _row("000002", "2026-03-01", 0.02, 2.0),
        _row("000002", "2026-03-02", 0.01, 4.0),
    ]
    audit._assign_primary_halves(rows)

    report = audit._cluster_bootstrap_low_minus_high(
        rows, "future_40d", reps=1, seed=7
    )

    assert report["reps_valid"] == 1
    assert report["ci95_low"] == pytest.approx(3.0)
    assert report["ci95_high"] == pytest.approx(3.0)


def test_bootstrap_contract_requires_all_replicates_and_minimum_clusters(monkeypatch):
    monkeypatch.setattr(audit, "PREREGISTERED_BOOTSTRAP_REPS", 10)
    monkeypatch.setattr(audit, "MIN_OUTCOME_CLUSTERS", 3)
    assert audit._bootstrap_contract_ok(
        {
            "mean_delta": 1.0,
            "reps_requested": 10,
            "reps_valid": 10,
            "cluster_count": 3,
        }
    )
    assert not audit._bootstrap_contract_ok(
        {
            "mean_delta": 1.0,
            "reps_requested": 10,
            "reps_valid": 9,
            "cluster_count": 3,
        }
    )


def test_cross_split_gate_requires_every_split_direction_and_ci():
    reports = {split: _split_report() for split in audit.SPLITS}
    assert audit._cross_split_gate(reports)["pass"] is True

    missing = dict(reports)
    missing.pop("test")
    assert audit._cross_split_gate(missing)["pass"] is False

    negative = dict(reports)
    negative["val"] = _split_report(trade_pnl_pct=-0.1)
    assert audit._cross_split_gate(negative)["pass"] is False

    weak_ci = dict(reports)
    weak_ci["train"] = _split_report(ci_positive=False)
    assert audit._cross_split_gate(weak_ci)["pass"] is False


def test_replay_integrity_accepts_exact_mapping_and_marks_source_outcomes_diagnostic():
    sources = [_source("000001"), _source("000002")]
    features = {candidate_id: {} for candidate_id in [audit.candidate_id(r) for r in sources]}
    replay = [
        {**source, "candidate_id": audit.candidate_id(source)} for source in sources
    ]

    checks, source_match = audit._assert_replay_integrity(
        sources,
        features,
        replay,
        Counter(),
        _valid_source_match(2),
    )

    assert checks["all_pass"] is True
    assert source_match["baseline_replay_complete"] is True
    assert source_match["source_outcomes_used"] is False
    assert source_match["replay_outcomes_used"] is True


@pytest.mark.parametrize(
    "failure",
    ["order", "skip", "eligible", "duplicate", "stale_explicit_id", "feature_order"],
)
def test_replay_integrity_rejects_mapping_or_skip_failures(failure: str):
    sources = [_source("000001"), _source("000002")]
    features = {audit.candidate_id(row): {} for row in sources}
    replay = [
        {**source, "candidate_id": audit.candidate_id(source)} for source in sources
    ]
    skips: Counter = Counter()
    match = _valid_source_match(2)
    if failure == "order":
        replay.reverse()
    elif failure == "skip":
        skips["missing"] = 1
    elif failure == "eligible":
        match["common_eligible_rows"] = 1
    elif failure == "duplicate":
        replay[1] = dict(replay[0])
    elif failure == "stale_explicit_id":
        replay[0]["candidate_id"] = "stale"
    elif failure == "feature_order":
        features = dict(reversed(list(features.items())))

    with pytest.raises(RuntimeError, match="replay integrity"):
        audit._assert_replay_integrity(
            sources,
            features,
            replay,
            skips,
            match,
        )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -float("inf"), "1.0", True])
def test_authoritative_outcomes_must_be_finite_numeric(bad_value: object):
    row = {
        **_source(),
        "candidate_id": "000001|2026-03-26|macd_above",
        "future_5d": 1.0,
        "future_20d": 1.0,
        "future_40d": bad_value,
        "mfe": 1.0,
        "mae": -1.0,
        "trade_pnl_pct": 1.0,
        "post_exit_5d": None,
        "post_exit_20d": None,
    }
    with pytest.raises(RuntimeError, match="authoritative replay outcome"):
        audit._validate_authoritative_outcomes([row])


def test_none_authoritative_outcome_is_allowed_and_excluded():
    row = _row("000001", "2026-03-01", 0.01, None)
    audit._validate_authoritative_outcomes([row])
    assert audit._valid_outcome_rows([row], "future_40d") == []


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"splits": ["train", "val"]}, "all required splits"),
        ({"label": "changed"}, "label differs"),
        ({"seed": 1}, "seed differs"),
        ({"bootstrap_reps": 1}, "bootstrap reps differ"),
        ({"expected_script_sha256": "bad"}, "64-character"),
        ({"input_dir": "D:/tmp/other"}, "input dir must remain"),
        ({"output_dir": "D:/tmp/other"}, "output dir must remain"),
        ({"config": "D:/tmp/config.yaml"}, "config path must remain"),
    ],
)
def test_preregistered_arguments_are_fixed(override: dict[str, object], message: str):
    with pytest.raises(RuntimeError, match=message):
        audit._validate_preregistered_args(_args(**override))


def test_script_sha_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(audit, "file_sha256", lambda path: "a" * 64)
    with pytest.raises(RuntimeError, match="script SHA differs"):
        audit._validate_preregistered_args(
            _args(expected_script_sha256="b" * 64)
        )


def test_history_manifest_is_stable_and_sensitive_to_hash_changes():
    snapshot = {
        "000001|qfq": {"path": "q", "sha256": "a" * 64},
        "000001|none": {"path": "n", "sha256": "b" * 64},
    }
    first = audit._history_manifest(snapshot)
    reordered = dict(reversed(list(snapshot.items())))
    assert audit._history_manifest(reordered) == first
    reordered["000001|none"] = {"path": "n", "sha256": "c" * 64}
    assert audit._history_manifest(reordered) != first


def test_snapshot_changes_detects_modified_file(tmp_path: Path):
    path = tmp_path / "protected.txt"
    path.write_text("before", encoding="utf-8")
    initial = audit._snapshot_named_paths({"protected": path})
    path.write_text("after", encoding="utf-8")

    _, changes = audit._snapshot_changes(initial)

    assert len(changes) == 1
    assert changes[0]["name"] == "protected"


def test_existing_formal_or_staging_output_is_never_overwritten(tmp_path: Path):
    output = tmp_path / "formal"
    output.mkdir()
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_directory_unused(output)
    output.rmdir()
    staging = output.with_name(output.name + ".tmp")
    staging.mkdir()
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_directory_unused(output)


def test_atomic_directory_writer_records_hashes_and_leaves_no_tmp(tmp_path: Path):
    output = tmp_path / "formal"
    staging = output.with_name(output.name + ".tmp")
    rows = {
        split: [{"candidate_id": f"{split}-1", "value": 1}]
        for split in audit.SPLITS
    }
    result: dict[str, object] = {
        "splits": {
            split: {"enriched": None, "enriched_sha256": None}
            for split in audit.SPLITS
        }
    }

    audit._write_outputs_atomically(output, staging, rows, result)

    assert output.is_dir()
    assert not staging.exists()
    assert (output / "downside_gap_risk_audit.json").is_file()
    for split in audit.SPLITS:
        artifact = result["artifacts"][split]  # type: ignore[index]
        assert artifact["rows"] == 1
        assert artifact["sha256"] == audit.file_sha256(
            output / f"downside_gap_risk_{split}.jsonl"
        )
    report = json.loads(
        (output / "downside_gap_risk_audit.json").read_text(encoding="utf-8")
    )
    assert report["report_path"].endswith("downside_gap_risk_audit.json")


def _install_mocked_run_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    snapshot_change_schedule: list[list[dict[str, str]]] | None = None,
) -> dict[str, object]:
    calls: dict[str, object] = {
        "profiles": [],
        "candidate_report_seeds": [],
        "published": 0,
    }
    sources_by_name = {
        f"candidates_{split}.jsonl": [
            _source(f"00000{index + 1}", "2026-03-26")
        ]
        for index, split in enumerate(audit.SPLITS)
    }
    schedule = iter(snapshot_change_schedule or [[], [], [], []])

    monkeypatch.setattr(audit, "_validate_preregistered_args", lambda args: None)
    monkeypatch.setattr(
        audit,
        "_assert_output_directory_unused",
        lambda output: tmp_path / "formal.tmp",
    )

    def fake_snapshot(paths: dict[str, Path]) -> dict[str, dict[str, str]]:
        return {
            name: {"path": str(path), "sha256": f"hash-{name}"}
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
        lambda input_dir, split, source_path, rows: {"all_pass": True},
    )
    monkeypatch.setattr(
        audit,
        "_history_manifest",
        lambda snapshot: audit.PREREGISTERED_HISTORY_MANIFEST_SHA256,
    )

    def fake_features(
        sources: list[dict[str, object]],
        cache: dict[str, object],
        history_snapshot: dict[str, object],
    ) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
        return (
            {
                audit.candidate_id(row): {
                    "candidate_id": audit.candidate_id(row),
                    "normalized_regime": "bull",
                    "factor_assignment_available": True,
                    "factor_error": None,
                    audit.PRIMARY_FACTOR: 0.01,
                }
                for row in sources
            },
            {
                "replay_history_coverage_failures": [],
                "qfq_none_window_consistency_failures": [],
                "history_input_failures": [],
            },
        )

    monkeypatch.setattr(audit, "_factor_features_for_sources", fake_features)
    monkeypatch.setattr(audit, "_assert_history_safe", lambda meta: None)

    def fake_snapshot_changes(
        initial: dict[str, dict[str, str]],
    ) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
        return initial, next(schedule)

    monkeypatch.setattr(audit, "_snapshot_changes", fake_snapshot_changes)

    def fake_replay(
        sources: list[dict[str, object]],
        config: dict[str, object],
        profile: str,
        history_dir: Path,
    ) -> tuple[list[dict[str, object]], Counter, dict[str, object]]:
        assert profile == "production_risk"
        assert history_dir == audit.HISTORY_DIR
        calls["profiles"].append(profile)  # type: ignore[union-attr]
        rows = []
        for source in sources:
            row = {
                **source,
                "candidate_id": audit.candidate_id(source),
                **{field: 1.0 for field in audit.FIELDS},
            }
            rows.append(row)
        return rows, Counter(), _valid_source_match(len(sources))

    monkeypatch.setattr(audit, "_production_replay_split", fake_replay)

    def fake_candidate_report(
        rows: list[dict[str, object]],
        source_match: dict[str, object],
        replay_integrity: dict[str, object],
        factor_meta: dict[str, object],
        seed: int,
    ) -> dict[str, object]:
        calls["candidate_report_seeds"].append(seed)  # type: ignore[union-attr]
        return {
            "source_match": source_match,
            "gate": {
                "eligible_for_cross_split": True,
                "primary_bootstrap_contract_ok": True,
                "primary_cluster_bootstrap_ci95_positive": True,
            },
            "cluster_bootstrap_low_minus_high": {
                "future_40d": {"mean_delta": 1.0},
                "trade_pnl_pct": {"mean_delta": 1.0},
            },
        }

    monkeypatch.setattr(audit, "_candidate_report", fake_candidate_report)

    def fake_publish(*args: object, **kwargs: object) -> None:
        calls["published"] = int(calls["published"]) + 1

    monkeypatch.setattr(audit, "_write_outputs_atomically", fake_publish)
    return calls


def test_mocked_run_uses_three_production_replays_one_fixed_seed_and_three_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = _install_mocked_run_harness(monkeypatch, tmp_path)

    result = audit.run(_args(output_dir=str(tmp_path / "formal")))

    assert calls["profiles"] == ["production_risk"] * 3
    assert calls["candidate_report_seeds"] == [audit.PREREGISTERED_SEED] * 3
    assert calls["published"] == 1
    assert result["baseline_replay_complete"] is True
    assert result["pre_replay_input_stability"]["all_unchanged"] is True
    assert result["protected_inputs"]["all_unchanged"] is True


def test_mocked_run_pre_replay_mutation_prevents_replay_and_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = _install_mocked_run_harness(
        monkeypatch,
        tmp_path,
        snapshot_change_schedule=[
            [{"name": "config", "before": "a", "after": "b"}],
            [],
        ],
    )

    with pytest.raises(RuntimeError, match="changed before replay"):
        audit.run(_args(output_dir=str(tmp_path / "formal")))

    assert calls["profiles"] == []
    assert calls["published"] == 0


def test_mocked_run_post_replay_mutation_prevents_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = _install_mocked_run_harness(
        monkeypatch,
        tmp_path,
        snapshot_change_schedule=[
            [],
            [],
            [{"name": "config", "before": "a", "after": "b"}],
            [],
        ],
    )

    with pytest.raises(RuntimeError, match="changed during audit"):
        audit.run(_args(output_dir=str(tmp_path / "formal")))

    assert calls["profiles"] == ["production_risk"] * 3
    assert calls["published"] == 0


def test_diagnostic_factor_and_regime_cannot_rescue_primary_gate(monkeypatch):
    monkeypatch.setattr(audit, "MIN_AFFECTED_CANDIDATES", 2)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SYMBOLS", 2)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SIGNAL_DAYS", 1)
    monkeypatch.setattr(audit, "MIN_OUTCOME_CLUSTERS", 2)
    monkeypatch.setattr(audit, "PREREGISTERED_BOOTSTRAP_REPS", 10)
    rows = [
        _row("000001", "2026-03-01", 0.01, -1.0),
        _row("000002", "2026-03-01", 0.04, 2.0),
    ]
    for row in rows:
        row.update(
            {
                "large_down_gap_frequency_60": 1.0 - float(row[audit.PRIMARY_FACTOR]),
                "worst_down_gap_60": 1.0 - float(row[audit.PRIMARY_FACTOR]),
                "none_downside_gap_semideviation_60": 1.0,
                "open_limit_down_proxy_frequency_60": 1.0,
                "post_large_up_day_chase_gap_mean_60": 1.0,
            }
        )
    audit._assign_primary_halves(rows)
    replay_integrity = {"all_pass": True}
    factor_meta = {"diagnostic_unavailable_counts": {}}

    report = audit._candidate_report(
        rows,
        _valid_source_match(2),
        replay_integrity,
        factor_meta,
        seed=1,
    )

    assert report["gate"]["primary_direction_positive"] is False
    assert report["gate"]["primary_cluster_bootstrap_ci95_positive"] is False
