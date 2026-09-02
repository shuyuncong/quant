"""Generate and seal the one-shot 2026-09+ MACD-near holdout candidates.

The script validates the v2 pre-freeze record, requires complete local cache
coverage, runs the production candidate pipeline without fetching, selects the
first signal day satisfying the pre-registered sample gates, and writes only
entry-time fields.  Candidate artifacts are write-once.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import HISTORY_DIR
from holdout_integrity import (
    candidate_ids_sha256,
    canonical_json,
    compute_candidate_seal,
    file_sha256,
    sha256_text,
)
from macd_near_regime_audit import (
    MIN_WEAK_NEAR_COUNT,
    MIN_WEAK_NEAR_UNIQUE_DAYS,
    MIN_WEAK_NEAR_UNIQUE_SYMBOLS,
    NEAR_SIGNAL,
    WEAK_REGIMES,
    _validate_freeze_manifest,
)
from utils.helpers import load_config


DEFAULT_HOLDOUT_DIR = Path(r"D:\tmp\holdout_full_a_v2")
DEFAULT_INDEX_DATA = BASE_DIR / "cache" / "index_000001_sh.pkl"
BACKTEST_SCRIPT = BASE_DIR / "backtest_winrate.py"

OUTCOME_FIELDS = frozenset(
    {
        "entry_day",
        "exit_trigger_day",
        "exit_day",
        "entry_price",
        "exit_price",
        "pnl_pct",
        "trade_pnl_pct",
        "holding_days",
        "holding_bars",
        "exit_reason",
        "exit_session",
        "future_5d",
        "future_20d",
        "future_40d",
        "mfe",
        "mae",
        "post_exit_5d",
        "post_exit_20d",
        "entry_commission_cash",
        "exit_commission_cash",
        "stamp_tax_cash",
        "slippage_cash",
        "reference_quantity",
        "price_limit_deferred_bars",
    }
)

ENTRY_FIELDS = (
    "symbol",
    "signal_day",
    "signal_type",
    "cross_day",
    "confirmation_bars",
    "confirmation_count",
    "confirmation_items",
    "stock_pool_metrics",
    "stock_pool_warnings",
    "fundamental_status",
    "fundamental_metrics",
    "fundamental_warnings",
)


class SampleNotReady(RuntimeError):
    """The frozen sample gates are not yet satisfied; no artifacts are written."""


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _candidate_id(row: dict[str, Any]) -> str:
    return (
        f"{str(row.get('symbol', '')).zfill(6)}|{row.get('signal_day')}|"
        f"{row.get('signal_type', '')}"
    )


def _row_regime(row: dict[str, Any]) -> str:
    explicit = str(row.get("regime", "")).lower()
    if explicit in {"bull", "range", "bear"}:
        return explicit
    context = row.get("market_context") or {}
    return str(context.get("regime", "unknown")).lower()


def _sanitize_candidate(row: dict[str, Any]) -> dict[str, Any]:
    clean = {
        key: row.get(key)
        for key in ENTRY_FIELDS
        if key in row and row.get(key) is not None
    }
    clean["symbol"] = str(row.get("symbol", "")).zfill(6)
    clean["signal_day"] = str(row.get("signal_day", ""))
    clean["signal_type"] = str(row.get("signal_type", ""))
    clean["regime"] = _row_regime(row)
    clean["candidate_id"] = _candidate_id(clean)
    return clean


def _weak_near(row: dict[str, Any]) -> bool:
    return (
        str(row.get("signal_type", "")) == NEAR_SIGNAL
        and _row_regime(row) in WEAK_REGIMES
    )


def _gate_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    weak = [row for row in rows if _weak_near(row)]
    ids = {_candidate_id(row) for row in weak}
    symbols = {str(row.get("symbol", "")).zfill(6) for row in weak}
    days = {str(row.get("signal_day", "")) for row in weak}
    return {
        "weak_near_unique_candidates": len(ids),
        "weak_near_unique_symbols": len(symbols),
        "weak_near_unique_signal_days": len(days),
        "minimum_candidates": MIN_WEAK_NEAR_COUNT,
        "minimum_symbols": MIN_WEAK_NEAR_UNIQUE_SYMBOLS,
        "minimum_signal_days": MIN_WEAK_NEAR_UNIQUE_DAYS,
        "sufficient": (
            len(ids) >= MIN_WEAK_NEAR_COUNT
            and len(symbols) >= MIN_WEAK_NEAR_UNIQUE_SYMBOLS
            and len(days) >= MIN_WEAK_NEAR_UNIQUE_DAYS
        ),
    }


def _select_collection_end(
    candidates: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any]]:
    ordered = sorted(
        candidates,
        key=lambda row: (
            str(row.get("signal_day", "")),
            str(row.get("symbol", "")),
            str(row.get("signal_type", "")),
        ),
    )
    last_gate = _gate_snapshot([])
    for signal_day in sorted({str(row.get("signal_day", "")) for row in ordered}):
        prefix = [
            row for row in ordered if str(row.get("signal_day", "")) <= signal_day
        ]
        last_gate = _gate_snapshot(prefix)
        if last_gate["sufficient"]:
            return signal_day, last_gate
    return None, last_gate


def _assert_generation_target_unused(
    freeze: dict[str, Any],
    candidate_path: Path,
    candidate_seal_path: Path,
) -> None:
    if bool((freeze.get("candidates") or {}).get("generated")):
        raise RuntimeError(
            "holdout candidates already generated; one-shot seal forbids overwrite"
        )
    for path in (candidate_path, candidate_seal_path):
        if path.exists():
            raise RuntimeError(f"holdout artifact already exists: {path}")


def _symbols_from_json(value: Any) -> list[str]:
    if isinstance(value, dict):
        for key in ("symbols", "codes", "universe", "stock_list"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list):
        raise RuntimeError("universe file must be a JSON list or contain symbols")
    return sorted({str(symbol).zfill(6) for symbol in value})


def _verify_optional_universe_file(path: Path | None, frozen: list[str]) -> None:
    if path is None:
        return
    external = _symbols_from_json(_load_json(path))
    if external != sorted(frozen):
        raise RuntimeError("external universe does not match the embedded frozen universe")


def _build_history_coverage(
    symbols: list[str], history_dir: Path = HISTORY_DIR
) -> dict[str, Any]:
    missing_none: list[str] = []
    missing_qfq: list[str] = []
    qfq_hashes: list[str] = []
    for symbol in symbols:
        none_path = history_dir / f"{symbol}_none.pkl"
        qfq_path = history_dir / f"{symbol}_qfq.pkl"
        if not none_path.exists():
            missing_none.append(symbol)
        if not qfq_path.exists():
            missing_qfq.append(symbol)
        else:
            qfq_hashes.append(f"{symbol}|{file_sha256(qfq_path)}")
    coverage = {
        "universe_count": len(symbols),
        "none_history_count": len(symbols) - len(missing_none),
        "qfq_history_count": len(symbols) - len(missing_qfq),
        "missing_none": missing_none,
        "missing_qfq": missing_qfq,
        "qfq_history_manifest_sha256": sha256_text("\n".join(qfq_hashes)),
    }
    if missing_none or missing_qfq:
        raise RuntimeError(
            "local history coverage incomplete: "
            f"missing_none={len(missing_none)}, missing_qfq={len(missing_qfq)}"
        )
    return coverage


def _manifest_preflight(
    freeze: dict[str, Any],
    manifest_path: Path,
    freeze_seal_path: Path,
    config: dict[str, Any],
    config_path: Path,
) -> dict[str, Any]:
    checks = _validate_freeze_manifest(
        freeze,
        [],
        config=config,
        config_path=config_path,
        freeze_seal_path=freeze_seal_path,
    )
    required = (
        "seal_match",
        "external_freeze_seal_match",
        "rules_hash_match",
        "rules_match",
        "universe_count_match",
        "universe_manifest_hash_match",
        "code_hashes_match",
        "runtime_versions_match",
        "config_file_hash_match",
        "config_snapshot_hash_match",
    )
    failed = [name for name in required if checks.get(name) is not True]
    if failed:
        raise RuntimeError(
            f"freeze manifest preflight failed for {manifest_path}: {failed}"
        )
    return checks


def _run_production_candidate_pipeline(
    *,
    config_path: Path,
    symbols: list[str],
    start: str,
    data_cutoff: str,
    index_data: Path,
    workers: int,
    history_bars: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not index_data.exists():
        raise FileNotFoundError(f"local market index history not found: {index_data}")
    with tempfile.TemporaryDirectory(prefix="holdout_v2_") as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        symbols_file = temp_dir / "symbols.json"
        symbols_file.write_text(
            json.dumps(symbols, ensure_ascii=False), encoding="utf-8"
        )
        report_path = temp_dir / "backtest_report.json"
        command = [
            sys.executable,
            str(BACKTEST_SCRIPT),
            "--config",
            str(config_path),
            "--experiment-id",
            "macd-near-holdout-v2-candidate-freeze",
            "--dataset-role",
            "test",
            "--start",
            start,
            "--end",
            data_cutoff,
            "--workers",
            str(max(workers, 1)),
            "--out",
            str(report_path),
            "--mode",
            "signal",
            "--adjust",
            "qfq",
            "--history-bars",
            str(max(history_bars, 300)),
            "--no-fetch-missing-adjusted",
            "--local-data-only",
            "--index-data",
            str(index_data),
            "--symbols-file",
            str(symbols_file),
            "--allow-incomplete",
        ]
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "production candidate pipeline failed: "
                f"returncode={completed.returncode}; stderr={completed.stderr[-2000:]}"
            )
        if not report_path.exists():
            raise RuntimeError("production candidate pipeline did not write its report")
        report = _load_json(report_path)
        trades_path = report_path.with_name(
            report_path.stem + "_signal_trades.jsonl"
        )
        if not trades_path.exists():
            raise RuntimeError("production candidate pipeline did not write signal trades")
        if int(report.get("symbols_requested", -1)) != len(symbols):
            raise RuntimeError("production candidate pipeline did not request the full universe")
        if int(report.get("symbols_failed", -1)) != 0:
            raise RuntimeError(
                "production candidate pipeline has symbol failures: "
                f"{report.get('symbols_failed')}"
            )
        if str(report.get("data_adjustment")) != "qfq":
            raise RuntimeError("production candidate pipeline did not use qfq history")
        if not bool((report.get("filters") or {}).get("local_data_only")):
            raise RuntimeError("production candidate pipeline did not enforce local-data-only mode")
        if not bool((report.get("filters") or {}).get("market_gate_enabled")):
            raise RuntimeError(
                "market gate must be enabled to freeze bull/range/bear regimes"
            )
        raw = _load_jsonl(trades_path)
        provenance = {
            "source_report_sha256": file_sha256(report_path),
            "source_signal_trades_sha256": file_sha256(trades_path),
            "symbols_requested": report.get("symbols_requested"),
            "symbols_succeeded": report.get("symbols_succeeded"),
            "data_adjustment": report.get("data_adjustment"),
            "market_gate_history": (report.get("filters") or {}).get(
                "market_gate_history"
            ),
        }
        return raw, provenance


def _write_jsonl_once(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        raise RuntimeError(f"refusing to overwrite existing artifact: {path}")
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def generate(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if output_dir != manifest_path.parent:
        raise RuntimeError("output_dir must equal the freeze manifest directory")
    freeze_seal_path = output_dir / "holdout_freeze.seal"
    candidate_path = output_dir / "candidates_holdout.jsonl"
    candidate_seal_path = output_dir / "holdout_candidates.seal"
    config_path = Path(args.config).expanduser().resolve()
    index_data = Path(args.index_data).expanduser().resolve()
    external_universe = (
        Path(args.universe_file).expanduser().resolve()
        if args.universe_file
        else None
    )

    freeze = _load_json(manifest_path)
    if str(freeze.get("version")) != "holdout_freeze.v2":
        raise RuntimeError("candidate generation requires holdout_freeze.v2")
    _assert_generation_target_unused(freeze, candidate_path, candidate_seal_path)
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {config_path}")
    _manifest_preflight(
        freeze, manifest_path, freeze_seal_path, config, config_path
    )

    symbols = sorted(
        {str(symbol).zfill(6) for symbol in freeze["universe"]["symbols"]}
    )
    if not symbols:
        raise RuntimeError("frozen universe is empty")
    _verify_optional_universe_file(external_universe, symbols)
    coverage = _build_history_coverage(symbols, HISTORY_DIR)

    holdout_start = str(freeze["holdout_window"]["start"])
    data_cutoff = date.fromisoformat(str(args.data_cutoff)).isoformat()
    if data_cutoff < holdout_start:
        raise RuntimeError("data cutoff is before the frozen holdout start")
    raw_candidates, provenance = _run_production_candidate_pipeline(
        config_path=config_path,
        symbols=symbols,
        start=holdout_start,
        data_cutoff=data_cutoff,
        index_data=index_data,
        workers=int(args.workers),
        history_bars=int(args.history_bars),
    )
    sanitized = [_sanitize_candidate(row) for row in raw_candidates]
    if any(
        row["regime"] not in {"bull", "range", "bear"} for row in sanitized
    ):
        raise RuntimeError("one or more candidates have no frozen market regime")
    ids = [row["candidate_id"] for row in sanitized]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate candidate ids in production candidate pipeline")

    collection_end, gate = _select_collection_end(sanitized)
    if collection_end is None:
        raise SampleNotReady(
            "pre-registered weak-near sample gates are not yet satisfied: "
            f"{gate}"
        )
    frozen_candidates = sorted(
        [row for row in sanitized if row["signal_day"] <= collection_end],
        key=lambda row: (row["signal_day"], row["symbol"], row["signal_type"]),
    )
    if any(OUTCOME_FIELDS.intersection(row) for row in frozen_candidates):
        raise RuntimeError("outcome fields leaked into the frozen candidate payload")

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl_once(candidate_path, frozen_candidates)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    generation_summary = {
        "pipeline": "backtest_winrate.py production candidate pipeline",
        "raw_candidate_count": len(raw_candidates),
        "frozen_candidate_count": len(frozen_candidates),
        "sample_gate": gate,
        "provenance": provenance,
    }
    coverage_hash = sha256_text(canonical_json(coverage))
    summary_hash = sha256_text(canonical_json(generation_summary))
    candidate_record: dict[str, Any] = {
        "generated": True,
        "candidates_file": str(candidate_path),
        "candidates_manifest_sha256": file_sha256(candidate_path),
        "candidate_ids_sha256": candidate_ids_sha256(
            row["candidate_id"] for row in frozen_candidates
        ),
        "generated_at_utc": generated_at,
        "collection_end": collection_end,
        "data_cutoff": data_cutoff,
        "universe_coverage": coverage,
        "universe_coverage_sha256": coverage_hash,
        "generation_summary": generation_summary,
        "generation_summary_sha256": summary_hash,
    }
    candidate_record["candidate_seal"] = compute_candidate_seal(
        str(freeze["seal"]), candidate_record
    )
    with candidate_seal_path.open("x", encoding="utf-8") as handle:
        handle.write(candidate_record["candidate_seal"])
    freeze["candidates"] = candidate_record
    manifest_path.write_text(
        json.dumps(freeze, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return {
        "status": "candidates_sealed",
        "manifest_path": str(manifest_path),
        "candidate_path": str(candidate_path),
        "candidate_seal_path": str(candidate_seal_path),
        "collection_end": collection_end,
        "candidate_count": len(frozen_candidates),
        "sample_gate": gate,
        "freeze_seal": freeze["seal"],
        "candidate_seal": candidate_record["candidate_seal"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_HOLDOUT_DIR / "holdout_freeze.json"),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_HOLDOUT_DIR)
    parser.add_argument(
        "--config", default=str(BASE_DIR / "config" / "config.yaml")
    )
    parser.add_argument("--universe-file", default=None)
    parser.add_argument(
        "--index-data",
        default=str(DEFAULT_INDEX_DATA),
        help="local historical index file; network fetching is not allowed",
    )
    parser.add_argument(
        "--data-cutoff",
        required=True,
        help="last completed local-data date included in candidate collection",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--history-bars", type=int, default=800)
    args = parser.parse_args()
    try:
        result = generate(args)
    except SampleNotReady as exc:
        print(
            json.dumps(
                {"status": "sample_not_ready", "detail": str(exc)},
                ensure_ascii=False,
                indent=1,
            )
        )
        return 2
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "blocked_fail_closed", "detail": str(exc)},
                ensure_ascii=False,
                indent=1,
            )
        )
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
