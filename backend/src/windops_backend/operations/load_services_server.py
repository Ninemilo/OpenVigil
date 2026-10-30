"""Fixed-role, task-owned services bootstrap; never a deployment entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import socket
import sys
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from windops_backend.config import Settings
from windops_backend.operations.load_runner import _strict_json
from windops_backend.operations.load_services_settings import (
    ROLES,
    Role,
    ServicesEndpoints,
    services_settings,
)
from windops_backend.operations.load_software import ROOT, validate_run_directory

RUNTIME_SCHEMA = "openvigil.load-services-runtime.v1"


def checked_workspace_path(path: Path, *, directory: bool = False) -> Path:
    """Require existing, lexical and resolved workspace paths without links."""
    absolute = path.absolute()
    if not absolute.is_relative_to(ROOT) or not absolute.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("services input must belong to this workspace")
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("linked services inputs are forbidden")
        if candidate == ROOT:
            break
    if not (absolute.is_dir() if directory else absolute.is_file()):
        raise ValueError("services input is unavailable")
    return absolute


def private_environment(directory: Path) -> dict[str, str]:
    """Keep OS essentials, excluding provider, user Python and telemetry settings."""
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "COMSPEC",
        "PATHEXT",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(
        {
            "PYTHONPATH": str(ROOT / "backend/src"),
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
            "TEMP": str(directory / "private"),
            "TMP": str(directory / "private"),
            "TMPDIR": str(directory / "private"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
    )
    return env


def _certificate(private: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OpenVigil services load only")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    for filename, content in (
        ("cert.pem", cert.public_bytes(serialization.Encoding.PEM)),
        (
            "key.pem",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        ),
    ):
        path = private / filename
        with path.open("xb") as stream:
            stream.write(content)
        if os.name != "nt":
            path.chmod(0o600)


def write_runtime(
    directory: Path,
    run_id: str,
    *,
    endpoints: ServicesEndpoints,
    password: str,
    delegation_secret: str,
    commit_sha: str,
    parser_python: Path | None = None,
    parser_models: Path | None = None,
    parser_manifest_sha256: str | None = None,
) -> str:
    validate_run_directory(directory, run_id)
    private = checked_workspace_path(directory / "private", directory=True)
    if (parser_python is None) != (parser_models is None) or (parser_python is None) != (
        parser_manifest_sha256 is None
    ):
        raise ValueError("complete parser inputs are required")
    runtime = private / "parser-runtime"
    if parser_python is not None and parser_models is not None:
        checked_workspace_path(parser_python)
        checked_workspace_path(parser_models, directory=True)
        runtime.mkdir()
    data: dict[str, Any] = {
        "schema_version": RUNTIME_SCHEMA,
        "run_id": run_id,
        "endpoints": asdict(endpoints),
        "password": password,
        "delegation_secret": delegation_secret,
        "commit_sha": commit_sha,
        "parser_python": str(parser_python) if parser_python is not None else None,
        "parser_models": str(parser_models) if parser_models is not None else None,
        "parser_manifest_sha256": parser_manifest_sha256,
    }
    # Validate all values before publishing private configuration or TLS material.
    services_settings(
        role="api",
        endpoints=endpoints,
        password=password,
        delegation_secret=delegation_secret,
        commit_sha=commit_sha,
        parser_python=parser_python,
        parser_models=parser_models,
        parser_manifest_sha256=parser_manifest_sha256,
        parser_runtime=runtime if parser_python is not None else None,
    )
    _certificate(private)
    path = private / "runtime.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, allow_nan=False)
    if os.name != "nt":
        path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_runtime(directory: Path, run_id: str, role: Role, runtime_sha256: str) -> Settings:
    validate_run_directory(directory, run_id)
    path = checked_workspace_path(directory / "private/runtime.json")
    if path.stat().st_size > 16384:
        raise ValueError("private services configuration exceeds bound")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != runtime_sha256:
        raise ValueError("private services configuration changed")
    raw = _strict_json(content)
    fields = {
        "schema_version",
        "run_id",
        "endpoints",
        "password",
        "delegation_secret",
        "commit_sha",
        "parser_python",
        "parser_models",
        "parser_manifest_sha256",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != fields
        or raw["schema_version"] != RUNTIME_SCHEMA
        or raw["run_id"] != run_id
    ):
        raise ValueError("invalid owned services configuration")
    parser_python = (
        checked_workspace_path(Path(raw["parser_python"]))
        if raw["parser_python"] is not None
        else None
    )
    parser_models = (
        checked_workspace_path(Path(raw["parser_models"]), directory=True)
        if raw["parser_models"] is not None
        else None
    )
    runtime = (
        checked_workspace_path(directory / "private/parser-runtime", directory=True)
        if parser_python is not None
        else None
    )
    return services_settings(
        role=role,
        endpoints=ServicesEndpoints(**raw["endpoints"]),
        password=raw["password"],
        delegation_secret=raw["delegation_secret"],
        commit_sha=raw["commit_sha"],
        parser_python=parser_python,
        parser_models=parser_models,
        parser_manifest_sha256=raw["parser_manifest_sha256"],
        parser_runtime=runtime,
    )


def _ready(directory: Path, run_id: str, role: Role, generation: int, **fields: Any) -> None:
    target = directory / f"ready-{role}-{generation}.json"
    temporary = directory / f"ready-{role}-{generation}.pending"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "run_id": run_id,
                "role": role,
                "generation": generation,
                "pid": os.getpid(),
                **fields,
            },
            stream,
        )
    # Publish only complete JSON and retain exclusive creation of the receipt.
    os.link(temporary, target)
    temporary.unlink()


async def _stop_command() -> None:
    loop = asyncio.get_running_loop()
    received: asyncio.Future[str] = loop.create_future()

    def deliver(command: str) -> None:
        if not received.done():
            received.set_result(command)

    def read() -> None:
        command = sys.stdin.readline(32)
        try:
            loop.call_soon_threadsafe(deliver, command)
        except RuntimeError:
            # The owner terminated an early-failing role before stdin arrived.
            if not loop.is_closed():
                raise

    threading.Thread(target=read, name="load-stop-control", daemon=True).start()
    command = await received
    if command != "stop\n":
        raise RuntimeError("invalid private services control")


async def serve_api(settings: Settings, directory: Path, run_id: str, generation: int) -> None:
    import uvicorn

    from windops_backend.main import create_app

    app = create_app(settings)
    private = directory / "private"
    cert = checked_workspace_path(private / "cert.pem")
    key = checked_workspace_path(private / "key.pem")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                workers=1,
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
                access_log=False,
                log_config=None,
                log_level="critical",
                timeout_graceful_shutdown=10,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        control: asyncio.Task[None] | None = None
        try:
            async with asyncio.timeout(60):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("services API startup failed")
                    await asyncio.sleep(0.05)
            pool = app.state.engine.pool
            _ready(
                directory,
                run_id,
                "api",
                generation,
                port=port,
                runtime={
                    "environment": settings.environment.value,
                    "auth_mode": settings.auth_mode,
                    "read_audit_sink": type(app.state.read_audit_sink).__qualname__,
                    "artifact_verifier": type(app.state.artifact_verifier).__qualname__,
                    "graph_store": type(app.state.knowledge_graph_store).__qualname__,
                    "database_dialect": app.state.engine.dialect.name,
                    "database_driver": app.state.engine.dialect.driver,
                    "pool_class": type(pool).__qualname__,
                    "pool_size": pool.size(),
                    "pool_timeout": pool.timeout(),
                    "pool_max_overflow": pool._max_overflow,
                    "pool_pre_ping": pool._pre_ping,
                    "pool_recycle": pool._recycle,
                    "rate_limit_enabled": settings.rate_limit_enabled,
                    "rate_limit_requests_per_minute": settings.rate_limit_requests_per_minute,
                    "rate_limit_client_requests_per_minute": (
                        settings.rate_limit_client_requests_per_minute
                    ),
                    "tls": True,
                    "workers": 1,
                    "embedding_execution": "not_in_query_profile",
                },
            )
            control = asyncio.create_task(_stop_command())
            done, _ = await asyncio.wait({task, control}, return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                await completed
            if task in done and control not in done:
                raise RuntimeError("services API exited before stop")
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 15)
            if control is not None and not control.done():
                control.cancel()
                await asyncio.gather(control, return_exceptions=True)


async def serve_worker(
    settings: Settings, directory: Path, run_id: str, role: Role, generation: int
) -> None:
    if role in {"general_worker", "document_worker"}:
        import dramatiq

        if role == "document_worker":
            if not settings.document_parser_enabled:
                raise ValueError("document worker requires verified parser inputs")
            from windops_backend import document_parse_tasks  # noqa: F401

            expected = {"parse_knowledge_document"}
            queues = {"document-parse"}
        else:
            from windops_backend import workers  # noqa: F401

            expected = {
                "process_mission_analysis",
                "project_knowledge_graph",
                "publish_eam_work_order",
                "index_knowledge_document",
            }
            queues = {"default"}
        broker = dramatiq.get_broker()
        if set(broker.actors) != expected:
            raise RuntimeError("services actor registry mismatch")
        broker.emit_after("process_boot")  # type: ignore[no-untyped-call]
        worker = dramatiq.Worker(broker, queues=queues, worker_threads=1)  # type: ignore[no-untyped-call]
        worker.start()  # type: ignore[no-untyped-call]
        _ready(
            directory,
            run_id,
            role,
            generation,
            runtime={
                "environment": settings.environment.value,
                "queues": sorted(queues),
                "threads": 1,
                "actors": sorted(expected),
                "embedding_provider": "deterministic_test",
                "embedding_model": "sha256-token-projection-v1",
                "runtime_ready": "not_observed",
            },
        )
        try:
            await _stop_command()
        finally:
            await asyncio.to_thread(worker.stop, timeout=30000)
            broker.close()  # type: ignore[no-untyped-call]
        return
    if role == "relay":
        from windops_backend.workers import relay_pending_once

        async def relay() -> None:
            while True:
                await relay_pending_once()
                await asyncio.sleep(2)

        task = asyncio.create_task(relay())
    elif role == "read_audit":
        from windops_backend.read_audit import _read_audit_worker_loop

        task = asyncio.create_task(_read_audit_worker_loop())
    else:
        raise ValueError("unknown services worker")
    control = asyncio.create_task(_stop_command())
    _ready(directory, run_id, role, generation, runtime={"environment": settings.environment.value})
    try:
        done, _ = await asyncio.wait({task, control}, return_when=asyncio.FIRST_COMPLETED)
        for completed in done:
            await completed
        if task in done:
            raise RuntimeError("services worker exited before stop")
    finally:
        for item in (task, control):
            if not item.done():
                item.cancel()
        await asyncio.gather(task, control, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--generation", type=int, choices=range(1, 4), required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        settings = load_runtime(args.run_directory, args.run_id, args.role, args.runtime_sha256)
        import windops_backend.config as configuration

        # Process-local installation before importing any actor registry. Direct
        # Settings validation has already selected the intended environment.
        configuration.get_settings = lambda: settings  # type: ignore[assignment]
        if args.role == "api":
            asyncio.run(serve_api(settings, args.run_directory, args.run_id, args.generation))
        else:
            asyncio.run(
                serve_worker(settings, args.run_directory, args.run_id, args.role, args.generation)
            )
    except Exception:
        print("SERVICES_ROLE_FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
