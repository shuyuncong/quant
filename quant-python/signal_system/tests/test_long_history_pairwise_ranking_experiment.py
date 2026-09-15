import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import long_history_pairwise_ranking_experiment as experiment


def _candidate(candidate_id: str, day: str, outcome: float = 1.0) -> dict:
    return {
        "candidate_id": candidate_id,
        "symbol": candidate_id.split("-")[0],
        "signal_day": day,
        "entry_day": day,
        "exit_day": day,
        "signal_type": "macd_golden_cross_pullback_confirmed_above",
        "confirmation_count": 2,
        "trade_pnl_pct": outcome,
    }


def _hydrate_rows(rows_by_split: dict[str, list[dict]], **_kwargs):
    hydrated = {}
    symbols = set()
    for split, rows in rows_by_split.items():
        hydrated[split] = []
        for index, row in enumerate(rows):
            copy = dict(row)
            copy["_p5a_features"] = {"dif_dea_gap": 0.01, "zero_dist": 0.02}
            copy["_p5b_features"] = {
                "ma60_dist": 0.1,
                "ma60_slope": 0.01,
                "ma250_dist": 0.2,
                "ma250_slope": 0.005,
                "atr_ratio": 0.01 + index / 1000,
                "recent_return": 0.03,
            }
            hydrated[split].append(copy)
            symbols.add(str(row["symbol"]))
    return hydrated, {
        "history_dir": "synthetic-history",
        "history_symbols": len(symbols),
        "history_manifest_sha256": "synthetic-qfq-manifest",
        "splits": {
            split: {
                "rows": len(rows),
                "hydrated_rows": len(rows),
                "missing_history": 0,
                "signal_day_not_in_history": 0,
                "hydrated_coverage_pct": 100.0,
            }
            for split, rows in rows_by_split.items()
        },
        "feature_time_boundary": "history datetime <= signal_day",
        "macd_parameters": {"fast": 12, "slow": 26, "signal": 9},
        "atr_period": 14,
        "ma_periods": [60, 250],
        "slope_lag_bars": 5,
        "recent_return_bars": 20,
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


def _fold_report(root: Path) -> dict:
    common_train = [
        _candidate("base-low", "2024-12-02", -2.0),
        _candidate("base-high", "2024-12-02", 3.0),
    ]
    folds = [
        {"fold_name": "fold_01", "minimums": {"passes": False}},
        {"fold_name": "fold_02", "minimums": {"passes": False}},
    ]
    for index in range(3, 9):
        fold_name = f"fold_{index:02d}"
        day = f"2025-{index:02d}-03"
        evaluation = [
            _candidate(f"eval{index}-low", day, -1.0),
            _candidate(f"eval{index}-high", day, 2.0),
        ]
        fold_dir = root / fold_name
        folds.append(
            {
                "fold_name": fold_name,
                "minimums": {"passes": True},
                "evaluation_window": {"start": day, "end": day},
                "artifacts": {
                    "train": _artifact(fold_dir / "train.jsonl", common_train),
                    "purged_training_labels": _artifact(
                        fold_dir / "purged_training_labels.jsonl", []
                    ),
                    "evaluation": _artifact(
                        fold_dir / "evaluation.jsonl", evaluation
                    ),
                },
            }
        )
    return {
        "input": {"qfq_history": {"history_dir": str(root / "history")}},
        "folds": folds,
    }


def _fake_portfolio(rows: list[dict], _costs: dict, config: dict) -> dict:
    if config["score_mode"] == "external_causal_score":
        total_return = 3.0
    elif config["tie_break"] == "symbol_asc":
        total_return = 1.0
    elif config["tie_break"] == "hash":
        total_return = 2.0
    else:
        total_return = float(int(config["seed"]) % 5)
    accepted = [dict(row) for row in rows[:1]]
    return {
        "summary": {
            "total_return_pct": total_return,
            "max_drawdown_pct": 4.0,
            "count": len(accepted),
            "win_rate": 50.0,
            "final_equity": 100000.0 + total_return,
        },
        "attribution": {
            "transaction_cost_cash": 1.0,
            "position_capacity_utilization_pct": 25.0,
            "max_positions_rejections": 0,
        },
        "rejection_reasons": {},
        "accepted_entries": accepted,
        "trades": accepted,
        "rejections": [],
        "equity_curve": [],
    }


def _audit_values(source_path: Path, fold_path: Path, fold_value: dict):
    source = {
        "report": str(source_path),
        "report_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "checks_passed": True,
    }
    folds = {
        "report": str(fold_path),
        "report_sha256": hashlib.sha256(fold_path.read_bytes()).hexdigest(),
        "fold_count": 8,
        "eligible_fold_count": 6,
        "dataset_candidates": 14,
        "dataset_candidate_ids_sha256": "dataset-manifest",
        "qfq_manifest_sha256": "synthetic-qfq-manifest",
        "checks_passed": True,
        "_report_value": fold_value,
    }
    return source, folds


def _screen_fold(name: str, accuracy: float, return_delta: float) -> dict:
    return {
        "fold_name": name,
        "training": {
            "model_fitted": True,
            "pairs": {"entry_days_with_pairs": 5},
        },
        "evaluation": {
            "pairwise_metrics": {"day_weighted_pairwise_accuracy": accuracy}
        },
        "portfolio_comparison": {
            "ranked_return_vs_random_median_pp": return_delta
        },
    }


def _screen_stitched(accuracy: float = 0.51, percentile: float = 75.0) -> dict:
    return {
        "evaluation": {
            "pairwise_metrics": {
                "entry_days_with_pairs": 20,
                "undirected_pairs": 100,
                "day_weighted_pairwise_accuracy": accuracy,
            }
        },
        "portfolio_comparison": {
            "pairwise_ranked": {
                "summary": {"total_return_pct": 11.0, "max_drawdown_pct": 10.0}
            },
            "random_order_reference": {
                "metric_distributions": {
                    "total_return_pct": {"median": 10.0},
                    "max_drawdown_pct": {"median": 10.0},
                }
            },
            "ranked_return_random_order_percentile": percentile,
        },
    }


def test_holdout_paths_are_always_blocked(tmp_path):
    with pytest.raises(experiment.PairwiseRankingExperimentError, match="Holdout"):
        experiment._guard_development_path(tmp_path / "reserved_holdout" / "report.json")


def test_output_directory_is_create_once(tmp_path):
    output = tmp_path / "already_exists"
    output.mkdir()
    with pytest.raises(experiment.PairwiseRankingExperimentError, match="refusing overwrite"):
        experiment.build_report(
            tmp_path / "source.json",
            tmp_path / "folds.json",
            output,
            tmp_path / "config.yaml",
        )


def test_fold_loader_rejects_cross_fold_candidate_conflict(tmp_path):
    fold_value = _fold_report(tmp_path)
    train_path = Path(fold_value["folds"][3]["artifacts"]["train"]["path"])
    changed = [
        _candidate("base-low", "2024-12-02", 999.0),
        _candidate("base-high", "2024-12-02", 3.0),
    ]
    fold_value["folds"][3]["artifacts"]["train"] = _artifact(train_path, changed)
    fold_report_path = tmp_path / "report.json"
    fold_report_path.write_text("{}", encoding="utf-8")
    with pytest.raises(experiment.PairwiseRankingExperimentError, match="differs across folds"):
        experiment._load_eligible_fold_rows(fold_report_path, fold_value)


def test_fold_loader_rejects_purged_candidate_in_same_training(tmp_path):
    fold_value = _fold_report(tmp_path)
    fold = fold_value["folds"][2]
    overlap = [_candidate("base-low", "2024-12-02", -2.0)]
    fold["artifacts"]["purged_training_labels"] = _artifact(
        Path(fold["artifacts"]["purged_training_labels"]["path"]), overlap
    )
    fold_report_path = tmp_path / "report.json"
    fold_report_path.write_text("{}", encoding="utf-8")
    with pytest.raises(experiment.PairwiseRankingExperimentError, match="purged candidates"):
        experiment._load_eligible_fold_rows(fold_report_path, fold_value)


def test_no_pair_training_uses_zero_model_and_fails_model_flag():
    rows = []
    for index in range(3):
        row = _candidate(f"single-{index}", f"2025-01-0{index + 2}")
        row["_p5a_features"] = {"dif_dea_gap": 0.01, "zero_dist": 0.02}
        row["_p5b_features"] = {
            "ma60_dist": 0.1,
            "ma60_slope": 0.01,
            "ma250_dist": 0.2,
            "ma250_slope": 0.005,
            "atr_ratio": 0.02,
            "recent_return": 0.03,
        }
        rows.append(row)
    model, audit = experiment._fit_training_rows(rows)
    assert audit["model_fitted"] is False
    assert audit["pairs"]["undirected_pairs"] == 0
    assert model["iterations"] == 0
    assert list(model["weights"]) == [0.0] * len(experiment.MODEL_FEATURE_NAMES)


def test_random_seed_contract_is_exact(monkeypatch):
    monkeypatch.setattr(experiment, "_portfolio", _fake_portfolio)
    runs, reference = experiment._random_seed_runs(
        [_candidate("one", "2025-01-02")], {}
    )
    assert len(runs) == 200
    assert [row["seed"] for row in runs] == list(range(20260830, 20261030))
    assert reference["seed_start"] == 20260830
    assert reference["seed_count"] == 200
    assert reference["seeds"] == [20260830, 20261029]


def test_research_screen_uses_strict_preregistered_boundaries():
    folds = [
        _screen_fold(name, 0.51 if index < 4 else 0.49, 1.0 if index < 4 else -0.5)
        for index, name in enumerate(experiment.EXPECTED_ELIGIBLE_FOLDS)
    ]
    result = experiment._research_screen(folds, _screen_stitched())
    assert result["passes_research_screen"] is True
    stitched_at_random = experiment._research_screen(
        folds, _screen_stitched(accuracy=0.5)
    )
    assert stitched_at_random["passes_research_screen"] is False
    below_percentile = experiment._research_screen(
        folds, _screen_stitched(percentile=74.99)
    )
    assert below_percentile["passes_research_screen"] is False


def prepare_synthetic_environment(tmp_path: Path, monkeypatch):
    fold_value = _fold_report(tmp_path / "fold_inputs")
    source_path = tmp_path / "source.json"
    fold_path = tmp_path / "fold_report.json"
    config_path = tmp_path / "config.yaml"
    source_path.write_text("{}\n", encoding="utf-8")
    fold_path.write_text(json.dumps(fold_value), encoding="utf-8")
    config_path.write_text("{}\n", encoding="utf-8")
    source_audit, fold_audit = _audit_values(source_path, fold_path, fold_value)
    monkeypatch.setattr(experiment, "audit_source_report", lambda _path: source_audit)
    monkeypatch.setattr(
        experiment, "audit_fold_report", lambda _path, _source: fold_audit
    )
    monkeypatch.setattr(experiment, "hydrate_causal_ranking_features", _hydrate_rows)
    monkeypatch.setattr(experiment, "load_config", lambda _path: {})
    monkeypatch.setattr(experiment, "_resolve_execution_config", lambda _value: {})
    monkeypatch.setattr(experiment, "_portfolio", _fake_portfolio)
    return source_path, fold_path, config_path, source_audit, fold_audit


def test_synthetic_build_is_deterministic(tmp_path, monkeypatch):
    source_path, fold_path, config_path, _, _ = prepare_synthetic_environment(
        tmp_path, monkeypatch
    )
    first = tmp_path / "run_one"
    second = tmp_path / "run_two"
    first_report = experiment.build_report(source_path, fold_path, first, config_path)
    second_report = experiment.build_report(source_path, fold_path, second, config_path)
    assert first_report["preregistered_design"] == second_report["preregistered_design"]
    first_artifacts = sorted(
        path.relative_to(first) for path in first.rglob("*.jsonl")
    )
    second_artifacts = sorted(
        path.relative_to(second) for path in second.rglob("*.jsonl")
    )
    assert first_artifacts == second_artifacts
    for relative in first_artifacts:
        assert (first / relative).read_bytes() == (second / relative).read_bytes()
