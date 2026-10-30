"""Import source snapshots into a caller-owned, isolated evaluation database.

This module never reads label files and never imports generated workflow rows.
The caller owns the outer transaction and the database lifecycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from windops_backend.access_control import ACCESS_POLICY_SESSION_KEY
from windops_backend.knowledge_graph.domain import GraphAccessPolicy
from windops_backend.models import (
    AgentExecution,
    Alarm,
    Approval,
    Decision,
    Evidence,
    IngestReceipt,
    IngestSource,
    KnowledgeDocument,
    KnowledgePassage,
    Mission,
    Resource,
    ResourceReservation,
    ScadaSample,
    Tenant,
    Turbine,
    WeatherWindow,
    WindFarm,
    WorkOrder,
)
from windops_backend.operations.workflow_eval_contracts import (
    WorkflowCaseInput,
    WorkflowInputBundle,
    canonical_sha256,
    read_bundle_bytes,
)
from windops_backend.services.missions import create_mission
from windops_backend.services.seed import seed_agent_catalog
from windops_backend.storage import OutboxEvent


@dataclass(frozen=True, slots=True)
class ImportedCase:
    case_id: str
    mission_id: str
    analysis_event_id: str
    input_sha256: str
    source_snapshot_sha256: str
    caller_scope: GraphAccessPolicy


def load_input_bundle(path: Path) -> WorkflowInputBundle:
    raw = read_bundle_bytes(path)
    bundle = WorkflowInputBundle.model_validate_json(raw)
    bundle._raw_sha256 = hashlib.sha256(raw).hexdigest()
    return bundle


def _verify_artifacts(case: WorkflowCaseInput, artifact_root: Path) -> None:
    root = artifact_root.absolute()
    if not root.is_dir():
        raise ValueError("source artifact root must be an existing directory")
    for artifact in case.source.source_artifacts:
        path = root.joinpath(*artifact.path.split("/"))
        # Check each original path component before resolve, including Windows
        # junctions. A lexical parent check alone would follow reparse targets.
        for part in (path, *path.parents):
            attributes = part.lstat()
            if part.is_symlink() or getattr(attributes, "st_file_attributes", 0) & (
                stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("source artifacts cannot use filesystem links")
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("source artifact escapes its root or is not a regular file")
        if path.stat().st_size != artifact.size_bytes:
            raise ValueError("source artifact size mismatch")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > artifact.size_bytes:
                    raise ValueError("source artifact changed while hashing")
                digest.update(chunk)
        if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
            raise ValueError("source artifact SHA-256 mismatch")


async def import_case(
    session: AsyncSession, case: WorkflowCaseInput, artifact_root: Path
) -> ImportedCase:
    """Validate and import one case without committing or starting execution.

    An existing scoped session is rejected: row filtering must not hide contaminating
    facts during the empty-database check. Execution receives the explicit policy.
    """
    if ACCESS_POLICY_SESSION_KEY in session.info:
        raise ValueError("source import requires a dedicated unscoped evaluation session")
    # Pydantic frozen models can contain mutable lists; revalidation checks the
    # exact snapshot being imported, including canonical facts and body hashes.
    case = WorkflowCaseInput.model_validate_json(case.model_dump_json())
    await asyncio.to_thread(_verify_artifacts, case, artifact_root)
    facts = case.facts
    digest = canonical_sha256(case)
    if session.get_bind().dialect.name == "sqlite":
        # Python's legacy SQLite transaction mode does not BEGIN for SAVEPOINT.
        # Its release would otherwise commit before the caller's commit/rollback.
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        active = getattr(raw.driver_connection, "in_transaction", None)
        if active is False:
            await connection.exec_driver_sql("BEGIN")
        elif active is not True:
            raise RuntimeError("cannot establish SQLite evaluation transaction state")
    async with session.begin_nested():
        for table in (
            WindFarm,
            Turbine,
            IngestSource,
            IngestReceipt,
            Alarm,
            KnowledgeDocument,
            Resource,
            WeatherWindow,
            Mission,
            Evidence,
            Decision,
            Approval,
            AgentExecution,
            WorkOrder,
            ResourceReservation,
            KnowledgePassage,
        ):
            if await session.scalar(select(table).limit(1)) is not None:
                raise ValueError("evaluation source import requires an empty business database")
        await seed_agent_catalog(session)
        session.add_all(Tenant(**row.model_dump()) for row in facts.topology.tenants)
        await session.flush()
        session.add_all(WindFarm(**row.model_dump()) for row in facts.topology.wind_farms)
        await session.flush()
        session.add_all(Turbine(**row.model_dump()) for row in facts.topology.turbines)
        session.add_all(
            IngestSource(
                **row.model_dump(),
                enabled=False,
                status="snapshot_imported",
                policy={"evaluation_snapshot": True},
            )
            for row in facts.ingest_sources
        )
        await session.flush()
        for receipt in facts.receipts:
            payload_hash = hashlib.sha256(
                json.dumps(
                    {
                        "source_id": receipt.source_id,
                        "sample": receipt.payload.model_dump(mode="json"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            session.add(
                IngestReceipt(
                    source_event_id=receipt.payload.source_event_id,
                    source_id=receipt.source_id,
                    received_at=receipt.received_at,
                    payload_hash=payload_hash,
                    disposition="accepted",
                )
            )
        await session.flush()
        receipts = {row.payload.source_event_id: row for row in facts.receipts}
        for sample in facts.samples:
            receipt = receipts[sample.source_event_id]
            payload = receipt.payload
            session.add(
                ScadaSample(
                    id=sample.sample_id,
                    source_event_id=sample.source_event_id,
                    source_id=receipt.source_id,
                    source_sequence=payload.source_sequence,
                    observed_at=payload.observed_at,
                    received_at=receipt.received_at,
                    turbine_id=payload.turbine_id,
                    variable=payload.variable,
                    value=payload.value,
                    unit=payload.unit,
                    quality=payload.quality,
                    attributes={**payload.attributes, "quality_code": payload.quality_code},
                )
            )
        receipt = receipts[facts.alarm.source_event_id]
        payload = receipt.payload
        session.add(
            Alarm(
                **facts.alarm.model_dump(),
                turbine_id=payload.turbine_id,
                triggered_at=payload.observed_at,
                evidence={
                    "variable": payload.variable,
                    "value": payload.value,
                    "unit": payload.unit,
                    "quality": payload.quality,
                    "quality_code": payload.quality_code,
                    "source_id": receipt.source_id,
                    "source_event_id": payload.source_event_id,
                    "attributes": payload.attributes,
                },
            )
        )
        for document in facts.knowledge_documents:
            session.add(
                KnowledgeDocument(
                    **document.model_dump(exclude={"body_sha256"}),
                    citation_uri=f"windops://knowledge/documents/{document.id}",
                    metadata_={
                        "evaluation_source_sha256": document.body_sha256,
                        "source_snapshot_sha256": case.source.source_snapshot_sha256,
                    },
                    ingestion_status="pending",
                    vectorized=False,
                )
            )
        session.add_all(Resource(**row.model_dump()) for row in facts.resources)
        session.add_all(WeatherWindow(**row.model_dump()) for row in facts.weather)
        await session.flush()
        response, replayed = await create_mission(
            session,
            facts.mission_request,
            idempotency_key=f"workflow-eval:{digest}",
            subject="workflow-evaluation-import",
        )
        if replayed:
            raise RuntimeError("fresh evaluation import unexpectedly replayed a mission")
        mission_id = str(response["mission_id"])
        analysis_event = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "mission.analysis.requested",
                    OutboxEvent.aggregate_id == mission_id,
                )
            )
        ).one()
        return ImportedCase(
            case_id=case.case_id,
            mission_id=mission_id,
            analysis_event_id=analysis_event.id,
            input_sha256=digest,
            source_snapshot_sha256=case.source.source_snapshot_sha256,
            caller_scope=facts.caller_scope.to_policy(),
        )
