"""Request-before-reservation gates for an isolated workflow evaluation process.

Install the two wrappers only in the evaluation subprocess and restore its transports
in ``finally``. No credentials, providers, prices, tokenizers or network are loaded here.
Each completion reserves the full explicitly documented model input capacity;
embedding batches reserve that full per-item capacity for every input string. Estimates
are rate-card exposure bounds, not vendor invoices or a guarantee against provider
misreporting. An unknown bill retains its reservation and stops new requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal
from threading import RLock
from time import perf_counter
from typing import Any, Literal, NoReturn, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RequestKind = Literal["completion", "embedding"]
Transport = Callable[..., Awaitable[Any]]
MILLION = Decimal(1_000_000)


class ModelRate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: RequestKind
    model: str = Field(min_length=1, max_length=256)
    response_models: tuple[str, ...] = Field(min_length=1, max_length=20)
    input_per_million: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=18, decimal_places=8)
    output_per_million: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=18, decimal_places=8)
    # Full completion input context, or full embedding capacity per input item.
    input_capacity_tokens: int = Field(strict=True, gt=0, le=1_000_000_000)
    input_capacity_source: str = Field(min_length=1, max_length=2000)
    input_capacity_evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def explicit_identity(self) -> ModelRate:
        if (
            self.model.strip() != self.model
            or not self.model.strip()
            or any(not item.strip() or item != item.strip() for item in self.response_models)
            or len(set(self.response_models)) != len(self.response_models)
            or not self.input_capacity_source.strip()
        ):
            raise ValueError("models and input-capacity provenance must be explicit")
        if self.kind == "embedding" and self.output_per_million != 0:
            raise ValueError("embedding rates must explicitly declare zero output-token price")
        return self


class FrozenRateCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["openvigil.workflow-eval-rate-card.v1"]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    source: str = Field(min_length=1, max_length=2000)
    verified_at: datetime
    rates: tuple[ModelRate, ...] = Field(min_length=2, max_length=100)

    @field_validator("verified_at")
    @classmethod
    def aware_date(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("rate-card verification time must include timezone")
        return value

    @model_validator(mode="after")
    def complete_models(self) -> FrozenRateCard:
        if not self.source.strip():
            raise ValueError("an explicit frozen pricing source is required")
        identities = [(rate.kind, rate.model) for rate in self.rates]
        if len(identities) != len(set(identities)) or {rate.kind for rate in self.rates} != {
            "completion",
            "embedding",
        }:
            raise ValueError("rate card must uniquely cover completion and embedding models")
        return self


class WorkflowBudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_requests: int = Field(strict=True, gt=0, le=10_000)
    max_input_bytes_per_request: int = Field(strict=True, gt=0, le=100_000_000)
    max_total_input_bytes: int = Field(strict=True, gt=0, le=1_000_000_000)
    max_output_tokens_per_request: int = Field(strict=True, gt=0, le=1_000_000_000)
    max_total_output_tokens: int = Field(strict=True, gt=0, le=1_000_000_000)
    max_total_cost: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=18, decimal_places=8)
    max_request_seconds: float = Field(gt=0, le=600, allow_inf_nan=False)
    max_total_seconds: float = Field(gt=0, le=86_400, allow_inf_nan=False)


class WorkflowBudgetError(RuntimeError):
    """Only a fixed safe code crosses the transport boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _amount(value: Decimal) -> str:
    return format(value, "f")


def _integer(value: Any) -> TypeGuard[int]:
    return type(value) is int and 0 <= value <= 2**63 - 1


