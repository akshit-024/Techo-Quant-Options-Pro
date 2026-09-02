"""Application service that validates and atomically records normalized snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from teco_quant.domain.enums import DataSource, SnapshotStatus
from teco_quant.domain.models import (
    AtomicSnapshot,
    ContractSpec,
    MarketState,
    PreviousOptionSnapshot,
    StrategyContext,
    TechnicalState,
)
from teco_quant.ingestion.normalization import (
    DEFAULT_CHANGE_OI_MAX_INTERVAL,
    normalize_dhan_option_chain,
    raw_payload_hash,
)
from teco_quant.ingestion.validation import SnapshotValidator, ValidationReport
from teco_quant.strategy.spec import STRATEGY_VERSION


class SnapshotWriter(Protocol):
    def save_ingestion(
        self, snapshot: AtomicSnapshot, report: ValidationReport
    ) -> SnapshotStatus: ...


@dataclass(frozen=True, slots=True)
class IngestionResult:
    snapshot_id: str
    status: SnapshotStatus
    report: ValidationReport


class SnapshotIngestionService:
    def __init__(self, *, validator: SnapshotValidator, repository: SnapshotWriter) -> None:
        self._validator = validator
        self._repository = repository

    def ingest(
        self, snapshot: AtomicSnapshot, *, now: datetime | None = None
    ) -> IngestionResult:
        report = self._validator.validate(snapshot, now=now)
        status = self._repository.save_ingestion(snapshot, report)
        return IngestionResult(
            snapshot_id=snapshot.snapshot_id,
            status=status,
            report=report,
        )


def build_dhan_snapshot(
    payload: Mapping[str, Any],
    *,
    sequence: int,
    source_timestamp: datetime,
    received_at: datetime,
    contract: ContractSpec,
    market: MarketState,
    technicals: TechnicalState,
    context: StrategyContext,
    previous_snapshot: PreviousOptionSnapshot | None = None,
    max_change_oi_interval: timedelta = DEFAULT_CHANGE_OI_MAX_INTERVAL,
    strategy_version: str = STRATEGY_VERSION,
) -> AtomicSnapshot:
    option_payload_hash = raw_payload_hash(payload)
    option_chain = normalize_dhan_option_chain(
        payload,
        contract=contract,
        observed_at=source_timestamp,
        sequence=sequence,
        source=DataSource.DHAN_REST,
        previous_snapshot=previous_snapshot,
        max_change_oi_interval=max_change_oi_interval,
    )
    return AtomicSnapshot.create(
        sequence=sequence,
        source=DataSource.DHAN_REST,
        source_timestamp=source_timestamp,
        received_at=received_at,
        contract=contract,
        market=market,
        technicals=technicals,
        context=context,
        option_chain=option_chain,
        strategy_version=strategy_version,
        metadata={
            "provider": "DHAN",
            "provider_api_version": "v2",
            "normalizer_version": "dhan-v2-2",
            "raw_payload_hash": option_payload_hash,
            "raw_component_hashes": {"option_chain": option_payload_hash},
            "raw_component_payloads": {"option_chain": payload},
        },
    )
