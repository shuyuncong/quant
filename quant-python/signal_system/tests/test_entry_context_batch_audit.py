from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import entry_context_batch_audit as audit


def _source(
    symbol: str = "000001",
    day: str = "2026-04-01",
    signal_type: str = "macd_golden_cross_pullback_confirmed_above",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "signal_day": day,
        "signal_type": signal_type,
        "regime": "bull",
    }


def _row(
    symbol: str,
    day: str,
    nearer: bool | None,
    outcome: float | None,
    *,
    signal_type: str = "macd_golden_cross_pullback_confirmed_above",
) -> dict[str, object]:
    row = {
        **_source(symbol, day, signal_type),
        "candidate_id": f"{symbol}|{day}|{signal_type}",
        audit.PRIMARY_FACTOR: 0.1 if nearer else 0.8,
        "zero_axis_factor_assignment_available": True,
        "zero_axis_nearer": nearer,
        "zero_axis_stratum": f"{day}|{signal_type}",
        "entry_day": day,
        "exit_reason": "stop_loss",
        "holding_days": 5,
    }
    for field in audit.FIELDS:
        row[field] = outcome
    return row


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
        "preflight_only": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _valid_source_match(count: int) -> dict[str, object]:
    return {
        "source_rows": count,
        "common_eligible_rows": count,
        "simulated_rows": count,
    }


def test_contract_constants_are_frozen():
    assert audit.VERSION == "entry_context_batch_audit.v1"
    assert audit.PRIMARY_FACTOR == "macd_zero_axis_distance"
    assert audit.MACD_PARAMETERS == {"fast": 12, "slow": 26, "signal": 9}
    assert audit.MIN_MACD_BARS == 34
    assert audit.PREREGISTERED_SEED == 20260908
    assert audit.PREREGISTERED_BOOTSTRAP_REPS == 2000
    assert audit.MIN_ASSIGNMENT_COVERAGE == pytest.approx(0.90)
    assert audit.MIN_AFFECTED_CANDIDATES == 30
    assert audit.MIN_AFFECTED_SYMBOLS == 10
    assert audit.MIN_AFFECTED_SIGNAL_DAYS == 10
    assert audit.STUDY_REGISTRY["strategy_stability_diagnostic"]["role"] == (
        "safety_diagnostic_only"
    )
    assert audit.STUDY_REGISTRY["zero_axis_distance_candidate"]["role"] == (
        "registered_historical_candidate"
    )


@pytest.mark.parametrize("value", ["1", "00001", "0000001", "ABC001", 1])
def test_symbol_must_be_full_six_digits(value: object):
    with pytest.raises(RuntimeError, match="six digits"):
        audit._normalize_symbol(value)


@pytest.mark.parametrize("value", ["2026-1-01", "2026/01/01", "", None])
def test_signal_day_must_be_canonical_iso(value: object):
    with pytest.raises(RuntimeError, match="signal_day"):
        audit._normalize_signal_day(value)


def test_duplicate_symbol_day_fails_even_when_signal_type_differs():
    rows = [
        _source("000001", "2026-04-01"),
        _source("000001", "2026-04-01", "buy_1"),
    ]
    with pytest.raises(RuntimeError, match="symbol x signal_day"):
        audit._validate_symbol_day_uniqueness(rows)


def test_replay_projection_strips_every_legacy_outcome():
    source = {
        **_source(),
        **{field: object() for field in audit.FIELDS},
        "entry_day": object(),
        "exit_reason": object(),
        "holding_days": object(),
    }
    projected = audit._replay_input_projection(source)
    assert not (set(projected) & audit.SOURCE_LEGACY_OUTCOME_FIELDS)
    assert projected["symbol"] == "000001"


def test_zero_axis_distance_matches_existing_p5a_formula():
    close = pd.Series(np.linspace(10.0, 20.0, 80))
    macd = audit.calculate_macd(close, **audit.MACD_PARAMETERS)
    expected = abs(float(macd["dif"].iloc[-1])) / float(close.iloc[-1])
    assert audit._zero_axis_distance(close) == pytest.approx(expected)


