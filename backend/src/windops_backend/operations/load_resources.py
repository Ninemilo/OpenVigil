"""Bounded resource observations for explicitly registered, caller-owned PIDs."""

from __future__ import annotations

import copy
import ctypes
import math
import os
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID


class ResourceObservationError(RuntimeError):
    def __init__(self, code: str, pid: int, os_error: int | None = None) -> None:
        self.code, self.pid, self.os_error = code, pid, os_error
        super().__init__(f"{code}: pid={pid}, os_error={os_error}")


@dataclass(frozen=True, slots=True)
class ResourceIssue:
    code: str
    os_error: int | None = None


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    label: str
    platform: str
    start_identity: str


@dataclass(frozen=True, slots=True)
class ProcessSample:
    pid: int
    start_identity: str
    observed_monotonic: float
    rss_bytes: int | None
    cpu_seconds: float
    issues: tuple[ResourceIssue, ...] = ()


def _os_error(pid: int, exc: OSError) -> ResourceObservationError:
    code = "PROCESS_READ_FAILED"
    if isinstance(exc, PermissionError):
        code = "PROCESS_PERMISSION_DENIED"
    elif isinstance(exc, (FileNotFoundError, ProcessLookupError)):
        code = "PROCESS_MISSING"
    return ResourceObservationError(code, pid, exc.errno)


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def ticks(self) -> int:
        return int(self.high) << 32 | int(self.low)


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("page_fault_count", ctypes.c_uint32),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    ]


def _read_windows(pid: int) -> ProcessSample:
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi: Any = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.GetProcessTimes.argtypes = (ctypes.c_void_p, *([ctypes.POINTER(_FileTime)] * 4))
    kernel.GetProcessTimes.restype = ctypes.c_int
    psapi.GetProcessMemoryInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_MemoryCounters),
        ctypes.c_uint32,
    )
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    # Query-only handle: never request mutation/termination rights.
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        code = "PROCESS_PERMISSION_DENIED" if error == 5 else "PROCESS_READ_FAILED"
        if error == 87:
            code = "PROCESS_MISSING"
        raise ResourceObservationError(code, pid, error)
    try:
        created, exited, system, user = (_FileTime() for _ in range(4))
        if not kernel.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(system),
            ctypes.byref(user),
        ):
            error = ctypes.get_last_error()
            raise ResourceObservationError(
                "PROCESS_PERMISSION_DENIED" if error == 5 else "PROCESS_READ_FAILED",
                pid,
                error,
            )
        if exited.ticks():
            raise ResourceObservationError("PROCESS_EXITED", pid)
        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        issues: tuple[ResourceIssue, ...] = ()
        rss: int | None = None
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            if counters.working_set_size > 0:
                rss = int(counters.working_set_size)
            else:
                issues = (ResourceIssue("RSS_UNAVAILABLE"),)
        else:
            error = ctypes.get_last_error()
            issues = (
                ResourceIssue("RSS_PERMISSION_DENIED" if error == 5 else "RSS_UNAVAILABLE", error),
            )
        return ProcessSample(
            pid,
            f"windows-filetime:{created.ticks()}",
            time.monotonic(),
            rss,
            (system.ticks() + user.ticks()) / 10_000_000,
            issues,
        )
    finally:
        if not kernel.CloseHandle(handle):
            raise ResourceObservationError(
                "PROCESS_HANDLE_CLOSE_FAILED", pid, ctypes.get_last_error()
            )


def _linux_stat(pid: int, proc_root: Path) -> tuple[str, int, int]:
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        prefix, fields = raw.rsplit(")", 1)
        if int(prefix.split("(", 1)[0]) != pid:
            raise ValueError("wrong pid")
        values = fields.split()
        if values[0] in {"Z", "X", "x"}:
            raise ResourceObservationError("PROCESS_EXITED", pid)
        start, cpu = int(values[19]), int(values[11]) + int(values[12])
        if start < 0 or cpu < 0:
            raise ValueError("negative counters")
        return values[0], start, cpu
    except OSError as exc:
        raise _os_error(pid, exc) from exc
    except (ValueError, IndexError) as exc:
        raise ResourceObservationError("PROCESS_STAT_MALFORMED", pid) from exc


