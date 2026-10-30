"""Real loopback socket checks; synthetic responses prove scheduler behavior only."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from windops_backend.operations.load_contracts import LoadProfile, WorkloadBudget, summarize_samples
from windops_backend.operations.load_runner import AllowedGet, PrivateUser, run_load


async def test_fresh_headers_bind_the_exact_encoded_get_target() -> None:
    bound: list[tuple[str, str, bytes]] = []

    def signer(method: str, target: str, body: bytes) -> dict[str, str]:
        bound.append((method, target, body))
        return {"authorization": "Bearer private-0", "X-Bound-Target": target}

    user = PrivateUser("u0", {"Authorization": "stale-private-99"}, signer)
    item = AllowedGet(
        "queries",
        "bound",
        "/api/v1/diagnoses",
        valid,
        query={"case": "bound", "name": "中文 and + /"},
    )
    async with server() as (origin, state):
        result = await run_load(origin=origin, users=[user], routes=[item], profile=profile())
    assert len(bound) == len(state.requests) == 100
    assert all(sample.outcome == "success" for sample in result.samples)
    assert all(method == "GET" and body == b"" for method, _, body in bound)
    assert all("%E4%B8%AD%E6%96%87+and+%2B+%2F" in target for _, target, _ in bound)
    assert "stale-private" not in repr(user) and "signer" not in repr(user)


async def test_invalid_dynamic_headers_fail_before_transmission() -> None:
    user = PrivateUser("u0", {}, lambda *_: {"Host": "outside.invalid"})
    async with server() as (origin, state):
        result = await run_load(origin=origin, users=[user], routes=[route()], profile=profile())
    assert not state.requests
    assert len(result.samples) == 100 and result.cleanup_passed
    assert all(sample.outcome == "failure" for sample in result.samples)


def profile(
    *,
    concurrency: int = 4,
    duration: float = 5.0,
    timeout: float = 1.0,
    warmup: int = 0,
    cap: int = 300,
    maximum_bytes: int = 4096,
    schedule: Literal["closed_loop", "fixed_interval"] = "closed_loop",
) -> LoadProfile:
    return LoadProfile(
        schema_version="openvigil.load-profile.v1",
        profile_id="synthetic-socket-contract",
        mode="software",
        workloads=("queries",),
        concurrency=concurrency,
        requests_per_route=100,
        maximum_requests=cap,
        maximum_duration_seconds=duration,
        request_timeout_seconds=timeout,
        warmup_requests=warmup,
        schedule=schedule,
        arrival_interval_ms=1.0 if schedule == "fixed_interval" else None,
        history_rows=0,
        documents=0,
        maximum_upload_bytes=0,
        budgets=(
            WorkloadBudget(
                workload="queries",
                p95_ms=500.0,
                p99_ms=1000.0,
                maximum_error_rate=0.0,
                maximum_response_bytes=maximum_bytes,
                maximum_process_rss_bytes=100000000,
                maximum_total_rss_bytes=200000000,
            ),
        ),
    )


def valid(value: object, alias: str) -> bool:
    return isinstance(value, dict) and value.get("user") == alias and value.get("ok") is True


def route(case: str = "normal", *, route_id: str = "diagnoses") -> AllowedGet:
    return AllowedGet(
        workload="queries",
        route_id=route_id,
        path="/api/v1/diagnoses",
        query={"case": case, "private_query": "query-DO-NOT-REPORT"},
        validator=valid,
    )


def users(count: int = 4) -> list[PrivateUser]:
    return [
        PrivateUser(alias=f"u{index}", headers={"Authorization": f"Bearer private-{index}"})
        for index in range(count)
    ]


@dataclass
class ServerState:
    requests: list[dict[str, str]] = field(default_factory=list)
    active: int = 0
    maximum_active: int = 0
    first_request: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@asynccontextmanager
async def server() -> AsyncIterator[tuple[str, ServerState]]:
    state = ServerState()
    handlers: set[asyncio.Task[None]] = set()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        counted = False
        try:
            request = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            lines = request.split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers = {
                key.lower(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            query = parse_qs(urlsplit(target).query)
            case = query.get("case", ["normal"])[0]
            user = "u" + headers.get("authorization", "Bearer private-0").rsplit("-", 1)[-1]
            state.requests.append(
                {
                    "method": method,
                    "path": urlsplit(target).path,
                    "user": user,
                    "cookie": headers.get("cookie", ""),
                    "case": case,
                    "scope": query.get("scope", [""])[0],
                    "common": query.get("common", [""])[0],
                }
            )
            state.active += 1
            counted = True
            state.maximum_active = max(state.maximum_active, state.active)
            state.first_request.set()
            if case in {"delay", "fixed"}:
                await asyncio.sleep(0.025)
            if case == "wait":
                await state.release.wait()
            if case == "disconnect":
                return
            status = 503 if case == "503" else 302 if case == "redirect" else 200
            body = json.dumps(
                {
                    "user": "wrong" if case == "scope" else user,
                    "ok": case != "bound" or headers.get("x-bound-target") == target,
                    "private_body": "body-DO-NOT-REPORT",
                }
            ).encode()
            if case == "badjson":
                body = b"private-bad-body"
            if case == "duplicate":
                body = b'{"ok":false,"ok":true}'
            if case == "nan":
                body = b'{"ok":NaN}'
            if case in {"oversize", "stream-large"}:
                body = b"x" * 8192
            length = "" if case == "stream-large" else f"Content-Length: {len(body)}\r\n"
            head = (
                f"HTTP/1.1 {status} Test\r\nConnection: close\r\n"
                f"Content-Type: application/json\r\n{length}"
                f"Set-Cookie: session={user}; Path=/\r\n"
                "Location: https://outside.invalid/private\r\n\r\n"
            ).encode()
            writer.write(head)
            await writer.drain()
            if case == "slow-body":
                await state.release.wait()
            writer.write(body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # The client intentionally closes timed-out/oversized fixture sockets.
        finally:
            if counted:
                state.active -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handlers.discard(task)

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        listener.close()
        state.release.set()
        await asyncio.sleep(0)
        pending = list(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.wait_for(listener.wait_closed(), 2)


async def test_real_concurrency_warmup_route_local_users_and_cookie_isolation() -> None:
    async with server() as (origin, state):
        result = await run_load(
            origin=origin,
            users=users(),
            routes=[route("delay"), route("delay", route_id="second")],
            profile=profile(warmup=4, cap=204),
        )
    assert result.stop_reason is None and result.cleanup_passed
    assert result.planned_requests == len(result.samples) == len(state.requests) == 204
    assert 2 <= result.max_observed_inflight <= 4
    assert 2 <= state.maximum_active <= 4
    assert all(item.outcome == "success" for item in result.samples)
    warmup = [item for item in result.samples if item.phase == "warmup"]
    measured = [item for item in result.samples if item.phase == "measured"]
    assert len(warmup) == 4 and len(measured) == 200
    assert max(item.finished_monotonic or 0 for item in warmup) <= min(
        item.started_monotonic or 0 for item in measured
    )
    assert Counter((item.route_id, item.user_id) for item in measured) == {
        (name, f"u{index}"): 25 for name in ("diagnoses", "second") for index in range(4)
    }
    assert all(
        not request["cookie"] or request["cookie"] == f"session={request['user']}"
        for request in state.requests
    )
    assert any(request["cookie"] for request in state.requests)
    assert {request["method"] for request in state.requests} == {"GET"}
    assert all(item.elapsed_ms is not None and item.elapsed_ms > 0 for item in result.samples)
    summary = summarize_samples(result.samples, planned=200)
    assert summary.success == 200 and summary.warmup == 4 and summary.error_rate == 0


async def test_per_user_query_overrides_merge_and_are_frozen() -> None:
    original = {"u0": {"scope": "A"}, "u1": {"scope": "B"}}
    selected = AllowedGet(
        workload="queries",
        route_id="scope",
        path="/api/v1/turbines",
        query={"scope": "default", "common": "kept"},
        query_by_user=original,
        validator=valid,
    )
    original["u0"]["scope"] = "tampered"
    async with server() as (origin, state):
        result = await run_load(origin=origin, users=users(2), routes=[selected], profile=profile())
    assert all(sample.outcome == "success" for sample in result.samples)
    assert {
        (request["user"], request["scope"], request["common"]) for request in state.requests
    } == {("u0", "A", "kept"), ("u1", "B", "kept")}


@pytest.mark.parametrize(
    ("case", "code", "status"),
    [
        ("503", "http_status", 503),
        ("redirect", "http_status", 302),
        ("badjson", "json_invalid", 200),
        ("duplicate", "json_invalid", 200),
        ("nan", "json_invalid", 200),
        ("scope", "semantic_invalid", 200),
        ("oversize", "response_too_large", 200),
        ("stream-large", "response_too_large", 200),
        ("disconnect", "transport_error", None),
    ],
)
async def test_http_json_semantic_size_and_transport_failures_are_retained_without_retry(
    case: str,
    code: str,
    status: int | None,
) -> None:
    async with server() as (origin, state):
        result = await run_load(
            origin=origin, users=users(1), routes=[route(case)], profile=profile()
        )
    assert len(state.requests) == len(result.samples) == 100
    assert result.cleanup_passed and result.stop_reason is None
    assert all(
        sample.outcome == "failure" and sample.error_code == code for sample in result.samples
    )
    assert all(sample.http_status == status and sample.started for sample in result.samples)
    if case == "stream-large":
        assert all((sample.response_bytes or 0) > 4096 for sample in result.samples)
    if case == "oversize":
        assert all(sample.response_bytes == 0 for sample in result.samples)
    if case == "disconnect":
        assert all(sample.response_bytes is None for sample in result.samples)
    summary = summarize_samples(result.samples, planned=100)
    assert summary.success == 0 and summary.measured_latencies == 100 and summary.error_rate == 1


async def test_callback_failure_and_http_debug_logs_never_expose_private_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def explode(value: object, alias: str) -> bool:
        raise RuntimeError(f"private-validator-{value}-{alias}")

    selected = AllowedGet(
        workload="queries",
        route_id="safe",
        path="/api/v1/diagnoses",
        query={"private_query": "query-DO-NOT-REPORT"},
        validator=explode,
    )
    caplog.set_level(logging.DEBUG)
    original_filters = tuple(logging.getLogger("httpx").filters)
    async with server() as (origin, _):
        result = await run_load(origin=origin, users=users(1), routes=[selected], profile=profile())
    assert all(sample.error_code == "semantic_validator_error" for sample in result.samples)
    public = json.dumps(
        {**asdict(result), "samples": [sample.model_dump() for sample in result.samples]}
    )
    combined = public + repr(users()) + repr(selected) + caplog.text
    for secret in (
        "private-0",
        "private_query",
        "query-DO-NOT-REPORT",
        "body-DO-NOT-REPORT",
        "private-validator",
        origin,
    ):
        assert secret not in combined
    assert tuple(logging.getLogger("httpx").filters) == original_filters


@pytest.mark.parametrize("case", ["wait", "slow-body"])
async def test_real_request_timeout_records_attempts_and_partial_response(case: str) -> None:
    async with server() as (origin, state):
        result = await run_load(
            origin=origin,
            users=users(1),
            routes=[route(case)],
            profile=profile(timeout=0.05, duration=5.0, concurrency=4),
        )
    assert 0 < len(state.requests) <= len(result.samples) == 100
    assert result.stop_reason is None
    assert all(
        sample.outcome == "timeout" and sample.error_code == "request_timeout"
        for sample in result.samples
    )
    assert all(
        sample.http_status is None and (sample.elapsed_ms or 0) >= 15 for sample in result.samples
    )
    if case == "slow-body":
        assert any(sample.response_bytes == 0 for sample in result.samples)
        assert all(sample.response_bytes in {None, 0} for sample in result.samples)


@pytest.mark.parametrize("task_cancel", [False, True])
async def test_cancel_keeps_every_started_and_unstarted_request(task_cancel: bool) -> None:
    cancellation = asyncio.Event()
    async with server() as (origin, state):
        task = asyncio.create_task(
            run_load(
                origin=origin,
                users=users(1),
                routes=[route("wait")],
                profile=profile(),
                cancel_event=cancellation,
            )
        )
        await asyncio.wait_for(state.first_request.wait(), 2)
        if task_cancel:
            task.cancel()
        else:
            cancellation.set()
        result = await asyncio.wait_for(task, 2)
    assert result.stop_reason == "request_cancelled" and result.cleanup_passed
    assert len(result.samples) == 100
    assert 1 <= len(state.requests) <= 4
    started = [sample for sample in result.samples if sample.started]
    unstarted = [sample for sample in result.samples if not sample.started]
    assert started and unstarted
    assert all(
        sample.outcome == "cancelled" and sample.elapsed_ms is not None for sample in started
    )
    assert all(
        sample.outcome == "not_run" and sample.elapsed_ms is None and sample.response_bytes is None
        for sample in unstarted
    )
    assert all(sample.error_code == "request_cancelled" for sample in result.samples)


async def test_precancelled_run_sends_nothing() -> None:
    cancellation = asyncio.Event()
    cancellation.set()
    async with server() as (origin, state):
        result = await run_load(
            origin=origin,
            users=users(),
            routes=[route()],
            profile=profile(),
            cancel_event=cancellation,
        )
    assert not state.requests and result.max_observed_inflight == 0
    assert len(result.samples) == 100 and all(
        sample.outcome == "not_run" for sample in result.samples
    )


async def test_run_deadline_retains_timeouts_cancelled_and_never_started_work() -> None:
    async with server() as (origin, state):
        result = await run_load(
            origin=origin,
            users=users(1),
            routes=[route("wait")],
            profile=profile(duration=0.25, timeout=0.05),
        )
    assert result.stop_reason == "run_deadline_exceeded" and result.cleanup_passed
    assert result.elapsed_seconds < 0.6
    assert len(result.samples) == 100 and 1 <= len(state.requests) < 100
    assert {sample.outcome for sample in result.samples} == {"timeout", "cancelled", "not_run"}
    assert all(
        sample.error_code == "run_deadline_exceeded"
        for sample in result.samples
        if sample.outcome in {"cancelled", "not_run"}
    )


async def test_fixed_arrival_overload_drops_work_without_unbounded_tasks() -> None:
    async with server() as (origin, state):
        result = await run_load(
            origin=origin,
            users=users(1),
            routes=[route("fixed")],
            profile=profile(concurrency=2, schedule="fixed_interval"),
        )
    assert result.stop_reason is None and result.cleanup_passed
    assert len(result.samples) == 100 and 1 < len(state.requests) < 25
    assert result.max_observed_inflight == state.maximum_active == 2
    assert {sample.outcome for sample in result.samples} == {"success", "dropped"}
    for previous, current in zip(result.samples, result.samples[1:], strict=False):
        assert current.scheduled_monotonic is not None and previous.scheduled_monotonic is not None
        assert current.scheduled_monotonic - previous.scheduled_monotonic == pytest.approx(0.001)
    assert all(
        sample.error_code == "arrival_overload" and sample.elapsed_ms is None
        for sample in result.samples
        if sample.outcome == "dropped"
    )


async def test_fixed_deadline_keeps_future_arrivals_without_fabricated_measurements() -> None:
    selected_profile = profile(duration=0.15, timeout=0.1, schedule="fixed_interval").model_copy(
        update={"arrival_interval_ms": 50.0}
    )
    async with server() as (origin, _):
        result = await run_load(
            origin=origin, users=users(1), routes=[route()], profile=selected_profile
        )
    assert result.stop_reason == "run_deadline_exceeded"
    assert len(result.samples) == 100
    assert result.samples[-1].outcome == "not_run"
    assert (result.samples[-1].scheduled_monotonic or 0) > 4
    assert result.samples[-1].elapsed_ms is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com:443",
        "http://localhost:1234",
        "http://127.0.0.1",
        "http://127.0.0.1:1234/path",
        "http://secret@127.0.0.1:1234",
        "http://127.0.0.1:1234?secret=x",
        "file:///tmp/private",
        "http://[::1%25scope]:1234",
        "http://127.0.0.1:1234\n",
    ],
)
async def test_origin_rejected_before_transport(
    origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("transport must not be created")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    with pytest.raises(ValueError, match="loopback") as error:
        await run_load(origin=origin, users=users(), routes=[route()], profile=profile())
    assert origin not in str(error.value)


@pytest.mark.parametrize(
    "path",
    [
        "https://outside.invalid",
        "//outside.invalid",
        "/api/v1/../secret",
        "/api/v1/diagnoses?secret=x",
        "/api/v1/knowledge/documents/%2e%2e",
        "/api/v1/admin",
    ],
)
def test_paths_cannot_expand_the_get_allowlist(path: str) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        AllowedGet(workload="queries", route_id="safe", path=path, validator=valid)


async def test_plan_overflow_unknown_user_and_tls_bypass_fail_before_requests() -> None:
    unknown_user = AllowedGet(
        workload="queries",
        route_id="safe",
        path="/api/v1/turbines",
        validator=valid,
        query_by_user={"not-configured": {"turbine_id": "private"}},
    )
    unverified = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    async with server() as (origin, state):
        with pytest.raises(ValueError, match="request cap"):
            await run_load(
                origin=origin,
                users=users(),
                routes=[route(), route(route_id="second")],
                profile=profile(cap=100),
            )
        with pytest.raises(ValueError, match="undeclared identity"):
            await run_load(origin=origin, users=users(), routes=[unknown_user], profile=profile())
        for verify in (False, unverified):
            with pytest.raises(ValueError, match="TLS"):
                await run_load(
                    origin=origin, users=users(), routes=[route()], profile=profile(), verify=verify
                )
    assert not state.requests


async def test_setup_failure_preserves_plan_without_exposing_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_client(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private-client-construction-secret")

    monkeypatch.setattr(httpx, "AsyncClient", broken_client)
    result = await run_load(
        origin="http://127.0.0.1:9", users=users(), routes=[route()], profile=profile()
    )
    assert result.stop_reason == "client_setup_failed" and result.cleanup_passed
    assert len(result.samples) == 100 and all(
        sample.outcome == "not_run" for sample in result.samples
    )
    assert "private-client" not in repr(result)


async def test_cleanup_failure_retains_real_requests_but_cannot_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient.aclose

    async def failed_close(client: httpx.AsyncClient) -> None:
        await original(client)
        raise RuntimeError("private-close-secret")

    monkeypatch.setattr(httpx.AsyncClient, "aclose", failed_close)
    async with server() as (origin, state):
        result = await run_load(origin=origin, users=users(1), routes=[route()], profile=profile())
    assert len(state.requests) == 100 and all(
        sample.outcome == "success" for sample in result.samples
    )
    assert not result.cleanup_passed and result.stop_reason == "client_cleanup_failed"
    assert "private-close" not in repr(result)


@pytest.mark.parametrize("ignore_cancellation", [False, True])
async def test_cleanup_timeout_is_bounded_even_when_close_ignores_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    ignore_cancellation: bool,
) -> None:
    original = httpx.AsyncClient.aclose
    release = asyncio.Event()
    closed = asyncio.Event()

    async def slow_close(client: httpx.AsyncClient) -> None:
        await original(client)
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    if not ignore_cancellation:
                        raise
        finally:
            closed.set()

    monkeypatch.setattr(httpx.AsyncClient, "aclose", slow_close)
    async with server() as (origin, state):
        started = time.perf_counter()
        try:
            result = await run_load(
                origin=origin, users=users(1), routes=[route()], profile=profile()
            )
            assert time.perf_counter() - started < 2.5
            assert result.stop_reason == "client_cleanup_failed" and not result.cleanup_passed
            assert len(state.requests) == 100
            assert all(sample.outcome == "success" for sample in result.samples)
        finally:
            release.set()
            await asyncio.wait_for(closed.wait(), 1)


async def test_repeated_cancellation_during_cleanup_preserves_completed_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient.aclose
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def close_after_release(client: httpx.AsyncClient) -> None:
        await original(client)
        cleaning.set()
        await release.wait()

    monkeypatch.setattr(httpx.AsyncClient, "aclose", close_after_release)
    async with server() as (origin, state):
        task = asyncio.create_task(
            run_load(origin=origin, users=users(1), routes=[route()], profile=profile())
        )
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        result = await asyncio.wait_for(task, 2)
    assert result.stop_reason == "request_cancelled" and result.cleanup_passed
    assert len(state.requests) == 100 and all(
        sample.outcome == "success" for sample in result.samples
    )
