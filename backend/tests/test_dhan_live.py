from __future__ import annotations

import json
import time
import unittest
from datetime import UTC, datetime, timedelta
from math import nan
from queue import Empty, Queue
from struct import pack
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from teco_quant.brokers.dhan import DhanCredentials, DhanFeedPacket
from teco_quant.brokers.dhan_live import (
    BoundedBackoff,
    DhanFeedDependencyError,
    DhanFeedReceiveTimeout,
    DhanFeedState,
    DhanFeedSupervisorConfig,
    DhanFeedTransportError,
    DhanLiveFeedSupervisor,
    DhanMarketStatus,
    WebsocketsSyncTransport,
    decode_feed_message,
)

FEED_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def full_packet(
    *,
    segment_code: int,
    security_id: int,
    ltp: float = 100.5,
    trade_epoch: int | None = None,
) -> bytes:
    selected_trade_epoch = (
        int(FEED_NOW.timestamp()) if trade_epoch is None else trade_epoch
    )
    payload = pack(
        "<fHIfIIIIIIffff",
        ltp,
        25,
        selected_trade_epoch,
        ltp - 1,
        10_000,
        2_000,
        2_500,
        15_000,
        16_000,
        14_000,
        ltp - 2,
        ltp - 3,
        ltp + 2,
        ltp - 4,
    )
    depth = b"".join(
        pack("<IIHHff", 100 + level, 200 + level, 2, 3, ltp - level, ltp + level)
        for level in range(5)
    )
    return pack("<BHBI", 8, 162, segment_code, security_id) + payload + depth


class FakeSocket:
    def __init__(self) -> None:
        self.incoming: Queue[bytes | str | Exception] = Queue()
        self.sent: list[str] = []
        self.ping_count = 0
        self.close_count = 0
        self._closed = False
        self._lock = Lock()

    def push(self, item: bytes | str | Exception) -> None:
        self.incoming.put(item)

    def send_text(self, message: str) -> None:
        with self._lock:
            if self._closed:
                raise DhanFeedTransportError("fake socket is closed")
            self.sent.append(message)

    def receive(self, timeout_seconds: float) -> bytes | str:
        try:
            item = self.incoming.get(timeout=timeout_seconds)
        except Empty:
            raise DhanFeedReceiveTimeout("fake receive timeout") from None
        if isinstance(item, Exception):
            raise item
        return item

    def ping(self, timeout_seconds: float) -> None:
        del timeout_seconds
        with self._lock:
            if self._closed:
                raise DhanFeedTransportError("fake heartbeat failed")
            self.ping_count += 1

    def close(self) -> None:
        with self._lock:
            self.close_count += 1
            if self._closed:
                return
            self._closed = True
        self.incoming.put(DhanFeedTransportError("fake socket closed"))


class FakeTransport:
    def __init__(self, connections: list[FakeSocket | Exception]) -> None:
        self.connections: Queue[FakeSocket | Exception] = Queue()
        for connection in connections:
            self.connections.put(connection)
        self.urls: list[str] = []
        self.connect_count = 0
        self._lock = Lock()

    def connect(self, url: str) -> FakeSocket:
        with self._lock:
            self.urls.append(url)
            self.connect_count += 1
        try:
            connection = self.connections.get_nowait()
        except Empty:
            raise DhanFeedTransportError("no scripted fake connection") from None
        if isinstance(connection, Exception):
            raise connection
        return connection