def test_zero_axis_distance_is_scale_invariant_and_suffix_safe():
    prefix = pd.Series(np.linspace(8.0, 15.0, 80))
    expected = audit._zero_axis_distance(prefix)
    assert audit._zero_axis_distance(prefix * 37.0) == pytest.approx(expected)
    suffix = pd.concat([prefix, pd.Series([1e6, 1e-6])], ignore_index=True)
    assert audit._zero_axis_distance(suffix.iloc[: len(prefix)]) == pytest.approx(
        expected
    )


def test_zero_axis_distance_requires_34_positive_finite_bars():
    with pytest.raises(RuntimeError, match="required=34"):
        audit._zero_axis_distance(pd.Series(np.linspace(10.0, 11.0, 33)))
    assert audit._zero_axis_distance(
        pd.Series(np.linspace(10.0, 11.0, 34))
    ) >= 0
    bad = pd.Series(np.linspace(10.0, 11.0, 34))
    bad.iloc[10] = np.nan
    with pytest.raises(RuntimeError, match="non-finite"):
        audit._zero_axis_distance(bad)
    bad.iloc[10] = 0.0
    with pytest.raises(RuntimeError, match="strictly positive"):
        audit._zero_axis_distance(bad)


def test_zero_axis_features_exclude_buy1_and_group_within_signal_type(
    monkeypatch: pytest.MonkeyPatch,
):
    day = "2026-04-01"
    sources = [
        _source("000001", day),
        _source("000002", day),
        _source("000003", day, "macd_golden_cross_pullback_confirmed_near"),
        _source("000004", day, "macd_golden_cross_pullback_confirmed_near"),
        _source("000005", day, "buy_1"),
    ]
    cache = {}
    for index, symbol in enumerate(
        ("000001", "000002", "000003", "000004"), start=1
    ):
        zone = "above" if index <= 2 else "near"
        cache[symbol] = {
            "closed": pd.DataFrame(
                {"datetime": [pd.Timestamp(day)], "close": [float(index)]}
            ),
            "entry_map": {(0, zone): [{"cross_index": 0}]},
        }
    monkeypatch.setattr(
        audit, "_zero_axis_distance", lambda values: float(values.iloc[-1])
    )
    features, meta = audit._zero_axis_features_for_sources(sources, cache)
    buy = features[audit.candidate_id(sources[-1])]
    assert buy[audit.PRIMARY_FACTOR] is None
    assert buy["zero_axis_factor_error"] == "non_macd_signal"
    above = [features[audit.candidate_id(row)] for row in sources[:2]]
    near = [features[audit.candidate_id(row)] for row in sources[2:4]]
    assert [row["zero_axis_nearer"] for row in above] == [True, False]
    assert [row["zero_axis_nearer"] for row in near] == [True, False]
    assert above[0]["zero_axis_stratum"] != near[0]["zero_axis_stratum"]
    assert meta["macd_candidates"] == 4
    assert meta["assigned_candidates"] == 4
    assert meta["comparable_candidates"] == 4


def test_all_ties_or_singleton_strata_are_not_comparable(
    monkeypatch: pytest.MonkeyPatch,
):
    sources = [
        _source("000001"),
        _source("000002"),
        _source("000003", signal_type="macd_golden_cross_pullback_confirmed_near"),
    ]
    cache = {
        "000001": {
            "closed": pd.DataFrame(
                {"datetime": [pd.Timestamp("2026-04-01")], "close": [1.0]}
            ),
            "entry_map": {(0, "above"): [{"cross_index": 0}]},
        },
        "000002": {
            "closed": pd.DataFrame(
                {"datetime": [pd.Timestamp("2026-04-01")], "close": [1.0]}
            ),
            "entry_map": {(0, "above"): [{"cross_index": 0}]},
        },
        "000003": {
            "closed": pd.DataFrame(
                {"datetime": [pd.Timestamp("2026-04-01")], "close": [1.0]}
            ),
            "entry_map": {(0, "near"): [{"cross_index": 0}]},
        },
    }
    monkeypatch.setattr(audit, "_zero_axis_distance", lambda values: 0.5)
    features, meta = audit._zero_axis_features_for_sources(sources, cache)
    assert all(item["zero_axis_nearer"] is None for item in features.values())
    assert meta["comparable_candidates"] == 0
    assert meta["excluded_singleton_or_tied_strata"] == 2


