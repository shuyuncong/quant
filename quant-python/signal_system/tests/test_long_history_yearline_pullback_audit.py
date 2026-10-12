from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from long_history_yearline_pullback_audit import (
    _distribution,
    _effect,
    _guard_development_path,
    _normalise_paths,
    _research_screen,
)


def test_holdout_paths_are_blocked():
    with pytest.raises(ValueError, match="Holdout path is blocked"):
        _guard_development_path(Path("D:/tmp/yearline_holdout_audit"))


def test_report_path_normalisation_replaces_only_the_output_root(tmp_path):
    value = {
        "artifact": str(tmp_path / "one.jsonl"),
        "external": "D:/frozen/input.pkl",
    }
    result = _normalise_paths(value, tmp_path.resolve())
    assert result["artifact"].endswith("<OUTPUT_ROOT>\\one.jsonl") or result[
        "artifact"
    ].endswith("<OUTPUT_ROOT>/one.jsonl")
    assert result["external"] == "D:/frozen/input.pkl"


def test_distribution_reports_order_quantiles():
    result = _distribution([-1.0, 0.0, 1.0, 2.0])
    assert result["n"] == 4
    assert result["median"] == 0.5
    assert result["positive_pct"] == 50.0
    assert result["non_negative_pct"] == 75.0


def test_allowlist_matches_the_real_primary_to_current_config_diff():
    """The allowlist must equal the actual diff, so a config edit cannot slip through.

    The frozen primary lives in a git blob; when it is unavailable (shallow clone,
    exported tree) the comparison is skipped rather than failed.
    """
    import subprocess

    import yaml

    from long_history_yearline_pullback_audit import (
        ALLOWED_NON_EXECUTION_CONFIG_DIFFS,
        CONFIG_REPOSITORY_PATH,
        PRIMARY_CONFIG_GIT_REVISION,
        _mapping_differences,
    )

    blob = subprocess.run(["git", "show", f"{PRIMARY_CONFIG_GIT_REVISION}:{CONFIG_REPOSITORY_PATH}"],
                          cwd=ROOT.parents[1], capture_output=True)
    if blob.returncode != 0:
        pytest.skip("frozen primary config blob is unavailable in this checkout")
    primary = yaml.safe_load(blob.stdout.decode("utf-8"))
    current = yaml.safe_load((ROOT / "config" / Path(CONFIG_REPOSITORY_PATH).name).read_text(encoding="utf-8"))
    differences = _mapping_differences(primary, current)
    # 允许表用 "<PRESENT>" 简写"新增了一个非空配置段"，其余条目必须逐值相等；
    # 两种写法都在这里被机器核对，避免手写条目长期与真实差异漂移。
    assert set(differences) == set(ALLOWED_NON_EXECUTION_CONFIG_DIFFS), (
        "config.yaml 与 allowlist 的差异集合不一致: "
        f"新增未登记={sorted(set(differences) - set(ALLOWED_NON_EXECUTION_CONFIG_DIFFS))} "
        f"已失效={sorted(set(ALLOWED_NON_EXECUTION_CONFIG_DIFFS) - set(differences))}"
    )
    for path, (left, right) in differences.items():
        allowed_left, allowed_right = ALLOWED_NON_EXECUTION_CONFIG_DIFFS[path]
        assert left == allowed_left, f"{path} 左侧不一致: {left!r} != {allowed_left!r}"
        if allowed_right == "<PRESENT>":
            assert right not in ("<MISSING>", None), f"{path} 声明为新增非空段，但实际不是: {right!r}"
        else:
            assert right == allowed_right, f"{path} 右侧不一致: {right!r} != {allowed_right!r}"


def test_effect_compares_dynamic_to_fixed_eight():
    result = _effect(
        {
            "fixed_sl8": {
                "total_return_pct": 2.0,
                "max_drawdown_pct": 5.0,
                "trade_count": 2,
                "_accepted_ids": {"a", "b"},
            },
            "dynamic_sl5_sl8": {
                "total_return_pct": 3.5,
                "max_drawdown_pct": 4.0,
                "trade_count": 3,
                "_accepted_ids": {"b", "c"},
            },
        }
    )
    assert result["return_delta_pp"] == 1.5
    assert result["max_drawdown_delta_pp"] == -1.0
    assert result["trade_count_delta"] == 1
    assert result["accepted_id_jaccard_pct"] == pytest.approx(33.3333)


def _split(
    absolute_median: float,
    absolute_p10: float,
    absolute_positive: float,
    delta_median: float,
    delta_p10: float,
    delta_positive: float,
    controls: list[tuple[float, float]],
) -> dict:
    return {
        "random_seed_sweep": {
            "profile_distributions": {
                "dynamic_sl5_sl8": {
                    "total_return_pct": {
                        "median": absolute_median,
                        "p10": absolute_p10,
                        "positive_pct": absolute_positive,
                    }
                }
            },
            "dynamic_vs_fixed_sl8_distributions": {
                "return_delta_pp": {
                    "median": delta_median,
                    "p10": delta_p10,
                    "positive_pct": delta_positive,
                }
            },
        },
        "deterministic_controls": {
            str(index): {
                "profiles": {"dynamic_sl5_sl8": {"total_return_pct": absolute}},
                "dynamic_vs_fixed_sl8": {"return_delta_pp": delta},
            }
            for index, (absolute, delta) in enumerate(controls)
        },
    }


def test_research_screen_uses_train_and_validation_not_viewed_test():
    passing = _split(1.0, 0.1, 90.0, 0.1, 0.01, 80.0, [(1, 1)] * 4)
    failing_viewed = _split(-99.0, -99.0, 0.0, -99.0, -99.0, 0.0, [(-1, -1)] * 4)
    result = _research_screen(
        {"checks_passed": True},
        {"train": passing, "val": passing, "viewed_test": failing_viewed},
    )
    assert result["passes_research_screen"] is True
    assert result["viewed_test_used_for_screen"] is False
    assert result["holdout_used"] is False


def test_research_screen_fails_a_negative_validation_p10():
    train = _split(1.0, 0.1, 90.0, 0.1, 0.01, 80.0, [(1, 1)] * 4)
    val = _split(1.0, 0.1, 90.0, 0.1, -0.01, 80.0, [(1, 1)] * 4)
    result = _research_screen(
        {"checks_passed": True},
        {"train": train, "val": val, "viewed_test": val},
    )
    assert result["passes_research_screen"] is False
    assert result["checks"]["validation_random_delta_p10_non_negative"] is False
