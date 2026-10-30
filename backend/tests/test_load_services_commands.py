"""Real owned CLI trees and sockets, with no Docker or external service calls."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from windops_backend.operations import load_services_commands as commands
from windops_backend.operations.load_resources import (
    ResourceObservationError,
    read_process,
    register_process,
)
from windops_backend.operations.load_services_commands import run_owned_command
from windops_backend.operations.load_services_processes import ProcessControlError, TrustedLaunch

BASE_PYTHON = str(getattr(sys, "_base_executable", sys.executable))
TREE = r"""
import json, os, pathlib, socket, subprocess, sys, time
root, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
if mode == 'grandchild':
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        (root / 'grandchild.json').write_text(json.dumps({
            'pid': os.getpid(), 'port': listener.getsockname()[1]}))
        while True:
            connection, _ = listener.accept()
            with connection:
                if connection.recv(8) == b'stop':
                    break
else:
    child = subprocess.Popen([sys.executable, '-u', __file__, str(root), 'grandchild'],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    (root / 'parent.json').write_text(json.dumps({'pid': os.getpid()}))
    deadline = time.monotonic() + 5
    while not (root / 'grandchild.json').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    print('private-stdout', flush=True)
    print('private-stderr', file=sys.stderr, flush=True)
    if mode == 'orphan':
        sys.exit(0)
    while True:
        time.sleep(.02)
"""


def launch(root: Path, script: str, *args: str) -> TrustedLaunch:
    root.mkdir()
    source = root / "command.py"
    source.write_text(script, encoding="utf-8")
    return TrustedLaunch(
        argv=(BASE_PYTHON, "-u", str(source), *args),
        cwd=root,
        env=dict(os.environ),
        private_log=root / "private.log",
    )


async def wait_json(path: Path) -> dict[str, Any]:
    async with asyncio.timeout(5):
        for _ in range(500):
            try:
                return dict(json.loads(await asyncio.to_thread(path.read_bytes)))
            except (FileNotFoundError, ValueError):
                pass
            await asyncio.sleep(0.01)
    raise RuntimeError("TEST_PROCESS_NOT_READY")


def assert_closed(port: int) -> None:
    with socket.socket() as client:
        client.settimeout(0.2)
        assert client.connect_ex(("127.0.0.1", port)) != 0


def start_sentinel() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [BASE_PYTHON, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_real_command_separates_outputs_and_keeps_actual_exit(
    tmp_path: Path, exit_code: int
) -> None:
    item = launch(
        tmp_path / "command",
        "import sys; print('private stdout'); print('private stderr', file=sys.stderr); "
        f"sys.exit({exit_code})",
    )
    result = await run_owned_command(
        item, workspace=tmp_path, deadline_seconds=5, maximum_output_bytes=1024
    )
    assert result.exit_code == exit_code
    assert result.cleanup_passed and result.reason_code is None
    assert result.stdout.strip() == b"private stdout"
    assert result.stderr.strip() == b"private stderr"
    assert (item.cwd / "private.log.stdout").read_bytes() == result.stdout
    assert (item.cwd / "private.log.stderr").read_bytes() == result.stderr
    assert "private stdout" not in repr(result) and "private stderr" not in repr(result)
    assert json.loads(item.private_log.read_bytes())["cleanup_passed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel", "orphan"])
async def test_timeout_cancellation_and_zero_exit_orphans_clear_tree_and_socket(
    tmp_path: Path, mode: str
) -> None:
    sentinel = start_sentinel()
    sentinel_identity = register_process(sentinel.pid, label="owned-command-test-sentinel")
    folder = tmp_path / "command"
    item = launch(folder, TREE, str(folder), "orphan" if mode == "orphan" else "wait")
    task = asyncio.create_task(
        run_owned_command(item, workspace=tmp_path, deadline_seconds=5, maximum_output_bytes=1024)
    )
    try:
        descendant = await wait_json(folder / "grandchild.json")
        identity = register_process(descendant["pid"], label="owned-command-test-grandchild")
        parent = await wait_json(folder / "parent.json")
        parent_identity = None
        if mode != "orphan":
            parent_identity = register_process(parent["pid"], label="owned-command-test-parent")
        with socket.create_connection(("127.0.0.1", descendant["port"]), timeout=0.2):
            pass
        if mode == "cancel":
            task.cancel()
            await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.005)
            task.cancel()
        started = time.monotonic()
        result = await asyncio.wait_for(task, timeout=9)
        assert time.monotonic() - started < 8
        assert result.cleanup_passed
        assert (
            result.reason_code
            == {
                "timeout": "COMMAND_TIMEOUT",
                "cancel": "COMMAND_CANCELLED",
                "orphan": "COMMAND_ORPHANS",
            }[mode]
        )
        if mode == "orphan":
            assert result.exit_code == 0  # This still MUST NOT be accepted as success.
        with pytest.raises(ResourceObservationError):
            read_process(identity)
        if parent_identity is not None:
            with pytest.raises(ResourceObservationError):
                read_process(parent_identity)
        assert_closed(descendant["port"])
        read_process(sentinel_identity)
        assert sentinel.poll() is None
    finally:
        if not task.done():
            task.cancel()
            await task
        assert sentinel.stdin is not None
        sentinel.stdin.write(b"S")
        sentinel.stdin.flush()
        sentinel.stdin.close()
        sentinel.wait(timeout=5)
        with pytest.raises(ResourceObservationError):
            read_process(sentinel_identity)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_each_output_has_a_strict_independent_cap(tmp_path: Path, stream: str) -> None:
    item = launch(
        tmp_path / "command",
        f"import sys; sys.{stream}.buffer.write(b'x' * 200000); sys.{stream}.flush()",
    )
    result = await run_owned_command(
        item, workspace=tmp_path, deadline_seconds=5, maximum_output_bytes=257
    )
    assert result.cleanup_passed and result.reason_code == "COMMAND_OUTPUT_LIMIT"
    assert getattr(result, stream) == b"x" * 257
    assert len(result.stdout) <= 257 and len(result.stderr) <= 257
    assert (item.cwd / f"private.log.{stream}").stat().st_size == 257


@pytest.mark.asyncio
async def test_two_full_streams_are_not_a_combined_output_budget(tmp_path: Path) -> None:
    item = launch(
        tmp_path / "command",
        "import sys; sys.stdout.buffer.write(b'a' * 256); sys.stderr.buffer.write(b'b' * 256)",
    )
    result = await run_owned_command(
        item, workspace=tmp_path, deadline_seconds=5, maximum_output_bytes=256
    )
    assert result.cleanup_passed and result.reason_code is None
    assert result.stdout == b"a" * 256 and result.stderr == b"b" * 256


@pytest.mark.asyncio
async def test_existing_logs_invalid_budgets_and_outside_paths_do_not_execute(
    tmp_path: Path,
) -> None:
    item = launch(tmp_path / "command", "raise RuntimeError('must not execute')")
    for deadline, limit in ((0, 100), (901, 100), (float("nan"), 100), (1, 0), (1, True)):
        with pytest.raises(ValueError, match="INVALID_COMMAND_BUDGET"):
            await run_owned_command(
                item, workspace=tmp_path, deadline_seconds=deadline, maximum_output_bytes=limit
            )
    with pytest.raises(ValueError, match="PROCESS_PATH_OUTSIDE_WORKSPACE"):
        await run_owned_command(
            replace(item, private_log=tmp_path.parent / "outside.log"),
            workspace=tmp_path,
            deadline_seconds=1,
            maximum_output_bytes=100,
        )
    item.private_log.write_bytes(b"existing")
    result = await run_owned_command(
        item, workspace=tmp_path, deadline_seconds=1, maximum_output_bytes=100
    )
    assert result.reason_code == "COMMAND_START_FAILED" and result.cleanup_passed
    assert result.exit_code is None and item.private_log.read_bytes() == b"existing"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["assignment", "identity"])
async def test_containment_assignment_failure_never_opens_execution_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if os.name != "nt" or failure == "identity":
        # The test exercises the same pre-gate failure on Linux's registration.
        def reject_registration(*args: Any, **kwargs: Any) -> Any:
            raise ResourceObservationError("TEST_REGISTRATION_REJECTED", args[0])

        monkeypatch.setattr(commands, "register_process", reject_registration)
    else:

        def reject_assignment(*args: Any, **kwargs: Any) -> None:
            raise ProcessControlError("JOB_ASSIGNMENT_FAILED")

        monkeypatch.setattr(commands._WindowsJob, "assign", reject_assignment)
    item = launch(
        tmp_path / "command", "import pathlib; pathlib.Path('executed').write_text('wrong')"
    )
    result = await run_owned_command(
        item, workspace=tmp_path, deadline_seconds=1, maximum_output_bytes=100
    )
    assert result.reason_code == "COMMAND_START_FAILED" and result.cleanup_passed
    assert not (item.cwd / "executed").exists()
