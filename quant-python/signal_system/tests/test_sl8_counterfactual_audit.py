import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sl8_counterfactual_audit import (  # noqa: E402
    GATE_TOLERANCES,
    MIN_SL_CUT_COUNTS,
    _bootstrap_mean_ci,
    _config_sha256,
    _config_snapshot,
    _gates,
    _rows_manifest_sha256,
)


def _split(
    delta_mean=1.0,
    ci_low=0.2,
    *,
    coverage=100.0,
    p10=0.0,
    min_delta=0.0,
    n=100,
):
    return {
        "paired_coverage_pct": coverage,
        "sl_cut": {
            "n": n,
            "paired_delta_pp": {"mean": delta_mean, "ci95_low": ci_low},
            "tail": {"p10_delta_pp": p10, "min_delta_pp": min_delta},
        },
    }


def test_bootstrap_empty_is_explicitly_empty():
    assert _bootstrap_mean_ci([], 1) == {"n": 0}


def test_bootstrap_singleton_has_degenerate_interval():
    assert _bootstrap_mean_ci([2.5], 1) == {
        "n": 1,
        "mean": 2.5,
        "median": 2.5,
        "p10": 2.5,
        "p90": 2.5,
        "min": 2.5,
        "max": 2.5,
        "positive_pct": 100.0,
        "ci95_low": 2.5,
        "ci95_high": 2.5,
    }


def test_bootstrap_ignores_non_finite_values():
    result = _bootstrap_mean_ci([float("nan"), float("inf"), 2.5], 1)
    assert result["n"] == 1
    assert result["mean"] == 2.5


def test_config_snapshot_exposes_production_risk_inputs():
    snapshot = _config_snapshot(
        {
            "risk": {"stop_loss_pct": 0.08, "stop_profit_pct": 0.30},
            "backtest": {
                "commission_pct": 0.0003,
                "chan_zero_axis": {"max_holding_bars": 40},
            },
        }
    )
    assert snapshot["stop_loss_pct"] == 0.08
    assert snapshot["stop_profit_pct"] == 0.30
    assert snapshot["max_holding_bars"] == 40


def test_manifest_and_config_hashes_are_deterministic_and_order_independent():
    rows = [{"symbol": "000002", "regime": "bear"}, {"symbol": "000001", "regime": "bull"}]
    assert _rows_manifest_sha256(rows) == _rows_manifest_sha256(list(reversed(rows)))
    config = {"risk": {"stop_loss_pct": 0.08}, "signal_strategy": {"default": "enabled"}}
    assert _config_sha256(config) == _config_sha256(dict(reversed(list(config.items()))))


def test_gates_pass_only_when_all_pre_frozen_checks_pass():
    report = {"splits": {split: _split() for split in ("train", "val", "test")}}
    result = _gates(report, GATE_TOLERANCES)
    assert result["all_pass"] is True
    assert all(result["checks"].values())


def test_gates_fail_when_baseline_variant_pairing_coverage_is_low():
    report = {
        "splits": {
            "train": _split(coverage=98.0),
            "val": _split(),
            "test": _split(),
        }
    }
    result = _gates(report, GATE_TOLERANCES)
    assert result["checks"]["paired_coverage_at_least_99pct"] is False
    assert result["all_pass"] is False


def test_gates_fail_when_validation_ci_is_not_positive():
    report = {"splits": {split: _split() for split in ("train", "val", "test")}}
    report["splits"]["val"] = _split(delta_mean=0.1, ci_low=-0.01)
    result = _gates(report, GATE_TOLERANCES)
    assert result["checks"]["val_ci95_low_positive"] is False
    assert result["all_pass"] is False


def test_gates_fail_when_stop_loss_sample_is_below_minimum():
    report = {"splits": {split: _split() for split in ("train", "val", "test")}}
    report["splits"]["test"] = _split(n=MIN_SL_CUT_COUNTS["test"] - 1)
    result = _gates(report, GATE_TOLERANCES)
    assert result["checks"]["minimum_sl_cut_sample_counts"] is False
    assert result["all_pass"] is False


def test_gates_fail_when_validation_tail_expands():
    report = {"splits": {split: _split() for split in ("train", "val", "test")}}
    report["splits"]["val"] = _split(p10=-5.01, min_delta=-10.01)
    result = _gates(report, GATE_TOLERANCES)
    assert result["checks"]["val_tail_not_expanded"] is False
    assert result["all_pass"] is False


def test_gates_fail_when_test_is_materially_worse():
    report = {"splits": {split: _split() for split in ("train", "val", "test")}}
    report["splits"]["test"] = _split(delta_mean=-0.51)
    result = _gates(report, GATE_TOLERANCES)
    assert result["checks"]["test_mean_not_below_tolerance"] is False
    assert result["all_pass"] is False
