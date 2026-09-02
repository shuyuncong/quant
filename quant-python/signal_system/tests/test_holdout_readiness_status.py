from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import holdout_readiness_status as status  # noqa: E402


def test_waits_until_ten_holdout_trading_days():
    assert status._decide_state(
        manifest_integrity_ok=True,
        cache_coverage_ok=True,
        index_available=True,
        holdout_trading_days=9,
        candidate_generated=False,
        candidate_integrity_ok=None,
        outcome_sessions_after_collection_end=None,
    ) == "waiting_for_signal_days"


def test_ready_to_probe_after_ten_days_when_unsealed():
    assert status._decide_state(
        manifest_integrity_ok=True,
        cache_coverage_ok=True,
        index_available=True,
        holdout_trading_days=10,
        candidate_generated=False,
        candidate_integrity_ok=None,
        outcome_sessions_after_collection_end=None,
    ) == "ready_to_probe_candidates"


def test_incomplete_data_fails_before_candidate_state():
    assert status._decide_state(
        manifest_integrity_ok=True,
        cache_coverage_ok=False,
        index_available=True,
        holdout_trading_days=20,
        candidate_generated=False,
        candidate_integrity_ok=None,
        outcome_sessions_after_collection_end=None,
    ) == "data_cache_not_ready"


def test_sealed_candidate_integrity_failure_is_explicit():
    assert status._decide_state(
        manifest_integrity_ok=True,
        cache_coverage_ok=True,
        index_available=True,
        holdout_trading_days=20,
        candidate_generated=True,
        candidate_integrity_ok=False,
        outcome_sessions_after_collection_end=50,
    ) == "sealed_integrity_failed"


def test_sealed_candidates_wait_for_forty_outcome_sessions():
    waiting = status._decide_state(
        manifest_integrity_ok=True,
        cache_coverage_ok=True,
        index_available=True,
        holdout_trading_days=20,
        candidate_generated=True,
        candidate_integrity_ok=True,
        outcome_sessions_after_collection_end=39,
    )
    ready = status._decide_state(
        manifest_integrity_ok=True,
        cache_coverage_ok=True,
        index_available=True,
        holdout_trading_days=50,
        candidate_generated=True,
        candidate_integrity_ok=True,
        outcome_sessions_after_collection_end=40,
    )
    assert waiting == "sealed_waiting_outcomes"
    assert ready == "ready_for_final_audit"


def test_index_summary_counts_only_holdout_sessions_and_post_collection_days():
    frame = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03"]
            ),
            "close": [10.0, 10.1, 10.2, 10.3],
        }
    )
    summary = status._index_summary(
        frame,
        holdout_start="2026-09-01",
        collection_end="2026-09-01",
    )
    assert summary["holdout_trading_days"] == 3
    assert summary["outcome_sessions_after_collection_end"] == 2
    assert summary["latest_date"] == "2026-09-03"


def test_status_tool_declares_no_write_operations():
    assert status.WRITE_OPERATIONS_PERFORMED is False

