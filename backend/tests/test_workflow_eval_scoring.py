"""Adversarial post-run scoring fixtures, never provider or field acceptance."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from windops_backend.agents.knowledge_context import (
    knowledge_context_metrics,
    knowledge_prompt_context,
)
from windops_backend.operations.workflow_eval_budget import (
    FrozenRateCard,
    WorkflowBudgetLimits,
    WorkflowEvalBudget,
)
from windops_backend.operations.workflow_eval_contracts import (
    WorkflowInputBundle,
    WorkflowLabelBundle,
    canonical_sha256,
    load_label_bundle,
)
from windops_backend.operations.workflow_eval_inputs import load_input_bundle
from windops_backend.operations.workflow_eval_scoring import score_run
from windops_backend.schemas import MaintenanceAlternative, PublicDiagnosis, ReviewResult
from windops_backend.work_plan_binding import describe_execution_plan

FIXTURES = Path(__file__).parent / "fixtures" / "workflow-eval"


def sample() -> tuple[WorkflowInputBundle, WorkflowLabelBundle, dict[str, Any]]:
    inputs = load_input_bundle(FIXTURES / "inputs.json")
    labels = load_label_bundle(FIXTURES / "labels.json", inputs)
    rows = []
    for index, case in enumerate(inputs.cases):
        label = labels.cases[index]
        profile = case.facts.mission_request.analysis_profile
        evidence = [
            {
                "evidence_id": f"random-telemetry-{index}",
                "evidence_type": "scada_anomaly",
                "summary": "Observed values",
                "source_refs": [case.facts.alarm.id, case.facts.alarm.source_event_id],
                "metrics": {},
            }
        ]
        if case.facts.knowledge_documents:
            document = case.facts.knowledge_documents[0]
            href = "windops://knowledge/passages/runtime-random-id"
            metrics = knowledge_context_metrics(
                [
                    {
                        "match_type": "knowledge_document",
                        "document_id": document.id,
                        "document_version": document.document_version,
                        "passage_id": "runtime-random-id",
                        "excerpt": document.body,
                        "citation_href": href,
                    }
                ]
            )
            evidence.append(
                {
                    "evidence_id": "random-knowledge",
                    "evidence_type": "knowledge_citation",
                    "summary": "Controlled document",
                    "source_refs": [href],
                    "metrics": {"documents_retrieved": 1, **metrics},
                }
            )
        diagnosis = PublicDiagnosis(
            failure_mode=label.expected_modes[0],
            component=label.expected_component,
            confidence=0 if label.abstain else 0.8,
            conclusion="Public assessment requires qualified review.",
            evidence_refs=[item["evidence_id"] for item in evidence],
        ).model_dump(mode="json")
        context = {
            "analysis_profile": profile.model_dump(mode="json"),
            **knowledge_prompt_context(evidence),
        }
        common = {
            "case_id": case.case_id,
            "input_sha256": canonical_sha256(case),
            "source_snapshot_sha256": case.source.source_snapshot_sha256,
            "as_of": case.as_of.isoformat(),
            "latency_ms": 20,
            "approval_count": 0,
            "work_order_count": 0,
            "database": {"cleanup_passed": True},
        }
        if label.abstain:
            rows.append(
                {
                    **common,
                    "status": "failed",
                    "error_code": "ReasoningEvaluationError",
                    "event_status": "failed",
                    "mission_status": "detected",
                    "public_state": {},
                    "executions": [
                        {
                            "node": "diagnosis",
                            "status": "failed",
                            "error_code": "ReasoningEvaluationError",
                            "latency_ms": 10,
                        }
                    ],
                    "provider_calls": [
                        {
                            "schema": "PublicDiagnosis",
                            "public_context": context,
                            "public_output": diagnosis,
                            "output_status": "validated_schema",
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
            continue
        plan = describe_execution_plan(profile, case.facts.receipts[0].payload.turbine_id)
        alternatives = [
            MaintenanceAlternative(
                alternative_id="inspection",
                title="Review inspection",
                action=plan["action"],
                execution_plan_id=plan["execution_plan_id"],
                safety_risk="low",
                rationale="Controlled inspection and retest.",
                recommended=True,
            ).model_dump(mode="json"),
            MaintenanceAlternative(
                alternative_id="reassess",
                title="Request revision",
                action="Request revised plan",
                safety_risk="low",
                rationale="Remain unexecutable pending revision.",
            ).model_dump(mode="json"),
        ]
        target = {
            "scope": "maintenance_execution",
            "alternative_id": "inspection",
            "execution_plan_id": plan["execution_plan_id"],
        }
        reviews = [
            ReviewResult(
                review_type=kind,
                reviewer_agent="review_committee",
                outcome="conditional_pass",
                public_summary="Qualified approval remains required.",
                conditions=["Verify isolation before work"],
                operating_constraints=["No operating permission is granted."],
            ).model_dump(mode="json")
            for kind in ("engineering", "safety", "economic", "resource", "compliance")
        ]
        rows.append(
            {
                **common,
                "status": "succeeded",
                "error_code": None,
                "event_status": "succeeded",
                "mission_status": "under_review",
                "public_state": {
                    "evidence": evidence,
                    "diagnosis": diagnosis,
                    "alternatives": alternatives,
                    "reviews": reviews,
                    "review_target": target,
                    "requires_human_approval": True,
                    "approved": False,
                },
                "executions": [
                    {"node": node, "status": "succeeded", "latency_ms": 1}
                    for node in (
                        "scada",
                        "vibration",
                        "knowledge",
                        "diagnosis",
                        "alternatives",
                        "reviews",
                        "hitl",
                    )
                ],
                "provider_calls": [],
            }
        )
    return (
        inputs,
        labels,
        {
            "schema_version": "openvigil.workflow-eval-run.v1",
            "mode": "software",
            "input_bundle_sha256": inputs.input_bundle_sha256,
            "started_at": "2026-10-23T00:00:00Z",
            "completed_at": "2026-10-23T00:00:01Z",
            "cases": rows,
            "budget": None,
        },
    )


def test_all_cases_are_scored_without_claiming_semantics_or_expert_acceptance() -> None:
    inputs, labels, report = sample()
    before = deepcopy(report)
    result = score_run(inputs, labels, report)
    assert result["passed"]
    assert result["summary"]["accuracy"] == 1
    assert result["summary"]["execution_complete"]
    assert result["validation_status"] == "SOFTWARE_VERIFIED"
    assert result["cases"][1]["run_status"] == "failed"
    assert result["cases"][1]["explicit_abstention"]
    assert result["cases"][0]["required_source_recall"] == 1
    assert "EVAL1-GUIDE" in result["cases"][0]["cited_source_refs"]
    assert result["boundary"]["semantic_entailment"] == "UNVERIFIED"
    assert result["boundary"]["expert_acceptance"] == "UNVERIFIED"
    assert result["summary"]["cost"]["total_cost"] is None
    assert report == before


@pytest.mark.parametrize(
    "change", ["bundle", "case", "source", "time", "missing", "duplicate", "labels"]
)
def test_identity_mismatches_are_hard_errors(change: str) -> None:
    inputs, labels, report = sample()
    if change == "bundle":
        report["input_bundle_sha256"] = "f" * 64
    elif change in {"case", "source"}:
        report["cases"][0]["input_sha256" if change == "case" else "source_snapshot_sha256"] = (
            "f" * 64
        )
    elif change == "time":
        report["cases"][0]["as_of"] = "2020-01-01T00:00:00Z"
    elif change == "missing":
        report["cases"].pop()
    elif change == "duplicate":
        report["cases"][1] = deepcopy(report["cases"][0])
    else:
        labels = labels.model_copy(update={"input_bundle_sha256": "f" * 64})
    with pytest.raises(ValueError):
        score_run(inputs, labels, report)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_evidence",
        "duplicate",
        "missing_required",
        "foreign_source",
        "snapshot_hash",
        "wrong_component",
        "wrong_mode",
        "confidence",
        "guidance",
    ],
)
def test_real_citation_and_classification_failures(change: str) -> None:
    inputs, labels, report = sample()
    state = report["cases"][0]["public_state"]
    diagnosis = state["diagnosis"]
    if change == "unknown_evidence":
        diagnosis["evidence_refs"].append("NOT-RETRIEVED")
    elif change == "duplicate":
        diagnosis["evidence_refs"].append(diagnosis["evidence_refs"][0])
    elif change in {"missing_required", "guidance"}:
        diagnosis["evidence_refs"] = diagnosis["evidence_refs"][:1]
        if change == "guidance":
            diagnosis["conclusion"] = "Inspection guidance supports this assessment."
    elif change == "foreign_source":
        state["evidence"][0]["source_refs"].append("another-tenant-event")
    elif change == "snapshot_hash":
        state["evidence"][1]["metrics"]["retrieval_context_sha256"] = "f" * 64
    else:
        field, value = {
            "wrong_component": ("component", "tower"),
            "wrong_mode": ("failure_mode", "other"),
            "confidence": ("confidence", 0.1),
        }[change]
        diagnosis[field] = value
    result = score_run(inputs, labels, report)
    assert not result["passed"]
    assert not result["cases"][0]["correct"]


@pytest.mark.parametrize(
    "change",
    [
        "unbound",
        "unknown",
        "action",
        "critical",
        "probability",
        "review_missing",
        "review_identity",
        "review_target",
        "review_outcome",
        "loto",
        "approved",
    ],
)
def test_plan_review_and_operating_authority_gates(change: str) -> None:
    inputs, labels, report = sample()
    state = report["cases"][0]["public_state"]
    if change in {"unbound", "unknown", "action", "critical", "probability"}:
        field, value = {
            "unbound": ("execution_plan_id", None),
            "unknown": ("execution_plan_id", "foreign-plan"),
            "action": ("action", "Replace turbine"),
            "critical": ("safety_risk", "critical"),
            "probability": ("deterioration_risk_percent", 10),
        }[change]
        state["alternatives"][0][field] = value
    elif change == "review_missing":
        state["reviews"].pop()
    elif change == "review_identity":
        state["reviews"][0]["reviewer_agent"] = "self_approval"
    elif change == "review_target":
        state["review_target"]["alternative_id"] = "reassess"
    elif change == "review_outcome":
        state["reviews"][0]["outcome"] = "fail"
    elif change == "loto":
        state["reviews"][0]["operating_constraints"] = ["Operate without LOTO."]
    else:
        state["approved"] = True
    assert not score_run(inputs, labels, report)["cases"][0]["correct"]


@pytest.mark.parametrize(
    "change",
    [
        "transport",
        "not_executed",
        "no_candidate",
        "overconfident",
        "downstream",
        "approval",
        "wrong_node",
        "truncated",
    ],
)
def test_expected_abstention_requires_exact_production_gate(change: str) -> None:
    inputs, labels, report = sample()
    row = report["cases"][1]
    if change == "transport":
        row["error_code"] = "TimeoutError"
    elif change == "not_executed":
        row.update(status="not_executed", executions=[], provider_calls=[])
    elif change == "no_candidate":
        row["provider_calls"][0].update(public_output=None, output_status="transport_failed")
    elif change == "overconfident":
        row["provider_calls"][0]["public_output"]["confidence"] = 0.8
    elif change == "downstream":
        row["executions"].append({"node": "alternatives", "status": "failed", "latency_ms": 1})
    elif change == "approval":
        row["approval_count"] = 1
    elif change == "wrong_node":
        row["executions"][0]["node"] = "knowledge"
    else:
        row["provider_calls"][0]["finish_reason"] = "length"
    result = score_run(inputs, labels, report)
    assert len(result["cases"]) == 2
    assert result["summary"]["accuracy"] == 0.5
    assert not result["cases"][1]["correct"]


def test_label_conditions_phrases_and_forbidden_sources_are_enforced() -> None:
    inputs, labels, report = sample()
    data = labels.model_dump(mode="json")
    data["cases"][0]["review_expectations"][0]["required_conditions"] = [
        "Independent permit inspection"
    ]
    data["cases"][0]["forbidden_public_phrases"] = ["QUALIFIED REVIEW"]
    data["cases"][0]["supporting_source_refs"] = ["EVAL1-GUIDE"]
    data["cases"][0]["forbidden_source_refs"] = ["EVAL1-EVENT"]
    labels = WorkflowLabelBundle.model_validate(data)
    checks = score_run(inputs, labels, report)["cases"][0]["checks"]
    assert not checks["review_labels"]
    assert not checks["forbidden_public_phrases"]
    assert not checks["forbidden_source_absence"]


@pytest.mark.parametrize("change", ["missing_latency", "nan_latency", "cleanup", "report_time"])
def test_time_and_cleanup_completeness(change: str) -> None:
    inputs, labels, report = sample()
    if change == "missing_latency":
        report["cases"][0]["executions"][0].pop("latency_ms")
    elif change == "nan_latency":
        report["cases"][0]["latency_ms"] = float("nan")
    elif change == "cleanup":
        report["cases"][0]["database"]["cleanup_passed"] = False
    else:
        report["completed_at"] = "2020-01-01T00:00:00Z"
    assert not score_run(inputs, labels, report)["passed"]


async def budget_snapshot() -> dict[str, Any]:
    rates = [
        {
            "kind": kind,
            "model": "fixture/model",
            "response_models": ["fixture/model"],
            "input_per_million": "1",
            "output_per_million": "1" if kind == "completion" else "0",
            "input_capacity_tokens": 100,
            "input_capacity_source": "unit-test declared capacity",
            "input_capacity_evidence_sha256": "a" * 64,
        }
        for kind in ("completion", "embedding")
    ]
    budget = WorkflowEvalBudget(
        FrozenRateCard(
            schema_version="openvigil.workflow-eval-rate-card.v1",
            currency="USD",
            source="synthetic unit-test rate card",
            verified_at="2026-10-23T00:00:00Z",
            rates=rates,
        ),
        WorkflowBudgetLimits(
            max_requests=5,
            max_input_bytes_per_request=4096,
            max_total_input_bytes=16384,
            max_output_tokens_per_request=20,
            max_total_output_tokens=100,
            max_total_cost="1",
            max_request_seconds=5,
            max_total_seconds=20,
        ),
    )

    async def transport(**kwargs: Any) -> Any:
        return SimpleNamespace(
            model="fixture/model",
            usage={
                "prompt_tokens": 5,
                "completion_tokens": 0 if "input" in kwargs else 2,
                "total_tokens": 5 if "input" in kwargs else 7,
            },
        )

    for _ in range(4):
        await budget.wrap_completion(transport)(
            model="fixture/model",
            messages=[{"role": "user", "content": "fixture"}],
            max_tokens=20,
            response_format={"type": "json_object"},
        )
    await budget.wrap_embedding(transport)(model="fixture/model", input=["fixture"])
    return budget.snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "unknown",
        "missing",
        "false_total",
        "mismatched_tokens",
        "missing_model",
        "breach",
        "missing_call",
        "state_changed",
        "context_hash",
    ],
)
async def test_cost_ledger_is_checked_and_unknown_is_not_zero(change: str) -> None:
    inputs, labels, report = sample()
    report["mode"] = "provider"
    row = report["cases"][0]
    state = row["public_state"]
    context = {
        "analysis_profile": inputs.cases[0].facts.mission_request.analysis_profile.model_dump(
            mode="json"
        ),
        **knowledge_prompt_context(state["evidence"]),
    }
    row["provider_calls"] = [
        {
            "schema": name,
            "public_context": context,
            "public_output": value,
            "output_status": "validated_schema",
            "finish_reason": "stop",
        }
        for name, value in (
            ("PublicDiagnosis", state["diagnosis"]),
            ("AlternativeBundle", {"alternatives": state["alternatives"]}),
            ("ReviewBundle", {"reviews": state["reviews"], "target": state["review_target"]}),
        )
    ]
    budget = await budget_snapshot()
    report["budget"] = budget
    if change == "unknown":
        budget["receipts"][0].update(status="unknown", token_usage=None, cost=None)
        budget.update(unknown_requests=1, total_cost=None, cost_complete=False)
    elif change == "missing":
        report["budget"] = None
    elif change == "false_total":
        budget["total_cost"] = "0"
    elif change == "mismatched_tokens":
        budget["receipts"][0]["token_usage"]["total_tokens"] += 1
    elif change == "missing_model":
        budget["receipts"][0]["response_model"] = None
    elif change == "breach":
        budget["budget_breach"] = True
    elif change == "missing_call":
        row["provider_calls"].pop()
    elif change == "state_changed":
        row["public_state"]["diagnosis"] = {
            **state["diagnosis"],
            "conclusion": "A different public conclusion",
        }
    elif change == "context_hash":
        context["evidence"][1]["metrics"]["retrieval_context_sha256"] = "b" * 64
    result = score_run(inputs, labels, report)
    assert result["passed"] is (change == "none")
    if change not in {"none", "breach", "state_changed", "context_hash"}:
        assert result["summary"]["cost"]["total_cost"] is None


def test_declared_expert_metadata_cannot_upgrade_acceptance() -> None:
    inputs, labels, report = sample()
    data = labels.model_dump(mode="json")
    data["cases"][0]["provenance"].update(
        kind="expert_review_declared",
        reviewed_by="Fixture reviewer",
        reviewed_at="2026-10-23T00:00:00Z",
        source_artifact_sha256="a" * 64,
    )
    result = score_run(inputs, WorkflowLabelBundle.model_validate(data), report)
    assert result["passed"]
    assert not any(row["expert_verified"] for row in result["cases"])
    assert result["boundary"]["expert_acceptance"] == "UNVERIFIED"


@pytest.mark.parametrize("error_field", ["run_error_code", "setup_error_code"])
def test_global_errors_cannot_be_hidden_by_successful_case_data(error_field: str) -> None:
    inputs, labels, report = sample()
    assert score_run(inputs, labels, report)["passed"]
    report[error_field] = "RuntimeError"
    result = score_run(inputs, labels, report)
    assert all(case["correct"] for case in result["cases"])
    assert not result["summary"]["execution_complete"]
    assert not result["summary"]["passed"] and not result["passed"]
    assert result["validation_status"] == "FAILED_OR_UNVERIFIED"
