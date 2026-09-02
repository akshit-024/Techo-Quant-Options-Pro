"""Deterministic indicators over completed OHLCV candles."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from .models import (
    AnalyticsInputError,
    Candle,
    HourlyConfirmation,
    TrendDirection,
    aware,
    decimal_value,
)


def _period(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AnalyticsInputError("indicator period must be a positive integer")
    return value


def _completed(
    candles: Iterable[Candle], *, as_of: datetime | None = None
) -> tuple[Candle, ...]:
    if as_of is not None:
        aware(as_of, name="indicator evaluation time")
    selected = sorted(
        (
            candle
            for candle in candles
            if candle.completed and (as_of is None or candle.end <= as_of)
        ),
        key=lambda candle: (candle.end, candle.start),
    )
    previous: Candle | None = None
    for candle in selected:
        if previous is not None and candle.start < previous.end:
            raise AnalyticsInputError("completed candles overlap")
        previous = candle
    return tuple(selected)


def vwap(candles: Iterable[Candle], *, as_of: datetime | None = None) -> Decimal | None:
    """Return typical-price VWAP, ignoring uncompleted/future candles."""

    selected = _completed(candles, as_of=as_of)
    total_volume = sum((candle.volume for candle in selected), Decimal(0))
    if total_volume == 0:
        return None
    weighted = sum(
        (
            ((candle.high + candle.low + candle.close) / Decimal(3)) * candle.volume
            for candle in selected
        ),
        Decimal(0),
    )
    return weighted / total_volume


def _ema_values(values: Sequence[Decimal], period: int) -> Decimal | None:
    selected_period = _period(period)
    if len(values) < selected_period:
        return None
    current = sum(values[:selected_period], Decimal(0)) / Decimal(selected_period)
    alpha = Decimal(2) / Decimal(selected_period + 1)
    for value in values[selected_period:]:
        current = value * alpha + current * (Decimal(1) - alpha)
    return current


def ema(
    candles: Iterable[Candle], period: int, *, as_of: datetime | None = None
) -> Decimal | None:
    """Return the latest EMA, seeded by the first full-period SMA."""

    selected = _completed(candles, as_of=as_of)
    return _ema_values(tuple(candle.close for candle in selected), period)


def wma(
    candles: Iterable[Candle], period: int, *, as_of: datetime | None = None
) -> Decimal | None:
    """Return a linearly weighted moving average (oldest weight 1)."""

    selected_period = _period(period)
    selected = _completed(candles, as_of=as_of)
    if len(selected) < selected_period:
        return None
    window = selected[-selected_period:]
    numerator = sum(
        (candle.close * Decimal(index) for index, candle in enumerate(window, start=1)),
        Decimal(0),
    )
    denominator = Decimal(selected_period * (selected_period + 1) // 2)
    return numerator / denominator


def wilder_rsi(
    candles: Iterable[Candle], period: int = 14, *, as_of: datetime | None = None
) -> float | None:
    """Return Wilder's RSI using an SMA seed and recursive Wilder smoothing."""

    selected_period = _period(period)
    selected = _completed(candles, as_of=as_of)
    if len(selected) < selected_period + 1:
        return None
    closes = tuple(candle.close for candle in selected)
    changes = tuple(current - previous for previous, current in pairwise(closes))
    gains = tuple(max(change, Decimal(0)) for change in changes)
    losses = tuple(max(-change, Decimal(0)) for change in changes)
    average_gain = sum(gains[:selected_period], Decimal(0)) / Decimal(selected_period)
    average_loss = sum(losses[:selected_period], Decimal(0)) / Decimal(selected_period)
    for gain, loss in zip(gains[selected_period:], losses[selected_period:]):
        average_gain = (
            average_gain * Decimal(selected_period - 1) + gain
        ) / Decimal(selected_period)
        average_loss = (
            average_loss * Decimal(selected_period - 1) + loss
        ) / Decimal(selected_period)
    if average_loss == 0:
        return 50.0 if average_gain == 0 else 100.0
    relative_strength = average_gain / average_loss
    return float(Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength))


def true_range(candle: Candle, previous_close: Decimal | None = None) -> Decimal:
    """Return true range for one completed candle."""

    if not candle.completed:
        raise AnalyticsInputError("true range requires a completed candle")
    if previous_close is None:
        return candle.high - candle.low
    normalized_close = decimal_value(previous_close, name="previous close")
    if normalized_close <= 0:
        raise AnalyticsInputError("previous close must be positive")
    return max(
        candle.high - candle.low,
        abs(candle.high - normalized_close),
        abs(candle.low - normalized_close),
    )


