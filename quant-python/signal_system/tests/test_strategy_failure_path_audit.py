from __future__ import annotations

import argparse
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy_failure_path_audit as audit


def _config() -> dict:
    return audit.load_config(str(audit.CONFIG_PATH))


def _closed(count: int = 70, *, start: str = "2026-01-01") -> pd.DataFrame:
    days = pd.bdate_range(start, periods=count)
    close = np.linspace(99.0, 112.0, count)
    return pd.DataFrame(
        {
            "datetime": days,
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "is_closed": True,
        }
    )


def _replay(closed: pd.DataFrame, *, exit_session: str = "open") -> dict:
    signal_idx = 4
    entry_idx = 5
    trigger_idx = 9
    exit_idx = 10
    entry_price = round(float(closed.iloc[entry_idx]["open"]), 4)
    row = {
        "candidate_id": "000001|2026-01-07|buy_1",
        "symbol": "000001",
        "signal_day": pd.Timestamp(closed.iloc[signal_idx]["datetime"]).date().isoformat(),
        "signal_type": "buy_1",
        "regime": "bull",
        "entry_day": pd.Timestamp(closed.iloc[entry_idx]["datetime"]).date().isoformat(),
        "entry_price": entry_price,
        "exit_trigger_day": pd.Timestamp(closed.iloc[trigger_idx]["datetime"]).date().isoformat(),
        "exit_day": pd.Timestamp(closed.iloc[exit_idx]["datetime"]).date().isoformat(),
        "exit_price": round(float(closed.iloc[exit_idx]["open"]), 4),
        "exit_reason": "sell_1",
        "exit_session": exit_session,
        "holding_days": 7,
        "holding_bars": exit_idx - entry_idx,
        "price_limit_deferred_bars": 0,
    }
    for field in audit.FIELDS:
        row[field] = 1.0
    row["trade_pnl_pct"] = 0.5
    return row


def _source(
    symbol: str = "000001",
    day: str = "2026-01-07",
    signal_type: str = "buy_1",
    regime: str = "bull",
) -> dict:
    return {
        "symbol": symbol,
        "signal_day": day,
        "signal_type": signal_type,
        "regime": regime,
    }


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "input_dir": str(audit.CANONICAL_INPUT_DIR),
        "output_dir": str(audit.FORMAL_OUTPUT_DIR),
        "config": str(audit.CONFIG_PATH),
        "index_data": str(audit.INDEX_PATH),
        "label": audit.PREREGISTERED_LABEL,
        "splits": list(audit.SPLITS),
        "expected_script_sha256": audit.file_sha256(Path(audit.__file__)),
        "preflight_only": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_contract_constants_are_frozen():
    assert audit.VERSION == "strategy_failure_path_audit.v1"
    assert audit.HORIZONS == (1, 3, 5, 10, 20, 40)
    assert audit.PREREGISTERED_LABEL == "strategy-failure-path-audit"
    assert audit.EXPECTED_CONFIG_SNAPSHOT["stop_loss_pct"] == pytest.approx(0.08)
    assert audit.EXPECTED_CONFIG_SNAPSHOT["stop_profit_pct"] == pytest.approx(0.30)
    assert audit.EXPECTED_CONFIG_SNAPSHOT["t_plus_one"] is True
    assert audit.EXPECTED_CONFIG_SNAPSHOT["intrabar_conflict"] == "stop_first"
    assert all(
        study["candidate_gate_implemented"] is False
        for study in audit.STUDY_REGISTRY.values()
    )


def test_config_matches_frozen_execution_and_market_gate():
    snapshot = audit._validate_config(_config())
    assert snapshot == audit.EXPECTED_CONFIG_SNAPSHOT


