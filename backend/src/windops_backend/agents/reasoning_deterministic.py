"""Deterministic reasoning policy for offline software and demonstration runs."""

from typing import Any

from pydantic import BaseModel

from windops_backend.agents.reasoning_contracts import (
    REVIEW_COMMITTEE_AGENT,
    AlternativeBundle,
    ReviewBundle,
    SchemaT,
    _review_target,
)
from windops_backend.agents.reasoning_evaluation import evaluate_public_output
from windops_backend.schemas import MaintenanceAlternative, PublicDiagnosis, ReviewResult


class DeterministicReasoningProvider:
    """Repeatable engineering policy used for tests and offline demonstrations."""

    def __init__(self) -> None:
        self._last_usage: dict[str, Any] = {}

    def reset_usage(self) -> None:
        self._last_usage = {}

    def consume_usage(self) -> dict[str, Any]:
        usage, self._last_usage = self._last_usage, {}
        return usage

    async def generate(
        self, schema: type[SchemaT], instruction: str, public_context: dict[str, Any]
    ) -> SchemaT:
        del instruction
        profile = public_context.get("analysis_profile", {})
        component = str(profile.get("component", "main_bearing"))
        display_component = component.replace("_", " ")
        turbine_id = str(public_context.get("turbine_id", "the turbine"))
        self._last_usage = {
            "provider": "deterministic",
            "model": "engineering-policy-2026.08",
            "token_usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "source": "deterministic_test_no_model",
            },
        }
        if public_context.get("structural_context"):
            from windops_backend.agents.structural_reasoning import deterministic_structural_output

            structural_value = deterministic_structural_output(schema, public_context)
            evaluate_public_output(
                structural_value, public_context=public_context, minimum_diagnosis_confidence=0
            )
            return schema.model_validate(structural_value.model_dump())
        if schema is PublicDiagnosis:
            evidence_refs = [
                str(item["evidence_id"]) for item in public_context.get("evidence", [])
            ]
            failure_mode_hint = str(profile.get("failure_mode_hint", "")).strip()
            failure_mode = (
                "early_main_bearing_degradation"
                if component == "main_bearing"
                else (failure_mode_hint or f"suspected {display_component} degradation")
                .lower()
                .replace(" ", "_")
                .replace("-", "_")
            )
            conclusion = (
                "Main-bearing RMS and temperature trends are consistent with early degradation."
                if component == "main_bearing"
                else (
                    f"The governed evidence is consistent with a developing "
                    f"{display_component} condition requiring controlled inspection."
                )
            )
            value: BaseModel = PublicDiagnosis(
                failure_mode=failure_mode,
                component=component,
                confidence=0.87,
                conclusion=conclusion,
                evidence_refs=evidence_refs,
            )
        elif schema is AlternativeBundle:
            asset_label = "WT-023" if turbine_id == "the turbine" else turbine_id
            plans = public_context.get("available_execution_plans", [])
            bound_plan = plans[0] if plans else None
            value = AlternativeBundle(
                alternatives=[
                    MaintenanceAlternative(
                        alternative_id="ALT-A",
                        title="Immediate shutdown and inspection",
                        action=f"Stop {asset_label} and inspect {display_component} immediately.",
                        safety_risk="low",
                        estimated_downtime_hours=18,
                        estimated_cost_cny=420000,
                        estimated_energy_loss_mwh=24,
                        deterioration_risk_percent=None,
                        weather_window_id="WEATHER-WINDOW-WT023",
                        required_resources=["crew", "vessel", "spare_part"],
                        rationale="Minimizes deterioration risk but has the highest outage cost.",
                    ),
                    MaintenanceAlternative(
                        alternative_id="ALT-B",
                        title=(
                            str(bound_plan["plan"]["title"])
                            if bound_plan
                            else "Derate and inspect within 72 hours"
                        ),
                        action=(
                            str(bound_plan["action"])
                            if bound_plan
                            else f"Derate {asset_label} to 70%, increase monitoring, then inspect "
                            f"{display_component} in the safe window."
                        ),
                        execution_plan_id=(
                            str(bound_plan["execution_plan_id"]) if bound_plan else None
                        ),
                        safety_risk="medium",
                        estimated_downtime_hours=10,
                        estimated_cost_cny=260000,
                        estimated_energy_loss_mwh=14,
                        deterioration_risk_percent=None,
                        weather_window_id="WEATHER-WINDOW-WT023",
                        required_resources=["crew", "vessel", "spare_part"],
                        rationale="Balances controlled derating, safe access, and repair cost.",
                        recommended=True,
                    ),
                    MaintenanceAlternative(
                        alternative_id="ALT-C",
                        title="Continue with enhanced monitoring",
                        action=(
                            f"Continue operation and acquire {display_component} condition data "
                            "every 15 minutes."
                        ),
                        safety_risk="high",
                        estimated_downtime_hours=0,
                        estimated_cost_cny=35000,
                        estimated_energy_loss_mwh=5,
                        deterioration_risk_percent=None,
                        weather_window_id=None,
                        required_resources=[],
                        rationale="Avoids an immediate outage but retains material failure risk.",
                    ),
                ]
            )
        elif schema is ReviewBundle:
            target = _review_target(public_context)
            value = ReviewBundle(
                target=target,
                reviews=[
                    ReviewResult(
                        review_type="engineering",
                        reviewer_agent=REVIEW_COMMITTEE_AGENT,
                        outcome="conditional_pass",
                        public_summary="Inspection requires verified condition measurements.",
                        conditions=["Assess measured results against the governed task limits"],
                        operating_constraints=[
                            "Derated operation is limited to 72 hours with "
                            "vibration RMS below 5.2 mm/s"
                        ],
                    ),
                    ReviewResult(
                        review_type="safety",
                        reviewer_agent=REVIEW_COMMITTEE_AGENT,
                        outcome="conditional_pass",
                        public_summary=(
                            "Field work requires isolation and offshore access approval."
                        ),
                        conditions=["human approval", "LOTO", "suitable marine weather"],
                    ),
                    ReviewResult(
                        review_type="economic",
                        reviewer_agent=REVIEW_COMMITTEE_AGENT,
                        outcome="pass",
                        public_summary=(
                            "Alternative B balances failure exposure and production loss."
                        ),
                    ),
                    ReviewResult(
                        review_type="resource",
                        reviewer_agent=REVIEW_COMMITTEE_AGENT,
                        outcome="pass",
                        public_summary="Crew, vessel, and bearing kit are available.",
                    ),
                    ReviewResult(
                        review_type="compliance",
                        reviewer_agent=REVIEW_COMMITTEE_AGENT,
                        outcome="pass",
                        public_summary=(
                            "The inspection plan follows the controlled maintenance procedure."
                        ),
                    ),
                ],
            )
        else:  # pragma: no cover - makes unsupported schemas fail loudly
            raise TypeError(f"Unsupported deterministic schema: {schema.__name__}")
        return schema.model_validate(value.model_dump())
