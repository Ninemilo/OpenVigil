"""Compose control contracts use synthetic CLI output, not a running Docker stack.

The separate bounded-command tests run real owned Python child processes.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import windops_backend.operations.load_services_stack as module
import windops_backend.operations.load_software as software
from windops_backend.operations.load_resources import (
    ResourceObservationError,
    read_process,
    register_process,
)
from windops_backend.operations.load_services_commands import OwnedCommandResult
from windops_backend.operations.load_services_stack import (
    PORTS,
    PROJECT_LABEL,
    OwnedComposeStack,
    StackError,
    compose_definition,
)


@pytest.fixture
def stack(tmp_path: Path, monkeypatch) -> OwnedComposeStack:
    monkeypatch.setattr(software, "ROOT", tmp_path)
    monkeypatch.setattr(software, "RUN_ROOT", tmp_path / ".artifacts/performance-e2e")
    run_id = uuid4().hex
    directory = software.RUN_ROOT / run_id
    (directory / "private").mkdir(parents=True)
    return OwnedComposeStack(directory, run_id, "a" * 64)


class DockerControlFixture:
    """Respond only to the exact fixed adapter operations used by these tests."""

    def __init__(self, stack: OwnedComposeStack) -> None:
        self.stack = stack
        self.calls: list[tuple[str, ...]] = []
        self.created = False
        self.fail_up = False
        self.partial = False
        self.wrong_label = False
        self.endpoint = "unix:///var/run/docker.sock"
        self.existing = False
        self.mutate_container: Callable[[list[dict[str, Any]]], None] | None = None
        self.mutate_after_stats: Callable[[list[dict[str, Any]]], None] | None = None
        self.stats_called = False
        self.stats_rows = [
            {"ID": f"{i + 1:064x}", "MemUsage": usage, "CPUPerc": cpu}
            for i, (usage, cpu) in enumerate(
                [
                    ("12.5MiB / 1GiB", "0.00%"),
                    ("1.25GB / 2GB", "132.125%"),
                    ("1024B / 1MiB", "0.03%"),
                    ("0B / 1GiB", "999.0%"),
                ]
            )
        ]

    async def command(self, args: tuple[str, ...], *, deadline_seconds: float = 30) -> str:
        self.calls.append(args)
        if args == ("context", "show"):
            return "default"
        if args == ("context", "inspect", "default"):
            return json.dumps([{"Endpoints": {"docker": {"Host": self.endpoint}}}])
        assert args[:2] == ("--host", "unix:///var/run/docker.sock")
        args = args[2:]
        if args[:1] == ("info",) or args[:2] == ("compose", "version"):
            return "synthetic-control-version"
        if args[0] == "compose":
            assert args[1:5] == (
                "--project-name",
                self.stack.project,
                "--file",
                str(self.stack.manifest),
            )
            if args[5] == "up":
                self.created = True
                if self.fail_up:
                    raise StackError("STACK_COMMAND_FAILED")
                return ""
            assert args[5:] == ("down", "--volumes", "--timeout", "20")
            self.created = False
            return ""
        if args[0] == "stats":
            assert args[:5] == ("stats", "--no-stream", "--no-trunc", "--format", "{{json .}}")
            assert args[5:] == tuple(f"{i + 1:064x}" for i in range(4))
            self.stats_called = True
            return "\n".join(json.dumps(row) for row in self.stats_rows)
        kind, operation = args[:2]
        if operation == "ls":
            assert f"label={PROJECT_LABEL}={self.stack.project}" in args
            if not self.created and not self.existing:
                return ""
            return {
                "container": "\n".join(f"{i + 1:064x}" for i in range(4)),
                "volume": "\n".join(f"{self.stack.project}_windops-{name}" for name in PORTS),
                "network": "a" * 64,
            }[kind]
        assert operation == "inspect"
        if kind == "container":
            entries = [
                {
                    "Id": f"{i + 1:064x}",
                    "Image": "sha256:" + "f" * 64,
                    "Created": "2026-10-23T00:00:00.123456789Z",
                    "Config": {
                        "Labels": {
                            PROJECT_LABEL: "unrelated" if self.wrong_label else self.stack.project,
                            "com.docker.compose.service": name,
                        }
                    },
                    "Mounts": [{"Type": "volume", "Name": f"{self.stack.project}_windops-{name}"}],
                    "State": {
                        "Running": not self.partial,
                        "Pid": 1000 + i,
                        "StartedAt": "2026-10-23T00:00:01.123456789Z",
                    },
                    "NetworkSettings": {
                        "Ports": {}
                        if self.partial
                        else {f"{port}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(18000 + i)}]}
                    },
                }
                for i, (name, port) in enumerate(PORTS.items())
            ]
            mutation = self.mutate_after_stats if self.stats_called else self.mutate_container
            if mutation is not None:
                mutation(entries)
            return json.dumps(entries)
        names = (
            [f"{self.stack.project}_windops-{name}" for name in PORTS]
            if kind == "volume"
            else [f"{self.stack.project}_default"]
        )
        return json.dumps(
            [{"Name": name, "Labels": {PROJECT_LABEL: self.stack.project}} for name in names]
        )


def test_only_fresh_pinned_dependencies_with_named_volumes() -> None:
    definition = compose_definition("a" * 64)
    assert set(definition["services"]) == set(PORTS)
    assert all(v is None for v in definition["volumes"].values())
    assert all(s["ports"] == [f"127.0.0.1::{PORTS[n]}"] for n, s in definition["services"].items())
    assert "minio-init" not in definition["services"]


async def test_owned_stack_lifecycle_and_idempotent_cleanup(stack, monkeypatch) -> None:
    fixture = DockerControlFixture(stack)
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    endpoints, evidence = await stack.start()
    assert endpoints.postgres == 18000 and len(evidence["containers"]) == 4
    assert (await stack.cleanup()) == {"passed": True, "status": "removed"}
    before = len(fixture.calls)
    assert (await stack.cleanup())["status"] == "already_cleaned"
    assert len(fixture.calls) == before


async def test_partial_initialization_still_checks_ownership_and_removes_owned_resources(
    stack, monkeypatch
) -> None:
    fixture = DockerControlFixture(stack)
    fixture.fail_up = fixture.partial = True
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    with pytest.raises(StackError, match="COMMAND_FAILED"):
        await stack.start()
    assert (await stack.cleanup())["passed"]
    assert not fixture.created


async def test_wrong_project_cannot_be_deleted(stack, monkeypatch) -> None:
    fixture = DockerControlFixture(stack)
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    await stack.start()
    fixture.wrong_label = True
    with pytest.raises(StackError, match="OWNERSHIP_MISMATCH"):
        await stack.cleanup()
    assert not any("down" in call for call in fixture.calls)


async def test_manifest_change_cannot_delete_resources(stack, monkeypatch) -> None:
    fixture = DockerControlFixture(stack)
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    await stack.start()
    stack.manifest.write_text("services: {}", encoding="utf-8")
    with pytest.raises(StackError, match="MANIFEST_CHANGED"):
        await stack.cleanup()
    assert not any("down" in call for call in fixture.calls)


@pytest.mark.parametrize(
    "endpoint", ["tcp://127.0.0.1:2375", "ssh://other-host", "npipe://remote/pipe/docker"]
)
async def test_remote_or_ambiguous_docker_endpoints_rejected(stack, monkeypatch, endpoint) -> None:
    fixture = DockerControlFixture(stack)
    fixture.endpoint = endpoint
    monkeypatch.setattr(stack, "_command", fixture.command)
    with pytest.raises(StackError, match="CONTEXT_NOT_LOCAL"):
        await stack.preflight()
    assert (await stack.cleanup())["status"] == "not_needed"
    assert not any("up" in call or "down" in call for call in fixture.calls)


async def test_existing_project_never_adopted(stack, monkeypatch) -> None:
    fixture = DockerControlFixture(stack)
    fixture.existing = True
    monkeypatch.setattr(stack, "_command", fixture.command)
    with pytest.raises(StackError, match="ALREADY_EXISTS"):
        await stack.preflight()
    with pytest.raises(StackError, match="INVALID_START_STATE"):
        await stack.start()
    assert (await stack.cleanup())["status"] == "not_needed"


async def test_context_name_change_does_not_change_frozen_local_transport(
    stack, monkeypatch
) -> None:
    fixture = DockerControlFixture(stack)
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    fixture.endpoint = "ssh://unrelated-host"
    await stack.start()
    assert stack.endpoint == "unix:///var/run/docker.sock"
    assert (await stack.cleanup())["passed"]
    assert all(call[:1] != ("--context",) for call in fixture.calls)


async def test_repeated_cancellation_cannot_interrupt_owned_stack_cleanup(
    stack, monkeypatch
) -> None:
    fixture = DockerControlFixture(stack)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def command(args, *, deadline_seconds=30):
        if "down" in args:
            entered.set()
            await release.wait()
        return await fixture.command(args, deadline_seconds=deadline_seconds)

    monkeypatch.setattr(stack, "_command", command)
    await stack.preflight()
    await stack.start()
    task = asyncio.create_task(stack.cleanup())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    result = await asyncio.wait_for(task, 2)
    assert result["passed"] and result["cancellation_observed"]
    assert not fixture.created and stack._cleaned


async def test_missing_runtime_has_no_start_or_cleanup_side_effect(stack) -> None:
    stack.docker = None
    with pytest.raises(StackError, match="SERVICE_RUNTIME_UNAVAILABLE"):
        await stack.preflight()
    assert not stack.attempted_start
    assert (await stack.cleanup())["passed"]


async def test_real_owned_command_output_bounded_and_private(stack, monkeypatch) -> None:
    stack.docker = sys.executable
    monkeypatch.setattr(module, "_MAX_OUTPUT", 1024)
    with pytest.raises(StackError, match="OUTPUT_LIMIT"):
        await stack._command(("-c", "print('private-marker-' * 1000)"))
    assert stack.events == []
    assert stack.command_count == 1


async def test_real_owned_command_deadline_and_cancellation_cleaned(stack) -> None:
    stack.docker = sys.executable
    with pytest.raises(StackError, match="STACK_COMMAND_FAILED"):
        await stack._command(("-c", "import time;time.sleep(30)"), deadline_seconds=0.15)
    task = asyncio.create_task(stack._command(("-c", "import time;time.sleep(30)")))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert len(list((stack.directory / "private").glob("stack-*.log.stdout"))) == 2


async def test_stats_reads_only_frozen_ids_and_reports_formatted_metric_precision(
    stack, monkeypatch
) -> None:
    fixture = DockerControlFixture(stack)
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    await stack.start()
    before = len(fixture.calls)
    samples = await stack.container_stats()
    assert tuple(sample.role for sample in samples) == tuple(PORTS)
    assert tuple(sample.memory_working_set_bytes for sample in samples) == (
        13_107_200,
        1_250_000_000,
        1024,
        0,
    )
    assert tuple(sample.cpu_percent for sample in samples) == (0.0, 132.125, 0.03, 999.0)
    assert all(sample.running and sample.observed_at.utcoffset() is not None for sample in samples)
    calls = fixture.calls[before:]
    assert len(calls) == 3
    assert calls[0][2:4] == calls[2][2:4] == ("container", "inspect")
    assert calls[0][4:] == calls[2][4:] == tuple(stack.containers.values())
    assert "CLI-rounded" in stack.container_metric_semantics["precision"]
    await stack.cleanup()
    with pytest.raises(StackError, match="STATS_UNVERIFIED"):
        await stack.container_stats()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MemUsage", "NaNMiB / 1GiB"),
        ("MemUsage", "InfB / 1GiB"),
        ("MemUsage", "-1B / 1GiB"),
        ("MemUsage", "1B / 0B"),
        ("MemUsage", "2GiB / 1GiB"),
        ("MemUsage", "10XB / 1GiB"),
        ("MemUsage", "12.5MiB"),
        ("MemUsage", None),
        ("CPUPerc", "NaN%"),
        ("CPUPerc", "Inf%"),
        ("CPUPerc", "-0.1%"),
        ("CPUPerc", "1000001%"),
        ("CPUPerc", "0.3"),
        ("CPUPerc", None),
    ],
)
async def test_stats_missing_nonfinite_or_ambiguous_metrics_fail_closed(
    stack, monkeypatch, field, value
) -> None:
    fixture = DockerControlFixture(stack)
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    await stack.start()
    fixture.stats_rows[0][field] = value
    with pytest.raises(StackError, match="METRICS_INVALID"):
        await stack.container_stats()


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unrelated", "truncated"])
async def test_stats_exact_four_full_id_set_required(stack, monkeypatch, mutation) -> None:
    fixture = DockerControlFixture(stack)
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    await stack.start()
    if mutation == "missing":
        fixture.stats_rows.pop()
    elif mutation == "duplicate":
        fixture.stats_rows[0]["ID"] = fixture.stats_rows[1]["ID"]
    elif mutation == "unrelated":
        fixture.stats_rows[0]["ID"] = "a" * 64
    else:
        fixture.stats_rows[0]["ID"] = fixture.stats_rows[0]["ID"][:12]
    with pytest.raises(StackError, match="METRICS_MISSING|IDENTITY_MISMATCH"):
        await stack.container_stats()


@pytest.mark.parametrize("replacement", ["--all", "a" * 64])
async def test_mutated_registered_id_is_rejected_before_any_cli_call(
    stack, monkeypatch, replacement
) -> None:
    fixture = DockerControlFixture(stack)
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    await stack.start()
    before = len(fixture.calls)
    stack.containers["postgres"] = replacement
    with pytest.raises(StackError, match="STATS_IDENTITY_MISMATCH"):
        await stack.container_stats()
    assert len(fixture.calls) == before


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize(
    "mutation", ["project", "cid", "start", "pid", "image", "stopped", "missing"]
)
async def test_stats_identity_changes_before_or_during_sampling_are_rejected(
    stack, monkeypatch, phase, mutation
) -> None:
    fixture = DockerControlFixture(stack)
    monkeypatch.setattr(stack, "_command", fixture.command)
    await stack.preflight()
    await stack.start()

    def alter(entries):
        if mutation == "missing":
            entries.pop()
        elif mutation == "project":
            entries[0]["Config"]["Labels"][PROJECT_LABEL] = "another-project"
        elif mutation == "cid":
            entries[0]["Id"] = "e" * 64
        elif mutation == "start":
            entries[0]["State"]["StartedAt"] = "2026-10-23T00:01:01Z"
        elif mutation == "pid":
            entries[0]["State"]["Pid"] += 1
        elif mutation == "image":
            entries[0]["Image"] = "sha256:" + "e" * 64
        else:
            entries[0]["State"]["Running"] = False

    if phase == "before":
        fixture.mutate_container = alter
    else:
        fixture.mutate_after_stats = alter
    with pytest.raises(StackError, match="IDENTITY_MISMATCH|NOT_RUNNING"):
        await stack.container_stats()
    assert fixture.stats_called is (phase == "after")


async def test_command_does_not_accept_zero_exit_or_decode_output_when_cleanup_failed(
    stack, monkeypatch
) -> None:
    async def failed(*args, **kwargs):
        return OwnedCommandResult(0, b"private-response", b"private-error", False, None)

    stack.docker = sys.executable
    monkeypatch.setattr(module, "run_owned_command", failed)
    with pytest.raises(StackError, match="COMMAND_CLEANUP_FAILED"):
        await stack._command(("-c", "pass"))
    assert stack.events == [{"event": "command_cleanup_failed", "command_number": 1}]


_CLI_TREE = r"""
import json, os, pathlib, socket, subprocess, sys, time
folder, mode = pathlib.Path(__file__).parent, sys.argv[1]
if mode == 'grandchild':
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        (folder / 'grandchild.json').write_text(json.dumps({
            'pid': os.getpid(), 'port': listener.getsockname()[1]}))
        while True:
            connection, _ = listener.accept()
            with connection:
                if connection.recv(8) == b'stop':
                    break
