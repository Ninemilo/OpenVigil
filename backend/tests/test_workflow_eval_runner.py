from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from windops_backend.agents.reasoning import LiteLLMReasoningProvider
from windops_backend.operations.workflow_eval import (
    PRE_REVIEW_NODES,
    execute_case,
    main,
    run_cases,
)
from windops_backend.operations.workflow_eval_budget import (
    FrozenRateCard,
    ModelRate,
    WorkflowBudgetLimits,
    WorkflowEvalBudget,
)
from windops_backend.operations.workflow_eval_inputs import load_input_bundle
from windops_backend.operations.workflow_eval_observer import PublicCallObserver

FIXTURES = Path(__file__).parent / "fixtures" / "workflow-eval"


def budget() -> WorkflowEvalBudget:
    return WorkflowEvalBudget(
        FrozenRateCard(
            schema_version="openvigil.workflow-eval-rate-card.v1",
            currency="USD",
            source="explicit synthetic transport prices, never actual vendor prices",
            verified_at=datetime.now(UTC),
            rates=tuple(
                ModelRate(
                    kind=kind,
                    model="test/model",
                    response_models=("test/model",),
                    input_per_million=Decimal("1"),
                    output_per_million=Decimal("1") if kind == "completion" else Decimal("0"),
                    input_capacity_tokens=10000,
                    input_capacity_source="synthetic transport capacity",
                    input_capacity_evidence_sha256="a" * 64,
                )
                for kind in ("completion", "embedding")
            ),
        ),
        WorkflowBudgetLimits(
            max_requests=10,
            max_input_bytes_per_request=100000,
            max_total_input_bytes=1000000,
            max_output_tokens_per_request=1000,
            max_total_output_tokens=10000,
            max_total_cost=Decimal("1"),
            max_request_seconds=10,
            max_total_seconds=100,
        ),
    )


@pytest.mark.asyncio
async def test_actual_graph_from_source_only_stops_at_human_review(tmp_path: Path) -> None:
    case = load_input_bundle(FIXTURES / "inputs.json").cases[0]
    result = await execute_case(
        case,
        tmp_path,
        FIXTURES,
        mode="software",
        admin_url=None,
        budget=None,
        observer=PublicCallObserver(),
    )
    assert result["status"] == "succeeded", result
    assert [row["node"] for row in result["executions"]] == PRE_REVIEW_NODES
    assert result["event_status"] == "succeeded"
    assert result["approval_count"] == result["work_order_count"] == 0
    assert result["database"]["cleanup_passed"] is True
    assert result["provider_calls"] == []
    assert (tmp_path / "workflow.sqlite").is_file()
    evidence = result["public_state"]["evidence"]
    knowledge = next(row for row in evidence if row["evidence_type"] == "knowledge_citation")
    snapshot = json.loads(knowledge["metrics"]["retrieval_context_json"])
    assert snapshot["passages"][0]["document_id"] == "EVAL1-GUIDE"
    assert snapshot["passages"][0]["excerpt"]
    assert result["public_state"]["approved"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_kind", ["refusal", "malformed", "foreign_evidence"])
async def test_real_provider_gate_rolls_back_but_preserves_public_candidate_and_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_kind: str
) -> None:
    # This uses the real SQL importer/outbox/graph/LiteLLM adapter. Only the network
    # transport is replaced, explicitly: it is not a paid-provider qualification.
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    import litellm

    import windops_backend.services.workflow as workflow

    calls: list[dict[str, Any]] = []

    async def transport(**kwargs: Any) -> Any:
        request = json.loads(kwargs["messages"][1]["content"])
        calls.append(request)
        context = request["public_context"]
        candidate: Any = {
            "component": context["analysis_profile"]["component"],
            "failure_mode": "insufficient_evidence",
            "confidence": 0,
            "conclusion": "The supplied facts do not establish a physical failure.",
            "evidence_refs": [context["evidence"][0]["evidence_id"]],
        }
        if candidate_kind == "malformed":
            candidate = {"hidden_reasoning": "DO-NOT-PERSIST-PRIVATE-TRACE"}
        elif candidate_kind == "foreign_evidence":
            candidate["evidence_refs"] = ["unavailable-other-tenant-evidence"]
        return SimpleNamespace(
            model="test/model",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(candidate),
                        reasoning_content="DO-NOT-PERSIST-PRIVATE-TRACE",
                    ),
                )
            ],
        )

    observer, limits = PublicCallObserver(), budget()
    monkeypatch.setattr(litellm, "acompletion", observer.wrap(limits.wrap_completion(transport)))
    monkeypatch.setattr(
        workflow,
        "build_reasoning_provider",
        lambda settings: LiteLLMReasoningProvider(
            "test/model",
            timeout_seconds=10,
            max_retries=0,
            minimum_diagnosis_confidence=0.5,
            max_output_tokens=1000,
        ),
    )
    case = load_input_bundle(FIXTURES / "inputs.json").cases[1]
    result = await execute_case(
        case,
        tmp_path,
        FIXTURES,
        mode="software",
        admin_url=None,
        budget=limits,
        observer=observer,
    )
    assert result["status"] == "failed"
    assert result["event_status"] == "failed"
    assert result["mission_status"] == "detected"
    assert result["approval_count"] == result["work_order_count"] == 0
    assert [row["node"] for row in result["executions"]] == ["diagnosis"]
    assert result["executions"][0]["status"] == "failed"
    assert result["executions"][0]["token_usage"]["total_tokens"] == 150
    assert len(calls) == 1
    assert "diagnosis" not in result["public_state"]
    assert limits.snapshot()["known_cost_subtotal"] == "0.00015"
    assert limits.snapshot()["requests"] == 1
    if candidate_kind == "malformed":
        assert result["provider_calls"][0]["public_output"] is None
        assert result["provider_calls"][0]["output_status"] == "invalid_schema"
    else:
        assert result["provider_calls"][0]["public_output"]["confidence"] == 0
        assert result["error_code"] == "ReasoningEvaluationError"
    assert "DO-NOT-PERSIST-PRIVATE-TRACE" not in json.dumps(result)
    assert b"DO-NOT-PERSIST-PRIVATE-TRACE" not in (tmp_path / "workflow.sqlite").read_bytes()
    assert not any(key in json.dumps(calls) for key in ("expected_modes", "review_expectations"))


