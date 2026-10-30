"""Real SQLite/API queue observations; worker/Redis health is a separate gate."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from test_document_parse_jobs import enable_parser, protocol_result, queued_document
from windops_backend.api import knowledge as knowledge_api
from windops_backend.knowledge_graph.domain import GraphAccessPolicy
from windops_backend.models import KnowledgeDocument, Tenant
from windops_backend.services import document_parse
from windops_backend.services.document_health import document_queue_health
from windops_backend.services.knowledge_contracts import KNOWLEDGE_DOCUMENT_PARSE_REQUESTED
from windops_backend.storage import OutboxEvent

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


async def add_job(
    app,
    identity,
    *,
    status="pending",
    document_status="pending_parse",
    age=400,
    lease_age=None,
    error=None,
    failure_age=60,
    event_type=None,
):
    async with app.state.session_factory() as session, session.begin():
        session.add(
            KnowledgeDocument(
                id=identity,
                title="Synthetic queue unit fixture",
                document_type="inspection-report",
                body="",
                citation_uri=f"knowledge://{identity}",
                ingestion_status=document_status,
                data_scope="knowledge",
                created_at=NOW - timedelta(seconds=age),
            )
        )
        session.add(
            OutboxEvent(
                id=f"event-{identity}",
                event_type=event_type or KNOWLEDGE_DOCUMENT_PARSE_REQUESTED,
                aggregate_type="knowledge_document",
                aggregate_id=identity,
                status=status,
                created_at=NOW - timedelta(seconds=age),
                attempts=1,
                claimed_at=NOW - timedelta(seconds=age) if status == "processing" else None,
                lease_expires_at=NOW - timedelta(seconds=lease_age)
                if lease_age is not None
                else None,
                last_error=error,
                processed_at=NOW - timedelta(seconds=failure_age) if error else None,
            )
        )


async def observe(app, **scope):
    async with app.state.session_factory() as session:
        return await document_queue_health(session, GraphAccessPolicy.from_values(**scope), now=NOW)


async def test_queue_uses_actual_ledger_and_does_not_mutate_expired_jobs(app):
    await add_job(app, "waiting")
    await add_job(
        app, "running", status="processing", document_status="parsing", age=20, lease_age=-200
    )
    await add_job(
        app, "expired", status="processing", document_status="parsing", age=320, lease_age=20
    )
    await add_job(app, "retry", status="failed", error="TimeoutError", age=500)
    await add_job(
        app,
        "terminal",
        status="failed",
        document_status="parse_failed",
        error="DocumentParserIdentityMismatch",
    )
    await add_job(
        app,
        "old-failure",
        status="failed",
        document_status="parse_failed",
        error="ValueError",
        failure_age=90_000,
    )
    await add_job(app, "indexed", status="succeeded", document_status="ready")
    await add_job(app, "not-parser", event_type="unrelated.event")
    actual = await observe(app, unrestricted=True)
    assert {
        key: actual[key]
        for key in (
            "waiting",
            "running",
            "retrying",
            "expired_leases",
            "overdue_running",
            "failed_last_24h",
        )
    } == dict(
        waiting=2, running=2, retrying=1, expired_leases=1, overdue_running=1, failed_last_24h=1
    )
    assert actual["oldest_waiting_age_seconds"] == 500
    assert actual["oldest_running_age_seconds"] == 320
    assert actual["backlogged"] is True
    assert {item["document_id"] for item in actual["recent_failures"]} == {"retry", "terminal"}
    async with app.state.session_factory() as session:
        expired = await session.get(OutboxEvent, "event-expired")
        assert expired.status == "processing" and expired.attempts == 1


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"entity_ids": ("visible",), "data_scopes": ("structural",), "allow_global": True},
        {"entity_ids": ("missing",), "allow_global": True},
        {"tenant_ids": ("private-tenant",)},
    ],
)
async def test_missing_or_wrong_data_scope_observes_no_private_counts_or_failures(app, scope):
    await add_job(app, "visible", status="failed", error="TimeoutError")
    actual = await observe(app, **scope)
    assert actual["waiting"] == actual["retrying"] == actual["running"] == 0
    assert actual["oldest_waiting_age_seconds"] is None
    assert actual["recent_failures"] == [] and actual["backlogged"] is False


async def test_entity_scope_filters_every_metric_and_failure_before_aggregation(app):
    await add_job(app, "visible", age=12)
    await add_job(app, "hidden", status="failed", error="private-stack-with-token", age=600)
    actual = await observe(
        app, entity_ids=("visible",), data_scopes=("knowledge",), allow_global=True
    )
    assert actual["waiting"] == 1 and actual["retrying"] == 0
    assert actual["oldest_waiting_age_seconds"] == 12 and not actual["backlogged"]
    assert actual["recent_failures"] == []


async def test_tenant_boundary_excludes_other_tenants_from_all_queue_observations(app):
    async with app.state.session_factory() as session, session.begin():
        session.add(Tenant(id="health-private", name="Private synthetic health fixture"))
        session.add(Tenant(id="health-visible", name="Visible synthetic health fixture"))
    await add_job(app, "tenant-visible", age=10)
    await add_job(
        app, "tenant-private", status="failed", document_status="parse_failed", error="TimeoutError"
    )
    async with app.state.session_factory() as session, session.begin():
        visible = await session.get(KnowledgeDocument, "tenant-visible")
        private = await session.get(KnowledgeDocument, "tenant-private")
        visible.tenant_id = "health-visible"
        private.tenant_id = "health-private"
    actual = await observe(app, tenant_ids=("health-visible",), data_scopes=("knowledge",))
    assert actual["waiting"] == 1 and actual["oldest_waiting_age_seconds"] == 10
    assert actual["failed_last_24h"] == 0 and actual["recent_failures"] == []


async def test_failure_details_are_bounded_and_do_not_echo_arbitrary_error_text(app):
    for index in range(14):
        await add_job(
            app,
            f"failure-{index:02}",
            status="failed",
            document_status="parse_failed",
            error="postgresql://private-secret",
            failure_age=index,
        )
    actual = await observe(app, unrestricted=True)
    assert actual["failed_last_24h"] == 14
    assert len(actual["recent_failures"]) == actual["recent_failure_limit"] == 10
    assert [item["document_id"] for item in actual["recent_failures"]] == [
        f"failure-{n:02}" for n in range(10)
    ]
    assert all(
        item["error_code"] == "DocumentParseError" and item["terminal"]
        for item in actual["recent_failures"]
    )


async def test_completed_ledger_updates_observation_without_inventing_worker_health(app, client):
    await add_job(
        app, "recover", status="processing", document_status="parsing", age=500, lease_age=10
    )
    assert (await observe(app, unrestricted=True))["expired_leases"] == 1
    async with app.state.session_factory() as session, session.begin():
        event = await session.get(OutboxEvent, "event-recover")
        document = await session.get(KnowledgeDocument, "recover")
        event.status = "succeeded"
        event.lease_expires_at = None
        document.ingestion_status = "pending"
    actual = await observe(app, unrestricted=True)
    assert actual["expired_leases"] == actual["running"] == actual["waiting"] == 0
    response = await client.get("/api/v1/knowledge/parser-capabilities")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["worker_health_verified"] is False
    assert response.json()["queue"]["scope"] == "authorized_documents"


async def test_capabilities_api_uses_caller_scope_and_denies_unauthenticated_reads(app):
    await add_job(
        app, "hidden", status="failed", document_status="parse_failed", error="TimeoutError"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthenticated = await client.get("/api/v1/knowledge/parser-capabilities")
        assert unauthenticated.status_code == 401
        response = await client.get(
            "/api/v1/knowledge/parser-capabilities",
            headers={
                "X-WindOps-Test-Principal": "scoped-reader",
                "X-WindOps-Test-Role": "operations_manager",
                "X-WindOps-Test-Tenant-Ids": "tenant-east-china",
                "X-WindOps-Test-Data-Scopes": "knowledge",
            },
        )
    assert response.status_code == 200
    assert response.json()["queue"]["failed_last_24h"] == 0
    assert response.json()["queue"]["recent_failures"] == []


async def test_naive_observation_time_is_rejected(app):
    async with app.state.session_factory() as session:
        with pytest.raises(ValueError, match="timezone-aware"):
            await document_queue_health(
                session,
                GraphAccessPolicy.from_values(unrestricted=True),
                now=NOW.replace(tzinfo=None),
            )


async def test_actual_expired_claim_recovery_clears_health_warning(app, client, monkeypatch):
    payload, event_id = await queued_document(app, client)
    factory = app.state.session_factory
    async with factory() as session, session.begin():
        event = await session.get(OutboxEvent, event_id)
        document = await session.get(KnowledgeDocument, payload["document_id"])
        event.status = "processing"
        event.claim_token = "expired-worker"
        event.claimed_at = datetime.now(UTC) - timedelta(seconds=400)
        event.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        document.ingestion_status = "parsing"
    async with factory() as session:
        before = await document_queue_health(
            session, GraphAccessPolicy.from_values(unrestricted=True)
        )
    assert before["expired_leases"] == before["overdue_running"] == 1
    assert (
        event_id,
        KNOWLEDGE_DOCUMENT_PARSE_REQUESTED,
    ) in await document_parse.recoverable_document_parse_events(factory)

    async def declared_parser_wire(content, **_kwargs):
        return protocol_result(content)

    monkeypatch.setattr(document_parse, "parse_document_subprocess", declared_parser_wire)
    assert await document_parse.process_document_parse_event(
        factory, app.state.settings, app.state.artifact_verifier, event_id
    )
    async with factory() as session:
        after = await document_queue_health(
            session, GraphAccessPolicy.from_values(unrestricted=True)
        )
        event = await session.get(OutboxEvent, event_id)
        assert event.status == "succeeded" and event.claim_token is None
    assert after["expired_leases"] == after["overdue_running"] == after["running"] == 0


@pytest.mark.parametrize("age,expected", [(10, 0), (300, 0), (301, 1)])
async def test_legacy_null_lease_uses_the_same_expiry_rule_as_atomic_claim(app, age, expected):
    await add_job(app, "legacy-null", status="processing", document_status="parsing", age=age)
    assert (await observe(app, unrestricted=True))["expired_leases"] == expected


@pytest.mark.parametrize(
    "worker_status,heartbeat,runtime,expected",
    [
        ("ready", True, True, True),
        ("ready", False, True, False),
        ("ready", True, False, False),
        ("ready", 1, True, False),
        ("ready", True, 1, False),
        ("starting", True, False, False),
        ("offline", False, False, False),
        ("degraded", True, False, False),
        ("version_mismatch", True, True, False),
        ("unavailable", False, False, False),
    ],
)
async def test_api_requires_current_worker_evidence_in_addition_to_configuration(
    app, client, monkeypatch, worker_status, heartbeat, runtime, expected
):
    enable_parser(app)
    redis_handle = object()
    monkeypatch.setattr(app.state, "redis_client", redis_handle)
    observed = {
        "status": worker_status,
        "heartbeat_verified": heartbeat,
        "runtime_ready": runtime,
        "observed_at": NOW.isoformat(),
        "last_heartbeat_age_seconds": 1,
        "heartbeat_ttl_seconds": 45,
        "reason_codes": [],
    }

    async def read_worker(settings, redis_client):
        assert settings is app.state.settings and redis_client is redis_handle
        return observed

    monkeypatch.setattr(knowledge_api, "read_document_worker_health", read_worker)
    response = await client.get("/api/v1/knowledge/parser-capabilities")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["worker"] == observed
    assert response.json()["worker_health_verified"] is expected
    assert response.json()["queue"]["scope"] == "authorized_documents"


async def test_unconfigured_api_never_claims_ready_from_a_stray_worker_record(
    app, client, monkeypatch
):
    async def read_worker(*_args):
        return {"status": "ready", "heartbeat_verified": True, "runtime_ready": True}

    monkeypatch.setattr(knowledge_api, "read_document_worker_health", read_worker)
    response = await client.get("/api/v1/knowledge/parser-capabilities")
    assert response.json()["docling_pdf_configured"] is False
    assert response.json()["worker_health_verified"] is False
