"""Shared entry-signal execution policy for live analysis and backtests.

Supports regime-aware overrides: the `by_regime` section in config allows
per-market-regime signal policies that tighten the static policy on top
of the base `signals` map.  Missing regime keys fall back to the static
policy for that signal type.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SIGNAL_EXECUTION_MODES = ("enabled", "observe_only", "disabled")
_MODE_PRIORITY: dict[str, int] = {"disabled": 0, "observe_only": 1, "enabled": 2}
_VALID_REGIMES = frozenset({"bull", "range", "bear"})


def _validated_mode(value: Any, *, field: str) -> str:
    mode = str(value).strip().lower()
    if mode not in SIGNAL_EXECUTION_MODES:
        allowed = "/".join(SIGNAL_EXECUTION_MODES)
        raise ValueError(f"{field} must be one of {allowed}, got {value!r}")
    return mode


def _resolve_signal_map(raw: Any, context: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError(f"{context} must be a mapping")
    return {
        str(key): _validated_mode(value, field=f"{context}.{key}")
        for key, value in raw.items()
    }


def resolve_signal_execution_policy(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve and strictly validate the configured entry-signal policy.

    Returns a dict with keys:
      default  – fallback mode for unlisted signal types
      signals  – static signal-type → mode map
      by_regime – regime → {signal_type → mode} overrides
    """
    strategy = config.get("signal_strategy", {})
    if not isinstance(strategy, dict):
        raise TypeError("signal_strategy must be a mapping")
    raw = strategy.get("execution_policy", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TypeError("signal_strategy.execution_policy must be a mapping")
    default = _validated_mode(
        raw.get("default", "enabled"),
        field="signal_strategy.execution_policy.default",
    )
    signals = _resolve_signal_map(
        raw.get("signals", {}),
        "signal_strategy.execution_policy.signals",
    )
    raw_by_regime = raw.get("by_regime", {})
    if raw_by_regime is None:
        raw_by_regime = {}
    if not isinstance(raw_by_regime, dict):
        raise TypeError("signal_strategy.execution_policy.by_regime must be a mapping")
    by_regime: dict[str, dict[str, str]] = {}
    for regime, value in raw_by_regime.items():
        r = str(regime).lower().strip()
        if r not in _VALID_REGIMES:
            raise ValueError(f"by_regime key must be one of {_VALID_REGIMES}, got {r!r}")
        by_regime[r] = _resolve_signal_map(
            value,
            f"signal_strategy.execution_policy.by_regime.{r}",
        )
    return {"default": default, "signals": signals, "by_regime": by_regime}


def signal_execution_mode(signal_type: str, policy: dict[str, Any]) -> str:
    """Return the configured mode for one entry signal type (static only)."""
    signals = policy.get("signals", {})
    mode = signals.get(str(signal_type), policy.get("default", "enabled"))
    return _validated_mode(mode, field=f"signal execution mode for {signal_type}")


def signal_execution_mode_with_regime(
    signal_type: str,
    policy: dict[str, Any],
    regime: str | None,
) -> str:
    """Return the effective mode considering regime overrides.

    Regime overrides can only tighten the static mode.  This keeps a
    user-selected ``observe_only``/``disabled`` setting from being silently
    re-enabled by a ``bull`` override in the configuration UI.
    """
    base = signal_execution_mode(signal_type, policy)
    if regime is None or regime not in _VALID_REGIMES:
        return base
    by_regime = policy.get("by_regime", {})
    regime_overrides = by_regime.get(str(regime), {})
    if not regime_overrides:
        return base
    override = _validated_mode(
        regime_overrides.get(str(signal_type), base),
        field=f"signal_execution_mode_with_regime({regime}.{signal_type})",
    )
    return min((base, override), key=lambda mode: _MODE_PRIORITY[mode])


def effective_signal_execution_mode(
    signal_types: Iterable[str],
    policy: dict[str, Any],
    regime: str | None = None,
) -> str:
    """Resolve a multi-component entry event under an optional regime."""
    names = [str(item) for item in signal_types if str(item)]
    if not names:
        return _validated_mode(
            policy.get("default", "enabled"),
            field="signal execution default",
        )
    return max(
        (signal_execution_mode_with_regime(name, policy, regime) for name in names),
        key=lambda mode: _MODE_PRIORITY[mode],
    )


def partition_entry_signals(
    events: Iterable[dict[str, Any]],
    policy: dict[str, Any],
    regime_lookup: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition entry signals into executable, observed and disabled lists.

    When *regime_lookup* is provided (day → regime), the per-signal policy
    is resolved with regime awareness.  Events without a matching regime
    key use the static policy.
    """
    grouped: dict[str, list[dict[str, Any]]] = {
        "enabled": [],
        "observe_only": [],
        "disabled": [],
    }
    for event in events:
        regime = None
        if regime_lookup is not None:
            day = str(event.get("day", ""))
            regime = regime_lookup.get(day)
        mode = signal_execution_mode_with_regime(
            str(event.get("signal_type", "")), policy, regime,
        )
        annotated = dict(event)
        annotated["execution_mode"] = mode
        annotated["regime"] = regime
        grouped[mode].append(annotated)
    return grouped["enabled"], grouped["observe_only"], grouped["disabled"]
