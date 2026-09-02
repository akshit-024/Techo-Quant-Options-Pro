"""Versioned, transparent evidence policy for options-buying candidates."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

EVIDENCE_VERSION = "teco-evidence-1.0.0"


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    version: str = EVIDENCE_VERSION
    premium_momentum_saturation: float = 0.02
    volatility_ratio_floor: float = 0.50
    volatility_ratio_ceiling: float = 2.00
    theoretical_error_ceiling: float = 0.25
    daily_theta_drag_ceiling: float = 0.10
    reward_risk_full_score: float = 2.0

    def __post_init__(self) -> None:
        values = (
            self.premium_momentum_saturation,
            self.volatility_ratio_floor,
            self.volatility_ratio_ceiling,
            self.theoretical_error_ceiling,
            self.daily_theta_drag_ceiling,
            self.reward_risk_full_score,
        )
        if any(not isfinite(value) or value <= 0 for value in values):
            raise ValueError("evidence-policy numeric values must be finite and positive")
        if self.volatility_ratio_floor >= 1 or self.volatility_ratio_ceiling <= 1:
            raise ValueError("volatility ratio bounds must surround 1.0")


DEFAULT_EVIDENCE_POLICY = EvidencePolicy()

