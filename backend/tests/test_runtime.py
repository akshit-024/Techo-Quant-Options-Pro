from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from http.client import HTTPException
from pathlib import Path
from threading import Event, Thread
from urllib.request import Request, urlopen

from teco_quant.config import RuntimeSettings
from teco_quant.runtime import create_runtime
from teco_quant.server import run_server


class _BlockingMarketReader:
    """Minimal long-poll reader whose release is controlled by the test."""

    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.finished = Event()

    @property
    def revision(self) -> int:
        return 0

    def wait_for_revision(self, after: int, timeout: float = 15.0) -> None:
        del after
        self.entered.set()
        self.release.wait(timeout)
        self.finished.set()


class BackendRuntimeTests(unittest.TestCase):
    def settings(self, directory: str, **changes: object) -> RuntimeSettings:
        base = RuntimeSettings(
            database_path=str(Path(directory) / "market.db"),
            execution_database_path=str(Path(directory) / "execution.db"),
            signal_history_database_path=str(Path(directory) / "signals.db"),
        )
        return replace(base, **changes)

    def test_composition_is_data_only_and_closes_attached_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            closed: list[str] = []
            runtime = create_runtime(self.settings(directory))
            runtime.add_cleanup(lambda: closed.append("integration"))

            status = runtime.controller.status()
            self.assertEqual(status["mode"], "DATA_ONLY")
            self.assertFalse(status["live_enabled"])
            self.assertFalse(status["live_gateway_configured"])
            self.assertEqual(runtime.repository.accepted_snapshot_count(), 0)
            self.assertIs(runtime.application.market_reader, runtime.market_read_model)
            self.assertEqual(runtime.market_read_model.revision, 0)

            runtime.close()
            runtime.close()

            self.assertEqual(closed, ["integration"])
            self.assertTrue((Path(directory) / "market.db").is_file())
            self.assertTrue((Path(directory) / "execution.db").is_file())
            self.assertTrue((Path(directory) / "signals.db").is_file())

    def test_paper_mode_is_local_and_database_paths_must_be_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with create_runtime(
                self.settings(directory, execution_mode="PAPER_TRADING")
            ) as runtime:
                status = runtime.controller.status()
                self.assertEqual(status["mode"], "PAPER_TRADING")
                self.assertFalse(status["live_enabled"])
                self.assertFalse(status["live_gateway_configured"])

            shared = str(Path(directory) / "shared.db")
            invalid = RuntimeSettings(
                database_path=shared,
                execution_database_path=shared,
                signal_history_database_path=str(Path(directory) / "other.db"),
            )
            with self.assertRaisesRegex(ValueError, "distinct paths"):
                create_runtime(invalid)

    def test_real_http_server_serves_health_with_exact_cors_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(
                directory,
                server_host="127.0.0.1",
                server_port=0,
                allowed_origins=("http://localhost:5173",),
                dhan_live_enabled=True,
            )
            with create_runtime(settings) as runtime:
                stop = Event()
                listening = Event()
                status: dict[str, object] = {}

                def ready(value: dict[str, object]) -> None:
                    status.update(value)
                    listening.set()

                thread = Thread(
                    target=run_server,
                    kwargs={
                        "runtime": runtime,
                        "stop_event": stop,
                        "ready_writer": ready,
                    },
                    daemon=True,
                )
                thread.start()
                self.assertTrue(listening.wait(3), "server did not start")
                url = str(status["url"])
                request = Request(
                    url + "/health",
                    headers={"Origin": "http://localhost:5173"},
                )
                with urlopen(request, timeout=3) as response:
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers["Access-Control-Allow-Origin"],
                        "http://localhost:5173",
                    )
                self.assertEqual(payload["mode"], "DATA_ONLY")
                self.assertTrue(payload["live_locked"])
                self.assertEqual(status["live_order_execution"], "disabled")

                with urlopen(url + "/status", timeout=3) as response:
                    status_payload = json.loads(response.read())
                feed = status_payload["market_data"]["feed"]
                self.assertEqual(feed["state"], "CONFIG_REQUIRED")
                self.assertFalse(feed["configured"])

                stop.set()
                thread.join(3)
                self.assertFalse(thread.is_alive(), "server did not stop gracefully")

    def test_server_drains_inflight_long_poll_before_runtime_can_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(
                directory,
                server_host="127.0.0.1",
                server_port=0,
            )
            with create_runtime(settings) as runtime:
                reader = _BlockingMarketReader()
                runtime.application.market_reader = reader
                stop = Event()
                listening = Event()
                server_returned = Event()
                request_finished = Event()
                status: dict[str, object] = {}
                request_errors: list[BaseException] = []

                def ready(value: dict[str, object]) -> None:
                    status.update(value)
                    listening.set()

                def host() -> None:
                    try:
                        run_server(
                            runtime,
                            stop_event=stop,
                            ready_writer=ready,
                        )
                    finally:
                        server_returned.set()

                server_thread = Thread(target=host, daemon=True)
                server_thread.start()
                self.assertTrue(listening.wait(3), "server did not start")
                url = str(status["url"])

                def long_poll() -> None:
                    try:
                        with urlopen(
                            url + "/market/updates?after=0&timeout=5",
                            timeout=6,
                        ) as response:
                            response.read()
                    except (HTTPException, OSError) as exc:
                        request_errors.append(exc)
                    finally:
                        request_finished.set()

                request_thread = Thread(target=long_poll, daemon=True)
                request_thread.start()
                self.assertTrue(reader.entered.wait(3), "long poll did not enter the app")

                # Threading support remains active: a health request completes while the
                # first handler is still blocked in its bounded long poll.
                with urlopen(url + "/health", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                self.assertFalse(request_finished.is_set())

                stop.set()
                # handle_request() polls at 0.5 seconds. An implementation that doesn't
                # track non-daemon handlers would return within this window and let the
                # surrounding context close runtime resources under the live request.
                self.assertFalse(server_returned.wait(0.75))
                self.assertFalse(request_finished.is_set())

                reader.release.set()
                self.assertTrue(request_finished.wait(3), "long poll did not finish")
                self.assertTrue(reader.finished.is_set())
                self.assertTrue(server_returned.wait(3), "server did not drain handlers")
                server_thread.join(1)
                request_thread.join(1)
                self.assertEqual(request_errors, [])


if __name__ == "__main__":
    unittest.main()
