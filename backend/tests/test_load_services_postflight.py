"""SQLite production services + real loopback HTTP, with a declared Bolt fixture.

The PDF parser wire and deterministic embedding fixture come from document tests.
The HTTP fixture is not MinIO; the Bolt adapter returns the actual production
GraphSnapshot's properties without executing Cypher. No real PG/Neo4j/OCR ran.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from minio import Minio
from sqlalchemy import event, select

from test_knowledge_passages import pdf_with_second_page_text
from test_load_document_evidence import indexed_document
from test_load_document_evidence import runtime as runtime  # noqa: F401
from windops_backend.knowledge_graph.domain import NodeType
from windops_backend.knowledge_graph.projection import build_knowledge_graph_snapshot
from windops_backend.models import KnowledgeDocument, ReadAccessAudit
from windops_backend.operations import load_services_postflight as module
from windops_backend.operations.load_dataset import USER_SCOPES, database_snapshot
from windops_backend.operations.load_services_settings import (
    SUBJECTS,
    USER_ROLES,
    ServicesEndpoints,
    services_settings,
)
from windops_backend.read_audit import build_read_audit_event, persist_read_audit_batch


@dataclass
class ObjectWire:
    port: int
    content: bytes
    requests: list
    tasks: set
    missing: int = 0
    status: int = 200
    content_type: str = "application/pdf"
    encoding: str | None = None
    hang: bool = False
    disconnected: int = 0


@pytest_asyncio.fixture
async def object_wire():
    wire = ObjectWire(0, pdf_with_second_page_text(), [], set())

    async def handle(reader, writer):
        task = asyncio.current_task()
        wire.tasks.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            wire.requests.append(request.split(b"\r\n")[0].decode())
            if wire.hang:
                await reader.read()
                return
            status = 404 if wire.missing else wire.status
            wire.missing = max(0, wire.missing - 1)
            body = wire.content if status == 200 else b""
            headers = (
                f"HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\n"
                f"Content-Type: {wire.content_type}\r\nConnection: close\r\n"
            )
            if wire.encoding is not None:
                headers += f"Content-Encoding: {wire.encoding}\r\n"
            writer.write(headers.encode() + b"\r\n" + body)
            await writer.drain()
        finally:
            wire.disconnected += 1
            writer.close()
            await writer.wait_closed()
            wire.tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    wire.port = server.sockets[0].getsockname()[1]
    try:
        yield wire
    finally:
        server.close()
        await server.wait_closed()
        for task in wire.tasks:
            task.cancel()
        await asyncio.gather(*wire.tasks, return_exceptions=True)


def settings_for(wire):
    return services_settings(
        role="api",
        endpoints=ServicesEndpoints(postgres=35431, redis=36379, minio=wire.port, neo4j=37687),
        password="a" * 64,
        delegation_secret="b" * 64,
        commit_sha="c" * 40,
    )


def audit_events(settings, *, when=None, aliases=tuple(USER_SCOPES)):
    return [
        build_read_audit_event(
            subject=SUBJECTS[alias],
            roles=[USER_ROLES[alias]],
            method="GET",
            endpoint=settings.api_prefix + "/knowledge/documents",
            query={"search": ["PRIVATE_QUERY"]},
            authorization={
                **settings.identity_scope_mappings[SUBJECTS[alias]].model_dump(),
                "unrestricted": False,
            },
            now=when,
        )
        for alias in aliases
    ]


async def seed_audit(runtime, settings):
    start = datetime.now(UTC) - timedelta(seconds=1)
    assert await persist_read_audit_batch(runtime.factory, audit_events(settings)) == 4
    return start


class Records:
    def __init__(self, rows):
        self.rows = rows

    async def single(self, *, strict):
        assert strict and len(self.rows) == 1
        return self.rows[0]

    def __aiter__(self):
        async def iterate():
            for row in self.rows:
                yield row

        return iterate()


class GraphWire:
    """Translate real projection properties at the Neo4j transport boundary only."""

    def __init__(self, snapshot):
        self.nodes = {
            node.uid: {
                **node.properties,
                "uid": node.uid,
                "nodeType": node.node_type.value,
                "entityId": node.entity_id,
                "projectionId": snapshot.projection_id,
            }
            for node in snapshot.nodes
        }
        self.edges = list(snapshot.relationships)
        self.calls = []
        self.missing = 0
        self.duplicate = False
        self.closed = False
        self.close_error = False
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.session_options = []

    def session(self, **options):
        self.session_options.append(options)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def begin_transaction(self, *, timeout):  # noqa: ASYNC109 - actual Neo4j SDK signature.
        assert 0 < timeout <= 5
        return self

    async def run(self, query, **parameters):
        # Installed Neo4j explicit transactions reject a Query wrapper.
        assert type(query) is str
        self.calls.append((query, copy.deepcopy(parameters)))
        logging.getLogger("neo4j.io").warning("PRIVATE_BOLT_WIRE_DETAILS")
        if "document_id" in parameters:
            count = sum(
                item.get("documentId") == parameters["document_id"] for item in self.nodes.values()
            )
            if self.missing:
                count = 0
                self.missing -= 1
            return Records([{"count": count}])
        rows = []
        for wanted in parameters["wanted"]:
            p, d = self.nodes.get(wanted["uid"], {}), self.nodes.get(wanted["document_uid"], {})
            row = {"wanted_uid": wanted["uid"], "nodes": int(bool(p))}
            row["edges"] = sum(
                edge.source_uid == wanted["document_uid"] and edge.target_uid == wanted["uid"]
                for edge in self.edges
            )
            mapping = {
                "uid": "uid",
                "entity_id": "entityId",
                "node_type": "nodeType",
                "projection_id": "projectionId",
                "document_id": "documentId",
                "version": "documentVersion",
                "source_sha256": "sourceSha256",
                "text_sha256": "textSha256",
                "tenant_id": "tenantId",
                "wind_farm_id": "windFarmId",
                "turbine_id": "turbineId",
                "ordinal": "ordinal",
                "page_number": "pageNumber",
                "char_start": "charStart",
                "char_end": "charEnd",
                "parser_version": "parserVersion",
                "citation_uri": "citationUri",
            }
            row.update({key: p.get(prop) for key, prop in mapping.items()})
            parent = {
                "parent_id": "entityId",
                "parent_version": "documentVersion",
                "parent_source_sha256": "artifactSha256",
                "parent_tenant_id": "tenantId",
                "parent_wind_farm_id": "windFarmId",
                "parent_turbine_id": "turbineId",
                "parent_projection_id": "projectionId",
                "parent_vectorized": "vectorized",
            }
            row.update({key: d.get(prop) for key, prop in parent.items()})
            for key, prop, size in (
                ("body", "body", "text_length"),
                ("locator", "nativeLocator", "locator_length"),
            ):
                value = p.get(prop)
                row[key] = value if isinstance(value, str) and len(value) == wanted[size] else None
            rows.append(row)
        if self.duplicate:
            rows.append(copy.deepcopy(rows[0]))
        return Records(rows[: parameters["row_limit"]])

    async def close(self):
        self.close_entered.set()
        await self.close_release.wait()
        self.closed = True
        logging.getLogger("neo4j.io").warning("PRIVATE_BOLT_CLOSE_DETAILS")
        if self.close_error:
            raise RuntimeError("PRIVATE_CLEANUP_ERROR")


async def graph_wire(runtime, monkeypatch):
    async with runtime.factory() as session:
        snapshot = await build_knowledge_graph_snapshot(session)
    wire = GraphWire(snapshot)

    def driver(uri, **options):
        assert uri == "bolt://127.0.0.1:37687"
        assert options["max_transaction_retry_time"] == 0
        assert options["max_connection_pool_size"] == 1
        return wire

    monkeypatch.setattr(module.AsyncGraphDatabase, "driver", driver)
    return wire


async def collect(runtime, settings, manifest, start, *, deadline=3):
    return await module.collect_postflight(
        settings,
        runtime.factory,
        manifest,
        started_at=start,
        expected_reads=dict.fromkeys(USER_SCOPES, 1),
        deadline_seconds=deadline,
    )


async def test_complete_sql_source_real_http_projection_schema_and_privacy(
    runtime, object_wire, monkeypatch, caplog
):
    spec, _ = await indexed_document(runtime)
    foreign, _ = await indexed_document(runtime, "foreign-b")
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)
    wire = await graph_wire(runtime, monkeypatch)
    before, statements = database_snapshot(runtime.database), []
    engine = runtime.factory.kw["bind"].sync_engine

    def observe(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", observe)
    try:
        # Exercise the SDKs this collector owns. Enabling global DEBUG also
        # enables SQLite's existing worker-thread SQL/parameter trace.
        with (
            caplog.at_level(logging.DEBUG, logger="neo4j.io"),
            caplog.at_level(logging.DEBUG, logger="httpx"),
            caplog.at_level(logging.DEBUG, logger="httpcore"),
        ):
            report = await collect(runtime, settings, [spec], start)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert report["passed"] and report["cleanup_passed"] and report["errors"] == []
    assert report["objects"]["verified"] and report["graph"]["verified"]
    assert report["objects"]["documents"][0]["sha256"] == spec.artifact_sha256
    assert report["audit"]["observed_counts"] == dict.fromkeys(USER_SCOPES, 1)
    assert wire.closed and wire.session_options[0]["default_access_mode"] == "READ"
    assert database_snapshot(runtime.database) == before
    assert all(sql.lstrip().upper().startswith(("SELECT", "BEGIN")) for sql, _ in statements)
    assert foreign.document_id not in repr(statements) + repr(wire.calls)
    assert len(wire.calls) == 2
    assert len(object_wire.requests) == object_wire.disconnected == 1
    request_target = object_wire.requests[0].split(" ")[1]
    signed = parse_qs(urlsplit(request_target).query)
    assert signed["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "X-Amz-Signature" in signed
    public = json.dumps(report) + caplog.text
    for marker in (
        spec.document_id,
        spec.tenant_id,
        "PRIVATE_",
        "Declared wire fixture",
        "X-Amz-Signature",
        settings.minio_secret_key.get_secret_value(),
    ):
        assert marker not in public


async def test_query_only_checks_four_real_subjects_without_connecting_services(
    runtime, object_wire, monkeypatch
):
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no service connection allowed")

    monkeypatch.setattr(module.AsyncGraphDatabase, "driver", forbidden)
    monkeypatch.setattr(Minio, "presigned_get_object", forbidden)
    report = await collect(runtime, settings, [], start)
    assert report["passed"]
    assert report["objects"] == {"required": False, "verified": False, "documents": []}
    assert not report["graph"]["required"] and not object_wire.requests


async def test_bounded_wait_for_audit_object_and_projection(runtime, object_wire, monkeypatch):
    spec, _ = await indexed_document(runtime)
    settings = settings_for(object_wire)
    start = datetime.now(UTC) - timedelta(seconds=1)
    wire = await graph_wire(runtime, monkeypatch)
    object_wire.missing, wire.missing = 1, 1

    async def persist_later():
        await asyncio.sleep(0.1)
        await persist_read_audit_batch(runtime.factory, audit_events(settings))

    task = asyncio.create_task(persist_later())
    try:
        report = await collect(runtime, settings, [spec], start)
    finally:
        await task
    assert report["passed"] and len(object_wire.requests) == 2
    assert len(wire.calls) == 3 and wire.closed


@pytest.mark.parametrize(
    "damage", ["wrong_scope", "missing_schema", "duplicate_id", "wrong_method"]
)
async def test_persisted_audit_tampering_cannot_turn_green(runtime, object_wire, damage):
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)
    async with runtime.factory() as session, session.begin():
        row = await session.scalar(
            select(ReadAccessAudit).where(ReadAccessAudit.subject == SUBJECTS["a1"])
        )
        if damage == "wrong_scope":
            changed = copy.deepcopy(row.query)
            # Same scope counts, a different tenant fingerprint.
            changed["authorization"]["scope_fingerprint_sha256"] = "f" * 64
            row.query = changed
        elif damage == "missing_schema":
            row.query = {"authorization": row.query["authorization"]}
        elif damage == "wrong_method":
            row.method = "POST"
        else:
            session.add(
                ReadAccessAudit(
                    id=row.id,
                    accessed_at=row.accessed_at + timedelta(microseconds=1),
                    subject=row.subject,
                    role=row.role,
                    method=row.method,
                    endpoint=row.endpoint,
                    query=row.query,
                )
            )
    report = await collect(runtime, settings, [], start)
    assert not report["passed"]
    assert report["errors"] == ["read_audit_scope_or_identity_invalid"]


async def test_audit_wrong_subject_old_future_or_missing_rows_do_not_count(runtime, object_wire):
    settings = settings_for(object_wire)
    start = datetime.now(UTC) - timedelta(seconds=1)
    old = audit_events(settings, when=start - timedelta(seconds=1))
    future = audit_events(settings, when=datetime.now(UTC) + timedelta(hours=1))
    foreign = [replace(item, subject="foreign-" + item.subject) for item in audit_events(settings)]
    await persist_read_audit_batch(runtime.factory, old + future + foreign)
    report = await collect(runtime, settings, [], start, deadline=0.15)
    assert not report["passed"] and report["cleanup_passed"]
    assert report["audit"]["observed_counts"] == dict.fromkeys(USER_SCOPES, 0)
    assert report["errors"] == ["postflight_deadline_exceeded"]


@pytest.mark.parametrize("damage", ["bytes", "type", "encoding", "redirect"])
async def test_real_http_object_negative(runtime, object_wire, monkeypatch, damage):
    spec, _ = await indexed_document(runtime)
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)
    wire = await graph_wire(runtime, monkeypatch)
    if damage == "bytes":
        object_wire.content = b"X" + object_wire.content[1:]
    elif damage == "type":
        object_wire.content_type = "text/plain"
    elif damage == "encoding":
        object_wire.encoding = "gzip"
    else:
        object_wire.status = 302
    report = await collect(runtime, settings, [spec], start)
    assert not report["passed"] and wire.closed
    assert report["errors"] == [
        "postflight_object_sha_mismatch"
        if damage == "bytes"
        else "postflight_object_response_invalid"
    ]
    assert len(object_wire.requests) == 1


@pytest.mark.parametrize(
    "damage", ["scope", "body_same_hash", "locator", "extra", "duplicate", "edge"]
)
async def test_projection_identity_content_and_cardinality_fail_closed(
    runtime, object_wire, monkeypatch, damage
):
    spec, _ = await indexed_document(runtime)
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)
    wire = await graph_wire(runtime, monkeypatch)
    uid, piece = next(
        (uid, node)
        for uid, node in wire.nodes.items()
        if node["nodeType"] == NodeType.KNOWLEDGE_PASSAGE
    )
    if damage == "scope":
        piece["tenantId"] = "foreign-private-tenant"
    elif damage == "body_same_hash":
        piece["body"] = "X" + piece["body"][1:]
    elif damage == "locator":
        piece["nativeLocator"] = "[]"
    elif damage == "extra":
        wire.nodes[uid + "-extra"] = copy.deepcopy(piece)
    elif damage == "duplicate":
        wire.duplicate = True
    else:
        wire.edges.clear()
    report = await collect(runtime, settings, [spec], start, deadline=0.3)
    assert not report["passed"] and wire.closed
    assert not report["graph"]["verified"] and report["errors"]


async def test_pending_or_sql_scope_mismatch_does_not_read_external_sources(
    runtime, object_wire, monkeypatch
):
    spec, event_id = await runtime.register()
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)
    wire = await graph_wire(runtime, monkeypatch)
    pending = await collect(runtime, settings, [spec], start, deadline=0.1)
    assert not pending["passed"] and not object_wire.requests and not wire.calls
    assert await runtime.parse(event_id)
    assert await runtime.index(spec)
    wrong = spec.model_copy(update={"tenant_id": "foreign-private-tenant"})
    failed = await collect(runtime, settings, [wrong], start)
    assert failed["errors"] == ["postflight_sql_source_invalid"]
    assert not object_wire.requests


async def test_source_path_scope_and_signing_never_discovers_region(
    runtime, object_wire, monkeypatch
):
    spec, _ = await indexed_document(runtime)
    settings = settings_for(object_wire)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("signing must not call the network")

    monkeypatch.setattr(Minio, "_execute", forbidden)
    uri = f"minio://{settings.minio_knowledge_bucket}/documents/{spec.document_id}/source.pdf"
    assert "X-Amz-Signature=" in module._signed_source(settings, spec, uri)
    for bad in (
        uri.replace(spec.document_id, "foreign"),
        uri + "/../x",
        uri + "%2fother",
        uri + "?secret=1",
    ):
        with pytest.raises(module._Failure, match="postflight_object_scope_invalid"):
            module._signed_source(settings, spec, bad)
    async with runtime.factory() as session, session.begin():
        row = await session.get(KnowledgeDocument, spec.document_id)
        row.artifact_uri = uri.replace(spec.document_id, "foreign")
    start = await seed_audit(runtime, settings)
    await graph_wire(runtime, monkeypatch)
    report = await collect(runtime, settings, [spec], start)
    assert not report["passed"] and not object_wire.requests


async def test_real_http_deadline_closes_socket_and_graph(runtime, object_wire, monkeypatch):
    spec, _ = await indexed_document(runtime)
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)
    wire = await graph_wire(runtime, monkeypatch)
    object_wire.hang = True
    report = await collect(runtime, settings, [spec], start, deadline=0.2)
    assert not report["passed"] and report["cleanup_passed"] and wire.closed
    async with asyncio.timeout(1):
        await asyncio.gather(*object_wire.tasks)
    assert object_wire.disconnected == len(object_wire.requests) == 1


async def test_cleanup_error_is_failure_not_green(runtime, object_wire, monkeypatch):
    spec, _ = await indexed_document(runtime)
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)
    wire = await graph_wire(runtime, monkeypatch)
    wire.close_error = True
    report = await collect(runtime, settings, [spec], start)
    assert wire.closed and not report["passed"] and not report["cleanup_passed"]
    assert report["errors"] == ["postflight_client_cleanup_failed"]
    assert "PRIVATE_CLEANUP_ERROR" not in json.dumps(report)


async def test_repeated_cancel_waits_for_both_owned_client_closures(
    runtime, object_wire, monkeypatch
):
    spec, _ = await indexed_document(runtime)
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)
    wire = await graph_wire(runtime, monkeypatch)
    wire.close_release.clear()
    http_entered, http_release, http_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = module.httpx.AsyncClient.aclose

    async def delayed_close(client):
        http_entered.set()
        await http_release.wait()
        await original(client)
        http_closed.set()

    monkeypatch.setattr(module.httpx.AsyncClient, "aclose", delayed_close)
    task = asyncio.create_task(collect(runtime, settings, [spec], start))
    try:
        async with asyncio.timeout(3):
            await wire.close_entered.wait()
            await http_entered.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done() and not wire.closed and not http_closed.is_set()
        wire.close_release.set()
        http_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert wire.closed and http_closed.is_set()
        assert not [
            item for item in asyncio.all_tasks() if item.get_name().startswith("postflight-owned-")
        ]
    finally:
        wire.close_release.set()
        http_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cleanup_has_hard_bound_and_reports_unfinished_close():
    closed = asyncio.Event()

    class StalledDriver:
        async def close(self):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    client = module.httpx.AsyncClient(trust_env=False)
    async with asyncio.timeout(6):
        assert not await module._close_clients(client, StalledDriver())
    assert client.is_closed and closed.is_set()
    assert not [
        task for task in asyncio.all_tasks() if task.get_name().startswith("postflight-owned-")
    ]


async def test_nonlocal_graph_and_object_origins_rejected_before_connection(
    runtime, object_wire, monkeypatch
):
    spec, _ = await indexed_document(runtime)
    settings = settings_for(object_wire)
    start = await seed_audit(runtime, settings)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no network or DNS allowed")

    monkeypatch.setattr(module.AsyncGraphDatabase, "driver", forbidden)
    for uri in (
        "bolt://example.invalid:7687",
        "neo4j://127.0.0.1:7687",
        "bolt://u:p@127.0.0.1:7687",
    ):
        settings.neo4j_uri = uri
        report = await collect(runtime, settings, [spec], start)
        assert not report["passed"] and report["errors"] == ["postflight_observation_failed"]
    settings.minio_endpoint = "example.invalid:9000"
    with pytest.raises(ValueError, match="explicit literal loopback"):
        module._signed_source(settings, spec, f"minio://bucket/documents/{spec.document_id}/s.pdf")
    assert not object_wire.requests


@pytest.mark.parametrize(
    "field", ["missing_subject", "bad_count", "future_start", "deadline", "global_scope"]
)
async def test_invalid_contract_rejected_before_sql_or_services(runtime, object_wire, field):
    settings = settings_for(object_wire)
    start, counts, deadline = datetime.now(UTC), dict.fromkeys(USER_SCOPES, 1), 2
    if field == "missing_subject":
        counts.pop("b2")
    elif field == "bad_count":
        counts["a1"] = True
    elif field == "future_start":
        start += timedelta(days=1)
    elif field == "deadline":
        deadline = float("inf")
    else:
        settings.identity_scope_mappings[SUBJECTS["a1"]].allow_global = True
    with pytest.raises(ValueError, match="invalid_postflight"):
        await module.collect_postflight(
            settings,
            runtime.factory,
            [],
            started_at=start,
            expected_reads=counts,
            deadline_seconds=deadline,
        )
    assert not object_wire.requests
