import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exit_experiment import _frozen_candidate_costs, _variant_config  # noqa: E402
from exit_experiment_compare import _paired_split, _screen  # noqa: E402
from backtest_winrate import _execution_values, _resolve_execution_config  # noqa: E402


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _comparison_row(
    candidate_id: str,
    pnl: float,
    *,
    post20: float | None = 1.0,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "trade_pnl_pct": pnl,
        "mfe_common_60": 10.0,
        "post_exit_20d": post20,
        "holding_bars": 40,
        "exit_reason": "timeout",
        "regime": "bull",
        "signal_type": "macd_near",
    }


def test_variant_config_keeps_baseline_exit_policy_explicit():
    base = {"backtest": {"chan_zero_axis": {"max_holding_bars": 40}}}
    config = _variant_config(base, "baseline")
    chan = config["backtest"]["chan_zero_axis"]
    assert chan["zero_axis_exit_confirmation_bars"] == 1
    assert chan["timeout_exit_mode"] == "fixed"


def test_timeout_variant_is_reproducibly_configured():
    base = {"backtest": {"chan_zero_axis": {"max_holding_bars": 40}}}
    config = _variant_config(base, "timeout_ma_break")
    chan = config["backtest"]["chan_zero_axis"]
    assert chan["timeout_exit_mode"] == "ma_break"
    assert chan["timeout_ma_period"] == 20
    assert chan["timeout_ma_confirm_bars"] == 1
    assert chan["timeout_hard_cap_bars"] == 60
    resolved = _execution_values(_frozen_candidate_costs(config))
    assert resolved["timeout_exit_mode"] == "ma_break"
    assert resolved["timeout_hard_cap_bars"] == 60
    assert resolved["stop_loss_pct"] is None


def test_production_execution_profile_keeps_risk_and_timeout_variant():
    config = _variant_config(
        {
            "backtest": {"chan_zero_axis": {"max_holding_bars": 40}},
            "risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.30},
        },
        "timeout_ma_break",
    )
    resolved = _execution_values(_resolve_execution_config(config))
    assert resolved["timeout_exit_mode"] == "ma_break"
    assert resolved["timeout_hard_cap_bars"] == 60
    assert resolved["stop_loss_pct"] == 0.08
    assert resolved["take_profit_pct"] == 0.30


def test_paired_split_uses_completed_candidate_intersection(tmp_path):
    _write_rows(
        tmp_path / "baseline" / "candidates_val.jsonl",
        [_comparison_row("a", 1.0), _comparison_row("b", 2.0), _comparison_row("c", 3.0)],
    )
    _write_rows(
        tmp_path / "zero_axis_confirm_2" / "candidates_val.jsonl",
        [_comparison_row("a", 1.5), _comparison_row("b", 2.5)],
    )
    _write_rows(
        tmp_path / "timeout_ma_break" / "candidates_val.jsonl",
        [_comparison_row("a", 0.5), _comparison_row("c", 3.5)],
    )

    report = _paired_split(tmp_path, "val")

    assert report["completed_counts"] == {
        "baseline": 3,
        "zero_axis_confirm_2": 2,
        "timeout_ma_break": 2,
    }
    assert report["paired_count"] == 1
    assert report["completed_only_by_variant"] == {
        "baseline": 2,
        "zero_axis_confirm_2": 1,
        "timeout_ma_break": 1,
    }
    assert report["variants"]["zero_axis_confirm_2"]["paired_pnl_delta_pp"]["mean"] == 0.5
    assert report["variants"]["timeout_ma_break"]["paired_pnl_delta_pp"]["mean"] == -0.5


def test_paired_split_reports_metric_specific_post_exit_coverage(tmp_path):
    _write_rows(
        tmp_path / "baseline" / "candidates_test.jsonl",
        [_comparison_row("a", 1.0), _comparison_row("b", 2.0)],
    )
    _write_rows(
        tmp_path / "zero_axis_confirm_2" / "candidates_test.jsonl",
        [_comparison_row("a", 1.5), _comparison_row("b", 2.5, post20=None)],
    )
    _write_rows(
        tmp_path / "timeout_ma_break" / "candidates_test.jsonl",
        [_comparison_row("a", 0.5), _comparison_row("b", 1.5)],
    )

    report = _paired_split(tmp_path, "test")

    confirm = report["variants"]["zero_axis_confirm_2"]
    timeout = report["variants"]["timeout_ma_break"]
    assert confirm["paired_post_exit20_delta_pp"]["n"] == 1
    assert confirm["paired_post_exit20_coverage_pct"] == 50.0
    assert timeout["paired_post_exit20_delta_pp"]["n"] == 2
    assert timeout["paired_post_exit20_coverage_pct"] == 100.0


def test_research_screen_requires_baseline_and_variant_pairing_coverage():
    metric = {"mean": 0.1, "ci95_low": 0.01}
    variant = "zero_axis_confirm_2"
    report = {
        "splits": {
            split: {
                "paired_coverage_pct": {"baseline": 98.0, variant: 100.0},
                "variants": {variant: {"paired_pnl_delta_pp": metric}},
            }
            for split in ("train", "val", "test")
        }
    }

    screen = _screen(report, variant)

    assert screen["checks"]["paired_coverage_at_least_99pct"] is False
    assert screen["passes_research_screen"] is False