def _read_linux(
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
    clock_ticks: int | None = None,
    page_size: int | None = None,
) -> ProcessSample:
    _, started, _ = _linux_stat(pid, proc_root)
    try:
        boot = str(UUID((proc_root / "sys/kernel/random/boot_id").read_text().strip()))
    except OSError as exc:
        raise _os_error(pid, exc) from exc
    except ValueError as exc:
        raise ResourceObservationError("PROCESS_BOOT_ID_UNAVAILABLE", pid) from exc
    sysconf = getattr(os, "sysconf", None)

    def counter_unit(explicit: int | None, name: str) -> int:
        if explicit is not None:
            return explicit
        if not callable(sysconf):
            raise ResourceObservationError("PROCESS_COUNTER_UNITS_UNAVAILABLE", pid)
        try:
            return int(sysconf(name))
        except (OSError, ValueError) as exc:
            raise ResourceObservationError("PROCESS_COUNTER_UNITS_UNAVAILABLE", pid) from exc

    ticks, pages = counter_unit(clock_ticks, "SC_CLK_TCK"), counter_unit(page_size, "SC_PAGE_SIZE")
    if ticks <= 0 or pages <= 0:
        raise ResourceObservationError("PROCESS_COUNTER_UNITS_INVALID", pid)
    rss: int | None = None
    issues: tuple[ResourceIssue, ...] = ()
    try:
        resident_pages = int((proc_root / str(pid) / "statm").read_text().split()[1])
        if resident_pages > 0:
            rss = resident_pages * pages
        else:
            issues = (ResourceIssue("RSS_UNAVAILABLE"),)
    except OSError as exc:
        issues = (
            ResourceIssue(
                "RSS_PERMISSION_DENIED" if isinstance(exc, PermissionError) else "RSS_UNAVAILABLE",
                exc.errno,
            ),
        )
    except (ValueError, IndexError):
        issues = (ResourceIssue("RSS_MALFORMED"),)
    # Detect PID reuse or exit between the identity and memory reads.
    _, after_started, cpu = _linux_stat(pid, proc_root)
    if started != after_started:
        raise ResourceObservationError("PROCESS_IDENTITY_CHANGED", pid)
    return ProcessSample(
        pid,
        f"linux-boot:{boot}:startticks:{started}",
        time.monotonic(),
        rss,
        cpu / ticks,
        issues,
    )


def _read_native(pid: int) -> ProcessSample:
    if sys.platform == "win32":
        return _read_windows(pid)
    if sys.platform.startswith("linux"):
        return _read_linux(pid)
    raise ResourceObservationError("PROCESS_PLATFORM_UNSUPPORTED", pid)


def register_process(pid: int | None = None, *, label: str = "process") -> ProcessIdentity:
    """Register current PID or a PID explicitly supplied as owned by the caller.

    This reads no process inventory and cannot establish ownership on behalf of
    the caller. Callers must supply only their task-owned processes.
    """
    selected = os.getpid() if pid is None else pid
    if (
        isinstance(selected, bool)
        or not isinstance(selected, int)
        or not 0 < selected <= 0xFFFFFFFF
    ):
        raise ValueError("pid must be a positive native process identifier")
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        raise ValueError("label must contain 1 to 120 characters")
    sample = _read_native(selected)
    return ProcessIdentity(selected, label, sys.platform, sample.start_identity)


def read_process(identity: ProcessIdentity) -> ProcessSample:
    if identity.platform != sys.platform:
        raise ResourceObservationError("PROCESS_IDENTITY_CHANGED", identity.pid)
    sample = _read_native(identity.pid)
    if sample.start_identity != identity.start_identity:
        raise ResourceObservationError("PROCESS_IDENTITY_CHANGED", identity.pid)
    return sample


