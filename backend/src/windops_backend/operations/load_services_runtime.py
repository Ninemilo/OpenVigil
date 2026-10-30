"""Lifecycle of this measurement's private services, with auditable ownership."""

from __future__ import annotations

import asyncio
import secrets
import socket
import ssl
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from windops_backend.operations.load_backlog import WorkerReceipt
from windops_backend.operations.load_contracts import LoadProfile
from windops_backend.operations.load_runner import _strict_json
from windops_backend.operations.load_services_commands import run_owned_command
from windops_backend.operations.load_services_inputs import ServicesInputs, parser_paths
from windops_backend.operations.load_services_processes import (
    OwnedRoleProcesses,
    Role,
    TrustedLaunch,
)
from windops_backend.operations.load_services_server import (
    checked_workspace_path,
    load_runtime,
    private_environment,
    write_runtime,
)
from windops_backend.operations.load_services_settings import services_users
from windops_backend.operations.load_services_stack import OwnedComposeStack
from windops_backend.operations.load_software import ROOT


class RuntimeErrorCode(RuntimeError):
    """Fixed public failures; private runtime configuration never escapes."""


def read_private_json(path: Path, maximum: int = 1024 * 1024) -> dict[str, Any]:
    path = checked_workspace_path(path)
    with path.open("rb") as stream:
        value = _strict_json(stream.read(maximum + 1)) if path.stat().st_size <= maximum else None
    if not isinstance(value, dict):
        raise RuntimeErrorCode("RUNTIME_EVIDENCE_INVALID")
    return value


