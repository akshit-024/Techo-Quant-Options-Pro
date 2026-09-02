"""Typed, broker-neutral execution contracts.

Nothing in this module imports a broker SDK or permits live trading by default.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ExecutionMode(StrEnum):
    OFF = "OFF"
    DATA_ONLY = "DATA_ONLY"
    PAPER_TRADING = "PAPER_TRADING"
    MANUAL_APPROVAL = "MANUAL_APPROVAL"
    LIVE_AUTOMATIC = "LIVE_AUTOMATIC"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderState(StrEnum):
    PENDING = "PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class FillState(StrEnum):
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"


class PositionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class SignalExecutionState(StrEnum):
    RECEIVED = "RECEIVED"
    BLOCKED = "BLOCKED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    UNCERTAIN = "UNCERTAIN"


@runtime_checkable
class ExecutablePlan(Protocol):
    """Read-only execution view implemented by immutable trade-plan objects."""

    @property
    def signal_id(self) -> str: ...

    @property
    def correlation_id(self) -> str: ...

    @property
    def symbol(self) -> str: ...

    @property
    def security_id(self) -> str: ...

    @property
    def side(self) -> OrderSide: ...

    @property
    def quantity(self) -> int: ...

    @property
    def limit_price(self) -> Decimal: ...

    @property
    def maximum_loss(self) -> Decimal: ...

    @property
    def signal_time(self) -> datetime: ...

    @property
    def data_time(self) -> datetime: ...

    @property
    def valid_until(self) -> datetime: ...

    @property
    def contract_expiry(self) -> datetime: ...

    @property
    def event_risk_active(self) -> bool | None: ...

    @property
    def expiry_risk_clear(self) -> bool: ...

    @property
    def strategy_version(self) -> str: ...


@dataclass(frozen=True, slots=True)
class TradePlan:
    signal_id: str
    correlation_id: str
    symbol: str
    security_id: str
    side: OrderSide
    quantity: int
    limit_price: Decimal
    maximum_loss: Decimal
    signal_time: datetime
    data_time: datetime
    valid_until: datetime
    contract_expiry: datetime
    event_risk_active: bool | None
    expiry_risk_clear: bool
    strategy_version: str


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    order_id: str
    correlation_id: str
    signal_id: str
    symbol: str
    security_id: str
    side: OrderSide
    quantity: int
    filled_quantity: int
    limit_price: Decimal
    average_fill_price: Decimal | None
    state: OrderState
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class BrokerFill:
    fill_id: str
    order_id: str
    quantity: int
    price: Decimal
    state: FillState
    filled_at: datetime


@dataclass(frozen=True, slots=True)
class Position:
    position_id: str
    signal_id: str
    symbol: str
    security_id: str
    side: OrderSide
    quantity: int
    average_entry_price: Decimal
    state: PositionState
    opened_at: datetime
    closed_at: datetime | None = None
    realized_pnl: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    signal_id: str
    actor: str
    reason: str
    approved_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    signal_id: str
    state: SignalExecutionState
    order: BrokerOrder | None = None
    fills: tuple[BrokerFill, ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class LiveOrderAcknowledgement:
    broker_order_id: str
    correlation_id: str
    state: OrderState
    filled_quantity: int = 0
    average_fill_price: Decimal | None = None


class LiveBrokerGateway(Protocol):
    """Minimal live boundary with mandatory idempotent reconciliation lookup."""

    def submit_order(
        self, plan: ExecutablePlan, *, correlation_id: str
    ) -> LiveOrderAcknowledgement: ...

    def lookup_by_correlation(
        self, correlation_id: str
    ) -> LiveOrderAcknowledgement | None: ...


def plan_to_dict(plan: ExecutablePlan) -> dict[str, Any]:
    return {
        "signal_id": plan.signal_id,
        "correlation_id": plan.correlation_id,
        "symbol": plan.symbol,
        "security_id": plan.security_id,
        "side": plan.side.value,
        "quantity": plan.quantity,
        "limit_price": str(plan.limit_price),
        "maximum_loss": str(plan.maximum_loss),
        "signal_time": plan.signal_time.isoformat(),
        "data_time": plan.data_time.isoformat(),
        "valid_until": plan.valid_until.isoformat(),
        "contract_expiry": plan.contract_expiry.isoformat(),
        "event_risk_active": plan.event_risk_active,
        "expiry_risk_clear": plan.expiry_risk_clear,
        "strategy_version": plan.strategy_version,
    }


def plan_from_dict(value: dict[str, Any]) -> TradePlan:
    return TradePlan(
        signal_id=str(value["signal_id"]),
        correlation_id=str(value["correlation_id"]),
        symbol=str(value["symbol"]),
        security_id=str(value["security_id"]),
        side=OrderSide(value["side"]),
        quantity=int(value["quantity"]),
        limit_price=Decimal(str(value["limit_price"])),
        maximum_loss=Decimal(str(value["maximum_loss"])),
        signal_time=datetime.fromisoformat(str(value["signal_time"])),
        data_time=datetime.fromisoformat(str(value["data_time"])),
        valid_until=datetime.fromisoformat(str(value["valid_until"])),
        contract_expiry=datetime.fromisoformat(str(value["contract_expiry"])),
        event_risk_active=value.get("event_risk_active"),
        expiry_risk_clear=bool(value["expiry_risk_clear"]),
        strategy_version=str(value["strategy_version"]),
    )


def model_to_dict(value: Any) -> dict[str, Any]:
    """Convert response dataclasses into JSON-friendly primitives."""

    raw: dict[str, Any] = asdict(value)

    def convert(item: Any) -> Any:
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, StrEnum):
            return item.value
        if isinstance(item, dict):
            return {key: convert(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [convert(child) for child in item]
        return item

    return {str(key): convert(child) for key, child in raw.items()}
