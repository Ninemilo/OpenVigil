"""Bounded, GET-only loopback load execution with a secret-free sample ledger.

Credentials and query values are trusted caller inputs, never report fields. Validators
are trusted, bounded, synchronous predicates: they must not log response data or perform
I/O. A subprocess owner must enforce a hard wall limit against blocking Python callbacks.
This module captures cancellation into the ledger; callers inspect ``stop_reason``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import ssl
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

import httpx

from windops_backend.operations.load_contracts import (
    ErrorCode,
    LoadProfile,
    Outcome,
    RequestSample,
    Workload,
)

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_PATH = re.compile(
    r"/api/v1/(?:diagnoses|maintenance-plans|turbines|health/assessments|"
    r"scada/(?:history|measurements)|knowledge/(?:documents|parser-capabilities)|"
    r"turbines/[A-Za-z0-9_-]{1,128}/(?:health|scada)|"
    r"knowledge/documents/[A-Za-z0-9_-]{1,128}(?:/passages)?)\Z"
)
_PRIVATE_TRANSPORT = ContextVar("load_runner_private_transport", default=False)
_CLEANUP_GRACE_SECONDS = 1.0


def _alias(value: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError("invalid public load identity")


def _private_map(values: Mapping[str, str], *, headers: bool = False) -> Mapping[str, str]:
    if not isinstance(values, Mapping) or len(values) > 32:
        raise ValueError("invalid private request parameters")
    copied: dict[str, str] = {}
    total = 0
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("invalid private request parameters")
        if (
            not key
            or len(key) > 128
            or len(value) > 4096
            or "\r" in key + value
            or "\n" in key + value
        ):
            raise ValueError("invalid private request parameters")
        total += len(key.encode("utf-8")) + len(value.encode("utf-8"))
        if headers and (
            _HEADER_NAME.fullmatch(key) is None
            or not value.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or key.lower() in {"host", "content-length", "transfer-encoding", "proxy-authorization"}
        ):
            raise ValueError("invalid private HTTP headers")
        copied[key] = value
    if total > 16 * 1024:
        raise ValueError("private request parameters exceed bound")
    return MappingProxyType(copied)


@dataclass(frozen=True)
class PrivateUser:
    alias: str
    headers: Mapping[str, str] = field(repr=False)
    headers_for_request: Callable[[str, str, bytes], Mapping[str, str]] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _alias(self.alias)
        object.__setattr__(self, "headers", _private_map(self.headers, headers=True))
        if self.headers_for_request is not None and not callable(self.headers_for_request):
            raise ValueError("invalid private request authenticator")

    def request_headers(self, method: str, target: str, body: bytes) -> Mapping[str, str]:
        """Bind fresh credentials to the exact already-encoded HTTP request.

        The trusted callback must be bounded and must not log its inputs. Dynamic
        names replace static names case-insensitively, so an old authorization
        header cannot accompany the fresh assertion.
        """
        if self.headers_for_request is None:
            return self.headers
        dynamic = _private_map(self.headers_for_request(method, target, body), headers=True)
        replaced = {key.lower() for key in dynamic}
        return _private_map(
            {**{k: v for k, v in self.headers.items() if k.lower() not in replaced}, **dynamic},
            headers=True,
        )


@dataclass(frozen=True)
class AllowedGet:
    workload: Workload
    route_id: str
    path: str = field(repr=False)
    validator: Callable[[object, str], bool] = field(repr=False)
    query: Mapping[str, str] = field(default_factory=dict, repr=False)
    query_by_user: Mapping[str, Mapping[str, str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _alias(self.route_id)
        if self.workload not in {"queries", "history", "document_batch", "backlog"}:
            raise ValueError("invalid workload")
        if not isinstance(self.path, str) or _PATH.fullmatch(self.path) is None:
            raise ValueError("GET path is outside the business allowlist")
        if not callable(self.validator):
            raise ValueError("a semantic validator is required")
        object.__setattr__(self, "query", _private_map(self.query))
        if not isinstance(self.query_by_user, Mapping) or len(self.query_by_user) > 32:
            raise ValueError("invalid per-user query mapping")
        overrides: dict[str, Mapping[str, str]] = {}
        for alias, query in self.query_by_user.items():
            _alias(alias)
            # Overrides replace individual common keys, never the entire common query.
            overrides[alias] = _private_map({**self.query, **_private_map(query)})
        object.__setattr__(self, "query_by_user", MappingProxyType(overrides))


@dataclass(frozen=True)
class LoadRunResult:
    samples: tuple[RequestSample, ...]
    planned_requests: int  # Includes warmup; summaries use measured requests separately.
    elapsed_seconds: float  # Work interval, excludes the separate bounded cleanup grace.
    stop_reason: ErrorCode | None
    cleanup_passed: bool
    max_observed_inflight: int


def _loopback_origin(origin: str) -> str:
    try:
        parts = urlsplit(origin)
        address = ipaddress.ip_address(parts.hostname or "")
        valid = (
            parts.scheme in {"http", "https"}
            and address.is_loopback
            and parts.port is not None
            and 0 < parts.port <= 65535
            and parts.username is None
            and parts.password is None
            and parts.path in {"", "/"}
            and not parts.query
            and not parts.fragment
            and not any(char.isspace() for char in origin)
            and "%" not in origin
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("an explicit literal loopback HTTP origin and port are required") from None
    return origin.rstrip("/")


class _HidePrivateTransport(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_TRANSPORT.get()


@contextmanager
def _quiet_transport() -> Iterator[set[asyncio.Task[None] | asyncio.Task[bool]]]:
    """Suppress SDK URL/header debug logs only in this execution's async context."""
    names = {
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.connection_pool",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.http_proxy",
        "httpcore.socks_proxy",
    }
    names.update(name for name in logging.Logger.manager.loggerDict if name.startswith("httpcore."))
    loggers = [logging.getLogger(name) for name in names]
    guard = _HidePrivateTransport()
    for logger in loggers:
        logger.addFilter(guard)
    token = _PRIVATE_TRANSPORT.set(True)
    unsettled: set[asyncio.Task[None] | asyncio.Task[bool]] = set()

    def remove_when_settled(task: asyncio.Task[None] | asyncio.Task[bool] | None = None) -> None:
        if task is not None and not task.cancelled():
            task.exception()
        if all(task.done() for task in unsettled):
            for logger in loggers:
                logger.removeFilter(guard)

    try:
        yield unsettled
    finally:
        _PRIVATE_TRANSPORT.reset(token)
        for task in unsettled:
            task.add_done_callback(remove_when_settled)
        remove_when_settled()


