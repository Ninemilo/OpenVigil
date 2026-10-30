"""Bounded internal CLI calls in a fresh owned process tree, without a shell.

Callers supply fixed application-owned argv, never profile/user command strings.
Cancellation is reported only after tree cleanup, so callers can persist its
receipt before propagating cancellation. A zero exit with orphans is a failure.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import subprocess  # nosec B404
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, BinaryIO

from windops_backend.operations.load_resources import (
    ProcessIdentity,
    ResourceObservationError,
    register_process,
)
from windops_backend.operations.load_services_processes import (
    ProcessControlError,
    TrustedLaunch,
    _finish_despite_cancellation,
    _linux_members,
    _posix,
    _validate_trusted_launch,
    _verify_root,
    _WindowsJob,
)

# The direct base interpreter cannot spawn the command before Job assignment.
# Keeping this gate alive after command exit also pins the Linux session leader.
_COMMAND_GUARDIAN = r"""
import ctypes, json, os, subprocess, sys
try:
    if os.read(0, 1) != b"G":
        raise RuntimeError()
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError()
    child = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL,
        stdout=sys.stdout, stderr=sys.stderr, close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    code = child.wait()
    with open(sys.argv[1], "wb") as status:
        status.write(json.dumps({"exit_code": code}).encode("ascii") + b"\n")
        status.flush()
    if os.read(0, 1) != b"S":
        raise RuntimeError()
except BaseException:
    sys.exit(125)
