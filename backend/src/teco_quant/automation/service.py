"""End-to-end Sprint 2 orchestration with execution kept behind explicit modes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from teco_quant.domain.models import AtomicSnapshot, ContractSpec, PreviousOptionSnapshot
from teco_quant.execution import ExecutionController
from teco_quant.execution.errors import ExecutionBlockedError
from teco_quant.execution.models import ExecutionResult
from teco_quant.ingestion.validation import ValidationReport
from teco_quant.signals import AutomatedSignalService, SignalPipelineResult
from teco_quant.signals.adapters import to_execution_plan

from .history import SignalHistoryRepository


@dataclass(frozen=True, slots=True)
class AutomationRunResult:
    signal: SignalPipelineResult
    execution: ExecutionResult | None
    execution_blocked_reason: str | None = None


class TecoAutomationService:
    def __init__(
        self,
        *,
        execution_controller: ExecutionController,
        signal_history: SignalHistoryRepository,
        signal_service: AutomatedSignalService | None = None,
    ) -> None:
        self.execution_controller = execution_controller
        self.signal_history = signal_history
        self._signals = signal_service or AutomatedSignalService()

    def process(
        self,
        snapshot: AtomicSnapshot,
        report: ValidationReport,
        *,
        previous_snapshot: PreviousOptionSnapshot | None,
        now: datetime,
    ) -> AutomationRunResult:
        signal = self._signals.evaluate(
            snapshot,
            report,
            previous_snapshot=previous_snapshot,
            now=now,
        )
        self.signal_history.record(signal)
        plan = signal.trade_plan
        if plan is None or not plan.actionable:
            return AutomationRunResult(signal=signal, execution=None)
        execution_plan = to_execution_plan(plan, snapshot)
        try:
            execution = self.execution_controller.submit(execution_plan, now=now)
        except ExecutionBlockedError as exc:
            return AutomationRunResult(
                signal=signal,
                execution=None,
                execution_blocked_reason=str(exc),
            )
        return AutomationRunResult(signal=signal, execution=execution)


def execution_registry_for_contract(contract: ContractSpec) -> dict[str, str]:
    """Build the exact option-symbol/security registry consumed by execution gates."""

    return {
        record.instrument.symbol: record.instrument.security_id
        for record in contract.option_contracts
    }