async def _settle(
    tasks: Sequence[asyncio.Task[None] | asyncio.Task[bool]],
    *,
    deadline: float,
    cancel: bool,
    unsettled: set[asyncio.Task[None] | asyncio.Task[bool]],
) -> bool:
    if not tasks:
        return True
    if cancel:
        for task in tasks:
            if not task.done():
                task.cancel()
    done, pending = await asyncio.wait(tasks, timeout=max(0, deadline - time.perf_counter()))
    passed = not pending
    for task in done:
        if task.cancelled():
            passed = passed and cancel
        elif task.exception() is not None:
            passed = False
    for task in pending:
        task.cancel()
    # No unbounded gather after cancellation. The process owner must terminate a
    # misbehaving task that ignores cancellation; the public result fails cleanup.
    unsettled.update(pending)
    return passed


@dataclass
class _Work:
    route: AllowedGet
    user: PrivateUser
    sequence: int
    phase: Literal["warmup", "measured"]
    scheduled: float | None = None
    sample: RequestSample | None = None

    def unsent(self, outcome: Outcome, code: ErrorCode) -> RequestSample:
        return RequestSample(
            workload=self.route.workload,
            route_id=self.route.route_id,
            user_id=self.user.alias,
            sequence=self.sequence,
            phase=self.phase,
            outcome=outcome,
            started=False,
            scheduled_monotonic=self.scheduled,
            error_code=code,
        )


def _strict_json(raw: bytes) -> object:
    def no_constant(value: str) -> object:
        raise ValueError("nonfinite JSON value")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8"), parse_constant=no_constant, object_pairs_hook=unique)


