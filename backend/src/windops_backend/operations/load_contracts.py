"""Bounded performance profiles and honest accounting, without network or secrets."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Workload = Literal["queries", "history", "document_batch", "backlog"]
Outcome = Literal["success", "failure", "timeout", "dropped", "cancelled", "not_run"]
ErrorCode = Literal[
    "http_status",
    "json_invalid",
    "semantic_invalid",
    "semantic_validator_error",
    "request_timeout",
    "response_too_large",
    "transport_error",
    "request_cancelled",
    "run_deadline_exceeded",
    "arrival_overload",
    "client_setup_failed",
    "client_cleanup_failed",
    "resource_limit",
    "not_started",
    "runner_error",
]
SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
# Profiles bound scheduled work; observations retain overruns rather than dropping them.
Milliseconds = Annotated[float, Field(ge=0)]
MAX_REQUESTS = 20_000
MIN_TAIL_SAMPLES = 100
MIB = 1024 * 1024


class LoadContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class WorkloadBudget(LoadContract):
    workload: Workload
    p95_ms: float = Field(gt=0, le=900_000)
    p99_ms: float = Field(gt=0, le=900_000)
    maximum_error_rate: float = Field(ge=0, le=1)
    maximum_response_bytes: int = Field(gt=0, le=64 * MIB)
    maximum_process_rss_bytes: int = Field(gt=0, le=64 * 1024 * MIB)
    maximum_total_rss_bytes: int = Field(gt=0, le=256 * 1024 * MIB)
    measurement_only: bool = False

    @model_validator(mode="after")
    def ordered_limits(self) -> Self:
        if self.p99_ms < self.p95_ms:
            raise ValueError("P99 budget cannot be below P95")
        if self.maximum_total_rss_bytes < self.maximum_process_rss_bytes:
            raise ValueError("total RSS budget cannot be below a process RSS budget")
        return self


class LoadProfile(LoadContract):
    schema_version: Literal["openvigil.load-profile.v1"]
    profile_id: SafeId
    mode: Literal["software", "services"]
    workloads: tuple[Workload, ...] = Field(min_length=1, max_length=4)
    concurrency: int = Field(ge=1, le=32)
    requests_per_route: int = Field(ge=100, le=2000)
    maximum_requests: int = Field(ge=1, le=MAX_REQUESTS)
    maximum_duration_seconds: float = Field(gt=0, le=900)
    request_timeout_seconds: float = Field(gt=0, le=30)
    warmup_requests: int = Field(ge=0, le=20)
    schedule: Literal["closed_loop", "fixed_interval"] = "closed_loop"
    arrival_interval_ms: float | None = Field(default=None, ge=1, le=30_000)
    history_rows: int = Field(ge=0, le=1_000_000)
    documents: int = Field(ge=0, le=32)
    maximum_upload_bytes: int = Field(ge=0, le=32 * 50 * MIB)
    budgets: tuple[WorkloadBudget, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def consistent_profile(self) -> Self:
        if len(set(self.workloads)) != len(self.workloads):
            raise ValueError("workloads must be unique")
        budget_workloads = [budget.workload for budget in self.budgets]
        if len(set(budget_workloads)) != len(budget_workloads) or set(budget_workloads) != set(
            self.workloads
        ):
            raise ValueError("each selected workload requires exactly one registered budget")
        if (
            self.maximum_requests
            < len(self.workloads) * self.requests_per_route + self.warmup_requests
        ):
            raise ValueError("request cap cannot cover the minimum selected workload plan")
        if self.request_timeout_seconds > self.maximum_duration_seconds:
            raise ValueError("request timeout exceeds the complete run budget")
        if (self.schedule == "fixed_interval") != (self.arrival_interval_ms is not None):
            raise ValueError("only fixed-interval scheduling requires an arrival interval")
        if "history" in self.workloads and self.history_rows == 0:
            raise ValueError("history workload requires declared source rows")
        if {"document_batch", "backlog"}.intersection(self.workloads) and (
            self.documents == 0 or self.maximum_upload_bytes == 0
        ):
            raise ValueError("document workloads require bounded documents and upload bytes")
        return self


def parse_load_profile(raw: bytes) -> LoadProfile:
    """A small JSON-only input boundary; duplicate keys must not rewrite a budget."""
    if len(raw) > 64 * 1024:
        raise ValueError("load profile exceeds 64 KiB")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate load profile field")
            result[key] = value
        return result

    json.loads(raw, object_pairs_hook=unique_pairs)
    # JSON arrays are accepted as immutable tuples; Python coercion remains disabled.
    return LoadProfile.model_validate_json(raw)


def profile_sha256(profile: LoadProfile) -> str:
    canonical = json.dumps(
        profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RequestSample(LoadContract):
    workload: Workload
    route_id: SafeId
    user_id: SafeId
    sequence: int = Field(ge=0, le=MAX_REQUESTS)
    phase: Literal["warmup", "measured"] = "measured"
    outcome: Outcome
    started: bool
    scheduled_monotonic: float | None = Field(default=None, ge=0)
    started_monotonic: float | None = Field(default=None, ge=0)
    finished_monotonic: float | None = Field(default=None, ge=0)
    scheduler_delay_ms: Milliseconds | None = None
    elapsed_ms: Milliseconds | None = None
    response_bytes: int | None = Field(default=None, ge=0, le=1024 * MIB)
    http_status: int | None = Field(default=None, ge=100, le=599)
    semantic_valid: bool = False
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def honest_outcome(self) -> Self:
        if self.outcome in {"success", "failure", "timeout"} and not self.started:
            raise ValueError("completed attempts must have started")
        if self.outcome in {"dropped", "not_run"} and self.started:
            raise ValueError("unsent work cannot be a started attempt")
        if not self.started and (
            self.elapsed_ms is not None
            or self.response_bytes is not None
            or self.http_status is not None
            or self.started_monotonic is not None
            or self.finished_monotonic is not None
        ):
            raise ValueError("unsent work cannot report HTTP measurements")
        if self.started:
            if (
                self.scheduled_monotonic is None
                or self.started_monotonic is None
                or self.finished_monotonic is None
                or self.elapsed_ms is None
                or self.scheduler_delay_ms is None
                or self.started_monotonic < self.scheduled_monotonic
                or self.finished_monotonic < self.started_monotonic
                or abs((self.finished_monotonic - self.started_monotonic) * 1000 - self.elapsed_ms)
                > 0.1
                or abs(
                    (self.started_monotonic - self.scheduled_monotonic) * 1000
                    - self.scheduler_delay_ms
                )
                > 0.1
            ):
                raise ValueError("started attempt timestamps and measured durations must agree")
        if self.outcome == "success":
            if (
                self.elapsed_ms is None
                or self.response_bytes is None
                or self.http_status is None
                or not 200 <= self.http_status < 300
                or not self.semantic_valid
                or self.error_code is not None
            ):
                raise ValueError("success requires measured, semantically valid HTTP success")
        elif self.error_code is None:
            raise ValueError("non-success requires a fixed safe failure code")
        if self.outcome == "timeout" and self.http_status is not None:
            raise ValueError("a timed-out attempt cannot claim a completed HTTP response")
        return self


class ResourceObservation(LoadContract):
    available: bool
    expected_samples: int = Field(ge=0, le=100_000)
    observed_samples: int = Field(ge=0, le=100_000)
    peak_process_rss_bytes: int | None = Field(default=None, gt=0)
    peak_total_rss_bytes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def consistent_observation(self) -> Self:
        if self.observed_samples > self.expected_samples:
            raise ValueError("observed resource samples exceed the sampling plan")
        if self.available and (
            self.observed_samples == 0
            or self.peak_process_rss_bytes is None
            or self.peak_total_rss_bytes is None
        ):
            raise ValueError("available resources require actual RSS observations")
        if (
            self.peak_process_rss_bytes is not None
            and self.peak_total_rss_bytes is not None
            and self.peak_process_rss_bytes > self.peak_total_rss_bytes
        ):
            raise ValueError("total RSS peak cannot be below the largest process peak")
        return self


class RequestSummary(LoadContract):
    workloads: tuple[Workload, ...]
    planned: int
    recorded: int
    warmup: int
    started: int
    completed: int  # Success/failure outcomes, excluding timeouts and cancellations.
    success: int
    failure: int
    timeout: int
    dropped: int
    cancelled: int
    not_run: int
    missing_samples: int
    measured_latencies: int
    unmeasured_started: int
    error_rate: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    maximum_ms: float | None
    success_p95_ms: float | None
    maximum_response_bytes: int | None
    reason_codes: tuple[str, ...]
    percentile_method: Literal["nearest_rank"] = "nearest_rank"


def nearest_rank(values: Sequence[float], fraction: float) -> float:
    if not values or len(values) > MAX_REQUESTS:
        raise ValueError("percentiles require 1..20000 observations")
    if isinstance(fraction, bool) or not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError("percentile fraction must be finite in (0, 1]")
    if any(isinstance(value, bool) or not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("latencies must be finite nonnegative numbers")
    return float(sorted(values)[math.ceil(len(values) * fraction) - 1])


def summarize_samples(samples: Sequence[RequestSample], *, planned: int) -> RequestSummary:
    if type(planned) is not int or not 0 <= planned <= MAX_REQUESTS:
        raise ValueError("planned measured requests must be between 0 and 20000")
    if len(samples) > MAX_REQUESTS:
        raise ValueError("sample ledger exceeds the total request bound")
    measured = [sample for sample in samples if sample.phase == "measured"]
    counts = {
        outcome: sum(sample.outcome == outcome for sample in measured)
        for outcome in ("success", "failure", "timeout", "dropped", "cancelled", "not_run")
    }
    missing = max(0, planned - len(measured))
    counts["not_run"] += missing
    durations = [sample.elapsed_ms for sample in measured if sample.elapsed_ms is not None]
    success_durations = [
        sample.elapsed_ms
        for sample in measured
        if sample.outcome == "success" and sample.elapsed_ms is not None
    ]
    response_bytes = [
        sample.response_bytes for sample in measured if sample.response_bytes is not None
    ]
    identities = {
        (sample.workload, sample.route_id, sample.user_id, sample.phase, sample.sequence)
        for sample in samples
    }
    reasons = []
    if len(measured) != planned:
        reasons.append("sample_count_mismatch")
    if len(identities) != len(samples):
        reasons.append("duplicate_sample_identity")
    if any(sample.outcome != "success" for sample in samples if sample.phase == "warmup"):
        reasons.append("warmup_failed")
    non_success = sum(
        counts[name] for name in ("failure", "timeout", "dropped", "cancelled", "not_run")
    )
    return RequestSummary(
        workloads=tuple(sorted({sample.workload for sample in measured})),
        planned=planned,
        recorded=len(measured),
        warmup=len(samples) - len(measured),
        started=sum(sample.started for sample in measured),
        completed=counts["success"] + counts["failure"],
        success=counts["success"],
        failure=counts["failure"],
        timeout=counts["timeout"],
        dropped=counts["dropped"],
        cancelled=counts["cancelled"],
        not_run=counts["not_run"],
        missing_samples=missing,
        measured_latencies=len(durations),
        unmeasured_started=sum(sample.started and sample.elapsed_ms is None for sample in measured),
        error_rate=min(1.0, non_success / planned) if planned else None,
        p50_ms=nearest_rank(durations, 0.5) if durations else None,
        p95_ms=nearest_rank(durations, 0.95) if durations else None,
        p99_ms=nearest_rank(durations, 0.99) if durations else None,
        maximum_ms=max(durations) if durations else None,
        success_p95_ms=nearest_rank(success_durations, 0.95) if success_durations else None,
        maximum_response_bytes=max(response_bytes) if response_bytes else None,
        reason_codes=tuple(reasons),
    )


class BudgetVerdict(LoadContract):
    status: Literal["passed", "failed", "insufficient_evidence", "measurement_only"]
    qualified: bool
    reason_codes: tuple[str, ...]


def evaluate_workload(
    summary: RequestSummary, budget: WorkloadBudget, resources: ResourceObservation
) -> BudgetVerdict:
    failures = list(summary.reason_codes)
    missing = []
    if summary.workloads and summary.workloads != (budget.workload,):
        failures.append("workload_budget_mismatch")
    if summary.dropped:
        failures.append("requests_dropped")
    if summary.cancelled or summary.not_run:
        failures.append("incomplete_execution")
    if summary.planned == 0 or summary.success == 0:
        missing.append("no_successful_requests")
    if summary.measured_latencies < MIN_TAIL_SAMPLES:
        missing.append("insufficient_tail_samples")
    if any(
        value is None
        for value in (
            summary.p95_ms,
            summary.p99_ms,
            summary.error_rate,
            summary.maximum_response_bytes,
        )
    ):
        missing.append("missing_request_measurements")
    if summary.unmeasured_started:
        missing.append("missing_attempt_latency")
    if not resources.available:
        missing.append("resources_unavailable")
    if resources.expected_samples == 0 or resources.observed_samples != resources.expected_samples:
        missing.append("resource_samples_incomplete")
    for value, maximum, code in (
        (summary.p95_ms, budget.p95_ms, "p95_exceeded"),
        (summary.p99_ms, budget.p99_ms, "p99_exceeded"),
        (summary.error_rate, budget.maximum_error_rate, "error_rate_exceeded"),
        (summary.maximum_response_bytes, budget.maximum_response_bytes, "response_bytes_exceeded"),
        (
            resources.peak_process_rss_bytes,
            budget.maximum_process_rss_bytes,
            "process_rss_exceeded",
        ),
        (resources.peak_total_rss_bytes, budget.maximum_total_rss_bytes, "total_rss_exceeded"),
    ):
        if value is not None and value > maximum:
            failures.append(code)
    if budget.measurement_only:
        return BudgetVerdict(
            status="measurement_only",
            qualified=False,
            reason_codes=tuple(["measurement_only", *failures, *missing]),
        )
    if failures:
        return BudgetVerdict(
            status="failed", qualified=False, reason_codes=tuple([*failures, *missing])
        )
    if missing:
        return BudgetVerdict(
            status="insufficient_evidence", qualified=False, reason_codes=tuple(missing)
        )
    return BudgetVerdict(status="passed", qualified=True, reason_codes=())


class RunIdentity(LoadContract):
    profile_sha256: Sha256
    dataset_sha256: Sha256
    runtime_sha256: (
        Sha256  # Dependencies/settings/transport; exclude the candidate source revision.
    )
    environment_sha256: Sha256
    cache_policy: SafeId
    source_sha256: Sha256


class ComparisonResult(LoadContract):
    comparable: bool
    reason_codes: tuple[str, ...]


def compare_runs(before: RunIdentity, after: RunIdentity) -> ComparisonResult:
    reasons = tuple(
        f"{field}_mismatch"
        for field in (
            "profile_sha256",
            "dataset_sha256",
            "runtime_sha256",
            "environment_sha256",
            "cache_policy",
        )
        if getattr(before, field) != getattr(after, field)
    )
    return ComparisonResult(comparable=not reasons, reason_codes=reasons)
