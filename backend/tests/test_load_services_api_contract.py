"""Real ASGI auth/routes with service JWT mappings and a SQLite external-adapter fixture.

The service API remains DEVELOPMENT with sites_delegation and test bypass disabled. Only
SQLite, memory graph/storage, direct database read-auditing and disabled Redis rate
limiting replace unavailable external dependencies. No PG/Timescale/Redis/MinIO
or network/TLS performance qualification is claimed by these contract tests.
One explicit TEST/bypass-positive control proves the same negative-control headers
are capable of authenticating when that separate test-only switch is enabled.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from windops_backend.config import Settings
from windops_backend.identity import DelegatedIdentityAuthenticator
from windops_backend.main import create_app
from windops_backend.model_base import Base
from windops_backend.models import ReadAccessAudit
from windops_backend.operations.load_dataset import USER_SCOPES
from windops_backend.operations.load_runner import PrivateUser
from windops_backend.operations.load_services_dataset import (
    ROUTES,
    ServicesDatasetOracle,
    seed_services_dataset,
    snapshot_services_dataset,
)
from windops_backend.operations.load_services_settings import (
    BYPASS_CONTROL_HEADERS,
    SUBJECTS,
    USER_ROLES,
    ServicesEndpoints,
    services_settings,
    services_users,
)
from windops_backend.read_audit import DatabaseReadAuditSink
from windops_backend.storage import InMemoryArtifactVerifier


@dataclass
class ApiFixture:
    client: httpx.AsyncClient
    engine: AsyncEngine
    users: dict[str, PrivateUser]
    settings: Settings

    def request(
        self, alias: str, route: str, query: Mapping[str, str] | None = None
    ) -> httpx.Request:
        request = self.client.build_request("GET", f"/api/v1/{route}", params=query)
        request.headers.update(
            self.users[alias].request_headers(
                "GET", request.url.raw_path.decode("ascii"), request.content
            )
        )
        return request


@pytest_asyncio.fixture
async def api(tmp_path: Path) -> AsyncIterator[ApiFixture]:
    database = tmp_path / "services-contract.sqlite3"
    url = f"sqlite+aiosqlite:///{database.as_posix()}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        # Metadata bootstrap is fixture-only, not a production migration receipt.
        await connection.run_sync(Base.metadata.create_all)
    await seed_services_dataset(engine, 1809)
    secret = secrets.token_hex(32)
    original = services_settings(
        role="api",
        endpoints=ServicesEndpoints(43101, 43102, 43103, 43104),
        password=secrets.token_hex(32),
        delegation_secret=secret,
        commit_sha="a" * 40,
    )
    settings = original.model_copy(
        update={
            "database_url": url,
            "knowledge_graph_backend": "memory",
            "rate_limit_enabled": False,
        }
    )
    assert settings.environment == original.environment
    assert settings.environment.value == "development"
    assert settings.identity_scope_mappings == original.identity_scope_mappings
    assert settings.identity_role_mappings == original.identity_role_mappings
    assert settings.auth_mode == original.auth_mode == "sites_delegation"
    assert settings.test_auth_bypass_enabled is original.test_auth_bypass_enabled is False
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            # Preserve the actual authenticator and Principal/DataScope dependencies.
            assert isinstance(app.state.identity_authenticator, DelegatedIdentityAuthenticator)
            assert not app.dependency_overrides
            app.state.artifact_verifier = InMemoryArtifactVerifier()
            app.state.read_audit_sink = DatabaseReadAuditSink(app.state.session_factory)
            await app.state.redis_client.aclose()
            app.state.redis_client = None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                trust_env=False,
            ) as client:
                yield ApiFixture(
                    client, engine, {user.alias: user for user in services_users(secret)}, settings
                )
    finally:
        await engine.dispose()


async def test_actual_service_signed_four_subjects_six_gets_and_audited_roles(
    api: ApiFixture,
) -> None:
    oracle = ServicesDatasetOracle(1809, dialect="sqlite")
    before = await snapshot_services_dataset(api.engine)
    for alias, scope in USER_SCOPES.items():
        for route in ROUTES:
            response = await api.client.send(
                api.request(alias, route, oracle.queries[route][alias])
            )
            assert response.status_code == 200, (alias, route, response.status_code)
            assert oracle.validate(route, response.json(), alias), (alias, route)
            assert not oracle.validate(route, response.json(), "b1" if scope == "a" else "a1")
    assert await snapshot_services_dataset(api.engine) == before
    async with api.engine.connect() as connection:
        audits = (
            await connection.execute(
                select(
                    ReadAccessAudit.subject,
                    ReadAccessAudit.role,
                    ReadAccessAudit.method,
                    ReadAccessAudit.endpoint,
                )
            )
        ).all()
    assert len(audits) == 24
    assert {(row.subject, row.role) for row in audits} == {
        (SUBJECTS[alias], USER_ROLES[alias]) for alias in USER_SCOPES
    }
    for alias in USER_SCOPES:
        assert {row.endpoint for row in audits if row.subject == SUBJECTS[alias]} == {
            f"/api/v1/{route}" for route in ROUTES
        }
    assert all(row.method == "GET" for row in audits)


@pytest.mark.parametrize("alias", tuple(USER_SCOPES))
async def test_signed_scope_rejects_foreign_domain_and_c_without_data_leak(
    api: ApiFixture, alias: str
) -> None:
    oracle = ServicesDatasetOracle(1809, dialect="sqlite")
    scope = USER_SCOPES[alias]
    for denied in ("c", "b" if scope == "a" else "a"):
        response = await api.client.send(
            api.request(
                alias,
                "scada/history",
                {
                    **oracle.queries["scada/history"][alias],
                    "turbine_id": f"turbine-{denied}",
                },
            )
        )
        assert response.status_code == 404
        response = await api.client.send(
            api.request(
                alias,
                "scada/measurements",
                {
                    **oracle.queries["scada/measurements"][alias],
                    "turbine_id": f"turbine-{denied}",
                },
            )
        )
        assert response.status_code == 200
        assert response.json()["measurements"] == [] and response.json()["meta"]["total"] == 0
        response = await api.client.send(
            api.request(alias, f"knowledge/documents/load-document-{denied}-1")
        )
        assert response.status_code == 404
        for route in ("diagnoses", "maintenance-plans"):
            response = await api.client.send(
                api.request(alias, route, {"turbineId": f"turbine-{denied}"})
            )
            assert response.status_code == 200
            assert response.json()["data"] == [] and response.json()["meta"]["filtered_total"] == 0


@pytest.mark.parametrize("fault", ["signature", "target", "query", "body", "method"])
async def test_tampered_or_unbound_jwt_never_authenticates(api: ApiFixture, fault: str) -> None:
    request = api.request("a1", "turbines", {"name": "scope + / 中文"})
    target = request.url.raw_path.decode("ascii")
    if fault == "signature":
        auth = request.headers["authorization"]
        prefix, signature = auth.rsplit(".", 1)
        request.headers["authorization"] = (
            prefix + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
        )
    else:
        method = "POST" if fault == "method" else "GET"
        bound_target = "/api/v1/knowledge/documents" if fault == "target" else target
        if fault == "query":
            bound_target = target.replace("%2B", "%20")
            assert bound_target != target
        body = b"different request body" if fault == "body" else b""
        request.headers.update(api.users["a1"].request_headers(method, bound_target, body))
    response = await api.client.send(request)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_test_headers_and_stable_rate_alias_do_not_override_signed_scope(
    api: ApiFixture,
) -> None:
    oracle = ServicesDatasetOracle(1809, dialect="sqlite")
    for headers in ({}, BYPASS_CONTROL_HEADERS):
        assert (await api.client.get("/api/v1/turbines", headers=headers)).status_code == 401
    request = api.request("a1", "turbines", oracle.queries["turbines"]["a1"])
    request.headers.update(
        {
            "X-Windops-User-Id": SUBJECTS["b1"],
            "X-WindOps-Test-Principal": SUBJECTS["b1"],
            "X-WindOps-Test-Role": "test_system",
            "X-Windops-Test-Tenant-Ids": "tenant-c",
            "X-Windops-Test-Wind-Farm-Ids": "farm-c",
            "X-Windops-Test-Turbine-Ids": "turbine-c",
            "X-Windops-Test-Allow-Global": "true",
            "X-Windops-Test-Data-Scopes": "asset,telemetry,knowledge,mission",
        }
    )
    response = await api.client.send(request)
    assert response.status_code == 200
    assert oracle.validate("turbines", response.json(), "a1")
    assert not oracle.validate("turbines", response.json(), "b1")
    async with api.engine.connect() as connection:
        subjects = (
            await connection.execute(select(ReadAccessAudit.subject, ReadAccessAudit.role))
        ).all()
    assert [tuple(row) for row in subjects] == [(SUBJECTS["a1"], USER_ROLES["a1"])]


async def test_same_bypass_controls_work_only_in_explicit_test_configuration(
    api: ApiFixture,
) -> None:
    # Revalidate the TEST control settings; no service settings or scopes change.
    values = api.settings.model_dump()
    values.update(environment="test", test_auth_bypass_enabled=True)
    positive_settings = type(api.settings)(**values)
    assert positive_settings.identity_scope_mappings == api.settings.identity_scope_mappings
    assert positive_settings.identity_role_mappings == api.settings.identity_role_mappings
    assert positive_settings.auth_mode == api.settings.auth_mode == "sites_delegation"
    app = create_app(positive_settings)
    oracle = ServicesDatasetOracle(1809, dialect="sqlite")
    async with app.router.lifespan_context(app):
        assert not app.dependency_overrides
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            trust_env=False,
        ) as client:
            for route in ROUTES:
                params = oracle.queries[route]["a1"]
                assert (
                    await api.client.get(
                        f"/api/v1/{route}", params=params, headers=BYPASS_CONTROL_HEADERS
                    )
                ).status_code == 401
                response = await client.get(
                    f"/api/v1/{route}", params=params, headers=BYPASS_CONTROL_HEADERS
                )
                assert response.request.headers.get("authorization") is None
                assert response.status_code == 200
                assert oracle.validate(route, response.json(), "a1")
                assert not oracle.validate(route, response.json(), "b1")