def test_config_drift_fails_closed():
    config = deepcopy(_config())
    config["risk"]["stop_loss_pct"] = 0.09
    with pytest.raises(RuntimeError, match="execution config differs"):
        audit._validate_config(config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("label", "other"),
        ("splits", ["test"]),
        ("input_dir", "D:/tmp/other"),
        ("output_dir", "D:/tmp/other"),
        ("config", "D:/tmp/other.yaml"),
        ("index_data", "D:/tmp/other.pkl"),
        ("expected_script_sha256", "0" * 64),
    ],
)
def test_preregistered_args_fail_closed(field: str, value: object):
    args = _args(**{field: value})
    with pytest.raises(RuntimeError):
        audit._validate_preregistered_args(args)


def test_output_directory_refuses_final_or_staging(tmp_path: Path):
    final = tmp_path / "final"
    final.mkdir()
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_directory_unused(final)
    final.rmdir()
    staging = tmp_path / "final.tmp"
    staging.mkdir()
    with pytest.raises(RuntimeError, match="overwrite is forbidden"):
        audit._assert_output_directory_unused(final)


def test_extreme_uses_earliest_offset_on_ties():
    assert audit._extreme_with_offset(pd.Series([1.0, 3.0, 3.0]), maximum=True) == (3.0, 1)
    assert audit._extreme_with_offset(pd.Series([2.0, 1.0, 1.0]), maximum=False) == (1.0, 1)


def test_held_excursion_open_fill_excludes_exit_day_high_low():
    closed = _closed(10)
    closed.loc[5, ["high", "low", "open"]] = [999.0, 1.0, 103.0]
    bounds = audit._held_excursion_bounds(closed, 1, 5, 100.0, 103.0, "open")
    assert bounds["held_excursion_observability"] == "exact_open_fill"
    assert bounds["held_mfe_lower_bound_pct"] == bounds["held_mfe_upper_bound_pct"]
    assert bounds["held_mae_best_bound_pct"] == bounds["held_mae_worst_bound_pct"]
    assert bounds["held_mfe_upper_bound_pct"] < 100.0


def test_held_excursion_close_fill_includes_full_exit_bar():
    closed = _closed(10)
    closed.loc[5, ["high", "low", "close"]] = [120.0, 80.0, 105.0]
    bounds = audit._held_excursion_bounds(closed, 1, 5, 100.0, 105.0, "close")
    assert bounds["held_excursion_observability"] == "exact_close_fill"
    assert bounds["held_mfe_upper_bound_pct"] == pytest.approx(20.0)
    assert bounds["held_mae_worst_bound_pct"] == pytest.approx(-20.0)


def test_held_excursion_intraday_is_bounded():
    closed = _closed(10)
    closed.loc[5, ["high", "low"]] = [120.0, 80.0]
    bounds = audit._held_excursion_bounds(closed, 1, 5, 100.0, 105.0, "intraday")
    assert bounds["held_excursion_observability"] == "bounded_intraday_fill"
    assert bounds["held_mfe_lower_bound_pct"] < bounds["held_mfe_upper_bound_pct"]
    assert bounds["held_mae_worst_bound_pct"] < bounds["held_mae_best_bound_pct"]


def test_lifecycle_rejects_holding_offset_mismatch():
    closed = _closed()
    replay = _replay(closed)
    replay["holding_bars"] = 99
    with pytest.raises(RuntimeError, match="holding_bars"):
        audit._validate_replay_lifecycle(
            replay, closed, audit._date_list(closed), 5
        )


def test_lifecycle_rejects_exit_price_outside_bar():
    closed = _closed()
    replay = _replay(closed)
    replay["exit_price"] = 9999.0
    with pytest.raises(RuntimeError, match="outside fill-day OHLC"):
        audit._validate_replay_lifecycle(
            replay, closed, audit._date_list(closed), 5
        )


def test_lifecycle_rejects_holding_days_mismatch():
    closed = _closed()
    replay = _replay(closed)
    replay["holding_days"] = 999
    with pytest.raises(RuntimeError, match="holding_days"):
        audit._validate_replay_lifecycle(
            replay, closed, audit._date_list(closed), 5
        )


