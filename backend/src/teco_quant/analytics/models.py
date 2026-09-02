"""Value objects used by deterministic market analytics.

Analytics deliberately operate only on explicitly completed candles.  A candle's
``end`` is exclusive: it is safe to use at an evaluation time equal to ``end``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class AnalyticsInputError(ValueError):
    """Raised when an analytics input cannot be used safely."""


class TrendDirection(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


def decimal_value(value: Decimal | float | str, *, name: str) -> Decimal:
    """Convert a numeric boundary value without importing binary float artefacts."""

    if isinstance(value, bool):
        raise AnalyticsInputError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AnalyticsInputError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise AnalyticsInputError(f"{name} must be finite")
    return result


def aware(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AnalyticsInputError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class Candle:
    """An OHLCV interval with explicit completion provenance."""

    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    completed: bool = True

    def __post_init__(self) -> None:
        aware(self.start, name="candle start")
        aware(self.end, name="candle end")
        if self.end <= self.start:
            raise AnalyticsInputError("candle end must be after candle start")
        if not isinstance(self.completed, bool):
            raise AnalyticsInputError("candle completed flag must be boolean")

        for field_name in ("open", "high", "low", "close"):
            normalized = decimal_value(getattr(self, field_name), name=f"candle {field_name}")
            if normalized <= 0:
                raise AnalyticsInputError(f"candle {field_name} must be positive")
            object.__setattr__(self, field_name, normalized)
        normalized_volume = decimal_value(self.volume, name="candle volume")
        if normalized_volume < 0:
            raise AnalyticsInputError("candle volume cannot be negative")
        object.__setattr__(self, "volume", normalized_volume)

        if self.high < max(self.open, self.close, self.low):
            raise AnalyticsInputError("candle high is below an OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise AnalyticsInputError("candle low is above an OHLC value")


@dataclass(frozen=True, slots=True)
class HourlyConfirmation:
    """Higher-timeframe EMA state evaluated from closed one-hour candles only."""

    as_of: datetime
    last_completed_hour: datetime | None
    fast_ema: Decimal | None
    slow_ema: Decimal | None
    direction: TrendDirection
    completed_hours: int


@dataclass(frozen=True, slots=True)
class ExpectedMove:
    underlying: Decimal
    move: Decimal
    lower_bound: Decimal
    upper_bound: Decimal

