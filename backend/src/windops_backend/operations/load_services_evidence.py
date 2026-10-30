"""Read-only SQL witnesses for the caller's explicitly owned backlog documents.

This adapter never renews or expires leases, claims jobs, alters clocks, or changes
rows. It proves persisted SQL state only. Redis, object storage, OCR execution and
Neo4j need independent observations from the runtime owner.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from windops_backend.document_parse_result_identity import document_parse_result_hash
from windops_backend.models import DomainEvent, KnowledgeDocument
from windops_backend.operations.load_backlog import ClaimWitness, RecoveryWitness
from windops_backend.operations.load_document_evidence import (
    COLLECTION_TIMEOUT_SECONDS,
    MAX_DOCUMENTS,
    OwnedDocumentEvidenceSpec,
    _bounded_rows,
    _document_receipt,
    collect_document_evidence,
)
from windops_backend.outbox import OUTBOX_LEASE
from windops_backend.services.document_parse import MAX_PARSE_ATTEMPTS, PARSE_STATE_KEY
from windops_backend.services.knowledge_contracts import (
    KNOWLEDGE_DOCUMENT_PARSE_REQUESTED,
)
from windops_backend.storage import OutboxEvent


class EvidenceObservationError(ValueError):
    """Fixed public error codes; no query, identifiers, content or token details."""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _job_hash(job: OutboxEvent) -> str:
    return hashlib.sha256(job.id.encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class _CapturedClaim:
    witness: ClaimWitness
    token: str = field(repr=False)
    payload_sha256: str
    start_sequence: int


@dataclass(frozen=True)
class _Observation:
    spec: OwnedDocumentEvidenceSpec = field(repr=False)
    document: KnowledgeDocument = field(repr=False)
    job: OutboxEvent = field(repr=False)
    state: dict[str, Any] = field(repr=False)
    events: list[DomainEvent] = field(repr=False)
    receipt: dict[str, Any]


def _starts(observation: _Observation) -> list[DomainEvent]:
    """Every committed production claim has a started event in its transaction."""
    starts = [
        event
        for event in observation.events
        if event.event_type == "knowledge.document.parse.started"
    ]
    if (
        type(observation.job.attempts) is not int
        or not 0 <= observation.job.attempts <= MAX_PARSE_ATTEMPTS
    ):
        raise EvidenceObservationError("parse_started_identity_invalid")
    expected_attempts = list(range(1, observation.job.attempts + 1))
    if [event.payload.get("attempt") for event in starts] != expected_attempts or any(
        type(event.payload.get("attempt")) is not int
        or event.payload.get("event_id") != observation.job.id
        for event in starts
    ):
        raise EvidenceObservationError("parse_started_identity_invalid")
    return starts


class OwnedEvidenceObservers:
    """Bounded callbacks compatible with BacklogCallbacks; no implicit discovery.

    Each callback has a 15-second deadline and one read-only consistent snapshot.
    The shared collector bounds documents (32), jobs/document (32), events/document
    (64), passages/document (512), text, JSON and vector bytes before hydration. Rows
    are processed one document at a time. The first valid claim's token remains private.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        manifest: Sequence[OwnedDocumentEvidenceSpec],
    ) -> None:
        if not 1 <= len(manifest) <= MAX_DOCUMENTS:
            raise EvidenceObservationError("invalid_owned_document_manifest")
        try:
            self._manifest = tuple(
                OwnedDocumentEvidenceSpec.model_validate(item.model_dump()) for item in manifest
            )
        except (AttributeError, TypeError, ValueError):
            raise EvidenceObservationError("invalid_owned_document_manifest") from None
        if len({item.document_id for item in self._manifest}) != len(self._manifest) or len(
            {item.document_alias for item in self._manifest}
        ) != len(self._manifest):
            raise EvidenceObservationError("invalid_owned_document_manifest")
        self._factory = session_factory
        self._captured: _CapturedClaim | None = None

    @asynccontextmanager
    async def _snapshot(self) -> AsyncIterator[AsyncSession]:
        try:
            async with asyncio.timeout(COLLECTION_TIMEOUT_SECONDS):
                async with self._factory() as session, session.begin():
                    session.autoflush = False
                    dialect = session.bind.dialect.name if session.bind is not None else "unknown"
                    if dialect == "postgresql":
                        await session.execute(
                            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                        )
                    elif dialect == "sqlite":
                        await session.execute(text("BEGIN"))
                    else:
                        raise EvidenceObservationError("unsupported_evidence_database")
                    yield session
        except TimeoutError:
            raise EvidenceObservationError("sql_evidence_timeout") from None
        except SQLAlchemyError:
            raise EvidenceObservationError("sql_evidence_unavailable") from None

    async def _observe(
        self, session: AsyncSession, spec: OwnedDocumentEvidenceSpec
    ) -> _Observation:
        try:
            document, pieces, jobs, events = await _bounded_rows(session, spec)
            if document is None:
                raise EvidenceObservationError("owned_document_missing")
            if any(
                getattr(document, name) != getattr(spec, name)
                for name in (
                    "tenant_id",
                    "wind_farm_id",
                    "turbine_id",
                    "data_scope",
                )
            ):
                raise EvidenceObservationError("owned_document_scope_mismatch")
            if any(not isinstance(job.payload, dict) for job in jobs) or any(
                not isinstance(event.payload, dict) for event in events
            ):
                raise EvidenceObservationError("sql_evidence_payload_invalid")
            receipt = _document_receipt(spec, document, pieces, jobs, events)
            if receipt["errors"]:
                raise EvidenceObservationError("owned_document_evidence_invalid")
            parse_jobs = [
                job for job in jobs if job.event_type == KNOWLEDGE_DOCUMENT_PARSE_REQUESTED
            ]
            if len(parse_jobs) != 1:
                raise EvidenceObservationError("owned_parse_job_invalid")
            state = document.metadata_[PARSE_STATE_KEY]
            if type(state.get("attempts")) is not int:
                raise EvidenceObservationError("owned_parse_attempt_invalid")
            return _Observation(spec, document, parse_jobs[0], state, events, receipt)
        except EvidenceObservationError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise EvidenceObservationError("owned_document_evidence_invalid") from None

    async def read_claim(self) -> ClaimWitness | None:
        """Return a matching active claim; reject malformed/expired processing rows."""
        candidate: _CapturedClaim | None = None
        async with self._snapshot() as session:
            for spec in self._manifest:
                observation = await self._observe(session, spec)
                job, document, state = observation.job, observation.document, observation.state
                starts = _starts(observation)
                if job.status != "processing":
                    continue
                now = datetime.now(UTC)
                if (
                    document.ingestion_status != "parsing"
                    or state.get("status") != "running"
                    or state.get("attempts") != job.attempts
                    or not starts
                    or job.claimed_at is None
                    or job.lease_expires_at is None
                    or not isinstance(job.claim_token, str)
                    or not 1 <= len(job.claim_token) <= 36
                ):
                    raise EvidenceObservationError("active_parse_claim_invalid")
                claimed, expires = _utc(job.claimed_at), _utc(job.lease_expires_at)
                if (
                    abs((expires - claimed - OUTBOX_LEASE).total_seconds()) > 0.1
                    or not claimed <= _utc(starts[-1].occurred_at) <= now < expires
                    or _utc(job.created_at) > claimed
                ):
                    raise EvidenceObservationError("active_parse_lease_invalid")
                witness = ClaimWitness(
                    document_alias=spec.document_alias,
                    job_id_sha256=_job_hash(job),
                    attempt=job.attempts,
                    claimed_at=claimed,
                    lease_expires_at=expires,
                    observed_at=now,
                    processing=True,
                    started_event_verified=True,
                    owned_scope_verified=True,
                )
                if candidate is None:
                    candidate = _CapturedClaim(
                        witness,
                        job.claim_token,
                        document_parse_result_hash(job.payload),
                        starts[-1].sequence,
                    )
        # A long read must not return an already expired claim as actionable.
        if candidate is not None and candidate.witness.lease_expires_at <= datetime.now(UTC):
            raise EvidenceObservationError("active_parse_lease_invalid")
        if candidate is not None:
            if self._captured is not None:
                if (
                    candidate.token != self._captured.token
                    or candidate.payload_sha256 != self._captured.payload_sha256
                    or candidate.witness.job_id_sha256 != self._captured.witness.job_id_sha256
                ):
                    raise EvidenceObservationError("observed_claim_changed")
                return self._captured.witness
            self._captured = candidate
            return candidate.witness
        return None

    async def verify_offline_documents(self) -> bool:
        """All planned documents must be registered and never claimed/published."""
        async with self._snapshot() as session:
            for spec in self._manifest:
                observation = await self._observe(session, spec)
                job, state = observation.job, observation.state
                if (
                    observation.document.ingestion_status != "pending_parse"
                    or state.get("status") != "pending"
                    or state.get("attempts") != 0
                    or job.attempts != 0
                    or job.status not in {"pending", "dispatched"}
                    or any(
                        value is not None
                        for value in (
                            job.claimed_at,
                            job.claim_token,
                            job.lease_expires_at,
                            job.processed_at,
                        )
                    )
                    or observation.receipt["passage_count"] != 0
                    or observation.receipt["outbox"]["index_count"] != 0
                    or len(observation.events) != 1
                    or observation.events[0].event_type != "knowledge.document.ingested"
                    or any(
                        state.get(name) is not None
                        for name in ("started_at", "completed_at", "result_sha256", "error_code")
                    )
                ):
                    return False
        return True

    async def verify_recovery(self, initial_claim: ClaimWitness | None) -> RecoveryWitness:
        captured = self._captured
        if (initial_claim is None) != (captured is None):
            raise EvidenceObservationError("initial_claim_evidence_missing")
        if initial_claim is not None and captured is not None:
            try:
                checked = ClaimWitness.model_validate(initial_claim.model_dump())
            except (AttributeError, TypeError, ValueError):
                raise EvidenceObservationError("initial_claim_evidence_invalid") from None
            if checked != captured.witness:
                raise EvidenceObservationError("initial_claim_evidence_invalid")
        counts = {"indexed": 0, "terminal_failed": 0, "unfinished": 0}
        unique = True
        recovered_hash, recovered_attempt, old_inactive = None, None, False
        async with self._snapshot() as session:
            for spec in self._manifest:
                observation = await self._observe(session, spec)
                job = observation.job
                starts = _starts(observation)
                outcome = observation.receipt["outcome"]
                counts[outcome] += 1
                parsed = [
                    event
                    for event in observation.events
                    if event.event_type == "knowledge.document.parsed"
                ]
                indexed = [
                    event
                    for event in observation.events
                    if event.event_type == "knowledge.document.indexed"
                ]
                publication_verified = (
                    outcome == "indexed"
                    and observation.receipt["evidence_status"] == "verified"
                    and len(parsed) == len(indexed) == 1
                    and bool(starts)
                    and starts[-1].sequence < parsed[0].sequence < indexed[0].sequence
                    and _utc(starts[-1].occurred_at)
                    <= _utc(parsed[0].occurred_at)
                    <= _utc(indexed[0].occurred_at)
                    and job.status == "succeeded"
                    and job.claim_token is None
                    and job.lease_expires_at is None
                )
                unique = unique and publication_verified
                if captured is not None and spec.document_alias == captured.witness.document_alias:
                    same_job = (
                        _job_hash(job) == captured.witness.job_id_sha256
                        and document_parse_result_hash(job.payload) == captured.payload_sha256
                    )
                    recovered_hash = _job_hash(job) if same_job else None
                    recovered_attempt = (
                        job.attempts if 1 <= job.attempts <= MAX_PARSE_ATTEMPTS else None
                    )
                    old_inactive = (
                        same_job
                        and publication_verified
                        and job.attempts > captured.witness.attempt
                        and starts[-1].sequence > captured.start_sequence
                        and job.claim_token != captured.token
                        and any(
                            event.sequence == captured.start_sequence
                            and event.payload.get("attempt") == captured.witness.attempt
                            for event in starts
                        )
                    )
        return RecoveryWitness(
            owned_documents=len(self._manifest),
            indexed=counts["indexed"],
            terminal_failed=counts["terminal_failed"],
            unfinished=counts["unfinished"],
            scope_verified=True,
            unique_publications_verified=unique,
            recovered_job_id_sha256=recovered_hash,
            recovered_attempt=recovered_attempt,
            old_claim_inactive=old_inactive,
        )

    async def collect(self) -> dict[str, Any]:
        """Public bounded report; contains no original claim token or raw IDs."""
        try:
            return await collect_document_evidence(self._factory, self._manifest)
        except TimeoutError:
            raise EvidenceObservationError("sql_evidence_timeout") from None
        except SQLAlchemyError:
            raise EvidenceObservationError("sql_evidence_unavailable") from None
