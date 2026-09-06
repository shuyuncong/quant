from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import pytest
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import canonical_replay_semantics_audit as audit  # noqa: E402


def _source(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": "000001",
        "signal_day": "2026-01-02",
        "signal_type": "macd_above",
        "entry_day": "2026-01-05",
        "exit_reason": "sell_1",
        "holding_days": 10,
        "trade_pnl_pct": 2.0,
        "future_5d": 1.0,
        "future_20d": 3.0,
        "future_40d": 4.0,
        "mfe": 6.0,
        "mae": -2.0,
        "regime": "bull",
    }
    row.update(overrides)
    return row


def _replay(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        **_source(),
        "candidate_id": "000001|2026-01-02|macd_above",
        "entry_price": 10.0,
        "exit_trigger_day": "2026-01-14",
        "exit_day": "2026-01-15",
        "exit_price": 10.3,
        "holding_bars": 8,
        "entry_commission_cash": 5.0,
        "exit_commission_cash": 5.0,
        "stamp_tax_cash": 1.03,
        "slippage_cash": 1.02,
    }
    row.update(overrides)
    return row


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_fixed_contract_rejects_holdout_and_custom_profiles():
    assert audit.SPLITS == ("train", "val", "test")
    assert audit.REPLAY_PROFILES == (
        "production_risk",
        "no_risk_sl_tp_current_simulator",
    )
    assert audit.OUTCOME_FIELDS.isdisjoint(audit.IDENTITY_FIELDS)
    assert audit.FUTURE_REPLAY_CONTRACT["source_artifact_role"] == (
        "signal_identity_only"
    )
    assert audit.FUTURE_REPLAY_CONTRACT["authoritative_outcome_source"] == (
        "versioned_replay"
    )


def test_membership_contract_is_independent_of_outcomes():
    rows = [_source(symbol="1"), _source(symbol="2", trade_pnl_pct=-99.0)]
    result = audit._membership_contract(rows, "train")
    assert result["all_pass"] is True
    assert result["split_assignment_source"] == "fixed_filename:train"
    assert result["ids_unchanged_after_outcome_redaction"] is True
    assert result["candidate_ids_before"] == result["candidate_ids_after"]


def test_membership_contract_rejects_duplicate_ids():
    with pytest.raises(RuntimeError, match="unique"):
        audit._membership_contract([_source(), _source(trade_pnl_pct=-1.0)], "train")


def test_compare_candidate_reports_observed_and_unavailable_fields():
    comparison = audit._compare_candidate(
        _source(),
        _replay(
            exit_reason="stop_loss",
            holding_days=4,
            trade_pnl_pct=-8.1,
        ),
        split="train",
        profile="production_risk",
    )
    assert comparison["differences"]["entry_day"]["match"] is True
    assert comparison["differences"]["exit_reason"]["match"] is False
    assert comparison["differences"]["holding_days"]["signed_diff"] == -6.0
    assert comparison["differences"]["trade_pnl_pct"]["abs_diff"] == 10.1
    assert comparison["differences"]["entry_price"]["source_available"] is False
    assert comparison["differences"]["entry_price"]["match"] is False
    assert comparison["differences"]["entry_price"]["availability_status"] == (
        "replay_only"
    )
    assert "risk_exit_semantics" in comparison["drift_categories"]


@pytest.mark.parametrize(
    ("source_reason", "replay_reason", "expected"),
    [
        ("sell_1", "stop_loss", "risk_exit_semantics"),
        ("sell_1", "timeout_hard_cap", "timeout_semantics"),
        ("sell_1", "zero_axis_death_cross", "sell_signal_semantics"),
    ],
)
def test_reason_transitions_are_classified(
    source_reason: str,
    replay_reason: str,
    expected: str,
):
    row = audit._compare_candidate(
        _source(exit_reason=source_reason),
        _replay(exit_reason=replay_reason),
        split="val",
        profile="production_risk",
    )
    assert expected in row["drift_categories"]


def test_matching_reason_and_holding_with_pnl_drift_is_price_cost_or_history():
    row = audit._compare_candidate(
        _source(),
        _replay(trade_pnl_pct=2.2),
        split="test",
        profile="no_risk_sl_tp_current_simulator",
    )
    assert row["drift_categories"] == ["price_cost_or_history"]


