"""SQLite insertion and real ASGI contracts, never a PostgreSQL/Timescale receipt."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, func, insert, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from windops_backend.config import IdentityAccessScope
from windops_backend.main import create_app
from windops_backend.model_base import Base
from windops_backend.models import KnowledgeDocument, Resource, ScadaSample, WorkOrder
from windops_backend.operations.load_dataset import SUBJECTS, USER_SCOPES
from windops_backend.operations.load_services_dataset import (
    MODELS,
    ROUTES,
    START,
    TIMESCALE_ORIGIN,
    ServicesDatasetOracle,
    seed_services_dataset,
    snapshot_services_dataset,
)
from windops_backend.operations.load_software_server import software_settings

KEYS = {user: f"{index:064x}" for index, user in enumerate(USER_SCOPES, start=1)}


@pytest_asyncio.fixture
async def migrated_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """SQLite metadata is a test setup, not production migrations or PG proof."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'seed.sqlite3').as_posix()}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_real_relations_counts_reproducibility_and_no_ddl(
    migrated_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(migrated_engine.sync_engine, "before_cursor_execute", capture)
    first = await seed_services_dataset(migrated_engine, 1809)
    assert not any(
        item.lstrip().upper().startswith(("CREATE", "DROP", "DELETE", "ALTER"))
        for item in statements
    )
    # The entire closed write set must be checked before the first insertion.
    prefix = statements[
        : next(index for index, item in enumerate(statements) if item.startswith("INSERT"))
    ]
    for model in MODELS:
        assert any(f"FROM {model.__tablename__}" in statement for statement in prefix)
    event.remove(migrated_engine.sync_engine, "before_cursor_execute", capture)
    async with migrated_engine.connect() as connection:
        assert not (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
        for model in MODELS:
            assert (
                await connection.scalar(select(func.count()).select_from(model))
                == first["counts"][model.__tablename__]
            )
        assert first["counts"]["scada_samples"] == 1809
        assert first["counts"]["work_orders"] == 3
        assert first["counts"]["knowledge_documents"] == 6
        assert first["counts"]["resource_reservations"] == 12
    assert first["schema_created"] is False
    assert first["classification"] == "synthetic"
    assert first["postgresql_runtime"] == "not_verified_here"
    second_engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'second.sqlite3').as_posix()}"
    )
    try:
        async with second_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        second = await seed_services_dataset(second_engine, 1809)
        assert second == first
        assert await snapshot_services_dataset(second_engine) == await snapshot_services_dataset(
            migrated_engine
        )
    finally:
        await second_engine.dispose()


@pytest.mark.asyncio
async def test_seed_exclusive_and_late_table_occupied_is_not_cleared(
    migrated_engine: AsyncEngine,
) -> None:
    async with migrated_engine.begin() as connection:
        await connection.execute(
            insert(Resource), [{"id": "existing", "resource_type": "tool", "name": "Keep"}]
        )
    before = await snapshot_services_dataset(migrated_engine)
    with pytest.raises(ValueError, match="empty seed tables"):
        await seed_services_dataset(migrated_engine, 90)
    assert await snapshot_services_dataset(migrated_engine) == before


@pytest.mark.asyncio
async def test_concurrent_seed_has_one_winner_and_second_call_preserves_rows(
    migrated_engine: AsyncEngine,
) -> None:
    results = await asyncio.gather(
        *(seed_services_dataset(migrated_engine, 90) for _ in range(2)), return_exceptions=True
    )
    assert sum(isinstance(value, dict) for value in results) == 1
    assert sum(isinstance(value, ValueError) for value in results) == 1
    before = await snapshot_services_dataset(migrated_engine)
    with pytest.raises(ValueError, match="empty seed tables"):
        await seed_services_dataset(migrated_engine, 90)
    assert await snapshot_services_dataset(migrated_engine) == before


