"""Actual signed authorization controls and scoped health response validation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from functools import partial
from typing import Any

from windops_backend.operations.load_backlog import BacklogHealth
from windops_backend.operations.load_services_dataset import ServicesDatasetOracle
from windops_backend.operations.load_services_http import ServicesHttp


async def authorization_controls(
    http: ServicesHttp, oracle: ServicesDatasetOracle
) -> dict[str, Any]:
    before = http.requests_used
    for user in ("anonymous", "test-bypass"):
        await http.get(
            route_id="anonymous-denied",
            path="/api/v1/turbines",
            user_id=user,
            expected_status=401,
            validator=lambda value: isinstance(value, dict),
        )
    for user in ("a1", "a2", "b1", "b2"):
        await http.get(
            route_id="scope-allowed",
            path="/api/v1/turbines",
            user_id=user,
            query=oracle.queries["turbines"][user],
            validator=partial(oracle.validate, "turbines", user=user),
        )
        query = {**oracle.queries["scada/history"][user], "turbine_id": "turbine-c"}
        await http.get(
            route_id="scope-denied",
            path="/api/v1/scada/history",
            user_id=user,
            query=query,
            expected_status=404,
            validator=lambda value: isinstance(value, dict),
        )
    return {
        "passed": True,
        "observed_requests": http.requests_used - before,
        "subjects": 4,
        "allowed_domains": 2,
        "denied_domain": True,
        "anonymous_denied": True,
        "test_header_bypass_denied": True,
    }


def _time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid health observation time")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None or not -5 <= (datetime.now(UTC) - result).total_seconds() <= 45:
        raise ValueError("stale health observation")
    return result


def aggregate_health(values: list[dict[str, Any]], *, scope_verified: bool) -> BacklogHealth:
    if len(values) != 2 or not scope_verified:
        raise ValueError("two authorized health observations required")
    workers, queues = [], []
    for value in values:
        worker, queue = value.get("worker"), value.get("queue")
        if (
            not isinstance(worker, dict)
            or not isinstance(queue, dict)
            or queue.get("scope") != "authorized_documents"
            or value.get("docling_pdf_configured") is not True
        ):
            raise ValueError("invalid configured scoped health response")
        for key in ("waiting", "running", "retrying", "expired_leases"):
            if type(queue.get(key)) is not int or not 0 <= queue[key] <= 10000:
                raise ValueError("invalid queue count")
        if type(queue.get("backlogged")) is not bool:
            raise ValueError("invalid queue backlog state")
        for key in ("heartbeat_verified", "runtime_ready"):
            if type(worker.get(key)) is not bool:
                raise ValueError("invalid worker health boolean")
        if type(value.get("worker_health_verified")) is not bool:
            raise ValueError("invalid combined worker health boolean")
        if (
            worker.get("status")
            not in {
                "unconfigured",
                "unavailable",
                "offline",
                "starting",
                "ready",
                "degraded",
                "version_mismatch",
            }
            or type(worker.get("heartbeat_ttl_seconds")) is not int
            or value["worker_health_verified"]
            != (
                worker["status"] == "ready"
                and worker["heartbeat_verified"]
                and worker["runtime_ready"]
            )
        ):
            raise ValueError("inconsistent worker health response")
        workers.append(worker)
        queues.append(queue)
    statuses = {w.get("status") for w in workers}
    ttl = {w.get("heartbeat_ttl_seconds") for w in workers}
    warning = {q.get("backlog_warning_seconds") for q in queues}
    if ttl != {45} or len(warning) != 1:
        raise ValueError("production health constants differ")
    return BacklogHealth.model_validate(
        dict(
            status=next(iter(statuses)) if len(statuses) == 1 else "unavailable",
            worker_health_verified=all(v["worker_health_verified"] for v in values),
            heartbeat_verified=all(w["heartbeat_verified"] for w in workers),
            runtime_ready=all(w["runtime_ready"] for w in workers),
            heartbeat_ttl_seconds=45,
            observed_at=min(_time(w.get("observed_at")) for w in workers),
            queue_observed_at=min(_time(q.get("observed_at")) for q in queues),
            scope_verified=True,
            **{
                key: sum(q[key] for q in queues)
                for key in ("waiting", "running", "retrying", "expired_leases")
            },
            backlogged=any(q["backlogged"] for q in queues),
            backlog_warning_seconds=next(iter(warning)),
        )
    )


async def read_health(http: ServicesHttp, *, scope_verified: bool) -> BacklogHealth:
    values = []
    for user in ("a1", "b1"):
        values.append(
            await http.get(
                route_id="parser-health",
                path="/api/v1/knowledge/parser-capabilities",
                user_id=user,
                validator=lambda value: isinstance(value, dict),
            )
        )
    return aggregate_health(values, scope_verified=scope_verified)


async def wait_document_ready(http: ServicesHttp, *, scope_verified: bool) -> BacklogHealth:
    async with asyncio.timeout(120):
        while True:
            health = await read_health(http, scope_verified=scope_verified)
            if health.status == "ready" and health.worker_health_verified:
                return health
            await asyncio.sleep(2)
