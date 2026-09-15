import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exit_experiment import (  # noqa: E402
    _frozen_candidate_costs,
    _guard_development_path as _guard_exit_path,
    _load_market_weak_by_day,
    _variant_config,
)
from exit_experiment_compare import (  # noqa: E402
    _guard_development_path as _guard_compare_path,
    _paired_split,
    _paired_portfolio_split,
    _screen,
)
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


def _portfolio_row(candidate_id: str, exit_price: float) -> dict:
    return {
        "candidate_id": candidate_id,
        "symbol": candidate_id,
        "signal_day": "2025-01-02",
        "entry_day": "2025-01-03",
        "exit_day": "2025-01-06",
        "entry_price": 10.0,
        "exit_price": exit_price,
        "exit_session": "open",
        "signal_type": "macd_golden_cross_pullback_confirmed_above",
        "holding_days": 3,
        "holding_bars": 1,
        "pnl_pct": (exit_price / 10.0 - 1.0) * 100.0,
        "trade_pnl_pct": (exit_price / 10.0 - 1.0) * 100.0,
        "_mark_prices": {"2025-01-03": 10.0, "2025-01-06": exit_price},
    }


def test_variant_config_keeps_baseline_exit_policy_explicit():
    base = {"backtest": {"chan_zero_axis": {"max_holding_bars": 40}}}
    config = _variant_config(base, "baseline")
    chan = config["backtest"]["chan_zero_axis"]
    assert chan["zero_axis_exit_confirmation_bars"] == 1
    assert chan["timeout_exit_mode"] == "fixed"


def test_exit_tools_block_holdout_paths_by_default(tmp_path):
    path = tmp_path / "reserved_holdout" / "candidates_test.jsonl"
    for guard in (_guard_exit_path, _guard_compare_path):
        with pytest.raises(ValueError, match="Holdout path is blocked"):
            guard(path)
        assert guard(path, True).name == "candidates_test.jsonl"


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
    execution_config = _resolve_execution_config(config)
    resolved = _execution_values(execution_config)
    assert resolved["timeout_exit_mode"] == "ma_break"
    assert resolved["timeout_hard_cap_bars"] == 60
    assert resolved["stop_loss_pct"] == 0.08
    assert resolved["take_profit_pct"] == 0.30
    assert execution_config["profit_protection"] == {"mode": "none"}
    assert resolved["profit_protection_mode"] == "none"


def test_profit_protection_variants_are_explicit_and_isolated():
    base = {
        "backtest": {"chan_zero_axis": {"max_holding_bars": 40}},
        "risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.30},
    }
    lock = _execution_values(
        _resolve_execution_config(_variant_config(base, "mfe_profit_lock"))
    )
    trailing = _execution_values(
        _resolve_execution_config(_variant_config(base, "atr_trailing"))
    )
    assert lock["profit_protection_mode"] == "mfe_lock"
    assert lock["profit_activation_pct"] == 0.08
    assert lock["profit_lock_pct"] == 0.02
    assert trailing["profit_protection_mode"] == "atr_trailing"
    assert trailing["trailing_atr_period"] == 14
    assert trailing["trailing_atr_multiple"] == 2.5


def test_stop_loss_research_variants_are_frozen_and_isolated():
    base = {
        "backtest": {"chan_zero_axis": {"max_holding_bars": 40}},
        "risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.30},
    }
    fixed = _execution_values(_resolve_execution_config(_variant_config(base, "fixed_sl5")))
    regime = _execution_values(
        _resolve_execution_config(_variant_config(base, "regime_sl5_sl8"))
    )
    low_open = _execution_values(
        _resolve_execution_config(_variant_config(base, "weak_market_low_open"))
    )
    assert fixed["stop_loss_pct"] == 0.05
    assert fixed["stop_loss_policy_mode"] == "fixed"
    assert regime["stop_loss_pct"] == 0.08
    assert regime["stop_loss_policy_mode"] == "market_regime"
    assert regime["weak_market_stop_loss_pct"] == 0.05
    assert low_open["stop_loss_pct"] == 0.08
    assert low_open["stop_loss_policy_mode"] == "fixed"
    assert low_open["weak_market_low_open_exit_pct"] == 0.015


def test_market_regime_map_uses_only_previous_index_day(tmp_path):
    path = tmp_path / "index.pkl"
    closes = [100.0] * 19 + [80.0, 120.0, 120.0]
    pd = pytest.importorskip("pandas")
    pd.DataFrame(
        {
            "datetime": pd.bdate_range("2026-01-01", periods=len(closes)),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * len(closes),
            "is_closed": [True] * len(closes),
        }
    ).to_pickle(path)
    mapping, metadata = _load_market_weak_by_day(path)
    days = [item.date().isoformat() for item in pd.bdate_range("2026-01-01", periods=len(closes))]
    assert mapping[days[20]] is True
    assert mapping[days[21]] is False
    assert metadata["all_cutoffs_before_target_day"] is True


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


def test_paired_split_rejects_duplicate_candidate_ids(tmp_path):
    duplicated = [_comparison_row("a", 1.0), _comparison_row("a", 1.0)]
    _write_rows(tmp_path / "baseline" / "candidates_val.jsonl", duplicated)
    _write_rows(
        tmp_path / "mfe_profit_lock" / "candidates_val.jsonl",
        [_comparison_row("a", 1.5)],
    )
    with pytest.raises(RuntimeError, match="duplicate candidate_id"):
        _paired_split(tmp_path, "val", ("mfe_profit_lock",))


def test_paired_portfolio_split_uses_same_candidate_ids_and_writes_artifacts(tmp_path):
    _write_rows(
        tmp_path / "baseline" / "candidates_val.jsonl",
        [_portfolio_row("a", 11.0), _portfolio_row("b", 9.0)],
    )
    _write_rows(
        tmp_path / "mfe_profit_lock" / "candidates_val.jsonl",
        [_portfolio_row("a", 12.0), _portfolio_row("b", 10.0)],
    )
    report = _paired_portfolio_split(
        tmp_path,
        "val",
        ("mfe_profit_lock",),
        {
            "commission_pct": 0.0,
            "minimum_commission": 0.0,
            "stamp_tax_pct": 0.0,
            "slippage_pct": 0.0,
            "lot_size": 100,
        },
    )
    assert report["paired_candidate_count"] == 2
    assert report["profiles"]["baseline"]["candidate_stats"]["count"] == 2
    assert report["baseline_entry_cohort"]["candidate_count"] == 2
    assert report["capital_release_attribution"]["mfe_profit_lock"][
        "decomposition_residual_pp"
    ] == 0.0
    assert "portfolio_attribution" in report["profiles"]["baseline"]
    assert (
        report["profiles"]["mfe_profit_lock"]["portfolio_summary"]["total_return_pct"]
        > report["profiles"]["baseline"]["portfolio_summary"]["total_return_pct"]
    )
    assert Path(
        report["profiles"]["baseline"]["artifacts"]["trades"]["path"]
    ).exists()


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
