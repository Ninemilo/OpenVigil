"""Owned real-HTTP software measurement lifecycle; no existing services are used."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess  # nosec B404 - fixed read-only Git commands; no shell or user arguments.
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from windops_backend.operations.load_contracts import (
    LoadProfile,
    ResourceObservation,
    RunIdentity,
    evaluate_workload,
    profile_sha256,
    summarize_samples,
)
from windops_backend.operations.load_dataset import (
    USER_SCOPES,
    HistoryOracle,
    canonical_hash,
    database_snapshot,
    seed_dataset,
    validate_turbines,
)
from windops_backend.operations.load_resources import (
    ProcessIdentity,
    ProcessResourceSampler,
    read_process,
    register_process,
)
from windops_backend.operations.load_runner import AllowedGet, PrivateUser, run_load
from windops_backend.operations.load_services_processes import OwnedRoleProcesses, TrustedLaunch

ROOT = Path(__file__).resolve().parents[4]
RUN_ROOT = ROOT / ".artifacts" / "performance-e2e"


def validate_run_directory(path: Path, run_id: str) -> None:
    if re.fullmatch(r"[0-9a-f]{32}", run_id) is None or path != RUN_ROOT / run_id:
        raise ValueError("run directory must be an owned workspace path")
    for candidate in (ROOT, RUN_ROOT.parent, RUN_ROOT, path):
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("linked run directories are forbidden")
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("run directory escapes workspace")


def _hash_file(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or path.is_junction():
        raise ValueError("linked evidence input is forbidden")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protection_snapshot() -> dict[str, Any]:
    git_binary = shutil.which("git")
    if git_binary is None:
        raise RuntimeError("Git executable unavailable for state protection")

    def git(*args: str) -> str:
        # All three callers below use fixed read-only arguments, not CLI input.
        return subprocess.check_output(  # nosec B603 - resolved executable and fixed argv, no shell.
            [git_binary, *args], cwd=ROOT, text=True, encoding="utf-8", timeout=10
        ).strip()

    index = Path(git("rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = ROOT / index
    return {
        "head": git("rev-parse", "HEAD"),
        "index_sha256": _hash_file(index),
        "env_sha256": _hash_file(ROOT / ".env"),
    }


def source_snapshot() -> dict[str, Any]:
    paths = sorted(
        {
            *list((ROOT / "backend" / "src").rglob("*.py")),
            ROOT / "backend" / "scripts" / "run_performance_e2e.py",
            ROOT / "backend" / "pyproject.toml",
            ROOT / "backend" / "uv.lock",
        }
    )
    hashes = {path.relative_to(ROOT).as_posix(): _hash_file(path) for path in paths}
    return {"sha256": canonical_hash(hashes), "files": hashes}


async def _wait_ready(
    processes: OwnedRoleProcesses, directory: Path, run_id: str
) -> dict[str, Any]:
    async with asyncio.timeout(45):
        while processes.info("api").role_exit_code is None:
            path = directory / "ready.json"
            if path.exists():
                try:
                    ready = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    await asyncio.sleep(0.05)
                    continue
                if (
                    ready.get("run_id") != run_id
                    or type(ready.get("port")) is not int
                    or not 1 <= ready["port"] <= 65535
                ):
                    raise RuntimeError("invalid owned server readiness")
                if not isinstance(ready.get("runtime"), dict):
                    raise RuntimeError("owned server runtime identity missing")
                _verified_api_members(processes, ready.get("process"))
                return dict(ready)
            await asyncio.sleep(0.05)
    raise RuntimeError("owned server did not start")


def _verified_api_members(
    processes: OwnedRoleProcesses, actual: object
) -> tuple[ProcessIdentity, ...]:
    """A readiness PID is evidence only after kernel-owned membership verification."""
    if (
        not isinstance(actual, dict)
        or type(actual.get("pid")) is not int
        or not isinstance(actual.get("start_identity"), str)
    ):
        raise RuntimeError("owned API interpreter identity missing")
    members = processes.members("api")
    if not any(
        member.pid == actual["pid"] and member.start_identity == actual["start_identity"]
        for member in members
    ):
        raise RuntimeError("owned API interpreter identity mismatch")
    info = processes.info("api")
    return tuple(
        replace(
            member,
            label=(
                "api_interpreter"
                if member.pid == actual["pid"]
                else "api_guardian"
                if member.pid == info.pid
                else "api_launcher"
                if member.pid == info.role_pid
                else "api_descendant"
            ),
        )
        for member in members
    )


async def _stop_server(processes: OwnedRoleProcesses | None, port: int | None) -> dict[str, Any]:
    receipts = (
        await processes.cleanup(grace_seconds=20, kill_seconds=5) if processes is not None else ()
    )
    forced = any(receipt.forced for receipt in receipts)
    released = True
    if port is not None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            released = probe.connect_ex(("127.0.0.1", port)) != 0
    exit_code = receipts[0].role_exit_code if receipts else None
    return {
        "passed": released and all(receipt.passed for receipt in receipts) and not forced,
        "server_exit_code": exit_code,
        "forced_stop": forced,
        "port_released": released,
        "database_retained_as_evidence": True,
        "descendants_empty": all(receipt.descendants_empty for receipt in receipts),
        "process_receipts": [asdict(receipt) for receipt in receipts],
        "process_events": list(processes.events) if processes is not None else [],
    }


async def _authorization_controls(origin: str, keys: dict[str, str]) -> dict[str, Any]:
    checks: list[bool] = []
    async with httpx.AsyncClient(
        base_url=origin, trust_env=False, timeout=5, follow_redirects=False
    ) as client:
        response = await client.get("/api/v1/turbines")
        checks.append(response.status_code == 401)
        # Test-only headers cannot grant a principal in the actual API process.
        response = await client.get(
            "/api/v1/turbines",
            headers={"X-WindOps-Test-Role": "operations_manager", "X-WindOps-Test-Principal": "b1"},
        )
        checks.append(response.status_code == 401)
        for user, key in keys.items():
            headers = {"Authorization": f"Bearer {key}"}
            response = await client.get("/api/v1/turbines", headers=headers)
            checks.append(response.status_code == 200 and validate_turbines(response.json(), user))
            response = await client.get(
                "/api/v1/scada/history", headers=headers, params={"turbine_id": "turbine-c"}
            )
            checks.append(response.status_code == 404)
    if not all(checks):
        raise RuntimeError("software authorization controls failed")
    return {
        "passed": True,
        "checks": len(checks),
        "distinct_subjects": 4,
        "disjoint_allowed_scopes": 2,
        "denied_scope": True,
        "test_header_bypass": False,
    }


async def _finish_server_cleanup(
    processes: OwnedRoleProcesses | None, port: int | None, failures: list[str]
) -> dict[str, Any]:
    """Cancellation must not interrupt the owned server's bounded shutdown."""
    cleanup = asyncio.create_task(_stop_server(processes, port))
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            if "software_run_cancelled" not in failures:
                failures.append("software_run_cancelled")
    return cleanup.result()