@pytest.mark.asyncio
async def test_failed_case_does_not_disappear_or_stop_later_software_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINDOPS_WORKFLOW_EVAL_CHILD", "1")
    source = json.loads((FIXTURES / "inputs.json").read_text())
    # A scope denying the requested mission must fail in the production loader,
    # without changing source facts to a generated diagnosis/expected outcome.
    source["cases"][0]["facts"]["caller_scope"]["tenant_ids"] = ["NO-ACCESS-TENANT"]
    source["cases"][0]["facts"]["caller_scope"]["turbine_ids"] = []
    source["cases"][0]["facts"]["caller_scope"]["wind_farm_ids"] = []
    source["cases"][0]["facts"]["caller_scope"]["entity_ids"] = []
    from windops_backend.operations.workflow_eval_contracts import WorkflowFacts, canonical_sha256

    source["cases"][0]["source"]["source_snapshot_sha256"] = canonical_sha256(
        WorkflowFacts.model_validate(source["cases"][0]["facts"])
    )
    input_file = tmp_path / "inputs.json"
    input_file.write_text(json.dumps(source), encoding="utf-8")
    report = await run_cases(
        argparse.Namespace(
            inputs=input_file,
            output=tmp_path,
            artifact_root=FIXTURES,
            mode="software",
            allow_paid_provider=False,
            rate_card=None,
            budget=None,
        )
    )
    assert len(report["cases"]) == 2
    assert report["cases"][0]["status"] == "failed"
    assert report["cases"][0]["error_code"] == "NotFoundError"
    assert report["cases"][1]["status"] == "succeeded"
    assert (tmp_path / "case-000.json").is_file()
    assert (tmp_path / "case-001.json").is_file()
    assert report["boundary"]["expert_acceptance"] == "UNVERIFIED"


