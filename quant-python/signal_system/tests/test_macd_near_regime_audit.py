import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import macd_near_regime_audit as audit  # noqa: E402


def _row(signal_type, regime, pnl, *, candidate_id="000001|2025-01-02|x"):
    return {
        "candidate_id": candidate_id,
        "signal_type": signal_type,
        "regime": regime,
        "trade_pnl_pct": pnl,
        "future_5d": 1.0,
        "future_20d": 2.0,
        "future_40d": 3.0,
        "mfe": 5.0,
        "mae": -2.0,
        "post_exit_5d": 0.5,
        "post_exit_20d": 1.0,
        "near_regime_variant_included": not (
            signal_type == audit.NEAR_SIGNAL and regime in audit.WEAK_REGIMES
        ),
    }


def test_disabled_rule_only_matches_near_in_weak_regimes():
    assert audit._is_disabled({"signal_type": audit.NEAR_SIGNAL, "regime": "bear"})
    assert audit._is_disabled({"signal_type": audit.NEAR_SIGNAL, "regime": "range"})
    assert not audit._is_disabled({"signal_type": audit.NEAR_SIGNAL, "regime": "bull"})
    assert not audit._is_disabled({"signal_type": "buy_1", "regime": "bear"})


def test_candidate_report_marks_small_weak_near_sample_exploratory():
    rows = [
        _row(audit.NEAR_SIGNAL, "range", -5.0, candidate_id="1"),
        _row("buy_1", "bull", 2.0, candidate_id="2"),
    ]
    retained = [row for row in rows if row["near_regime_variant_included"]]
    report = audit._candidate_report(rows, retained, {}, {}, seed=7)
    assert report["disabled_weak_near"]["n"] == 1
    assert report["sample_gate"]["sufficient"] is False
    assert report["retained_pair_invariant"]["exit_semantics_changed"] is False
    assert report["policy_effect"]["disabled_pnl_sum_pp"] == -5.0


def test_manifest_hash_is_order_independent():
    rows = [{"symbol": "000002", "regime": "bear"}, {"symbol": "000001", "regime": "bull"}]
    assert audit._rows_manifest_sha256(rows) == audit._rows_manifest_sha256(list(reversed(rows)))


def test_replay_split_reports_missing_history_without_database(monkeypatch, tmp_path):
    source = tmp_path / "candidates_train.jsonl"
    source.write_text(
        json.dumps({"symbol": "999999", "signal_day": "2025-01-02", "signal_type": "buy_1"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "HISTORY_DIR", tmp_path / "history")
    rows, skipped, match = audit._replay_split(source, {"risk": {}, "backtest": {}})
    assert rows == []
    assert skipped["missing_history"] == 1
    assert match["replayed_rows"] == 0
    assert match["baseline_replay_complete"] is False


def test_portfolio_config_keeps_production_risk_and_p0_ordering():
    costs, portfolio = audit._portfolio_config(
        {
            "risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.30},
            "position": {"max_stocks": 4, "base_position_per_stock": 0.25},
            "backtest": {"initial_cash": 100000, "lot_size": 100},
        },
        seed=20260830,
    )
    assert costs["stop_loss_pct"] == 0.08
    assert costs["take_profit_pct"] == 0.30
    assert portfolio["score_mode"] == "P0"
    assert portfolio["tie_break"] == "symbol_asc"
    assert portfolio["seed"] == 20260830


def test_confirmatory_sample_gate_uses_holdout_not_viewed_history():
    reports = {
        "val": {"candidate_report": {"sample_gate": {"sufficient": False}}},
        "holdout": {"candidate_report": {"sample_gate": {"sufficient": True}}},
    }
    assert audit._confirmatory_sample_ok(reports, ("val", "holdout")) is True


def test_without_holdout_all_requested_history_splits_remain_diagnostic_gate():
    reports = {
        "train": {"candidate_report": {"sample_gate": {"sufficient": True}}},
        "val": {"candidate_report": {"sample_gate": {"sufficient": False}}},
    }
    assert audit._confirmatory_sample_ok(reports, ("train", "val")) is False