class _Runner:
    def __init__(
        self,
        origin: str,
        users: tuple[PrivateUser, ...],
        routes: tuple[AllowedGet, ...],
        profile: LoadProfile,
        verify: ssl.SSLContext | Literal[True],
    ) -> None:
        self.origin = origin
        self.users = users
        self.profile = profile
        self.verify = verify
        # perf_counter is monotonic and retains sub-millisecond precision on Windows
        # Python 3.12, where time.monotonic can have a roughly 15 ms resolution.
        self.started_at = time.perf_counter()
        self.stop_reason: ErrorCode | None = None
        self.inflight = 0
        self.max_inflight = 0
        self.clients: dict[str, httpx.AsyncClient] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.byte_limits = {item.workload: item.maximum_response_bytes for item in profile.budgets}
        self.work: list[_Work] = []
        for phase, count in (
            ("warmup", profile.warmup_requests),
            ("measured", profile.requests_per_route * len(routes)),
        ):
            for index in range(count):
                sequence = len(self.work)
                self.work.append(
                    _Work(
                        routes[index % len(routes)],
                        users[(index // len(routes)) % len(users)],
                        sequence,
                        "warmup" if phase == "warmup" else "measured",
                    )
                )

    def now(self) -> float:
        return time.perf_counter() - self.started_at

    def stopped(self) -> bool:
        if self.stop_reason is None and self.now() >= self.profile.maximum_duration_seconds:
            self.stop_reason = "run_deadline_exceeded"
        return self.stop_reason is not None

    async def execute(self) -> None:
        try:
            for user in self.users:
                if self.stopped():
                    return
                self.clients[user.alias] = httpx.AsyncClient(
                    base_url=self.origin,
                    headers={"Accept-Encoding": "identity"},
                    verify=self.verify,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self.profile.request_timeout_seconds,
                    limits=httpx.Limits(
                        max_connections=self.profile.concurrency,
                        max_keepalive_connections=self.profile.concurrency,
                    ),
                )
        except Exception:
            self.stop_reason = "client_setup_failed"
            return
        for phase in ("warmup", "measured"):
            if self.stopped():
                return
            work = [item for item in self.work if item.phase == phase]
            if self.profile.schedule == "fixed_interval":
                await self.fixed_interval(work)
            else:
                await self.closed_loop(work)

    async def closed_loop(self, work: list[_Work]) -> None:
        cursor = 0

        async def consume() -> None:
            nonlocal cursor
            while cursor < len(work) and not self.stopped():
                item = work[cursor]
                cursor += 1
                item.scheduled = self.now()
                await self.request(item)

        workers = [
            asyncio.create_task(consume()) for _ in range(min(len(work), self.profile.concurrency))
        ]
        self.tasks.update(workers)
        try:
            await asyncio.gather(*workers)
        finally:
            self.tasks.difference_update(workers)

    async def fixed_interval(self, work: list[_Work]) -> None:
        interval = self.profile.arrival_interval_ms
        if interval is None:
            raise ValueError("fixed interval is missing")
        phase_start = self.now()
        for index, item in enumerate(work):
            item.scheduled = phase_start + index * interval / 1000
        active: set[asyncio.Task[None]] = set()
        try:
            for item in work:
                if self.stopped():
                    break
                if item.scheduled is None:
                    raise ValueError("arrival time is missing")
                # asyncio timers may wake early by their clock resolution. Recheck
                # the actual arrival boundary rather than inventing a later start.
                # This is a clock deadline, not polling for an event producer.
                while (delay := item.scheduled - self.now()) > 0 and not self.stopped():  # noqa: ASYNC110
                    await asyncio.sleep(delay)
                if self.stopped():
                    break
                done = {task for task in active if task.done()}
                for task in done:
                    task.result()
                active.difference_update(done)
                self.tasks.difference_update(done)
                if len(active) >= self.profile.concurrency:
                    item.sample = item.unsent("dropped", "arrival_overload")
                else:
                    task = asyncio.create_task(self.request(item))
                    active.add(task)
                    self.tasks.add(task)
            if active:
                await asyncio.gather(*active)
        finally:
            # Cancellation of a sleeping arrival scheduler must also cancel active HTTP.
            for task in active:
                if not task.done():
                    task.cancel()
            # The run owner bounds settlement and closes clients. Keep unfinished
            # tasks registered instead of blocking cancellation in this scheduler.
            self.tasks.difference_update(task for task in active if task.done())

    async def request(self, item: _Work) -> None:
        if self.stopped():
            return
        if item.scheduled is None:
            raise ValueError("request schedule is missing")
        started = self.now()
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        outcome: Outcome = "failure"
        code: ErrorCode | None = "transport_error"
        status: int | None = None
        observed_bytes: int | None = None
        semantic_valid = False
        try:
            async with asyncio.timeout(self.profile.request_timeout_seconds):
                target = httpx.URL(
                    item.route.path,
                    params=dict(item.route.query_by_user.get(item.user.alias, item.route.query)),
                ).raw_path.decode("ascii")
                async with self.clients[item.user.alias].stream(
                    "GET",
                    target,
                    headers=item.user.request_headers("GET", target, b""),
                ) as response:
                    status = response.status_code
                    observed_bytes = 0
                    raw = bytearray()
                    maximum = self.byte_limits[item.route.workload]
                    length = response.headers.get("content-length")
                    too_large = bool(length and length.isdigit() and int(length) > maximum)
                    if not too_large:
                        # Raw streaming never decompresses a potentially unbounded body.
                        async for chunk in response.aiter_raw():
                            observed_bytes += len(chunk)
                            if observed_bytes > maximum:
                                too_large = True
                                break
                            raw.extend(chunk)
                    if too_large:
                        code = "response_too_large"
                    elif not 200 <= status < 300:
                        code = "http_status"
                    else:
                        try:
                            value = _strict_json(bytes(raw))
                        except (ValueError, RecursionError):
                            code = "json_invalid"
                        else:
                            try:
                                semantic_valid = (
                                    item.route.validator(value, item.user.alias) is True
                                )
                            except BaseException:
                                # A callback's own cancellation is not an HTTP cancel.
                                code = "semantic_validator_error"
                            else:
                                code = None if semantic_valid else "semantic_invalid"
                                outcome = "success" if semantic_valid else "failure"
            # Sync validators cannot be preempted by an asyncio timeout. Check on return.
            if self.now() - started >= self.profile.request_timeout_seconds:
                outcome, code, status, semantic_valid = "timeout", "request_timeout", None, False
        except (TimeoutError, httpx.TimeoutException):
            outcome, code, status, semantic_valid = "timeout", "request_timeout", None, False
        except asyncio.CancelledError:
            outcome = "cancelled"
            code = self.stop_reason or "request_cancelled"
            semantic_valid = False
        except Exception:
            outcome = "failure"
            code = "transport_error"
            semantic_valid = False
        finally:
            finished = self.now()
            self.inflight -= 1
            item.sample = RequestSample(
                workload=item.route.workload,
                route_id=item.route.route_id,
                user_id=item.user.alias,
                sequence=item.sequence,
                phase=item.phase,
                outcome=outcome,
                started=True,
                scheduled_monotonic=item.scheduled,
                started_monotonic=started,
                finished_monotonic=finished,
                scheduler_delay_ms=(started - item.scheduled) * 1000,
                elapsed_ms=(finished - started) * 1000,
                response_bytes=observed_bytes,
                http_status=status,
                semantic_valid=semantic_valid,
                error_code=code,
            )

    async def cleanup(
        self,
        deadline: float,
        unsettled: set[asyncio.Task[None] | asyncio.Task[bool]],
    ) -> bool:
        tasks = [asyncio.create_task(client.aclose()) for client in self.clients.values()]
        return await _settle(tasks, deadline=deadline, cancel=False, unsettled=unsettled)


async def run_load(
    *,
    origin: str,
    users: Sequence[PrivateUser],
    routes: Sequence[AllowedGet],
    profile: LoadProfile,
    cancel_event: asyncio.Event | None = None,
    verify: ssl.SSLContext | bool = True,
) -> LoadRunResult:
    """Execute a fully accounted plan; never retry, redirect, or use environment proxies.

    ``query_by_user`` merges common query keys with per-alias overrides. User aliases
    rotate independently for each route, so even four routes exercise every identity.
    ``planned_requests`` includes warmup; measured count is routes * requests_per_route.
    TLS verification cannot be disabled. A custom trusted SSLContext is accepted.
    """
    origin = _loopback_origin(origin)
    if verify is not True and not isinstance(verify, ssl.SSLContext):
        raise ValueError("TLS verification must remain enabled")
    if isinstance(verify, ssl.SSLContext) and (
        verify.verify_mode != ssl.CERT_REQUIRED or not verify.check_hostname
    ):
        raise ValueError("TLS context must verify certificates and hostnames")
    if not 1 <= len(users) <= 32 or not 1 <= len(routes) <= 64:
        raise ValueError("load identities or routes exceed bounds")
    if not all(isinstance(user, PrivateUser) for user in users) or not all(
        isinstance(route, AllowedGet) for route in routes
    ):
        raise ValueError("explicit validated request identities and routes are required")
    user_ids = {user.alias for user in users}
    if len(user_ids) != len(users) or len({route.route_id for route in routes}) != len(routes):
        raise ValueError("load identities and route aliases must be unique")
    if {route.workload for route in routes} != set(profile.workloads):
        raise ValueError("route workloads do not match the profile")
    if any(set(route.query_by_user) - user_ids for route in routes):
        raise ValueError("per-user query overrides reference an undeclared identity")
    if (
        profile.warmup_requests + len(routes) * profile.requests_per_route
        > profile.maximum_requests
    ):
        raise ValueError("request cap cannot cover the explicit route plan")
    runner = _Runner(origin, tuple(users), tuple(routes), profile, verify)
    cleanup_passed = False
    with _quiet_transport() as unsettled:
        execution: asyncio.Task[None] | None = None
        cancellation: asyncio.Task[bool] | None = None
        try:
            if cancel_event is not None and cancel_event.is_set():
                runner.stop_reason = "request_cancelled"
            else:
                execution = asyncio.create_task(runner.execute())
                watched: set[asyncio.Task[None] | asyncio.Task[bool]] = {execution}
                if cancel_event is not None:
                    cancellation = asyncio.create_task(cancel_event.wait())
                    watched.add(cancellation)
                done, _ = await asyncio.wait(
                    watched,
                    timeout=max(0, profile.maximum_duration_seconds - runner.now()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if execution in done:
                    execution.result()
                    runner.stopped()
                else:
                    runner.stop_reason = (
                        "request_cancelled" if cancellation in done else "run_deadline_exceeded"
                    )
        except asyncio.CancelledError:
            runner.stop_reason = "request_cancelled"
        except Exception:
            runner.stop_reason = "runner_error"
        finally:
            owned: list[asyncio.Task[None] | asyncio.Task[bool]] = list(runner.tasks)
            owned.extend(task for task in (execution, cancellation) if task is not None)
            elapsed = runner.now()
            cleanup_deadline = time.perf_counter() + _CLEANUP_GRACE_SECONDS

            async def finish() -> bool:
                settled = await _settle(
                    owned,
                    deadline=cleanup_deadline - _CLEANUP_GRACE_SECONDS / 2,
                    cancel=True,
                    unsettled=unsettled,
                )
                return await runner.cleanup(cleanup_deadline, unsettled) and settled

            finishing = asyncio.create_task(finish())
            while not finishing.done():
                try:
                    await asyncio.shield(finishing)
                except asyncio.CancelledError:
                    runner.stop_reason = "request_cancelled"
                except Exception:
                    break
            cleanup_passed = (
                not finishing.cancelled() and finishing.exception() is None and finishing.result()
            )
            if not cleanup_passed and runner.stop_reason is None:
                runner.stop_reason = "client_cleanup_failed"
    samples = tuple(
        item.sample or item.unsent("not_run", runner.stop_reason or "not_started")
        for item in runner.work
    )
    return LoadRunResult(
        samples=samples,
        planned_requests=len(samples),
        elapsed_seconds=elapsed,
        stop_reason=runner.stop_reason,
        cleanup_passed=cleanup_passed,
        max_observed_inflight=runner.max_inflight,
    )
