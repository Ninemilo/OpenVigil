"""Public-output governance and bounded, redacted validation diagnostics."""

import re
from typing import Any, get_args

from pydantic import BaseModel, ValidationError
from pydantic_core.core_schema import ErrorType

from windops_backend.agents.reasoning_contracts import (
    REVIEW_COMMITTEE_AGENT,
    AlternativeBundle,
    ReasoningEvaluationError,
    ReviewBundle,
    _review_target,
)
from windops_backend.schema_structural_workflow import StructuralScreeningOutput
from windops_backend.schemas import PublicDiagnosis
from windops_backend.work_plan_binding import validate_execution_binding


def _validation_diagnostics(exc: ValidationError, schema: type[BaseModel]) -> dict[str, Any]:
    # Only expose declared field names and built-in error codes. Pydantic's
    # messages, input, context, custom codes and dictionary keys can contain
    # provider content or secrets, including when JSON parsing itself fails.
    field_names: set[str] = set()

    def collect_fields(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                field_names.update(properties)
            for child in node.values():
                collect_fields(child)
        elif isinstance(node, list):
            for child in node:
                collect_fields(child)

    collect_fields(schema.model_json_schema())
    built_in_types = set(get_args(ErrorType))
    details = exc.errors(include_url=False, include_context=False, include_input=False)
    errors = [
        {
            "path": [
                part if isinstance(part, int) or part in field_names else "<redacted>"
                for part in detail["loc"][:20]
            ],
            "type": detail["type"] if detail["type"] in built_in_types else "custom_error",
        }
        for detail in details[:20]
    ]
    return {
        "stage": "response_schema",
        "error_count": exc.error_count(),
        "errors": errors,
        "truncated": len(details) > 20 or any(len(detail["loc"]) > 20 for detail in details[:20]),
    }


def validate_review_operating_constraints(value: ReviewBundle) -> None:
    # Reject the observed English LOTO-exception construction, including custom
    # providers. This is a specific contradiction check; human content review is
    # still required for other operating-authority claims and other languages.
    for review in value.reviews:
        for constraint in review.operating_constraints:
            if re.search(
                r"\b(?:operate|operation|rotate|rotation)\b[^.;\n]{0,240}"
                r"\b(?:without|unless|except)\b[^.;\n]{0,80}"
                r"\b(?:LOTO|lockout(?:[ /-]*tagout)?)\b",
                constraint,
                flags=re.IGNORECASE,
            ):
                raise ReasoningEvaluationError(
                    "review operating restriction treats LOTO as an operation exception"
                )


def validate_diagnosis_guidance_citations(
    value: PublicDiagnosis, public_context: dict[str, Any]
) -> None:
    # Guard the observed English/Chinese guidance-reference omission. This is
    # not a general semantic verifier; other claims still need content review.
    guidance_ids = set()
    for item in public_context.get("evidence", []):
        if not isinstance(item, dict) or item.get("evidence_type") != "knowledge_citation":
            continue
        metrics = item.get("metrics", {})
        count = metrics.get("documents_retrieved") if isinstance(metrics, dict) else None
        if (
            item.get("evidence_id")
            and item.get("source_refs")
            and isinstance(count, (int, float))
            and count > 0
        ):
            guidance_ids.add(str(item["evidence_id"]))
    discusses_guidance = re.search(
        r"\b(?:inspection\s+guidance|knowledge\s+(?:citation|guidance))\b"
        r"|知识(?:引用|指南)|(?:检查|检修)指南",
        value.conclusion,
        flags=re.IGNORECASE,
    )
    if guidance_ids and discusses_guidance and not guidance_ids.intersection(value.evidence_refs):
        raise ReasoningEvaluationError(
            "diagnosis mentions retrieved inspection guidance without a knowledge citation"
        )


def evaluate_public_output(
    value: BaseModel,
    *,
    public_context: dict[str, Any],
    minimum_diagnosis_confidence: float,
) -> dict[str, Any]:
    checks: list[str] = ["strict_schema"]
    if isinstance(value, StructuralScreeningOutput):
        allowed = {item["evidence_id"] for item in public_context.get("evidence", [])}
        missing = public_context["structural_context"]["missing_evidence"]
        if value.component != public_context["analysis_profile"]["component"]:
            raise ReasoningEvaluationError(
                "structural interpretation component differs from its frozen scope"
            )
        if len(set(value.evidence_refs)) != len(value.evidence_refs) or not set(
            value.evidence_refs
        ).issubset(allowed):
            raise ReasoningEvaluationError(
                "structural interpretation cites unknown or duplicate evidence"
            )
        if value.missing_evidence != missing or (
            missing and value.failure_mode != "insufficient_structural_evidence"
        ):
            raise ReasoningEvaluationError("structural interpretation omitted missing evidence")
        checks.extend(
            [
                "structural_scope",
                "source_grounding",
                "missing_evidence_preserved",
                "no_damage_probability",
            ]
        )
    elif isinstance(value, PublicDiagnosis):
        profile = public_context.get("analysis_profile", {})
        expected_component = str(profile.get("component", ""))
        allowed_evidence = {
            str(item.get("evidence_id"))
            for item in public_context.get("evidence", [])
            if isinstance(item, dict) and item.get("evidence_id")
        }
        if value.component != expected_component:
            raise ReasoningEvaluationError("diagnosis component does not match governed scope")
        if len(set(value.evidence_refs)) != len(value.evidence_refs):
            raise ReasoningEvaluationError("diagnosis evidence references must be unique")
        if not set(value.evidence_refs).issubset(allowed_evidence):
            raise ReasoningEvaluationError("diagnosis cites evidence outside the governed context")
        validate_diagnosis_guidance_citations(value, public_context)
        if value.failure_mode == "insufficient_evidence" and value.confidence != 0:
            raise ReasoningEvaluationError("an abstaining diagnosis must have zero confidence")
        if value.confidence < minimum_diagnosis_confidence:
            raise ReasoningEvaluationError("diagnosis confidence is below the release gate")
        checks.extend(["component_scope", "evidence_grounding", "confidence_gate"])
    elif isinstance(value, AlternativeBundle):
        try:
            for alternative in value.alternatives:
                if alternative.deterioration_risk_percent is not None:
                    raise ReasoningEvaluationError(
                        "unsupported deterioration probability without a calibrated contract"
                    )
                validate_execution_binding(
                    alternative.model_dump(), public_context.get("available_execution_plans", [])
                )
        except ValueError as exc:
            raise ReasoningEvaluationError(str(exc)) from exc
        checks.extend(
            [
                "alternative_uniqueness",
                "single_recommendation",
                "safety_gate",
                "execution_plan_binding",
                "no_unsupported_probability",
            ]
        )
    elif isinstance(value, ReviewBundle):
        validate_review_operating_constraints(value)
        target = _review_target(public_context)
        if value.target != target:
            raise ReasoningEvaluationError("review target does not match the governed proposal")
        if any(review.reviewer_agent != REVIEW_COMMITTEE_AGENT for review in value.reviews):
            raise ReasoningEvaluationError(
                "reviewer identity does not match the governed committee"
            )
        # A validly scoped failure is a finding for the human reviewer, not a
        # malformed provider response to be retried or converted into a pass.
        checks.extend(
            [
                "review_coverage",
                "review_target_binding",
                "reviewer_identity_binding",
                "no_loto_operation_exception",
            ]
        )
    return {"status": "passed", "checks": checks, "score": 1.0}
