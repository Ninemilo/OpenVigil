from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from windops_backend.operations import load_resources as resources


def _wait_stopped(sampler: resources.ProcessResourceSampler) -> None:
    deadline = time.monotonic() + 3
    while sampler.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not sampler.is_running, "bounded sampler did not stop itself"


def test_native_current_process_reports_real_identity_rss_and_cumulative_cpu() -> None:
    identity = resources.register_process(label="test-owned-current-process")
    before = resources.read_process(identity)
    deadline = time.perf_counter() + 0.08
    while time.perf_counter() < deadline:
        sum(range(1000))
    after = resources.read_process(identity)
    assert identity.pid == os.getpid()
    assert identity.platform == sys.platform
    assert before.start_identity == after.start_identity == identity.start_identity
    assert before.rss_bytes is not None and before.rss_bytes > 0, before.issues
    assert after.rss_bytes is not None and after.rss_bytes > 0, after.issues
    assert after.cpu_seconds > before.cpu_seconds >= 0
    assert after.observed_monotonic >= before.observed_monotonic
    assert not before.issues and not after.issues


def test_wrong_start_identity_fails_closed_on_real_current_process() -> None:
    identity = resources.register_process()
    with pytest.raises(resources.ResourceObservationError, match="PROCESS_IDENTITY_CHANGED"):
        resources.read_process(replace(identity, start_identity="unrelated-start-identity"))
    with pytest.raises(resources.ResourceObservationError, match="PROCESS_IDENTITY_CHANGED"):
        resources.read_process(replace(identity, platform="unrelated-platform"))


def test_bounded_sampler_records_current_and_owned_child_exit_without_zero_fill() -> None:
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; print('ready', flush=True); sys.stdin.readline()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    sampler: resources.ProcessResourceSampler | None = None
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        identities = [
            resources.register_process(),
            resources.register_process(child.pid, label="owned-child"),
        ]
        sampler = resources.ProcessResourceSampler(
            identities, duration_seconds=0.4, interval_seconds=0.04
        ).start()
        time.sleep(0.12)
        assert child.stdin is not None
        child.stdin.write("exit\n")
        child.stdin.flush()
        child.wait(timeout=5)
        _wait_stopped(sampler)
        report = sampler.stop()
        current, exited = report["processes"]
        assert current["rss_valid_samples"] >= 3
        assert exited["rss_valid_samples"] >= 1
        assert 0 < exited["rss_coverage"] < 1
        assert current["observed_peak_rss_bytes"] > 0
        assert current["cpu_observed_delta_seconds"] >= 0
        failures = [sample for sample in exited["samples"] if sample["error"]]
        assert failures
        assert all(
            sample["error"]["code"] in {"PROCESS_MISSING", "PROCESS_EXITED"} for sample in failures
        )
        assert all(
            sample["rss_bytes"] is None and sample["cpu_seconds"] is None for sample in failures
        )
        assert report["completed_duration"] and report["sampler_stopped"]
        assert report["aggregate"]["expected_observations"] == 2 * report["expected_slot_count"]
        assert (
            report["aggregate"]["rss_valid_observations"]
            < report["aggregate"]["expected_observations"]
        )
        assert "not an absolute peak" in report["rss_semantics"]
        assert sampler.stop() == report
        report["processes"].clear()
        assert len(sampler.stop()["processes"]) == 2
    finally:
        if sampler is not None:
            sampler.stop()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()


def test_context_exception_stops_promptly_and_stop_is_idempotent() -> None:
    sampler = resources.ProcessResourceSampler(
        [resources.register_process()], duration_seconds=2, interval_seconds=1
    )
    started = time.monotonic()
    with pytest.raises(ValueError, match="synthetic workload failure"), sampler:
        time.sleep(0.03)
        raise ValueError("synthetic workload failure")
    assert time.monotonic() - started < 1
    assert not sampler.is_running
    report = sampler.stop()
    assert not report["completed_duration"]
    assert report["sampler_stopped"]
    assert sampler.stop() == report
    with pytest.raises(RuntimeError, match="cannot be started"):
        sampler.start()


