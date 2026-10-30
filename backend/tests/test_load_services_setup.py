"""Setup ownership and bootstrap evidence tests; no external dependency execution."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory

from windops_backend.operations import load_services_runtime as runtime
from windops_backend.operations import load_services_setup as setup
from windops_backend.operations.load_services_server import _ready
from windops_backend.operations.load_services_settings import ServicesEndpoints, services_settings
from windops_backend.operations.load_software import RUN_ROOT


def settings():
    return services_settings(
        role="api",
        endpoints=ServicesEndpoints(17001, 17002, 17003, 17004),
        password="a" * 64,
        delegation_secret="b" * 64,
        commit_sha="c" * 40,
    )


def test_setup_uses_actual_unique_migration_head_and_explicit_script_directory():
    config = setup.migration_config()
    assert Path(config.get_main_option("script_location")) == setup.ROOT / "backend/alembic"
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0033_knowledge_numeric_review"]


@pytest.mark.parametrize("case", ["fresh", "existing", "creation_lost"])
def test_buckets_created_only_in_fresh_owned_storage_and_native_transport_closed(monkeypatch, case):
    created, calls = [], []

    class Transport:
        def __init__(self, **kwargs):
            assert kwargs["retries"] is False
            assert kwargs["timeout"].connect_timeout == 3 and kwargs["timeout"].read_timeout == 5

        def clear(self):
            calls.append("closed")

    class Storage:
        def __init__(self, endpoint, **kwargs):
            assert endpoint == "127.0.0.1:17003" and kwargs["secure"] is False

        def list_buckets(self):
            return ["unowned"] if case == "existing" else []

        def make_bucket(self, name):
            created.append(name)

        def bucket_exists(self, name):
            return name in created and case != "creation_lost"

    monkeypatch.setattr(setup, "PoolManager", Transport)
    monkeypatch.setattr(setup, "Minio", Storage)
    if case == "fresh":
        buckets = setup.create_buckets(settings())
        assert buckets == created and len(buckets) == 5
        assert "windops-knowledge-documents" in buckets
    else:
        with pytest.raises(RuntimeError):
            setup.create_buckets(settings())
        assert len(created) == (0 if case == "existing" else 1)
    assert calls == ["closed"]


@pytest.fixture
def owned_directory():
    run_id = uuid4().hex
    directory = RUN_ROOT / run_id
    directory.mkdir(parents=True)
    (directory / "private").mkdir()
    return directory, run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["owned", "foreign_pid", "foreign_run", "oversized", "exited"])
async def test_role_receipt_must_match_kernel_registered_member_and_fresh_run(
    owned_directory, case
):
    directory, run_id = owned_directory
    service = runtime.ServicesRuntime(directory, run_id)
    service.owner = SimpleNamespace(
        info=lambda role: SimpleNamespace(role_exit_code=1 if case == "exited" else None),
        members=lambda role: [SimpleNamespace(pid=os.getpid())],
    )
    if case != "exited":
        _ready(
            directory,
            "f" * 32 if case == "foreign_run" else run_id,
            "api",
            1,
            runtime={"fixture": True},
            port=12345,
        )
        path = directory / "ready-api-1.json"
        value = json.loads(path.read_text())
        if case == "foreign_pid":
            value["pid"] = 99999999
        if case == "oversized":
            value["runtime"]["fixture"] = "x" * 20000
        path.write_text(json.dumps(value))
    if case == "owned":
        assert (await service.wait_ready("api", 1))["pid"] == os.getpid()
        assert service.api_port == 12345
        assert not (directory / "ready-api-1.pending").exists()
    else:
        with pytest.raises(runtime.RuntimeErrorCode):
            await asyncio.wait_for(service.wait_ready("api", 1), 2)


@pytest.mark.asyncio
async def test_failed_role_cleanup_does_not_skip_stack_removal(owned_directory):
    directory, run_id = owned_directory
    service = runtime.ServicesRuntime(directory, run_id)
    calls = []

    async def roles():
        calls.append("roles")
        raise RuntimeError("private-roles")

    async def stack():
        calls.append("stack")
        return {"passed": True}

    service.owner = SimpleNamespace(cleanup=roles)
    service.stack = SimpleNamespace(cleanup=stack)
    result = await service.cleanup()
    assert calls == ["roles", "stack"] and result["passed"] is False
    assert result["failures"] == ["role_cleanup_failed"]


def test_private_json_refuses_duplicate_keys_and_outside_workspace(owned_directory, tmp_path):
    directory, _ = owned_directory
    path = directory / "private/bad.json"
    path.write_bytes(b'{"run_id":"a","run_id":"b"}')
    with pytest.raises(ValueError):
        runtime.read_private_json(path)
    with pytest.raises(ValueError):
        runtime.read_private_json(Path(os.environ.get("SystemRoot", "/etc")) / "unowned.json")
