"""Private, task-owned loopback API process for software performance runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

import uvicorn
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from windops_backend.config import Settings
from windops_backend.main import create_app
from windops_backend.operations.load_dataset import KEY_FIELDS, SUBJECTS, USER_SCOPES
from windops_backend.operations.load_resources import register_process

PRIVATE_STARTUP_ENV = "OPENVIGIL_SOFTWARE_PRIVATE_STARTUP"


class SoftwareSettings(Settings):
    """No dotenv, inherited environment, paid provider or test-header bypass."""

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


def software_settings(database: Path, keys: dict[str, str]) -> Settings:
    if (
        set(keys) != set(USER_SCOPES)
        or any(re.fullmatch(r"[a-f0-9]{64}", value) is None for value in keys.values())
        or len(set(keys.values())) != len(keys)
    ):
        raise ValueError("four distinct private credentials are required")
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite+aiosqlite:///{database.as_posix()}",
        "schema_bootstrap": False,
        "demo_seed": False,
        "agent_mode": "deterministic",
        "knowledge_graph_backend": "memory",
        "outbox_inline_drain": False,
        "auth_mode": "static_tokens",
        "test_auth_bypass_enabled": False,
        "docs_enabled": False,
        "rate_limit_enabled": False,
        "otel_enabled": False,
        "identity_scope_mappings": {
            SUBJECTS[user]: {
                "tenant_ids": [f"tenant-{scope}"],
                "wind_farm_ids": [f"farm-{scope}"],
                "turbine_ids": [f"turbine-{scope}", f"turbine-{scope}-extra"],
                "data_scopes": ["asset", "telemetry"],
            }
            for user, scope in USER_SCOPES.items()
        },
    }
    values.update({KEY_FIELDS[user]: key for user, key in keys.items()})
    return SoftwareSettings(**values)


async def serve(run_directory: Path, run_id: str, keys: dict[str, str]) -> None:
    from windops_backend.operations.load_software import validate_run_directory

    validate_run_directory(run_directory, run_id)
    database = run_directory / "software.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise ValueError("owned dataset is required")
    settings = software_settings(database, keys)
    app = create_app(settings)
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
                access_log=False,
                log_config=None,
                log_level="critical",
                timeout_graceful_shutdown=10,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(40):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("software server failed during startup")
                    await asyncio.sleep(0.05)
            with (run_directory / "ready.json").open("x", encoding="utf-8") as stream:
                pool = app.state.engine.pool
                runtime = {
                    "settings": settings.model_dump(
                        mode="json",
                        include={
                            "environment",
                            "agent_mode",
                            "knowledge_graph_backend",
                            "outbox_inline_drain",
                            "auth_mode",
                            "test_auth_bypass_enabled",
                            "identity_scope_mappings",
                            "docs_enabled",
                            "rate_limit_enabled",
                            "rate_limit_requests_per_minute",
                            "rate_limit_client_requests_per_minute",
                            "otel_enabled",
                            "api_prefix",
                            "trusted_hosts",
                            "max_request_body_bytes",
                            "schema_bootstrap",
                            "demo_seed",
                            "document_parser_enabled",
                            "knowledge_graph_rebuild_on_startup",
                        },
                    ),
                    "database_dialect": app.state.engine.dialect.name,
                    "database_driver": app.state.engine.dialect.driver,
                    "pool_class": type(pool).__qualname__,
                    "pool_size": pool.size(),
                    "pool_timeout": pool.timeout(),
                    # SQLAlchemy exposes no public capacity/config getters for
                    # these values; the actual locked implementation is recorded.
                    "pool_max_overflow": pool._max_overflow,
                    "pool_pre_ping": pool._pre_ping,
                    "pool_recycle": pool._recycle,
                    "read_audit_sink": type(app.state.read_audit_sink).__qualname__,
                    "workers": 1,
                    "transport": "loopback_http",
                    "tls": False,
                    "resource_accounting": "load_client_and_owned_api_tree_v1",
                }
                identity = register_process(label="api_interpreter")
                json.dump(
                    {
                        "run_id": run_id,
                        "port": port,
                        "runtime": runtime,
                        "process": {
                            "pid": identity.pid,
                            "start_identity": identity.start_identity,
                        },
                    },
                    stream,
                )
            command = await asyncio.to_thread(sys.stdin.readline)
            if command.strip() != "stop":
                raise RuntimeError("invalid private server control")
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=15)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        # Task-private child environment, never argv, report data or a file.
        # Consume it before creating the API; stdin remains the stop-only channel.
        raw = os.environ.pop(PRIVATE_STARTUP_ENV, "")
        if len(raw) > 4096:
            raise ValueError("private startup input exceeds limit")
        keys = json.loads(raw)
        if not isinstance(keys, dict):
            raise ValueError("invalid private startup input")
        asyncio.run(serve(args.run_directory, args.run_id, keys))
    except Exception:
        # The parent receives a fixed code; private configuration is never logged.
        print("SOFTWARE_SERVER_FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
