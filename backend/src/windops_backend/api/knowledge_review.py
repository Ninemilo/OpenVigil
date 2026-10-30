from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from windops_backend.api.deps import (
    Principal,
    get_artifact_verifier,
    get_runtime_settings,
    get_session,
    require_read_access,
    require_roles,
)
from windops_backend.config import Settings
from windops_backend.schema_knowledge_review import NumericReviewDecision, NumericReviewProposal
from windops_backend.services.idempotency import execute_idempotent_command
from windops_backend.services.knowledge_review import (
    decide_numeric_review,
    numeric_review_detail,
    propose_numeric_review,
    review_source,
)
from windops_backend.storage import ArtifactVerifier

router = APIRouter(tags=["knowledge"])


@router.get("/knowledge/passages/{passage_id}/numeric-review")
async def knowledge_numeric_review_detail(
    passage_id: str,
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_read_access),
) -> dict[str, Any]:
    source = await review_source(session, passage_id, principal.graph_access_policy())
    return await numeric_review_detail(
        session,
        source,
        principal.subject,
        principal.roles,
        cursor=cursor,
        limit=limit,
    )


@router.post("/knowledge/passages/{passage_id}/numeric-review")
async def knowledge_numeric_review_propose(
    passage_id: str,
    payload: NumericReviewProposal,
    response: Response,
    key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("operations_manager", "maintenance_reviewer")),
) -> dict[str, Any]:
    source = await review_source(
        session,
        passage_id,
        principal.graph_access_policy(),
        payload,
        lock=True,
    )

    async def operation() -> dict[str, Any]:
        await propose_numeric_review(session, source, payload, principal.subject)
        return await numeric_review_detail(session, source, principal.subject, principal.roles)

    result, replayed = await execute_idempotent_command(
        session,
        subject=principal.subject,
        command_type="knowledge.numeric-review.propose.v1",
        target=passage_id,
        idempotency_key=key,
        payload=payload,
        status_code=200,
        operation=operation,
    )
    await session.commit()
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    return result


@router.post("/knowledge/passages/{passage_id}/numeric-review/decision")
async def knowledge_numeric_review_decide(
    passage_id: str,
    payload: NumericReviewDecision,
    response: Response,
    key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("operations_manager", "maintenance_reviewer")),
    verifier: ArtifactVerifier = Depends(get_artifact_verifier),
    settings: Settings = Depends(get_runtime_settings),
) -> dict[str, Any]:
    source = await review_source(
        session,
        passage_id,
        principal.graph_access_policy(),
        payload,
        lock=True,
    )

    async def operation() -> dict[str, Any]:
        await decide_numeric_review(
            session,
            source,
            payload,
            principal.subject,
            principal.roles,
            verifier,
            settings,
        )
        return await numeric_review_detail(session, source, principal.subject, principal.roles)

    result, replayed = await execute_idempotent_command(
        session,
        subject=principal.subject,
        command_type="knowledge.numeric-review.decide.v1",
        target=passage_id,
        idempotency_key=key,
        payload=payload,
        status_code=200,
        operation=operation,
    )
    await session.commit()
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    return result
