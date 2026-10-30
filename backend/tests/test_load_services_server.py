"""Private bootstrap contracts; actual TLS bytes, no dependency service claims."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import ssl
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography import x509

import windops_backend.operations.load_services_server as server
import windops_backend.operations.load_software as software
from windops_backend.operations.load_services_settings import ROLES, ServicesEndpoints


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(software, "ROOT", tmp_path)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(software, "RUN_ROOT", tmp_path / ".artifacts/performance-e2e")
    run_id = uuid4().hex
    directory = software.RUN_ROOT / run_id
    (directory / "private").mkdir(parents=True)
    digest = server.write_runtime(
        directory,
        run_id,
        endpoints=ServicesEndpoints(17001, 17002, 17003, 17004),
        password="a" * 64,
        delegation_secret="b" * 64,
        commit_sha="c" * 40,
    )
    return directory, run_id, digest


def test_private_configuration_validated_for_each_role_and_not_modified(runtime) -> None:
    directory, run_id, digest = runtime
    path = directory / "private/runtime.json"
    before = path.read_bytes()
    for role in ROLES:
        settings = server.load_runtime(directory, run_id, role, digest)
        assert settings.environment.value == ("development" if role == "api" else "test")
        assert settings.gateway_delegation_secret.get_secret_value() == "b" * 64
    assert path.read_bytes() == before


def test_tls_certificate_has_only_loopback_identity_and_is_loadable(runtime) -> None:
    directory, _, _ = runtime
    private = directory / "private"
    cert = x509.load_pem_x509_certificate((private / "cert.pem").read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(v) for v in names.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]
    assert not names.get_values_for_type(x509.DNSName)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(private / "cert.pem", private / "key.pem")
    client = ssl.create_default_context(cafile=str(private / "cert.pem"))
    assert client.check_hostname and client.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize(
    "change", ["schema_version", "run_id", "extra", "endpoints", "raw_credentials"]
)
def test_private_config_tamper_cannot_redirect_owned_roles(runtime, change) -> None:
    directory, run_id, digest = runtime
    path = directory / "private/runtime.json"
    raw = json.loads(path.read_bytes())
    raw[change] = "external-private-value"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="configuration changed"):
        server.load_runtime(directory, run_id, "api", digest)


def test_unknown_runtime_fields_rejected_even_with_updated_hash(runtime) -> None:
    directory, run_id, _ = runtime
    path = directory / "private/runtime.json"
    raw = json.loads(path.read_bytes())
    raw["environment"] = "production"
    path.write_text(json.dumps(raw), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="invalid owned"):
        server.load_runtime(directory, run_id, "api", digest)


def test_provider_and_python_injection_environment_not_inherited(runtime, monkeypatch) -> None:
    directory, _, _ = runtime
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "WINDOPS_DATABASE_URL",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "AWS_SECRET_ACCESS_KEY",
        "DOCKER_HOST",
    ):
        monkeypatch.setenv(name, "private-injected-value")
    env = server.private_environment(directory)
    assert "private-injected-value" not in env.values()
    assert env["PYTHONPATH"] == str(server.ROOT / "backend/src")
    assert env["TMP"] == str(directory / "private")
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"


def test_parser_paths_outside_workspace_rejected_before_configuration_publish(
    runtime, tmp_path
) -> None:
    directory, _, _ = runtime
    with pytest.raises(ValueError, match="workspace"):
        server.checked_workspace_path(tmp_path.parent / "unrelated-parser")
    with pytest.raises(ValueError, match="unavailable"):
        server.checked_workspace_path(directory / "missing-parser")


async def test_exact_stdin_stop_and_invalid_control(monkeypatch) -> None:
    monkeypatch.setattr(server.sys, "stdin", io.StringIO("stop\n"))
    await asyncio.wait_for(server._stop_command(), 1)
    monkeypatch.setattr(server.sys, "stdin", io.StringIO("exit\n"))
    with pytest.raises(RuntimeError, match="invalid private services control"):
        await asyncio.wait_for(server._stop_command(), 1)


async def test_waiting_stdin_control_can_be_cancelled_without_executor_block(monkeypatch) -> None:
    release = threading.Event()
    entered = threading.Event()

    class DelayedInput:
        def readline(self, limit: int) -> str:
            entered.set()
            release.wait(3)
            return "stop\n"

    monkeypatch.setattr(server.sys, "stdin", DelayedInput())
    task = asyncio.create_task(server._stop_command())
    try:
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    finally:
        release.set()