def test_factor_input_projection_never_receives_legacy_outcomes():
    source = {
        **_source(),
        **{field: "forbidden" for field in audit.FIELDS},
        "entry_day": "forbidden",
        "exit_reason": "forbidden",
        "holding_days": "forbidden",
    }
    projected = audit._factor_input_projection(source)
    assert projected == {
        "symbol": "000001",
        "signal_day": "2026-04-01",
        "signal_type": "macd_golden_cross_pullback_confirmed_above",
    }
    assert not (set(projected) & audit.SOURCE_LEGACY_OUTCOME_FIELDS)


def test_entry_provenance_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    source = _source()
    cache = {
        "000001": {
            "closed": pd.DataFrame(
                {"datetime": [pd.Timestamp("2026-04-01")], "close": [10.0]}
            ),
            "entry_map": {(0, "near"): [{"cross_index": 0}]},
        }
    }
    monkeypatch.setattr(audit, "_zero_axis_distance", lambda values: 0.1)
    with pytest.raises(RuntimeError, match="entry_provenance_mismatch"):
        audit._zero_axis_features_for_sources([source], cache)


def test_feature_builder_uses_full_prefix_and_ignores_real_future_suffix(
    monkeypatch: pytest.MonkeyPatch,
):
    dates = pd.bdate_range("2026-01-01", periods=82)
    signal_day = dates[79].date().isoformat()
    prefix = np.linspace(10.0, 20.0, 80)
    original = audit.calculate_macd
    lengths: list[int] = []

    def capture_length(values, **kwargs):
        lengths.append(len(values))
        return original(values, **kwargs)

    monkeypatch.setattr(audit, "calculate_macd", capture_length)

    def feature_for_suffix(symbol: str, suffix: list[float]):
        closed = pd.DataFrame(
            {"datetime": dates, "close": np.concatenate([prefix, suffix])}
        )
        cache = {
            symbol: {
                "closed": closed,
                "entry_map": {(79, "above"): [{"cross_index": 70}]},
            }
        }
        source = _source(symbol, signal_day)
        features, _ = audit._zero_axis_features_for_sources([source], cache)
        return features[audit.candidate_id(source)][audit.PRIMARY_FACTOR]

    first = feature_for_suffix("000001", [1e6, 1e-6])
    second = feature_for_suffix("000002", [1e-6, 1e6])
    assert first == pytest.approx(second)
    assert lengths == [80, 80]


def test_duplicate_entry_provenance_selects_latest_cross(
    monkeypatch: pytest.MonkeyPatch,
):
    day = "2026-04-01"
    source = _source(day=day)
    cache = {
        "000001": {
            "closed": pd.DataFrame(
                {
                    "datetime": pd.bdate_range(end=day, periods=40),
                    "close": np.linspace(10.0, 11.0, 40),
                }
            ),
            "entry_map": {
                (39, "above"): [{"cross_index": 30}, {"cross_index": 35}]
            },
        }
    }
    monkeypatch.setattr(audit, "_zero_axis_distance", lambda values: 0.1)
    features, meta = audit._zero_axis_features_for_sources([source], cache)
    feature = features[audit.candidate_id(source)]
    assert feature["zero_axis_matching_entry_count"] == 2
    assert feature["zero_axis_selected_cross_index"] == 35
    assert meta["ambiguous_same_day_entry_candidates"] == 1


def test_feature_manifest_detects_factor_or_label_mutation():
    feature_map = {
        "000001|2026-04-01|buy_1": {
            "candidate_id": "000001|2026-04-01|buy_1",
            audit.PRIMARY_FACTOR: 0.1,
            "zero_axis_nearer": True,
        }
    }
    expected = audit._feature_manifest(feature_map)
    audit._assert_feature_manifest(feature_map, expected, "test")
    feature_map["000001|2026-04-01|buy_1"]["zero_axis_nearer"] = False
    with pytest.raises(RuntimeError, match="feature manifest changed"):
        audit._assert_feature_manifest(feature_map, expected, "test")


def test_study_projections_are_scalar_allowlists_without_nested_aliases():
    row = _row("000001", "2026-04-01", True, 1.0)
    row["confirmation_items"] = [{"mutable": True}]
    row["_p5a_features"] = {"mutable": True}
    row["regime"] = {"unexpected": ["mutable"]}
    strategy = audit._strategy_study_projection(row)
    zero = audit._zero_axis_study_projection(row)
    assert "confirmation_items" not in strategy
    assert "_p5a_features" not in strategy
    assert "confirmation_items" not in zero
    assert "_p5a_features" not in zero
    strategy["regime"]["unexpected"].append("changed")
    assert row["regime"] == {"unexpected": ["mutable"]}