def test_permission_errors_are_reported_and_reduce_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = resources.register_process()

    def denied(_identity: resources.ProcessIdentity) -> resources.ProcessSample:
        raise resources.ResourceObservationError("PROCESS_PERMISSION_DENIED", identity.pid, 5)

    monkeypatch.setattr(resources, "read_process", denied)
    sampler = resources.ProcessResourceSampler(
        [identity], duration_seconds=0.09, interval_seconds=0.03
    ).start()
    _wait_stopped(sampler)
    report = sampler.stop()
    process = report["processes"][0]
    assert process["samples"]
    assert all(
        sample["error"] == {"code": "PROCESS_PERMISSION_DENIED", "os_error": 5}
        for sample in process["samples"]
    )
    assert process["observed_peak_rss_bytes"] is None
    assert process["last_cpu_seconds"] is None
    assert process["cpu_observed_delta_seconds"] is None
    assert process["rss_coverage"] == process["cpu_coverage"] == 0
    assert report["aggregate"]["rss_valid_observations"] == 0
    assert report["aggregate"]["expected_observations"] > 0


def test_unexpected_reader_failure_is_explicit_and_thread_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = resources.register_process()
    monkeypatch.setattr(
        resources, "read_process", Mock(side_effect=ValueError("private diagnostic"))
    )
    sampler = resources.ProcessResourceSampler(
        [identity], duration_seconds=0.1, interval_seconds=0.03
    ).start()
    _wait_stopped(sampler)
    report = sampler.stop()
    assert report["fatal_error"] == "ValueError"
    assert report["aggregate"]["rss_valid_observations"] == 0
    assert "private diagnostic" not in str(report)


def test_missed_intervals_lower_coverage_instead_of_catch_up_bursts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = resources.register_process()
    original = resources.read_process

    def slow(selected: resources.ProcessIdentity) -> resources.ProcessSample:
        time.sleep(0.06)
        return original(selected)

    monkeypatch.setattr(resources, "read_process", slow)
    sampler = resources.ProcessResourceSampler(
        [identity], duration_seconds=0.15, interval_seconds=0.02
    ).start()
    _wait_stopped(sampler)
    report = sampler.stop()
    assert (
        report["aggregate"]["attempted_observations"] < report["aggregate"]["expected_observations"]
    )
    assert 0 < report["aggregate"]["rss_coverage"] < 1
    assert (
        len({sample["slot"] for sample in report["processes"][0]["samples"]})
        == report["aggregate"]["attempted_observations"]
    )


@pytest.mark.parametrize(
    "duration,interval",
    [
        (0, 0.02),
        (1, 0),
        (1, 2),
        (float("inf"), 1),
        (1, float("nan")),
        (3601, 1),
        (1, True),
        (1000, 0.02),
    ],
)
def test_sampling_budget_rejects_invalid_or_unbounded_requests(
    duration: float, interval: float
) -> None:
    with pytest.raises(ValueError):
        resources.ProcessResourceSampler(
            [resources.register_process()], duration_seconds=duration, interval_seconds=interval
        )


def _linux_fixture(tmp_path: Path) -> tuple[int, Path]:
    pid = 42
    root = tmp_path / "proc"
    process = root / str(pid)
    process.mkdir(parents=True)
    boot = root / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("b04d34c6-0c16-4a5d-9645-b72b00996689\n")
    fields = ["0"] * 22
    fields[0], fields[11], fields[12], fields[19] = "S", "250", "50", "900"
    (process / "stat").write_text(f"{pid} (owned child ) with spaces) {' '.join(fields)}")
    (process / "statm").write_text("100 8 4 2 0 3 0")
    return pid, root


