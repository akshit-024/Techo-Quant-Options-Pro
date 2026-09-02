"""Dependency-free local HTTP server for :class:`~teco_quant.api.JsonWSGIApp`."""

from __future__ import annotations

import json
import logging
import signal
from collections.abc import Callable
from socketserver import ThreadingMixIn
from threading import Event, current_thread, main_thread
from types import FrameType
from typing import Any
from wsgiref.simple_server import WSGIServer, make_server

from teco_quant.brokers.dhan import DhanCredentials
from teco_quant.config import RuntimeSettings
from teco_quant.market_data_runtime import attach_dhan_market_data
from teco_quant.runtime import BackendRuntime, create_runtime

ReadyWriter = Callable[[dict[str, object]], None]


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Serve concurrent polls and drain every handler before resource teardown.

    Request threads must remain non-daemon. ``ThreadingMixIn.server_close()`` tracks and
    joins only non-daemon request threads; this guarantees that the surrounding runtime
    context cannot close read models or databases while a handler is still using them.
    API long polls are independently capped, so the drain remains bounded.
    """

    daemon_threads = False
    block_on_close = True
    # Longer than the API's 30-second maximum long poll, but finite so a client that
    # stalls while sending a request body or receiving a response cannot prevent drain
    # forever during shutdown.
    request_socket_timeout_seconds = 35.0

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        request.settimeout(self.request_socket_timeout_seconds)
        super().process_request_thread(request, client_address)


def serve(
    settings: RuntimeSettings,
    *,
    dhan_credentials: DhanCredentials | None = None,
    stop_event: Event | None = None,
    ready_writer: ReadyWriter | None = None,
) -> int:
    """Compose and host the backend until a termination signal is received."""

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    with create_runtime(settings) as runtime:
        attach_dhan_market_data(runtime, credentials=dhan_credentials)
        run_server(runtime, stop_event=stop_event, ready_writer=ready_writer)
    return 0


def run_server(
    runtime: BackendRuntime,
    *,
    stop_event: Event | None = None,
    ready_writer: ReadyWriter | None = None,
) -> None:
    """Host an already composed runtime, allowing integrations to attach cleanup hooks."""

    stop = stop_event or Event()
    server = make_server(
        runtime.settings.server_host,
        runtime.settings.server_port,
        runtime.application,
        server_class=ThreadingWSGIServer,
    )
    previous_handlers: dict[int, Any] = {}
    try:
        server.timeout = 0.5
        previous_handlers = _install_stop_handlers(stop)
        writer = ready_writer or _print_ready
        writer(
            {
                "status": "listening",
                "url": _display_url(server, runtime.settings.server_host),
                "mode": runtime.settings.execution_mode,
                "live_order_execution": "disabled",
                "dhan_live_feed_requested": runtime.settings.dhan_live_enabled,
                "allowed_origins": list(runtime.settings.allowed_origins),
                "mutations_enabled": runtime.settings.api_key is not None,
            }
        )
        while not stop.is_set():
            server.handle_request()
    finally:
        _restore_handlers(previous_handlers)
        server.server_close()


def _display_url(server: WSGIServer, configured_host: str) -> str:
    host = configured_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{server.server_port}"


def _print_ready(status: dict[str, object]) -> None:
    print(json.dumps(status, sort_keys=True), flush=True)


def _install_stop_handlers(stop_event: Event) -> dict[int, Any]:
    if current_thread() is not main_thread():
        return {}

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop_event.set()

    previous: dict[int, Any] = {}
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        previous[int(stop_signal)] = signal.getsignal(stop_signal)
        signal.signal(stop_signal, request_stop)
    return previous


def _restore_handlers(previous: dict[int, Any]) -> None:
    if current_thread() is not main_thread():
        return
    for signal_number, handler in previous.items():
        signal.signal(signal_number, handler)