def test_ineligible_artifact_projection_never_pairs_zero_factor_with_outcomes():
    row = _row("000001", "2026-04-01", True, 1.0)
    projected = audit._artifact_row_projection(
        row, publish_zero_axis_pairing=False
    )
    assert audit.PRIMARY_FACTOR not in projected
    assert "zero_axis_nearer" not in projected
    assert "zero_axis_stratum" not in projected
    assert all(outcome in projected for outcome in audit.PRIMARY_OUTCOMES)


def test_valid_rows_keep_only_strata_with_both_groups():
    rows = [
        _row("000001", "2026-04-01", True, 2.0),
        _row("000002", "2026-04-01", False, 0.0),
        _row("000003", "2026-04-02", True, 1.0),
        _row("000004", "2026-04-03", True, None),
        _row("000005", "2026-04-03", False, 0.0),
    ]
    valid = audit._valid_zero_axis_rows(rows, "future_40d")
    assert [row["symbol"] for row in valid] == ["000001", "000002"]


def test_fixed_effect_beta_controls_stratum_not_pooled_mean():
    rows = [
        _row("000001", "2026-04-01", True, 10.0),
        _row("000002", "2026-04-01", False, 8.0),
        _row("000003", "2026-04-02", True, -8.0),
        _row("000004", "2026-04-02", False, -10.0),
    ]
    valid = audit._valid_zero_axis_rows(rows, "future_40d")
    assert audit._stratum_fixed_effect_beta(valid, "future_40d") == pytest.approx(
        2.0
    )


def test_cluster_bootstrap_is_deterministic_and_order_invariant():
    rows = []
    for day_index in range(4):
        day = f"2026-04-{day_index + 1:02d}"
        rows.extend(
            [
                _row(f"{day_index * 2 + 1:06d}", day, True, 2.0),
                _row(f"{day_index * 2 + 2:06d}", day, False, 0.0),
            ]
        )
    first = audit._cluster_bootstrap_zero_axis(
        rows, "future_40d", reps=100, seed=17
    )
    second = audit._cluster_bootstrap_zero_axis(
        list(reversed(rows)), "future_40d", reps=100, seed=17
    )
    assert first == second
    assert first["fixed_effect_beta"] == pytest.approx(2.0)
    assert 0 < first["reps_valid"] <= 100


def test_symbol_cluster_weight_applies_to_same_symbol_across_strata():
    rows = [
        _row("000001", "2026-04-01", True, 2.0),
        _row("000002", "2026-04-01", False, 0.0),
        _row("000001", "2026-04-02", True, 4.0),
        _row("000003", "2026-04-02", False, 0.0),
    ]
    valid = audit._valid_zero_axis_rows(rows, "future_40d")
    unweighted = audit._stratum_fixed_effect_beta(valid, "future_40d")
    weighted = audit._stratum_fixed_effect_beta(
        valid, "future_40d", {"000001": 3, "000002": 1, "000003": 1}
    )
    assert unweighted == pytest.approx(3.0)
    assert weighted == pytest.approx(3.0)


def test_bootstrap_contract_requires_all_reps_and_ten_clusters():
    valid = {
        "fixed_effect_beta": 1.0,
        "reps_requested": audit.PREREGISTERED_BOOTSTRAP_REPS,
        "reps_valid": audit.PREREGISTERED_BOOTSTRAP_REPS,
        "cluster_count": 10,
    }
    assert audit._bootstrap_contract_ok(valid) is True
    assert audit._bootstrap_contract_ok({**valid, "reps_valid": 1999}) is False
    assert audit._bootstrap_contract_ok({**valid, "cluster_count": 9}) is False


