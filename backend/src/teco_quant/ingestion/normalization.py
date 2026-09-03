"""Provider payload normalization into canonical units and identifiers."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
from math import isfinite
from typing import Any

from teco_quant.domain.enums import DataSource, Exchange, OptionType
from teco_quant.domain.models import (
    ContractSpec,
    Greeks,
    InstrumentId,
    InstrumentMasterProvenance,
    InstrumentMasterRecord,
    OptionQuote,
    PreviousOptionSnapshot,
)


class NormalizationError(ValueError):
    pass


DEFAULT_CHANGE_OI_MAX_INTERVAL = timedelta(seconds=30)
DEFAULT_DHAN_QUOTE_MAX_AGE = timedelta(seconds=30)
DEFAULT_DHAN_QUOTE_FUTURE_SKEW = timedelta(seconds=2)
DHAN_QUOTE_RECEIPT_FALLBACK_WARNING = "LAST_TRADE_TIME_ABSENT_RECEIPT_FALLBACK"
_IST = timezone(timedelta(hours=5, minutes=30), name="IST")

DHAN_API_SEGMENTS = frozenset(
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

_DHAN_RAW_SEGMENT_EXCHANGES: Mapping[str, frozenset[str]] = {
    "E": frozenset({"NSE", "BSE"}),
    "D": frozenset({"NSE", "BSE"}),
    "C": frozenset({"NSE", "BSE"}),
    "M": frozenset({"MCX"}),
}


def _is_known_unsupported_dhan_master_route(*, exchange: Any, segment: Any) -> bool:
    """Identify provider rows that cannot be addressed through Dhan APIs.

    The public detailed master can contain known raw segment letters paired with an
    unrelated exchange (currently, for example, ``NSE/M``).  Those rows are outside
    the addressable Dhan route matrix and must not poison ingestion of the supported
    universe.  Blank or unknown values deliberately return ``False`` so the normal
    strict validation path still rejects them instead of silently discarding them.
    """

    selected_exchange = str(exchange or "").strip().upper()
    selected_segment = str(segment or "").strip().upper()
    allowed_exchanges = _DHAN_RAW_SEGMENT_EXCHANGES.get(selected_segment)
    return bool(
        selected_exchange
        and allowed_exchanges is not None
        and selected_exchange not in allowed_exchanges
    )


def dhan_api_segment(*, exchange: str, segment: str, instrument: str) -> str:
    """Map current Dhan master ``E/D/C/M`` values to API/feed segments.

    Detailed masters describe the exchange and raw segment separately, while all
    quote, chain, history, and feed APIs require the combined Annexure enum.  Index
    rows are an exception: their API segment is the exchange-agnostic ``IDX_I``.
    Already-normalized values are accepted so archived fixtures remain readable.
    Unknown combinations fail closed rather than reaching a different market.
    """

    selected_exchange = str(exchange).strip().upper()
    selected_segment = str(segment).strip().upper()
    selected_instrument = str(instrument).strip().upper()
    if selected_segment in DHAN_API_SEGMENTS:
        return selected_segment
    if selected_instrument == "INDEX":
        if selected_exchange not in {"NSE", "BSE"}:
            raise NormalizationError(
                f"Dhan INDEX cannot be mapped for exchange {selected_exchange!r}"
            )
        return "IDX_I"
    mapping = {
        ("NSE", "E"): "NSE_EQ",
        ("NSE", "D"): "NSE_FNO",
        ("NSE", "C"): "NSE_CURRENCY",
        ("BSE", "E"): "BSE_EQ",
        ("BSE", "D"): "BSE_FNO",
        ("BSE", "C"): "BSE_CURRENCY",
        ("MCX", "M"): "MCX_COMM",
    }
    try:
        return mapping[(selected_exchange, selected_segment)]
    except KeyError as exc:
        raise NormalizationError(
            "unsupported Dhan exchange/segment combination: "
            f"{selected_exchange!r}/{selected_segment!r}"
        ) from exc


def decimal_value(value: Any, *, field: str, allow_none: bool = False) -> Decimal | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise NormalizationError(f"{field} is required")
    if isinstance(value, bool):
        raise NormalizationError(f"{field} cannot be boolean")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise NormalizationError(f"{field} is not a valid decimal") from exc
    if not result.is_finite():
        raise NormalizationError(f"{field} must be finite")
    return result


def integer_value(value: Any, *, field: str, allow_none: bool = False) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise NormalizationError(f"{field} is required")
    if isinstance(value, bool):
        raise NormalizationError(f"{field} cannot be boolean")
    try:
        decimal = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise NormalizationError(f"{field} is not an integer") from exc
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        raise NormalizationError(f"{field} is not an integer")
    return int(decimal)


def float_value(value: Any, *, field: str, allow_none: bool = False) -> float | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise NormalizationError(f"{field} is required")
    if isinstance(value, bool):
        raise NormalizationError(f"{field} cannot be boolean")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"{field} is not numeric") from exc
    if not isfinite(result):
        raise NormalizationError(f"{field} must be finite")
    return result


def dhan_iv_decimal(value: Any, *, field: str) -> float | None:
    """Dhan option-chain IV is documented as percentage points (e.g. 18 -> 0.18)."""

    raw = float_value(value, field=field, allow_none=True)
    if raw is None:
        return None
    if raw <= 0:
        return raw
    return raw / 100.0


def raw_payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()


def normalize_option_side(value: str) -> OptionType:
    normalized = value.strip().upper()
    if normalized in {"CE", "CALL", "C"}:
        return OptionType.CALL
    if normalized in {"PE", "PUT", "P"}:
        return OptionType.PUT
    raise NormalizationError(f"unsupported option side: {value!r}")


def normalize_dhan_option_chain(
    payload: Mapping[str, Any],
    *,
    contract: ContractSpec,
    observed_at: datetime,
    sequence: int,
    source: DataSource,
    previous_snapshot: PreviousOptionSnapshot | None = None,
    max_change_oi_interval: timedelta = DEFAULT_CHANGE_OI_MAX_INTERVAL,
) -> tuple[OptionQuote, ...]:
    """Normalize one Dhan chain and derive change OI only from safe provenance.

    Dhan's ``previous_oi`` is retained as a provider field, but it is not an
    intraday change value. Intraday change OI is derived only when a typed prior
    snapshot and its individual leg are both strictly earlier, recent, and from
    the exact same contract and instrument identity.
    """

    if max_change_oi_interval <= timedelta(0):
        raise NormalizationError("max_change_oi_interval must be positive")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise NormalizationError("Dhan option-chain data object is missing")
    chain = data.get("oc")
    if not isinstance(chain, Mapping):
        raise NormalizationError("Dhan option-chain strike map is missing")

    previous_by_key = _compatible_previous_quotes(
        previous_snapshot,
        contract=contract,
        sequence=sequence,
        source=source,
        observed_at=observed_at,
        max_interval=max_change_oi_interval,
    )
    contract_strikes = {
        record.strike: record.strike
        for record in contract.option_contracts
        if record.strike is not None
    }
    normalized: list[OptionQuote] = []
    for strike_text, sides in chain.items():
        if not isinstance(sides, Mapping):
            raise NormalizationError(f"Dhan strike {strike_text!r} is not an object")
        provider_strike = decimal_value(strike_text, field="data.oc.strike")
        assert provider_strike is not None
        # Dhan returns the entire chain.  A ContractSpec is intentionally bound to
        # exactly ATM-2..ATM+2; preserve the full raw payload in snapshot evidence
        # but normalize only records verified in that immutable master mapping.
        if provider_strike not in contract_strikes:
            continue
        # Preserve the exact Decimal representation from verified master data.
        # Numerically equal values such as 24800 and 24800.000000 otherwise
        # serialize to different TEXT keys in the immutable contract mapping.
        strike = contract_strikes.get(provider_strike, provider_strike)
        for provider_side, option_type in (("ce", OptionType.CALL), ("pe", OptionType.PUT)):
            leg = sides.get(provider_side)
            if leg is None:
                continue
            if not isinstance(leg, Mapping):
                raise NormalizationError(
                    f"Dhan {provider_side.upper()} leg at strike {strike} is not an object"
                )
            open_interest = integer_value(
                leg.get("oi"), field=f"{strike}.{provider_side}.oi", allow_none=True
            )
            previous_oi = integer_value(
                leg.get("previous_oi"),
                field=f"{strike}.{provider_side}.previous_oi",
                allow_none=True,
            )
            security_id_raw = leg.get("security_id")
            security_id = (
                "" if security_id_raw is None else str(security_id_raw).strip()
            )
            prior = previous_by_key.get((strike, option_type))
            change_oi: int | None = None
            change_oi_source_snapshot_id: str | None = None
            change_oi_interval_seconds: float | None = None
            if prior is not None:
                prior_quote, prior_interval = prior
                if (
                    security_id
                    and prior_quote.security_id == security_id
                    and prior_quote.expiry == contract.option_expiry
                    and prior_quote.strike == strike
                    and prior_quote.option_type is option_type
                    and prior_quote.open_interest is not None
                    and open_interest is not None
                ):
                    change_oi = open_interest - prior_quote.open_interest
                    assert previous_snapshot is not None
                    change_oi_source_snapshot_id = previous_snapshot.snapshot_id
                    change_oi_interval_seconds = prior_interval

            provider_greeks = leg.get("greeks") or {}
            if not isinstance(provider_greeks, Mapping):
                raise NormalizationError(
                    f"Dhan greeks at {strike} {provider_side.upper()} are malformed"
                )
            normalized.append(
                OptionQuote(
                    security_id=security_id,
                    strike=strike,
                    option_type=option_type,
                    expiry=contract.option_expiry,
                    bid=decimal_value(
                        leg.get("top_bid_price"),
                        field=f"{strike}.{provider_side}.bid",
                        allow_none=True,
                    ),
                    ask=decimal_value(
                        leg.get("top_ask_price"),
                        field=f"{strike}.{provider_side}.ask",
                        allow_none=True,
                    ),
                    ltp=decimal_value(
                        leg.get("last_price"),
                        field=f"{strike}.{provider_side}.ltp",
                        allow_none=True,
                    ),
                    volume=integer_value(
                        leg.get("volume"),
                        field=f"{strike}.{provider_side}.volume",
                        allow_none=True,
                    ),
                    open_interest=open_interest,
                    previous_open_interest=previous_oi,
                    change_open_interest=change_oi,
                    implied_volatility=dhan_iv_decimal(
                        leg.get("implied_volatility"),
                        field=f"{strike}.{provider_side}.iv",
                    ),
                    greeks=Greeks(
                        delta=float_value(
                            provider_greeks.get("delta"),
                            field=f"{strike}.{provider_side}.delta",
                            allow_none=True,
                        ),
                        gamma=float_value(
                            provider_greeks.get("gamma"),
                            field=f"{strike}.{provider_side}.gamma",
                            allow_none=True,
                        ),
                        theta=float_value(
                            provider_greeks.get("theta"),
                            field=f"{strike}.{provider_side}.theta",
                            allow_none=True,
                        ),
                        vega=float_value(
                            provider_greeks.get("vega"),
                            field=f"{strike}.{provider_side}.vega",
                            allow_none=True,
                        ),
                    ),
                    observed_at=observed_at,
                    bid_quantity=integer_value(
                        leg.get("top_bid_quantity"),
                        field=f"{strike}.{provider_side}.bid_quantity",
                        allow_none=True,
                    ),
                    ask_quantity=integer_value(
                        leg.get("top_ask_quantity"),
                        field=f"{strike}.{provider_side}.ask_quantity",
                        allow_none=True,
                    ),
                    previous_close=decimal_value(
                        leg.get("previous_close_price"),
                        field=f"{strike}.{provider_side}.previous_close",
                        allow_none=True,
                    ),
                    change_oi_source_snapshot_id=change_oi_source_snapshot_id,
                    change_oi_interval_seconds=change_oi_interval_seconds,
                )
            )
    return tuple(sorted(normalized, key=lambda quote: (quote.strike, quote.option_type.value)))


def _compatible_previous_quotes(
    previous_snapshot: PreviousOptionSnapshot | None,
    *,
    contract: ContractSpec,
    sequence: int,
    source: DataSource,
    observed_at: datetime,
    max_interval: timedelta,
) -> dict[tuple[Decimal, OptionType], tuple[OptionQuote, float]]:
    """Return unambiguous prior legs with their exact observation interval."""

    if (
        previous_snapshot is None
        or not previous_snapshot.snapshot_id.strip()
        or source is not DataSource.DHAN_REST
        or previous_snapshot.source is not DataSource.DHAN_REST
        or previous_snapshot.contract_key != contract.contract_key
        or previous_snapshot.sequence >= sequence
        or _strict_interval_seconds(
            observed_at,
            previous_snapshot.source_timestamp,
            max_interval=max_interval,
        )
        is None
    ):
        return {}

    compatible: dict[tuple[Decimal, OptionType], tuple[OptionQuote, float]] = {}
    ambiguous: set[tuple[Decimal, OptionType]] = set()
    for quote in previous_snapshot.option_chain:
        interval = _strict_interval_seconds(
            observed_at,
            quote.observed_at,
            max_interval=max_interval,
        )
        if interval is None or quote.key in ambiguous:
            continue
        if quote.key in compatible:
            compatible.pop(quote.key)
            ambiguous.add(quote.key)
            continue
        compatible[quote.key] = (quote, interval)
    return compatible


def _strict_interval_seconds(
    current: datetime,
    previous: datetime,
    *,
    max_interval: timedelta,
) -> float | None:
    """Return a positive bounded interval; incompatible timestamps fail closed."""

    if (
        current.tzinfo is None
        or current.utcoffset() is None
        or previous.tzinfo is None
        or previous.utcoffset() is None
    ):
        return None
    try:
        seconds = (current - previous).total_seconds()
    except (OverflowError, TypeError):
        return None
    if seconds <= 0 or seconds > max_interval.total_seconds():
        return None
    return seconds


@dataclass(frozen=True, slots=True)
class NormalizedMarketQuote:
    segment: str
    security_id: str
    observed_at: datetime
    received_at: datetime
    timestamp_warning: str | None
    last_price: Decimal | None
    day_open: Decimal | None
    day_high: Decimal | None
    day_low: Decimal | None
    previous_close: Decimal | None
    volume: int | None
    open_interest: int | None
    best_bid: Decimal | None
    best_ask: Decimal | None


def normalize_dhan_market_quote(
    payload: Mapping[str, Any],
    *,
    segment: str,
    security_id: str,
    observed_at: datetime,
    maximum_trade_age: timedelta = DEFAULT_DHAN_QUOTE_MAX_AGE,
    maximum_future_skew: timedelta = DEFAULT_DHAN_QUOTE_FUTURE_SKEW,
) -> NormalizedMarketQuote:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise NormalizationError("Dhan quote HTTP receipt time must be timezone-aware")
    if maximum_trade_age <= timedelta(0):
        raise NormalizationError("Dhan quote maximum trade age must be positive")
    if maximum_future_skew < timedelta(0):
        raise NormalizationError("Dhan quote future skew cannot be negative")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise NormalizationError("Dhan market-quote data object is missing")
    segment_data = data.get(segment)
    if not isinstance(segment_data, Mapping):
        raise NormalizationError(f"Dhan market-quote segment {segment!r} is missing")
    quote = segment_data.get(str(security_id))
    if quote is None:
        quote = segment_data.get(int(security_id)) if str(security_id).isdigit() else None
    if not isinstance(quote, Mapping):
        raise NormalizationError(f"Dhan quote for security {security_id!r} is missing")

    provider_observed_at, timestamp_warning = _dhan_quote_timestamp(
        quote,
        received_at=observed_at,
        maximum_trade_age=maximum_trade_age,
        maximum_future_skew=maximum_future_skew,
    )

    ohlc = quote.get("ohlc") or {}
    depth = quote.get("depth") or {}
    if not isinstance(ohlc, Mapping) or not isinstance(depth, Mapping):
        raise NormalizationError("Dhan quote OHLC/depth objects are malformed")
    buy_depth = depth.get("buy") or []
    sell_depth = depth.get("sell") or []
    best_bid_raw = buy_depth[0].get("price") if buy_depth and isinstance(buy_depth[0], Mapping) else None
    best_ask_raw = (
        sell_depth[0].get("price") if sell_depth and isinstance(sell_depth[0], Mapping) else None
    )
    return NormalizedMarketQuote(
        segment=segment.upper(),
        security_id=str(security_id),
        observed_at=provider_observed_at,
        received_at=observed_at,
        timestamp_warning=timestamp_warning,
        last_price=decimal_value(
            quote.get("last_price"), field="quote.last_price", allow_none=True
        ),
        day_open=decimal_value(ohlc.get("open"), field="quote.ohlc.open", allow_none=True),
        day_high=decimal_value(ohlc.get("high"), field="quote.ohlc.high", allow_none=True),
        day_low=decimal_value(ohlc.get("low"), field="quote.ohlc.low", allow_none=True),
        previous_close=decimal_value(
            ohlc.get("close"), field="quote.ohlc.close", allow_none=True
        ),
        volume=integer_value(quote.get("volume"), field="quote.volume", allow_none=True),
        open_interest=integer_value(quote.get("oi"), field="quote.oi", allow_none=True),
        best_bid=decimal_value(best_bid_raw, field="quote.best_bid", allow_none=True),
        best_ask=decimal_value(best_ask_raw, field="quote.best_ask", allow_none=True),
    )


def _dhan_quote_timestamp(
    quote: Mapping[str, Any],
    *,
    received_at: datetime,
    maximum_trade_age: timedelta,
    maximum_future_skew: timedelta,
) -> tuple[datetime, str | None]:
    if "last_trade_time" not in quote:
        return received_at, DHAN_QUOTE_RECEIPT_FALLBACK_WARNING
    raw = quote.get("last_trade_time")
    if not isinstance(raw, str) or not raw.strip():
        raise NormalizationError("Dhan quote last_trade_time is malformed")
    selected = raw.strip()
    try:
        parsed = datetime.strptime(selected, "%d/%m/%Y %H:%M:%S").replace(tzinfo=_IST)
    except ValueError as exc:
        raise NormalizationError(
            "Dhan quote last_trade_time must use DD/MM/YYYY HH:MM:SS IST"
        ) from exc
    if parsed.strftime("%d/%m/%Y %H:%M:%S") != selected:
        raise NormalizationError(
            "Dhan quote last_trade_time must use DD/MM/YYYY HH:MM:SS IST"
        )
    # Dhan documents this field as LAST TRADE time. It is not the observation
    # timestamp of the current REST quote response, so age/future-skew checks on LTT
    # can reject valid current quotes for illiquid or provider-clock-shifted contracts.
    # Keep strict provider-format validation, but use authenticated HTTP receipt time
    # as the observation timestamp for the current quote snapshot.
    del parsed, maximum_trade_age, maximum_future_skew
    return received_at, None


@dataclass(frozen=True, slots=True)
class NormalizedInstrumentRecord:
    exchange: str
    segment: str
    security_id: str
    symbol: str
    display_name: str
    instrument: str
    instrument_type: str
    underlying_security_id: str | None
    underlying_symbol: str | None
    expiry: str | None
    strike: Decimal | None
    option_type: OptionType | None
    lot_size: int | None
    tick_size: Decimal | None


def normalize_dhan_instrument_master(csv_text: str) -> tuple[NormalizedInstrumentRecord, ...]:
    """Parse either current detailed or compact Dhan instrument-master CSV columns."""

    reader = csv.DictReader(StringIO(csv_text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise NormalizationError("instrument master has no header")

    def first(row: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in row and row[name] not in (None, ""):
                return row[name]
        return None

    records: list[NormalizedInstrumentRecord] = []
    for row_number, row in enumerate(reader, start=2):
        exchange = first(row, "EXCH_ID", "SEM_EXM_EXCH_ID")
        segment = first(row, "SEGMENT", "SEM_SEGMENT")
        if _is_known_unsupported_dhan_master_route(
            exchange=exchange,
            segment=segment,
        ):
            continue

        security_id = first(row, "SECURITY_ID", "SEM_SMST_SECURITY_ID")
        symbol = first(row, "SYMBOL_NAME", "SM_SYMBOL_NAME", "SEM_TRADING_SYMBOL")
        if not all((security_id, exchange, segment, symbol)):
            raise NormalizationError(
                f"instrument master row {row_number} lacks identity fields"
            )
        option_raw = first(row, "OPTION_TYPE", "SEM_OPTION_TYPE")
        option_type = None
        if option_raw and str(option_raw).strip().upper() not in {"NA", "XX", "-"}:
            option_type = normalize_option_side(str(option_raw))
        instrument = str(
            first(row, "INSTRUMENT", "SEM_INSTRUMENT_NAME") or ""
        ).strip().upper()
        records.append(
            NormalizedInstrumentRecord(
                exchange=str(exchange).strip().upper(),
                segment=dhan_api_segment(
                    exchange=str(exchange),
                    segment=str(segment),
                    instrument=instrument,
                ),
                security_id=str(security_id).strip(),
                symbol=str(symbol).strip().upper(),
                display_name=str(
                    first(row, "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL") or symbol
                ).strip(),
                instrument=instrument,
                instrument_type=str(
                    first(row, "INSTRUMENT_TYPE", "SEM_EXCH_INSTRUMENT_TYPE") or ""
                ).strip().upper(),
                underlying_security_id=(
                    str(first(row, "UNDERLYING_SECURITY_ID")).strip()
                    if first(row, "UNDERLYING_SECURITY_ID") is not None
                    else None
                ),
                underlying_symbol=(
                    str(first(row, "UNDERLYING_SYMBOL")).strip().upper()
                    if first(row, "UNDERLYING_SYMBOL") is not None
                    else None
                ),
                expiry=(
                    str(
                        first(
                            row,
                            "EXPIRY_DATE",
                            "SM_EXPIRY_DATE",
                            "SEM_EXPIRY_DATE",
                        )
                    ).strip()
                    if first(
                        row,
                        "EXPIRY_DATE",
                        "SM_EXPIRY_DATE",
                        "SEM_EXPIRY_DATE",
                    )
                    is not None
                    else None
                ),
                strike=decimal_value(
                    first(row, "STRIKE_PRICE", "SEM_STRIKE_PRICE"),
                    field=f"instrument[{row_number}].strike",
                    allow_none=True,
                ),
                option_type=option_type,
                lot_size=integer_value(
                    first(row, "LOT_SIZE", "SEM_LOT_UNITS"),
                    field=f"instrument[{row_number}].lot_size",
                    allow_none=True,
                ),
                tick_size=decimal_value(
                    first(row, "TICK_SIZE", "SEM_TICK_SIZE"),
                    field=f"instrument[{row_number}].tick_size",
                    allow_none=True,
                ),
            )
        )
    return tuple(records)


def build_dhan_master_batch(
    csv_text: str,
    *,
    fetched_at: datetime,
    source_url: str,
    schema_version: str = "dhan-detailed-v1",
) -> tuple[InstrumentMasterProvenance, tuple[NormalizedInstrumentRecord, ...]]:
    """Content-address one Dhan master without inventing exchange expiry times."""

    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise NormalizationError("instrument-master fetched_at must be timezone-aware")
    if not source_url.strip() or not schema_version.strip():
        raise NormalizationError("instrument-master source and schema version are required")
    records = normalize_dhan_instrument_master(csv_text)
    digest = sha256(csv_text.lstrip("\ufeff").encode("utf-8")).hexdigest()
    # A content hash identifies the payload, while a batch identifies one exact
    # retrieval attestation.  Dhan may legitimately publish an unchanged file for
    # longer than our maximum age; folding fetched_at into the identity permits a
    # fresh, immutable re-attestation without rewriting the earlier provenance.
    attestation_material = "\x00".join(
        (
            "DHAN",
            digest,
            source_url.strip(),
            schema_version.strip(),
            fetched_at.astimezone(UTC).isoformat(),
        )
    )
    attestation_hash = sha256(attestation_material.encode("utf-8")).hexdigest()
    provenance = InstrumentMasterProvenance(
        batch_id=f"DHAN:{digest[:16]}:{attestation_hash[:16]}",
        provider="DHAN",
        source_url=source_url,
        content_hash=digest,
        schema_version=schema_version,
        fetched_at=fetched_at,
        row_count=len(records),
    )
    return provenance, records


def materialize_master_records(
    records: Iterable[NormalizedInstrumentRecord],
    *,
    expiry_resolver: Callable[[NormalizedInstrumentRecord], datetime | None],
) -> tuple[InstrumentMasterRecord, ...]:
    """Resolve exchange-calendar expiry instants before master records are persisted."""

    materialized: list[InstrumentMasterRecord] = []
    for record in records:
        resolved_expiry = expiry_resolver(record)
        if record.expiry is None:
            if resolved_expiry is not None:
                raise NormalizationError(
                    f"expiry resolver added an expiry to {record.security_id}"
                )
        else:
            if (
                resolved_expiry is None
                or resolved_expiry.tzinfo is None
                or resolved_expiry.utcoffset() is None
            ):
                raise NormalizationError(
                    f"expiry resolver must return an aware instant for {record.security_id}"
                )
            try:
                provider_date = date.fromisoformat(record.expiry.strip()[:10])
            except ValueError as exc:
                raise NormalizationError(
                    f"instrument {record.security_id} has an invalid expiry date"
                ) from exc
            if resolved_expiry.date() != provider_date:
                raise NormalizationError(
                    f"resolved expiry date differs for {record.security_id}"
                )
        try:
            exchange = Exchange(record.exchange)
        except ValueError as exc:
            raise NormalizationError(
                f"unsupported exchange {record.exchange!r} for {record.security_id}"
            ) from exc
        materialized.append(
            InstrumentMasterRecord(
                instrument=InstrumentId(
                    exchange=exchange,
                    segment=record.segment,
                    security_id=record.security_id,
                    symbol=record.symbol,
                ),
                display_name=record.display_name,
                instrument_type=record.instrument_type,
                underlying_security_id=record.underlying_security_id,
                expiry=resolved_expiry,
                strike=record.strike,
                option_type=record.option_type,
                lot_size=record.lot_size,
                tick_size=record.tick_size,
            )
        )
    return tuple(materialized)
