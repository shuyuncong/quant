from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import atr_volatility_audit as audit  # noqa: E402


def _frame(count: int = 30, start: str = "2026-01-02") -> pd.DataFrame:
    close = [100.0 + index for index in range(count)]
    frame = pd.DataFrame(
        {
            "datetime": pd.bdate_range(start, periods=count),
            "open": close,
            "high": [value + 1.0 for value in close],
            "low": [value - 1.0 for value in close],
            "close": close,
            "volume": [1000.0] * count,
            "amount": [100000.0] * count,
            "is_closed": [True] * count,
        }
    )
    frame.attrs["adjust"] = "qfq"
    return frame


def _source(symbol: str = "000001", day: str = "2026-01-02") -> dict[str, object]:
    return {
        "symbol": symbol,
        "signal_day": day,
        "signal_type": "macd_above",
    }


def _row(symbol: str, day: str, factor: float) -> dict[str, object]:
    return {
        **_source(symbol, day),
        "candidate_id": f"{symbol}|{day}|macd_above",
        "atr20_ratio": factor,
        "downside_semideviation_20": 1.0 - factor,
        "factor_assignment_available": True,
        "future_20d": factor,
        "future_40d": factor,
        "trade_pnl_pct": factor,
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
            "cluster_bootstrap_high_minus_low": {
                "future_40d": {"mean_delta": future_40d},
                "trade_pnl_pct": {"mean_delta": trade_pnl_pct},
            },
        }
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
        "history_symbol_count": 10,
        "history_input_safe": True,
        "history_input_failures": [],
        "replay_history_coverage_safe": True,
        "replay_history_coverage_failures": [],
        "factor_errors": {},
        "diagnostic_unavailable_counts": {"downside_semideviation_20": 0},
    }