def test_aggregate_comparisons_reports_transitions_and_numeric_stats():
    rows = [
        audit._compare_candidate(
            _source(symbol="1"),
            _replay(
                symbol="1",
                candidate_id="000001|2026-01-02|macd_above",
                exit_reason="stop_loss",
                trade_pnl_pct=-8.0,
            ),
            split="train",
            profile="production_risk",
        ),
        audit._compare_candidate(
            _source(symbol="2"),
            _replay(
                symbol="2",
                candidate_id="000002|2026-01-02|macd_above",
            ),
            split="train",
            profile="production_risk",
        ),
    ]
    result = audit._aggregate_comparisons(rows)
    assert result["n"] == 2
    assert result["fields"]["exit_reason"]["match_rate_pct"] == 50.0
    assert result["fields"]["trade_pnl_pct"]["absolute_diff"]["max"] == 10.0
    assert result["exit_reason_transitions"]["sell_1->stop_loss"] == 1
    assert result["drift_category_counts"]["risk_exit_semantics"] == 1


def test_replay_integrity_rejects_skips_and_order_drift():
    sources = [_source(symbol="1"), _source(symbol="2")]
    replayed = [
        _replay(symbol="2", candidate_id="000002|2026-01-02|macd_above"),
        _replay(symbol="1", candidate_id="000001|2026-01-02|macd_above"),
    ]
    match = {
        "source_rows": 2,
        "common_eligible_rows": 2,
        "simulated_rows": 2,
        "history_manifest_sha256": "history",
    }
    with pytest.raises(RuntimeError, match="integrity"):
        audit._assert_replay_integrity(
            sources, replayed, {}, match, "history", "production_risk"
        )
    with pytest.raises(RuntimeError, match="integrity"):
        audit._assert_replay_integrity(
            sources,
            list(reversed(replayed)),
            {"missing_history": 1},
            match,
            "history",
            "production_risk",
        )


def test_replay_integrity_rejects_explicit_id_that_disagrees_with_row_identity():
    source = [_source()]
    replayed = [_replay(symbol="000002")]
    match = {
        "source_rows": 1,
        "common_eligible_rows": 1,
        "simulated_rows": 1,
        "history_manifest_sha256": "history",
    }
    with pytest.raises(RuntimeError, match="integrity"):
        audit._assert_replay_integrity(
            source, replayed, {}, match, "history", "production_risk"
        )
    with pytest.raises(RuntimeError, match="disagrees"):
        audit._compare_candidate(
            _source(), replayed[0], split="train", profile="production_risk"
        )