def test_authoritative_outcomes_require_all_fields_numeric_and_finite():
    row = _row("000001", "2026-04-01", True, 1.0)
    audit._validate_authoritative_outcomes([row])
    missing = dict(row)
    missing.pop("future_5d")
    with pytest.raises(RuntimeError, match="field is missing"):
        audit._validate_authoritative_outcomes([missing])
    nonnumeric = dict(row)
    nonnumeric["future_5d"] = "bad"
    with pytest.raises(TypeError, match="not numeric"):
        audit._validate_authoritative_outcomes([nonnumeric])
    nonfinite = dict(row)
    nonfinite["future_5d"] = float("inf")
    with pytest.raises(RuntimeError, match="not finite"):
        audit._validate_authoritative_outcomes([nonfinite])


def test_replay_integrity_rejects_order_skips_and_feature_mismatch():
    sources = [_source("000001"), _source("000002", "2026-04-02")]
    feature_map = {
        audit.candidate_id(row): {"candidate_id": audit.candidate_id(row)}
        for row in sources
    }
    replay = []
    for source in sources:
        row = {**source, "candidate_id": audit.candidate_id(source)}
        row.update({field: 1.0 for field in audit.FIELDS})
        replay.append(row)
    checks, _ = audit._assert_replay_integrity(
        sources, feature_map, replay, Counter(), _valid_source_match(2)
    )
    assert checks["all_pass"] is True
    with pytest.raises(RuntimeError, match="integrity"):
        audit._assert_replay_integrity(
            sources,
            feature_map,
            list(reversed(replay)),
            Counter(),
            _valid_source_match(2),
        )
    with pytest.raises(RuntimeError, match="integrity"):
        audit._assert_replay_integrity(
            sources,
            feature_map,
            replay,
            Counter({"missing": 1}),
            _valid_source_match(2),
        )
    with pytest.raises(RuntimeError, match="integrity"):
        audit._assert_replay_integrity(
            sources,
            {next(iter(feature_map)): next(iter(feature_map.values()))},
            replay,
            Counter(),
            _valid_source_match(2),
        )


def test_outcome_sample_gate_reapplies_thresholds_after_filter(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(audit, "MIN_AFFECTED_CANDIDATES", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SYMBOLS", 4)
    monkeypatch.setattr(audit, "MIN_AFFECTED_SIGNAL_DAYS", 2)
    rows = [
        _row("000001", "2026-04-01", True, 1.0),
        _row("000002", "2026-04-01", False, 0.0),
        _row("000003", "2026-04-02", True, 1.0),
        _row("000004", "2026-04-02", False, None),
    ]
    report = audit._zero_axis_outcome_sample_gate(rows, "future_40d")
    assert report["valid_signal_days"] == 1
    assert report["sufficient"] is False


def test_cross_split_gate_requires_every_split_eligible_and_supported():
    reports = {}
    for split in audit.SPLITS:
        reports[split] = {
            "gate": {
                "eligible_for_cross_split": True,
                "primary_bootstrap_contract_ok": True,
                "primary_cluster_bootstrap_ci95_positive": True,
            },
            "fixed_effect_cluster_bootstrap_nearer_minus_farther": {
                outcome: {"fixed_effect_beta": 1.0}
                for outcome in audit.PRIMARY_OUTCOMES
            },
        }
    assert audit._zero_axis_cross_split_gate(reports)["pass"] is True
    reports["train"]["gate"]["eligible_for_cross_split"] = False
    assert audit._zero_axis_cross_split_gate(reports)["pass"] is False


def test_strategy_stability_is_diagnostic_only_and_reports_insufficient_overlap():
    replay = {
        split: [
            _row(f"{index + 1:06d}", "2026-04-01", True, 1.0)
            for index in range(2)
        ]
        for split in audit.SPLITS
    }
    report, cells = audit._strategy_stability_report(replay)
    assert report["status"] == "insufficient_overlap"
    assert report["diagnostic_completeness_gate"]["all_pass"] is True
    assert cells
    forbidden = {
        "candidate_gate",
        "production_gate",
        "best_cell",
        "recommended_filter",
        "significance_ranking",
    }
    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value)) if value else set()
        return set()

    assert not (keys(report) & forbidden)
    assert report["production_decision_authority"] is False
    assert report["can_rescue_other_study"] is False