@pytest.mark.asyncio
async def test_insert_failure_rolls_back_every_table(migrated_engine: AsyncEngine) -> None:
    def fail(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.startswith("INSERT INTO work_order_tasks"):
            raise RuntimeError("owned injected insert failure")

    event.listen(migrated_engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="owned injected"):
            await seed_services_dataset(migrated_engine, 90)
    finally:
        event.remove(migrated_engine.sync_engine, "before_cursor_execute", fail)
    snapshot = await snapshot_services_dataset(migrated_engine)
    assert all(value["count"] == 0 for value in snapshot["tables"].values())
    assert (await seed_services_dataset(migrated_engine, 90))["history_rows"] == 90


@pytest.mark.asyncio
async def test_requires_existing_schema_and_valid_rows_before_database_io(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    try:
        for rows in (False, 89, 1_000_001, 90.0):
            with pytest.raises(ValueError, match="integer rows"):
                await seed_services_dataset(engine, rows)  # type: ignore[arg-type]
            assert not database.exists()
        with pytest.raises(OperationalError, match="no such table"):
            await seed_services_dataset(engine, 90)
        async with engine.connect() as connection:
            assert not (
                await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ).all()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_allows_new_document_but_detects_original_change_and_deletion(
    migrated_engine: AsyncEngine,
) -> None:
    await seed_services_dataset(migrated_engine, 90)
    before = await snapshot_services_dataset(migrated_engine)
    async with migrated_engine.begin() as connection:
        await connection.execute(
            insert(KnowledgeDocument),
            [
                {
                    "id": "new-owned-upload",
                    "title": "New fixture",
                    "document_type": "manual",
                    "body": "Fixture",
                    "citation_uri": "urn:fixture",
                }
            ],
        )
    assert await snapshot_services_dataset(migrated_engine) == before
    async with migrated_engine.begin() as connection:
        await connection.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id == "load-document-a-1")
            .values(body="Changed")
        )
    altered = await snapshot_services_dataset(migrated_engine)
    assert altered != before
    assert altered["tables"]["knowledge_documents"]["count"] == 6
    async with migrated_engine.begin() as connection:
        await connection.exec_driver_sql(
            "DELETE FROM knowledge_documents WHERE id='load-document-a-1'"
        )
    removed = await snapshot_services_dataset(migrated_engine)
    assert removed["tables"]["knowledge_documents"]["count"] == 5
    assert removed != altered


@pytest_asyncio.fixture
async def services_api(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, AsyncEngine]]:
    await seed_services_dataset(migrated_engine, 1809)
    settings = software_settings(tmp_path / "seed.sqlite3", KEYS)
    scopes = {
        SUBJECTS[user]: IdentityAccessScope(
            tenant_ids=[f"tenant-{scope}"],
            wind_farm_ids=[f"farm-{scope}"],
            turbine_ids=[],
            data_scopes=[
                "asset",
                "telemetry",
                "alarm",
                "mission",
                "decision",
                "work_order",
                "resource",
                "knowledge",
            ],
        )
        for user, scope in USER_SCOPES.items()
    }
    settings = settings.model_copy(update={"identity_scope_mappings": scopes})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", trust_env=False
        ) as client:
            yield client, migrated_engine


@pytest.mark.asyncio
async def test_six_real_api_routes_four_subjects_complete_oracles_and_readonly_business(
    services_api: tuple[httpx.AsyncClient, AsyncEngine],
) -> None:
    client, engine = services_api
    oracle = ServicesDatasetOracle(1809, dialect="sqlite")
    before = await snapshot_services_dataset(engine)
    for user, scope in USER_SCOPES.items():
        headers = {"Authorization": f"Bearer {KEYS[user]}"}
        for route in ROUTES:
            response = await client.get(
                f"/api/v1/{route}", headers=headers, params=oracle.queries[route][user]
            )
            assert response.status_code == 200, (route, response.text)
            assert oracle.validate(route, response.json(), user), (route, response.json())
            assert not oracle.validate(route, response.json(), "b1" if scope == "a" else "a1")
        for denied in ("c", "b" if scope == "a" else "a"):
            response = await client.get(
                "/api/v1/scada/history",
                headers=headers,
                params={**oracle.queries["scada/history"][user], "turbine_id": f"turbine-{denied}"},
            )
            assert response.status_code == 404
            response = await client.get(
                "/api/v1/scada/measurements",
                headers=headers,
                params={
                    **oracle.queries["scada/measurements"][user],
                    "turbine_id": f"turbine-{denied}",
                },
            )
            assert response.status_code == 200
            assert response.json()["measurements"] == []
            assert response.json()["meta"]["total"] == 0
            assert not oracle.validate("scada/measurements", response.json(), user)
    assert await snapshot_services_dataset(engine) == before
    assert (await client.get("/api/v1/turbines")).status_code == 401
    assert (
        await client.get(
            "/api/v1/turbines", headers={"X-WindOps-Test-Principal": "integration-test-system"}
        )
    ).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_oracles_reject_value_count_order_scope_and_contract_corruption(
    services_api: tuple[httpx.AsyncClient, AsyncEngine],
    route: str,
) -> None:
    client, _ = services_api
    oracle = ServicesDatasetOracle(1809, dialect="sqlite")
    response = await client.get(
        f"/api/v1/{route}",
        headers={"Authorization": f"Bearer {KEYS['a1']}"},
        params=oracle.queries[route]["a1"],
    )
    assert response.status_code == 200
    original = response.json()
    assert oracle.validate(route, original, "a1")
    changed = copy.deepcopy(original)
    match route:
        case "turbines":
            changed["turbines"][0]["active_power_mw"] += 0.1
        case "diagnoses":
            changed["data"][0]["anomaly"]["signals"][0]["value"] += 0.1
        case "maintenance-plans":
            changed["data"][0]["durationHours"] += 0.1
        case "knowledge/documents":
            changed["documents"][0]["vectorized"] = True
        case "scada/history":
            changed["data"][0]["points"][0]["value"] += 0.1
        case "scada/measurements":
            changed["measurements"][0]["value"] += 0.1
    assert not oracle.validate(route, changed, "a1")
    array_key = (
        "turbines"
        if route == "turbines"
        else "documents"
        if route == "knowledge/documents"
        else "measurements"
        if route == "scada/measurements"
        else "data"
    )
    for mutate in (
        lambda values: values.pop(),
        lambda values: values.append(copy.deepcopy(values[0])),
    ):
        changed = copy.deepcopy(original)
        mutate(changed[array_key])
        assert not oracle.validate(route, changed, "a1")
    if len(original[array_key]) > 1:
        changed = copy.deepcopy(original)
        changed[array_key].reverse()
        assert not oracle.validate(route, changed, "a1")
    changed = copy.deepcopy(original)
    changed[array_key][0]["unrecognized_semantics"] = True
    assert not oracle.validate(route, changed, "a1")
    assert not oracle.validate(route, original, "unknown")
    assert not oracle.validate(route, None, "a1")


