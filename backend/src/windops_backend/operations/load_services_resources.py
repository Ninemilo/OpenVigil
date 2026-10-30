"""Dynamic, owned-member resource observations with separate process/container budgets.

Only caller-supplied containment members are read. Native reads are query-only;
the process owner retains the hard termination boundary for a blocked native call.
Sampled peaks and presence coverage do not establish complete short-child lifetimes.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from windops_backend.operations.load_resources import (
    ProcessIdentity,
    ResourceObservationError,
    read_process,
    register_process,
)
from windops_backend.operations.load_runner import _quiet_transport, _settle

ContainerRole = Literal["postgres", "redis", "minio", "neo4j"]
ROLES = frozenset({"postgres", "redis", "minio", "neo4j"})
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")


class ResourceSamplingError(RuntimeError):
    """Only fixed public codes; underlying callback diagnostics remain private."""


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class ContainerIdentity(_Contract):
    role: ContainerRole
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_verified: bool


class ContainerSample(_Contract):
    role: ContainerRole
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    running: bool
    memory_working_set_bytes: int | None = Field(ge=0)
    cpu_percent: float | None = Field(ge=0, le=1_000_000)

    @model_validator(mode="after")
    def aware(self) -> Self:
        if self.observed_at.utcoffset() is None:
            raise ValueError("timezone-aware container observation required")
        return self


class ServicesResourceLimits(_Contract):
    duration_seconds: float = Field(gt=0, le=900)
    interval_seconds: float = Field(ge=0.02, le=10)
    callback_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    cleanup_timeout_seconds: float = Field(default=10.0, gt=0, le=10)
    maximum_slots: int = Field(default=10_000, ge=1, le=10_000)
    maximum_members: int = Field(default=64, ge=1, le=128)
    maximum_unique_members: int = Field(default=256, ge=1, le=1024)
    maximum_process_rss_bytes: int = Field(gt=0, le=256 * 1024**3)
    maximum_total_process_rss_bytes: int = Field(gt=0, le=1024 * 1024**3)
    maximum_container_working_set_bytes: int = Field(gt=0, le=256 * 1024**3)
    maximum_total_container_working_set_bytes: int = Field(gt=0, le=1024 * 1024**3)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        slots = math.ceil(self.duration_seconds / self.interval_seconds)
        if (
            self.interval_seconds > self.duration_seconds
            or slots > self.maximum_slots
            or slots * self.maximum_members > 100_000
        ):
            raise ValueError("resource observation plan exceeds its bound")
        return self


class ServicesResourceSampler:
    def __init__(
        self,
        *,
        members: Callable[[], Sequence[ProcessIdentity]],
        containers: Sequence[ContainerIdentity],
        container_stats: Callable[[], Awaitable[Sequence[ContainerSample]]],
        limits: ServicesResourceLimits,
        cancel_event: asyncio.Event,
    ) -> None:
        if (
            len(containers) != 4
            or {item.role for item in containers} != ROLES
            or len({item.container_id for item in containers}) != 4
            or not all(item.identity_verified for item in containers)
        ):
            raise ValueError("four distinct verified owned container identities required")
        if not callable(members) or not callable(container_stats):
            raise ValueError("trusted resource callbacks required")
        self.members, self.container_stats, self.limits = members, container_stats, limits
        self.containers = tuple(containers)
        self.cancel_event = cancel_event
        self._stop, self._first = asyncio.Event(), asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._children: set[asyncio.Task[Any]] = set()
        self._started: float | None = None
        self._ended: float | None = None
        self._planned = math.ceil(limits.duration_seconds / limits.interval_seconds)
        self._slots: list[dict[str, Any]] = []
        self._processes: dict[int, dict[str, Any]] = {}
        self._container_rows: dict[str, list[dict[str, Any]]] = {
            item.role: [] for item in containers
        }
        self._errors: list[str] = []
        self._report: dict[str, Any] | None = None
        self._stop_task: asyncio.Task[dict[str, Any]] | None = None
        self._frozen = False

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _fail(self, code: str) -> None:
        if code not in self._errors:
            self._errors.append(code)
        self.cancel_event.set()

    def _native(self) -> tuple[list[dict[str, Any]], str | None]:
        try:
            members = self.members()
            if (
                not isinstance(members, Sequence)
                or not 1 <= len(members) <= self.limits.maximum_members
            ):
                return [], "process_members_unavailable"
            if any(
                not isinstance(item, ProcessIdentity) or not _ALIAS.fullmatch(item.label)
                for item in members
            ):
                return [], "process_identity_invalid"
            if len({item.pid for item in members}) != len(members):
                return [], "process_members_duplicate"
        except Exception:
            return [], "process_members_unavailable"
        rows: list[dict[str, Any]] = []
        for identity in members:
            row: dict[str, Any] = {
                "identity": asdict(identity),
                "rss_bytes": None,
                "cpu_seconds": None,
                "error_code": None,
            }
            try:
                actual = register_process(identity.pid, label=identity.label)
                if actual != identity:
                    raise ResourceSamplingError("process_identity_changed")
                sample = read_process(identity)
                if (
                    sample.rss_bytes is None
                    or sample.rss_bytes <= 0
                    or not math.isfinite(sample.cpu_seconds)
                    or sample.cpu_seconds < 0
                    or sample.issues
                ):
                    raise ResourceSamplingError("process_metrics_unavailable")
                row.update(rss_bytes=sample.rss_bytes, cpu_seconds=sample.cpu_seconds)
            except ResourceSamplingError as exc:
                row["error_code"] = str(exc)
            except ResourceObservationError as exc:
                row["error_code"] = (
                    "process_identity_changed"
                    if exc.code == "PROCESS_IDENTITY_CHANGED"
                    else "process_metrics_unavailable"
                )
            except Exception:
                row["error_code"] = "process_metrics_unavailable"
            rows.append(row)
        return rows, None

    async def _containers(self) -> tuple[list[dict[str, Any]], str | None]:
        try:
            samples = await self.container_stats()
            if (
                not isinstance(samples, Sequence)
                or len(samples) != 4
                or any(not isinstance(item, ContainerSample) for item in samples)
                or {item.role for item in samples} != ROLES
            ):
                return [], "container_observations_unavailable"
        except asyncio.CancelledError:
            raise
        except Exception:
            return [], "container_observations_unavailable"
        expected = {item.role: item.container_id for item in self.containers}
        rows = []
        for sample in samples:
            error = None
            if sample.container_id != expected[sample.role]:
                error = "container_identity_changed"
            elif not sample.running:
                error = "container_not_running"
            elif not -5 <= (datetime.now(UTC) - sample.observed_at).total_seconds() <= 30:
                error = "container_observation_stale"
            elif sample.memory_working_set_bytes is None or sample.cpu_percent is None:
                error = "container_metrics_unavailable"
            rows.append(
                {
                    "role": sample.role,
                    "container_id": expected[sample.role],
                    "observed_at": sample.observed_at.isoformat(),
                    "memory_working_set_bytes": sample.memory_working_set_bytes
                    if error is None
                    else None,
                    "cpu_percent": sample.cpu_percent if error is None else None,
                    "error_code": error,
                }
            )
        return rows, None

    async def _sample(self, slot: int) -> None:
        row: dict[str, Any] = {
            "slot": slot,
            "elapsed_seconds": self._elapsed(),
            "process_count": None,
            "process_total_rss_bytes": None,
            "container_total_working_set_bytes": None,
            "process_error": None,
            "container_error": None,
        }
        native = asyncio.create_task(asyncio.to_thread(self._native))
        containers = asyncio.create_task(self._containers())
        self._children.update((native, containers))
        try:
            remaining = self.limits.duration_seconds - self._elapsed()
            async with asyncio.timeout(
                min(self.limits.callback_timeout_seconds, max(0.001, remaining))
            ):
                process_result, container_result = await asyncio.gather(native, containers)
            if self._frozen:
                return
            processes, row["process_error"] = process_result
            container_rows, row["container_error"] = container_result
            if row["process_error"]:
                self._fail(row["process_error"])
            else:
                row["process_count"] = len(processes)
                for process in processes:
                    identity = process.pop("identity")
                    pid = identity["pid"]
                    previous = self._processes.get(pid)
                    if previous is not None and previous["identity"] != identity:
                        process.update(
                            error_code="process_identity_changed", rss_bytes=None, cpu_seconds=None
                        )
                    if previous is None:
                        if len(self._processes) >= self.limits.maximum_unique_members:
                            self._fail("unique_process_budget_exhausted")
                            continue
                        previous = {"identity": identity, "samples": []}
                        self._processes[pid] = previous
                    earlier = previous["samples"]
                    if (
                        earlier
                        and earlier[-1]["cpu_seconds"] is not None
                        and process["cpu_seconds"] is not None
                        and process["cpu_seconds"] < earlier[-1]["cpu_seconds"]
                    ):
                        process.update(error_code="process_cpu_counter_regressed", cpu_seconds=None)
                    previous["samples"].append(
                        {"slot": slot, "elapsed_seconds": self._elapsed(), **process}
                    )
                    if process["error_code"]:
                        self._fail(process["error_code"])
                if processes and all(item["rss_bytes"] is not None for item in processes):
                    row["process_total_rss_bytes"] = sum(item["rss_bytes"] for item in processes)
                    if any(
                        item["rss_bytes"] > self.limits.maximum_process_rss_bytes
                        for item in processes
                    ):
                        self._fail("process_rss_limit_exceeded")
                    if row["process_total_rss_bytes"] > self.limits.maximum_total_process_rss_bytes:
                        self._fail("process_total_rss_limit_exceeded")
            if row["container_error"]:
                self._fail(row["container_error"])
                container_rows = [
                    {
                        "role": item.role,
                        "container_id": item.container_id,
                        "observed_at": None,
                        "memory_working_set_bytes": None,
                        "cpu_percent": None,
                        "error_code": row["container_error"],
                    }
                    for item in self.containers
                ]
            for container in container_rows:
                self._container_rows[container["role"]].append({"slot": slot, **container})
                if container["error_code"]:
                    self._fail(container["error_code"])
            if container_rows and all(
                item["memory_working_set_bytes"] is not None for item in container_rows
            ):
                row["container_total_working_set_bytes"] = sum(
                    item["memory_working_set_bytes"] for item in container_rows
                )
                if any(
                    item["memory_working_set_bytes"]
                    > self.limits.maximum_container_working_set_bytes
                    for item in container_rows
                ):
                    self._fail("container_memory_limit_exceeded")
                if (
                    row["container_total_working_set_bytes"]
                    > self.limits.maximum_total_container_working_set_bytes
                ):
                    self._fail("container_total_memory_limit_exceeded")
        except TimeoutError:
            row["process_error"] = row["container_error"] = "resource_observation_timeout"
            self._fail("resource_observation_timeout")
        except asyncio.CancelledError:
            if not self._frozen:
                row["process_error"] = row["container_error"] = "resource_observation_cancelled"
                self._fail("resource_observation_cancelled")
            raise
        finally:
            for child in (native, containers):
                if child.done():
                    self._children.discard(child)
            if not self._frozen:
                self._slots.append(row)
                self._first.set()

    def _elapsed(self) -> float:
        return 0.0 if self._started is None else time.perf_counter() - self._started

    async def _run(self) -> None:
        try:
            slot = 0
            while (
                slot < self._planned and not self._stop.is_set() and not self.cancel_event.is_set()
            ):
                wait = slot * self.limits.interval_seconds - self._elapsed()
                if wait > 0:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=wait)
                        break
                    except TimeoutError:
                        pass
                if self._elapsed() >= self.limits.duration_seconds:
                    break
                if self._stop.is_set() or self.cancel_event.is_set():
                    break
                actual_slot = int(self._elapsed() / self.limits.interval_seconds)
                if actual_slot > slot:
                    self._fail("sampling_intervals_missed")
                    break
                await self._sample(slot)
                slot += 1
            if not self._stop.is_set() and not self.cancel_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=max(0.001, self.limits.duration_seconds - self._elapsed()),
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("resource_sampler_failed")
        finally:
            self._ended = time.perf_counter()
            self._first.set()

    def start(self) -> Self:
        if self._task is not None or self._stop.is_set():
            raise RuntimeError("resource_sampler_already_started_or_stopped")
        self._started = time.perf_counter()
        self._task = asyncio.create_task(self._run())
        return self

    async def wait_first_sample(self) -> None:
        if self._task is None:
            self._fail("resource_sampler_not_started")
            raise ResourceSamplingError("resource_sampler_not_started")
        try:
            async with asyncio.timeout(self.limits.callback_timeout_seconds + 1.0):
                await self._first.wait()
        except TimeoutError:
            self._fail("resource_observation_timeout")
        if (
            not self._slots
            or self._errors
            or self._slots[0]["process_total_rss_bytes"] is None
            or self._slots[0]["container_total_working_set_bytes"] is None
        ):
            self._fail("initial_resource_observation_failed")
            raise ResourceSamplingError("initial_resource_observation_failed")

    async def _finish(self) -> dict[str, Any]:
        self._stop.set()
        cleanup_started = time.perf_counter()
        with _quiet_transport() as unsettled:
            tasks = [
                task
                for task in [*self._children, *([self._task] if self._task is not None else [])]
                if not task.done()
            ]
            cleanup_passed = await _settle(
                tasks,
                deadline=cleanup_started + self.limits.cleanup_timeout_seconds,
                cancel=self.cancel_event.is_set(),
                unsettled=unsettled,
            )
        self._frozen = True
        if not cleanup_passed:
            self._fail("resource_sampler_cleanup_failed")
        if self._started is None:
            self._fail("resource_sampler_not_started")
        elif not self._slots:
            self._fail("resource_observations_unavailable")
        elapsed = max(
            0.0, (self._ended or time.perf_counter()) - (self._started or time.perf_counter())
        )
        expected = (
            min(self._planned, math.ceil(elapsed / self.limits.interval_seconds))
            if self._started is not None
            else 0
        )
        processes = []
        for process in self._processes.values():
            samples = process["samples"]
            rss = [item["rss_bytes"] for item in samples if item["rss_bytes"] is not None]
            cpu = [item for item in samples if item["cpu_seconds"] is not None]
            processes.append(
                {
                    **process,
                    "presence_observations": len(samples),
                    "rss_valid_observations": len(rss),
                    "cpu_valid_observations": len(cpu),
                    "rss_presence_coverage": len(rss) / len(samples),
                    "cpu_presence_coverage": len(cpu) / len(samples),
                    "observed_peak_rss_bytes": max(rss) if rss else None,
                    "cpu_observed_delta_seconds": cpu[-1]["cpu_seconds"] - cpu[0]["cpu_seconds"]
                    if len(cpu) >= 2
                    else None,
                    "cpu_observed_interval_seconds": cpu[-1]["elapsed_seconds"]
                    - cpu[0]["elapsed_seconds"]
                    if len(cpu) >= 2
                    else None,
                }
            )
        containers = []
        for identity in self.containers:
            samples = self._container_rows[identity.role]
            memory = [
                item["memory_working_set_bytes"]
                for item in samples
                if item["memory_working_set_bytes"] is not None
            ]
            cpu_values = [
                item["cpu_percent"] for item in samples if item["cpu_percent"] is not None
            ]
            containers.append(
                {
                    "identity": identity.model_dump(),
                    "samples": samples,
                    "memory_valid_observations": len(memory),
                    "cpu_valid_observations": len(cpu_values),
                    "memory_coverage": len(memory) / expected if expected else None,
                    "cpu_coverage": len(cpu_values) / expected if expected else None,
                    "observed_peak_working_set_bytes": max(memory) if memory else None,
                    "observed_peak_cpu_percent": max(cpu_values) if cpu_values else None,
                }
            )
        process_totals = [
            item["process_total_rss_bytes"]
            for item in self._slots
            if item["process_total_rss_bytes"] is not None
        ]
        container_totals = [
            item["container_total_working_set_bytes"]
            for item in self._slots
            if item["container_total_working_set_bytes"] is not None
        ]
        if expected and (len(process_totals) < expected or len(container_totals) < expected):
            self._fail("resource_coverage_incomplete")
        self._report = {
            "schema_version": "openvigil.load-services-resources.v1",
            "sampler_stopped": not self.is_running,
            "cleanup_passed": cleanup_passed,
            "cleanup_elapsed_seconds": time.perf_counter() - cleanup_started,
            "limits": self.limits.model_dump(),
            "errors": self._errors.copy(),
            "budget_passed": not self._errors
            and bool(self._slots)
            and not self.cancel_event.is_set(),
            "requested_duration_seconds": self.limits.duration_seconds,
            "elapsed_seconds": elapsed,
            "completed_duration": elapsed >= self.limits.duration_seconds,
            "planned_slot_count": self._planned,
            "expected_slot_count": expected,
            "attempted_slot_count": len(self._slots),
            "slot_coverage": len(self._slots) / expected if expected else None,
            "interval_seconds": self.limits.interval_seconds,
            "slots": self._slots,
            "processes": processes,
            "containers": containers,
            "process_rss": {
                "complete_observation_slots": len(process_totals),
                "coverage": len(process_totals) / expected if expected else None,
                "observed_peak_total_bytes": max(process_totals) if process_totals else None,
            },
            "container_working_set": {
                "complete_observation_slots": len(container_totals),
                "coverage": len(container_totals) / expected if expected else None,
                "observed_peak_total_bytes": max(container_totals) if container_totals else None,
            },
            "boundary": {
                "peaks": "sampled observations, not absolute peaks",
                "process_memory": "native current resident bytes",
                "container_memory": (
                    "Docker working-set usage; separate from process RSS; "
                    "never summed with host processes"
                ),
                "process_cpu": (
                    "user plus kernel cumulative seconds; delta covers observed interval only"
                ),
                "container_cpu": (
                    "Docker interval CPU percentage; not host process cumulative CPU seconds"
                ),
                "dynamic_members": (
                    "Only explicit owned containment members; short children "
                    "can appear and exit between samples; full lifetime coverage unverified"
                ),
                "timeout": (
                    "Read-only native threads may finish after timeout; "
                    "process owner enforces hard termination"
                ),
            },
        }
        return copy.deepcopy(self._report)

    async def stop(self) -> dict[str, Any]:
        if self._report is not None:
            return copy.deepcopy(self._report)
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._finish())
        while not self._stop_task.done():
            try:
                await asyncio.shield(self._stop_task)
            except asyncio.CancelledError:
                self._fail("resource_sampler_stop_cancelled")
        return copy.deepcopy(self._stop_task.result())

    async def __aenter__(self) -> Self:
        return self.start()

    async def __aexit__(self, *_args: object) -> None:
        await self.stop()