def _args(input_dir: Path, output_dir: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "config": str(audit.BASE_DIR / "config" / "config.yaml"),
        "label": audit.PREREGISTERED_LABEL,
        "splits": list(audit.SPLITS),
        "seed": audit.PREREGISTERED_SEED,
        "bootstrap_reps": audit.PREREGISTERED_BOOTSTRAP_REPS,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_primary_factor_direction_and_diagnostic_role_are_fixed():
    assert audit.PRIMARY_FACTOR == "atr20_ratio"
    assert audit.FACTOR_SPECS[audit.PRIMARY_FACTOR] == {
        "larger_is_better": True,
        "role": "primary",
    }
    assert audit.FACTOR_SPECS["downside_semideviation_20"] == {
        "larger_is_better": False,
        "role": "diagnostic",
    }
    assert audit.ATR_TRUE_RANGES == 20
    assert audit.PRIOR_PROBE_TRUE_RANGES == 14


def test_atr20_uses_exact_true_range_formula_and_previous_close_gap():
    frame = _frame(21)
    frame[["open", "high", "low", "close"]] = 100.0
    frame["high"] = 101.0
    frame["low"] = 99.0
    frame.loc[20, ["open", "high", "low", "close"]] = [109.0, 110.0, 108.0, 109.0]

    result = audit._atr_volatility_features(frame, 20)

    expected_atr = (19 * 2.0 + 10.0) / 20.0
    assert result["factor_assignment_available"] is True
    assert result["atr20"] == pytest.approx(expected_atr)
    assert result["atr20_ratio"] == pytest.approx(expected_atr / 109.0)
    assert result["downside_semideviation_20"] == 0.0


def test_downside_semideviation_uses_all_twenty_intervals_as_denominator():
    returns = [-0.10, 0.10] + [0.0] * 18
    closes = [100.0]
    for interval_return in returns:
        closes.append(closes[-1] * (1.0 + interval_return))
    frame = _frame(21)
    frame["close"] = closes
    frame["open"] = closes
    frame["high"] = [value + 1.0 for value in closes]
    frame["low"] = [value - 1.0 for value in closes]

    result = audit._atr_volatility_features(frame, 20)

    assert result["downside_semideviation_20"] == pytest.approx(
        math.sqrt(0.10**2 / 20.0)
    )


def test_features_do_not_use_bars_after_signal_day():
    frame = _frame(30)
    expected = audit._atr_volatility_features(frame, 20)
    modified = frame.copy()
    modified.loc[21:, ["open", "high", "low", "close"]] = 10000.0
    assert audit._atr_volatility_features(modified, 20) == expected


def test_twenty_true_ranges_require_twenty_one_bars():
    result = audit._atr_volatility_features(_frame(20), 19)
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == "insufficient_20_true_range_history"


@pytest.mark.parametrize(
    ("column", "index", "value", "expected_error"),
    [
        ("close", 0, 0.0, "invalid_atr_price_values"),
        ("high", 20, float("nan"), "invalid_atr_price_values"),
        ("close", 20, 0.0, "invalid_signal_close"),
    ],
)
def test_invalid_factor_prices_are_rejected(
    column: str, index: int, value: float, expected_error: str
):
    frame = _frame(21)
    frame.loc[index, column] = value
    result = audit._atr_volatility_features(frame, 20)
    assert result["factor_assignment_available"] is False
    assert result["factor_error"] == expected_error


def test_same_day_median_halves_and_ties_are_deterministic():
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

    assert [row["primary_high_atr"] for row in rows[:4]] == [False, True, True, True]
    assert [row["variant_included"] for row in rows[:4]] == [False, True, True, True]
    assert all(row["primary_high_atr"] is None for row in rows[4:])
    assert all(row["variant_included"] is False for row in rows[4:])


def test_valid_history_frame_preserves_qfq_and_closed_rows():
    frame = _frame(22)
    frame.loc[21, "is_closed"] = False
    validated = audit._validate_history_frame(frame, Path("unused.pkl"))
    assert len(validated) == 21
    assert validated.attrs["adjust"] == "qfq"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("non_qfq", "not qfq"),
        ("unsorted", "not sorted"),
        ("duplicate_day", "duplicate trading dates"),
        ("invalid_high", "invalid OHLC"),
        ("invalid_low", "invalid OHLC"),
        ("invalid_datetime", "invalid datetime"),
        ("no_closed", "no closed bars"),
    ],
)
def test_history_validation_fails_closed(mutation: str, message: str):
    frame = _frame(22)
    if mutation == "non_qfq":
        frame.attrs["adjust"] = "none"
    elif mutation == "unsorted":
        frame.loc[[0, 1], "datetime"] = frame.loc[[1, 0], "datetime"].to_numpy()
    elif mutation == "duplicate_day":
        frame.loc[1, "datetime"] = frame.loc[0, "datetime"]
    elif mutation == "invalid_high":
        frame.loc[1, "high"] = frame.loc[1, "close"] - 1.0
    elif mutation == "invalid_low":
        frame.loc[1, "low"] = frame.loc[1, "close"] + 1.0
    elif mutation == "invalid_datetime":
        frame["datetime"] = frame["datetime"].astype(object)
        frame.loc[1, "datetime"] = "not-a-date"
    elif mutation == "no_closed":
        frame["is_closed"] = False
    with pytest.raises(RuntimeError, match=message):
        audit._validate_history_frame(frame, Path("unused.pkl"))


def test_missing_required_history_column_fails_closed():
    with pytest.raises(RuntimeError, match="missing columns"):
        audit._validate_history_frame(_frame().drop(columns=["high"]), Path("unused.pkl"))


def test_missing_is_closed_schema_field_fails_closed():
    with pytest.raises(RuntimeError, match="missing columns"):
        audit._validate_history_frame(
            _frame().drop(columns=["is_closed"]), Path("unused.pkl")
        )