class WorkflowEvalBudget:
    def __init__(
        self,
        rate_card: FrozenRateCard,
        limits: WorkflowBudgetLimits,
        *,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.rate_card = rate_card
        self.limits = limits
        self._rates = {(rate.kind, rate.model): rate for rate in rate_card.rates}
        encoded = json.dumps(
            rate_card.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        self._rate_sha256 = hashlib.sha256(encoded.encode()).hexdigest()
        self._clock = clock
        self._started = clock()
        if not math.isfinite(self._started):
            raise ValueError("budget clock must be finite")
        self._lock = RLock()
        self._halt_reason: str | None = None
        self._breach = False
        self._receipts: list[dict[str, Any]] = []
        self._input_bytes = 0
        self._known_cost = Decimal(0)
        self._reserved_cost = Decimal(0)
        self._known_prompt_tokens = 0
        self._known_output_tokens = 0
        self._reserved_output_tokens = 0

    def remaining_seconds(self) -> float:
        """The runner also uses this to bound the entire workflow, including local work."""
        with self._lock:
            elapsed = self._clock() - self._started
            if not math.isfinite(elapsed) or elapsed < 0:
                self._deny("budget_clock_invalid")
            return max(0.0, self.limits.max_total_seconds - elapsed)

    def check_deadline(self) -> None:
        """Check local workflow work explicitly, before the runner's cleanup grace.

        Reading a snapshot does not change budget state; runners must call this
        after local work as well as bounding its awaitable by remaining_seconds.
        """
        with self._lock:
            if self.remaining_seconds() <= 0:
                self._deny("total_deadline_exhausted")

    def _deny(self, code: str) -> NoReturn:
        self._halt_reason = self._halt_reason or code
        raise WorkflowBudgetError(code)

    def wrap_completion(self, transport: Transport) -> Transport:
        async def completion(**kwargs: Any) -> Any:
            return await self._invoke("completion", transport, kwargs)

        return completion

    def wrap_embedding(self, transport: Transport) -> Transport:
        async def embedding(**kwargs: Any) -> Any:
            return await self._invoke("embedding", transport, kwargs)

        return embedding

    def _reserve(self, kind: RequestKind, kwargs: dict[str, Any]) -> tuple[int, ModelRate, float]:
        with self._lock:
            if self._halt_reason is not None:
                raise WorkflowBudgetError("budget_halted")
            remaining = self.remaining_seconds()
            if remaining <= 0:
                self._deny("total_time_limit")
            model = kwargs.get("model")
            if not isinstance(model, str) or (kind, model) not in self._rates:
                self._deny("unpriced_request_model")
            rate = self._rates[(kind, model)]
            common = {"model", "api_base", "api_key", "timeout", "num_retries", "max_retries"}
            allowed = common | (
                {"messages", "max_tokens", "response_format", "extra_body"}
                if kind == "completion"
                else {"input", "dimensions", "allowed_openai_params"}
            )
            if set(kwargs) - allowed or any(
                not _integer(kwargs.get(key, 0)) or kwargs.get(key, 0) != 0
                for key in ("num_retries", "max_retries")
            ):
                self._deny("uncontrolled_transport_options")
            maximum_output = 0
            if kind == "completion":
                payload = kwargs.get("messages")
                if (
                    not isinstance(payload, list)
                    or not payload
                    or any(
                        not isinstance(message, dict)
                        or set(message) != {"role", "content"}
                        or message.get("role") not in {"system", "user", "assistant"}
                        or not isinstance(message.get("content"), str)
                        for message in payload
                    )
                    or kwargs.get("response_format") != {"type": "json_object"}
                ):
                    self._deny("unsupported_completion_input")
                extra = kwargs.get("extra_body", {})
                if (
                    not isinstance(extra, dict)
                    or set(extra) - {"enable_thinking"}
                    or ("enable_thinking" in extra and type(extra["enable_thinking"]) is not bool)
                ):
                    self._deny("uncontrolled_generation_options")
                requested_output = kwargs.get("max_tokens")
                if (
                    not _integer(requested_output)
                    or not 0 < requested_output <= self.limits.max_output_tokens_per_request
                ):
                    self._deny("output_request_limit")
                maximum_output = requested_output
            else:
                payload = kwargs.get("input")
                if (
                    not isinstance(payload, list)
                    or not payload
                    or any(not isinstance(text, str) or not text for text in payload)
                ):
                    self._deny("unsupported_embedding_input")
            input_items = len(payload) if kind == "embedding" else 1
            reserved_input_tokens = rate.input_capacity_tokens * input_items
            encoded = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            input_bytes = len(encoded)
            if (
                input_bytes > self.limits.max_input_bytes_per_request
                or self._input_bytes + input_bytes > self.limits.max_total_input_bytes
            ):
                self._deny("input_bytes_limit")
            if len(self._receipts) >= self.limits.max_requests:
                self._deny("request_count_limit")
            if (
                self._known_output_tokens + self._reserved_output_tokens + maximum_output
                > self.limits.max_total_output_tokens
            ):
                self._deny("output_total_limit")
            reserved = (
                reserved_input_tokens * rate.input_per_million
                + maximum_output * rate.output_per_million
            ) / MILLION
            if self._known_cost + self._reserved_cost + reserved > self.limits.max_total_cost:
                self._deny("cost_reservation_limit")
            timeout = kwargs.get("timeout", self.limits.max_request_seconds)
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
                self._deny("invalid_request_timeout")
            timeout = min(timeout, remaining, self.limits.max_request_seconds)
            kwargs.update(num_retries=0, max_retries=0, timeout=timeout)
            self._input_bytes += input_bytes
            self._reserved_cost += reserved
            self._reserved_output_tokens += maximum_output
            # Unknown usage is null, never a synthetic zero token bill.
            self._receipts.append(
                dict(
                    request_index=len(self._receipts),
                    kind=kind,
                    model=model,
                    status="reserved",
                    request_sha256=hashlib.sha256(encoded).hexdigest(),
                    input_bytes=input_bytes,
                    input_capacity_tokens=rate.input_capacity_tokens,
                    input_items=input_items,
                    reserved_input_tokens=reserved_input_tokens,
                    input_capacity_evidence_sha256=rate.input_capacity_evidence_sha256,
                    max_output_tokens=maximum_output,
                    timeout_seconds=timeout,
                    reservation_basis="full_declared_model_input_capacity",
                    reserved_cost=_amount(reserved),
                    cost=None,
                    token_usage=None,
                    response_model=None,
                    reported_usage=None,
                )
            )
            return len(self._receipts) - 1, rate, timeout

    def _uncertain(self, index: int, code: str) -> None:
        with self._lock:
            self._receipts[index].update(status="unknown", error_code=code)
            self._halt_reason = self._halt_reason or code

    async def _invoke(self, kind: RequestKind, transport: Transport, kwargs: dict[str, Any]) -> Any:
        index, rate, timeout = self._reserve(kind, kwargs)
        started = self._clock()
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                response = await transport(**kwargs)
        except asyncio.CancelledError:
            self._uncertain(index, "request_cancelled")
            raise
        except TimeoutError:
            self._uncertain(index, "request_timeout")
            raise WorkflowBudgetError("request_timeout") from None
        except Exception:
            self._uncertain(index, "transport_failure")
            raise WorkflowBudgetError("transport_failure") from None
        finally:
            with self._lock:
                self._receipts[index]["latency_seconds"] = max(0.0, self._clock() - started)
                self._receipts[index]["timeout_expired"] = deadline.expired()
        try:
            self._settle(index, rate, response)
        except WorkflowBudgetError:
            raise
        except Exception:
            self._uncertain(index, "invalid_provider_response")
            raise WorkflowBudgetError("invalid_provider_response") from None
        return response

    def _settle(self, index: int, rate: ModelRate, response: Any) -> None:
        with self._lock:
            returned_model = getattr(response, "model", None)
            receipt = self._receipts[index]
            receipt["response_model"] = (
                returned_model
                if isinstance(returned_model, str) and len(returned_model) <= 256
                else None
            )
            raw_usage = getattr(response, "usage", None)
            if raw_usage is not None and hasattr(raw_usage, "model_dump"):
                raw_usage = raw_usage.model_dump()
            usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
            prompt, output, total = (
                usage.get("prompt_tokens"),
                usage.get("completion_tokens", 0 if rate.kind == "embedding" else None),
                usage.get("total_tokens"),
            )
            receipt["reported_usage"] = {
                "prompt_tokens": prompt if _integer(prompt) else None,
                "completion_tokens": output if _integer(output) else None,
                "total_tokens": total if _integer(total) else None,
                "source": "provider_response_unvalidated",
            }
            if returned_model not in rate.response_models:
                self._uncertain(index, "response_model_mismatch")
                raise WorkflowBudgetError("response_model_mismatch")
            if (
                not _integer(prompt)
                or not _integer(output)
                or not _integer(total)
                or prompt <= 0
                or total != prompt + output
                or (rate.kind == "embedding" and output != 0)
            ):
                self._uncertain(index, "usage_unknown")
                raise WorkflowBudgetError("usage_unknown")
            cost = (prompt * rate.input_per_million + output * rate.output_per_million) / MILLION
            self._reserved_cost -= Decimal(receipt["reserved_cost"])
            self._reserved_output_tokens -= receipt["max_output_tokens"]
            self._known_cost += cost
            self._known_prompt_tokens += prompt
            self._known_output_tokens += output
            receipt.update(
                status="settled",
                cost=_amount(cost),
                token_usage={
                    "prompt_tokens": prompt,
                    "completion_tokens": output,
                    "total_tokens": total,
                    "source": "provider_reported",
                },
            )
            if (
                prompt > receipt["reserved_input_tokens"]
                or output > receipt["max_output_tokens"]
                or self._known_cost + self._reserved_cost > self.limits.max_total_cost
                or self.remaining_seconds() <= 0
                or receipt["latency_seconds"] > receipt["timeout_seconds"]
                or receipt["timeout_expired"]
            ):
                receipt.update(status="budget_breach", error_code="budget_breach")
                self._breach = True
                self._deny("budget_breach")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            elapsed = self._clock() - self._started
            unknown = sum(receipt["status"] == "unknown" for receipt in self._receipts)
            pending = sum(receipt["status"] == "reserved" for receipt in self._receipts)
            return {
                "schema_version": "openvigil.workflow-eval-budget.v1",
                "rate_card_sha256": self._rate_sha256,
                "currency": self.rate_card.currency,
                "limits": self.limits.model_dump(mode="json"),
                "elapsed_seconds": elapsed if math.isfinite(elapsed) and elapsed >= 0 else None,
                "halted": self._halt_reason is not None,
                "halt_reason": self._halt_reason,
                "budget_breach": self._breach,
                "requests": len(self._receipts),
                "input_bytes": self._input_bytes,
                "unknown_requests": unknown,
                "pending_requests": pending,
                "known_prompt_tokens": self._known_prompt_tokens,
                "known_output_tokens": self._known_output_tokens,
                "reserved_output_tokens": self._reserved_output_tokens,
                "known_cost_subtotal": _amount(self._known_cost),
                "reserved_cost": _amount(self._reserved_cost),
                "cost_exposure": _amount(self._known_cost + self._reserved_cost),
                "total_cost": None if unknown or pending else _amount(self._known_cost),
                "cost_status": "rate_card_estimate",
                "cost_complete": not unknown and not pending,
                "capacity_evidence_status": "declared",
                "validation_status": "UNVERIFIED"
                if unknown or pending or self._breach or self._halt_reason is not None
                else "COMPLETE",
                "billing_guarantee": False,
                "receipts": json.loads(json.dumps(self._receipts)),
            }