def test_strategy_stability_rejects_unknown_signal_or_exit_category():
    row = _row("000001", "2026-04-01", True, 1.0)
    row["signal_type"] = "unregistered"
    replay = {split: [dict(row)] for split in audit.SPLITS}
    with pytest.raises(RuntimeError, match="completeness"):
        audit._strategy_stability_report(replay)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"splits": ["train", "test", "val"]}, "required splits"),
        ({"label": "changed"}, "label"),
        ({"seed": 1}, "seed"),
        ({"bootstrap_reps": 10}, "bootstrap reps"),
        ({"expected_script_sha256": "0" * 64}, "script SHA"),
    ],
)
def test_preregistered_args_fail_closed(
    override: dict[str, object], message: str
):
    with pytest.raises(RuntimeError, match=message):
        audit._validate_preregistered_args(_args(**override))


def test_output_directory_refuses_final_or_staging(tmp_path: Path):
    final = tmp_path / "formal"
    final.mkdir()
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_directory_unused(final)
    final.rmdir()
    (tmp_path / "formal.tmp").mkdir()
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_directory_unused(final)


def test_atomic_writer_publishes_five_files_and_records_hashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    final = tmp_path / "formal"
    staging = tmp_path / "formal.tmp"
    rows = {split: [_row("000001", "2026-04-01", True, 1.0)] for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}
    initial = {"x": {"path": str(tmp_path / "x"), "size_bytes": 1, "sha256": "h"}}
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: (value, []))
    monkeypatch.setattr(audit, "_snapshot_manifest", lambda value: "manifest")
    manifests = {
        split: audit._enriched_feature_manifest(rows[split])
        for split in audit.SPLITS
    }
    audit._write_outputs_atomically(
        final,
        staging,
        rows,
        [{"cell": 1}],
        result,
        initial,
        initial,
        manifests,
        False,
    )
    assert final.is_dir()
    assert not staging.exists()
    assert len(list(final.iterdir())) == 5
    published = json.loads(
        (final / "entry_context_batch_train.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert audit.PRIMARY_FACTOR not in published
    assert "zero_axis_nearer" not in published
    assert all(outcome in published for outcome in audit.PRIMARY_OUTCOMES)
    assert result["artifacts"]["strategy_stability_cells"]["rows"] == 1
    assert result["pre_publish_input_stability"]["all_unchanged"] is True


def test_atomic_writer_keeps_staging_on_prepublish_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    final = tmp_path / "formal"
    staging = tmp_path / "formal.tmp"
    rows = {split: [_row("000001", "2026-04-01", True, 1.0)] for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}
    initial = {"x": {"path": str(tmp_path / "x"), "size_bytes": 1, "sha256": "h"}}
    calls = iter([(initial, [{"name": "x"}]), (initial, [])])
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: next(calls))
    manifests = {
        split: audit._enriched_feature_manifest(rows[split])
        for split in audit.SPLITS
    }
    with pytest.raises(RuntimeError, match="changed before publish"):
        audit._write_outputs_atomically(
            final,
            staging,
            rows,
            [],
            result,
            initial,
            initial,
            manifests,
            False,
        )
    assert staging.is_dir()
    assert not final.exists()


def test_atomic_writer_refuses_final_that_appears_before_rename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    final = tmp_path / "formal"
    staging = tmp_path / "formal.tmp"
    rows = {split: [_row("000001", "2026-04-01", True, 1.0)] for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}
    initial = {"x": {"path": str(tmp_path / "x"), "size_bytes": 1, "sha256": "h"}}
    call_count = 0

    def unchanged_then_create_final(value):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            final.mkdir()
        return value, []

    monkeypatch.setattr(audit, "_snapshot_changes", unchanged_then_create_final)
    monkeypatch.setattr(audit, "_snapshot_manifest", lambda value: "manifest")
    manifests = {
        split: audit._enriched_feature_manifest(rows[split])
        for split in audit.SPLITS
    }
    with pytest.raises(RuntimeError, match="appeared before publish"):
        audit._write_outputs_atomically(
            final,
            staging,
            rows,
            [],
            result,
            initial,
            initial,
            manifests,
            False,
        )
    assert staging.is_dir()
    assert final.is_dir()


def _mocked_prepare(tmp_path: Path) -> dict[str, object]:
    sources_by_split = {
        split: [
            {
                **_source(f"{index + 1:06d}", f"2026-04-0{index + 1}"),
                **{field: "legacy-malformed" for field in audit.FIELDS},
                "entry_day": "legacy-malformed",
                "exit_reason": object(),
                "holding_days": "legacy-malformed",
            }
        ]
        for index, split in enumerate(audit.SPLITS)
    }
    features_by_split = {
        split: {
            audit.candidate_id(row): {
                "candidate_id": audit.candidate_id(row),
                audit.PRIMARY_FACTOR: 0.1,
                "zero_axis_factor_assignment_available": True,
                "zero_axis_factor_error": None,
                "zero_axis_nearer": True,
                "zero_axis_stratum": f"{row['signal_day']}|{row['signal_type']}",
                "zero_axis_stratum_median": 0.1,
            }
            for row in rows
        }
        for split, rows in sources_by_split.items()
    }
    factor_meta_by_split = {
        split: {
            "feature_manifest_sha256": audit._feature_manifest(
                features_by_split[split]
            ),
            "assignment_coverage": 1.0,
            "comparable_candidates": 1,
            "comparable_symbols": 1,
            "comparable_signal_days": 1,
        }
        for split in audit.SPLITS
    }
    static = {
        "audit_script": {"path": "script", "size_bytes": 1, "sha256": "script"},
        "config": {"path": "config", "size_bytes": 1, "sha256": "config"},
        **{
            f"canonical_{split}": {
                "path": split,
                "size_bytes": 1,
                "sha256": split,
            }
            for split in audit.SPLITS
        },
    }
    return {
        "input_dir": tmp_path / "input",
        "output_dir": tmp_path / "formal",
        "config_path": tmp_path / "config.yaml",
        "staging_dir": tmp_path / "formal.tmp",
        "static_initial": static,
        "history_initial": {
            "qfq:000001": {"path": "history", "size_bytes": 1, "sha256": "h"}
        },
        "dependency_contract": {"all": {"match": True}},
        "config": {},
        "sources_by_split": sources_by_split,
        "integrity_by_split": {split: {"all_pass": True} for split in audit.SPLITS},
        "history_inventory": {
            "symbol_count": 3,
            "file_count": 3,
            "history_manifest_sha256": "schema",
            "all_valid": True,
        },
        "frozen_history_manifest": audit.PREREGISTERED_HISTORY_MANIFEST_SHA256,
        "signal_day_coverage": {"all_pass": True},
        "features_by_split": features_by_split,
        "factor_meta_by_split": factor_meta_by_split,
    }


def _install_run_execution_mocks(
    monkeypatch: pytest.MonkeyPatch, prepared: dict[str, object]
) -> tuple[list[str], list[bool]]:
    calls: list[str] = []
    published: list[bool] = []
    monkeypatch.setattr(audit, "_prepare", lambda args: prepared)
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: (value, []))
    monkeypatch.setattr(audit, "_snapshot_manifest", lambda value: "manifest")

    def fake_replay(sources, config, profile, history_dir):
        assert profile == "production_risk"
        assert all(not (set(row) & audit.SOURCE_LEGACY_OUTCOME_FIELDS) for row in sources)
        calls.append(profile)
        replay = []
        for source in sources:
            row = {**source, "candidate_id": audit.candidate_id(source)}
            row.update({field: 1.0 for field in audit.FIELDS})
            row.update({"entry_day": source["signal_day"], "exit_reason": "stop_loss", "holding_days": 5})
            replay.append(row)
        return replay, Counter(), _valid_source_match(len(sources))

    monkeypatch.setattr(audit, "_production_replay_split", fake_replay)
    monkeypatch.setattr(
        audit,
        "_strategy_stability_report",
        lambda rows: (
            {
                **audit.STUDY_REGISTRY["strategy_stability_diagnostic"],
                "diagnostic_completeness_gate": {"all_pass": True},
                "status": "insufficient_overlap",
            },
            [],
        ),
    )

    monkeypatch.setattr(
        audit,
        "_zero_axis_split_report",
        lambda *args, **kwargs: pytest.fail(
            "ineligible zero-axis study must not access outcomes"
        ),
    )
    monkeypatch.setattr(
        audit,
        "_write_outputs_atomically",
        lambda *args, **kwargs: published.append(True),
    )
    return calls, published


