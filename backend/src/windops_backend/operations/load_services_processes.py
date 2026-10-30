"""Fresh, fixed-role service processes with task-owned descendant containment.

The command factory is trusted application code, never a CLI/profile input. On
Windows a Python gate is assigned to a private Job before executing the role;
the Job disallows breakaway and kills descendants when its last handle closes.
On Linux the gate is a session leader and a process-local child subreaper.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import subprocess  # nosec B404
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol

from windops_backend.operations.load_resources import (
    ProcessIdentity,
    ResourceObservationError,
    read_process,
    register_process,
)

Role = Literal["api", "general_worker", "relay", "read_audit", "document_worker"]
ROLES: tuple[Role, ...] = ("api", "general_worker", "relay", "read_audit", "document_worker")
ROOT = Path(__file__).resolve().parents[4]
MAX_MEMBERS = 64

# -I -S disables user/site imports before the one-byte gate. No role or child can
# execute until the parent has assigned containment and registered its identity.
# Only this fixed code is passed to -c; the trusted role argv follows separately.
_GUARDIAN = r"""
import ctypes, json, os, queue, signal, subprocess, sys, threading, time
def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields)), file=sys.stderr, flush=True)
try:
    if os.read(0, 1) != b"G":
        raise RuntimeError()
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError()
    commands = queue.Queue(maxsize=2)
    def receive():
        command = sys.stdin.buffer.readline(32)
        commands.put(command)
    threading.Thread(target=receive, daemon=True).start()
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE,
        stdout=sys.stdout, stderr=sys.stdout, close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    emit("started", pid=child.pid)
    stopping = False
    deadline = None
    announced = False
    while True:
        code = child.poll()
        if code is not None and not announced:
            emit("exited", returncode=code)
            announced = True
        if not stopping:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                command = None
            if command is not None:
                if command not in (b"stop\n", b""):
                    raise RuntimeError()
                stopping, deadline = True, time.monotonic() + 35
                if child.poll() is None:
                    try:
                        child.stdin.write(b"stop\n")
                        child.stdin.flush()
                        child.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
        if stopping and time.monotonic() >= deadline:
            if os.name != "nt":
                os.killpg(os.getpgrp(), signal.SIGKILL)
            raise RuntimeError()
        if stopping and code is not None:
            if sys.platform.startswith("linux"):
                while True:
                    try:
                        found, _ = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        found = 0
                    if not found:
                        break
                with open(f"/proc/{os.getpid()}/task/{os.getpid()}/children") as stream:
                    remaining = stream.read(4096).strip()
                if remaining:
                    time.sleep(.02)
                    continue
            sys.exit(code if code >= 0 else 1)
        time.sleep(.02)
except BaseException as exc:
    if isinstance(exc, SystemExit):
        raise
    emit("error")
    sys.exit(125)
