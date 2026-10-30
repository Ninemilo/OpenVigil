"""Real SQLite recovery ledger/relay tests; Redis publication is a declared transport stub."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from windops_backend import workers
from windops_backend.models import KnowledgeDocument
from windops_backend.outbox import (
    OUTBOX_DISPATCH_VISIBILITY,
    OutboxLeaseLostError,
    assert_current_claim,
    claim_event,
)
from windops_backend.services.document_parse import (
    recoverable_document_parse_events,
    reserve_document_parse_recoveries,
)
from windops_backend.services.knowledge_contracts import KNOWLEDGE_DOCUMENT_PARSE_REQUESTED
from windops_backend.storage import OutboxEvent


async def recovery_job(app, identity, *, status="failed", age=600):
    now = datetime.now(UTC)
    async with app.state.session_factory() as session, session.begin():
        session.add(
            KnowledgeDocument(
                id=identity,
                title="Synthetic recovery fixture",
                document_type="inspection-report",
                body="",
                citation_uri=f"knowledge://{identity}",
                ingestion_status="pending_parse" if status == "failed" else "parsing",
            )
        )
        session.add(
            OutboxEvent(
                id=f"event-{identity}",
                event_type=KNOWLEDGE_DOCUMENT_PARSE_REQUESTED,
                aggregate_type="knowledge_document",
                aggregate_id=identity,
                status=status,
                attempts=1,
                created_at=now - timedelta(seconds=age),
                dispatched_at=now - timedelta(seconds=age),
                claimed_at=now - timedelta(seconds=age),
                claim_token="old-worker" if status == "processing" else None,
                lease_expires_at=now - timedelta(seconds=1) if status == "processing" else None,
                last_error="TimeoutError" if status == "failed" else None,
                processed_at=now - timedelta(seconds=10) if status == "failed" else None,
            )
        )
    return f"event-{identity}"


@pytest.mark.parametrize("status", ["failed", "processing"])
async def test_relay_reserves_recovery_before_publish_and_does_not_flood(app, monkeypatch, status):
    event_id = await recovery_job(app, "recover", status=status)
    sent = []
    started = datetime.now(UTC)

    def publish(event_ids):
        # The independent connection can see the committed reservation before
        # the declared Redis boundary is called, not merely an ORM dirty value.
        path = app.state.settings.database_url.removeprefix("sqlite+aiosqlite:///")
        with sqlite3.connect(path) as connection:
            dispatched_at = connection.execute(
                "SELECT dispatched_at FROM outbox_events WHERE id = ?", (event_id,)
            ).fetchone()[0]
        assert datetime.fromisoformat(dispatched_at).replace(tzinfo=UTC) >= started
        sent.extend(event_ids)

    monkeypatch.setattr(workers, "get_settings", lambda: app.state.settings)
    monkeypatch.setattr(workers, "dispatch_document_parse_event_ids", publish)

    assert await workers.relay_pending_once() == 1
    assert await workers.relay_pending_once() == 0
    assert sent == [event_id]
    async with app.state.session_factory() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event.status == status and event.attempts == 1
        assert event.claim_token == ("old-worker" if status == "processing" else None)


async def test_recovery_batch_moves_past_the_first_fifty_stopped_jobs(app, monkeypatch):
    expected = {await recovery_job(app, f"job-{index:02}") for index in range(51)}
    sent = []
    monkeypatch.setattr(workers, "get_settings", lambda: app.state.settings)
    monkeypatch.setattr(workers, "dispatch_document_parse_event_ids", sent.extend)

    assert await workers.relay_pending_once() == 50
    assert await workers.relay_pending_once() == 1
    assert await workers.relay_pending_once() == 0
    assert len(sent) == 51 and set(sent) == expected


async def test_parallel_sqlite_relays_reserve_each_recovery_at_most_once(app):
    expected = {await recovery_job(app, f"parallel-{index:02}") for index in range(70)}
    results = await asyncio.gather(
        reserve_document_parse_recoveries(app.state.session_factory),
        reserve_document_parse_recoveries(app.state.session_factory),
    )
    first, second = ({identity for identity, _kind in result} for result in results)
    assert first.isdisjoint(second)
    assert first | second == expected
    assert sorted(map(len, results)) == [20, 50]


@pytest.mark.parametrize("publication_fails", [False, True])
async def test_failed_or_lost_publication_retries_only_after_visibility_expiry(
    app, monkeypatch, publication_fails
):
    event_id = await recovery_job(app, "publication")
    sent = []

    def publish(event_ids):
        sent.extend(event_ids)
        if publication_fails:
            raise ConnectionError("controlled Redis transport failure")

    monkeypatch.setattr(workers, "get_settings", lambda: app.state.settings)
    monkeypatch.setattr(workers, "dispatch_document_parse_event_ids", publish)
    if publication_fails:
        with pytest.raises(ConnectionError, match="controlled Redis"):
            await workers.relay_pending_once()
    else:
        assert await workers.relay_pending_once() == 1
    assert await workers.relay_pending_once() == 0
    assert sent == [event_id]
    async with app.state.session_factory() as session, session.begin():
        event = await session.get(OutboxEvent, event_id)
        assert event.status == "failed" and event.last_error == "TimeoutError"
        event.dispatched_at = datetime.now(UTC) - OUTBOX_DISPATCH_VISIBILITY - timedelta(seconds=1)
    monkeypatch.setattr(workers, "dispatch_document_parse_event_ids", sent.extend)
    assert await workers.relay_pending_once() == 1
    assert sent == [event_id, event_id]


@pytest.mark.parametrize(
    ("change", "eligible"),
    [
        ("fresh-claim", False),
        ("terminal-document", False),
        ("indexed-document", False),
        ("wrong-event", False),
        ("wrong-aggregate", False),
        ("fresh-null-lease", False),
        ("old-null-lease", True),
    ],
)
async def test_recovery_respects_current_claim_document_and_event_boundaries(app, change, eligible):
    event_id = await recovery_job(app, "boundaries", status="processing")
    async with app.state.session_factory() as session, session.begin():
        event = await session.get(OutboxEvent, event_id)
        document = await session.get(KnowledgeDocument, "boundaries")
        if change == "fresh-claim":
            event.lease_expires_at = datetime.now(UTC) + timedelta(seconds=300)
        elif change == "terminal-document":
            document.ingestion_status = "parse_failed"
        elif change == "indexed-document":
            document.ingestion_status = "indexed"
        elif change == "wrong-event":
            event.event_type = "unrelated.event"
        elif change == "wrong-aggregate":
            event.aggregate_type = "mission"
        else:
            event.lease_expires_at = None
            if change == "fresh-null-lease":
                event.claimed_at = datetime.now(UTC) - timedelta(seconds=30)
    expected = [(event_id, KNOWLEDGE_DOCUMENT_PARSE_REQUESTED)] if eligible else []
    assert await recoverable_document_parse_events(app.state.session_factory) == expected
    assert await reserve_document_parse_recoveries(app.state.session_factory) == expected


async def test_reservation_does_not_extend_or_overwrite_old_and_new_worker_fences(app):
    event_id = await recovery_job(app, "fencing", status="processing")
    factory = app.state.session_factory
    assert await reserve_document_parse_recoveries(factory)
    async with factory() as session, session.begin():
        with pytest.raises(OutboxLeaseLostError):
            await assert_current_claim(session, event_id, "old-worker")
        event = await claim_event(session, event_id)
        assert event is not None and event.claim_token != "old-worker"
        new_token = event.claim_token
        new_lease = event.lease_expires_at.replace(tzinfo=UTC)
    assert await reserve_document_parse_recoveries(factory) == []
    async with factory() as session, session.begin():
        await assert_current_claim(session, event_id, new_token)
        with pytest.raises(OutboxLeaseLostError):
            await assert_current_claim(session, event_id, "old-worker")
        event = await session.get(OutboxEvent, event_id)
        assert event.claim_token == new_token
        assert event.lease_expires_at.replace(tzinfo=UTC) == new_lease
        assert event.attempts == 2


@pytest.mark.parametrize("limit", [0, -1, 101])
async def test_recovery_rejects_unbounded_or_empty_batches(app, limit):
    with pytest.raises(ValueError, match="1 to 100"):
        await reserve_document_parse_recoveries(app.state.session_factory, limit=limit)