def test_linux_proc_parser_and_unavailable_rss_are_explicit(tmp_path: Path) -> None:
    pid, root = _linux_fixture(tmp_path)
    sample = resources._read_linux(pid, proc_root=root, clock_ticks=100, page_size=4096)
    assert sample.rss_bytes == 8 * 4096
    assert sample.cpu_seconds == 3
    assert sample.start_identity.endswith(":startticks:900")
    (root / str(pid) / "statm").unlink()
    unknown = resources._read_linux(pid, proc_root=root, clock_ticks=100, page_size=4096)
    assert unknown.rss_bytes is None and unknown.cpu_seconds == 3
    assert unknown.issues[0].code == "RSS_UNAVAILABLE"


def test_linux_identity_reuse_during_memory_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, root = _linux_fixture(tmp_path)
    monkeypatch.setattr(
        resources, "_linux_stat", Mock(side_effect=[("S", 900, 300), ("S", 901, 301)])
    )
    with pytest.raises(resources.ResourceObservationError, match="PROCESS_IDENTITY_CHANGED"):
        resources._read_linux(pid, proc_root=root, clock_ticks=100, page_size=4096)


def test_linux_rss_permission_denial_retains_cpu_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, root = _linux_fixture(tmp_path)
    original = Path.read_text

    def read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name == "statm":
            raise PermissionError(errno.EACCES, "synthetic denial")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    sample = resources._read_linux(pid, proc_root=root, clock_ticks=100, page_size=4096)
    assert sample.rss_bytes is None and sample.cpu_seconds == 3
    assert sample.issues == (resources.ResourceIssue("RSS_PERMISSION_DENIED", errno.EACCES),)


def test_windows_failure_closes_query_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = SimpleNamespace(
        OpenProcess=Mock(return_value=123),
        CloseHandle=Mock(return_value=1),
        GetProcessTimes=Mock(return_value=0),
    )
    psapi = SimpleNamespace(GetProcessMemoryInfo=Mock(return_value=0))
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda name, **_kw: kernel if name == "kernel32" else psapi, raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    with pytest.raises(resources.ResourceObservationError, match="PROCESS_PERMISSION_DENIED"):
        resources._read_windows(42)
    kernel.CloseHandle.assert_called_once_with(123)
    psapi.GetProcessMemoryInfo.assert_not_called()


def test_windows_rss_permission_denial_is_unknown_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def times(_handle: int, created: Any, _exited: Any, system: Any, user: Any) -> int:
        ctypes.cast(created, ctypes.POINTER(resources._FileTime)).contents.low = 100
        ctypes.cast(system, ctypes.POINTER(resources._FileTime)).contents.low = 10_000_000
        ctypes.cast(user, ctypes.POINTER(resources._FileTime)).contents.low = 20_000_000
        return 1

    kernel = SimpleNamespace(
        OpenProcess=Mock(return_value=123),
        CloseHandle=Mock(return_value=1),
        GetProcessTimes=Mock(side_effect=times),
    )
    psapi = SimpleNamespace(GetProcessMemoryInfo=Mock(return_value=0))
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda name, **_kw: kernel if name == "kernel32" else psapi, raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    sample = resources._read_windows(42)
    assert sample.rss_bytes is None and sample.cpu_seconds == 3
    assert sample.issues == (resources.ResourceIssue("RSS_PERMISSION_DENIED", 5),)
    kernel.CloseHandle.assert_called_once_with(123)


def test_stop_before_start_records_no_observations_and_duplicate_pids_are_rejected() -> None:
    identity = resources.register_process()
    sampler = resources.ProcessResourceSampler([identity], duration_seconds=1, interval_seconds=0.1)
    report = sampler.stop()
    assert report["expected_slot_count"] == 0
    assert report["aggregate"]["rss_coverage"] is None
    assert sampler.stop() == report
    with pytest.raises(RuntimeError, match="cannot be started"):
        sampler.start()
    with pytest.raises(ValueError, match="distinct"):
        resources.ProcessResourceSampler(
            [identity, identity], duration_seconds=1, interval_seconds=0.1
        )
