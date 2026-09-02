"""Typed, provider-neutral inputs and outputs for deterministic option replay.

The replay clock is deliberately explicit: a signal carried by a ``ReplayFrame`` is
known only at that frame's timestamp and can first be filled by a later frame.  This
keeps signal production separate from execution and prevents same-bar look-ahead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from math import isfinite

ZERO = Decimal(0)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_finite_decimal(value: Decimal, name: str) -> None:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


class ExitReason(StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"
    EXPIRY = "EXPIRY"
    TIME = "TIME"
    END_OF_DATA = "END_OF_DATA"


class JournalEvent(StrEnum):
    SIGNAL = "SIGNAL"
    ENTRY = "ENTRY"
    EXIT_FILL = "EXIT_FILL"
    EXIT = "EXIT"
    SKIPPED = "SKIPPED"
    UNFILLED = "UNFILLED"


@dataclass(frozen=True, slots=True)
class OptionBar:
    """Point-in-time option bar plus executable top-of-book quote.

    ``timestamp`` is the instant at which all fields became available to the replay.
    Quantities are optional because normal backtests do not invent partial fills.  They
    are used only when :class:`PartialFillConfig` is explicitly enabled.
    """

    instrument_id: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    bid: Decimal
    ask: Decimal
    bid_quantity: int | None = None
    ask_quantity: int | None = None

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("bar instrument_id cannot be blank")
        _require_aware(self.timestamp, "bar timestamp")
        prices = {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "bid": self.bid,
            "ask": self.ask,
        }
        for name, value in prices.items():
            _require_finite_decimal(value, f"bar {name}")
            if value <= ZERO:
                raise ValueError(f"bar {name} must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high must be at least open, low, and close")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low must be at most open, high, and close")
        if self.ask < self.bid:
            raise ValueError("bar ask cannot be below bid")
        for name, quantity in (
            ("bid_quantity", self.bid_quantity),
            ("ask_quantity", self.ask_quantity),
        ):
            if quantity is not None and quantity < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class BacktestSignal:
    """A long-option instruction produced from information known at ``generated_at``."""

    signal_id: str
    instrument_id: str
    generated_at: datetime
    option_side: str
    lots: int
    lot_size: int
    stop_price: Decimal
    target_price: Decimal
    contract_expiry: datetime
    score: float
    score_band: str
    market: str
    expiry_bucket: str
    moneyness: str
    time_bucket: str
    volatility_bucket: str
    regime: str
    max_holding: timedelta | None = None
    valid_until: datetime | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    @property
    def requested_quantity(self) -> int:
        return self.lots * self.lot_size

    def __post_init__(self) -> None:
        if not self.signal_id.strip() or not self.instrument_id.strip():
            raise ValueError("signal_id and instrument_id cannot be blank")
        if not self.option_side.strip():
            raise ValueError("option_side cannot be blank")
        _require_aware(self.generated_at, "signal generated_at")
        _require_aware(self.contract_expiry, "signal contract_expiry")
        if self.contract_expiry <= self.generated_at:
            raise ValueError("contract_expiry must be after signal generation")
        if self.valid_until is not None:
            _require_aware(self.valid_until, "signal valid_until")
            if self.valid_until <= self.generated_at:
                raise ValueError("valid_until must be after signal generation")
        if self.max_holding is not None and self.max_holding <= timedelta(0):
            raise ValueError("max_holding must be positive")
        if self.lots <= 0 or self.lot_size <= 0:
            raise ValueError("lots and lot_size must be positive")
        _require_finite_decimal(self.stop_price, "signal stop_price")
        _require_finite_decimal(self.target_price, "signal target_price")
        if self.stop_price < ZERO or self.target_price <= self.stop_price:
            raise ValueError("long-option prices require 0 <= stop < target")
        if not isfinite(self.score) or not 0 <= self.score <= 100:
            raise ValueError("signal score must be finite and within 0..100")
        dimensions = {
            "score_band": self.score_band,
            "market": self.market,
            "expiry_bucket": self.expiry_bucket,
            "moneyness": self.moneyness,
            "time_bucket": self.time_bucket,
            "volatility_bucket": self.volatility_bucket,
            "regime": self.regime,
        }
        if any(not value.strip() for value in dimensions.values()):
            raise ValueError("signal breakdown dimensions cannot be blank")


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    """All replay information that becomes visible at one clock instant."""

    timestamp: datetime
    bars: tuple[OptionBar, ...] = ()
    signals: tuple[BacktestSignal, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "frame timestamp")
        instruments: set[str] = set()
        for bar in self.bars:
            if bar.timestamp != self.timestamp:
                raise ValueError("every bar timestamp must equal its frame timestamp")
            if bar.instrument_id in instruments:
                raise ValueError("a frame cannot contain duplicate instrument bars")
            instruments.add(bar.instrument_id)
        signal_ids: set[str] = set()
        for signal in self.signals:
            if signal.generated_at != self.timestamp:
                raise ValueError(
                    "a signal must be placed in the frame where it became available"
                )
            if signal.signal_id in signal_ids:
                raise ValueError("a frame cannot contain duplicate signal IDs")
            signal_ids.add(signal.signal_id)


@dataclass(frozen=True, slots=True)
class SlippageConfig:
    entry_bps: Decimal = ZERO
    exit_bps: Decimal = ZERO
    fixed_entry_per_unit: Decimal = ZERO
    fixed_exit_per_unit: Decimal = ZERO

    def __post_init__(self) -> None:
        for name, value in (
            ("entry_bps", self.entry_bps),
            ("exit_bps", self.exit_bps),
            ("fixed_entry_per_unit", self.fixed_entry_per_unit),
            ("fixed_exit_per_unit", self.fixed_exit_per_unit),
        ):
            _require_finite_decimal(value, name)
            if value < ZERO:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Per-order and notional-based costs, kept neutral across broker providers."""

    brokerage_per_order: Decimal = ZERO
    entry_tax_rate: Decimal = ZERO
    exit_tax_rate: Decimal = ZERO
    transaction_fee_rate: Decimal = ZERO
    regulatory_fee_rate: Decimal = ZERO
    gst_rate: Decimal = ZERO

    def __post_init__(self) -> None:
        for name, value in (
            ("brokerage_per_order", self.brokerage_per_order),
            ("entry_tax_rate", self.entry_tax_rate),
            ("exit_tax_rate", self.exit_tax_rate),
            ("transaction_fee_rate", self.transaction_fee_rate),
            ("regulatory_fee_rate", self.regulatory_fee_rate),
            ("gst_rate", self.gst_rate),
        ):
            _require_finite_decimal(value, name)
            if value < ZERO:
                raise ValueError(f"{name} cannot be negative")
        for name, value in (
            ("entry_tax_rate", self.entry_tax_rate),
            ("exit_tax_rate", self.exit_tax_rate),
            ("transaction_fee_rate", self.transaction_fee_rate),
            ("regulatory_fee_rate", self.regulatory_fee_rate),
            ("gst_rate", self.gst_rate),
        ):
            if value > Decimal(1):
                raise ValueError(f"{name} must be within 0..1")