def test_lifecycle_rejects_t_plus_one_same_bar_fill():
    closed = _closed()
    replay = _replay(closed, exit_session="close")
    replay["exit_trigger_day"] = replay["entry_day"]
    replay["exit_day"] = replay["entry_day"]
    replay["exit_price"] = replay["entry_price"]
    replay["holding_days"] = 0
    replay["holding_bars"] = 0
    with pytest.raises(RuntimeError, match=r"T\+1 replay"):
        audit._validate_replay_lifecycle(
            replay, closed, audit._date_list(closed), 5
        )


def test_lifecycle_rejects_open_or_close_price_away_from_session_mark():
    closed = _closed()
    replay = _replay(closed)
    replay["exit_price"] = round(float(closed.iloc[10]["open"]) + 0.2, 4)
    with pytest.raises(RuntimeError, match="differs from bar open"):
        audit._validate_replay_lifecycle(
            replay, closed, audit._date_list(closed), 5
        )
    replay = _replay(closed, exit_session="close")
    replay["exit_price"] = round(float(closed.iloc[10]["close"]) - 0.2, 4)
    with pytest.raises(RuntimeError, match="differs from bar close"):
        audit._validate_replay_lifecycle(
            replay, closed, audit._date_list(closed), 5
        )


def test_lifecycle_rejects_impossible_price_limit_deferred_count():
    closed = _closed()
    replay = _replay(closed)
    replay["exit_trigger_day"] = pd.Timestamp(closed.iloc[6]["datetime"]).date().isoformat()
    replay["exit_day"] = pd.Timestamp(closed.iloc[10]["datetime"]).date().isoformat()
    replay["holding_days"] = (
        pd.Timestamp(closed.iloc[10]["datetime"])
        - pd.Timestamp(closed.iloc[5]["datetime"])
    ).days
    replay["holding_bars"] = 5
    replay["price_limit_deferred_bars"] = 0
    with pytest.raises(RuntimeError, match="differs from simulator event timing"):
        audit._validate_replay_lifecycle(
            replay, closed, audit._date_list(closed), 5
        )


def test_replay_passthrough_rejects_regime_mismatch():
    source = _source(regime="bull")
    replay = {**source, "regime": "bear", "candidate_id": audit.candidate_id(source)}
    with pytest.raises(RuntimeError, match="regime passthrough"):
        audit._validate_replay_passthrough([source], [replay])


def test_entry_bar_stop_is_recorded_even_though_t_plus_one_defers_fill():
    closed = _closed()
    replay = _replay(closed)
    entry_idx = 5
    entry_price = float(replay["entry_price"])
    closed.loc[entry_idx, "low"] = entry_price * 0.90
    replay["exit_reason"] = "stop_loss"
    replay["exit_trigger_day"] = replay["entry_day"]
    replay["exit_day"] = pd.Timestamp(closed.iloc[entry_idx + 1]["datetime"]).date().isoformat()
    replay["exit_price"] = round(float(closed.iloc[entry_idx + 1]["open"]), 4)
    replay["holding_days"] = (
        pd.Timestamp(closed.iloc[entry_idx + 1]["datetime"])
        - pd.Timestamp(closed.iloc[entry_idx]["datetime"])
    ).days
    replay["holding_bars"] = 1
    path = audit._fixed_path_for_replay(replay, closed, _config())
    assert path["first_stop_touch_offset"] == 0
    assert path["raw_first_risk_trigger_offset"] == 0
    assert path["raw_first_risk_trigger_reason"] == "stop_loss"
    assert path["raw_risk_trigger_on_entry_bar"] is True
    assert path["exit_fill_offset"] == 1


def test_entry_bar_risk_trigger_rejects_contradictory_replay_reason():
    closed = _closed()
    replay = _replay(closed)
    entry_price = float(replay["entry_price"])
    closed.loc[5, "low"] = entry_price * 0.90
    with pytest.raises(
        RuntimeError, match=r"contradicts authoritative T\+1 replay"
    ):
        audit._fixed_path_for_replay(replay, closed, _config())


