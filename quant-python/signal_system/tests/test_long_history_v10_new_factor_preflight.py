import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import long_history_v10_new_factor_preflight as preflight
import long_history_v10_new_factor_preflight_audit as audit


def _history(periods: int = 90, *, start: str = "2024-01-02") -> pd.DataFrame:
    offset = np.arange(periods, dtype=float)
    daily_return = 0.001 + 0.006 * np.sin(offset / 4.0)
    close = 20.0 * np.cumprod(1.0 + daily_return)
    volume = 1000.0 + 5.0 * offset + 40.0 * np.cos(offset / 5.0)
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range(start, periods=periods),
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
            "amount": np.zeros(periods),
            "is_closed": np.ones(periods, dtype=bool),
        }
    )


def _index_history(periods: int = 90, *, start: str = "2024-01-02") -> pd.DataFrame:
    offset = np.arange(periods, dtype=float)
    daily_return = 0.0005 + 0.004 * np.cos(offset / 6.0)
    close = 3000.0 * np.cumprod(1.0 + daily_return)
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range(start, periods=periods),
            "open": close * 0.998,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1_000_000.0 + 100.0 * offset,
            "amount": close * (1_000_000.0 + 100.0 * offset),
            "is_closed": np.ones(periods, dtype=bool),
        }
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _valid_v9a_report() -> dict:
    checks = {name: True for name in preflight.V9A_FAILED_CHECKS}
    checks.update(
        {
            "all_six_models_fitted_and_converged": True,
            "at_least_four_folds_risk_capture_above_random": True,
        }
    )
    for name in preflight.V9A_FAILED_CHECKS:
        checks[name] = False
    return {
        "version": "long_history_v9_bottom_tail_risk.v1",
        "passes_research_screen": False,
        "production_eligible": False,
        "screen": {
            "checks": checks,
            "passes_research_screen": False,
            "production_eligible": False,
        },
    }