def test_manifest_validation_accepts_exact_canonical_contract(tmp_path: Path):
    rows = [_source()]
    source = tmp_path / "candidates_train.jsonl"
    _write_jsonl(source, rows)
    ids_hash = audit.candidate_ids_sha256(rows)
    manifest = {
        "version": audit.INTEGRITY_VERSION,
        "output_dir": str(tmp_path.resolve()),
        "splits": {
            "train": {
                "output_file": str(source.resolve()),
                "output_sha256": audit.file_sha256(source),
                "output_rows": 1,
                "candidate_ids_sha256": ids_hash,
            }
        },
    }
    (tmp_path / "candidate_integrity_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    result = audit._validate_integrity_manifest(tmp_path, "train", source, rows)
    assert result["all_pass"] is True


def test_manifest_validation_fails_closed_on_hash_mismatch(tmp_path: Path):
    rows = [_source()]
    source = tmp_path / "candidates_train.jsonl"
    _write_jsonl(source, rows)
    manifest = {
        "version": audit.INTEGRITY_VERSION,
        "output_dir": str(tmp_path.resolve()),
        "splits": {
            "train": {
                "output_file": str(source.resolve()),
                "output_sha256": "bad",
                "output_rows": 1,
                "candidate_ids_sha256": audit.candidate_ids_sha256(rows),
            }
        },
    }
    (tmp_path / "candidate_integrity_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="integrity"):
        audit._validate_integrity_manifest(tmp_path, "train", source, rows)


def test_fixed_args_reject_wrong_split_label_and_input(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(audit, "CANONICAL_INPUT_DIR", tmp_path.resolve())
    valid = argparse.Namespace(
        input_dir=str(tmp_path),
        output_dir=str(tmp_path.parent / "out"),
        config=str(audit.DEFAULT_CONFIG),
        label=audit.PREREGISTERED_LABEL,
        splits=list(audit.SPLITS),
        profiles=list(audit.REPLAY_PROFILES),
    )
    audit._validate_fixed_args(valid)
    for overrides in (
        {"splits": ["train", "val", "holdout"]},
        {"label": "changed"},
        {"profiles": ["production_risk"]},
        {"input_dir": str(tmp_path / "other")},
    ):
        values = vars(valid).copy()
        values.update(overrides)
        with pytest.raises(RuntimeError):
            audit._validate_fixed_args(argparse.Namespace(**values))


def test_atomic_writer_refuses_existing_output_and_leaves_no_tmp(tmp_path: Path):
    output = tmp_path / "formal"
    comparisons = {
        (split, profile): [
            audit._compare_candidate(
                _source(), _replay(), split=split, profile=profile
            )
        ]
        for split in audit.SPLITS
        for profile in audit.REPLAY_PROFILES
    }
    audit._write_outputs_atomically(output, comparisons, {"version": audit.VERSION})
    assert (output / "canonical_replay_semantics_audit.json").exists()
    assert len(list(output.glob("*.jsonl"))) == 6
    assert not list(output.glob("*.tmp"))
    with pytest.raises(RuntimeError, match="exists"):
        audit._write_outputs_atomically(output, comparisons, {})


def test_protected_snapshot_comparison_detects_any_change(tmp_path: Path):
    path = tmp_path / "input.txt"
    path.write_text("before", encoding="utf-8")
    before = audit._paths_snapshot([path])
    path.write_text("after", encoding="utf-8")
    after = audit._paths_snapshot([path])
    result = audit._compare_snapshots(before, after)
    assert result["all_unchanged"] is False
    assert str(path.resolve()) in result["changed_paths"]


def test_history_mutation_between_initial_snapshot_and_inventory_fails(
    tmp_path: Path,
):
    path = tmp_path / "000001_qfq.pkl"
    path.write_bytes(b"validated-version")
    initial = audit._paths_snapshot([path])
    path.write_bytes(b"mutated-during-preflight")
    inventory = {
        "file_sha256_by_symbol": {"000001": audit.file_sha256(path)}
    }
    with pytest.raises(RuntimeError, match="initial snapshot"):
        audit._assert_history_inventory_matches_snapshot(
            inventory, initial, history_dir=tmp_path
        )


def test_static_mutation_after_first_snapshot_is_detected_before_replay(
    tmp_path: Path,
):
    config = tmp_path / "config.yaml"
    config.write_text("before", encoding="utf-8")
    initial = audit._paths_snapshot([config])
    config.write_text("changed-before-parse", encoding="utf-8")
    pre_replay = audit._paths_snapshot([config])
    result = audit._compare_snapshots(initial, pre_replay)
    assert result["all_unchanged"] is False
    assert result["changed_paths"] == [str(config.resolve())]


def test_profile_attribution_does_not_claim_exact_historical_reconstruction():
    profiles = {
        "production_risk": {
            "comparable_field_match_rate_pct": 60.0,
            "fields": {
                "exit_reason": {"match_rate_pct": 40.0},
                "trade_pnl_pct": {"match_rate_pct": 30.0},
            },
        },
        "no_risk_sl_tp_current_simulator": {
            "comparable_field_match_rate_pct": 90.0,
            "fields": {
                "exit_reason": {"match_rate_pct": 85.0},
                "trade_pnl_pct": {"match_rate_pct": 80.0},
            },
        },
    }
    result = audit._profile_attribution(profiles)
    assert result["overall_profile_winner_selected"] is False
    assert result["risk_profile_explanatory_improvement"][
        "exit_reason_match_rate_pp"
    ] == 45.0
    assert result["exact_source_semantics_reconstructable"] is False
    assert result["exact_historical_version_identified"] is False


def test_any_future_field_drift_is_detected_without_hidden_materiality_gate():
    row = audit._compare_candidate(
        _source(),
        _replay(future_40d=99.0),
        split="train",
        profile="production_risk",
    )
    report = audit._aggregate_comparisons([row])
    report["fields"]["exit_reason"]["match_rate_pct"] = 100.0
    report["fields"]["trade_pnl_pct"]["absolute_diff"] = {
        "n": 1,
        "median": 0.0,
    }
    observed = audit._observed_difference_summary(report)
    assert observed["detected"] is True
    assert observed["by_field"]["future_40d"]["observed_differences"] == 1
    assert observed["materiality_threshold_applied"] is False


def test_future_contract_candidate_id_field_order_is_deterministic():
    assert audit.FUTURE_REPLAY_CONTRACT["candidate_id_fields"] == [
        "symbol",
        "signal_day",
        "signal_type",
    ]


def _history_frame(days: pd.DatetimeIndex) -> pd.DataFrame:
    values = [10.0 + index for index in range(len(days))]
    frame = pd.DataFrame(
        {
            "datetime": days,
            "open": values,
            "high": [value + 1.0 for value in values],
            "low": [value - 1.0 for value in values],
            "close": values,
            "is_closed": [True] * len(days),
        }
    )
    frame.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    return frame


@pytest.mark.parametrize(
    ("signal_day", "mutation", "reason"),
    [
        ("2025-12-31", "none", "missing_exact_signal_day_bar"),
        ("2026-01-09", "none", "missing_exact_signal_day_bar"),
        ("2026-01-06", "drop_signal", "missing_exact_signal_day_bar"),
        ("2026-01-08", "none", "missing_next_entry_bar"),
        ("2026-01-06", "unclosed_signal", "missing_exact_signal_day_bar"),
    ],
)
def test_signal_day_coverage_fails_for_truncated_missing_or_unclosed_history(
    tmp_path: Path,
    signal_day: str,
    mutation: str,
    reason: str,
):
    days = pd.bdate_range("2026-01-02", periods=5)
    frame = _history_frame(days)
    signal_timestamp = pd.Timestamp(signal_day)
    if mutation == "drop_signal":
        frame = frame[frame["datetime"] != signal_timestamp].reset_index(drop=True)
        frame.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    elif mutation == "unclosed_signal":
        frame.loc[frame["datetime"] == signal_timestamp, "is_closed"] = False
    frame.to_pickle(tmp_path / "000001_qfq.pkl")
    sources = {split: [] for split in audit.SPLITS}
    sources["train"] = [_source(signal_day=signal_day)]
    with pytest.raises(RuntimeError, match=reason):
        audit._validate_signal_day_coverage(sources, history_dir=tmp_path)


def test_signal_day_coverage_accepts_exact_signal_and_next_bar(tmp_path: Path):
    days = pd.bdate_range("2026-01-02", periods=5)
    _history_frame(days).to_pickle(tmp_path / "000001_qfq.pkl")
    sources = {split: [] for split in audit.SPLITS}
    sources["train"] = [_source(signal_day=days[2].date().isoformat())]
    result = audit._validate_signal_day_coverage(sources, history_dir=tmp_path)
    assert result["all_pass"] is True
    assert result["checked_candidates"] == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_closed", "missing required columns"),
        ("unsorted", "invalid or unsorted"),
        ("duplicate", "duplicate trading dates"),
        ("non_qfq", "not qfq"),
        ("bad_ohlc", "invalid OHLC"),
        ("infinite", "invalid OHLC"),
    ],
)
def test_history_inventory_fails_closed_on_invalid_cache(
    tmp_path: Path,
    mutation: str,
    message: str,
):
    frame = _history_frame(pd.bdate_range("2026-01-02", periods=5))
    if mutation == "missing_closed":
        frame = frame.drop(columns=["is_closed"])
    elif mutation == "unsorted":
        frame.loc[[0, 1], "datetime"] = frame.loc[[1, 0], "datetime"].to_numpy()
    elif mutation == "duplicate":
        frame.loc[1, "datetime"] = frame.loc[0, "datetime"]
    elif mutation == "non_qfq":
        frame.attrs["adjust"] = "none"
    elif mutation == "bad_ohlc":
        frame.loc[1, "high"] = frame.loc[1, "close"] - 1.0
    elif mutation == "infinite":
        frame.loc[1, "close"] = float("inf")
        frame.loc[1, "high"] = float("inf")
    frame.to_pickle(tmp_path / "000001_qfq.pkl")
    with pytest.raises(RuntimeError, match=message):
        audit._history_inventory(["000001"], history_dir=tmp_path)


def test_sha256_helper_is_stable(tmp_path: Path):
    path = tmp_path / "x"
    path.write_bytes(b"abc")
    assert audit.file_sha256(path) == hashlib.sha256(b"abc").hexdigest()


def test_run_orchestrates_all_splits_and_public_profile_mapping(
    monkeypatch,
    tmp_path: Path,
):
    input_dir = tmp_path / "canonical"
    output_dir = tmp_path / "formal"
    input_dir.mkdir()
    rows_by_split: dict[str, list[dict[str, object]]] = {}
    manifest: dict[str, object] = {
        "version": audit.INTEGRITY_VERSION,
        "output_dir": str(input_dir.resolve()),
        "splits": {},
    }
    for index, split in enumerate(audit.SPLITS, start=1):
        row = _source(symbol=str(index).zfill(6))
        rows = [row]
        rows_by_split[split] = rows
        path = input_dir / f"candidates_{split}.jsonl"
        _write_jsonl(path, rows)
        manifest["splits"][split] = {  # type: ignore[index]
            "output_file": str(path.resolve()),
            "output_sha256": audit.file_sha256(path),
            "output_rows": 1,
            "candidate_ids_sha256": audit.candidate_ids_sha256(rows),
        }
    (input_dir / "candidate_integrity_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    monkeypatch.setattr(audit, "HISTORY_DIR", history_dir)
    for index in range(1, len(audit.SPLITS) + 1):
        symbol = str(index).zfill(6)
        (history_dir / f"{symbol}_qfq.pkl").write_bytes(symbol.encode("ascii"))
    config_hash = audit.file_sha256(config)
    monkeypatch.setattr(audit, "CANONICAL_INPUT_DIR", input_dir.resolve())
    monkeypatch.setattr(audit, "DEFAULT_CONFIG", config.resolve())
    monkeypatch.setattr(audit, "PREREGISTERED_CONFIG_SHA256", config_hash)
    monkeypatch.setattr(
        audit,
        "_dependency_contract",
        lambda *_: {
            name: {
                "path": str(config.resolve()),
                "expected_sha256": config_hash,
                "actual_sha256": config_hash,
                "match": True,
            }
            for name in ("attribution_audit", "backtest_winrate")
        },
    )
    monkeypatch.setattr(audit, "load_config", lambda _: {})
    def inventory_stub(symbols):
        paths = []
        hashes = {}
        for symbol_value in sorted(set(symbols)):
            symbol = str(symbol_value).zfill(6)
            path = history_dir / f"{symbol}_qfq.pkl"
            paths.append(path)
            hashes[symbol] = audit.file_sha256(path)
        return (
            {
                "history_dir": "test",
                "symbol_count": len(hashes),
                "file_count": len(hashes),
                "history_manifest_sha256": audit._history_manifest_sha256(hashes),
                "file_sha256_by_symbol": hashes,
                "earliest_first_day": "2026-01-01",
                "latest_last_day": "2026-12-31",
                "adjustment": "qfq",
                "timeframe": "1d",
                "local_files_only": True,
                "all_valid": True,
            },
            paths,
        )

    monkeypatch.setattr(audit, "_history_inventory", inventory_stub)
    monkeypatch.setattr(
        audit,
        "_validate_signal_day_coverage",
        lambda _: {
            "checked_candidates": 3,
            "checked_symbols": 3,
            "exact_signal_day_required": True,
            "next_entry_bar_required": True,
            "failures": [],
            "all_pass": True,
        },
    )
    calls: list[str] = []

    def replay_stub(sources, config_value, profile, history_dir):
        del config_value
        calls.append(profile)
        replayed = [
            {
                **source,
                "candidate_id": audit.candidate_id(source),
                "entry_price": 10.0,
                "exit_day": "2026-01-15",
                "exit_trigger_day": "2026-01-14",
                "exit_price": 10.3,
                "holding_bars": 8,
            }
            for source in sources
        ]
        history_hashes = {
            str(source["symbol"]).zfill(6): audit.file_sha256(
                history_dir / f"{str(source['symbol']).zfill(6)}_qfq.pkl"
            )
            for source in sources
        }
        return replayed, Counter(), {
            "source_rows": len(sources),
            "common_eligible_rows": len(sources),
            "simulated_rows": len(sources),
            "history_manifest_sha256": audit._history_manifest_sha256(history_hashes),
        }

    monkeypatch.setattr(audit, "_replay_split", replay_stub)
    args = argparse.Namespace(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        config=str(config),
        label=audit.PREREGISTERED_LABEL,
        splits=list(audit.SPLITS),
        profiles=list(audit.REPLAY_PROFILES),
    )
    result = audit.run(args)
    assert result["audit_contract_pass"] is True
    assert calls == [
        "production_risk",
        "frozen_source",
        "production_risk",
        "frozen_source",
        "production_risk",
        "frozen_source",
    ]
    assert result["semantic_attribution"]["overall_profile_winner_selected"] is False
    assert (output_dir / "canonical_replay_semantics_audit.json").exists()
