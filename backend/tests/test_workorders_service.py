from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from tests.test_wt023_vertical_slice import create_review_mission, valid_field_measurements

from windops_backend.enums import TaskStatus, WorkOrderStatus
from windops_backend.errors import InvalidTransitionError, NotFoundError
from windops_backend.models import WorkOrder, WorkOrderTask
from windops_backend.schemas import TaskCompletionRequest
from windops_backend.services.workorders import complete_task
from windops_backend.storage import FieldTaskEvidence


def _request() -> TaskCompletionRequest:
    return TaskCompletionRequest(
        result="fixture",
        artifact_uri="minio://windops-field-evidence/work-order/task/evidence.json",
        artifact_sha256="a" * 64,
        measurement={"fixture": "value"},
    )


@pytest.mark.asyncio
async def test_complete_task_rejects_missing_work_order_and_task() -> None:
    session = AsyncMock()
    session.scalar.return_value = None
    with pytest.raises(NotFoundError):
        await complete_task(session, "missing-work-order", "task-1", _request(), object())

    session.scalar.return_value = SimpleNamespace(id="work-order-1", status="scheduled")
    session.get.return_value = None
    with pytest.raises(NotFoundError):
        await complete_task(session, "work-order-1", "missing-task", _request(), object())


@pytest.mark.asyncio
async def test_complete_task_is_idempotent_for_completed_task() -> None:
    session = AsyncMock()
    session.scalar.return_value = SimpleNamespace(id="work-order-1", status="scheduled")
    session.get.return_value = SimpleNamespace(
        id="task-1",
        work_order_id="work-order-1",
        status=TaskStatus.COMPLETED.value,
    )
    result = await complete_task(session, "work-order-1", "task-1", _request(), object())
    assert result == {
        "task_id": "task-1",
        "task_status": TaskStatus.COMPLETED.value,
        "work_order_status": "scheduled",
        "already_completed": True,
    }


@pytest.mark.asyncio
async def test_complete_task_rejects_completed_work_order() -> None:
    session = AsyncMock()
    session.scalar.return_value = SimpleNamespace(
        id="work-order-1", status=WorkOrderStatus.COMPLETED.value
    )
    session.get.return_value = SimpleNamespace(
        id="task-1",
        work_order_id="work-order-1",
        status=TaskStatus.PENDING.value,
    )
    with pytest.raises(InvalidTransitionError):
        await complete_task(session, "work-order-1", "task-1", _request(), object())


@pytest.mark.parametrize("gate", ["predecessor", "measurement"])
async def test_operational_gate_rejection_preserves_tasks_and_evidence(
    app: FastAPI, client: AsyncClient, gate: str
) -> None:
    mission = await create_review_mission(client, f"WORKORDER-GATE-{gate}")
    approval = await client.post(
        f"/api/v1/missions/{mission['mission_id']}/approvals",
        headers={"Idempotency-Key": f"workorder-gate-approval-{gate}"},
        json={
            "action": "approve",
            "expected_revision": mission["revision"],
            "reason": "Review the persisted work plan before field execution",
        },
    )
    assert approval.status_code == 200, approval.text
    work_order_id = approval.json()["work_order_id"]
    order = (await client.get(f"/api/v1/work-orders/{work_order_id}")).json()
    index = 1 if gate == "predecessor" else 0
    task_id = order["tasks"][index]["task_id"]
    measurement = valid_field_measurements()[index]
    if gate == "measurement":
        measurement["water_content_ppm"] = 10000.0
    request = TaskCompletionRequest(
        result="Measured field result awaiting the operational gate",
        artifact_uri=f"minio://test/field-task-{index + 1}.json",
        artifact_sha256=f"{index + 1:064x}",
        measurement=measurement,
    )
    message = "before its predecessors" if gate == "predecessor" else "operational gate"
    async with app.state.session_factory() as session, session.begin():
        with pytest.raises(InvalidTransitionError, match=message):
            await complete_task(
                session, work_order_id, task_id, request, app.state.artifact_verifier
            )
    async with app.state.session_factory() as session:
        persisted = await session.get(WorkOrder, work_order_id)
        assert persisted is not None and persisted.status == WorkOrderStatus.SCHEDULED.value
        tasks = list(
            await session.scalars(
                select(WorkOrderTask).where(WorkOrderTask.work_order_id == work_order_id)
            )
        )
        assert len(tasks) == 5
        assert all(
            task.status == TaskStatus.PENDING.value and task.result is None for task in tasks
        )
        evidence_count = await session.scalar(
            select(func.count())
            .select_from(FieldTaskEvidence)
            .join(WorkOrderTask, WorkOrderTask.id == FieldTaskEvidence.task_id)
            .where(WorkOrderTask.work_order_id == work_order_id)
        )
        assert evidence_count == 0