def test_same_bar_stop_take_conflict_uses_frozen_stop_first():
    closed = _closed()
    replay = _replay(closed)
    entry_price = float(replay["entry_price"])
    closed.loc[5, "low"] = entry_price * 0.90
    closed.loc[5, "high"] = entry_price * 1.35
    replay["exit_reason"] = "stop_loss"
    replay["exit_trigger_day"] = replay["entry_day"]
    replay["exit_day"] = pd.Timestamp(closed.iloc[6]["datetime"]).date().isoformat()
    replay["exit_price"] = round(float(closed.iloc[6]["open"]), 4)
    replay["holding_days"] = (
        pd.Timestamp(closed.iloc[6]["datetime"])
        - pd.Timestamp(closed.iloc[5]["datetime"])
    ).days
    replay["holding_bars"] = 1
    path = audit._fixed_path_for_replay(replay, closed, _config())
    assert path["first_stop_touch_offset"] == 0
    assert path["first_take_touch_offset"] == 0
    assert path["raw_first_risk_trigger_reason"] == "stop_loss"


def test_fixed_horizon_uses_entry_plus_h_and_inclusive_excursion():
    closed = _closed()
    replay = _replay(closed)
    path = audit._fixed_path_for_replay(replay, closed, _config())
    expected = audit._pct(float(closed.iloc[6]["close"]), float(replay["entry_price"]))
    assert path["close_return_1b_pct"] == expected
    assert path["mfe_1b_offset"] in {0, 1}
    assert path["mae_1b_offset"] in {0, 1}
    assert path["gross_mfe_giveback_lower_bound_pct"] <= path[
        "gross_mfe_giveback_upper_bound_pct"
    ]


def test_path_marks_fill_after_fixed_40_bar_window():
    closed = _closed(90)
    replay = _replay(closed, exit_session="close")
    replay["exit_trigger_day"] = pd.Timestamp(closed.iloc[46]["datetime"]).date().isoformat()
    replay["exit_day"] = pd.Timestamp(closed.iloc[47]["datetime"]).date().isoformat()
    replay["exit_price"] = round(float(closed.iloc[47]["close"]), 4)
    replay["holding_days"] = (
        pd.Timestamp(closed.iloc[47]["datetime"])
        - pd.Timestamp(closed.iloc[5]["datetime"])
    ).days
    replay["holding_bars"] = 42
    path = audit._fixed_path_for_replay(replay, closed, _config())
    assert path["exit_after_40b"] is True
    assert path["exit_fill_offset"] == 42


def test_fill_mfe40_same_bar_is_unobservable_not_false():
    closed = _closed()
    replay = _replay(closed, exit_session="close")
    exit_idx = 10
    closed.loc[exit_idx, "high"] = float(closed["high"].max()) + 50.0
    replay["exit_price"] = round(float(closed.iloc[exit_idx]["close"]), 4)
    path = audit._fixed_path_for_replay(replay, closed, _config())
    assert path["mfe_40b_offset"] == exit_idx - 5
    assert path["exit_fill_before_mfe40"] is None
    assert path["exit_fill_mfe40_relation"] == "same_bar_order_unobservable"


def test_path_summary_stop_rate_denominator_includes_no_touch():
    rows = [
        {"symbol": "1", "signal_day": "d", "raw_first_risk_trigger_reason": "stop_loss"},
        {"symbol": "2", "signal_day": "d", "raw_first_risk_trigger_reason": None},
    ]
    summary = audit._path_summary(rows)
    assert summary["rates_pct"]["raw_stop_before_take_all_candidates"] == 50.0
    assert summary["rates_pct"]["raw_no_risk_touch_all_candidates"] == 50.0


def test_path_diagnostic_emits_only_registered_cuts():
    row = audit._path_evaluator_projection(
        {
            "candidate_id": "x",
            "split": "train",
            "symbol": "000001",
            "signal_day": "2026-01-01",
            "normalized_regime": "bull",
            "normalized_signal_type": "buy_1",
            "exit_category": "risk_stop_loss",
        }
    )
    rows = {split: [{**row, "split": split}] for split in audit.SPLITS}
    report, cells = audit._path_diagnostic(rows)
    assert report["registered_cuts"] == [
        "overall",
        "regime",
        "signal_type",
        "exit_category",
        "regime_x_signal_type",
    ]
    assert {cell["dimension"] for cell in cells} == {
        "overall",
        "regime",
        "signal_type",
        "exit_category",
        "regime_x_signal_type",
    }


