"""Supervised DhanHQ v2 live-market feed.

The coordinator in this module is deliberately read-only.  It owns connection lifecycle,
subscription replay, packet validation, and fail-closed health; consumers own the creation
of atomic market snapshots from accepted packets.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from importlib import import_module
from math import isfinite
from struct import unpack_from
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from typing import Any, Protocol

from teco_quant.brokers.dhan import (
    DhanCredentials,
    DhanFeedMode,
    DhanFeedPacket,
    decode_feed_packet,
    live_feed_url,
    subscription_messages,
)

__all__ = [
    "DHAN_EXCHANGE_SEGMENT_CODES",
    "DHAN_MAX_INSTRUMENTS_PER_CONNECTION",
    "BoundedBackoff",
    "DhanFeedConsumerError",
    "DhanFeedDependencyError",
    "DhanFeedDisconnected",
    "DhanFeedHealth",
    "DhanFeedIdleError",
    "DhanFeedInstrument",
    "DhanFeedProtocolError",
    "DhanFeedReceiveTimeout",
    "DhanFeedSocket",
    "DhanFeedState",
    "DhanFeedSupervisorConfig",
    "DhanFeedTransport",
    "DhanFeedTransportError",
    "DhanLiveFeedError",
    "DhanLiveFeedSupervisor",
    "DhanMarketStatus",
    "WebsocketsSyncTransport",
    "decode_feed_message",
]


DHAN_MAX_INSTRUMENTS_PER_CONNECTION = 5_000
_PRIMARY_PRICE_RESPONSE_CODES = frozenset((2, 4, 8))
DHAN_EXCHANGE_SEGMENT_CODES: Mapping[str, int] = {
    "IDX_I": 0,
    "NSE_EQ": 1,
    "NSE_FNO": 2,
    "NSE_CURRENCY": 3,
    "BSE_EQ": 4,
    "MCX_COMM": 5,
    "BSE_CURRENCY": 7,
    "BSE_FNO": 8,
}


class _SilentTransportLogger:
    """Logger-like sink that prevents a credential-bearing URI reaching log handlers."""

    def isEnabledFor(self, level: int) -> bool:
        del level
        return False

    def debug(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    info = debug
    warning = debug
    error = debug
    exception = debug
    critical = debug
    log = debug


_TRANSPORT_LOGGER = _SilentTransportLogger()


class DhanLiveFeedError(RuntimeError):
    """Base class for sanitized live-feed failures."""


class DhanFeedDependencyError(DhanLiveFeedError):
    pass


class DhanFeedTransportError(DhanLiveFeedError):
    pass


class DhanFeedReceiveTimeout(DhanLiveFeedError):
    """A normal receive poll timeout, not necessarily a failed connection."""


class DhanFeedIdleError(DhanLiveFeedError):
    pass


class DhanFeedProtocolError(DhanLiveFeedError):
    pass


class DhanFeedConsumerError(DhanLiveFeedError):
    pass


class DhanFeedDisconnected(DhanLiveFeedError):
    def __init__(self, provider_code: int) -> None:
        super().__init__("Dhan closed the live-market feed")
        self.provider_code = provider_code


class DhanFeedSocket(Protocol):
    """Minimal synchronous socket contract used by the supervisor."""

    def send_text(self, message: str) -> None: ...

    def receive(self, timeout_seconds: float) -> bytes | str: ...

    def ping(self, timeout_seconds: float) -> None: ...

    def close(self) -> None: ...


class DhanFeedTransport(Protocol):
    """Injectable connection factory; fakes implement this contract in tests."""

    def connect(self, url: str) -> DhanFeedSocket: ...


class _WebsocketsSyncSocket:
    """Adapter around ``websockets.sync.client.ClientConnection``."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def send_text(self, message: str) -> None:
        try:
            self._connection.send(message)
        except Exception:  # noqa: BLE001 - dependency exceptions are deliberately sanitized
            raise DhanFeedTransportError("Dhan live-feed send failed") from None

    def receive(self, timeout_seconds: float) -> bytes | str:
        try:
            message: object = self._connection.recv(timeout=timeout_seconds)
        except TimeoutError:
            raise DhanFeedReceiveTimeout("Dhan live-feed receive poll timed out") from None
        except Exception:  # noqa: BLE001 - dependency exceptions are deliberately sanitized
            raise DhanFeedTransportError("Dhan live-feed receive failed") from None
        if not isinstance(message, (bytes, str)):
            raise DhanFeedProtocolError("Dhan live-feed returned an unsupported message type")
        return message

    def ping(self, timeout_seconds: float) -> None:
        try:
            pong = self._connection.ping()
            if not pong.wait(timeout_seconds):
                raise DhanFeedTransportError("Dhan live-feed heartbeat timed out")
        except DhanFeedTransportError:
            raise
        except Exception:  # noqa: BLE001 - dependency exceptions are deliberately sanitized
            raise DhanFeedTransportError("Dhan live-feed heartbeat failed") from None

    def close(self) -> None:
        try:
            self._connection.close(code=1000, reason="service shutdown")
        except Exception:  # noqa: BLE001 - best-effort close accepts any dependency failure
            # Closing is best effort; callers have already made health fail closed.
            return


