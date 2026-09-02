from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from teco_quant.brokers.base import KeyedRateLimiter, TransportResponse
from teco_quant.brokers.dhan import DhanCredentials, DhanRestClient
from teco_quant.ingestion.dhan_historical import (
    completed_15m_boundary,
    completed_technical_state,
    expected_completed_15m_boundary,
    normalize_dhan_intraday,
    validate_completed_15m_coverage,
)
from teco_quant.ingestion.normalization import NormalizationError

NOW = datetime(2026, 8, 25, 6, 30, tzinfo=UTC)


class FakeTransport:
    def __init__(self, response: TransportResponse) -> None:
        self.response = response
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, json=None, timeout=10.0):
        self.requests.append(
            {"method": method, "url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return self.response


class DhanHistoricalTests(unittest.TestCase):
    def test_client_sends_aware_window_as_explicit_ist(self) -> None:
        payload = _historical_payload(NOW)
        transport = FakeTransport(
            TransportResponse(status_code=200, headers={}, body=payload, text="")
        )
        no_wait = KeyedRateLimiter(0)
        client = DhanRestClient(
            DhanCredentials("client", "token"),
            transport=transport,
            option_chain_limiter=no_wait,
            quote_limiter=no_wait,
            historical_limiter=no_wait,
        )
        result = client.intraday_candles(
            security_id="13",
            exchange_segment="IDX_I",
            instrument="INDEX",
            interval=15,
            from_datetime=datetime(2026, 8, 24, 3, 45, tzinfo=UTC),
            to_datetime=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        )

        self.assertIs(result, payload)
        request = transport.requests[0]
        self.assertTrue(request["url"].endswith("/v2/charts/intraday"))
        self.assertEqual(request["json"]["fromDate"], "2026-08-24 09:15:00")
        self.assertEqual(request["json"]["toDate"], "2026-08-24 15:30:00")
        self.assertEqual(request["json"]["interval"], "15")
        self.assertNotIn("token", str(request["json"]))

    def test_parallel_arrays_normalize_and_only_closed_candles_feed_indicators(self) -> None:
        payload = _historical_payload(NOW, include_partial=True)
        series = normalize_dhan_intraday(payload, interval_minutes=15, as_of=NOW)
        result = completed_technical_state(series, observed_at=NOW)

        self.assertEqual(len(series.candles), 47)
        self.assertEqual(len(series.completed), 46)
        self.assertFalse(series.candles[-1].completed)
        self.assertEqual(result.completed_candle_count, 46)
        self.assertEqual(result.latest_candle.end, NOW)
        self.assertTrue(result.state.completed_candle)
        self.assertIsNotNone(result.state.ema_9)
        self.assertIsNotNone(result.state.ema_21)
        self.assertIsNotNone(result.state.wma_44)
        self.assertIsNotNone(result.state.previous_wma_44)
        self.assertGreater(result.state.reference_volatility or 0, 0)
        self.assertGreater(result.session_vwap, Decimal(0))

    def test_mismatched_or_non_monotonic_arrays_are_rejected(self) -> None:
        mismatched = _historical_payload(NOW)
        mismatched["volume"] = mismatched["volume"][:-1]
        with self.assertRaisesRegex(NormalizationError, "different lengths"):
            normalize_dhan_intraday(mismatched, interval_minutes=15, as_of=NOW)

        unordered = _historical_payload(NOW)
        unordered["timestamp"][10] = unordered["timestamp"][9]
        with self.assertRaisesRegex(NormalizationError, "strictly increasing"):
            normalize_dhan_intraday(unordered, interval_minutes=15, as_of=NOW)

    def test_completed_boundary_is_ist_aligned_without_tzdata_dependency(self) -> None:
        value = datetime(2026, 8, 25, 4, 43, 12, tzinfo=UTC)
        boundary = completed_15m_boundary(value)
        self.assertEqual(boundary.isoformat(), "2026-08-25T10:00:00+05:30")

    def test_normal_session_requires_exact_latest_completed_boundary(self) -> None:
        series = normalize_dhan_intraday(
            _historical_payload(NOW, include_partial=True),
            interval_minutes=15,
            as_of=NOW,
        )
        result = completed_technical_state(series, observed_at=NOW)

        expected = validate_completed_15m_coverage(
            result,
            observed_at=NOW,
            exchange_segment="IDX_I",
        )

        self.assertEqual(expected, completed_15m_boundary(NOW))

    def test_six_hour_old_and_one_bucket_lagging_history_are_rejected(self) -> None:
        for lag in (timedelta(hours=6), timedelta(minutes=15)):
            with self.subTest(lag=lag):
                series = normalize_dhan_intraday(
                    _historical_payload(NOW - lag),
                    interval_minutes=15,
                    as_of=NOW,
                )
                result = completed_technical_state(series, observed_at=NOW)

                with self.assertRaisesRegex(
                    NormalizationError,
                    "does not match the expected IST boundary",
                ):
                    validate_completed_15m_coverage(
                        result,
                        observed_at=NOW,
                        exchange_segment="IDX_I",
                    )

    def test_weekend_and_after_hours_have_no_fresh_expected_boundary(self) -> None:
        weekend = datetime(2026, 8, 29, 6, 30, tzinfo=UTC)
        after_close = datetime(2026, 8, 25, 10, 1, tzinfo=UTC)

        with self.assertRaisesRegex(NormalizationError, "weekends"):
            expected_completed_15m_boundary(weekend, exchange_segment="IDX_I")
        with self.assertRaisesRegex(NormalizationError, "active IST session"):
            expected_completed_15m_boundary(after_close, exchange_segment="IDX_I")

    def test_intraday_continuity_allows_overnight_but_not_session_holes(self) -> None:
        overnight = _payload_for_starts(
            (
                datetime(2026, 8, 21, 3, 45, tzinfo=UTC),
                datetime(2026, 8, 21, 4, 0, tzinfo=UTC),
                datetime(2026, 8, 24, 3, 45, tzinfo=UTC),
                datetime(2026, 8, 24, 4, 0, tzinfo=UTC),
            )
        )
        series = normalize_dhan_intraday(
            overnight,
            interval_minutes=15,
            as_of=datetime(2026, 8, 24, 4, 15, tzinfo=UTC),
        )
        self.assertEqual(len(series.candles), 4)

        session_hole = _payload_for_starts(
            (
                datetime(2026, 8, 25, 3, 45, tzinfo=UTC),
                datetime(2026, 8, 25, 4, 15, tzinfo=UTC),
            )
        )
        with self.assertRaisesRegex(NormalizationError, "discontinuous"):
            normalize_dhan_intraday(
                session_hole,
                interval_minutes=15,
                as_of=datetime(2026, 8, 25, 4, 30, tzinfo=UTC),
            )


def _historical_payload(as_of: datetime, *, include_partial: bool = False) -> dict:
    count = 47 if include_partial else 46
    first = as_of - timedelta(minutes=15 * 46)
    values = {name: [] for name in ("open", "high", "low", "close", "volume", "timestamp")}
    for index in range(count):
        opening = Decimal(24700) + Decimal(index * 3) + Decimal(index % 4)
        closing = opening + (Decimal(2) if index % 2 else Decimal(-1))
        values["open"].append(float(opening))
        values["high"].append(float(max(opening, closing) + Decimal(4)))
        values["low"].append(float(min(opening, closing) - Decimal(4)))
        values["close"].append(float(closing))
        values["volume"].append(1000 + index * 10)
        values["timestamp"].append(int((first + timedelta(minutes=15 * index)).timestamp()))
    return values


def _payload_for_starts(starts: tuple[datetime, ...]) -> dict:
    values = {name: [] for name in ("open", "high", "low", "close", "volume", "timestamp")}
    for index, start in enumerate(starts):
        opening = 100 + index
        values["open"].append(opening)
        values["high"].append(opening + 2)
        values["low"].append(opening - 2)
        values["close"].append(opening + 1)
        values["volume"].append(1_000 + index)
        values["timestamp"].append(int(start.timestamp()))
    return values


if __name__ == "__main__":
    unittest.main()