def test_regime_prefix_reconstruction_is_suffix_invariant():
    closed = _closed(360, start="2024-01-01")
    signal_idx = 300
    day = pd.Timestamp(closed.iloc[signal_idx]["datetime"]).date().isoformat()
    prefix_regime = audit.build_market_gate(closed.iloc[: signal_idx + 1], _config())[day]["regime"]
    sources = {split: [_source(day=day, regime=prefix_regime)] for split in audit.SPLITS}
    original, _ = audit._index_regime_contexts(sources, _config(), closed)
    mutated = closed.copy(deep=True)
    mutated.loc[signal_idx + 1 :, ["open", "high", "low", "close"]] *= 50.0
    changed, _ = audit._index_regime_contexts(sources, _config(), mutated)
    assert original[day]["rebuilt_regime"] == changed[day]["rebuilt_regime"]


def test_index_day_horizons_persistence_and_transition_endpoints():
    closed = _closed(360, start="2024-01-01")
    signal_idx = 300
    day = pd.Timestamp(closed.iloc[signal_idx]["datetime"]).date().isoformat()
    regime = str(
        audit.calculate_strict_regime(
            closed["close"], fast_period=10, slow_period=20
        ).iloc[signal_idx]
    )
    contexts = {
        day: {
            "signal_index": signal_idx,
            "canonical_regime": regime,
            "rebuilt_regime": regime,
            "allows_entries": True,
            "blocked_by": (),
        }
    }
    candidate = {
        "signal_day": day,
        "symbol": "000001",
        "future_40d": 1.0,
        "trade_pnl_pct": 2.0,
        "mfe_40b_pct": 3.0,
        "mae_40b_pct": -1.0,
        "raw_first_risk_trigger_reason": None,
    }
    rows = {split: [{**candidate, "split": split}] for split in audit.SPLITS}
    day_rows = audit._index_day_rows(rows, closed, contexts)
    assert len(day_rows) == 3
    first = day_rows[0]
    entry_open = float(closed.iloc[signal_idx + 1]["open"])
    assert first["index_future_1b_pct"] == audit._pct(
        float(closed.iloc[signal_idx + 2]["close"]), entry_open
    )
    strict = audit.calculate_strict_regime(
        closed["close"], fast_period=10, slow_period=20
    )
    expected_persistence = round(
        float((strict.iloc[signal_idx + 1 : signal_idx + 6] == regime).mean() * 100),
        4,
    )
    assert first["regime_persistence_5b_pct"] == expected_persistence
    expected_transition = next(
        (
            offset
            for offset in range(1, 41)
            if str(strict.iloc[signal_idx + offset]) != regime
        ),
        None,
    )
    assert first["first_regime_transition_offset"] == expected_transition


def test_regime_context_rejects_incomplete_40bar_suffix():
    closed = _closed(330, start="2024-01-01")
    signal_idx = 300
    day = pd.Timestamp(closed.iloc[signal_idx]["datetime"]).date().isoformat()
    regime = audit.build_market_gate(closed.iloc[: signal_idx + 1], _config())[day]["regime"]
    sources = {split: [_source(day=day, regime=regime)] for split in audit.SPLITS}
    with pytest.raises(RuntimeError, match="incomplete index 40-bar suffix"):
        audit._index_regime_contexts(sources, _config(), closed)


def test_regime_summary_uses_equal_day_rows():
    rows = [
        {
            "regime_label_match": True,
            "candidate_mean_future_40d_pct": 10.0,
            "index_future_40b_pct": 2.0,
            "first_regime_transition_offset": None,
        },
        {
            "regime_label_match": True,
            "candidate_mean_future_40d_pct": -10.0,
            "index_future_40b_pct": 0.0,
            "first_regime_transition_offset": 3,
        },
    ]
    report = audit._regime_summary(rows)
    assert report["metrics"]["candidate_mean_future_40d_pct"]["mean"] == 0.0
    assert report["metrics"]["index_future_40b_pct"]["mean"] == 1.0
    assert report["rates_pct"]["transition_within_5b"] == 50.0


