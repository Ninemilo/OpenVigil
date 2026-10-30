"""Bounded read-only postflight against the explicitly configured local services."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from minio import Minio
from neo4j import READ_ACCESS, AsyncGraphDatabase
from sqlalchemy import and_, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from windops_backend.config import Settings
from windops_backend.document_parse_result_identity import document_parse_result_hash
from windops_backend.knowledge_graph.domain import NodeType, graph_uid
from windops_backend.knowledge_graph.projection_values import KNOWLEDGE_GRAPH_PROJECTION_ID
from windops_backend.models import ReadAccessAudit
from windops_backend.operations.load_dataset import USER_SCOPES
from windops_backend.operations.load_document_evidence import (
    MAX_DOCUMENTS,
    OwnedDocumentEvidenceSpec,
    _bounded_rows,
    _document_receipt,
)
from windops_backend.operations.load_runner import _loopback_origin, _quiet_transport, _settle
from windops_backend.operations.load_services_settings import SUBJECTS
from windops_backend.read_audit import minimized_read_metadata

MIB = 1024 * 1024
MAX_POLLS = 120
MAX_READS_PER_SUBJECT = 250_000
MAX_GRAPH_BATCH = 64
_PRIVATE_GRAPH = ContextVar("load_postflight_private_graph", default=False)

# Each row is restricted to an owned UID. Text/locator values are returned only
# at the exact bounded length supplied from the verified SQL source snapshot.
PASSAGE_QUERY = """
UNWIND $wanted AS wanted
OPTIONAL MATCH (p:WindOpsKnowledge:KnowledgePassage {uid: wanted.uid})
OPTIONAL MATCH (d:WindOpsKnowledge:KnowledgeDocument {uid: wanted.document_uid})
 -[r:HAS_PASSAGE]->(p)
WITH wanted, p, d, count(r) AS edges
RETURN wanted.uid AS wanted_uid, CASE WHEN p IS NULL THEN 0 ELSE 1 END AS nodes, edges,
 p.uid AS uid, p.entityId AS entity_id, p.nodeType AS node_type,
 p.projectionId AS projection_id, p.documentId AS document_id,
 p.documentVersion AS version, p.sourceSha256 AS source_sha256,
 p.textSha256 AS text_sha256, p.tenantId AS tenant_id,
 p.windFarmId AS wind_farm_id, p.turbineId AS turbine_id,
 p.ordinal AS ordinal, p.pageNumber AS page_number,
 p.charStart AS char_start, p.charEnd AS char_end,
 p.parserVersion AS parser_version, p.citationUri AS citation_uri,
 CASE WHEN size(p.body) = wanted.text_length THEN p.body ELSE null END AS body,
 CASE WHEN size(p.nativeLocator) = wanted.locator_length
 THEN p.nativeLocator ELSE null END AS locator,
 d.entityId AS parent_id, d.documentVersion AS parent_version,
 d.artifactSha256 AS parent_source_sha256, d.tenantId AS parent_tenant_id,
 d.windFarmId AS parent_wind_farm_id, d.turbineId AS parent_turbine_id,
 d.projectionId AS parent_projection_id, d.vectorized AS parent_vectorized
 LIMIT $row_limit
