"""Private, isolated services configuration and request-bound synthetic identities.

The API uses DEVELOPMENT to select real storage, Redis auditing and authentication.
Workers use TEST solely for deterministic embeddings; no production validation is
bypassed or mutated. These settings cannot be used as release qualification.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import jwt
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from windops_backend.config import Settings
from windops_backend.operations.load_dataset import USER_SCOPES
from windops_backend.operations.load_runner import PrivateUser

Role = Literal["api", "general_worker", "relay", "read_audit", "document_worker"]
ROLES: tuple[Role, ...] = ("api", "general_worker", "relay", "read_audit", "document_worker")
SUBJECTS = {alias: f"performance-{alias}" for alias in USER_SCOPES}
USER_ROLES = {
    "a1": "operations_manager",
    "a2": "operations_approver",
    "b1": "operations_manager",
    "b2": "field_technician",
}
ISSUER = "windops-sites-gateway"
AUDIENCE = "windops-python-backend"
BYPASS_CONTROL_HEADERS = {
    "X-WindOps-Test-Principal": "performance-a1",
    "X-WindOps-Test-Role": "operations_manager",
}


class IsolatedServicesSettings(Settings):
    """Only explicit settings, never inherited WINDOPS variables or dotenv secrets."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


@dataclass(frozen=True)
class ServicesEndpoints:
    """Ports discovered from this run's freshly owned Compose project."""

    postgres: int
    redis: int
    minio: int
    neo4j: int

    def __post_init__(self) -> None:
        values = (self.postgres, self.redis, self.minio, self.neo4j)
        if len(set(values)) != 4 or any(type(v) is not int or not 1 <= v <= 65535 for v in values):
            raise ValueError("four distinct owned loopback ports are required")


def services_settings(
    *,
    role: Role,
    endpoints: ServicesEndpoints,
    password: str,
    delegation_secret: str,
    commit_sha: str,
    parser_python: Path | None = None,
    parser_models: Path | None = None,
    parser_manifest_sha256: str | None = None,
    parser_runtime: Path | None = None,
) -> Settings:
    if role not in ROLES:
        raise ValueError("unknown services role")
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (password, delegation_secret)):
        raise ValueError("fresh private service credentials are required")
    if password == delegation_secret or re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None:
        raise ValueError("independent credentials and an actual source revision are required")
    parser = (parser_python, parser_models, parser_manifest_sha256, parser_runtime)
    if any(value is not None for value in parser) and any(value is None for value in parser):
        raise ValueError("complete parser configuration is required")
    values: dict[str, Any] = {
        "environment": "development" if role == "api" else "test",
        "database_url": f"postgresql+asyncpg://windops:{password}@127.0.0.1:{endpoints.postgres}/windops",
        "redis_url": f"redis://127.0.0.1:{endpoints.redis}/0",
        "minio_endpoint": f"127.0.0.1:{endpoints.minio}",
        "minio_public_base": f"http://127.0.0.1:{endpoints.minio}",
        "minio_access_key": "windops",
        "minio_secret_key": password,
        "minio_secure": False,
        "neo4j_uri": f"bolt://127.0.0.1:{endpoints.neo4j}",
        "neo4j_password": password,
        "knowledge_graph_backend": "neo4j",
        "schema_bootstrap": False,
        "demo_seed": False,
        "knowledge_graph_rebuild_on_startup": False,
        "agent_mode": "deterministic",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 1536,
        "outbox_inline_drain": False,
        "auth_mode": "sites_delegation",
        "gateway_delegation_secret": delegation_secret,
        "test_auth_bypass_enabled": False,
        "docs_enabled": False,
        "rate_limit_enabled": True,
        "otel_enabled": False,
        "local_acceptance": False,
        "release_id": "performance-synthetic-services",
        "release_commit_sha": commit_sha,
        "release_image_digest": "",
        "identity_role_mappings": {SUBJECTS[a]: [USER_ROLES[a]] for a in USER_SCOPES},
        "identity_scope_mappings": {
            SUBJECTS[alias]: {
                "tenant_ids": [f"tenant-{scope}"],
                "wind_farm_ids": [f"farm-{scope}"],
                # A nonempty turbine grant intentionally excludes farm-level
                # planning joins in the actual authorization implementation.
                # The complete domain here is one explicit synthetic wind farm.
                "turbine_ids": [],
                "data_scopes": [
                    "asset",
                    "telemetry",
                    "alarm",
                    "mission",
                    "decision",
                    "work_order",
                    "resource",
                    "knowledge",
                ],
                "allow_global": False,
            }
            for alias, scope in USER_SCOPES.items()
        },
        "document_parser_enabled": parser_python is not None,
        "document_parser_python": str(parser_python) if parser_python is not None else None,
        "document_parser_models_path": parser_models,
        "document_parser_model_manifest_sha256": parser_manifest_sha256,
        "document_parser_runtime_directory": parser_runtime,
    }
    return IsolatedServicesSettings(**values)


@dataclass(frozen=True)
class RequestSigner:
    alias: str
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.alias not in SUBJECTS or re.fullmatch(r"[0-9a-f]{64}", self.secret) is None:
            raise ValueError("invalid private service identity")

    def __call__(self, method: str, target: str, body: bytes) -> Mapping[str, str]:
        parsed = urlsplit(target)
        if (
            method not in {"GET", "POST"}
            or not target.isascii()
            or not target.startswith("/api/v1/")
            or parsed.netloc
            or parsed.scheme
            or parsed.fragment
            or any(ord(c) < 33 or ord(c) == 127 for c in target)
            or not isinstance(body, bytes)
            or len(body) > 2 * 1024 * 1024
        ):
            raise ValueError("unsupported private service request")
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": SUBJECTS[self.alias],
                "iat": now,
                "exp": now + 60,
                "jti": uuid4().hex,
                "method": method,
                "target": target,
                "body_sha256": hashlib.sha256(body).hexdigest(),
            },
            self.secret,
            algorithm="HS256",
        )
        # This stable key partitions rate limits; authorization still derives only
        # from the server-verified token and server-owned scope mappings.
        return {"Authorization": f"Bearer {token}", "X-Windops-User-Id": SUBJECTS[self.alias]}


def services_users(secret: str) -> tuple[PrivateUser, ...]:
    return tuple(
        PrivateUser(alias, {}, headers_for_request=RequestSigner(alias, secret))
        for alias in USER_SCOPES
    )
