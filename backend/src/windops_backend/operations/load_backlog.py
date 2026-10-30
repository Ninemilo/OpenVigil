"""Owned-worker fault orchestration; callbacks supply real HTTP/SQL/process evidence.

This module never changes clocks, leases, queues or database rows. A successful
control fixture is not evidence that the natural production waits or OCR ran.
The runtime owner must clean up processes even after cancellation or unknown control.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from windops_backend.document_worker_health import HEARTBEAT_TTL_SECONDS
from windops_backend.operations.load_documents import DocumentBatchResult
from windops_backend.operations.load_runner import _quiet_transport, _settle
from windops_backend.outbox import OUTBOX_LEASE

Scenario = Literal["offline_then_resume", "claim_loss_then_recover"]
T = TypeVar("T")
LEASE_SECONDS = OUTBOX_LEASE.total_seconds()


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class BacklogLimits(_Contract):
    maximum_total_seconds: float = Field(default=900.0, ge=180, le=900)
    callback_timeout_seconds: float = Field(default=30.0, gt=0, le=30)
    poll_interval_seconds: float = Field(default=2.0, ge=0.01, le=10)
    health_timeout_seconds: float = Field(default=120.0, gt=0, le=300)
    claim_timeout_seconds: float = Field(default=90.0, gt=0, le=300)
    drain_timeout_seconds: float = Field(default=120.0, gt=0, le=300)
    maximum_callbacks: int = Field(default=2048, ge=1, le=4096)
    maximum_health_observations: int = Field(default=512, ge=1, le=1024)


class WorkerReceipt(_Contract):
    worker_alias: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    generation: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    identity_verified: bool
    running: bool
    abrupt: bool = False


class BacklogHealth(_Contract):
    """Fresh, scope-verified aggregate of the two authorized API observations."""

    status: Literal[
        "unconfigured",
        "unavailable",
        "offline",
        "starting",
        "ready",
        "degraded",
        "version_mismatch",
    ]
    worker_health_verified: bool
    heartbeat_verified: bool
    runtime_ready: bool
    heartbeat_ttl_seconds: int = Field(ge=1, le=3600)
    observed_at: datetime
    queue_observed_at: datetime
    scope_verified: bool
    waiting: int = Field(ge=0)
    running: int = Field(ge=0)
    retrying: int = Field(ge=0)
    expired_leases: int = Field(ge=0)
    backlogged: bool
    backlog_warning_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def aware(self) -> Self:
        if self.observed_at.utcoffset() is None or self.queue_observed_at.utcoffset() is None:
            raise ValueError("timezone-aware health observations required")
        return self


class ClaimWitness(_Contract):
    """Read-only bound SQL claim and matching persisted parse.started event."""

    document_alias: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    job_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt: int = Field(ge=1, le=3)
    claimed_at: datetime
    lease_expires_at: datetime
    observed_at: datetime
    processing: bool
    started_event_verified: bool
    owned_scope_verified: bool

    @model_validator(mode="after")
    def valid_times(self) -> Self:
        if any(
            value.utcoffset() is None
            for value in (self.claimed_at, self.lease_expires_at, self.observed_at)
        ):
            raise ValueError("timezone-aware claim observations required")
        if abs((self.lease_expires_at - self.claimed_at).total_seconds() - LEASE_SECONDS) > 0.1:
            raise ValueError("claim must retain the production lease")
        if not self.claimed_at <= self.observed_at < self.lease_expires_at:
            raise ValueError("an active claim observation is required")
        return self


class RecoveryWitness(_Contract):
    owned_documents: int = Field(ge=1, le=32)
    indexed: int = Field(ge=0, le=32)
    terminal_failed: int = Field(ge=0, le=32)
    unfinished: int = Field(ge=0, le=32)
    scope_verified: bool
    unique_publications_verified: bool
    recovered_job_id_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recovered_attempt: int | None = Field(default=None, ge=1, le=3)
    old_claim_inactive: bool = False


@dataclass(frozen=True)
class BacklogCallbacks:
    current_worker: Callable[[], Awaitable[WorkerReceipt]] = field(repr=False)
    read_health: Callable[[], Awaitable[BacklogHealth]] = field(repr=False)
    stop_worker: Callable[[], Awaitable[WorkerReceipt]] = field(repr=False)
    start_worker: Callable[[], Awaitable[WorkerReceipt]] = field(repr=False)
    run_documents: Callable[[Callable[[], Awaitable[None]]], Awaitable[DocumentBatchResult]] = (
        field(repr=False)
    )
    read_claim: Callable[[], Awaitable[ClaimWitness | None]] = field(repr=False)
    verify_offline_documents: Callable[[], Awaitable[bool]] = field(repr=False)
    verify_recovery: Callable[[ClaimWitness | None], Awaitable[RecoveryWitness]] = field(repr=False)


@dataclass
class BacklogStage:
    stage: str
    outcome: Literal["not_run", "success", "failure", "timeout", "cancelled"] = "not_run"
    started_monotonic: float | None = None
    finished_monotonic: float | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class BacklogResult:
    scenario: Scenario
    planned_documents: int
    outcome: Literal["success", "failure", "cancelled"]
    stages: tuple[BacklogStage, ...]
    elapsed_seconds: float
    error_code: str | None
    callback_count: int
    initial_worker: WorkerReceipt | None
    restarted_worker: WorkerReceipt | None
    claim: ClaimWitness | None
    recovery: RecoveryWitness | None
    health_observations: tuple[dict[str, object], ...]
    document_batch: DocumentBatchResult | None
    task_cleanup_passed: bool
    # Process lifecycle remains the runtime owner's responsibility, never inferred here.
    external_services_verified: bool = False

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        for name in ("initial_worker", "restarted_worker", "claim", "recovery"):
            value = getattr(self, name)
            result[name] = value.model_dump(mode="json") if value is not None else None
        return result


class _Failure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _fresh(value: datetime) -> bool:
    return -5 <= (_utc_now() - value).total_seconds() <= HEARTBEAT_TTL_SECONDS


class _Run:
    def __init__(
        self, scenario: Scenario, planned: int, callbacks: BacklogCallbacks, limits: BacklogLimits
    ) -> None:
        self.scenario, self.planned, self.callbacks, self.limits = (
            scenario,
            planned,
            callbacks,
            limits,
        )
        self.started = time.perf_counter()
        names = ["initial_worker", "initial_ready"]
        if scenario == "offline_then_resume":
            names += ["stop_worker", "wait_offline", "document_batch", "offline_documents"]
        else:
            names += [
                "document_batch",
                "wait_claim",
                "stop_worker",
                "wait_offline",
                "wait_lease_expiry",
            ]
        names += ["restart_worker", "wait_ready", "drain_queue", "verify_recovery"]
        self.stages = tuple(BacklogStage(name) for name in names)
        self.calls = 0
        self.health: list[dict[str, object]] = []
        self.initial: WorkerReceipt | None = None
        self.restarted: WorkerReceipt | None = None
        self.claim: ClaimWitness | None = None
        self.claim_deadline_monotonic: float | None = None
        self.recovery: RecoveryWitness | None = None
        self.batch: DocumentBatchResult | None = None
        self.error: str | None = None
        self.stopped_at: float | None = None

    def now(self) -> float:
        return time.perf_counter() - self.started

    async def invoke(self, call: Callable[[], Awaitable[T]], *, whole_batch: bool = False) -> T:
        if self.calls >= self.limits.maximum_callbacks:
            raise _Failure("callback_budget_exhausted")
        self.calls += 1
        remaining = self.limits.maximum_total_seconds - self.now()
        if remaining <= 0:
            raise _Failure("backlog_deadline_exceeded")
        timeout = remaining if whole_batch else min(remaining, self.limits.callback_timeout_seconds)
        try:
            async with asyncio.timeout(timeout):
                return await call()
        except _Failure:
            raise
        except TimeoutError:
            raise _Failure("callback_timeout") from None
        except Exception:
            raise _Failure("callback_failed") from None

    async def stage(self, name: str, operation: Callable[[], Awaitable[None]]) -> None:
        stage = next(value for value in self.stages if value.stage == name)
        stage.started_monotonic = self.now()
        try:
            await operation()
            stage.outcome = "success"
        except asyncio.CancelledError:
            stage.outcome, stage.error_code = "cancelled", self.error or "backlog_cancelled"
            raise
        except _Failure as exc:
            stage.outcome = (
                "timeout" if "timeout" in exc.code or "deadline" in exc.code else "failure"
            )
            stage.error_code = exc.code
            raise
        except Exception:
            stage.outcome, stage.error_code = "failure", "backlog_stage_failed"
            raise _Failure("backlog_stage_failed") from None
        finally:
            stage.finished_monotonic = self.now()

    async def read_health(self) -> BacklogHealth:
        if len(self.health) >= self.limits.maximum_health_observations:
            raise _Failure("health_observation_budget_exhausted")
        value = await self.invoke(self.callbacks.read_health)
        if not isinstance(value, BacklogHealth) or not value.scope_verified:
            raise _Failure("health_evidence_invalid")
        if (
            value.heartbeat_ttl_seconds != HEARTBEAT_TTL_SECONDS
            or value.backlog_warning_seconds != 300
        ):
            raise _Failure("production_timing_mismatch")
        if not _fresh(value.observed_at) or not _fresh(value.queue_observed_at):
            raise _Failure("health_evidence_stale")
        self.health.append({"elapsed_seconds": self.now(), **value.model_dump(mode="json")})
        return value

    async def wait_health(self, *, offline: bool = False, drain: bool = False) -> None:
        timeout = self.limits.drain_timeout_seconds if drain else self.limits.health_timeout_seconds
        until = self.now() + timeout
        while self.now() < until:
            value = await self.read_health()
            ready = (
                value.status == "ready"
                and value.worker_health_verified
                and value.heartbeat_verified
                and value.runtime_ready
            )
            if (
                drain
                and ready
                and value.waiting == value.running == value.retrying == value.expired_leases == 0
            ):
                return
            if not drain and not offline and ready:
                return
            if offline:
                if self.stopped_at is None:
                    raise _Failure("owned_stop_evidence_missing")
                expired = self.now() - self.stopped_at >= HEARTBEAT_TTL_SECONDS
                if (
                    expired
                    and value.status == "offline"
                    and not any(
                        (
                            value.worker_health_verified,
                            value.heartbeat_verified,
                            value.runtime_ready,
                        )
                    )
                ):
                    return
            await asyncio.sleep(min(self.limits.poll_interval_seconds, max(0, until - self.now())))
        raise _Failure("queue_drain_timeout" if drain else "worker_health_timeout")

    async def initial_worker(self) -> None:
        value = await self.invoke(self.callbacks.current_worker)
        if not isinstance(value, WorkerReceipt) or not value.identity_verified or not value.running:
            raise _Failure("owned_worker_unverified")
        self.initial = value

    async def stop_worker(self) -> None:
        value = await self.invoke(self.callbacks.stop_worker)
        if (
            self.initial is None
            or not isinstance(value, WorkerReceipt)
            or not value.identity_verified
            or value.running
            or not value.abrupt
            or value.generation != self.initial.generation
            or value.worker_alias != self.initial.worker_alias
        ):
            raise _Failure("owned_stop_unverified")
        self.stopped_at = self.now()

    async def restart_worker(self) -> None:
        value = await self.invoke(self.callbacks.start_worker)
        if (
            self.initial is None
            or not isinstance(value, WorkerReceipt)
            or not value.identity_verified
            or not value.running
            or value.generation == self.initial.generation
            or value.worker_alias != self.initial.worker_alias
        ):
            raise _Failure("owned_restart_unverified")
        self.restarted = value

    async def wait_claim(self) -> None:
        until = self.now() + self.limits.claim_timeout_seconds
        while self.now() < until:
            value = await self.invoke(self.callbacks.read_claim)
            if value is not None:
                if (
                    not isinstance(value, ClaimWitness)
                    or not all(
                        (value.processing, value.started_event_verified, value.owned_scope_verified)
                    )
                    or not _fresh(value.observed_at)
                ):
                    raise _Failure("claim_evidence_invalid")
                # Check again at action time: a stale already expired claim is not a crash witness.
                if value.lease_expires_at <= _utc_now():
                    raise _Failure("claim_already_expired")
                self.claim = value
                self.claim_deadline_monotonic = (
                    self.now() + (value.lease_expires_at - value.observed_at).total_seconds()
                )
                return
            await asyncio.sleep(min(self.limits.poll_interval_seconds, max(0, until - self.now())))
        raise _Failure("claim_observation_timeout")

    async def wait_lease(self) -> None:
        if self.claim is None or self.claim_deadline_monotonic is None:
            raise _Failure("claim_evidence_missing")
        # UTC evidence locates the production lease; a monotonic wait also prevents
        # a forward wall-clock correction from shortening the remaining real wait.
        until = self.claim_deadline_monotonic
        while self.now() < until or _utc_now() < self.claim.lease_expires_at:
            delay = max(
                until - self.now(), (self.claim.lease_expires_at - _utc_now()).total_seconds()
            )
            await asyncio.sleep(min(self.limits.poll_interval_seconds, max(0.001, delay)))

    async def offline_documents(self) -> None:
        if await self.invoke(self.callbacks.verify_offline_documents) is not True:
            raise _Failure("offline_documents_unverified")
        value = await self.read_health()
        if (
            value.status != "offline"
            or any((value.worker_health_verified, value.heartbeat_verified, value.runtime_ready))
            or value.waiting < self.planned
            or value.running != 0
        ):
            raise _Failure("offline_queue_unverified")

    async def after_submissions(self) -> None:
        if self.scenario == "offline_then_resume":
            await self.stage("offline_documents", self.offline_documents)
        else:
            await self.stage("wait_claim", self.wait_claim)
            await self.stage("stop_worker", self.stop_worker)
            await self.stage("wait_offline", lambda: self.wait_health(offline=True))
            await self.stage("wait_lease_expiry", self.wait_lease)
        await self.stage("restart_worker", self.restart_worker)
        await self.stage("wait_ready", self.wait_health)

    async def documents(self) -> None:
        batch = await self.invoke(
            lambda: self.callbacks.run_documents(self.after_submissions), whole_batch=True
        )
        if not isinstance(batch, DocumentBatchResult):
            raise _Failure("document_batch_evidence_invalid")
        self.batch = batch
        # The document runner deliberately converts hook errors into its own ledger.
        failed_hook = next(
            (
                stage.error_code
                for stage in self.stages
                if stage.outcome in {"failure", "timeout", "cancelled"}
            ),
            None,
        )
        if failed_hook:
            raise _Failure(failed_hook)
        if (
            self.restarted is None
            or len(self.batch.documents) != self.planned
            or not self.batch.completed
            or not self.batch.cleanup_passed
            or any(
                doc.outcome != "indexed" or doc.accepted is not True for doc in self.batch.documents
            )
        ):
            raise _Failure("document_batch_incomplete")

    async def verify_recovery(self) -> None:
        value = await self.invoke(lambda: self.callbacks.verify_recovery(self.claim))
        if not isinstance(value, RecoveryWitness):
            raise _Failure("recovery_evidence_invalid")
        self.recovery = value
        if (
            value.owned_documents != self.planned
            or value.indexed != self.planned
            or value.terminal_failed
            or value.unfinished
            or not value.scope_verified
            or not value.unique_publications_verified
        ):
            raise _Failure("recovery_evidence_incomplete")
        if self.claim is not None and (
            value.recovered_job_id_sha256 != self.claim.job_id_sha256
            or value.recovered_attempt is None
            or value.recovered_attempt <= self.claim.attempt
            or not value.old_claim_inactive
        ):
            raise _Failure("claim_recovery_unverified")

    async def execute(self) -> None:
        await self.stage("initial_worker", self.initial_worker)
        await self.stage("initial_ready", self.wait_health)
        if self.scenario == "offline_then_resume":
            await self.stage("stop_worker", self.stop_worker)
            await self.stage("wait_offline", lambda: self.wait_health(offline=True))
        await self.stage("document_batch", self.documents)
        await self.stage("drain_queue", lambda: self.wait_health(drain=True))
        await self.stage("verify_recovery", self.verify_recovery)


async def run_backlog(
    *,
    scenario: Scenario,
    planned_documents: int,
    callbacks: BacklogCallbacks,
    limits: BacklogLimits,
    cancel_event: asyncio.Event | None = None,
) -> BacklogResult:
    """Only the two explicit faults are supported. The owner always cleans processes."""
    if (
        scenario not in {"offline_then_resume", "claim_loss_then_recover"}
        or type(planned_documents) is not int
        or not 1 <= planned_documents <= 32
    ):
        raise ValueError("invalid backlog plan")
    if scenario == "claim_loss_then_recover" and limits.maximum_total_seconds < 600:
        raise ValueError("claim recovery requires at least 600 seconds")
    run = _Run(scenario, planned_documents, callbacks, limits)
    tasks: list[asyncio.Task[None] | asyncio.Task[bool]] = []
    cleanup_passed = False

    async def execute_recorded() -> None:
        try:
            await run.execute()
        except _Failure as exc:
            if run.error is None:
                run.error = exc.code
        except asyncio.CancelledError:
            if run.error is None:
                run.error = "backlog_cancelled"
            raise
        except Exception:
            if run.error is None:
                run.error = "backlog_runner_failed"

    with _quiet_transport() as unsettled:
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise _Failure("backlog_cancelled")
            execution = asyncio.create_task(execute_recorded())
            tasks.append(execution)
            cancellation = (
                asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
            )
            if cancellation is not None:
                tasks.append(cancellation)
            done, _ = await asyncio.wait(
                tasks, timeout=limits.maximum_total_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if execution in done:
                execution.result()
                if run.now() >= limits.maximum_total_seconds:
                    raise _Failure("backlog_deadline_exceeded")
            else:
                raise _Failure(
                    "backlog_cancelled" if cancellation in done else "backlog_deadline_exceeded"
                )
        except asyncio.CancelledError:
            run.error = "backlog_cancelled"
        except _Failure as exc:
            run.error = exc.code
        except Exception:
            run.error = "backlog_runner_failed"
        finally:
            elapsed = run.now()
            cleanup = asyncio.create_task(
                _settle(tasks, deadline=time.perf_counter() + 1.0, cancel=True, unsettled=unsettled)
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    run.error = "backlog_cancelled"
            cleanup_passed = cleanup.result()
    if not cleanup_passed and run.error is None:
        run.error = "task_cleanup_failed"
    for stage in run.stages:
        if stage.outcome == "not_run":
            stage.error_code = run.error or "not_started"
    return BacklogResult(
        scenario,
        planned_documents,
        "success"
        if run.error is None
        else ("cancelled" if run.error == "backlog_cancelled" else "failure"),
        run.stages,
        elapsed,
        run.error,
        run.calls,
        run.initial,
        run.restarted,
        run.claim,
        run.recovery,
        tuple(run.health),
        run.batch,
        cleanup_passed,
    )
