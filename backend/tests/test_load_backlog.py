"""Control fixtures only: no OCR, Redis TTL or real 300-second lease qualification.

The test model advances this module's observation clocks; production constants,
default budgets, asyncio's actual timeout clock and database contents are unchanged.
The real runtime must separately observe natural TTL and lease expiry.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from windops_backend.document_worker_health import HEARTBEAT_TTL_SECONDS
from windops_backend.operations import load_backlog as module
from windops_backend.operations.load_backlog import (
    BacklogCallbacks,
    BacklogHealth,
    BacklogLimits,
    BacklogResult,
    ClaimWitness,
    RecoveryWitness,
    Scenario,
    WorkerReceipt,
    run_backlog,
)
from windops_backend.operations.load_documents import DocumentBatchResult, DocumentResult


@dataclass
class ModelClock:
    started: float = field(default_factory=time.perf_counter)
    beginning: datetime = field(default_factory=lambda: datetime.now(UTC))

    def seconds(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    def utc(self) -> datetime:
        return self.beginning + timedelta(seconds=self.seconds())


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ModelClock:
    value = ModelClock()
    monkeypatch.setattr(module._Run, "now", lambda _: value.seconds())
    monkeypatch.setattr(module, "_utc_now", value.utc)
    return value


@dataclass
class Controls:
    clock: ModelClock
    fault: str = ""
    actions: list[tuple[str, float]] = field(default_factory=list)
    stopped: bool = False
    restarted: bool = False
    submitted: bool = False
    finished: bool = False
    claim: ClaimWitness | None = None
    holding: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def current(self) -> WorkerReceipt:
        self.actions.append(("initial", self.clock.seconds()))
        return WorkerReceipt(
            worker_alias="document-worker",
            generation="generation-1",
            identity_verified=self.fault != "initial_unverified",
            running=True,
        )

    async def stop(self) -> WorkerReceipt:
        self.actions.append(("stop", self.clock.seconds()))
        self.stopped = True
        return WorkerReceipt(
            worker_alias="document-worker",
            generation="foreign" if self.fault == "stop_generation" else "generation-1",
            identity_verified=True,
            running=False,
            abrupt=self.fault != "graceful_stop",
        )

    async def start(self) -> WorkerReceipt:
        self.actions.append(("start", self.clock.seconds()))
        self.stopped, self.restarted = False, True
        return WorkerReceipt(
            worker_alias="document-worker",
            generation="generation-1" if self.fault == "same_generation" else "generation-2",
            identity_verified=True,
            running=True,
        )

    async def health(self) -> BacklogHealth:
        if self.fault == "private_exception":
            raise RuntimeError("private-source-query-and-token")
        if self.fault == "blocked_health":
            self.holding.set()
            await self.release.wait()
        ready = not self.stopped and self.fault != "never_ready"
        now = self.clock.utc()
        return BacklogHealth(
            status="ready" if ready else ("offline" if self.stopped else "starting"),
            worker_health_verified=ready,
            heartbeat_verified=ready,
            runtime_ready=ready,
            heartbeat_ttl_seconds=1 if self.fault == "short_ttl" else 45,
            observed_at=now - timedelta(seconds=46) if self.fault == "stale_health" else now,
            queue_observed_at=now - timedelta(seconds=46) if self.fault == "stale_queue" else now,
            scope_verified=self.fault != "foreign_scope",
            waiting=2
            if self.submitted and not self.finished
            else (1 if self.fault == "never_drained" else 0),
            running=0,
            retrying=0,
            expired_leases=0,
            backlogged=False,
            backlog_warning_seconds=300,
        )

    async def docs(self, hook: Callable[[], Awaitable[None]]) -> DocumentBatchResult:
        self.actions.append(("submit", self.clock.seconds()))
        self.submitted = True
        complete = True
        try:
            await hook()
        except (Exception, asyncio.CancelledError):
            # Match the document runner's bounded cleanup and preserved partial ledger.
            complete = False
        self.finished = complete
        return DocumentBatchResult(
            run_id="a" * 32,
            documents=tuple(
                DocumentResult(
                    f"owned-{i}",
                    f"u{i}",
                    "docling",
                    "a" * 64,
                    10,
                    (),
                    outcome="indexed" if complete else "failed",
                    accepted=True,
                )
                for i in range(2)
            ),
            http_samples=(),
            elapsed_seconds=1.0,
            completed=complete,
            cleanup_passed=True,
            stop_reason=None if complete else "after_submissions_failed",
            total_upload_bytes=20,
            total_response_bytes=10,
            max_observed_inflight=2,
        )

    async def claim_read(self) -> ClaimWitness | None:
        if self.fault == "missing_claim":
            return None
        if self.fault == "blocked_claim":
            self.holding.set()
            await self.release.wait()
        now = self.clock.utc()
        self.claim = ClaimWitness(
            document_alias="doc-a",
            job_id_sha256="b" * 64,
            attempt=1,
            claimed_at=now,
            lease_expires_at=now + timedelta(seconds=300),
            observed_at=now,
            processing=True,
            started_event_verified=self.fault != "no_started_event",
            owned_scope_verified=True,
        )
        self.actions.append(("claim", self.clock.seconds()))
        return self.claim

    async def offline(self) -> bool:
        self.actions.append(("offline_documents", self.clock.seconds()))
        return self.fault != "offline_sql_invalid" and self.stopped and self.submitted

    async def recovery(self, initial: ClaimWitness | None) -> RecoveryWitness:
        self.actions.append(("reconcile", self.clock.seconds()))
        return RecoveryWitness(
            owned_documents=2,
            indexed=1 if self.fault == "unfinished" else 2,
            terminal_failed=0,
            unfinished=1 if self.fault == "unfinished" else 0,
            scope_verified=True,
            unique_publications_verified=self.fault != "duplicate_publication",
            recovered_job_id_sha256=(
                "c" * 64 if self.fault == "foreign_job" else initial.job_id_sha256
            )
            if initial
            else None,
            recovered_attempt=(1 if self.fault == "attempt_not_increased" else 2)
            if initial
            else None,
            old_claim_inactive=self.fault != "old_claim_active",
        )

    def callbacks(self) -> BacklogCallbacks:
        return BacklogCallbacks(
            self.current,
            self.health,
            self.stop,
            self.start,
            self.docs,
            self.claim_read,
            self.offline,
            self.recovery,
        )


async def execute(
    controls: Controls, scenario: Scenario = "offline_then_resume", **limits: Any
) -> BacklogResult:
    return await run_backlog(
        scenario=scenario,
        planned_documents=2,
        callbacks=controls.callbacks(),
        limits=BacklogLimits(poll_interval_seconds=0.01, **limits),
    )


@pytest.mark.parametrize("scenario", ["offline_then_resume", "claim_loss_then_recover"])
async def test_distinct_control_sequences_require_full_model_ttl_and_lease(
    clock: ModelClock, scenario: Scenario
) -> None:
    controls = Controls(clock)
    result = await execute(controls, scenario)
    assert result.outcome == "success" and result.task_cleanup_passed
    assert not result.external_services_verified
    assert result.document_batch is not None and result.document_batch.completed
    assert all(stage.outcome == "success" for stage in result.stages)
    times = dict(controls.actions)
    assert times["start"] - times["stop"] >= 45
    if scenario == "offline_then_resume":
        assert times["stop"] < times["submit"] < times["start"]
        assert times["submit"] - times["stop"] >= 45
        assert "claim" not in times and result.claim is None
    else:
        assert times["submit"] < times["claim"] < times["stop"] < times["start"]
        assert times["start"] - times["claim"] >= 299.9
        assert "offline_documents" not in times
    assert result.initial_worker is not None and result.restarted_worker is not None
    assert result.initial_worker.generation != result.restarted_worker.generation
    assert json.loads(json.dumps(result.to_dict()))["outcome"] == "success"
    assert HEARTBEAT_TTL_SECONDS == 45 and module.LEASE_SECONDS == 300


@pytest.mark.parametrize(
    "fault,code",
    [
        ("initial_unverified", "owned_worker_unverified"),
        ("stale_health", "health_evidence_stale"),
        ("stale_queue", "health_evidence_stale"),
        ("foreign_scope", "health_evidence_invalid"),
        ("short_ttl", "production_timing_mismatch"),
        ("private_exception", "callback_failed"),
    ],
)
async def test_invalid_initial_evidence_never_stops_or_submits(
    clock: ModelClock, fault: str, code: str
) -> None:
    controls = Controls(clock, fault)
    result = await execute(controls)
    assert result.error_code == code and result.outcome == "failure"
    assert [name for name, _ in controls.actions] == ["initial"]
    assert result.document_batch is None
    assert "private-source-query-and-token" not in repr(asdict(result))


@pytest.mark.parametrize(
    "fault,code",
    [
        ("stop_generation", "owned_stop_unverified"),
        ("graceful_stop", "owned_stop_unverified"),
        ("same_generation", "owned_restart_unverified"),
        ("offline_sql_invalid", "offline_documents_unverified"),
    ],
)
async def test_worker_and_offline_receipts_are_verified(
    clock: ModelClock, fault: str, code: str
) -> None:
    result = await execute(Controls(clock, fault))
    assert result.error_code == code and result.outcome == "failure"
    assert result.task_cleanup_passed
    if fault in {"same_generation", "offline_sql_invalid"}:
        assert result.document_batch is not None and not result.document_batch.completed


@pytest.mark.parametrize(
    "fault,code",
    [
        ("no_started_event", "claim_evidence_invalid"),
        ("missing_claim", "claim_observation_timeout"),
        ("foreign_job", "claim_recovery_unverified"),
        ("attempt_not_increased", "claim_recovery_unverified"),
        ("old_claim_active", "claim_recovery_unverified"),
        ("duplicate_publication", "recovery_evidence_incomplete"),
        ("unfinished", "recovery_evidence_incomplete"),
    ],
)
async def test_claim_recovery_needs_bound_attempt_and_unique_publication(
    clock: ModelClock, fault: str, code: str
) -> None:
    result = await execute(
        Controls(clock, fault), "claim_loss_then_recover", claim_timeout_seconds=40.0
    )
    assert result.error_code == code and result.outcome == "failure"
    assert result.task_cleanup_passed


async def test_starting_health_is_observed_until_bounded_timeout(clock: ModelClock) -> None:
    controls = Controls(clock, "never_ready")
    result = await execute(controls, health_timeout_seconds=40.0)
    assert result.error_code == "worker_health_timeout"
    assert [name for name, _ in controls.actions] == ["initial"]
    assert len(result.health_observations) >= 2


async def test_queue_drain_is_required_after_http_indexed(clock: ModelClock) -> None:
    result = await execute(Controls(clock, "never_drained"), drain_timeout_seconds=40.0)
    assert result.document_batch is not None and result.document_batch.completed
    assert result.error_code == "queue_drain_timeout" and result.outcome == "failure"


@pytest.mark.parametrize(
    "limit,code",
    [
        ("maximum_callbacks", "callback_budget_exhausted"),
        ("maximum_health_observations", "health_observation_budget_exhausted"),
    ],
)
async def test_all_observation_budgets_stop_without_infinite_polling(
    clock: ModelClock, limit: str, code: str
) -> None:
    options: dict[str, Any] = {limit: 1}
    result = await execute(Controls(clock), **options)
    assert result.error_code == code and result.task_cleanup_passed
    assert result.callback_count < 10


async def test_pre_cancel_keeps_every_planned_stage_not_run(clock: ModelClock) -> None:
    event = asyncio.Event()
    event.set()
    controls = Controls(clock)
    result = await run_backlog(
        scenario="offline_then_resume",
        planned_documents=2,
        callbacks=controls.callbacks(),
        limits=BacklogLimits(),
        cancel_event=event,
    )
    assert result.outcome == "cancelled" and result.task_cleanup_passed
    assert result.callback_count == 0 and not controls.actions
    assert all(stage.outcome == "not_run" for stage in result.stages)


async def test_real_async_cancellation_retains_partial_document_ledger(clock: ModelClock) -> None:
    controls = Controls(clock, "blocked_claim")
    task = asyncio.create_task(execute(controls, "claim_loss_then_recover"))
    await asyncio.wait_for(controls.holding.wait(), timeout=2)
    task.cancel()
    result = await asyncio.wait_for(task, timeout=2)
    assert result.outcome == "cancelled" and result.task_cleanup_passed
    assert result.document_batch is not None and not result.document_batch.completed
    assert not controls.restarted
    assert all(
        stage.outcome == "not_run"
        for stage in result.stages
        if stage.stage in {"restart_worker", "wait_ready", "verify_recovery"}
    )


async def test_real_callback_timeout_is_not_turned_into_ready(clock: ModelClock) -> None:
    controls = Controls(clock, "blocked_health")
    result = await execute(controls, callback_timeout_seconds=0.02)
    assert result.error_code == "callback_timeout" and result.task_cleanup_passed
    assert not controls.stopped


def test_claim_contract_rejects_shortened_or_already_expired_lease() -> None:
    now = datetime.now(UTC)
    args: dict[str, Any] = {
        "document_alias": "doc-a",
        "job_id_sha256": "b" * 64,
        "attempt": 1,
        "claimed_at": now,
        "lease_expires_at": now + timedelta(seconds=300),
        "observed_at": now,
        "processing": True,
        "started_event_verified": True,
        "owned_scope_verified": True,
    }
    with pytest.raises(ValidationError):
        ClaimWitness(**{**args, "lease_expires_at": now + timedelta(seconds=1)})
    with pytest.raises(ValidationError):
        ClaimWitness(**{**args, "observed_at": now + timedelta(seconds=301)})
    with pytest.raises(ValidationError):
        ClaimWitness(**{**args, "observed_at": now.replace(tzinfo=None)})


async def test_scenario_minimum_budget_cannot_be_shortened(clock: ModelClock) -> None:
    with pytest.raises(ValidationError):
        BacklogLimits(maximum_total_seconds=45.0)
    with pytest.raises(ValueError, match="600"):
        await execute(Controls(clock), "claim_loss_then_recover", maximum_total_seconds=599.0)
    assert BacklogLimits().maximum_total_seconds == 900.0
