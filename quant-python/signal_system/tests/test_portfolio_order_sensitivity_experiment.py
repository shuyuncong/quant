import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_order_sensitivity_experiment import (  # noqa: E402
    _distribution,
    _guard_development_path,
    _load_aligned_profiles,
    _order_robustness_screen,
    _run_order_sensitivity_split,
)


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _row(candidate_id: str, symbol: str, exit_price: float, exit_day: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "signal_day": "2025-01-02",
        "entry_day": "2025-01-03",
        "exit_day": exit_day,
        "entry_price": 10.0,
        "exit_price": exit_price,
        "exit_session": "open",
        "signal_type": "macd_golden_cross_pullback_confirmed_above",
        "holding_days": 3,
        "holding_bars": 1,
        "pnl_pct": (exit_price / 10.0 - 1.0) * 100.0,
        "trade_pnl_pct": (exit_price / 10.0 - 1.0) * 100.0,
        "_mark_prices": {
            "2025-01-03": 10.0,
            exit_day: exit_price,
        },
    }


def _costs() -> dict:
    return {
        "commission_pct": 0.0,
        "minimum_commission": 0.0,
        "stamp_tax_pct": 0.0,
        "slippage_pct": 0.0,
        "lot_size": 100,
        "initial_cash": 1000.0,
    }


def test_guard_blocks_holdout_paths_by_default(tmp_path):
    path = tmp_path / "reserved_holdout" / "report.json"
    with pytest.raises(ValueError, match="Holdout path is blocked"):
        _guard_development_path(path)
    assert _guard_development_path(path, True).name == "report.json"


def test_distribution_reports_algorithmic_quantiles():
    result = _distribution([-1.0, 0.0, 1.0, 2.0])
    assert result["n"] == 4
    assert result["median"] == 0.5
    assert result["positive_pct"] == 50.0
    assert result["non_negative_pct"] == 75.0


def test_aligned_profiles_reject_duplicate_candidate_ids(tmp_path):
    duplicate = [_row("a", "000001", 11.0, "2025-01-06")] * 2
    _write_rows(tmp_path / "baseline" / "candidates_val.jsonl", duplicate)
    _write_rows(
        tmp_path / "mfe_profit_lock" / "candidates_val.jsonl",
        [_row("a", "000001", 11.5, "2025-01-06")],
    )
    with pytest.raises(RuntimeError, match="duplicate candidate_id"):
        _load_aligned_profiles(tmp_path, "val", ("mfe_profit_lock",))


def test_aligned_profiles_reject_entry_field_differences(tmp_path):
    _write_rows(
        tmp_path / "baseline" / "candidates_val.jsonl",
        [_row("a", "000001", 11.0, "2025-01-06")],
    )
    changed = _row("a", "000001", 11.5, "2025-01-06")
    changed["entry_price"] = 10.1
    _write_rows(tmp_path / "mfe_profit_lock" / "candidates_val.jsonl", [changed])
    with pytest.raises(RuntimeError, match="entry-time fields differ"):
        _load_aligned_profiles(tmp_path, "val", ("mfe_profit_lock",))


def test_order_sensitivity_uses_paired_seeds_and_writes_artifacts(tmp_path):
    baseline_rows = [
        _row(f"id-{index}", f"00000{index}", 9.0 + index, "2025-01-06")
        for index in range(1, 4)
    ]
    variant_rows = [
        _row(f"id-{index}", f"00000{index}", 10.0 + index, "2025-01-06")
        for index in range(1, 4)
    ]
    _write_rows(tmp_path / "input" / "baseline" / "candidates_val.jsonl", baseline_rows)
    _write_rows(
        tmp_path / "input" / "mfe_profit_lock" / "candidates_val.jsonl",
        variant_rows,
    )
    portfolio_config = {
        "initial_cash": 1000.0,
        "max_positions": 1,
        "position_size_pct": 1.0,
        "signal_priority": ["macd_golden_cross_pullback_confirmed_above"],
        "score_mode": "P0",
    }
    result = _run_order_sensitivity_split(
        tmp_path / "input",
        "val",
        ("mfe_profit_lock",),
        _costs(),
        tmp_path / "output",
        random_seed_count=5,
        random_seed_start=10,
        portfolio_config=portfolio_config,
    )

    assert result["integrity"]["paired_count"] == 3
    sweep = result["random_seed_sweep"]
    assert sweep["seed_count"] == 5
    assert sweep["artifacts"]["seed_runs"]["rows"] == 10
    assert sweep["artifacts"]["paired_seed_deltas"]["rows"] == 5
    delta = sweep["paired_variant_delta_distributions"]["mfe_profit_lock"]
    assert delta["return_delta_pp"]["n"] == 5
    assert delta["accepted_id_jaccard_pct"]["min"] == 100.0


def _screen_split(median: float, p10: float, positive_pct: float, controls: list[float]):
    return {
        "random_seed_sweep": {
            "paired_variant_delta_distributions": {
                "mfe_profit_lock": {
                    "return_delta_pp": {
                        "median": median,
                        "p10": p10,
                        "positive_pct": positive_pct,
                    }
                }
            }
        },
        "deterministic_controls": {
            "paired_variant_effects": {
                "mfe_profit_lock": {
                    str(index): {"return_delta_pp": value}
                    for index, value in enumerate(controls)
                }
            }
        },
    }


def test_robustness_screen_uses_train_and_validation_only():
    reports = {
        "train": _screen_split(0.1, -1.0, 50.0, [1.0, 1.0, 1.0, -1.0]),
        "val": _screen_split(0.2, 0.01, 70.0, [1.0, 1.0, 1.0, -1.0]),
        "test": _screen_split(-99.0, -99.0, 0.0, [-1.0, -1.0, -1.0, -1.0]),
    }
    result = _order_robustness_screen(reports, ("mfe_profit_lock",))[
        "mfe_profit_lock"
    ]
    assert result["passes_order_robustness_screen"] is True
    assert result["changes_preregistered_candidate_efficacy_screen"] is False
    assert result["production_eligible"] is False
