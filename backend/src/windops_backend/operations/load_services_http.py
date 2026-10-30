"""Finite, private GET observations for owned service controls and recovery.

These samples stay separate from business throughput. An expected 401/404 control
can pass its own predicate but can never be reported as a successful business read.
"""

from __future__ import annotations

import asyncio
import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from windops_backend.operations.load_runner import (
    _PATH,
    PrivateUser,
    _loopback_origin,
    _private_map,
    _quiet_transport,
    _settle,
    _strict_json,
)


class ObservationError(RuntimeError):
    """A fixed public code, never response data or exception details."""


@dataclass
class ObservationSample:
    route_id: str
    user_id: str
    sequence: int
    outcome: str = "not_run"
    started: bool = False
    elapsed_ms: float | None = None
    http_status: int | None = None
    response_bytes: int | None = None
    reason_code: str | None = None


class ServicesHttp:
    def __init__(
        self,
        *,
        origin: str,
        users: Sequence[PrivateUser],
        verify: ssl.SSLContext,
        maximum_requests: int,
        maximum_response_bytes: int = 1024 * 1024,
        maximum_total_response_bytes: int = 64 * 1024 * 1024,
        request_timeout_seconds: float = 5.0,
    ) -> None:
        self.origin = _loopback_origin(origin)
        if (
            not isinstance(verify, ssl.SSLContext)
            or verify.verify_mode != ssl.CERT_REQUIRED
            or not verify.check_hostname
        ):
            raise ValueError("verified services TLS context is required")
        if not self.origin.startswith("https://"):
            raise ValueError("services observations require loopback HTTPS")
        if (
            not 1 <= maximum_requests <= 20000
            or not 1 <= maximum_response_bytes <= 24 * 1024 * 1024
        ):
            raise ValueError("invalid observation request or response limit")
        if not maximum_response_bytes <= maximum_total_response_bytes <= 256 * 1024 * 1024:
            raise ValueError("invalid total observation response limit")
        if (
            not 0 < request_timeout_seconds <= 30
            or not users
            or len({u.alias for u in users}) != len(users)
        ):
            raise ValueError("invalid observation users or timeout")
        self.users = {u.alias: u for u in users}
        self.verify = verify
        self.maximum_requests = maximum_requests
        self.maximum_response_bytes = maximum_response_bytes
        self.maximum_total_response_bytes = maximum_total_response_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self.total_response_bytes = 0
        self.samples: list[ObservationSample] = []
        self.cleanup_passed = True

    @property
    def requests_used(self) -> int:
        return len(self.samples)

    async def get(
        self,
        *,
        route_id: str,
        path: str,
        user_id: str,
        validator: Callable[[object], bool],
        query: Mapping[str, str] | None = None,
        expected_status: int = 200,
    ) -> Any:
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", route_id) is None or user_id not in self.users:
            raise ValueError("invalid observation route or user alias")
        if path != "/api/v1/readyz" and _PATH.fullmatch(path) is None:
            raise ValueError("observation path is outside the fixed GET allowlist")
        if expected_status not in {200, 401, 403, 404} or not callable(validator):
            raise ValueError("a registered control predicate is required")
        if len(self.samples) >= self.maximum_requests:
            raise ObservationError("OBSERVATION_REQUEST_BUDGET")
        sample = ObservationSample(route_id, user_id, len(self.samples))
        self.samples.append(sample)
        started = time.perf_counter()
        user = self.users[user_id]
        target = httpx.URL(path, params=dict(_private_map(query or {}))).raw_path.decode("ascii")
        with _quiet_transport() as unsettled:
            client: httpx.AsyncClient | None = None
            try:
                async with asyncio.timeout(self.request_timeout_seconds):
                    headers = {
                        **user.request_headers("GET", target, b""),
                        "Accept-Encoding": "identity",
                    }
                    client = httpx.AsyncClient(
                        base_url=self.origin,
                        headers=headers,
                        verify=self.verify,
                        timeout=self.request_timeout_seconds,
                        follow_redirects=False,
                        trust_env=False,
                        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                    )
                    sample.started = True
                    async with client.stream("GET", target) as response:
                        sample.http_status = response.status_code
                        sample.response_bytes = 0
                        raw = bytearray()
                        async for chunk in response.aiter_raw():
                            sample.response_bytes += len(chunk)
                            self.total_response_bytes += len(chunk)
                            if (
                                len(raw) + len(chunk) > self.maximum_response_bytes
                                or self.total_response_bytes > self.maximum_total_response_bytes
                            ):
                                raise ObservationError("OBSERVATION_RESPONSE_BUDGET")
                            raw.extend(chunk)
                        value = _strict_json(bytes(raw))
                    if sample.http_status != expected_status or validator(value) is not True:
                        raise ObservationError("OBSERVATION_SEMANTIC_FAILURE")
                    if time.perf_counter() - started >= self.request_timeout_seconds:
                        raise TimeoutError
                    sample.outcome = "success"
                    return value
            except asyncio.CancelledError:
                sample.outcome, sample.reason_code = "cancelled", "OBSERVATION_CANCELLED"
                raise
            except (TimeoutError, httpx.TimeoutException):
                sample.outcome, sample.reason_code = "timeout", "OBSERVATION_TIMEOUT"
                raise ObservationError("OBSERVATION_TIMEOUT") from None
            except Exception as exc:
                sample.outcome = "failure"
                sample.reason_code = (
                    str(exc) if isinstance(exc, ObservationError) else "OBSERVATION_FAILED"
                )
                raise ObservationError(sample.reason_code) from None
            finally:
                sample.elapsed_ms = (time.perf_counter() - started) * 1000
                if client is not None:
                    task = asyncio.create_task(client.aclose())
                    cleanup = asyncio.create_task(
                        _settle(
                            [task],
                            deadline=time.perf_counter() + 1,
                            cancel=False,
                            unsettled=unsettled,
                        )
                    )
                    cancelled = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            cancelled = True
                    if not cleanup.result():
                        self.cleanup_passed = False
                        sample.outcome, sample.reason_code = "failure", "OBSERVATION_CLEANUP_FAILED"
                        raise ObservationError("OBSERVATION_CLEANUP_FAILED")
                    if cancelled:
                        sample.outcome, sample.reason_code = "cancelled", "OBSERVATION_CANCELLED"
                        raise asyncio.CancelledError

    def report(self) -> dict[str, Any]:
        return {
            "kind": "control_or_observation_not_business_throughput",
            "requests_used": self.requests_used,
            "total_response_bytes": self.total_response_bytes,
            "cleanup_passed": self.cleanup_passed,
            "samples": [asdict(s) for s in self.samples],
        }
