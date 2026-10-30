"""Actual owned process trees; no Docker, worker services or unrelated PIDs."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from windops_backend.operations import load_services_processes as processes
from windops_backend.operations.load_resources import (
    ResourceObservationError,
    read_process,
    register_process,
)
from windops_backend.operations.load_services_processes import (
    OwnedRoleProcesses,
    ProcessControlError,
    Role,
    TrustedLaunch,
)

TREE = r"""
import json, os, pathlib, subprocess, sys, time
root, mode, depth = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
child = None
if depth:
    child = subprocess.Popen([sys.executable, '-u', __file__, str(root), mode, str(depth-1)],
        stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
(root / ('pid-' + str(depth) + '.json')).write_text(json.dumps({'pid': os.getpid()}))
if mode == 'orphan' and depth == 2:
    sys.exit(0)
if mode in ('orphan', 'uncooperative'):
    while True:
        time.sleep(.05)
command = sys.stdin.buffer.readline()
if command != b'stop\n':
    sys.exit(11)
if mode == 'slow':
    time.sleep(.3)
if child is not None:
    child.stdin.write(b'stop\n')
    child.stdin.flush()
    child.stdin.close()
    child.wait(timeout=5)
    sys.exit(child.returncode)
"""


class Harness:
    def __init__(self, root: Path, mode: str = "cooperative") -> None:
        self.root, self.mode = root, mode
        root.mkdir()
        self.script = root / "owned-tree.py"
        self.script.write_text(TREE, encoding="utf-8")
        self.launches: list[tuple[Role, int]] = []
        self.manager = OwnedRoleProcesses(self.factory, workspace=root)

    def factory(self, role: Role, generation: int) -> TrustedLaunch:
        folder = self.root / f"{role}-{generation}"
        folder.mkdir()
        self.launches.append((role, generation))
        return TrustedLaunch(
            argv=(sys.executable, "-u", str(self.script), str(folder), self.mode, "2"),
            cwd=self.root,
            env={**os.environ, "PRIVATE_PROCESS_SECRET": "must-not-appear-in-receipt"},
            private_log=folder / "private.log",
        )

    async def pids(self, role: Role, generation: int = 1) -> tuple[int, ...]:
        folder = self.root / f"{role}-{generation}"
        async with asyncio.timeout(8):
            for _ in range(400):
                if len(list(folder.glob("pid-*.json"))) == 3:
                    break
                await asyncio.sleep(0.02)
        return tuple(
            json.loads((folder / f"pid-{index}.json").read_bytes())["pid"] for index in range(3)
        )


def assert_gone(identities: tuple[Any, ...]) -> None:
    for identity in identities:
        with pytest.raises(ResourceObservationError):
            read_process(identity)


def assert_contained(manager: OwnedRoleProcesses, role: Role, members: tuple[Any, ...]) -> None:
    # Windows venv redirectors and console hosts are actual descendants too.
    # Independently ask the kernel for membership of every returned PID.
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        api.OpenProcess.restype = ctypes.c_void_p
        api.IsProcessInJob.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        )
        api.IsProcessInJob.restype = ctypes.c_int
        api.CloseHandle.argtypes = (ctypes.c_void_p,)
        job = manager._owned[role].job
        assert job is not None
        for identity in members:
            handle = api.OpenProcess(0x1000, False, identity.pid)
            assert handle
            try:
                contained = ctypes.c_int()
                assert api.IsProcessInJob(handle, job.handle, ctypes.byref(contained))
                assert contained.value == 1
            finally:
                assert api.CloseHandle(handle)
    else:
        for identity in members:
            assert os.getsid(identity.pid) == manager.info(role).pid
            assert os.getpgid(identity.pid) == manager.info(role).pid


def start_sentinel() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            str(getattr(sys, "_base_executable", sys.executable)),
            "-c",
            "import time; time.sleep(60)",
        ],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


@pytest.mark.asyncio
async def test_real_tree_is_contained_enumerated_gracefully_stopped_and_idempotent(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "owned")
    manager = harness.manager
    try:
        info = await manager.start("api")
        pids = await harness.pids("api")
        members = manager.members("api")
        assert {info.pid, info.role_pid, *pids}.issubset({item.pid for item in members})
        assert len(members) >= 4
        assert len({(item.pid, item.start_identity) for item in members}) == len(members)
        assert_contained(manager, "api", members)
        with pytest.raises(ProcessControlError, match="ROLE_ALREADY_OWNED"):
            await manager.start("api")
        receipt = await manager.stop("api", grace_seconds=5)
        assert receipt.passed and receipt.descendants_empty and receipt.identity_verified
        assert receipt.action == "graceful" and not receipt.forced
        assert receipt.role_exit_code == receipt.guardian_exit_code == 0
        assert manager.info("api").role_exit_code == 0
        assert await manager.stop("api") == receipt
        assert await manager.cleanup() == (receipt,)
        assert manager.members() == ()
        assert_gone(members)
        public = json.dumps([asdict(info), asdict(receipt), manager.events])
        assert "must-not-appear-in-receipt" not in public
        assert str(tmp_path) not in public
        assert str(harness.script) not in public
    finally:
        await manager.cleanup(grace_seconds=0, kill_seconds=5)


@pytest.mark.asyncio
async def test_abrupt_tree_fault_and_restart_preserve_unrelated_sentinel(tmp_path: Path) -> None:
    sentinel = start_sentinel()
    sentinel_identity = register_process(sentinel.pid, label="owned-test-sentinel")
    harness = Harness(tmp_path / "owned", "uncooperative")
    manager = harness.manager
    try:
        initial = await manager.start("document_worker")
        pids = await harness.pids("document_worker")
        members = manager.members()
        assert {initial.pid, initial.role_pid, *pids}.issubset({item.pid for item in members})
        assert_contained(manager, "document_worker", members)
        assert sentinel.pid not in {item.pid for item in members}
        receipt = await manager.stop("document_worker", abrupt=True, kill_seconds=5)
        assert receipt.passed and receipt.action == "abrupt" and receipt.forced
        assert receipt.descendants_empty and receipt.role_exit_code is None
        assert_gone(members)
        read_process(sentinel_identity)
        new = await manager.restart_document_worker()
        await harness.pids("document_worker", 2)
        assert new.generation == 2 and new.start_identity != initial.start_identity
        assert new.role_pid != initial.role_pid
        second = await manager.stop("document_worker", abrupt=True)
        assert second.passed
        assert harness.launches == [("document_worker", 1), ("document_worker", 2)]
        read_process(sentinel_identity)
    finally:
        await manager.cleanup(grace_seconds=0, kill_seconds=5)
        sentinel.terminate()
        sentinel.wait(timeout=5)


@pytest.mark.asyncio
async def test_role_parent_exits_before_descendants_without_losing_ownership(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "owned", "orphan")
    manager = harness.manager
    try:
        info = await manager.start("general_worker")
        pids = await harness.pids("general_worker")
        async with asyncio.timeout(5):
            for _ in range(250):
                if manager.info("general_worker").role_exit_code is not None:
                    break
                await asyncio.sleep(0.02)
        members = manager.members()
        assert {info.pid, pids[0], pids[1]}.issubset({item.pid for item in members})
        assert pids[2] not in {item.pid for item in members}
        assert_contained(manager, "general_worker", members)
        receipt = await manager.stop("general_worker", abrupt=True)
        assert receipt.passed and receipt.descendants_empty
        assert receipt.role_exit_code == 0
        assert_gone(members)
    finally:
        await manager.cleanup(grace_seconds=0, kill_seconds=5)


@pytest.mark.asyncio
async def test_identity_change_refuses_control_until_original_identity_is_restored(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "owned")
    manager = harness.manager
    original = None
    try:
        await manager.start("relay")
        await harness.pids("relay")
        members = manager.members()
        owned = manager._owned["relay"]
        original = owned.identity
        owned.identity = replace(original, start_identity="different-start-identity")
        with pytest.raises(ProcessControlError, match="PROCESS_IDENTITY_CHANGED"):
            manager.members()
        receipt = await manager.stop("relay", abrupt=True)
        assert not receipt.passed and not receipt.identity_verified
        assert receipt.reason_codes == ("PROCESS_IDENTITY_CHANGED",)
        for identity in members:
            read_process(identity)
        owned.identity = original
        assert (await manager.stop("relay")).passed
    finally:
        if original is not None:
            manager._owned["relay"].identity = original
        await manager.cleanup(grace_seconds=0, kill_seconds=5)


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_interrupt_stop_or_stack_cleanup(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "owned", "slow")
    manager = harness.manager
    try:
        await manager.start("read_audit")
        await harness.pids("read_audit")
        members = manager.members()
        stop = asyncio.create_task(manager.stop("read_audit", grace_seconds=5))
        for _ in range(5):
            await asyncio.sleep(0.03)
            stop.cancel()
        receipt = await asyncio.wait_for(stop, 8)
        assert receipt.passed and receipt.cancellation_observed
        assert_gone(members)
        await manager.start("api")
        await harness.pids("api")
        members = manager.members()
        cleanup = asyncio.create_task(manager.cleanup(grace_seconds=5))
        for _ in range(5):
            await asyncio.sleep(0.03)
            cleanup.cancel()
        receipts = await asyncio.wait_for(cleanup, 8)
        assert len(receipts) == 2 and all(item.passed for item in receipts)
        assert_gone(members)
        assert any(
            item["event"] == "stack_cleanup_cancellation_observed" for item in manager.events
        )
    finally:
        await manager.cleanup(grace_seconds=0, kill_seconds=5)


@pytest.mark.asyncio
async def test_noncooperative_graceful_stop_is_forced_and_never_claimed_graceful_success(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "owned", "uncooperative")
    manager = harness.manager
    try:
        await manager.start("api")
        await harness.pids("api")
        members = manager.members()
        receipt = await manager.stop("api", grace_seconds=0.1, kill_seconds=5)
        assert receipt.forced and receipt.descendants_empty and not receipt.passed
        assert receipt.action == "graceful"
        assert receipt.elapsed_seconds < 7
        assert_gone(members)
    finally:
        await manager.cleanup(grace_seconds=0, kill_seconds=5)


@pytest.mark.asyncio
async def test_invalid_role_budget_and_outside_paths_do_not_spawn(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "owned")
    manager = harness.manager
    with pytest.raises(ValueError, match="UNKNOWN_SERVICE_ROLE"):
        await manager.start("untrusted")
    assert harness.launches == [] and manager.members() == ()
    with pytest.raises(ProcessControlError, match="DOCUMENT_WORKER_NOT_STOPPED"):
        await manager.restart_document_worker()
    for value in (-1, float("nan"), float("inf"), True, 31):
        with pytest.raises(ValueError, match="INVALID_PROCESS_STOP_BUDGET"):
            await manager.stop("api", grace_seconds=value)
    launch = TrustedLaunch((sys.executable,), tmp_path, {}, tmp_path / "outside.log")
    outside = OwnedRoleProcesses(lambda *_: launch, workspace=harness.root)
    with pytest.raises(ValueError, match="OUTSIDE_WORKSPACE"):
        await outside.start("api")
    assert not launch.private_log.exists()


@pytest.mark.asyncio
async def test_job_assignment_failure_never_releases_role_gate(tmp_path: Path, monkeypatch) -> None:
    # The actual Windows Popen remains behind the gate until Job assignment.
    # Linux exercises the equivalent invalid executable before any role executes.
    harness = Harness(tmp_path / "owned")
    manager = harness.manager
    if os.name == "nt":

        def reject(self, process):
            raise ProcessControlError("JOB_ASSIGNMENT_FAILED")

        monkeypatch.setattr(processes._WindowsJob, "assign", reject)
        with pytest.raises(ProcessControlError, match="JOB_ASSIGNMENT_FAILED"):
            await manager.start("api")
    else:
        original = harness.factory
        manager._factory = lambda role, generation: replace(
            original(role, generation), argv=(str(harness.root / "absent"),)
        )
        with pytest.raises(ValueError, match="INVALID_TRUSTED_LAUNCH"):
            await manager.start("api")
    assert not list(harness.root.rglob("pid-*.json"))
    assert manager.members() == ()


@pytest.mark.asyncio
async def test_start_cancellation_reaps_partially_started_tree(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "owned", "uncooperative")
    manager = harness.manager
    start = asyncio.create_task(manager.start("api"))
    try:
        await asyncio.sleep(0)
        start.cancel()
        await asyncio.sleep(0.001)
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, 8)
        receipt = manager._owned["api"].receipt
        assert receipt is not None and receipt.descendants_empty
        assert receipt.action == "abrupt"
        assert manager.members() == ()
    finally:
        await manager.cleanup(grace_seconds=0, kill_seconds=5)


@pytest.mark.asyncio
async def test_role_cannot_execute_while_windows_assignment_is_delayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path / "owned")
    manager = harness.manager
    original = processes._WindowsJob.assign
    checked = []

    def delayed_assignment(self, process):
        time.sleep(0.2)
        assert process.poll() is None
        assert not list(harness.root.rglob("pid-*.json"))
        checked.append(process.pid)
        original(self, process)
        assert process.pid in self.pids()

    if os.name == "nt":
        monkeypatch.setattr(processes._WindowsJob, "assign", delayed_assignment)
    try:
        info = await manager.start("api")
        pids = await harness.pids("api")
        members = manager.members()
        assert {info.pid, info.role_pid, *pids}.issubset({item.pid for item in members})
        assert_contained(manager, "api", members)
        if os.name == "nt":
            assert checked == [info.pid]
        else:
            assert info.containment == "linux_session_subreaper"
        assert (await manager.stop("api", grace_seconds=5)).passed
        assert_gone(members)
    finally:
        await manager.cleanup(grace_seconds=0, kill_seconds=5)
