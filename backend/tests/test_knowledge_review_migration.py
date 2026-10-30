"""Execute the new migration on an isolated SQLite predecessor schema."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from windops_backend.models import (
    Base,
    KnowledgeDocument,
    KnowledgeNumericReview,
    KnowledgeNumericReviewEvent,
    KnowledgePassage,
)


def test_numeric_review_migration_matches_models_and_enforces_append_only_history(tmp_path):
    path = Path(__file__).parents[1] / "alembic/versions/0033_knowledge_numeric_review.py"
    spec = importlib.util.spec_from_file_location("numeric_review_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0033_knowledge_numeric_review"
    assert module.down_revision == "0032_structural_workflow"
    tables = {"knowledge_numeric_reviews", "knowledge_numeric_review_events"}
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            Base.metadata.create_all(
                connection,
                tables=[
                    table for name, table in Base.metadata.tables.items() if name not in tables
                ],
            )
            before = set(inspect(connection).get_table_names())
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            inspector = inspect(connection)
            assert set(inspector.get_table_names()) == before | tables
            for model in (KnowledgeNumericReview, KnowledgeNumericReviewEvent):
                assert {
                    column["name"] for column in inspector.get_columns(model.__tablename__)
                } == set(model.__table__.columns.keys())
                assert {
                    entry["name"] for entry in inspector.get_check_constraints(model.__tablename__)
                } == {
                    item.name
                    for item in model.__table__.constraints
                    if item.__class__.__name__ == "CheckConstraint"
                }
                assert {
                    entry["name"] for entry in inspector.get_unique_constraints(model.__tablename__)
                } == {
                    item.name
                    for item in model.__table__.constraints
                    if item.__class__.__name__ == "UniqueConstraint"
                }
            # Real parent rows establish the migration's FK constraints. API
            # tests separately import and verify the original PDF bytes.
            now = datetime.now(UTC)
            connection.execute(
                KnowledgeDocument.__table__.insert().values(
                    id="doc",
                    title="Migration fixture",
                    document_type="manual",
                    body="123.45",
                    citation_uri="windops://knowledge/documents/doc",
                )
            )
            connection.execute(
                KnowledgePassage.__table__.insert().values(
                    id="passage",
                    document_id="doc",
                    ordinal=0,
                    document_version="1",
                    source_sha256="a" * 64,
                    text_sha256="b" * 64,
                    parser_version="fixture",
                    native_locator={},
                    char_start=0,
                    char_end=6,
                    text="123.45",
                )
            )
            connection.execute(
                KnowledgeNumericReview.__table__.insert().values(
                    id="review",
                    document_id="doc",
                    passage_id="passage",
                    source_binding={},
                    original_text="123.45",
                    revision=1,
                    status="pending",
                    kind="confirm_original",
                    corrections=[],
                    corrected_text="123.45",
                    reason="test",
                    proposed_by="author",
                    proposed_at=now,
                )
            )
            connection.execute(
                KnowledgeNumericReviewEvent.__table__.insert().values(
                    id="event",
                    review_id="review",
                    document_id="doc",
                    passage_id="passage",
                    revision=1,
                    action="propose",
                    actor="author",
                    occurred_at=now,
                    reason="test",
                    snapshot={},
                    snapshot_sha256="a" * 64,
                )
            )
            for sql in (
                "UPDATE knowledge_numeric_review_events SET reason='tampered'",
                "DELETE FROM knowledge_numeric_review_events",
            ):
                with pytest.raises(IntegrityError, match="immutable"):
                    connection.execute(text(sql))
            with pytest.raises(IntegrityError, match="independent_approval"):
                connection.execute(
                    text(
                        "UPDATE knowledge_numeric_reviews SET status='approved', "
                        "reviewed_by='author', reviewed_at=CURRENT_TIMESTAMP"
                    )
                )
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
            assert set(inspect(connection).get_table_names()) == before
    finally:
        engine.dispose()
