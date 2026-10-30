"""Four bounded services workloads with durable partial evidence and owned cleanup.

This runner never falls back to SQLite or substitutes parser output. Synthetic
local measurements do not qualify production capacity or a signed release.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from windops_backend.operations.load_backlog import BacklogCallbacks, BacklogLimits, run_backlog
from windops_backend.operations.load_contracts import (
    LoadProfile,
    ResourceObservation,
    RunIdentity,
    evaluate_workload,
    nearest_rank,
    profile_sha256,
    summarize_samples,
)
from windops_backend.operations.load_dataset import canonical_hash
from windops_backend.operations.load_document_evidence import (
    OwnedDocumentEvidenceSpec,
    collect_document_evidence,
)
from windops_backend.operations.load_documents import (
    DocumentBatchLimits,
    DocumentBatchResult,
    DocumentInput,
    run_document_batch,
)
from windops_backend.operations.load_resources import register_process
from windops_backend.operations.load_runner import AllowedGet, LoadRunResult, PrivateUser, run_load
from windops_backend.operations.load_services_controls import (
    authorization_controls,
    read_health,
    wait_document_ready,
)
from windops_backend.operations.load_services_dataset import (
    ServicesDatasetOracle,
    snapshot_services_dataset,
)
from windops_backend.operations.load_services_evidence import OwnedEvidenceObservers
from windops_backend.operations.load_services_http import ServicesHttp
from windops_backend.operations.load_services_inputs import (
    ServicesInputs,
    input_identity,
    prepare_inputs,
    validate_plan,
)
from windops_backend.operations.load_services_resources import (
    ContainerIdentity,
    ContainerRole,
    ServicesResourceLimits,
    ServicesResourceSampler,
)
from windops_backend.operations.load_services_runtime import ServicesRuntime
from windops_backend.operations.load_services_server import checked_workspace_path, load_runtime
from windops_backend.operations.load_services_settings import BYPASS_CONTROL_HEADERS
from windops_backend.operations.load_software import (
    ROOT,
    RUN_ROOT,
    protection_snapshot,
    source_snapshot,
    validate_run_directory,
)

_CONTAINER_ROLES: tuple[ContainerRole, ...] = ("postgres", "redis", "minio", "neo4j")


def services_source_snapshot() -> dict[str, Any]:
    source = source_snapshot()
    for name in (
        "backend/docker-compose.yml",
        "backend/deploy/Dockerfile.minio",
        "backend/requirements.document-parser.txt",
        "backend/alembic.ini",
        *(
            path.relative_to(ROOT).as_posix()
            for path in sorted((ROOT / "backend/alembic").rglob("*.py"))
        ),
    ):
        path = checked_workspace_path(ROOT / name)
        # Build and parser inputs participate in the same source identity.
        source["files"][name] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
    source["sha256"] = canonical_hash(source["files"])
    return source


def read_routes(profile: LoadProfile, oracle: ServicesDatasetOracle) -> list[AllowedGet]:
    routes = []
    for workload, names in (
        ("queries", ("turbines", "diagnoses", "maintenance-plans", "knowledge/documents")),
        ("history", ("scada/history", "scada/measurements")),
    ):
        if workload not in profile.workloads:
            continue
        for name in names:
            routes.append(
                AllowedGet(
                    workload=workload,
                    route_id=name.replace("/", "-"),
                    path=f"/api/v1/{name}",
                    query_by_user=oracle.queries[name],
                    validator=partial(oracle.validate, name),
                )
            )
    return routes


def evidence_manifest(
    inputs: ServicesInputs,
    documents: dict[str, DocumentInput],
    batches: dict[str, DocumentBatchResult],
    *,
    workload: str | None = None,
    expect_accepted: bool = False,
) -> tuple[OwnedDocumentEvidenceSpec, ...]:
    observed = {d.document_id: d.accepted for batch in batches.values() for d in batch.documents}
    result = []
    for planned in inputs.documents:
        if workload is not None and planned.workload != workload:
            continue
        item = documents[planned.alias]
        accepted = observed.get(item.document_id, True if expect_accepted else False)
        result.append(
            OwnedDocumentEvidenceSpec(
                document_alias=planned.alias,
                document_id=item.document_id,
                artifact_sha256=item.artifact_sha256,
                document_version=item.document_version,
                tenant_id=item.tenant_id,
                wind_farm_id=item.wind_farm_id,
                turbine_id=item.turbine_id,
                submission="accepted"
                if accepted is True
                else "submission_unknown"
                if accepted is None
                else "not_accepted",
                embedding_provider="deterministic_test",
                embedding_model="sha256-token-projection-v1",
                embedding_dimensions=1536,
                expected_passages=planned.expected_passages,
            )
        )
    return tuple(result)


def document_summary(batch: DocumentBatchResult, planned: int) -> dict[str, Any]:
    durations = []
    for document in batch.documents:
        start = document.stages[0].started_monotonic
        finish = document.stages[-1].finished_monotonic
        if document.outcome == "indexed" and start is not None and finish is not None:
            durations.append((finish - start) * 1000)
    indexed = sum(d.outcome == "indexed" for d in batch.documents)
    return {
        "unit": "document_source_validation_through_index_observed_ms",
        "planned": planned,
        "indexed": indexed,
        "non_success": planned - indexed,
        "error_rate": (planned - indexed) / planned,
        "observed_success_durations_ms": durations,
        "p95_ms": nearest_rank(durations, 0.95) if len(durations) >= 100 else None,
        "p99_ms": nearest_rank(durations, 0.99) if len(durations) >= 100 else None,
        "tail_evidence": "insufficient_samples" if len(durations) < 100 else "observed",
        "http_requests": len(batch.http_samples),
        "qualified": False,
    }


def resource_observation(raw: dict[str, Any]) -> ResourceObservation:
    peaks = [
        p["observed_peak_rss_bytes"]
        for p in raw["processes"]
        if p["observed_peak_rss_bytes"] is not None
    ]
    observed = raw["process_rss"]["complete_observation_slots"]
    return ResourceObservation(
        available=bool(peaks and observed),
        expected_samples=raw["expected_slot_count"],
        observed_samples=observed,
        peak_process_rss_bytes=max(peaks) if peaks else None,
        peak_total_rss_bytes=raw["process_rss"]["observed_peak_total_bytes"],
    )


class _Measurement:
    def __init__(
        self,
        runtime: ServicesRuntime,
        profile: LoadProfile,
        inputs: ServicesInputs,
        documents: dict[str, DocumentInput],
        report: dict[str, Any],
    ) -> None:
        self.runtime, self.profile, self.inputs = runtime, profile, inputs
        self.documents, self.report = documents, report
        self.cancel = asyncio.Event()
        self.batches: dict[str, DocumentBatchResult] = {}
        self.read_result: LoadRunResult | None = None
        self.routes = read_routes(profile, ServicesDatasetOracle(profile.history_rows))
        self.read_plan = len(self.routes) * profile.requests_per_route + (
            profile.warmup_requests if self.routes else 0
        )
        self.health_cap = 512 if "backlog" in profile.workloads else 0
        self.document_cap = profile.maximum_requests - self.read_plan - self.health_cap
        if inputs.documents and self.document_cap < len(inputs.documents) * 5:
            raise ValueError("services request budget must include recovery observations")
        self.http: ServicesHttp | None = None
        self.started: float | None = None

    def remaining(self) -> float:
        if self.started is None or self.cancel.is_set():
            raise RuntimeError("MEASUREMENT_CANCELLED")
        seconds = self.profile.maximum_duration_seconds - (time.perf_counter() - self.started)
        if seconds <= 0:
            raise TimeoutError
        return seconds

    async def batch(
        self, workload: str, hook: Callable[[], Awaitable[None]] | None = None
    ) -> DocumentBatchResult:
        selected = [
            self.documents[d.alias] for d in self.inputs.documents if d.workload == workload
        ]
        cap = self.document_cap * len(selected) // len(self.inputs.documents)
        remaining = self.remaining()
        result = await run_document_batch(
            run_id=self.runtime.run_id,
            api_origin=self.runtime.origin,
            minio_origin=f"http://127.0.0.1:{self.runtime.stack_evidence['containers']['minio']['port']}",
            minio_bucket="windops-knowledge-documents",
            users=self.runtime.users,
            documents=selected,
            limits=DocumentBatchLimits(
                concurrency=min(8, self.profile.concurrency),
                maximum_documents=len(selected),
                maximum_http_requests=cap,
                maximum_total_seconds=remaining,
                request_timeout_seconds=min(self.profile.request_timeout_seconds, remaining),
                maximum_total_upload_bytes=sum(d.size_bytes for d in selected),
                maximum_polls_per_document=900,
                maximum_response_bytes=min(
                    24 * 1024 * 1024,
                    next(
                        b.maximum_response_bytes
                        for b in self.profile.budgets
                        if b.workload == workload
                    ),
                ),
            ),
            workspace_root=ROOT,
            verify=self.runtime.verify,
            cancel_event=self.cancel,
            after_submissions=hook,
        )
        self.batches[workload] = result
        if result.stop_reason is not None:
            self.cancel.set()
        for planned in self.report["planned_documents"]:
            if planned["workload"] == workload:
                item = self.documents[planned["alias"]]
                document = next(d for d in result.documents if d.document_id == item.document_id)
                planned["status"] = document.outcome
        self.report["workloads"][workload] = {
            "status": "observed",
            "batch": asdict(result),
            "summary": document_summary(result, len(selected)),
        }
        return result

    async def run(self) -> None:
        if self.started is None:
            raise RuntimeError("RESOURCE_WINDOW_NOT_STARTED")
        # The budget starts before sampling starts. Waiting for the first complete
        # sample consumes it, so all HTTP work ends inside the sampled window.
        async with asyncio.timeout(self.remaining()):
            if self.routes:
                remaining = self.remaining()
                workloads = tuple(w for w in self.profile.workloads if w in {"queries", "history"})
                subprofile = LoadProfile.model_validate_json(
                    self.profile.model_copy(
                        update={
                            "workloads": workloads,
                            "budgets": tuple(
                                b for b in self.profile.budgets if b.workload in workloads
                            ),
                            "maximum_duration_seconds": remaining,
                            "request_timeout_seconds": min(
                                remaining, self.profile.request_timeout_seconds
                            ),
                            "maximum_requests": self.read_plan,
                        }
                    ).model_dump_json()
                )
                result = await run_load(
                    origin=self.runtime.origin,
                    users=self.runtime.users,
                    routes=self.routes,
                    profile=subprofile,
                    cancel_event=self.cancel,
                    verify=self.runtime.verify,
                )
                self.read_result = result
                self.report["read_samples"] = [s.model_dump(mode="json") for s in result.samples]
                self.report["read_scheduler"] = {
                    "planned": result.planned_requests,
                    "elapsed_seconds": result.elapsed_seconds,
                    "cleanup_passed": result.cleanup_passed,
                    "stop_reason": result.stop_reason,
                    "max_observed_inflight": result.max_observed_inflight,
                }
                for workload in workloads:
                    self.report["workloads"][workload] = {"status": "observed"}
                if (
                    not result.cleanup_passed
                    or result.stop_reason
                    or any(s.outcome != "success" for s in result.samples)
                ):
                    self.report["failure_codes"].append("read_workload_incomplete")
                if result.stop_reason is not None:
                    self.cancel.set()
            if "document_batch" in self.profile.workloads:
                self.remaining()
                batch_result = await self.batch("document_batch")
                if not batch_result.completed or not batch_result.cleanup_passed:
                    self.report["failure_codes"].append("document_batch_incomplete")
            if self.inputs.backlog_mode is not None:
                self.remaining()
                self.http = ServicesHttp(
                    origin=self.runtime.origin,
                    users=self.runtime.users,
                    verify=self.runtime.verify,
                    maximum_requests=self.health_cap,
                )
                observers = OwnedEvidenceObservers(
                    self.runtime.factory(),
                    evidence_manifest(
                        self.inputs,
                        self.documents,
                        self.batches,
                        workload="backlog",
                        expect_accepted=True,
                    ),
                )

                async def health() -> Any:
                    if self.http is None:
                        raise RuntimeError("HEALTH_OBSERVER_UNAVAILABLE")
                    return await read_health(self.http, scope_verified=True)

                async def batch(hook: Callable[[], Awaitable[None]]) -> DocumentBatchResult:
                    return await self.batch("backlog", hook)

                result_backlog = await run_backlog(
                    scenario=self.inputs.backlog_mode,
                    planned_documents=sum(d.workload == "backlog" for d in self.inputs.documents),
                    callbacks=BacklogCallbacks(
                        current_worker=self.runtime.current_document_worker,
                        read_health=health,
                        stop_worker=self.runtime.stop_document_worker,
                        start_worker=self.runtime.start_document_worker,
                        run_documents=batch,
                        read_claim=observers.read_claim,
                        verify_offline_documents=observers.verify_offline_documents,
                        verify_recovery=observers.verify_recovery,
                    ),
                    # Registration uses the declared phase ceiling. The enclosing
                    # timeout and every document call still use the actual global
                    # remainder; rounding below 180/600 must not invalidate it.
                    limits=BacklogLimits(
                        maximum_total_seconds=self.profile.maximum_duration_seconds
                    ),
                    cancel_event=self.cancel,
                )
                self.report["backlog"] = result_backlog.to_dict()
                if result_backlog.outcome != "success" or not result_backlog.task_cleanup_passed:
                    self.report["failure_codes"].append("backlog_incomplete")
        self.report["measurement_elapsed_seconds"] = time.perf_counter() - self.started
        if self.report["measurement_elapsed_seconds"] > self.profile.maximum_duration_seconds:
            self.report["failure_codes"].append("measurement_deadline_exceeded")

    def summarize(self, resources: dict[str, Any]) -> None:
        if self.http is not None:
            self.report["measurement_observations"] = self.http.report()
        total = (
            (len(self.read_result.samples) if self.read_result else 0)
            + sum(len(b.http_samples) for b in self.batches.values())
            + (self.http.requests_used if self.http else 0)
        )
        self.report["measurement_request_accounting"] = {
            "requests_accounted": total,
            "maximum_requests": self.profile.maximum_requests,
            "read_reserved": self.read_plan,
            "health_reserved": self.health_cap,
            "documents_reserved": self.document_cap,
            "within_budget": total <= self.profile.maximum_requests,
            "setup_controls_included": False,
        }
        if total > self.profile.maximum_requests:
            self.report["failure_codes"].append("measurement_request_budget_exceeded")
        if self.read_result is None:
            return
        observation = resource_observation(resources)
        self.report["summaries"], self.report["budget_verdicts"] = {}, {}
        for route in self.routes:
            samples = [s for s in self.read_result.samples if s.route_id == route.route_id]
            summary = summarize_samples(samples, planned=self.profile.requests_per_route)
            self.report["summaries"][route.route_id] = summary.model_dump(mode="json")
            budget = next(b for b in self.profile.budgets if b.workload == route.workload)
            self.report["budget_verdicts"][route.route_id] = evaluate_workload(
                summary, budget, observation
            ).model_dump(mode="json")


async def execute_services(
    profile: LoadProfile, inputs: ServicesInputs
) -> tuple[Path, dict[str, Any]]:
    validate_plan(profile, inputs)
    run_id = uuid4().hex
    directory = RUN_ROOT / run_id
    validate_run_directory(directory, run_id)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "private").mkdir(mode=0o700)
    report: dict[str, Any] = {
        "schema_version": "openvigil.load-services-report.v1",
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "failed",
        "qualified": False,
        "execution_mode": "services",
        "external_services_verified": False,
        "profile": profile.model_dump(mode="json"),
        "input_identity": input_identity(inputs),
        "workloads": {w: {"status": "not_run"} for w in profile.workloads},
        "planned_documents": [
            {"alias": d.alias, "workload": d.workload, "status": "not_run"}
            for d in inputs.documents
        ],
        "failure_codes": [],
        "boundaries": [
            "reviewed_synthetic_inputs",
            "deterministic_test_embeddings",
            "no_paid_model_calls",
            "single_api_worker",
            "loopback_verified_tls",
            "production_rate_limits_unchanged",
            "sampled_resource_peaks",
            "no_production_capacity_claim",
        ],
    }
    runtime: ServicesRuntime | None = None
    sampler: ServicesResourceSampler | None = None
    measurement: _Measurement | None = None
    setup_http: ServicesHttp | None = None
    documents: dict[str, DocumentInput] = {}
    try:
        report["source_before"] = services_source_snapshot()
        report["protection_before"] = protection_snapshot()
        async with asyncio.timeout(inputs.setup_deadline_seconds):
            documents, report["inputs_before"] = prepare_inputs(inputs, run_id)
            runtime = ServicesRuntime(directory, run_id)
            await runtime.setup(profile, inputs, report["protection_before"]["head"])
            report["stack"] = runtime.stack_evidence
            report["setup"] = runtime.setup_evidence
            report["roles"] = runtime.ready
            users = (
                *runtime.users,
                PrivateUser("anonymous", {}),
                PrivateUser("test-bypass", BYPASS_CONTROL_HEADERS),
            )
            setup_http = ServicesHttp(
                origin=runtime.origin, users=users, verify=runtime.verify, maximum_requests=256
            )
            await setup_http.get(
                route_id="dependency-ready",
                path="/api/v1/readyz",
                user_id="a1",
                validator=lambda value: (
                    isinstance(value, dict)
                    and value.get("status") == "ready"
                    and value.get("knowledge_graph") == "neo4j"
                ),
            )
            report["authorization_controls"] = await authorization_controls(
                setup_http, ServicesDatasetOracle(profile.history_rows)
            )
            if inputs.documents:
                report["initial_document_health"] = (
                    await wait_document_ready(setup_http, scope_verified=True)
                ).model_dump(mode="json")
        environment = {
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
            "host_sha256": canonical_hash(platform.node()),
            "processor": platform.processor(),
        }
        packages = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
        report["environment"], report["runtime_packages"] = environment, packages
        report["identity"] = RunIdentity(
            profile_sha256=profile_sha256(profile),
            dataset_sha256=canonical_hash(
                {"dataset": runtime.setup_evidence["dataset"], "inputs": input_identity(inputs)}
            ),
            runtime_sha256=canonical_hash(
                {
                    "packages": packages,
                    "runtime": {r: v["runtime"] for r, v in runtime.ready.items()},
                    "parser": report["inputs_before"]["parser"],
                    "images": {
                        r: v["image_id"] for r, v in runtime.stack_evidence["containers"].items()
                    },
                }
            ),
            environment_sha256=canonical_hash(environment),
            source_sha256=report["source_before"]["sha256"],
            cache_policy="fresh_services_sequential_read_warmup_v1",
        ).model_dump(mode="json")
        measurement = _Measurement(runtime, profile, inputs, documents, report)
        driver = register_process(label="load_driver")
        measurement.started = time.perf_counter()
        report["measurement_started_at"] = datetime.now(UTC).isoformat()
        report["resource_window_policy"] = "first_sample_wait_consumes_measurement_budget"
        sampler = ServicesResourceSampler(
            members=lambda: (driver, *runtime.owner.members()),
            containers=tuple(
                ContainerIdentity(
                    role=role, container_id=runtime.stack.containers[role], identity_verified=True
                )
                for role in _CONTAINER_ROLES
            ),
            container_stats=runtime.stack.container_stats,
            limits=ServicesResourceLimits(
                duration_seconds=profile.maximum_duration_seconds,
                interval_seconds=min(10.0, profile.maximum_duration_seconds),
                callback_timeout_seconds=8.0,
                maximum_process_rss_bytes=min(b.maximum_process_rss_bytes for b in profile.budgets),
                maximum_total_process_rss_bytes=min(
                    b.maximum_total_rss_bytes for b in profile.budgets
                ),
                maximum_container_working_set_bytes=inputs.maximum_container_working_set_bytes,
                maximum_total_container_working_set_bytes=inputs.maximum_total_container_working_set_bytes,
            ),
            cancel_event=measurement.cancel,
        ).start()
        report["container_metric_semantics"] = runtime.stack.container_metric_semantics
        await sampler.wait_first_sample()
        try:
            await measurement.run()
        finally:
            report["resources"] = await sampler.stop()
            sampler = None
            measurement.summarize(report["resources"])
        if not report["resources"]["budget_passed"] or not report["resources"]["cleanup_passed"]:
            report["failure_codes"].append("resource_observation_failed")
        async with asyncio.timeout(inputs.evidence_deadline_seconds):
            manifest = evidence_manifest(inputs, documents, measurement.batches)
            if manifest:
                report["document_sql_evidence"] = await collect_document_evidence(
                    runtime.factory(), manifest
                )
                if not report["document_sql_evidence"]["database_evidence_verified"] or report[
                    "document_sql_evidence"
                ]["counts"]["indexed"] != len(manifest):
                    report["failure_codes"].append("document_sql_evidence_failed")
            from windops_backend.operations.load_services_postflight import collect_postflight

            expected_reads = {user: 0 for user in ("a1", "a2", "b1", "b2")}
            for ledger in (
                setup_http.report(),
                measurement.http.report() if measurement.http else {},
            ):
                for sample in ledger.get("samples", []):
                    if (
                        sample["user_id"] in expected_reads
                        and sample["http_status"] == 200
                        and sample["route_id"] != "dependency-ready"
                    ):
                        expected_reads[sample["user_id"]] += 1
            if measurement.read_result:
                for sample in measurement.read_result.samples:
                    if sample.outcome == "success":
                        expected_reads[sample.user_id] += 1
            if runtime.runtime_sha256 is None or runtime.engine is None:
                raise RuntimeError("RUNTIME_IDENTITY_MISSING")
            report["postflight"] = await collect_postflight(
                load_runtime(directory, run_id, "api", runtime.runtime_sha256),
                runtime.factory(),
                manifest,
                started_at=datetime.fromisoformat(report["started_at"]),
                expected_reads=expected_reads,
                deadline_seconds=float(inputs.evidence_deadline_seconds),
            )
            if not report["postflight"]["passed"]:
                report["failure_codes"].append("postflight_evidence_failed")
            report["database_after"] = await snapshot_services_dataset(runtime.engine)
            if report["database_after"] != runtime.setup_evidence["database_before"]:
                report["failure_codes"].append("seed_business_data_changed")
    except asyncio.CancelledError:
        report["failure_codes"].append("services_run_cancelled")
    except Exception as exc:
        report["failure_codes"].append("services_run_failed")
        report["failure_type"] = type(exc).__name__
    finally:
        finalize = asyncio.create_task(
            _finalize_services(
                report,
                directory,
                run_id,
                inputs,
                documents,
                runtime,
                sampler,
                measurement,
                setup_http,
            )
        )
        while not finalize.done():
            try:
                await asyncio.shield(finalize)
            except asyncio.CancelledError:
                if "finalization_cancelled" not in report["failure_codes"]:
                    report["failure_codes"].append("finalization_cancelled")
        finalize.result()
        if report["failure_codes"]:
            report["status"] = "failed"
            report["external_services_verified"] = False
        report = json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
        with (directory / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    return directory / "report.json", report


async def _finalize_services(
    report: dict[str, Any],
    directory: Path,
    run_id: str,
    inputs: ServicesInputs,
    documents: dict[str, DocumentInput],
    runtime: ServicesRuntime | None,
    sampler: ServicesResourceSampler | None,
    measurement: _Measurement | None,
    setup_http: ServicesHttp | None,
) -> None:
    """Finish every owned cleanup step even when an earlier evidence step fails."""
    if setup_http is not None:
        report["setup_observations"] = setup_http.report()
    if measurement is not None and measurement.http is not None:
        report["measurement_observations"] = measurement.http.report()
    if sampler is not None:
        try:
            report["resources"] = await sampler.stop()
        except Exception:
            report["failure_codes"].append("resource_cleanup_failed")
    if (
        measurement is not None
        and inputs.documents
        and runtime is not None
        and runtime.engine is not None
        and "document_sql_evidence" not in report
    ):
        # Preserve a bounded read-only partial ledger after cancellation/failure.
        try:
            report["document_sql_evidence"] = await collect_document_evidence(
                runtime.factory(), evidence_manifest(inputs, documents, measurement.batches)
            )
        except Exception:
            report["failure_codes"].append("partial_document_evidence_failed")
    cleanup_started = time.perf_counter()
    try:
        report["cleanup"] = (
            await runtime.cleanup() if runtime else {"passed": True, "status": "not_started"}
        )
    except Exception:
        report["cleanup"] = {"passed": False, "code": "owned_cleanup_failed"}
    elapsed = time.perf_counter() - cleanup_started
    report["cleanup"]["elapsed_seconds"] = elapsed
    report["cleanup"]["budget_seconds"] = inputs.cleanup_deadline_seconds
    # Safety cleanup is itself composed of bounded operations. Do not abandon an
    # owned child at the aggregate budget boundary; preserve the overrun as failure.
    if elapsed > inputs.cleanup_deadline_seconds:
        report["cleanup"]["passed"] = False
        report["failure_codes"].append("cleanup_budget_exceeded")
    if not report["cleanup"]["passed"]:
        report["failure_codes"].append("services_cleanup_failed")
    for name, callback in (
        ("source", services_source_snapshot),
        ("protection", protection_snapshot),
    ):
        try:
            report[f"{name}_after"] = callback()
            if report[f"{name}_after"] != report.get(f"{name}_before"):
                report["failure_codes"].append(f"{name}_changed")
        except Exception:
            report["failure_codes"].append(f"{name}_verification_failed")
    if "inputs_before" in report:
        try:
            _, report["inputs_after"] = prepare_inputs(inputs, run_id)
            if report["inputs_before"] != report["inputs_after"]:
                report["failure_codes"].append("source_artifact_changed")
        except Exception:
            report["failure_codes"].append("source_artifact_verification_failed")
    if not report["failure_codes"]:
        report["external_services_verified"] = True
        report["status"] = (
            "budget_unqualified"
            if any(
                v["status"] in {"failed", "insufficient_evidence"}
                for v in report.get("budget_verdicts", {}).values()
            )
            else "measurement_only"
        )
    report["finished_at"] = datetime.now(UTC).isoformat()
