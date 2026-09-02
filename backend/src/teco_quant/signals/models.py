"""Typed outputs of the deterministic Sprint 2 signal pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from teco_quant.domain.enums import DecisionState, OptionType


@dataclass(frozen=True, slots=True)
class EvidenceBreakdown:
    factors: Mapping[str, float]
    points: Mapping[str, float]
    total: float


@dataclass(frozen=True, slots=True)
class RankedStrike:
    rank: int
    security_id: str
    strike: Decimal
    option_type: OptionType
    entry_ask: Decimal | None
    score: float
    evidence: EvidenceBreakdown | None
    liquidity_score: float
    eligible: bool
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TradePlan:
    """A broker-independent plan; ``actionable`` is never an order instruction."""

    signal_id: str
    snapshot_id: str
    contract_key: str
    strategy_version: str
    evidence_version: str
    generated_at: datetime
    decision: DecisionState
    actionable: bool
    symbol: str
    security_id: str
    option_type: OptionType
    strike: Decimal
    expiry: datetime
    score: float
    score_gap: float
    entry: Decimal
    stop: Decimal
    targets: tuple[Decimal, ...]
    lot_size: int
    lots: int
    quantity: int
    maximum_risk: Decimal
    risk_per_lot: Decimal
    premium_required: Decimal


@dataclass(frozen=True, slots=True)
class SignalPipelineResult:
    snapshot_id: str
    generated_at: datetime
    decision: DecisionState
    reason: str
    call_score: float
    put_score: float
    score_gap: float
    ranked_strikes: tuple[RankedStrike, ...]
    trade_plan: TradePlan | None
    warnings: tuple[str, ...] = ()