else:
    subprocess.Popen([sys.executable, '-u', __file__, 'grandchild'],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    (folder / 'parent.json').write_text(json.dumps({'pid': os.getpid()}))
    while True:
        if mode == 'orphan' and (folder / 'release-parent').exists():
            sys.exit(0)
        time.sleep(.01)
"""


def _start_sentinel() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            str(getattr(sys, "_base_executable", sys.executable)),
            "-c",
            "import sys; sys.stdin.buffer.read(1)",
        ],
        stdin=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


async def _tree_identity(path: Path) -> dict[str, int]:
    async with asyncio.timeout(5):
        for _ in range(500):
            try:
                return dict(json.loads(await asyncio.to_thread(path.read_bytes)))
            except (FileNotFoundError, ValueError):
                await asyncio.sleep(0.01)
    raise AssertionError("owned command did not start its test tree")


@pytest.mark.parametrize("mode", ["timeout", "cancel", "orphan"])
async def test_real_stack_command_cleans_entire_cli_tree_and_preserves_sentinel(
    stack, mode
) -> None:
    stack.docker = str(getattr(sys, "_base_executable", sys.executable))
    folder = stack.directory / "private"
    script = folder / "owned-cli.py"
    script.write_text(_CLI_TREE, encoding="utf-8")
    sentinel = _start_sentinel()
    sentinel_identity = register_process(sentinel.pid, label="stack-test-sentinel")
    task = asyncio.create_task(stack._command(("-u", str(script), mode), deadline_seconds=6))
    try:
        child = await _tree_identity(folder / "grandchild.json")
        parent = await _tree_identity(folder / "parent.json")
        identities = tuple(
            register_process(row["pid"], label="owned-stack-cli-test") for row in (parent, child)
        )
        with socket.create_connection(("127.0.0.1", child["port"]), timeout=0.2):
            pass
        if mode == "orphan":
            (folder / "release-parent").write_bytes(b"exit now")
        if mode == "cancel":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 9)
        else:
            with pytest.raises(
                StackError, match="ORPHANS" if mode == "orphan" else "COMMAND_FAILED"
            ):
                await asyncio.wait_for(task, 10)
        for identity in identities:
            with pytest.raises(ResourceObservationError):
                read_process(identity)
        with socket.socket() as client:
            client.settimeout(0.2)
            assert client.connect_ex(("127.0.0.1", child["port"])) != 0
        read_process(sentinel_identity)
        receipt = json.loads((folder / "stack-0001.log").read_bytes())
        assert receipt["cleanup_passed"]
        assert (
            receipt["reason_code"]
            == {
                "timeout": "COMMAND_TIMEOUT",
                "cancel": "COMMAND_CANCELLED",
                "orphan": "COMMAND_ORPHANS",
            }[mode]
        )
        if mode == "orphan":
            assert receipt["exit_code"] == 0
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert sentinel.stdin is not None
        sentinel.stdin.write(b"S")
        sentinel.stdin.flush()
        sentinel.stdin.close()
        sentinel.wait(timeout=5)
        with pytest.raises(ResourceObservationError):
            read_process(sentinel_identity)


async def test_stats_callback_deadline_is_bounded_and_never_returns_partial_samples(
    stack, monkeypatch
) -> None:
    fixture = DockerControlFixture(stack)

    async def command(args, *, deadline_seconds=30):
        if "stats" in args:
            await asyncio.sleep(10)
        return await fixture.command(args, deadline_seconds=deadline_seconds)

    monkeypatch.setattr(stack, "_command", command)
    await stack.preflight()
    await stack.start()
    monkeypatch.setattr(module, "_STATS_TIMEOUT", 0.02)
    with pytest.raises(StackError, match="STATS_TIMEOUT"):
        await asyncio.wait_for(stack.container_stats(), 0.5)
