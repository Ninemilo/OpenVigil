"""Bounded, read-only SQL receipts for explicitly owned Docling documents.

This collector observes persisted application facts. It does not verify object
storage, OCR execution, Redis delivery, embedding semantics or Neo4j projection.
The caller owns the fresh database and the private manifest; public output never
contains raw identifiers, text, object URLs, credentials or fencing tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Text, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from windops_backend.agents.embeddings import (
    _unpack_vector,
    aggregate_embedding_vectors,
    pack_embedding_vector,
)
from windops_backend.document_parse_contract import (
    MAX_OUTPUT_BYTES,
    MAX_PASSAGES,
    MAX_TEXT_CHARACTERS,
    PARSE_CONTRACT,
    DocumentParseResult,
)
from windops_backend.document_parse_result_identity import (
    DOCUMENT_RESULT_HASH_CONTRACT,
    document_parse_result_hash,
)
from windops_backend.model_knowledge_passage import KnowledgePassage
from windops_backend.models import DomainEvent, KnowledgeDocument
from windops_backend.services.document_parse import PARSE_STATE_KEY
from windops_backend.services.knowledge_contracts import (
    KNOWLEDGE_DOCUMENT_INDEX_REQUESTED,
    KNOWLEDGE_DOCUMENT_PARSE_REQUESTED,
)
from windops_backend.storage import OutboxEvent

MAX_DOCUMENTS = 32
MAX_EVENTS = 64
MAX_JOBS = 32
MAX_OUTBOX_ERROR_CHARACTERS = 65_536
COLLECTION_TIMEOUT_SECONDS = 15
EVENT_TYPES = (
    "knowledge.document.ingested",
    "knowledge.document.parse.started",
    "knowledge.document.parse.failed",
    "knowledge.document.parsed",
    "knowledge.document.indexed",
)


class OwnedDocumentEvidenceSpec(BaseModel):
    """Private caller-registered ownership and expected immutable identity."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    document_alias: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    document_id: str = Field(min_length=1, max_length=64)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_version: str = Field(min_length=1, max_length=32)
    tenant_id: str = Field(min_length=1, max_length=64)
    wind_farm_id: str | None = Field(default=None, min_length=1, max_length=64)
    turbine_id: str | None = Field(default=None, min_length=1, max_length=64)
    data_scope: str = Field(default="knowledge", min_length=1, max_length=48)
    submission: Literal["accepted", "submission_unknown", "not_accepted"] = "accepted"
    embedding_provider: str = Field(min_length=1, max_length=96)
    embedding_model: str = Field(min_length=1, max_length=160)
    embedding_dimensions: Literal[1536] = 1536
    expected_passages: int | None = Field(default=None, ge=1, le=MAX_PASSAGES)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_hash(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _utc(value: datetime) -> datetime:
    # The production SQLite representation drops tzinfo from UTC DateTime.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _time(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _duration(start: datetime | None, end: datetime | None, warnings: list[str]) -> float | None:
    if start is None or end is None:
        return None
    value = (_utc(end) - _utc(start)).total_seconds() * 1000
    if value < 0:
        warnings.append("clock_inconsistent")
        return None
    return value


def _positive_attempt(payload: dict[str, Any]) -> int | None:
    value = payload.get("attempt")
    return value if type(value) is int and 1 <= value <= MAX_EVENTS else None


def _timeline(
    events: list[DomainEvent],
    parse_job: OutboxEvent | None,
    index_job: OutboxEvent | None,
    accepted_at: datetime,
    indexed_at: datetime | None,
) -> dict[str, Any]:
    warnings: list[str] = []
    ordered = sorted(events, key=lambda row: row.sequence)
    if any(
        _utc(left.occurred_at) > _utc(right.occurred_at)
        for left, right in zip(ordered, ordered[1:], strict=False)
    ):
        warnings.append("clock_inconsistent")
    starts: dict[int, list[DomainEvent]] = {}
    finishes: dict[int, list[DomainEvent]] = {}
    parsed = [event for event in ordered if event.event_type == "knowledge.document.parsed"]
    terminal_failures = []
    for event in ordered:
        payload = event.payload
        if not isinstance(payload, dict):
            warnings.append("event_payload_invalid")
            continue
        if event.event_type == "knowledge.document.parse.started":
            attempt = _positive_attempt(payload)
            if parse_job is None or payload.get("event_id") != parse_job.id or attempt is None:
                warnings.append("start_identity_missing_or_mismatched")
                continue
            starts.setdefault(attempt, []).append(event)
        elif event.event_type in {"knowledge.document.parse.failed", "knowledge.document.parsed"}:
            attempt = _positive_attempt(payload)
            if attempt is None:
                warnings.append("finish_attempt_unknown")
            elif parse_job is not None:
                # Existing failed events omit event_id. Only the unique, bound
                # parse job permits the document+attempt association.
                if payload.get("event_id", parse_job.id) != parse_job.id:
                    warnings.append("finish_job_mismatched")
                else:
                    finishes.setdefault(attempt, []).append(event)
            if event.event_type.endswith("failed"):
                if payload.get("terminal") is True:
                    terminal_failures.append(event)
                elif type(payload.get("terminal")) is not bool:
                    warnings.append("failure_terminality_unknown")
    attempts = []
    retry_waits = []
    for attempt in sorted(starts):
        beginning, ending = starts[attempt], finishes.get(attempt, [])
        start = beginning[0] if len(beginning) == 1 else None
        finish = ending[0] if len(ending) == 1 else None
        if start is None or len(ending) > 1:
            warnings.append("ambiguous_attempt_events")
        if start is not None and finish is not None and finish.sequence <= start.sequence:
            warnings.append("attempt_sequence_inconsistent")
            finish = None
        state = (
            "paired"
            if start is not None and finish is not None
            else ("finish_identity_unknown" if parsed else "interrupted_no_terminal_event")
        )
        attempts.append(
            {
                "attempt": attempt,
                "status": state,
                "start_sequence": start.sequence if start is not None else None,
                "finish_sequence": finish.sequence if finish is not None else None,
                "started_at": _time(start.occurred_at) if start is not None else None,
                "finished_at": _time(finish.occurred_at) if finish is not None else None,
                "service_interval_ms": _duration(
                    start.occurred_at if start is not None else None,
                    finish.occurred_at if finish is not None else None,
                    warnings,
                ),
            }
        )
        following = starts.get(attempt + 1, [])
        if finish is not None and len(following) == 1:
            retry_waits.append(
                {
                    "after_attempt": attempt,
                    "wait_ms": _duration(finish.occurred_at, following[0].occurred_at, warnings)
                    if following[0].sequence > finish.sequence
                    else None,
                }
            )
    if any(attempt not in starts for attempt in finishes):
        warnings.append("finish_without_start")
    all_starts = [items[0] for items in starts.values() if len(items) == 1]
    first = min(all_starts, key=lambda row: row.sequence) if all_starts else None
    terminal = (
        parsed[0]
        if len(parsed) == 1
        else (terminal_failures[0] if len(terminal_failures) == 1 else None)
    )
    return {
        "clock": "application_utc_not_database_commit_time",
        "cross_process_clock_synchronization": "not_verified",
        "attempts": attempts,
        "retry_waits": retry_waits,
        "first_parse_queue_wait_ms": _duration(
            parse_job.created_at if parse_job is not None else None,
            first.occurred_at if first is not None else None,
            warnings,
        ),
        "parse_end_to_end_ms": _duration(
            accepted_at,
            terminal.occurred_at if terminal is not None else None,
            warnings,
        ),
        "index_queue_to_persisted_ms": _duration(
            index_job.created_at if index_job is not None else None,
            indexed_at,
            warnings,
        ),
        "index_end_to_end_ms": _duration(accepted_at, indexed_at, warnings),
        "latest_index_claim_to_processed_ms": _duration(
            index_job.claimed_at if index_job is not None else None,
            index_job.processed_at if index_job is not None else None,
            warnings,
        ),
        "warnings": sorted(set(warnings)),
        "pure_ocr_cpu_time": "not_available",
        "dispatch_history": "not_available",
        "index_attempt_history": "not_available",
        "attempt_history_complete": bool(attempts)
        and not warnings
        and all(item["status"] == "paired" for item in attempts),
    }


def _vector(value: Any, spec: OwnedDocumentEvidenceSpec) -> tuple[list[float], dict[str, Any]]:
    if isinstance(value, bytes):
        if len(value) != spec.embedding_dimensions * 4:
            raise ValueError("vector_dimensions_invalid")
        numbers = _unpack_vector(value)
    else:
        if value is None or len(value) != spec.embedding_dimensions:
            raise ValueError("vector_dimensions_invalid")
        numbers = [float(item) for item in value]
    if not all(math.isfinite(item) for item in numbers):
        raise ValueError("vector_nonfinite")
    return numbers, {
        "dimensions": len(numbers),
        "sha256": hashlib.sha256(pack_embedding_vector(numbers)).hexdigest(),
        "hash_contract": "ieee754-float32-network.v1",
    }


def _parsed_result(
    document: KnowledgeDocument,
    pieces: list[KnowledgePassage],
    state: dict[str, Any],
) -> dict[str, Any]:
    identity = state["identity"]
    layout = document.metadata_["_openvigil_source_layout"]
    result = {
        "contract": PARSE_CONTRACT,
        "source_sha256": document.artifact_sha256,
        "model_manifest_sha256": identity["model_manifest_sha256"],
        "execution_code_sha256": state["observed_execution_code_sha256"],
        "library_versions": state["observed_library_versions"],
        "body": document.body,
        "source_page_count": layout["page_count"],
        "passages": [
            {
                **{
                    key: getattr(piece, key)
                    for key in (
                        "ordinal",
                        "text",
                        "text_sha256",
                        "char_start",
                        "char_end",
                        "parser_version",
                        "page_number",
                        "section",
                    )
                },
                "native_locator": {
                    key: value
                    for key, value in piece.native_locator.items()
                    if key != "source_verified"
                },
            }
            for piece in pieces
        ],
    }
    DocumentParseResult.model_validate(result)
    return result


async def _bounded_rows(
    session: AsyncSession,
    spec: OwnedDocumentEvidenceSpec,
) -> tuple[KnowledgeDocument | None, list[KnowledgePassage], list[OutboxEvent], list[DomainEvent]]:
    document_size = (
        await session.execute(
            select(
                KnowledgeDocument.id,
                func.length(KnowledgeDocument.body),
                func.length(cast(KnowledgeDocument.metadata_, Text)),
            )
            .where(KnowledgeDocument.id == spec.document_id)
            .limit(1)
        )
    ).first()
    if document_size is None:
        return None, [], [], []
    if document_size[1] > MAX_TEXT_CHARACTERS or document_size[2] > MAX_OUTPUT_BYTES:
        raise ValueError("evidence_limit_exceeded")
    for model, predicate, maximum, payload in (
        (
            KnowledgePassage,
            KnowledgePassage.document_id == spec.document_id,
            MAX_PASSAGES,
            KnowledgePassage.native_locator,
        ),
        (
            OutboxEvent,
            (OutboxEvent.aggregate_id == spec.document_id)
            & (OutboxEvent.aggregate_type == "knowledge_document"),
            MAX_JOBS,
            OutboxEvent.payload,
        ),
        (
            DomainEvent,
            (DomainEvent.aggregate_id == spec.document_id)
            & (DomainEvent.aggregate_type == "knowledge_document"),
            MAX_EVENTS,
            DomainEvent.payload,
        ),
    ):
        count, size = (
            await session.execute(
                select(
                    func.count(),
                    func.sum(func.length(cast(payload, Text))),
                )
                .select_from(model)
                .where(predicate)
            )
        ).one()
        if count > maximum or (size or 0) > MAX_OUTPUT_BYTES:
            raise ValueError("evidence_limit_exceeded")
    # ORM hydration below also reads last_error, an unbounded Text column. Its
    # aggregate must be checked in SQL before loading any owned job objects.
    error_characters = await session.scalar(
        select(func.sum(func.length(OutboxEvent.last_error))).where(
            OutboxEvent.aggregate_type == "knowledge_document",
            OutboxEvent.aggregate_id == spec.document_id,
        )
    )
    if (error_characters or 0) > MAX_OUTBOX_ERROR_CHARACTERS:
        raise ValueError("evidence_limit_exceeded")
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        document_vector_size = await session.scalar(
            select(func.length(KnowledgeDocument.embedding)).where(
                KnowledgeDocument.id == spec.document_id
            )
        )
        if (document_vector_size or 0) > 1536 * 4:
            raise ValueError("evidence_limit_exceeded")
    document = await session.get(KnowledgeDocument, spec.document_id)
    text_size = await session.scalar(
        select(func.sum(func.length(KnowledgePassage.text))).where(
            KnowledgePassage.document_id == spec.document_id,
        )
    )
    if (text_size or 0) > MAX_TEXT_CHARACTERS:
        raise ValueError("evidence_limit_exceeded")
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        vector_size = await session.scalar(
            select(func.sum(func.length(KnowledgePassage.embedding))).where(
                KnowledgePassage.document_id == spec.document_id,
            )
        )
        if (vector_size or 0) > MAX_PASSAGES * 1536 * 4:
            raise ValueError("evidence_limit_exceeded")
    pieces = list(
        await session.scalars(
            select(KnowledgePassage)
            .where(
                KnowledgePassage.document_id == spec.document_id,
            )
            .order_by(KnowledgePassage.ordinal)
            .limit(MAX_PASSAGES + 1)
        )
    )
    jobs = list(
        await session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.aggregate_type == "knowledge_document",
                OutboxEvent.aggregate_id == spec.document_id,
            )
            .limit(MAX_JOBS + 1)
        )
    )
    events = list(
        await session.scalars(
            select(DomainEvent)
            .where(
                DomainEvent.aggregate_type == "knowledge_document",
                DomainEvent.aggregate_id == spec.document_id,
                DomainEvent.event_type.in_(EVENT_TYPES),
            )
            .order_by(DomainEvent.sequence)
            .limit(MAX_EVENTS + 1)
        )
    )
    return document, pieces, jobs, events


