"""Versioned configuration contract for the four-layer strategy framework.

The framework metadata is intentionally descriptive in the first increment. It
does not replace the existing strategy sections; it gives live scans and
backtests a stable, serializable view of which selection and execution layers
are intended to be active. Missing metadata keeps the legacy P0-compatible
defaults.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping


FRAMEWORK_VERSION = "four-layer-v1"
_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ALLOWED_FRAMEWORK_KEYS = {
    "version",
    "profile",
    "experiment_id",
    "dataset_role",
    "selection_layers",
    "execution_layers",
}
_ALLOWED_DATASET_ROLES = {"baseline", "train", "validation", "test", "full"}
_LAYER_GROUPS = {
    "selection_layers": {"fundamental", "volume", "technical"},
    "execution_layers": {"regime", "position", "risk", "t_trading"},
}

_DEFAULT_FRAMEWORK: dict[str, Any] = {
    "version": FRAMEWORK_VERSION,
    "profile": "P0",
    "experiment_id": "p0-baseline",
    "dataset_role": "baseline",
    "selection_layers": {
        "fundamental": False,
        "volume": True,
        "technical": True,
    },
    "execution_layers": {
        "regime": True,
        "position": True,
        "risk": True,
        "t_trading": True,
    },
}


def _read_bool(value: Any, path: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{path} must be a boolean")


def _read_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    return value


def resolve_strategy_framework(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a validated framework snapshot without changing runtime behavior.

    ``strategy.framework`` is optional so older configs remain valid. The
    effective fundamental state is derived from the existing historical/live
    switches when no explicit framework metadata is supplied.
    """

    root = _read_mapping(config, "config")
    strategy = _read_mapping(root.get("strategy"), "strategy")
    raw = _read_mapping(strategy.get("framework"), "strategy.framework")
    result = deepcopy(_DEFAULT_FRAMEWORK)

    unknown = set(raw) - _ALLOWED_FRAMEWORK_KEYS
    if unknown:
        names = ", ".join(sorted(str(item) for item in unknown))
        raise ValueError(f"strategy.framework contains unknown keys: {names}")

    version = str(raw.get("version", result["version"])).strip()
    profile = str(raw.get("profile", result["profile"])).strip()
    if version != FRAMEWORK_VERSION:
        raise ValueError(
            f"strategy.framework.version must be {FRAMEWORK_VERSION!r}, got {version!r}"
        )
    if not profile or not _PROFILE_PATTERN.fullmatch(profile):
        raise ValueError(
            "strategy.framework.profile must match [A-Za-z0-9][A-Za-z0-9_.-]*"
        )
    result["version"] = version
    result["profile"] = profile
    result["experiment_id"] = str(raw.get("experiment_id", result["experiment_id"])).strip()
    if not result["experiment_id"] or not _PROFILE_PATTERN.fullmatch(result["experiment_id"]):
        raise ValueError(
            "strategy.framework.experiment_id must match [A-Za-z0-9][A-Za-z0-9_.-]*"
        )
    dataset_role = str(raw.get("dataset_role", result["dataset_role"])).strip().lower()
    if dataset_role not in _ALLOWED_DATASET_ROLES:
        choices = ", ".join(sorted(_ALLOWED_DATASET_ROLES))
        raise ValueError(f"strategy.framework.dataset_role must be one of: {choices}")
    result["dataset_role"] = dataset_role

    for group in ("selection_layers", "execution_layers"):
        values = _read_mapping(raw.get(group), f"strategy.framework.{group}")
        unknown_layers = set(values) - _LAYER_GROUPS[group]
        if unknown_layers:
            names = ", ".join(sorted(str(item) for item in unknown_layers))
            raise ValueError(f"strategy.framework.{group} contains unknown keys: {names}")
        for name, default in result[group].items():
            if name in values:
                result[group][name] = _read_bool(
                    values[name], f"strategy.framework.{group}.{name}"
                )

    # Keep the snapshot honest when a runtime switch is present. The metadata
    # block is the declared experiment intent, while the existing backtest /
    # entry-filter switch is the effective behavior for this run.
    backtest = _read_mapping(root.get("backtest"), "backtest")
    historical = _read_mapping(backtest.get("fundamental"), "backtest.fundamental")
    entry_filters = _read_mapping(root.get("entry_filters"), "entry_filters")
    runtime_fundamental: bool | None = None
    if "enabled" in historical:
        runtime_fundamental = _read_bool(
            historical["enabled"], "backtest.fundamental.enabled"
        )
    elif "fundamental_enabled" in entry_filters:
        runtime_fundamental = _read_bool(
            entry_filters["fundamental_enabled"], "entry_filters.fundamental_enabled"
        )
    else:
        # Historical fundamental filtering is opt-in. A declared framework
        # flag alone must not silently enable the backtest filter.
        runtime_fundamental = False

    declared = deepcopy(result)
    effective = deepcopy(result)
    effective["selection_layers"]["fundamental"] = runtime_fundamental

    result["declared"] = declared
    result["effective_layers"] = effective
    result["runtime_switches"] = {
        "fundamental": effective["selection_layers"]["fundamental"],
        # These switches describe the current runtime path. Layer flags for
        # volume/technical/execution are metadata until their dedicated gates
        # are wired; mismatches are surfaced for research review.
        "volume": bool(_read_mapping(root.get("stock_pool"), "stock_pool").get("enabled", True)),
        "technical": True,
        "regime": bool(_read_mapping(root.get("entry_filters"), "entry_filters").get("market_gate_enabled", False)),
        "position": True,
        "risk": True,
        # The daily backtest does not simulate intraday T trades. The flag is
        # still retained in the declaration for live execution planning.
        "t_trading": False,
    }
    result["runtime_unmodeled_layers"] = ["t_trading"]
    unsupported_flags = [
        name
        for group, names in _LAYER_GROUPS.items()
        for name in sorted(names)
        if name != "fundamental"
        and result[group][name] != result["runtime_switches"].get(name)
    ]
    if result["execution_layers"]["t_trading"]:
        unsupported_flags.append("t_trading")
    result["unsupported_layer_flags"] = sorted(set(unsupported_flags))
    result["declaration_mismatches"] = [
        name
        for group, names in _LAYER_GROUPS.items()
        for name in sorted(names)
        if result[group][name] != result["runtime_switches"].get(name)
    ]

    result["effective"] = {
        f"{name}_enabled": enabled
        for name, enabled in result["runtime_switches"].items()
    }
    return result


_SENSITIVE_KEY_PARTS = (
    "token",
    "password",
    "secret",
    "webhook",
    "authorization",
    "device_key",
)


def _sanitize_config(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for name, item in value.items():
            text_name = str(name)
            if text_name.startswith("_"):
                continue
            if any(part in text_name.lower() for part in _SENSITIVE_KEY_PARTS):
                result[text_name] = "<redacted>"
            else:
                result[text_name] = _sanitize_config(item, text_name)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_config(item, key) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def build_config_snapshot(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a redacted, deterministic config snapshot and content hash."""

    snapshot = _sanitize_config(config or {})
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "config": snapshot,
    }