class ServicesRuntime:
    def __init__(self, directory: Path, run_id: str) -> None:
        self.directory, self.run_id = directory, run_id
        self.password, self.secret = secrets.token_hex(32), secrets.token_hex(32)
        self.stack = OwnedComposeStack(directory=directory, run_id=run_id, password=self.password)
        self.owner = OwnedRoleProcesses(self.launch, workspace=ROOT)
        self.runtime_sha256: str | None = None
        self.engine: AsyncEngine | None = None
        self.api_port: int | None = None
        self.ready: dict[str, dict[str, Any]] = {}
        self.setup_evidence: dict[str, Any] = {}
        self.stack_evidence: dict[str, Any] = {}
        self.users = services_users(self.secret)
        self._cleanup_task: asyncio.Task[dict[str, Any]] | None = None

    def launch(self, role: Role, generation: int) -> TrustedLaunch:
        if self.runtime_sha256 is None:
            raise RuntimeErrorCode("RUNTIME_NOT_CONFIGURED")
        return TrustedLaunch(
            argv=(
                sys.executable,
                "-m",
                "windops_backend.operations.load_services_server",
                "--run-directory",
                str(self.directory),
                "--run-id",
                self.run_id,
                "--runtime-sha256",
                self.runtime_sha256,
                "--role",
                role,
                "--generation",
                str(generation),
            ),
            cwd=ROOT,
            env=private_environment(self.directory),
            private_log=self.directory / "private" / f"{role}-{generation}.log",
        )

    async def wait_ready(self, role: Role, generation: int) -> dict[str, Any]:
        path = self.directory / f"ready-{role}-{generation}.json"
        async with asyncio.timeout(70):
            while not path.exists():
                if self.owner.info(role).role_exit_code is not None:
                    raise RuntimeErrorCode("RUNTIME_ROLE_EXITED")
                await asyncio.sleep(0.1)
            value = read_private_json(path, 16384)
        if (
            value.get("run_id") != self.run_id
            or value.get("role") != role
            or value.get("generation") != generation
            or value.get("pid") not in {p.pid for p in self.owner.members(role)}
            or not isinstance(value.get("runtime"), dict)
        ):
            raise RuntimeErrorCode("RUNTIME_READY_IDENTITY_MISMATCH")
        if role == "api":
            port = value.get("port")
            if type(port) is not int or not 1 <= port <= 65535:
                raise RuntimeErrorCode("RUNTIME_API_PORT_INVALID")
            self.api_port = port
        self.ready[role] = value
        return value

    async def setup(self, profile: LoadProfile, inputs: ServicesInputs, commit: str) -> None:
        await self.stack.preflight()
        endpoints, self.stack_evidence = await self.stack.start()
        python, models = parser_paths(inputs)
        self.runtime_sha256 = write_runtime(
            self.directory,
            self.run_id,
            endpoints=endpoints,
            password=self.password,
            delegation_secret=self.secret,
            commit_sha=commit,
            parser_python=python,
            parser_models=models,
            parser_manifest_sha256=inputs.parser_manifest_sha256,
        )
        setup = await run_owned_command(
            TrustedLaunch(
                argv=(
                    sys.executable,
                    "-m",
                    "windops_backend.operations.load_services_setup",
                    "--run-directory",
                    str(self.directory),
                    "--run-id",
                    self.run_id,
                    "--runtime-sha256",
                    self.runtime_sha256,
                    "--history-rows",
                    str(profile.history_rows),
                ),
                cwd=ROOT,
                env=private_environment(self.directory),
                private_log=self.directory / "private" / "setup.log",
            ),
            workspace=ROOT,
            deadline_seconds=300,
            maximum_output_bytes=1024 * 1024,
        )
        if setup.reason_code == "COMMAND_CANCELLED":
            raise asyncio.CancelledError
        if setup.reason_code is not None or not setup.cleanup_passed or setup.exit_code != 0:
            raise RuntimeErrorCode("RUNTIME_SETUP_FAILED")
        self.setup_evidence = read_private_json(self.directory / "private" / "setup.json")
        if (
            self.setup_evidence.get("run_id") != self.run_id
            or self.setup_evidence.get("runtime_sha256") != self.runtime_sha256
        ):
            raise RuntimeErrorCode("RUNTIME_SETUP_IDENTITY_MISMATCH")
        settings = load_runtime(self.directory, self.run_id, "api", self.runtime_sha256)
        self.engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"timeout": 5, "command_timeout": 15},
        )
        roles: list[Role] = ["general_worker", "relay", "read_audit", "api"]
        if inputs.documents:
            roles.append("document_worker")
        for role in roles:
            info = await self.owner.start(role)
            await self.wait_ready(role, info.generation)

    @property
    def origin(self) -> str:
        if self.api_port is None:
            raise RuntimeErrorCode("RUNTIME_API_NOT_READY")
        return f"https://127.0.0.1:{self.api_port}"

    @property
    def verify(self) -> ssl.SSLContext:
        return ssl.create_default_context(
            cafile=str(checked_workspace_path(self.directory / "private" / "cert.pem"))
        )

    def factory(self) -> async_sessionmaker[Any]:
        if self.engine is None:
            raise RuntimeErrorCode("RUNTIME_DATABASE_NOT_READY")
        return async_sessionmaker(self.engine, expire_on_commit=False)

    async def current_document_worker(self) -> WorkerReceipt:
        info = self.owner.info("document_worker")
        members = self.owner.members("document_worker")
        return WorkerReceipt(
            worker_alias="document_worker",
            generation=str(info.generation),
            identity_verified=bool(members),
            running=bool(members) and info.role_exit_code is None,
        )

    async def stop_document_worker(self) -> WorkerReceipt:
        result = await self.owner.stop("document_worker", abrupt=True)
        if not result.passed or not result.descendants_empty:
            raise RuntimeErrorCode("RUNTIME_DOCUMENT_STOP_FAILED")
        return WorkerReceipt(
            worker_alias="document_worker",
            generation=str(result.generation),
            identity_verified=result.identity_verified,
            running=False,
            abrupt=True,
        )

    async def start_document_worker(self) -> WorkerReceipt:
        info = await self.owner.start("document_worker")
        await self.wait_ready("document_worker", info.generation)
        return await self.current_document_worker()

    async def _cleanup(self) -> dict[str, Any]:
        result: dict[str, Any] = {"passed": False, "roles": [], "failures": []}
        try:
            receipts = await self.owner.cleanup()
            result["roles"] = [asdict(item) for item in receipts]
            if not all(item.passed for item in receipts):
                result["failures"].append("role_cleanup_failed")
        except Exception:
            result["failures"].append("role_cleanup_failed")
        if self.engine is not None:
            try:
                async with asyncio.timeout(10):
                    await self.engine.dispose()
            except Exception:
                result["failures"].append("observer_engine_cleanup_failed")
        try:
            result["stack"] = await self.stack.cleanup()
            if not result["stack"]["passed"]:
                result["failures"].append("stack_cleanup_failed")
        except Exception:
            result["failures"].append("stack_cleanup_failed")
        ports = [v["port"] for v in self.stack_evidence.get("containers", {}).values()]
        if self.api_port is not None:
            ports.append(self.api_port)

        def released(port: int) -> bool:
            with socket.socket() as probe:
                probe.settimeout(0.5)
                return probe.connect_ex(("127.0.0.1", port)) != 0

        result["ports_released"] = all(released(port) for port in ports)
        if not result["ports_released"]:
            result["failures"].append("owned_port_still_open")
        result["passed"] = not result["failures"]
        return result

    async def cleanup(self) -> dict[str, Any]:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        cancelled = False
        while not self._cleanup_task.done():
            try:
                await asyncio.shield(self._cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        result = self._cleanup_task.result()
        return {**result, **({"cancellation_observed": True} if cancelled else {})}
