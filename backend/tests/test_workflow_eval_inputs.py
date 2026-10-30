from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from windops_backend.access_control import attach_access_policy
from windops_backend.models import (
    AgentDefinition,
    AgentExecution,
    Alarm,
    Approval,
    Base,
    Decision,
    Evidence,
    KnowledgeDocument,
    Mission,
    Resource,
    ScadaSample,
    Tenant,
    Turbine,
    WorkOrder,
)
from windops_backend.operations import workflow_eval_inputs
from windops_backend.operations.workflow_eval_contracts import (
    WorkflowCaseInput,
    WorkflowFacts,
    WorkflowLabelBundle,
    canonical_sha256,
    load_label_bundle,
)
from windops_backend.operations.workflow_eval_inputs import import_case, load_input_bundle
from windops_backend.storage import OutboxEvent

FIXTURES = Path(__file__).parent / "fixtures" / "workflow-eval"


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sources.sqlite'}")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection: object, record: object) -> None:
        cursor = connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def test_separate_synthetic_bundles_are_bound_to_exact_bytes_and_cases() -> None:
    inputs = load_input_bundle(FIXTURES / "inputs.json")
    labels = load_label_bundle(FIXTURES / "labels.json", inputs)
    assert (
        inputs.input_bundle_sha256
        == hashlib.sha256((FIXTURES / "inputs.json").read_bytes()).hexdigest()
    )
    assert len(inputs.cases) == len(labels.cases) == 2
    assert {case.source.kind for case in inputs.cases} == {"synthetic_fixture"}
    assert all(not case.provenance.expert_verified for case in labels.cases)
    assert "expected_modes" not in inputs.model_dump_json()
    assert "WT023" not in inputs.model_dump_json()
    assert "WT-023" not in inputs.model_dump_json()
    assert not inputs.cases[0].facts.caller_scope.to_policy().unrestricted


@pytest.mark.parametrize("case_index", [0, 1])
async def test_actual_sqlite_import_creates_only_sources_and_queued_mission(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path, case_index: int
) -> None:
    case = load_input_bundle(FIXTURES / "inputs.json").cases[case_index]
    async with sessions() as session:
        imported = await import_case(session, case, tmp_path)
        await session.commit()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Turbine)) == 1
        assert await session.get(Turbine, "WT-023") is None
        assert (await session.get(Mission, imported.mission_id)).status == "detected"
        event_row = await session.get(OutboxEvent, imported.analysis_event_id)
        assert event_row.event_type == "mission.analysis.requested"
        assert event_row.status == "pending"
        assert event_row.payload["mission_id"] == imported.mission_id
        assert imported.input_sha256 == canonical_sha256(case)
        assert imported.caller_scope == case.facts.caller_scope.to_policy()
        assert await session.scalar(select(func.count()).select_from(AgentDefinition)) > 0
        for table in (Evidence, Decision, Approval, AgentExecution, WorkOrder):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
        samples = list(await session.scalars(select(ScadaSample)))
        assert len(samples) == len(case.facts.samples)
        alarm = await session.get(Alarm, case.facts.alarm.id)
        assert alarm.evidence["value"] == case.facts.receipts[0].payload.value
        documents = list(await session.scalars(select(KnowledgeDocument)))
        assert all(row.embedding is None and not row.vectorized for row in documents)
        assert all(row.ingestion_status == "pending" for row in documents)


async def test_import_preserves_callers_transaction_rollback(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case = load_input_bundle(FIXTURES / "inputs.json").cases[0]
    async with sessions() as session:
        await import_case(session, case, tmp_path)
        await session.rollback()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Turbine)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentDefinition)) == 0


async def test_failure_after_source_writes_rolls_back_savepoint_and_preserves_outer_transaction(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_input_bundle(FIXTURES / "inputs.json").cases[0]

    async def reject_mission(session: AsyncSession, *args: Any, **kwargs: Any) -> None:
        assert await session.scalar(select(func.count()).select_from(Turbine)) == 1
        assert await session.scalar(select(func.count()).select_from(ScadaSample)) == 1
        raise RuntimeError("injected mission creation failure after source writes")

    monkeypatch.setattr(workflow_eval_inputs, "create_mission", reject_mission)
    async with sessions() as session:
        session.add(Tenant(id="outer-tenant", name="Caller-owned transaction row"))
        await session.flush()
        with pytest.raises(RuntimeError, match="mission creation failure"):
            await import_case(session, case, tmp_path)
        assert await session.scalar(select(func.count()).select_from(Turbine)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentDefinition)) == 0
        assert await session.get(Tenant, "outer-tenant") is not None
        await session.commit()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 1


