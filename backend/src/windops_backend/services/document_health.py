"""Scope-filtered queue observations from the authoritative document/outbox ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from windops_backend.document_parse_process import PARSE_TIMEOUT_SECONDS
from windops_backend.knowledge_graph.domain import GraphAccessPolicy
from windops_backend.models import KnowledgeDocument
from windops_backend.outbox import OUTBOX_LEASE
from windops_backend.services.knowledge_access import knowledge_document_query
from windops_backend.services.knowledge_contracts import KNOWLEDGE_DOCUMENT_PARSE_REQUESTED
from windops_backend.storage import OutboxEvent

RECENT_FAILURE_LIMIT = 10
BACKLOG_WARNING_SECONDS = 300
PUBLIC_FAILURE_CODES = frozenset(
    {
        "TimeoutError",
        "DocumentParseFailure",
        "DocumentParserIdentityMismatch",
        "DocumentParseAttemptsExhausted",
        "ValueError",
    }
)


def _age(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return max(0, int((now - normalized).total_seconds()))


async def document_queue_health(
    session: AsyncSession, policy: GraphAccessPolicy, *, now: datetime | None = None
) -> dict[str, Any]:
    """Aggregate in SQL; never load all jobs or include another principal's jobs.

    These observations do not establish worker liveness. Expired leases remain
    recoverable by the existing relay; reading this endpoint never retries jobs.
    """
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("queue observation time must be timezone-aware")
    visible = knowledge_document_query(policy).with_only_columns(KnowledgeDocument.id).subquery()
    source = OutboxEvent.__table__.join(
        KnowledgeDocument, KnowledgeDocument.id == OutboxEvent.aggregate_id
    ).join(visible, visible.c.id == KnowledgeDocument.id)
    parse_events = and_(
        OutboxEvent.event_type == KNOWLEDGE_DOCUMENT_PARSE_REQUESTED,
        OutboxEvent.aggregate_type == "knowledge_document",
    )
    unfinished = KnowledgeDocument.ingestion_status.in_(("pending_parse", "parsing"))
    waiting = and_(unfinished, OutboxEvent.status.in_(("pending", "dispatched", "failed")))
    running = and_(unfinished, OutboxEvent.status == "processing")
    expired = and_(
        running,
        or_(
            OutboxEvent.lease_expires_at <= observed_at,
            and_(
                OutboxEvent.lease_expires_at.is_(None),
                OutboxEvent.claimed_at < observed_at - OUTBOX_LEASE,
            ),
        ),
    )
    timed_out = and_(
        running,
        OutboxEvent.claimed_at <= observed_at - timedelta(seconds=PARSE_TIMEOUT_SECONDS),
    )
    recent_failure = and_(
        OutboxEvent.status == "failed",
        OutboxEvent.processed_at >= observed_at - timedelta(hours=24),
    )
    conditions = {
        "waiting": waiting,
        "running": running,
        "retrying": and_(waiting, OutboxEvent.status == "failed"),
        "expired_leases": expired,
        "overdue_running": timed_out,
        "failed_last_24h": and_(
            recent_failure, KnowledgeDocument.ingestion_status == "parse_failed"
        ),
    }
    row = (
        await session.execute(
            select(
                *[
                    func.coalesce(func.sum(case((condition, 1), else_=0)), 0).label(name)
                    for name, condition in conditions.items()
                ],
                func.min(case((waiting, OutboxEvent.created_at))).label("oldest_waiting"),
                func.min(case((running, OutboxEvent.claimed_at))).label("oldest_running"),
            )
            .select_from(source)
            .where(parse_events)
        )
    ).one()
    failures = (
        await session.execute(
            select(
                KnowledgeDocument.id,
                OutboxEvent.last_error,
                OutboxEvent.attempts,
                OutboxEvent.processed_at,
                KnowledgeDocument.ingestion_status,
            )
            .select_from(source)
            .where(parse_events, recent_failure)
            .order_by(OutboxEvent.processed_at.desc(), OutboxEvent.id)
            .limit(RECENT_FAILURE_LIMIT)
        )
    ).all()
    counts = {name: int(row._mapping[name]) for name in conditions}
    oldest_waiting = _age(row.oldest_waiting, observed_at)
    return {
        "observed_at": observed_at.isoformat(),
        "scope": "authorized_documents",
        **counts,
        "oldest_waiting_age_seconds": oldest_waiting,
        "oldest_running_age_seconds": _age(row.oldest_running, observed_at),
        "backlogged": oldest_waiting is not None and oldest_waiting >= BACKLOG_WARNING_SECONDS,
        "backlog_warning_seconds": BACKLOG_WARNING_SECONDS,
        "execution_timeout_seconds": PARSE_TIMEOUT_SECONDS,
        "recent_failures": [
            {
                "document_id": item.id,
                "error_code": item.last_error
                if item.last_error in PUBLIC_FAILURE_CODES
                else "DocumentParseError",
                "attempts": item.attempts,
                "age_seconds": _age(item.processed_at, observed_at),
                "terminal": item.ingestion_status == "parse_failed",
            }
            for item in failures
        ],
        "recent_failure_limit": RECENT_FAILURE_LIMIT,
    }
