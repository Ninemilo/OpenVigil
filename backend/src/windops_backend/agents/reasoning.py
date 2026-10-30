"""Compatibility exports and provider selection for governed reasoning.

Prompt/schema, output governance and provider execution each have a dedicated
owner. Existing public and private imports continue to resolve here.
"""

import asyncio as asyncio
import hashlib as hashlib
import json as json
import re as re
from typing import Any as Any
from typing import Protocol as Protocol
from typing import TypeVar as TypeVar
from typing import get_args as get_args

from pydantic import BaseModel as BaseModel
from pydantic import SecretStr as SecretStr
from pydantic import ValidationError as ValidationError
from pydantic_core.core_schema import ErrorType as ErrorType

from windops_backend.agents.reasoning_contracts import (
    REVIEW_COMMITTEE_AGENT as REVIEW_COMMITTEE_AGENT,
)
from windops_backend.agents.reasoning_contracts import (
    AlternativeBundle as AlternativeBundle,
)
from windops_backend.agents.reasoning_contracts import (
    PublicReasoningProvider as PublicReasoningProvider,
)
from windops_backend.agents.reasoning_contracts import (
    ReasoningEvaluationError as ReasoningEvaluationError,
)
from windops_backend.agents.reasoning_contracts import (
    ReasoningProviderUnavailableError as ReasoningProviderUnavailableError,
)
from windops_backend.agents.reasoning_contracts import (
    ReviewBundle as ReviewBundle,
)
from windops_backend.agents.reasoning_contracts import (
    SchemaT as SchemaT,
)
from windops_backend.agents.reasoning_contracts import (
    _review_target as _review_target,
)
from windops_backend.agents.reasoning_deterministic import (
    DeterministicReasoningProvider as DeterministicReasoningProvider,
)
from windops_backend.agents.reasoning_evaluation import (
    _validation_diagnostics as _validation_diagnostics,
)
from windops_backend.agents.reasoning_evaluation import (
    evaluate_public_output as evaluate_public_output,
)
from windops_backend.agents.reasoning_evaluation import (
    validate_diagnosis_guidance_citations as validate_diagnosis_guidance_citations,
)
from windops_backend.agents.reasoning_evaluation import (
    validate_review_operating_constraints as validate_review_operating_constraints,
)
from windops_backend.agents.reasoning_litellm import (
    LiteLLMReasoningProvider as LiteLLMReasoningProvider,
)
from windops_backend.agents.reasoning_schema import (
    PUBLIC_REASONING_SYSTEM_PROMPT as PUBLIC_REASONING_SYSTEM_PROMPT,
)
from windops_backend.agents.reasoning_schema import (
    _response_schema as _response_schema,
)
from windops_backend.config import Settings
from windops_backend.schema_structural_workflow import (
    StructuralScreeningOutput as StructuralScreeningOutput,
)
from windops_backend.schemas import (
    MaintenanceAlternative as MaintenanceAlternative,
)
from windops_backend.schemas import (
    PublicDiagnosis as PublicDiagnosis,
)
from windops_backend.schemas import (
    ReviewResult as ReviewResult,
)
from windops_backend.work_plan_binding import (
    validate_execution_binding as validate_execution_binding,
)


def build_reasoning_provider(settings: Settings) -> PublicReasoningProvider:
    if settings.agent_mode == "litellm":
        model, api_base, api_key = settings.reasoning_connection()
        return LiteLLMReasoningProvider(
            model,
            timeout_seconds=settings.litellm_timeout_seconds,
            review_timeout_seconds=settings.litellm_review_timeout_seconds,
            max_retries=settings.litellm_max_retries,
            minimum_diagnosis_confidence=settings.litellm_minimum_diagnosis_confidence,
            api_base=api_base,
            api_key=api_key,
            max_output_tokens=settings.litellm_max_output_tokens,
            enable_thinking=(
                settings.siliconflow_enable_thinking
                if settings.llm_provider == "siliconflow"
                else None
            ),
        )
    return DeterministicReasoningProvider()
