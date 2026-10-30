"""Native owned-Python resources plus HTTP container-stat fixtures; no Docker/OCR claims."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from windops_backend.operations.load_resources import (
    ProcessIdentity,
    ResourceObservationError,
    read_process,
    register_process,
)
from windops_backend.operations.load_services_resources import (
    ContainerIdentity,
    ContainerRole,
    ContainerSample,
    ResourceSamplingError,
    ServicesResourceLimits,
    ServicesResourceSampler,
)

CONTAINER_ROLES: tuple[ContainerRole, ...] = ("postgres", "redis", "minio", "neo4j")
IDENTITIES = tuple(
    ContainerIdentity(role=role, container_id=str(i) * 64, identity_verified=True)
    for i, role in enumerate(CONTAINER_ROLES, 1)
)


@pytest_asyncio.fixture
async def owned_driver() -> AsyncIterator[ProcessIdentity]:
    """A fixed stdlib-only workload, independent of the pytest process's retained RSS."""
    # Windows venv python.exe can be a launcher; this fixture needs the actual
    # interpreter PID. The child still reports and verifies its own PID.
    executable = str(getattr(sys, "_base_executable", sys.executable))
    child = await asyncio.create_subprocess_exec(
        executable,
        "-u",
        "-c",
        "import os, sys; print(os.getpid(), flush=True); sys.stdin.readline()",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    identity: ProcessIdentity | None = None
    try:
        assert child.stdout is not None and child.stdin is not None
        actual_pid = int((await asyncio.wait_for(child.stdout.readline(), 3)).strip())
        assert actual_pid == child.pid
        identity = register_process(actual_pid, label="fixture-driver")
        yield identity
    finally:
        if child.returncode is None:
            if child.stdin is not None:
                child.stdin.close()
            try:
                await asyncio.wait_for(child.wait(), 3)
            except TimeoutError:
                child.kill()
                await asyncio.wait_for(child.wait(), 3)
        assert child.returncode == 0
        if identity is not None:
            with pytest.raises(ResourceObservationError):
                read_process(identity)


@dataclass
class StatsFixture:
    origin: str = ""
    mode: str = "normal"
    memory: int = 1024
    calls: int = 0
    release: asyncio.Event = field(default_factory=asyncio.Event)


@asynccontextmanager
async def stats_server() -> AsyncIterator[
    tuple[StatsFixture, Callable[[], Awaitable[tuple[ContainerSample, ...]]]]
]:
    state = StatsFixture()
    handlers: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            state.calls += 1
            if state.mode == "blocked":
                await state.release.wait()
            samples = [
                {
                    "role": item.role,
                    "container_id": item.container_id,
                    "observed_at": (
                        datetime.now(UTC) - timedelta(seconds=31 if state.mode == "stale" else 0)
                    ).isoformat(),
                    "running": state.mode != "stopped",
                    "memory_working_set_bytes": state.memory,
                    "cpu_percent": 0.0,
                }
                for item in IDENTITIES
            ]
            if state.mode == "identity":
                samples[0]["container_id"] = "a" * 64
            elif state.mode == "missing_metric":
                samples[0]["memory_working_set_bytes"] = None
            elif state.mode == "missing_container":
                samples.pop()
            raw = json.dumps(samples).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + raw
            )
            await writer.drain()
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state.origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:

            async def query() -> tuple[ContainerSample, ...]:
                response = await client.get(state.origin + "/fixture-owned-container-stats")
                response.raise_for_status()
                # This adapter is synthetic; JSON transport is real and observations are validated.
                return tuple(
                    ContainerSample.model_validate_json(json.dumps(row)) for row in response.json()
                )

            yield state, query
    finally:
        state.release.set()
        server.close()
        pending = list(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()


def limits(**overrides: Any) -> ServicesResourceLimits:
    options: dict[str, Any] = {
        "duration_seconds": 2.0,
        "interval_seconds": 0.1,
        "callback_timeout_seconds": 0.5,
        "maximum_process_rss_bytes": 512 * 1024**2,
        "maximum_total_process_rss_bytes": 1024**3,
        "maximum_container_working_set_bytes": 2048,
        "maximum_total_container_working_set_bytes": 4096,
    }
    return ServicesResourceLimits(**{**options, **overrides})


def create(
    query: Callable[[], Any],
    event: asyncio.Event,
    driver: ProcessIdentity,
    *,
    members: Callable[[], Sequence[ProcessIdentity]] | None = None,
    **overrides: Any,
) -> ServicesResourceSampler:
    return ServicesResourceSampler(
        members=members or (lambda: (driver,)),
        containers=IDENTITIES,
        container_stats=query,
        limits=limits(**overrides),
        cancel_event=event,
    )


async def test_actual_owned_child_allocation_and_exit_are_dynamic_observations(
    owned_driver: ProcessIdentity,
) -> None:
    event = asyncio.Event()
    driver = owned_driver
    active: list[ProcessIdentity] = [driver]
    child: asyncio.subprocess.Process | None = None
    code = """import os, sys, time
print('ready:' + str(os.getpid()), flush=True)
for line in sys.stdin:
    if line.strip() == 'allocate':
        allocation = bytearray(32 * 1024 * 1024)
        for offset in range(0, len(allocation), 4096): allocation[offset] = 1
        until = time.perf_counter() + .03
        while time.perf_counter() < until: sum(range(1000))
        print('allocated', flush=True)
    else: break
"""
    async with stats_server() as (state, query):
        sampler = create(query, event, owned_driver, members=lambda: tuple(active)).start()
        try:
            await sampler.wait_first_sample()
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-c",
                code,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            assert child.stdout is not None and child.stdin is not None
            ready = (await asyncio.wait_for(child.stdout.readline(), 3)).strip()
            assert ready.startswith(b"ready:")
            actual_pid = int(ready.split(b":")[1])
            if actual_pid != child.pid:
                active.append(register_process(child.pid, label="fixture-child-launcher"))
            actual_identity = register_process(actual_pid, label="fixture-dynamic-child")
            active.append(actual_identity)
            await asyncio.sleep(0.25)
            child.stdin.write(b"allocate\n")
            await child.stdin.drain()
            assert (await asyncio.wait_for(child.stdout.readline(), 3)).strip() == b"allocated"
            await asyncio.sleep(0.25)
            del active[1:]  # Containment no longer reports children during owned shutdown.
            child.stdin.write(b"exit\n")
            await child.stdin.drain()
            assert await asyncio.wait_for(child.wait(), 3) == 0
            with pytest.raises(ResourceObservationError):
                read_process(actual_identity)
            await asyncio.sleep(0.25)
            report = await sampler.stop()
        finally:
            await sampler.stop()
            if child is not None and child.returncode is None:
                if child.stdin is not None:
                    child.stdin.write(b"exit\n")
                    await child.stdin.drain()
                try:
                    await asyncio.wait_for(child.wait(), 3)
                except TimeoutError:
                    child.kill()
                    await asyncio.wait_for(child.wait(), 3)
    assert report["budget_passed"] and report["cleanup_passed"] and report["sampler_stopped"]
    assert not event.is_set() and state.calls >= 6
    observed = next(
        item for item in report["processes"] if item["identity"]["label"] == "fixture-dynamic-child"
    )
    rss = [sample["rss_bytes"] for sample in observed["samples"]]
    assert max(rss) - min(rss) >= 24 * 1024**2
    assert observed["cpu_observed_delta_seconds"] > 0
    assert observed["samples"][0]["slot"] > 0
    assert observed["samples"][-1]["slot"] < report["slots"][-1]["slot"]
    assert report["container_working_set"]["observed_peak_total_bytes"] == 4096
    assert report["process_rss"]["observed_peak_total_bytes"] > 4096
    assert "full lifetime coverage unverified" in report["boundary"]["dynamic_members"]
    assert "never summed" in report["boundary"]["container_memory"]
    assert await sampler.stop() == report
    report["processes"].clear()
    assert len((await sampler.stop())["processes"]) in {2, 3}


@pytest.mark.parametrize(
    "budget,code",
    [
        ("maximum_process_rss_bytes", "process_rss_limit_exceeded"),
        ("maximum_total_process_rss_bytes", "process_total_rss_limit_exceeded"),
        ("maximum_container_working_set_bytes", "container_memory_limit_exceeded"),
        ("maximum_total_container_working_set_bytes", "container_total_memory_limit_exceeded"),
    ],
)
async def test_each_resource_budget_cancels_with_its_own_metric(
    budget: str,
    code: str,
    owned_driver: ProcessIdentity,
) -> None:
    event = asyncio.Event()
    options: dict[str, Any] = {budget: 1}
    async with stats_server() as (_, query):
        sampler = create(query, event, owned_driver, **options).start()
        with pytest.raises(ResourceSamplingError, match="initial_resource_observation_failed"):
            await sampler.wait_first_sample()
        report = await sampler.stop()
    assert event.is_set() and code in report["errors"] and not report["budget_passed"]
    assert report["process_rss"]["observed_peak_total_bytes"] is not None
    assert report["container_working_set"]["observed_peak_total_bytes"] == 4096


@pytest.mark.parametrize(
    "mode,code",
    [
        ("identity", "container_identity_changed"),
        ("missing_metric", "container_metrics_unavailable"),
        ("missing_container", "container_observations_unavailable"),
        ("stale", "container_observation_stale"),
        ("stopped", "container_not_running"),
    ],
)
async def test_container_runtime_evidence_failures_are_not_zero_filled(
    mode: str,
    code: str,
    owned_driver: ProcessIdentity,
) -> None:
    event = asyncio.Event()
    async with stats_server() as (state, query):
        state.mode = mode
        sampler = create(query, event, owned_driver).start()
        with pytest.raises(ResourceSamplingError):
            await sampler.wait_first_sample()
        report = await sampler.stop()
    assert event.is_set() and code in report["errors"]
    assert report["container_working_set"]["observed_peak_total_bytes"] is None
    failed = report["containers"][0]
    assert failed["observed_peak_working_set_bytes"] is None
    assert failed["samples"][0]["memory_working_set_bytes"] is None
    assert failed["memory_coverage"] == 0


async def test_actual_pid_with_wrong_start_identity_cancels_without_metrics(
    owned_driver: ProcessIdentity,
) -> None:
    identity = owned_driver
    event = asyncio.Event()
    async with stats_server() as (_, query):
        sampler = create(
            query,
            event,
            owned_driver,
            members=lambda: (replace(identity, start_identity="wrong-start"),),
        ).start()
        with pytest.raises(ResourceSamplingError):
            await sampler.wait_first_sample()
        report = await sampler.stop()
    assert event.is_set() and "process_identity_changed" in report["errors"]
    assert report["process_rss"]["observed_peak_total_bytes"] is None
    assert report["processes"][0]["samples"][0]["rss_bytes"] is None


async def test_members_callback_failure_does_not_invent_empty_process_usage(
    owned_driver: ProcessIdentity,
) -> None:
    def unavailable() -> Sequence[ProcessIdentity]:
        raise RuntimeError("private-command-and-credentials")

    event = asyncio.Event()
    async with stats_server() as (_, query):
        sampler = create(query, event, owned_driver, members=unavailable).start()
        with pytest.raises(ResourceSamplingError):
            await sampler.wait_first_sample()
        report = await sampler.stop()
    assert event.is_set() and report["slots"][0]["process_count"] is None
    assert report["process_rss"]["observed_peak_total_bytes"] is None
    assert "private-command-and-credentials" not in json.dumps(report)


async def test_real_http_observation_timeout_and_stop_are_bounded(
    owned_driver: ProcessIdentity,
) -> None:
    event = asyncio.Event()
    async with stats_server() as (state, query):
        state.mode = "blocked"
        sampler = create(query, event, owned_driver, callback_timeout_seconds=0.05).start()
        started = time.perf_counter()
        with pytest.raises(ResourceSamplingError):
            await sampler.wait_first_sample()
        report = await sampler.stop()
    assert time.perf_counter() - started < 1
    assert event.is_set() and "resource_observation_timeout" in report["errors"]
    assert report["cleanup_passed"] and report["sampler_stopped"]
    assert report["container_working_set"]["observed_peak_total_bytes"] is None


async def test_context_exception_stops_only_sampler_and_preserves_observed_metrics(
    owned_driver: ProcessIdentity,
) -> None:
    event = asyncio.Event()
    identity = owned_driver
    async with stats_server() as (_, query):
        sampler = create(query, event, owned_driver)
        with pytest.raises(ValueError, match="fixture-work-failed"):
            async with sampler:
                await sampler.wait_first_sample()
                raise ValueError("fixture-work-failed")
        report = await sampler.stop()
    assert report["cleanup_passed"] and report["sampler_stopped"]
    assert register_process(identity.pid, label=identity.label) == identity
    assert report["process_rss"]["observed_peak_total_bytes"] is not None
    with pytest.raises(RuntimeError):
        sampler.start()


def test_exact_container_set_and_bounded_schedule_are_required() -> None:
    with pytest.raises(ValidationError):
        ContainerIdentity(role="redis", container_id="short-id", identity_verified=True)
    with pytest.raises(ValidationError):
        limits(duration_seconds=900.0, interval_seconds=0.02)
    with pytest.raises(ValidationError):
        limits(duration_seconds=float("nan"))


async def test_unverified_container_and_no_initial_sample_are_rejected(
    owned_driver: ProcessIdentity,
) -> None:
    event = asyncio.Event()
    async with stats_server() as (_, query):
        with pytest.raises(ValueError):
            ServicesResourceSampler(
                members=lambda: (),
                containers=IDENTITIES[:3],
                container_stats=query,
                limits=limits(),
                cancel_event=event,
            )
        sampler = create(query, event, owned_driver)
        with pytest.raises(ResourceSamplingError, match="not_started"):
            await sampler.wait_first_sample()
        report = await sampler.stop()
    assert (
        event.is_set()
        and not report["budget_passed"]
        and report["process_rss"]["observed_peak_total_bytes"] is None
    )


async def test_cancel_stop_waits_for_owned_callback_cleanup_beyond_one_second(
    owned_driver: ProcessIdentity,
) -> None:
    event, entered, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async with stats_server() as (state, query):

        async def controlled() -> tuple[ContainerSample, ...]:
            await (
                query()
            )  # A real fixture HTTP request occurs before this owned cancellation boundary.
            entered.set()
            try:
                await asyncio.Event().wait()
                raise RuntimeError("unexpected fixture release")
            except asyncio.CancelledError:
                # Model contained-command child cleanup using actual elapsed time.
                await asyncio.sleep(1.2)
                cleaned.set()
                raise

        sampler = create(
            controlled,
            event,
            owned_driver,
            callback_timeout_seconds=4.0,
            cleanup_timeout_seconds=3.0,
            duration_seconds=5.0,
        ).start()
        await asyncio.wait_for(entered.wait(), 2)
        event.set()
        started = time.perf_counter()
        report = await sampler.stop()
        elapsed = time.perf_counter() - started
        observed_calls = state.calls
        await asyncio.sleep(0.15)
        assert state.calls == observed_calls == 1
    assert 1.15 <= elapsed < 3
    assert cleaned.is_set() and not sampler.is_running
    assert report["sampler_stopped"] and report["cleanup_passed"]
    assert report["cleanup_elapsed_seconds"] >= 1.15
    assert not report["budget_passed"] and "resource_observation_cancelled" in report["errors"]
    assert report["slots"][0]["container_total_working_set_bytes"] is None


async def test_slow_real_observation_lowers_coverage_and_cancels_missed_schedule(
    owned_driver: ProcessIdentity,
) -> None:
    event = asyncio.Event()
    async with stats_server() as (_, query):

        async def delayed() -> tuple[ContainerSample, ...]:
            await asyncio.sleep(0.15)
            return await query()

        sampler = create(delayed, event, owned_driver, interval_seconds=0.05).start()
        await asyncio.wait_for(event.wait(), 1)
        report = await sampler.stop()
    assert "sampling_intervals_missed" in report["errors"]
    assert report["attempted_slot_count"] == 1
    assert 0 < report["slot_coverage"] < 1
    assert not report["budget_passed"] and report["cleanup_passed"]


async def test_parent_resident_allocation_is_excluded_but_explicit_overbudget_pid_is_rejected(
    owned_driver: ProcessIdentity,
) -> None:
    # A lower fixed budget makes the boundary visible without retaining >512 MiB
    # in the routine suite. Both runs use the same real native reader and limits.
    budget = 64 * 1024**2
    retained = bytearray(32 * 1024**2)
    for offset in range(0, len(retained), 4096):
        retained[offset] = 1
    parent = register_process(label="pytest-outside-workload")
    parent_sample = read_process(parent)
    child_sample = read_process(owned_driver)
    assert parent_sample.rss_bytes is not None and parent_sample.rss_bytes > budget
    assert child_sample.rss_bytes is not None and child_sample.rss_bytes < budget
    event = asyncio.Event()
    options: dict[str, Any] = {
        "maximum_process_rss_bytes": budget,
        "maximum_total_process_rss_bytes": budget,
    }
    async with stats_server() as (_, query):
        sampler = create(query, event, owned_driver, **options).start()
        try:
            await sampler.wait_first_sample()
        finally:
            isolated = await sampler.stop()
        assert isolated["budget_passed"] and not event.is_set()
        assert {item["identity"]["pid"] for item in isolated["processes"]} == {owned_driver.pid}
        assert isolated["process_rss"]["observed_peak_total_bytes"] < budget
        overbudget = create(query, event, parent, **options).start()
        try:
            with pytest.raises(ResourceSamplingError, match="initial_resource_observation_failed"):
                await overbudget.wait_first_sample()
        finally:
            rejected = await overbudget.stop()
        assert event.is_set() and not rejected["budget_passed"]
        assert "process_rss_limit_exceeded" in rejected["errors"]
        assert "process_total_rss_limit_exceeded" in rejected["errors"]
        assert rejected["process_rss"]["observed_peak_total_bytes"] > budget
        assert isolated["cleanup_passed"] and rejected["cleanup_passed"]
        assert retained[0] == retained[-4096] == 1