@dataclass(frozen=True, slots=True)
class PartialFillConfig:
    enabled: bool = False
    maximum_participation_rate: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        _require_finite_decimal(
            self.maximum_participation_rate, "maximum_participation_rate"
        )
        if not ZERO < self.maximum_participation_rate <= Decimal(1):
            raise ValueError("maximum_participation_rate must be within (0, 1]")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_capital: Decimal
    slippage: SlippageConfig = field(default_factory=SlippageConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    partial_fills: PartialFillConfig = field(default_factory=PartialFillConfig)

    def __post_init__(self) -> None:
        _require_finite_decimal(self.initial_capital, "initial_capital")
        if self.initial_capital <= ZERO:
            raise ValueError("initial_capital must be positive")


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    brokerage: Decimal = ZERO
    taxes: Decimal = ZERO
    transaction_fees: Decimal = ZERO
    regulatory_fees: Decimal = ZERO
    gst: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return (
            self.brokerage
            + self.taxes
            + self.transaction_fees
            + self.regulatory_fees
            + self.gst
        )

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(
            brokerage=self.brokerage + other.brokerage,
            taxes=self.taxes + other.taxes,
            transaction_fees=self.transaction_fees + other.transaction_fees,
            regulatory_fees=self.regulatory_fees + other.regulatory_fees,
            gst=self.gst + other.gst,
        )


@dataclass(frozen=True, slots=True)
class JournalEntry:
    timestamp: datetime
    event: JournalEvent
    signal_id: str
    instrument_id: str
    message: str
    quantity: int = 0
    price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Trade:
    signal_id: str
    instrument_id: str
    option_side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: int
    lots: Decimal
    stop_price: Decimal
    target_price: Decimal
    exit_reason: ExitReason
    entry_costs: CostBreakdown
    exit_costs: CostBreakdown
    gross_pnl: Decimal
    net_pnl: Decimal
    initial_risk: Decimal
    r_multiple: Decimal
    slippage_cost: Decimal
    holding_seconds: float
    score: float
    score_band: str
    market: str
    expiry_bucket: str
    moneyness: str
    time_bucket: str
    volatility_bucket: str
    regime: str

    @property
    def total_costs(self) -> Decimal:
        return self.entry_costs.total + self.exit_costs.total


@dataclass(frozen=True, slots=True)
class OpenPositionSummary:
    signal_id: str
    instrument_id: str
    remaining_quantity: int
    entry_price: Decimal
    last_timestamp: datetime
    pending_exit_reason: ExitReason | None


@dataclass(frozen=True, slots=True)
class BreakdownMetrics:
    trades: int
    wins: int
    win_rate: float
    gross_pnl: Decimal
    net_pnl: Decimal
    expectancy: Decimal
    average_r: Decimal


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_pnl: Decimal
    net_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal
    max_drawdown: Decimal
    average_r: Decimal
    sharpe_like: float | None
    return_on_capital: Decimal
    exposure_ratio: float
    average_holding_seconds: float
    total_slippage: Decimal
    total_costs: Decimal


@dataclass(frozen=True, slots=True)
class BacktestReport:
    started_at: datetime
    ended_at: datetime
    initial_capital: Decimal
    ending_capital: Decimal
    trades: tuple[Trade, ...]
    journal: tuple[JournalEntry, ...]
    metrics: BacktestMetrics
    breakdowns: Mapping[str, Mapping[str, BreakdownMetrics]]
    open_positions: tuple[OpenPositionSummary, ...] = ()