def wait_for(predicate, *, timeout: float = 1.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition wasn't met before timeout")


class DhanLiveFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credentials = DhanCredentials("1000000001", "never-log-this-token")
        self.config = DhanFeedSupervisorConfig(
            receive_poll_seconds=0.01,
            heartbeat_interval_seconds=0.5,
            heartbeat_timeout_seconds=0.1,
            idle_reconnect_seconds=1.0,
            data_stale_seconds=0.5,
            stop_timeout_seconds=1.0,
        )
        self.no_backoff = BoundedBackoff(
            base_seconds=0,
            maximum_seconds=0,
            jitter=lambda lower, upper: (lower + upper) / 2,
        )
        self.supervisors: list[DhanLiveFeedSupervisor] = []

    def tearDown(self) -> None:
        for supervisor in self.supervisors:
            supervisor.stop()

    def supervisor(
        self,
        instruments,
        transport: FakeTransport,
        *,
        consumer=None,
        config: DhanFeedSupervisorConfig | None = None,
        backoff: BoundedBackoff | None = None,
        wall_clock=None,
    ) -> DhanLiveFeedSupervisor:
        instance = DhanLiveFeedSupervisor(
            self.credentials,
            instruments,
            on_packet=consumer or (lambda packet: None),
            transport=transport,
            config=config or self.config,
            backoff=backoff or self.no_backoff,
            wall_clock=wall_clock or (lambda: FEED_NOW),
        )
        self.supervisors.append(instance)
        return instance

    def test_health_is_fail_closed_until_every_primary_packet_is_consumed(self) -> None:
        socket = FakeSocket()
        transport = FakeTransport([socket])
        consumed = []
        supervisor = self.supervisor(
            [("NSE_FNO", 50001), ("NSE_FNO", 50002)],
            transport,
            consumer=consumed.append,
        )
        supervisor.start()
        wait_for(lambda: len(socket.sent) == 1)

        socket.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().packets_received == 1)
        warming = supervisor.health_snapshot()
        self.assertFalse(warming.healthy)
        self.assertIs(warming.state, DhanFeedState.WARMING)
        self.assertEqual(warming.ready_instruments, 1)
        self.assertEqual(warming.missing_instruments, ("NSE_FNO:50002",))

        socket.push(full_packet(segment_code=2, security_id=50002))
        wait_for(lambda: supervisor.health_snapshot().healthy)
        healthy = supervisor.health_snapshot()
        self.assertEqual(healthy.ready_instruments, 2)
        self.assertEqual(len(consumed), 2)
        self.assertIsNotNone(healthy.packet_age_seconds)
        self.assertGreaterEqual(healthy.packet_age_seconds or 0.0, 0.0)
        self.assertNotIn("never-log-this-token", json.dumps(healthy.as_dict()))

    def test_explicit_consumer_rejection_cannot_mark_an_instrument_ready(self) -> None:
        socket = FakeSocket()
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)],
            FakeTransport([socket]),
            consumer=lambda packet: False,
        )
        supervisor.start()
        wait_for(lambda: len(socket.sent) == 1)
        socket.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().packets_rejected == 1)
        health = supervisor.health_snapshot()
        self.assertFalse(health.healthy)
        self.assertEqual(health.ready_instruments, 0)
        self.assertEqual(health.packets_received, 0)

    def test_primary_packets_require_valid_ltt_but_receipt_time_drives_readiness(
        self,
    ) -> None:
        socket = FakeSocket()
        consumed: list[DhanFeedPacket] = []
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)],
            FakeTransport([socket]),
            consumer=consumed.append,
        )
        supervisor.start()
        wait_for(lambda: len(socket.sent) == 1)

        missing_epoch = DhanFeedPacket(
            response_code=8,
            message_length=162,
            exchange_segment_code=2,
            security_id="50001",
            fields={"last_price": 100.5},
        )
        with patch(
            "teco_quant.brokers.dhan_live.decode_feed_message",
            return_value=(missing_epoch,),
        ):
            socket.push(b"synthetic missing-epoch packet")
            wait_for(lambda: supervisor.health_snapshot().packets_rejected == 1)

        socket.push(full_packet(segment_code=2, security_id=50001, trade_epoch=0))
        wait_for(lambda: supervisor.health_snapshot().packets_rejected == 2)

        socket.push(
            full_packet(
                segment_code=2,
                security_id=50001,
                trade_epoch=int((FEED_NOW - timedelta(seconds=1)).timestamp()),
            )
        )
        wait_for(lambda: len(consumed) == 1)
        self.assertTrue(supervisor.health_snapshot().healthy)

        socket.push(
            full_packet(
                segment_code=2,
                security_id=50001,
                trade_epoch=int((FEED_NOW + timedelta(seconds=3)).timestamp()),
            )
        )
        wait_for(lambda: len(consumed) == 2)

        valid = full_packet(segment_code=2, security_id=50001)
        socket.push(valid)
        socket.push(valid)
        wait_for(lambda: len(consumed) == 4)

        health = supervisor.health_snapshot()
        self.assertTrue(health.healthy)
        self.assertEqual(health.packets_rejected, 2)
        self.assertEqual(health.trade_timestamp_rejections, 2)
        self.assertGreaterEqual(health.replayed_packets, 1)
        self.assertEqual(health.last_trade_at, FEED_NOW - timedelta(seconds=1))
        self.assertEqual(health.trade_age_seconds, 1.0)

    def test_wall_clock_ltt_age_does_not_invalidate_recent_packet_readiness(self) -> None:
        socket = FakeSocket()
        wall_time = [FEED_NOW]
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)],
            FakeTransport([socket]),
            wall_clock=lambda: wall_time[0],
        )
        supervisor.start()
        wait_for(lambda: len(socket.sent) == 1)
        socket.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().healthy)

        wall_time[0] = FEED_NOW + timedelta(seconds=1)
        health = supervisor.health_snapshot()
        self.assertTrue(health.healthy)
        self.assertEqual(health.ready_instruments, 1)
        self.assertEqual(health.trade_age_seconds, 1.0)

    def test_subscription_batches_are_replayed_after_transport_reconnect(self) -> None:
        first = FakeSocket()
        second = FakeSocket()
        transport = FakeTransport([first, second])
        supervisor = self.supervisor([("NSE_FNO", 50001)], transport)
        supervisor.start()
        wait_for(lambda: len(first.sent) == 1)
        first.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().healthy)

        first.push(DhanFeedTransportError("simulated connection loss"))
        wait_for(lambda: len(second.sent) == 1)
        second.push(
            full_packet(
                segment_code=2,
                security_id=50001,
                trade_epoch=int(FEED_NOW.timestamp()) + 1,
            )
        )
        wait_for(
            lambda: supervisor.health_snapshot().generation == 2
            and supervisor.health_snapshot().healthy
        )

        health = supervisor.health_snapshot()
        self.assertEqual(health.reconnect_count, 1)
        self.assertEqual(json.loads(first.sent[0]), json.loads(second.sent[0]))
        self.assertEqual(health.consecutive_failures, 0)

    def test_provider_disconnect_packet_is_sanitized_and_reconnected(self) -> None:
        first = FakeSocket()
        second = FakeSocket()
        transport = FakeTransport([first, second])
        supervisor = self.supervisor([("NSE_FNO", 50001)], transport)
        supervisor.start()
        wait_for(lambda: len(first.sent) == 1)

        first.push(pack("<BHBIH", 50, 10, 2, 50001, 807))
        wait_for(lambda: len(second.sent) == 1)
        health = supervisor.health_snapshot()
        self.assertEqual(health.last_error, "provider disconnected live feed (code 807)")
        self.assertFalse(health.healthy)
        self.assertNotIn("never-log-this-token", json.dumps(health.as_dict()))

    def test_runtime_instrument_replacement_forces_clean_resubscription(self) -> None:
        first = FakeSocket()
        second = FakeSocket()
        transport = FakeTransport([first, second])
        supervisor = self.supervisor([("NSE_FNO", 50001)], transport)
        supervisor.start()
        wait_for(lambda: len(first.sent) == 1)
        first.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().healthy)

        self.assertTrue(supervisor.replace_instruments([("NSE_FNO", 60001)]))
        wait_for(lambda: len(second.sent) == 1)
        replacement = json.loads(second.sent[0])
        self.assertEqual(replacement["InstrumentList"][0]["SecurityId"], "60001")
        self.assertFalse(supervisor.health_snapshot().healthy)

        second.push(full_packet(segment_code=2, security_id=60001))
        wait_for(lambda: supervisor.health_snapshot().healthy)
        health = supervisor.health_snapshot()
        self.assertEqual(health.generation, 2)
        self.assertEqual(health.consecutive_failures, 0)
        self.assertFalse(supervisor.replace_instruments([("NSE_FNO", "60001")]))

    def test_corrupt_packet_is_counted_and_discarded_without_reconnect(self) -> None:
        socket = FakeSocket()
        transport = FakeTransport([socket])
        supervisor = self.supervisor([("NSE_FNO", 50001)], transport)
        supervisor.start()
        wait_for(lambda: len(socket.sent) == 1)

        socket.push(pack("<BHBI", 99, 8, 2, 50001))
        wait_for(lambda: supervisor.health_snapshot().packets_rejected == 1)
        self.assertEqual(transport.connect_count, 1)
        self.assertFalse(supervisor.health_snapshot().healthy)

        socket.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().healthy)
        self.assertEqual(transport.connect_count, 1)

    def test_unexpected_text_frame_is_isolated_without_reconnect(self) -> None:
        socket = FakeSocket()
        transport = FakeTransport([socket])
        supervisor = self.supervisor([("NSE_FNO", 50001)], transport)
        supervisor.start()
        wait_for(lambda: len(socket.sent) == 1)

        socket.push('{"unexpected":"provider text"}')
        wait_for(lambda: supervisor.health_snapshot().packets_rejected == 1)
        self.assertEqual(transport.connect_count, 1)

        socket.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().healthy)
        self.assertEqual(transport.connect_count, 1)

    def test_market_status_and_concatenated_packets_are_decoded(self) -> None:
        market_status = pack("<BHBI", 7, 8, 0, 0)
        full = full_packet(segment_code=2, security_id=50001)
        packets = decode_feed_message(market_status + full)
        self.assertEqual([packet.response_code for packet in packets], [7, 8])
        self.assertEqual(packets[1].security_id, "50001")

    def test_market_status_is_tracked_and_explicit_close_gates_health(self) -> None:
        socket = FakeSocket()
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)], FakeTransport([socket])
        )
        supervisor.start()
        wait_for(lambda: len(socket.sent) == 1)
        socket.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().healthy)

        # The documented eight-byte code-7 header carries no status value.  It is tracked
        # but remains UNKNOWN and must not permanently suppress an otherwise healthy feed.
        socket.push(pack("<BHBI", 7, 8, 0, 0))
        wait_for(lambda: supervisor.health_snapshot().market_status_packets == 1)
        unknown = supervisor.health_snapshot()
        self.assertIs(unknown.market_status, DhanMarketStatus.UNKNOWN)
        self.assertFalse(unknown.market_status_known)
        self.assertIsNone(unknown.market_open)
        self.assertTrue(unknown.healthy)

        closed_packet = DhanFeedPacket(
            response_code=7,
            message_length=8,
            exchange_segment_code=0,
            security_id="0",
            fields={"market_open": 0},
        )
        with patch(
            "teco_quant.brokers.dhan_live.decode_feed_message",
            return_value=(closed_packet,),
        ):
            socket.push(b"synthetic explicit-close packet")
            wait_for(
                lambda: supervisor.health_snapshot().market_status
                is DhanMarketStatus.CLOSED
            )
        closed = supervisor.health_snapshot()
        self.assertFalse(closed.healthy)
        self.assertEqual(closed.ready_instruments, 0)
        self.assertTrue(closed.market_status_known)
        self.assertFalse(closed.market_open)

        open_packet = DhanFeedPacket(
            response_code=7,
            message_length=8,
            exchange_segment_code=0,
            security_id="0",
            fields={"market_open": 1},
        )
        with patch(
            "teco_quant.brokers.dhan_live.decode_feed_message",
            return_value=(open_packet,),
        ):
            socket.push(b"synthetic explicit-open packet")
            wait_for(
                lambda: supervisor.health_snapshot().market_status
                is DhanMarketStatus.OPEN
            )
        self.assertFalse(supervisor.health_snapshot().healthy)

        socket.push(
            full_packet(
                segment_code=2,
                security_id=50001,
                trade_epoch=int(FEED_NOW.timestamp()) + 1,
            )
        )
        wait_for(lambda: supervisor.health_snapshot().healthy)
        reopened = supervisor.health_snapshot()
        self.assertEqual(reopened.market_status_packets, 3)
        self.assertTrue(reopened.market_open)
        self.assertEqual(reopened.last_market_status_at, FEED_NOW)
        self.assertEqual(reopened.as_dict()["market_status"], "OPEN")

    def test_stale_instrument_makes_connected_feed_unhealthy(self) -> None:
        socket = FakeSocket()
        transport = FakeTransport([socket])
        short_stale = DhanFeedSupervisorConfig(
            receive_poll_seconds=0.005,
            heartbeat_interval_seconds=0.2,
            heartbeat_timeout_seconds=0.1,
            idle_reconnect_seconds=1.0,
            data_stale_seconds=0.04,
            stop_timeout_seconds=1.0,
        )
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)], transport, config=short_stale
        )
        supervisor.start()
        wait_for(lambda: len(socket.sent) == 1)
        socket.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().healthy)
        wait_for(lambda: supervisor.health_snapshot().state is DhanFeedState.STALE)
        health = supervisor.health_snapshot()
        self.assertTrue(health.connected)
        self.assertFalse(health.healthy)
        self.assertEqual(health.ready_instruments, 0)

    def test_idle_connection_is_pinged_then_reconnected(self) -> None:
        first = FakeSocket()
        second = FakeSocket()
        transport = FakeTransport([first, second])
        idle_config = DhanFeedSupervisorConfig(
            receive_poll_seconds=0.005,
            heartbeat_interval_seconds=0.015,
            heartbeat_timeout_seconds=0.01,
            idle_reconnect_seconds=0.06,
            data_stale_seconds=0.05,
            stop_timeout_seconds=1.0,
        )
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)], transport, config=idle_config
        )
        supervisor.start()
        wait_for(lambda: first.ping_count > 0)
        wait_for(lambda: transport.connect_count >= 2)
        health = supervisor.health_snapshot()
        self.assertEqual(health.last_error, "live feed exceeded the idle limit")
        self.assertIsNotNone(health.last_heartbeat_at)

    def test_failures_never_expose_authenticated_url_or_token(self) -> None:
        leaked_error = RuntimeError(
            "failed wss://api-feed.dhan.co?token=never-log-this-token&clientId=1000000001"
        )
        transport = FakeTransport([leaked_error])
        slow_backoff = BoundedBackoff(
            base_seconds=0.2,
            maximum_seconds=0.2,
            jitter=lambda lower, upper: lower,
        )
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)], transport, backoff=slow_backoff
        )
        supervisor.start()
        wait_for(lambda: supervisor.health_snapshot().consecutive_failures == 1)
        serialized = json.dumps(supervisor.health_snapshot().as_dict())
        self.assertNotIn("never-log-this-token", serialized)
        self.assertNotIn("api-feed.dhan.co", serialized)
        self.assertEqual(
            supervisor.health_snapshot().last_error, "live-feed connection failed"
        )

    def test_stop_sends_provider_disconnect_and_closes_socket(self) -> None:
        socket = FakeSocket()
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)], FakeTransport([socket])
        )
        supervisor.start()
        wait_for(lambda: len(socket.sent) == 1)
        self.assertTrue(supervisor.stop())
        self.assertIn('{"RequestCode":12}', socket.sent)
        self.assertGreaterEqual(socket.close_count, 1)
        self.assertIs(supervisor.health_snapshot().state, DhanFeedState.STOPPED)

    def test_stop_rejects_invalid_timeout(self) -> None:
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)], FakeTransport([])
        )
        with self.assertRaisesRegex(ValueError, "finite and non-negative"):
            supervisor.stop(nan)
        with self.assertRaisesRegex(ValueError, "finite and non-negative"):
            supervisor.stop(-1)

    def test_stop_interrupts_reconnect_backoff(self) -> None:
        transport = FakeTransport([DhanFeedTransportError("offline")])
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)],
            transport,
            backoff=BoundedBackoff(
                base_seconds=5,
                maximum_seconds=5,
                jitter=lambda lower, upper: lower,
            ),
        )
        supervisor.start()
        wait_for(lambda: supervisor.health_snapshot().state is DhanFeedState.BACKING_OFF)
        started_at = time.monotonic()
        self.assertTrue(supervisor.stop(timeout_seconds=0.5))
        self.assertLess(time.monotonic() - started_at, 0.5)

    def test_supervisor_can_be_stopped_before_start_and_restarted_after_shutdown(self) -> None:
        first = FakeSocket()
        second = FakeSocket()
        supervisor = self.supervisor(
            [("NSE_FNO", 50001)], FakeTransport([first, second])
        )
        self.assertTrue(supervisor.stop())
        self.assertIs(supervisor.health_snapshot().state, DhanFeedState.STOPPED)

        supervisor.start()
        wait_for(lambda: len(first.sent) == 1)
        self.assertTrue(supervisor.stop())
        supervisor.start()
        wait_for(lambda: len(second.sent) == 1)
        second.push(full_packet(segment_code=2, security_id=50001))
        wait_for(lambda: supervisor.health_snapshot().healthy)
        self.assertEqual(supervisor.health_snapshot().generation, 2)

    def test_constructor_deduplicates_and_enforces_connection_limit(self) -> None:
        supervisor = self.supervisor(
            [("NSE_FNO", 1), ("NSE_FNO", "1")], FakeTransport([])
        )
        self.assertEqual(supervisor.health_snapshot().expected_instruments, 1)
        too_many = [("NSE_FNO", value) for value in range(1, 5_002)]
        with self.assertRaisesRegex(ValueError, "5,000"):
            DhanLiveFeedSupervisor(
                self.credentials,
                too_many,
                on_packet=lambda packet: None,
                transport=FakeTransport([]),
            )

    def test_bounded_backoff_uses_injected_equal_jitter(self) -> None:
        backoff = BoundedBackoff(
            base_seconds=1,
            maximum_seconds=4,
            jitter=lambda lower, upper: upper,
        )
        self.assertEqual([backoff.delay(value) for value in range(1, 6)], [1, 2, 4, 4, 4])

    def test_timing_configuration_rejects_non_finite_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive and finite"):
            DhanFeedSupervisorConfig(receive_poll_seconds=nan)
        with self.assertRaisesRegex(ValueError, "finite and non-negative"):
            DhanFeedSupervisorConfig(future_skew_seconds=-0.1)
        with self.assertRaisesRegex(ValueError, "finite and non-negative"):
            DhanFeedSupervisorConfig(future_skew_seconds=nan)
        with self.assertRaisesRegex(ValueError, "finite"):
            BoundedBackoff(base_seconds=1, maximum_seconds=nan)
        with self.assertRaisesRegex(ValueError, "full Dhan packet"):
            WebsocketsSyncTransport(max_message_bytes=161)

    def test_websockets_17_sync_transport_contract_and_error_redaction(self) -> None:
        pong = Event()
        pong.set()
        connection = SimpleNamespace(
            send=Mock(),
            recv=Mock(return_value=b"packet"),
            ping=Mock(return_value=pong),
            close=Mock(),
        )
        connect = Mock(return_value=connection)
        module = SimpleNamespace(connect=connect)
        transport = WebsocketsSyncTransport()

        with patch("teco_quant.brokers.dhan_live.import_module", return_value=module):
            socket = transport.connect(
                "wss://api-feed.dhan.co?token=never-log-this-token"
            )
        self.assertEqual(socket.receive(0.25), b"packet")
        socket.send_text("subscription")
        socket.ping(0.25)
        socket.close()

        _, kwargs = connect.call_args
        self.assertEqual(kwargs["max_size"], 2 * 1024 * 1024)
        self.assertEqual(kwargs["ping_interval"], 10.0)
        self.assertIsNone(kwargs["compression"])
        self.assertFalse(kwargs["logger"].isEnabledFor(10))
        connection.recv.assert_called_once_with(timeout=0.25)
        connection.close.assert_called_once_with(code=1000, reason="service shutdown")

        connection.recv.side_effect = RuntimeError("never-log-this-token")
        with self.assertRaisesRegex(DhanFeedTransportError, "receive failed") as caught:
            socket.receive(0.25)
        self.assertNotIn("never-log-this-token", str(caught.exception))

    def test_missing_websockets_dependency_has_actionable_sanitized_error(self) -> None:
        with patch(
            "teco_quant.brokers.dhan_live.import_module", side_effect=ImportError
        ), self.assertRaisesRegex(DhanFeedDependencyError, "websockets") as caught:
            WebsocketsSyncTransport().connect(
                "wss://api-feed.dhan.co?token=never-log-this-token"
            )
        self.assertNotIn("never-log-this-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
