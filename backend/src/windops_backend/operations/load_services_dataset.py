"""Synthetic service-load inputs for an already migrated, caller-owned database.

No schema creation, discovery, credentials, network clients or production service
settings live here. SQLite exercises insertion/oracles only; it cannot verify the
PostgreSQL/Timescale branch. The caller must establish exclusive database ownership.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import Table, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from windops_backend.model_base import Base
from windops_backend.models import (
    Alarm,
    Approval,
    Decision,
    Evidence,
    IngestReceipt,
    IngestSource,
    KnowledgeDocument,
    Mission,
    Resource,
    ResourceReservation,
    ScadaSample,
    Tenant,
    Turbine,
    WeatherWindow,
    WindFarm,
    WorkOrder,
    WorkOrderTask,
)
from windops_backend.operations.load_dataset import (
    START,
    USER_SCOPES,
    VARIABLES,
    canonical_hash,
    history_query,
    sample_row,
)

GENERATOR = "openvigil.synthetic-services-load.v1"
ROUTES = (
    "turbines",
    "diagnoses",
    "maintenance-plans",
    "knowledge/documents",
    "scada/history",
    "scada/measurements",
)
RESOURCE_TYPES = ("crew", "vessel", "spare_part", "tool")
# Timescale's default origin for sub-month intervals, unlike SQLite's window origin.
# https://www.tigerdata.com/docs/reference/timescaledb/hyperfunctions/time-series-utilities/time_bucket
TIMESCALE_ORIGIN = datetime(2000, 1, 3, tzinfo=UTC)
MODELS: tuple[type[Base], ...] = (
    Tenant,
    WindFarm,
    Turbine,
    IngestSource,
    IngestReceipt,
    ScadaSample,
    Alarm,
    Mission,
    Approval,
    Decision,
    Evidence,
    WorkOrder,
    WorkOrderTask,
    Resource,
    ResourceReservation,
    WeatherWindow,
    KnowledgeDocument,
)
SEED_DOCUMENT_IDS = tuple(f"load-document-{scope}-{n}" for scope in "abc" for n in (1, 2))


def _table(model: type[Base]) -> Table:
    return cast(Table, model.__table__)


def _check_rows(rows: int) -> None:
    if type(rows) is not int or not 90 <= rows <= 1_000_000:
        raise ValueError("services history requires 90..1000000 integer rows")


def _wire(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


def _static_rows() -> list[tuple[type[Base], list[dict[str, Any]]]]:
    """Explicit persisted synthetic ledgers; no claim of real human decisions."""
    data: dict[type[Base], list[dict[str, Any]]] = {model: [] for model in MODELS}
    for scope in "abc":
        data[Tenant].append(
            dict(id=f"tenant-{scope}", name=f"Synthetic {scope}", status="active", created_at=START)
        )
        data[WindFarm].append(
            dict(
                id=f"farm-{scope}",
                tenant_id=f"tenant-{scope}",
                name=f"Synthetic farm {scope}",
                capacity_mw=10.0,
                created_at=START,
            )
        )
        data[Turbine].extend(
            dict(
                id=f"turbine-{scope}{suffix}",
                wind_farm_id=f"farm-{scope}",
                model=GENERATOR,
                status="running",
                health_score=86.0,
                updated_at=START,
            )
            for suffix in ("", "-extra")
        )
        data[IngestSource].append(
            dict(
                id=f"source-{scope}",
                display_name=f"Synthetic {scope}",
                source_kind="rest",
                policy={},
                created_at=START,
                updated_at=START,
            )
        )
        data[Alarm].append(
            dict(
                id=f"load-alarm-{scope}",
                turbine_id=f"turbine-{scope}",
                source_event_id=f"receipt-{ord(scope) - ord('a'):07d}",
                code="SYNTHETIC_LOAD",
                subsystem="generator",
                title=f"Synthetic alarm {scope}",
                severity="major",
                status="open",
                triggered_at=START,
                updated_at=START,
                evidence={
                    "variable": "active_power",
                    "value": 42.5,
                    "unit": "MW",
                    "attributes": {"anomaly_score": 0.8, "threshold": 40.0},
                },
            )
        )
        data[Mission].append(
            dict(
                id=f"load-mission-{scope}",
                alarm_id=f"load-alarm-{scope}",
                turbine_id=f"turbine-{scope}",
                title=f"Synthetic diagnosis {scope}",
                status="diagnosed",
                created_at=START,
                updated_at=START,
                public_state={
                    "classification": "synthetic",
                    "diagnosis": {
                        "confidence": 0.82,
                        "component": "generator",
                        "failure_mode": "Synthetic test condition",
                        "conclusion": f"Synthetic conclusion {scope}",
                    },
                },
            )
        )
        data[Approval].append(
            dict(
                id=f"load-approval-{scope}",
                mission_id=f"load-mission-{scope}",
                mission_revision=1,
                action="approve",
                selected_alternative_id="load-alt",
                approver="synthetic-fixture",
                reason="Synthetic fixture only",
                created_at=START,
            )
        )
        data[Decision].append(
            dict(
                id=f"load-decision-{scope}",
                mission_id=f"load-mission-{scope}",
                status="approved",
                alternatives=[{"id": "load-alt", "classification": "synthetic"}],
                recommended_alternative_id="load-alt",
                selected_alternative_id="load-alt",
                recommendation_reason="Synthetic fixture only",
                approval_id=f"load-approval-{scope}",
                created_at=START,
                updated_at=START,
            )
        )
        data[Evidence].append(
            dict(
                id=f"load-evidence-{scope}",
                mission_id=f"load-mission-{scope}",
                source_key="synthetic-input",
                evidence_type="synthetic",
                summary=f"Synthetic evidence {scope}",
                source_refs=[f"load-alarm-{scope}"],
                metrics={"classification": "synthetic"},
                created_at=START,
            )
        )
        data[WorkOrder].append(
            dict(
                id=f"load-order-{scope}",
                mission_id=f"load-mission-{scope}",
                approval_id=f"load-approval-{scope}",
                turbine_id=f"turbine-{scope}",
                title=f"Synthetic maintenance {scope}",
                selected_alternative_id="load-alt",
                selected_action="Synthetic inspection only",
                status="scheduled",
                priority="high",
                assigned_team=f"Synthetic team {scope}",
                created_by="synthetic-fixture",
                planned_start=START,
                deadline=START + timedelta(hours=3),
                estimated_duration_hours=2.0,
                created_at=START,
                updated_at=START,
            )
        )
        for number in (1, 2):
            data[WorkOrderTask].append(
                dict(
                    id=f"load-task-{scope}-{number}",
                    work_order_id=f"load-order-{scope}",
                    sequence=number,
                    title=f"Synthetic task {number}",
                    schema_version="synthetic.v1",
                    measurement_schema={"classification": "synthetic"},
                    status="completed" if number == 1 else "pending",
                )
            )
            data[KnowledgeDocument].append(
                dict(
                    id=f"load-document-{scope}-{number}",
                    tenant_id=f"tenant-{scope}",
                    wind_farm_id=f"farm-{scope}",
                    turbine_id=f"turbine-{scope}",
                    data_scope="knowledge",
                    title=f"Synthetic reference {scope} {number}",
                    document_type="manual",
                    body=f"Synthetic load reference {scope} {number}; not field guidance.",
                    citation_uri=f"urn:openvigil:synthetic:{scope}:{number}",
                    document_version="1",
                    metadata={"classification": "synthetic", "generator": GENERATOR},
                    ingestion_status="pending",
                    vectorized=False,
                    created_by="synthetic-fixture",
                    created_at=START,
                    updated_at=START,
                )
            )
        for kind in RESOURCE_TYPES:
            data[Resource].append(
                dict(
                    id=f"load-resource-{scope}-{kind}",
                    resource_type=kind,
                    name=f"Synthetic {scope} {kind}",
                    quantity=1,
                    status="reserved",
                    updated_at=START,
                )
            )
            data[ResourceReservation].append(
                dict(
                    id=f"load-reservation-{scope}-{kind}",
                    mission_id=f"load-mission-{scope}",
                    resource_id=f"load-resource-{scope}-{kind}",
                    quantity=1,
                    created_at=START,
                )
            )
        data[WeatherWindow].append(
            dict(
                id=f"load-weather-{scope}",
                wind_farm_id=f"farm-{scope}",
                starts_at=START,
                ends_at=START + timedelta(hours=3),
                wind_speed_ms=5.0,
                wave_height_m=0.5,
                suitable=True,
                attributes={"visibility_km": 10.0},
            )
        )
    return [(model, data[model]) for model in MODELS if data[model]]


async def seed_services_dataset(engine: AsyncEngine, rows: int) -> dict[str, Any]:
    """Atomically seed empty, already migrated tables; never erase/reuse data."""
    _check_rows(rows)
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        raise ValueError("unsupported services dataset dialect")
    digest = hashlib.sha256()
    expected_counts = {_table(model).name: 0 for model in MODELS}
    async with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            enabled = (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar()
            if enabled != 1:
                raise RuntimeError("foreign keys must be enabled")
        else:
            await connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            # Closed model list, quoted by the dialect; no caller SQL identifiers.
            names = ", ".join(
                engine.dialect.identifier_preparer.quote(_table(model).name)
                for model in sorted(MODELS, key=lambda model: _table(model).name)
            )
            await connection.exec_driver_sql(f"LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE")
        for model in MODELS:
            if await connection.scalar(select(_table(model).c[0]).limit(1)) is not None:
                raise ValueError("services dataset requires empty seed tables")
        static = _static_rows()
        # Parents before telemetry; alarms deliberately reference real sample receipts.
        for model, batch in static[:4]:
            await connection.execute(insert(_table(model)), batch)
            expected_counts[_table(model).name] = len(batch)
            digest.update(canonical_hash([_table(model).name, _wire(batch)]).encode())
        for offset in range(0, rows, 1000):
            batch = [sample_row(index) for index in range(offset, min(rows, offset + 1000))]
            receipts = []
            for row in batch:
                row_hash = canonical_hash(_wire(row))
                digest.update(row_hash.encode())
                receipts.append(
                    dict(
                        source_event_id=row["source_event_id"],
                        source_id=row["source_id"],
                        payload_hash=row_hash,
                        received_at=row["received_at"],
                    )
                )
            await connection.execute(insert(IngestReceipt), receipts)
            await connection.execute(insert(ScadaSample), batch)
        expected_counts["ingest_receipts"] = expected_counts["scada_samples"] = rows
        for model, batch in static[4:]:
            await connection.execute(insert(_table(model)), batch)
            expected_counts[_table(model).name] = len(batch)
            digest.update(canonical_hash([_table(model).name, _wire(batch)]).encode())
        for model in MODELS:
            actual = await connection.scalar(select(func.count()).select_from(model))
            if actual != expected_counts[_table(model).name]:
                raise RuntimeError("services dataset count mismatch")
        if (
            engine.dialect.name == "sqlite"
            and (await connection.exec_driver_sql("PRAGMA foreign_key_check")).first()
        ):
            raise RuntimeError("services dataset foreign key mismatch")
    return {
        "generator": GENERATOR,
        "classification": "synthetic",
        "history_rows": rows,
        "counts": expected_counts,
        "input_sha256": digest.hexdigest(),
        "window": history_query(rows, "a"),
        "schema_created": False,
        "foreign_keys_verified": True,
        "postgresql_runtime": "not_verified_here",
        "ocr_runtime": "not_verified",
        "oracle_contract_sha256": ServicesDatasetOracle(
            rows, dialect=cast(Literal["postgresql", "sqlite"], engine.dialect.name)
        ).contract_sha256,
    }


async def snapshot_services_dataset(engine: AsyncEngine) -> dict[str, Any]:
    """Hash only the fixed seed tables, allowing new documents/outbox/audits.

    All original seed documents remain included, so alteration/deletion is detected.
    Other tables include all rows; newly appended non-document business rows therefore
    also change the snapshot. No table names or predicates can be supplied by callers.
    """
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        raise ValueError("unsupported services dataset dialect")
    summaries: dict[str, Any] = {}
    async with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            await connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
        else:
            await connection.exec_driver_sql("BEGIN")
        for model in MODELS:
            table = _table(model)
            statement = select(table).order_by(*table.primary_key.columns)
            if model is KnowledgeDocument:
                statement = statement.where(KnowledgeDocument.id.in_(SEED_DOCUMENT_IDS))
            digest, count = hashlib.sha256(), 0
            async with connection.stream(statement) as stream:
                async for row in stream.mappings():
                    digest.update(canonical_hash(_wire(dict(row))).encode())
                    count += 1
            summaries[table.name] = {"count": count, "sha256": digest.hexdigest()}
    return {
        "tables": summaries,
        "sha256": canonical_hash(summaries),
        "scope": "fixed_seed_tables_and_seed_document_ids",
    }


def _matches(actual: Any, expected: Any) -> bool:
    """Exact shape, tolerant finite floats and explicit UTC timestamp semantics."""
    if isinstance(expected, datetime):
        if not isinstance(actual, str):
            return False
        try:
            parsed = datetime.fromisoformat(actual.replace("Z", "+00:00"))
        except ValueError:
            return False
        # SQLite ORM datetimes serialize naive; that dialect's persisted values
        # are UTC. PostgreSQL strictness is enforced before this comparator.
        return parsed.replace(tzinfo=parsed.tzinfo or UTC) == expected
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(_matches(actual[key], value) for key, value in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_matches(item, target) for item, target in zip(actual, expected, strict=True))
        )
    if isinstance(expected, float):
        return (
            type(actual) in (float, int)
            and math.isfinite(actual)
            and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-10)
        )
    return type(actual) is type(expected) and actual == expected


def _document_expected(scope: str, number: int) -> dict[str, Any]:
    return dict(
        document_id=f"load-document-{scope}-{number}",
        tenant_id=f"tenant-{scope}",
        wind_farm_id=f"farm-{scope}",
        turbine_id=f"turbine-{scope}",
        data_scope="knowledge",
        title=f"Synthetic reference {scope} {number}",
        document_type="manual",
        document_version="1",
        citation_uri=f"urn:openvigil:synthetic:{scope}:{number}",
        artifact_uri=None,
        artifact_sha256=None,
        content_type=None,
        content_size_bytes=None,
        metadata={"classification": "synthetic", "generator": GENERATOR},
        source_layout=None,
        parse_state=None,
        ingestion_status="pending",
        vectorized=False,
        embedding_provider=None,
        embedding_model=None,
        indexed_at=None,
        created_by="synthetic-fixture",
        created_at=START,
        updated_at=START,
    )


def _diagnosis_expected(scope: str) -> dict[str, Any]:
    mission, alarm, evidence = (
        f"load-mission-{scope}",
        f"load-alarm-{scope}",
        f"load-evidence-{scope}",
    )
    return {
        "id": f"DX-{mission}",
        "turbineId": f"turbine-{scope}",
        "turbineStatus": "running",
        "healthScore": 86.0,
        "risk": "high",
        "status": "scheduled",
        "headline": f"Synthetic conclusion {scope}",
        "overallConfidencePercent": 82,
        "missionId": mission,
        "activeAlarmCount": 1,
        "updatedAt": START,
        "anomaly": {
            "source": "alarm-correlation",
            "detectedAt": START,
            "anomalyScore": 0.8,
            "threshold": 40.0,
            "summary": f"Synthetic alarm {scope}",
            "signals": [
                {
                    "id": f"{alarm}:primary",
                    "label": "active_power",
                    "value": 42.5,
                    "baseline": 42.5,
                    "unit": "MW",
                    "deltaPercent": 0,
                    "abnormal": True,
                }
            ],
        },
        "candidates": [
            {
                "id": f"{mission}:candidate:1",
                "faultMode": "Synthetic test condition",
                "subsystem": "generator",
                "probabilityPercent": 82,
                "confidencePercent": 82,
                "supportingEvidenceIds": [evidence],
                "supportingSummary": [f"Synthetic evidence {scope}"],
                "counterEvidence": [],
                "recommendedNextStep": (
                    "Complete human review before any controlled maintenance action."
                ),
            }
        ],
        "citations": [
            {
                "id": evidence,
                "title": "synthetic-input",
                "sourceLabel": "synthetic",
                "sourceType": "synthetic",
                "observedAt": START,
                "summary": f"Synthetic evidence {scope}",
                "href": f"/missions/{mission}#evidence",
                "deepLink": True,
            }
        ],
        "collaboration": [],
        "gate": {
            "state": "authorized",
            "humanReviewRequired": False,
            "title": "Governed human approval gate",
            "explanation": (
                "Execution remains controlled by the persisted decision and approval state."
            ),
            "allowedActions": ["review evidence", "record approval"],
            "blockedActions": [],
            "decisionStatus": "approved",
            "workOrderStatus": "scheduled",
        },
    }


def _maintenance_expected(scope: str) -> dict[str, Any]:
    def resource(kind: str) -> dict[str, Any]:
        return dict(
            id=f"load-resource-{scope}-{kind}",
            label=f"Synthetic {scope} {kind}",
            state="reserved",
            detail="Quantity 1; source is the PostgreSQL resource ledger.",
        )

    return dict(
        id=f"PLAN-load-order-{scope}",
        workOrderId=f"load-order-{scope}",
        turbineId=f"turbine-{scope}",
        turbineHealthScore=86.0,
        issue=f"Synthetic maintenance {scope}",
        description="Synthetic inspection only",
        assignedTeam=f"Synthetic team {scope}",
        teamKey=f"Synthetic team {scope}",
        priority="high",
        risk="high",
        workOrderStatus="scheduled",
        status="ready",
        startsAt=START,
        endsAt=START + timedelta(hours=2),
        deadline=START + timedelta(hours=3),
        updatedAt=START,
        dayKey=START.date().isoformat(),
        durationHours=2.0,
        taskProgressPercent=50,
        weather=dict(
            id=f"load-weather-{scope}",
            state="suitable",
            coverage="full",
            startsAt=START,
            endsAt=START + timedelta(hours=3),
            windSpeedMps=5.0,
            waveHeightM=0.5,
            visibilityKm=10.0,
            reason="Governed suitable window selected.",
        ),
        crew=resource("crew"),
        vessels=[resource("vessel")],
        spareParts=[resource("spare_part")],
        tools=[resource("tool")],
        readinessPercent=100,
        conflicts=[],
        relatedMissionId=f"load-mission-{scope}",
        readOnly=False,
        source="postgresql-governed-schedule",
    )


class ServicesDatasetOracle:
    """Closed six-route oracle, derived solely from frozen generator inputs.

    Expected records are never hydrated from HTTP output or queried from the database.
    Each validator checks its own route's full persisted semantic content and paging.
    """

    def __init__(
        self, rows: int, *, dialect: Literal["postgresql", "sqlite"] = "postgresql"
    ) -> None:
        _check_rows(rows)
        if dialect not in {"postgresql", "sqlite"}:
            raise ValueError("unsupported oracle dialect")
        self.dialect = dialect
        self.queries: dict[str, dict[str, dict[str, str]]] = {
            route: {user: {} for user in USER_SCOPES} for route in ROUTES
        }
        self._expected: dict[str, dict[str, Any]] = {route: {} for route in ROUTES}
        end = START + timedelta(minutes=(rows - 1) // 9 + 1)
        interval = max(1, math.ceil((end - START).total_seconds() / 100))
        origin = TIMESCALE_ORIGIN if dialect == "postgresql" else START
        for scope in "ab":
            # Bounded measurement page from index arithmetic, not a million-row sort.
            indexes = range(ord(scope) - ord("a"), rows, 3)
            total = len(indexes)
            last = list(indexes[max(0, total - 20) :])
            measurement_rows = [sample_row(index) for index in reversed(last)]
            measurements = [
                dict(
                    sample_id=row["id"],
                    source_event_id=row["source_event_id"],
                    source_id=row["source_id"],
                    source_sequence=row["source_sequence"],
                    turbine_id=row["turbine_id"],
                    variable=row["variable"],
                    label=row["variable"].replace("_", " "),
                    observed_at=row["observed_at"],
                    received_at=row["received_at"],
                    value=row["value"],
                    unit=row["unit"],
                    quality=row["quality"],
                    quality_code=None,
                    late=row["is_late"],
                    is_anomaly=False,
                )
                for row in measurement_rows
            ]
            buckets: dict[str, dict[int, dict[str, Any]]] = {name: {} for name, _ in VARIABLES}
            latest: dict[str, dict[str, Any]] = {}
            for index in indexes:
                row = sample_row(index)
                latest[row["variable"]] = row
                bucket = int((row["observed_at"] - origin).total_seconds()) // interval
                values = buckets[row["variable"]].setdefault(
                    bucket, {"sum": 0.0, "count": 0, "late": False, "uncertain": False}
                )
                values["sum"] += row["value"]
                values["count"] += 1
                values["late"] |= row["is_late"]
                values["uncertain"] |= row["quality"] == "uncertain"
            series: list[dict[str, Any]] = [
                dict(
                    series_id=f"source-{scope}:turbine-{scope}:{name}",
                    source_id=f"source-{scope}",
                    turbine_id=f"turbine-{scope}",
                    variable=name,
                    label=name.replace("_", " "),
                    unit=dict(VARIABLES)[name],
                    precision=2,
                    normal_range=[0.0, 0.0],
                    thresholds=[],
                    points=[
                        dict(
                            timestamp=origin + timedelta(seconds=bucket * interval),
                            value=values["sum"] / values["count"],
                            quality="uncertain" if values["uncertain"] else "good",
                            late=values["late"],
                            is_anomaly=False,
                        )
                        for bucket, values in sorted(buckets[name].items())
                    ],
                )
                for name in sorted(buckets)
            ]
            self._expected["scada/history"][scope] = {
                "data": series,
                "meta": dict(
                    count=3,
                    point_count=sum(len(item["points"]) for item in series),
                    turbine_id=f"turbine-{scope}",
                    range="CUSTOM",
                    interval_seconds=interval,
                    starts_at=START,
                    ends_at=end,
                    deterministic=False,
                ),
            }
            self._expected["scada/measurements"][scope] = {
                "measurements": measurements,
                "meta": {
                    "total": total,
                    "offset": 0,
                    "limit": 20,
                    "turbine_id": f"turbine-{scope}",
                    "variable": None,
                    "from": START,
                    "to": end,
                },
            }
            turbine_rows = []
            for suffix in ("", "-extra"):
                turbine_rows.append(
                    dict(
                        turbine_id=f"turbine-{scope}{suffix}",
                        wind_farm_id=f"farm-{scope}",
                        model=GENERATOR,
                        status="running",
                        health_score=86.0,
                        manufacturer=None,
                        latitude=None,
                        longitude=None,
                        active_power_mw=None if suffix else latest["active_power"]["value"],
                        wind_speed_ms=None if suffix else latest["wind_speed"]["value"],
                        rotor_speed_rpm=None if suffix else latest["rotor_speed"]["value"],
                        nacelle_direction_deg=None,
                        availability_percent=None,
                        telemetry_observed_at=None
                        if suffix
                        else max(row["observed_at"] for row in latest.values()),
                        active_alarm_count=0 if suffix else 1,
                        current_mission_id=None if suffix else f"load-mission-{scope}",
                        last_maintenance_at=None,
                        next_inspection_at=None,
                        updated_at=START,
                    )
                )
            self._expected["turbines"][scope] = dict(
                count=2,
                next_cursor=None,
                turbines=turbine_rows,
                wind_farms=[
                    dict(
                        wind_farm_id=f"farm-{scope}",
                        name=f"Synthetic farm {scope}",
                        capacity_mw=10.0,
                    )
                ],
            )
            self._expected["knowledge/documents"][scope] = dict(
                count=2,
                next_cursor=None,
                documents=[_document_expected(scope, number) for number in (1, 2)],
            )
            for route, expected_row in (
                ("diagnoses", _diagnosis_expected(scope)),
                ("maintenance-plans", _maintenance_expected(scope)),
            ):
                self._expected[route][scope] = {
                    "data": [expected_row],
                    "meta": dict(
                        count=1,
                        total=1,
                        filtered_total=1,
                        offset=0,
                        limit=50,
                        has_more=False,
                        next_offset=None,
                        source="postgresql-governed-missions"
                        if route == "diagnoses"
                        else "postgresql-work-orders-resources-weather",
                    ),
                }
                self._expected[route][scope]["meta"][
                    "real_inference" if route == "diagnoses" else "read_only"
                ] = False
        for user, scope in USER_SCOPES.items():
            for route in ROUTES[:4]:
                self.queries[route][user] = {"limit": "50"}
            self.queries["scada/history"][user] = history_query(rows, scope)
            self.queries["scada/measurements"][user] = {
                key: value
                for key, value in history_query(rows, scope).items()
                if key != "points_per_series"
            }
            self.queries["scada/measurements"][user]["limit"] = "20"
        self.contract_sha256 = canonical_hash(
            {
                "generator": GENERATOR,
                "rows": rows,
                "dialect": dialect,
                "queries": self.queries,
                "expected": _wire(self._expected),
            }
        )

    def validate(self, route: str, value: object, user: str) -> bool:
        if route not in ROUTES or user not in USER_SCOPES or not isinstance(value, dict):
            return False
        actual = deepcopy(value)
        if route in {"diagnoses", "maintenance-plans", "scada/history", "scada/measurements"}:
            meta = actual.get("meta")
            if not isinstance(meta, dict):
                return False
            timestamp = meta.pop("snapshot_at", None)
            if not isinstance(timestamp, str):
                return False
            try:
                if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is None:
                    return False
            except ValueError:
                return False
            if route in {"diagnoses", "maintenance-plans"}:
                boundaries = meta.pop("related_data_boundaries", None)
                if not isinstance(boundaries, dict) or not _valid_boundaries(route, boundaries):
                    return False
        expected = self._expected[route][USER_SCOPES[user]]
        if self.dialect == "postgresql" and not _aware_dates(actual, expected):
            return False
        return _matches(actual, expected)


def _aware_dates(actual: Any, expected: Any) -> bool:
    if isinstance(expected, datetime):
        if not isinstance(actual, str):
            return False
        try:
            return datetime.fromisoformat(actual.replace("Z", "+00:00")).tzinfo is not None
        except ValueError:
            return False
    if isinstance(expected, dict) and isinstance(actual, dict):
        return all(_aware_dates(actual.get(key), value) for key, value in expected.items())
    if isinstance(expected, list) and isinstance(actual, list):
        return len(actual) == len(expected) and all(
            _aware_dates(item, target) for item, target in zip(actual, expected, strict=True)
        )
    return True


def _valid_boundaries(route: str, value: dict[str, Any]) -> bool:
    if route == "diagnoses":
        return value == {
            key: {
                "strategy": "latest_per_mission",
                "per_mission_limit": 1,
                "truncated_mission_count": 0,
            }
            for key in ("evidence", "agent_executions")
        }
    return value == {
        "tasks": {
            "strategy": "aggregate_only",
            "detail": "Only total and completed counts are loaded for page rows.",
        },
        "reservations": {
            "strategy": "latest_per_mission_and_resource_type",
            "per_mission_type_limit": 1,
            "truncated_group_count": 0,
        },
        "weather_windows": {
            "strategy": "suitable_first_then_earliest_overlap",
            "per_work_order_limit": 1,
            "deterministic_order": ["starts_at_asc", "ends_at_asc", "id_asc"],
            "truncated_work_order_count": 0,
        },
    }
