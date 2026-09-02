"""Execution state machine and all pre-trade safety gates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite
from threading import RLock

from teco_quant.execution.errors import (
    ApprovalError,
    DuplicatePositionError,
    DuplicateSignalError,
    ExecutionBlockedError,
    ExecutionUncertainError,
    KillSwitchError,
    LiveExecutionDisabledError,
    PlanValidationError,
)
from teco_quant.execution.ledger import ExecutionLedger
from teco_quant.execution.models import (
    Approval,
    BrokerFill,
    BrokerOrder,
    ExecutablePlan,
    ExecutionMode,
    ExecutionResult,
    FillState,
    LiveBrokerGateway,
    LiveOrderAcknowledgement,
    OrderSide,
    OrderState,
    SignalExecutionState,
)
from teco_quant.execution.paper import PaperBroker


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    mode: ExecutionMode = ExecutionMode.OFF
    max_data_age_seconds: float = 30.0
    future_clock_skew_seconds: float = 2.0
    min_expiry_buffer_seconds: float = 900.0
    max_order_quantity: int = 10_000
    max_order_notional: Decimal = Decimal(1000000)
    max_plan_loss: Decimal = Decimal(50000)
    max_daily_loss: Decimal = Decimal(100000)
    max_consecutive_losses: int = 3
    live_enabled: bool = False

    def __post_init__(self) -> None:
        for name in (
            "max_data_age_seconds",
            "future_clock_skew_seconds",
            "min_expiry_buffer_seconds",
        ):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.max_order_quantity <= 0 or self.max_consecutive_losses <= 0:
            raise ValueError("quantity and consecutive-loss limits must be positive")
        for name in ("max_order_notional", "max_plan_loss", "max_daily_loss"):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


class ExecutionController:
    def __init__(
        self,
        *,
        ledger: ExecutionLedger,
        instrument_registry: Mapping[str, str],
        policy: ExecutionPolicy | None = None,
        paper_broker: PaperBroker | None = None,
        live_gateway: LiveBrokerGateway | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.ledger = ledger
        self.policy = policy or ExecutionPolicy()
        self._instrument_registry = dict(instrument_registry)
        self._registry_lock = RLock()
        self._paper = paper_broker or PaperBroker()
        self._live = live_gateway
        self._clock = clock or (lambda: datetime.now(UTC))

    def replace_instrument_registry(self, instruments: Mapping[str, str]) -> None:
        """Atomically replace the verified symbol/security map after a master refresh."""

        normalized: dict[str, str] = {}
        for raw_symbol, raw_security_id in instruments.items():
            if not isinstance(raw_symbol, str) or not raw_symbol.strip():
                raise ValueError("instrument-registry symbols must be non-empty strings")
            if not isinstance(raw_security_id, str) or not raw_security_id.strip():
                raise ValueError("instrument-registry security IDs must be non-empty strings")
            symbol = raw_symbol.strip()
            security_id = raw_security_id.strip()
            if symbol in normalized and normalized[symbol] != security_id:
                raise ValueError("instrument registry contains a conflicting symbol")
            normalized[symbol] = security_id
        with self._registry_lock:
            self._instrument_registry = normalized

    def submit(
        self, plan: ExecutablePlan, *, now: datetime | None = None
    ) -> ExecutionResult:
        current = now or self._clock()
        self._require_aware(current, "execution time")
        self._check_kill_limits(current)
        self._validate_plan(plan, current, check_signal=True)
        self.ledger.record_signal(plan, SignalExecutionState.RECEIVED, recorded_at=current)

        if self.policy.mode in (ExecutionMode.OFF, ExecutionMode.DATA_ONLY):
            self.ledger.update_signal_state(
                plan.signal_id, SignalExecutionState.BLOCKED, now=current
            )
            raise ExecutionBlockedError(
                f"execution mode {self.policy.mode.value} does not submit orders"
            )
        if self.policy.mode is ExecutionMode.MANUAL_APPROVAL:
            self.ledger.update_signal_state(
                plan.signal_id, SignalExecutionState.AWAITING_APPROVAL, now=current
            )
            return ExecutionResult(
                signal_id=plan.signal_id,
                state=SignalExecutionState.AWAITING_APPROVAL,
                message="manual approval required",
            )
        if self.policy.mode is ExecutionMode.PAPER_TRADING:
            return self._execute_paper(plan, current)
        if self.policy.mode is ExecutionMode.LIVE_AUTOMATIC:
            return self._execute_live(plan, current)
        raise ExecutionBlockedError("unsupported execution mode")

    def approve(
        self,
        signal_id: str,
        *,
        actor: str,
        reason: str,
        approved_at: datetime | None = None,
        now: datetime | None = None,
    ) -> ExecutionResult:
        if self.policy.mode is not ExecutionMode.MANUAL_APPROVAL:
            raise ApprovalError("manual approval is only valid in MANUAL_APPROVAL mode")
        current = now or self._clock()
        approval_time = approved_at or current
        self._require_aware(current, "execution time")
        self._require_aware(approval_time, "approval time")
        if not actor.strip() or not reason.strip():
            raise ApprovalError("manual approval requires actor and reason")
        if (approval_time - current).total_seconds() > self.policy.future_clock_skew_seconds:
            raise ApprovalError("approval timestamp is in the future")
        plan = self.ledger.plan(signal_id)
        if plan is None:
            raise ApprovalError("signal does not exist")
        if approval_time < plan.signal_time:
            raise ApprovalError("approval timestamp precedes the signal")
        if self.ledger.signal_state(signal_id) is not SignalExecutionState.AWAITING_APPROVAL:
            raise ApprovalError("signal is not awaiting approval")
        self._check_kill_limits(current)
        self._validate_plan(plan, current, check_signal=False)
        self.ledger.record_approval(
            Approval(
                signal_id=signal_id,
                actor=actor.strip(),
                reason=reason.strip(),
                approved_at=approval_time,
            )
        )
        # Manual approval deliberately routes to the deterministic paper broker in
        # this foundation. There is no live manual-order implementation.
        return self._execute_paper(plan, current)

    def set_kill_switch(
        self,
        *,
        active: bool,
        actor: str,
        reason: str,
        changed_at: datetime | None = None,
    ) -> None:
        self.ledger.set_kill_switch(
            active=active,
            actor=actor,
            reason=reason,
            changed_at=changed_at or self._clock(),
        )

    def status(self) -> dict[str, object]:
        return {
            "mode": self.policy.mode.value,
            "live_enabled": self.policy.live_enabled,
            "live_gateway_configured": self._live is not None,
            "kill_switch": self.ledger.kill_switch(),
            "counts": self.ledger.counts(),
        }

    def _validate_plan(
        self, plan: ExecutablePlan, now: datetime, *, check_signal: bool
    ) -> None:
        if check_signal and self.ledger.signal_exists(plan.signal_id):
            raise DuplicateSignalError("signal has already been processed")
        for name in ("signal_id", "correlation_id", "symbol", "security_id", "strategy_version"):
            value = getattr(plan, name)
            if not isinstance(value, str) or not value.strip():
                raise PlanValidationError(f"{name} is required")
        with self._registry_lock:
            expected_security = self._instrument_registry.get(plan.symbol)
        if expected_security is None or expected_security != plan.security_id:
            raise PlanValidationError("symbol/security ID does not match the execution registry")
        if isinstance(plan.quantity, bool) or not isinstance(plan.quantity, int):
            raise PlanValidationError("quantity must be an integer")
        if not isinstance(plan.side, OrderSide):
            raise PlanValidationError("order side must be BUY or SELL")
        if not 0 < plan.quantity <= self.policy.max_order_quantity:
            raise PlanValidationError("quantity exceeds the configured limit")
        if not plan.limit_price.is_finite() or plan.limit_price <= 0:
            raise PlanValidationError("limit price must be finite and positive")
        if not plan.maximum_loss.is_finite() or not (
            Decimal(0) < plan.maximum_loss <= self.policy.max_plan_loss
        ):
            raise PlanValidationError("maximum plan loss exceeds the configured limit")
        if plan.limit_price * plan.quantity > self.policy.max_order_notional:
            raise PlanValidationError("order notional exceeds the configured limit")
        for name in ("signal_time", "data_time", "valid_until", "contract_expiry"):
            self._require_aware(getattr(plan, name), name)
        data_age = (now - plan.data_time).total_seconds()
        if data_age > self.policy.max_data_age_seconds:
            raise PlanValidationError("market data is stale")
        if data_age < -self.policy.future_clock_skew_seconds:
            raise PlanValidationError("market data is future-dated")
        if plan.signal_time > now and (
            plan.signal_time - now
        ).total_seconds() > self.policy.future_clock_skew_seconds:
            raise PlanValidationError("signal timestamp is future-dated")
        if plan.valid_until <= now:
            raise PlanValidationError("execution plan has expired")
        if plan.event_risk_active is not False:
            raise PlanValidationError("event risk must be explicitly clear")
        if not plan.expiry_risk_clear:
            raise PlanValidationError("expiry-risk gate is not clear")
        if (
            plan.contract_expiry - now
        ).total_seconds() <= self.policy.min_expiry_buffer_seconds:
            raise PlanValidationError("contract is inside the expiry-risk buffer")
        if self.ledger.open_position_for_security(plan.security_id) is not None:
            raise DuplicatePositionError("an open position already exists for this security")

    def _check_kill_limits(self, now: datetime) -> None:
        current = self.ledger.kill_switch()
        if current["active"]:
            raise KillSwitchError(str(current.get("reason") or "kill switch is active"))
        daily_pnl = self.ledger.daily_realized_pnl(now.astimezone(UTC).date())
        if daily_pnl <= -self.policy.max_daily_loss:
            self.ledger.set_kill_switch(
                active=True,
                actor="system",
                reason="daily loss limit reached",
                changed_at=now,
            )
            raise KillSwitchError("daily loss limit reached")
        if self.ledger.consecutive_losses() >= self.policy.max_consecutive_losses:
            self.ledger.set_kill_switch(
                active=True,
                actor="system",
                reason="consecutive loss limit reached",
                changed_at=now,
            )
            raise KillSwitchError("consecutive loss limit reached")

    def _execute_paper(self, plan: ExecutablePlan, now: datetime) -> ExecutionResult:
        order, fills = self._paper.submit(plan, now=now)
        self.ledger.record_execution(order, fills, now=now)
        state = (
            SignalExecutionState.FILLED
            if order.state is OrderState.FILLED
            else SignalExecutionState.PARTIALLY_FILLED
            if order.state is OrderState.PARTIALLY_FILLED
            else SignalExecutionState.SUBMITTED
        )
        return ExecutionResult(plan.signal_id, state, order, fills)

    def _execute_live(self, plan: ExecutablePlan, now: datetime) -> ExecutionResult:
        if not self.policy.live_enabled:
            self.ledger.update_signal_state(
                plan.signal_id, SignalExecutionState.BLOCKED, now=now
            )
            raise LiveExecutionDisabledError("live execution is locked by policy")
        if self._live is None:
            self.ledger.update_signal_state(
                plan.signal_id, SignalExecutionState.BLOCKED, now=now
            )
            raise LiveExecutionDisabledError("no live broker gateway is configured")
        try:
            acknowledgement = self._live.submit_order(
                plan, correlation_id=plan.correlation_id
            )
        except TimeoutError:
            # A mutation timeout is ambiguous. Reconcile by the idempotency key and
            # never submit the mutation a second time.
            reconciled = self._live.lookup_by_correlation(plan.correlation_id)
            if reconciled is None:
                unknown = self._live_order(
                    plan,
                    LiveOrderAcknowledgement(
                        broker_order_id=f"unknown-{plan.correlation_id}",
                        correlation_id=plan.correlation_id,
                        state=OrderState.UNKNOWN,
                    ),
                    now,
                )
                self.ledger.record_execution(unknown, (), now=now)
                self.ledger.update_signal_state(
                    plan.signal_id, SignalExecutionState.UNCERTAIN, now=now
                )
                raise ExecutionUncertainError(
                    "live mutation timed out and reconciliation found no order; no retry was sent"
                )
            acknowledgement = reconciled
        if acknowledgement.correlation_id != plan.correlation_id:
            raise ExecutionUncertainError("broker reconciliation correlation mismatch")
        self._validate_live_acknowledgement(plan, acknowledgement)
        order = self._live_order(plan, acknowledgement, now)
        fills: tuple[BrokerFill, ...] = ()
        if acknowledgement.filled_quantity:
            if acknowledgement.average_fill_price is None:
                raise ExecutionUncertainError("broker reported fills without an average price")
            fills = (
                BrokerFill(
                    fill_id=f"live-fill-{acknowledgement.broker_order_id}-reconciled",
                    order_id=acknowledgement.broker_order_id,
                    quantity=acknowledgement.filled_quantity,
                    price=acknowledgement.average_fill_price,
                    state=(
                        FillState.COMPLETE
                        if acknowledgement.filled_quantity == plan.quantity
                        else FillState.PARTIAL
                    ),
                    filled_at=now,
                ),
            )
        self.ledger.record_execution(order, fills, now=now)
        state = (
            SignalExecutionState.FILLED
            if order.state is OrderState.FILLED
            else SignalExecutionState.PARTIALLY_FILLED
            if order.state is OrderState.PARTIALLY_FILLED
            else SignalExecutionState.SUBMITTED
        )
        return ExecutionResult(plan.signal_id, state, order, fills)

    @staticmethod
    def _live_order(
        plan: ExecutablePlan,
        acknowledgement: LiveOrderAcknowledgement,
        now: datetime,
    ) -> BrokerOrder:
        return BrokerOrder(
            order_id=acknowledgement.broker_order_id,
            correlation_id=plan.correlation_id,
            signal_id=plan.signal_id,
            symbol=plan.symbol,
            security_id=plan.security_id,
            side=plan.side,
            quantity=plan.quantity,
            filled_quantity=acknowledgement.filled_quantity,
            limit_price=plan.limit_price,
            average_fill_price=acknowledgement.average_fill_price,
            state=acknowledgement.state,
            submitted_at=now,
        )

    @staticmethod
    def _validate_live_acknowledgement(
        plan: ExecutablePlan, acknowledgement: LiveOrderAcknowledgement
    ) -> None:
        quantity = acknowledgement.filled_quantity
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or not 0 <= quantity <= plan.quantity
        ):
            raise ExecutionUncertainError(
                "broker acknowledgement has an invalid fill quantity"
            )
        price = acknowledgement.average_fill_price
        if quantity > 0 and (
            price is None or not price.is_finite() or price <= 0
        ):
            raise ExecutionUncertainError(
                "broker filled quantity lacks a valid average price"
            )
        if quantity == 0 and price is not None:
            raise ExecutionUncertainError("broker average price exists without a fill")

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PlanValidationError(f"{name} must be timezone-aware")
