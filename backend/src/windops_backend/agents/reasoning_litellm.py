"""Production structured-output transport, deadlines and audited provider usage."""

import asyncio
import hashlib
import json
from typing import Any

from pydantic import SecretStr, ValidationError

from windops_backend.agents.reasoning_contracts import (
    ReasoningProviderUnavailableError,
    ReviewBundle,
    SchemaT,
)
from windops_backend.agents.reasoning_evaluation import (
    _validation_diagnostics,
    evaluate_public_output,
)
from windops_backend.agents.reasoning_schema import PUBLIC_REASONING_SYSTEM_PROMPT, _response_schema


class LiteLLMReasoningProvider:
    """Production structured-output adapter; prompts explicitly exclude private reasoning."""

    def __init__(
        self,
        model: str,
        *,
        timeout_seconds: int,
        max_retries: int,
        minimum_diagnosis_confidence: float,
        api_base: str | None = None,
        api_key: SecretStr | None = None,
        max_output_tokens: int | None = None,
        enable_thinking: bool | None = None,
        review_timeout_seconds: int | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        if review_timeout_seconds is not None and not 5 <= review_timeout_seconds <= 120:
            raise ValueError("review_timeout_seconds must be between 5 and 120")
        self.review_timeout_seconds = review_timeout_seconds
        self.max_retries = max_retries
        self.minimum_diagnosis_confidence = minimum_diagnosis_confidence
        self.api_base = api_base
        self.api_key = api_key
        if max_output_tokens is not None and max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.max_output_tokens = max_output_tokens
        self.enable_thinking = enable_thinking
        self._last_usage: dict[str, Any] = {}

    def reset_usage(self) -> None:
        self._last_usage = {}

    def consume_usage(self) -> dict[str, Any]:
        usage, self._last_usage = self._last_usage, {}
        return usage

    async def generate(
        self, schema: type[SchemaT], instruction: str, public_context: dict[str, Any]
    ) -> SchemaT:
        # LiteLLM imports tokenizer data in some releases. Keep that production-only
        # initialization out of deterministic/offline test processes.
        import litellm

        response_schema = _response_schema(schema, public_context)
        timeout_seconds = self.timeout_seconds
        review_override = False
        if schema is ReviewBundle and self.review_timeout_seconds is not None:
            timeout_seconds = self.review_timeout_seconds
            review_override = True
        execution_options = {"timeout_seconds": timeout_seconds} if review_override else {}
        generation_options = (
            {"enable_thinking": self.enable_thinking} if self.enable_thinking is not None else {}
        )
        messages = [
            {"role": "system", "content": PUBLIC_REASONING_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": instruction,
                        "schema": response_schema,
                        "public_context": public_context,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        request_metadata = {
            "sha256": hashlib.sha256(
                json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "utf8_bytes": sum(len(message["content"].encode("utf-8")) for message in messages),
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": timeout_seconds,
            **generation_options,
        }
        degradation_policy = {
            "mode": "fail_closed",
            "automatic_deterministic_fallback": False,
            "human_review_required": True,
            "max_retries": self.max_retries,
            "timeout_seconds": timeout_seconds,
        }
        self._last_usage = {
            "provider": "litellm",
            "model": self.model,
            "requested_model": self.model,
            "request_contract_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "system": PUBLIC_REASONING_SYSTEM_PROMPT,
                        "schema": response_schema,
                        **(
                            {"generation_options": generation_options} if generation_options else {}
                        ),
                        **({"execution_options": execution_options} if execution_options else {}),
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest(),
            "token_usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "source": "unavailable_before_provider_response",
            },
            "evaluation_result": {"status": "pending", "checks": [], "request": request_metadata},
            "degradation_policy": degradation_policy,
        }
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await litellm.acompletion(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    **({"max_tokens": self.max_output_tokens} if self.max_output_tokens else {}),
                    num_retries=self.max_retries,
                    timeout=timeout_seconds,
                    **({"extra_body": generation_options} if generation_options else {}),
                    **(
                        {
                            "api_base": self.api_base,
                            "api_key": self.api_key.get_secret_value(),
                        }
                        if self.api_base is not None and self.api_key is not None
                        else {}
                    ),
                )
            # Account for paid responses before validation: a malformed or rejected
            # answer still consumes tokens and must not appear free in evaluations.
            raw_usage = getattr(response, "usage", None)
            if raw_usage is not None and hasattr(raw_usage, "model_dump"):
                raw_usage = raw_usage.model_dump()
            usage = raw_usage if isinstance(raw_usage, dict) else {}
            token_fields = ("prompt_tokens", "completion_tokens", "total_tokens")
            reported = all(
                isinstance(usage.get(field), int)
                and not isinstance(usage[field], bool)
                and usage[field] >= 0
                for field in token_fields
            )
            reported = reported and usage["total_tokens"] == (
                usage["prompt_tokens"] + usage["completion_tokens"]
            )
            returned_model = getattr(response, "model", None)
            self._last_usage.update(
                {
                    "model": returned_model
                    if isinstance(returned_model, str) and returned_model.strip()
                    else None,
                    "token_usage": {
                        **{field: usage[field] if reported else 0 for field in token_fields},
                        "source": "provider_reported" if reported else "provider_usage_missing",
                    },
                }
            )
            content = response.choices[0].message.content
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            self._last_usage["evaluation_result"]["response"] = {
                "finish_reason": finish_reason
                if finish_reason
                in {None, "stop", "length", "content_filter", "tool_calls", "function_call"}
                else "unknown",
                "utf8_bytes": len(content.encode("utf-8")) if isinstance(content, str) else None,
            }
            if finish_reason == "length":
                raise ReasoningProviderUnavailableError("LiteLLM returned a truncated response")
            if not isinstance(content, str):
                raise ReasoningProviderUnavailableError("LiteLLM returned no structured content")
            try:
                value = schema.model_validate_json(content)
            except ValidationError as exc:
                self._last_usage["evaluation_result"]["validation"] = _validation_diagnostics(
                    exc, schema
                )
                raise
            evaluation = evaluate_public_output(
                value,
                public_context=public_context,
                minimum_diagnosis_confidence=self.minimum_diagnosis_confidence,
            )
        except TimeoutError as exc:
            self._last_usage["evaluation_result"].update(
                {
                    "status": "failed",
                    "error_code": "provider_timeout",
                }
            )
            raise ReasoningProviderUnavailableError("LiteLLM request timed out") from exc
        except Exception as exc:
            self._last_usage["evaluation_result"].update(
                {
                    "status": "failed",
                    "error_code": type(exc).__name__,
                }
            )
            raise
        self._last_usage["evaluation_result"].update(evaluation)
        return value
