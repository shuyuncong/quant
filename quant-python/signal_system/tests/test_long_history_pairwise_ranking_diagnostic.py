import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import long_history_pairwise_ranking_diagnostic as diagnostic

SIGNAL_TYPE = "macd_golden_cross_pullback_confirmed_above"


def _candidate(index: int, day: str, outcome: float) -> dict:
    symbol = f"{index:06d}"
    return {
        "candidate_id": f"{symbol}|{day}|{SIGNAL_TYPE}",
        "symbol": symbol,
        "signal_day": day,
        "entry_day": day,
        "exit_day": day,
        "signal_type": SIGNAL_TYPE,
        "entry_price": 10.0,
        "exit_price": 10.0 + outcome / 10.0,
        "trade_pnl_pct": outcome,
    }


def _score(row: dict, score: float) -> dict:
    return {
        "candidate_id": row["candidate_id"],
        "symbol": row["symbol"],
        "signal_day": row["signal_day"],
        "entry_day": row["entry_day"],
        "signal_type": row["signal_type"],
        "pairwise_model_score": score,
        "portfolio_rank_score": score,
    }


def _artifact(path: Path, rows: list[dict]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "path": str(path),
        "rows": len(rows),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _comparison(return_pct: float = 3.0) -> dict:
    summary = {
        "total_return_pct": return_pct,
        "max_drawdown_pct": 4.0,
        "count": 2,
        "win_rate": 50.0,
        "final_equity": 100003.0,
    }
    return {
        "pairwise_ranked": {
            "summary": summary,
            "attribution": {},
            "rejection_reasons": {},
        },
        "p0_symbol_asc": {
            "summary": {**summary, "total_return_pct": 1.0},
            "attribution": {},
            "rejection_reasons": {},
        },
        "p0_fixed_hash": {
            "summary": {**summary, "total_return_pct": 2.0},
            "attribution": {},
            "rejection_reasons": {},
        },
        "random_order_reference": {
            "seed_start": 20260830,
            "seed_count": 200,
            "seeds": [20260830, 20261029],
            "interpretation": "synthetic",
            "metric_distributions": {
                "total_return_pct": {"n": 200, "median": 1.5},
                "max_drawdown_pct": {"n": 200, "median": 5.0},
            },
        },
        "ranked_return_vs_symbol_asc_pp": return_pct - 1.0,
        "ranked_return_vs_hash_pp": return_pct - 2.0,
        "ranked_return_vs_random_median_pp": return_pct - 1.5,
        "ranked_return_random_order_percentile": 80.0,
        "ranked_drawdown_vs_random_median_pp": -1.0,
    }


def _fake_evaluate_portfolios(evaluation_rows, _scored_rows, _costs):
    return_pct = round(sum(float(row["trade_pnl_pct"]) for row in evaluation_rows) / 100, 2)
    comparison = _comparison(return_pct)
    accepted = [
        {
            "candidate_id": row["candidate_id"],
            "symbol": row["symbol"],
            "signal_day": row["signal_day"],
            "entry_day": row["entry_day"],
            "signal_type": row["signal_type"],
        }
        for row in evaluation_rows[:2]
    ]
    ranked = {
        "summary": comparison["pairwise_ranked"]["summary"],
        "accepted_entries": accepted,
        "trades": [],
        "rejections": [],
        "equity_curve": [],
        "attribution": {},
        "rejection_reasons": {},
    }
    random_runs = [
        {
            "seed": seed,
            "total_return_pct": float(seed % 3),
            "max_drawdown_pct": 5.0,
            "trade_count": 2,
            "win_rate": 50.0,
            "final_equity": 100000.0,
            "transaction_cost_cash": 1.0,
            "position_capacity_utilization_pct": 50.0,
            "max_positions_rejections": 0,
            "accepted_candidate_count": 2,
            "accepted_candidate_ids_sha256": "synthetic",
        }
        for seed in diagnostic.RANDOM_SEEDS
    ]
    return comparison, ranked, random_runs


def _synthetic_v6(root: Path) -> tuple[Path, dict]:
    config = root / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}\n", encoding="utf-8")
    folds = []
    stitched_candidates: list[dict] = []
    stitched_scores: list[dict] = []
    stitched_accepted: list[dict] = []
    stitched_rejections: list[dict] = []
    stitched_trades: list[dict] = []
    for fold_index in range(3, 9):
        fold_name = f"fold_{fold_index:02d}"
        day = f"2025-{fold_index:02d}-03"
        candidates = [
            _candidate(fold_index * 10 + offset, day, outcome)
            for offset, outcome in enumerate((5.0, 3.0, 1.0, -1.0, -3.0))
        ]
        scores = [_score(row, 5.0 - index) for index, row in enumerate(candidates)]
        accepted = [
            {
                "candidate_id": row["candidate_id"],
                "symbol": row["symbol"],
                "signal_day": row["signal_day"],
                "entry_day": row["entry_day"],
                "signal_type": row["signal_type"],
            }
            for row in candidates[:2]
        ]
        rejections = [
            {
                "symbol": row["symbol"],
                "signal_day": row["signal_day"],
                "entry_day": row["entry_day"],
                "signal_types": [row["signal_type"]],
                "reason": "max_positions",
            }
            for row in candidates[2:]
        ]
        trades = [
            {
                **accepted[0],
                "pnl_cash": float(fold_index * 100),
                "pnl_pct": 5.0,
            },
            {**accepted[1], "pnl_cash": -50.0, "pnl_pct": -1.0},
        ]
        fold_dir = root / fold_name
        input_artifacts = {
            "train": _artifact(fold_dir / "train.jsonl", []),
            "purged_training_labels": _artifact(fold_dir / "purged.jsonl", []),
            "evaluation": _artifact(fold_dir / "evaluation.jsonl", candidates),
        }
        artifacts = {
            "candidate_scores": _artifact(fold_dir / "scores.jsonl", scores),
            "accepted_entries": _artifact(fold_dir / "accepted.jsonl", accepted),
            "trades": _artifact(fold_dir / "trades.jsonl", trades),
            "rejections": _artifact(fold_dir / "rejections.jsonl", rejections),
            "equity_curve": _artifact(fold_dir / "equity.jsonl", []),
            "random_seed_runs": _artifact(fold_dir / "random.jsonl", []),
        }
        coefficients = {
            name: (feature_index + 1) * (1.0 if fold_index % 2 else -1.0)
            for feature_index, name in enumerate(diagnostic.MODEL_FEATURE_NAMES)
        }
        folds.append(
            {
                "fold_name": fold_name,
                "input_artifacts": input_artifacts,
                "artifacts": artifacts,
                "training": {"model": {"coefficients": coefficients}},
                "evaluation": {
                    "pairwise_metrics": {"day_weighted_pairwise_accuracy": 0.6}
                },
                "portfolio_comparison": _comparison(),
            }
        )
        stitched_candidates.extend(candidates)
        stitched_scores.extend(scores)
        stitched_accepted.extend(accepted)
        stitched_rejections.extend(rejections)
        stitched_trades.extend(trades)
    stitched_dir = root / "stitched"
    stitched = {
        "artifacts": {
            "candidate_scores": _artifact(
                stitched_dir / "scores.jsonl", stitched_scores
            ),
            "accepted_entries": _artifact(
                stitched_dir / "accepted.jsonl", stitched_accepted
            ),
            "trades": _artifact(stitched_dir / "trades.jsonl", stitched_trades),
            "rejections": _artifact(
                stitched_dir / "rejections.jsonl", stitched_rejections
            ),
            "equity_curve": _artifact(stitched_dir / "equity.jsonl", []),
            "random_seed_runs": _artifact(stitched_dir / "random.jsonl", []),
        },
        "evaluation": {
            "pairwise_metrics": {"day_weighted_pairwise_accuracy": 0.55}
        },
        "portfolio_comparison": _comparison(10.0),
    }
    report = {
        "input": {
            "config": {
                "path": str(config),
                "sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            }
        },
        "portfolio_config": {},
        "folds": folds,
        "stitched_oos": stitched,
    }
    report_path = root / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report_path, report


def fake_v6_audit(report_path: Path) -> dict:
    return {
        "report": str(report_path),
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "eligible_folds": list(diagnostic.EXPECTED_ELIGIBLE_FOLDS),
        "artifact_sha256": {"synthetic": "same"},
        "passes_research_screen": False,
        "model_fitted": True,
        "checks_passed": True,
        "_report_value": {"same": True},
    }


def test_bucket_top4_metrics_use_score_order():
    day = "2025-01-03"
    rows = [_candidate(index, day, outcome) for index, outcome in enumerate((5, 3, 1, -1, -3))]
    for index, row in enumerate(rows):
        row["pairwise_model_score"] = 5 - index
        row["portfolio_rank_score"] = 5 - index
    result = diagnostic._bucket_rows("fold_03", rows)
    assert len(result) == 1
    assert result[0]["top1_is_best"] is True
    assert result[0]["top4_mean_advantage_pp"] == 5.0
    assert result[0]["top4_boundary_pairwise_accuracy"] == 1.0


def test_top4_requires_at_least_five_candidates():
    day = "2025-01-03"
    rows = [_candidate(index, day, float(index)) for index in range(4)]
    for index, row in enumerate(rows):
        row["pairwise_model_score"] = float(index)
        row["portfolio_rank_score"] = float(index)
    assert diagnostic._bucket_rows("fold_03", rows)[0]["top4_eligible"] is False


def test_capacity_ignores_non_max_position_rejections():
    day = "2025-01-03"
    rows = [_candidate(index, day, outcome) for index, outcome in enumerate((5, -2, -5))]
    for index, row in enumerate(rows):
        row["pairwise_model_score"] = 3 - index
        row["portfolio_rank_score"] = 3 - index
    accepted = [{"candidate_id": rows[0]["candidate_id"]}]
    rejections = [
        {
            "symbol": rows[1]["symbol"],
            "signal_day": day,
            "signal_types": [SIGNAL_TYPE],
            "reason": "max_positions",
        },
        {
            "symbol": rows[2]["symbol"],
            "signal_day": day,
            "signal_types": [SIGNAL_TYPE],
            "reason": "insufficient_cash",
        },
    ]
    result = diagnostic._capacity_rows("fold_03", rows, accepted, rejections)
    assert len(result) == 1
    assert result[0]["max_positions_rejected_candidates"] == 1
    assert result[0]["accepted_advantage_pp"] == 7.0


def test_quintile_assignment_does_not_depend_on_outcome():
    day = "2025-01-03"
    rows = [_candidate(index, day, float(index)) for index in range(5)]
    for index, row in enumerate(rows):
        row["pairwise_model_score"] = float(index)
    first = diagnostic._quintile_assignments("fold_03", rows)
    for row in rows:
        row["trade_pnl_pct"] = -999.0
    second = diagnostic._quintile_assignments("fold_03", rows)
    assert [(row["candidate_id"], row["quintile"]) for row in first] == [
        (row["candidate_id"], row["quintile"]) for row in second
    ]


def test_concentration_handles_no_positive_pnl():
    trades = [
        {"entry_day": "2025-01-02", "pnl_cash": -1.0},
        {"entry_day": "2025-01-03", "pnl_cash": 0.0},
    ]
    result = diagnostic._concentration(trades)
    assert result["entry_day"]["top_1_positive_pnl_share_pct"] is None
    assert result["entry_day"]["top_period"] == "2025-01-03"


def prepare_diagnostic_runs(tmp_path: Path, monkeypatch):
    v6_report, _ = _synthetic_v6(tmp_path / "v6")
    monkeypatch.setattr(diagnostic, "audit_v6_report", fake_v6_audit)
    monkeypatch.setattr(diagnostic, "_evaluate_portfolios", _fake_evaluate_portfolios)
    monkeypatch.setattr("utils.helpers.load_config", lambda _path: {})
    first = tmp_path / "diagnostic_primary"
    second = tmp_path / "diagnostic_verify"
    diagnostic.build_report(v6_report, first)
    diagnostic.build_report(v6_report, second)
    return v6_report, first / "report.json", second / "report.json"


def test_two_synthetic_diagnostic_runs_are_byte_deterministic(tmp_path, monkeypatch):
    _, first, second = prepare_diagnostic_runs(tmp_path, monkeypatch)
    first_root = first.parent
    second_root = second.parent
    for name in diagnostic.ARTIFACT_NAMES:
        assert (first_root / f"{name}.jsonl").read_bytes() == (
            second_root / f"{name}.jsonl"
        ).read_bytes()
    report = json.loads(first.read_text(encoding="utf-8"))
    assert report["diagnostic_status"] == "post_hoc_exploratory"
    assert report["interpretation_policy"]["changes_v6_screen"] is False
    assert len(report["leave_one_out"]) == 2
    assert report["holdout_used"] is False
    assert report["production_eligible"] is False


def test_reserved_path_is_blocked(tmp_path):
    with pytest.raises(diagnostic.PairwiseRankingDiagnosticError, match="Holdout"):
        diagnostic._guard_development_path(tmp_path / "holdout" / "report.json")


def test_existing_output_is_blocked(tmp_path):
    output = tmp_path / "exists"
    output.mkdir()
    with pytest.raises(diagnostic.PairwiseRankingDiagnosticError, match="refusing overwrite"):
        diagnostic.build_report(tmp_path / "v6.json", output)
