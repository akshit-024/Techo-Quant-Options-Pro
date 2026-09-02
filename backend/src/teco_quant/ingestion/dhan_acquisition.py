"""Bounded, read-only Dhan REST acquisition service for the supported universe."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from threading import Event, Lock, RLock, Thread, current_thread
from typing import Any, Protocol

from teco_quant.api.market_read_model import MarketReadModelStore
from teco_quant.automation.service import TecoAutomationService
from teco_quant.brokers.dhan import (
    DHAN_INSTRUMENT_MASTER_DETAILED,
    DhanCredentials,
    DhanRestClient,
)
from teco_quant.domain.enums import (
    DataSource,
    MarketKind,
    OperatingMode,
    SnapshotStatus,
    TradingStyle,
)
from teco_quant.domain.models import (
    ContractSpec,
    InstrumentMasterProvenance,
    InstrumentMasterRecord,
    MarketState,
    PreviousOptionSnapshot,
    StrategyContext,
)
from teco_quant.ingestion.dhan_catalog import (
    IST,
    SUPPORTED_UNIVERSE,
    DhanCatalogBatch,
    DhanInstrumentCatalog,
    ResolvedDhanContract,
    build_supported_dhan_catalog_batch,
)
from teco_quant.ingestion.dhan_historical import (
    CompletedTechnicalResult,
    completed_technical_state,
    expected_completed_15m_boundary,
    normalize_dhan_intraday,
    validate_completed_15m_coverage,
)
from teco_quant.ingestion.normalization import normalize_dhan_market_quote
from teco_quant.ingestion.service import SnapshotIngestionService, build_dhan_snapshot
from teco_quant.signals.models import SignalPipelineResult
from teco_quant.signals.service import AutomatedSignalService

_LOGGER = logging.getLogger(__name__)


class AcquisitionLifecycle(StrEnum):
    DISABLED = "DISABLED"
    CONFIG_REQUIRED = "CONFIG_REQUIRED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    ERROR = "ERROR"


class DhanReadClient(Protocol):
    def instrument_master(self, *, detailed: bool = True) -> str: ...

    def market_quote(
        self, instruments_by_segment: Mapping[str, Sequence[int | str]]
    ) -> Mapping[str, Any]: ...

    def expiry_list(
        self, *, underlying_security_id: int, underlying_segment: str
    ) -> list[str]: ...

    def option_chain(
        self,
        *,
        underlying_security_id: int,
        underlying_segment: str,
        expiry: Any,
    ) -> Mapping[str, Any]: ...

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
    ) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class AcquisitionRepository(Protocol):
    def record_instrument_master(
        self,
        provenance: InstrumentMasterProvenance,
        records: Iterable[InstrumentMasterRecord],
    ) -> str: ...

    def latest_previous_option_snapshot(
        self,
        contract_key: str,
        source: DataSource,
        *,
        before_sequence: int | None = None,
        before_source_timestamp: datetime | None = None,
    ) -> PreviousOptionSnapshot | None: ...


class AcquisitionContextProvider(Protocol):
    def __call__(
        self,
        symbol: str,
        contract: ContractSpec,
        technicals: CompletedTechnicalResult,
        now: datetime,
    ) -> StrategyContext: ...


SubscriptionsCallback = Callable[[tuple[tuple[str, str], ...]], None]
ContractsCallback = Callable[
    [Mapping[str, ContractSpec], Mapping[str, Mapping[str, str]]], None
]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ExplicitStrategyInputs:
    """User/risk-owned decision inputs; no market-data default can make them favorable."""

    account_capital: Decimal
    risk_per_trade: float
    maximum_premium_allocation: float
    event_risk_active: bool
    expected_holding_hours: float
    operating_mode: OperatingMode = OperatingMode.PRO
    trading_style: TradingStyle = TradingStyle.INTRADAY
    price_action_confirmed: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.account_capital, Decimal):
            object.__setattr__(self, "account_capital", Decimal(str(self.account_capital)))
        if not self.account_capital.is_finite() or self.account_capital <= 0:
            raise ValueError("account_capital must be a positive finite Decimal")
        if not 0 < self.risk_per_trade <= 1:
            raise ValueError("risk_per_trade must be within (0, 1]")
        if not 0 < self.maximum_premium_allocation <= 1:
            raise ValueError("maximum_premium_allocation must be within (0, 1]")
        if not isinstance(self.event_risk_active, bool):
            raise TypeError("event_risk_active must be explicitly true or false")
        if self.expected_holding_hours <= 0:
            raise ValueError("expected_holding_hours must be positive")
        if self.price_action_confirmed not in {True, False, None}:
            raise ValueError("price_action_confirmed must be true, false, or null")

    def __call__(
        self,
        symbol: str,
        contract: ContractSpec,
        technicals: CompletedTechnicalResult,
        now: datetime,
    ) -> StrategyContext:
        del symbol, contract, now
        candle = technicals.latest_candle
        return StrategyContext(
            operating_mode=self.operating_mode,
            trading_style=self.trading_style,
            account_capital=self.account_capital,
            risk_per_trade=self.risk_per_trade,
            maximum_premium_allocation=self.maximum_premium_allocation,
            event_risk_active=self.event_risk_active,
            price_action_confirmed=self.price_action_confirmed,
            signal_candle_high=candle.high,
            signal_candle_low=candle.low,
            expected_holding_hours=self.expected_holding_hours,
        )


class FailClosedStrategyInputs:
    """Seed valid data/OI baselines while unknown event risk guarantees no new trade."""

    def __call__(
        self,
        symbol: str,
        contract: ContractSpec,
        technicals: CompletedTechnicalResult,
        now: datetime,
    ) -> StrategyContext:
        del symbol, contract, now
        candle = technicals.latest_candle
        return StrategyContext(
            operating_mode=OperatingMode.PRO,
            trading_style=TradingStyle.INTRADAY,
            # Structurally valid, deliberately unaffordable placeholders.  They
            # are not presented as configured user risk settings, and unknown
            # event risk remains the decisive NO TRADE gate.
            account_capital=Decimal(1),
            risk_per_trade=0.000001,
            maximum_premium_allocation=0.000001,
            event_risk_active=None,
            price_action_confirmed=None,
            signal_candle_high=candle.high,
            signal_candle_low=candle.low,
            expected_holding_hours=0.25,
        )


@dataclass(frozen=True, slots=True)
class MarketAcquisitionResult:
    symbol: str
    success: bool
    accepted: bool
    published: bool
    snapshot_id: str | None = None
    contract_key: str | None = None
    error_code: str | None = None

    @property
    def data_success(self) -> bool:
        return self.success and self.accepted and self.published

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "success": self.success,
            "accepted": self.accepted,
            "published": self.published,
            "data_success": self.data_success,
            "snapshot_id": self.snapshot_id,
            "contract_key": self.contract_key,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class AcquisitionCycleResult:
    started_at: datetime
    completed_at: datetime
    configured: bool
    master_batch_id: str | None
    markets: tuple[MarketAcquisitionResult, ...]
    subscriptions: tuple[tuple[str, str], ...]

    @property
    def successful_markets(self) -> int:
        return sum(result.success for result in self.markets)

    @property
    def failed_markets(self) -> int:
        return len(self.markets) - self.successful_markets

    @property
    def accepted_markets(self) -> int:
        return sum(result.accepted for result in self.markets)

    @property
    def published_markets(self) -> int:
        return sum(result.published for result in self.markets)

    @property
    def data_successful_markets(self) -> int:
        return sum(result.data_success for result in self.markets)

    def as_dict(self) -> dict[str, object]:
        return {
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "configured": self.configured,
            "master_batch_id": self.master_batch_id,
            "successful_markets": self.successful_markets,
            "failed_markets": self.failed_markets,
            "accepted_markets": self.accepted_markets,
            "published_markets": self.published_markets,
            "data_successful_markets": self.data_successful_markets,
            "subscriptions": [
                {"segment": segment, "security_id": security_id}
                for segment, security_id in self.subscriptions
            ],
            "markets": [result.as_dict() for result in self.markets],
        }


@dataclass(frozen=True, slots=True)
class _TechnicalCacheEntry:
    boundary: datetime
    result: CompletedTechnicalResult


@dataclass(slots=True)
class _MarketHealth:
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    snapshot_id: str | None = None
    accepted: bool = False
    error_code: str | None = None


class DhanAcquisitionService:
    """Refresh, resolve, acquire, validate, persist, analyze, and publish read models.

    The service has no order methods.  Credentials are represented only by the optional
    injected read client, so importing or constructing the offline runtime never asks for
    them.  One symbol failure is isolated and the last published read model remains visible;
    its normal read-time freshness checks then mark it stale and non-actionable.
    """

    def __init__(
        self,
        *,
        client: DhanReadClient | None,
        repository: AcquisitionRepository,
        ingestion_service: SnapshotIngestionService,
        read_models: MarketReadModelStore,
        automation_service: TecoAutomationService | None = None,
        signal_service: AutomatedSignalService | None = None,
        context_provider: AcquisitionContextProvider | None = None,
        symbols: Sequence[str] = tuple(SUPPORTED_UNIVERSE),
        on_subscriptions: SubscriptionsCallback | None = None,
        on_contracts: ContractsCallback | None = None,
        clock: Clock | None = None,
        poll_interval_seconds: float = 10.0,
        maximum_backoff_seconds: float = 60.0,
        history_days: int = 14,
        maximum_quote_chain_skew_seconds: float = 5.0,
        source_url: str = DHAN_INSTRUMENT_MASTER_DETAILED,
        close_client_on_stop: bool = True,
    ) -> None:
        if poll_interval_seconds < 3:
            raise ValueError("poll interval cannot be below Dhan's three-second chain limit")
        if maximum_backoff_seconds < poll_interval_seconds:
            raise ValueError("maximum backoff cannot be below the poll interval")
        if isinstance(history_days, bool) or not 2 <= history_days <= 90:
            raise ValueError("history_days must be within 2..90")
        if not 0 < maximum_quote_chain_skew_seconds <= 5:
            raise ValueError("quote/chain skew policy must be within (0, 5] seconds")
        selected_symbols = tuple(dict.fromkeys(str(value).strip().upper() for value in symbols))
        if not selected_symbols or any(symbol not in SUPPORTED_UNIVERSE for symbol in selected_symbols):
            raise ValueError("symbols must be a non-empty supported-universe subset")
        if not source_url.strip():
            raise ValueError("instrument master source URL is required")

        self._client = client
        self._repository = repository
        self._ingestion = ingestion_service
        self._read_models = read_models
        self._automation = automation_service
        self._signals = signal_service or AutomatedSignalService()
        self._context_provider = context_provider or FailClosedStrategyInputs()
        self._decision_inputs_configured = context_provider is not None
        self._symbols = selected_symbols
        self._on_subscriptions = on_subscriptions
        self._on_contracts = on_contracts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._poll_interval = float(poll_interval_seconds)
        self._maximum_backoff = float(maximum_backoff_seconds)
        self._history_days = history_days
        self._maximum_quote_chain_skew = float(maximum_quote_chain_skew_seconds)
        self._source_url = source_url.strip()
        self._close_client_on_stop = close_client_on_stop

        self._lock = RLock()
        self._cycle_lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._catalog: DhanInstrumentCatalog | None = None
        self._catalog_batch: DhanCatalogBatch | None = None
        self._last_master_refresh: datetime | None = None
        self._technical_cache: dict[tuple[str, str], _TechnicalCacheEntry] = {}
        self._expiry_cache: dict[tuple[str, str, str, date], tuple[date, ...]] = {}
        self._subscriptions: tuple[tuple[str, str], ...] = ()
        self._contracts: dict[str, ContractSpec] = {}
        self._registries: dict[str, Mapping[str, str]] = {}
        self._market_health = {symbol: _MarketHealth() for symbol in self._symbols}
        self._lifecycle = (
            AcquisitionLifecycle.DISABLED
            if client is not None
            else AcquisitionLifecycle.CONFIG_REQUIRED
        )
        self._last_cycle_started: datetime | None = None
        self._last_cycle_completed: datetime | None = None
        self._last_success: datetime | None = None
        self._consecutive_failures = 0
        self._last_cycle: AcquisitionCycleResult | None = None
        self._master_error_code: str | None = None
        self._callback_error_code: str | None = None
        self._client_closed = False

    def start(self) -> bool:
        """Start the bounded worker; return false when credentials/client are absent."""

        with self._lock:
            if self._client is None:
                self._lifecycle = AcquisitionLifecycle.CONFIG_REQUIRED
                return False
            if self._client_closed:
                self._lifecycle = AcquisitionLifecycle.ERROR
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._lifecycle = AcquisitionLifecycle.INITIALIZING
            self._thread = Thread(
                target=self._worker,
                name="teco-dhan-acquisition",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, *, timeout: float = 15.0) -> bool:
        if timeout < 0:
            raise ValueError("stop timeout cannot be negative")
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            with self._lock:
                self._lifecycle = AcquisitionLifecycle.ERROR
            return False
        with self._lock:
            self._thread = None
            self._lifecycle = (
                AcquisitionLifecycle.CONFIG_REQUIRED
                if self._client is None
                else AcquisitionLifecycle.DISABLED
            )
            should_close = (
                self._client is not None
                and self._close_client_on_stop
                and not self._client_closed
            )
            if should_close:
                self._client_closed = True
        if should_close and self._client is not None:
            self._client.close()
        return True

    def run_once(self, *, now: datetime | None = None) -> AcquisitionCycleResult:
        """Run one deterministic, failure-isolated acquisition cycle."""

        started = self._checked_time(now or self._clock())
        if not self._cycle_lock.acquire(blocking=False):
            raise RuntimeError("a Dhan acquisition cycle is already running")
        try:
            return self._run_once_locked(started)
        finally:
            self._cycle_lock.release()

    def health_snapshot(self) -> dict[str, object]:
        """Return a bounded API-safe mapping containing no credentials or raw payloads."""

        check_time = self._checked_time(self._clock())
        with self._lock:
            markets = {
                symbol: {
                    "last_attempt_at": _iso(health.last_attempt_at),
                    "last_success_at": _iso(health.last_success_at),
                    "data_age_seconds": (
                        None
                        if health.last_success_at is None
                        else max(0.0, (check_time - health.last_success_at).total_seconds())
                    ),
                    "snapshot_id": health.snapshot_id,
                    "accepted": health.accepted,
                    "error_code": health.error_code,
                }
                for symbol, health in self._market_health.items()
            }
            last_cycle = self._last_cycle
            thread = self._thread
            return {
                "component": "dhan_acquisition",
                "lifecycle_state": self._lifecycle.value,
                "configured": self._client is not None,
                "decision_inputs_configured": self._decision_inputs_configured,
                "running": thread is not None and thread.is_alive(),
                "master_batch_id": (
                    None if self._catalog is None else self._catalog.provenance.batch_id
                ),
                "last_master_refresh": _iso(self._last_master_refresh),
                "last_cycle_started_at": _iso(self._last_cycle_started),
                "last_cycle_completed_at": _iso(self._last_cycle_completed),
                "last_success_at": _iso(self._last_success),
                "consecutive_failures": self._consecutive_failures,
                "master_error_code": self._master_error_code,
                "callback_error_code": self._callback_error_code,
                "subscriptions_count": len(self._subscriptions),
                "successful_markets": (
                    0 if last_cycle is None else last_cycle.successful_markets
                ),
                "failed_markets": 0 if last_cycle is None else last_cycle.failed_markets,
                "accepted_markets": (
                    0 if last_cycle is None else last_cycle.accepted_markets
                ),
                "published_markets": (
                    0 if last_cycle is None else last_cycle.published_markets
                ),
                "data_successful_markets": (
                    0 if last_cycle is None else last_cycle.data_successful_markets
                ),
                "markets": markets,
            }

    def subscriptions_snapshot(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return self._subscriptions

    def contracts_snapshot(self) -> Mapping[str, ContractSpec]:
        with self._lock:
            return dict(self._contracts)

    def execution_registry_snapshot(self) -> Mapping[str, Mapping[str, str]]:
        with self._lock:
            return {symbol: dict(registry) for symbol, registry in self._registries.items()}

    def _run_once_locked(self, started: datetime) -> AcquisitionCycleResult:
        with self._lock:
            self._last_cycle_started = started
            if self._client is not None:
                self._lifecycle = AcquisitionLifecycle.INITIALIZING
        if self._client is None:
            results = tuple(
                MarketAcquisitionResult(
                    symbol=symbol,
                    success=False,
                    accepted=False,
                    published=False,
                    error_code="CONFIG_REQUIRED",
                )
                for symbol in self._symbols
            )
            cycle = AcquisitionCycleResult(
                started_at=started,
                completed_at=started,
                configured=False,
                master_batch_id=None,
                markets=results,
                subscriptions=(),
            )
            self._record_cycle(cycle)
            return cycle

        master_error: BaseException | None = None
        if self._master_refresh_due(started):
            try:
                self._refresh_master()
            except Exception as exc:  # noqa: BLE001 - provider boundary is isolated
                master_error = exc
                with self._lock:
                    self._master_error_code = _error_code(exc)
        with self._lock:
            catalog = self._catalog
        if catalog is None:
            results = tuple(
                self._failure(symbol, master_error or RuntimeError("master unavailable"), started)
                for symbol in self._symbols
            )
            cycle = AcquisitionCycleResult(
                started_at=started,
                completed_at=self._checked_time(self._clock()),
                configured=True,
                master_batch_id=None,
                markets=results,
                subscriptions=self.subscriptions_snapshot(),
            )
            self._record_cycle(cycle)
            return cycle

        families: dict[str, Any] = {}
        preflight_failures: dict[str, MarketAcquisitionResult] = {}
        for symbol in self._symbols:
            try:
                families[symbol] = self._verified_family(catalog, symbol, started)
            except Exception as exc:  # noqa: BLE001 - one symbol must not block others
                preflight_failures[symbol] = self._failure(symbol, exc, started)

        technicals_by_symbol: dict[str, CompletedTechnicalResult] = {}
        for symbol, family in tuple(families.items()):
            try:
                technicals_by_symbol[symbol] = self._technicals_for(
                    segment=family.underlying.instrument.segment,
                    security_id=family.underlying.instrument.security_id,
                    instrument=family.historical_instrument,
                    now=self._checked_time(self._clock()),
                )
            except Exception as exc:  # noqa: BLE001 - one symbol must not block others
                preflight_failures[symbol] = self._failure(symbol, exc, started)
                families.pop(symbol, None)

        quote_payload: Mapping[str, Any] | None = None
        quote_received_at: datetime | None = None
        if families:
            request: dict[str, list[str]] = {}
            for family in families.values():
                for record in (family.underlying, family.future):
                    request.setdefault(record.instrument.segment, []).append(
                        record.instrument.security_id
                    )
            request = {
                segment: list(dict.fromkeys(security_ids))
                for segment, security_ids in request.items()
            }
            try:
                quote_payload = self._client.market_quote(request)
                quote_received_at = self._checked_time(self._clock())
            except Exception as exc:  # noqa: BLE001 - provider boundary is isolated
                for symbol in families:
                    preflight_failures[symbol] = self._failure(symbol, exc, started)
                families.clear()

        resolved: dict[str, ResolvedDhanContract] = {}
        quotes: dict[tuple[str, str], Any] = {}
        if quote_payload is not None and quote_received_at is not None:
            for symbol, family in families.items():
                try:
                    underlying_quote = self._quote(
                        quote_payload, family.underlying, quote_received_at, quotes
                    )
                    future_quote = self._quote(
                        quote_payload, family.future, quote_received_at, quotes
                    )
                    pricing = (
                        future_quote.last_price
                        if family.definition.market_kind is MarketKind.COMMODITY
                        else underlying_quote.last_price
                    )
                    if pricing is None or pricing <= 0:
                        raise ValueError("pricing quote is unavailable")
                    resolved[symbol] = family.contract_at(pricing)
                except Exception as exc:  # noqa: BLE001 - one symbol must not block others
                    preflight_failures[symbol] = self._failure(symbol, exc, started)

        self._publish_contract_callbacks(resolved)
        results_by_symbol = dict(preflight_failures)
        for symbol, selected in resolved.items():
            assert quote_payload is not None
            assert quote_received_at is not None
            family = families[symbol]
            try:
                results_by_symbol[symbol] = self._acquire_market(
                    selected,
                    underlying_quote=self._quote(
                        quote_payload, family.underlying, quote_received_at, quotes
                    ),
                    future_quote=self._quote(
                        quote_payload, family.future, quote_received_at, quotes
                    ),
                    technicals=technicals_by_symbol[symbol],
                )
            except Exception as exc:  # noqa: BLE001 - one symbol must not block others
                results_by_symbol[symbol] = self._failure(symbol, exc, started)

        if master_error is not None:
            # The prior catalog remains usable and inevitably ages into the
            # validator/read-model stale lock.  Do not discard good market updates.
            with self._lock:
                self._consecutive_failures = max(1, self._consecutive_failures)
        completed = self._checked_time(self._clock())
        cycle = AcquisitionCycleResult(
            started_at=started,
            completed_at=completed,
            configured=True,
            master_batch_id=catalog.provenance.batch_id,
            markets=tuple(results_by_symbol[symbol] for symbol in self._symbols),
            subscriptions=self.subscriptions_snapshot(),
        )
        self._record_cycle(cycle)
        return cycle

    def _refresh_master(self) -> None:
        assert self._client is not None
        csv_text = self._client.instrument_master(detailed=True)
        fetched_at = self._checked_time(self._clock())
        batch = build_supported_dhan_catalog_batch(
            csv_text,
            fetched_at=fetched_at,
            source_url=self._source_url,
        )
        self._repository.record_instrument_master(batch.provenance, batch.records)
        catalog = DhanInstrumentCatalog(batch)
        with self._lock:
            self._catalog_batch = batch
            self._catalog = catalog
            self._last_master_refresh = fetched_at
            self._master_error_code = None
            self._expiry_cache.clear()
        _LOGGER.info(
            "market_master %s",
            json.dumps(
                {
                    "batch_id": batch.provenance.batch_id,
                    "event": "instrument_master_ready",
                    "fetched_at": fetched_at.isoformat(),
                    "records": len(batch.records),
                },
                sort_keys=True,
            ),
        )

    def _verified_family(
        self,
        catalog: DhanInstrumentCatalog,
        symbol: str,
        now: datetime,
    ) -> Any:
        """Intersect the daily master with Dhan's currently served expiry list."""

        assert self._client is not None
        tentative = catalog.family(symbol, as_of=now)
        cache_key = (
            catalog.provenance.batch_id,
            tentative.option_chain_segment,
            tentative.option_chain_security_id,
            now.astimezone(IST).date(),
        )
        with self._lock:
            expiry_dates = self._expiry_cache.get(cache_key)
        if expiry_dates is None:
            values = self._client.expiry_list(
                underlying_security_id=int(tentative.option_chain_security_id),
                underlying_segment=tentative.option_chain_segment,
            )
            parsed: list[date] = []
            for value in values:
                try:
                    parsed.append(date.fromisoformat(value.strip()[:10]))
                except (AttributeError, ValueError) as exc:
                    raise ValueError("Dhan expiry list contains a non-ISO date") from exc
            expiry_dates = tuple(sorted(set(parsed)))
            if not expiry_dates:
                raise ValueError("Dhan expiry list is empty")
            with self._lock:
                self._expiry_cache[cache_key] = expiry_dates
        return catalog.family(
            symbol,
            as_of=now,
            broker_expiry_dates=expiry_dates,
        )

    def _master_refresh_due(self, now: datetime) -> bool:
        with self._lock:
            previous = self._last_master_refresh
            catalog_missing = self._catalog is None
        return catalog_missing or previous is None or (
            previous.astimezone(IST).date() != now.astimezone(IST).date()
        )

    def _acquire_market(
        self,
        selected: ResolvedDhanContract,
        *,
        underlying_quote: Any,
        future_quote: Any,
        technicals: CompletedTechnicalResult,
    ) -> MarketAcquisitionResult:
        assert self._client is not None
        chain_payload = self._client.option_chain(
            underlying_security_id=int(selected.option_chain_security_id),
            underlying_segment=selected.option_chain_segment,
            expiry=selected.contract.option_expiry.date(),
        )
        chain_received_at = self._checked_time(self._clock())
        if selected.contract.option_expiry <= chain_received_at:
            raise ValueError("option contract expired before chain receipt")
        quote_received_at = min(underlying_quote.received_at, future_quote.received_at)
        if abs((chain_received_at - quote_received_at).total_seconds()) > self._maximum_quote_chain_skew:
            refresh_request: dict[str, list[str]] = {}
            for record in (selected.contract.futures,):
                assert record is not None
                refresh_request.setdefault(record.instrument.segment, []).append(
                    record.instrument.security_id
                )
            underlying_record = InstrumentMasterRecord(
                instrument=selected.contract.underlying,
                display_name=selected.contract.underlying.symbol,
                instrument_type=selected.historical_instrument,
            )
            refresh_request.setdefault(selected.contract.underlying.segment, []).append(
                selected.contract.underlying.security_id
            )
            refresh_request = {
                segment: list(dict.fromkeys(ids)) for segment, ids in refresh_request.items()
            }
            refreshed_payload = self._client.market_quote(refresh_request)
            refreshed_at = self._checked_time(self._clock())
            refresh_cache: dict[tuple[str, str], Any] = {}
            underlying_quote = self._quote(
                refreshed_payload, underlying_record, refreshed_at, refresh_cache
            )
            assert selected.contract.futures is not None
            future_quote = self._quote(
                refreshed_payload,
                selected.contract.futures,
                refreshed_at,
                refresh_cache,
            )
            quote_received_at = refreshed_at
        if abs((chain_received_at - quote_received_at).total_seconds()) > self._maximum_quote_chain_skew:
            raise ValueError("quote and option chain exceed the five-second coherence window")

        calculation_time = self._checked_time(self._clock())
        validate_completed_15m_coverage(
            technicals,
            observed_at=calculation_time,
            exchange_segment=selected.contract.underlying.segment,
        )
        technicals = replace(
            technicals,
            state=replace(technicals.state, observed_at=calculation_time),
        )
        context = self._context_provider(
            selected.symbol,
            selected.contract,
            technicals,
            calculation_time,
        )
        if not isinstance(context, StrategyContext):
            raise TypeError("context provider must return StrategyContext")
        contract = selected.contract
        pricing_quote = (
            future_quote if contract.market_kind is MarketKind.COMMODITY else underlying_quote
        )
        market = MarketState(
            observed_at=min(underlying_quote.observed_at, future_quote.observed_at),
            spot_price=(
                None
                if contract.market_kind is MarketKind.COMMODITY
                else underlying_quote.last_price
            ),
            futures_price=future_quote.last_price,
            previous_close=pricing_quote.previous_close,
            day_open=pricing_quote.day_open,
            day_high=pricing_quote.day_high,
            day_low=pricing_quote.day_low,
            vwap=technicals.session_vwap,
            futures_open_interest=future_quote.open_interest,
        )
        previous = self._repository.latest_previous_option_snapshot(
            contract.contract_key,
            DataSource.DHAN_REST,
        )
        sequence = 1 if previous is None else previous.sequence + 1
        received_at = self._checked_time(self._clock())
        snapshot = build_dhan_snapshot(
            chain_payload,
            sequence=sequence,
            source_timestamp=chain_received_at,
            received_at=received_at,
            contract=contract,
            market=market,
            technicals=technicals.state,
            context=context,
            previous_snapshot=previous,
        )
        ingestion = self._ingestion.ingest(snapshot, now=received_at)
        analysis: SignalPipelineResult | None = None
        try:
            if self._automation is not None:
                automation = self._automation.process(
                    snapshot,
                    ingestion.report,
                    previous_snapshot=previous,
                    now=received_at,
                )
                analysis = automation.signal
            else:
                analysis = self._signals.evaluate(
                    snapshot,
                    ingestion.report,
                    previous_snapshot=previous,
                    now=received_at,
                )
        except Exception:  # noqa: BLE001 - rejected evidence must still be published
            # Invalid/rejected evidence remains useful in the read model; it must
            # never become actionable merely because analytics failed.
            analysis = None
        published = self._read_models.publish(snapshot, ingestion.report, analysis)
        accepted = ingestion.status is SnapshotStatus.ACCEPTED
        data_error_code = (
            None
            if accepted and published
            else "SNAPSHOT_REJECTED"
            if not accepted
            else "READ_MODEL_NOT_PUBLISHED"
        )
        result = MarketAcquisitionResult(
            symbol=selected.symbol,
            success=True,
            accepted=accepted,
            published=published,
            snapshot_id=snapshot.snapshot_id,
            contract_key=contract.contract_key,
            error_code=data_error_code,
        )
        with self._lock:
            health = self._market_health[selected.symbol]
            health.last_attempt_at = received_at
            if result.data_success:
                health.last_success_at = received_at
            health.snapshot_id = snapshot.snapshot_id
            health.accepted = accepted
            health.error_code = data_error_code
        best = (
            next((item for item in analysis.ranked_strikes if item.eligible), None)
            if analysis is not None
            else None
        )
        _LOGGER.info(
            "market_snapshot %s",
            json.dumps(
                {
                    "accepted": accepted,
                    "actionable": bool(
                        analysis is not None
                        and analysis.trade_plan is not None
                        and analysis.trade_plan.actionable
                    ),
                    "best_ask": None if best is None or best.entry_ask is None else str(best.entry_ask),
                    "best_side": None if best is None else best.option_type.value,
                    "best_strike": None if best is None else str(best.strike),
                    "call_score": None if analysis is None else analysis.call_score,
                    "captured_at": snapshot.source_timestamp.isoformat(),
                    "decision": None if analysis is None else analysis.decision.value,
                    "decision_reason": None if analysis is None else analysis.reason,
                    "event": "snapshot_processed",
                    "expiry": contract.option_expiry.isoformat(),
                    "futures": None if market.futures_price is None else str(market.futures_price),
                    "option_legs": len(snapshot.option_chain),
                    "published": published,
                    "put_score": None if analysis is None else analysis.put_score,
                    "sequence": snapshot.sequence,
                    "snapshot_id": snapshot.snapshot_id,
                    "spot": None if market.spot_price is None else str(market.spot_price),
                    "symbol": selected.symbol,
                    "validation_issues": [str(issue.code) for issue in ingestion.report.issues],
                },
                sort_keys=True,
            ),
        )
        return result

    def _technicals_for(
        self,
        *,
        segment: str,
        security_id: str,
        instrument: str,
        now: datetime,
    ) -> CompletedTechnicalResult:
        boundary = expected_completed_15m_boundary(
            now,
            exchange_segment=segment,
        )
        key = (segment, security_id)
        with self._lock:
            cached = self._technical_cache.get(key)
        if cached is not None and cached.boundary == boundary:
            validate_completed_15m_coverage(
                cached.result,
                observed_at=now,
                exchange_segment=segment,
            )
            # Re-stamp the calculation observation time so a newly refreshed
            # coherent snapshot does not misrepresent a closed candle as a live tick.
            state = cached.result.state
            refreshed = CompletedTechnicalResult(
                state=replace(state, observed_at=now),
                session_vwap=cached.result.session_vwap,
                latest_candle=cached.result.latest_candle,
                completed_candle_count=cached.result.completed_candle_count,
            )
            return refreshed
        assert self._client is not None
        payload = self._client.intraday_candles(
            security_id=security_id,
            exchange_segment=segment,
            instrument=instrument,
            interval=15,
            from_datetime=now - timedelta(days=self._history_days),
            to_datetime=now,
            include_open_interest=False,
        )
        observed_at = self._checked_time(self._clock())
        series = normalize_dhan_intraday(payload, interval_minutes=15, as_of=observed_at)
        result = completed_technical_state(series, observed_at=observed_at)
        boundary = validate_completed_15m_coverage(
            result,
            observed_at=observed_at,
            exchange_segment=segment,
        )
        with self._lock:
            self._technical_cache[key] = _TechnicalCacheEntry(
                boundary=boundary,
                result=result,
            )
        return result

    def _quote(
        self,
        payload: Mapping[str, Any] | None,
        record: InstrumentMasterRecord,
        now: datetime,
        cache: dict[tuple[str, str], Any],
    ) -> Any:
        if payload is None:
            raise ValueError("quote payload is unavailable")
        key = (record.instrument.segment, record.instrument.security_id)
        if key not in cache:
            cache[key] = normalize_dhan_market_quote(
                payload,
                segment=key[0],
                security_id=key[1],
                observed_at=now,
                maximum_trade_age=timedelta(
                    seconds=self._maximum_quote_chain_skew
                ),
            )
        return cache[key]

    def _publish_contract_callbacks(
        self, resolved: Mapping[str, ResolvedDhanContract]
    ) -> None:
        subscriptions = _unique_subscriptions(
            item for selected in resolved.values() for item in selected.subscriptions
        )
        contracts = {symbol: selected.contract for symbol, selected in resolved.items()}
        registries = {
            symbol: selected.execution_registry for symbol, selected in resolved.items()
        }
        with self._lock:
            if len(resolved) == len(self._symbols):
                selected_subscriptions = subscriptions
                selected_contracts = contracts
                selected_registries = registries
                self._subscriptions = selected_subscriptions
                self._contracts = selected_contracts
                self._registries = selected_registries
            else:
                # A partial resolution may contain a new contract for one symbol
                # while another symbol still points at an older/expired family.
                # Keep the last complete registry as one coherent generation;
                # before the first complete generation this deliberately stays
                # empty rather than exposing a misleading partial universe.
                selected_subscriptions = self._subscriptions
                selected_contracts = dict(self._contracts)
                selected_registries = dict(self._registries)

        callback_error_code: str | None = None
        callback_attempted = False
        if self._on_subscriptions is not None:
            callback_attempted = True
            try:
                self._on_subscriptions(selected_subscriptions)
            except Exception as exc:  # noqa: BLE001 - injected callback boundary
                callback_error_code = _error_code(exc)
        if self._on_contracts is not None:
            callback_attempted = True
            try:
                self._on_contracts(dict(selected_contracts), dict(selected_registries))
            except Exception as exc:  # noqa: BLE001 - injected callback boundary
                callback_error_code = callback_error_code or _error_code(exc)
        with self._lock:
            if callback_error_code is not None:
                self._callback_error_code = callback_error_code
            elif callback_attempted:
                # Clear a prior delivery error only after a complete callback
                # reconciliation attempt has actually succeeded.
                self._callback_error_code = None

    def _failure(
        self, symbol: str, error: BaseException, now: datetime
    ) -> MarketAcquisitionResult:
        code = _error_code(error)
        with self._lock:
            health = self._market_health[symbol]
            health.last_attempt_at = now
            health.error_code = code
        return MarketAcquisitionResult(
            symbol=symbol,
            success=False,
            accepted=False,
            published=False,
            error_code=code,
        )

    def _record_cycle(self, cycle: AcquisitionCycleResult) -> None:
        with self._lock:
            self._last_cycle = cycle
            self._last_cycle_completed = cycle.completed_at
            if not cycle.configured:
                self._lifecycle = AcquisitionLifecycle.CONFIG_REQUIRED
                self._consecutive_failures += 1
            elif (
                cycle.data_successful_markets == len(cycle.markets)
                and self._master_error_code is None
                and self._callback_error_code is None
            ):
                self._lifecycle = AcquisitionLifecycle.RUNNING
                self._consecutive_failures = 0
                self._last_success = cycle.completed_at
            elif cycle.data_successful_markets:
                self._lifecycle = AcquisitionLifecycle.PARTIAL
                self._consecutive_failures += 1
                self._last_success = cycle.completed_at
            else:
                self._lifecycle = AcquisitionLifecycle.ERROR
                self._consecutive_failures += 1
            lifecycle = self._lifecycle.value
            consecutive_failures = self._consecutive_failures
        _LOGGER.info(
            "market_cycle %s",
            json.dumps(
                {
                    "accepted_markets": cycle.accepted_markets,
                    "completed_at": cycle.completed_at.isoformat(),
                    "configured": cycle.configured,
                    "consecutive_failures": consecutive_failures,
                    "event": "acquisition_cycle",
                    "markets": [
                        {
                            "error_code": result.error_code,
                            "published": result.published,
                            "symbol": result.symbol,
                        }
                        for result in cycle.markets
                    ],
                    "published_markets": cycle.published_markets,
                    "state": lifecycle,
                    "subscriptions": len(cycle.subscriptions),
                },
                sort_keys=True,
            ),
        )

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                cycle = self.run_once()
                failed = cycle.data_successful_markets == 0
            except Exception:  # noqa: BLE001 - daemon supervisor must remain bounded
                failed = True
                with self._lock:
                    self._lifecycle = AcquisitionLifecycle.ERROR
                    self._consecutive_failures += 1
            with self._lock:
                failures = self._consecutive_failures
            delay = self._poll_interval
            if failed:
                delay = min(
                    self._maximum_backoff,
                    self._poll_interval * (2 ** min(max(0, failures - 1), 6)),
                )
            self._stop_event.wait(delay)

    @staticmethod
    def _checked_time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("acquisition clock must return a timezone-aware datetime")
        return value


def build_dhan_acquisition_service(
    *,
    credentials: DhanCredentials | None,
    repository: AcquisitionRepository,
    ingestion_service: SnapshotIngestionService,
    read_models: MarketReadModelStore,
    **kwargs: Any,
) -> DhanAcquisitionService:
    """Factory that remains offline and CONFIG_REQUIRED when credentials are absent."""

    client = None if credentials is None else DhanRestClient(credentials)
    return DhanAcquisitionService(
        client=client,
        repository=repository,
        ingestion_service=ingestion_service,
        read_models=read_models,
        **kwargs,
    )


def _unique_subscriptions(
    values: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for segment, security_id in values:
        selected = (str(segment).strip().upper(), str(security_id).strip())
        if selected not in seen:
            seen.add(selected)
            result.append(selected)
    return tuple(result)


def _error_code(error: BaseException) -> str:
    # Deliberately omit exception messages: transports/callbacks are injectable
    # and an unknown implementation could place a credential in its error text.
    name = type(error).__name__.strip() or "ERROR"
    return "".join(character if character.isalnum() else "_" for character in name).upper()[:64]


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
