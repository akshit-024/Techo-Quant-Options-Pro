"""Fail-closed validation for normalized atomic market snapshots."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite
from typing import Any, TypeGuard

from teco_quant.domain.enums import (
    DataSource,
    Exchange,
    MarketKind,
    OptionType,
    PricingModel,
    ValidationSeverity,
)
from teco_quant.domain.models import AtomicSnapshot, OptionQuote, PreviousOptionSnapshot
from teco_quant.serialization import content_hash
from teco_quant.strategy.spec import DEFAULT_STRATEGY_CONFIG, StrategyConfig, nearest_atm


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    severity: ValidationSeverity
    path: str
    message: str
    observed: Any | None = None
    expected: Any | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]
    snapshot_id: str | None = None
    snapshot_hash: str | None = None

    @property
    def accepted(self) -> bool:
        return not any(issue.severity is ValidationSeverity.ERROR for issue in self.issues)

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity is ValidationSeverity.ERROR
        )

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity is ValidationSeverity.WARNING
        )

    def has_code(self, code: str) -> bool:
        return any(issue.code == code for issue in self.issues)


class SnapshotValidator:
    """Validate every component before a snapshot can be promoted as current."""

    def __init__(
        self,
        config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
        clock: Callable[[], datetime] | None = None,
        change_oi_reference_loader: (
            Callable[[str], PreviousOptionSnapshot | None] | None
        ) = None,
    ) -> None:
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._change_oi_reference_loader = change_oi_reference_loader

    def validate(
        self, snapshot: AtomicSnapshot, *, now: datetime | None = None
    ) -> ValidationReport:
        check_time = now or self._clock()
        issues: list[ValidationIssue] = []

        if not self._require_aware(check_time, "evaluation_time", issues):
            return ValidationReport(
                tuple(issues),
                snapshot_id=snapshot.snapshot_id,
                snapshot_hash=content_hash(snapshot),
            )
        self._validate_envelope(snapshot, check_time, issues)
        self._validate_contract(snapshot, check_time, issues)
        self._validate_market(snapshot, issues)
        self._validate_technicals(snapshot, issues)
        self._validate_context(snapshot, issues)
        self._validate_chain(snapshot, issues)
        self._validate_component_coherence(snapshot, check_time, issues)
        return ValidationReport(
            tuple(issues),
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=content_hash(snapshot),
        )

    def _error(
        self,
        issues: list[ValidationIssue],
        code: str,
        path: str,
        message: str,
        observed: Any | None = None,
        expected: Any | None = None,
    ) -> None:
        issues.append(
            ValidationIssue(
                code=code,
                severity=ValidationSeverity.ERROR,
                path=path,
                message=message,
                observed=observed,
                expected=expected,
            )
        )

    def _warning(
        self,
        issues: list[ValidationIssue],
        code: str,
        path: str,
        message: str,
        observed: Any | None = None,
        expected: Any | None = None,
    ) -> None:
        issues.append(
            ValidationIssue(
                code=code,
                severity=ValidationSeverity.WARNING,
                path=path,
                message=message,
                observed=observed,
                expected=expected,
            )
        )

    def _require_aware(
        self, value: datetime, path: str, issues: list[ValidationIssue]
    ) -> bool:
        if value.tzinfo is None or value.utcoffset() is None:
            self._error(
                issues,
                "NAIVE_TIMESTAMP",
                path,
                "timestamp must include an explicit timezone",
            )
            return False
        return True

    def _validate_envelope(
        self,
        snapshot: AtomicSnapshot,
        now: datetime,
        issues: list[ValidationIssue],
    ) -> None:
        if snapshot.sequence < 0:
            self._error(
                issues,
                "INVALID_SEQUENCE",
                "sequence",
                "snapshot sequence cannot be negative",
                snapshot.sequence,
            )
        if snapshot.strategy_version != self._config.version:
            self._error(
                issues,
                "STRATEGY_VERSION_MISMATCH",
                "strategy_version",
                "snapshot and active strategy versions differ",
                snapshot.strategy_version,
                self._config.version,
            )
        source_aware = self._require_aware(
            snapshot.source_timestamp, "source_timestamp", issues
        )
        received_aware = self._require_aware(snapshot.received_at, "received_at", issues)
        if source_aware:
            age = (now - snapshot.source_timestamp).total_seconds()
            if age > self._config.live_max_age_seconds:
                self._error(
                    issues,
                    "STALE_DATA",
                    "source_timestamp",
                    "live snapshot exceeds the maximum data age",
                    age,
                    self._config.live_max_age_seconds,
                )
            if age < -self._config.future_clock_skew_seconds:
                self._error(
                    issues,
                    "FUTURE_DATED_DATA",
                    "source_timestamp",
                    "source timestamp is materially ahead of the evaluation clock",
                    age,
                )
        if source_aware and received_aware:
            latency = (snapshot.received_at - snapshot.source_timestamp).total_seconds()
            if latency < -self._config.future_clock_skew_seconds:
                self._error(
                    issues,
                    "RECEIVED_BEFORE_SOURCE",
                    "received_at",
                    "received time precedes source time beyond clock-skew tolerance",
                    latency,
                )
        raw_hash = snapshot.metadata.get("raw_payload_hash")
        if not _is_sha256(raw_hash):
            self._warning(
                issues,
                "RAW_HASH_MISSING",
                "metadata.raw_payload_hash",
                "raw provider payload lacks a valid SHA-256 content address",
            )
        expected_component_hashes = {
            "contract": content_hash(snapshot.contract),
            "market": content_hash(snapshot.market),
            "technicals": content_hash(snapshot.technicals),
            "context": content_hash(snapshot.context),
            "option_chain": content_hash(snapshot.option_chain),
        }
        observed_component_hashes = snapshot.metadata.get(
            "normalized_component_hashes"
        )
        if observed_component_hashes != expected_component_hashes:
            self._error(
                issues,
                "COMPONENT_HASH_MISMATCH",
                "metadata.normalized_component_hashes",
                "normalized component hashes do not match the evaluated snapshot",
            )
        if snapshot.source is DataSource.DHAN_REST:
            raw_components = snapshot.metadata.get("raw_component_payloads")
            raw_hashes = snapshot.metadata.get("raw_component_hashes")
            option_payload = (
                raw_components.get("option_chain")
                if isinstance(raw_components, Mapping)
                else None
            )
            option_hash = (
                raw_hashes.get("option_chain")
                if isinstance(raw_hashes, Mapping)
                else None
            )
            if (
                option_payload is None
                or not _is_sha256(option_hash)
                or content_hash(option_payload) != option_hash
                or option_hash != raw_hash
            ):
                self._error(
                    issues,
                    "RAW_COMPONENT_PROVENANCE_INVALID",
                    "metadata.raw_component_payloads.option_chain",
                    "Dhan option payload and its content hash must be preserved together",
                )

    def _validate_contract(
        self,
        snapshot: AtomicSnapshot,
        now: datetime,
        issues: list[ValidationIssue],
    ) -> None:
        contract = snapshot.contract
        expiry_aware = self._require_aware(
            contract.option_expiry, "contract.option_expiry", issues
        )
        if expiry_aware:
            remaining = (contract.option_expiry - now).total_seconds()
            if remaining <= 0:
                self._error(
                    issues,
                    "INVALID_EXPIRY",
                    "contract.option_expiry",
                    "option expiry must be later than evaluation time",
                    remaining,
                    "> 0 seconds",
                )
            elif remaining <= self._config.extreme_expiry_seconds:
                self._warning(
                    issues,
                    "EXTREME_EXPIRY_RISK",
                    "contract.option_expiry",
                    "time to expiry is inside the extreme-risk lockout window",
                    remaining,
                    self._config.extreme_expiry_seconds,
                )

        for path, value in (
            ("contract.lot_size", contract.lot_size),
            ("contract.strike_interval", contract.strike_interval),
            ("contract.tick_size", contract.tick_size),
        ):
            if isinstance(value, Decimal) and (not value.is_finite() or value <= 0):
                invalid = True
            else:
                invalid = value <= 0
            if invalid:
                self._error(
                    issues,
                    "INVALID_CONTRACT_SPEC",
                    path,
                    "contract-controlled numeric values must be positive",
                    value,
                )

        master = contract.master
        if not all(
            (
                master.batch_id.strip(),
                master.provider.strip(),
                master.source_url.strip(),
                master.schema_version.strip(),
            )
        ):
            self._error(
                issues,
                "UNVERIFIED_CONTRACT",
                "contract.master",
                "contract must identify a persisted instrument-master batch",
            )
        if (
            len(master.content_hash) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in master.content_hash)
        ):
            self._error(
                issues,
                "INVALID_MASTER_HASH",
                "contract.master.content_hash",
                "instrument-master content hash must be a SHA-256 hexadecimal digest",
                master.content_hash,
            )
        if master.row_count <= 0:
            self._error(
                issues,
                "EMPTY_CONTRACT_MASTER",
                "contract.master.row_count",
                "instrument-master batch must contain at least one record",
                master.row_count,
            )
        if self._require_aware(master.fetched_at, "contract.master.fetched_at", issues):
            master_age = (now - master.fetched_at).total_seconds()
            if master_age > self._config.instrument_master_max_age_seconds:
                self._error(
                    issues,
                    "STALE_CONTRACT_MASTER",
                    "contract.master.fetched_at",
                    "contract metadata is older than the configured master-data limit",
                    master_age,
                    self._config.instrument_master_max_age_seconds,
                )
            elif master_age < -self._config.future_clock_skew_seconds:
                self._error(
                    issues,
                    "FUTURE_DATED_CONTRACT_MASTER",
                    "contract.master.fetched_at",
                    "contract verification time is ahead of the evaluation clock",
                    master_age,
                )

        expected_segment = {
            Exchange.NSE: "NSE_FNO",
            Exchange.BSE: "BSE_FNO",
            Exchange.MCX: "MCX_COMM",
        }[contract.underlying.exchange]
        if contract.market_kind is MarketKind.COMMODITY:
            if contract.underlying.exchange is not Exchange.MCX:
                self._error(
                    issues,
                    "MARKET_EXCHANGE_MISMATCH",
                    "contract.underlying.exchange",
                    "commodity contracts must use an MCX pricing instrument",
                    contract.underlying.exchange,
                    Exchange.MCX,
                )
            if contract.pricing_model is not PricingModel.BLACK_76:
                self._error(
                    issues,
                    "WRONG_PRICING_MODEL",
                    "contract.pricing_model",
                    "commodity options require Black-76",
                    contract.pricing_model,
                    PricingModel.BLACK_76,
                )
        else:
            if contract.underlying.exchange is Exchange.MCX:
                self._error(
                    issues,
                    "MARKET_EXCHANGE_MISMATCH",
                    "contract.underlying.exchange",
                    "index and stock policies cannot use an MCX pricing instrument",
                    contract.underlying.exchange,
                )
            if contract.pricing_model is not PricingModel.BLACK_SCHOLES:
                self._error(
                    issues,
                    "WRONG_PRICING_MODEL",
                    "contract.pricing_model",
                    "NSE/BSE index and stock options require the Black-Scholes policy",
                    contract.pricing_model,
                    PricingModel.BLACK_SCHOLES,
                )

        future = contract.futures
        if future is None:
            self._error(
                issues,
                "FUTURES_MAPPING_MISSING",
                "contract.futures",
                "the exact futures contract must be resolved from the same master batch",
            )
        else:
            self._validate_future_record(
                contract=contract,
                expected_segment=expected_segment,
                issues=issues,
            )

        seen_master_keys: set[tuple[Decimal, OptionType]] = set()
        seen_master_ids: set[str] = set()
        for index, record in enumerate(contract.option_contracts):
            path = f"contract.option_contracts[{index}]"
            if record.strike is None or record.option_type is None:
                self._error(
                    issues,
                    "INVALID_OPTION_MASTER_RECORD",
                    path,
                    "option master record requires strike and side",
                )
                continue
            key = (record.strike, record.option_type)
            if not _is_positive_decimal(record.strike):
                self._error(
                    issues,
                    "INVALID_OPTION_MASTER_STRIKE",
                    f"{path}.strike",
                    "option master strike must be a positive finite decimal",
                    record.strike,
                )
            if key in seen_master_keys or record.instrument.security_id in seen_master_ids:
                self._error(
                    issues,
                    "DUPLICATE_OPTION_MASTER_RECORD",
                    path,
                    "master option mappings must have unique strike/side and security ID",
                    key,
                )
            seen_master_keys.add(key)
            seen_master_ids.add(record.instrument.security_id)
            if (
                record.instrument.exchange is not contract.underlying.exchange
                or record.instrument.segment.upper() != expected_segment
                or record.underlying_security_id != contract.underlying.security_id
            ):
                self._error(
                    issues,
                    "OPTION_MASTER_IDENTITY_MISMATCH",
                    path,
                    "option record exchange, segment, and underlying must match the contract",
                )
            if record.expiry != contract.option_expiry:
                self._error(
                    issues,
                    "OPTION_MASTER_EXPIRY_MISMATCH",
                    f"{path}.expiry",
                    "option master expiry must match the selected expiry exactly",
                    record.expiry,
                    contract.option_expiry,
                )
            if record.lot_size != contract.lot_size or record.tick_size != contract.tick_size:
                self._error(
                    issues,
                    "OPTION_MASTER_SPEC_MISMATCH",
                    path,
                    "option lot size and tick size must match the contract revision",
                )

    def _validate_future_record(
        self,
        *,
        contract: Any,
        expected_segment: str,
        issues: list[ValidationIssue],
    ) -> None:
        future = contract.futures
        assert future is not None
        if (
            future.instrument.exchange is not contract.underlying.exchange
            or future.instrument.segment.upper() != expected_segment
            or not future.instrument_type.upper().startswith("FUT")
        ):
            self._error(
                issues,
                "FUTURES_MASTER_IDENTITY_MISMATCH",
                "contract.futures",
                "futures exchange, segment, and instrument type must match the contract",
            )
        if contract.market_kind is MarketKind.COMMODITY:
            if future.instrument.canonical_key != contract.underlying.canonical_key:
                self._error(
                    issues,
                    "MCX_FUTURES_MAPPING_MISMATCH",
                    "contract.futures.instrument",
                    "MCX pricing underlying must be the exact mapped futures instrument",
                )
        elif future.underlying_security_id != contract.underlying.security_id:
            self._error(
                issues,
                "FUTURES_UNDERLYING_MISMATCH",
                "contract.futures.underlying_security_id",
                "futures master record must map to the selected underlying",
                future.underlying_security_id,
                contract.underlying.security_id,
            )
        if future.expiry is None:
            self._error(
                issues,
                "FUTURES_EXPIRY_MISSING",
                "contract.futures.expiry",
                "mapped futures expiry is required",
            )
        elif self._require_aware(future.expiry, "contract.futures.expiry", issues):
            option_expiry_aware = (
                contract.option_expiry.tzinfo is not None
                and contract.option_expiry.utcoffset() is not None
            )
            if option_expiry_aware and future.expiry < contract.option_expiry:
                self._error(
                    issues,
                    "FUTURES_EXPIRY_MISMATCH",
                    "contract.futures.expiry",
                    "underlying futures cannot expire before the option",
                    future.expiry,
                    contract.option_expiry,
                )
        if future.lot_size != contract.lot_size or future.tick_size != contract.tick_size:
            self._error(
                issues,
                "FUTURES_MASTER_SPEC_MISMATCH",
                "contract.futures",
                "futures lot and tick must match the selected contract revision",
            )

    def _validate_market(
        self, snapshot: AtomicSnapshot, issues: list[ValidationIssue]
    ) -> None:
        market = snapshot.market
        self._require_aware(market.observed_at, "market.observed_at", issues)
        pricing_underlying = market.pricing_underlying(snapshot.contract.market_kind)
        if not _is_positive_decimal(pricing_underlying):
            self._error(
                issues,
                "PRICING_UNDERLYING_MISSING",
                "market.pricing_underlying",
                "the correct positive pricing underlying is required",
                pricing_underlying,
            )
        if (
            snapshot.contract.market_kind is not MarketKind.COMMODITY
            and not _is_positive_decimal(market.futures_price)
        ):
            self._error(
                issues,
                "FUTURES_PRICE_MISSING",
                "market.futures_price",
                "relevant futures price is required as directional context",
                market.futures_price,
            )
        required_prices = {
            "previous_close": market.previous_close,
            "day_open": market.day_open,
            "day_high": market.day_high,
            "day_low": market.day_low,
            "vwap": market.vwap,
        }
        for name, value in required_prices.items():
            if not _is_positive_decimal(value):
                self._error(
                    issues,
                    "MARKET_FIELD_MISSING",
                    f"market.{name}",
                    "positive market-structure input is required",
                    value,
                )
        day_high = market.day_high
        day_low = market.day_low
        if (
            _is_positive_decimal(day_high)
            and _is_positive_decimal(day_low)
            and day_high < day_low
        ):
            self._error(
                issues,
                "INVALID_DAY_RANGE",
                "market.day_high",
                "day high cannot be below day low",
                day_high,
                day_low,
            )

    def _validate_technicals(
        self, snapshot: AtomicSnapshot, issues: list[ValidationIssue]
    ) -> None:
        technicals = snapshot.technicals
        self._require_aware(technicals.observed_at, "technicals.observed_at", issues)
        if not technicals.completed_candle:
            self._error(
                issues,
                "INCOMPLETE_TECHNICAL_CANDLE",
                "technicals.completed_candle",
                "technical indicators must use completed candles only",
                technicals.completed_candle,
                True,
            )
        for name, value in (
            ("ema_9", technicals.ema_9),
            ("ema_21", technicals.ema_21),
            ("wma_44", technicals.wma_44),
            ("previous_wma_44", technicals.previous_wma_44),
        ):
            if not _is_positive_decimal(value):
                self._error(
                    issues,
                    "TECHNICAL_FIELD_MISSING",
                    f"technicals.{name}",
                    "positive completed-candle indicator is required",
                    value,
                )
        if (
            technicals.rsi_14 is None
            or not isfinite(technicals.rsi_14)
            or not 0 <= technicals.rsi_14 <= 100
        ):
            self._error(
                issues,
                "INVALID_RSI",
                "technicals.rsi_14",
                "RSI must be within 0..100",
                technicals.rsi_14,
            )
        if not _is_positive_decimal(technicals.atr_14):
            self._error(
                issues,
                "INVALID_ATR",
                "technicals.atr_14",
                "ATR must be positive and must not be substituted with RSI",
                technicals.atr_14,
            )
        if (
            technicals.reference_volatility is None
            or not isfinite(technicals.reference_volatility)
            or technicals.reference_volatility <= 0
        ):
            self._error(
                issues,
                "REFERENCE_VOLATILITY_MISSING",
                "technicals.reference_volatility",
                "reference volatility must be a positive decimal ratio",
                technicals.reference_volatility,
            )

    def _validate_context(
        self, snapshot: AtomicSnapshot, issues: list[ValidationIssue]
    ) -> None:
        context = snapshot.context
        if not context.account_capital.is_finite() or context.account_capital <= 0:
            self._error(
                issues,
                "INVALID_ACCOUNT_CAPITAL",
                "context.account_capital",
                "account capital must be positive",
                context.account_capital,
            )
        if not isfinite(context.risk_per_trade) or not (
            0 < context.risk_per_trade <= self._config.maximum_risk_per_trade
        ):
            self._error(
                issues,
                "RISK_LIMIT_EXCEEDED",
                "context.risk_per_trade",
                "risk per trade must be positive and within the hard ceiling",
                context.risk_per_trade,
                self._config.maximum_risk_per_trade,
            )
        if not isfinite(context.maximum_premium_allocation) or not (
            0 < context.maximum_premium_allocation <= 1
        ):
            self._error(
                issues,
                "INVALID_PREMIUM_ALLOCATION",
                "context.maximum_premium_allocation",
                "premium allocation must be within (0, 1]",
                context.maximum_premium_allocation,
            )
        signal_prices_finite = (
            context.signal_candle_high.is_finite()
            and context.signal_candle_low.is_finite()
        )
        if not signal_prices_finite or (
            context.signal_candle_high <= 0 or context.signal_candle_low <= 0
        ):
            self._error(
                issues,
                "SIGNAL_CANDLE_MISSING",
                "context.signal_candle_high",
                "positive signal-candle high and low are required",
            )
        elif context.signal_candle_high < context.signal_candle_low:
            self._error(
                issues,
                "INVALID_SIGNAL_CANDLE",
                "context.signal_candle_high",
                "signal-candle high cannot be below its low",
                context.signal_candle_high,
                context.signal_candle_low,
            )
        if not isfinite(context.expected_holding_hours) or context.expected_holding_hours <= 0:
            self._error(
                issues,
                "INVALID_HOLDING_PERIOD",
                "context.expected_holding_hours",
                "expected holding period must be positive",
                context.expected_holding_hours,
            )
        if context.event_risk_active is None:
            self._warning(
                issues,
                "EVENT_RISK_UNKNOWN",
                "context.event_risk_active",
                "unknown event risk is accepted for display but blocks a trade decision",
            )

    def _validate_chain(
        self, snapshot: AtomicSnapshot, issues: list[ValidationIssue]
    ) -> None:
        quotes = snapshot.option_chain
        if not quotes:
            self._error(
                issues,
                "OPTION_CHAIN_MISSING",
                "option_chain",
                "option chain cannot be empty",
            )
            return

        seen_keys: set[tuple[Decimal, OptionType]] = set()
        seen_security_ids: set[str] = set()
        master_by_key = {
            (record.strike, record.option_type): record
            for record in snapshot.contract.option_contracts
            if record.strike is not None and record.option_type is not None
        }
        loaded_references: dict[str, PreviousOptionSnapshot | None] = {}
        for index, quote in enumerate(quotes):
            path = f"option_chain[{index}]"
            if quote.key in seen_keys:
                self._error(
                    issues,
                    "DUPLICATE_STRIKE_SIDE",
                    path,
                    "strike and option side must be unique within a snapshot",
                    quote.key,
                )
            seen_keys.add(quote.key)
            if not quote.security_id:
                self._error(
                    issues,
                    "SECURITY_ID_MISSING",
                    f"{path}.security_id",
                    "provider security ID is required",
                )
            elif quote.security_id in seen_security_ids:
                self._error(
                    issues,
                    "DUPLICATE_SECURITY_ID",
                    f"{path}.security_id",
                    "security ID cannot identify more than one option leg",
                    quote.security_id,
                )
            seen_security_ids.add(quote.security_id)
            master_record = master_by_key.get(quote.key)
            if master_record is None:
                self._error(
                    issues,
                    "OPTION_NOT_IN_CONTRACT_MASTER",
                    path,
                    "strike and side are absent from the selected master revision",
                    quote.key,
                )
            elif master_record.instrument.security_id != quote.security_id:
                self._error(
                    issues,
                    "OPTION_SECURITY_ID_MISMATCH",
                    f"{path}.security_id",
                    "quote security ID does not match the master strike/side mapping",
                    quote.security_id,
                    master_record.instrument.security_id,
                )
            if quote.expiry != snapshot.contract.option_expiry:
                self._error(
                    issues,
                    "MIXED_EXPIRY",
                    f"{path}.expiry",
                    "option leg expiry differs from the selected contract",
                    quote.expiry,
                    snapshot.contract.option_expiry,
                )
            self._validate_quote(quote, snapshot.source, path, issues)
            self._validate_change_oi_reference(
                snapshot,
                quote,
                path,
                issues,
                loaded_references,
            )
            if (
                getattr(quote, "change_oi_source_snapshot_id", None)
                == snapshot.snapshot_id
            ):
                self._error(
                    issues,
                    "SELF_REFERENTIAL_CHANGE_OI",
                    f"{path}.change_oi_source_snapshot_id",
                    "change OI cannot derive from the current snapshot",
                )

        pricing_underlying = snapshot.market.pricing_underlying(snapshot.contract.market_kind)
        if not _is_positive_decimal(pricing_underlying):
            return
        strikes = sorted(
            {quote.strike for quote in quotes if _is_positive_decimal(quote.strike)}
        )
        if len(strikes) < 5:
            self._error(
                issues,
                "FIVE_STRIKES_UNAVAILABLE",
                "option_chain",
                "at least five unique strikes are required",
                len(strikes),
                5,
            )
            return
        atm = nearest_atm(pricing_underlying, strikes)
        interval = snapshot.contract.strike_interval
        if not _is_positive_decimal(interval):
            return
        expected = tuple(atm + Decimal(offset) * interval for offset in (-2, -1, 0, 1, 2))
        available = set(strikes)
        missing_strikes = tuple(strike for strike in expected if strike not in available)
        if missing_strikes:
            self._error(
                issues,
                "FIVE_STRIKE_TOPOLOGY_INVALID",
                "option_chain",
                "ATM-2 through ATM+2 are not present at the verified interval",
                missing_strikes,
                expected,
            )
        required_keys = {
            (strike, side) for strike in expected for side in (OptionType.CALL, OptionType.PUT)
        }
        missing_master_keys = sorted(
            required_keys - set(master_by_key),
            key=lambda item: (item[0], item[1].value),
        )
        if missing_master_keys:
            self._error(
                issues,
                "FIVE_STRIKE_MASTER_MAPPING_MISSING",
                "contract.option_contracts",
                "verified master must map both sides of ATM-2 through ATM+2",
                missing_master_keys,
            )
        missing_legs = sorted(
            required_keys - seen_keys, key=lambda item: (item[0], item[1].value)
        )
        if missing_legs:
            self._error(
                issues,
                "FIVE_STRIKE_LEG_MISSING",
                "option_chain",
                "both CE and PE are required for each of the five strikes",
                missing_legs,
            )

    def _validate_quote(
        self,
        quote: OptionQuote,
        source: DataSource,
        path: str,
        issues: list[ValidationIssue],
    ) -> None:
        self._require_aware(quote.expiry, f"{path}.expiry", issues)
        self._require_aware(quote.observed_at, f"{path}.observed_at", issues)
        if not _is_positive_decimal(quote.strike):
            self._error(
                issues,
                "INVALID_STRIKE",
                f"{path}.strike",
                "strike must be positive",
                quote.strike,
            )
        for name, price_value in (
            ("bid", quote.bid),
            ("ask", quote.ask),
            ("ltp", quote.ltp),
        ):
            if not _is_positive_decimal(price_value):
                self._error(
                    issues,
                    "QUOTE_FIELD_MISSING",
                    f"{path}.{name}",
                    "bid, ask, and LTP must be present and positive",
                    price_value,
                )
        bid = quote.bid
        ask = quote.ask
        if (
            _is_positive_decimal(bid)
            and _is_positive_decimal(ask)
            and ask < bid
        ):
            self._error(
                issues,
                "ASK_BELOW_BID",
                f"{path}.ask",
                "ask cannot be below bid",
                ask,
                f">= {bid}",
            )
        spread_ratio = quote.spread_ratio
        if spread_ratio is not None and spread_ratio > self._config.maximum_spread_ratio:
            self._warning(
                issues,
                "WIDE_SPREAD",
                f"{path}.spread_ratio",
                "leg exceeds the configured executable spread",
                spread_ratio,
                self._config.maximum_spread_ratio,
            )
        for name, integer_value in (
            ("volume", quote.volume),
            ("open_interest", quote.open_interest),
        ):
            if integer_value is None or integer_value < 0:
                self._error(
                    issues,
                    "QUOTE_FIELD_MISSING",
                    f"{path}.{name}",
                    "volume and open interest must be present and non-negative",
                    integer_value,
                )
        change_source = getattr(quote, "change_oi_source_snapshot_id", None)
        change_interval = getattr(quote, "change_oi_interval_seconds", None)
        if quote.change_open_interest is None:
            self._warning(
                issues,
                "CHANGE_OI_BASELINE_REQUIRED",
                f"{path}.change_open_interest",
                "snapshot can seed the next interval but cannot produce an OI score",
            )
            if change_source is not None or change_interval is not None:
                self._error(
                    issues,
                    "ORPHAN_CHANGE_OI_PROVENANCE",
                    f"{path}.change_oi_source_snapshot_id",
                    "change-OI provenance cannot exist without a derived value",
                )
        elif source is not DataSource.DHAN_REST:
            self._error(
                issues,
                "INVALID_CHANGE_OI_SOURCE",
                f"{path}.change_open_interest",
                "intraday change OI may only be derived within the Dhan REST stream",
                source,
                DataSource.DHAN_REST,
            )
        elif (
            not change_source
            or change_interval is None
            or not isfinite(change_interval)
            or not 0 < change_interval <= self._config.live_max_age_seconds
        ):
            self._error(
                issues,
                "INVALID_CHANGE_OI_PROVENANCE",
                f"{path}.change_oi_source_snapshot_id",
                "change OI requires a prior accepted snapshot and bounded positive interval",
                {"snapshot_id": change_source, "interval_seconds": change_interval},
            )
        if (
            quote.implied_volatility is None
            or not isfinite(quote.implied_volatility)
            or quote.implied_volatility <= 0
        ):
            self._error(
                issues,
                "IV_UNAVAILABLE",
                f"{path}.implied_volatility",
                "the current IV-dependent policy cannot score a leg without positive IV",
                quote.implied_volatility,
            )

    def _validate_change_oi_reference(
        self,
        snapshot: AtomicSnapshot,
        quote: OptionQuote,
        path: str,
        issues: list[ValidationIssue],
        loaded: dict[str, PreviousOptionSnapshot | None],
    ) -> None:
        """Recompute change OI from the exact accepted leg supplied by the loader."""

        if quote.change_open_interest is None:
            return
        reference_id = quote.change_oi_source_snapshot_id
        if not reference_id or snapshot.source is not DataSource.DHAN_REST:
            return
        if self._change_oi_reference_loader is None:
            self._error(
                issues,
                "CHANGE_OI_REFERENCE_UNAVAILABLE",
                f"{path}.change_oi_source_snapshot_id",
                "change OI cannot be accepted without loading its accepted prior snapshot",
                reference_id,
            )
            return
        if reference_id not in loaded:
            try:
                loaded[reference_id] = self._change_oi_reference_loader(reference_id)
            except Exception as exc:  # noqa: BLE001 - fail closed on adapter failures
                loaded[reference_id] = None
                self._error(
                    issues,
                    "CHANGE_OI_REFERENCE_LOAD_FAILED",
                    f"{path}.change_oi_source_snapshot_id",
                    "accepted prior snapshot could not be loaded",
                    type(exc).__name__,
                )
                return
        previous = loaded[reference_id]
        if previous is None:
            self._error(
                issues,
                "CHANGE_OI_REFERENCE_NOT_ACCEPTED",
                f"{path}.change_oi_source_snapshot_id",
                "referenced change-OI snapshot is not an accepted snapshot",
                reference_id,
            )
            return
        if (
            previous.snapshot_id != reference_id
            or previous.source is not DataSource.DHAN_REST
            or previous.contract_key != snapshot.contract.contract_key
            or previous.sequence >= snapshot.sequence
        ):
            self._error(
                issues,
                "CHANGE_OI_REFERENCE_MISMATCH",
                f"{path}.change_oi_source_snapshot_id",
                "referenced snapshot must be an earlier Dhan snapshot of the exact contract",
                {
                    "snapshot_id": previous.snapshot_id,
                    "source": previous.source,
                    "contract_key": previous.contract_key,
                    "sequence": previous.sequence,
                },
            )
            return

        matches = tuple(
            prior
            for prior in previous.option_chain
            if prior.security_id == quote.security_id
            and prior.strike == quote.strike
            and prior.option_type is quote.option_type
            and prior.expiry == quote.expiry
        )
        if len(matches) != 1:
            self._error(
                issues,
                "CHANGE_OI_PRIOR_LEG_MISMATCH",
                f"{path}.change_oi_source_snapshot_id",
                "referenced snapshot must contain exactly one matching security/expiry/strike/side",
                len(matches),
                1,
            )
            return
        prior = matches[0]
        timestamps = (
            snapshot.source_timestamp,
            quote.observed_at,
            previous.source_timestamp,
            prior.observed_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            self._error(
                issues,
                "CHANGE_OI_TIMESTAMP_INVALID",
                f"{path}.change_oi_interval_seconds",
                "current and prior source/leg timestamps must all be timezone-aware",
            )
            return
        current_source = snapshot.source_timestamp.astimezone(UTC)
        current_leg = quote.observed_at.astimezone(UTC)
        prior_source = previous.source_timestamp.astimezone(UTC)
        prior_leg = prior.observed_at.astimezone(UTC)
        source_interval = (current_source - prior_source).total_seconds()
        leg_interval = (current_leg - prior_leg).total_seconds()
        declared_interval = quote.change_oi_interval_seconds
        if (
            current_source != current_leg
            or prior_source != prior_leg
            or source_interval <= 0
            or leg_interval != source_interval
            or declared_interval is None
            or not isfinite(declared_interval)
            or abs(declared_interval - leg_interval) > 1e-9
        ):
            self._error(
                issues,
                "CHANGE_OI_INTERVAL_MISMATCH",
                f"{path}.change_oi_interval_seconds",
                "declared interval must exactly match coherent prior/current source and leg times",
                declared_interval,
                leg_interval,
            )
        if (
            prior.open_interest is None
            or quote.open_interest is None
            or quote.change_open_interest != quote.open_interest - prior.open_interest
        ):
            self._error(
                issues,
                "CHANGE_OI_DELTA_MISMATCH",
                f"{path}.change_open_interest",
                "change OI must equal current open interest minus the exact prior leg's open interest",
                quote.change_open_interest,
                (
                    None
                    if prior.open_interest is None or quote.open_interest is None
                    else quote.open_interest - prior.open_interest
                ),
            )

    def _validate_component_coherence(
        self,
        snapshot: AtomicSnapshot,
        now: datetime,
        issues: list[ValidationIssue],
    ) -> None:
        timestamps: list[tuple[str, datetime]] = [
            ("source_timestamp", snapshot.source_timestamp),
            ("market.observed_at", snapshot.market.observed_at),
            ("technicals.observed_at", snapshot.technicals.observed_at),
        ]
        timestamps.extend(
            (f"option_chain[{index}].observed_at", quote.observed_at)
            for index, quote in enumerate(snapshot.option_chain)
        )
        aware_values: list[datetime] = []
        for path, value in timestamps:
            if value.tzinfo is None or value.utcoffset() is None:
                continue
            aware_values.append(value)
            age = (now - value).total_seconds()
            if age > self._config.live_max_age_seconds:
                self._error(
                    issues,
                    "STALE_COMPONENT",
                    path,
                    "snapshot component is stale",
                    age,
                    self._config.live_max_age_seconds,
                )
            elif age < -self._config.future_clock_skew_seconds:
                self._error(
                    issues,
                    "FUTURE_DATED_COMPONENT",
                    path,
                    "snapshot component is ahead of the evaluation clock",
                    age,
                )
        if aware_values:
            skew = (max(aware_values) - min(aware_values)).total_seconds()
            if skew > self._config.component_skew_seconds:
                self._error(
                    issues,
                    "ATOMIC_SNAPSHOT_SKEW",
                    "snapshot",
                    "components do not belong to one coherent evaluation window",
                    skew,
                    self._config.component_skew_seconds,
                )


def issue_codes(issues: Iterable[ValidationIssue]) -> tuple[str, ...]:
    return tuple(issue.code for issue in issues)


def _is_positive_decimal(value: Decimal | None) -> TypeGuard[Decimal]:
    return value is not None and value.is_finite() and value > 0


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )
