"""Real SQLite/ASGI/loopback coverage for the isolated software measurement mode.

These tests neither use production services nor qualify production capacity. The
one injected failure affects resource-report delivery after actual OS sampling;
the API, scheduler, authorization and child cleanup remain real.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from windops_backend.main import create_app
from windops_backend.operations import load_software
from windops_backend.operations.load_contracts import parse_load_profile
from windops_backend.operations.load_dataset import (
    KEY_FIELDS,
    SUBJECTS,
    USER_SCOPES,
    HistoryOracle,
    database_snapshot,
    seed_dataset,
    validate_turbines,
)
from windops_backend.operations.load_resources import (
    ProcessIdentity,
    ProcessResourceSampler,
    ResourceObservationError,
    read_process,
    register_process,
)
from windops_backend.operations.load_services_processes import OwnedRoleProcesses, TrustedLaunch
from windops_backend.operations.load_software_server import software_settings

ROOT = Path(__file__).resolve().parents[2]
KEYS = {user: f"{index:064x}" for index, user in enumerate(USER_SCOPES, start=1)}

_RESOURCE_CHILD = """
import json, os, pathlib, subprocess, sys, threading, time
from windops_backend.operations.load_resources import register_process
root = pathlib.Path(sys.argv[1])
identity = register_process()
(root / 'identity.json').write_text(json.dumps(dict(
    pid=identity.pid, start_identity=identity.start_identity)))
stop = threading.Event()
threading.Thread(target=lambda: (sys.stdin.readline(), stop.set()), daemon=True).start()
memory = None
child = None
while not stop.is_set():
    if memory is None and (root / 'allocate').exists():
        memory = bytearray(64 * 1024 * 1024)
        for offset in range(0, len(memory), 4096):
            memory[offset] = 1
        until = time.process_time() + .3
        value = 1
        while time.process_time() < until:
            value = (value * 17 + 3) % 1_000_003
        (root / 'allocated').write_text(str(value))
    if child is None and (root / 'spawn').exists():
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.readline()'],
            stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        (root / 'spawned').write_text(str(child.pid))
    time.sleep(.005)
if child is not None:
    child.stdin.write(b'stop\\n')
    child.stdin.flush()
    child.stdin.close()
    child.wait(timeout=5)
"""


def _resource_child_owner(root: Path) -> OwnedRoleProcesses:
    script = root / "owned-resource-child.py"
    script.write_text(_RESOURCE_CHILD, encoding="utf-8")
    launch = TrustedLaunch(
        argv=(sys.executable, str(script), str(root)),
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend/src"), "PYTHONUTF8": "1"},
        private_log=root / "private.log",
    )
    return OwnedRoleProcesses(lambda _role, _generation: launch, workspace=root)


async def _wait_file(path: Path) -> None:
    async with asyncio.timeout(10):
        for _ in range(1000):
            if await asyncio.to_thread(path.exists):
                return
            await asyncio.sleep(0.01)
    raise AssertionError("owned child did not publish its marker")


@pytest.mark.asyncio
async def test_real_api_tree_records_launcher_interpreter_memory_cpu_and_rejects_wrong_identity(
    tmp_path: Path,
) -> None:
    owner = _resource_child_owner(tmp_path)
    sampler = None
    members: tuple[ProcessIdentity, ...] = ()
    try:
        info = await owner.start("api")
        await _wait_file(tmp_path / "identity.json")
        actual = json.loads((tmp_path / "identity.json").read_bytes())
        members = load_software._verified_api_members(owner, actual)
        interpreter = next(member for member in members if member.pid == actual["pid"])
        assert interpreter.label == "api_interpreter"
        assert {info.pid, info.role_pid, interpreter.pid} <= {member.pid for member in members}
        if os.name == "nt":
            assert interpreter.pid != info.role_pid  # The locked venv's real redirector.
            assert {member.label for member in members} >= {
                "api_guardian",
                "api_launcher",
                "api_interpreter",
            }
        # Current unrelated test process is alive, but not owned by the API Job.
        unrelated = register_process()
        with pytest.raises(RuntimeError, match="identity mismatch"):
            load_software._verified_api_members(
                owner,
                {
                    "pid": unrelated.pid,
                    "start_identity": unrelated.start_identity,
                },
            )
        with pytest.raises(RuntimeError, match="identity mismatch"):
            load_software._verified_api_members(owner, {**actual, "start_identity": "changed"})
        baseline = read_process(interpreter)
        assert baseline.rss_bytes is not None
        sampler = ProcessResourceSampler(members, duration_seconds=8, interval_seconds=0.05).start()
        await asyncio.sleep(0.1)
        (tmp_path / "allocate").touch()
        await _wait_file(tmp_path / "allocated")
        await asyncio.sleep(0.15)
        measured = sampler.stop()
        actual_samples = next(
            entry for entry in measured["processes"] if entry["identity"]["pid"] == interpreter.pid
        )
        assert actual_samples["observed_peak_rss_bytes"] - baseline.rss_bytes >= 48 * 1024 * 1024
        assert actual_samples["cpu_observed_delta_seconds"] >= 0.2
        assert {entry["identity"]["pid"] for entry in measured["processes"]} == {
            member.pid for member in members
        }
        (tmp_path / "actual-resource-evidence.json").write_text(
            json.dumps(measured), encoding="utf-8"
        )
    finally:
        if sampler is not None:
            sampler.stop()
        cleanup = await load_software._stop_server(owner, None)
    assert cleanup["passed"] and cleanup["descendants_empty"]
    assert owner.members() == ()
    for identity in members:
        with pytest.raises(ResourceObservationError):
            read_process(identity)
    assert read_process(unrelated).pid == os.getpid()


@pytest.mark.asyncio
async def test_real_new_descendant_cancels_fixed_resource_plan_and_entire_tree_is_cleaned(
    tmp_path: Path,
) -> None:
    owner = _resource_child_owner(tmp_path)
    members: tuple[ProcessIdentity, ...] = ()
    guard = None
    try:
        await owner.start("api")
        await _wait_file(tmp_path / "identity.json")
        actual = json.loads((tmp_path / "identity.json").read_bytes())
        original = load_software._verified_api_members(owner, actual)
        identities = [register_process(label="load_client"), *original]
        cancel, failures = asyncio.Event(), []
        guard = asyncio.create_task(
            load_software._guard_resources(
                identities,
                parse_load_profile(_profile_bytes()),
                cancel,
                failures,
                owner,
            )
        )
        await asyncio.sleep(0.05)
        assert not cancel.is_set()
        (tmp_path / "spawn").touch()
        await _wait_file(tmp_path / "spawned")
        async with asyncio.timeout(5):
            await cancel.wait()
        await guard
        members = owner.members("api")
        assert len(members) > len(original)
        assert failures == ["resource_process_tree_changed"]
        unavailable = load_software._resource_observation(
            {
                "aggregate": {
                    "expected_observations": 2,
                    "rss_valid_observations": 2,
                    "cpu_valid_observations": 2,
                },
                "processes": [{"observed_peak_rss_bytes": 100}],
                "fatal_error": None,
            },
            membership_verified=False,
        )
        assert unavailable.available is False
        assert unavailable.peak_total_rss_bytes is None
    finally:
        if guard is not None:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
        cleanup = await load_software._stop_server(owner, None)
    assert cleanup["passed"] and cleanup["descendants_empty"]
    for identity in members:
        with pytest.raises(ResourceObservationError):
            read_process(identity)


def _profile_bytes() -> bytes:
    raw = json.loads(
        (Path(__file__).parent / "fixtures/performance/software-small.json").read_bytes()
    )
    raw["profile_id"] = "software-lifecycle-test-v1"
    raw["history_rows"] = 1809
    # Keep the real 100 measured requests per route and all four warmups. These
    # are measurement-only test limits, not the product's capacity thresholds.
    for budget in raw["budgets"]:
        budget["p95_ms"] = 15_000.0
        budget["p99_ms"] = 20_000.0
    return json.dumps(raw).encode()


@pytest.mark.asyncio
async def test_seed_real_sqlite_relations_counts_and_exclusive_creation(tmp_path: Path) -> None:
    database = tmp_path / "seed.sqlite3"
    report = await seed_dataset(database, 1809)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, expected in (
            ("tenants", 3),
            ("wind_farms", 3),
            ("turbines", 6),
            ("ingest_sources", 3),
            ("ingest_receipts", 1809),
            ("scada_samples", 1809),
        ):
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (expected,)
        assert connection.execute(
            "SELECT turbine_id, variable, value, unit, source_sequence, quality, is_late "
            "FROM scada_samples WHERE id='sample-0000001'"
        ).fetchone() == ("turbine-b", "active_power", 11.0, "MW", 0, "uncertain", 1)
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE scada_samples SET turbine_id='missing' WHERE id=?", ("sample-0000001",)
            )
    assert report["history_rows"] == 1809
    assert report["foreign_keys_verified"] is True
    assert report["classification"] == "synthetic"
    assert len(report["input_sha256"]) == 64
    before = database.read_bytes()
    with pytest.raises(FileExistsError):
        await seed_dataset(database, 1809)
    assert database.read_bytes() == before
    second = await seed_dataset(tmp_path / "second.sqlite3", 1809)
    assert second["input_sha256"] == report["input_sha256"]


@pytest.mark.asyncio
async def test_seed_rejects_out_of_bounds_before_creation_and_preserves_existing_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "not-a-database.sqlite3"
    for rows in (0, 89, 1_000_001):
        with pytest.raises(ValueError):
            await seed_dataset(database, rows)
        assert not database.exists()
    database.write_bytes(b"existing evidence must not be replaced")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        await seed_dataset(database, 90)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_server_settings_ignore_environment_and_define_four_scoped_subjects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = {
        "WINDOPS_DATABASE_URL": "postgresql+asyncpg://forbidden.invalid/untouched",
        "WINDOPS_AGENT_MODE": "reasoning",
        "WINDOPS_AUTH_MODE": "disabled",
        "WINDOPS_TEST_AUTH_BYPASS_ENABLED": "true",
        "WINDOPS_DEMO_SEED": "true",
        "WINDOPS_SCHEMA_BOOTSTRAP": "true",
        "WINDOPS_KNOWLEDGE_GRAPH_BACKEND": "neo4j",
        "WINDOPS_RATE_LIMIT_ENABLED": "true",
        "WINDOPS_OTEL_ENABLED": "true",
        "WINDOPS_OPERATIONS_APPROVER_API_KEY": "environment-must-not-authenticate",
        "WINDOPS_IDENTITY_SCOPE_MAPPINGS": "not even valid JSON",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    database = tmp_path / "isolated.sqlite3"
    settings = software_settings(database, KEYS)
    assert settings.database_url == f"sqlite+aiosqlite:///{database.as_posix()}"
    assert settings.agent_mode == "deterministic"
    assert settings.auth_mode == "static_tokens"
    assert settings.knowledge_graph_backend == "memory"
    for field in (
        "demo_seed",
        "schema_bootstrap",
        "test_auth_bypass_enabled",
        "outbox_inline_drain",
        "rate_limit_enabled",
        "otel_enabled",
        "docs_enabled",
    ):
        assert getattr(settings, field) is False, field
    assert set(settings.identity_scope_mappings) == set(SUBJECTS.values())
    for user, scope in USER_SCOPES.items():
        mapping = settings.identity_scope_mappings[SUBJECTS[user]].model_dump(mode="json")
        assert mapping["tenant_ids"] == [f"tenant-{scope}"]
        assert mapping["wind_farm_ids"] == [f"farm-{scope}"]
        assert mapping["turbine_ids"] == [f"turbine-{scope}", f"turbine-{scope}-extra"]
        assert mapping["data_scopes"] == ["asset", "telemetry"]
        key = getattr(settings, KEY_FIELDS[user])
        assert (key.get_secret_value() if hasattr(key, "get_secret_value") else key) == KEYS[user]


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {user: "a" * 64 for user in USER_SCOPES},
        {**KEYS, "a1": "too-short"},
        {**KEYS, "unexpected": "b" * 64},
    ],
)
def test_server_rejects_incomplete_reused_or_invalid_credentials(
    tmp_path: Path, keys: dict
) -> None:
    with pytest.raises(ValueError, match="four distinct"):
        software_settings(tmp_path / "unused.sqlite3", keys)


@pytest_asyncio.fixture
async def software_api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Path]]:
    database = tmp_path / "real-api.sqlite3"
    await seed_dataset(database, 1809)
    app = create_app(software_settings(database, KEYS))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            trust_env=False,
        ) as client:
            yield client, database


@pytest.mark.asyncio
async def test_real_api_four_subjects_scope_denials_full_history_and_only_read_audits(
    software_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, database = software_api
    before = database_snapshot(database)
    oracle = HistoryOracle(1809)
    assert oracle.interval == 121
    for user, scope in USER_SCOPES.items():
        headers = {"Authorization": f"Bearer {KEYS[user]}"}
        response = await client.get("/api/v1/turbines", headers=headers, params={"limit": "50"})
        assert response.status_code == 200
        assert validate_turbines(response.json(), user)
        assert not validate_turbines(response.json(), "b1" if scope == "a" else "a1")
        response = await client.get(
            "/api/v1/scada/history", headers=headers, params=oracle.queries[user]
        )
        assert response.status_code == 200
        value = response.json()
        assert oracle.validate(value, user)
        active = next(item for item in value["data"] if item["variable"] == "active_power")
        assert active["points"][0]["value"] == pytest.approx(1.1 if scope == "a" else 11.1)
        assert active["points"][0]["late"] is True
        assert active["points"][0]["quality"] == "uncertain"
        for denied in ("c", "b" if scope == "a" else "a"):
            response = await client.get(
                "/api/v1/scada/history",
                headers=headers,
                params={**oracle.queries[user], "turbine_id": f"turbine-{denied}"},
            )
            assert response.status_code == 404
            assert not oracle.validate(response.json(), user)
    assert (await client.get("/api/v1/turbines")).status_code == 401
    assert (
        await client.get(
            "/api/v1/turbines",
            headers={
                "X-WindOps-Test-Principal": "integration-test-system",
                "X-WindOps-Test-Subject": "operations-approver",
                "X-WindOps-Test-Role": "test_system",
            },
        )
    ).status_code == 401
    after = database_snapshot(database)
    assert after["business_tables"] == before["business_tables"]
    # Authenticated denied reads are audited as well: two allowed plus two
    # denied GETs per subject. Anonymous/test-header-only attempts add no rows.
    assert after["read_audit_count"] - before["read_audit_count"] == 16
    assert after["audit_by_subject"] == {subject: 4 for subject in SUBJECTS.values()}
    with sqlite3.connect(database) as connection:
        audits = connection.execute("SELECT method, query FROM read_access_audits").fetchall()
    for method, query in audits:
        assert method == "GET"
        authorization = json.loads(query)["authorization"]
        assert authorization["allow_global"] is False
        assert authorization["unrestricted"] is False
        assert authorization["turbine_count"] == 2


@pytest.mark.asyncio
async def test_history_oracle_rejects_complete_response_corruption_and_real_database_change(
    software_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, database = software_api
    oracle = HistoryOracle(1809)
    headers = {"Authorization": f"Bearer {KEYS['a1']}"}
    response = await client.get(
        "/api/v1/scada/history", headers=headers, params=oracle.queries["a1"]
    )
    assert response.status_code == 200
    original = response.json()
    assert oracle.validate(original, "a1")
    cases = [
        (("data", 0, "points", 0, "value"), original["data"][0]["points"][0]["value"] + 0.01),
        (("data", 0, "points", 0, "value"), True),
        (("data", 0, "points", 0, "value"), float("nan")),
        (("data", 0, "points", 0, "value"), float("inf")),
        (("data", 0, "points", 0, "timestamp"), "2026-01-01T00:00:01Z"),
        (("data", 0, "points", 0, "late"), False),
        (("data", 0, "points", 0, "quality"), "good"),
        (("data", 0, "source_id"), "source-b"),
        (("data", 0, "turbine_id"), "turbine-b"),
        (("data", 0, "unit"), "unverified-unit"),
        (("data", 0, "variable"), original["data"][1]["variable"]),
        (("meta", "point_count"), original["meta"]["point_count"] - 1),
        (("meta", "interval_seconds"), 120),
        (("meta", "turbine_id"), "turbine-b"),
        (("data", 0, "points"), original["data"][0]["points"][:-1]),
        (("data", 0, "points"), list(reversed(original["data"][0]["points"]))),
        (("data",), original["data"][:-1]),
    ]
    for path, replacement in cases:
        changed = copy.deepcopy(original)
        target: Any = changed
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = replacement
        assert not oracle.validate(changed, "a1"), path
    before = database_snapshot(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE scada_samples SET value=value+1 WHERE id='sample-0000000'")
    response = await client.get(
        "/api/v1/scada/history", headers=headers, params=oracle.queries["a1"]
    )
    assert response.status_code == 200
    assert not oracle.validate(response.json(), "a1")
    after = database_snapshot(database)
    old, new = (item["business_tables"]["scada_samples"] for item in (before, after))
    assert old["count"] == new["count"] == 1809
    assert old["sha256"] != new["sha256"]
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE tenants SET name='changed same count' WHERE id='tenant-a'")
    assert (
        database_snapshot(database)["business_tables"]["tenants"]["sha256"]
        != after["business_tables"]["tenants"]["sha256"]
    )


def test_owned_directory_rejects_escape_or_another_run(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    run_root = root / ".artifacts/performance-e2e"
    run_root.mkdir(parents=True)
    monkeypatch.setattr(load_software, "ROOT", root)
    monkeypatch.setattr(load_software, "RUN_ROOT", run_root)
    run_id = "a" * 32
    load_software.validate_run_directory(run_root / run_id, run_id)
    for path, identity in (
        (tmp_path / run_id, run_id),
        (run_root / ("b" * 32), run_id),
        (run_root / ".." / run_id, run_id),
        (Path(run_id), run_id),
        (run_root / run_id, "../" + run_id),
        (run_root / run_id, run_id.upper()),
    ):
        with pytest.raises(ValueError):
            load_software.validate_run_directory(path, identity)


def test_owned_directory_rejects_real_junction_or_symlink_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    run_root = root / ".artifacts/performance-e2e"
    run_root.mkdir(parents=True)
    target = tmp_path / "owned-target"
    target.mkdir()
    marker = target / "retained.txt"
    marker.write_text("must survive link cleanup", encoding="utf-8")
    run_id = "c" * 32
    linked = run_root / run_id
    monkeypatch.setattr(load_software, "ROOT", root)
    monkeypatch.setattr(load_software, "RUN_ROOT", run_root)
    if os.name == "nt":
        # Junction creation needs no administrator privilege. Both paths belong
        # to this pytest directory; removing only the link cannot erase target.
        escaped_link = str(linked).replace("'", "''")
        escaped_target = str(target).replace("'", "''")
        command = (
            f"New-Item -ItemType Junction -Path '{escaped_link}' "
            f"-Target '{escaped_target}' -ErrorAction Stop | Out-Null"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
            timeout=15,
        )
        assert linked.is_junction()
    else:
        linked.symlink_to(target, target_is_directory=True)
        assert linked.is_symlink()
    try:
        with pytest.raises(ValueError, match="linked"):
            load_software.validate_run_directory(linked, run_id)
    finally:
        assert linked.parent == run_root
        assert linked.is_symlink() or linked.is_junction()
        if os.name == "nt":
            os.rmdir(linked)
        else:
            linked.unlink()
    assert marker.read_text(encoding="utf-8") == "must survive link cleanup"


@pytest.mark.parametrize(
    "peaks,rss,cpu,fatal",
    [
        ([None, None], 0, 4, None),
        ([1234, None], 2, 4, None),
        ([1234, 2345], 4, 0, None),
        ([1234, 2345], 4, 4, "sampler_failed"),
    ],
)
def test_unknown_resource_evidence_never_becomes_zero_or_available(peaks, rss, cpu, fatal) -> None:
    value = load_software._resource_observation(
        {
            "aggregate": {
                "expected_observations": 4,
                "rss_valid_observations": rss,
                "cpu_valid_observations": cpu,
            },
            "processes": [{"observed_peak_rss_bytes": value} for value in peaks],
            "fatal_error": fatal,
        }
    )
    assert value.available is False
    assert value.peak_process_rss_bytes is None
    assert value.peak_total_rss_bytes is None
    assert value.observed_samples == min(rss, cpu)
    assert value.expected_samples == 4


def test_partial_resource_coverage_remains_partial_and_sum_is_conservative_envelope() -> None:
    value = load_software._resource_observation(
        {
            "aggregate": {
                "expected_observations": 10,
                "rss_valid_observations": 8,
                "cpu_valid_observations": 6,
            },
            "processes": [{"observed_peak_rss_bytes": 100}, {"observed_peak_rss_bytes": 200}],
            "fatal_error": None,
        }
    )
    assert value.available is True
    assert value.expected_samples == 10
    assert value.observed_samples == 6
    assert value.peak_process_rss_bytes == 200
    assert value.peak_total_rss_bytes == 300


def _assert_real_cleanup(path: Path, report: dict[str, Any]) -> None:
    assert {
        key: report["cleanup"][key]
        for key in (
            "passed",
            "server_exit_code",
            "forced_stop",
            "port_released",
            "database_retained_as_evidence",
        )
    } == {
        "passed": True,
        "server_exit_code": 0,
        "forced_stop": False,
        "port_released": True,
        "database_retained_as_evidence": True,
    }
    assert report["cleanup"]["descendants_empty"] is True
    assert len(report["cleanup"]["process_receipts"]) == 1
    receipt = report["cleanup"]["process_receipts"][0]
    assert receipt["identity_verified"] and receipt["descendants_empty"] and receipt["passed"]
    assert receipt["guardian_exit_code"] == receipt["role_exit_code"] == 0
    ready = json.loads((path.parent / "ready.json").read_bytes())
    assert ready["run_id"] == report["run_id"]
    assert ready["process"] == report["process_tree"]["actual_api"]
    for member in report["process_tree"]["members"]:
        with pytest.raises(ResourceObservationError):
            read_process(ProcessIdentity(**member))
    with socket.socket() as probe:
        probe.settimeout(0.3)
        assert probe.connect_ex(("127.0.0.1", ready["port"])) != 0
    assert (path.parent / "software.sqlite3").is_file()
    assert report["source_after"] == report["source_before"]
    assert report["protection_after"] == report["protection_before"]
    assert json.loads(path.read_bytes()) == report


def test_software_cli_real_loopback_100_per_route_measurement_is_not_qualification(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "software-profile.json"
    profile.write_bytes(_profile_bytes())
    env = {**os.environ, "PYTHONPATH": str(ROOT / "backend/src"), "PYTHONUTF8": "1"}
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "backend/scripts/run_performance_e2e.py"),
            "--profile",
            str(profile),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    receipt = json.loads(result.stdout)
    (tmp_path / "cli-receipt.json").write_text(result.stdout, encoding="utf-8")
    assert receipt["status"] == "measurement_only"
    assert receipt["qualified"] is False
    path = Path(receipt["report"])
    assert path.parent.parent == load_software.RUN_ROOT
    report = json.loads(path.read_bytes())
    assert report["failure_codes"] == []
    assert report["execution_mode"] == "software"
    assert report["transport"] == "loopback_http"
    assert report["external_services_verified"] is False
    assert report["qualified"] is False
    runtime = report["runtime_configuration"]
    assert runtime["pool_max_overflow"] == 10
    assert runtime["pool_pre_ping"] is True
    assert runtime["pool_recycle"] == -1
    assert runtime["settings"]["auth_mode"] == "static_tokens"
    assert runtime["settings"]["test_auth_bypass_enabled"] is False
    assert runtime["settings"]["agent_mode"] == "deterministic"
    assert runtime["settings"]["rate_limit_enabled"] is False
    assert not any("api_key" in key for key in runtime["settings"])

    def runtime_hash(configuration: dict[str, Any]) -> str:
        value = {"packages": report["runtime_packages"], "runtime": configuration}
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    assert report["identity"]["runtime_sha256"] == runtime_hash(runtime)
    for field, different_value in (
        ("pool_max_overflow", 11),
        ("pool_pre_ping", False),
        ("pool_recycle", 60),
    ):
        changed = {**runtime, field: different_value}
        assert runtime_hash(changed) != report["identity"]["runtime_sha256"]
    assert report["authorization_controls"] == {
        "passed": True,
        "checks": 10,
        "distinct_subjects": 4,
        "disjoint_allowed_scopes": 2,
        "denied_scope": True,
        "test_header_bypass": False,
    }
    assert len(report["samples"]) == 204
    assert all(item["outcome"] == "success" for item in report["samples"])
    assert {item["user_id"] for item in report["samples"]} == set(USER_SCOPES)
    assert len(report["summaries_by_user"]) == 8
    for summary in report["summaries"].values():
        assert summary["success"] == summary["measured_latencies"] == 100
        assert summary["p99_ms"] is not None
        assert summary["reason_codes"] == []
    for verdict in report["budget_verdicts"].values():
        assert verdict["status"] == "measurement_only"
        assert verdict["qualified"] is False
    before, after = report["database_before"], report["database_after"]
    assert before["business_tables"] == after["business_tables"]
    assert after["read_audit_count"] - before["read_audit_count"] == 204
    assert report["resource_budget_observation"]["available"] is True
    assert report["resources"]["sampler_stopped"] is True
    measured = report["resources"]["processes"]
    tree = report["process_tree"]
    assert tree["membership_verified"] is True
    assert {item["identity"]["pid"] for item in measured} == {
        *[member["pid"] for member in tree["members"]],
        *[
            item["identity"]["pid"]
            for item in measured
            if item["identity"]["label"] == "load_client"
        ],
    }
    actual = next(item for item in measured if item["identity"]["pid"] == tree["actual_api"]["pid"])
    assert actual["identity"]["label"] == "api_interpreter"
    assert actual["observed_peak_rss_bytes"] > 32 * 1024 * 1024
    assert actual["cpu_observed_delta_seconds"] > 0
    assert actual["rss_coverage"] > 0.9
    if os.name == "nt":
        assert {item["identity"]["label"] for item in measured} >= {
            "load_client",
            "api_guardian",
            "api_launcher",
            "api_interpreter",
        }
    _assert_real_cleanup(path, report)


@pytest.mark.asyncio
async def test_real_loopback_resource_stop_failure_keeps_ledger_and_stops_owned_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[ProcessResourceSampler] = []
    real_stop = ProcessResourceSampler.stop

    def stop_then_fail(self: ProcessResourceSampler) -> dict[str, Any]:
        real_stop(self)
        stopped.append(self)
        raise RuntimeError("INJECTED_PRIVATE_RESOURCE_ERROR_MUST_NOT_LEAK")

    monkeypatch.setattr(ProcessResourceSampler, "stop", stop_then_fail)
    profile = parse_load_profile(_profile_bytes())
    async with asyncio.timeout(180):
        path, report = await load_software.execute_software(profile)
    (tmp_path / "failure-receipt.json").write_text(
        json.dumps({"report": str(path), "failure_codes": report["failure_codes"]}),
        encoding="utf-8",
    )
    assert report["status"] == "failed"
    assert report["qualified"] is False
    assert "software_run_failed" in report["failure_codes"]
    assert "resource_cleanup_failed" in report["failure_codes"]
    assert len(report["samples"]) == 204
    assert all(item["outcome"] == "success" for item in report["samples"])
    assert report["scheduler"]["cleanup_passed"] is True
    assert stopped and all(not sampler.is_running for sampler in stopped)
    assert b"INJECTED_PRIVATE_RESOURCE_ERROR_MUST_NOT_LEAK" not in path.read_bytes()
    _assert_real_cleanup(path, report)


@pytest.mark.asyncio
async def test_real_loopback_cancellation_during_child_cleanup_still_waits_and_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = tmp_path / "delayed-owned-server.py"
    bootstrap.write_text(
        "import asyncio\n"
        "from windops_backend.operations import load_software_server as server\n"
        "real_serve = server.serve\n"
        "async def delayed_exit(directory, run_id, keys):\n"
        "    await real_serve(directory, run_id, keys)\n"
        "    (directory / 'cleanup-delay-started').write_text('owned child still alive')\n"
        "    await asyncio.sleep(0.5)\n"
        "server.serve = delayed_exit\n"
        "raise SystemExit(server.main())\n",
        encoding="utf-8",
    )
    managers: list[OwnedRoleProcesses] = []
    directories: list[Path] = []

    def delayed_owner(factory: Any, *, workspace: Path) -> OwnedRoleProcesses:
        def delayed_launch(role: Any, generation: int) -> TrustedLaunch:
            launch = factory(role, generation)
            assert launch.argv[:3] == (
                sys.executable,
                "-m",
                "windops_backend.operations.load_software_server",
            )
            directory = Path(launch.argv[launch.argv.index("--run-directory") + 1])
            assert directory.parent == load_software.RUN_ROOT
            directories.append(directory)
            return replace(launch, argv=(sys.executable, str(bootstrap), *launch.argv[3:]))

        manager = OwnedRoleProcesses(delayed_launch, workspace=workspace)
        managers.append(manager)
        return manager

    monkeypatch.setattr(load_software, "OwnedRoleProcesses", delayed_owner)
    task = asyncio.create_task(load_software.execute_software(parse_load_profile(_profile_bytes())))
    try:
        async with asyncio.timeout(180):
            while not directories or not (directories[0] / "cleanup-delay-started").exists():
                if task.done():
                    pytest.fail(f"lifecycle ended before cleanup cancellation: {await task}")
                await asyncio.sleep(0.01)
            assert len(managers) == 1 and managers[0].info("api").role_exit_code is None
            task.cancel()
            await asyncio.sleep(0.03)
            task.cancel()
            path, report = await task
    finally:
        # Always join the task; the production lifecycle must finish its own
        # child cleanup even when the test assertion/cancellation path fails.
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for manager in managers:
            receipts = await manager.cleanup()
            assert all(receipt.descendants_empty for receipt in receipts)
    (tmp_path / "cancel-receipt.json").write_text(
        json.dumps({"report": str(path), "failure_codes": report["failure_codes"]}),
        encoding="utf-8",
    )
    assert managers[0].info("api").role_exit_code == 0
    assert report["status"] == "failed"
    assert report["qualified"] is False
    assert report["failure_codes"] == ["software_run_cancelled"]
    assert len(report["samples"]) == 204
    assert all(item["outcome"] == "success" for item in report["samples"])
    assert report["resources"]["sampler_stopped"] is True
    _assert_real_cleanup(path, report)