def test_cli_requires_new_output_and_explicit_paid_consent(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("existing evidence", encoding="utf-8")
    common = ["run", "--inputs", str(FIXTURES / "inputs.json"), "--artifact-root", str(FIXTURES)]
    assert main([*common, "--output", str(existing)]) == 2
    assert marker.read_text() == "existing evidence"
    target = tmp_path / "no-consent"
    assert main([*common, "--output", str(target), "--mode", "provider"]) == 2
    assert not target.exists()


def test_source_only_cli_subprocess_freezes_inputs_without_labels(tmp_path: Path) -> None:
    # Only copy inputs, no labels file exists anywhere in this evidence directory.
    inputs = tmp_path / "source-only.json"
    inputs.write_bytes((FIXTURES / "inputs.json").read_bytes())
    output = tmp_path / "executed"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "windops_backend.operations.workflow_eval",
            "run",
            "--inputs",
            str(inputs),
            "--artifact-root",
            str(FIXTURES),
            "--output",
            str(output),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert report["mode"] == "software"
    assert report["budget"] is None
    assert len(report["cases"]) == 2
    assert (output / "inputs.json").read_bytes() == inputs.read_bytes()
    assert not list(output.rglob("*label*"))
    assert all(row["status"] == "succeeded" for row in report["cases"])


@pytest.mark.asyncio
async def test_child_instrumentation_cannot_be_used_in_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WINDOPS_WORKFLOW_EVAL_CHILD", raising=False)
    with pytest.raises(RuntimeError, match="dedicated child"):
        await run_cases(argparse.Namespace())


@pytest.mark.asyncio
async def test_direct_provider_case_cannot_bypass_child_budget(tmp_path: Path) -> None:
    case = load_input_bundle(FIXTURES / "inputs.json").cases[0]
    with pytest.raises(RuntimeError, match="active child transport budget"):
        await execute_case(
            case,
            tmp_path,
            FIXTURES,
            mode="provider",
            admin_url="postgresql+asyncpg://operator@127.0.0.1/postgres",
            budget=budget(),
            observer=PublicCallObserver(),
        )
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


@pytest.mark.asyncio
async def test_three_actual_provider_adapters_keep_same_retrieved_body_and_human_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    import litellm

    import windops_backend.services.workflow as workflow

    observer, limits = PublicCallObserver(), budget()

    async def transport(**kwargs: Any) -> Any:
        request = json.loads(kwargs["messages"][1]["content"])
        context, name = request["public_context"], request["schema"]["title"]
        if name == "PublicDiagnosis":
            content = {
                "component": context["analysis_profile"]["component"],
                "failure_mode": "synthetic_transport_assessment",
                "confidence": 0.7,
                "conclusion": "The supplied readings require further inspection and human review.",
                "evidence_refs": [row["evidence_id"] for row in context["evidence"]],
            }
        elif name == "AlternativeBundle":
            plan = context["available_execution_plans"][0]
            content = {
                "alternatives": [
                    {
                        "alternative_id": "PLAN",
                        "title": "Inspect under the governed plan",
                        "action": plan["action"],
                        "execution_plan_id": plan["execution_plan_id"],
                        "safety_risk": "medium",
                        "rationale": "Requires independent human review.",
                        "recommended": True,
                    },
                    {
                        "alternative_id": "REVISE",
                        "title": "Request more evidence",
                        "action": "Request independent measurements and a revised plan.",
                        "execution_plan_id": None,
                        "safety_risk": "medium",
                        "rationale": "Proposal remains unexecutable without a governed plan.",
                        "recommended": False,
                    },
                    {
                        "alternative_id": "EXPERT",
                        "title": "Request specialist assessment",
                        "action": "Seek a specialist assessment before proposing execution.",
                        "execution_plan_id": None,
                        "safety_risk": "medium",
                        "rationale": "Requires an independent assessment and a governed plan.",
                        "recommended": False,
                    },
                ]
            }
        else:
            assert name == "ReviewBundle"
            content = {
                "target": context["review_target"],
                "reviews": [
                    {
                        "review_type": kind,
                        "reviewer_agent": "review_committee",
                        "outcome": "conditional_pass",
                        "public_summary": "Independent review required.",
                        "conditions": [
                            "Verify permits and source evidence before field execution."
                        ],
                        "operating_constraints": [],
                    }
                    for kind in ("engineering", "safety", "economic", "resource", "compliance")
                ],
            }
        return SimpleNamespace(
            model="test/model",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=json.dumps(content))
                )
            ],
        )

    monkeypatch.setattr(litellm, "acompletion", observer.wrap(limits.wrap_completion(transport)))
    monkeypatch.setattr(
        workflow,
        "build_reasoning_provider",
        lambda settings: LiteLLMReasoningProvider(
            "test/model",
            timeout_seconds=10,
            max_retries=0,
            minimum_diagnosis_confidence=0.5,
            max_output_tokens=1000,
        ),
    )
    case = load_input_bundle(FIXTURES / "inputs.json").cases[0]
    result = await execute_case(
        case,
        tmp_path,
        FIXTURES,
        mode="software",
        admin_url=None,
        budget=limits,
        observer=observer,
    )
    assert result["status"] == "succeeded", result
    assert [call["schema"] for call in observer.calls] == [
        "PublicDiagnosis",
        "AlternativeBundle",
        "ReviewBundle",
    ]
    snapshots = [call["public_context"]["retrieved_knowledge"] for call in observer.calls]
    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert snapshots[0][0]["passages"][0]["excerpt"]
    assert result["approval_count"] == result["work_order_count"] == 0
    assert limits.snapshot()["requests"] == 3
    assert limits.snapshot()["known_cost_subtotal"] == "0.00045"
    assert sum(row["token_usage"].get("total_tokens", 0) for row in result["executions"]) == 450
