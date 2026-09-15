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

from long_history_walk_forward_audit import (  # noqa: E402
    WalkForwardAuditError,
    audit_source_report,
    run_audit,
)
from long_history_walk_forward_dataset import (  # noqa: E402
    _source_report_audit,
    build_dataset,
)


def _candidate(
    symbol: str,
    signal_day: str,
    entry_day: str,
    exit_day: str,
) -> dict:
    return {
        "symbol": symbol,
        "signal_day": signal_day,
        "entry_day": entry_day,
        "exit_day": exit_day,
        "signal_type": "buy_1",
        "entry_price": 10.0,
        "exit_price": 10.5,
        "exit_reason": "timeout",
        "trade_pnl_pct": 5.0,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_frozen_inputs(
    root: Path,
    symbols: list[str],
    *,
    short_symbols: set[str] | None = None,
    no_window_symbols: set[str] | None = None,
) -> tuple[dict, Path, Path]:
    short_symbols = short_symbols or set()
    no_window_symbols = no_window_symbols or set()
    shared = root.parent / "_frozen_inputs"
    shared.mkdir(exist_ok=True)
    index_path = shared / "index.pkl"
    if not index_path.exists():
        index_path.write_bytes(b"index")
    engine_path = shared / "backtest_engine.py"
    if not engine_path.exists():
        engine_path.write_text("# frozen engine\n", encoding="utf-8")
    signal_engine_path = shared / "signal_engine.py"
    if not signal_engine_path.exists():
        signal_engine_path.write_text("# frozen signal engine\n", encoding="utf-8")
    history_dir = shared / "history"
    history_dir.mkdir(exist_ok=True)
    rows: list[dict] = []
    entries: list[str] = []
    for symbol in symbols:
        path = history_dir / f"{symbol}_qfq.pkl"
        periods = 30 if symbol in short_symbols else 100
        start = "2022-01-03" if symbol in no_window_symbols else "2024-01-01"
        pd.DataFrame(
            {
                "datetime": pd.bdate_range(start, periods=periods),
                "close": [10.0] * periods,
                "is_closed": [True] * periods,
            }
        ).to_pickle(path)
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
                "path": str(engine_path),
                "sha256": hashlib.sha256(engine_path.read_bytes()).hexdigest(),
            },
            "signal_engine": {
                "path": str(signal_engine_path),
                "sha256": hashlib.sha256(signal_engine_path.read_bytes()).hexdigest(),
            },
            "qfq_history": {
                "symbols": len(symbols),
                "missing_symbols": 0,
                "manifest_sha256": hashlib.sha256(
                    "\n".join(entries).encode("utf-8")
                ).hexdigest(),
            },
        },
        manifest,
        history_dir,
    )


def _write_preflight(
    root: Path,
    universe: dict,
    frozen_inputs: dict,
    qfq_manifest: Path,
    config_path: Path,
    *,
    stock_pool: dict | None = None,
    stock_pool_path: Path | None = None,
    short_symbols: set[str] | None = None,
    no_window_symbols: set[str] | None = None,
) -> tuple[dict, Path]:
    stock_pool = stock_pool or {}
    short_symbols = short_symbols or set()
    no_window_symbols = no_window_symbols or set()
    qfq_rows = [
        json.loads(line)
        for line in qfq_manifest.read_text(encoding="utf-8").splitlines()
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
            "start": "2024-01-01",
            "end": "2024-12-31",
            "history_bars": 1200,
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
        "symbols": [],
    }
    for row in qfq_rows:
        symbol = row["symbol"]
        is_short = symbol in short_symbols
        has_no_window = symbol in no_window_symbols
        qfq_bars = len(pd.read_pickle(row["path"]))
        preflight["symbols"].append(
            {
                "symbol": symbol,
                "qfq": {
                    "path": row["path"],
                    "sha256": row["sha256"],
                    "bars": qfq_bars,
                },
                "stock_pool": (
                    {"required": True}
                    if is_short or has_no_window
                    else (
                        {
                            "required": bool(stock_pool.get("enabled")),
                            "path": str(stock_pool_path),
                            "sha256": hashlib.sha256(
                                stock_pool_path.read_bytes()
                            ).hexdigest(),
                        }
                        if stock_pool_path is not None
                        else {}
                    )
                ),
                "blocking_issues": {},
                "nonblocking_issues": (
                    {"insufficient_history": 1}
                    if is_short
                    else ({"no_history_in_window": 1} if has_no_window else {})
                ),
            }
        )
    summary["nonblocking_by_reason"] = {
        key: value
        for key, value in (
            ("insufficient_history", len(short_symbols)),
            ("no_history_in_window", len(no_window_symbols)),
        )
        if value
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
        artifact,
    )


