"""Versioned signal scoring, ranking, and planning."""

from .adapters import to_backtest_signal, to_execution_plan
from .models import EvidenceBreakdown, RankedStrike, SignalPipelineResult, TradePlan
from .pipeline import SignalPipeline
from .policy import DEFAULT_EVIDENCE_POLICY, EVIDENCE_VERSION, EvidencePolicy
from .service import AutomatedSignalService

__all__ = [
    "DEFAULT_EVIDENCE_POLICY",
    "EVIDENCE_VERSION",
    "AutomatedSignalService",
    "EvidenceBreakdown",
    "EvidencePolicy",
    "RankedStrike",
    "SignalPipeline",
    "SignalPipelineResult",
    "TradePlan",
    "to_backtest_signal",
    "to_execution_plan",
]
