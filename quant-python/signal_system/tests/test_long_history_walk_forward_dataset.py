import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from candidate_integrity import CandidateIntegrityError  # noqa: E402
from long_history_walk_forward_dataset import (  # noqa: E402
    _guard_development_path,
    _prepare_candidates,
    _source_report_audit,
    build_dataset,
    build_fold_boundaries,
)


def _candidate(
    symbol: str,
    signal_day: str,
    entry_day: str,
    exit_day: str,
    pnl: float = 1.0,
) -> dict:
    return {
        "symbol": symbol,
        "signal_day": signal_day,
        "entry_day": entry_day,
        "exit_day": exit_day,
        "signal_type": "buy_1",
        "entry_price": 10.0,
        "exit_price": 10.1,
        "trade_pnl_pct": pnl,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _write_universe(root: Path, symbols: list[str]) -> tuple[dict, str]:
    source = root / "universe.pkl"
    pd.DataFrame(
        [{"code": symbol, "name": ""} for symbol in symbols]
    ).to_pickle(source)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = root / "universe_manifest.jsonl"
    _write_jsonl(manifest, [{"symbol": symbol, "name": ""} for symbol in symbols])
    symbol_manifest = hashlib.sha256(
        "\n".join(symbols).encode("utf-8")
    ).hexdigest()
    return (
        {
            "source": "frozen_universe_file",
            "strict": True,
            "source_path": str(source),
            "source_sha256": source_sha256,
            "expected_sha256": source_sha256,
            "source_symbols": len(symbols),
            "selected_symbols": len(symbols),
            "symbol_manifest_sha256": symbol_manifest,
        },
        str(manifest),
    )


def _write_frozen_inputs(
    root: Path, history_dir: Path, index_path: Path, symbols: list[str]
) -> tuple[dict, str]:
    engine = root / "backtest_engine.py"
    engine.write_text("# frozen engine\n", encoding="utf-8")
    signal_engine = root / "signal_engine.py"
    signal_engine.write_text("# frozen signal engine\n", encoding="utf-8")
    rows: list[dict] = []
    entries: list[str] = []
    for symbol in symbols:
        path = history_dir / f"{symbol}_qfq.pkl"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(
            {
                "symbol": symbol,
                "file": path.name,
                "path": str(path),
                "exists": True,
                "sha256": digest,
            }
        )
        entries.append(f"{symbol}|{digest}")
    manifest = root / "qfq_history_manifest.jsonl"
    _write_jsonl(manifest, rows)
    return (
        {
            "index_data": {
                "path": str(index_path),
                "sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
            },
            "backtest_engine": {
                "path": str(engine),
                "sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
            },
            "signal_engine": {
                "path": str(signal_engine),
                "sha256": hashlib.sha256(signal_engine.read_bytes()).hexdigest(),
            },
            "qfq_history": {
                "symbols": len(symbols),
                "missing_symbols": 0,
                "manifest_sha256": hashlib.sha256(
                    "\n".join(entries).encode("utf-8")
                ).hexdigest(),
            },
        },
        str(manifest),
    )


def _write_preflight(
    root: Path,
    universe: dict,
    frozen_inputs: dict,
    qfq_manifest: str,
    config_path: Path,
    *,
    stock_pool: dict | None = None,
    start: str = "2023-09-01",
    end: str = "2024-09-30",
    history_bars: int = 1200,
) -> tuple[dict, str]:
    stock_pool = stock_pool or {}
    qfq_rows = [
        json.loads(line)
        for line in Path(qfq_manifest).read_text(encoding="utf-8").splitlines()
    ]
    checks = {
        "data_adjustment_is_qfq": True,
        "local_data_only": True,
        "fetch_missing_adjusted_disabled": True,
        "incomplete_trades_excluded": True,
        "dataset_role_is_full": True,
        "strict_frozen_universe": True,
        "stock_pool_missing_data_policy_is_reject": True,
        "index_data_explicit": True,
        "index_data_readable": True,
        "all_universe_qfq_loadable": True,
        "stock_pool_coverage_ready": True,
    }
    summary = {
        "symbols": universe["selected_symbols"],
        "blocking_symbols": 0,
        "blocking_by_reason": {},
        "nonblocking_by_reason": {},
        "warning_by_reason": {},
    }
    preflight = {
        "version": "long_history_preflight.v1",
        "mode": "read_only_preflight",
        "passes_preflight": True,
        "production_eligible": False,
        "policy": {
            "holdout_used": False,
            "network_used": False,
            "database_used": False,
            "writes_performed": False,
        },
        "effective_run": {
            "start": start,
            "end": end,
            "history_bars": history_bars,
            "adjustment": "qfq",
            "local_data_only": True,
            "fetch_missing_adjusted": False,
            "allow_incomplete": False,
            "dataset_role": "full",
            "stock_pool": stock_pool,
        },
        "inputs": {
            "config": {
                "path": str(config_path),
                "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            },
            "index_data": frozen_inputs["index_data"],
            "universe": universe,
        },
        "checks": checks,
        "summary": summary,
        "symbols": [
            {
                "symbol": row["symbol"],
                "qfq": {
                    "path": row["path"],
                    "sha256": row["sha256"],
                    "bars": len(pd.read_pickle(row["path"])),
                },
                "stock_pool": {},
                "blocking_issues": {},
                "nonblocking_issues": {},
            }
            for row in qfq_rows
        ],
    }
    artifact = root / "preflight.json"
    artifact.write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return (
        {
            "version": preflight["version"],
            "passes_preflight": True,
            "artifact": str(artifact),
            "artifact_sha256": digest,
            "checks": checks,
            "summary": summary,
            "universe_source_sha256": universe["source_sha256"],
            "universe_symbol_manifest_sha256": universe[
                "symbol_manifest_sha256"
            ],
            "index_sha256": frozen_inputs["index_data"]["sha256"],
            "full_result_sha256": digest,
        },
        str(artifact),
    )


def test_guard_blocks_holdout_paths_by_default(tmp_path):
    path = tmp_path / "future_holdout" / "report.json"
    with pytest.raises(ValueError, match="Holdout path is blocked"):
        _guard_development_path(path)
    assert _guard_development_path(path, True).name == "report.json"


def test_fold_boundaries_are_contiguous_calendar_windows():
    boundaries = build_fold_boundaries(
        date(2024, 7, 1), date(2025, 3, 31), evaluation_months=3
    )
    assert boundaries == [
        (date(2024, 7, 1), date(2024, 9, 30)),
        (date(2024, 10, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 3, 31)),
    ]


def test_prepare_candidates_rejects_conflicting_duplicate_ids():
    first = _candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")
    second = dict(first)
    second["trade_pnl_pct"] = 2.0
    with pytest.raises(CandidateIntegrityError, match="conflicting duplicate"):
        _prepare_candidates([first, second])


def test_dataset_purges_training_labels_crossing_evaluation_start(
    tmp_path, monkeypatch
):
    candidates_path = tmp_path / "signals.jsonl"
    source_report_path = tmp_path / "source_report.json"
    index_path = tmp_path / "index.pkl"
    output_dir = tmp_path / "folds"
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    rows = [
        _candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10"),
        _candidate("000002", "2024-06-27", "2024-06-28", "2024-07-05"),
        _candidate("000003", "2024-07-01", "2024-07-02", "2024-07-10"),
    ]
    _write_jsonl(candidates_path, rows)
    index_path.write_bytes(b"index")
    for symbol in ("000001", "000002", "000003"):
        pd.DataFrame(
            {
                "datetime": pd.bdate_range("2023-09-01", periods=100),
                "close": [10.0] * 100,
                "is_closed": [True] * 100,
            }
        ).to_pickle(history_dir / f"{symbol}_qfq.pkl")
    universe, universe_manifest = _write_universe(
        tmp_path, ["000001", "000002", "000003"]
    )
    frozen_inputs, qfq_manifest = _write_frozen_inputs(
        tmp_path,
        history_dir,
        index_path,
        ["000001", "000002", "000003"],
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("stock_pool: {}\n", encoding="utf-8")
    preflight_attestation, preflight_artifact = _write_preflight(
        tmp_path,
        universe,
        frozen_inputs,
        qfq_manifest,
        config_path,
    )
    source_report_path.write_text(
        json.dumps(
            {
                "artifacts": {
                    "signal_trades": str(candidates_path),
                    "universe_manifest": universe_manifest,
                    "qfq_history_manifest": qfq_manifest,
                    "preflight": preflight_artifact,
                },
                "config_source": str(config_path),
                "universe": universe,
                "frozen_inputs": frozen_inputs,
                "preflight_attestation": preflight_attestation,
                "data_adjustment": "qfq",
                "history_bars_requested": 1200,
                "allow_incomplete": False,
                "filters": {
                    "local_data_only": True,
                    "fetch_missing_adjusted": False,
                    "stock_pool": {},
                },
                "signal": {"summary": {"count": 3}},
                "experiment": {"dataset_role": "full"},
                "symbols_requested": 3,
                "symbols_succeeded": 3,
                "symbols_failed": 0,
                "window": {"start": "2023-09-01", "end": "2024-09-30"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "long_history_walk_forward_dataset.HISTORY_DIR", history_dir
    )

    report = build_dataset(
        candidates_path,
        source_report_path,
        output_dir,
        index_path,
        dataset_start=date(2024, 1, 1),
        first_evaluation_start=date(2024, 7, 1),
        last_evaluation_end=date(2024, 9, 30),
        evaluation_months=3,
        min_train_candidates=1,
        min_evaluation_candidates=1,
    )

    fold = report["folds"][0]
    assert fold["train"]["candidates"] == 1
    assert fold["purged_training_labels"]["candidates"] == 1
    assert fold["evaluation"]["candidates"] == 1
    assert fold["label_leakage_count"] == 0
    assert fold["minimums"]["passes"] is True
    assert report["model_fitted"] is False
    assert report["dataset_status"] == "development_viewed_not_holdout"
    assert "_universe_symbols" not in report["input"]["source_report"]["audit"]

    source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
    signal_engine_path = Path(source_report["frozen_inputs"]["signal_engine"]["path"])
    original_signal_engine = signal_engine_path.read_bytes()
    signal_engine_path.write_bytes(b"# tampered signal engine\n")
    with pytest.raises(RuntimeError, match="signal_engine_hash_matches"):
        _source_report_audit(
            source_report,
            candidates_path,
            index_path,
            date(2024, 1, 1),
            date(2024, 9, 30),
        )
    signal_engine_path.write_bytes(original_signal_engine)

    preflight_path = Path(source_report["artifacts"]["preflight"])
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["checks"] = {"synthetic_fixture_ready": True}
    preflight_path.write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    digest = hashlib.sha256(preflight_path.read_bytes()).hexdigest()
    source_report["preflight_attestation"].update(
        {
            "checks": preflight["checks"],
            "artifact_sha256": digest,
            "full_result_sha256": digest,
        }
    )
    with pytest.raises(RuntimeError, match="successful_preflight_attestation"):
        _source_report_audit(
            source_report,
            candidates_path,
            index_path,
            date(2024, 1, 1),
            date(2024, 9, 30),
        )


def test_dataset_refuses_existing_output_directory(tmp_path):
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    with pytest.raises(FileExistsError, match="refusing overwrite"):
        build_dataset(
            tmp_path / "missing.jsonl",
            tmp_path / "missing_report.json",
            output_dir,
            tmp_path / "missing_index.pkl",
        )


def test_dataset_source_gate_requires_successful_preflight_attestation(tmp_path):
    candidates_path = tmp_path / "signals.jsonl"
    candidates_path.write_text("", encoding="utf-8")
    index_path = tmp_path / "index.pkl"
    index_path.write_bytes(b"index")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    pd.DataFrame(
        {
            "datetime": pd.bdate_range("2024-01-01", periods=100),
            "close": [10.0] * 100,
            "is_closed": [True] * 100,
        }
    ).to_pickle(history_dir / "000001_qfq.pkl")
    universe, universe_manifest = _write_universe(tmp_path, ["000001"])
    frozen_inputs, qfq_manifest = _write_frozen_inputs(
        tmp_path, history_dir, index_path, ["000001"]
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("stock_pool: {}\n", encoding="utf-8")
    _, preflight_artifact = _write_preflight(
        tmp_path,
        universe,
        frozen_inputs,
        qfq_manifest,
        config_path,
        start="2024-01-01",
    )
    report = {
        "artifacts": {
            "signal_trades": str(candidates_path),
            "universe_manifest": universe_manifest,
            "qfq_history_manifest": qfq_manifest,
            "preflight": preflight_artifact,
        },
        "config_source": str(config_path),
        "universe": universe,
        "frozen_inputs": frozen_inputs,
        "data_adjustment": "qfq",
        "history_bars_requested": 1200,
        "allow_incomplete": False,
        "filters": {
            "local_data_only": True,
            "fetch_missing_adjusted": False,
            "stock_pool": {},
        },
        "signal": {"summary": {"count": 0}},
        "experiment": {"dataset_role": "full"},
        "symbols_requested": 1,
        "symbols_succeeded": 1,
        "symbols_failed": 0,
        "window": {"start": "2024-01-01", "end": "2024-09-30"},
    }

    with pytest.raises(RuntimeError, match="successful_preflight_attestation"):
        _source_report_audit(
            report,
            candidates_path,
            index_path,
            date(2024, 1, 1),
            date(2024, 9, 30),
        )


def test_dataset_rejects_malformed_source_report_sections(tmp_path):
    candidates_path = tmp_path / "signals.jsonl"
    source_report_path = tmp_path / "source_report.json"
    index_path = tmp_path / "index.pkl"
    _write_jsonl(candidates_path, [])
    source_report_path.write_text(
        json.dumps(
            {
                "artifacts": [],
                "filters": "invalid",
                "window": None,
                "config_snapshot": "invalid",
            }
        ),
        encoding="utf-8",
    )
    index_path.write_bytes(b"index")

    with pytest.raises(RuntimeError, match="fails local/frozen checks"):
        build_dataset(
            candidates_path,
            source_report_path,
            tmp_path / "folds",
            index_path,
            dataset_start=date(2024, 1, 1),
            first_evaluation_start=date(2024, 7, 1),
            last_evaluation_end=date(2024, 9, 30),
        )


def test_dataset_rejects_source_window_that_does_not_cover_folds(
    tmp_path, monkeypatch
):
    candidates_path = tmp_path / "signals.jsonl"
    source_report_path = tmp_path / "source_report.json"
    index_path = tmp_path / "index.pkl"
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    row = _candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")
    _write_jsonl(candidates_path, [row])
    universe, universe_manifest = _write_universe(tmp_path, ["000001"])
    source_report_path.write_text(
        json.dumps(
            {
                "artifacts": {
                    "signal_trades": str(candidates_path),
                    "universe_manifest": universe_manifest,
                },
                "universe": universe,
                "data_adjustment": "qfq",
                "allow_incomplete": False,
                "filters": {
                    "local_data_only": True,
                    "fetch_missing_adjusted": False,
                },
                "signal": {"summary": {"count": 1}},
                "experiment": {"dataset_role": "full"},
                "symbols_requested": 1,
                "symbols_succeeded": 1,
                "symbols_failed": 0,
                "window": {"start": "2024-02-01", "end": "2024-08-31"},
            }
        ),
        encoding="utf-8",
    )
    index_path.write_bytes(b"index")
    (history_dir / "000001_qfq.pkl").write_bytes(b"history")
    monkeypatch.setattr(
        "long_history_walk_forward_dataset.HISTORY_DIR", history_dir
    )

    with pytest.raises(RuntimeError, match="source_window_starts_by_dataset_start"):
        build_dataset(
            candidates_path,
            source_report_path,
            tmp_path / "folds",
            index_path,
            dataset_start=date(2024, 1, 1),
            first_evaluation_start=date(2024, 7, 1),
            last_evaluation_end=date(2024, 9, 30),
            min_train_candidates=1,
            min_evaluation_candidates=1,
        )


def test_dataset_rejects_incomplete_local_stock_pool_audit(tmp_path):
    candidates_path = tmp_path / "signals.jsonl"
    source_report_path = tmp_path / "source_report.json"
    index_path = tmp_path / "index.pkl"
    _write_jsonl(candidates_path, [])
    universe, universe_manifest = _write_universe(tmp_path, ["000001"])
    source_report_path.write_text(
        json.dumps(
            {
                "artifacts": {
                    "signal_trades": str(candidates_path),
                    "universe_manifest": universe_manifest,
                },
                "universe": universe,
                "data_adjustment": "qfq",
                "allow_incomplete": False,
                "filters": {
                    "local_data_only": True,
                    "fetch_missing_adjusted": False,
                    "stock_pool": {
                        "enabled": True,
                        "missing_data_policy": "reject",
                    },
                    "stock_pool_history": {
                        "sources": {"missing_or_invalid": 1},
                        "local_cache_manifest": {
                            "files": 0,
                            "manifest_sha256": "",
                        },
                    },
                },
                "signal": {
                    "summary": {"count": 0},
                    "skipped": {"stock_pool_history_fetch_failed": 1},
                },
                "experiment": {"dataset_role": "full"},
                "symbols_requested": 1,
                "symbols_succeeded": 1,
                "symbols_failed": 0,
                "window": {"start": "2024-01-01", "end": "2024-09-30"},
            }
        ),
        encoding="utf-8",
    )
    index_path.write_bytes(b"index")

    with pytest.raises(RuntimeError, match="stock_pool_history_failures_zero"):
        build_dataset(
            candidates_path,
            source_report_path,
            tmp_path / "folds",
            index_path,
            dataset_start=date(2024, 1, 1),
            first_evaluation_start=date(2024, 7, 1),
            last_evaluation_end=date(2024, 9, 30),
        )


def test_dataset_rejects_non_reject_stock_pool_missing_policy(tmp_path):
    candidates_path = tmp_path / "signals.jsonl"
    source_report_path = tmp_path / "source_report.json"
    index_path = tmp_path / "index.pkl"
    _write_jsonl(candidates_path, [])
    universe, universe_manifest = _write_universe(tmp_path, ["000001"])
    source_report_path.write_text(
        json.dumps(
            {
                "artifacts": {
                    "signal_trades": str(candidates_path),
                    "universe_manifest": universe_manifest,
                    "stock_pool_history_manifest": str(tmp_path / "pool.jsonl"),
                },
                "universe": universe,
                "data_adjustment": "qfq",
                "allow_incomplete": False,
                "filters": {
                    "local_data_only": True,
                    "fetch_missing_adjusted": False,
                    "stock_pool": {
                        "enabled": True,
                        "missing_data_policy": "allow",
                    },
                    "stock_pool_history": {
                        "sources": {},
                        "local_cache_manifest": {
                            "files": 1,
                            "manifest_sha256": "placeholder",
                        },
                    },
                },
                "signal": {"summary": {"count": 0}, "skipped": {}},
                "experiment": {"dataset_role": "full"},
                "symbols_requested": 1,
                "symbols_succeeded": 1,
                "symbols_failed": 0,
                "window": {"start": "2024-01-01", "end": "2024-09-30"},
            }
        ),
        encoding="utf-8",
    )
    index_path.write_bytes(b"index")

    with pytest.raises(RuntimeError, match="stock_pool_missing_data_policy_is_reject"):
        build_dataset(
            candidates_path,
            source_report_path,
            tmp_path / "folds",
            index_path,
            dataset_start=date(2024, 1, 1),
            first_evaluation_start=date(2024, 7, 1),
            last_evaluation_end=date(2024, 9, 30),
        )