"""


@dataclass(frozen=True, slots=True)
class OwnedCommandResult:
    exit_code: int | None
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    cleanup_passed: bool
    reason_code: str | None


@dataclass(slots=True)
class _Capture:
    stream: IO[bytes]
    output: BinaryIO
    limit: int
    data: bytearray = field(default_factory=bytearray)
    overflow: bool = False
    failed: bool = False
    thread: threading.Thread | None = None

    def read(self) -> None:
        try:
            if not isinstance(self.stream, io.BufferedReader):
                raise ValueError()
            while block := self.stream.read1(65_536):
                available = self.limit - len(self.data)
                retained = block[:available]
                self.data.extend(retained)
                self.output.write(retained)
                if len(block) > available:
                    self.overflow = True
                    return
        except (OSError, ValueError):
            self.failed = True


@dataclass(slots=True)
class _CommandTree:
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity
    job: _WindowsJob | None

    def pids(self) -> tuple[int, ...]:
        _verify_root(self)
        return self.job.pids() if self.job is not None else _linux_members(self)

    def empty(self) -> bool:
        if self.job is not None:
            return not self.job.pids() and self.process.poll() is not None
        if self.process.poll() is None:
            return False
        try:
            _posix("killpg", self.process.pid, 0)
        except ProcessLookupError:
            return True
        return False

    def terminate(self) -> None:
        _verify_root(self)
        if self.job is not None:
            self.job.terminate()
        elif self.process.poll() is None:
            _posix("killpg", self.process.pid, 9)
        else:
            raise ProcessControlError("GUARDIAN_EXITED_UNEXPECTEDLY")


async def _wait_empty(tree: _CommandTree, seconds: float) -> bool:
    end = time.monotonic() + seconds
    for _ in range(math.ceil(seconds / 0.02) + 1):
        if tree.empty():
            return True
        if time.monotonic() >= end:
            break
        await asyncio.sleep(min(0.02, max(0, end - time.monotonic())))
    return tree.empty()


async def _cleanup(tree: _CommandTree, *, force: bool) -> bool:
    passed = True
    try:
        if not force and tree.process.poll() is None:
            if tree.process.stdin is None:
                raise ProcessControlError("PROCESS_CONTROL_UNAVAILABLE")
            tree.process.stdin.write(b"S")
            tree.process.stdin.flush()
            if not await _wait_empty(tree, 0.5):
                passed = False
                force = True
        if force and not tree.empty():
            tree.terminate()
        if not await _wait_empty(tree, 5):
            passed = False
    except (ProcessControlError, OSError):
        passed = False
    finally:
        # The retained Job handle remains authoritative even on control failure.
        # Closing it requests final whole-tree termination, never a PID lookup.
        if tree.job is not None:
            try:
                if tree.job.pids():
                    tree.job.terminate()
                    if not await _wait_empty(tree, 1):
                        passed = False
                tree.job.close()
            except ProcessControlError:
                passed = False
    return passed and tree.empty()


def _read_exit(status: BinaryIO) -> int | None:
    status.seek(0)
    raw = status.read(1025)
    if len(raw) > 1024:
        raise ProcessControlError("COMMAND_CONTROL_FAILED")
    if not raw or not raw.endswith(b"\n"):
        return None
    try:
        value = json.loads(raw)
        if set(value) != {"exit_code"} or type(value["exit_code"]) is not int:
            raise ValueError()
        return int(value["exit_code"])
    except (ValueError, TypeError) as exc:
        raise ProcessControlError("COMMAND_CONTROL_FAILED") from exc


async def _observe(
    tree: _CommandTree, captures: tuple[_Capture, _Capture], status: BinaryIO, deadline: float
) -> tuple[int | None, str | None]:
    code = None
    completed_at = None
    # Each iteration has a finite deadline and checks both output readers.
    for _ in range(math.ceil(max(0, deadline - time.monotonic()) / 0.01) + 1):
        if any(capture.overflow for capture in captures):
            return code, "COMMAND_OUTPUT_LIMIT"
        if any(capture.failed for capture in captures):
            return code, "COMMAND_OUTPUT_FAILED"
        if time.monotonic() >= deadline:
            return code, "COMMAND_TIMEOUT"
        if code is None:
            code = _read_exit(status)
            if code is not None:
                completed_at = time.monotonic()
        if code is not None:
            descendants = set(tree.pids()) - {tree.process.pid}
            if not descendants:
                return code, None
            # Reaped launchers/console hosts can disappear just after wait().
            if completed_at is not None and time.monotonic() - completed_at >= 0.15:
                return code, "COMMAND_ORPHANS"
        if tree.process.poll() is not None:
            return code, "COMMAND_CONTROL_FAILED"
        await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))
    return code, "COMMAND_TIMEOUT"


def _validate(launch: TrustedLaunch, workspace: Path) -> None:
    _validate_trusted_launch(launch, workspace.resolve())


def _spawn(launch: TrustedLaunch, status_path: str) -> subprocess.Popen[bytes]:
    # Must stay synchronous until its retained handle is contained: cancelling a
    # thread-offloaded Popen could leave a process whose handle was never returned.
    executable = str(getattr(sys, "_base_executable", sys.executable))
    return subprocess.Popen(  # nosec B603
        [executable, "-I", "-S", "-c", _COMMAND_GUARDIAN, status_path, *launch.argv],
        cwd=launch.cwd,
        env=dict(launch.env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=os.name != "nt",
        creationflags=0x8 if os.name == "nt" else 0,
    )


async def run_owned_command(
    launch: TrustedLaunch,
    *,
    workspace: Path,
    deadline_seconds: float,
    maximum_output_bytes: int,
) -> OwnedCommandResult:
    """Run trusted argv; output limit is per stream, deadline covers execution.

    Tree cleanup and reader joins have a separate bound of seven seconds. Outputs are stored
    beside private_log as exclusive .stdout/.stderr/.status files. Cancellation
    returns COMMAND_CANCELLED after cleanup; callers must preserve that state.
    A nonzero actual command exit is returned unchanged for caller interpretation.
    """
    if (
        isinstance(deadline_seconds, bool)
        or not math.isfinite(deadline_seconds)
        or not 0 < deadline_seconds <= 900
        or type(maximum_output_bytes) is not int
        or not 1 <= maximum_output_bytes <= 16 * 1024 * 1024
    ):
        raise ValueError("INVALID_COMMAND_BUDGET")
    if os.name != "nt" and not sys.platform.startswith("linux"):
        raise ProcessControlError("PROCESS_PLATFORM_UNSUPPORTED")
    _validate(launch, workspace)
    deadline = time.monotonic() + deadline_seconds
    handles: list[BinaryIO] = []
    process = None
    job = None
    tree = None
    captures: tuple[_Capture, _Capture] | None = None
    code: int | None = None
    reason: str | None = None
    cleanup_passed = True
    try:
        # Exclusive creation also rejects an existing link or another run's file.
        for suffix in ("", ".stdout", ".stderr", ".status"):
            handles.append(
                launch.private_log.with_name(launch.private_log.name + suffix).open("x+b")
            )
        summary, stdout_file, stderr_file, status = handles
        job = _WindowsJob() if os.name == "nt" else None
        # No await between creation, Job assignment, identity registration and gate.
        process = _spawn(launch, str(status.name))
        if job is not None:
            job.assign(process)
        tree = _CommandTree(process, register_process(process.pid, label="command-guardian"), job)
        if process.stdout is None or process.stderr is None or process.stdin is None:
            raise ProcessControlError("COMMAND_CONTROL_FAILED")
        captures = (
            _Capture(process.stdout, stdout_file, maximum_output_bytes),
            _Capture(process.stderr, stderr_file, maximum_output_bytes),
        )
        for capture in captures:
            capture.thread = threading.Thread(target=capture.read, daemon=True)
            capture.thread.start()
        process.stdin.write(b"G")
        process.stdin.flush()
        code, reason = await _observe(tree, captures, status, deadline)
    except asyncio.CancelledError:
        reason = "COMMAND_CANCELLED"
    except (OSError, ProcessControlError, ResourceObservationError):
        reason = "COMMAND_CONTROL_FAILED" if tree is not None else "COMMAND_START_FAILED"
    finally:
        if tree is not None:
            cleanup_passed, cancelled = await _finish_despite_cancellation(
                asyncio.create_task(_cleanup(tree, force=reason is not None))
            )
            if cancelled:
                reason = "COMMAND_CANCELLED"
        else:
            # Assignment can fail before the gate opens; no command can exist.
            try:
                if job is not None:
                    job.terminate()
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if job is not None:
                    job.close()
            except (ProcessControlError, OSError, subprocess.TimeoutExpired):
                cleanup_passed = False
        if captures is not None:
            for capture in captures:
                if capture.thread is not None:
                    capture.thread.join(timeout=0.25)
                    if capture.thread.is_alive():
                        cleanup_passed = False
                if capture.overflow and reason is None:
                    reason = "COMMAND_OUTPUT_LIMIT"
                if capture.failed:
                    cleanup_passed = False
        blocked_streams = {
            id(capture.stream)
            for capture in captures or ()
            if capture.thread is not None and capture.thread.is_alive()
        }
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                # BufferedReader.close waits on a reader lock. Never let a failed
                # native termination turn this failure receipt into an endless wait.
                if pipe is not None and id(pipe) not in blocked_streams:
                    try:
                        pipe.close()
                    except OSError:
                        cleanup_passed = False
        if not cleanup_passed and reason is None:
            reason = "COMMAND_CLEANUP_FAILED"
        if len(handles) == 4:
            try:
                handles[0].write(
                    json.dumps(
                        {"exit_code": code, "cleanup_passed": cleanup_passed, "reason_code": reason}
                    ).encode("ascii")
                )
            except OSError:
                cleanup_passed = False
                reason = reason or "COMMAND_OUTPUT_FAILED"
        for handle in handles:
            try:
                handle.close()
            except OSError:
                cleanup_passed = False
                reason = reason or "COMMAND_OUTPUT_FAILED"
    return OwnedCommandResult(
        code,
        bytes(captures[0].data) if captures else b"",
        bytes(captures[1].data) if captures else b"",
        cleanup_passed,
        reason,
    )