def test_mocked_run_uses_exactly_one_replay_per_split_and_isolates_studies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    prepared = _mocked_prepare(tmp_path)
    calls, published = _install_run_execution_mocks(monkeypatch, prepared)
    result = audit.run(_args(output_dir=str(tmp_path / "formal")))
    assert calls == ["production_risk"] * 3
    assert result["replay_call_count_actual"] == 3
    assert result["studies"]["strategy_stability_diagnostic"]["status"] == (
        "insufficient_overlap"
    )
    zero = result["studies"]["zero_axis_distance_candidate"]
    assert zero["preflight_eligibility"]["status"] == "ineligible_preflight"
    assert zero["verdict"] == "sample_insufficient"
    assert zero["candidate_gate"]["pass"] is False
    assert zero["candidate_level_factor_outcome_pairing_published"] is False
    assert published == [True]
    assert result["production_decision"] == "unchanged_P0"


def test_run_rejects_feature_manifest_change_before_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    prepared = _mocked_prepare(tmp_path)
    prepared["features_by_split"]["train"][
        next(iter(prepared["features_by_split"]["train"]))
    ][audit.PRIMARY_FACTOR] = 999.0
    monkeypatch.setattr(audit, "_prepare", lambda args: prepared)
    monkeypatch.setattr(
        audit,
        "_production_replay_split",
        lambda *args, **kwargs: pytest.fail("replay must not start"),
    )
    with pytest.raises(RuntimeError, match="feature manifest changed at pre_replay"):
        audit.run(_args(output_dir=str(tmp_path / "formal")))