"""
COUNT_QUERY = """
MATCH (p:WindOpsKnowledge:KnowledgePassage)
WHERE p.documentId = $document_id
RETURN count(p) AS count
"""


class _Failure(Exception):
    pass


class _HideGraphDetails(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_GRAPH.get()


@contextmanager
def _quiet_graph() -> Iterator[None]:
    names = {"neo4j", "neo4j.io", "neo4j.pool", "neo4j.bolt", "neo4j.auth_management"}
    names.update(name for name in logging.Logger.manager.loggerDict if name.startswith("neo4j."))
    loggers = [logging.getLogger(name) for name in names]
    guard = _HideGraphDetails()
    for logger in loggers:
        logger.addFilter(guard)
    token = _PRIVATE_GRAPH.set(True)
    try:
        yield
    finally:
        _PRIVATE_GRAPH.reset(token)
        for logger in loggers:
            logger.removeFilter(guard)


async def _close_clients(client: httpx.AsyncClient | None, driver: Any) -> bool:
    async def close_one(value: Any, *, http: bool) -> None:
        with _quiet_transport(), _quiet_graph():
            async with asyncio.timeout(4):
                await (value.aclose() if http else value.close())

    async def close() -> bool:
        with _quiet_transport() as unsettled:
            tasks = [
                asyncio.create_task(close_one(value, http=http), name="postflight-owned-close")
                for value, http in ((client, True), (driver, False))
                if value is not None
            ]
            return await _settle(
                tasks, deadline=time.perf_counter() + 5, cancel=False, unsettled=unsettled
            )

    task = asyncio.create_task(close(), name="postflight-owned-cleanup")
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        return task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


def _inputs(
    settings: Settings,
    manifest: Sequence[OwnedDocumentEvidenceSpec],
    started_at: datetime,
    expected_reads: Mapping[str, int],
    deadline_seconds: float,
) -> tuple[OwnedDocumentEvidenceSpec, ...]:
    if (
        not isinstance(started_at, datetime)
        or started_at.utcoffset() is None
        or started_at > datetime.now(UTC)
        or type(deadline_seconds) not in (int, float)
        or not math.isfinite(deadline_seconds)
        or not 0 < deadline_seconds <= 300
        or set(expected_reads) != set(USER_SCOPES)
        or any(
            type(count) is not int or not 1 <= count <= MAX_READS_PER_SUBJECT
            for count in expected_reads.values()
        )
        or len(manifest) > MAX_DOCUMENTS
    ):
        raise ValueError("invalid_postflight_contract")
    try:
        owned = tuple(
            OwnedDocumentEvidenceSpec.model_validate(item.model_dump()) for item in manifest
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError("invalid_postflight_manifest") from None
    if len({item.document_id for item in owned}) != len(owned) or len(
        {item.document_alias for item in owned}
    ) != len(owned):
        raise ValueError("invalid_postflight_manifest")
    for alias, scope in USER_SCOPES.items():
        grant = settings.identity_scope_mappings.get(SUBJECTS[alias])
        if (
            grant is None
            or grant.tenant_ids != [f"tenant-{scope}"]
            or grant.wind_farm_ids != [f"farm-{scope}"]
            or grant.turbine_ids
            or grant.entity_ids
            or grant.allow_global
            or not grant.data_scopes
            or "*" in grant.data_scopes
        ):
            raise ValueError("invalid_postflight_scope_configuration")
    return owned


async def _audit(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    started_at: datetime,
    expected_reads: Mapping[str, int],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    observed = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.autoflush = False
        dialect = session.bind.dialect.name if session.bind is not None else "unknown"
        if dialect == "postgresql":
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        elif dialect == "sqlite":
            await session.execute(text("BEGIN"))
        else:
            raise _Failure("postflight_database_unsupported")
        for alias, subject in SUBJECTS.items():
            scope = {
                **settings.identity_scope_mappings[subject].model_dump(),
                "unrestricted": False,
            }
            authorization = minimized_read_metadata({}, scope)["authorization"]
            if not isinstance(authorization, dict):
                raise _Failure("postflight_scope_invalid")
            query = ReadAccessAudit.query
            predicates = [
                query["schema"].as_string() == "windops.read-access-metadata.v1",
                ReadAccessAudit.method == "GET",
                ReadAccessAudit.endpoint.startswith(settings.api_prefix + "/"),
            ]
            for key, value in authorization.items():
                scalar = query["authorization"][key]
                predicates.append(
                    (
                        scalar.as_boolean()
                        if type(value) is bool
                        else scalar.as_integer()
                        if type(value) is int
                        else scalar.as_string()
                    )
                    == value
                )
            total, unique, invalid = (
                await session.execute(
                    select(
                        func.count(),
                        func.count(func.distinct(ReadAccessAudit.id)),
                        func.sum(case((and_(*predicates), 0), else_=1)),
                    ).where(
                        ReadAccessAudit.subject == subject,
                        ReadAccessAudit.accessed_at >= started_at,
                        ReadAccessAudit.accessed_at <= observed,
                    )
                )
            ).one()
            if total > MAX_READS_PER_SUBJECT or unique != total or (invalid or 0):
                raise _Failure("read_audit_scope_or_identity_invalid")
            counts[alias] = int(total)
    return {
        "verified": all(counts[alias] >= expected for alias, expected in expected_reads.items()),
        "observed_counts": counts,
        "minimum_expected_counts": dict(expected_reads),
        "scope_verified": True,
        "window_start": started_at.isoformat(),
        "observed_at": observed.isoformat(),
        "coverage": "minimum_successful_read_count_not_exact_request_join",
    }


async def _target(
    factory: async_sessionmaker[AsyncSession], spec: OwnedDocumentEvidenceSpec
) -> dict[str, Any] | None:
    async with factory() as session, session.begin():
        session.autoflush = False
        dialect = session.bind.dialect.name if session.bind is not None else "unknown"
        if dialect == "postgresql":
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        elif dialect == "sqlite":
            await session.execute(text("BEGIN"))
        else:
            raise _Failure("postflight_database_unsupported")
        document, pieces, jobs, events = await _bounded_rows(session, spec)
        if document is None:
            return None
        receipt = _document_receipt(spec, document, pieces, jobs, events)
        if receipt["errors"]:
            raise _Failure("postflight_sql_source_invalid")
        if receipt["outcome"] != "indexed":
            return None
        if (
            not isinstance(document.artifact_uri, str)
            or type(document.content_size_bytes) is not int
            or not 0 < document.content_size_bytes <= 50 * MIB
            or document.content_type != "application/pdf"
        ):
            raise _Failure("postflight_object_contract_invalid")
        return {
            "uri": document.artifact_uri,
            "bytes": document.content_size_bytes,
            "pieces": [
                {
                    "id": piece.id,
                    "ordinal": piece.ordinal,
                    "page_number": piece.page_number,
                    "char_start": piece.char_start,
                    "char_end": piece.char_end,
                    "text": piece.text,
                    "text_sha256": piece.text_sha256,
                    "parser_version": piece.parser_version,
                    "locator": piece.native_locator,
                }
                for piece in pieces
            ],
        }


def _signed_source(settings: Settings, spec: OwnedDocumentEvidenceSpec, uri: str) -> str:
    origin = _loopback_origin(
        ("https://" if settings.minio_secure else "http://") + settings.minio_endpoint
    )
    parsed = urlsplit(uri)
    prefix = f"/documents/{spec.document_id}/"
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", spec.document_id) is None
        or parsed.scheme != "minio"
        or parsed.netloc != settings.minio_knowledge_bucket
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(prefix)
        or not parsed.path[len(prefix) :]
        or any(part in {"", ".", ".."} for part in parsed.path[1:].split("/"))
        or "%" in parsed.path
        or "\\" in parsed.path
    ):
        raise _Failure("postflight_object_scope_invalid")
    # An explicit region makes signing a local operation; SDK region discovery
    # would otherwise perform an unbounded synchronous metadata request.
    client = Minio(
        settings.minio_endpoint,
        settings.minio_access_key,
        settings.minio_secret_key.get_secret_value(),
        secure=settings.minio_secure,
        region="us-east-1",
    )
    try:
        url = client.presigned_get_object(
            settings.minio_knowledge_bucket, parsed.path[1:], expires=timedelta(minutes=5)
        )
    finally:
        client._http.clear()
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}" != origin:
        raise _Failure("postflight_object_origin_invalid")
    return url


async def _object(
    client: httpx.AsyncClient,
    settings: Settings,
    spec: OwnedDocumentEvidenceSpec,
    target: dict[str, Any],
) -> dict[str, Any] | None:
    url = _signed_source(settings, spec, target["uri"])
    async with client.stream("GET", url) as response:
        if response.status_code == 404:
            return None
        if (
            response.status_code != 200
            or response.headers.get("content-type", "").split(";")[0] != "application/pdf"
            or response.headers.get("content-encoding") not in (None, "identity")
            or response.headers.get("content-length") != str(target["bytes"])
        ):
            raise _Failure("postflight_object_response_invalid")
        digest, size = hashlib.sha256(), 0
        async for chunk in response.aiter_raw(64 * 1024):
            size += len(chunk)
            if size > target["bytes"]:
                raise _Failure("postflight_object_bytes_exceeded")
            digest.update(chunk)
        if size != target["bytes"] or digest.hexdigest() != spec.artifact_sha256:
            raise _Failure("postflight_object_sha_mismatch")
    return {
        "document_alias": spec.document_alias,
        "verified": True,
        "bytes": size,
        "sha256": digest.hexdigest(),
    }


def _graph_uri(settings: Settings) -> str:
    parsed = urlsplit(settings.neo4j_uri)
    try:
        valid = (
            parsed.scheme == "bolt"
            and ipaddress.ip_address(parsed.hostname or "").is_loopback
            and parsed.port is not None
            and 0 < parsed.port <= 65535
            and parsed.path in ("", "/")
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("explicit_loopback_bolt_endpoint_required")
    return settings.neo4j_uri


async def _graph(
    driver: Any,
    settings: Settings,
    spec: OwnedDocumentEvidenceSpec,
    target: dict[str, Any],
    query_timeout_seconds: float,
) -> dict[str, Any] | None:
    pieces = target["pieces"]
    async with driver.session(
        database=settings.neo4j_database,
        default_access_mode=READ_ACCESS,
        fetch_size=MAX_GRAPH_BATCH,
    ) as session:
        async with await session.begin_transaction(timeout=query_timeout_seconds) as transaction:
            result = await transaction.run(COUNT_QUERY, document_id=spec.document_id)
            count_row = await result.single(strict=True)
            if count_row is None or type(count_row["count"]) is not int:
                raise _Failure("postflight_graph_count_invalid")
            if count_row["count"] == 0:
                return None
            if count_row["count"] != len(pieces):
                raise _Failure("postflight_graph_passage_count_mismatch")
            for offset in range(0, len(pieces), MAX_GRAPH_BATCH):
                batch = pieces[offset : offset + MAX_GRAPH_BATCH]
                wanted = [
                    {
                        "uid": graph_uid(NodeType.KNOWLEDGE_PASSAGE, piece["id"]),
                        "document_uid": graph_uid(NodeType.KNOWLEDGE_DOCUMENT, spec.document_id),
                        "text_length": len(piece["text"]),
                        "locator_length": len(
                            json.dumps(
                                piece["locator"], ensure_ascii=False, sort_keys=True, default=str
                            )
                        ),
                    }
                    for piece in batch
                ]
                result = await transaction.run(
                    PASSAGE_QUERY, wanted=wanted, row_limit=len(batch) + 1
                )
                actual = [dict(record) async for record in result]
                if len(actual) != len(batch):
                    raise _Failure("postflight_graph_identity_invalid")
                by_uid = {row.get("wanted_uid"): row for row in actual}
                if len(by_uid) != len(batch):
                    raise _Failure("postflight_graph_identity_invalid")
                for piece, item in zip(batch, wanted, strict=True):
                    row = by_uid.get(item["uid"])
                    if row is None or row.get("nodes") != 1 or row.get("edges") != 1:
                        return None
                    expected = {
                        "uid": item["uid"],
                        "entity_id": piece["id"],
                        "node_type": "KnowledgePassage",
                        "projection_id": KNOWLEDGE_GRAPH_PROJECTION_ID,
                        "document_id": spec.document_id,
                        "version": spec.document_version,
                        "source_sha256": spec.artifact_sha256,
                        "text_sha256": piece["text_sha256"],
                        "tenant_id": spec.tenant_id,
                        "wind_farm_id": spec.wind_farm_id,
                        "turbine_id": spec.turbine_id,
                        "ordinal": piece["ordinal"],
                        "page_number": piece["page_number"],
                        "char_start": piece["char_start"],
                        "char_end": piece["char_end"],
                        "parser_version": piece["parser_version"],
                        "citation_uri": f"windops://knowledge/passages/{piece['id']}",
                        "parent_id": spec.document_id,
                        "parent_version": spec.document_version,
                        "parent_source_sha256": spec.artifact_sha256,
                        "parent_tenant_id": spec.tenant_id,
                        "parent_wind_farm_id": spec.wind_farm_id,
                        "parent_turbine_id": spec.turbine_id,
                        "parent_projection_id": KNOWLEDGE_GRAPH_PROJECTION_ID,
                        "parent_vectorized": True,
                    }
                    if any(
                        type(row.get(key)) is not type(value) or row.get(key) != value
                        for key, value in expected.items()
                    ):
                        raise _Failure("postflight_graph_source_identity_mismatch")
                    body, locator = row.get("body"), row.get("locator")
                    if (
                        not isinstance(body, str)
                        or hashlib.sha256(body.encode()).hexdigest() != piece["text_sha256"]
                        or not isinstance(locator, str)
                        or document_parse_result_hash(json.loads(locator))
                        != document_parse_result_hash(piece["locator"])
                    ):
                        raise _Failure("postflight_graph_content_mismatch")
    return {"document_alias": spec.document_alias, "verified": True, "passages": len(pieces)}


async def collect_postflight(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    manifest: Sequence[OwnedDocumentEvidenceSpec],
    *,
    started_at: datetime,
    expected_reads: Mapping[str, int],
    deadline_seconds: float,
) -> dict[str, Any]:
    """Read source bytes, owned graph nodes and four-subject persisted audit counts.

    No service startup or changes occur. Missing asynchronous projections/audit rows
    may settle within the supplied deadline; mismatched evidence immediately fails.
    Only aliases, hashes, counts, fixed codes and times enter the result.
    """
    owned = _inputs(settings, manifest, started_at, expected_reads, deadline_seconds)
    beginning = time.monotonic()
    report: dict[str, Any] = {
        "schema_version": "openvigil.services-postflight.v1",
        "passed": False,
        "errors": [],
        "audit": {"verified": False},
        "objects": {"required": bool(owned), "verified": False, "documents": []},
        "graph": {"required": bool(owned), "verified": False, "documents": []},
        "cleanup_passed": True,
    }
    driver = None
    client = None
    try:
        async with asyncio.timeout(deadline_seconds):
            with _quiet_transport(), _quiet_graph():
                client = httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=min(5.0, deadline_seconds),
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                )
                if owned:
                    driver = AsyncGraphDatabase.driver(
                        _graph_uri(settings),
                        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
                        connection_timeout=min(5.0, deadline_seconds),
                        connection_acquisition_timeout=min(5.0, deadline_seconds),
                        max_transaction_retry_time=0,
                        max_connection_pool_size=1,
                    )
                targets, objects, graphs = {}, {}, {}
                for poll in range(MAX_POLLS):
                    report["audit"] = await _audit(settings, factory, started_at, expected_reads)
                    for spec in owned:
                        alias = spec.document_alias
                        if alias not in targets:
                            target = await _target(factory, spec)
                            if target is None:
                                continue
                            targets[alias] = target
                        if alias not in objects:
                            value = await _object(client, settings, spec, targets[alias])
                            if value is not None:
                                objects[alias] = value
                        if alias not in graphs:
                            value = await _graph(
                                driver,
                                settings,
                                spec,
                                targets[alias],
                                min(5.0, deadline_seconds),
                            )
                            if value is not None:
                                graphs[alias] = value
                    report["objects"]["documents"] = list(objects.values())
                    report["graph"]["documents"] = list(graphs.values())
                    report["objects"]["verified"] = bool(owned) and len(objects) == len(owned)
                    report["graph"]["verified"] = bool(owned) and len(graphs) == len(owned)
                    if report["audit"]["verified"] and len(objects) == len(graphs) == len(owned):
                        report["passed"] = True
                        break
                    if poll + 1 == MAX_POLLS:
                        raise _Failure("postflight_poll_budget_exhausted")
                    await asyncio.sleep(min(0.25, deadline_seconds))
    except TimeoutError:
        report["errors"].append("postflight_deadline_exceeded")
    except _Failure as error:
        report["errors"].append(str(error))
    except asyncio.CancelledError:
        raise
    except Exception:
        report["errors"].append("postflight_observation_failed")
    finally:
        try:
            report["cleanup_passed"] = await _close_clients(client, driver)
        except Exception:
            report["cleanup_passed"] = False
        if not report["cleanup_passed"]:
            report["passed"] = False
            report["errors"].append("postflight_client_cleanup_failed")
        report["elapsed_seconds"] = time.monotonic() - beginning
        report["observed_at"] = datetime.now(UTC).isoformat()
    return report
