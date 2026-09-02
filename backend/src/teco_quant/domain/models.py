"""Normalized domain models for market data and automation state.

Prices are represented with :class:`decimal.Decimal`; ratios, volatility, and Greeks use
floats. Timestamps must be timezone-aware and are checked by the validation layer before a
snapshot is accepted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from teco_quant.domain.enums import (
    DataSource,
    Exchange,
    MarketKind,
    OperatingMode,
    OptionType,
    PricingModel,
    TradingStyle,
)
from teco_quant.serialization import content_hash


@dataclass(frozen=True, slots=True)
class InstrumentId:
    exchange: Exchange
    segment: str
    security_id: str
    symbol: str

    @property
    def canonical_key(self) -> str:
        return f"{self.exchange}:{self.segment}:{self.security_id}"


@dataclass(frozen=True, slots=True)
class InstrumentMasterProvenance:
    batch_id: str
    provider: str
    source_url: str
    content_hash: str
    schema_version: str
    fetched_at: datetime
    row_count: int


@dataclass(frozen=True, slots=True)
class InstrumentMasterRecord:
    instrument: InstrumentId
    display_name: str
    instrument_type: str
    underlying_security_id: str | None = None
    expiry: datetime | None = None
    strike: Decimal | None = None
    option_type: OptionType | None = None
    lot_size: int | None = None
    tick_size: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ContractSpec:
    underlying: InstrumentId
    market_kind: MarketKind
    pricing_model: PricingModel
    option_expiry: datetime
    lot_size: int
    strike_interval: Decimal
    tick_size: Decimal
    master: InstrumentMasterProvenance
    option_contracts: tuple[InstrumentMasterRecord, ...]
    futures: InstrumentMasterRecord | None = None

    @property
    def contract_key(self) -> str:
        future_fields = (
            _master_record_identity(self.futures) if self.futures is not None else ()
        )
        option_fields = tuple(
            sorted(_master_record_identity(record) for record in self.option_contracts)
        )
        material = repr(
            (
                self.master.batch_id,
                self.master.provider,
                self.master.source_url,
                self.master.content_hash,
                self.master.schema_version,
                _identity_timestamp(self.master.fetched_at),
                self.master.row_count,
                self.underlying.canonical_key,
                self.market_kind.value,
                self.pricing_model.value,
                _identity_timestamp(self.option_expiry),
                self.lot_size,
                str(self.strike_interval),
                str(self.tick_size),
                future_fields,
                option_fields,
            )
        )
        return f"contract:{sha256(material.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class Greeks:
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    theoretical_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OptionQuote:
    security_id: str
    strike: Decimal
    option_type: OptionType
    expiry: datetime
    bid: Decimal | None
    ask: Decimal | None
    ltp: Decimal | None
    volume: int | None
    open_interest: int | None
    previous_open_interest: int | None
    change_open_interest: int | None
    implied_volatility: float | None
    greeks: Greeks
    observed_at: datetime
    bid_quantity: int | None = None
    ask_quantity: int | None = None
    previous_close: Decimal | None = None
    change_oi_source_snapshot_id: str | None = None
    change_oi_interval_seconds: float | None = None

    @property
    def key(self) -> tuple[Decimal, OptionType]:
        return (self.strike, self.option_type)

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_ratio(self) -> float | None:
        spread = self.spread
        if spread is None or self.ask is None or self.ask <= 0:
            return None
        return float(spread / self.ask)


@dataclass(frozen=True, slots=True)
class MarketState:
    observed_at: datetime
    spot_price: Decimal | None
    futures_price: Decimal | None
    previous_close: Decimal | None = None
    day_open: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    vwap: Decimal | None = None
    futures_open_interest: int | None = None

    def pricing_underlying(self, market_kind: MarketKind) -> Decimal | None:
        if market_kind is MarketKind.COMMODITY:
            return self.futures_price
        return self.spot_price


@dataclass(frozen=True, slots=True)
class TechnicalState:
    observed_at: datetime
    ema_9: Decimal | None = None
    ema_21: Decimal | None = None
    wma_44: Decimal | None = None
    previous_wma_44: Decimal | None = None
    rsi_14: float | None = None
    atr_14: Decimal | None = None
    reference_volatility: float | None = None
    timeframe: str = "15m"
    completed_candle: bool = True


@dataclass(frozen=True, slots=True)
class AtomicSnapshot:
    snapshot_id: str
    sequence: int
    source: DataSource
    source_timestamp: datetime
    received_at: datetime
    contract: ContractSpec
    market: MarketState
    technicals: TechnicalState
    context: StrategyContext
    option_chain: tuple[OptionQuote, ...]
    strategy_version: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        metadata = dict(self.metadata)
        metadata["normalized_component_hashes"] = {
            "contract": content_hash(self.contract),
            "market": content_hash(self.market),
            "technicals": content_hash(self.technicals),
            "context": content_hash(self.context),
            "option_chain": content_hash(self.option_chain),
        }
        object.__setattr__(self, "metadata", _freeze_mapping(metadata))

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        source: DataSource,
        source_timestamp: datetime,
        received_at: datetime,
        contract: ContractSpec,
        market: MarketState,
        technicals: TechnicalState,
        context: StrategyContext,
        option_chain: tuple[OptionQuote, ...],
        strategy_version: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> AtomicSnapshot:
        return cls(
            snapshot_id=str(uuid4()),
            sequence=sequence,
            source=source,
            source_timestamp=source_timestamp,
            received_at=received_at,
            contract=contract,
            market=market,
            technicals=technicals,
            context=context,
            option_chain=option_chain,
            strategy_version=strategy_version,
            metadata=metadata or {},
        )


@dataclass(frozen=True, slots=True)
class ManualOverride:
    field_name: str
    imported_value: Any
    override_value: Any | None = None
    overridden_by: str | None = None
    overridden_at: datetime | None = None
    reason: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.field_name not in MANUAL_OVERRIDE_FIELDS:
            raise ValueError(f"unsupported manual-override field: {self.field_name!r}")
        if not isinstance(self.overridden_by, str) or not self.overridden_by.strip():
            raise ValueError("manual override requires a non-empty actor")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("manual override requires a non-empty reason")
        if self.overridden_at is None or not _is_aware(self.overridden_at):
            raise ValueError("manual override requires a timezone-aware override timestamp")
        if self.expires_at is not None:
            if not _is_aware(self.expires_at):
                raise ValueError("manual-override expiry must be timezone-aware")
            if not self.is_active:
                raise ValueError("an inactive manual override cannot have an expiry")
            if self.expires_at <= self.overridden_at:
                raise ValueError("manual-override expiry must be after its override timestamp")

    @property
    def is_active(self) -> bool:
        return self.override_value is not None

    def effective_value(self, now: datetime | None = None) -> Any:
        if not self.is_active:
            return self.imported_value
        if now is not None and not _is_aware(now):
            raise ValueError("manual-override evaluation time must be timezone-aware")
        if self.expires_at is not None:
            evaluation_time = now or datetime.now(UTC)
            if evaluation_time >= self.expires_at:
                return self.imported_value
        return self.override_value


@dataclass(frozen=True, slots=True)
class StrategyContext:
    operating_mode: OperatingMode
    trading_style: TradingStyle
    account_capital: Decimal
    risk_per_trade: float
    maximum_premium_allocation: float
    event_risk_active: bool | None
    price_action_confirmed: bool | None
    signal_candle_high: Decimal
    signal_candle_low: Decimal
    expected_holding_hours: float


@dataclass(frozen=True, slots=True)
class PreviousOptionSnapshot:
    snapshot_id: str
    sequence: int
    source: DataSource
    source_timestamp: datetime
    contract_key: str
    option_chain: tuple[OptionQuote, ...]


# Only decision inputs may be manually superseded. Provider identity, contract,
# pricing, and option-chain fields remain immutable source evidence.
MANUAL_OVERRIDE_FIELDS = frozenset(
    {
        "context.operating_mode",
        "context.trading_style",
        "context.account_capital",
        "context.risk_per_trade",
        "context.maximum_premium_allocation",
        "context.event_risk_active",
        "context.price_action_confirmed",
        "context.signal_candle_high",
        "context.signal_candle_low",
        "context.expected_holding_hours",
        "technicals.reference_volatility",
    }
)


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _identity_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None or value.utcoffset() is None:
        return f"NAIVE:{value.isoformat()}"
    return value.astimezone(UTC).isoformat()


def _master_record_identity(record: InstrumentMasterRecord) -> tuple[str, ...]:
    return (
        record.instrument.canonical_key,
        record.instrument.symbol,
        record.display_name,
        record.instrument_type,
        record.underlying_security_id or "",
        _identity_timestamp(record.expiry),
        str(record.strike) if record.strike is not None else "",
        record.option_type.value if record.option_type is not None else "",
        str(record.lot_size) if record.lot_size is not None else "",
        str(record.tick_size) if record.tick_size is not None else "",
    )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType({str(key): freeze(child) for key, child in item.items()})
        if isinstance(item, (list, tuple)):
            return tuple(freeze(child) for child in item)
        return item

    return MappingProxyType(
        {str(key): freeze(child) for key, child in value.items()}
    )