def test_run_stops_on_pre_replay_input_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    prepared = _mocked_prepare(tmp_path)
    calls, published = _install_run_execution_mocks(monkeypatch, prepared)
    schedule = iter(
        [
            (prepared["static_initial"], [{"name": "config"}]),
            (prepared["history_initial"], []),
        ]
    )
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: next(schedule))
    with pytest.raises(RuntimeError, match="changed before replay"):
        audit.run(_args(output_dir=str(tmp_path / "formal")))
    assert calls == []
    assert published == []


def test_run_stops_on_post_replay_input_mutation_before_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    prepared = _mocked_prepare(tmp_path)
    calls, published = _install_run_execution_mocks(monkeypatch, prepared)
    schedule = iter(
        [
            (prepared["static_initial"], []),
            (prepared["history_initial"], []),
            (prepared["static_initial"], [{"name": "config"}]),
            (prepared["history_initial"], []),
        ]
    )
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: next(schedule))
    with pytest.raises(RuntimeError, match="changed during audit"):
        audit.run(_args(output_dir=str(tmp_path / "formal")))
    assert calls == ["production_risk"] * 3
    assert published == []


@pytest.mark.skipif(
    not audit.CANONICAL_INPUT_DIR.exists()
    or audit.FORMAL_OUTPUT_DIR.exists()
    or audit.FORMAL_OUTPUT_DIR.with_name(
        audit.FORMAL_OUTPUT_DIR.name + ".tmp"
    ).exists(),
    reason="frozen local preflight inputs or unused formal path unavailable",
)
def test_real_outcome_free_preflight_freezes_zero_axis_feasibility_counts():
    report = audit.preflight(_args(preflight_only=True))
    assert report["technical_preflight_pass"] is True
    assert report["replay_performed"] is False
    assert report["output_generated"] is False
    assert report["canonical_rows"] == {"train": 89, "val": 1107, "test": 346}
    assert report["history_symbol_count"] == 809
    assert report["history_file_count"] == 809
    assert report["history_manifest_sha256"] == (
        audit.PREREGISTERED_HISTORY_MANIFEST_SHA256
    )
    expected = {
        "train": (47, 47, 43, 42, 7, 24, 19),
        "val": (1043, 1043, 1024, 640, 91, 547, 477),
        "test": (268, 268, 255, 226, 28, 140, 115),
    }
    for split, values in expected.items():
        item = report["zero_axis_outcome_free_feasibility"][split]
        assert (
            item["macd_candidates"],
            item["assigned_candidates"],
            item["comparable_candidates"],
            item["comparable_symbols"],
            item["comparable_signal_days"],
            item["nearer_n"],
            item["farther_n"],
        ) == values
    assert report["zero_axis_all_splits_sample_eligible"] is False
    assert report["formal_run_allowed_for_strategy_diagnostic"] is True
    assert report["formal_run_can_make_zero_axis_candidate_pass"] is False
