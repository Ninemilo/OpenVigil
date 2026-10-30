"""Real numeric observations and malformed inputs; no requests or timing stubs."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from pydantic import ValidationError

from windops_backend.operations.load_contracts import (
    LoadProfile,
    RequestSample,
    ResourceObservation,
    RunIdentity,
    WorkloadBudget,
    compare_runs,
    evaluate_workload,
    nearest_rank,
    parse_load_profile,
    profile_sha256,
    summarize_samples,
)


def budget(**updates: Any) -> WorkloadBudget:
    return WorkloadBudget.model_validate(
        {
            "workload": "queries",
            "p95_ms": 500.0,
            "p99_ms": 1000.0,
            "maximum_error_rate": 0.01,
            "maximum_response_bytes": 512 * 1024,
            "maximum_process_rss_bytes": 1024 * 1024,
            "maximum_total_rss_bytes": 4 * 1024 * 1024,
            **updates,
        }
    )


def profile_data() -> dict[str, Any]:
    return {
        "schema_version": "openvigil.load-profile.v1",
        "profile_id": "local-queries",
        "mode": "software",
        "workloads": ["queries"],
        "concurrency": 4,
        "requests_per_route": 100,
        "maximum_requests": 1000,
        "maximum_duration_seconds": 60.0,
        "request_timeout_seconds": 5.0,
        "warmup_requests": 1,
        "history_rows": 0,
        "documents": 0,
        "maximum_upload_bytes": 0,
        "budgets": [budget().model_dump()],
    }


def sample(sequence: int, elapsed: float = 10.0, **updates: Any) -> RequestSample:
    scheduled = sequence / 100
    started = scheduled + 0.005
    return RequestSample.model_validate(
        {
            "workload": "queries",
            "route_id": "diagnoses",
            "user_id": "operator-a",
            "sequence": sequence,
            "outcome": "success",
            "started": True,
            "scheduled_monotonic": scheduled,
            "started_monotonic": started,
            "finished_monotonic": started + elapsed / 1000,
            "scheduler_delay_ms": 5.0,
            "elapsed_ms": elapsed,
            "response_bytes": 1200,
            "http_status": 200,
            "semantic_valid": True,
            **updates,
        }
    )


def unsent(sequence: int, outcome: str = "not_run") -> RequestSample:
    return RequestSample.model_validate(
        {
            "workload": "queries",
            "route_id": "diagnoses",
            "user_id": "operator-a",
            "sequence": sequence,
            "outcome": outcome,
            "started": False,
            "error_code": "not_started",
        }
    )


def resources(**updates: Any) -> ResourceObservation:
    return ResourceObservation.model_validate(
        {
            "available": True,
            "expected_samples": 10,
            "observed_samples": 10,
            "peak_process_rss_bytes": 400_000,
            "peak_total_rss_bytes": 1_200_000,
            **updates,
        }
    )


def test_profile_json_is_strict_hash_stable_and_does_not_accept_secrets() -> None:
    body = profile_data()
    first = parse_load_profile(json.dumps(body).encode())
    second = parse_load_profile(json.dumps(body, sort_keys=True, indent=2).encode())
    assert isinstance(first.workloads, tuple)
    assert profile_sha256(first) == profile_sha256(second)
    assert first.budgets[0].workload == "queries"
    with pytest.raises(ValidationError):
        first.concurrency = 8
    for forbidden in ("url", "headers", "api_key", "sql", "database_url"):
        with pytest.raises(ValidationError):
            parse_load_profile(json.dumps({**body, forbidden: "rejected"}).encode())
    duplicate = json.dumps(body).replace('"concurrency": 4', '"concurrency": 4, "concurrency": 8')
    with pytest.raises(ValueError, match="duplicate"):
        parse_load_profile(duplicate.encode())
    with pytest.raises(ValueError, match="64 KiB"):
        parse_load_profile(b" " * (64 * 1024 + 1))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "other"},
        {"mode": "production"},
        {"profile_id": "https://remote"},
        {"workloads": ["arbitrary_sql"]},
        {"workloads": ["queries", "queries"]},
        {"concurrency": 0},
        {"concurrency": 33},
        {"concurrency": True},
        {"concurrency": "4"},
        {"requests_per_route": 99},
        {"requests_per_route": 2001},
        {"maximum_requests": 100},
        {"maximum_requests": 20_001},
        {"maximum_duration_seconds": 901.0},
        {"maximum_duration_seconds": math.inf},
        {"request_timeout_seconds": 31.0},
        {"request_timeout_seconds": 0.0},
        {"maximum_duration_seconds": 1.0},
        {"warmup_requests": 21},
        {"warmup_requests": -1},
        {"history_rows": 1_000_001},
        {"documents": 33},
        {"maximum_upload_bytes": 32 * 50 * 1024**2 + 1},
        {"schedule": "fixed_interval"},
        {"arrival_interval_ms": 10.0},
        {"schedule": "fixed_interval", "arrival_interval_ms": math.nan},
        {"budgets": []},
        {"budgets": [budget().model_dump(), budget().model_dump()]},
    ],
)
def test_profile_rejects_unbounded_or_inconsistent_input(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_load_profile(json.dumps({**profile_data(), **changes}).encode())


def test_profiles_bind_every_workload_and_predeclare_real_data_limits() -> None:
    body = profile_data()
    for workload in ("history", "document_batch", "backlog"):
        body.update(workloads=[workload], budgets=[budget(workload=workload).model_dump()])
        with pytest.raises(ValidationError):
            parse_load_profile(json.dumps(body).encode())
        valid = {**body, "history_rows": 1000, "documents": 4, "maximum_upload_bytes": 1_000_000}
        assert parse_load_profile(json.dumps(valid).encode()).workloads == (workload,)
    fixed = parse_load_profile(
        json.dumps(
            {**profile_data(), "schedule": "fixed_interval", "arrival_interval_ms": 20.0}
        ).encode()
    )
    assert fixed.arrival_interval_ms == 20.0
    with pytest.raises(ValidationError):
        parse_load_profile(
            json.dumps(
                {**profile_data(), "budgets": [budget(workload="history").model_dump()]}
            ).encode()
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"p95_ms": math.nan},
        {"p99_ms": 499.0},
        {"maximum_error_rate": -0.1},
        {"maximum_error_rate": 1.1},
        {"maximum_response_bytes": 0},
        {"maximum_process_rss_bytes": True},
        {"maximum_total_rss_bytes": 1},
        {"measurement_only": "false"},
    ],
)
def test_budgets_cannot_be_nonfinite_coerced_or_inverted(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        budget(**changes)


def test_nearest_rank_uses_every_observation_and_rejects_invalid_numbers() -> None:
    values = [float(number) for number in range(100, 0, -1)]
    assert nearest_rank(values, 0.5) == 50
    assert nearest_rank(values, 0.95) == 95
    assert nearest_rank(values, 0.99) == 99
    assert nearest_rank([0.0], 1) == 0
    assert values[0] == 100  # Sorting must not mutate the ledger.
    for invalid in ([], [math.nan], [math.inf], [-1.0], [True], [0.0] * 20_001):
        with pytest.raises(ValueError):
            nearest_rank(invalid, 0.95)
    for fraction in (0.0, 1.01, math.inf, math.nan, True):
        with pytest.raises(ValueError):
            nearest_rank([1.0], fraction)


def test_failure_latencies_are_not_discarded_and_success_latency_is_separate() -> None:
    ledger = [sample(index, float(index + 1)) for index in range(90)]
    ledger += [
        sample(
            index,
            2000.0,
            outcome="failure",
            http_status=503,
            semantic_valid=False,
            error_code="http_status",
        )
        for index in range(90, 100)
    ]
    result = summarize_samples(ledger, planned=100)
    assert result.success == 90 and result.failure == 10
    assert result.started == result.completed == result.measured_latencies == 100
    assert result.error_rate == 0.1
    assert result.p95_ms == result.p99_ms == result.maximum_ms == 2000
    assert result.success_p95_ms == 86
    verdict = evaluate_workload(result, budget(), resources())
    assert verdict.status == "failed" and not verdict.qualified
    assert set(verdict.reason_codes) == {"p95_exceeded", "p99_exceeded", "error_rate_exceeded"}


def test_accounting_retains_missing_unsent_cancelled_and_timeout_work() -> None:
    ledger = [
        sample(0),
        sample(
            1,
            outcome="timeout",
            http_status=None,
            response_bytes=None,
            semantic_valid=False,
            error_code="request_timeout",
        ),
        sample(
            2,
            outcome="cancelled",
            http_status=None,
            response_bytes=None,
            semantic_valid=False,
            error_code="request_cancelled",
        ),
        unsent(3, "dropped"),
        unsent(4, "cancelled"),
        unsent(5),
    ]
    summary = summarize_samples(ledger, planned=8)
    assert (summary.planned, summary.recorded, summary.missing_samples) == (8, 6, 2)
    assert (summary.started, summary.completed, summary.success, summary.failure) == (3, 1, 1, 0)
    assert (summary.timeout, summary.dropped, summary.cancelled, summary.not_run) == (1, 1, 2, 3)
    assert summary.measured_latencies == 3 and summary.error_rate == 7 / 8
    assert "sample_count_mismatch" in summary.reason_codes
    verdict = evaluate_workload(summary, budget(maximum_error_rate=1.0), resources())
    assert not verdict.qualified
    assert {"incomplete_execution", "requests_dropped", "insufficient_tail_samples"}.issubset(
        verdict.reason_codes
    )


def test_no_requests_small_tail_or_unavailable_resources_cannot_pass() -> None:
    for size in (0, 1, 99):
        summary = summarize_samples([sample(index) for index in range(size)], planned=size)
        result = evaluate_workload(summary, budget(), resources())
        assert result.status == "insufficient_evidence" and not result.qualified
    summary = summarize_samples([sample(index) for index in range(100)], planned=100)
    assert evaluate_workload(summary, budget(), resources()).qualified
    for observation in (
        resources(
            available=False,
            observed_samples=0,
            peak_process_rss_bytes=None,
            peak_total_rss_bytes=None,
        ),
        resources(observed_samples=9),
    ):
        result = evaluate_workload(summary, budget(), observation)
        assert result.status == "insufficient_evidence" and not result.qualified
    measured = evaluate_workload(summary, budget(measurement_only=True), resources())
    assert measured.status == "measurement_only" and not measured.qualified


def test_warmup_failure_duplicate_or_extra_samples_cannot_pass() -> None:
    ledger = [sample(index) for index in range(100)]
    warmup = sample(
        0,
        phase="warmup",
        outcome="failure",
        http_status=500,
        semantic_valid=False,
        error_code="http_status",
    )
    summary = summarize_samples([warmup, *ledger], planned=100)
    assert summary.warmup == 1 and summary.success == 100 and summary.error_rate == 0
    assert evaluate_workload(summary, budget(), resources()).reason_codes == ("warmup_failed",)
    duplicate = summarize_samples([*ledger[:-1], ledger[0]], planned=100)
    assert (
        "duplicate_sample_identity"
        in evaluate_workload(duplicate, budget(), resources()).reason_codes
    )
    excess = summarize_samples(ledger, planned=99)
    assert "sample_count_mismatch" in evaluate_workload(excess, budget(), resources()).reason_codes
    for invalid in (True, 1.5, -1, 20_001):
        with pytest.raises(ValueError):
            summarize_samples(ledger, planned=invalid)


def test_observed_overruns_remain_in_the_failure_ledger() -> None:
    overrun = sample(
        0,
        elapsed=900_001.0,
        outcome="timeout",
        http_status=None,
        response_bytes=None,
        semantic_valid=False,
        error_code="run_deadline_exceeded",
    )
    summary = summarize_samples([overrun], planned=1)
    assert summary.timeout == summary.measured_latencies == 1
    assert summary.p99_ms == 900_001.0
    assert not evaluate_workload(summary, budget(), resources()).qualified


@pytest.mark.parametrize(
    "changes",
    [
        {"started": False},
        {"http_status": 503},
        {"http_status": None},
        {"semantic_valid": False},
        {"response_bytes": None},
        {"elapsed_ms": None},
        {"elapsed_ms": math.nan},
        {"started_monotonic": None},
        {"scheduled_monotonic": None},
        {"finished_monotonic": -1.0},
        {"finished_monotonic": 10.0},
        {"scheduler_delay_ms": 400.0},
        {"user_id": "Bearer secret"},
        {"route_id": "https://remote"},
        {"outcome": "failure", "error_code": "raw exception with a secret"},
    ],
)
def test_samples_reject_false_success_and_inconsistent_timing(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        sample(1, **changes)


def test_unsent_has_no_fake_time_and_resource_zero_is_not_availability() -> None:
    assert unsent(1).scheduled_monotonic is None
    for key, value in (
        ("elapsed_ms", 0.0),
        ("http_status", 200),
        ("response_bytes", 0),
        ("started_monotonic", 0.0),
        ("finished_monotonic", 0.0),
    ):
        with pytest.raises(ValidationError):
            RequestSample.model_validate({**unsent(1).model_dump(), key: value})
    for changes in (
        {"observed_samples": 11},
        {"observed_samples": 0},
        {"peak_process_rss_bytes": 0},
        {"peak_total_rss_bytes": 1},
    ):
        with pytest.raises(ValidationError):
            resources(**changes)


def test_response_and_resource_budgets_apply_even_with_fast_http() -> None:
    summary = summarize_samples(
        [sample(index, response_bytes=600_000) for index in range(100)], planned=100
    )
    verdict = evaluate_workload(
        summary,
        budget(),
        resources(peak_process_rss_bytes=2_000_000, peak_total_rss_bytes=5_000_000),
    )
    assert set(verdict.reason_codes) == {
        "response_bytes_exceeded",
        "process_rss_exceeded",
        "total_rss_exceeded",
    }
    assert not verdict.qualified
    wrong = evaluate_workload(summary, budget(workload="history"), resources())
    assert "workload_budget_mismatch" in wrong.reason_codes


def test_comparison_ignores_only_candidate_source_and_not_runtime_or_dataset() -> None:
    fields = {
        name: "a" * 64
        for name in (
            "profile_sha256",
            "dataset_sha256",
            "runtime_sha256",
            "environment_sha256",
            "source_sha256",
        )
    }
    before = RunIdentity(**fields, cache_policy="warm_process")
    for name in (*fields, "cache_policy"):
        changed = before.model_dump()
        changed[name] = "cold_process" if name == "cache_policy" else "b" * 64
        result = compare_runs(before, RunIdentity.model_validate(changed))
        assert result.comparable is (name == "source_sha256")
        assert result.reason_codes == (() if name == "source_sha256" else (f"{name}_mismatch",))


def test_valid_full_profile_has_no_arbitrary_endpoints_or_credentials() -> None:
    data = profile_data()
    data.update(
        mode="services",
        workloads=["queries", "history", "document_batch", "backlog"],
        history_rows=1_000_000,
        documents=32,
        maximum_upload_bytes=32 * 50 * 1024**2,
        maximum_requests=20_000,
        requests_per_route=2000,
        concurrency=32,
        maximum_duration_seconds=900.0,
        request_timeout_seconds=30.0,
        budgets=[
            budget(workload=workload).model_dump()
            for workload in ("queries", "history", "document_batch", "backlog")
        ],
    )
    validated = parse_load_profile(json.dumps(data).encode())
    assert isinstance(validated, LoadProfile)
    assert len(validated.budgets) == 4
