from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from io import BytesIO

from teco_quant.api import ApiConfig, JsonWSGIApp, MarketReadModelEvent
from teco_quant.execution.controller import ExecutionController, ExecutionPolicy
from teco_quant.execution.ledger import ExecutionLedger
from teco_quant.execution.models import ExecutionMode

NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


class FakeMarketReader:
    def __init__(self) -> None:
        self.revision = 7
        self.calls: list[tuple[object, ...]] = []

    def markets(self, *, now=None):
        del now
        return {"generated_at": NOW.isoformat(), "markets": [{"market_id": "NIFTY"}]}

    def contract(self, market_id, symbol, expiry=None, *, now=None):
        del now
        return self._selection("contract", market_id, symbol, expiry)

    def workspace(self, market_id, symbol, expiry=None, *, now=None):
        del now
        return self._selection("workspace", market_id, symbol, expiry)

    def chain(self, market_id, symbol, expiry=None, *, now=None):
        del now
        return self._selection("chain", market_id, symbol, expiry)

    def analytics(self, market_id, symbol, expiry=None, *, now=None):
        del now
        return self._selection("analytics", market_id, symbol, expiry)

    def latest_feed_tick(self, security_id, *, now=None):
        del now
        self.calls.append(("tick", security_id))
        if security_id != "123":
            return None
        return {"security_id": security_id, "revision": self.revision, "actionable": False}

    def wait_for_revision(self, after, timeout=15.0):
        self.calls.append(("updates", after, timeout))
        if after >= self.revision:
            return None
        return MarketReadModelEvent(
            revision=self.revision,
            event_type="WORKSPACE",
            occurred_at=NOW,
            market_id="NIFTY",
            symbol="NIFTY",
            expiry="2026-08-27T15:30:00+05:30",
            snapshot_id="snapshot-7",
        )

    def _selection(self, kind, market_id, symbol, expiry):
        self.calls.append((kind, market_id, symbol, expiry))
        if symbol.upper() == "UNKNOWN":
            return None
        return {
            "kind": kind,
            "selection": {
                "market_id": market_id,
                "symbol": symbol,
                "expiry": expiry,
            },
        }


class MarketApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = ExecutionLedger()
        self.addCleanup(self.ledger.close)
        controller = ExecutionController(
            ledger=self.ledger,
            instrument_registry={},
            policy=ExecutionPolicy(mode=ExecutionMode.DATA_ONLY),
            clock=lambda: NOW,
        )
        self.reader = FakeMarketReader()
        self.app = JsonWSGIApp(
            controller,
            ApiConfig(allowed_origins=("http://127.0.0.1:5173",)),
            market_reader=self.reader,
            feed_health=lambda: {
                "state": "HEALTHY",
                "connected": True,
                "healthy": True,
                "generation": 2,
                "expected_instruments": 12,
                "ready_instruments": 12,
                "missing_instruments": [],
                "trade_timestamp_rejections": 3,
                "replayed_packets": 1,
                "market_status_packets": 2,
                "last_packet_at": NOW.isoformat(),
                "last_trade_at": NOW.isoformat(),
                "trade_age_seconds": -0.25,
                "market_status": "OPEN",
                "market_status_known": True,
                "market_open": True,
                "last_market_status_at": NOW.isoformat(),
                "attempted_markets": 4,
                "accepted_markets": 3,
                "published_markets": 2,
                "data_successful_markets": 3,
                "successful_markets": 2,
                "last_error": "authenticated URL contained super-secret-token",
                "access_token": "super-secret-token",
            },
        )

    def call(
        self,
        path: str,
        query: str = "",
        *,
        origin: str | None = None,
        app: JsonWSGIApp | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str], bytes]:
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": "0",
            "wsgi.input": BytesIO(),
        }
        if origin is not None:
            environ["HTTP_ORIGIN"] = origin
        captured: dict[str, object] = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        raw = b"".join((app or self.app)(environ, start_response))
        payload = json.loads(raw)
        return (
            int(str(captured["status"]).split()[0]),
            payload,
            captured["headers"],
            raw,
        )

    def test_catalog_selection_and_tick_routes(self) -> None:
        status, payload, headers, _ = self.call(
            "/markets", origin="http://127.0.0.1:5173"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["markets"][0]["market_id"], "NIFTY")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://127.0.0.1:5173")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

        query = "market=nifty&symbol=NIFTY&expiry=2026-08-27"
        for path, kind in (
            ("/contracts", "contract"),
            ("/market/workspace", "workspace"),
            ("/market/chain", "chain"),
            ("/market/analytics", "analytics"),
        ):
            with self.subTest(path=path):
                status, payload, _, _ = self.call(path, query)
                self.assertEqual(status, 200)
                self.assertEqual(payload["kind"], kind)
                self.assertEqual(payload["selection"]["expiry"], "2026-08-27")

        status, payload, _, _ = self.call("/market/ticks/123")
        self.assertEqual(status, 200)
        self.assertEqual(payload["security_id"], "123")
        self.assertFalse(payload["actionable"])

    def test_long_poll_changed_timeout_and_restart_reset(self) -> None:
        status, payload, _, _ = self.call(
            "/market/updates", "after=6&timeout=0"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["changed"])
        self.assertFalse(payload["reset_required"])
        self.assertEqual(payload["event"]["snapshot_id"], "snapshot-7")
        self.assertEqual(self.reader.calls[-1], ("updates", 6, 0.0))

        status, payload, _, _ = self.call(
            "/market/updates", "after=7&timeout=0"
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["changed"])
        self.assertIsNone(payload["event"])

        status, payload, _, _ = self.call(
            "/market/updates", "after=99&timeout=30"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["changed"])
        self.assertTrue(payload["reset_required"])
        self.assertEqual(payload["revision"], 7)
        self.assertNotEqual(self.reader.calls[-1], ("updates", 99, 30.0))

    def test_queries_fail_closed_with_stable_error_envelopes(self) -> None:
        cases = (
            ("/contracts", "symbol=NIFTY", "INVALID_QUERY"),
            ("/contracts", "market=NIFTY&market=SENSEX&symbol=NIFTY", "INVALID_QUERY"),
            ("/contracts", "market=NIFTY&symbol=%ZZ", "INVALID_QUERY"),
            ("/contracts", "market=NIFTY&symbol=NIFTY&extra=x", "INVALID_QUERY"),
            ("/contracts", "market=NIFTY&symbol=NIFTY&expiry=not-a-date", "INVALID_QUERY"),
            ("/contracts", "market=NIFTY&symbol=UNKNOWN", "MARKET_SELECTION_NOT_FOUND"),
            ("/market/ticks/not-numeric", "", "INVALID_QUERY"),
            ("/market/ticks/999", "", "TICK_NOT_FOUND"),
            ("/market/updates", "after=-1&timeout=0", "INVALID_QUERY"),
            ("/market/updates", "after=0&timeout=31", "INVALID_QUERY"),
            ("/health", "unexpected=true", "INVALID_QUERY"),
        )
        for path, query, code in cases:
            with self.subTest(path=path, query=query):
                status, payload, _, _ = self.call(path, query)
                self.assertIn(status, {404, 422})
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(set(payload), {"error"})

    def test_status_adds_redacted_serializable_feed_health(self) -> None:
        status, payload, _, raw = self.call("/status")
        self.assertEqual(status, 200)
        for existing in (
            "mode",
            "live_enabled",
            "live_gateway_configured",
            "kill_switch",
            "counts",
        ):
            self.assertIn(existing, payload)
        market_data = payload["market_data"]
        self.assertTrue(market_data["read_model_configured"])
        self.assertEqual(market_data["revision"], 7)
        self.assertTrue(market_data["feed"]["healthy"])
        self.assertEqual(
            market_data["feed"]["last_error"], "feed supervisor reported an error"
        )
        self.assertEqual(market_data["feed"]["trade_timestamp_rejections"], 3)
        self.assertEqual(market_data["feed"]["replayed_packets"], 1)
        self.assertEqual(market_data["feed"]["market_status"], "OPEN")
        self.assertTrue(market_data["feed"]["market_status_known"])
        self.assertTrue(market_data["feed"]["market_open"])
        self.assertEqual(market_data["feed"]["trade_age_seconds"], -0.25)
        self.assertEqual(market_data["feed"]["attempted_markets"], 4)
        self.assertEqual(market_data["feed"]["accepted_markets"], 3)
        self.assertEqual(market_data["feed"]["published_markets"], 2)
        self.assertEqual(market_data["feed"]["data_successful_markets"], 3)
        self.assertEqual(market_data["feed"]["successful_markets"], 2)
        self.assertNotIn(b"super-secret-token", raw)
        self.assertNotIn("access_token", market_data["feed"])

    def test_optional_integrations_and_exact_cors_are_fail_closed(self) -> None:
        controller = self.app.controller
        unconfigured = JsonWSGIApp(controller)
        status, payload, _, _ = self.call("/markets", app=unconfigured)
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "MARKET_DATA_UNAVAILABLE")

        status, payload, _, _ = self.call("/status", app=unconfigured)
        self.assertEqual(status, 200)
        self.assertFalse(payload["market_data"]["read_model_configured"])
        self.assertEqual(payload["market_data"]["feed"]["state"], "NOT_CONFIGURED")

        credential_required = JsonWSGIApp(
            controller,
            market_reader=self.reader,
            feed_health=lambda: {
                "state": "CONFIG_REQUIRED",
                "error": "token=must-not-leak",
            },
        )
        status, payload, _, raw = self.call("/status", app=credential_required)
        self.assertEqual(status, 200)
        self.assertEqual(payload["market_data"]["feed"]["state"], "CONFIG_REQUIRED")
        self.assertNotIn(b"must-not-leak", raw)

        _, _, headers, _ = self.call(
            "/health", origin="http://localhost:5173"
        )
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        with self.assertRaises(ValueError):
            ApiConfig(allowed_origins=("*",))
        with self.assertRaises(ValueError):
            ApiConfig(max_body_bytes=1.5)  # type: ignore[arg-type]
        secret_config = ApiConfig(api_key="do-not-print")
        self.assertNotIn("do-not-print", repr(secret_config))


if __name__ == "__main__":
    unittest.main()