"""


class ProcessControlError(RuntimeError):
    """Safe fixed code only; argv, environment and private logs never escape."""


@dataclass(frozen=True, slots=True)
class TrustedLaunch:
    argv: tuple[str, ...] = field(repr=False)
    cwd: Path = field(repr=False)
    env: Mapping[str, str] = field(repr=False)
    private_log: Path = field(repr=False)


@dataclass(frozen=True, slots=True)
class OwnedProcessInfo:
    role: Role
    generation: int
    pid: int
    start_identity: str
    role_pid: int
    role_exit_code: int | None
    started_at: str
    containment: Literal["windows_job", "linux_session_subreaper"]


@dataclass(frozen=True, slots=True)
class StopReceipt:
    role: Role
    generation: int
    pid: int
    action: Literal["graceful", "abrupt"]
    started_at: str
    finished_at: str
    elapsed_seconds: float
    identity_verified: bool
    descendants_empty: bool
    role_exit_code: int | None
    guardian_exit_code: int | None
    forced: bool
    passed: bool
    cancellation_observed: bool = False
    reason_codes: tuple[str, ...] = ()


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _JobPids(ctypes.Structure):
    _fields_ = [
        ("assigned", ctypes.c_uint32),
        ("count", ctypes.c_uint32),
        ("pids", ctypes.c_size_t * MAX_MEMBERS),
    ]


class _WindowsJob:
    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if not callable(loader):
            raise ProcessControlError("PROCESS_PLATFORM_UNSUPPORTED")
        self.api: Any = loader("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ((ctypes.c_void_p, ctypes.c_wchar_p), ctypes.c_void_p),
            "SetInformationJobObject": (
                (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32),
                ctypes.c_int,
            ),
            "AssignProcessToJobObject": ((ctypes.c_void_p, ctypes.c_void_p), ctypes.c_int),
            "QueryInformationJobObject": (
                (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p),
                ctypes.c_int,
            ),
            "TerminateJobObject": ((ctypes.c_void_p, ctypes.c_uint32), ctypes.c_int),
            "CloseHandle": ((ctypes.c_void_p,), ctypes.c_int),
            "OpenProcess": ((ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32), ctypes.c_void_p),
            "IsProcessInJob": ((ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p), ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle: int | None = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ProcessControlError("JOB_CREATE_FAILED")
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x8  # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS; no breakaway.
        limits.basic.active_process_limit = MAX_MEMBERS
        if not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise ProcessControlError("JOB_LIMITS_FAILED")

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        handle = getattr(process, "_handle", None)  # CPython's retained native Popen handle.
        if handle is None or not self.api.AssignProcessToJobObject(self.handle, int(handle)):
            raise ProcessControlError("JOB_ASSIGNMENT_FAILED")

    def pids(self) -> tuple[int, ...]:
        if self.handle is None:
            return ()
        result = _JobPids()
        if not self.api.QueryInformationJobObject(
            self.handle, 3, ctypes.byref(result), ctypes.sizeof(result), None
        ):
            raise ProcessControlError("JOB_MEMBERS_UNAVAILABLE")
        if result.assigned > MAX_MEMBERS or result.count != result.assigned:
            raise ProcessControlError("JOB_MEMBER_BOUND_EXCEEDED")
        return tuple(int(result.pids[index]) for index in range(result.count))

    def terminate(self) -> None:
        if self.handle is not None and not self.api.TerminateJobObject(self.handle, 137):
            raise ProcessControlError("JOB_TERMINATION_FAILED")

    def contains(self, pid: int) -> bool:
        process = self.api.OpenProcess(0x1000, False, pid)
        if not process:
            get_error = getattr(ctypes, "get_last_error", None)
            if callable(get_error) and get_error() == 87:
                return False
            raise ProcessControlError("JOB_MEMBER_IDENTITY_UNAVAILABLE")
        try:
            contained = ctypes.c_int()
            if not self.api.IsProcessInJob(process, self.handle, ctypes.byref(contained)):
                raise ProcessControlError("JOB_MEMBER_IDENTITY_UNAVAILABLE")
            return bool(contained.value)
        finally:
            if not self.api.CloseHandle(process):
                raise ProcessControlError("PROCESS_HANDLE_CLOSE_FAILED")

    def close(self) -> None:
        if self.handle is not None:
            if not self.api.CloseHandle(self.handle):
                raise ProcessControlError("JOB_CLOSE_FAILED")
            self.handle = None


@dataclass(slots=True)
class _Owned:
    role: Role
    generation: int
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity
    log: BinaryIO
    job: _WindowsJob | None
    started_at: str
    role_pid: int = 0
    role_exit_code: int | None = None
    control_error: bool = False
    reader: threading.Thread | None = None
    members: dict[int, ProcessIdentity] = field(default_factory=dict)
    receipt: StopReceipt | None = None


def _control_reader(owned: _Owned) -> None:
    stream = owned.process.stderr
    if stream is None:
        owned.control_error = True
        return
    try:
        for _ in range(4):
            raw = stream.readline(1025)
            if not raw:
                return
            if len(raw) > 1024:
                raise ValueError()
            event = json.loads(raw)
            if (
                event.get("event") == "started"
                and type(event.get("pid")) is int
                and event["pid"] > 0
            ):
                if owned.role_pid:
                    raise ValueError()
                owned.role_pid = event["pid"]
            elif event.get("event") == "exited" and type(event.get("returncode")) is int:
                owned.role_exit_code = event["returncode"]
            else:
                raise ValueError()
        owned.control_error = True
    except (ValueError, OSError):
        owned.control_error = True


def _checked_role(role: Role) -> None:
    if role not in ROLES:
        raise ValueError("UNKNOWN_SERVICE_ROLE")


class _ProcessTree(Protocol):
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity


def _verify_root(owned: _ProcessTree) -> None:
    if owned.process.pid != owned.identity.pid:
        raise ProcessControlError("PROCESS_IDENTITY_CHANGED")
    if owned.process.poll() is None:
        try:
            read_process(owned.identity)
        except ResourceObservationError as exc:
            raise ProcessControlError("PROCESS_IDENTITY_CHANGED") from exc


def _posix(name: str, *arguments: int) -> int | None:
    operation = getattr(os, name, None)
    if not callable(operation):
        raise ProcessControlError("PROCESS_PLATFORM_UNSUPPORTED")
    value = operation(*arguments)
    return int(value) if value is not None else None


def _linux_members(owned: _ProcessTree) -> tuple[int, ...]:
    _verify_root(owned)
    if owned.process.poll() is not None:
        # The guardian exits normally only after its adopted children are reaped.
        # An unexpectedly dead guardian cannot establish future group ownership.
        if owned.process.returncode == 0:
            return ()
        raise ProcessControlError("GUARDIAN_EXITED_UNEXPECTEDLY")
    pending = [owned.process.pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        if len(seen) >= MAX_MEMBERS:
            raise ProcessControlError("PROCESS_MEMBER_BOUND_EXCEEDED")
        seen.add(pid)
        try:
            if (
                _posix("getsid", pid) != owned.process.pid
                or _posix("getpgid", pid) != owned.process.pid
            ):
                raise ProcessControlError("PROCESS_CONTAINMENT_CHANGED")
            raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
        except ProcessLookupError:
            continue
        except FileNotFoundError:
            continue
        if len(raw) > 4096:
            raise ProcessControlError("PROCESS_MEMBER_BOUND_EXCEEDED")
        pending.extend(int(value) for value in raw.split())
    return tuple(sorted(seen))


async def _finish_despite_cancellation[T](task: asyncio.Task[T]) -> tuple[T, bool]:
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.done() and task.cancelled():
                raise
            cancelled = True


def _spawn_gated(launch: TrustedLaunch, log: BinaryIO) -> subprocess.Popen[bytes]:
    # Keep creation and ownership registration in one non-awaiting section. A
    # cancelled thread-offloaded Popen could otherwise leave an unreturned child.
    # A Windows venv redirector can spawn the real interpreter before assignment.
    # Launch the base interpreter itself; this gate imports only the stdlib.
    executable = str(getattr(sys, "_base_executable", sys.executable))
    # Fixed gate and trusted application argv; shell execution is never enabled.
    return subprocess.Popen(  # nosec B603
        [executable, "-I", "-S", "-c", _GUARDIAN, *launch.argv],
        cwd=launch.cwd,
        env=dict(launch.env),
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=os.name != "nt",
        creationflags=0x8 if os.name == "nt" else 0,  # Windows DETACHED_PROCESS.
    )


def _validate_trusted_launch(launch: TrustedLaunch, workspace: Path) -> None:
    if (
        not isinstance(launch.argv, tuple)
        or not 1 <= len(launch.argv) <= 64
        or any(not isinstance(arg, str) or "\0" in arg for arg in launch.argv)
        or sum(len(arg) for arg in launch.argv) > 32_000
        or not Path(launch.argv[0]).is_absolute()
        or not Path(launch.argv[0]).is_file()
        or len(launch.env) > 512
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\0" in key + value
            or "=" in key
            or len(key + value) > 32_767
            for key, value in launch.env.items()
        )
    ):
        raise ValueError("INVALID_TRUSTED_LAUNCH")
    for path in (launch.cwd, launch.private_log):
        if not path.is_absolute() or not path.resolve().is_relative_to(workspace):
            raise ValueError("PROCESS_PATH_OUTSIDE_WORKSPACE")
        for parent in (path, *path.parents):
            if parent.is_symlink() or parent.is_junction():
                raise ValueError("PROCESS_LINKED_PATH")
            if parent == workspace:
                break


class OwnedRoleProcesses:
    """No adoption API: every handle and containment object is created here."""

    def __init__(
        self, factory: Callable[[Role, int], TrustedLaunch], *, workspace: Path = ROOT
    ) -> None:
        if os.name != "nt" and not sys.platform.startswith("linux"):
            raise ProcessControlError("PROCESS_PLATFORM_UNSUPPORTED")
        self._factory, self._workspace = factory, workspace.resolve()
        self._owned: dict[Role, _Owned] = {}
        self._generations: dict[Role, int] = {}
        self._stop_tasks: dict[Role, asyncio.Task[StopReceipt]] = {}
        self._events: list[dict[str, Any]] = []

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(event) for event in self._events)

    def info(self, role: Role) -> OwnedProcessInfo:
        _checked_role(role)
        owned = self._owned[role]
        return OwnedProcessInfo(
            role,
            owned.generation,
            owned.process.pid,
            owned.identity.start_identity,
            owned.role_pid,
            owned.role_exit_code,
            owned.started_at,
            "windows_job" if owned.job is not None else "linux_session_subreaper",
        )

    def _validate_launch(self, launch: TrustedLaunch) -> None:
        _validate_trusted_launch(launch, self._workspace)

    async def start(self, role: Role) -> OwnedProcessInfo:
        _checked_role(role)
        if role in self._owned and self._owned[role].receipt is None:
            raise ProcessControlError("ROLE_ALREADY_OWNED")
        generation = self._generations.get(role, 0) + 1
        if generation > 4:
            raise ProcessControlError("ROLE_GENERATION_BOUND_EXCEEDED")
        launch = self._factory(role, generation)
        self._validate_launch(launch)
        log = launch.private_log.open("xb")
        job: _WindowsJob | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            job = _WindowsJob() if os.name == "nt" else None
            process = _spawn_gated(launch, log)
            if job is not None:
                job.assign(process)
            identity = register_process(process.pid, label=f"{role}-guardian-{generation}")
            owned = _Owned(
                role, generation, process, identity, log, job, datetime.now(UTC).isoformat()
            )
            self._owned[role], self._generations[role] = owned, generation
            self._stop_tasks.pop(role, None)
            owned.reader = threading.Thread(target=_control_reader, args=(owned,), daemon=True)
            owned.reader.start()
            if process.stdin is None:
                raise ProcessControlError("PROCESS_CONTROL_UNAVAILABLE")
            process.stdin.write(b"G")
            process.stdin.flush()
            async with asyncio.timeout(10):
                while not owned.role_pid:
                    if owned.control_error or process.poll() is not None:
                        raise ProcessControlError("ROLE_START_FAILED")
                    await asyncio.sleep(0.01)
            self.members(role)
            self._events.append({"event": "started", **asdict(self.info(role))})
            return self.info(role)
        except BaseException:
            if role in self._owned and self._owned[role].process is process:
                await self.stop(role, abrupt=True, grace_seconds=0, kill_seconds=5)
            else:
                if job is not None:
                    job.terminate()
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if job is not None:
                    job.close()
                if process is not None:
                    for pipe in (process.stdin, process.stderr):
                        if pipe is not None:
                            pipe.close()
                log.close()
            raise

    def members(self, role: Role | None = None) -> tuple[ProcessIdentity, ...]:
        if role is not None:
            _checked_role(role)
        result: list[ProcessIdentity] = []
        selected = [self._owned[role]] if role is not None else list(self._owned.values())
        for owned in selected:
            if owned.receipt is not None:
                continue
            _verify_root(owned)
            pids = owned.job.pids() if owned.job is not None else _linux_members(owned)
            for pid in pids:
                try:
                    identity = owned.members.get(pid)
                    if identity is None:
                        identity = register_process(pid, label=f"{owned.role}-{owned.generation}")
                        owned.members[pid] = identity
                    else:
                        read_process(identity)
                    if owned.job is not None and not owned.job.contains(pid):
                        # The listed process may have exited and its PID been reused.
                        continue
                    result.append(identity)
                except ResourceObservationError as exc:
                    current = owned.job.pids() if owned.job is not None else _linux_members(owned)
                    if pid in current:
                        raise ProcessControlError("PROCESS_MEMBER_IDENTITY_CHANGED") from exc
        return tuple(result)

    def _force(self, owned: _Owned) -> None:
        _verify_root(owned)
        if owned.job is not None:
            owned.job.terminate()
        elif owned.process.poll() is None:
            _posix("killpg", owned.process.pid, 9)  # Linux SIGKILL.
        else:
            raise ProcessControlError("GUARDIAN_EXITED_UNEXPECTEDLY")

    def _empty(self, owned: _Owned) -> bool:
        if owned.job is not None:
            return not owned.job.pids() and owned.process.poll() is not None
        if owned.process.poll() is None:
            return False
        try:
            _posix("killpg", owned.process.pid, 0)
        except ProcessLookupError:
            return True
        return False

    async def _wait_empty(self, owned: _Owned, seconds: float) -> None:
        # Job/group membership has no asyncio event; query it on a bounded plan.
        end = time.monotonic() + seconds
        for _ in range(math.ceil(seconds / 0.02)):
            if self._empty(owned) or time.monotonic() >= end:
                return
            await asyncio.sleep(min(0.02, max(0, end - time.monotonic())))

    async def _stop(
        self, owned: _Owned, abrupt: bool, grace_seconds: float, kill_seconds: float
    ) -> StopReceipt:
        started, at = time.monotonic(), datetime.now(UTC).isoformat()
        forced, verified, empty = abrupt, False, False
        reasons: list[str] = []
        try:
            _verify_root(owned)
            verified = True
            if not abrupt and owned.process.poll() is None:
                if owned.process.stdin is None:
                    raise ProcessControlError("PROCESS_CONTROL_UNAVAILABLE")
                try:
                    owned.process.stdin.write(b"stop\n")
                    owned.process.stdin.flush()
                except (BrokenPipeError, OSError):
                    reasons.append("GRACEFUL_CONTROL_FAILED")
                await self._wait_empty(owned, grace_seconds)
            if not self._empty(owned):
                forced = True
                if not abrupt:
                    reasons.append("GRACEFUL_STOP_INCOMPLETE")
                self._force(owned)
                await self._wait_empty(owned, kill_seconds)
            empty = self._empty(owned)
            if not empty:
                reasons.append("PROCESS_CLEANUP_TIMEOUT")
        except ProcessControlError as exc:
            reasons.append(str(exc))
        except OSError:
            reasons.append("PROCESS_CONTROL_FAILED")
        finally:
            if empty:
                if owned.reader is not None:
                    owned.reader.join(timeout=0.5)
                    if owned.reader.is_alive():
                        reasons.append("PROCESS_CONTROL_READER_UNCLOSED")
                for pipe in (owned.process.stdin, owned.process.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            reasons.append("PROCESS_PIPE_CLOSE_FAILED")
                try:
                    owned.log.close()
                except OSError:
                    reasons.append("PROCESS_LOG_CLOSE_FAILED")
                if owned.job is not None:
                    try:
                        owned.job.close()
                    except ProcessControlError:
                        reasons.append("JOB_CLOSE_FAILED")
        receipt = StopReceipt(
            owned.role,
            owned.generation,
            owned.process.pid,
            "abrupt" if abrupt else "graceful",
            at,
            datetime.now(UTC).isoformat(),
            time.monotonic() - started,
            verified,
            empty,
            owned.role_exit_code,
            owned.process.returncode,
            forced,
            verified
            and empty
            and not reasons
            and (
                abrupt
                or (not forced and owned.role_exit_code == 0 and owned.process.returncode == 0)
            ),
            reason_codes=tuple(reasons),
        )
        if empty:
            owned.receipt = receipt
        self._events.append({"event": "stopped", **asdict(receipt)})
        return receipt

    async def stop(
        self,
        role: Role,
        *,
        abrupt: bool = False,
        grace_seconds: float = 10,
        kill_seconds: float = 5,
    ) -> StopReceipt:
        _checked_role(role)
        if (
            any(
                isinstance(value, bool) or not math.isfinite(value)
                for value in (grace_seconds, kill_seconds)
            )
            or not 0 <= grace_seconds <= 30
            or not 0.05 <= kill_seconds <= 10
        ):
            raise ValueError("INVALID_PROCESS_STOP_BUDGET")
        owned = self._owned[role]
        if owned.receipt is not None:
            return owned.receipt
        task = self._stop_tasks.get(role)
        if task is None or task.done():
            task = asyncio.create_task(self._stop(owned, abrupt, grace_seconds, kill_seconds))
            self._stop_tasks[role] = task
        receipt, cancelled = await _finish_despite_cancellation(task)
        if cancelled:
            receipt = replace(receipt, cancellation_observed=True)
            if owned.receipt is not None:
                owned.receipt = receipt
            self._events.append(
                {
                    "event": "cleanup_cancellation_observed",
                    "role": role,
                    "generation": owned.generation,
                }
            )
        return receipt

    async def restart_document_worker(self) -> OwnedProcessInfo:
        previous = self._owned.get("document_worker")
        if previous is None or previous.receipt is None or not previous.receipt.descendants_empty:
            raise ProcessControlError("DOCUMENT_WORKER_NOT_STOPPED")
        return await self.start("document_worker")

    async def cleanup(
        self, *, grace_seconds: float = 10, kill_seconds: float = 5
    ) -> tuple[StopReceipt, ...]:
        async def finish() -> tuple[StopReceipt, ...]:
            return tuple(
                await asyncio.gather(
                    *(
                        self.stop(role, grace_seconds=grace_seconds, kill_seconds=kill_seconds)
                        for role in reversed(ROLES)
                        if role in self._owned
                    )
                )
            )

        result, cancelled = await _finish_despite_cancellation(asyncio.create_task(finish()))
        if cancelled:
            self._events.append({"event": "stack_cleanup_cancellation_observed"})
        return result