@pytest.mark.parametrize(
    ("signal_position", "expected_error"),
    [
        ("before", "signal_day_before_history"),
        ("after", "signal_day_after_history"),
        ("inside", "missing_signal_bar"),
        ("unclosed", "missing_signal_bar"),
    ],
)
def test_signal_day_coverage_failures_abort_before_replay(
    monkeypatch, tmp_path: Path, signal_position: str, expected_error: str
):
    frame = _frame(30)
    if signal_position == "before":
        signal_day = "2025-12-01"
    elif signal_position == "after":
        signal_day = "2027-01-01"
    else:
        index = 21
        signal_day = pd.Timestamp(frame.loc[index, "datetime"]).date().isoformat()
        if signal_position == "inside":
            frame = frame.drop(index=index).reset_index(drop=True)
        else:
            frame.loc[index, "is_closed"] = False
    frame.to_pickle(tmp_path / "000001_qfq.pkl")
    monkeypatch.setattr(audit, "HISTORY_DIR", tmp_path)

    features, meta = audit._factor_features_for_sources([_source(day=signal_day)])
    identifier = audit.candidate_id(_source(day=signal_day))

    assert features[identifier]["factor_error"] == expected_error
    assert meta["replay_history_coverage_safe"] is False
    assert meta["replay_history_coverage_failures"][0]["error"] == expected_error
    with pytest.raises(RuntimeError, match="history safety gate failed before replay"):
        audit._assert_history_safe(meta)


@pytest.mark.parametrize("case", ["missing", "corrupt", "non_qfq"])
def test_missing_corrupt_or_non_qfq_history_aborts(monkeypatch, tmp_path: Path, case: str):
    path = tmp_path / "000001_qfq.pkl"
    if case == "corrupt":
        path.write_bytes(b"not a pickle")
    elif case == "non_qfq":
        frame = _frame(30)
        frame.attrs["adjust"] = "none"
        frame.to_pickle(path)
    monkeypatch.setattr(audit, "HISTORY_DIR", tmp_path)

    _, meta = audit._factor_features_for_sources([_source()])

    assert meta["history_input_safe"] is False
    assert len(meta["history_input_failures"]) == 1
    with pytest.raises(RuntimeError, match="history safety gate failed before replay"):
        audit._assert_history_safe(meta)


def test_replay_integrity_accepts_exact_unique_one_to_one_mapping():
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


