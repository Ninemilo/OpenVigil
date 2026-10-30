"""Verify numeric review acceptance against authoritative rows, not UI flags alone."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from windops_backend.document_parse_result_identity import document_parse_result_hash
from windops_backend.model_knowledge_review import (
    KnowledgeNumericReview,
    KnowledgeNumericReviewEvent,
)
from windops_backend.models import CommandReceipt, DomainEvent, KnowledgePassage


async def document_numeric_review_evidence(
    session: AsyncSession, browser: dict[str, Any], passages: list[KnowledgePassage]
) -> dict[str, Any]:
    observed = browser.get("numeric_review")
    if not isinstance(observed, dict):
        raise RuntimeError("numeric review acceptance missing browser evidence")
    for key in (
        "rejected_incorrect_correction",
        "original_text_unchanged",
        "pdf_rendered",
        "self_review_rejected",
        "replay_preserved_history",
        "stale_revision_rejected",
        "restricted_read_rejected",
        "reloaded_persisted_state",
    ):
        if observed.get(key) is not True:
            raise RuntimeError(f"numeric review acceptance missing verified {key}")
    passage = next((row for row in passages if row.id == observed.get("passage_id")), None)
    if passage is None or "+123.45 kN" not in passage.text:
        raise RuntimeError("numeric review acceptance has no matching immutable passage")
    if hashlib.sha256(passage.text.encode()).hexdigest() != passage.text_sha256:
        raise RuntimeError("numeric review acceptance source text hash changed")
    head = await session.scalar(
        select(KnowledgeNumericReview).where(
            KnowledgeNumericReview.passage_id == passage.id,
        )
    )
    if head is None or (
        head.id != observed.get("review_id")
        or head.document_id != passage.document_id
        or head.revision != 4
        or head.status != "approved"
        or head.kind != "confirm_original"
        or head.proposed_by != "business-manager"
        or head.reviewed_by != "business-reviewer"
        or head.proposed_by == head.reviewed_by
        or head.reviewed_at is None
        or head.original_text != passage.text
        or head.corrected_text != passage.text
        or head.corrections != []
        or head.source_binding != observed.get("source_binding")
        or head.source_binding.get("expected_source_sha256") != passage.source_sha256
        or head.source_binding.get("expected_text_sha256") != passage.text_sha256
        or head.source_binding.get("expected_document_version") != passage.document_version
        or head.source_binding.get("expected_parser_version") != passage.parser_version
        or head.source_binding.get("expected_locator_sha256")
        != document_parse_result_hash(passage.native_locator)
    ):
        raise RuntimeError("numeric review acceptance persisted head differs from source or review")
    events = list(
        await session.scalars(
            select(KnowledgeNumericReviewEvent)
            .where(
                KnowledgeNumericReviewEvent.review_id == head.id,
            )
            .order_by(KnowledgeNumericReviewEvent.revision)
        )
    )
    if [(row.revision, row.action, row.actor) for row in events] != [
        (1, "propose", "business-manager"),
        (2, "reject", "business-reviewer"),
        (3, "propose", "business-manager"),
        (4, "approve", "business-reviewer"),
    ]:
        raise RuntimeError("numeric review acceptance revision history differs")
    for item in events:
        if (
            document_parse_result_hash(item.snapshot) != item.snapshot_sha256
            or item.document_id != passage.document_id
            or item.passage_id != passage.id
            or item.snapshot.get("source_binding") != head.source_binding
            or item.snapshot.get("original_text") != passage.text
            or item.snapshot.get("current", {}).get("revision") != item.revision
        ):
            raise RuntimeError("numeric review acceptance snapshot binding or hash differs")
    if [row.snapshot_sha256 for row in reversed(events)] != observed.get("history_hashes"):
        raise RuntimeError("numeric review acceptance browser history differs from database")
    receipts = list(
        await session.scalars(
            select(CommandReceipt).where(
                CommandReceipt.target == passage.id,
                CommandReceipt.command_type.in_(
                    (
                        "knowledge.numeric-review.propose.v1",
                        "knowledge.numeric-review.decide.v1",
                    )
                ),
            )
        )
    )
    if len(receipts) != 4 or sorted(
        row.response_body.get("current", {}).get("revision", -1) for row in receipts
    ) != [1, 2, 3, 4]:
        raise RuntimeError("numeric review acceptance receipt count or revision differs")
    if not any(
        row.replay_count >= 1 and row.response_body["current"]["revision"] == 1 for row in receipts
    ):
        raise RuntimeError("numeric review acceptance did not persist the original receipt replay")
    domain_events = list(
        await session.scalars(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == passage.document_id,
                DomainEvent.event_type.like("knowledge.numeric-review.%"),
            )
        )
    )
    if sorted((row.payload.get("revision", -1), row.event_type) for row in domain_events) != [
        (1, "knowledge.numeric-review.propose"),
        (2, "knowledge.numeric-review.reject"),
        (3, "knowledge.numeric-review.propose"),
        (4, "knowledge.numeric-review.approve"),
    ]:
        raise RuntimeError("numeric review acceptance domain events differ")
    return {
        "passage_id": passage.id,
        "review_id": head.id,
        "revision": head.revision,
        "status": head.status,
        "kind": head.kind,
        "history_events": len(events),
        "command_receipts": len(receipts),
        "domain_events": len(domain_events),
        "original_text_sha256": passage.text_sha256,
        "source_sha256": passage.source_sha256,
        "independent_reviewer_verified": True,
        "replay_verified": True,
        "snapshots_rehashed_from_database": True,
        "boundary": "Synthetic PDF software review; no field or expert accuracy qualification",
    }