def test_registered_warning_set_is_frozen_and_complete():
    def path_split(f40: float, pnl: float, mfe: float, mae: float, giveback: float, stop: float):
        return {
            "overall": {
                "metrics": {
                    "future_40d": {"mean": f40},
                    "trade_pnl_pct": {"mean": pnl},
                    "mfe_40b_pct": {"mean": mfe},
                    "mae_40b_pct": {"mean": mae},
                    "net_mfe_giveback_lower_bound_pct": {"mean": giveback},
                },
                "rates_pct": {"raw_stop_before_take_all_candidates": stop},
            }
        }

    path_report = {
        "splits": {
            "train": path_split(4, 1, 10, -5, 4, 20),
            "val": path_split(3, 1, 9, -6, 5, 25),
            "test": path_split(-2, -1, 8, -7, 6, 30),
        }
    }
    regime_report = {
        "splits": {
            split: {
                "overall": {
                    "metrics": {
                        "candidate_mean_future_40d_pct": {"mean": -2.0},
                        "index_future_40b_pct": {"mean": 1.0},
                    }
                }
            }
            for split in audit.SPLITS
        }
    }
    flags = audit._registered_warning_flags(path_report, regime_report)
    assert len(flags) == 9
    assert all(value["triggered"] is True for value in flags.values())


def test_candidate_artifact_is_scalar_and_excludes_replay_internals():
    row = {field: None for field in audit.CANDIDATE_ARTIFACT_FIELDS}
    row.update(
        {
            "candidate_id": "x",
            "symbol": "000001",
            "source_trade_pnl_pct": 999.0,
            "_mark_prices": {"future": 1.0},
        }
    )
    projected = audit._artifact_row_projection(row)
    assert "source_trade_pnl_pct" not in projected
    assert "_mark_prices" not in projected
    assert all(not isinstance(value, (dict, list, tuple, set)) for value in projected.values())


@pytest.mark.parametrize("bad_value", [np.int64(1), np.array([1]), Path("x"), object()])
def test_candidate_artifact_rejects_non_native_json_scalars(bad_value: object):
    row = {field: None for field in audit.CANDIDATE_ARTIFACT_FIELDS}
    row["candidate_id"] = bad_value
    with pytest.raises(RuntimeError, match="not a JSON scalar"):
        audit._artifact_row_projection(row)


def test_replay_input_projection_strips_all_legacy_source_outcomes():
    source = {
        **_source(),
        **{field: object() for field in audit.FIELDS},
        "entry_day": object(),
        "exit_reason": object(),
        "holding_days": object(),
    }
    projected = audit.batch_base._replay_input_projection(source)
    assert not (set(projected) & audit.SOURCE_LEGACY_OUTCOME_FIELDS)


def test_immutable_evaluator_rejects_nested_mutation():
    source = [{"nested": {"values": [1, 2]}}]

    def mutating(rows):
        rows[0]["nested"]["values"].append(3)
        return {}

    with pytest.raises(RuntimeError, match="evaluator input mutated"):
        audit._run_immutable_evaluator(source, mutating, "test")
    assert source == [{"nested": {"values": [1, 2]}}]


def test_immutable_evaluator_accepts_read_only_evaluation():
    source = [{"value": 1}]
    result = audit._run_immutable_evaluator(
        source, lambda rows: sum(row["value"] for row in rows), "test"
    )
    assert result == 1
    assert source == [{"value": 1}]


def test_atomic_writer_publishes_six_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output = tmp_path / "final"
    prepared = {
        "output_dir": output,
        "staging_dir": tmp_path / "final.tmp",
        "static_initial": {},
        "history_initial": {},
    }
    rows = {
        split: [{"candidate_id": split, "split": split, "symbol": "000001"}]
        for split in audit.SPLITS
    }
    manifests = {split: audit._rows_manifest(rows[split]) for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: ({}, []))
    prepared["staging_dir"].mkdir()
    audit._write_outputs_atomically(
        prepared, rows, [{"cell": 1}], [{"day": 1}], result, manifests
    )
    assert output.exists()
    assert len(list(output.iterdir())) == 6
    assert not prepared["staging_dir"].exists()


