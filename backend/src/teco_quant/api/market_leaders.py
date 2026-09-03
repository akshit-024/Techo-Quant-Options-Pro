"""Thread-safe live market-leader read model built from Dhan quote snapshots.

The option workspace ranks strikes for one selected underlying. This module serves a
different question: which configured underlyings are leading a selected market group?
The ranking is deliberately simple and auditable -- descending session percentage
change -- and never promotes stale observations as live.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from threading import RLock
from typing import Any, Protocol

from teco_quant.ingestion.normalization import (
    NormalizedInstrumentRecord,
    normalize_dhan_instrument_master,
)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
RANKING_BASIS = "DAY_CHANGE_PERCENT_DESC"
MARKET_IDS = ("NIFTY", "BANKNIFTY", "SENSEX", "STOCK_FNO", "MCX")

# These are transparent scanner universes, not claims that Dhan supplies index
# membership. They contain liquid derivative-capable names and can be versioned when
# an operator wants a different basket. Overlaps are fetched only once.
LEADER_UNIVERSES: Mapping[str, tuple[tuple[str, str], ...]] = {
    "NIFTY": (
        ("RELIANCE", "Reliance Industries"),
        ("HDFCBANK", "HDFC Bank"),
        ("ICICIBANK", "ICICI Bank"),
        ("BHARTIARTL", "Bharti Airtel"),
        ("INFY", "Infosys"),
        ("TCS", "Tata Consultancy Services"),
        ("LT", "Larsen & Toubro"),
        ("ITC", "ITC"),
        ("SBIN", "State Bank of India"),
        ("AXISBANK", "Axis Bank"),
    ),
    "BANKNIFTY": (
        ("HDFCBANK", "HDFC Bank"),
        ("ICICIBANK", "ICICI Bank"),
        ("SBIN", "State Bank of India"),
        ("AXISBANK", "Axis Bank"),
        ("KOTAKBANK", "Kotak Mahindra Bank"),
        ("BANKBARODA", "Bank of Baroda"),
        ("INDUSINDBK", "IndusInd Bank"),
        ("FEDERALBNK", "Federal Bank"),
        ("IDFCFIRSTB", "IDFC First Bank"),
        ("AUBANK", "AU Small Finance Bank"),
    ),
    "SENSEX": (
        ("RELIANCE", "Reliance Industries"),
        ("HDFCBANK", "HDFC Bank"),
        ("ICICIBANK", "ICICI Bank"),
        ("BHARTIARTL", "Bharti Airtel"),
        ("INFY", "Infosys"),
        ("TCS", "Tata Consultancy Services"),
        ("LT", "Larsen & Toubro"),
        ("ITC", "ITC"),
        ("SBIN", "State Bank of India"),
        ("AXISBANK", "Axis Bank"),
    ),
    "STOCK_FNO": (
        ("RELIANCE", "Reliance Industries"),
        ("TCS", "Tata Consultancy Services"),
        ("INFY", "Infosys"),
        ("HDFCBANK", "HDFC Bank"),
        ("ICICIBANK", "ICICI Bank"),
        ("SBIN", "State Bank of India"),
        ("AXISBANK", "Axis Bank"),
        ("KOTAKBANK", "Kotak Mahindra Bank"),
        ("BHARTIARTL", "Bharti Airtel"),
        ("LT", "Larsen & Toubro"),
        ("ITC", "ITC"),
        ("BAJFINANCE", "Bajaj Finance"),
    ),
    "MCX": (
        ("GOLD", "Gold"),
        ("SILVER", "Silver"),
        ("CRUDEOIL", "Crude Oil"),
        ("NATURALGAS", "Natural Gas"),
        ("COPPER", "Copper"),
        ("ZINC", "Zinc"),
        ("ALUMINIUM", "Aluminium"),
        ("LEAD", "Lead"),
    ),
}

_SESSION_HOURS: Mapping[str, tuple[time, time]] = {
    "NSE_EQ": (time(9, 15), time(15, 30)),
    "MCX_COMM": (time(9), time(23, 30)),
}


class MarketLeaderReader(Protocol):
    """Credential-free API surface for a market-leader read model."""

    def leaders(self, market_id: str) -> dict[str, object]: ...


class MarketLeaderPublisher(Protocol):
    """Acquisition-side surface used to refresh the leader read model."""

    def replace_instrument_master(
        self,
        csv_text: str,
        *,
        as_of: datetime,
    ) -> None: ...

    def quote_request(self) -> dict[str, list[str]]: ...

    def publish_dhan_quote(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LeaderInstrument:
    symbol: str
    display_name: str
    segment: str
    security_id: str


@dataclass(frozen=True, slots=True)
class LeaderObservation:
    instrument: LeaderInstrument
    last_price: Decimal
    previous_close: Decimal
    change: Decimal
    change_percent: Decimal
    volume: int | None
    observed_at: datetime
    provider_timestamp: bool


class MarketLeaderStore:
    """Resolve configured instruments and expose credential-free ranked snapshots."""

    def __init__(
        self,
        *,
        maximum_age_seconds: float = 30.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isfinite(maximum_age_seconds) or maximum_age_seconds <= 0:
            raise ValueError(
                "maximum_age_seconds must be finite and positive"
            )

        self._maximum_age = float(maximum_age_seconds)
        self._clock = clock
        self._lock = RLock()

        self._instruments: dict[
            str,
            tuple[LeaderInstrument, ...],
        ] = {
            market_id: ()
            for market_id in MARKET_IDS
        }

        self._missing: dict[
            str,
            tuple[str, ...],
        ] = {
            market_id: tuple(
                symbol
                for symbol, _ in LEADER_UNIVERSES[market_id]
            )
            for market_id in MARKET_IDS
        }

        self._observations: dict[
            tuple[str, str],
            LeaderObservation,
        ] = {}

        self._generated_at: datetime | None = None

    def replace_instrument_master(
        self,
        csv_text: str,
        *,
        as_of: datetime,
    ) -> None:
        _aware(
            as_of,
            "leader catalog as_of",
        )

        records = normalize_dhan_instrument_master(
            csv_text
        )

        resolved, missing = resolve_leader_instruments(
            records,
            as_of=as_of,
        )

        valid_keys = {
            (
                instrument.segment,
                instrument.security_id,
            )
            for instruments in resolved.values()
            for instrument in instruments
        }

        with self._lock:
            self._instruments = dict(resolved)
            self._missing = dict(missing)

            self._observations = {
                key: value
                for key, value in self._observations.items()
                if key in valid_keys
            }

    def quote_request(
        self,
    ) -> dict[str, list[str]]:
        with self._lock:
            instruments = tuple(
                instrument
                for market in self._instruments.values()
                for instrument in market
            )

        result: dict[str, list[str]] = {}
        seen: set[tuple[str, str]] = set()

        for instrument in instruments:
            key = (
                instrument.segment,
                instrument.security_id,
            )

            if key in seen:
                continue

            seen.add(key)

            result.setdefault(
                instrument.segment,
                [],
            ).append(
                instrument.security_id
            )

        return result

    def publish_dhan_quote(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: datetime,
    ) -> None:
        _aware(
            received_at,
            "leader quote received_at",
        )

        data = payload.get("data")

        # The Dhan response must expose "data" as a mapping.
        # If it is present with the wrong type, this is a type
        # contract violation rather than a bad scalar value.
        if not isinstance(data, Mapping):
            raise TypeError(
                "Dhan leader quote data object is missing"
            )

        with self._lock:
            instruments = tuple(
                {
                    (
                        item.segment,
                        item.security_id,
                    ): item
                    for values in self._instruments.values()
                    for item in values
                }.values()
            )

        observations: dict[
            tuple[str, str],
            LeaderObservation,
        ] = {}

        for instrument in instruments:
            segment = data.get(
                instrument.segment
            )

            if not isinstance(
                segment,
                Mapping,
            ):
                continue

            raw = segment.get(
                instrument.security_id
            )

            if raw is None:
                try:
                    raw = segment.get(
                        int(
                            instrument.security_id
                        )
                    )
                except ValueError:
                    raw = None

            if not isinstance(
                raw,
                Mapping,
            ):
                continue

            observation = _observation(
                instrument,
                raw,
                received_at,
            )

            if observation is not None:
                observations[
                    (
                        instrument.segment,
                        instrument.security_id,
                    )
                ] = observation

        # A provider response can legally omit one instrument temporarily. Preserve
        # its last accepted observation so callers can see it as STALE rather than
        # converting a partial quote response into a global disappearance.
        if observations:
            with self._lock:
                valid_keys = {
                    (item.segment, item.security_id)
                    for values in self._instruments.values()
                    for item in values
                }
                retained = {
                    key: value
                    for key, value in self._observations.items()
                    if key in valid_keys
                }
                retained.update(observations)
                self._observations = retained
                self._generated_at = received_at.astimezone(UTC)

    def leaders(
        self,
        market_id: str,
    ) -> dict[str, object]:
        selected = (
            str(market_id)
            .strip()
            .upper()
        )

        if selected not in MARKET_IDS:
            raise ValueError(
                "unsupported market leader group"
            )

        now = self._clock()

        _aware(
            now,
            "market leader clock",
        )

        with self._lock:
            instruments = self._instruments[
                selected
            ]

            missing = self._missing[
                selected
            ]

            generated_at = self._generated_at

            observations = tuple(
                self._observations.get(
                    (
                        item.segment,
                        item.security_id,
                    )
                )
                for item in instruments
            )

        present = tuple(
            item
            for item in observations
            if item is not None
        )

        ranked = sorted(
            present,
            key=lambda item: (
                (
                    0
                    if _is_live(
                        item,
                        now,
                        self._maximum_age,
                    )
                    else 1
                ),
                -item.change_percent,
                -(item.volume or 0),
                item.instrument.symbol,
            ),
        )

        serialized = [
            _serialize_observation(
                index,
                item,
                now,
                self._maximum_age,
            )
            for index, item in enumerate(
                ranked,
                start=1,
            )
        ]

        live_count = sum(
            item["data_mode"] == "LIVE"
            for item in serialized
        )

        required_live = min(
            5,
            len(LEADER_UNIVERSES[selected]),
        )

        if (
            required_live > 0
            and live_count >= required_live
        ):
            market_state = "LIVE"

        elif serialized:
            market_state = "STALE"

        else:
            market_state = "UNAVAILABLE"

        return {
            "market_id": selected,
            "generated_at": (
                None
                if generated_at is None
                else generated_at.isoformat()
            ),
            "ranking_basis": RANKING_BASIS,
            "market_state": market_state,
            "universe_size": len(
                LEADER_UNIVERSES[selected]
            ),
            "available_count": len(
                serialized
            ),
            "missing_symbols": list(
                missing
            ),
            "leaders": serialized,
        }


def resolve_leader_instruments(
    records: Sequence[NormalizedInstrumentRecord],
    *,
    as_of: datetime,
) -> tuple[
    Mapping[str, tuple[LeaderInstrument, ...]],
    Mapping[str, tuple[str, ...]],
]:
    """Resolve equity spot IDs and nearest commodity futures from one Dhan master."""

    _aware(as_of, "leader catalog as_of")

    # ---------------------------------------------------------
    # NSE / STOCK F&O
    # ---------------------------------------------------------
    equity_targets = {
        ticker
        for market_id in MARKET_IDS
        if market_id != "MCX"
        for ticker, _ in LEADER_UNIVERSES[market_id]
    }

    derivative_ids: dict[str, set[str]] = {
        ticker: set()
        for ticker in equity_targets
    }

    for record in records:
        if (
            record.segment != "NSE_FNO"
            or "FUTSTK" not in {record.instrument, record.instrument_type}
            or not record.underlying_security_id
            or not _active(record, as_of)
        ):
            continue

        matched_ticker = _matching_ticker(
            record,
            equity_targets,
        )

        if matched_ticker is not None:
            derivative_ids[matched_ticker].add(
                record.underlying_security_id
            )

    equity_by_id = {
        record.security_id: record
        for record in records
        if (
            record.segment == "NSE_EQ"
            and "EQUITY" in {record.instrument, record.instrument_type}
        )
    }

    equity: dict[str, LeaderInstrument] = {}

    display_by_symbol = {
        ticker: display_name
        for market_id in MARKET_IDS
        for ticker, display_name in LEADER_UNIVERSES[market_id]
    }

    for ticker, underlying_ids in derivative_ids.items():
        if len(underlying_ids) != 1:
            continue

        security_id = next(iter(underlying_ids))

        if security_id not in equity_by_id:
            continue

        equity[ticker] = LeaderInstrument(
            symbol=ticker,
            display_name=display_by_symbol[ticker],
            segment="NSE_EQ",
            security_id=security_id,
        )

    # ---------------------------------------------------------
    # MCX
    # ---------------------------------------------------------
    mcx_targets = {
        ticker
        for ticker, _ in LEADER_UNIVERSES["MCX"]
    }

    mcx_candidates: dict[
        str,
        list[tuple[datetime, NormalizedInstrumentRecord]],
    ] = {
        ticker: []
        for ticker in mcx_targets
    }

    for record in records:
        if (
            record.segment != "MCX_COMM"
            or "FUTCOM" not in {record.instrument, record.instrument_type}
            or not _active(record, as_of)
        ):
            continue

        mcx_symbol = _matching_ticker(
            record,
            mcx_targets,
        )

        expiry = _expiry(record)

        if mcx_symbol is not None and expiry is not None:
            mcx_candidates[mcx_symbol].append(
                (expiry, record)
            )

    mcx: dict[str, LeaderInstrument] = {}

    for mcx_symbol, candidate_records in mcx_candidates.items():
        if not candidate_records:
            continue

        _, selected_record = min(
            candidate_records,
            key=lambda item: (
                item[0],
                item[1].security_id,
            ),
        )

        mcx[mcx_symbol] = LeaderInstrument(
            symbol=mcx_symbol,
            display_name=display_by_symbol[mcx_symbol],
            segment="MCX_COMM",
            security_id=selected_record.security_id,
        )

    # ---------------------------------------------------------
    # Final resolved universes
    # ---------------------------------------------------------
    resolved: dict[str, tuple[LeaderInstrument, ...]] = {}
    missing: dict[str, tuple[str, ...]] = {}

    for market_id in MARKET_IDS:
        source = mcx if market_id == "MCX" else equity

        resolved_instruments = tuple(
            source[ticker]
            for ticker, _ in LEADER_UNIVERSES[market_id]
            if ticker in source
        )

        missing_symbols = tuple(
            ticker
            for ticker, _ in LEADER_UNIVERSES[market_id]
            if ticker not in source
        )

        resolved[market_id] = resolved_instruments
        missing[market_id] = missing_symbols

    return resolved, missing

def _matching_ticker(
    record: NormalizedInstrumentRecord,
    targets: set[str],
) -> str | None:
    values = (
        record.underlying_symbol or "",
        record.symbol,
    )

    for target in targets:
        for value in values:
            selected = (
                value.strip().upper()
            )

            if (
                selected == target
                or selected.startswith(
                    f"{target}-"
                )
            ):
                return target

    return None


def _active(
    record: NormalizedInstrumentRecord,
    as_of: datetime,
) -> bool:
    expiry = _expiry(
        record
    )

    return (
        expiry is not None
        and expiry
        > as_of.astimezone(IST)
    )


def _expiry(
    record: NormalizedInstrumentRecord,
) -> datetime | None:
    if record.expiry is None:
        return None

    try:
        value = datetime.fromisoformat(
            record.expiry.strip()[:10]
        )

    except ValueError:
        return None

    close = (
        time(23, 30)
        if record.segment == "MCX_COMM"
        else time(15, 30)
    )

    return datetime.combine(
        value.date(),
        close,
        tzinfo=IST,
    )


def _observation(
    instrument: LeaderInstrument,
    raw: Mapping[str, Any],
    received_at: datetime,
) -> LeaderObservation | None:
    last_price = _positive_decimal(
        raw.get("last_price")
    )

    ohlc = raw.get("ohlc")

    previous_close = (
        _positive_decimal(
            ohlc.get("close")
        )
        if isinstance(
            ohlc,
            Mapping,
        )
        else None
    )

    if (
        last_price is None
        or previous_close is None
    ):
        return None

    change = (
        last_price
        - previous_close
    )

    change_percent = (
        change
        / previous_close
        * Decimal(100)
    )

    volume = _optional_nonnegative_int(
        raw.get("volume")
    )

    (
        observed_at,
        provider_timestamp,
    ) = _observed_at(
        raw.get("last_trade_time"),
        received_at,
    )

    return LeaderObservation(
        instrument=instrument,
        last_price=last_price,
        previous_close=previous_close,
        change=change,
        change_percent=change_percent,
        volume=volume,
        observed_at=observed_at,
        provider_timestamp=provider_timestamp,
    )


def _observed_at(
    value: Any,
    received_at: datetime,
) -> tuple[datetime, bool]:
    if (
        isinstance(value, str)
        and value.strip()
    ):
        try:
            return (
                datetime.strptime(
                    value.strip(),
                    "%d/%m/%Y %H:%M:%S",
                ).replace(
                    tzinfo=IST
                ),
                True,
            )

        except ValueError:
            pass

    return (
        received_at.astimezone(IST),
        False,
    )


def _is_live(
    observation: LeaderObservation,
    now: datetime,
    maximum_age: float,
) -> bool:
    local = now.astimezone(
        IST
    )

    session = _SESSION_HOURS.get(
        observation.instrument.segment
    )

    if (
        session is None
        or local.weekday() >= 5
    ):
        return False

    (
        session_open,
        session_close,
    ) = session

    if not (
        session_open
        <= local.time().replace(
            tzinfo=None
        )
        <= session_close
    ):
        return False

    age = (
        now.astimezone(UTC)
        - observation.observed_at.astimezone(
            UTC
        )
    ).total_seconds()

    return (
        observation.provider_timestamp
        and -2
        <= age
        <= maximum_age
    )


def _serialize_observation(
    rank: int,
    observation: LeaderObservation,
    now: datetime,
    maximum_age: float,
) -> dict[str, object]:
    return {
        "rank": rank,
        "symbol": (
            observation.instrument.symbol
        ),
        "display_name": (
            observation.instrument.display_name
        ),
        # Decimal strings retain provider precision across the JSON/JavaScript
        # boundary. Percentage is a display/ranking scalar and remains numeric.
        "last_price": str(observation.last_price),
        "previous_close": str(observation.previous_close),
        "change": str(observation.change),
        "change_percent": float(observation.change_percent),
        "volume": observation.volume,
        "observed_at": (
            observation.observed_at.isoformat()
        ),
        "data_mode": (
            "LIVE"
            if _is_live(
                observation,
                now,
                maximum_age,
            )
            else "STALE"
        ),
    }


def _positive_decimal(
    value: Any,
) -> Decimal | None:
    if (
        value is None
        or isinstance(
            value,
            bool,
        )
    ):
        return None

    try:
        selected = Decimal(
            str(value)
        )

    except (
        InvalidOperation,
        ValueError,
    ):
        return None

    return (
        selected
        if (
            selected.is_finite()
            and selected > 0
        )
        else None
    )


def _optional_nonnegative_int(
    value: Any,
) -> int | None:
    if (
        value is None
        or isinstance(
            value,
            bool,
        )
    ):
        return None

    try:
        selected = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    return (
        selected
        if selected >= 0
        else None
    )


def _aware(
    value: datetime,
    name: str,
) -> None:
    if (
        value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(
            f"{name} must be timezone-aware"
        )