def _document_receipt(
    spec: OwnedDocumentEvidenceSpec,
    document: KnowledgeDocument | None,
    pieces: list[KnowledgePassage],
    jobs: list[OutboxEvent],
    events: list[DomainEvent],
) -> dict[str, Any]:
    errors: list[str] = []
    result: dict[str, Any] = {
        "document_alias": spec.document_alias,
        "submission": spec.submission,
        "database_present": document is not None,
        "accepted": document is not None or spec.submission == "accepted",
        "outcome": "unfinished",
        "evidence_status": "incomplete",
        "errors": errors,
    }
    if document is None:
        if spec.submission == "accepted":
            errors.append("accepted_document_missing")
        return result
    if spec.submission == "not_accepted":
        errors.append("unexpected_accepted_document")
    scope = {
        key: getattr(document, key)
        for key in (
            "tenant_id",
            "wind_farm_id",
            "turbine_id",
            "data_scope",
        )
    }
    if any(value != getattr(spec, key) for key, value in scope.items()):
        result.update(evidence_status="invalid", errors=["scope_mismatch"])
        return result
    result["scope_sha256"] = document_parse_result_hash(scope)
    if (
        document.artifact_sha256 != spec.artifact_sha256
        or document.document_version != spec.document_version
    ):
        errors.append("source_identity_mismatch")
    state = (
        document.metadata_.get(PARSE_STATE_KEY) if isinstance(document.metadata_, dict) else None
    )
    if not isinstance(state, dict):
        result.update(evidence_status="invalid", errors=[*errors, "parse_state_missing"])
        return result
    parse_jobs = [job for job in jobs if job.event_type == KNOWLEDGE_DOCUMENT_PARSE_REQUESTED]
    index_jobs = [job for job in jobs if job.event_type == KNOWLEDGE_DOCUMENT_INDEX_REQUESTED]
    parse_job = parse_jobs[0] if len(parse_jobs) == 1 else None
    index_job = index_jobs[0] if len(index_jobs) == 1 else None
    if parse_job is None or state.get("event_id") != parse_job.id:
        errors.append("parse_job_identity_invalid")
    identity = state.get("identity")
    if (
        not isinstance(identity, dict)
        or parse_job is None
        or identity != parse_job.payload.get("parse_identity")
        or identity.get("source_sha256") != spec.artifact_sha256
        or identity.get("document_version") != spec.document_version
    ):
        errors.append("parse_identity_mismatch")
        identity = {}
    if any(job.payload.get("document_id") != document.id for job in [*parse_jobs, *index_jobs]):
        errors.append("outbox_payload_identity_mismatch")
    if any(
        not isinstance(event.payload, dict) or event.payload.get("document_id") != document.id
        for event in events
    ):
        errors.append("event_payload_identity_mismatch")
    ingested = [event for event in events if event.event_type == "knowledge.document.ingested"]
    parsed = [event for event in events if event.event_type == "knowledge.document.parsed"]
    indexed = [event for event in events if event.event_type == "knowledge.document.indexed"]
    if len(ingested) != 1:
        errors.append("ingested_event_count_invalid")
    elif (
        ingested[0].payload.get("artifact_sha256") != spec.artifact_sha256
        or ingested[0].payload.get("document_version") != spec.document_version
    ):
        errors.append("ingested_identity_mismatch")
    result["accepted_at"] = _time(document.created_at)
    result["outbox"] = {
        "parse_count": len(parse_jobs),
        "index_count": len(index_jobs),
        "parse_status": parse_job.status
        if parse_job
        and parse_job.status in {"pending", "dispatched", "processing", "succeeded", "failed"}
        else "unknown",
        "parse_attempts": parse_job.attempts if parse_job else None,
        "index_status": index_job.status
        if index_job
        and index_job.status in {"pending", "dispatched", "processing", "succeeded", "failed"}
        else "unknown",
        "index_attempts": index_job.attempts if index_job else None,
        "parse_processed_at": _time(parse_job.processed_at) if parse_job else None,
        "index_processed_at": _time(index_job.processed_at) if index_job else None,
    }
    result["timing"] = _timeline(
        events, parse_job, index_job, document.created_at, document.indexed_at
    )
    result["passage_count"] = len(pieces)
    if state.get("status") == "parsed":
        if (
            len(parsed) != 1
            or index_job is None
            or parse_job is None
            or parse_job.status != "succeeded"
            or parse_job.processed_at is None
            or parse_job.attempts < 1
        ):
            errors.append("parsed_ledger_incomplete")
        if spec.expected_passages is not None and len(pieces) != spec.expected_passages:
            errors.append("passage_count_mismatch")
        if any(
            piece.document_version != spec.document_version
            or piece.source_sha256 != spec.artifact_sha256
            or not isinstance(piece.native_locator, dict)
            or piece.native_locator.get("source_verified") is not True
            for piece in pieces
        ):
            errors.append("passage_source_identity_mismatch")
        try:
            parsed_result = _parsed_result(document, pieces, state)
            sha = document_parse_result_hash(parsed_result)
            result["result_sha256"] = sha
            if (
                len(parsed) != 1
                or parsed[0].payload.get("artifact_sha256") != spec.artifact_sha256
                or parsed[0].payload.get("passage_count") != len(pieces)
                or parsed[0].payload.get("page_count") != parsed_result["source_page_count"]
            ):
                errors.append("parsed_event_identity_mismatch")
            if parse_job is not None and state.get("attempts") != parse_job.attempts:
                errors.append("parse_attempt_count_mismatch")
            if (
                state.get("result_hash_contract") != DOCUMENT_RESULT_HASH_CONTRACT
                or state.get("result_sha256") != sha
                or len(parsed) != 1
                or parsed[0].payload.get("result_sha256") != sha
            ):
                errors.append("result_hash_mismatch")
            if parsed_result["execution_code_sha256"] != identity.get(
                "execution_code_sha256"
            ) or parsed_result["library_versions"] != identity.get("library_versions"):
                errors.append("observed_parser_identity_mismatch")
        except (AttributeError, KeyError, TypeError, ValueError):
            errors.append("persisted_parse_result_invalid")
    elif (
        pieces
        or document.body
        or document.embedding is not None
        or document.vectorized
        or index_jobs
    ):
        errors.append("partial_publication_before_parse_success")
    if parsed and state.get("status") != "parsed":
        errors.append("parsed_state_mismatch")
    if indexed and document.ingestion_status != "indexed":
        errors.append("indexed_state_mismatch")
    if document.ingestion_status == "indexed":
        if (
            not document.vectorized
            or document.indexed_at is None
            or len(indexed) != 1
            or state.get("status") != "parsed"
            or index_job is None
            or index_job.status != "succeeded"
            or index_job.processed_at is None
            or index_job.attempts < 1
        ):
            errors.append("indexed_ledger_incomplete")
        if (
            document.embedding_provider != spec.embedding_provider
            or document.embedding_model != spec.embedding_model
            or any(
                piece.embedding_provider != spec.embedding_provider
                or piece.embedding_model != spec.embedding_model
                or piece.indexed_at != document.indexed_at
                for piece in pieces
            )
            or any(
                event.payload.get("embedding_provider") != spec.embedding_provider
                or event.payload.get("embedding_model") != spec.embedding_model
                for event in indexed
            )
        ):
            errors.append("embedding_identity_mismatch")
        vectors = []
        receipts = []
        try:
            for piece in pieces:
                vector, receipt = _vector(piece.embedding, spec)
                vectors.append(vector)
                receipts.append(
                    {
                        "ordinal": piece.ordinal,
                        "text_sha256": _safe_hash(piece.text_sha256),
                        "source_sha256": _safe_hash(piece.source_sha256),
                        "vector": receipt,
                    }
                )
            document_vector, receipt = _vector(document.embedding, spec)
            expected = aggregate_embedding_vectors(vectors)
            if not all(
                math.isclose(left, right, rel_tol=1e-5, abs_tol=1e-6)
                for left, right in zip(document_vector, expected, strict=True)
            ):
                errors.append("document_vector_aggregate_mismatch")
            result["embedding"] = {
                "provider_sha256": _sha(spec.embedding_provider),
                "model_sha256": _sha(spec.embedding_model),
                "document_vector": receipt,
                "passages": receipts,
                "semantic_correctness": "not_verified",
            }
        except (TypeError, ValueError, OverflowError):
            errors.append("vector_evidence_invalid")
        if not errors:
            result.update(outcome="indexed", evidence_status="verified")
    elif document.ingestion_status == "parse_failed":
        failed = [
            event for event in events if event.event_type == "knowledge.document.parse.failed"
        ]
        if (
            state.get("status") != "failed"
            or parse_job is None
            or parse_job.status != "failed"
            or parse_job.processed_at is None
            or not failed
            or (
                failed[-1].payload.get("terminal") is not True
                and failed[-1].payload.get("error_code") != "DocumentParseAttemptsExhausted"
            )
        ):
            errors.append("terminal_failure_ledger_incomplete")
        elif not errors:
            result.update(outcome="terminal_failed", evidence_status="verified")
    elif document.ingestion_status == "failed":
        # An index failed row alone carries no durable retry-exhaustion policy.
        result["index_failure"] = "observed_current_failure_not_proven_terminal"
    if errors:
        result.update(evidence_status="invalid", errors=sorted(set(errors)))
    return result


