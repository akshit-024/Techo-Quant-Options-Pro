"""Strict Dhan intraday normalization and completed-candle technical state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal
from itertools import pairwise
from math import isfinite, log, sqrt
from statistics import pstdev
from typing import Any

from teco_quant.analytics.indicators import atr, ema, vwap, wilder_rsi, wma
from teco_quant.analytics.models import Candle
from teco_quant.domain.models import TechnicalState
from teco_quant.ingestion.normalization import (
    NormalizationError,
    decimal_value,
    integer_value,
)

_IST = timezone(timedelta(hours=5, minutes=30), name="IST")
SUPPORTED_INTERVALS = frozenset({1, 5, 15, 25, 60})
_DHAN_SESSION_HOURS: Mapping[str, tuple[time, time]] = {
    "IDX_I": (time(9, 15), time(15, 30)),
    "NSE_EQ": (time(9, 15), time(15, 30)),
    "NSE_FNO": (time(9, 15), time(15, 30)),
    "BSE_EQ": (time(9, 15), time(15, 30)),
    "BSE_FNO": (time(9, 15), time(15, 30)),
    "NSE_CURRENCY": (time(9), time(17)),
    "BSE_CURRENCY": (time(9), time(17)),
    "MCX_COMM": (time(9), time(23, 30)),
}


@dataclass(frozen=True, slots=True)
class DhanIntradaySeries:
    """One coherent set of provider OHLCV arrays."""

    interval_minutes: int
    candles: tuple[Candle, ...]
    source_first_timestamp: datetime
    source_last_timestamp: datetime

    @property
    def completed(self) -> tuple[Candle, ...]:
        return tuple(candle for candle in self.candles if candle.completed)


@dataclass(frozen=True, slots=True)
class CompletedTechnicalResult:
    state: TechnicalState
    session_vwap: Decimal
    latest_candle: Candle
    completed_candle_count: int


def normalize_dhan_intraday(
    payload: Mapping[str, Any],
    *,
    interval_minutes: int,
    as_of: datetime,
) -> DhanIntradaySeries:
    """Convert Dhan parallel arrays to ordered candles or reject the whole payload."""

    if isinstance(interval_minutes, bool) or interval_minutes not in SUPPORTED_INTERVALS:
        raise NormalizationError("unsupported Dhan intraday interval")
    _aware(as_of, name="intraday as_of")
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        raise NormalizationError("Dhan intraday data object is missing")
    names = ("open", "high", "low", "close", "volume", "timestamp")
    arrays: dict[str, Sequence[Any]] = {}
    for name in names:
        value = data.get(name)
        if not isinstance(value, (list, tuple)):
            raise NormalizationError(f"Dhan intraday {name} must be an array")
        arrays[name] = value
    lengths = {len(value) for value in arrays.values()}
    if len(lengths) != 1:
        raise NormalizationError("Dhan intraday OHLCV arrays have different lengths")
    count = next(iter(lengths), 0)
    if count == 0:
        raise NormalizationError("Dhan intraday payload contains no candles")

    duration = timedelta(minutes=interval_minutes)
    candles: list[Candle] = []
    previous_start: datetime | None = None
    for index in range(count):
        epoch = integer_value(arrays["timestamp"][index], field=f"timestamp[{index}]")
        assert epoch is not None
        if epoch <= 0:
            raise NormalizationError(f"timestamp[{index}] must be a positive Unix epoch")
        try:
            start = datetime.fromtimestamp(epoch, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise NormalizationError(f"timestamp[{index}] is outside the supported range") from exc
        if previous_start is not None and start <= previous_start:
            raise NormalizationError("Dhan intraday timestamps must be strictly increasing")
        if (
            previous_start is not None
            and start.astimezone(_IST).date()
            == previous_start.astimezone(_IST).date()
            and start - previous_start != duration
        ):
            raise NormalizationError(
                "Dhan intraday candles are discontinuous within an IST session"
            )
        previous_start = start
        end = start + duration
        candle = Candle(
            start=start,
            end=end,
            open=_required_price(arrays["open"][index], f"open[{index}]"),
            high=_required_price(arrays["high"][index], f"high[{index}]"),
            low=_required_price(arrays["low"][index], f"low[{index}]"),
            close=_required_price(arrays["close"][index], f"close[{index}]"),
            volume=_required_volume(arrays["volume"][index], f"volume[{index}]"),
            completed=end <= as_of,
        )
        candles.append(candle)

    return DhanIntradaySeries(
        interval_minutes=interval_minutes,
        candles=tuple(candles),
        source_first_timestamp=candles[0].start,
        source_last_timestamp=candles[-1].start,
    )


def completed_technical_state(
    series: DhanIntradaySeries,
    *,
    observed_at: datetime,
    annual_periods: int = 6_300,
) -> CompletedTechnicalResult:
    """Calculate the required 15-minute indicators without partial-candle leakage."""

    _aware(observed_at, name="technical observed_at")
    if series.interval_minutes != 15:
        raise NormalizationError("strategy technicals require 15-minute candles")
    if isinstance(annual_periods, bool) or not isinstance(annual_periods, int) or annual_periods <= 0:
        raise NormalizationError("annual_periods must be a positive integer")
    completed = tuple(
        candle for candle in series.candles if candle.completed and candle.end <= observed_at
    )
    if len(completed) < 45:
        raise NormalizationError(
            "at least 45 completed 15-minute candles are required for WMA44 history"
        )

    ema_9 = ema(completed, 9, as_of=observed_at)
    ema_21 = ema(completed, 21, as_of=observed_at)
    wma_44 = wma(completed, 44, as_of=observed_at)
    previous_wma_44 = wma(completed[:-1], 44, as_of=observed_at)
    rsi_14 = wilder_rsi(completed, 14, as_of=observed_at)
    atr_14 = atr(completed, 14, as_of=observed_at)
    if any(value is None for value in (ema_9, ema_21, wma_44, previous_wma_44, rsi_14, atr_14)):
        raise NormalizationError("completed candle history cannot produce required indicators")

    latest = completed[-1]
    latest_session_date = latest.end.astimezone(_IST).date()
    session = tuple(
        candle
        for candle in completed
        if candle.end.astimezone(_IST).date() == latest_session_date
    )
    session_vwap = vwap(session, as_of=observed_at)
    if session_vwap is None or not session_vwap.is_finite() or session_vwap <= 0:
        raise NormalizationError("latest completed session cannot produce a positive VWAP")
    reference_volatility = _realized_volatility(completed, annual_periods=annual_periods)
    if reference_volatility is None:
        raise NormalizationError("completed candle history cannot produce positive volatility")

    assert ema_9 is not None
    assert ema_21 is not None
    assert wma_44 is not None
    assert previous_wma_44 is not None
    assert rsi_14 is not None
    assert atr_14 is not None
    return CompletedTechnicalResult(
        state=TechnicalState(
            # This is the calculation observation instant.  The latest closed
            # candle boundary is returned separately and preserved in metadata.
            observed_at=observed_at,
            ema_9=ema_9,
            ema_21=ema_21,
            wma_44=wma_44,
            previous_wma_44=previous_wma_44,
            rsi_14=rsi_14,
            atr_14=atr_14,
            reference_volatility=reference_volatility,
            timeframe="15m",
            completed_candle=True,
        ),
        session_vwap=session_vwap,
        latest_candle=latest,
        completed_candle_count=len(completed),
    )


def completed_15m_boundary(value: datetime) -> datetime:
    """Return the latest 15-minute boundary in IST as an aware instant."""

    _aware(value, name="boundary time")
    local = value.astimezone(_IST)
    minute = local.minute - (local.minute % 15)
    return local.replace(minute=minute, second=0, microsecond=0)


def expected_completed_15m_boundary(
    value: datetime,
    *,
    exchange_segment: str,
) -> datetime:
    """Return the candle boundary required for fresh in-session technicals.

    This is intentionally a conservative acquisition policy rather than a holiday
    calendar. Weekends and times outside the regular session are rejected outright.
    On an exchange holiday, the provider's last candle cannot match the expected
    current-day boundary, so the same fail-closed coverage check rejects it.
    """

    _aware(value, name="expected boundary time")
    segment = str(exchange_segment).strip().upper()
    try:
        open_time, close_time = _DHAN_SESSION_HOURS[segment]
    except KeyError as exc:
        raise NormalizationError(
            f"unsupported Dhan historical session segment: {segment!r}"
        ) from exc
    local = value.astimezone(_IST)
    if local.weekday() >= 5:
        raise NormalizationError("fresh technicals are unavailable on weekends")
    session_open = local.replace(
        hour=open_time.hour,
        minute=open_time.minute,
        second=0,
        microsecond=0,
    )
    session_close = local.replace(
        hour=close_time.hour,
        minute=close_time.minute,
        second=0,
        microsecond=0,
    )
    first_completed = session_open + timedelta(minutes=15)
    if local < first_completed or local > session_close:
        raise NormalizationError(
            "fresh technicals require a completed candle in the active IST session"
        )
    boundary = completed_15m_boundary(local)
    if boundary < first_completed or boundary > session_close:
        raise NormalizationError(
            "expected completed candle boundary is outside the active IST session"
        )
    return boundary


def validate_completed_15m_coverage(
    result: CompletedTechnicalResult,
    *,
    observed_at: datetime,
    exchange_segment: str,
) -> datetime:
    """Require technical inputs to cover the latest expected in-session bucket."""

    if not isinstance(result, CompletedTechnicalResult):
        raise NormalizationError("technical result has an unsupported type")
    _aware(observed_at, name="technical coverage observed_at")
    expected = expected_completed_15m_boundary(
        observed_at,
        exchange_segment=exchange_segment,
    )
    latest_end = result.latest_candle.end
    _aware(latest_end, name="latest completed candle end")
    if not result.latest_candle.completed or latest_end != expected:
        raise NormalizationError(
            "latest completed 15-minute candle does not match the expected IST boundary"
        )
    return expected


def _realized_volatility(
    candles: Sequence[Candle], *, annual_periods: int
) -> float | None:
    if len(candles) < 2:
        return None
    returns = [
        log(float(current.close / previous.close))
        for previous, current in pairwise(candles)
    ]
    if not returns:
        return None
    deviation = pstdev(returns)
    annualized = deviation * sqrt(annual_periods)
    if not isfinite(annualized) or annualized <= 0:
        return None
    return annualized


def _required_price(value: Any, field: str) -> Decimal:
    selected = decimal_value(value, field=field)
    assert selected is not None
    if selected <= 0:
        raise NormalizationError(f"{field} must be positive")
    return selected


def _required_volume(value: Any, field: str) -> Decimal:
    selected = decimal_value(value, field=field)
    assert selected is not None
    if selected < 0:
        raise NormalizationError(f"{field} cannot be negative")
    return selected


def _aware(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise NormalizationError(f"{name} must be timezone-aware")
    return value
