"""Runtime composition for read-only Dhan REST and WebSocket market data.

This module is deliberately separate from execution.  It can discover contracts, ingest
and publish validated snapshots, and cache live ticks, but it has no broker order method
and never installs a live execution gateway.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from threading import Lock, RLock
from typing import Protocol

from teco_quant.automation import TecoAutomationService
from teco_quant.brokers import (
    BoundedBackoff,
    DhanFeedSupervisorConfig,
    DhanLiveFeedSupervisor,
)
from teco_quant.brokers.dhan import DhanCredentials
from teco_quant.domain.models import ContractSpec, StrategyContext
from teco_quant.ingestion.dhan_acquisition import (
    ExplicitStrategyInputs,
    build_dhan_acquisition_service,
)
from teco_quant.ingestion.dhan_historical import CompletedTechnicalResult
from teco_quant.ingestion.service import SnapshotIngestionService
from teco_quant.ingestion.validation import SnapshotValidator
from teco_quant.runtime import BackendRuntime

_LOGGER = logging.getLogger(__name__)


class AcquisitionRuntime(Protocol):
    """Lifecycle and health surface required from the REST acquisition worker."""

    def start(self) -> bool: ...

    def stop(self, *, timeout: float = 15.0) -> bool: ...

    def health_snapshot(self) -> Mapping[str, object]: ...


class LiveFeedRuntime(Protocol):
    """Lifecycle and health surface required from the supervised WebSocket feed."""

    def start(self) -> None: ...

    def stop(self, timeout_seconds: float | None = None) -> bool: ...

    def replace_instruments(
        self, instruments: Sequence[tuple[str, str]]
    ) -> bool: ...

    def instruments_healthy(
        self, instruments: Sequence[tuple[str, str]]
    ) -> bool: ...

    def health_snapshot(self) -> object: ...


AcquisitionFactory = Callable[..., AcquisitionRuntime]
LiveFeedFactory = Callable[..., LiveFeedRuntime]


class BackendMarketDataService:
    """Own Dhan's read-only acquisition and live-feed workers as one integration.

    The acquisition worker starts first.  Once it resolves the daily instrument master,
    its callback starts (or atomically rotates) the WebSocket subscription.  Shutdown uses
    the reverse dependency order and is idempotent.
    """

    def __init__(
        self,
        runtime: BackendRuntime,
        *,
        credentials: DhanCredentials | None,
        acquisition_factory: AcquisitionFactory = build_dhan_acquisition_service,
        live_feed_factory: LiveFeedFactory = DhanLiveFeedSupervisor,
    ) -> None:
        self._runtime = runtime
        self._settings = runtime.settings
        self._credentials = credentials
        self._live_feed_factory = live_feed_factory
        self._lock = RLock()
        self._lifecycle_lock = Lock()
        self._live_feed: LiveFeedRuntime | None = None
        self._explicit_inputs: ExplicitStrategyInputs | None = None
        self._started = False
        self._stopping = False
        self._closed = False

        validator = SnapshotValidator(
            change_oi_reference_loader=runtime.repository.accepted_option_snapshot
        )
        ingestion = SnapshotIngestionService(
            validator=validator,
            repository=runtime.repository,
        )
        automation = TecoAutomationService(
            execution_controller=runtime.controller,
            signal_history=runtime.signal_history,
        )
        configured_inputs = self._settings.strategy_inputs
        if configured_inputs is not None:
            self._explicit_inputs = ExplicitStrategyInputs(
                account_capital=configured_inputs.account_capital,
                risk_per_trade=configured_inputs.risk_per_trade,
                maximum_premium_allocation=(
                    configured_inputs.maximum_premium_allocation
                ),
                event_risk_active=configured_inputs.event_risk_active,
                expected_holding_hours=configured_inputs.expected_holding_hours,
                operating_mode=configured_inputs.operating_mode,
                trading_style=configured_inputs.trading_style,
                price_action_confirmed=configured_inputs.price_action_confirmed,
            )
        context_provider = (
            None if self._explicit_inputs is None else self._live_gated_context
        )
        # Passing no credentials is intentional: the acquisition object then exposes a
        # fail-closed CONFIG_REQUIRED health state without opening a network connection.
        self._acquisition = acquisition_factory(
            credentials=(
                credentials if self._settings.dhan_live_enabled else None
            ),
            repository=runtime.repository,
            ingestion_service=ingestion,
            read_models=runtime.market_read_model,
            leader_store=runtime.market_leaders,
            automation_service=automation,
            context_provider=context_provider,
            on_subscriptions=self._replace_subscriptions,
            on_contracts=self._replace_contracts,
        )

    @property
    def acquisition(self) -> AcquisitionRuntime:
        """Expose the owned acquisition service for diagnostics and acceptance tests."""

        return self._acquisition

    def start(self) -> bool:
        """Start market data when enabled and configured; otherwise remain offline."""

        with self._lifecycle_lock, self._lock:
            if self._closed:
                raise RuntimeError("Dhan market-data integration is closed")
            if self._started:
                return False
            if not self._settings.dhan_live_enabled:
                _LOGGER.info(
                    'market_runtime %s',
                    json.dumps(
                        {"event": "market_data_start", "state": "DISABLED"},
                        sort_keys=True,
                    ),
                )
                return False
            if self._credentials is None:
                _LOGGER.info(
                    'market_runtime %s',
                    json.dumps(
                        {"event": "market_data_start", "state": "CONFIG_REQUIRED"},
                        sort_keys=True,
                    ),
                )
                return False
            self._started = True
            self._stopping = False
            try:
                started = self._acquisition.start()
            except BaseException:
                self._started = False
                raise
            if not started:
                self._started = False
            _LOGGER.info(
                "market_runtime %s",
                json.dumps(
                    {
                        "decision_inputs_configured": self._explicit_inputs is not None,
                        "event": "market_data_start",
                        "state": "INITIALIZING" if started else "START_FAILED",
                    },
                    sort_keys=True,
                ),
            )
            return started

    def stop(self) -> None:
        """Stop REST acquisition then the socket; report incomplete worker shutdown."""

        with self._lifecycle_lock:
            with self._lock:
                if self._closed:
                    return
                self._stopping = True
                feed = self._live_feed

            errors: list[str] = []
            acquisition_stopped = False
            feed_stopped = feed is None
            try:
                acquisition_stopped = self._acquisition.stop(timeout=15.0)
                if not acquisition_stopped:
                    errors.append("Dhan acquisition worker did not stop")
            except Exception:  # noqa: BLE001 - continue stopping independent resources
                errors.append("Dhan acquisition worker failed during shutdown")
            try:
                if feed is not None:
                    feed_stopped = feed.stop(timeout_seconds=5.0)
                    if not feed_stopped:
                        errors.append("Dhan live-feed worker did not stop")
            except Exception:  # noqa: BLE001 - continue stopping independent resources
                errors.append("Dhan live-feed worker failed during shutdown")

            with self._lock:
                if acquisition_stopped and feed_stopped:
                    self._started = False
                    self._live_feed = None
                    self._closed = True
                    self._stopping = False
                # On failure, retain worker handles and the stopping state. A later
                # ``stop`` call can retry; callbacks remain suppressed meanwhile.
            if errors:
                raise RuntimeError("; ".join(errors))
            _LOGGER.info(
                "market_runtime %s",
                json.dumps(
                    {"event": "market_data_stop", "state": "STOPPED"},
                    sort_keys=True,
                ),
            )

    def health_snapshot(self) -> dict[str, object]:
        """Combine REST and WebSocket health without exposing credentials or payloads."""

        if not self._settings.dhan_live_enabled:
            return _disabled_health()

        acquisition = _mapping_health(self._acquisition.health_snapshot())
        with self._lock:
            feed = self._live_feed
        socket = {} if feed is None else _object_health(feed.health_snapshot())
        lifecycle = _safe_text(acquisition.get("lifecycle_state"), "INITIALIZING")
        socket_state = _safe_text(
            socket.get("state"), "STOPPED" if feed is None else "INITIALIZING"
        )

        if self._credentials is None or acquisition.get("configured") is False:
            state = "CONFIG_REQUIRED"
        elif lifecycle in {"ERROR", "PARTIAL", "INITIALIZING"}:
            state = lifecycle
        elif lifecycle == "RUNNING" and socket.get("healthy") is True:
            state = "RUNNING"
        elif lifecycle == "RUNNING":
            state = socket_state
        else:
            state = lifecycle

        decision_inputs_configured = (
            acquisition.get("decision_inputs_configured") is True
        )
        transport_healthy = socket.get("healthy") is True
        successful_data = acquisition.get(
            "data_successful_markets",
            acquisition.get("successful_markets"),
        )
        data_healthy = bool(
            lifecycle in {"RUNNING", "PARTIAL"}
            and isinstance(successful_data, int)
            and not isinstance(successful_data, bool)
            and successful_data > 0
        )

        result: dict[str, object] = {
            "provider": "DHAN",
            "configured": self._credentials is not None,
            "state": state,
            "acquisition_state": lifecycle,
            "socket_state": socket_state,
            "connected": socket.get("connected") is True,
            "transport_healthy": transport_healthy,
            "data_healthy": data_healthy,
            "healthy": data_healthy and transport_healthy,
            "decision_inputs_configured": decision_inputs_configured,
            "actionable_ready": (
                data_healthy and transport_healthy and decision_inputs_configured
            ),
        }
        for name in (
            "master_batch_id",
            "last_master_refresh",
            "last_master_attempt",
            "last_cycle_started_at",
            "last_cycle_completed_at",
            "last_success_at",
            "master_error_code",
            "callback_error_code",
        ):
            result[name] = acquisition.get(name)
        for name in (
            "subscriptions_count",
            "successful_markets",
            "accepted_markets",
            "published_markets",
            "data_successful_markets",
            "failed_markets",
        ):
            result[name] = acquisition.get(name)
        for name in (
            "generation",
            "expected_instruments",
            "ready_instruments",
            "subscription_batches",
            "packets_received",
            "packets_rejected",
            "reconnect_count",
            "consecutive_failures",
            "last_connected_at",
            "last_packet_at",
            "packet_age_seconds",
            "last_trade_at",
            "trade_age_seconds",
            "last_heartbeat_at",
            "last_healthy_at",
            "trade_timestamp_rejections",
            "replayed_packets",
            "market_status_packets",
            "market_status",
            "market_status_known",
            "market_open",
            "last_market_status_at",
            "next_retry_seconds",
            "missing_instruments",
            "last_error",
        ):
            if name in socket:
                result[name] = socket[name]
        markets = acquisition.get("markets")
        if isinstance(markets, Mapping):
            result["markets"] = markets
        return result

    def _replace_subscriptions(
        self, subscriptions: tuple[tuple[str, str], ...]
    ) -> None:
        detached_feed: LiveFeedRuntime | None = None
        with self._lock:
            if self._stopping or self._closed or self._credentials is None:
                return
            feed = self._live_feed
            if not subscriptions:
                # An empty generation is meaningful after contract rollover or when
                # every cached resolution has expired. Detach under the state lock so
                # no obsolete feed remains authoritative, then stop it without holding
                # the lock used by lifecycle and future subscription callbacks.
                if feed is None:
                    return
                self._live_feed = None
                detached_feed = feed
            elif feed is not None:
                changed = feed.replace_instruments(subscriptions)
                _LOGGER.info(
                    "market_feed %s",
                    json.dumps(
                        {
                            "event": "subscriptions_reconciled",
                            "instruments": len(subscriptions),
                            "rotated": changed,
                        },
                        sort_keys=True,
                    ),
                )
                return
            else:
                feed = self._live_feed_factory(
                    self._credentials,
                    subscriptions,
                    on_packet=self._runtime.market_read_model.publish_feed_tick,
                    config=DhanFeedSupervisorConfig(
                        idle_reconnect_seconds=self._settings.feed_idle_timeout_seconds,
                        data_stale_seconds=self._settings.live_max_age_seconds,
                    ),
                    backoff=BoundedBackoff(
                        maximum_seconds=self._settings.feed_reconnect_max_seconds
                    ),
                )
                self._live_feed = feed
                # Start while retaining the state lock. Shutdown can only capture this
                # feed after start returns, eliminating an orphan start-after-stop race.
                try:
                    feed.start()
                    _LOGGER.info(
                        "market_feed %s",
                        json.dumps(
                            {
                                "event": "websocket_start",
                                "instruments": len(subscriptions),
                                "state": "INITIALIZING",
                            },
                            sort_keys=True,
                        ),
                    )
                except BaseException:
                    if self._live_feed is feed:
                        self._live_feed = None
                    raise
                return

        assert detached_feed is not None
        try:
            stopped = detached_feed.stop(timeout_seconds=5.0)
        except BaseException:
            with self._lock:
                if (
                    self._live_feed is None
                    and not self._stopping
                    and not self._closed
                ):
                    self._live_feed = detached_feed
            raise
        if not stopped:
            # Keep the handle reachable for a later shutdown/retry when no newer
            # subscription generation claimed the slot in the meantime.
            with self._lock:
                if (
                    self._live_feed is None
                    and not self._stopping
                    and not self._closed
                ):
                    self._live_feed = detached_feed
            raise RuntimeError("obsolete Dhan live-feed worker did not stop")
        _LOGGER.info(
            "market_feed %s",
            json.dumps(
                {
                    "event": "subscriptions_cleared",
                    "instruments": 0,
                    "state": "STOPPED",
                },
                sort_keys=True,
            ),
        )

    def _replace_contracts(
        self,
        contracts: Mapping[str, ContractSpec],
        registries: Mapping[str, Mapping[str, str]],
    ) -> None:
        del contracts
        combined: dict[str, str] = {}
        for symbol in sorted(registries):
            for instrument_symbol, security_id in registries[symbol].items():
                current = combined.get(instrument_symbol)
                if current is not None and current != security_id:
                    raise ValueError("Dhan contracts contain a conflicting security ID")
                combined[instrument_symbol] = security_id
        self._runtime.controller.replace_instrument_registry(combined)

    def _live_gated_context(
        self,
        symbol: str,
        contract: ContractSpec,
        technicals: CompletedTechnicalResult,
        now: datetime,
    ) -> StrategyContext:
        """Apply explicit risk inputs only while the exact live generation is healthy."""

        configured = self._explicit_inputs
        if configured is None:
            raise RuntimeError("explicit strategy inputs are not configured")
        context = configured(symbol, contract, technicals, now)
        with self._lock:
            feed = self._live_feed
        healthy = False
        if feed is not None:
            try:
                future = contract.futures
                if future is not None:
                    required = (
                        (
                            contract.underlying.segment,
                            contract.underlying.security_id,
                        ),
                        (
                            future.instrument.segment,
                            future.instrument.security_id,
                        ),
                        *(
                            (
                                record.instrument.segment,
                                record.instrument.security_id,
                            )
                            for record in contract.option_contracts
                        ),
                    )
                    checker = getattr(feed, "instruments_healthy", None)
                    if callable(checker):
                        healthy = checker(required) is True
                    else:
                        # Preserve fail-closed compatibility with injected legacy
                        # supervisors while production uses contract-scoped health.
                        healthy = (
                            _object_health(feed.health_snapshot()).get("healthy")
                            is True
                        )
            except Exception:  # noqa: BLE001 - feed health must gate fail-closed
                healthy = False
        if healthy:
            return context
        return replace(
            context,
            event_risk_active=None,
            price_action_confirmed=None,
        )


def attach_dhan_market_data(
    runtime: BackendRuntime,
    *,
    credentials: DhanCredentials | None,
    acquisition_factory: AcquisitionFactory = build_dhan_acquisition_service,
    live_feed_factory: LiveFeedFactory = DhanLiveFeedSupervisor,
) -> BackendMarketDataService:
    """Attach read-only Dhan workers to an existing runtime and start when possible."""

    integration = BackendMarketDataService(
        runtime,
        credentials=credentials,
        acquisition_factory=acquisition_factory,
        live_feed_factory=live_feed_factory,
    )
    try:
        runtime.register_market_data_integration(integration, integration.stop)
    except BaseException:
        # Construction may own an HTTP transport even though no worker has started.
        # Close it when the runtime slot was already claimed or the runtime is closed.
        integration.stop()
        raise
    runtime.application.feed_health = integration.health_snapshot
    integration.start()
    return integration


def _disabled_health() -> dict[str, object]:
    return {
        "provider": "DHAN",
        "configured": False,
        "state": "DISABLED",
        "acquisition_state": "DISABLED",
        "socket_state": "STOPPED",
        "connected": False,
        "healthy": False,
        "transport_healthy": False,
        "data_healthy": False,
        "decision_inputs_configured": False,
        "actionable_ready": False,
    }


def _mapping_health(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("acquisition health must be a mapping")
    return value


def _object_health(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    converter = getattr(value, "as_dict", None)
    converted = converter() if callable(converter) else None
    if not isinstance(converted, Mapping):
        raise TypeError("live-feed health must be an API-safe mapping")
    return converted


def _safe_text(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
