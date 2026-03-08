"""Manual override helpers for market regime decisions."""

from dataclasses import dataclass
from typing import Dict, Optional


OVERRIDE_TO_REGIME = {
    "auto": None,
    "force_bull": "bull",
    "force_bear": "bear",
    "force_range": "range",
}


@dataclass(frozen=True)
class RegimeOverride:
    """Represents a user supplied override on top of auto regime detection."""

    mode: str = "auto"
    reason: Optional[str] = None

    def __post_init__(self):
        if self.mode not in OVERRIDE_TO_REGIME:
            raise ValueError(f"Unsupported override mode: {self.mode}")

    @property
    def is_active(self) -> bool:
        return OVERRIDE_TO_REGIME[self.mode] is not None

    @property
    def forced_regime(self) -> Optional[str]:
        return OVERRIDE_TO_REGIME[self.mode]

    def apply(self, auto_regime: str) -> Dict[str, Optional[str]]:
        final_regime = self.forced_regime or auto_regime
        return {
            "auto_regime": auto_regime,
            "final_regime": final_regime,
            "override_mode": self.mode,
            "override_reason": self.reason,
            "is_overridden": self.is_active,
        }

    @classmethod
    def from_config(cls, config: Dict) -> "RegimeOverride":
        override_config = (config or {}).get("manual_overrides", {})
        return cls(
            mode=override_config.get("regime_override", "auto"),
            reason=override_config.get("regime_override_reason"),
        )