def _source_report(root: Path, rows: list[dict]) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    candidates = root / "baseline_full_signal_trades.jsonl"
    report_path = root / "baseline_full.json"
    _write_jsonl(candidates, rows)
    symbols = sorted({row["symbol"] for row in rows})
    shared = root.parent / "_frozen_inputs"
    shared.mkdir(exist_ok=True)
    universe_source = shared / "universe.pkl"
    if not universe_source.exists():
        pd.DataFrame(
            [{"code": symbol, "name": ""} for symbol in symbols]
        ).to_pickle(universe_source)
    universe_source_hash = hashlib.sha256(universe_source.read_bytes()).hexdigest()
    universe_manifest = root / "universe_manifest.jsonl"
    _write_jsonl(
        universe_manifest,
        [{"symbol": symbol, "name": ""} for symbol in symbols],
    )
    universe_symbol_hash = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()
    frozen_inputs, qfq_manifest, _ = _write_frozen_inputs(root, symbols)
    universe = {
        "source": "frozen_universe_file",
        "strict": True,
        "source_path": str(universe_source),
        "source_sha256": universe_source_hash,
        "expected_sha256": universe_source_hash,
        "source_symbols": len(symbols),
        "selected_symbols": len(symbols),
        "symbol_manifest_sha256": universe_symbol_hash,
    }
    config_path = shared / "config.yaml"
    if not config_path.exists():
        config_path.write_text("stock_pool: {}\n", encoding="utf-8")
    preflight_attestation, preflight_artifact = _write_preflight(
        root,
        universe,
        frozen_inputs,
        qfq_manifest,
        config_path,
        stock_pool={"enabled": False},
    )
    report_path.write_text(
        json.dumps(
            {
                "report_version": 2,
                "mode": "signal",
                "data_adjustment": "qfq",
                "allow_incomplete": False,
                "symbols_requested": len(symbols),
                "symbols_succeeded": len(symbols),
                "symbols_failed": 0,
                "config_source": str(config_path),
                "universe": universe,
                "frozen_inputs": frozen_inputs,
                "preflight_attestation": preflight_attestation,
                "experiment": {"dataset_role": "full"},
                "history_bars_requested": 1200,
                "filters": {
                    "local_data_only": True,
                    "fetch_missing_adjusted": False,
                    "stock_pool": {"enabled": False},
                },
                "signal": {"summary": {"count": len(rows)}, "skipped": {}},
                "window": {"start": "2024-01-01", "end": "2024-12-31"},
                "artifacts": {
                    "signal_trades": str(candidates),
                    "universe_manifest": str(universe_manifest),
                    "qfq_history_manifest": str(qfq_manifest),
                    "preflight": str(preflight_artifact),
                    "report": str(report_path),
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return candidates, report_path


def _build_run(root: Path, rows: list[dict], monkeypatch) -> tuple[Path, Path]:
    candidates, source_report = _source_report(root, rows)
    source_value = json.loads(source_report.read_text(encoding="utf-8"))
    index_path = Path(source_value["frozen_inputs"]["index_data"]["path"])
    qfq_manifest = Path(source_value["artifacts"]["qfq_history_manifest"])
    history_dir = Path(json.loads(qfq_manifest.read_text(encoding="utf-8").splitlines()[0])["path"]).parent
    monkeypatch.setattr(
        "long_history_walk_forward_dataset.HISTORY_DIR", history_dir
    )
    build_dataset(
        candidates,
        source_report,
        root / "folds",
        index_path,
        dataset_start=date(2024, 1, 1),
        first_evaluation_start=date(2024, 7, 1),
        last_evaluation_end=date(2024, 12, 31),
        evaluation_months=3,
        min_train_candidates=1,
        min_evaluation_candidates=1,
    )
    return source_report, root / "folds" / "report.json"


def test_audit_accepts_independent_folds_and_verify_run(tmp_path, monkeypatch):
    rows = [
        _candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10"),
        _candidate("000002", "2024-06-27", "2024-06-28", "2024-07-05"),
        _candidate("000003", "2024-07-01", "2024-07-02", "2024-07-10"),
        _candidate("000004", "2024-10-01", "2024-10-02", "2024-10-15"),
    ]
    source, folds = _build_run(tmp_path / "primary", rows, monkeypatch)
    verify_source, verify_folds = _build_run(tmp_path / "verify", rows, monkeypatch)

    result = run_audit(
        source,
        folds,
        verify_source_report=verify_source,
        verify_fold_report=verify_folds,
    )

    assert result["passes_audit"] is True
    assert result["determinism"]["checks_passed"] is True
    assert result["determinism"]["normalized_source_report_equal"] is True
    assert result["determinism"]["normalized_fold_report_equal"] is True
    assert result["primary"]["folds"]["fold_count"] == 2
    assert result["primary"]["folds"]["fold_counts"][0] == {
        "fold": 1,
        "train": 1,
        "purged": 1,
        "evaluation": 1,
        "minimums_pass": True,
    }


def test_audit_rejects_tampered_fold_artifact(tmp_path, monkeypatch):
    rows = [
        _candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10"),
        _candidate("000002", "2024-07-01", "2024-07-02", "2024-07-10"),
    ]
    source, folds = _build_run(tmp_path / "primary", rows, monkeypatch)
    evaluation = tmp_path / "primary" / "folds" / "fold_01" / "evaluation.jsonl"
    evaluation.write_text("", encoding="utf-8")

    with pytest.raises(WalkForwardAuditError, match="row mismatch|hash mismatch"):
        run_audit(source, folds)


def test_audit_always_blocks_holdout_path(tmp_path):
    with pytest.raises(WalkForwardAuditError, match="Holdout path is blocked"):
        run_audit(
            tmp_path / "research_holdout" / "source.json",
            tmp_path / "folds" / "report.json",
        )


def test_source_audit_rejects_non_reject_stock_pool_policy(tmp_path):
    _, report_path = _source_report(
        tmp_path / "source",
        [_candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["filters"]["stock_pool"] = {
        "enabled": True,
        "missing_data_policy": "allow",
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(WalkForwardAuditError, match="missing-data policy"):
        audit_source_report(report_path)


def test_source_audit_requires_successful_preflight_attestation(tmp_path):
    _, report_path = _source_report(
        tmp_path / "source",
        [_candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("preflight_attestation")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(WalkForwardAuditError, match="missing preflight attestation"):
        audit_source_report(report_path)


def test_source_audit_rejects_signal_engine_hash_mismatch(tmp_path):
    _, report_path = _source_report(
        tmp_path / "source",
        [_candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    signal_engine = Path(report["frozen_inputs"]["signal_engine"]["path"])
    signal_engine.write_text("# tampered signal engine\n", encoding="utf-8")

    with pytest.raises(WalkForwardAuditError, match="signal_engine hash mismatch"):
        audit_source_report(report_path)


def test_source_audit_rejects_forged_successful_preflight_contract(tmp_path):
    _, report_path = _source_report(
        tmp_path / "source",
        [_candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    preflight_path = Path(report["artifacts"]["preflight"])
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["checks"] = {"synthetic_fixture_ready": True}
    preflight_path.write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    digest = hashlib.sha256(preflight_path.read_bytes()).hexdigest()
    report["preflight_attestation"].update(
        {
            "checks": preflight["checks"],
            "artifact_sha256": digest,
            "full_result_sha256": digest,
        }
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(WalkForwardAuditError, match="failed checks"):
        audit_source_report(report_path)


def test_source_audit_rejects_tampered_universe_manifest(tmp_path):
    _, report_path = _source_report(
        tmp_path / "source",
        [_candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = Path(report["artifacts"]["universe_manifest"])
    _write_jsonl(manifest, [{"symbol": "000002", "name": ""}])

    with pytest.raises(WalkForwardAuditError, match="manifest hash mismatch"):
        audit_source_report(report_path)


def test_source_audit_compares_universe_source_symbols_to_manifest(tmp_path):
    _, report_path = _source_report(
        tmp_path / "source",
        [_candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    source = Path(report["universe"]["source_path"])
    pd.DataFrame([{"code": "000002", "name": ""}]).to_pickle(source)
    new_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    report["universe"]["source_sha256"] = new_hash
    report["universe"]["expected_sha256"] = new_hash
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(WalkForwardAuditError, match="source symbols differ"):
        audit_source_report(report_path)


def test_source_audit_rejects_limited_frozen_universe(tmp_path):
    _, report_path = _source_report(
        tmp_path / "source",
        [_candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["universe"]["source_symbols"] = 2
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(WalkForwardAuditError, match="limited or count-mismatched"):
        audit_source_report(report_path)


def test_candidate_symbols_must_belong_to_frozen_universe(tmp_path, monkeypatch):
    candidates, report_path = _source_report(
        tmp_path / "source",
        [_candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")],
    )
    _write_jsonl(
        candidates,
        [_candidate("999999", "2024-01-02", "2024-01-03", "2024-01-10")],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    qfq_manifest = Path(report["artifacts"]["qfq_history_manifest"])
    history_dir = Path(
        json.loads(qfq_manifest.read_text(encoding="utf-8").splitlines()[0])["path"]
    ).parent
    monkeypatch.setattr("long_history_walk_forward_dataset.HISTORY_DIR", history_dir)

    with pytest.raises(RuntimeError, match="outside frozen universe"):
        build_dataset(
            candidates,
            report_path,
            tmp_path / "folds",
            Path(report["frozen_inputs"]["index_data"]["path"]),
            dataset_start=date(2024, 1, 1),
            first_evaluation_start=date(2024, 7, 1),
            last_evaluation_end=date(2024, 12, 31),
            evaluation_months=3,
            min_train_candidates=1,
            min_evaluation_candidates=1,
        )
    with pytest.raises(WalkForwardAuditError, match="outside frozen universe"):
        audit_source_report(report_path)


def test_source_audit_rejects_qfq_symbol_path_swap(tmp_path):
    _, report_path = _source_report(
        tmp_path / "source",
        [
            _candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10"),
            _candidate("000002", "2024-01-03", "2024-01-04", "2024-01-11"),
        ],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = Path(report["artifacts"]["qfq_history_manifest"])
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    first_path = rows[0]["path"]
    second_path = rows[1]["path"]
    rows[0]["path"] = second_path
    rows[0]["file"] = Path(second_path).name
    rows[0]["sha256"] = hashlib.sha256(Path(second_path).read_bytes()).hexdigest()
    rows[1]["path"] = first_path
    rows[1]["file"] = Path(first_path).name
    rows[1]["sha256"] = hashlib.sha256(Path(first_path).read_bytes()).hexdigest()
    _write_jsonl(manifest, rows)
    report["frozen_inputs"]["qfq_history"]["manifest_sha256"] = hashlib.sha256(
        "\n".join(f'{row["symbol"]}|{row["sha256"]}' for row in rows).encode(
            "utf-8"
        )
    ).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(WalkForwardAuditError, match="QFQ symbol/path mismatch"):
        audit_source_report(report_path)


def test_source_audit_verifies_stock_pool_cache_manifest(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    candidate = _candidate("000001", "2024-01-02", "2024-01-03", "2024-01-10")
    candidates = root / "baseline_full_signal_trades.jsonl"
    _write_jsonl(candidates, [candidate])
    cache_path = root / "cache.pkl"
    cache_path.write_bytes(b"cache")
    cache_hash = hashlib.sha256(cache_path.read_bytes()).hexdigest()
    manifest = root / "baseline_full_stock_pool_history_manifest.jsonl"
    _write_jsonl(
        manifest,
        [{"file": cache_path.name, "path": str(cache_path), "sha256": cache_hash}],
    )
    aggregate = hashlib.sha256(
        f"{cache_path.name}|{cache_hash}".encode("utf-8")
    ).hexdigest()
    symbols = ["000001", "000002", "000003"]
    universe_source = root / "universe.pkl"
    pd.DataFrame(
        [{"code": symbol, "name": ""} for symbol in symbols]
    ).to_pickle(universe_source)
    universe_source_hash = hashlib.sha256(universe_source.read_bytes()).hexdigest()
    universe_manifest = root / "universe_manifest.jsonl"
    _write_jsonl(
        universe_manifest,
        [{"symbol": symbol, "name": ""} for symbol in symbols],
    )
    universe_symbol_hash = hashlib.sha256("\n".join(symbols).encode()).hexdigest()
    frozen_inputs, qfq_manifest, _ = _write_frozen_inputs(
        root,
        symbols,
        short_symbols={"000002"},
        no_window_symbols={"000003"},
    )
    universe = {
        "source": "frozen_universe_file",
        "strict": True,
        "source_path": str(universe_source),
        "source_sha256": universe_source_hash,
        "expected_sha256": universe_source_hash,
        "source_symbols": 3,
        "selected_symbols": 3,
        "symbol_manifest_sha256": universe_symbol_hash,
    }
    stock_pool = {"enabled": True, "missing_data_policy": "reject"}
    config_path = root / "config.yaml"
    config_path.write_text("stock_pool: {}\n", encoding="utf-8")
    preflight_attestation, preflight_artifact = _write_preflight(
        root,
        universe,
        frozen_inputs,
        qfq_manifest,
        config_path,
        stock_pool=stock_pool,
        stock_pool_path=cache_path,
        short_symbols={"000002"},
        no_window_symbols={"000003"},
    )
    report_path = root / "baseline_full.json"
    report_path.write_text(
        json.dumps(
            {
                "mode": "signal",
                "data_adjustment": "qfq",
                "allow_incomplete": False,
                "symbols_requested": 3,
                "symbols_succeeded": 3,
                "symbols_failed": 0,
                "config_source": str(config_path),
                "universe": universe,
                "frozen_inputs": frozen_inputs,
                "preflight_attestation": preflight_attestation,
                "experiment": {"dataset_role": "full"},
                "history_bars_requested": 1200,
                "window": {"start": "2024-01-01", "end": "2024-12-31"},
                "filters": {
                    "local_data_only": True,
                    "fetch_missing_adjusted": False,
                    "stock_pool": stock_pool,
                    "stock_pool_history": {
                        "local_cache_manifest": {
                            "files": 1,
                            "manifest_sha256": aggregate,
                        }
                    },
                },
                "signal": {"summary": {"count": 1}, "skipped": {}},
                "artifacts": {
                    "signal_trades": str(candidates),
                    "universe_manifest": str(universe_manifest),
                    "qfq_history_manifest": str(qfq_manifest),
                    "preflight": str(preflight_artifact),
                    "stock_pool_history_manifest": str(manifest),
                },
            }
        ),
        encoding="utf-8",
    )

    report_value = json.loads(report_path.read_text(encoding="utf-8"))
    source_gate = _source_report_audit(
        report_value,
        candidates,
        Path(frozen_inputs["index_data"]["path"]),
        date(2024, 1, 1),
        date(2024, 12, 31),
    )
    assert source_gate["checks"]["successful_preflight_attestation"] is True
    result = audit_source_report(report_path)
    assert result["stock_pool_cache"]["files"] == 1

    original_preflight = preflight_artifact.read_bytes()
    preflight = json.loads(original_preflight)
    short_row = next(
        row for row in preflight["symbols"] if row["symbol"] == "000002"
    )
    short_row["stock_pool"].update(
        {"path": str(cache_path), "sha256": cache_hash}
    )
    preflight_artifact.write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    forged_digest = hashlib.sha256(preflight_artifact.read_bytes()).hexdigest()
    forged_report = json.loads(report_path.read_text(encoding="utf-8"))
    forged_report["preflight_attestation"].update(
        {"artifact_sha256": forged_digest, "full_result_sha256": forged_digest}
    )
    with pytest.raises(RuntimeError, match="successful_preflight_attestation"):
        _source_report_audit(
            forged_report,
            candidates,
            Path(frozen_inputs["index_data"]["path"]),
            date(2024, 1, 1),
            date(2024, 12, 31),
        )
    report_path.write_text(json.dumps(forged_report), encoding="utf-8")
    with pytest.raises(
        WalkForwardAuditError, match="invalid preflight stock-pool exemption"
    ):
        audit_source_report(report_path)

    preflight_artifact.write_bytes(original_preflight)
    report_path.write_text(json.dumps(report_value), encoding="utf-8")

    for forged_issue in ("insufficient_history", "no_history_in_window"):
        forged_preflight = json.loads(original_preflight)
        normal_row = next(
            row
            for row in forged_preflight["symbols"]
            if row["symbol"] == "000001"
        )
        normal_row["nonblocking_issues"] = {forged_issue: 1}
        normal_row["stock_pool"] = {"required": True}
        preflight_artifact.write_text(
            json.dumps(
                forged_preflight,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        forged_digest = hashlib.sha256(preflight_artifact.read_bytes()).hexdigest()
        forged_report = dict(report_value)
        forged_report["preflight_attestation"] = dict(
            report_value["preflight_attestation"]
        )
        forged_report["preflight_attestation"].update(
            {
                "artifact_sha256": forged_digest,
                "full_result_sha256": forged_digest,
            }
        )
        with pytest.raises(RuntimeError, match="successful_preflight_attestation"):
            _source_report_audit(
                forged_report,
                candidates,
                Path(frozen_inputs["index_data"]["path"]),
                date(2024, 1, 1),
                date(2024, 12, 31),
            )
        report_path.write_text(json.dumps(forged_report), encoding="utf-8")
        with pytest.raises(
            WalkForwardAuditError,
            match="invalid preflight stock-pool exemption evidence",
        ):
            audit_source_report(report_path)

    preflight_artifact.write_bytes(original_preflight)
    report_path.write_text(json.dumps(report_value), encoding="utf-8")

    for exempt_symbol in ("000002", "000003"):
        exempt_candidate = _candidate(
            exempt_symbol,
            "2024-02-01",
            "2024-02-02",
            "2024-02-09",
        )
        _write_jsonl(candidates, [candidate, exempt_candidate])
        candidate_report = json.loads(json.dumps(report_value))
        candidate_report["signal"]["summary"]["count"] = 2
        report_path.write_text(json.dumps(candidate_report), encoding="utf-8")
        with pytest.raises(RuntimeError, match="preflight-exempt symbols"):
            build_dataset(
                candidates,
                report_path,
                root / f"folds_{exempt_symbol}",
                Path(frozen_inputs["index_data"]["path"]),
                dataset_start=date(2024, 1, 1),
                first_evaluation_start=date(2024, 7, 1),
                last_evaluation_end=date(2024, 12, 31),
                evaluation_months=3,
                min_train_candidates=1,
                min_evaluation_candidates=1,
            )
        with pytest.raises(
            WalkForwardAuditError, match="preflight-exempt symbols"
        ):
            audit_source_report(report_path)

    for noncanonical_symbol in ("2", "000002.SZ"):
        noncanonical_candidate = _candidate(
            noncanonical_symbol,
            "2024-02-01",
            "2024-02-02",
            "2024-02-09",
        )
        _write_jsonl(candidates, [candidate, noncanonical_candidate])
        candidate_report = json.loads(json.dumps(report_value))
        candidate_report["signal"]["summary"]["count"] = 2
        report_path.write_text(json.dumps(candidate_report), encoding="utf-8")
        with pytest.raises(RuntimeError, match="non-canonical symbols"):
            build_dataset(
                candidates,
                report_path,
                root / f"folds_noncanonical_{noncanonical_symbol.replace('.', '_')}",
                Path(frozen_inputs["index_data"]["path"]),
                dataset_start=date(2024, 1, 1),
                first_evaluation_start=date(2024, 7, 1),
                last_evaluation_end=date(2024, 12, 31),
                evaluation_months=3,
                min_train_candidates=1,
                min_evaluation_candidates=1,
            )
        with pytest.raises(
            WalkForwardAuditError, match="non-canonical symbols"
        ):
            audit_source_report(report_path)

    _write_jsonl(candidates, [candidate])
    report_path.write_text(json.dumps(report_value), encoding="utf-8")

    cache_path.write_bytes(b"tampered")
    with pytest.raises(
        WalkForwardAuditError,
        match="preflight stock-pool hash mismatch|cache hash mismatch",
    ):
        audit_source_report(report_path)
