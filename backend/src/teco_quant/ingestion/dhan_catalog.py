"""Dhan instrument-master subset, expiry policy, and supported-universe resolver."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from itertools import pairwise

from teco_quant.domain.enums import Exchange, MarketKind, OptionType, PricingModel
from teco_quant.domain.models import (
    ContractSpec,
    InstrumentMasterProvenance,
    InstrumentMasterRecord,
)
from teco_quant.ingestion.normalization import (
    NormalizationError,
    NormalizedInstrumentRecord,
    materialize_master_records,
    normalize_dhan_instrument_master,
)
from teco_quant.serialization import content_hash
from teco_quant.strategy.spec import nearest_atm

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
SUPPORTED_SUBSET_SCHEMA = "dhan-detailed-v2-supported-universe-v1"


class DhanCatalogError(ValueError):
    """Raised when a master cannot prove one exact supported contract."""


@dataclass(frozen=True, slots=True)
class UniverseDefinition:
    symbol: str
    aliases: tuple[str, ...]
    exchange: Exchange
    market_kind: MarketKind
    underlying_segment: str
    derivative_segment: str
    underlying_instrument: str
    future_instrument: str
    option_instrument: str
    pricing_model: PricingModel


SUPPORTED_UNIVERSE: Mapping[str, UniverseDefinition] = {
    "NIFTY": UniverseDefinition(
        "NIFTY", ("NIFTY", "NIFTY 50"), Exchange.NSE, MarketKind.INDEX,
        "IDX_I", "NSE_FNO", "INDEX", "FUTIDX", "OPTIDX", PricingModel.BLACK_SCHOLES,
    ),
    "BANKNIFTY": UniverseDefinition(
        "BANKNIFTY", ("BANKNIFTY", "NIFTY BANK"), Exchange.NSE, MarketKind.INDEX,
        "IDX_I", "NSE_FNO", "INDEX", "FUTIDX", "OPTIDX", PricingModel.BLACK_SCHOLES,
    ),
    "SENSEX": UniverseDefinition(
        "SENSEX", ("SENSEX", "BSE SENSEX"), Exchange.BSE, MarketKind.INDEX,
        "IDX_I", "BSE_FNO", "INDEX", "FUTIDX", "OPTIDX", PricingModel.BLACK_SCHOLES,
    ),
    "RELIANCE": UniverseDefinition(
        "RELIANCE", (
            "RELIANCE",
            "RELIANCE INDUSTRIES",
            "RELIANCE INDUSTRIES LTD",
        ), Exchange.NSE, MarketKind.STOCK,
        "NSE_EQ", "NSE_FNO", "EQUITY", "FUTSTK", "OPTSTK", PricingModel.BLACK_SCHOLES,
    ),
    "TCS": UniverseDefinition(
        "TCS", (
            "TCS",
            "TATA CONSULTANCY SERVICES",
            "TATA CONSULTANCY SERVICES LTD",
        ), Exchange.NSE, MarketKind.STOCK,
        "NSE_EQ", "NSE_FNO", "EQUITY", "FUTSTK", "OPTSTK", PricingModel.BLACK_SCHOLES,
    ),
    "INFY": UniverseDefinition(
        "INFY", ("INFY", "INFOSYS"), Exchange.NSE, MarketKind.STOCK,
        "NSE_EQ", "NSE_FNO", "EQUITY", "FUTSTK", "OPTSTK", PricingModel.BLACK_SCHOLES,
    ),
    "GOLD": UniverseDefinition(
        "GOLD", ("GOLD",), Exchange.MCX, MarketKind.COMMODITY,
        "MCX_COMM", "MCX_COMM", "FUTCOM", "FUTCOM", "OPTFUT", PricingModel.BLACK_76,
    ),
    "CRUDEOIL": UniverseDefinition(
        "CRUDEOIL", ("CRUDEOIL", "CRUDE OIL"), Exchange.MCX, MarketKind.COMMODITY,
        "MCX_COMM", "MCX_COMM", "FUTCOM", "FUTCOM", "OPTFUT", PricingModel.BLACK_76,
    ),
    "SILVER": UniverseDefinition(
        "SILVER", ("SILVER",), Exchange.MCX, MarketKind.COMMODITY,
        "MCX_COMM", "MCX_COMM", "FUTCOM", "FUTCOM", "OPTFUT", PricingModel.BLACK_76,
    ),
}


@dataclass(frozen=True, slots=True)
class DhanCatalogBatch:
    provenance: InstrumentMasterProvenance
    normalized_records: tuple[NormalizedInstrumentRecord, ...]
    records: tuple[InstrumentMasterRecord, ...]
    source_content_hash: str


@dataclass(frozen=True, slots=True)
class DhanContractFamily:
    definition: UniverseDefinition
    provenance: InstrumentMasterProvenance
    underlying: InstrumentMasterRecord
    future: InstrumentMasterRecord
    option_expiry: datetime
    option_records: tuple[InstrumentMasterRecord, ...]
    option_chain_security_id: str
    option_chain_segment: str
    historical_instrument: str

    def contract_at(self, pricing_underlying: Decimal) -> ResolvedDhanContract:
        if not isinstance(pricing_underlying, Decimal):
            pricing_underlying = Decimal(str(pricing_underlying))
        if not pricing_underlying.is_finite() or pricing_underlying <= 0:
            raise DhanCatalogError("pricing underlying must be a positive finite Decimal")
        selected, interval = _select_five_strikes(self.option_records, pricing_underlying)

        # ContractSpec represents the selected option contract family. The option
        # legs therefore define its executable lot size and tick size. A covering
        # futures contract may legitimately have a different tick size and keeps
        # that specification on its own InstrumentMasterRecord.
        lot_sizes = {record.lot_size for record in selected}
        tick_sizes = {record.tick_size for record in selected}

        if len(lot_sizes) != 1 or None in lot_sizes:
            raise DhanCatalogError(
                "selected option contracts have inconsistent lot sizes"
            )
        if len(tick_sizes) != 1 or None in tick_sizes:
            raise DhanCatalogError(
                "selected option contracts have inconsistent tick sizes"
            )

        lot_size = next(iter(lot_sizes))
        tick_size = next(iter(tick_sizes))
        assert lot_size is not None
        assert tick_size is not None
        # ContractSpec is an immutable provider-verified identity and must preserve
        # the exact underlying record from the Dhan instrument master. Presentation/
        # selection continues to use ResolvedDhanContract.symbol below.
        contract_underlying = self.underlying.instrument
        contract = ContractSpec(
            underlying=contract_underlying,
            market_kind=self.definition.market_kind,
            pricing_model=self.definition.pricing_model,
            option_expiry=self.option_expiry,
            lot_size=lot_size,
            strike_interval=interval,
            tick_size=tick_size,
            master=self.provenance,
            option_contracts=selected,
            futures=self.future,
        )
        subscription_candidates = [
            (self.future.instrument.segment, self.future.instrument.security_id),
            *(
                (record.instrument.segment, record.instrument.security_id)
                for record in selected
            ),
        ]
        if self.definition.market_kind is not MarketKind.INDEX:
            subscription_candidates.insert(
                0,
                (
                    self.underlying.instrument.segment,
                    self.underlying.instrument.security_id,
                ),
            )
        subscriptions = _unique_subscriptions(tuple(subscription_candidates))
        return ResolvedDhanContract(
            symbol=self.definition.symbol,
            contract=contract,
            option_chain_security_id=self.option_chain_security_id,
            option_chain_segment=self.option_chain_segment,
            historical_security_id=self.underlying.instrument.security_id,
            historical_segment=self.underlying.instrument.segment,
            historical_instrument=self.historical_instrument,
            subscriptions=subscriptions,
        )


@dataclass(frozen=True, slots=True)
class ResolvedDhanContract:
    symbol: str
    contract: ContractSpec
    option_chain_security_id: str
    option_chain_segment: str
    historical_security_id: str
    historical_segment: str
    historical_instrument: str
    subscriptions: tuple[tuple[str, str], ...]

    @property
    def execution_registry(self) -> Mapping[str, str]:
        return {
            record.instrument.symbol: record.instrument.security_id
            for record in self.contract.option_contracts
        }


class DhanInstrumentCatalog:
    """Resolve supported symbols only from one immutable verified master subset."""

    def __init__(self, batch: DhanCatalogBatch) -> None:
        if batch.provenance.row_count != len(batch.records):
            raise DhanCatalogError("catalog provenance does not cover its complete record set")
        if len(batch.normalized_records) != len(batch.records):
            raise DhanCatalogError("normalized and materialized master lengths differ")
        keys: set[tuple[str, str, str]] = set()
        entries: list[tuple[NormalizedInstrumentRecord, InstrumentMasterRecord]] = []
        for normalized, materialized in zip(batch.normalized_records, batch.records):
            key = (normalized.exchange, normalized.segment, normalized.security_id)
            if key in keys:
                raise DhanCatalogError(f"duplicate master identity: {key!r}")
            if materialized.instrument.canonical_key != (
                f"{materialized.instrument.exchange}:{normalized.segment}:{normalized.security_id}"
            ):
                raise DhanCatalogError("normalized/materialized master identity mismatch")
            keys.add(key)
            entries.append((normalized, materialized))
        self._batch = batch
        self._entries = tuple(entries)

    @property
    def provenance(self) -> InstrumentMasterProvenance:
        return self._batch.provenance

    @property
    def records(self) -> tuple[InstrumentMasterRecord, ...]:
        return self._batch.records

    def family(
        self,
        symbol: str,
        *,
        as_of: datetime,
        broker_expiry_dates: Iterable[date] | None = None,
    ) -> DhanContractFamily:
        _aware(as_of, name="catalog as_of")
        definition = _definition(symbol)
        allowed_dates = None if broker_expiry_dates is None else set(broker_expiry_dates)
        options = [
            (normalized, record)
            for normalized, record in self._entries
            if normalized.exchange == definition.exchange.value
            and normalized.segment == definition.derivative_segment
            and normalized.option_type is not None
            and _instrument_matches(normalized, definition.option_instrument)
            and record.expiry is not None
            and record.expiry > as_of
            and (allowed_dates is None or record.expiry.date() in allowed_dates)
            and _related_to_definition(normalized, definition)
        ]
        if not options:
            raise DhanCatalogError(f"no active option contracts found for {definition.symbol}")

        expiries = sorted({record.expiry for _, record in options if record.expiry is not None})
        errors: list[str] = []
        for expiry in expiries:
            assert expiry is not None
            expiry_options = [item for item in options if item[1].expiry == expiry]
            underlying_ids = sorted(
                {
                    record.underlying_security_id
                    for _, record in expiry_options
                    if record.underlying_security_id
                }
            )
            for underlying_id in underlying_ids:
                grouped_entries = tuple(
                    item
                    for item in expiry_options
                    if item[1].underlying_security_id == underlying_id
                )
                grouped = tuple(record for _, record in grouped_entries)
                relationship_alias = _unanimous_underlying_alias(grouped_entries)
                try:
                    return self._family_for_group(
                        definition,
                        expiry,
                        underlying_id,
                        grouped,
                        relationship_alias,
                    )
                except DhanCatalogError as exc:
                    errors.append(str(exc))
        detail = errors[-1] if errors else "no unambiguous underlying mapping"
        raise DhanCatalogError(f"cannot resolve {definition.symbol}: {detail}")

    def _family_for_group(
        self,
        definition: UniverseDefinition,
        expiry: datetime,
        underlying_id: str,
        options: tuple[InstrumentMasterRecord, ...],
        relationship_alias: str | None,
    ) -> DhanContractFamily:
        if definition.market_kind is MarketKind.COMMODITY:
            future_candidates = tuple(
                item
                for item in self._records(
                    exchange=definition.exchange,
                    segment=definition.derivative_segment,
                    instrument=definition.future_instrument,
                )
                if (
                    item[1].instrument.security_id == underlying_id
                    or item[1].underlying_security_id == underlying_id
                )
                and _record_name_matches(item[0], definition)
                and item[1].expiry is not None
                and item[1].expiry >= expiry
            )
            if not future_candidates:
                raise DhanCatalogError(
                    f"commodity option {definition.symbol} does not map to a covering future"
                )
            nearest_expiry = min(
                record.expiry
                for _, record in future_candidates
                if record.expiry is not None
            )
            nearest_futures = tuple(
                item for item in future_candidates if item[1].expiry == nearest_expiry
            )
            if len(nearest_futures) != 1:
                raise DhanCatalogError(
                    f"commodity option {definition.symbol} maps ambiguously to its nearest covering future"
                )
            normalized_future, future = nearest_futures[0]
            underlying = future
            historical_instrument = normalized_future.instrument or normalized_future.instrument_type
        else:
            identity_candidates = self._records(
                exchange=definition.exchange,
                segment=definition.underlying_segment,
                security_id=underlying_id,
                instrument=definition.underlying_instrument,
            )
            if identity_candidates:
                underlying_candidates = tuple(
                    item
                    for item in identity_candidates
                    if _record_name_matches(item[0], definition)
                )
            else:
                # Dhan's current index derivatives use a derivative-family ID in
                # UNDERLYING_SECURITY_ID (for example, NIFTY's 26000), while the
                # quoteable IDX_I record has a different security ID.  Preserve
                # the exact derivative relationship for options/futures and bridge
                # to IDX_I only through one unanimous, exact provider alias.
                underlying_candidates = tuple(
                    item
                    for item in self._records(
                        exchange=definition.exchange,
                        segment=definition.underlying_segment,
                        instrument=definition.underlying_instrument,
                    )
                    if relationship_alias is not None
                    and relationship_alias
                    in {_token(alias) for alias in definition.aliases}
                    and _record_name_has_token(item[0], relationship_alias)
                )
            if len(underlying_candidates) != 1:
                raise DhanCatalogError(
                    f"{definition.symbol} options do not map to one exact underlying record"
                )
            normalized_underlying, underlying = underlying_candidates[0]
            futures = tuple(
                item
                for item in self._records(
                    exchange=definition.exchange,
                    segment=definition.derivative_segment,
                    instrument=definition.future_instrument,
                )
                if item[1].underlying_security_id == underlying_id
                and item[1].expiry is not None
                and item[1].expiry >= expiry
            )
            if not futures:
                raise DhanCatalogError(f"no covering future found for {definition.symbol}")
            _, future = min(
                futures,
                key=lambda item: (item[1].expiry, item[1].instrument.security_id),
            )
            historical_instrument = (
                normalized_underlying.instrument or normalized_underlying.instrument_type
            )

        _ensure_unambiguous_option_records(options)
        return DhanContractFamily(
            definition=definition,
            provenance=self.provenance,
            underlying=underlying,
            future=future,
            option_expiry=expiry,
            option_records=tuple(
                sorted(options, key=lambda item: (item.strike or Decimal(0), item.option_type.value if item.option_type else ""))
            ),
            option_chain_security_id=underlying.instrument.security_id,
            option_chain_segment=underlying.instrument.segment,
            historical_instrument=historical_instrument,
        )

    def _records(
        self,
        *,
        exchange: Exchange,
        segment: str,
        instrument: str,
        security_id: str | None = None,
    ) -> tuple[tuple[NormalizedInstrumentRecord, InstrumentMasterRecord], ...]:
        return tuple(
            (normalized, record)
            for normalized, record in self._entries
            if normalized.exchange == exchange.value
            and normalized.segment == segment
            and (security_id is None or normalized.security_id == security_id)
            and _instrument_matches(normalized, instrument)
        )


def build_supported_dhan_catalog_batch(
    csv_text: str,
    *,
    fetched_at: datetime,
    source_url: str,
) -> DhanCatalogBatch:
    """Create a content-addressed, internally complete supported-universe subset."""

    _aware(fetched_at, name="master fetched_at")
    if not source_url.strip():
        raise NormalizationError("instrument-master source URL is required")
    all_records = normalize_dhan_instrument_master(csv_text)
    filtered = filter_supported_instruments(all_records)
    if not filtered:
        raise NormalizationError("Dhan master contains no supported-universe records")
    materialized = materialize_master_records(filtered, expiry_resolver=resolve_expiry_ist)
    subset_hash = content_hash(filtered)
    source_hash = sha256(csv_text.lstrip("\ufeff").encode("utf-8")).hexdigest()
    attestation = sha256(
        "\x00".join(
            (
                subset_hash,
                source_hash,
                source_url.strip(),
                fetched_at.astimezone(UTC).isoformat(),
                SUPPORTED_SUBSET_SCHEMA,
            )
        ).encode("utf-8")
    ).hexdigest()
    provenance = InstrumentMasterProvenance(
        batch_id=f"DHAN-SUPPORTED:{subset_hash[:16]}:{attestation[:16]}",
        provider="DHAN",
        source_url=source_url.strip(),
        content_hash=subset_hash,
        schema_version=SUPPORTED_SUBSET_SCHEMA,
        fetched_at=fetched_at,
        row_count=len(materialized),
    )
    return DhanCatalogBatch(
        provenance=provenance,
        normalized_records=filtered,
        records=materialized,
        source_content_hash=source_hash,
    )


def filter_supported_instruments(
    records: Iterable[NormalizedInstrumentRecord],
) -> tuple[NormalizedInstrumentRecord, ...]:
    """Keep complete derivative families for the nine documented universe symbols."""

    materialized = tuple(records)
    selected: set[int] = set()
    related_ids: set[tuple[str, str]] = set()
    for index, record in enumerate(materialized):
        definitions = tuple(
            definition
            for definition in SUPPORTED_UNIVERSE.values()
            if record.exchange == definition.exchange.value
            and _related_to_definition(record, definition)
        )
        if definitions:
            selected.add(index)
            related_ids.add((record.exchange, record.security_id))
            if record.underlying_security_id:
                related_ids.add((record.exchange, record.underlying_security_id))

    changed = True
    while changed:
        changed = False
        for index, record in enumerate(materialized):
            if index in selected:
                continue
            identity = (record.exchange, record.security_id)
            underlying = (
                (record.exchange, record.underlying_security_id)
                if record.underlying_security_id
                else None
            )
            if identity in related_ids or (underlying is not None and underlying in related_ids):
                selected.add(index)
                related_ids.add(identity)
                if underlying is not None:
                    related_ids.add(underlying)
                changed = True

    result = tuple(
        sorted(
            (materialized[index] for index in selected),
            key=lambda item: (item.exchange, item.segment, item.security_id),
        )
    )
    return result


def resolve_expiry_ist(record: NormalizedInstrumentRecord) -> datetime | None:
    """Resolve provider expiry dates to explicit, conservative exchange-close instants."""

    if record.expiry is None or not record.expiry.strip():
        return None
    try:
        expiry_date = date.fromisoformat(record.expiry.strip()[:10])
    except ValueError as exc:
        raise NormalizationError(
            f"instrument {record.security_id} has an invalid ISO expiry date"
        ) from exc
    if record.exchange in {Exchange.NSE.value, Exchange.BSE.value}:
        close = time(15, 30)
    elif record.exchange == Exchange.MCX.value:
        # Dhan's master provides a date, not an expiry instant.  MCX sessions can
        # extend into the evening; 23:30 IST avoids a premature same-day expiry.
        close = time(23, 30)
    else:
        raise NormalizationError(f"unsupported expiry exchange: {record.exchange!r}")
    return datetime.combine(expiry_date, close, tzinfo=IST)


def _select_five_strikes(
    records: Sequence[InstrumentMasterRecord], pricing_underlying: Decimal
) -> tuple[tuple[InstrumentMasterRecord, ...], Decimal]:
    by_key: dict[tuple[Decimal, OptionType], InstrumentMasterRecord] = {}
    strikes_with_both: set[Decimal] = set()
    for record in records:
        if record.strike is None or record.option_type is None:
            continue
        key = (record.strike, record.option_type)
        if key in by_key:
            raise DhanCatalogError("duplicate option strike/side in master")
        by_key[key] = record
    for strike in {key[0] for key in by_key}:
        if all((strike, side) in by_key for side in (OptionType.CALL, OptionType.PUT)):
            strikes_with_both.add(strike)
    if len(strikes_with_both) < 5:
        raise DhanCatalogError("fewer than five complete option strikes are listed")
    atm = nearest_atm(pricing_underlying, tuple(strikes_with_both))
    ordered = sorted(strikes_with_both)
    intervals = sorted(
        {right - left for left, right in pairwise(ordered) if right > left}
    )
    for interval in intervals:
        required = tuple(atm + Decimal(offset) * interval for offset in (-2, -1, 0, 1, 2))
        keys = tuple(
            (strike, side)
            for strike in required
            for side in (OptionType.CALL, OptionType.PUT)
        )
        if all(key in by_key for key in keys):
            return tuple(by_key[key] for key in keys), interval
    raise DhanCatalogError("master does not contain exact ATM-2..ATM+2 CE/PE records")


def _ensure_unambiguous_option_records(records: Sequence[InstrumentMasterRecord]) -> None:
    keys: set[tuple[Decimal, OptionType]] = set()
    security_ids: set[str] = set()
    for record in records:
        if record.strike is None or record.option_type is None:
            raise DhanCatalogError("option record lacks strike or side")
        key = (record.strike, record.option_type)
        if key in keys or record.instrument.security_id in security_ids:
            raise DhanCatalogError("option master mapping is ambiguous")
        keys.add(key)
        security_ids.add(record.instrument.security_id)


def _definition(symbol: str) -> UniverseDefinition:
    selected = _token(symbol)
    for definition in SUPPORTED_UNIVERSE.values():
        if selected == _token(definition.symbol) or selected in {
            _token(alias) for alias in definition.aliases
        }:
            return definition
    raise DhanCatalogError(f"unsupported universe symbol: {symbol!r}")


def _related_to_definition(
    record: NormalizedInstrumentRecord, definition: UniverseDefinition
) -> bool:
    values = (record.underlying_symbol, record.symbol, record.display_name)
    aliases = {_token(alias) for alias in definition.aliases}
    for value in values:
        if value and _token(value) in aliases:
            return True
    # Trading/display symbols contain expiry and strike suffixes.  The first
    # alphanumeric word must match exactly; this never treats BANKNIFTY as NIFTY.
    for value in (record.symbol, record.display_name):
        first = next(iter(re.findall(r"[A-Z0-9]+", value.upper())), "")
        if first and _token(first) in aliases:
            return True
    return False


def _record_name_matches(
    record: NormalizedInstrumentRecord, definition: UniverseDefinition
) -> bool:
    aliases = {_token(alias) for alias in definition.aliases}
    return any(_token(value) in aliases for value in (record.symbol, record.display_name))


def _record_name_has_token(record: NormalizedInstrumentRecord, expected: str) -> bool:
    return any(
        _token(value) == expected for value in (record.symbol, record.display_name)
    )


def _unanimous_underlying_alias(
    entries: Sequence[tuple[NormalizedInstrumentRecord, InstrumentMasterRecord]],
) -> str | None:
    aliases = tuple(
        _token(normalized.underlying_symbol)
        for normalized, _ in entries
        if normalized.underlying_symbol
    )
    if len(aliases) != len(entries) or len(set(aliases)) != 1:
        return None
    return aliases[0]


def _instrument_matches(record: NormalizedInstrumentRecord, expected: str) -> bool:
    return expected in {record.instrument.upper(), record.instrument_type.upper()}


def _token(value: str) -> str:
    return "".join(character for character in str(value).upper() if character.isalnum())


def _unique_subscriptions(
    values: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for segment, security_id in values:
        selected = (str(segment).strip().upper(), str(security_id).strip())
        if selected not in seen:
            seen.add(selected)
            result.append(selected)
    return tuple(result)


def _aware(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise NormalizationError(f"{name} must be timezone-aware")
    return value
