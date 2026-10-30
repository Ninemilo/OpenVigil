"""Bounded public response observations inside the dedicated evaluation child.

Never retain raw model content, provider exception messages, credentials, or hidden
reasoning. Rejected public candidates are evidence for scoring, not published state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from windops_backend.agents.reasoning_contracts import AlternativeBundle, ReviewBundle
from windops_backend.operations.workflow_eval_budget import Transport
from windops_backend.schemas import PublicDiagnosis

PUBLIC_SCHEMAS: dict[str, type[BaseModel]] = {
    schema.__name__: schema for schema in (PublicDiagnosis, AlternativeBundle, ReviewBundle)
}
MAX_PUBLIC_BYTES = 2 * 1024 * 1024


class PublicCallObserver:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def wrap(self, transport: Transport) -> Transport:
        async def observed(**kwargs: Any) -> Any:
            # These messages come from the production structured-output adapter.
            # Extract only its public context; no expected labels are accepted.
            messages = kwargs.get("messages")
            if not isinstance(messages, list) or len(messages) != 2:
                raise ValueError("evaluation requires the production public request envelope")
            content = messages[1].get("content")
            if not isinstance(content, str) or len(content.encode()) > MAX_PUBLIC_BYTES:
                raise ValueError("evaluation public request exceeds its evidence bound")
            envelope = json.loads(content)
            schema_name = envelope.get("schema", {}).get("title")
            if schema_name not in PUBLIC_SCHEMAS or not isinstance(
                envelope.get("public_context"), dict
            ):
                raise ValueError("evaluation received an unsupported public schema")
            call: dict[str, Any] = {
                "schema": schema_name,
                "public_context": envelope["public_context"],
                "request_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "public_output": None,
                "output_status": "transport_failed",
                "finish_reason": None,
            }
            self.calls.append(call)
            response = await transport(**kwargs)
            try:
                choice = response.choices[0]
                finish_reason = choice.finish_reason
                call["finish_reason"] = (
                    finish_reason if finish_reason in {"stop", "length", "content_filter"} else None
                )
                raw = choice.message.content
                if not isinstance(raw, str) or len(raw.encode()) > MAX_PUBLIC_BYTES:
                    raise ValueError("invalid public response size")
                parsed = json.loads(raw)
                schema = PUBLIC_SCHEMAS[schema_name]
                # AlternativeBundle/ReviewBundle historically ignore extra fields.
                # The observation must never retain those fields or private traces.
                public = schema.model_validate(parsed)
                call["public_output"] = public.model_dump(mode="json")
                call["output_status"] = "validated_schema"
            except (ValueError, TypeError, AttributeError, IndexError, RecursionError):
                call["output_status"] = "invalid_schema"
            return response

        return observed
