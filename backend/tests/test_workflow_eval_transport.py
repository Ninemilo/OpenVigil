"""Real installed SDK dispatch/client construction with the final HTTP boundary replaced.

No socket is opened and no provider is billed. These tests verify retry and routing
configuration for the frozen LiteLLM/OpenAI versions, not vendor request behavior.
"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from windops_backend.config import Settings
from windops_backend.operations.workflow_eval_transport import (
    SUPPORTED_SDK_VERSIONS,
    WorkflowTransportIsolationError,
    isolated_workflow_transport,
)


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("WINDOPS_WORKFLOW_EVAL_CHILD", "1")
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    return Settings(
        _env_file=None,
        environment="development",
        agent_mode="litellm",
        litellm_model="openai/gpt-4o-mini",
        embedding_model="text-embedding-3-small",
    )


@pytest.mark.asyncio
async def test_real_sdk_completion_and_embedding_construct_zero_retry_clients(
    settings, monkeypatch
):
    import litellm
    import litellm.llms.openai.openai as adapter
    from litellm.types.utils import EmbeddingResponse, ModelResponse

    log = SimpleNamespace(
        pre_call=lambda **kwargs: None,
        post_call=lambda **kwargs: None,
        model_call_details={},
        dynamic_success_callbacks=[],
    )
    observed = []

    async def refuse_network(*args, **kwargs):
        client = kwargs["openai_aclient"]
        observed.append(client.max_retries)
        # This is the final provider HTTP request boundary, after real SDK client creation.
        raise RuntimeError("synthetic final network boundary")

    monkeypatch.setattr(
        adapter.OpenAIChatCompletion, "make_openai_chat_completion_request", refuse_network
    )
    monkeypatch.setattr(
        adapter.OpenAIChatCompletion, "make_openai_embedding_request", refuse_network
    )
    sdk_client = adapter.OpenAIChatCompletion()
    before = litellm.DEFAULT_MAX_RETRIES
    with isolated_workflow_transport(settings) as isolated:
        proof = isolated.evidence()
        assert proof["sdk_versions"] == SUPPORTED_SDK_VERSIONS
        assert len(proof["openai_adapter_sha256"]) == 64
        for kind in ("completion", "embedding"):
            if kind == "completion":
                pending = sdk_client.completion(
                    model_response=ModelResponse(),
                    timeout=1.0,
                    optional_params={"max_retries": 0},
                    litellm_params={},
                    logging_obj=log,
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "synthetic"}],
                    api_key="synthetic-only",
                    acompletion=True,
                )
            else:
                pending = sdk_client.embedding(
                    model="text-embedding-3-small",
                    input=["synthetic"],
                    timeout=1.0,
                    logging_obj=log,
                    model_response=EmbeddingResponse(),
                    optional_params={},
                    api_key="synthetic-only",
                    aembedding=True,
                    max_retries=0,
                )
            with pytest.raises(adapter.OpenAIError, match="synthetic final network boundary"):
                await pending
    assert observed == [0, 0]
    assert litellm.DEFAULT_MAX_RETRIES == before


def test_embedding_sdk_zero_would_otherwise_be_replaced_by_default(settings, monkeypatch):
    import litellm
    import litellm.llms.openai.openai as adapter
    from litellm.types.utils import EmbeddingResponse

    monkeypatch.setattr(litellm, "DEFAULT_MAX_RETRIES", 2)
    captured = []
    sdk_client = adapter.OpenAIChatCompletion()
    monkeypatch.setattr(
        sdk_client, "aembedding", lambda **kwargs: captured.append(kwargs["max_retries"])
    )
    arguments = dict(
        model="text-embedding-3-small",
        input=["synthetic"],
        timeout=1.0,
        logging_obj=SimpleNamespace(pre_call=lambda **kwargs: None),
        model_response=EmbeddingResponse(),
        optional_params={},
        api_key="synthetic-only",
        aembedding=True,
        max_retries=0,
    )
    sdk_client.embedding(**arguments)
    with isolated_workflow_transport(settings):
        sdk_client.embedding(**arguments)
    sdk_client.embedding(**arguments)
    assert captured == [2, 0, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["completion", "embedding"])
async def test_real_top_level_sdk_dispatch_has_one_zero_retry_http_attempt(
    settings, monkeypatch, kind
):
    import litellm
    import litellm.llms.openai.openai as adapter

    attempts = []

    async def refuse_network(*args, **kwargs):
        attempts.append(kwargs["openai_aclient"].max_retries)
        raise RuntimeError("synthetic final network boundary")

    monkeypatch.setattr(
        adapter.OpenAIChatCompletion, "make_openai_chat_completion_request", refuse_network
    )
    monkeypatch.setattr(
        adapter.OpenAIChatCompletion, "make_openai_embedding_request", refuse_network
    )
    arguments = dict(api_key="synthetic-only", num_retries=0, max_retries=0, timeout=1)
    if kind == "completion":
        arguments.update(
            model=settings.litellm_model,
            messages=[{"role": "user", "content": "synthetic"}],
            response_format={"type": "json_object"},
            max_tokens=8,
        )
    else:
        arguments.update(model=settings.embedding_model, input=["synthetic"])
    with isolated_workflow_transport(settings) as isolated:
        with pytest.raises(litellm.InternalServerError, match="synthetic final network boundary"):
            await getattr(isolated, kind)(**arguments)
    assert attempts == [0]


def test_controls_and_original_transports_restore_even_when_caller_raises(settings, monkeypatch):
    import litellm
    import litellm.llms.openai.openai as adapter

    original_completion, original_embedding = litellm.acompletion, litellm.aembedding
    original_client = adapter.OpenAIChatCompletion._get_openai_client
    original_callbacks = ["synthetic callback"]
    original_fallbacks = [{"synthetic": ["another"]}]
    monkeypatch.setattr(litellm, "callbacks", original_callbacks)
    monkeypatch.setattr(litellm, "model_fallbacks", original_fallbacks)
    monkeypatch.setenv("DEFAULT_MAX_RETRIES", "7")
    with pytest.raises(RuntimeError, match="synthetic caller failure"):
        with isolated_workflow_transport(settings) as isolated:
            assert litellm.callbacks == [] and litellm.model_fallbacks is None
            assert os.environ["DEFAULT_MAX_RETRIES"] == "0"
            litellm.acompletion = isolated.completion
            litellm.aembedding = isolated.embedding
            raise RuntimeError("synthetic caller failure")
    assert litellm.callbacks is original_callbacks
    assert litellm.model_fallbacks is original_fallbacks
    assert litellm.acompletion is original_completion
    assert litellm.aembedding is original_embedding
    assert adapter.OpenAIChatCompletion._get_openai_client is original_client
    assert os.environ["DEFAULT_MAX_RETRIES"] == "7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option", ["model_list", "client", "fallbacks", "retry_policy", "metadata", "mock_response"]
)
async def test_request_router_and_fallback_options_are_refused_before_transport(settings, option):
    with isolated_workflow_transport(settings) as isolated:
        with pytest.raises(WorkflowTransportIsolationError, match="uncontrolled SDK request"):
            await isolated.completion(
                model=settings.litellm_model,
                num_retries=0,
                max_retries=0,
                **{option: "synthetic unsupported route"},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control",
    [
        "DEFAULT_MAX_RETRIES",
        "model_fallbacks",
        "callbacks",
        "model_alias_map",
        "drop_params",
        "route_all_chat_openai_to_responses",
    ],
)
async def test_changed_global_controls_fail_before_provider_call(settings, control):
    import litellm

    with isolated_workflow_transport(settings) as isolated:
        if control == "callbacks":
            litellm.callbacks.append("synthetic illicit callback")
        elif control == "model_alias_map":
            litellm.model_alias_map["synthetic"] = "other"
        else:
            setattr(litellm, control, 2 if control == "DEFAULT_MAX_RETRIES" else True)
        with pytest.raises(WorkflowTransportIsolationError, match="controls changed"):
            await isolated.embedding(
                model=settings.embedding_model, input=["synthetic"], num_retries=0, max_retries=0
            )


def test_existing_router_bound_transport_is_rejected_and_controls_restore(settings, monkeypatch):
    import litellm

    class Router:
        async def acompletion(self, **kwargs):
            raise AssertionError("must not invoke Router")

    original = Router().acompletion
    default = litellm.DEFAULT_MAX_RETRIES
    monkeypatch.setattr(litellm, "acompletion", original)
    with pytest.raises(WorkflowTransportIsolationError, match="router transport"):
        with isolated_workflow_transport(settings):
            pytest.fail("unsupported Router was accepted")
    assert litellm.acompletion is original
    assert litellm.DEFAULT_MAX_RETRIES == default


def test_unreviewed_provider_route_and_sdk_version_fail_closed(settings, monkeypatch):
    import windops_backend.operations.workflow_eval_transport as module

    unsupported = settings.model_copy(update={"litellm_model": "anthropic/claude-opus-4"})
    with pytest.raises(WorkflowTransportIsolationError, match="only verified direct"):
        with isolated_workflow_transport(unsupported):
            pytest.fail("unsupported provider was accepted")
    responses = settings.model_copy(update={"litellm_model": "openai/responses/gpt-4o-mini"})
    with pytest.raises(WorkflowTransportIsolationError, match="Responses SDK route"):
        with isolated_workflow_transport(responses):
            pytest.fail("unverified Responses route was accepted")
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: "future-version")
    with pytest.raises(WorkflowTransportIsolationError, match="SDK version"):
        with isolated_workflow_transport(settings):
            pytest.fail("unknown SDK version was accepted")


@pytest.mark.asyncio
async def test_parent_nested_and_expired_contexts_are_refused(settings, monkeypatch):
    monkeypatch.delenv("WINDOPS_WORKFLOW_EVAL_CHILD")
    with pytest.raises(WorkflowTransportIsolationError, match="dedicated child"):
        with isolated_workflow_transport(settings):
            pytest.fail("parent was accepted")
    monkeypatch.setenv("WINDOPS_WORKFLOW_EVAL_CHILD", "1")
    with isolated_workflow_transport(settings) as isolated:
        with pytest.raises(WorkflowTransportIsolationError, match="dedicated child"):
            with isolated_workflow_transport(settings):
                pytest.fail("nested context was accepted")
    with pytest.raises(WorkflowTransportIsolationError, match="no longer active"):
        await isolated.completion(model=settings.litellm_model, num_retries=0, max_retries=0)


def test_underlying_client_cannot_inherit_hidden_retry_budget(settings):
    import litellm.llms.openai.openai as adapter

    with isolated_workflow_transport(settings):
        with pytest.raises(WorkflowTransportIsolationError, match="uncontrolled"):
            adapter.OpenAIChatCompletion()._get_openai_client(
                is_async=True, max_retries=2, api_key="synthetic-only"
            )


def test_first_import_in_dedicated_child_does_not_attempt_network(settings):
    script = """
import os
import socket
attempts = []
def deny(*args, **kwargs):
    attempts.append(True)
    raise AssertionError('unexpected network attempt')
socket.socket.connect = deny
socket.getaddrinfo = deny
from windops_backend.config import Settings
from windops_backend.operations.workflow_eval_transport import isolated_workflow_transport
settings = Settings(_env_file=None, environment='development', agent_mode='litellm',
                    litellm_model='openai/gpt-4o-mini', embedding_model='text-embedding-3-small')
assert 'LITELLM_LOCAL_MODEL_COST_MAP' not in os.environ
with isolated_workflow_transport(settings) as transport:
    assert transport.evidence()['sdk_max_retries'] == 0
    assert os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] == 'True'
assert 'LITELLM_LOCAL_MODEL_COST_MAP' not in os.environ
assert attempts == []
"""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("WINDOPS_")
    }
    environment.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    environment["WINDOPS_WORKFLOW_EVAL_CHILD"] = "1"
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