class ProcessResourceSampler:
    """Sample registered PIDs on a bounded schedule; stop never touches those PIDs."""

    def __init__(
        self,
        identities: Sequence[ProcessIdentity],
        *,
        duration_seconds: float,
        interval_seconds: float,
    ) -> None:
        if (
            not identities
            or len(identities) > 32
            or len({x.pid for x in identities}) != len(identities)
        ):
            raise ValueError("register between 1 and 32 distinct processes")
        if (
            isinstance(duration_seconds, bool)
            or isinstance(interval_seconds, bool)
            or not math.isfinite(duration_seconds)
            or not math.isfinite(interval_seconds)
            or not 0 < duration_seconds <= 3600
            or not 0.02 <= interval_seconds <= duration_seconds
        ):
            raise ValueError("require 0.02 <= interval <= duration <= 3600 seconds")
        self.identities = tuple(identities)
        self.duration_seconds, self.interval_seconds = duration_seconds, interval_seconds
        self._planned = math.ceil(duration_seconds / interval_seconds)
        if self._planned * len(identities) > 20_000:
            raise ValueError("sampling budget exceeds 20000 process observations")
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._started: float | None = None
        self._ended: float | None = None
        self._samples: list[dict[str, Any]] = []
        self._report: dict[str, Any] | None = None
        self._fatal: str | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _take_sample(self, slot: int) -> None:
        for identity in self.identities:
            if self._stop.is_set():
                return
            try:
                entry: dict[str, Any] = {
                    "slot": slot,
                    **asdict(read_process(identity)),
                    "error": None,
                }
            except ResourceObservationError as exc:
                entry = {
                    "slot": slot,
                    "pid": identity.pid,
                    "observed_monotonic": time.monotonic(),
                    "start_identity": None,
                    "rss_bytes": None,
                    "cpu_seconds": None,
                    "issues": [],
                    "error": {"code": exc.code, "os_error": exc.os_error},
                }
            with self._lock:
                self._samples.append(entry)

    def start(self) -> ProcessResourceSampler:
        with self._lock:
            if self._started is not None or self._stop.is_set():
                raise RuntimeError("sampler cannot be started twice or after stop")
            self._started = time.monotonic()
            self._thread = threading.Thread(
                target=self._run, name="load-resource-sampler", daemon=True
            )
            self._thread.start()
        return self

    def _run(self) -> None:
        try:
            if self._started is None:
                raise RuntimeError("RESOURCE_SAMPLER_NOT_STARTED")
            slot = 0
            while slot < self._planned:
                remaining = self._started + slot * self.interval_seconds - time.monotonic()
                if self._stop.wait(max(0.0, remaining)):
                    break
                elapsed = time.monotonic() - self._started
                if elapsed >= self.duration_seconds:
                    break
                # Missed intervals lower coverage; never issue a burst of catch-up reads.
                slot = max(slot, int(elapsed / self.interval_seconds))
                self._take_sample(slot)
                slot += 1
            if not self._stop.is_set():
                self._stop.wait(max(0.0, self._started + self.duration_seconds - time.monotonic()))
        except Exception as exc:
            self._fatal = type(exc).__name__
        finally:
            self._ended = time.monotonic()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("RESOURCE_SAMPLER_STOP_TIMEOUT")
        with self._lock:
            if self._report is not None:
                return copy.deepcopy(self._report)
            elapsed = (
                max(0.0, (self._ended or time.monotonic()) - self._started)
                if self._started is not None
                else 0.0
            )
            expected = (
                min(self._planned, int(elapsed / self.interval_seconds) + 1)
                if self._started is not None
                else 0
            )
            processes: list[dict[str, Any]] = []
            for identity in self.identities:
                samples = [sample for sample in self._samples if sample["pid"] == identity.pid]
                rss = [sample["rss_bytes"] for sample in samples if sample["rss_bytes"] is not None]
                cpu = [sample for sample in samples if sample["cpu_seconds"] is not None]
                processes.append(
                    {
                        "identity": asdict(identity),
                        "samples": samples,
                        "attempted_samples": len(samples),
                        "identity_valid_samples": sum(
                            sample["error"] is None for sample in samples
                        ),
                        "rss_valid_samples": len(rss),
                        "cpu_valid_samples": len(cpu),
                        "rss_coverage": len(rss) / expected if expected else None,
                        "cpu_coverage": len(cpu) / expected if expected else None,
                        "observed_peak_rss_bytes": max(rss) if rss else None,
                        "last_cpu_seconds": cpu[-1]["cpu_seconds"] if cpu else None,
                        "cpu_observed_delta_seconds": cpu[-1]["cpu_seconds"] - cpu[0]["cpu_seconds"]
                        if len(cpu) >= 2
                        else None,
                        "cpu_observed_interval_seconds": cpu[-1]["observed_monotonic"]
                        - cpu[0]["observed_monotonic"]
                        if len(cpu) >= 2
                        else None,
                    }
                )
            expected_observations = expected * len(self.identities)
            rss_valid = sum(item["rss_valid_samples"] for item in processes)
            cpu_valid = sum(item["cpu_valid_samples"] for item in processes)
            self._report = {
                "schema_version": "openvigil.load-process-resources.v1",
                "sampler_stopped": True,
                "requested_duration_seconds": self.duration_seconds,
                "elapsed_seconds": elapsed,
                "interval_seconds": self.interval_seconds,
                "planned_slot_count": self._planned,
                "expected_slot_count": expected,
                "completed_duration": elapsed >= self.duration_seconds,
                "fatal_error": self._fatal,
                "processes": processes,
                "aggregate": {
                    "expected_observations": expected_observations,
                    "attempted_observations": len(self._samples),
                    "identity_valid_observations": sum(
                        item["identity_valid_samples"] for item in processes
                    ),
                    "rss_valid_observations": rss_valid,
                    "cpu_valid_observations": cpu_valid,
                    "rss_coverage": rss_valid / expected_observations
                    if expected_observations
                    else None,
                    "cpu_coverage": cpu_valid / expected_observations
                    if expected_observations
                    else None,
                },
                "rss_semantics": (
                    "sampled current resident bytes; observed peak is not an absolute peak"
                ),
                "cpu_semantics": (
                    "cumulative process user plus kernel CPU seconds; "
                    "delta covers observed interval only"
                ),
            }
            return copy.deepcopy(self._report)

    def __enter__(self) -> ProcessResourceSampler:
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.stop()
