"""Composition root for the locally hosted TECO Quant backend."""

from __future__ import annotations

from _thread import RLock as RLockType
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Literal, Self

from teco_quant.api import (
    ApiConfig,
    FeedHealthProvider,
    JsonWSGIApp,
    MarketReadModelStore,
)
from teco_quant.automation import SignalHistoryRepository
from teco_quant.config import RuntimeSettings
from teco_quant.execution import ExecutionController, ExecutionLedger, ExecutionPolicy
from teco_quant.execution.models import ExecutionMode
from teco_quant.persistence.sqlite import SQLiteRepository
from teco_quant.strategy.spec import DEFAULT_STRATEGY_CONFIG


@dataclass(slots=True)
class BackendRuntime:
    """Owned backend resources and the WSGI application that uses them."""

    settings: RuntimeSettings
    repository: SQLiteRepository
    execution_ledger: ExecutionLedger
    signal_history: SignalHistoryRepository
    market_read_model: MarketReadModelStore
    controller: ExecutionController
    application: JsonWSGIApp
    _additional_cleanup: list[Callable[[], None]] = field(
        default_factory=list, init=False, repr=False
    )
    _market_data_owner: object | None = field(default=None, init=False, repr=False)
    _close_lock: RLockType = field(default_factory=RLock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def add_cleanup(self, callback: Callable[[], None]) -> None:
        """Register an integration resource to close before the databases."""

        with self._close_lock:
            if self._closed:
                raise RuntimeError("backend runtime is already closed")
            self._additional_cleanup.append(callback)

    def register_market_data_integration(
        self,
        owner: object,
        cleanup: Callable[[], None],
    ) -> None:
        """Claim the one market-data slot and register its producer cleanup atomically."""

        with self._close_lock:
            if self._closed:
                raise RuntimeError("backend runtime is already closed")
            if self._market_data_owner is not None:
                raise RuntimeError("backend runtime already has a market-data integration")
            self._market_data_owner = owner
            self._additional_cleanup.append(cleanup)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return

            # Producers must be gone before their read models and databases. If any
            # integration cannot stop, keep every dependency open and retain only the
            # failed cleanup callbacks so a later close call can retry safely.
            cleanup_errors: list[BaseException] = []
            failed_indexes: set[int] = set()
            for index in range(len(self._additional_cleanup) - 1, -1, -1):
                try:
                    self._additional_cleanup[index]()
                except BaseException as exc:  # noqa: BLE001 - preserve all cleanup failures
                    cleanup_errors.append(exc)
                    failed_indexes.add(index)
            self._additional_cleanup = [
                callback
                for index, callback in enumerate(self._additional_cleanup)
                if index in failed_indexes
            ]
            if cleanup_errors:
                raise RuntimeError(
                    "one or more backend producers failed to stop; dependencies remain open"
                ) from cleanup_errors[0]

            errors: list[BaseException] = []
            for callback in (
                self.market_read_model.close,
                self.signal_history.close,
                self.execution_ledger.close,
                self.repository.close,
            ):
                try:
                    callback()
                except BaseException as exc:  # noqa: BLE001 - close independent resources
                    errors.append(exc)
            if errors:
                raise RuntimeError(
                    "one or more backend resources failed to close"
                ) from errors[0]
            self._closed = True

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("backend runtime is already closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, exc, traceback
        self.close()
        return False


def create_runtime(
    settings: RuntimeSettings,
    *,
    feed_health: FeedHealthProvider | None = None,
) -> BackendRuntime:
    """Create all repositories, the fail-closed controller, and the JSON app.

    No Dhan client or live-order gateway is constructed here. Even ``PAPER_TRADING``
    remains routed to the deterministic local broker.
    """

    if settings.execution_mode not in {"DATA_ONLY", "PAPER_TRADING"}:
        raise ValueError("backend runtime refuses live execution modes")
    if "*" in settings.allowed_origins:
        raise ValueError("backend runtime refuses wildcard CORS origins")
    _validate_database_paths(settings)
    for database in (
        settings.database_path,
        settings.execution_database_path,
        settings.signal_history_database_path,
    ):
        _prepare_database_directory(database)

    repository: SQLiteRepository | None = None
    execution_ledger: ExecutionLedger | None = None
    signal_history: SignalHistoryRepository | None = None
    market_read_model: MarketReadModelStore | None = None
    try:
        repository = SQLiteRepository(settings.database_path)
        repository.publish_strategy_config(DEFAULT_STRATEGY_CONFIG)
        execution_ledger = ExecutionLedger(settings.execution_database_path)
        signal_history = SignalHistoryRepository(settings.signal_history_database_path)
        market_read_model = MarketReadModelStore()
        policy = ExecutionPolicy(
            mode=ExecutionMode(settings.execution_mode),
            max_data_age_seconds=settings.live_max_age_seconds,
            live_enabled=False,
        )
        controller = ExecutionController(
            ledger=execution_ledger,
            instrument_registry={},
            policy=policy,
            live_gateway=None,
        )
        application = JsonWSGIApp(
            controller,
            ApiConfig(
                api_key=settings.api_key,
                allowed_origins=settings.allowed_origins,
                max_body_bytes=settings.api_max_body_bytes,
            ),
            signal_history=signal_history,
            market_reader=market_read_model,
            feed_health=feed_health or (lambda: _default_feed_health(settings)),
        )
        return BackendRuntime(
            settings=settings,
            repository=repository,
            execution_ledger=execution_ledger,
            signal_history=signal_history,
            market_read_model=market_read_model,
            controller=controller,
            application=application,
        )
    except BaseException:
        if market_read_model is not None:
            with suppress(BaseException):
                market_read_model.close()
        if signal_history is not None:
            with suppress(BaseException):
                signal_history.close()
        if execution_ledger is not None:
            with suppress(BaseException):
                execution_ledger.close()
        if repository is not None:
            with suppress(BaseException):
                repository.close()
        raise


def _default_feed_health(settings: RuntimeSettings) -> dict[str, object]:
    enabled = settings.dhan_live_enabled
    return {
        "configured": False,
        "state": "CONFIG_REQUIRED" if enabled else "DISABLED",
        "connected": False,
        "healthy": False,
        "last_error": None,
    }


def _prepare_database_directory(database: str) -> None:
    if database == ":memory:":
        return
    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _validate_database_paths(settings: RuntimeSettings) -> None:
    paths = (
        settings.database_path,
        settings.execution_database_path,
        settings.signal_history_database_path,
    )
    resolved = [
        str(Path(path).expanduser().resolve()).casefold()
        for path in paths
        if path != ":memory:"
    ]
    if len(resolved) != len(set(resolved)):
        raise ValueError(
            "market, execution, and signal-history databases must use distinct paths"
        )
