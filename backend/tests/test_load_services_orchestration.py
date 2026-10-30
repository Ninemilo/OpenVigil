"""Failure boundaries and scheduling contracts; no Docker or OCR execution."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from windops_backend.operations import load_services as execution
from windops_backend.operations.load_contracts import parse_load_profile
from windops_backend.operations.load_services_controls import aggregate_health
from windops_backend.operations.load_services_inputs import parse_services_inputs
from windops_backend.operations.load_services_stack import OwnedComposeStack, StackError


def profile():
    value = json.loads(
        (Path(__file__).parent / "fixtures/performance/software-small.json").read_text()
    )
    value.update(mode="services", maximum_requests=1000)
    return parse_load_profile(json.dumps(value).encode())


def inputs():
    return parse_services_inputs(b'{"schema_version":"openvigil.load-services-inputs.v1"}')


@pytest.mark.parametrize(
    "name",
    ["services-reads", "services-documents", "services-backlog-offline", "services-backlog-claim"],
)
def test_registered_profiles_have_matching_bounded_private_inputs(name):
    directory = Path(__file__).parent / "fixtures/performance"
    plan = parse_load_profile((directory / f"{name}.json").read_bytes())
    registered = parse_services_inputs((directory / f"{name}.inputs.json").read_bytes())
    execution.validate_plan(plan, registered)
    for source in registered.documents:
        assert source.classification == "reviewed_synthetic"
        assert source.expected_source_pages == 2 and source.expected_passages == 15


def health():
    now = datetime.now(UTC).isoformat()
    return {
        "docling_pdf_configured": True,
        "worker_health_verified": True,
        "worker": {
            "status": "ready",
            "runtime_ready": True,
            "heartbeat_verified": True,
            "heartbeat_ttl_seconds": 45,
            "observed_at": now,
        },
        "queue": {
            "scope": "authorized_documents",
            "observed_at": now,
            "waiting": 2,
            "running": 0,
            "retrying": 0,
            "expired_leases": 0,
            "backlogged": False,
            "backlog_warning_seconds": 120,
        },
    }


def test_two_scoped_health_counts_are_summed_without_inventing_unavailable_metrics():
    a, b = health(), health()
    b["queue"]["running"] = 1
    result = aggregate_health([a, b], scope_verified=True)
    assert result.waiting == 4 and result.running == 1 and result.worker_health_verified
    b["worker"].update(status="offline", runtime_ready=False, heartbeat_verified=False)
    b["worker_health_verified"] = False
    result = aggregate_health([a, b], scope_verified=True)
    assert result.status == "unavailable" and not result.worker_health_verified


@pytest.mark.parametrize("case", ["missing", "null", "boolean", "stale", "foreign", "ttl", "scope"])
def test_invalid_health_never_becomes_zero_or_fresh(case):
    a, b = health(), health()
    if case == "missing":
        del a["queue"]["waiting"]
    elif case == "null":
        a["queue"]["waiting"] = None
    elif case == "boolean":
        a["queue"]["waiting"] = True
    elif case == "stale":
        a["worker"]["observed_at"] = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    elif case == "foreign":
        a["queue"]["scope"] = "global"
    elif case == "ttl":
        a["worker"]["heartbeat_ttl_seconds"] = 1
    with pytest.raises(ValueError):
        aggregate_health([a, b], scope_verified=case != "scope")


def test_six_routes_have_four_real_subjects_and_separate_history_oracles():
    routes = execution.read_routes(profile(), execution.ServicesDatasetOracle(9000))
    assert {r.route_id for r in routes} == {
        "turbines",
        "diagnoses",
        "maintenance-plans",
        "knowledge-documents",
        "scada-history",
        "scada-measurements",
    }
    assert all(set(r.query_by_user) == {"a1", "a2", "b1", "b2"} for r in routes)
    assert all(not r.validator({"meta": {"total": 0}}, "a1") for r in routes)


@pytest.fixture
def stable_snapshots(monkeypatch):
    # Component tests deliberately freeze source metadata while other agents edit.
    # Actual run source integrity is separately checked by snapshot comparison.
    monkeypatch.setattr(execution, "services_source_snapshot", lambda: {"sha256": "a" * 64})
    monkeypatch.setattr(execution, "protection_snapshot", lambda: {"head": "c" * 40})


@pytest.mark.asyncio
async def test_unavailable_services_preserve_report_and_never_fall_back(
    monkeypatch, stable_snapshots
):
    calls = []

    async def unavailable(self):
        calls.append("preflight")
        raise StackError("SERVICE_RUNTIME_UNAVAILABLE")

    async def forbidden(*args, **kwargs):
        pytest.fail("no software fallback or service mutation after failed preflight")

    monkeypatch.setattr(OwnedComposeStack, "preflight", unavailable)
    monkeypatch.setattr(OwnedComposeStack, "start", forbidden)
    path, report = await execution.execute_services(profile(), inputs())
    assert calls == ["preflight"]
    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert report["status"] == "failed" and report["qualified"] is False
    assert report["external_services_verified"] is False and report["cleanup"]["passed"]
    assert all(v["status"] == "not_run" for v in report["workloads"].values())
    assert "services_run_failed" in report["failure_codes"]
    assert "password" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cancelled_setup_and_repeated_cleanup_cancellation_keep_durable_report(
    monkeypatch, stable_snapshots
):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def setup(self, *args):
        entered.set()
        await asyncio.Event().wait()

    original = execution.ServicesRuntime.cleanup

    async def cleanup(self):
        cleaning.set()
        await release.wait()
        return await original(self)

    monkeypatch.setattr(execution.ServicesRuntime, "setup", setup)
    monkeypatch.setattr(execution.ServicesRuntime, "cleanup", cleanup)
    task = asyncio.create_task(execution.execute_services(profile(), inputs()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    path, report = await asyncio.wait_for(task, 5)
    assert report["cleanup"]["passed"] and report["status"] == "failed"
    assert {"services_run_cancelled", "finalization_cancelled"} <= set(report["failure_codes"])
    assert json.loads(path.read_text(encoding="utf-8")) == report


@pytest.mark.asyncio
async def test_cleanup_failure_still_persists_original_failure_and_snapshots(
    monkeypatch, stable_snapshots
):
    async def setup(self, *args):
        raise RuntimeError("private-diagnostic-not-public")

    async def cleanup(self):
        raise RuntimeError("private-cleanup-diagnostic")

    monkeypatch.setattr(execution.ServicesRuntime, "setup", setup)
    monkeypatch.setattr(execution.ServicesRuntime, "cleanup", cleanup)
    path, report = await execution.execute_services(profile(), inputs())
    assert "services_run_failed" in report["failure_codes"]
    assert "services_cleanup_failed" in report["failure_codes"]
    assert report["source_after"] == report["source_before"]
    assert "private-diagnostic" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("late_cancel", [False, True])
async def test_cancellation_at_completed_finalizer_never_persists_success(
    monkeypatch, stable_snapshots, late_cancel
):
    # Successful external adapters are explicit fixtures. The production execute,
    # finalizer, report publication and real asyncio cancellation remain intact.
    events = []

    async def setup(self, *args):
        self.stack_evidence = {
            "containers": {
                role: {"image_id": "sha256:" + "a" * 64, "port": index + 43101}
                for index, role in enumerate(execution._CONTAINER_ROLES)
            }
        }
        self.stack.containers = {role: "a" * 64 for role in execution._CONTAINER_ROLES}
        self.setup_evidence = {"dataset": {}, "database_before": {}}
        self.ready = {"api": {"runtime": {}}}
        self.runtime_sha256 = "a" * 64
        self.engine = object()
        self.api_port = 43100

    async def cleanup(self):
        await asyncio.sleep(0)
        events.append("owned_cleanup_finished")
        return {"passed": True, "roles": [], "ports_released": True}

    class HttpFixture:
        def __init__(self, **kwargs):
            pass

        async def get(self, **kwargs):
            return {"status": "ready", "knowledge_graph": "neo4j"}

        def report(self):
            return {"samples": [], "cleanup_passed": True}

    class SamplerFixture:
        def __init__(self, **kwargs):
            pass

        def start(self):
            return self

        async def wait_first_sample(self):
            await asyncio.sleep(0)

        async def stop(self):
            return {"budget_passed": True, "cleanup_passed": True}

    async def measured(self):
        for workload in self.profile.workloads:
            self.report["workloads"][workload] = {"status": "observed"}
        await asyncio.sleep(0)

    async def verified(*args, **kwargs):
        return {"passed": True}

    async def database(*args):
        return {}

    monkeypatch.setattr(execution.ServicesRuntime, "setup", setup)
    monkeypatch.setattr(execution.ServicesRuntime, "cleanup", cleanup)
    monkeypatch.setattr(execution.ServicesRuntime, "verify", property(lambda self: True))
    monkeypatch.setattr(execution.ServicesRuntime, "factory", lambda self: None)
    monkeypatch.setattr(execution, "ServicesHttp", HttpFixture)
    monkeypatch.setattr(execution, "ServicesResourceSampler", SamplerFixture)
    monkeypatch.setattr(execution._Measurement, "run", measured)
    monkeypatch.setattr(execution, "authorization_controls", verified)
    monkeypatch.setattr(execution, "snapshot_services_dataset", database)
    monkeypatch.setattr(execution, "load_runtime", lambda *args: object())
    monkeypatch.setattr(
        "windops_backend.operations.load_services_postflight.collect_postflight", verified
    )
    original_finalize = execution._finalize_services
    execute_task = None

    async def finalized(*args):
        await original_finalize(*args)
        events.append("finalizer_finished")
        if late_cancel:
            # Queue cancellation immediately before the completed shield wakes its
            # caller. Never replace or mutate the finalizer's success calculation.
            asyncio.get_running_loop().call_soon(execute_task.cancel)

    monkeypatch.setattr(execution, "_finalize_services", finalized)
    execute_task = asyncio.create_task(execution.execute_services(profile(), inputs()))
    path, report = await asyncio.wait_for(execute_task, 5)
    assert events == ["owned_cleanup_finished", "finalizer_finished"]
    assert report["cleanup"]["passed"] and report["cleanup"]["ports_released"]
    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert report["qualified"] is False
    if late_cancel:
        assert report["failure_codes"] == ["finalization_cancelled"]
        assert report["status"] == "failed"
        assert report["external_services_verified"] is False
    else:
        assert report["failure_codes"] == []
        assert report["status"] == "measurement_only"
        assert report["external_services_verified"] is True


@pytest.mark.asyncio
async def test_first_resource_sample_wait_consumes_http_work_deadline(monkeypatch):
    budget = profile().model_copy(
        update={"maximum_duration_seconds": 0.2, "request_timeout_seconds": 0.1}
    )
    run = execution._Measurement(
        SimpleNamespace(origin="https://127.0.0.1:1", users=(), verify=True),
        budget,
        inputs(),
        {},
        {"failure_codes": [], "workloads": {}},
    )
    start = time.perf_counter()
    run.started = start - 0.08
    seen = []

    async def reads(**kwargs):
        seen.append(kwargs["profile"].maximum_duration_seconds)
        await asyncio.sleep(2)

    monkeypatch.setattr(execution, "run_load", reads)
    with pytest.raises(TimeoutError):
        await run.run()
    assert 0 < seen[0] <= 0.12
    assert run.started == start - 0.08 and time.perf_counter() - start < 0.4


@pytest.mark.parametrize(
    "mode,seconds", [("offline_then_resume", 180.0), ("claim_loss_then_recover", 600.0)]
)
@pytest.mark.asyncio
async def test_exact_minimum_backlog_registration_keeps_declared_ceiling(
    monkeypatch, mode, seconds
):
    # Documents are protocol fixtures: only the budget integration is exercised.
    configured = inputs().model_copy(
        update={
            "backlog_mode": mode,
            "documents": (
                SimpleNamespace(workload="backlog", alias="a"),
                SimpleNamespace(workload="backlog", alias="b"),
            ),
        }
    )
    raw = profile().model_dump(mode="json")
    raw.update(
        workloads=["backlog"],
        documents=2,
        maximum_upload_bytes=100,
        maximum_duration_seconds=seconds,
        budgets=[{**raw["budgets"][0], "workload": "backlog"}],
    )
    configured_profile = parse_load_profile(json.dumps(raw).encode())
    runtime = SimpleNamespace(
        origin="https://127.0.0.1:1",
        users=(),
        verify=True,
        factory=lambda: None,
        current_document_worker=lambda: None,
        stop_document_worker=lambda: None,
        start_document_worker=lambda: None,
    )
    run = execution._Measurement(runtime, configured_profile, configured, {}, {"failure_codes": []})
    run.started = time.perf_counter() - 0.1
    observer = SimpleNamespace(
        read_claim=lambda: None,
        verify_offline_documents=lambda: None,
        verify_recovery=lambda value: None,
    )
    monkeypatch.setattr(execution, "ServicesHttp", lambda **kwargs: object())
    monkeypatch.setattr(execution, "evidence_manifest", lambda *args, **kwargs: ())
    monkeypatch.setattr(execution, "OwnedEvidenceObservers", lambda *args: observer)
    seen = []

    async def backlog(**kwargs):
        seen.append(kwargs["limits"].maximum_total_seconds)
        return SimpleNamespace(outcome="success", task_cleanup_passed=True, to_dict=lambda: {})

    monkeypatch.setattr(execution, "run_backlog", backlog)
    await run.run()
    assert seen == [seconds] and run.remaining() < seconds


def test_cli_dispatch_keeps_services_failure_and_exit_code(tmp_path, monkeypatch, capsys):
    script = Path(__file__).parents[1] / "scripts/run_performance_e2e.py"
    spec = importlib.util.spec_from_file_location("owned_performance_cli_fixture", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "profile.json"
    source.write_text(profile().model_dump_json())
    manifest = tmp_path / "input.json"
    manifest.write_text(inputs().model_dump_json())
    monkeypatch.setattr(
        "sys.argv", [str(script), "--profile", str(source), "--services-inputs", str(manifest)]
    )

    async def services(value, registered):
        assert value.mode == "services" and registered == inputs()
        return tmp_path / "report.json", {
            "status": "failed",
            "qualified": False,
            "failure_codes": ["runtime_unavailable"],
        }

    async def software(value):
        pytest.fail("services must not use software runtime")

    monkeypatch.setattr(module, "execute_services", services)
    monkeypatch.setattr(module, "execute_software", software)
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out)["failure_codes"] == ["runtime_unavailable"]