def atr(
    candles: Iterable[Candle], period: int = 14, *, as_of: datetime | None = None
) -> Decimal | None:
    """Return Wilder ATR, seeding the first TR with high minus low."""

    selected_period = _period(period)
    selected = _completed(candles, as_of=as_of)
    if len(selected) < selected_period:
        return None
    ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for candle in selected:
        ranges.append(true_range(candle, previous_close))
        previous_close = candle.close
    current = sum(ranges[:selected_period], Decimal(0)) / Decimal(selected_period)
    for value in ranges[selected_period:]:
        current = (
            current * Decimal(selected_period - 1) + value
        ) / Decimal(selected_period)
    return current


def _duration_microseconds(value: timedelta, *, name: str) -> int:
    total = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    if total <= 0:
        raise AnalyticsInputError(f"{name} must be positive")
    return total


def _utc_microseconds(value: datetime) -> int:
    normalized = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = normalized - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def resample_completed_candles(
    candles: Iterable[Candle],
    interval: timedelta,
    *,
    as_of: datetime,
    anchor: datetime | None = None,
) -> tuple[Candle, ...]:
    """Aggregate fully covered closed buckets without using future/partial data.

    A bucket is emitted only when its source candles are contiguous and cover the
    complete ``[bucket_start, bucket_end)`` interval.
    """

    aware(as_of, name="resampling evaluation time")
    interval_us = _duration_microseconds(interval, name="resampling interval")
    selected = _completed(candles, as_of=as_of)
    if not selected:
        return ()
    selected_anchor = anchor
    if selected_anchor is None:
        first = selected[0].start
        selected_anchor = first.replace(hour=0, minute=0, second=0, microsecond=0)
    aware(selected_anchor, name="resampling anchor")
    anchor_us = _utc_microseconds(selected_anchor)

    buckets: dict[int, list[Candle]] = {}
    for candle in selected:
        bucket_index = (_utc_microseconds(candle.start) - anchor_us) // interval_us
        bucket_end_us = anchor_us + (bucket_index + 1) * interval_us
        if _utc_microseconds(candle.end) > bucket_end_us:
            raise AnalyticsInputError("source candle crosses a resampling bucket boundary")
        buckets.setdefault(bucket_index, []).append(candle)

    result: list[Candle] = []
    for bucket_index in sorted(buckets):
        group = buckets[bucket_index]
        bucket_start = selected_anchor + timedelta(microseconds=bucket_index * interval_us)
        bucket_end = bucket_start + interval
        if group[0].start != bucket_start or group[-1].end != bucket_end:
            continue
        if any(left.end != right.start for left, right in pairwise(group)):
            continue
        result.append(
            Candle(
                start=bucket_start,
                end=bucket_end,
                open=group[0].open,
                high=max(candle.high for candle in group),
                low=min(candle.low for candle in group),
                close=group[-1].close,
                volume=sum((candle.volume for candle in group), Decimal(0)),
                completed=True,
            )
        )
    return tuple(result)


def hourly_confirmation(
    candles: Iterable[Candle],
    *,
    as_of: datetime,
    fast_period: int = 9,
    slow_period: int = 21,
    anchor: datetime | None = None,
) -> HourlyConfirmation:
    """Confirm a trend using EMAs of fully closed one-hour buckets."""

    fast = _period(fast_period)
    slow = _period(slow_period)
    if fast >= slow:
        raise AnalyticsInputError("hourly fast period must be below slow period")
    hourly = resample_completed_candles(
        candles, timedelta(hours=1), as_of=as_of, anchor=anchor
    )
    fast_value = ema(hourly, fast)
    slow_value = ema(hourly, slow)
    if fast_value is None or slow_value is None:
        direction = TrendDirection.INSUFFICIENT_DATA
    elif fast_value > slow_value:
        direction = TrendDirection.BULLISH
    elif fast_value < slow_value:
        direction = TrendDirection.BEARISH
    else:
        direction = TrendDirection.NEUTRAL
    return HourlyConfirmation(
        as_of=as_of,
        last_completed_hour=hourly[-1].end if hourly else None,
        fast_ema=fast_value,
        slow_ema=slow_value,
        direction=direction,
        completed_hours=len(hourly),
    )
