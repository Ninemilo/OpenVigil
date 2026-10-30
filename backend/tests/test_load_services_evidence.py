"""Actual SQLite/production-service SQL witnesses with declared parser fixtures.

The success retry uses the production failure/retry path, with no clock or lease
shortening. Corrupt rows below are explicitly negative fixtures. Natural 300-second
lease expiry, worker death, Redis/MinIO/Neo4j and OCR execution are NOT exercised.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm.attributes import flag_modified

from test_document_parse_jobs import protocol_result
from test_load_document_evidence import Runtime, indexed_document
from test_load_document_evidence import runtime as runtime  # noqa: F401
from windops_backend.document_parse_process import DocumentParseFailure
from windops_backend.model_base import utcnow
from windops_backend.models import DomainEvent, KnowledgeDocument
from windops_backend.operations import load_services_evidence as module
from windops_backend.operations.load_dataset import database_snapshot
from windops_backend.operations.load_services_evidence import (
    EvidenceObservationError,
    OwnedEvidenceObservers,
)
from windops_backend.services import document_parse
from windops_backend.services.document_parse import PARSE_STATE_KEY
from windops_backend.services.events import append_domain_event
from windops_backend.storage import OutboxEvent


@asynccontextmanager
async def paused_parse(
    runtime: Runtime,
    event_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = True,
) -> AsyncIterator[tuple[asyncio.Task[bool], asyncio.Event]]:
    entered, release = asyncio.Event(), asyncio.Event()

    async def parser(content: bytes, **_kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        if fail:
            raise DocumentParseFailure("declared parser retry fixture")
        return protocol_result(content)

    monkeypatch.setattr(document_parse, "parse_document_subprocess", parser)
    task = asyncio.create_task(runtime.parse(event_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        yield task, release
    finally:
        # Teardown only: tests explicitly inspect expected results where relevant.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def successful_retry(
    runtime: Runtime, event_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def parser(content: bytes, **_kwargs: Any) -> Any:
        return protocol_result(content)

    monkeypatch.setattr(document_parse, "parse_document_subprocess", parser)
    assert await runtime.parse(event_id)


async def test_offline_requires_real_every_owned_registration_and_observes_no_claim(
    runtime: Runtime,
) -> None:
    first, _ = await runtime.register("doc-a")
    second, _ = await runtime.register("doc-b")
    observer = OwnedEvidenceObservers(runtime.factory, [first, second])
    before = database_snapshot(runtime.database)
    assert await observer.verify_offline_documents() is True
    assert await observer.read_claim() is None
    recovery = await observer.verify_recovery(None)
    assert recovery.owned_documents == recovery.unfinished == 2
    assert recovery.indexed == 0
    assert recovery.unique_publications_verified is False
    assert database_snapshot(runtime.database) == before


async def test_capture_real_active_claim_then_retry_and_complete_exact_publications(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, event_id = await runtime.register()
    observer = OwnedEvidenceObservers(runtime.factory, [spec])
    async with paused_parse(runtime, event_id, monkeypatch) as (task, release):
        before = database_snapshot(runtime.database)
        claim = await observer.read_claim()
        assert claim is not None
        assert claim.document_alias == spec.document_alias
        assert claim.job_id_sha256 == hashlib.sha256(event_id.encode()).hexdigest()
        assert claim.attempt == 1
        assert claim.processing and claim.started_event_verified and claim.owned_scope_verified
        assert (claim.lease_expires_at - claim.claimed_at).total_seconds() == 300
        assert claim.claimed_at <= claim.observed_at < claim.lease_expires_at
        assert await observer.read_claim() == claim
        assert await observer.verify_offline_documents() is False
        async with runtime.factory() as session:
            original_token = (await session.get(OutboxEvent, event_id)).claim_token
        assert original_token
        assert database_snapshot(runtime.database) == before
        release.set()
        with pytest.raises(DocumentParseFailure):
            await task
    # This is actual production retry, not a simulated natural lease expiry.
    await successful_retry(runtime, event_id, monkeypatch)
    assert await runtime.index(spec)
    before = database_snapshot(runtime.database)
    recovered = await observer.verify_recovery(claim)
    assert recovered.model_dump() == {
        "owned_documents": 1,
        "indexed": 1,
        "terminal_failed": 0,
        "unfinished": 0,
        "scope_verified": True,
        "unique_publications_verified": True,
        "recovered_job_id_sha256": claim.job_id_sha256,
        "recovered_attempt": 2,
        "old_claim_inactive": True,
    }
    report = await observer.collect()
    assert report["database_evidence_verified"] is True
    assert report["documents"][0]["outbox"]["parse_attempts"] == 2
    assert "finish_attempt_unknown" in report["documents"][0]["timing"]["warnings"]
    assert set(report["external_verification"].values()) == {"not_verified"}
    public = json.dumps(
        {
            "claim": claim.model_dump(mode="json"),
            "recovery": recovered.model_dump(mode="json"),
            "collect": report,
        }
    )
    for private in (
        original_token,
        event_id,
        spec.document_id,
        spec.tenant_id,
        "PRIVATE_TITLE",
        "minio://",
        "claim_token",
    ):
        assert private not in public
    assert original_token not in repr(observer) and original_token not in repr(observer._captured)
    assert database_snapshot(runtime.database) == before


async def test_same_attempt_completion_does_not_claim_recovery(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, event_id = await runtime.register()
    observer = OwnedEvidenceObservers(runtime.factory, [spec])
    async with paused_parse(runtime, event_id, monkeypatch, fail=False) as (task, release):
        claim = await observer.read_claim()
        assert claim is not None
        release.set()
        assert await task
    assert await runtime.index(spec)
    recovered = await observer.verify_recovery(claim)
    assert recovered.indexed == 1 and recovered.unique_publications_verified
    assert recovered.recovered_attempt == 1
    assert recovered.old_claim_inactive is False


async def test_offline_resume_complete_and_all_callbacks_are_readonly_owned_sql(
    runtime: Runtime,
) -> None:
    spec, event_id = await runtime.register()
    foreign, _ = await runtime.register("doc-b", tenant="private-tenant-b")
    observer = OwnedEvidenceObservers(runtime.factory, [spec])
    assert await observer.verify_offline_documents()
    assert await runtime.parse(event_id)
    assert await runtime.index(spec)
    before = database_snapshot(runtime.database)
    statements = []

    def observe(
        _connection: Any, _cursor: Any, statement: str, parameters: Any, *_args: Any
    ) -> None:
        statements.append((statement, parameters))

    engine = runtime.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert await observer.read_claim() is None
        assert await observer.verify_offline_documents() is False
        recovered = await observer.verify_recovery(None)
        assert recovered.indexed == recovered.owned_documents == 1
        assert recovered.unique_publications_verified and recovered.scope_verified
        assert recovered.recovered_job_id_sha256 is None
        assert recovered.recovered_attempt is None
        assert recovered.old_claim_inactive is False
        assert (await observer.collect())["database_evidence_verified"] is True
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "BEGIN")) for statement, _ in statements
    )
    assert foreign.document_id not in repr(statements)
    assert database_snapshot(runtime.database) == before


@pytest.mark.parametrize("fault", ["missing", "scope", "duplicate", "payload", "source"])
async def test_missing_scope_duplicate_and_identity_fail_closed(
    runtime: Runtime, fault: str
) -> None:
    spec, event_id = await runtime.register()
    async with runtime.factory() as session, session.begin():
        document = await session.get(KnowledgeDocument, spec.document_id)
        job = await session.get(OutboxEvent, event_id)
        if fault == "missing":
            await session.delete(document)
        elif fault == "scope":
            document.tenant_id = "private-tenant-b"
        elif fault == "duplicate":
            session.add(
                OutboxEvent(
                    event_type=job.event_type,
                    aggregate_type=job.aggregate_type,
                    aggregate_id=job.aggregate_id,
                    payload=copy.deepcopy(job.payload),
                )
            )
        elif fault == "payload":
            job.payload = {**job.payload, "document_id": "private-other-document"}
        else:
            document.artifact_sha256 = "b" * 64
    observer = OwnedEvidenceObservers(runtime.factory, [spec])
    for callback in (
        observer.read_claim,
        observer.verify_offline_documents,
        lambda: observer.verify_recovery(None),
    ):
        with pytest.raises(EvidenceObservationError) as error:
            await callback()
        assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "fault",
    [
        "expired",
        "shortened",
        "token",
        "missing-start",
        "wrong-attempt",
        "duplicate-start",
        "huge-attempt",
        "boolean-attempt",
    ],
)
async def test_corrupt_claim_and_started_event_cannot_be_witnesses(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    spec, event_id = await runtime.register()
    observer = OwnedEvidenceObservers(runtime.factory, [spec])
    async with paused_parse(runtime, event_id, monkeypatch):
        # Explicit negative corruption only; no successful path shortens a lease.
        async with runtime.factory() as session, session.begin():
            job = await session.get(OutboxEvent, event_id)
            started = await session.scalar(
                select(DomainEvent).where(
                    DomainEvent.aggregate_id == spec.document_id,
                    DomainEvent.event_type == "knowledge.document.parse.started",
                )
            )
            assert started is not None
            if fault == "expired":
                job.claimed_at = utcnow() - timedelta(seconds=301)
                job.lease_expires_at = job.claimed_at + timedelta(seconds=300)
            elif fault == "shortened":
                job.lease_expires_at = job.claimed_at + timedelta(seconds=10)
            elif fault == "token":
                job.claim_token = None
            elif fault == "missing-start":
                await session.delete(started)
            elif fault == "wrong-attempt":
                started.payload = {**started.payload, "attempt": 2}
            elif fault == "huge-attempt":
                job.attempts = 2_147_483_647
            elif fault == "boolean-attempt":
                document = await session.get(KnowledgeDocument, spec.document_id)
                document.metadata_ = {
                    **document.metadata_,
                    PARSE_STATE_KEY: {**document.metadata_[PARSE_STATE_KEY], "attempts": True},
                }
                # Python compares True == 1; explicitly dirty the JSON column so
                # this negative fixture actually persists the changed JSON type.
                flag_modified(document, "metadata_")
            else:
                append_domain_event(
                    session,
                    event_type=started.event_type,
                    aggregate_type=started.aggregate_type,
                    aggregate_id=started.aggregate_id,
                    payload=copy.deepcopy(started.payload),
                )
        before = database_snapshot(runtime.database)
        with pytest.raises(EvidenceObservationError):
            await observer.read_claim()
        assert database_snapshot(runtime.database) == before


async def test_active_retry_claim_handles_previous_attempt_processed_at(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, event_id = await runtime.register()
    async with paused_parse(runtime, event_id, monkeypatch) as (task, release):
        release.set()
        with pytest.raises(DocumentParseFailure):
            await task
    # Production claim_event deliberately retains the last failure's processed_at.
    async with paused_parse(runtime, event_id, monkeypatch):
        claim = await OwnedEvidenceObservers(runtime.factory, [spec]).read_claim()
        assert claim is not None and claim.attempt == 2
        async with runtime.factory() as session:
            assert (await session.get(OutboxEvent, event_id)).processed_at is not None


async def test_unrelated_or_forged_initial_witness_is_rejected(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, event_id = await runtime.register()
    observer = OwnedEvidenceObservers(runtime.factory, [spec])
    async with paused_parse(runtime, event_id, monkeypatch):
        claim = await observer.read_claim()
        assert claim is not None
        for bad in (
            None,
            claim.model_copy(update={"attempt": 2}),
            claim.model_copy(update={"job_id_sha256": "c" * 64}),
        ):
            with pytest.raises(EvidenceObservationError, match="initial_claim_evidence"):
                await observer.verify_recovery(bad)
        other = OwnedEvidenceObservers(runtime.factory, [spec])
        with pytest.raises(EvidenceObservationError, match="initial_claim_evidence"):
            await other.verify_recovery(claim)


@pytest.mark.parametrize(
    "fault", ["parsed-duplicate", "indexed-duplicate", "result", "passage", "terminal-token"]
)
async def test_recovery_rejects_duplicate_or_tampered_success_evidence(
    runtime: Runtime, fault: str
) -> None:
    spec, event_id = await indexed_document(runtime)
    async with runtime.factory() as session, session.begin():
        if fault.endswith("duplicate"):
            event_type = (
                "knowledge.document.parsed"
                if fault.startswith("parsed")
                else "knowledge.document.indexed"
            )
            original = await session.scalar(
                select(DomainEvent).where(
                    DomainEvent.aggregate_id == spec.document_id,
                    DomainEvent.event_type == event_type,
                )
            )
            append_domain_event(
                session,
                event_type=event_type,
                aggregate_type="knowledge_document",
                aggregate_id=spec.document_id,
                payload=copy.deepcopy(original.payload),
            )
        elif fault == "result":
            document = await session.get(KnowledgeDocument, spec.document_id)
            document.metadata_ = {
                **document.metadata_,
                PARSE_STATE_KEY: {**document.metadata_[PARSE_STATE_KEY], "result_sha256": "d" * 64},
            }
        elif fault == "passage":
            document = await session.get(KnowledgeDocument, spec.document_id)
            document.body = "Tampered parsed body"
        else:
            job = await session.get(OutboxEvent, event_id)
            job.claim_token = "private-terminal-token"
    observer = OwnedEvidenceObservers(runtime.factory, [spec])
    if fault == "terminal-token":
        recovered = await observer.verify_recovery(None)
        assert recovered.indexed == 1 and recovered.unique_publications_verified is False
    else:
        with pytest.raises(EvidenceObservationError):
            await observer.verify_recovery(None)


async def test_terminal_failure_and_unfinished_are_explicit_not_success(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed, event_id = await runtime.register("failed")
    pending, _ = await runtime.register("pending")

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise DocumentParseFailure("declared fixture")

    monkeypatch.setattr(document_parse, "parse_document_subprocess", broken)
    for _ in range(2):
        with pytest.raises(DocumentParseFailure):
            await runtime.parse(event_id)
    assert await runtime.parse(event_id)
    witness = await OwnedEvidenceObservers(runtime.factory, [failed, pending]).verify_recovery(None)
    assert witness.owned_documents == 2 and witness.indexed == 0
    assert witness.terminal_failed == witness.unfinished == 1
    assert witness.unique_publications_verified is False


async def test_manifest_boundaries_and_sql_payload_budget(runtime: Runtime) -> None:
    spec, event_id = await runtime.register()
    for manifest in (
        [],
        [spec, spec],
        [spec] * 33,
        [spec.model_copy(update={"document_alias": "PRIVATE/INVALID"})],
    ):
        with pytest.raises(EvidenceObservationError, match="invalid_owned_document_manifest"):
            OwnedEvidenceObservers(runtime.factory, manifest)
    async with runtime.factory() as session, session.begin():
        job = await session.get(OutboxEvent, event_id)
        job.payload = ["private-invalid-json-shape"]
    with pytest.raises(EvidenceObservationError, match="payload_invalid"):
        await OwnedEvidenceObservers(runtime.factory, [spec]).read_claim()


async def test_query_timeout_and_cancellation_close_read_session(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _ = await runtime.register()
    observer = OwnedEvidenceObservers(runtime.factory, [spec])
    entered = asyncio.Event()

    async def blocked(*_args: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "_bounded_rows", blocked)
    monkeypatch.setattr(module, "COLLECTION_TIMEOUT_SECONDS", 0.03)
    before = database_snapshot(runtime.database)
    with pytest.raises(EvidenceObservationError, match="sql_evidence_timeout"):
        await observer.read_claim()
    assert runtime.factory.kw["bind"].pool.checkedout() == 0
    monkeypatch.setattr(module, "COLLECTION_TIMEOUT_SECONDS", 15)
    entered.clear()
    task = asyncio.create_task(observer.verify_offline_documents())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert runtime.factory.kw["bind"].pool.checkedout() == 0
    assert database_snapshot(runtime.database) == before
