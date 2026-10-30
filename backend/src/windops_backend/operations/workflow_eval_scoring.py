"""Independent, label-bound scoring of public production workflow observations.

These checks establish report consistency and declared-label agreement, not expert
qualification, semantic entailment, source authenticity, or a provider invoice.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeGuard

from pydantic import BaseModel

from windops_backend.agents.knowledge_context import (
    CONTEXT_JSON_METRIC,
    CONTEXT_SHA_METRIC,
    knowledge_prompt_context,
)
from windops_backend.agents.reasoning import evaluate_public_output
from windops_backend.agents.reasoning_contracts import AlternativeBundle, ReviewBundle
from windops_backend.operations.workflow_eval_contracts import (
    WorkflowCaseInput,
    WorkflowCaseLabel,
    WorkflowInputBundle,
    WorkflowLabelBundle,
    canonical_sha256,
)
from windops_backend.schemas import PublicDiagnosis
from windops_backend.work_plan_binding import describe_execution_plan, validate_execution_binding

PRE_REVIEW_NODES = [
    "scada",
    "vibration",
    "knowledge",
    "diagnosis",
    "alternatives",
    "reviews",
    "hitl",
]


def _time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else None
        return result if result is not None and result.utcoffset() is not None else None
    except ValueError:
        return None


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _integer(value: Any) -> TypeGuard[int]:
    return type(value) is int and value >= 0


def _money(value: Any) -> Decimal | None:
    try:
        result = Decimal(value) if isinstance(value, str) else None
        return result if result is not None and result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def _objects(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) and all(isinstance(row, dict) for row in value) else []


def _guard(value: BaseModel, context: dict[str, Any], *, abstain: bool = False) -> bool:
    try:
        evaluate_public_output(
            value, public_context=context, minimum_diagnosis_confidence=0 if abstain else 0.5
        )
        return True
    except (ValueError, TypeError, KeyError):
        return False


def _candidate(
    name: str, schema: type[BaseModel], state_value: Any, calls: list[dict[str, Any]]
) -> tuple[BaseModel | None, dict[str, Any] | None]:
    matching = [call for call in calls if call.get("schema") == name]
    if matching:
        call = matching[-1]
        if call.get("output_status") != "validated_schema" or call.get("finish_reason") == "length":
            return None, None
        body = call.get("public_output")
        context = call.get("public_context")
    else:
        body, context = state_value, None
    try:
        return schema.model_validate(body), context if isinstance(context, dict) else None
    except (ValueError, TypeError):
        return None, None


def _source_map(
    case: WorkflowCaseInput, context: dict[str, Any]
) -> tuple[dict[str, set[str]], bool]:
    """Resolve runtime passage IDs only through the validated retrieval snapshot."""
    evidence = _objects(context.get("evidence"))
    retrieved = _objects(context.get("retrieved_knowledge"))
    if any(not isinstance(row.get("evidence_id"), str) for row in [*evidence, *retrieved]):
        return {}, False
    if len({row["evidence_id"] for row in retrieved}) != len(retrieved):
        return {}, False
    # Provider contexts decode snapshots and remove the JSON metric. Restore the
    # exact canonical document to reuse the production hash/provenance validator.
    restored = json.loads(json.dumps(evidence))
    by_id = {row.get("evidence_id"): row for row in restored}
    valid = len(by_id) == len(evidence) and bool(evidence)
    for snapshot in retrieved:
        row = by_id.get(snapshot.get("evidence_id"))
        if row is None:
            return {}, False
        content = {
            key: value
            for key, value in snapshot.items()
            if key not in {"evidence_id", "snapshot_sha256"}
        }
        metrics = row.get("metrics")
        if not isinstance(metrics, dict) or metrics.get(CONTEXT_SHA_METRIC) != snapshot.get(
            "snapshot_sha256"
        ):
            return {}, False
        metrics[CONTEXT_JSON_METRIC] = json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        metrics[CONTEXT_SHA_METRIC] = snapshot.get("snapshot_sha256")
    try:
        decoded = knowledge_prompt_context(restored)
    except (ValueError, TypeError, KeyError):
        return {}, False
    documents = {row.id for row in case.facts.knowledge_documents}
    known = {
        case.facts.alarm.id,
        *(row.payload.source_event_id for row in case.facts.receipts),
        *documents,
    }
    aliases = {f"windops://knowledge/documents/{value}": value for value in documents}
    for snapshot in decoded["retrieved_knowledge"]:
        for passage in snapshot["passages"]:
            document_id = passage["document_id"]
            if document_id not in documents:
                valid = False
            for key in ("citation_href", "citation_uri", "source_href", "passage_id"):
                alias = passage.get(key)
                if alias:
                    if alias in aliases and aliases[alias] != document_id:
                        valid = False
                    aliases[alias] = document_id
    mapped: dict[str, set[str]] = {}
    for row in evidence:
        identity, refs = row.get("evidence_id"), row.get("source_refs")
        if (
            not isinstance(identity, str)
            or not isinstance(refs, list)
            or any(not isinstance(ref, str) for ref in refs)
        ):
            valid = False
            continue
        mapped[identity] = {aliases.get(ref, ref) for ref in refs if isinstance(ref, str)}
        if not mapped[identity] <= known:
            valid = False
    return mapped, valid


def _cost_audit(budget: Any, mode: str, observed_calls: int) -> dict[str, Any]:
    if mode == "software":
        return {"complete": budget is None, "status": "not_applicable_software", "total_cost": None}
    if not isinstance(budget, dict):
        return {"complete": False, "status": "unknown", "total_cost": None}
    receipts = _objects(budget.get("receipts"))
    complete = (
        budget.get("schema_version") == "openvigil.workflow-eval-budget.v1"
        and budget.get("cost_status") == "rate_card_estimate"
        and budget.get("billing_guarantee") is False
        and budget.get("cost_complete") is True
        and budget.get("unknown_requests") == 0
        and budget.get("pending_requests") == 0
        and budget.get("requests") == len(receipts)
        and bool(receipts)
        and sum(row.get("kind") == "completion" for row in receipts) == observed_calls
    )
    prompt_total = output_total = 0
    known_cost = Decimal(0)
    for index, row in enumerate(receipts):
        usage = row.get("token_usage")
        cost = _money(row.get("cost"))
        if not isinstance(usage, dict) or cost is None:
            complete = False
            continue
        prompt, output, total = (
            usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        if not _integer(prompt) or not _integer(output) or not _integer(total):
            complete = False
            continue
        valid = (
            prompt > 0
            and total == prompt + output
            and usage.get("source") == "provider_reported"
            and row.get("request_index") == index
            and row.get("status") == "settled"
            and isinstance(row.get("response_model"), str)
            and bool(row["response_model"])
            and _finite(row.get("latency_seconds"))
            and row.get("kind") in {"completion", "embedding"}
            and (row.get("kind") != "embedding" or output == 0)
        )
        complete = complete and valid
        if valid:
            prompt_total += prompt
            output_total += output
            known_cost += cost
    complete = complete and (
        prompt_total == budget.get("known_prompt_tokens")
        and output_total == budget.get("known_output_tokens")
        and known_cost == _money(budget.get("known_cost_subtotal"))
        and known_cost == _money(budget.get("total_cost"))
        and known_cost == _money(budget.get("cost_exposure"))
        and _money(budget.get("reserved_cost")) == 0
    )
    return {
        "complete": complete,
        "status": "rate_card_estimate" if complete else "unknown_or_inconsistent",
        "total_cost": str(known_cost) if complete else None,
        "currency": budget.get("currency"),
        "budget_gate_passed": budget.get("halted") is False
        and budget.get("budget_breach") is False,
        "billing_guarantee": False,
    }


def _case_score(
    case: WorkflowCaseInput, label: WorkflowCaseLabel, row: dict[str, Any], mode: str
) -> dict[str, Any]:
    raw_state = row.get("public_state")
    state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
    calls = _objects(row.get("provider_calls"))
    executions = _objects(row.get("executions"))
    profile = case.facts.mission_request.analysis_profile
    alarm_receipt = next(
        item
        for item in case.facts.receipts
        if item.payload.source_event_id == case.facts.alarm.source_event_id
    )
    plan = describe_execution_plan(profile, alarm_receipt.payload.turbine_id)
    base_context: dict[str, Any] = {
        "analysis_profile": profile.model_dump(mode="json"),
        "evidence": state.get("evidence", []),
    }
    diagnosis, observed_context = _candidate(
        "PublicDiagnosis", PublicDiagnosis, state.get("diagnosis"), calls
    )
    context = {
        **(observed_context or base_context),
        "analysis_profile": base_context["analysis_profile"],
    }
    sources, sources_valid = _source_map(case, context)
    cited = diagnosis.evidence_refs if isinstance(diagnosis, PublicDiagnosis) else []
    resolved = set().union(*(sources.get(ref, set()) for ref in cited)) if cited else set()
    membership = bool(cited) and len(set(cited)) == len(cited) and set(cited) <= sources.keys()
    required = set(label.required_source_refs)
    supporting = set(label.supporting_source_refs)
    required_recall = len(required & resolved) / len(required) if required else None
    support_precision = (
        len(supporting & resolved) / len(resolved) if resolved and supporting else None
    )
    guards = isinstance(diagnosis, PublicDiagnosis) and _guard(
        diagnosis, context, abstain=label.abstain
    )
    classification = (
        isinstance(diagnosis, PublicDiagnosis)
        and diagnosis.component == label.expected_component
        and diagnosis.failure_mode in label.expected_modes
    )
    no_writes = (
        type(row.get("approval_count")) is int
        and row["approval_count"] == 0
        and type(row.get("work_order_count")) is int
        and row["work_order_count"] == 0
    )
    checks: dict[str, bool] = {
        "diagnosis_schema_and_guards": guards,
        "classification": classification,
        "citation_membership": membership,
        "source_membership": sources_valid,
        "required_source_recall": required <= resolved,
        "forbidden_source_absence": not set(label.forbidden_source_refs) & resolved,
        "no_approval_or_work_order": no_writes,
        "database_cleanup": isinstance(row.get("database"), dict)
        and row["database"].get("cleanup_passed") is True,
        "latency_complete": _finite(row.get("latency_ms"))
        and bool(executions)
        and all(
            _finite(item.get("latency_ms")) and item["latency_ms"] <= row["latency_ms"] + 1
            for item in executions
        ),
        "provider_observations": mode == "software"
        or {call.get("schema") for call in calls}
        == (
            {"PublicDiagnosis"}
            if label.abstain
            else {"PublicDiagnosis", "AlternativeBundle", "ReviewBundle"}
        ),
    }
    downstream = {"alternatives", "reviews", "hitl", "workorder", "closure"}
    if label.abstain:
        failed = [
            item
            for item in executions
            if item.get("node") == "diagnosis"
            and item.get("status") == "failed"
            and item.get("error_code") == "ReasoningEvaluationError"
        ]
        checks["expected_stop"] = (
            row.get("status") == "failed"
            and row.get("error_code") == "ReasoningEvaluationError"
            and row.get("event_status") == "failed"
            and row.get("mission_status") == "detected"
            and len(failed) == 1
            and no_writes
            and isinstance(diagnosis, PublicDiagnosis)
            and diagnosis.failure_mode == "insufficient_evidence"
            and diagnosis.confidence == 0
            and not any(item.get("node") in downstream for item in executions)
            and not any(
                call.get("schema") in {"AlternativeBundle", "ReviewBundle"} for call in calls
            )
            and not any(
                state.get(key)
                for key in ("alternatives", "reviews", "approved", "decision_id", "work_order_id")
            )
        )
    else:
        checks["expected_stop"] = (
            row.get("status") == "succeeded"
            and row.get("mission_status") == "under_review"
            and row.get("event_status") == "succeeded"
            and [item.get("node") for item in executions] == PRE_REVIEW_NODES
            and all(item.get("status") == "succeeded" for item in executions)
            and state.get("requires_human_approval") is True
            and state.get("approved") is False
        )
        alternatives, _ = _candidate(
            "AlternativeBundle",
            AlternativeBundle,
            {"alternatives": state.get("alternatives")},
            calls,
        )
        plan_context = {"available_execution_plans": [plan]}
        valid_plan = isinstance(alternatives, AlternativeBundle) and _guard(
            alternatives, plan_context
        )
        target = None
        if isinstance(alternatives, AlternativeBundle):
            recommended = next(item for item in alternatives.alternatives if item.recommended)
            try:
                validate_execution_binding(recommended.model_dump(), [plan], require_bound=True)
            except ValueError:
                valid_plan = False
            valid_plan = (
                valid_plan and recommended.execution_plan_id in label.allowed_execution_plan_ids
            )
            target = {
                "scope": "maintenance_execution",
                "alternative_id": recommended.alternative_id,
                "execution_plan_id": recommended.execution_plan_id,
            }
        checks["execution_plan_binding"] = valid_plan
        reviews, _ = _candidate(
            "ReviewBundle",
            ReviewBundle,
            {"reviews": state.get("reviews"), "target": state.get("review_target")},
            calls,
        )
        checks["review_guards"] = (
            isinstance(reviews, ReviewBundle)
            and target is not None
            and _guard(reviews, {"review_target": target})
        )
        by_type = (
            {item.review_type: item for item in reviews.reviews}
            if isinstance(reviews, ReviewBundle)
            else {}
        )
        checks["review_labels"] = bool(by_type) and all(
            expected.review_type in by_type
            and by_type[expected.review_type].outcome in expected.allowed_outcomes
            and set(expected.required_conditions) <= set(by_type[expected.review_type].conditions)
            for expected in label.review_expectations
        )
        checks["persisted_public_output"] = (
            isinstance(diagnosis, PublicDiagnosis)
            and state.get("diagnosis") == diagnosis.model_dump(mode="json")
            and isinstance(alternatives, AlternativeBundle)
            and state.get("alternatives") == alternatives.model_dump(mode="json")["alternatives"]
            and isinstance(reviews, ReviewBundle)
            and state.get("reviews") == reviews.model_dump(mode="json")["reviews"]
            and state.get("review_target") == target
        )
    public: list[Any] = [
        value.model_dump(mode="json") for value in (diagnosis,) if value is not None
    ]
    public.extend(
        call["public_output"] for call in calls if isinstance(call.get("public_output"), dict)
    )
    public.extend(state.get(key) for key in ("alternatives", "reviews") if state.get(key))
    # This is explicitly lexical matching; no paraphrase or entailment claim.
    phrases = label.forbidden_public_phrases
    text = json.dumps(public, ensure_ascii=False).casefold()
    checks["forbidden_public_phrases"] = not any(phrase.casefold() in text for phrase in phrases)
    return {
        "case_id": case.case_id,
        "input_sha256": canonical_sha256(case),
        "run_status": row.get("status"),
        "correct": all(checks.values()),
        "checks": checks,
        "failed_checks": [key for key, passed in checks.items() if not passed],
        "citation_membership_precision": sum(ref in sources for ref in cited) / len(cited)
        if cited
        else None,
        "required_source_recall": required_recall,
        "declared_support_precision": support_precision,
        "cited_source_refs": sorted(resolved),
        "explicit_abstention": isinstance(diagnosis, PublicDiagnosis)
        and diagnosis.failure_mode == "insufficient_evidence"
        and diagnosis.confidence == 0,
        "latency_ms": row.get("latency_ms") if _finite(row.get("latency_ms")) else None,
        "error_code": row.get("error_code"),
        "label_provenance": label.provenance.kind,
        "expert_verified": False,
    }


def score_run(
    inputs: WorkflowInputBundle, labels: WorkflowLabelBundle, report: dict[str, Any]
) -> dict[str, Any]:
    """Score every source case; incompatible identities are errors, never warnings."""
    labels.verify_inputs(inputs)
    if report.get("schema_version") != "openvigil.workflow-eval-run.v1" or report.get(
        "mode"
    ) not in {"software", "provider"}:
        raise ValueError("unsupported workflow run report")
    if report.get("input_bundle_sha256") != inputs.input_bundle_sha256:
        raise ValueError("report input bundle SHA-256 mismatch")
    rows = _objects(report.get("cases"))
    if (
        len(rows) != len(inputs.cases)
        or len({row.get("case_id") for row in rows}) != len(rows)
        or {row.get("case_id") for row in rows} != {case.case_id for case in inputs.cases}
    ):
        raise ValueError("report must preserve every input case exactly once")
    by_id = {row["case_id"]: row for row in rows}
    by_label = {label.case_id: label for label in labels.cases}
    for case in inputs.cases:
        row = by_id[case.case_id]
        if (
            row.get("input_sha256") != canonical_sha256(case)
            or row.get("source_snapshot_sha256") != case.source.source_snapshot_sha256
            or _time(row.get("as_of")) != case.as_of
        ):
            raise ValueError("report input case SHA-256 or snapshot binding mismatch")
        if row.get("status") not in {"succeeded", "failed", "not_executed"}:
            raise ValueError("invalid workflow case status")
    results = [
        _case_score(case, by_label[case.case_id], by_id[case.case_id], report["mode"])
        for case in inputs.cases
    ]
    started, completed = _time(report.get("started_at")), _time(report.get("completed_at"))
    timing = started is not None and completed is not None and completed >= started
    cost = _cost_audit(
        report.get("budget"),
        report["mode"],
        sum(len(_objects(row.get("provider_calls"))) for row in rows),
    )
    execution_complete = not report.get("run_error_code") and not report.get("setup_error_code")
    passed = (
        all(row["correct"] for row in results)
        and execution_complete
        and timing
        and cost["complete"]
        and cost.get("budget_gate_passed", True)
    )
    return {
        "schema_version": "openvigil.workflow-eval-score.v1",
        "passed": passed,
        "input_bundle_sha256": inputs.input_bundle_sha256,
        "labels_sha256": canonical_sha256(labels),
        "mode": report["mode"],
        "cases": results,
        "summary": {
            "passed": passed,
            "cases": len(results),
            "correct": sum(row["correct"] for row in results),
            "accuracy": sum(row["correct"] for row in results) / len(results),
            "execution_complete": execution_complete,
            "timing_complete": timing and all(row["checks"]["latency_complete"] for row in results),
            "cost": cost,
        },
        "validation_status": (
            "SOFTWARE_VERIFIED" if report["mode"] == "software" else "DECLARED_LABEL_CHECKS_PASSED"
        )
        if passed
        else "FAILED_OR_UNVERIFIED",
        "boundary": {
            "expert_acceptance": "UNVERIFIED",
            "field_acceptance": "UNVERIFIED",
            "semantic_entailment": "UNVERIFIED",
            "source_authenticity": "UNVERIFIED",
            "prohibited_suggestions": "production_guards_and_declared_lexical_labels_only",
            "billing_guarantee": False,
        },
    }
