"""Read-only replay and portfolio-order audit for the frozen yearline pullback route.

The audit compares two independently generated yearline experiment directories,
then replays the funded portfolio under predeclared deterministic and random
same-day candidate orders.  Viewed-test is reported but never used by the
research screen.  Holdout paths and existing output directories are blocked.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import yaml

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import _resolve_execution_config, run_portfolio
from utils.helpers import load_config

VERSION = "long_history_yearline_pullback_audit.v1"
INPUT_VERSION = "long_history_yearline_trend_experiment.v1"
PRIMARY_CONFIG_GIT_REVISION = "e2787288051ab1ec6bcdfba40c04fa7e59295863"
CONFIG_REPOSITORY_PATH = "quant-python/signal_system/config/config.yaml"
PRIMARY_CONFIG_SHA256 = "4e0084eb945aa2533dca25f9e0f68b4e27e9f2cbc52c11b7898c082d466000f1"
VERIFY_CONFIG_SHA256 = "9aec5db0f80b307c205898e2c2ed3196f68f73f20f6da00b5298eea844dbf745"
FROZEN_EXPERIMENT_SHA256 = "033818294f9504fba61c6223a671fa99224fa89827990b74d43c26d3f4375b67"
FROZEN_BACKTEST_ENGINE_SHA256 = "562278a0a551e7ed3ef77cb21e49a6e85369cc9dd8df8843d83872d5c3969e2b"
ALLOWED_NON_EXECUTION_CONFIG_DIFFS = {
    "monitor.daily_scan_time": ("04:00", "17:00"),
    "runtime.schedule.daily_scan_time": ("04:00", "17:00"),
}
ROUTE = "yearline_pullback"
PROFILES = ("fixed_sl8", "dynamic_sl5_sl8")
SPLITS = ("train", "val", "viewed_test")
DETERMINISTIC_TIE_BREAKS = ("symbol_asc", "symbol_desc", "hash", "rotate")
RANDOM_SEED_START = 20260914
RANDOM_SEED_COUNT = 200
PORTFOLIO_CONFIG = {
    "initial_cash": 100000.0,
    "max_positions": 4,
    "position_size_pct": 0.25,
    "lot_size": 100,
    "signal_priority": ["yearline_pullback_hold"],
    "score_mode": "P0",
}


def _guard_development_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("holdout" in part.lower() for part in resolved.parts):
        raise ValueError(f"Holdout path is blocked: {resolved}")
    return resolved


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _manifest(values: list[str] | set[str]) -> str:
    payload = "\n".join(sorted(str(value) for value in values)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"expected JSON objects in {path}")
    return rows


def _git_blob(revision: str, repository_path: str) -> bytes:
    return subprocess.check_output(
        ["git", "cat-file", "blob", f"{revision}:{repository_path}"],
        cwd=BASE_DIR.parents[1],
    )


def _mapping_differences(left: Any, right: Any, prefix: str = "") -> dict[str, tuple[Any, Any]]:
    if isinstance(left, dict) and isinstance(right, dict):
        result: dict[str, tuple[Any, Any]] = {}
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(
                _mapping_differences(left.get(key, "<MISSING>"), right.get(key, "<MISSING>"), path)
            )
        return result
    return {} if left == right else {prefix: (left, right)}


def _normalise_semantically_equivalent_config(report: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(report)
    result["inputs"]["config"]["sha256"] = "<SEMANTICALLY_EQUIVALENT_CONFIG>"
    return result


def _config_semantic_audit(
    primary: dict[str, Any], verify: dict[str, Any]
) -> dict[str, Any]:
    primary_bytes = _git_blob(PRIMARY_CONFIG_GIT_REVISION, CONFIG_REPOSITORY_PATH)
    primary_sha = hashlib.sha256(primary_bytes).hexdigest()
    verify_path = Path(str(verify["inputs"]["config"]["path"])).expanduser().resolve()
    verify_bytes = verify_path.read_bytes()
    verify_sha = hashlib.sha256(verify_bytes).hexdigest()
    primary_config = yaml.safe_load(primary_bytes.decode("utf-8"))
    verify_config = yaml.safe_load(verify_bytes.decode("utf-8"))
    differences = _mapping_differences(primary_config, verify_config)
    resolved_primary = _resolve_execution_config(primary_config)
    resolved_verify = _resolve_execution_config(verify_config)
    checks = {
        "primary_config_git_blob_matches_frozen_sha256": primary_sha
        == PRIMARY_CONFIG_SHA256
        == primary["inputs"]["config"]["sha256"],
        "verify_config_file_matches_frozen_sha256": verify_sha
        == VERIFY_CONFIG_SHA256
        == verify["inputs"]["config"]["sha256"],
        "config_differences_exactly_allowlisted": differences
        == ALLOWED_NON_EXECUTION_CONFIG_DIFFS,
        "resolved_backtest_execution_equal": resolved_primary == resolved_verify,
    }
    return {
        "checks": checks,
        "checks_passed": all(checks.values()),
        "primary_git_revision": PRIMARY_CONFIG_GIT_REVISION,
        "primary_sha256": primary_sha,
        "verify_sha256": verify_sha,
        "allowed_non_execution_differences": {
            path: {"primary": values[0], "verify": values[1]}
            for path, values in sorted(differences.items())
        },
        "resolved_execution_sha256": hashlib.sha256(
            _canonical(resolved_primary).encode("utf-8")
        ).hexdigest(),
    }


def _normalise_paths(value: Any, root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _normalise_paths(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise_paths(item, root) for item in value]
    if isinstance(value, str):
        root_text = str(root)
        variants = {
            root_text,
            root_text.replace("\\", "/"),
            root_text.replace("/", "\\"),
        }
        result = value
        for variant in sorted(variants, key=len, reverse=True):
            result = result.replace(variant, "<OUTPUT_ROOT>")
        return result
    return value


def _candidate_index(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        candidate_id = str(row.get("candidate_id", ""))
        if not candidate_id:
            raise RuntimeError(f"missing candidate_id in {label}")
        if candidate_id in indexed:
            raise RuntimeError(f"duplicate candidate_id in {label}: {candidate_id}")
        indexed[candidate_id] = row
    return indexed


def _artifact_name(route: str, profile: str, split: str) -> str:
    return f"{route}_{profile}_{split}.jsonl"


def _validate_report(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    report_path = root / "report.json"
    if not report_path.exists():
        raise FileNotFoundError(report_path)
    report = _load_json(report_path)
    if report.get("version") != INPUT_VERSION:
        raise RuntimeError(f"unexpected input version in {report_path}")
    if report.get("policy", {}).get("holdout_used") is not False:
        raise RuntimeError(f"input report used Holdout: {report_path}")
    route = report.get("routes", {}).get(ROUTE)
    if not isinstance(route, dict):
        raise TypeError(f"missing {ROUTE} route in {report_path}")
    if route.get("screen", {}).get("passes_research_screen") is not True:
        raise RuntimeError(f"frozen pullback efficacy screen did not pass: {report_path}")

    artifact_checks: dict[str, Any] = {}
    for name, metadata in sorted(report.get("artifacts", {}).items()):
        path = Path(str(metadata.get("path", ""))).expanduser().resolve()
        expected = root / name
        if path != expected or not path.exists():
            raise RuntimeError(f"artifact path mismatch or missing: {name}")
        actual_sha = _sha256(path)
        if actual_sha != metadata.get("sha256"):
            raise RuntimeError(f"artifact hash mismatch: {path}")
        with path.open(encoding="utf-8") as handle:
            actual_rows = sum(1 for line in handle if line.strip())
        if actual_rows != int(metadata.get("rows", -1)):
            raise RuntimeError(f"artifact row-count mismatch: {path}")
        artifact_checks[name] = {
            "path": str(path),
            "rows": actual_rows,
            "sha256": actual_sha,
        }
    expected_names = {
        _artifact_name(route_name, profile, split)
        for route_name in ("volume_breakout", "yearline_pullback", "combined")
        for profile in ("fixed_sl8", "fixed_sl5", "dynamic_sl5_sl8")
        for split in SPLITS
    }
    if set(artifact_checks) != expected_names:
        raise RuntimeError("input artifact set differs from the frozen 27-file contract")
    return report, {
        "report": {"path": str(report_path), "sha256": _sha256(report_path)},
        "artifacts": artifact_checks,
    }


def _compare_replays(
    primary_root: Path,
    verify_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    primary, primary_integrity = _validate_report(primary_root)
    verify, verify_integrity = _validate_report(verify_root)
    artifact_results: dict[str, Any] = {}
    for name in sorted(primary_integrity["artifacts"]):
        left = primary_root / name
        right = verify_root / name
        byte_identical = left.read_bytes() == right.read_bytes()
        artifact_results[name] = {
            "artifact_byte_identical": byte_identical,
            "primary_sha256": _sha256(left),
            "verify_sha256": _sha256(right),
        }
    config_semantics = _config_semantic_audit(primary, verify)
    normalised_equal = _normalise_paths(
        _normalise_semantically_equivalent_config(primary), primary_root
    ) == _normalise_paths(
        _normalise_semantically_equivalent_config(verify), verify_root
    )
    primary_non_config_inputs = {
        key: value for key, value in primary.get("inputs", {}).items() if key != "config"
    }
    verify_non_config_inputs = {
        key: value for key, value in verify.get("inputs", {}).items() if key != "config"
    }
    experiment_path = BASE_DIR / "long_history_yearline_trend_experiment.py"
    backtest_engine_path = BASE_DIR / "backtest_winrate.py"
    checks = {
        "non_config_input_hashes_equal": primary_non_config_inputs
        == verify_non_config_inputs,
        "config_semantics_checks_passed": config_semantics["checks_passed"],
        "normalized_report_equal_after_config_semantic_normalization": normalised_equal,
        "all_27_artifacts_byte_identical": all(
            item["artifact_byte_identical"] for item in artifact_results.values()
        ),
        "frozen_experiment_code_hash_matches": _sha256(experiment_path)
        == FROZEN_EXPERIMENT_SHA256,
        "frozen_backtest_engine_hash_matches": _sha256(backtest_engine_path)
        == FROZEN_BACKTEST_ENGINE_SHA256,
        "primary_pullback_screen_passed": primary["routes"][ROUTE]["screen"][
            "passes_research_screen"
        ]
        is True,
        "verify_pullback_screen_passed": verify["routes"][ROUTE]["screen"][
            "passes_research_screen"
        ]
        is True,
    }
    return primary, verify, {
        "checks": checks,
        "checks_passed": all(checks.values()),
        "artifact_count": len(artifact_results),
        "artifact_comparison": artifact_results,
        "config_semantic_audit": config_semantics,
        "primary": primary_integrity,
        "verify": verify_integrity,
    }


def _load_pullback_profiles(root: Path, split: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    rows = {
        profile: _load_jsonl(root / _artifact_name(ROUTE, profile, split))
        for profile in PROFILES
    }
    indexed = {
        profile: _candidate_index(values, f"{profile}/{split}")
        for profile, values in rows.items()
    }
    expected_ids = set(indexed[PROFILES[0]])
    if any(set(indexed[profile]) != expected_ids for profile in PROFILES[1:]):
        raise RuntimeError(f"candidate IDs differ across stop profiles for {split}")
    invariant_fields = (
        "symbol",
        "signal_day",
        "entry_day",
        "entry_price",
        "signal_type",
    )
    for candidate_id in sorted(expected_ids):
        expected = _canonical(
            {field: indexed[PROFILES[0]][candidate_id].get(field) for field in invariant_fields}
        )
        for profile in PROFILES[1:]:
            actual = _canonical(
                {field: indexed[profile][candidate_id].get(field) for field in invariant_fields}
            )
            if actual != expected:
                raise RuntimeError(f"entry fields differ for {profile}/{candidate_id}")
    ordered_ids = sorted(expected_ids)
    aligned = {
        profile: [indexed[profile][candidate_id] for candidate_id in ordered_ids]
        for profile in PROFILES
    }
    return aligned, {
        "candidate_count": len(ordered_ids),
        "candidate_manifest_sha256": _manifest(expected_ids),
        "candidate_ids_identical_across_profiles": True,
        "entry_invariant_fields": list(invariant_fields),
        "entry_fields_identical_across_profiles": True,
        "input_artifacts": {
            profile: {
                "path": str(root / _artifact_name(ROUTE, profile, split)),
                "sha256": _sha256(root / _artifact_name(ROUTE, profile, split)),
            }
            for profile in PROFILES
        },
    }


def _distribution(values: list[float]) -> dict[str, Any]:
    clean = np.asarray(
        [float(value) for value in values if math.isfinite(float(value))], dtype=float
    )
    if clean.size == 0:
        return {"n": 0}
    return {
        "n": int(clean.size),
        "mean": round(float(clean.mean()), 4),
        "std": round(float(clean.std(ddof=0)), 4),
        "min": round(float(clean.min()), 4),
        "p10": round(float(np.quantile(clean, 0.10)), 4),
        "median": round(float(np.median(clean)), 4),
        "p90": round(float(np.quantile(clean, 0.90)), 4),
        "max": round(float(clean.max()), 4),
        "positive_pct": round(float((clean > 0).mean() * 100.0), 2),
        "non_negative_pct": round(float((clean >= 0).mean() * 100.0), 2),
    }


def _portfolio_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    summary = result["summary"]
    attribution = result["attribution"]
    accepted_ids = {
        str(item["candidate_id"]) for item in result.get("accepted_entries", [])
    }
    return {
        "total_return_pct": float(summary["total_return_pct"]),
        "max_drawdown_pct": float(summary["max_drawdown_pct"]),
        "trade_count": int(summary["count"]),
        "accepted_count": int(summary["accepted"]),
        "candidate_acceptance_pct": float(attribution["candidate_acceptance_pct"]),
        "accepted_candidate_ids_sha256": _manifest(accepted_ids),
        "_accepted_ids": accepted_ids,
    }


def _run_once(
    profiles: dict[str, list[dict[str, Any]]],
    costs: dict[str, Any],
    tie_break: str,
    seed: int,
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for profile in PROFILES:
        config = dict(PORTFOLIO_CONFIG)
        config.update({"tie_break": tie_break, "seed": seed})
        result = run_portfolio([dict(row) for row in profiles[profile]], costs, config)
        snapshots[profile] = _portfolio_snapshot(result)
    return snapshots


def _public(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if not key.startswith("_")}


def _effect(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    baseline = snapshots["fixed_sl8"]
    dynamic = snapshots["dynamic_sl5_sl8"]
    union = baseline["_accepted_ids"] | dynamic["_accepted_ids"]
    intersection = baseline["_accepted_ids"] & dynamic["_accepted_ids"]
    return {
        "return_delta_pp": round(
            dynamic["total_return_pct"] - baseline["total_return_pct"], 4
        ),
        "max_drawdown_delta_pp": round(
            dynamic["max_drawdown_pct"] - baseline["max_drawdown_pct"], 4
        ),
        "trade_count_delta": dynamic["trade_count"] - baseline["trade_count"],
        "accepted_id_jaccard_pct": round(
            len(intersection) / len(union) * 100.0, 4
        )
        if union
        else 100.0,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical(row) + "\n")
    return {"path": str(path), "rows": len(rows), "sha256": _sha256(path)}


def _run_order_sensitivity(
    root: Path,
    costs: dict[str, Any],
    output_dir: Path,
    random_seed_count: int,
    random_seed_start: int,
) -> dict[str, Any]:
    split_reports: dict[str, Any] = {}
    pending_artifacts: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for split in SPLITS:
        profiles, integrity = _load_pullback_profiles(root, split)
        deterministic: dict[str, Any] = {}
        for tie_break in DETERMINISTIC_TIE_BREAKS:
            snapshots = _run_once(profiles, costs, tie_break, random_seed_start)
            deterministic[tie_break] = {
                "profiles": {
                    profile: _public(snapshot) for profile, snapshot in snapshots.items()
                },
                "dynamic_vs_fixed_sl8": _effect(snapshots),
            }

        seed_rows: list[dict[str, Any]] = []
        delta_rows: list[dict[str, Any]] = []
        profile_values = {
            profile: {"total_return_pct": [], "max_drawdown_pct": [], "accepted_count": []}
            for profile in PROFILES
        }
        delta_values = {
            "return_delta_pp": [],
            "max_drawdown_delta_pp": [],
            "accepted_id_jaccard_pct": [],
        }
        for offset in range(random_seed_count):
            seed = random_seed_start + offset
            snapshots = _run_once(profiles, costs, "random", seed)
            for profile, snapshot in snapshots.items():
                public = _public(snapshot)
                seed_rows.append({"split": split, "seed": seed, "profile": profile, **public})
                for metric in profile_values[profile]:
                    profile_values[profile][metric].append(float(public[metric]))
            effect = _effect(snapshots)
            delta_rows.append({"split": split, "seed": seed, **effect})
            for metric, values in delta_values.items():
                values.append(float(effect[metric]))

        pending_artifacts[split] = (seed_rows, delta_rows)
        split_reports[split] = {
            "integrity": integrity,
            "deterministic_controls": deterministic,
            "random_seed_sweep": {
                "interpretation": (
                    "Algorithmic same-day candidate-order sensitivity only; random-seed "
                    "quantiles are not confidence intervals or independent market samples."
                ),
                "seed_start": random_seed_start,
                "seed_count": random_seed_count,
                "profile_distributions": {
                    profile: {
                        metric: _distribution(values)
                        for metric, values in metrics.items()
                    }
                    for profile, metrics in profile_values.items()
                },
                "dynamic_vs_fixed_sl8_distributions": {
                    metric: _distribution(values) for metric, values in delta_values.items()
                },
            },
        }

    output_dir.mkdir(parents=True, exist_ok=False)
    for split, (seed_rows, delta_rows) in pending_artifacts.items():
        split_reports[split]["random_seed_sweep"]["artifacts"] = {
            "portfolio_runs": _write_jsonl(
                output_dir / f"random_portfolio_runs_{split}.jsonl", seed_rows
            ),
            "paired_deltas": _write_jsonl(
                output_dir / f"random_paired_deltas_{split}.jsonl", delta_rows
            ),
        }
    return split_reports


def _research_screen(
    replay: dict[str, Any], split_reports: dict[str, Any]
) -> dict[str, Any]:
    train = split_reports["train"]
    val = split_reports["val"]
    train_dynamic = train["random_seed_sweep"]["profile_distributions"][
        "dynamic_sl5_sl8"
    ]["total_return_pct"]
    val_dynamic = val["random_seed_sweep"]["profile_distributions"][
        "dynamic_sl5_sl8"
    ]["total_return_pct"]
    train_delta = train["random_seed_sweep"][
        "dynamic_vs_fixed_sl8_distributions"
    ]["return_delta_pp"]
    val_delta = val["random_seed_sweep"][
        "dynamic_vs_fixed_sl8_distributions"
    ]["return_delta_pp"]
    val_controls = val["deterministic_controls"]
    positive_absolute_controls = sum(
        float(control["profiles"]["dynamic_sl5_sl8"]["total_return_pct"]) > 0
        for control in val_controls.values()
    )
    positive_delta_controls = sum(
        float(control["dynamic_vs_fixed_sl8"]["return_delta_pp"]) > 0
        for control in val_controls.values()
    )
    checks = {
        "independent_replay_checks_passed": replay["checks_passed"] is True,
        "train_random_dynamic_median_return_positive": train_dynamic["median"] > 0,
        "validation_random_dynamic_median_return_positive": val_dynamic["median"] > 0,
        "validation_random_dynamic_p10_return_positive": val_dynamic["p10"] > 0,
        "validation_random_dynamic_positive_seed_pct_at_least_80": val_dynamic[
            "positive_pct"
        ]
        >= 80.0,
        "validation_dynamic_positive_in_at_least_3_of_4_controls": (
            positive_absolute_controls >= 3
        ),
        "train_random_delta_median_non_negative": train_delta["median"] >= 0,
        "validation_random_delta_median_positive": val_delta["median"] > 0,
        "validation_random_delta_p10_non_negative": val_delta["p10"] >= 0,
        "validation_random_delta_positive_seed_pct_at_least_70": val_delta[
            "positive_pct"
        ]
        >= 70.0,
        "validation_delta_positive_in_at_least_3_of_4_controls": (
            positive_delta_controls >= 3
        ),
    }
    return {
        "checks": checks,
        "validation_positive_absolute_controls": positive_absolute_controls,
        "validation_positive_delta_controls": positive_delta_controls,
        "passes_research_screen": all(checks.values()),
        "viewed_test_used_for_screen": False,
        "holdout_used": False,
        "production_eligible": False,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    primary_root = _guard_development_path(Path(args.primary_root))
    verify_root = _guard_development_path(Path(args.verify_root))
    output_dir = _guard_development_path(Path(args.output_dir))
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite output directory: {output_dir}")
    if int(args.random_seeds) < 1:
        raise ValueError("random-seeds must be positive")

    primary, verify, replay = _compare_replays(primary_root, verify_root)
    if not replay["checks_passed"]:
        raise RuntimeError("independent replay checks failed")
    config_path = Path(str(primary["inputs"]["config"]["path"])).expanduser().resolve()
    current_config_sha = _sha256(config_path)
    if current_config_sha != verify["inputs"]["config"]["sha256"]:
        raise RuntimeError("current local config does not match verify input hash")
    costs = _resolve_execution_config(load_config(str(config_path)))
    split_reports = _run_order_sensitivity(
        primary_root,
        costs,
        output_dir,
        int(args.random_seeds),
        int(args.seed_start),
    )
    screen = _research_screen(replay, split_reports)
    report = {
        "version": VERSION,
        "status": "post_hoc_portfolio_order_robustness_audit",
        "research_question": (
            "Is the frozen yearline-pullback route, and the ATR 5%-8% stop relative "
            "to fixed 8%, robust to arbitrary same-day order under four-position capacity?"
        ),
        "inputs": {
            "primary_report": replay["primary"]["report"],
            "verify_report": replay["verify"]["report"],
            "config": {"path": str(config_path), "sha256": _sha256(config_path)},
            "experiment_code": {
                "path": str(BASE_DIR / "long_history_yearline_trend_experiment.py"),
                "sha256": _sha256(BASE_DIR / "long_history_yearline_trend_experiment.py"),
            },
            "audit_code": {
                "path": str(Path(__file__).resolve()),
                "sha256": _sha256(Path(__file__).resolve()),
            },
        },
        "frozen_design": {
            "route": ROUTE,
            "profiles": list(PROFILES),
            "deterministic_tie_breaks": list(DETERMINISTIC_TIE_BREAKS),
            "random_seed_start": int(args.seed_start),
            "random_seed_count": int(args.random_seeds),
            "portfolio_config": PORTFOLIO_CONFIG,
            "parameter_or_threshold_search": False,
            "viewed_test_used_for_screen": False,
        },
        "determinism": replay,
        "splits": split_reports,
        "screen": screen,
        "passes_audit": replay["checks_passed"],
        "policy": {
            "full_experiment_replayed": True,
            "portfolio_order_replayed": True,
            "random_order_is_confidence_interval": False,
            "viewed_test_used_for_screen": False,
            "holdout_used": False,
            "production_config_modified": False,
            "production_eligible": False,
            "database_used": False,
            "sql_executed": False,
            "network_used": False,
            "read_only_inputs": True,
        },
    }
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "version": VERSION,
                "report": str(report_path),
                "report_sha256": _sha256(report_path),
                "passes_audit": report["passes_audit"],
                "passes_research_screen": screen["passes_research_screen"],
                "production_eligible": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-root", required=True)
    parser.add_argument("--verify-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--random-seeds", type=int, default=RANDOM_SEED_COUNT)
    parser.add_argument("--seed-start", type=int, default=RANDOM_SEED_START)
    args = parser.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