async def collect_document_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    manifest: Sequence[OwnedDocumentEvidenceSpec],
) -> dict[str, Any]:
    """Read only registered IDs under one consistent snapshot, never discover IDs."""
    if (
        len(manifest) > MAX_DOCUMENTS
        or any(not isinstance(item, OwnedDocumentEvidenceSpec) for item in manifest)
        or len({item.document_id for item in manifest}) != len(manifest)
        or len({item.document_alias for item in manifest}) != len(manifest)
    ):
        raise ValueError("invalid_owned_document_manifest")
    try:
        manifest = tuple(
            OwnedDocumentEvidenceSpec.model_validate(item.model_dump()) for item in manifest
        )
    except ValueError:
        # model_copy/model_construct can bypass Pydantic's original validation.
        raise ValueError("invalid_owned_document_manifest") from None
    rows = []
    async with asyncio.timeout(COLLECTION_TIMEOUT_SECONDS):
        async with session_factory() as session, session.begin():
            session.autoflush = False
            dialect = session.bind.dialect.name if session.bind is not None else "unknown"
            if dialect == "postgresql":
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
            elif dialect == "sqlite":
                await session.execute(text("BEGIN"))
            else:
                raise ValueError("unsupported_evidence_database")
            for item in manifest:
                try:
                    rows.append(_document_receipt(item, *await _bounded_rows(session, item)))
                except ValueError as error:
                    if str(error) != "evidence_limit_exceeded":
                        raise
                    rows.append(
                        {
                            "document_alias": item.document_alias,
                            "submission": item.submission,
                            "database_present": True,
                            "accepted": True,
                            "outcome": "unfinished",
                            "evidence_status": "invalid",
                            "errors": ["evidence_limit_exceeded"],
                        }
                    )
    accepted = [row for row in rows if row["accepted"]]
    counts = {
        outcome: sum(row["outcome"] == outcome for row in accepted)
        for outcome in ("indexed", "terminal_failed", "unfinished")
    }
    return {
        "schema_version": "openvigil.load-document-evidence.v1",
        "verification_scope": "document_source_result_and_index_persistence_only",
        "documents": rows,
        "counts": {
            "planned": len(rows),
            "accepted": len(accepted),
            **counts,
            "submission_unknown": sum(row["submission"] == "submission_unknown" for row in rows),
            "submission_unknown_unresolved": sum(
                row["submission"] == "submission_unknown" and not row["database_present"]
                for row in rows
            ),
            "not_accepted": sum(
                not row["accepted"] and row["submission"] == "not_accepted" for row in rows
            ),
        },
        "conservation_verified": len(accepted) == sum(counts.values()),
        "database_evidence_verified": bool(accepted)
        and all(row["evidence_status"] == "verified" for row in accepted),
        "external_verification": {
            "redis_delivery": "not_verified",
            "neo4j_projection": "not_verified",
            "object_storage": "not_verified",
            "ocr_execution": "not_verified",
        },
    }