@pytest.mark.asyncio
async def test_real_database_changes_fail_oracles_and_snapshot(
    services_api: tuple[httpx.AsyncClient, AsyncEngine],
) -> None:
    client, engine = services_api
    oracle = ServicesDatasetOracle(1809, dialect="sqlite")
    before = await snapshot_services_dataset(engine)
    async with engine.begin() as connection:
        await connection.execute(
            update(ScadaSample)
            .where(ScadaSample.id == "sample-0001806")
            .values(value=100.0, quality="bad")
        )
        await connection.execute(
            update(WorkOrder)
            .where(WorkOrder.id == "load-order-a")
            .values(estimated_duration_hours=20.0)
        )
    for route in ("turbines", "scada/history", "scada/measurements", "maintenance-plans"):
        response = await client.get(
            f"/api/v1/{route}",
            headers={"Authorization": f"Bearer {KEYS['a1']}"},
            params=oracle.queries[route]["a1"],
        )
        assert response.status_code == 200
        assert not oracle.validate(route, response.json(), "a1")
    after = await snapshot_services_dataset(engine)
    assert after["tables"]["scada_samples"] != before["tables"]["scada_samples"]
    assert after["tables"]["work_orders"] != before["tables"]["work_orders"]


@pytest.mark.asyncio
async def test_timescale_origin_is_separate_from_actual_sqlite_aggregation(
    services_api: tuple[httpx.AsyncClient, AsyncEngine],
) -> None:
    client, _ = services_api
    sqlite = ServicesDatasetOracle(1809, dialect="sqlite")
    postgres = ServicesDatasetOracle(1809, dialect="postgresql")
    response = await client.get(
        "/api/v1/scada/history",
        headers={"Authorization": f"Bearer {KEYS['a1']}"},
        params=sqlite.queries["scada/history"]["a1"],
    )
    assert sqlite.validate("scada/history", response.json(), "a1")
    assert not postgres.validate("scada/history", response.json(), "a1")
    assert postgres.contract_sha256 != sqlite.contract_sha256
    # Pure independent arithmetic evidence only: no PG driver/network is used.
    offset = int((START - TIMESCALE_ORIGIN).total_seconds()) % 121
    assert offset != 0
    pg_expected = postgres._expected["scada/history"]["a"]
    first = pg_expected["data"][0]["points"][0]
    assert first["timestamp"] == START - timedelta(seconds=offset)
    expected_ticks = [tick for tick in range(3) if tick * 60 < 121 - offset]
    assert first["value"] == pytest.approx(
        sum(1.0 + tick / 10 for tick in expected_ticks) / len(expected_ticks)
    )
    assert first["quality"] == "uncertain"
    assert first["late"] is True
    assert postgres.queries["scada/history"]["a1"]["to"] == "2026-01-01T03:21:00+00:00"


def test_oracle_contract_changes_with_rows_and_rejects_invalid_contracts() -> None:
    assert ServicesDatasetOracle(90).contract_sha256 != ServicesDatasetOracle(91).contract_sha256
    for value in (True, 89, 1_000_001):
        with pytest.raises(ValueError):
            ServicesDatasetOracle(value)
    with pytest.raises(ValueError, match="dialect"):
        ServicesDatasetOracle(90, dialect="unknown")  # type: ignore[arg-type]
    assert not ServicesDatasetOracle(90).validate("unexpected", {}, "a1")
