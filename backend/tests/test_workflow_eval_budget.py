"""No network: exercise actual production providers with declared SDK-boundary fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from windops_backend.agents.embeddings import (
    EMBEDDING_DIMENSIONS,
    LiteLLMEmbeddingProvider,
    embed_texts_with_controls,
)
from windops_backend.agents.reasoning import LiteLLMReasoningProvider
from windops_backend.operations.workflow_eval_budget import (
    FrozenRateCard,
    ModelRate,
    WorkflowBudgetError,
    WorkflowBudgetLimits,
    WorkflowEvalBudget,
)
from windops_backend.schemas import PublicDiagnosis


def rate_card() -> FrozenRateCard:
    return FrozenRateCard(
        schema_version="openvigil.workflow-eval-rate-card.v1",
        currency="USD",
        source="declared unit-test pricing only, not a real model tariff",
        verified_at=datetime(2026, 10, 23, tzinfo=UTC),
        rates=(
            ModelRate(
                kind="completion",
                model="fixture/reasoning",
                response_models=("fixture/reasoning", "fixture-explicit-alias"),
                input_per_million=Decimal("2"),
                output_per_million=Decimal("4"),
                input_capacity_tokens=20_000,
                input_capacity_source="synthetic declared full capacity",
                input_capacity_evidence_sha256="a" * 64,
            ),
            ModelRate(
                kind="embedding",
                model="fixture/embedding",
                response_models=("fixture/embedding",),
                input_per_million=Decimal("0.5"),
                output_per_million=Decimal("0"),
                input_capacity_tokens=10_000,
                input_capacity_source="synthetic declared full capacity",
                input_capacity_evidence_sha256="b" * 64,
            ),
        ),
    )


def limits(**updates: Any) -> WorkflowBudgetLimits:
    return WorkflowBudgetLimits.model_validate(
        {
            "max_requests": 20,
            "max_input_bytes_per_request": 100_000,
            "max_total_input_bytes": 1_000_000,
            "max_output_tokens_per_request": 128,
            "max_total_output_tokens": 2_000,
            "max_total_cost": "1",
            "max_request_seconds": 5,
            "max_total_seconds": 60,
            **updates,
        }
    )


def reasoning(**updates: Any) -> LiteLLMReasoningProvider:
    return LiteLLMReasoningProvider(
        **{
            "model": "fixture/reasoning",
            "timeout_seconds": 5,
            "max_retries": 0,
            "minimum_diagnosis_confidence": 0,
            "max_output_tokens": 128,
            "api_base": "https://fixture.invalid/v1",
            "api_key": SecretStr("PRIVATE-UNIT-KEY"),
            **updates,
        }
    )


async def diagnose(provider: LiteLLMReasoningProvider) -> PublicDiagnosis:
    return await provider.generate(
        PublicDiagnosis,
        "核对公开证据 PRIVATE-UNIT-PROMPT",
        {
            "analysis_profile": {"component": "main_bearing"},
            "evidence": [{"evidence_id": "EVD-SYNTHETIC", "summary": "缺少实测数据"}],
        },
    )


def completion_response(**updates: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "model": "fixture/reasoning",
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            "choices": [
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "component": "main_bearing",
                                "failure_mode": "insufficient_evidence",
                                "confidence": 0,
                                "conclusion": "Public fixture requires human review.",
                                "evidence_refs": ["EVD-SYNTHETIC"],
                            }
                        )
                    ),
                )
            ],
            **updates,
        }
    )


def embedding_response(**updates: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "model": "fixture/embedding",
            "usage": {"prompt_tokens": 50, "total_tokens": 50},
            "data": [{"index": 0, "embedding": [0.1] * EMBEDDING_DIMENSIONS}],
            **updates,
        }
    )


def install(monkeypatch, budget, completion=None, embedding=None):
    calls = []

    async def complete(**kwargs):
        calls.append(("completion", kwargs))
        return await completion(**kwargs) if completion else completion_response()

    async def embed(**kwargs):
        calls.append(("embedding", kwargs))
        return await embedding(**kwargs) if embedding else embedding_response()

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            acompletion=budget.wrap_completion(complete),
            aembedding=budget.wrap_embedding(embed),
        ),
    )
    return calls


async def test_production_llm_and_embedding_reserve_before_transport_then_account_actual_usage(
    monkeypatch,
):
    budget = WorkflowEvalBudget(rate_card(), limits())
    observations = []

    async def completion(**_kwargs):
        observations.append(budget.snapshot())
        return completion_response()

    async def embedding(**_kwargs):
        observations.append(budget.snapshot())
        return embedding_response()

    calls = install(monkeypatch, budget, completion, embedding)
    assert (await diagnose(reasoning())).failure_mode == "insufficient_evidence"
    assert len(await LiteLLMEmbeddingProvider("fixture/embedding").embed(["风塔查询"])) == 1
    assert [item["pending_requests"] for item in observations] == [1, 1]
    assert Decimal(observations[0]["reserved_cost"]) == Decimal("0.040512")
    assert Decimal(observations[1]["reserved_cost"]) == Decimal("0.005")
    assert all(options["num_retries"] == options["max_retries"] == 0 for _, options in calls)
    snapshot = budget.snapshot()
    assert snapshot["requests"] == 2 and snapshot["known_prompt_tokens"] == 70
    assert snapshot["known_output_tokens"] == 10 and snapshot["reserved_output_tokens"] == 0
    assert Decimal(snapshot["total_cost"]) == Decimal("0.000105")
    assert snapshot["cost_status"] == "rate_card_estimate"
    assert snapshot["capacity_evidence_status"] == "declared" and not snapshot["billing_guarantee"]
    payload = json.dumps(
        calls[0][1]["messages"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert snapshot["receipts"][0]["input_bytes"] == len(payload)
    assert snapshot["receipts"][0]["request_sha256"] == hashlib.sha256(payload).hexdigest()
    assert snapshot["receipts"][0]["response_model"] == "fixture/reasoning"
    assert "PRIVATE-UNIT" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    "limit_update,provider_update,code",
    [
        ({"max_total_cost": "0.04"}, {}, "cost_reservation_limit"),
        ({"max_input_bytes_per_request": 1}, {}, "input_bytes_limit"),
        ({"max_total_output_tokens": 100}, {}, "output_total_limit"),
        ({}, {"max_output_tokens": None}, "output_request_limit"),
        ({}, {"max_output_tokens": 129}, "output_request_limit"),
        ({}, {"max_retries": 1}, "uncontrolled_transport_options"),
        ({}, {"model": "unpriced/model"}, "unpriced_request_model"),
    ],
)
async def test_budget_refusal_happens_before_any_real_transport_call(
    monkeypatch, limit_update, provider_update, code
):
    budget = WorkflowEvalBudget(rate_card(), limits(**limit_update))
    calls = install(monkeypatch, budget)
    with pytest.raises(WorkflowBudgetError, match=code):
        await diagnose(reasoning(**provider_update))
    assert calls == []
    snapshot = budget.snapshot()
    assert snapshot["requests"] == 0 and Decimal(snapshot["reserved_cost"]) == 0


async def test_concurrent_reservations_cannot_oversubscribe_shared_money(monkeypatch):
    budget = WorkflowEvalBudget(rate_card(), limits(max_total_cost="0.05"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def completion(**_kwargs):
        entered.set()
        await release.wait()
        return completion_response()

    calls = install(monkeypatch, budget, completion)
    first = asyncio.create_task(diagnose(reasoning()))
    await entered.wait()
    try:
        with pytest.raises(WorkflowBudgetError, match="cost_reservation_limit"):
            await diagnose(reasoning())
        assert len(calls) == 1
        assert budget.snapshot()["pending_requests"] == 1
    finally:
        release.set()
        await first
    assert budget.snapshot()["requests"] == 1


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        {"prompt_tokens": True, "completion_tokens": 10, "total_tokens": 11},
        {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 31},
        {"prompt_tokens": "20", "completion_tokens": 10, "total_tokens": 30},
    ],
)
async def test_unknown_llm_usage_retains_reservation_and_stops_following_embedding(
    monkeypatch, usage
):
    budget = WorkflowEvalBudget(rate_card(), limits())

    async def completion(**_kwargs):
        return completion_response(usage=usage)

    calls = install(monkeypatch, budget, completion)
    with pytest.raises(WorkflowBudgetError, match="usage_unknown"):
        await diagnose(reasoning())
    with pytest.raises(WorkflowBudgetError, match="budget_halted"):
        await LiteLLMEmbeddingProvider("fixture/embedding").embed(["query"])
    snapshot = budget.snapshot()
    assert len(calls) == snapshot["unknown_requests"] == 1
    assert snapshot["total_cost"] is None and not snapshot["cost_complete"]
    assert Decimal(snapshot["reserved_cost"]) == Decimal("0.040512")
    assert snapshot["receipts"][0]["token_usage"] is None


@pytest.mark.parametrize("kind", ["missing", "zero", "wrong_model", "timeout", "transport_error"])
async def test_embedding_unknown_bills_stop_even_the_existing_application_retry_loop(
    monkeypatch, kind
):
    budget = WorkflowEvalBudget(rate_card(), limits(max_request_seconds=0.03))

    async def embedding(**_kwargs):
        if kind == "timeout":
            await asyncio.Event().wait()
        if kind == "transport_error":
            raise RuntimeError("PRIVATE-TRANSPORT-SECRET")
        return embedding_response(
            **{
                "missing": {"usage": None},
                "zero": {"usage": {"prompt_tokens": 0, "total_tokens": 0}},
                "wrong_model": {"model": "unexpected/model"},
            }.get(kind, {})
        )

    calls = install(monkeypatch, budget, embedding=embedding)
    with pytest.raises(RuntimeError):
        await embed_texts_with_controls(
            LiteLLMEmbeddingProvider("fixture/embedding"),
            ["query"],
            settings=SimpleNamespace(
                embedding_batch_size=2, embedding_max_retries=1, embedding_timeout_seconds=5
            ),
        )
    snapshot = budget.snapshot()
    assert len(calls) == snapshot["requests"] == 1
    assert snapshot["unknown_requests"] == 1 and snapshot["total_cost"] is None
    assert Decimal(snapshot["reserved_cost"]) == Decimal("0.005")
    assert "PRIVATE-TRANSPORT" not in json.dumps(snapshot)


async def test_paid_invalid_vector_retry_is_reserved_and_counted_again(monkeypatch):
    budget = WorkflowEvalBudget(rate_card(), limits())
    attempt = 0

    async def embedding(**_kwargs):
        nonlocal attempt
        attempt += 1
        return (
            embedding_response(data=[{"index": 0, "embedding": [0.1]}])
            if attempt == 1
            else embedding_response()
        )

    calls = install(monkeypatch, budget, embedding=embedding)
    result = await embed_texts_with_controls(
        LiteLLMEmbeddingProvider("fixture/embedding"),
        ["query"],
        settings=SimpleNamespace(
            embedding_batch_size=2, embedding_max_retries=1, embedding_timeout_seconds=5
        ),
    )
    assert len(result[0]) == EMBEDDING_DIMENSIONS
    snapshot = budget.snapshot()
    assert len(calls) == snapshot["requests"] == 2
    assert snapshot["known_prompt_tokens"] == 100
    assert Decimal(snapshot["total_cost"]) == Decimal("0.00005")


async def test_llm_response_model_requires_explicit_alias_and_malformed_paid_output_still_costs(
    monkeypatch,
):
    budget = WorkflowEvalBudget(rate_card(), limits())

    async def completion(**_kwargs):
        return completion_response(
            model="fixture-explicit-alias",
            choices=[SimpleNamespace(message=SimpleNamespace(content="invalid JSON"))],
        )

    calls = install(monkeypatch, budget, completion)
    with pytest.raises(ValidationError):
        await diagnose(reasoning())
    assert len(calls) == 1
    assert Decimal(budget.snapshot()["total_cost"]) == Decimal("0.00008")
    assert budget.snapshot()["receipts"][0]["response_model"] == "fixture-explicit-alias"


@pytest.mark.parametrize("prompt,output", [(20001, 10), (20, 129)])
async def test_reported_usage_over_reservation_is_a_visible_breach_not_a_budget_guarantee(
    monkeypatch, prompt, output
):
    budget = WorkflowEvalBudget(rate_card(), limits())

    async def completion(**_kwargs):
        return completion_response(
            usage={
                "prompt_tokens": prompt,
                "completion_tokens": output,
                "total_tokens": prompt + output,
            }
        )

    calls = install(monkeypatch, budget, completion)
    with pytest.raises(WorkflowBudgetError, match="budget_breach"):
        await diagnose(reasoning())
    snapshot = budget.snapshot()
    assert snapshot["budget_breach"] and snapshot["halted"] and not snapshot["billing_guarantee"]
    assert snapshot["known_prompt_tokens"] == prompt and snapshot["known_output_tokens"] == output
    assert Decimal(snapshot["total_cost"]) > 0 and len(calls) == 1


async def test_cancellation_keeps_an_unknown_reservation(monkeypatch):
    budget = WorkflowEvalBudget(rate_card(), limits())
    entered = asyncio.Event()

    async def embedding(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    calls = install(monkeypatch, budget, embedding=embedding)
    task = asyncio.create_task(LiteLLMEmbeddingProvider("fixture/embedding").embed(["query"]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == budget.snapshot()["unknown_requests"] == 1
    assert budget.snapshot()["halt_reason"] == "request_cancelled"


async def test_request_count_and_total_bytes_are_not_refunded_after_known_success(monkeypatch):
    for constraints, code in [
        ({"max_requests": 1}, "request_count_limit"),
        ({"max_total_input_bytes": 7}, "input_bytes_limit"),
    ]:
        budget = WorkflowEvalBudget(rate_card(), limits(**constraints))
        calls = install(monkeypatch, budget)
        await LiteLLMEmbeddingProvider("fixture/embedding").embed(["风"])
        with pytest.raises(WorkflowBudgetError, match=code):
            await LiteLLMEmbeddingProvider("fixture/embedding").embed(["风"])
        assert len(calls) == 1 and budget.snapshot()["input_bytes"] == 7


async def test_expired_total_time_refuses_before_transport(monkeypatch):
    clock = [1.0]
    budget = WorkflowEvalBudget(rate_card(), limits(max_total_seconds=2), clock=lambda: clock[0])
    calls = install(monkeypatch, budget)
    clock[0] = 3.0
    assert budget.remaining_seconds() == 0
    with pytest.raises(WorkflowBudgetError, match="total_time_limit"):
        await diagnose(reasoning())
    assert calls == []


async def test_local_work_after_last_response_must_pass_explicit_deadline(monkeypatch):
    clock = [10.0]
    budget = WorkflowEvalBudget(rate_card(), limits(max_total_seconds=2), clock=lambda: clock[0])
    calls = install(monkeypatch, budget)
    await diagnose(reasoning())
    clock[0] = 11.0
    budget.check_deadline()
    assert budget.snapshot()["validation_status"] == "COMPLETE"
    clock[0] = 12.0
    with pytest.raises(WorkflowBudgetError, match="total_deadline_exhausted"):
        budget.check_deadline()
    snapshot = budget.snapshot()
    assert snapshot["elapsed_seconds"] == 2
    assert snapshot["validation_status"] == "UNVERIFIED"
    assert snapshot["halt_reason"] == "total_deadline_exhausted"
    assert snapshot["cost_complete"] and snapshot["requests"] == 1
    assert Decimal(snapshot["total_cost"]) == Decimal("0.00008")
    with pytest.raises(WorkflowBudgetError, match="budget_halted"):
        await diagnose(reasoning())
    assert len(calls) == 1


def test_snapshot_during_cleanup_reports_elapsed_without_mutating_a_work_deadline():
    clock = [10.0]
    budget = WorkflowEvalBudget(rate_card(), limits(max_total_seconds=2), clock=lambda: clock[0])
    clock[0] = 11.0
    budget.check_deadline()
    clock[0] = 13.0
    snapshot = budget.snapshot()
    assert snapshot["elapsed_seconds"] == 3
    assert not snapshot["halted"]


async def test_embedding_batch_reserves_full_capacity_for_each_input_before_transport(monkeypatch):
    budget = WorkflowEvalBudget(rate_card(), limits(max_total_cost="0.0075"))
    calls = install(monkeypatch, budget)
    with pytest.raises(WorkflowBudgetError, match="cost_reservation_limit"):
        await LiteLLMEmbeddingProvider("fixture/embedding").embed(["first", "second"])
    assert not calls
    assert budget.snapshot()["requests"] == 0


async def test_embedding_batch_settles_total_usage_against_all_item_reservations(monkeypatch):
    budget = WorkflowEvalBudget(rate_card(), limits(max_total_cost="0.01"))
    observed = []

    async def embedding(**_kwargs):
        observed.append(budget.snapshot())
        return embedding_response(
            usage={"prompt_tokens": 15_000, "total_tokens": 15_000},
            data=[
                {"index": index, "embedding": [0.1] * EMBEDDING_DIMENSIONS} for index in range(2)
            ],
        )

    calls = install(monkeypatch, budget, embedding=embedding)
    vectors = await LiteLLMEmbeddingProvider("fixture/embedding").embed(["first", "second"])
    assert len(vectors) == 2 and len(calls) == 1
    receipt = observed[0]["receipts"][0]
    assert receipt["input_items"] == 2 and receipt["reserved_input_tokens"] == 20_000
    assert Decimal(receipt["reserved_cost"]) == Decimal("0.01")
    settled = budget.snapshot()
    assert settled["known_prompt_tokens"] == 15_000
    assert Decimal(settled["total_cost"]) == Decimal("0.0075")
    assert not settled["budget_breach"] and Decimal(settled["reserved_cost"]) == 0


async def test_inflight_known_response_still_settles_after_another_request_becomes_unknown(
    monkeypatch,
):
    budget = WorkflowEvalBudget(rate_card(), limits())
    entered, release = asyncio.Event(), asyncio.Event()

    async def completion(**_kwargs):
        entered.set()
        await release.wait()
        return completion_response()

    async def embedding(**_kwargs):
        return embedding_response(usage=None)

    calls = install(monkeypatch, budget, completion, embedding)
    task = asyncio.create_task(diagnose(reasoning()))
    await entered.wait()
    try:
        with pytest.raises(WorkflowBudgetError, match="usage_unknown"):
            await LiteLLMEmbeddingProvider("fixture/embedding").embed(["query"])
    finally:
        release.set()
        await task
    snapshot = budget.snapshot()
    assert len(calls) == snapshot["requests"] == 2
    assert snapshot["known_prompt_tokens"] == 20 and snapshot["unknown_requests"] == 1
    assert snapshot["pending_requests"] == 0 and snapshot["total_cost"] is None
    assert Decimal(snapshot["known_cost_subtotal"]) == Decimal("0.00008")
    assert Decimal(snapshot["reserved_cost"]) == Decimal("0.005")
    snapshot["receipts"][0]["cost"] = "0"
    assert Decimal(budget.snapshot()["receipts"][0]["cost"]) == Decimal("0.00008")


async def test_transport_that_suppresses_cancellation_cannot_hide_a_request_time_breach(
    monkeypatch,
):
    budget = WorkflowEvalBudget(rate_card(), limits(max_request_seconds=0.01))

    async def completion(**_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return completion_response()

    calls = install(monkeypatch, budget, completion)
    with pytest.raises(WorkflowBudgetError, match="budget_breach"):
        await diagnose(reasoning())
    assert len(calls) == 1 and budget.snapshot()["budget_breach"]
    assert Decimal(budget.snapshot()["total_cost"]) == Decimal("0.00008")


@pytest.mark.parametrize(
    "change",
    ["no_embedding", "no_capacity", "no_provenance", "bad_evidence", "naive_time", "nan_price"],
)
def test_rate_card_needs_both_models_with_explicit_declared_capacity_provenance(change):
    data = rate_card().model_dump(mode="json")
    if change == "no_embedding":
        data["rates"] = data["rates"][:1]
    elif change == "no_capacity":
        del data["rates"][0]["input_capacity_tokens"]
    elif change == "no_provenance":
        data["rates"][0]["input_capacity_source"] = " "
    elif change == "bad_evidence":
        data["rates"][0]["input_capacity_evidence_sha256"] = "not-a-hash"
    elif change == "naive_time":
        data["verified_at"] = "2026-10-23T10:00:00"
    elif change == "nan_price":
        data["rates"][0]["input_per_million"] = "NaN"
    with pytest.raises(ValidationError):
        FrozenRateCard.model_validate(data)
