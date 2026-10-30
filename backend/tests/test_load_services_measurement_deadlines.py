"""Actual asyncio deadlines with explicit scheduler fixtures; no HTTP, Docker or OCR."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from windops_backend.operations import load_services as execution
from windops_backend.operations.load_contracts import RequestSample, parse_load_profile
from windops_backend.operations.load_documents import (
    STAGES,
    DocumentBatchResult,
    DocumentHttpSample,
    DocumentInput,
    DocumentResult,
    DocumentStageSample,
    new_document_id,
)
from windops_backend.operations.load_runner import LoadRunResult
from windops_backend.operations.load_services_inputs import parse_services_inputs, validate_plan


def measurement(tmp_path, workloads):
    raw = json.loads(
        (Path(__file__).parent / "fixtures/performance/software-small.json").read_text()
    )
    run_id = "a" * 32
    documents, planned = {}, []
    for workload in workloads:
        if workload not in {"document_batch", "backlog"}:
            continue
        for user_id in ("a1", "b1"):
            alias = f"{workload}-{user_id}"
            item = DocumentInput(
                document_id=new_document_id(run_id),
                user_id=user_id,
                path=tmp_path / f"{alias}.pdf",
                artifact_sha256="b" * 64,
                size_bytes=100,
                tenant_id=f"tenant-{user_id}",
                wind_farm_id=f"farm-{user_id}",
                turbine_id=f"turbine-{user_id}",
            )
            documents[alias] = item
            planned.append(
                dict(
                    alias=alias,
                    workload=workload,
                    user_id=user_id,
                    pdf_path=str(item.path),
                    artifact_sha256=item.artifact_sha256,
                    size_bytes=item.size_bytes,
                    expected_source_pages=1,
                    expected_passages=1,
                    classification="reviewed_synthetic",
                )
            )
    raw.update(
        mode="services",
        workloads=workloads,
        maximum_requests=2000,
        maximum_duration_seconds=180.0,
        warmup_requests=0,
        documents=len(documents),
        maximum_upload_bytes=sum(d.size_bytes for d in documents.values()),
        budgets=[{**raw["budgets"][0], "workload": w} for w in workloads],
    )
    profile = parse_load_profile(json.dumps(raw).encode())
    source = dict(schema_version="openvigil.load-services-inputs.v1", documents=planned)
    if documents:
        source.update(
            parser_python="fixture-not-executed",
            parser_models="fixture-not-read",
            parser_manifest_sha256="c" * 64,
        )
    if "backlog" in workloads:
        source["backlog_mode"] = "offline_then_resume"
    inputs = parse_services_inputs(json.dumps(source).encode())
    validate_plan(profile, inputs)
    runtime = SimpleNamespace(
        run_id=run_id,
        origin="https://127.0.0.1:1",
        users=(),
        verify=True,
        stack_evidence={"containers": {"minio": {"port": 1}}},
    )
    report = {
        "failure_codes": [],
        "workloads": {w: {"status": "not_run"} for w in workloads},
        "planned_documents": [
            {"alias": d.alias, "workload": d.workload, "status": "not_run"}
            for d in inputs.documents
        ],
    }
    return execution._Measurement(runtime, profile, inputs, documents, report)


def read_partial(run, elapsed, *, stop_reason="request_cancelled"):
    samples = []
    for sequence in range(run.read_plan):
        fields = dict(
            workload=run.routes[sequence // run.profile.requests_per_route].workload,
            route_id=run.routes[sequence // run.profile.requests_per_route].route_id,
            user_id="a1",
            sequence=sequence,
            outcome="not_run",
            started=False,
            error_code="not_started",
        )
        if sequence == 0:
            fields.update(
                outcome="cancelled",
                started=True,
                scheduled_monotonic=0.0,
                started_monotonic=0.0,
                finished_monotonic=elapsed,
                scheduler_delay_ms=0.0,
                elapsed_ms=elapsed * 1000,
                error_code="request_cancelled",
            )
        samples.append(RequestSample(**fields))
    return LoadRunResult(tuple(samples), run.read_plan, elapsed, stop_reason, True, 1)


def document_partial(run, selected, elapsed):
    documents = []
    for index, item in enumerate(selected):
        stages = tuple(DocumentStageSample(stage) for stage in STAGES)
        if index == 0:
            for stage in stages[:3]:
                stage.outcome = "success"
                stage.started_monotonic = stage.finished_monotonic = 0.0
            stages[3].outcome = "unknown"
            stages[3].started_monotonic = 0.0
            stages[3].finished_monotonic = elapsed
            stages[3].error_code = "request_cancelled"
        documents.append(
            DocumentResult(
                item.document_id,
                item.user_id,
                item.parser,
                item.artifact_sha256,
                item.size_bytes,
                stages,
                outcome="submission_unknown" if index == 0 else "not_run",
                accepted=None if index == 0 else False,
                error_code="request_cancelled" if index == 0 else None,
            )
        )
    item = selected[0]
    http = tuple(
        DocumentHttpSample(
            sequence,
            item.document_id,
            item.user_id,
            stage,
            outcome="unknown" if stage == "ingest" else "success",
            started_monotonic=0.0,
            finished_monotonic=elapsed if stage == "ingest" else 0.0,
            http_status=None if stage == "ingest" else 200,
            request_bytes=item.size_bytes if stage == "upload" else 0,
            response_bytes=None if stage == "ingest" else 2,
            error_code="request_cancelled" if stage == "ingest" else None,
        )
        for sequence, stage in enumerate(("presign", "upload", "ingest"))
    )
    return DocumentBatchResult(
        run.runtime.run_id,
        tuple(documents),
        http,
        elapsed,
        False,
        True,
        "request_cancelled",
        item.size_bytes,
        4,
        1,
    )


def forbid_later_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a later workload or its HTTP client started after measurement cancellation")

    monkeypatch.setattr(execution, "ServicesHttp", forbidden)
    monkeypatch.setattr(execution, "run_backlog", forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["deadline", "resource_cancel", "scheduler_stop"])
async def test_partial_reads_stop_later_workloads_and_keep_every_planned_sample(
    tmp_path, monkeypatch, trigger
):
    run = measurement(tmp_path, ["queries", "document_batch", "backlog"])
    forbid_later_work(monkeypatch)
    calls, returned = [], []

    async def forbidden_document(**kwargs):
        pytest.fail("document requests started after read scheduler stopped")

    async def reads(**kwargs):
        calls.append("reads")
        started = time.perf_counter()
        if trigger == "deadline":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                calls.append("cancel_received")
        elif trigger == "resource_cancel":
            kwargs["cancel_event"].set()
            await asyncio.sleep(0)
        result = read_partial(run, time.perf_counter() - started)
        returned.append(result)
        return result

    monkeypatch.setattr(execution, "run_load", reads)
    monkeypatch.setattr(execution, "run_document_batch", forbidden_document)
    run.started = time.perf_counter() - (179.9 if trigger == "deadline" else 0)
    with pytest.raises(RuntimeError, match="MEASUREMENT_CANCELLED"):
        await asyncio.wait_for(run.run(), 2)
    assert calls == (["reads", "cancel_received"] if trigger == "deadline" else ["reads"])
    assert run.cancel.is_set() and run.read_result is returned[0]
    ledger = run.report["read_samples"]
    assert ledger == [sample.model_dump(mode="json") for sample in returned[0].samples]
    assert len(ledger) == run.read_plan == 400
    assert ledger[0]["outcome"] == "cancelled" and ledger[0]["elapsed_ms"] > 0
    assert all(sample["outcome"] == "not_run" for sample in ledger[1:])
    assert "read_workload_incomplete" in run.report["failure_codes"]
    assert all(d["status"] == "not_run" for d in run.report["planned_documents"])
    assert run.report["workloads"]["document_batch"]["status"] == "not_run"
    assert run.report["workloads"]["backlog"]["status"] == "not_run"
    assert not run.batches and run.http is None


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["deadline", "resource_cancel"])
async def test_partial_document_batch_keeps_unknown_submission_and_never_starts_backlog(
    tmp_path, monkeypatch, trigger
):
    run = measurement(tmp_path, ["document_batch", "backlog"])
    forbid_later_work(monkeypatch)
    calls, returned = [], []

    async def documents(**kwargs):
        calls.append("documents")
        started = time.perf_counter()
        if trigger == "deadline":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                calls.append("cancel_received")
        else:
            kwargs["cancel_event"].set()
            await asyncio.sleep(0)
        result = document_partial(run, kwargs["documents"], time.perf_counter() - started)
        returned.append(result)
        return result

    monkeypatch.setattr(execution, "run_document_batch", documents)
    run.started = time.perf_counter() - (179.9 if trigger == "deadline" else 0)
    with pytest.raises(RuntimeError, match="MEASUREMENT_CANCELLED"):
        await asyncio.wait_for(run.run(), 2)
    assert calls == (["documents", "cancel_received"] if trigger == "deadline" else ["documents"])
    assert run.cancel.is_set() and run.batches == {"document_batch": returned[0]}
    assert run.report["workloads"]["document_batch"]["batch"] == asdict(returned[0])
    assert len(returned[0].http_samples) == 3
    assert returned[0].http_samples[-1].outcome == "unknown"
    assert returned[0].documents[0].accepted is None
    assert "document_batch_incomplete" in run.report["failure_codes"]
    assert [d["status"] for d in run.report["planned_documents"]] == [
        "submission_unknown",
        "not_run",
        "not_run",
        "not_run",
    ]
    assert run.report["workloads"]["backlog"]["status"] == "not_run"
    assert run.http is None


@pytest.mark.asyncio
async def test_preexisting_resource_cancel_starts_no_scheduler(tmp_path, monkeypatch):
    run = measurement(tmp_path, ["queries", "document_batch", "backlog"])
    forbid_later_work(monkeypatch)

    async def forbidden(**kwargs):
        pytest.fail("a scheduler started after resource cancellation")

    monkeypatch.setattr(execution, "run_load", forbidden)
    monkeypatch.setattr(execution, "run_document_batch", forbidden)
    run.started = time.perf_counter()
    run.cancel.set()
    with pytest.raises(RuntimeError, match="MEASUREMENT_CANCELLED"):
        await run.run()
    assert run.read_result is None and not run.batches
    assert all(w["status"] == "not_run" for w in run.report["workloads"].values())


@pytest.mark.asyncio
async def test_last_scheduler_swallowing_deadline_cannot_hide_measured_overrun(
    tmp_path, monkeypatch
):
    run = measurement(tmp_path, ["queries"])
    cancelled = asyncio.Event()

    async def reads(**kwargs):
        started = time.perf_counter()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
        return read_partial(run, time.perf_counter() - started)

    monkeypatch.setattr(execution, "run_load", reads)
    run.started = time.perf_counter() - 179.9
    await asyncio.wait_for(run.run(), 2)
    assert cancelled.is_set() and run.cancel.is_set()
    assert run.report["measurement_elapsed_seconds"] >= run.profile.maximum_duration_seconds
    assert {"measurement_deadline_exceeded", "read_workload_incomplete"} <= set(
        run.report["failure_codes"]
    )
    assert len(run.report["read_samples"]) == 400
