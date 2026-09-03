"""Dependency-free JSON WSGI surface for execution and market read models."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from math import isfinite
from typing import Any, Protocol, TypeAlias
from urllib.parse import parse_qsl, urlsplit

from teco_quant.api.market_leaders import MarketLeaderReader
from teco_quant.api.market_read_model import MarketWorkspaceReader
from teco_quant.execution.controller import ExecutionController
from teco_quant.execution.errors import ExecutionError
from teco_quant.execution.models import model_to_dict

StartResponse = Callable[[str, list[tuple[str, str]]], Any]
MarketFocusProvider: TypeAlias = Callable[[str, str, str | None], object]


@dataclass(frozen=True, slots=True)
class ApiConfig:
    api_key: str | None = field(default=None, repr=False)
    allowed_origins: tuple[str, ...] = ()
    max_body_bytes: int = 16_384

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_body_bytes, bool)
            or not isinstance(self.max_body_bytes, int)
            or self.max_body_bytes <= 0
        ):
            raise ValueError("max_body_bytes must be positive")
        if self.api_key is not None and not self.api_key:
            raise ValueError("api_key cannot be empty")
        if len(set(self.allowed_origins)) != len(self.allowed_origins):
            raise ValueError("allowed_origins cannot contain duplicates")
        for origin in self.allowed_origins:
            if not _is_exact_http_origin(origin):
                raise ValueError("allowed_origins must contain exact HTTP(S) origins")


class JsonWSGIApp:
    def __init__(
        self,
        controller: ExecutionController,
        config: ApiConfig | None = None,
        *,
        signal_history: SignalHistoryReader | None = None,
        market_reader: MarketWorkspaceReader | None = None,
        market_leaders: MarketLeaderReader | None = None,
        feed_health: FeedHealthProvider | None = None,
        market_focus: MarketFocusProvider | None = None,
    ) -> None:
        self.controller = controller
        self.config = config or ApiConfig()
        self.signal_history = signal_history
        self.market_reader = market_reader
        self.market_leaders = market_leaders
        self.feed_health = feed_health
        self.market_focus = market_focus

    def __call__(
        self, environ: Mapping[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        query_string = str(environ.get("QUERY_STRING", ""))
        origin = str(environ.get("HTTP_ORIGIN", ""))
        cors = self._cors_headers(origin)
        if method == "OPTIONS":
            return self._respond(
                start_response,
                204,
                None,
                cors
                + [
                    ("Access-Control-Allow-Methods", "GET,POST,OPTIONS"),
                    ("Access-Control-Allow-Headers", "Content-Type,X-API-Key"),
                ],
            )
        try:
            if method == "GET":
                return self._get(path, query_string, start_response, cors)
            if method == "POST":
                self._authorize(environ)
                body = self._read_json(environ)
                return self._post(path, body, start_response, cors)
            return self._error(start_response, 405, "METHOD_NOT_ALLOWED", "method not allowed", cors)
        except ApiError as exc:
            return self._error(start_response, exc.status, exc.code, str(exc), cors)
        except ExecutionError as exc:
            return self._error(start_response, 409, exc.code, str(exc), cors)
        except (KeyError, TypeError, ValueError) as exc:
            return self._error(start_response, 422, "INVALID_REQUEST", str(exc), cors)
        except Exception:  # noqa: BLE001 - final WSGI trust-boundary error envelope
            return self._error(
                start_response,
                500,
                "INTERNAL_ERROR",
                "internal server error",
                cors,
            )

    def _get(
        self,
        path: str,
        query_string: str,
        start_response: StartResponse,
        cors: list[tuple[str, str]],
    ) -> Iterable[bytes]:
        if path == "/health":
            _reject_query(query_string)
            status = self.controller.status()
            return self._respond(
                start_response,
                200,
                {
                    "status": "ok",
                    "mode": status["mode"],
                    "live_locked": not bool(status["live_enabled"]),
                },
                cors,
            )
        if path == "/status":
            _reject_query(query_string)
            status = dict(self.controller.status())
            status["market_data"] = self._market_data_status()
            return self._respond(start_response, 200, status, cors)
        if path == "/signals/latest":
            _reject_query(query_string)
            signal = (
                self.signal_history.latest()
                if self.signal_history is not None
                else self.controller.ledger.latest_signal()
            )
            return self._respond(
                start_response, 200, {"signal": signal}, cors
            )
        if path == "/paper/positions":
            _reject_query(query_string)
            return self._respond(
                start_response,
                200,
                {"positions": self.controller.ledger.positions()},
                cors,
            )
        if path == "/journal/summary":
            _reject_query(query_string)
            return self._respond(
                start_response, 200, self.controller.ledger.journal_summary(), cors
            )
        if path == "/markets":
            _reject_query(query_string)
            reader = self._require_market_reader()
            return self._respond(start_response, 200, reader.markets(), cors)
        if path == "/market/leaders":
            market_id = _market_leader_selection(query_string)
            return self._respond(
                start_response,
                200,
                self._require_market_leaders().leaders(market_id),
                cors,
            )
        market_methods = {
            "/contracts": "contract",
            "/market/workspace": "workspace",
            "/market/chain": "chain",
            "/market/analytics": "analytics",
        }
        method_name = market_methods.get(path)
        if method_name is not None:
            reader = self._require_market_reader()
            market, symbol, expiry = _market_selection(query_string)
            if self.market_focus is not None:
                self.market_focus(market, symbol, expiry)
            method = getattr(reader, method_name)
            value = method(market, symbol, expiry)
            if value is None:
                raise ApiError(
                    404,
                    "MARKET_SELECTION_NOT_FOUND",
                    "no market data matches the requested selection",
                )
            return self._respond(start_response, 200, value, cors)
        tick_prefix = "/market/ticks/"
        if path.startswith(tick_prefix):
            _reject_query(query_string)
            security_id = path[len(tick_prefix) :]
            if not re.fullmatch(r"[0-9]{1,32}", security_id):
                raise ApiError(
                    422,
                    "INVALID_QUERY",
                    "security_id must contain 1 to 32 decimal digits",
                )
            value = self._require_market_reader().latest_feed_tick(security_id)
            if value is None:
                raise ApiError(
                    404,
                    "TICK_NOT_FOUND",
                    "no accepted feed tick exists for the requested security_id",
                )
            return self._respond(start_response, 200, value, cors)
        if path == "/market/updates":
            reader = self._require_market_reader()
            after, timeout = _update_query(query_string)
            current = reader.revision
            if after > current:
                return self._respond(
                    start_response,
                    200,
                    {
                        "after": after,
                        "revision": current,
                        "changed": True,
                        "reset_required": True,
                        "event": None,
                    },
                    cors,
                )
            event = reader.wait_for_revision(after, timeout)
            revision = reader.revision
            return self._respond(
                start_response,
                200,
                {
                    "after": after,
                    "revision": revision,
                    "changed": event is not None,
                    "reset_required": False,
                    "event": None if event is None else event.as_payload(),
                },
                cors,
            )
        raise ApiError(404, "NOT_FOUND", "endpoint not found")

    def _require_market_reader(self) -> MarketWorkspaceReader:
        if self.market_reader is None:
            raise ApiError(
                503,
                "MARKET_DATA_UNAVAILABLE",
                "market read models are not configured",
            )
        return self.market_reader

    def _require_market_leaders(self) -> MarketLeaderReader:
        if self.market_leaders is None:
            raise ApiError(
                503,
                "MARKET_LEADERS_UNAVAILABLE",
                "market leader data is not configured",
            )
        return self.market_leaders

    def _market_data_status(self) -> dict[str, object]:
        revision = self.market_reader.revision if self.market_reader is not None else 0
        return {
            "read_model_configured": self.market_reader is not None,
            "revision": revision,
            "feed": _safe_feed_health(self.feed_health),
        }

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        start_response: StartResponse,
        cors: list[tuple[str, str]],
    ) -> Iterable[bytes]:
        prefix = "/signals/"
        suffix = "/approve"
        if path.startswith(prefix) and path.endswith(suffix):
            signal_id = path[len(prefix) : -len(suffix)]
            if not signal_id or "/" in signal_id:
                raise ApiError(404, "NOT_FOUND", "endpoint not found")
            approved_at = _optional_datetime(body.get("approved_at"))
            result = self.controller.approve(
                signal_id,
                actor=_required_text(body, "actor"),
                reason=_required_text(body, "reason"),
                approved_at=approved_at,
            )
            return self._respond(start_response, 200, model_to_dict(result), cors)
        if path == "/kill-switch":
            active = body.get("active")
            if not isinstance(active, bool):
                raise ApiError(422, "INVALID_REQUEST", "active must be boolean")
            self.controller.set_kill_switch(
                active=active,
                actor=_required_text(body, "actor"),
                reason=_required_text(body, "reason"),
                changed_at=_optional_datetime(body.get("changed_at")),
            )
            return self._respond(
                start_response,
                200,
                {"kill_switch": self.controller.ledger.kill_switch()},
                cors,
            )
        raise ApiError(404, "NOT_FOUND", "endpoint not found")

    def _authorize(self, environ: Mapping[str, Any]) -> None:
        if self.config.api_key is None:
            raise ApiError(503, "MUTATIONS_DISABLED", "mutation API key is not configured")
        supplied = str(environ.get("HTTP_X_API_KEY", ""))
        if not hmac.compare_digest(supplied, self.config.api_key):
            raise ApiError(401, "UNAUTHORIZED", "valid API key required")

    def _read_json(self, environ: Mapping[str, Any]) -> dict[str, Any]:
        raw_length = str(environ.get("CONTENT_LENGTH", "") or "")
        if raw_length:
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ApiError(400, "INVALID_BODY", "invalid Content-Length") from exc
            if length < 0:
                raise ApiError(400, "INVALID_BODY", "invalid Content-Length")
            if length > self.config.max_body_bytes:
                raise ApiError(413, "BODY_TOO_LARGE", "request body exceeds configured limit")
        else:
            length = self.config.max_body_bytes + 1
        stream = environ.get("wsgi.input")
        if stream is None:
            raise ApiError(400, "INVALID_BODY", "request body is missing")
        payload = stream.read(min(length, self.config.max_body_bytes + 1))
        if len(payload) > self.config.max_body_bytes:
            raise ApiError(413, "BODY_TOO_LARGE", "request body exceeds configured limit")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "INVALID_JSON", "request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ApiError(422, "INVALID_REQUEST", "JSON body must be an object")
        return value

    def _cors_headers(self, origin: str) -> list[tuple[str, str]]:
        if not origin:
            return []
        if origin in self.config.allowed_origins:
            return [
                ("Access-Control-Allow-Origin", origin),
                ("Vary", "Origin"),
            ]
        return []

    @staticmethod
    def _respond(
        start_response: StartResponse,
        status: int,
        value: Any,
        extra_headers: list[tuple[str, str]],
    ) -> Iterable[bytes]:
        payload = b"" if value is None else json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        reason = {
            200: "OK",
            204: "No Content",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            405: "Method Not Allowed",
            409: "Conflict",
            413: "Payload Too Large",
            422: "Unprocessable Entity",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }[status]
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ] + extra_headers
        start_response(f"{status} {reason}", headers)
        return [payload]

    @classmethod
    def _error(
        cls,
        start_response: StartResponse,
        status: int,
        code: str,
        message: str,
        headers: list[tuple[str, str]],
    ) -> Iterable[bytes]:
        return cls._respond(
            start_response,
            status,
            {"error": {"code": code, "message": message}},
            headers,
        )


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class SignalHistoryReader(Protocol):
    def latest(self) -> dict[str, object] | None: ...


class ApiSafeFeedHealth(Protocol):
    def as_dict(self) -> dict[str, object]: ...


class FeedHealthReader(Protocol):
    def health_snapshot(self) -> ApiSafeFeedHealth: ...


FeedHealthProvider: TypeAlias = (
    FeedHealthReader
    | Callable[[], ApiSafeFeedHealth | Mapping[str, object]]
)


_MAX_QUERY_LENGTH = 4_096
_MAX_QUERY_FIELDS = 8
_MAX_LONG_POLL_SECONDS = 30.0
_SAFE_FEED_STATES = frozenset(
    {
        "DISABLED",
        "CONFIG_REQUIRED",
        "INITIALIZING",
        "RUNNING",
        "PARTIAL",
        "ERROR",
        "STOPPED",
        "CONNECTING",
        "SUBSCRIBING",
        "WARMING",
        "HEALTHY",
        "STALE",
        "BACKING_OFF",
        "STOPPING",
    }
)


def _is_exact_http_origin(value: object) -> bool:
    if not isinstance(value, str) or not value or value == "*" or value.endswith("/"):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
        and (port is None or 1 <= port <= 65_535)
    )


def _parse_query(
    query_string: str, *, allowed: frozenset[str]
) -> dict[str, str]:
    if not query_string:
        return {}
    if len(query_string) > _MAX_QUERY_LENGTH:
        raise ApiError(422, "INVALID_QUERY", "query string is too long")
    if not _has_valid_percent_encoding(query_string):
        raise ApiError(422, "INVALID_QUERY", "query string is malformed")
    try:
        pairs = parse_qsl(
            query_string,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=_MAX_QUERY_FIELDS,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(422, "INVALID_QUERY", "query string is malformed") from exc
    values: dict[str, str] = {}
    for name, value in pairs:
        if name not in allowed:
            raise ApiError(422, "INVALID_QUERY", f"unsupported query parameter: {name}")
        if name in values:
            raise ApiError(422, "INVALID_QUERY", f"duplicate query parameter: {name}")
        values[name] = value
    return values


def _reject_query(query_string: str) -> None:
    if _parse_query(query_string, allowed=frozenset()):
        raise ApiError(422, "INVALID_QUERY", "this endpoint does not accept query parameters")


def _has_valid_percent_encoding(value: str) -> bool:
    hexadecimal = frozenset("0123456789abcdefABCDEF")
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in hexadecimal
            or value[index + 2] not in hexadecimal
        ):
            return False
        index += 3
    return True


def _query_text(
    values: Mapping[str, str],
    name: str,
    *,
    required: bool,
    maximum_length: int,
) -> str | None:
    raw = values.get(name)
    if raw is None:
        if required:
            raise ApiError(422, "INVALID_QUERY", f"{name} query parameter is required")
        return None
    value = raw.strip()
    if not value:
        raise ApiError(422, "INVALID_QUERY", f"{name} query parameter cannot be empty")
    if len(value) > maximum_length or any(ord(character) < 32 for character in value):
        raise ApiError(422, "INVALID_QUERY", f"{name} query parameter is invalid")
    return value


def _market_selection(query_string: str) -> tuple[str, str, str | None]:
    values = _parse_query(
        query_string, allowed=frozenset({"market", "symbol", "expiry"})
    )
    market = _query_text(values, "market", required=True, maximum_length=64)
    symbol = _query_text(values, "symbol", required=True, maximum_length=128)
    expiry = _query_text(values, "expiry", required=False, maximum_length=64)
    assert market is not None and symbol is not None
    if expiry is not None:
        _validate_expiry(expiry)
    return market, symbol, expiry


def _market_leader_selection(query_string: str) -> str:
    values = _parse_query(query_string, allowed=frozenset({"market"}))
    market = _query_text(values, "market", required=True, maximum_length=64)
    assert market is not None
    return market


def _validate_expiry(value: str) -> None:
    if "T" not in value and " " not in value:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ApiError(
                422, "INVALID_QUERY", "expiry must be an ISO-8601 date or timestamp"
            ) from exc
        return
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApiError(
            422, "INVALID_QUERY", "expiry must be an ISO-8601 date or timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError(422, "INVALID_QUERY", "expiry timestamp must include a timezone")


def _update_query(query_string: str) -> tuple[int, float]:
    values = _parse_query(
        query_string, allowed=frozenset({"after", "timeout"})
    )
    raw_after = values.get("after", "0")
    if not re.fullmatch(r"[0-9]{1,20}", raw_after):
        raise ApiError(422, "INVALID_QUERY", "after must be a non-negative integer")
    after = int(raw_after)
    raw_timeout = values.get("timeout", "15")
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ApiError(422, "INVALID_QUERY", "timeout must be a number") from exc
    if not isfinite(timeout) or not 0 <= timeout <= _MAX_LONG_POLL_SECONDS:
        raise ApiError(
            422,
            "INVALID_QUERY",
            f"timeout must be between 0 and {_MAX_LONG_POLL_SECONDS:g} seconds",
        )
    return after, timeout


def _safe_feed_health(reader: FeedHealthProvider | None) -> dict[str, object]:
    if reader is None:
        return {
            "configured": False,
            "state": "NOT_CONFIGURED",
            "connected": False,
            "healthy": False,
        }
    try:
        snapshot = reader() if callable(reader) else reader.health_snapshot()
        raw = snapshot if isinstance(snapshot, Mapping) else snapshot.as_dict()
    except Exception:  # noqa: BLE001 - optional provider health must fail closed
        return {
            "configured": True,
            "state": "UNAVAILABLE",
            "connected": False,
            "healthy": False,
            "last_error": "feed health could not be read",
        }
    if not isinstance(raw, Mapping):
        return {
            "configured": True,
            "state": "UNAVAILABLE",
            "connected": False,
            "healthy": False,
            "last_error": "feed health returned an invalid response",
        }
    state = raw.get("state")
    safe_state = state if isinstance(state, str) and state in _SAFE_FEED_STATES else "UNAVAILABLE"
    result: dict[str, object] = {
        "configured": raw.get("configured") is not False,
        "state": safe_state,
        "connected": raw.get("connected") is True,
        "healthy": raw.get("healthy") is True,
    }
    for name in (
        "generation",
        "expected_instruments",
        "ready_instruments",
        "subscription_batches",
        "packets_received",
        "packets_rejected",
        "trade_timestamp_rejections",
        "replayed_packets",
        "market_status_packets",
        "reconnect_count",
        "consecutive_failures",
        "subscriptions_count",
        "attempted_markets",
        "accepted_markets",
        "published_markets",
        "data_successful_markets",
        "successful_markets",
        "failed_markets",
    ):
        value = raw.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[name] = value
    for name in (
        "last_connected_at",
        "last_packet_at",
        "last_trade_at",
        "last_market_status_at",
        "last_heartbeat_at",
        "last_healthy_at",
        "last_master_refresh",
        "last_cycle_started_at",
        "last_cycle_completed_at",
        "last_success_at",
    ):
        value = raw.get(name)
        result[name] = value if value is None or isinstance(value, str) else None
    retry = raw.get("next_retry_seconds")
    result["next_retry_seconds"] = (
        retry
        if not isinstance(retry, bool)
        and isinstance(retry, (int, float))
        and isfinite(float(retry))
        and retry >= 0
        else None
    )
    packet_age = raw.get("packet_age_seconds")
    result["packet_age_seconds"] = (
        packet_age
        if not isinstance(packet_age, bool)
        and isinstance(packet_age, (int, float))
        and isfinite(float(packet_age))
        and packet_age >= 0
        else None
    )
    trade_age = raw.get("trade_age_seconds")
    result["trade_age_seconds"] = (
        trade_age
        if not isinstance(trade_age, bool)
        and isinstance(trade_age, (int, float))
        and isfinite(float(trade_age))
        else None
    )
    market_status = raw.get("market_status")
    safe_market_status = (
        market_status
        if isinstance(market_status, str)
        and market_status in {"UNKNOWN", "OPEN", "CLOSED"}
        else "UNKNOWN"
    )
    result["market_status"] = safe_market_status
    result["market_status_known"] = safe_market_status != "UNKNOWN"
    result["market_open"] = {
        "UNKNOWN": None,
        "OPEN": True,
        "CLOSED": False,
    }[safe_market_status]
    for name in ("provider", "acquisition_state", "socket_state", "master_batch_id"):
        value = raw.get(name)
        if isinstance(value, str) and value:
            result[name] = value[:128]
    result["decision_inputs_configured"] = (
        raw.get("decision_inputs_configured") is True
    )
    for name in ("transport_healthy", "data_healthy", "actionable_ready"):
        result[name] = raw.get(name) is True
    for name in ("master_error_code", "callback_error_code"):
        value = raw.get(name)
        result[name] = (
            value
            if isinstance(value, str)
            and bool(re.fullmatch(r"[A-Z0-9_]{1,64}", value))
            else None
        )
    missing = raw.get("missing_instruments")
    if isinstance(missing, (list, tuple)):
        result["missing_instruments"] = [
            value[:128]
            for value in missing[:5_000]
            if isinstance(value, str) and value
        ]
    reported_error = raw.get("last_error", raw.get("error"))
    result["last_error"] = (
        None if reported_error in (None, "") else "feed supervisor reported an error"
    )
    markets = raw.get("markets")
    if isinstance(markets, Mapping):
        safe_markets: dict[str, object] = {}
        for raw_symbol, raw_market in list(markets.items())[:100]:
            if (
                not isinstance(raw_symbol, str)
                or not re.fullmatch(r"[A-Z0-9_.-]{1,32}", raw_symbol)
                or not isinstance(raw_market, Mapping)
            ):
                continue
            market: dict[str, object] = {
                "accepted": raw_market.get("accepted") is True,
            }
            for name in ("last_attempt_at", "last_success_at"):
                value = raw_market.get(name)
                market[name] = value if value is None or isinstance(value, str) else None
            age = raw_market.get("data_age_seconds")
            market["data_age_seconds"] = (
                age
                if not isinstance(age, bool)
                and isinstance(age, (int, float))
                and isfinite(float(age))
                and age >= 0
                else None
            )
            snapshot_id = raw_market.get("snapshot_id")
            market["snapshot_id"] = (
                snapshot_id[:128]
                if isinstance(snapshot_id, str) and snapshot_id
                else None
            )
            error_code = raw_market.get("error_code")
            market["error_code"] = (
                error_code
                if isinstance(error_code, str)
                and bool(re.fullmatch(r"[A-Z0-9_]{1,64}", error_code))
                else None
            )
            validation_issues = raw_market.get("validation_issues")
            if isinstance(validation_issues, (list, tuple)):
                market["validation_issues"] = [
                    value
                    for value in validation_issues[:64]
                    if isinstance(value, str)
                    and bool(re.fullmatch(r"[A-Z0-9_]{1,64}", value))
                ]
            else:
                market["validation_issues"] = []
            safe_markets[raw_symbol] = market
        result["markets"] = safe_markets
    return result


def _required_text(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(422, "INVALID_REQUEST", f"{name} is required")
    return value.strip()


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(422, "INVALID_REQUEST", "timestamp must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(422, "INVALID_REQUEST", "timestamp must be ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ApiError(422, "INVALID_REQUEST", "timestamp must include a timezone")
    return result
