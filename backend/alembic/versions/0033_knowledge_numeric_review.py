"""Add source-bound numeric review heads and append-only action snapshots.

Revision ID: 0033_knowledge_numeric_review
Revises: 0032_structural_workflow
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033_knowledge_numeric_review"
down_revision = "0032_structural_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "knowledge_numeric_reviews",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "document_id", sa.String(64), sa.ForeignKey("knowledge_documents.id"), nullable=False
        ),
        sa.Column(
            "passage_id", sa.String(64), sa.ForeignKey("knowledge_passages.id"), nullable=False
        ),
        sa.Column("source_binding", json_type, nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("corrections", json_type, nullable=False),
        sa.Column("corrected_text", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.String(160), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(160), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("passage_id", name="uq_numeric_review_passage"),
        sa.UniqueConstraint("id", "document_id", "passage_id", name="uq_numeric_review_identity"),
        sa.CheckConstraint("revision >= 1", name="ck_numeric_review_revision"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','withdrawn')",
            name="ck_numeric_review_status",
        ),
        sa.CheckConstraint(
            "kind IN ('confirm_original','correct_values')", name="ck_numeric_review_kind"
        ),
        sa.CheckConstraint(
            "status != 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND reviewed_by != proposed_by)",
            name="ck_numeric_review_independent_approval",
        ),
    )
    op.create_index(
        "ix_numeric_review_document", "knowledge_numeric_reviews", ["document_id", "passage_id"]
    )
    op.create_table(
        "knowledge_numeric_review_events",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("review_id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("passage_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("actor", sa.String(160), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("snapshot", json_type, nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_id", "document_id", "passage_id"],
            [
                "knowledge_numeric_reviews.id",
                "knowledge_numeric_reviews.document_id",
                "knowledge_numeric_reviews.passage_id",
            ],
        ),
        sa.UniqueConstraint("review_id", "revision", name="uq_numeric_review_event_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_numeric_review_event_revision"),
        sa.CheckConstraint(
            "action IN ('propose','approve','reject','withdraw')",
            name="ck_numeric_review_event_action",
        ),
    )
    op.create_index(
        "ix_numeric_review_event_history",
        "knowledge_numeric_review_events",
        ["passage_id", "revision"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION windops_numeric_review_event_immutable() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'numeric review events are immutable' USING ERRCODE = '23000';
            END $$""")
        op.execute("""CREATE TRIGGER numeric_review_event_immutable
            BEFORE UPDATE OR DELETE ON knowledge_numeric_review_events
            FOR EACH ROW EXECUTE FUNCTION windops_numeric_review_event_immutable()""")
    elif op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER numeric_review_event_no_{operation.lower()} BEFORE {operation} "
                "ON knowledge_numeric_review_events BEGIN "
                "SELECT RAISE(ABORT, 'numeric review events are immutable'); END"
            )


def downgrade() -> None:
    op.drop_table("knowledge_numeric_review_events")
    op.drop_table("knowledge_numeric_reviews")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION windops_numeric_review_event_immutable()")