def test_atomic_writer_keeps_staging_on_input_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output = tmp_path / "final"
    prepared = {
        "output_dir": output,
        "staging_dir": tmp_path / "final.tmp",
        "static_initial": {},
        "history_initial": {},
    }
    rows = {
        split: [{"candidate_id": split, "split": split, "symbol": "000001"}]
        for split in audit.SPLITS
    }
    manifests = {split: audit._rows_manifest(rows[split]) for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}
    calls = iter([({}, ["changed"]), ({}, [])])
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: next(calls))
    prepared["staging_dir"].mkdir()
    with pytest.raises(RuntimeError, match="changed before publish"):
        audit._write_outputs_atomically(
            prepared, rows, [], [], result, manifests
        )
    assert prepared["staging_dir"].exists()
    assert not output.exists()


def test_atomic_writer_rejects_joined_manifest_drift(tmp_path: Path):
    output = tmp_path / "final"
    staging = tmp_path / "final.tmp"
    staging.mkdir()
    prepared = {
        "output_dir": output,
        "staging_dir": staging,
        "static_initial": {},
        "history_initial": {},
    }
    rows = {
        split: [{"candidate_id": split, "split": split, "symbol": "000001"}]
        for split in audit.SPLITS
    }
    result = {"splits": {split: {} for split in audit.SPLITS}}
    bad = {split: "0" * 64 for split in audit.SPLITS}
    with pytest.raises(RuntimeError, match="changed before writer"):
        audit._write_outputs_atomically(prepared, rows, [], [], result, bad)