@dataclass(frozen=True, slots=True)
class WebsocketsSyncTransport:
    """Concrete production transport using ``websockets.sync.client.connect``.

    Importing the optional dependency is delayed until connection time, keeping offline
    tooling and tests credential- and network-free.
    """

    open_timeout_seconds: float = 10.0
    close_timeout_seconds: float = 5.0
    ping_interval_seconds: float = 10.0
    ping_timeout_seconds: float = 10.0
    max_message_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in {
            "open_timeout_seconds": self.open_timeout_seconds,
            "close_timeout_seconds": self.close_timeout_seconds,
            "ping_interval_seconds": self.ping_interval_seconds,
            "ping_timeout_seconds": self.ping_timeout_seconds,
        }.items():
            _require_positive_finite(name, value)
        if self.max_message_bytes < 162:
            raise ValueError("max_message_bytes must accommodate a full Dhan packet")

    def connect(self, url: str) -> DhanFeedSocket:
        try:
            module = import_module("websockets.sync.client")
        except ImportError:
            raise DhanFeedDependencyError(
                "Dhan live feed requires the 'websockets>=17.0.1,<18' package"
            ) from None
        try:
            connection = module.connect(
                url,
                open_timeout=self.open_timeout_seconds,
                close_timeout=self.close_timeout_seconds,
                ping_interval=self.ping_interval_seconds,
                ping_timeout=self.ping_timeout_seconds,
                max_size=self.max_message_bytes,
                compression=None,
                logger=_TRANSPORT_LOGGER,
            )
        except Exception:  # noqa: BLE001 - raw dependency errors may contain the secret URI
            # Never surface the authenticated URL carried by provider/library exceptions.
            raise DhanFeedTransportError("Dhan live-feed connection failed") from None
        return _WebsocketsSyncSocket(connection)


@dataclass(frozen=True, slots=True, order=True)
class DhanFeedInstrument:
    exchange_segment: str
    security_id: str

    @property
    def segment_code(self) -> int:
        return DHAN_EXCHANGE_SEGMENT_CODES[self.exchange_segment]

    @property
    def label(self) -> str:
        return f"{self.exchange_segment}:{self.security_id}"


class DhanFeedState(str, Enum):
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    SUBSCRIBING = "SUBSCRIBING"
    WARMING = "WARMING"
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    BACKING_OFF = "BACKING_OFF"
    STOPPING = "STOPPING"


