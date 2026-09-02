from __future__ import annotations

import unittest
from datetime import date
from struct import pack

from teco_quant.brokers.base import KeyedRateLimiter, TransportResponse
from teco_quant.brokers.dhan import (
    DhanAuthenticationError,
    DhanCredentials,
    DhanFeedMode,
    DhanRestClient,
    decode_feed_packet,
    live_feed_url,
    subscription_messages,
)


class FakeTransport:
    def __init__(self, responses: list[TransportResponse]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, json=None, timeout=10.0):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


def response(body, status=200, text="") -> TransportResponse:
    return TransportResponse(status_code=status, headers={}, body=body, text=text)


class DhanAdapterTests(unittest.TestCase):
    def credentials(self) -> DhanCredentials:
        return DhanCredentials(client_id="1000000001", access_token="secret-token")

    def test_credentials_repr_and_client_surface_do_not_expose_secrets(self) -> None:
        credentials = self.credentials()
        client = DhanRestClient(credentials, transport=FakeTransport([]))

        self.assertNotIn("1000000001", repr(credentials))
        self.assertNotIn("secret-token", repr(credentials))
        self.assertFalse(hasattr(client, "headers"))

    def client(self, transport: FakeTransport) -> DhanRestClient:
        no_wait = KeyedRateLimiter(0)
        return DhanRestClient(
            self.credentials(),
            transport=transport,
            option_chain_limiter=no_wait,
            quote_limiter=no_wait,
        )

    def test_option_chain_request_uses_current_v2_contract(self) -> None:
        transport = FakeTransport(
            [response({"status": "success", "data": {"last_price": 1, "oc": {}}})]
        )
        payload = self.client(transport).option_chain(
            underlying_security_id=13,
            underlying_segment="IDX_I",
            expiry=date(2026, 8, 27),
        )
        self.assertEqual(payload["status"], "success")
        request = transport.requests[0]
        self.assertTrue(request["url"].endswith("/v2/optionchain"))
        self.assertEqual(request["headers"]["access-token"], "secret-token")
        self.assertEqual(request["headers"]["client-id"], "1000000001")
        self.assertEqual(request["json"]["UnderlyingScrip"], 13)
        self.assertEqual(request["json"]["Expiry"], "2026-08-27")

    def test_market_quote_enforces_provider_limit(self) -> None:
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, "1,000"):
            self.client(transport).market_quote({"NSE_FNO": list(range(1001))})

    def test_authentication_errors_are_sanitized(self) -> None:
        transport = FakeTransport([response({"errorCode": "DH-901"}, status=401)])
        with self.assertRaises(DhanAuthenticationError) as caught:
            self.client(transport).profile()
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertNotIn("secret-token", repr(self.credentials()))

    def test_live_subscription_batches_and_uses_full_mode(self) -> None:
        instruments = [("NSE_FNO", str(value)) for value in range(205)]
        messages = subscription_messages(instruments, mode=DhanFeedMode.FULL)
        self.assertEqual([message["InstrumentCount"] for message in messages], [100, 100, 5])
        self.assertTrue(all(message["RequestCode"] == 21 for message in messages))
        url = live_feed_url(self.credentials())
        self.assertIn("version=2", url)
        self.assertIn("authType=2", url)

    def test_binary_ticker_packet_is_length_checked_and_decoded(self) -> None:
        packet = pack("<BHBI", 2, 16, 1, 12345) + pack("<fI", 100.5, 1_700_000_000)
        decoded = decode_feed_packet(packet)
        self.assertEqual(decoded.security_id, "12345")
        self.assertAlmostEqual(decoded.fields["last_price"], 100.5)
        with self.assertRaisesRegex(ValueError, "length mismatch"):
            decode_feed_packet(packet + b"\x00")

    def test_full_packet_decodes_ohlc_oi_and_five_depth_levels(self) -> None:
        payload = pack(
            "<fHIfIIIIIIffff",
            100.5,
            25,
            1_700_000_000,
            99.5,
            10_000,
            2_000,
            2_500,
            15_000,
            16_000,
            14_000,
            98.0,
            97.5,
            102.0,
            96.0,
        )
        depth = b"".join(
            pack(
                "<IIHHff",
                100 + level,
                200 + level,
                2 + level,
                3 + level,
                100.0 - level,
                101.0 + level,
            )
            for level in range(5)
        )
        packet = pack("<BHBI", 8, 162, 2, 50001) + payload + depth

        decoded = decode_feed_packet(packet)

        self.assertEqual(decoded.fields["open_interest"], 15_000)
        self.assertAlmostEqual(decoded.fields["day_high"], 102.0)
        self.assertEqual(len(decoded.depth), 5)
        self.assertAlmostEqual(decoded.depth[0].bid_price, 100.0)
        self.assertAlmostEqual(decoded.depth[4].ask_price, 105.0)

        oversized = bytearray(packet + b"\x00")
        oversized[1:3] = pack("<H", len(oversized))
        with self.assertRaisesRegex(ValueError, "expected=162"):
            decode_feed_packet(bytes(oversized))

    def test_unsupported_feed_packets_fail_closed(self) -> None:
        packet = pack("<BHBI", 7, 8, 1, 12345)
        with self.assertRaisesRegex(ValueError, "unsupported Dhan feed response code"):
            decode_feed_packet(packet)


if __name__ == "__main__":
    unittest.main()
