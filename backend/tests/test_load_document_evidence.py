"""Real SQLite services/transactions with declared object and parser wire fixtures.

No object server, OCR subprocess, Redis, Neo4j or paid embedding is executed.
Production registration, fenced parse publication/failure, DomainEvent writes,
index transactions and deterministic test embeddings are exercised directly.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_document_parse_jobs import protocol_result
from test_knowledge_passages import pdf_with_second_page_text
from windops_backend.agents.embeddings import DeterministicTestEmbeddingProvider
from windops_backend.config import Settings
from windops_backend.document_parse_process import DocumentParseFailure
from windops_backend.model_base import Base, utcnow
from windops_backend.model_knowledge_passage import KnowledgePassage
from windops_backend.models import DomainEvent, KnowledgeDocument, Tenant
from windops_backend.operations.load_dataset import database_snapshot
from windops_backend.operations.load_document_evidence import (
    MAX_OUTBOX_ERROR_CHARACTERS,
    OwnedDocumentEvidenceSpec,
    collect_document_evidence,
)
from windops_backend.schemas import KnowledgeDocumentCreateRequest
from windops_backend.services import document_parse
from windops_backend.services.document_parse import (
    PARSE_STATE_KEY,
    configured_parse_identity,
    process_document_parse_event,
)
from windops_backend.services.events import append_domain_event
from windops_backend.services.knowledge import (
    create_knowledge_document,
    process_knowledge_document_index_event,
)
from windops_backend.services.knowledge_contracts import KNOWLEDGE_DOCUMENT_INDEX_REQUESTED
from windops_backend.storage import InMemoryArtifactVerifier, OutboxEvent


@dataclass
class Runtime:
    factory: async_sessionmaker
    settings: Settings
    verifier: InMemoryArtifactVerifier
    database: Path

    async def register(self, alias: str = "doc-a", *, tenant: str = "private-tenant-a"):
        document_id = f"private-document-{alias}"
        content = pdf_with_second_page_text()
        uri = f"minio://{self.settings.minio_knowledge_bucket}/documents/{document_id}/source.pdf"
        sha = self.verifier.register_object(uri, content, "application/pdf")
        identity = await configured_parse_identity(
            self.settings, source_sha256=sha, document_version="1"
        )
        async with self.factory() as session, session.begin():
            _document, replay, event_id = await create_knowledge_document(
                session,
                KnowledgeDocumentCreateRequest(
                    document_id=document_id,
                    title="PRIVATE_TITLE_MUST_NOT_LEAK",
                    document_type="manual",
                    artifact_uri=uri,
                    artifact_sha256=sha,
                    content_type="application/pdf",
                    parser="docling",
                    tenant_id=tenant,
                ),
                extracted_text="",
                content_size_bytes=len(content),
                subject="PRIVATE_SUBJECT_MUST_NOT_LEAK",
                parse_identity=identity,
            )
            assert replay is False and event_id is not None
        return OwnedDocumentEvidenceSpec(
            document_alias=alias,
            document_id=document_id,
            artifact_sha256=sha,
            document_version="1",
            tenant_id=tenant,
            embedding_provider="deterministic_test",
            embedding_model="sha256-token-projection-v1",
            expected_passages=1,
        ), event_id

    async def parse(self, event_id: str):
        return await process_document_parse_event(
            self.factory, self.settings, self.verifier, event_id
        )

    async def index(self, spec: OwnedDocumentEvidenceSpec, provider=None):
        async with self.factory() as session:
            event_id = await session.scalar(
                select(OutboxEvent.id).where(
                    OutboxEvent.aggregate_id == spec.document_id,
                    OutboxEvent.event_type == KNOWLEDGE_DOCUMENT_INDEX_REQUESTED,
                )
            )
        assert event_id is not None
        return await process_knowledge_document_index_event(
            self.factory, self.settings, event_id, provider
        )


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    database = tmp_path / "document-evidence.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        session.add_all(
            [Tenant(id=f"private-tenant-{scope}", name=f"Synthetic {scope}") for scope in "ab"]
        )
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        schema_bootstrap=False,
        demo_seed=False,
        agent_mode="deterministic",
        knowledge_graph_backend="memory",
        outbox_inline_drain=False,
    )
    settings.document_parser_enabled = True
    settings.document_parser_models_path = tmp_path / "declared-no-model-fixture"
    settings.document_parser_model_manifest_sha256 = "a" * 64
    settings.document_parser_python = "declared-no-subprocess-fixture"

    async def fixture_parser(content, **_kwargs):
        return protocol_result(content)

    monkeypatch.setattr(document_parse, "parse_document_subprocess", fixture_parser)
    try:
        yield Runtime(factory, settings, InMemoryArtifactVerifier(), database)
    finally:
        await engine.dispose()


async def indexed_document(runtime, alias="doc-a"):
    spec, event_id = await runtime.register(alias)
    assert await runtime.parse(event_id)
    assert await runtime.index(spec)
    return spec, event_id


async def test_real_success_readonly_exact_owned_ids_and_public_privacy(runtime):
    spec, _event_id = await indexed_document(runtime)
    foreign, _ = await runtime.register("doc-private-b", tenant="private-tenant-b")
    before = database_snapshot(runtime.database)
    statements = []
    engine = runtime.factory.kw["bind"].sync_engine

    def observe(_connection, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", observe)
    try:
        report = await collect_document_evidence(runtime.factory, [spec])
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert database_snapshot(runtime.database) == before
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "BEGIN")) for statement, _ in statements
    )
    assert foreign.document_id not in repr(statements)
    assert report["counts"] == {
        "planned": 1,
        "accepted": 1,
        "indexed": 1,
        "terminal_failed": 0,
        "unfinished": 0,
        "submission_unknown": 0,
        "submission_unknown_unresolved": 0,
        "not_accepted": 0,
    }
    assert report["database_evidence_verified"] is True
    row = report["documents"][0]
    assert row["errors"] == []
    assert row["evidence_status"] == "verified"
    assert row["outbox"]["parse_attempts"] == row["outbox"]["index_attempts"] == 1
    assert row["embedding"]["document_vector"]["dimensions"] == 1536
    assert len(row["embedding"]["document_vector"]["sha256"]) == 64
    assert row["timing"]["parse_end_to_end_ms"] >= 0
    assert row["timing"]["index_end_to_end_ms"] >= row["timing"]["parse_end_to_end_ms"]
    assert row["timing"]["attempts"][0]["service_interval_ms"] is None
    assert row["timing"]["attempts"][0]["status"] == "finish_identity_unknown"
    assert "finish_attempt_unknown" in row["timing"]["warnings"]
    assert row["timing"]["attempt_history_complete"] is False
    public = json.dumps(report)
    for private in (
        spec.document_id,
        spec.tenant_id,
        "PRIVATE_TITLE",
        "PRIVATE_SUBJECT",
        "Declared wire fixture",
        "minio://",
        "claim_token",
        foreign.document_id,
    ):
        assert private not in public
    assert set(report["external_verification"].values()) == {"not_verified"}


async def test_real_terminal_failures_pending_and_unknown_submission_balance(runtime, monkeypatch):
    failed, failed_event = await runtime.register("doc-failed")

    async def failing_parser(_content, **_kwargs):
        raise RuntimeError("PRIVATE_PARSER_ERROR_MUST_NOT_LEAK")

    monkeypatch.setattr(document_parse, "parse_document_subprocess", failing_parser)
    for attempt in range(3):
        if attempt < 2:
            with pytest.raises(DocumentParseFailure):
                await runtime.parse(failed_event)
        else:
            assert await runtime.parse(failed_event)
    pending, _ = await runtime.register("doc-pending")
    found_unknown, _ = await runtime.register("doc-found-unknown")
    found_unknown = found_unknown.model_copy(update={"submission": "submission_unknown"})
    missing = pending.model_copy(
        update={
            "document_alias": "doc-missing",
            "document_id": "private-missing",
            "submission": "submission_unknown",
        }
    )
    rejected = pending.model_copy(
        update={
            "document_alias": "doc-rejected",
            "document_id": "private-rejected",
            "submission": "not_accepted",
        }
    )
    report = await collect_document_evidence(
        runtime.factory, [failed, pending, found_unknown, missing, rejected]
    )
    assert report["counts"] == {
        "planned": 5,
        "accepted": 3,
        "indexed": 0,
        "terminal_failed": 1,
        "unfinished": 2,
        "submission_unknown": 2,
        "submission_unknown_unresolved": 1,
        "not_accepted": 1,
    }
    assert report["conservation_verified"] is True
    assert report["database_evidence_verified"] is False
    row = report["documents"][0]
    assert row["evidence_status"] == "verified"
    assert row["passage_count"] == 0
    assert row["outbox"]["index_count"] == 0
    assert [item["attempt"] for item in row["timing"]["attempts"]] == [1, 2, 3]
    assert all(
        item["status"] == "paired" and item["service_interval_ms"] >= 0
        for item in row["timing"]["attempts"]
    )
    assert len(row["timing"]["retry_waits"]) == 2
    assert "PRIVATE_PARSER_ERROR" not in json.dumps(report)


async def test_real_retry_events_do_not_use_overwritten_claim_or_dispatch_for_history(
    runtime, monkeypatch
):
    spec, event_id = await runtime.register()
    calls = 0

    async def flaky_parser(content, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("declared parser wire failure")
        return protocol_result(content)

    monkeypatch.setattr(document_parse, "parse_document_subprocess", flaky_parser)
    for _ in range(2):
        with pytest.raises(DocumentParseFailure):
            await runtime.parse(event_id)
    assert await runtime.parse(event_id)
    assert await runtime.index(spec)
    first = await collect_document_evidence(runtime.factory, [spec])
    async with runtime.factory() as session, session.begin():
        job = await session.get(OutboxEvent, event_id)
        job.claimed_at = utcnow() + timedelta(days=1)
        job.dispatched_at = utcnow() + timedelta(days=2)
    second = await collect_document_evidence(runtime.factory, [spec])
    assert first["documents"][0]["timing"] == second["documents"][0]["timing"]
    attempts = second["documents"][0]["timing"]["attempts"]
    assert [item["status"] for item in attempts] == ["paired", "paired", "finish_identity_unknown"]
    assert second["documents"][0]["outbox"]["parse_attempts"] == 3


async def test_cross_scope_same_bytes_cannot_produce_verified_receipt(runtime):
    spec, _ = await indexed_document(runtime)
    wrong = spec.model_copy(update={"tenant_id": "private-tenant-b"})
    report = await collect_document_evidence(runtime.factory, [wrong])
    assert report["counts"]["indexed"] == 0
    assert report["counts"]["unfinished"] == 1
    assert report["documents"][0]["errors"] == ["scope_mismatch"]
    assert "result_sha256" not in report["documents"][0]


@pytest.mark.parametrize(
    "corruption,expected_error",
    [
        ("body", "persisted_parse_result_invalid"),
        ("source_flag", "passage_source_identity_mismatch"),
        ("private_source_hash", "passage_source_identity_mismatch"),
        ("private_text_hash", "persisted_parse_result_invalid"),
        ("passage_version", "passage_source_identity_mismatch"),
        ("parsed_source_same_result_hash", "parsed_event_identity_mismatch"),
        ("vector_dimensions", "vector_evidence_invalid"),
        ("embedding_model", "embedding_identity_mismatch"),
        ("vector_aggregate", "document_vector_aggregate_mismatch"),
    ],
)
async def test_real_persisted_tampering_cannot_pass_with_unchanged_result_hash(
    runtime, corruption, expected_error
):
    spec, _ = await indexed_document(runtime)
    before = await collect_document_evidence(runtime.factory, [spec])
    async with runtime.factory() as session, session.begin():
        document = await session.get(KnowledgeDocument, spec.document_id)
        piece = await session.scalar(
            select(KnowledgePassage).where(KnowledgePassage.document_id == spec.document_id)
        )
        old_hash = document.metadata_[PARSE_STATE_KEY]["result_sha256"]
        if corruption == "body":
            document.body += " PRIVATE_EXTRA_TEXT"
        elif corruption == "source_flag":
            piece.native_locator = {**piece.native_locator, "source_verified": False}
        elif corruption == "private_source_hash":
            piece.source_sha256 = "PRIVATE_SOURCE_MARKER"
        elif corruption == "private_text_hash":
            piece.text_sha256 = "PRIVATE_HASH_MARKER"
        elif corruption == "passage_version":
            piece.document_version = "wrong-version"
        elif corruption == "parsed_source_same_result_hash":
            parsed = await session.scalar(
                select(DomainEvent).where(
                    DomainEvent.aggregate_id == spec.document_id,
                    DomainEvent.event_type == "knowledge.document.parsed",
                )
            )
            parsed.payload = {**parsed.payload, "artifact_sha256": "b" * 64}
        elif corruption == "vector_dimensions":
            piece.embedding = b"short"
        elif corruption == "embedding_model":
            piece.embedding_model = "PRIVATE_WRONG_MODEL"
        elif corruption == "vector_aggregate":
            document.embedding = b"\x00" * (1536 * 4)
        assert document.metadata_[PARSE_STATE_KEY]["result_sha256"] == old_hash
    after = await collect_document_evidence(runtime.factory, [spec])
    assert before["database_evidence_verified"] is True
    assert after["database_evidence_verified"] is False
    assert after["counts"]["indexed"] == 0
    assert expected_error in after["documents"][0]["errors"]
    assert "PRIVATE_" not in json.dumps(after)


async def test_missing_attempt_finish_and_clock_inversion_are_unknown_not_zero(
    runtime, monkeypatch
):
    spec, event_id = await runtime.register()

    async def failure(_content, **_kwargs):
        raise RuntimeError("declared failure")

    monkeypatch.setattr(document_parse, "parse_document_subprocess", failure)
    with pytest.raises(DocumentParseFailure):
        await runtime.parse(event_id)
    async with runtime.factory() as session, session.begin():
        failed = await session.scalar(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == spec.document_id,
                DomainEvent.event_type == "knowledge.document.parse.failed",
            )
        )
        failed.payload = {
            "document_id": spec.document_id,
            "error_code": "DocumentParseAttemptsExhausted",
        }
        failed.occurred_at -= timedelta(days=1)
    report = await collect_document_evidence(runtime.factory, [spec])
    timing = report["documents"][0]["timing"]
    assert {"finish_attempt_unknown", "failure_terminality_unknown", "clock_inconsistent"} <= set(
        timing["warnings"]
    )
    assert timing["attempts"][0]["service_interval_ms"] is None
    assert timing["attempts"][0]["status"] == "interrupted_no_terminal_event"


async def test_real_index_failure_is_unfinished_without_proven_retry_exhaustion(runtime):
    spec, event_id = await runtime.register()
    assert await runtime.parse(event_id)

    class FailingEmbedding(DeterministicTestEmbeddingProvider):
        async def embed(self, _texts):
            raise RuntimeError("PRIVATE_EMBEDDING_ERROR")

    with pytest.raises(RuntimeError, match="PRIVATE_EMBEDDING_ERROR"):
        await runtime.index(spec, FailingEmbedding())
    report = await collect_document_evidence(runtime.factory, [spec])
    assert report["counts"]["unfinished"] == 1
    assert report["counts"]["terminal_failed"] == 0
    assert report["documents"][0]["outbox"]["index_status"] == "failed"
    assert report["documents"][0]["index_failure"] == "observed_current_failure_not_proven_terminal"
    assert "PRIVATE_EMBEDDING_ERROR" not in json.dumps(report)


async def test_duplicate_jobs_and_event_limits_are_fail_closed(runtime):
    spec, event_id = await runtime.register()
    async with runtime.factory() as session, session.begin():
        original = await session.get(OutboxEvent, event_id)
        session.add(
            OutboxEvent(
                event_type=original.event_type,
                aggregate_type="knowledge_document",
                aggregate_id=spec.document_id,
                payload=copy.deepcopy(original.payload),
            )
        )
    duplicate = await collect_document_evidence(runtime.factory, [spec])
    assert "parse_job_identity_invalid" in duplicate["documents"][0]["errors"]
    async with runtime.factory() as session, session.begin():
        for _ in range(65):
            append_domain_event(
                session,
                event_type="knowledge.document.parse.started",
                aggregate_type="knowledge_document",
                aggregate_id=spec.document_id,
                payload={"document_id": spec.document_id, "attempt": 1, "event_id": event_id},
            )
    bounded = await collect_document_evidence(runtime.factory, [spec])
    assert bounded["documents"][0]["errors"] == ["evidence_limit_exceeded"]
    assert bounded["conservation_verified"] is True
    assert bounded["database_evidence_verified"] is False


async def test_manifest_duplicates_missing_accepted_and_no_evidence_never_green(runtime):
    spec, _ = await runtime.register()
    with pytest.raises(ValueError, match="invalid_owned_document_manifest"):
        await collect_document_evidence(runtime.factory, [spec, spec])
    with pytest.raises(ValueError, match="invalid_owned_document_manifest"):
        await collect_document_evidence(
            runtime.factory,
            [spec.model_copy(update={"document_alias": "PRIVATE_UNVALIDATED_ALIAS"})],
        )
    missing = spec.model_copy(update={"document_id": "private-not-present"})
    report = await collect_document_evidence(runtime.factory, [missing])
    assert report["counts"]["accepted"] == report["counts"]["unfinished"] == 1
    assert report["documents"][0]["errors"] == ["accepted_document_missing"]
    unknown = missing.model_copy(update={"submission": "submission_unknown"})
    report = await collect_document_evidence(runtime.factory, [unknown])
    assert report["counts"]["submission_unknown_unresolved"] == 1
    assert report["database_evidence_verified"] is False


async def test_outbox_error_text_is_bounded_in_sql_before_job_hydration(runtime):
    spec, parse_id = await indexed_document(runtime)
    async with runtime.factory() as session, session.begin():
        parse_job = await session.get(OutboxEvent, parse_id)
        parse_job.last_error = "x" * MAX_OUTBOX_ERROR_CHARACTERS
    boundary = await collect_document_evidence(runtime.factory, [spec])
    assert boundary["database_evidence_verified"] is True
    async with runtime.factory() as session, session.begin():
        index_job = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == spec.document_id,
                OutboxEvent.event_type == KNOWLEDGE_DOCUMENT_INDEX_REQUESTED,
            )
        )
        index_job.last_error = "PRIVATE_ERROR"
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = runtime.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        report = await collect_document_evidence(runtime.factory, [spec])
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert report["documents"][0]["errors"] == ["evidence_limit_exceeded"]
    assert report["database_evidence_verified"] is False
    assert "PRIVATE_ERROR" not in json.dumps(report)
    assert not any(statement.startswith("SELECT outbox_events.id") for statement in statements)