class DhanMarketStatus(str, Enum):
    """Normalized market status when a provider packet carries an explicit value."""

    UNKNOWN = "UNKNOWN"
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class DhanFeedHealth:
    state: DhanFeedState
    connected: bool
    healthy: bool
    generation: int
    expected_instruments: int
    ready_instruments: int
    missing_instruments: tuple[str, ...]
    subscription_batches: int
    packets_received: int
    packets_rejected: int
    trade_timestamp_rejections: int
    replayed_packets: int
    market_status_packets: int
    reconnect_count: int
    consecutive_failures: int
    last_connected_at: datetime | None
    last_packet_at: datetime | None
    packet_age_seconds: float | None
    last_trade_at: datetime | None
    trade_age_seconds: float | None
    market_status: DhanMarketStatus
    market_status_known: bool
    market_open: bool | None
    last_market_status_at: datetime | None
    last_heartbeat_at: datetime | None
    last_healthy_at: datetime | None
    next_retry_seconds: float | None
    last_error: str | None

    def as_dict(self) -> dict[str, object]:
        """Return an API-safe snapshot with ISO-8601 timestamps and no credentials."""

        return {
            "state": self.state.value,
            "connected": self.connected,
            "healthy": self.healthy,
            "generation": self.generation,
            "expected_instruments": self.expected_instruments,
            "ready_instruments": self.ready_instruments,
            "missing_instruments": list(self.missing_instruments),
            "subscription_batches": self.subscription_batches,
            "packets_received": self.packets_received,
            "packets_rejected": self.packets_rejected,
            "trade_timestamp_rejections": self.trade_timestamp_rejections,
            "replayed_packets": self.replayed_packets,
            "market_status_packets": self.market_status_packets,
            "reconnect_count": self.reconnect_count,
            "consecutive_failures": self.consecutive_failures,
            "last_connected_at": _isoformat(self.last_connected_at),
            "last_packet_at": _isoformat(self.last_packet_at),
            "packet_age_seconds": self.packet_age_seconds,
            "last_trade_at": _isoformat(self.last_trade_at),
            "trade_age_seconds": self.trade_age_seconds,
            "market_status": self.market_status.value,
            "market_status_known": self.market_status_known,
            "market_open": self.market_open,
            "last_market_status_at": _isoformat(self.last_market_status_at),
            "last_heartbeat_at": _isoformat(self.last_heartbeat_at),
            "last_healthy_at": _isoformat(self.last_healthy_at),
            "next_retry_seconds": self.next_retry_seconds,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class DhanFeedSupervisorConfig:
    receive_poll_seconds: float = 1.0
    heartbeat_interval_seconds: float = 10.0
    heartbeat_timeout_seconds: float = 5.0
    idle_reconnect_seconds: float = 45.0
    data_stale_seconds: float = 30.0
    future_skew_seconds: float = 2.0
    stop_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        positive = {
            "receive_poll_seconds": self.receive_poll_seconds,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
            "idle_reconnect_seconds": self.idle_reconnect_seconds,
            "data_stale_seconds": self.data_stale_seconds,
            "stop_timeout_seconds": self.stop_timeout_seconds,
        }
        for name, value in positive.items():
            _require_positive_finite(name, value)
        if not isfinite(self.future_skew_seconds) or self.future_skew_seconds < 0:
            raise ValueError("future_skew_seconds must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class BoundedBackoff:
    base_seconds: float = 1.0
    maximum_seconds: float = 30.0
    jitter: Callable[[float, float], float] = random.uniform

    def __post_init__(self) -> None:
        if not isfinite(self.base_seconds) or self.base_seconds < 0:
            raise ValueError("backoff base cannot be negative")
        if not isfinite(self.maximum_seconds):
            raise ValueError("backoff maximum must be finite")
        if self.maximum_seconds < self.base_seconds:
            raise ValueError("backoff maximum cannot be less than its base")

    def delay(self, consecutive_failure: int) -> float:
        """Equal-jitter exponential delay, always bounded by ``maximum_seconds``."""

        if consecutive_failure < 1:
            raise ValueError("consecutive_failure must be at least one")
        cap = min(
            self.maximum_seconds,
            self.base_seconds * (2 ** min(consecutive_failure - 1, 30)),
        )
        if cap == 0:
            return 0.0
        half = cap / 2
        jitter_value = self.jitter(0.0, half)
        return float(min(cap, max(half, half + jitter_value)))


PacketConsumer = Callable[[DhanFeedPacket], bool | None]
MonotonicClock = Callable[[], float]
WallClock = Callable[[], datetime]


class DhanLiveFeedSupervisor:
    """Own a resilient, read-only Dhan live-feed connection.

    Health is deliberately strict: each connection generation begins empty, packets only
    become ready after the consumer returns successfully, and every required instrument
    must have a fresh primary packet before ``healthy`` can become true.
    """

    def __init__(
        self,
        credentials: DhanCredentials,
        instruments: Iterable[tuple[str, str | int] | DhanFeedInstrument],
        *,
        on_packet: PacketConsumer,
        transport: DhanFeedTransport | None = None,
        mode: DhanFeedMode = DhanFeedMode.FULL,
        config: DhanFeedSupervisorConfig | None = None,
        backoff: BoundedBackoff | None = None,
        clock: MonotonicClock = monotonic,
        wall_clock: WallClock = lambda: datetime.now(UTC),
    ) -> None:
        self._credentials = credentials
        self._instruments = _normalize_instruments(instruments)
        self._instrument_by_key = {
            (instrument.segment_code, instrument.security_id): instrument
            for instrument in self._instruments
        }
        self._expected_keys = frozenset(self._instrument_by_key)
        self._on_packet = on_packet
        self._transport = transport or WebsocketsSyncTransport()
        self._mode = mode
        self._config = config or DhanFeedSupervisorConfig()
        self._backoff = backoff or BoundedBackoff()
        self._clock = clock
        self._wall_clock = wall_clock
        self._subscription_messages = subscription_messages(
            ((item.exchange_segment, item.security_id) for item in self._instruments),
            mode=mode,
        )

        self._lock = Lock()
        self._run_lock = Lock()
        self._stop_event = Event()
        self._restart_event = Event()
        self._thread: Thread | None = None
        self._socket: DhanFeedSocket | None = None

        self._state = DhanFeedState.STOPPED
        self._connected = False
        self._generation = 0
        self._ready_at: dict[tuple[int, str], float] = {}
        self._ready_trade_epoch: dict[tuple[int, str], float] = {}
        self._last_trade_epoch_by_key: dict[tuple[int, str], float] = {}
        self._ever_complete = False
        self._packets_received = 0
        self._packets_rejected = 0
        self._trade_timestamp_rejections = 0
        self._replayed_packets = 0
        self._market_status_packets = 0
        self._reconnect_count = 0
        self._consecutive_failures = 0
        self._last_connected_at: datetime | None = None
        self._last_packet_at: datetime | None = None
        self._last_packet_monotonic: float | None = None
        self._last_trade_at: datetime | None = None
        self._market_status = DhanMarketStatus.UNKNOWN
        self._last_market_status_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None
        self._last_healthy_at: datetime | None = None
        self._next_retry_seconds: float | None = None
        self._last_error: str | None = None

    @property
    def instruments(self) -> tuple[DhanFeedInstrument, ...]:
        with self._lock:
            return self._instruments

    def replace_instruments(
        self,
        instruments: Iterable[tuple[str, str | int] | DhanFeedInstrument],
    ) -> bool:
        """Atomically replace the subscription set and resubscribe on a new generation.

        The method is safe while stopped, connecting, backing off, or receiving.  When a
        socket is active it is closed to unblock ``receive``; the reconnect is treated as a
        requested rotation rather than a transport failure, so no backoff is applied.
        """

        normalized = _normalize_instruments(instruments)
        messages = subscription_messages(
            ((item.exchange_segment, item.security_id) for item in normalized),
            mode=self._mode,
        )
        instrument_by_key = {
            (instrument.segment_code, instrument.security_id): instrument
            for instrument in normalized
        }
        with self._lock:
            if normalized == self._instruments:
                return False
            self._instruments = normalized
            self._instrument_by_key = instrument_by_key
            self._expected_keys = frozenset(instrument_by_key)
            self._subscription_messages = messages
            self._ready_at.clear()
            self._ready_trade_epoch.clear()
            self._last_trade_epoch_by_key = {
                key: epoch
                for key, epoch in self._last_trade_epoch_by_key.items()
                if key in self._expected_keys
            }
            self._ever_complete = False
            self._last_error = None
            socket = self._socket
            running = self._run_lock.locked()
            if running:
                self._restart_event.set()
                if self._connected:
                    self._state = DhanFeedState.WARMING
        if socket is not None:
            _send_disconnect_and_close(socket)
        return True

    def start(self) -> None:
        """Start the supervisor in one daemon thread; repeated live starts are rejected."""

        with self._lock:
            if self._run_lock.locked() or (
                self._thread is not None and self._thread.is_alive()
            ):
                raise RuntimeError("Dhan live-feed supervisor is already running")
            self._stop_event.clear()
            self._restart_event.clear()
            self._state = DhanFeedState.CONNECTING
            thread = Thread(
                target=self.run_forever,
                name="teco-dhan-live-feed",
                daemon=True,
            )
            self._thread = thread
            # Starting while holding the state lock closes the small race where another
            # caller could observe the not-yet-alive Thread and start a second worker.
            try:
                thread.start()
            except BaseException:
                self._thread = None
                self._state = DhanFeedState.STOPPED
                raise

    def run_forever(self) -> None:
        """Run the supervised reconnect loop in the current thread."""

        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Dhan live-feed supervisor is already running")
        try:
            self._run_loop()
        finally:
            with self._lock:
                self._socket = None
                self._connected = False
                self._ready_at.clear()
                self._ready_trade_epoch.clear()
                self._state = DhanFeedState.STOPPED
                self._next_retry_seconds = None
            self._run_lock.release()

    def stop(self, timeout_seconds: float | None = None) -> bool:
        """Request shutdown, close the socket to unblock receive, and join the worker."""

        join_timeout = (
            self._config.stop_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if not isfinite(join_timeout) or join_timeout < 0:
            raise ValueError("stop timeout must be finite and non-negative")
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            running = self._run_lock.locked() or (
                thread is not None and thread.is_alive()
            )
            socket = self._socket
            if not running and socket is None:
                self._state = DhanFeedState.STOPPED
                self._connected = False
                self._ready_at.clear()
                self._ready_trade_epoch.clear()
                return True
            self._state = DhanFeedState.STOPPING
            self._connected = False
            self._ready_at.clear()
            self._ready_trade_epoch.clear()
        if socket is not None:
            _send_disconnect_and_close(socket)
        if thread is not None and thread is not current_thread():
            thread.join(join_timeout)
            return not thread.is_alive()
        return True

    def health_snapshot(self) -> DhanFeedHealth:
        """Return a thread-safe, credential-free health copy."""

        now = self._clock()
        wall_time = self._utc_now()
        with self._lock:
            healthy = self._refresh_health_locked(now, wall_time)
            ready_keys = self._fresh_ready_keys_locked(now, wall_time)
            packet_age = (
                None
                if self._last_packet_monotonic is None
                else max(0.0, now - self._last_packet_monotonic)
            )
            trade_age = (
                None
                if self._last_trade_at is None
                else (wall_time - self._last_trade_at).total_seconds()
            )
            market_open = {
                DhanMarketStatus.UNKNOWN: None,
                DhanMarketStatus.OPEN: True,
                DhanMarketStatus.CLOSED: False,
            }[self._market_status]
            missing = tuple(
                instrument.label
                for instrument in self._instruments
                if (instrument.segment_code, instrument.security_id) not in ready_keys
            )
            return DhanFeedHealth(
                state=self._state,
                connected=self._connected,
                healthy=healthy,
                generation=self._generation,
                expected_instruments=len(self._expected_keys),
                ready_instruments=len(ready_keys),
                missing_instruments=missing,
                subscription_batches=len(self._subscription_messages),
                packets_received=self._packets_received,
                packets_rejected=self._packets_rejected,
                trade_timestamp_rejections=self._trade_timestamp_rejections,
                replayed_packets=self._replayed_packets,
                market_status_packets=self._market_status_packets,
                reconnect_count=self._reconnect_count,
                consecutive_failures=self._consecutive_failures,
                last_connected_at=self._last_connected_at,
                last_packet_at=self._last_packet_at,
                packet_age_seconds=packet_age,
                last_trade_at=self._last_trade_at,
                trade_age_seconds=trade_age,
                market_status=self._market_status,
                market_status_known=self._market_status is not DhanMarketStatus.UNKNOWN,
                market_open=market_open,
                last_market_status_at=self._last_market_status_at,
                last_heartbeat_at=self._last_heartbeat_at,
                last_healthy_at=self._last_healthy_at,
                next_retry_seconds=self._next_retry_seconds,
                last_error=self._last_error,
            )

    def instruments_healthy(
        self,
        instruments: Iterable[tuple[str, str | int] | DhanFeedInstrument],
    ) -> bool:
        """Return freshness for one coherent contract subset.

        A single Dhan connection can carry instruments from exchanges with different
        sessions and liquidity. Global health deliberately remains strict for diagnostics,
        while decision gating can use this method to require every leg of only the exact
        contract being evaluated. Unknown or unsubscribed identities fail closed.
        """

        selected = _normalize_instruments(instruments)
        requested_keys = frozenset(
            (instrument.segment_code, instrument.security_id)
            for instrument in selected
        )
        now = self._clock()
        wall_time = self._utc_now()
        with self._lock:
            self._refresh_health_locked(now, wall_time)
            ready_keys = self._fresh_ready_keys_locked(now, wall_time)
            return bool(
                self._connected
                and self._market_status is not DhanMarketStatus.CLOSED
                and requested_keys.issubset(self._expected_keys)
                and requested_keys.issubset(ready_keys)
            )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            socket: DhanFeedSocket | None = None
            requested_restart = False
            try:
                self._begin_connection_attempt()
                socket = self._transport.connect(live_feed_url(self._credentials))
                # connect() may take until its open timeout. If shutdown arrived while it
                # was blocked, don't briefly publish a connected generation or subscribe.
                if self._stop_event.is_set():
                    continue
                self._connected_socket(socket)
                self._subscribe(socket)
                self._receive_loop(socket)
            except Exception as error:  # noqa: BLE001 - supervisor isolates every dependency
                requested_restart = self._restart_event.is_set()
                if requested_restart:
                    self._restart_event.clear()
                elif not self._stop_event.is_set():
                    self._record_failure(error)
            finally:
                self._clear_socket(socket)
            if self._stop_event.is_set():
                break
            if requested_restart:
                continue
            delay = self._backoff.delay(max(1, self._consecutive_failure_count()))
            with self._lock:
                self._state = DhanFeedState.BACKING_OFF
                self._next_retry_seconds = delay
            self._stop_event.wait(delay)

    def _begin_connection_attempt(self) -> None:
        with self._lock:
            # A replacement made before a socket existed is already reflected in the
            # subscription snapshot used by this new attempt.
            self._restart_event.clear()
            if self._generation > 0:
                self._reconnect_count += 1
            self._state = DhanFeedState.CONNECTING
            self._connected = False
            self._ready_at.clear()
            self._ready_trade_epoch.clear()
            self._market_status = DhanMarketStatus.UNKNOWN
            self._ever_complete = False
            self._next_retry_seconds = None

    def _connected_socket(self, socket: DhanFeedSocket) -> None:
        with self._lock:
            # If the set changed while connect() was blocking, this fresh socket can use
            # the latest subscription snapshot directly.  Later replacements will see and
            # close this socket instead.
            self._restart_event.clear()
            self._socket = socket
            self._connected = True
            self._generation += 1
            self._state = DhanFeedState.SUBSCRIBING
            self._last_connected_at = self._utc_now()

    def _subscribe(self, socket: DhanFeedSocket) -> None:
        with self._lock:
            messages = self._subscription_messages
        for message in messages:
            socket.send_text(json.dumps(message, separators=(",", ":"), sort_keys=True))
        with self._lock:
            self._state = DhanFeedState.WARMING

    def _receive_loop(self, socket: DhanFeedSocket) -> None:
        last_message_at = self._clock()
        last_heartbeat_at = last_message_at
        while not self._stop_event.is_set():
            try:
                message = socket.receive(self._config.receive_poll_seconds)
            except DhanFeedReceiveTimeout:
                now = self._clock()
                self._refresh_health(now)
                if now - last_message_at >= self._config.idle_reconnect_seconds:
                    raise DhanFeedIdleError("Dhan live feed became idle")
                if now - last_heartbeat_at >= self._config.heartbeat_interval_seconds:
                    socket.ping(self._config.heartbeat_timeout_seconds)
                    last_heartbeat_at = self._clock()
                    with self._lock:
                        self._last_heartbeat_at = self._utc_now()
                continue
            except DhanLiveFeedError:
                raise
            except Exception:  # noqa: BLE001 - transport exceptions are deliberately sanitized
                raise DhanFeedTransportError("Dhan live-feed receive failed") from None

            now = self._clock()
            if isinstance(message, str):
                with self._lock:
                    self._packets_rejected += 1
                # Dhan documents binary-only responses. Isolate an individual unexpected
                # text frame just like a malformed binary packet; it cannot affect
                # readiness, and a fully corrupt stream is still caught by the idle limit.
                if now - last_message_at >= self._config.idle_reconnect_seconds:
                    raise DhanFeedIdleError("Dhan live feed became idle")
                continue
            try:
                packets = decode_feed_message(message)
            except ValueError:
                with self._lock:
                    self._packets_rejected += 1
                # A single corrupt or currently unsupported provider packet doesn't justify
                # dropping other valid subscriptions.  Health remains fail closed because
                # rejected packets can never mark an instrument ready.
                if now - last_message_at >= self._config.idle_reconnect_seconds:
                    raise DhanFeedIdleError("Dhan live feed became idle")
                continue
            last_message_at = now
            for packet in packets:
                self._accept_packet(packet, now)
            if now - last_heartbeat_at >= self._config.heartbeat_interval_seconds:
                socket.ping(self._config.heartbeat_timeout_seconds)
                last_heartbeat_at = self._clock()
                with self._lock:
                    self._last_heartbeat_at = self._utc_now()

    def _accept_packet(self, packet: DhanFeedPacket, received_at: float) -> None:
        if packet.response_code == 50:
            raise DhanFeedDisconnected(int(packet.fields["disconnect_code"]))
        wall_time = self._utc_now()
        if packet.response_code == 7:
            self._accept_market_status(packet, received_at, wall_time)
            return
        key = (packet.exchange_segment_code, packet.security_id)
        with self._lock:
            expected = key in self._expected_keys
        if not expected:
            with self._lock:
                self._packets_rejected += 1
            return

        trade_epoch: float | None = None
        trade_time: datetime | None = None
        if packet.response_code in _PRIMARY_PRICE_RESPONSE_CODES:
            trade_timestamp = _positive_trade_timestamp(packet)
            if trade_timestamp is None:
                with self._lock:
                    self._packets_rejected += 1
                    self._trade_timestamp_rejections += 1
                return
            # Dhan LTT is the exchange's last executed trade time, not the
            # timestamp of this current quote/depth packet. Provider LTT can be old,
            # repeated, or clock-shifted without making the current packet stale.
            trade_epoch, trade_time = trade_timestamp
        try:
            accepted = self._on_packet(packet)
        except Exception:  # noqa: BLE001 - callback errors are deliberately sanitized
            raise DhanFeedConsumerError("Dhan live-feed packet consumer failed") from None
        if accepted is False:
            with self._lock:
                self._packets_rejected += 1
            return

        with self._lock:
            if key not in self._expected_keys:
                self._packets_rejected += 1
                return
            self._packets_received += 1
            self._last_packet_at = wall_time
            self._last_packet_monotonic = received_at
            if trade_epoch is not None and trade_time is not None:
                previous_epoch = self._last_trade_epoch_by_key.get(key)
                if previous_epoch is None or trade_epoch > previous_epoch:
                    self._last_trade_epoch_by_key[key] = trade_epoch
                    if (
                        (wall_time - trade_time).total_seconds()
                        >= -self._config.future_skew_seconds
                    ):
                        self._last_trade_at = trade_time
                elif trade_epoch < previous_epoch:
                    # Preserve out-of-order LTT as telemetry only. A current FULL quote
                    # packet can legitimately carry an older trade time while still
                    # containing current depth/OI data.
                    self._replayed_packets += 1
            if packet.response_code == _primary_response_code(self._mode):
                self._ready_at[key] = received_at
                if trade_epoch is not None:
                    self._ready_trade_epoch[key] = trade_epoch
            self._refresh_health_locked(received_at, wall_time)

    def _accept_market_status(
        self,
        packet: DhanFeedPacket,
        received_at: float,
        wall_time: datetime,
    ) -> None:
        explicit_status = _explicit_market_status(packet)
        with self._lock:
            self._market_status_packets += 1
            self._last_market_status_at = wall_time
            if explicit_status is not None and explicit_status is not self._market_status:
                self._market_status = explicit_status
                # A state transition invalidates pre-transition prices.  Opening therefore
                # requires a new trade before readiness can recover; closing fails closed
                # immediately even if the old receipt-age window has not elapsed.
                self._ready_at.clear()
                self._ready_trade_epoch.clear()
            self._refresh_health_locked(received_at, wall_time)

    def _refresh_health(self, now: float) -> None:
        wall_time = self._utc_now()
        with self._lock:
            self._refresh_health_locked(now, wall_time)

    def _refresh_health_locked(self, now: float, wall_time: datetime) -> bool:
        ready_keys = self._fresh_ready_keys_locked(now, wall_time)
        complete = (
            self._connected
            and self._market_status is not DhanMarketStatus.CLOSED
            and ready_keys == self._expected_keys
        )
        if complete:
            self._state = DhanFeedState.HEALTHY
            self._ever_complete = True
            self._consecutive_failures = 0
            self._last_error = None
            self._last_healthy_at = wall_time
            return True
        if self._connected and self._state not in {
            DhanFeedState.CONNECTING,
            DhanFeedState.SUBSCRIBING,
            DhanFeedState.STOPPING,
        }:
            self._state = DhanFeedState.STALE if self._ever_complete else DhanFeedState.WARMING
        return False

    def _fresh_ready_keys_locked(
        self, now: float, wall_time: datetime
    ) -> frozenset[tuple[int, str]]:
        del wall_time
        # FULL/QUOTE packets are real-time quote updates. Their LTT field records the
        # last executed trade and can legitimately be old for illiquid options. Socket
        # readiness therefore follows the age of the validated packet receipt, while
        # malformed or future-dated LTT is rejected earlier in _accept_packet.
        return frozenset(
            key
            for key, received_at in self._ready_at.items()
            if 0 <= now - received_at <= self._config.data_stale_seconds
        )

    def _record_failure(self, error: Exception) -> None:
        with self._lock:
            self._connected = False
            self._ready_at.clear()
            self._ready_trade_epoch.clear()
            self._ever_complete = False
            self._consecutive_failures += 1
            self._last_error = _sanitized_error(error)

    def _clear_socket(self, socket: DhanFeedSocket | None) -> None:
        with self._lock:
            if self._socket is socket:
                self._socket = None
            self._connected = False
            self._ready_at.clear()
            self._ready_trade_epoch.clear()
        if socket is not None:
            socket.close()

    def _consecutive_failure_count(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def _utc_now(self) -> datetime:
        value = self._wall_clock()
        if value.tzinfo is None:
            raise ValueError("wall_clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def decode_feed_message(message: bytes) -> tuple[DhanFeedPacket, ...]:
    """Decode one or more Dhan packets carried in a binary WebSocket message."""

    if not message:
        raise ValueError("Dhan live-feed message cannot be empty")
    packets: list[DhanFeedPacket] = []
    offset = 0
    while offset < len(message):
        remaining = len(message) - offset
        if remaining < 8:
            raise ValueError("Dhan live-feed message ends with a truncated header")
        response_code = message[offset]
        message_length = unpack_from("<H", message, offset + 1)[0]
        if message_length < 8 or message_length > remaining:
            raise ValueError("Dhan live-feed message contains an invalid packet length")
        packet_bytes = message[offset : offset + message_length]
        if response_code == 7:
            if message_length != 8:
                raise ValueError("invalid Dhan market-status packet length")
            _, _, segment_code, security_id = unpack_from("<BHBI", packet_bytes, 0)
            packet = DhanFeedPacket(
                response_code=7,
                message_length=8,
                exchange_segment_code=segment_code,
                security_id=str(security_id),
                fields={},
            )
        else:
            packet = decode_feed_packet(packet_bytes)
        packets.append(packet)
        offset += message_length
    return tuple(packets)


def _normalize_instruments(
    instruments: Iterable[tuple[str, str | int] | DhanFeedInstrument],
) -> tuple[DhanFeedInstrument, ...]:
    normalized: list[DhanFeedInstrument] = []
    seen: set[DhanFeedInstrument] = set()
    for value in instruments:
        if isinstance(value, DhanFeedInstrument):
            segment = value.exchange_segment
            security_id_value: str | int = value.security_id
        else:
            segment, security_id_value = value
        segment = segment.strip().upper()
        if segment not in DHAN_EXCHANGE_SEGMENT_CODES:
            raise ValueError(f"unsupported Dhan exchange segment: {segment}")
        if isinstance(security_id_value, bool):
            raise ValueError(  # noqa: TRY004 - one stable validation error for all bad IDs
                "Dhan security ID must be a positive integer"
            )
        try:
            security_id = str(int(str(security_id_value).strip()))
        except (TypeError, ValueError):
            raise ValueError("Dhan security ID must be a positive integer") from None
        if int(security_id) <= 0:
            raise ValueError("Dhan security ID must be a positive integer")
        item = DhanFeedInstrument(segment, security_id)
        if item not in seen:
            normalized.append(item)
            seen.add(item)
    if not normalized:
        raise ValueError("at least one Dhan live-feed instrument is required")
    if len(normalized) > DHAN_MAX_INSTRUMENTS_PER_CONNECTION:
        raise ValueError(
            "Dhan live feed supports at most 5,000 instruments per connection"
        )
    return tuple(normalized)


def _primary_response_code(mode: DhanFeedMode) -> int:
    return {
        DhanFeedMode.TICKER: 2,
        DhanFeedMode.QUOTE: 4,
        DhanFeedMode.FULL: 8,
    }[mode]


def _positive_trade_timestamp(
    packet: DhanFeedPacket,
) -> tuple[float, datetime] | None:
    value = packet.fields.get("last_trade_epoch")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or value <= 0
    ):
        return None
    epoch = float(value)
    try:
        trade_time = datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return epoch, trade_time


def _explicit_market_status(packet: DhanFeedPacket) -> DhanMarketStatus | None:
    """Read only an adapter-normalized status; never guess from the bare Dhan header.

    Dhan's documented eight-byte response-code 7 packet doesn't define an open/closed
    value.  A transport/decoder with additional provider metadata may normalize an
    explicit boolean into ``fields["market_open"]``.  Unknown or malformed values leave
    the current generation's status unchanged.
    """

    value = packet.fields.get("market_open")
    if isinstance(value, bool):
        return DhanMarketStatus.OPEN if value else DhanMarketStatus.CLOSED
    if isinstance(value, (int, float)) and isfinite(float(value)):
        if float(value) == 1.0:
            return DhanMarketStatus.OPEN
        if float(value) == 0.0:
            return DhanMarketStatus.CLOSED
    return None


def _send_disconnect_and_close(socket: DhanFeedSocket) -> None:
    try:
        socket.send_text('{"RequestCode":12}')
    except Exception:  # noqa: BLE001,S110 - disconnect is deliberately best effort
        pass
    socket.close()


def _sanitized_error(error: Exception) -> str:
    if isinstance(error, DhanFeedDisconnected):
        return f"provider disconnected live feed (code {error.provider_code})"
    if isinstance(error, DhanFeedDependencyError):
        return "live-feed WebSocket dependency is unavailable"
    if isinstance(error, DhanFeedIdleError):
        return "live feed exceeded the idle limit"
    if isinstance(error, DhanFeedProtocolError):
        return "live feed returned invalid protocol data"
    if isinstance(error, DhanFeedConsumerError):
        return "live-feed packet consumer failed"
    if isinstance(error, DhanFeedTransportError):
        return "live-feed transport failed"
    return "live-feed connection failed"


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _require_positive_finite(name: str, value: float) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
