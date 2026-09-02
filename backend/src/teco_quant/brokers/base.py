"""Provider-agnostic transport contracts and safe throttling primitives."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from time import monotonic, sleep
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: Any
    text: str


class RestTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any | None = None,
        timeout: float = 10.0,
    ) -> TransportResponse: ...


class HttpxTransport:
    """Small httpx boundary that is easy to replace with a fake in tests."""

    def __init__(self) -> None:
        import httpx

        self._client = httpx.Client(follow_redirects=True)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any | None = None,
        timeout: float = 10.0,
    ) -> TransportResponse:
        response = self._client.request(
            method=method,
            url=url,
            headers=headers,
            json=json,
            timeout=timeout,
        )
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        return TransportResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=body,
            text=response.text,
        )

    def close(self) -> None:
        self._client.close()


class KeyedRateLimiter:
    """Thread-safe minimum-interval limiter with injectable time for deterministic tests."""

    def __init__(
        self,
        minimum_interval_seconds: float,
        *,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if minimum_interval_seconds < 0:
            raise ValueError("minimum interval cannot be negative")
        self._minimum_interval = minimum_interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._last_request: dict[str, float] = {}
        self._lock = Lock()

    def wait(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            previous = self._last_request.get(key)
            if previous is not None:
                remaining = self._minimum_interval - (now - previous)
                if remaining > 0:
                    self._sleeper(remaining)
                    now = self._clock()
            self._last_request[key] = now