def test_atomic_writer_refuses_late_created_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output = tmp_path / "final"
    staging = tmp_path / "final.tmp"
    staging.mkdir()
    prepared = {
        "output_dir": output,
        "staging_dir": staging,
        "static_initial": {},
        "history_initial": {},
    }
    rows = {
        split: [{"candidate_id": split, "split": split, "symbol": "000001"}]
        for split in audit.SPLITS
    }
    manifests = {split: audit._rows_manifest(rows[split]) for split in audit.SPLITS}
    result = {"splits": {split: {} for split in audit.SPLITS}}
    calls = 0

    def snapshot(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            output.mkdir()
        return {}, []

    monkeypatch.setattr(audit, "_snapshot_changes", snapshot)
    with pytest.raises(RuntimeError, match="appeared before publish"):
        audit._write_outputs_atomically(
            prepared, rows, [], [], result, manifests
        )
    assert staging.exists()


def test_mocked_run_uses_exactly_one_replay_per_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sources = {
        split: [
            {
                **_source(day="2026-01-07"),
                **{field: 999.0 for field in audit.FIELDS},
            }
        ]
        for split in audit.SPLITS
    }
    prepared = {
        "input_dir": audit.CANONICAL_INPUT_DIR,
        "output_dir": audit.FORMAL_OUTPUT_DIR,
        "config_path": audit.CONFIG_PATH,
        "staging_dir": tmp_path / "formal.tmp",
        "static_initial": {},
        "history_initial": {},
        "dependency_contract": {},
        "config": _config(),
        "config_snapshot": audit.EXPECTED_CONFIG_SNAPSHOT,
        "sources_by_split": sources,
        "integrity_by_split": {split: {"all_pass": True} for split in audit.SPLITS},
        "history_inventory": {"symbol_count": 1, "file_count": 1},
        "history_manifest": audit.PREREGISTERED_HISTORY_MANIFEST_SHA256,
        "signal_day_coverage": {"all_pass": True},
        "path_feasibility": {split: {"coverage": 1.0} for split in audit.SPLITS},
        "index_closed": _closed(360),
        "regime_contexts": {},
        "regime_feasibility": {"all_regime_labels_match": True},
    }
    calls = 0

    def fake_replay(rows, config, profile, history_dir):
        nonlocal calls
        calls += 1
        assert not (set(rows[0]) & audit.SOURCE_LEGACY_OUTCOME_FIELDS)
        replay = {
            **rows[0],
            "candidate_id": audit.candidate_id(rows[0]),
            **{field: 1.0 for field in audit.FIELDS},
        }
        return [replay], Counter(), {
            "source_rows": 1,
            "common_eligible_rows": 1,
            "simulated_rows": 1,
        }

    enriched = {
        split: [
            {
                "candidate_id": audit.candidate_id(sources[split][0]),
                "split": split,
                "symbol": "000001",
                "signal_day": "2026-01-07",
                "normalized_regime": "bull",
                "normalized_signal_type": "buy_1",
                "exit_category": "risk_stop_loss",
            }
        ]
        for split in audit.SPLITS
    }
    monkeypatch.setattr(audit, "_prepare", lambda args: prepared)
    monkeypatch.setattr(audit, "_snapshot_changes", lambda value: ({}, []))
    monkeypatch.setattr(audit, "_production_replay_split", fake_replay)
    monkeypatch.setattr(
        audit.batch_base,
        "_assert_replay_integrity",
        lambda *args: (
            {"all_pass": True},
            {"baseline_replay_complete": True},
        ),
    )
    monkeypatch.setattr(audit.batch_base, "_validate_authoritative_outcomes", lambda rows: None)
    monkeypatch.setattr(audit, "_enrich_replay_paths", lambda replay, config: deepcopy(enriched))
    monkeypatch.setattr(audit, "_index_day_rows", lambda *args: [])
    monkeypatch.setattr(
        audit,
        "_write_outputs_atomically",
        lambda *args, **kwargs: args[4].update({"report_path": "report.json"}),
    )
    result = audit.run(_args())
    assert calls == 3
    assert result["replay_call_count_actual"] == 3
    assert result["batch_contract_pass"] is True
    assert result["production_decision"] == "unchanged_P0"
    assert prepared["staging_dir"].exists()


def test_formal_failure_before_replay_leaves_one_shot_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    prepared = {
        "staging_dir": tmp_path / "formal.tmp",
        "static_initial": {},
        "history_initial": {},
    }
    monkeypatch.setattr(audit, "_prepare", lambda args: prepared)
    monkeypatch.setattr(
        audit, "_snapshot_changes", lambda value: ({}, ["changed"])
    )
    with pytest.raises(RuntimeError, match="changed before replay"):
        audit.run(_args())
    assert prepared["staging_dir"].exists()


@pytest.mark.skipif(
    audit.FORMAL_OUTPUT_DIR.exists()
    or audit.FORMAL_OUTPUT_DIR.with_name(audit.FORMAL_OUTPUT_DIR.name + ".tmp").exists(),
    reason="formal output path is already consumed",
)
def test_real_preflight_is_read_only_and_replay_free(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        audit,
        "_production_replay_split",
        lambda *args: pytest.fail("preflight must not call replay"),
    )
    report = audit.preflight(_args(preflight_only=True))
    assert report["technical_preflight_pass"] is True
    assert report["canonical_rows"] == {"train": 89, "val": 1107, "test": 346}
    assert report["history_symbol_count"] == 809
    assert report["history_file_count"] == 809
    assert report["regime_feasibility"]["all_regime_labels_match"] is True
    assert report["replay_performed"] is False
    assert report["output_generated"] is False
    assert not audit.FORMAL_OUTPUT_DIR.exists()


def test_main_routes_preflight_without_run(monkeypatch: pytest.MonkeyPatch, capsys):
    args = _args(preflight_only=True)
    monkeypatch.setattr(audit, "_parser", lambda: type("P", (), {"parse_args": lambda self: args})())
    monkeypatch.setattr(audit, "preflight", lambda value: {"ok": True})
    monkeypatch.setattr(audit, "run", lambda value: pytest.fail("formal run must not start"))
    assert audit.main() == 0
    assert '"ok":true' in capsys.readouterr().out
