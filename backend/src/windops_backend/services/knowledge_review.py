"""Source-bound numeric review without rewriting OCR or retrieval content."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import false, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from windops_backend.config import Settings
from windops_backend.document_parse_result_identity import document_parse_result_hash
from windops_backend.errors import ConflictError, InvalidTransitionError, NotFoundError
from windops_backend.knowledge_graph.domain import GraphAccessPolicy
from windops_backend.model_base import utcnow
from windops_backend.model_knowledge_review import (
    KnowledgeNumericReview,
    KnowledgeNumericReviewEvent,
)
from windops_backend.models import KnowledgeDocument, KnowledgePassage
from windops_backend.schema_knowledge_review import (
    NumericCorrection,
    NumericReviewDecision,
    NumericReviewProposal,
    NumericSourceBinding,
)
from windops_backend.services.events import append_domain_event
from windops_backend.services.knowledge import MAX_KNOWLEDGE_ARTIFACT_BYTES
from windops_backend.services.knowledge_access import knowledge_document_query
from windops_backend.storage import ArtifactVerifier

REVIEW_ROLES = frozenset({"operations_manager", "maintenance_reviewer", "test_system"})


@dataclass(frozen=True)
class NumericReviewSource:
    document: KnowledgeDocument
    passage: KnowledgePassage
    binding: dict[str, Any]


async def review_source(
    session: AsyncSession,
    passage_id: str,
    policy: GraphAccessPolicy,
    expected: NumericSourceBinding | None = None,
    *,
    lock: bool = False,
) -> NumericReviewSource:
    """Always run before idempotency lookup, including permission revocation."""
    document_query = knowledge_document_query(policy).where(
        KnowledgeDocument.id
        == select(KnowledgePassage.document_id)
        .where(KnowledgePassage.id == passage_id)
        .scalar_subquery()
    )
    if lock:
        document_query = document_query.with_for_update()
    document = await session.scalar(document_query.execution_options(populate_existing=True))
    if document is None:
        raise NotFoundError("knowledge passage was not found")
    passage = await session.scalar(
        select(KnowledgePassage)
        .where(
            KnowledgePassage.id == passage_id,
            KnowledgePassage.document_id == document.id,
        )
        .execution_options(populate_existing=True)
    )
    if passage is None:
        raise NotFoundError("knowledge passage was not found")
    if (
        document.content_type != "application/pdf"
        or not document.artifact_uri
        or not document.artifact_sha256
        or passage.source_sha256 is None
        or passage.native_locator.get("source_verified") is not True
        or passage.page_number is None
    ):
        raise InvalidTransitionError("numeric review requires a verified PDF passage")
    if (
        passage.document_version != document.document_version
        or passage.source_sha256 != document.artifact_sha256
        or hashlib.sha256(passage.text.encode()).hexdigest() != passage.text_sha256
        or document.body[passage.char_start : passage.char_end] != passage.text
        or len(passage.text) > 8000
    ):
        raise ConflictError("immutable passage source identity changed")
    binding = NumericSourceBinding(
        expected_document_version=document.document_version,
        expected_source_sha256=document.artifact_sha256,
        expected_text_sha256=passage.text_sha256,
        expected_parser_version=passage.parser_version,
        expected_locator_sha256=document_parse_result_hash(passage.native_locator),
    ).model_dump()
    if expected is not None and any(
        getattr(expected, key) != value for key, value in binding.items()
    ):
        raise ConflictError("numeric review source binding changed")
    head = await session.scalar(
        select(KnowledgeNumericReview).where(
            KnowledgeNumericReview.passage_id == passage_id,
        )
    )
    if head is not None and (
        head.source_binding != binding
        or head.original_text != passage.text
        or head.document_id != document.id
    ):
        raise ConflictError("numeric review no longer matches its original source")
    return NumericReviewSource(document, passage, binding)


def apply_corrections(text: str, corrections: Sequence[NumericCorrection]) -> str:
    spans: list[tuple[int, int, str]] = []
    for item in corrections:
        if item.end > len(text) or text[item.start : item.end] != item.original_value:
            raise InvalidTransitionError("numeric correction does not match the original span")
        # Do not permit editing only a few digits inside a larger numeric token.
        numeric = "0123456789OoIl|+-.−,eE"
        following = text[item.end : item.end + 1]
        continuation = following in numeric and bool(following)
        if following in {".", ","} and (
            item.end + 1 == len(text) or text[item.end + 1] not in "0123456789OoIl|"
        ):
            continuation = False  # Sentence punctuation is not part of the number.
        if (item.start and text[item.start - 1] in numeric) or continuation:
            raise InvalidTransitionError("numeric correction must select the complete token")
        spans.append((item.start, item.end, item.corrected_value))
        if item.unit_start is not None and item.unit_end is not None:
            if (
                item.unit_end > len(text)
                or text[item.unit_start : item.unit_end] != item.original_unit
            ):
                raise InvalidTransitionError("unit correction does not match the original span")
            gap = (
                text[item.end : item.unit_start]
                if item.unit_start >= item.end
                else text[item.unit_end : item.start]
                if item.unit_end <= item.start
                else None
            )
            if gap is None or gap.strip():
                raise InvalidTransitionError("unit span must be adjacent to its numeric value")
            spans.append((item.unit_start, item.unit_end, item.corrected_unit))
    spans.sort()
    for previous, current in zip(spans, spans[1:], strict=False):
        if previous[1] > current[0]:
            raise InvalidTransitionError("numeric and unit correction spans cannot overlap")
    result = text
    for start, end, replacement in reversed(spans):
        result = result[:start] + replacement + result[end:]
    return result


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=UTC).isoformat()
        if value.tzinfo is None
        else value.astimezone(UTC).isoformat()
    )


def serialize_review(head: KnowledgeNumericReview) -> dict[str, Any]:
    return {
        "review_id": head.id,
        "revision": head.revision,
        "status": head.status,
        "kind": head.kind,
        "corrections": head.corrections,
        "corrected_text": head.corrected_text,
        "reason": head.reason,
        "proposed_by": head.proposed_by,
        "proposed_at": _iso(head.proposed_at),
        "reviewed_by": head.reviewed_by,
        "reviewed_at": _iso(head.reviewed_at),
        "review_reason": head.review_reason,
    }


def review_permissions(
    head: KnowledgeNumericReview | None, subject: str, roles: Sequence[str]
) -> dict[str, bool]:
    allowed = not REVIEW_ROLES.isdisjoint(roles)
    independent = allowed and head is not None and subject != head.proposed_by
    pending = head is not None and head.status == "pending"
    return {
        "can_propose": allowed,
        "can_approve": independent and pending,
        "can_reject": independent and pending,
        "can_withdraw": bool(
            allowed
            and head
            and head.status in {"pending", "approved"}
            and subject in {head.proposed_by, head.reviewed_by}
        ),
    }


async def numeric_review_detail(
    session: AsyncSession,
    source: NumericReviewSource,
    subject: str,
    roles: Sequence[str],
    *,
    cursor: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    if not 1 <= limit <= 50 or (cursor is not None and cursor < 1):
        raise ValueError("invalid numeric review history window")
    head = await session.scalar(
        select(KnowledgeNumericReview)
        .where(
            KnowledgeNumericReview.passage_id == source.passage.id,
        )
        .execution_options(populate_existing=True)
    )
    statement = select(KnowledgeNumericReviewEvent).where(
        KnowledgeNumericReviewEvent.passage_id == source.passage.id,
        KnowledgeNumericReviewEvent.document_id == source.document.id,
    )
    if head is None:
        statement = statement.where(false())
    else:
        # A concurrent action may commit between the two reads; never attach a
        # future event to an earlier current-state response.
        statement = statement.where(
            KnowledgeNumericReviewEvent.review_id == head.id,
            KnowledgeNumericReviewEvent.revision <= head.revision,
        )
    if cursor is not None:
        statement = statement.where(KnowledgeNumericReviewEvent.revision < cursor)
    rows = list(
        await session.scalars(
            statement.order_by(KnowledgeNumericReviewEvent.revision.desc()).limit(limit + 1)
        )
    )
    page = rows[:limit]
    return {
        "passage_id": source.passage.id,
        "document_id": source.document.id,
        "source_binding": source.binding,
        "original_text": source.passage.text,
        "page_number": source.passage.page_number,
        "current": serialize_review(head) if head else None,
        "history": [
            {
                "event_id": row.id,
                "revision": row.revision,
                "action": row.action,
                "actor": row.actor,
                "occurred_at": _iso(row.occurred_at),
                "reason": row.reason,
                "snapshot": row.snapshot,
                "snapshot_sha256": row.snapshot_sha256,
            }
            for row in page
        ],
        "next_cursor": page[-1].revision if len(rows) > limit else None,
        "permissions": review_permissions(head, subject, roles),
        "updates_retrieval_text": False,
        "authorizes_work": False,
        "fixtureFallback": False,
    }


async def _head(session: AsyncSession, passage_id: str) -> KnowledgeNumericReview | None:
    return cast(
        KnowledgeNumericReview | None,
        await session.scalar(
            select(KnowledgeNumericReview)
            .where(
                KnowledgeNumericReview.passage_id == passage_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ),
    )


async def _record_event(
    session: AsyncSession,
    head: KnowledgeNumericReview,
    action: str,
    actor: str,
    reason: str,
) -> None:
    snapshot = {
        "source_binding": head.source_binding,
        "original_text": head.original_text,
        "current": serialize_review(head),
    }
    session.add(
        KnowledgeNumericReviewEvent(
            id=str(uuid4()),
            review_id=head.id,
            document_id=head.document_id,
            passage_id=head.passage_id,
            revision=head.revision,
            action=action,
            actor=actor,
            occurred_at=utcnow(),
            reason=reason,
            snapshot=snapshot,
            snapshot_sha256=document_parse_result_hash(snapshot),
        )
    )
    append_domain_event(
        session,
        event_type=f"knowledge.numeric-review.{action}",
        aggregate_type="knowledge_document",
        aggregate_id=head.document_id,
        payload={
            "document_id": head.document_id,
            "passage_id": head.passage_id,
            "review_id": head.id,
            "revision": head.revision,
            "status": head.status,
            "actor": actor,
        },
    )
    await session.flush()


async def propose_numeric_review(
    session: AsyncSession,
    source: NumericReviewSource,
    payload: NumericReviewProposal,
    subject: str,
) -> None:
    corrected_text = apply_corrections(source.passage.text, payload.corrections)
    head = await _head(session, source.passage.id)
    if (head.revision if head else 0) != payload.expected_revision:
        raise ConflictError("numeric review revision changed")
    values = {
        "revision": payload.expected_revision + 1,
        "status": "pending",
        "kind": payload.kind,
        "corrections": [row.model_dump() for row in payload.corrections],
        "corrected_text": corrected_text,
        "reason": payload.reason,
        "proposed_by": subject,
        "proposed_at": utcnow(),
        "reviewed_by": None,
        "reviewed_at": None,
        "review_reason": None,
    }
    if head is None:
        try:
            async with session.begin_nested():
                head = KnowledgeNumericReview(
                    id=str(uuid4()),
                    document_id=source.document.id,
                    passage_id=source.passage.id,
                    source_binding=source.binding,
                    original_text=source.passage.text,
                    **values,
                )
                session.add(head)
                await session.flush()
        except IntegrityError as exc:
            if await _head(session, source.passage.id) is not None:
                raise ConflictError("numeric review was concurrently created") from exc
            raise
    else:
        changed = await session.scalar(
            update(KnowledgeNumericReview)
            .where(
                KnowledgeNumericReview.id == head.id,
                KnowledgeNumericReview.revision == payload.expected_revision,
            )
            .values(**values)
            .returning(KnowledgeNumericReview.id)
            .execution_options(synchronize_session="fetch")
        )
        if changed is None:
            raise ConflictError("numeric review revision changed during proposal")
    await _record_event(session, head, "propose", subject, payload.reason)


async def decide_numeric_review(
    session: AsyncSession,
    source: NumericReviewSource,
    payload: NumericReviewDecision,
    subject: str,
    roles: Sequence[str],
    verifier: ArtifactVerifier,
    settings: Settings,
) -> None:
    head = await _head(session, source.passage.id)
    if head is None:
        raise NotFoundError("numeric review was not found")
    if head.revision != payload.expected_revision:
        raise ConflictError("numeric review revision changed")
    if not review_permissions(head, subject, roles)[f"can_{payload.action}"]:
        raise InvalidTransitionError(
            "numeric review action requires an allowed independent reviewer"
        )
    if payload.action == "approve":
        document = source.document
        try:
            _, content_type = await verifier.read_verified_object(
                document.artifact_uri or "",
                document.artifact_sha256 or "",
                bucket=settings.minio_knowledge_bucket,
                prefix=f"documents/{document.id}",
                allowed_content_types=frozenset({"application/pdf"}),
                max_bytes=MAX_KNOWLEDGE_ARTIFACT_BYTES,
            )
            if content_type != document.content_type:
                raise ValueError("source content type changed")
        except ValueError as exc:
            raise InvalidTransitionError("numeric review source verification failed") from exc
    changed = await session.scalar(
        update(KnowledgeNumericReview)
        .where(
            KnowledgeNumericReview.id == head.id,
            KnowledgeNumericReview.revision == payload.expected_revision,
        )
        .values(
            revision=payload.expected_revision + 1,
            status={"approve": "approved", "reject": "rejected", "withdraw": "withdrawn"}[
                payload.action
            ],
            reviewed_by=subject,
            reviewed_at=utcnow(),
            review_reason=payload.reason,
        )
        .returning(KnowledgeNumericReview.id)
        .execution_options(synchronize_session="fetch")
    )
    if changed is None:
        raise ConflictError("numeric review revision changed during decision")
    await _record_event(session, head, payload.action, subject, payload.reason)