def _resource_observation(
    raw: dict[str, Any], *, membership_verified: bool = True
) -> ResourceObservation:
    aggregate = raw["aggregate"]
    processes = raw["processes"]
    peaks = [
        item["observed_peak_rss_bytes"]
        for item in processes
        if item["observed_peak_rss_bytes"] is not None
    ]
    expected = aggregate["expected_observations"]
    observed = min(aggregate["rss_valid_observations"], aggregate["cpu_valid_observations"])
    # The sum of individual observed peaks is a conservative envelope, not a
    # simultaneous peak and never an assertion about between-sample maxima.
    available = (
        membership_verified
        and bool(peaks)
        and len(peaks) == len(processes)
        and observed > 0
        and raw["fatal_error"] is None
    )
    return ResourceObservation(
        available=available,
        expected_samples=expected,
        observed_samples=observed,
        peak_process_rss_bytes=max(peaks) if available else None,
        peak_total_rss_bytes=sum(peaks) if available else None,
    )


async def _guard_resources(
    identities: list[ProcessIdentity],
    profile: LoadProfile,
    cancel: asyncio.Event,
    failures: list[str],
    processes: OwnedRoleProcesses,
) -> None:
    process_limit = min(item.maximum_process_rss_bytes for item in profile.budgets)
    total_limit = min(item.maximum_total_rss_bytes for item in profile.budgets)
    while True:
        try:
            expected = {(item.pid, item.start_identity) for item in identities[1:]}
            current = {(item.pid, item.start_identity) for item in processes.members("api")}
            if current != expected:
                failures.append("resource_process_tree_changed")
                cancel.set()
                return
            observations = [read_process(identity) for identity in identities]
            rss = [item.rss_bytes for item in observations if item.rss_bytes is not None]
            if len(rss) != len(identities):
                failures.append("resource_guard_unavailable")
                cancel.set()
                return
            if max(rss) > process_limit or sum(rss) > total_limit:
                failures.append("resource_limit_exceeded")
                cancel.set()
                return
        except Exception:
            failures.append("resource_guard_unavailable")
            cancel.set()
            return
        await asyncio.sleep(0.25)


