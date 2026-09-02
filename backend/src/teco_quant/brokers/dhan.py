"""Current DhanHQ v2 read-only market-data adapter.

Sprint 1 intentionally exposes no order mutation methods. Authentication headers and
provider shapes are normalized at this boundary and never leak into strategy code.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import IntEnum
from struct import unpack_from
from typing import Any
from urllib.parse import urlencode

from teco_quant.brokers.base import HttpxTransport, KeyedRateLimiter, RestTransport

DHAN_API_BASE_URL = "https://api.dhan.co/v2"
DHAN_INSTRUMENT_MASTER_COMPACT = "https://images.dhan.co/api-data/api-scrip-master.csv"
DHAN_INSTRUMENT_MASTER_DETAILED = (
    "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
)
DHAN_LIVE_FEED_URL = "wss://api-feed.dhan.co"
DHAN_INTRADAY_INTERVALS = frozenset({1, 5, 15, 25, 60})
DHAN_EXCHANGE_SEGMENTS = frozenset(
    {
        "IDX_I",
        "NSE_EQ",
        "NSE_FNO",
        "NSE_CURRENCY",
        "BSE_EQ",
        "BSE_FNO",
        "BSE_CURRENCY",
        "MCX_COMM",
    }
)
DHAN_INSTRUMENT_TYPES = frozenset(
    {
        "INDEX",
        "FUTIDX",
        "OPTIDX",
        "EQUITY",
        "FUTSTK",
        "OPTSTK",
        "FUTCOM",
        "OPTFUT",
        "FUTCUR",
        "OPTCUR",
    }
)
_IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class DhanError(RuntimeError):
    """Base class for sanitized provider failures."""


class DhanConfigurationError(DhanError):
    pass


class DhanAuthenticationError(DhanError):
    pass


class DhanRateLimitError(DhanError):
    pass


class DhanResponseError(DhanError):
    def __init__(self, message: str, *, status_code: int, error_code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class DhanCredentials:
    client_id: str = field(repr=False)
    access_token: str = field(repr=False)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> DhanCredentials:
        values = environment if environment is not None else os.environ
        client_id = values.get("DHAN_CLIENT_ID", "").strip()
        token = values.get("DHAN_ACCESS_TOKEN", "").strip()
        if not client_id or not token:
            raise DhanConfigurationError(
                "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be provided outside the workbook"
            )
        return cls(client_id=client_id, access_token=token)


class DhanRestClient:
    """Read-only REST client for profile, instruments, quotes, and option chains."""

    def __init__(
        self,
        credentials: DhanCredentials,
        *,
        transport: RestTransport | None = None,
        base_url: str = DHAN_API_BASE_URL,
        option_chain_limiter: KeyedRateLimiter | None = None,
        quote_limiter: KeyedRateLimiter | None = None,
        historical_limiter: KeyedRateLimiter | None = None,
    ) -> None:
        if not credentials.client_id or not credentials.access_token:
            raise DhanConfigurationError("Dhan credentials cannot be blank")
        self._credentials = credentials
        self._transport = transport or HttpxTransport()
        self._base_url = base_url.rstrip("/")
        self._option_chain_limiter = option_chain_limiter or KeyedRateLimiter(3.0)
        self._quote_limiter = quote_limiter or KeyedRateLimiter(1.0)
        self._historical_limiter = historical_limiter or KeyedRateLimiter(1.0)

    def _headers(self) -> Mapping[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": self._credentials.access_token,
            "client-id": self._credentials.client_id,
        }

    def profile(self) -> Mapping[str, Any]:
        return self._json_request("GET", "/profile")

    def expiry_list(self, *, underlying_security_id: int, underlying_segment: str) -> list[str]:
        key = f"expiry:{underlying_segment}:{underlying_security_id}"
        self._option_chain_limiter.wait(key)
        payload = {
            "UnderlyingScrip": underlying_security_id,
            "UnderlyingSeg": underlying_segment,
        }
        response = self._json_request("POST", "/optionchain/expirylist", payload)
        data = response.get("data")
        if not isinstance(data, list) or not all(isinstance(value, str) for value in data):
            raise DhanResponseError("Dhan expiry-list payload is malformed", status_code=200)
        return data

    def option_chain(
        self,
        *,
        underlying_security_id: int,
        underlying_segment: str,
        expiry: date,
    ) -> Mapping[str, Any]:
        key = f"chain:{underlying_segment}:{underlying_security_id}:{expiry.isoformat()}"
        self._option_chain_limiter.wait(key)
        payload = {
            "UnderlyingScrip": underlying_security_id,
            "UnderlyingSeg": underlying_segment,
            "Expiry": expiry.isoformat(),
        }
        response = self._json_request("POST", "/optionchain", payload)
        data = response.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("oc"), Mapping):
            raise DhanResponseError("Dhan option-chain payload is malformed", status_code=200)
        return response

    def market_quote(
        self, instruments_by_segment: Mapping[str, Sequence[int | str]]
    ) -> Mapping[str, Any]:
        count = sum(len(values) for values in instruments_by_segment.values())
        if count == 0:
            raise ValueError("at least one instrument is required")
        if count > 1_000:
            raise ValueError("Dhan Market Quote supports at most 1,000 instruments per request")
        payload = {
            segment: [int(security_id) for security_id in security_ids]
            for segment, security_ids in instruments_by_segment.items()
        }
        self._quote_limiter.wait("market_quote")
        response = self._json_request("POST", "/marketfeed/quote", payload)
        if not isinstance(response.get("data"), Mapping):
            raise DhanResponseError("Dhan market-quote payload is malformed", status_code=200)
        return response

    def intraday_candles(
        self,
        *,
        security_id: str | int,
        exchange_segment: str,
        instrument: str,
        interval: int,
        from_datetime: datetime,
        to_datetime: datetime,
        include_open_interest: bool = False,
    ) -> Mapping[str, Any]:
        """Read Dhan's minute OHLCV arrays for one active instrument.

        Dhan accepts timezone-less request strings.  This boundary requires aware
        instants and converts them to India Standard Time explicitly, preventing a
        machine's local timezone from silently changing the requested window.
        """

        selected_security_id = str(security_id).strip()
        selected_segment = str(exchange_segment).strip().upper()
        selected_instrument = str(instrument).strip().upper()
        if not selected_security_id:
            raise ValueError("security_id is required")
        if selected_segment not in DHAN_EXCHANGE_SEGMENTS:
            raise ValueError(f"unsupported Dhan exchange segment: {selected_segment!r}")
        if selected_instrument not in DHAN_INSTRUMENT_TYPES:
            raise ValueError(f"unsupported Dhan instrument type: {selected_instrument!r}")
        if isinstance(interval, bool) or interval not in DHAN_INTRADAY_INTERVALS:
            raise ValueError("Dhan intraday interval must be one of 1, 5, 15, 25, or 60")
        if not isinstance(include_open_interest, bool):
            raise TypeError("include_open_interest must be boolean")
        for value, name in (
            (from_datetime, "from_datetime"),
            (to_datetime, "to_datetime"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if to_datetime <= from_datetime:
            raise ValueError("to_datetime must be later than from_datetime")
        if to_datetime - from_datetime > timedelta(days=90):
            raise ValueError("Dhan intraday requests cannot span more than 90 days")

        payload = {
            "securityId": selected_security_id,
            "exchangeSegment": selected_segment,
            "instrument": selected_instrument,
            "interval": str(interval),
            "oi": include_open_interest,
            "fromDate": from_datetime.astimezone(_IST).strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": to_datetime.astimezone(_IST).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._historical_limiter.wait(
            f"intraday:{selected_segment}:{selected_security_id}:{interval}"
        )
        response = self._json_request("POST", "/charts/intraday", payload)
        data = response.get("data", response)
        if not isinstance(data, Mapping):
            raise DhanResponseError("Dhan intraday payload is malformed", status_code=200)
        required = ("open", "high", "low", "close", "volume", "timestamp")
        if any(not isinstance(data.get(name), list) for name in required):
            raise DhanResponseError("Dhan intraday payload is malformed", status_code=200)
        return response

    def instrument_master(self, *, detailed: bool = True) -> str:
        url = (
            DHAN_INSTRUMENT_MASTER_DETAILED
            if detailed
            else DHAN_INSTRUMENT_MASTER_COMPACT
        )
        response = self._transport.request("GET", url, timeout=30.0)
        self._raise_for_error(response.status_code, response.body)
        if not response.text.strip():
            raise DhanResponseError("Dhan instrument master is empty", status_code=200)
        return response.text

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()

    def _json_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        response = self._transport.request(
            method,
            f"{self._base_url}{path}",
            headers=self._headers(),
            json=payload,
            timeout=10.0,
        )
        self._raise_for_error(response.status_code, response.body)
        if not isinstance(response.body, Mapping):
            raise DhanResponseError("Dhan returned non-JSON data", status_code=response.status_code)
        status = response.body.get("status")
        if status is not None and str(status).lower() not in {"success", "ok"}:
            code = self._extract_error_code(response.body)
            self._raise_provider_failure(response.status_code, code)
        return response.body

    def _raise_for_error(self, status_code: int, body: Any) -> None:
        if 200 <= status_code < 300:
            return
        code = self._extract_error_code(body)
        self._raise_provider_failure(status_code, code)

    def _raise_provider_failure(self, status_code: int, code: str | None) -> None:
        if status_code in {401, 403} or code in {"DH-901", "DH-902", "808", "809"}:
            raise DhanAuthenticationError("Dhan authentication or subscription was rejected")
        if status_code == 429 or code == "DH-904":
            raise DhanRateLimitError("Dhan rate limit reached")
        raise DhanResponseError(
            "Dhan request failed",
            status_code=status_code,
            error_code=code,
        )

    @staticmethod
    def _extract_error_code(body: Any) -> str | None:
        if not isinstance(body, Mapping):
            return None
        for key in ("errorCode", "error_code", "code"):
            value = body.get(key)
            if value is not None:
                return str(value)
        return None


class DhanFeedMode(IntEnum):
    TICKER = 15
    QUOTE = 17
    FULL = 21


def live_feed_url(credentials: DhanCredentials) -> str:
    query = urlencode(
        {
            "version": "2",
            "token": credentials.access_token,
            "clientId": credentials.client_id,
            "authType": "2",
        }
    )
    return f"{DHAN_LIVE_FEED_URL}?{query}"


def subscription_messages(
    instruments: Iterable[tuple[str, str]],
    *,
    mode: DhanFeedMode = DhanFeedMode.FULL,
) -> tuple[Mapping[str, Any], ...]:
    """Build Dhan subscription requests in the documented 100-instrument batches."""

    normalized = [
        {"ExchangeSegment": segment, "SecurityId": str(security_id)}
        for segment, security_id in instruments
    ]
    messages: list[Mapping[str, Any]] = []
    for start in range(0, len(normalized), 100):
        batch = normalized[start : start + 100]
        messages.append(
            {
                "RequestCode": int(mode),
                "InstrumentCount": len(batch),
                "InstrumentList": batch,
            }
        )
    return tuple(messages)


@dataclass(frozen=True, slots=True)
class DhanDepthLevel:
    bid_quantity: int
    ask_quantity: int
    bid_orders: int
    ask_orders: int
    bid_price: float
    ask_price: float


@dataclass(frozen=True, slots=True)
class DhanFeedPacket:
    response_code: int
    message_length: int
    exchange_segment_code: int
    security_id: str
    fields: Mapping[str, int | float]
    depth: tuple[DhanDepthLevel, ...] = ()


def decode_feed_packet(packet: bytes) -> DhanFeedPacket:
    """Decode the stable header and common Dhan v2 ticker/quote/OI/full fields.

    Full packets include all five documented bid/ask levels. Packet length is checked before
    any field is read so truncated data can never be accepted.
    """

    if len(packet) < 8:
        raise ValueError("Dhan feed packet is shorter than its 8-byte header")
    response_code, message_length, segment_code, security_id = unpack_from("<BHBI", packet, 0)
    if message_length != len(packet):
        raise ValueError(
            f"Dhan feed packet length mismatch: header={message_length}, actual={len(packet)}"
        )
    exact_lengths = {2: 16, 4: 50, 5: 12, 6: 16, 8: 162, 50: 10}
    expected_length = exact_lengths.get(response_code)
    if expected_length is None:
        raise ValueError(f"unsupported Dhan feed response code: {response_code}")
    if len(packet) != expected_length:
        raise ValueError(
            f"invalid Dhan response-code {response_code} packet length: "
            f"expected={expected_length}, actual={len(packet)}"
        )
    fields: dict[str, int | float] = {}
    if response_code == 2:
        fields["last_price"] = unpack_from("<f", packet, 8)[0]
        fields["last_trade_epoch"] = unpack_from("<I", packet, 12)[0]
    elif response_code == 4:
        fields.update(
            {
                "last_price": unpack_from("<f", packet, 8)[0],
                "last_quantity": unpack_from("<H", packet, 12)[0],
                "last_trade_epoch": unpack_from("<I", packet, 14)[0],
                "average_price": unpack_from("<f", packet, 18)[0],
                "volume": unpack_from("<I", packet, 22)[0],
                "total_sell_quantity": unpack_from("<I", packet, 26)[0],
                "total_buy_quantity": unpack_from("<I", packet, 30)[0],
                "day_open": unpack_from("<f", packet, 34)[0],
                "day_close": unpack_from("<f", packet, 38)[0],
                "day_high": unpack_from("<f", packet, 42)[0],
                "day_low": unpack_from("<f", packet, 46)[0],
            }
        )
    elif response_code == 5:
        fields["open_interest"] = unpack_from("<I", packet, 8)[0]
    elif response_code == 6:
        fields["previous_close"] = unpack_from("<f", packet, 8)[0]
        fields["previous_open_interest"] = unpack_from("<I", packet, 12)[0]
    elif response_code == 8:
        fields.update(
            {
                "last_price": unpack_from("<f", packet, 8)[0],
                "last_quantity": unpack_from("<H", packet, 12)[0],
                "last_trade_epoch": unpack_from("<I", packet, 14)[0],
                "average_price": unpack_from("<f", packet, 18)[0],
                "volume": unpack_from("<I", packet, 22)[0],
                "total_sell_quantity": unpack_from("<I", packet, 26)[0],
                "total_buy_quantity": unpack_from("<I", packet, 30)[0],
                "open_interest": unpack_from("<I", packet, 34)[0],
                "highest_open_interest": unpack_from("<I", packet, 38)[0],
                "lowest_open_interest": unpack_from("<I", packet, 42)[0],
                "day_open": unpack_from("<f", packet, 46)[0],
                "day_close": unpack_from("<f", packet, 50)[0],
                "day_high": unpack_from("<f", packet, 54)[0],
                "day_low": unpack_from("<f", packet, 58)[0],
            }
        )
    elif response_code == 50:
        fields["disconnect_code"] = unpack_from("<H", packet, 8)[0]
    depth: tuple[DhanDepthLevel, ...] = ()
    if response_code == 8:
        depth = tuple(
            DhanDepthLevel(*unpack_from("<IIHHff", packet, 62 + level * 20))
            for level in range(5)
        )
    return DhanFeedPacket(
        response_code=response_code,
        message_length=message_length,
        exchange_segment_code=segment_code,
        security_id=str(security_id),
        fields=fields,
        depth=depth,
    )
