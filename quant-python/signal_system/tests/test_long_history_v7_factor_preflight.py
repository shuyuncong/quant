import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import long_history_v7_factor_preflight as preflight


def _history(periods: int = 100, *, start: str = "2024-01-02") -> pd.DataFrame:
    days = pd.bdate_range(start, periods=periods)
    close = [10.0 + index * 0.05 for index in range(periods)]
    return pd.DataFrame(
        {
            "datetime": days,
            "open": [value - 0.1 for value in close],
            "high": [value + 0.2 for value in close],
            "low": [value - 0.2 for value in close],
            "close": close,
            "volume": [1000.0 + index for index in range(periods)],
            "amount": [0.0] * periods,
            "turnover_rate": [1.0 + index / 100.0 for index in range(periods)],
            "circulating_market_cap": [100.0 + index for index in range(periods)],
            "is_closed": [True] * periods,
        }
    )


def _candidate(frame: pd.DataFrame, **extra) -> dict:
    signal_day = frame["datetime"].iloc[-2].date().isoformat()
    cross_day = frame["datetime"].iloc[-5].date().isoformat()
    row = {
        "candidate_id": "000001|candidate",
        "symbol": "000001",
        "signal_day": signal_day,
        "signal_type": "macd_golden_cross_pullback_confirmed_above",
        "cross_day": cross_day,
        "confirmation_bars": 3,
    }
    row.update(extra)
    return row


def _sanitized(frame: pd.DataFrame, **extra) -> dict:
    return preflight._sanitize_candidate(_candidate(frame, **extra))