async def execute_software(profile: LoadProfile) -> tuple[Path, dict[str, Any]]:
    if profile.mode != "software" or set(profile.workloads) - {"queries", "history"}:
        raise ValueError(
            "software mode measures queries/history only; services require their own runtime"
        )
    run_id = uuid4().hex
    directory = RUN_ROOT / run_id
    validate_run_directory(directory, run_id)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    directory.mkdir(exist_ok=False)
    database = directory / "software.sqlite3"
    protection = protection_snapshot()
    sources = source_snapshot()
    processes: OwnedRoleProcesses | None = None
    sampler: ProcessResourceSampler | None = None
    port: int | None = None
    report: dict[str, Any] = {
        "schema_version": "openvigil.load-software-report.v1",
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "failed",
        "qualified": False,
        "execution_mode": "software",
        "transport": "loopback_http",
        "profile": profile.model_dump(mode="json"),
        "source_before": sources,
        "protection_before": protection,
        "external_services_verified": False,
        "execution_plan": {
            "planned": len(profile.workloads) * profile.requests_per_route
            + profile.warmup_requests,
            "started": 0,
            "not_run": len(profile.workloads) * profile.requests_per_route
            + profile.warmup_requests,
        },
        "resource_window": "warmup_and_measured_requests_only",
        "boundaries": [
            "synthetic_sqlite_dataset",
            "single_uvicorn_worker",
            "database_read_audit_sink",
            "no_gateway_or_tls",
            "rate_limiter_disabled",
            "no_model_calls",
            "no_document_worker",
            "no_production_capacity_claim",
        ],
        "failure_codes": [],
    }
    keys = {user: secrets.token_hex(32) for user in USER_SCOPES}
    try:
        async with asyncio.timeout(120):
            dataset = await seed_dataset(database, max(90, profile.history_rows))
        report["dataset"] = dataset
        oracle = HistoryOracle(dataset["history_rows"])
        environment = {
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "host_sha256": canonical_hash(platform.node()),
            "processor": platform.processor(),
            "workers": 1,
            "tls": False,
            "audit": "database",
            "rate_limiter": False,
        }
        packages = {
            dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()
        }
        report.update(
            environment=environment,
            runtime_packages=packages,
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("WINDOPS_", "OTEL_"))
        }
        env["PYTHONPATH"] = str(ROOT / "backend" / "src")
        env["PYTHONUTF8"] = "1"
        env["OPENVIGIL_SOFTWARE_PRIVATE_STARTUP"] = json.dumps(keys)
        launch = TrustedLaunch(
            argv=(
                sys.executable,
                "-m",
                "windops_backend.operations.load_software_server",
                "--run-directory",
                str(directory),
                "--run-id",
                run_id,
            ),
            cwd=ROOT,
            env=env,
            private_log=directory / "server.log",
        )
        processes = OwnedRoleProcesses(lambda _role, _generation: launch, workspace=ROOT)
        await processes.start("api")
        ready = await _wait_ready(processes, directory, run_id)
        port = ready["port"]
        report["runtime_configuration"] = ready["runtime"]
        identity = RunIdentity(
            profile_sha256=profile_sha256(profile),
            dataset_sha256=canonical_hash(dataset),
            runtime_sha256=canonical_hash({"packages": packages, "runtime": ready["runtime"]}),
            environment_sha256=canonical_hash(environment),
            cache_policy="fresh_db_sequential_warmup_v1",
            source_sha256=sources["sha256"],
        )
        report["identity"] = identity.model_dump(mode="json")
        origin = f"http://127.0.0.1:{port}"
        report["authorization_controls"] = await _authorization_controls(origin, keys)
        report["database_before"] = database_snapshot(database)
        users = [
            PrivateUser(user, {"Authorization": f"Bearer {key}"}) for user, key in keys.items()
        ]
        routes = []
        if "queries" in profile.workloads:
            routes.append(
                AllowedGet(
                    "queries",
                    "scoped_turbines",
                    "/api/v1/turbines",
                    query={"limit": "50"},
                    validator=validate_turbines,
                )
            )
        if "history" in profile.workloads:
            routes.append(
                AllowedGet(
                    "history",
                    "long_history",
                    "/api/v1/scada/history",
                    query={},
                    validator=oracle.validate,
                    query_by_user=oracle.queries,
                )
            )
        members = _verified_api_members(processes, ready["process"])
        identities = [register_process(label="load_client"), *members]
        report["process_tree"] = {
            "containment": processes.info("api").containment,
            "actual_api": ready["process"],
            "members": [asdict(member) for member in members],
            "membership_policy": "fixed_members_checked_every_250ms_and_at_end",
            "resource_scope": "load_client_and_all_owned_api_tree_members",
            "membership_verified": False,
        }
        sampler = ProcessResourceSampler(
            identities,
            duration_seconds=profile.maximum_duration_seconds + 5,
            interval_seconds=0.25,
        ).start()
        cancel = asyncio.Event()
        guard = asyncio.create_task(
            _guard_resources(identities, profile, cancel, report["failure_codes"], processes)
        )
        try:
            result = await run_load(
                origin=origin, users=users, routes=routes, profile=profile, cancel_event=cancel
            )
        finally:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
        report["samples"] = [sample.model_dump(mode="json") for sample in result.samples]
        report["execution_plan"] = {
            "planned": result.planned_requests,
            "started": sum(sample.started for sample in result.samples),
            "not_run": sum(sample.outcome == "not_run" for sample in result.samples),
        }
        report["scheduler"] = {
            "elapsed_seconds": result.elapsed_seconds,
            "stop_reason": result.stop_reason,
            "cleanup_passed": result.cleanup_passed,
            "max_observed_inflight": result.max_observed_inflight,
            "planned_requests": result.planned_requests,
        }
        expected_members = {(member.pid, member.start_identity) for member in members}
        current_members = {
            (member.pid, member.start_identity) for member in processes.members("api")
        }
        if current_members != expected_members:
            report["failure_codes"].append("resource_process_tree_changed")
        report["process_tree"]["membership_verified"] = not any(
            code in {"resource_process_tree_changed", "resource_guard_unavailable"}
            for code in report["failure_codes"]
        )
        raw_resources = sampler.stop()
        sampler = None
        report["resources"] = raw_resources
        resources = _resource_observation(
            raw_resources, membership_verified=report["process_tree"]["membership_verified"]
        )
        report["resource_budget_observation"] = resources.model_dump(mode="json")
        report["summaries"] = {}
        report["budget_verdicts"] = {}
        for route in routes:
            selected = [sample for sample in result.samples if sample.route_id == route.route_id]
            summary = summarize_samples(selected, planned=profile.requests_per_route)
            report["summaries"][route.route_id] = summary.model_dump(mode="json")
            budget = next(item for item in profile.budgets if item.workload == route.workload)
            report["budget_verdicts"][route.route_id] = evaluate_workload(
                summary, budget, resources
            ).model_dump(mode="json")
        report["summaries_by_user"] = {
            f"{route.route_id}:{user}": summarize_samples(
                [
                    sample
                    for sample in result.samples
                    if sample.route_id == route.route_id and sample.user_id == user
                ],
                planned=sum(
                    sample.phase == "measured"
                    for sample in result.samples
                    if sample.route_id == route.route_id and sample.user_id == user
                ),
            ).model_dump(mode="json")
            for route in routes
            for user in USER_SCOPES
        }
        report["database_after"] = database_snapshot(database)
        before, after = report["database_before"], report["database_after"]
        if before["business_tables"] != after["business_tables"]:
            report["failure_codes"].append("business_data_changed")
        expected_audits = sum(sample.outcome == "success" for sample in result.samples)
        if after["read_audit_count"] - before["read_audit_count"] < expected_audits:
            report["failure_codes"].append("read_audit_missing")
        if not result.cleanup_passed or result.stop_reason is not None:
            report["failure_codes"].append("scheduler_incomplete")
        if any(sample.outcome != "success" for sample in result.samples):
            report["failure_codes"].append("request_not_successful")
    except asyncio.CancelledError:
        report["failure_codes"].append("software_run_cancelled")
    except Exception as exc:
        report["failure_codes"].append("software_run_failed")
        report["failure_type"] = type(exc).__name__
    finally:
        if sampler is not None:
            try:
                report["resources"] = sampler.stop()
            except Exception:
                report["failure_codes"].append("resource_cleanup_failed")
        try:
            report["cleanup"] = await _finish_server_cleanup(
                processes, port, report["failure_codes"]
            )
        except Exception:
            report["cleanup"] = {"passed": False, "code": "server_cleanup_failed"}
        for name, snapshot, expected in (
            ("source", source_snapshot, sources),
            ("protection", protection_snapshot, protection),
        ):
            try:
                report[f"{name}_after"] = snapshot()
                if report[f"{name}_after"] != expected:
                    report["failure_codes"].append(f"{name}_changed")
            except Exception:
                report["failure_codes"].append(f"{name}_verification_failed")
        if not report["cleanup"]["passed"]:
            report["failure_codes"].append("cleanup_failed")
        verdicts = list(report.get("budget_verdicts", {}).values())
        if not report["failure_codes"] and verdicts:
            if any(item["status"] in {"failed", "insufficient_evidence"} for item in verdicts):
                report["status"] = "budget_unqualified"
            else:
                report["status"] = (
                    "measurement_only"
                    if any(item["status"] == "measurement_only" for item in verdicts)
                    else "budget_qualified"
                )
        report["qualified"] = report["status"] == "budget_qualified" and all(
            item["qualified"] for item in verdicts
        )
        report["finished_at"] = datetime.now(UTC).isoformat()
        # Return precisely the persisted wire representation, including resource
        # issue tuples, so callers can verify the saved report without coercion.
        report = json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
        with (directory / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
    return directory / "report.json", report