def _patch_build_inputs(monkeypatch, tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    stock = _history()
    index = _index_history()
    signal_day = stock["datetime"].iloc[-2].date().isoformat()
    candidate = {
        "candidate_id": f"000001|{signal_day}|buy_1",
        "symbol": "000001",
        "signal_day": signal_day,
        "signal_type": "buy_1",
        "features": {"rank_recent_return": 0.5},
        "missing": {"rank_recent_return": False},
        "source_fields": {"cross_day": None, "confirmation_bars": None},
        "data_cutoffs": {"qfq_max_day_used": signal_day},
    }
    source_path = tmp_path / "source.json"
    fold_path = tmp_path / "fold.json"
    index_path = tmp_path / "index.pkl"
    qfq_path = tmp_path / "history" / "000001_qfq.pkl"
    v7_report_path = tmp_path / "v7" / "report.json"
    v7_artifact_path = tmp_path / "v7" / "candidate_features.jsonl"
    v9a_report_path = tmp_path / "v9a" / "report.json"
    _write_json(source_path, {})
    _write_json(fold_path, {})
    index.to_pickle(index_path)
    qfq_path.parent.mkdir(parents=True, exist_ok=True)
    stock.to_pickle(qfq_path)
    _write_jsonl(v7_artifact_path, [candidate])
    _write_json(v9a_report_path, _valid_v9a_report())
    v7_report_value = {
        "input": {
            "source_report": {"path": str(source_path), "sha256": _sha256(source_path)},
            "fold_report": {"path": str(fold_path), "sha256": _sha256(fold_path)},
            "index_data": {"path": str(index_path), "sha256": _sha256(index_path)},
        }
    }
    _write_json(v7_report_path, v7_report_value)
    v7_run = {
        "checks_passed": True,
        "candidate_count": 1,
        "factor_count": 19,
        "_report_value": v7_report_value,
        "_artifact_paths": {"candidate_features": v7_artifact_path},
    }
    source_audit = {
        "checks_passed": True,
        "_candidates": [
            {"candidate_id": candidate["candidate_id"], "trade_pnl_pct": -99.0}
        ],
        "frozen_inputs": {
            "index_data": {"path": str(index_path), "sha256": _sha256(index_path)},
            "qfq_history": {
                "_symbol_path": {"000001": str(qfq_path.resolve())},
                "_symbol_sha256": {"000001": _sha256(qfq_path)},
            },
        },
    }
    fold_audit = {
        "checks_passed": True,
        "dataset_candidates": 1,
        "dataset_candidate_ids_sha256": "candidate-ids",
        "_report_value": {
            "input": {"qfq_history": {"history_dir": str(qfq_path.parent)}}
        },
    }
    monkeypatch.setattr(preflight, "_assert_frozen_inputs", lambda *_args: None)
    monkeypatch.setattr(preflight.v7audit, "audit_run", lambda _path: v7_run)
    monkeypatch.setattr(
        preflight.v7preflight, "audit_source_report", lambda _path: source_audit
    )
    monkeypatch.setattr(
        preflight.v7preflight,
        "audit_fold_report",
        lambda _path, _source: fold_audit,
    )
    monkeypatch.setattr(preflight, "V7_FACTOR_REPORT_SHA256", _sha256(v7_report_path))
    monkeypatch.setattr(preflight, "V9A_PARENT_REPORT_SHA256", _sha256(v9a_report_path))
    return {
        "v7": v7_report_path,
        "source": source_path,
        "fold": fold_path,
        "index": index_path,
        "qfq": qfq_path,
        "v9a": v9a_report_path,
        "candidate": candidate,
        "source_audit": source_audit,
    }


def _build_snapshot(paths: dict) -> dict:
    return preflight.build_snapshot(
        paths["v7"],
        paths["source"],
        paths["fold"],
        paths["index"],
        paths["v9a"],
    )


def _build_report(paths: dict, output: Path) -> dict:
    return preflight.build_report(
        paths["v7"],
        paths["source"],
        paths["fold"],
        paths["index"],
        paths["v9a"],
        output,
    )


def test_factor_contract_is_new_and_independent_formula_matches_manual_values():
    assert len(preflight.FACTOR_NAMES) == 12
    assert len(set(preflight.FACTOR_NAMES)) == 12
    assert set(preflight.FACTOR_NAMES).isdisjoint(preflight.v7preflight.FACTOR_NAMES)
    stock = _history()
    index = _index_history()
    candidate = {"candidate_id": "manual"}
    actual = preflight.compute_candidate_features(candidate, stock, index)
    independent = audit._independent_candidate_features(candidate, stock, index)
    assert actual == independent
    assert not any(actual["missing"].values())

    stock_close = stock["close"].to_numpy(dtype=float)
    index_close = index["close"].to_numpy(dtype=float)
    stock_returns = stock_close[-21:][1:] / stock_close[-21:][:-1] - 1.0
    index_returns = index_close[-21:][1:] / index_close[-21:][:-1] - 1.0
    index_delta = index_returns - index_returns.mean()
    stock_delta = stock_returns - stock_returns.mean()
    beta = float(stock_delta @ index_delta) / float(index_delta @ index_delta)
    residual = stock_returns - beta * index_returns
    effective_amount = stock["close"].to_numpy() * stock["volume"].to_numpy()
    expected = {
        "index_return_60": index_close[-1] / index_close[-61] - 1.0,
        "index_realized_volatility_20": np.std(index_returns, ddof=0),
        "index_drawdown_from_high_60": index_close[-1] / index_close[-60:].max() - 1.0,
        "excess_return_5": (
            stock_close[-1] / stock_close[-6] - index_close[-1] / index_close[-6]
        ),
        "stock_index_correlation_20": np.corrcoef(stock_returns, index_returns)[0, 1],
        "residual_volatility_20": np.std(residual, ddof=0),
        "stock_realized_volatility_20": np.std(stock_returns, ddof=0),
        "drawdown_from_high_60": stock_close[-1] / stock_close[-60:].max() - 1.0,
        "price_efficiency_20": abs(stock_close[-1] / stock_close[-21] - 1.0)
        / np.abs(stock_returns).sum(),
        "volume_mean_5_to_20": stock["volume"].iloc[-5:].mean()
        / stock["volume"].iloc[-20:].mean(),
        "amount_mean_5_to_20": effective_amount[-5:].mean()
        / effective_amount[-20:].mean(),
        "return_volume_change_correlation_20": np.corrcoef(
            stock_returns, np.diff(np.log(stock["volume"].to_numpy()[-21:]))
        )[0, 1],
    }
    for name, value in expected.items():
        assert actual["features"][name] == pytest.approx(value, abs=2e-12)


def test_future_bars_do_not_change_features_and_short_history_is_not_backfilled():
    stock = _history()
    index = _index_history()
    signal_day = stock["datetime"].iloc[-2].date()
    candidate = {"candidate_id": "cutoff"}
    stock_cut = preflight.v7preflight._as_of(stock, signal_day, label="stock")
    index_cut = preflight.v7preflight._as_of(index, signal_day, label="index")
    before = preflight.compute_candidate_features(candidate, stock_cut, index_cut)
    future_stock = stock.copy()
    future_stock.loc[future_stock.index[-1], ["close", "volume", "amount"]] = 1e12
    future_index = index.copy()
    future_index.loc[future_index.index[-1], "close"] = 1e12
    after = preflight.compute_candidate_features(
        candidate,
        preflight.v7preflight._as_of(future_stock, signal_day, label="stock"),
        preflight.v7preflight._as_of(future_index, signal_day, label="index"),
    )
    assert before == after

    short = preflight.compute_candidate_features(
        candidate, stock.iloc[:10], index.iloc[:10]
    )
    for name in (
        "index_return_60",
        "stock_index_correlation_20",
        "residual_volatility_20",
        "drawdown_from_high_60",
        "return_volume_change_correlation_20",
    ):
        assert short["features"][name] is None
        assert short["missing"][name] is True


def test_input_and_path_guards_fail_closed(tmp_path):
    with pytest.raises(preflight.NewFactorPreflightError, match="QFQ close"):
        preflight.compute_candidate_features(
            {"candidate_id": "bad"}, _history().drop(columns="close"), _index_history()
        )
    with pytest.raises(preflight.NewFactorPreflightError, match="Holdout"):
        preflight._guard_development_path(tmp_path / "reserved_holdout" / "run")
    output = tmp_path / "exists"
    output.mkdir()
    with pytest.raises(preflight.NewFactorPreflightError, match="refusing overwrite"):
        preflight.build_report(
            tmp_path / "v7.json",
            tmp_path / "source.json",
            tmp_path / "fold.json",
            tmp_path / "index.pkl",
            tmp_path / "v9.json",
            output,
        )


def test_build_snapshot_is_outcome_blind_and_emits_only_public_identity(
    monkeypatch, tmp_path
):
    paths = _patch_build_inputs(monkeypatch, tmp_path)
    first = _build_snapshot(paths)
    paths["source_audit"]["_candidates"][0]["trade_pnl_pct"] = 999.0
    paths["source_audit"]["_candidates"][0]["exit_reason"] = "changed"
    second = _build_snapshot(paths)
    for name in preflight.ARTIFACT_NAMES:
        assert first[name] == second[name]
    row = first["candidate_features"][0]
    assert set(row) == {
        *preflight.PUBLIC_CANDIDATE_FIELDS,
        "features",
        "missing",
        "data_cutoffs",
    }
    serialized = json.dumps(row, sort_keys=True).lower()
    assert "pnl" not in serialized
    assert "exit" not in serialized
    assert row["data_cutoffs"]["qfq_max_day_used"] <= row["signal_day"]
    assert row["data_cutoffs"]["index_max_day_used"] <= row["signal_day"]


def test_outcome_field_qfq_drift_and_parent_screen_drift_fail_closed(
    monkeypatch, tmp_path
):
    paths = _patch_build_inputs(monkeypatch, tmp_path)
    v7_artifact = Path(
        preflight.v7audit.audit_run(paths["v7"])["_artifact_paths"][
            "candidate_features"
        ]
    )
    row = json.loads(v7_artifact.read_text(encoding="utf-8"))
    row["trade_pnl_pct"] = 5.0
    _write_jsonl(v7_artifact, [row])
    with pytest.raises(preflight.NewFactorPreflightError, match="outcome field"):
        _build_snapshot(paths)

    del row["trade_pnl_pct"]
    _write_jsonl(v7_artifact, [row])
    paths["qfq"].write_bytes(paths["qfq"].read_bytes() + b"drift")
    with pytest.raises(preflight.NewFactorPreflightError, match="QFQ hash drift"):
        _build_snapshot(paths)

    paths = _patch_build_inputs(monkeypatch, tmp_path / "parent")
    parent = _valid_v9a_report()
    parent["passes_research_screen"] = True
    _write_json(paths["v9a"], parent)
    with pytest.raises(preflight.NewFactorPreflightError, match="was not failed"):
        _build_snapshot(paths)


def test_build_report_writes_four_create_once_outcome_free_artifacts(
    monkeypatch, tmp_path
):
    paths = _patch_build_inputs(monkeypatch, tmp_path / "inputs")
    output = tmp_path / "v10"
    report = _build_report(paths, output)
    assert report["passes_preflight"] is True
    assert report["factor_contract"]["factor_count"] == 12
    assert report["parent_decision"]["passes_research_screen"] is False
    assert report["model_fitted"] is False
    assert report["candidate_outcomes_read"] is False
    assert report["factor_selection_performed"] is False
    assert report["hyperparameters_selected"] is False
    assert report["holdout_used"] is False
    assert report["production_eligible"] is False
    assert set(report["artifacts"]) == set(preflight.ARTIFACT_NAMES)
    assert (output / "report.json").is_file()
    assert all(
        (output / f"{name}.jsonl").is_file() for name in preflight.ARTIFACT_NAMES
    )
    candidate_text = (output / "candidate_features.jsonl").read_text(encoding="utf-8")
    assert "pnl" not in candidate_text.lower()
    assert "exit" not in candidate_text.lower()