def _write_pickle(path: Path, frame: pd.DataFrame) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_pickle(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _fake_audits(
    source_path: Path,
    fold_path: Path,
    index_path: Path,
    qfq_path: Path,
    stock_path: Path,
    candidate: dict,
) -> tuple[dict, dict]:
    qfq_hash = hashlib.sha256(qfq_path.read_bytes()).hexdigest()
    stock_hash = hashlib.sha256(stock_path.read_bytes()).hexdigest()
    index_hash = hashlib.sha256(index_path.read_bytes()).hexdigest()
    source = {
        "checks_passed": True,
        "candidate_ids_sha256": "candidate-manifest",
        "_candidates": [candidate],
        "_report_value": {
            "filters": {"stock_pool": {"volume_unit_shares": 100.0}},
            "window": {"end": "2026-09-01"},
        },
        "frozen_inputs": {
            "index_data": {"path": str(index_path), "sha256": index_hash},
            "qfq_history": {
                "manifest_sha256": "full-qfq-manifest",
                "_symbol_path": {"000001": str(qfq_path)},
                "_symbol_sha256": {"000001": qfq_hash},
            },
        },
        "stock_pool_cache": {
            "manifest_sha256": "stock-manifest",
            "_path_sha256": {str(stock_path.resolve()): stock_hash},
        },
    }
    folds = {
        "checks_passed": True,
        "dataset_candidate_ids_sha256": "candidate-manifest",
        "dataset_candidates": 1,
        "qfq_manifest_sha256": qfq_hash,
        "_report_value": {
            "input": {"qfq_history": {"history_dir": str(qfq_path.parent)}}
        },
    }
    return source, folds


def _patch_build_inputs(monkeypatch, tmp_path: Path, *, outcome: float = 1.0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    frame = _history()
    candidate = _candidate(frame, trade_pnl_pct=outcome, exit_reason="timeout")
    source_path = tmp_path / "source.json"
    fold_path = tmp_path / "fold.json"
    index_path = tmp_path / "index.pkl"
    qfq_path = tmp_path / "history" / "000001_qfq.pkl"
    stock_path = tmp_path / "stock.pkl"
    _write_json(source_path, {})
    _write_json(fold_path, {})
    _write_pickle(index_path, frame.assign(close=frame["close"] * 1.01))
    _write_pickle(qfq_path, frame)
    _write_pickle(stock_path, frame)
    source, folds = _fake_audits(
        source_path,
        fold_path,
        index_path,
        qfq_path,
        stock_path,
        candidate,
    )
    monkeypatch.setattr(preflight, "audit_source_report", lambda _path: source)
    monkeypatch.setattr(preflight, "audit_fold_report", lambda _path, _source: folds)
    calls = []

    def load_stock(symbol, **kwargs):
        calls.append((symbol, kwargs))
        loaded = pd.read_pickle(stock_path)
        loaded.attrs["stock_pool_history_source"] = "hashed_local_cache"
        loaded.attrs["stock_pool_history_path"] = str(stock_path.resolve())
        return loaded

    monkeypatch.setattr(preflight, "load_stock_pool_history", load_stock)
    return source_path, fold_path, index_path, stock_path, calls


def test_factor_schema_and_amount_fallback_are_frozen():
    frame = _history()
    candidate = _sanitized(frame)
    signal_day = pd.Timestamp(candidate["signal_day"]).date()
    qfq = preflight._as_of(
        preflight._prepare_history(frame, label="qfq"), signal_day, label="qfq"
    )
    index = preflight._as_of(
        preflight._prepare_history(
            frame.assign(close=frame["close"] * 1.01), label="index"
        ),
        signal_day,
        label="index",
    )
    stock = preflight._as_of(
        preflight._prepare_history(frame, label="stock"),
        signal_day,
        label="stock",
    )
    result = preflight.compute_candidate_features(
        candidate, qfq, index, stock, volume_unit_shares=100.0
    )
    assert tuple(result["features"]) == preflight.FACTOR_NAMES
    assert tuple(result["missing"]) == preflight.FACTOR_NAMES
    assert result["features"]["amount_to_median_20"] is not None
    assert result["features"]["cross_age_trading_days"] == 3
    assert result["features"]["beta_60"] is not None


def test_future_bars_do_not_change_candidate_features():
    frame = _history()
    candidate = _sanitized(frame)
    signal_day = pd.Timestamp(candidate["signal_day"]).date()

    def calculate(source: pd.DataFrame) -> dict:
        qfq = preflight._as_of(
            preflight._prepare_history(source, label="qfq"), signal_day, label="qfq"
        )
        index = preflight._as_of(
            preflight._prepare_history(source, label="index"), signal_day, label="index"
        )
        stock = preflight._as_of(
            preflight._prepare_history(source, label="stock"), signal_day, label="stock"
        )
        return preflight.compute_candidate_features(
            candidate, qfq, index, stock, volume_unit_shares=100.0
        )

    baseline = calculate(frame)
    future = frame.copy()
    future.loc[len(future)] = {
        "datetime": frame["datetime"].iloc[-1] + pd.Timedelta(days=30),
        "open": 999.0,
        "high": 1200.0,
        "low": 1.0,
        "close": 1000.0,
        "volume": 999999.0,
        "amount": 999999999.0,
        "turnover_rate": 99.0,
        "circulating_market_cap": 999999.0,
        "is_closed": True,
    }
    assert calculate(future) == baseline


def test_outcome_changes_do_not_change_public_candidate_payload():
    frame = _history()
    low = preflight._sanitize_candidate(
        _candidate(frame, trade_pnl_pct=-99.0, exit_reason="stop")
    )
    high = preflight._sanitize_candidate(
        _candidate(frame, trade_pnl_pct=999.0, exit_reason="take_profit")
    )
    assert low == high
    payload = json.dumps(low, sort_keys=True)
    assert "pnl" not in payload.lower()
    assert "exit" not in payload.lower()


def test_core_candidate_fields_and_cross_boundary_fail_closed():
    frame = _history()
    missing = _candidate(frame)
    missing.pop("signal_type")
    with pytest.raises(preflight.FactorPreflightError, match="missing core fields"):
        preflight._sanitize_candidate(missing)
    future_cross = _candidate(
        frame,
        cross_day=(frame["datetime"].iloc[-1] + pd.Timedelta(days=10))
        .date()
        .isoformat(),
    )
    with pytest.raises(preflight.FactorPreflightError, match="cross_day follows"):
        preflight._sanitize_candidate(future_cross)


def test_cross_metadata_is_required_for_macd_and_atomic_when_present():
    frame = _history()
    macd_missing = _candidate(frame)
    macd_missing.pop("cross_day")
    macd_missing.pop("confirmation_bars")
    with pytest.raises(preflight.FactorPreflightError, match="missing cross metadata"):
        preflight._sanitize_candidate(macd_missing)
    partial = _candidate(frame)
    partial.pop("confirmation_bars")
    with pytest.raises(preflight.FactorPreflightError, match="partial cross metadata"):
        preflight._sanitize_candidate(partial)


def test_legacy_buy_signal_preserves_missing_cross_factors():
    frame = _history()
    legacy = _candidate(frame, signal_type="buy_1")
    legacy.pop("cross_day")
    legacy.pop("confirmation_bars")
    candidate = preflight._sanitize_candidate(legacy)
    assert candidate["source_fields"] == {
        "cross_day": None,
        "confirmation_bars": None,
    }
    signal_day = pd.Timestamp(candidate["signal_day"]).date()
    qfq = preflight._as_of(
        preflight._prepare_history(frame, label="qfq"), signal_day, label="qfq"
    )
    index = preflight._as_of(
        preflight._prepare_history(frame, label="index"), signal_day, label="index"
    )
    stock = preflight._as_of(
        preflight._prepare_history(frame, label="stock"), signal_day, label="stock"
    )
    result = preflight.compute_candidate_features(
        candidate, qfq, index, stock, volume_unit_shares=100.0
    )
    assert result["features"]["confirmation_bars"] is None
    assert result["features"]["cross_age_trading_days"] is None
    assert result["missing"]["confirmation_bars"] is True
    assert result["missing"]["cross_age_trading_days"] is True


def test_short_index_history_is_reported_as_missing_not_backfilled():
    frame = _history()
    candidate = _sanitized(frame)
    signal_day = pd.Timestamp(candidate["signal_day"]).date()
    qfq = preflight._as_of(
        preflight._prepare_history(frame, label="qfq"), signal_day, label="qfq"
    )
    index = preflight._as_of(
        preflight._prepare_history(frame.tail(30), label="index"),
        signal_day,
        label="index",
    )
    stock = preflight._as_of(
        preflight._prepare_history(frame, label="stock"), signal_day, label="stock"
    )
    result = preflight.compute_candidate_features(
        candidate, qfq, index, stock, volume_unit_shares=100.0
    )
    assert result["features"]["index_return_20"] is not None
    assert result["features"]["excess_return_60"] is None
    assert result["features"]["beta_60"] is None
    assert result["missing"]["stock_index_correlation_60"] is True


def test_holdout_paths_are_blocked(tmp_path):
    with pytest.raises(preflight.FactorPreflightError, match="Holdout"):
        preflight._guard_development_path(tmp_path / "reserved_holdout" / "run")


def test_existing_output_is_blocked(tmp_path):
    output = tmp_path / "exists"
    output.mkdir()
    with pytest.raises(preflight.FactorPreflightError, match="refusing overwrite"):
        preflight.build_report(
            tmp_path / "source.json",
            tmp_path / "fold.json",
            tmp_path / "index.pkl",
            output,
        )


def test_build_snapshot_uses_local_only_loader_and_emits_no_outcome(
    monkeypatch, tmp_path
):
    source_path, fold_path, index_path, _stock_path, calls = _patch_build_inputs(
        monkeypatch, tmp_path
    )
    snapshot = preflight.build_snapshot(source_path, fold_path, index_path)
    assert len(snapshot["candidate_features"]) == 1
    assert calls[0][1]["local_only"] is True
    assert calls[0][1]["history_bars"] == preflight.STOCK_POOL_HISTORY_BARS
    assert calls[0][1]["end"].isoformat() == "2026-09-01"
    serialized = json.dumps(snapshot["candidate_features"], sort_keys=True)
    assert "trade_pnl_pct" not in serialized
    assert "exit_reason" not in serialized
    assert snapshot["coverage"][0]["candidates"] == 1


def test_build_snapshot_is_invariant_to_outcome_values(monkeypatch, tmp_path):
    source_path, fold_path, index_path, _stock_path, _calls = _patch_build_inputs(
        monkeypatch, tmp_path, outcome=-50.0
    )
    first = preflight.build_snapshot(source_path, fold_path, index_path)
    source = preflight.audit_source_report(source_path)
    source["_candidates"][0]["trade_pnl_pct"] = 500.0
    source["_candidates"][0]["exit_reason"] = "changed"
    second = preflight.build_snapshot(source_path, fold_path, index_path)
    for artifact in preflight.ARTIFACT_NAMES:
        assert first[artifact] == second[artifact]


def test_stock_pool_manifest_drift_fails_closed(monkeypatch, tmp_path):
    source_path, fold_path, index_path, stock_path, _calls = _patch_build_inputs(
        monkeypatch, tmp_path
    )
    stock_path.write_bytes(stock_path.read_bytes() + b"drift")
    with pytest.raises(preflight.FactorPreflightError, match="drifted"):
        preflight.build_snapshot(source_path, fold_path, index_path)


def test_explicit_index_must_match_frozen_v5_path(monkeypatch, tmp_path):
    source_path, fold_path, index_path, _stock_path, _calls = _patch_build_inputs(
        monkeypatch, tmp_path
    )
    alternate = tmp_path / "alternate-index.pkl"
    alternate.write_bytes(index_path.read_bytes())
    with pytest.raises(preflight.FactorPreflightError, match="index path differs"):
        preflight.build_snapshot(source_path, fold_path, alternate)


def test_qfq_manifest_drift_fails_closed(monkeypatch, tmp_path):
    source_path, fold_path, index_path, _stock_path, _calls = _patch_build_inputs(
        monkeypatch, tmp_path
    )
    source = preflight.audit_source_report(source_path)
    qfq_path = Path(source["frozen_inputs"]["qfq_history"]["_symbol_path"]["000001"])
    qfq_path.write_bytes(qfq_path.read_bytes() + b"drift")
    with pytest.raises(preflight.FactorPreflightError, match="QFQ SHA256 drift"):
        preflight.build_snapshot(source_path, fold_path, index_path)


def test_build_report_writes_create_once_outcome_free_artifacts(monkeypatch, tmp_path):
    source_path, fold_path, index_path, _stock_path, _calls = _patch_build_inputs(
        monkeypatch, tmp_path
    )
    output = tmp_path / "v7"
    report = preflight.build_report(source_path, fold_path, index_path, output)
    assert report["passes_preflight"] is True
    assert report["model_fitted"] is False
    assert report["outcomes_read"] is False
    assert report["factor_contract"]["factor_count"] == 19
    assert set(report["artifacts"]) == set(preflight.ARTIFACT_NAMES)
    candidate_text = (output / "candidate_features.jsonl").read_text(encoding="utf-8")
    assert "pnl" not in candidate_text.lower()
    assert "exit" not in candidate_text.lower()
