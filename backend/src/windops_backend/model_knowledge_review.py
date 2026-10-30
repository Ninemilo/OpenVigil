"""Mutable review heads and immutable, source-bound revision snapshots."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from windops_backend.model_base import JSON_VALUE, Base


class KnowledgeNumericReview(Base):
    __tablename__ = "knowledge_numeric_reviews"
    __table_args__ = (
        UniqueConstraint("passage_id", name="uq_numeric_review_passage"),
        UniqueConstraint("id", "document_id", "passage_id", name="uq_numeric_review_identity"),
        CheckConstraint("revision >= 1", name="ck_numeric_review_revision"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','withdrawn')",
            name="ck_numeric_review_status",
        ),
        CheckConstraint(
            "kind IN ('confirm_original','correct_values')", name="ck_numeric_review_kind"
        ),
        CheckConstraint(
            "status != 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND reviewed_by != proposed_by)",
            name="ck_numeric_review_independent_approval",
        ),
        Index("ix_numeric_review_document", "document_id", "passage_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), nullable=False)
    passage_id: Mapped[str] = mapped_column(ForeignKey("knowledge_passages.id"), nullable=False)
    source_binding: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    corrections: Mapped[list[dict[str, Any]]] = mapped_column(JSON_VALUE, nullable=False)
    corrected_text: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(160))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(Text)


class KnowledgeNumericReviewEvent(Base):
    __tablename__ = "knowledge_numeric_review_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id", "document_id", "passage_id"],
            [
                "knowledge_numeric_reviews.id",
                "knowledge_numeric_reviews.document_id",
                "knowledge_numeric_reviews.passage_id",
            ],
        ),
        UniqueConstraint("review_id", "revision", name="uq_numeric_review_event_revision"),
        CheckConstraint("revision >= 1", name="ck_numeric_review_event_revision"),
        CheckConstraint(
            "action IN ('propose','approve','reject','withdraw')",
            name="ck_numeric_review_event_action",
        ),
        Index("ix_numeric_review_event_history", "passage_id", "revision"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_id: Mapped[str] = mapped_column(String(36), nullable=False)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    passage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


# Metadata-created SQLite test databases enforce the same append-only history
# invariant as the explicit migration. Static SQL has no caller interpolation.
for operation in ("UPDATE", "DELETE"):
    event.listen(
        KnowledgeNumericReviewEvent.__table__,
        "after_create",
        cast(Callable[[str], DDL], DDL)(
            f"CREATE TRIGGER numeric_review_event_no_{operation.lower()} BEFORE {operation} "
            "ON knowledge_numeric_review_events BEGIN "
            "SELECT RAISE(ABORT, 'numeric review events are immutable'); END"
        ).execute_if(dialect="sqlite"),
    )