async def test_mutable_nested_source_tampering_is_rechecked_at_import(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case = load_input_bundle(FIXTURES / "inputs.json").cases[0]
    case.facts.resources.clear()
    async with sessions() as session:
        with pytest.raises(ValidationError, match="snapshot SHA-256 mismatch"):
            await import_case(session, case, tmp_path)
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 0


async def test_contaminated_or_scoped_database_rejected_without_overwrite(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case = load_input_bundle(FIXTURES / "inputs.json").cases[0]
    async with sessions() as session:
        session.add(Resource(id="existing", resource_type="crew", name="Existing", quantity=2))
        await session.commit()
        with pytest.raises(ValueError, match="empty business database"):
            await import_case(session, case, tmp_path)
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 0
        assert (await session.get(Resource, "existing")).quantity == 2
        attach_access_policy(session, case.facts.caller_scope.to_policy())
        with pytest.raises(ValueError, match="dedicated unscoped"):
            await import_case(session, case, tmp_path)


@pytest.mark.parametrize("mutation", ["bytes", "case_hash", "missing_case", "duplicate_case"])
def test_labels_reject_bundle_or_case_identity_drift(tmp_path: Path, mutation: str) -> None:
    inputs = load_input_bundle(FIXTURES / "inputs.json")
    data = json.loads((FIXTURES / "labels.json").read_bytes())
    if mutation == "bytes":
        path = tmp_path / "inputs.json"
        path.write_bytes((FIXTURES / "inputs.json").read_bytes() + b"\n")
        inputs = load_input_bundle(path)
    elif mutation == "case_hash":
        data["cases"][0]["input_case_sha256"] = "0" * 64
    elif mutation == "missing_case":
        data["cases"].pop()
    else:
        data["cases"].append(data["cases"][0])
    labels = WorkflowLabelBundle.model_validate(data)
    with pytest.raises(ValueError):
        labels.verify_inputs(inputs)


@pytest.mark.parametrize("mutation", ["hash", "relation", "future", "embedding", "hint", "scope"])
def test_source_contract_rejects_tampering_and_generated_rows(mutation: str) -> None:
    case = load_input_bundle(FIXTURES / "inputs.json").cases[0].model_dump(mode="json")
    facts = case["facts"]
    if mutation == "hash":
        facts["receipts"][0]["payload"]["value"] = 99
    elif mutation == "relation":
        facts["samples"][0]["source_event_id"] = "absent-receipt"
    elif mutation == "future":
        case["as_of"] = "2000-01-01T00:00:00Z"
    elif mutation == "embedding":
        facts["receipts"][0]["payload"]["attributes"]["embedding"] = [1, 2, 3]
        case["source"]["source_snapshot_sha256"] = canonical_sha256(
            WorkflowFacts.model_validate(facts)
        )
    elif mutation == "hint":
        facts["mission_request"]["analysis_profile"]["failure_mode_hint"] = "label answer"
    else:
        facts["caller_scope"]["tenant_ids"] = ["*"]
    with pytest.raises(ValidationError):
        WorkflowCaseInput.model_validate(case)


@pytest.mark.parametrize(
    "path", ["../source.json", "/tmp/source", "C:/source", "a\\source", "a/./b"]
)
def test_artifact_contract_rejects_unsafe_paths(path: str) -> None:
    case = load_input_bundle(FIXTURES / "inputs.json").cases[0].model_dump(mode="json")
    case["source"]["source_artifacts"] = [{"path": path, "sha256": "0" * 64, "size_bytes": 1}]
    with pytest.raises(ValidationError):
        WorkflowCaseInput.model_validate(case)


async def test_field_artifact_verified_before_any_writes(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    data = load_input_bundle(FIXTURES / "inputs.json").cases[0].model_dump(mode="json")
    source = tmp_path / "source.json"
    raw = b'{"deidentified":true}'
    source.write_bytes(raw)
    data["source"].update(
        kind="deidentified_field_snapshot",
        deidentification_statement="Test source contains no identities.",
        source_artifacts=[
            {"path": source.name, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
        ],
    )
    case = WorkflowCaseInput.model_validate(data)
    source.write_bytes(raw.replace(b"true", b"null"))
    async with sessions() as session:
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            await import_case(session, case, tmp_path)
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 0
        source.write_bytes(raw)
        imported = await import_case(session, case, tmp_path)
        assert imported.case_id == case.case_id


def test_duplicate_json_keys_rejected_and_expert_declaration_never_self_qualifies(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":"first","schema_version":"second"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_input_bundle(path)
    data = json.loads((FIXTURES / "labels.json").read_bytes())
    provenance = data["cases"][0]["provenance"]
    provenance.update(
        kind="expert_review_declared",
        reviewed_by="Declared reviewer",
        reviewed_at="2026-10-23T01:00:00Z",
        source_artifact_sha256="1" * 64,
    )
    labels = WorkflowLabelBundle.model_validate(data)
    assert not labels.cases[0].provenance.expert_verified