@pytest.mark.parametrize(
    "failure",
    [
        "missing_replay",
        "duplicate_replay",
        "reordered_replay",
        "extra_feature",
        "missing_feature",
        "replay_skip",
        "policy_skip",
        "incomplete_baseline",
    ],
)
def test_replay_integrity_rejects_any_mapping_or_skip_failure(failure: str):
    sources = [_source("000001"), _source("000002")]
    replay = [dict(row) for row in sources]
    features = {
        audit.candidate_id(row): {"candidate_id": audit.candidate_id(row)}
        for row in sources
    }
    skips: Counter = Counter()
    source_match = _valid_source_match(2)
    if failure == "missing_replay":
        replay.pop()
    elif failure == "duplicate_replay":
        replay[1] = dict(replay[0])
    elif failure == "reordered_replay":
        replay.reverse()
    elif failure == "extra_feature":
        features["999999|2026-01-02|macd_above"] = {}
    elif failure == "missing_feature":
        features.pop(audit.candidate_id(sources[1]))
    elif failure == "replay_skip":
        skips["missing_entry_bar"] = 1
    elif failure == "policy_skip":
        source_match["policy_skips"] = {"policy_disabled": 1}
    elif failure == "incomplete_baseline":
        source_match["baseline_replay_complete"] = False

    with pytest.raises(RuntimeError, match="replay integrity validation failed"):
        audit._assert_replay_integrity(
            sources, features, replay, skips, source_match
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


def test_config_hash_must_match_preregistered_snapshot(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(audit, "file_sha256", lambda path: "0" * 64)
    with pytest.raises(RuntimeError, match="config hash differs"):
        audit.run(_args(tmp_path / "missing", tmp_path / "output"))


def test_cross_split_gate_requires_every_split_direction_and_bootstrap_contract():
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

    partial = {"train": reports["train"], "val": reports["val"]}
    assert audit._cross_split_gate(partial)["pass"] is False


def test_symbol_cluster_bootstrap_is_deterministic_and_meets_fixed_contract():
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
        rows,
        "future_40d",
        reps=audit.PREREGISTERED_BOOTSTRAP_REPS,
        seed=audit.PREREGISTERED_SEED,
    )
    second = audit._cluster_bootstrap_delta(
        rows,
        "future_40d",
        reps=audit.PREREGISTERED_BOOTSTRAP_REPS,
        seed=audit.PREREGISTERED_SEED,
    )
    assert first == second
    assert first["reps_valid"] == first["reps_requested"] == 2000
    assert first["cluster_count"] == 10
    assert audit._bootstrap_contract_ok(first) is True


def test_bootstrap_contract_rejects_too_few_clusters_or_invalid_replicates():
    base = {
        "mean_delta": 1.0,
        "reps_requested": 2000,
        "reps_valid": 2000,
        "cluster_count": 10,
    }
    assert audit._bootstrap_contract_ok({**base, "cluster_count": 9}) is False
    assert audit._bootstrap_contract_ok({**base, "reps_valid": 1999}) is False
    assert audit._bootstrap_contract_ok({**base, "reps_requested": 1999}) is False


def test_diagnostic_factor_cannot_rescue_failed_primary_gate(monkeypatch):
    rows = [
        _row(f"{symbol:06d}", f"2026-01-{day:02d}", float(symbol))
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

    assert report["factors"]["downside_semideviation_20"]["n"] == len(rows)
    assert report["gate"]["primary_direction_positive"] is False
    assert report["gate"]["primary_cluster_bootstrap_ci95_positive"] is False


def test_existing_formal_or_temporary_output_is_never_overwritten(tmp_path: Path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    target = output_dir / "atr_volatility_train.jsonl"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_targets_unused(output_dir)
    target.unlink()
    target.with_name(target.name + ".tmp").write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_targets_unused(output_dir)


def test_atomic_writer_records_final_paths_and_temporary_hashes(tmp_path: Path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    enriched = {split: [{"split": split}] for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}

    audit._write_outputs_atomically(output_dir, enriched, result)

    for split in audit.SPLITS:
        target = output_dir / f"atr_volatility_{split}.jsonl"
        assert target.exists()
        assert not target.with_name(target.name + ".tmp").exists()
        assert result["splits"][split]["enriched"] == str(target)
        assert result["splits"][split]["enriched_sha256"] == audit.file_sha256(target)
    written = json.loads((output_dir / "atr_volatility_audit.json").read_text(encoding="utf-8"))
    assert written["splits"] == result["splits"]


def test_run_emits_provenance_and_all_fixed_safety_fields(monkeypatch, tmp_path: Path):
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
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
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

    def factor_features(sources):
        return (
            {
                audit.candidate_id(row): {
                    "candidate_id": audit.candidate_id(row),
                    "factor_assignment_available": True,
                    "factor_error": None,
                    "atr20_ratio": float(row["atr20_ratio"]),
                    "downside_semideviation_20": float(
                        row["downside_semideviation_20"]
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

    monkeypatch.setattr(audit, "_factor_features_for_sources", factor_features)
    monkeypatch.setattr(audit, "_replay_split", replay_split)
    monkeypatch.setattr(audit, "_cluster_bootstrap_delta", positive_bootstrap)
    monkeypatch.setattr(audit, "load_config", lambda path: {})
    monkeypatch.setattr(audit, "_config_snapshot", lambda config: {})

    result = audit.run(_args(input_dir, output_dir))

    assert result["research_provenance"] == {
        "prior_probe_window_true_ranges": 14,
        "audit_window_true_ranges": 20,
        "direction_source": "frozen_P5b_three_window_probe",
        "audit_type": "nearby_fixed_horizon_robustness",
        "exact_replication": False,
        "independent_confirmation": False,
        "prior_probe_may_overlap_canonical_splits": True,
        "holdout_reserved": True,
    }
    assert result["design"]["holdout_consumed"] is False
    assert result["design"]["portfolio_layer_implemented"] is False
    assert result["design"]["production_eligible"] is False
    assert result["design"]["period_scan_performed"] is False
    assert result["design"]["reverse_direction_tested"] is False
    assert result["production_database_connected"] is False
    assert result["config_file_unchanged"] is True
    assert result["config_file_expected_sha256"] == audit.PREREGISTERED_CONFIG_SHA256
    assert result["config_file_sha256_before"] == result["config_file_sha256_after"]
    assert result["production_decision"] == "unchanged_P0"
    assert result["candidate_gate"]["pass"] is True
    assert (output_dir / "atr_volatility_audit.json").exists()
