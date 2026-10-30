"""Actual request authentication; no services/OCR or provider execution claimed."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from windops_backend.agents.embeddings import DeterministicTestEmbeddingProvider
from windops_backend.identity import DelegatedIdentityAuthenticator, InvalidIdentityError
from windops_backend.operations.load_services_settings import (
    AUDIENCE,
    ISSUER,
    ROLES,
    SUBJECTS,
    RequestSigner,
    ServicesEndpoints,
    services_settings,
    services_users,
)
from windops_backend.services.knowledge import _embedding_provider

SECRET = "a" * 64


def test_exact_signature_and_new_nonce_for_every_request() -> None:
    auth = DelegatedIdentityAuthenticator(
        secrets=(SECRET,),
        issuer=ISSUER,
        audience=AUDIENCE,
        role_mappings={s: ["operations_manager"] for s in SUBJECTS.values()},
        clock_skew_seconds=0,
    )
    target = "/api/v1/knowledge/documents?name=%E4%B8%AD%E6%96%87+space&literal=%2B"
    body = b'{"title":"synthetic"}'
    ids = set()
    for user in services_users(SECRET):
        for _ in range(2):
            token = user.request_headers("POST", target, body)["Authorization"][7:]
            identity = auth.authenticate(
                token,
                request_method="POST",
                request_target=target,
                body_sha256=hashlib.sha256(body).hexdigest(),
            )
            assert identity.subject == SUBJECTS[user.alias]
            ids.add(identity.delegation_id)
            for method, changed_target, content in (
                ("GET", target, body),
                ("POST", target + "x", body),
                ("POST", target, body + b" "),
            ):
                with pytest.raises(InvalidIdentityError):
                    auth.authenticate(
                        token,
                        request_method=method,
                        request_target=changed_target,
                        body_sha256=hashlib.sha256(content).hexdigest(),
                    )
        assert SECRET not in repr(user) and "headers_for_request" not in repr(user)
    assert len(ids) == 8


@pytest.mark.parametrize(
    "method,target",
    [
        ("PUT", "/api/v1/knowledge/documents"),
        ("GET", "https://127.0.0.1:1234/api/v1/turbines"),
        ("GET", "/api/v1/turbines#secret"),
        ("GET", "/api/v1/turbines?q=raw space"),
        ("GET", "/api/v1/turbines?q=中文"),
    ],
)
def test_signer_cannot_authorize_object_store_or_unencoded_targets(
    method: str, target: str
) -> None:
    with pytest.raises(ValueError, match="unsupported private service request"):
        RequestSigner("a1", SECRET)(method, target, b"")


def test_settings_ignore_environment_and_preserve_actual_runtime_boundaries(monkeypatch) -> None:
    monkeypatch.setenv("WINDOPS_ENVIRONMENT", "production")
    monkeypatch.setenv("WINDOPS_DATABASE_URL", "external-private-url")
    monkeypatch.setenv("WINDOPS_EMBEDDING_API_KEY", "private-paid-key")
    monkeypatch.setenv("WINDOPS_RATE_LIMIT_ENABLED", "false")
    for role in ROLES:
        settings = services_settings(
            role=role,
            endpoints=ServicesEndpoints(17001, 17002, 17003, 17004),
            password="b" * 64,
            delegation_secret=SECRET,
            commit_sha="c" * 40,
        )
        assert settings.environment.value == ("development" if role == "api" else "test")
        assert settings.database_url.endswith("@127.0.0.1:17001/windops")
        assert settings.auth_mode == "sites_delegation" and settings.rate_limit_enabled
        assert settings.rate_limit_requests_per_minute == 600
        assert settings.rate_limit_client_requests_per_minute == 10_000
        assert not settings.schema_bootstrap and not settings.demo_seed
        assert not settings.test_auth_bypass_enabled and not settings.outbox_inline_drain
        assert not settings.local_acceptance and not settings.release_image_digest
        assert settings.knowledge_graph_backend == "neo4j"
        assert not settings.embedding_api_key.get_secret_value()
        assert len(settings.identity_role_mappings) == 4
        assert settings.identity_role_mappings[SUBJECTS["a1"]] == ["operations_manager"]
        assert settings.identity_role_mappings[SUBJECTS["b1"]] == ["operations_manager"]
        for alias, subject in SUBJECTS.items():
            scope = settings.identity_scope_mappings[subject]
            assert scope.tenant_ids == ["tenant-" + alias[0]]
            assert scope.wind_farm_ids == ["farm-" + alias[0]] and not scope.turbine_ids
            assert not scope.allow_global and "*" not in scope.data_scopes
        if role != "api":
            assert isinstance(_embedding_provider(settings), DeterministicTestEmbeddingProvider)


def test_parser_configuration_is_atomic() -> None:
    with pytest.raises(ValueError, match="complete parser"):
        services_settings(
            role="document_worker",
            endpoints=ServicesEndpoints(17001, 17002, 17003, 17004),
            password="b" * 64,
            delegation_secret=SECRET,
            commit_sha="c" * 40,
            parser_python=Path("unverified-python"),
        )


@pytest.mark.parametrize("ports", [(1, 1, 2, 3), (0, 2, 3, 4), (True, 2, 3, 4), (65536, 2, 3, 4)])
def test_only_distinct_owned_port_values_allowed(ports: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="distinct owned"):
        ServicesEndpoints(*ports)
