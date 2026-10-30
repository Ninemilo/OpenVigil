"""Compatibility and ownership contracts for governed reasoning."""

import ast
from pathlib import Path
from types import ModuleType

import pytest

from windops_backend.agents import (
    reasoning,
    reasoning_contracts,
    reasoning_deterministic,
    reasoning_evaluation,
    reasoning_litellm,
    reasoning_schema,
)


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (reasoning_contracts, "REVIEW_COMMITTEE_AGENT"),
        (reasoning_contracts, "AlternativeBundle"),
        (reasoning_contracts, "ReviewBundle"),
        (reasoning_contracts, "ReasoningEvaluationError"),
        (reasoning_contracts, "ReasoningProviderUnavailableError"),
        (reasoning_contracts, "PublicReasoningProvider"),
        (reasoning_contracts, "SchemaT"),
        (reasoning_contracts, "_review_target"),
        (reasoning_schema, "PUBLIC_REASONING_SYSTEM_PROMPT"),
        (reasoning_schema, "_response_schema"),
        (reasoning_evaluation, "_validation_diagnostics"),
        (reasoning_evaluation, "validate_review_operating_constraints"),
        (reasoning_evaluation, "validate_diagnosis_guidance_citations"),
        (reasoning_evaluation, "evaluate_public_output"),
        (reasoning_deterministic, "DeterministicReasoningProvider"),
        (reasoning_litellm, "LiteLLMReasoningProvider"),
    ],
)
def test_legacy_reasoning_exports_are_the_owner_objects(owner: ModuleType, name: str) -> None:
    assert getattr(reasoning, name) is getattr(owner, name)


def test_providers_use_the_shared_schema_and_governance_owners() -> None:
    deterministic_globals = reasoning.DeterministicReasoningProvider.generate.__globals__
    litellm_globals = reasoning.LiteLLMReasoningProvider.generate.__globals__
    assert (
        deterministic_globals["evaluate_public_output"]
        is reasoning_evaluation.evaluate_public_output
    )
    assert litellm_globals["evaluate_public_output"] is reasoning_evaluation.evaluate_public_output
    assert litellm_globals["_response_schema"] is reasoning_schema._response_schema
    assert (
        litellm_globals["_validation_diagnostics"] is reasoning_evaluation._validation_diagnostics
    )
    assert (
        litellm_globals["PUBLIC_REASONING_SYSTEM_PROMPT"]
        is reasoning_schema.PUBLIC_REASONING_SYSTEM_PROMPT
    )
    assert (
        litellm_globals["ReasoningProviderUnavailableError"]
        is reasoning_contracts.ReasoningProviderUnavailableError
    )


def test_reasoning_owners_do_not_import_the_compatibility_entrypoint() -> None:
    for owner in (
        reasoning_contracts,
        reasoning_schema,
        reasoning_evaluation,
        reasoning_deterministic,
        reasoning_litellm,
    ):
        assert owner.__file__ is not None
        tree = ast.parse(Path(owner.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "windops_backend.agents.reasoning", owner.__name__
                if node.module == "windops_backend.agents":
                    assert "reasoning" not in {alias.name for alias in node.names}, owner.__name__
            elif isinstance(node, ast.Import):
                assert "windops_backend.agents.reasoning" not in {
                    alias.name for alias in node.names
                }, owner.__name__
