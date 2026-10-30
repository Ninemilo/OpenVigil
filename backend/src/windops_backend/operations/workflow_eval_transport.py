"""Isolate the verified direct OpenAI SDK route inside an evaluation child.

LiteLLM 1.96.2 treats embedding ``max_retries=0`` as falsy and substitutes its
default (normally 2). Set that default before import and at runtime, and check the
actual OpenAI client before any request. Disable routing, fallbacks, callbacks,
caches and parameter-repair retries. Other SDK versions/routes fail closed until
their network-free boundary tests have been reviewed. No vendor call occurs here.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import inspect
import os
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from windops_backend.agents.runtime import prepare_reasoning_runtime
from windops_backend.config import Settings
from windops_backend.operations.workflow_eval_budget import Transport

SUPPORTED_SDK_VERSIONS = {"litellm": "1.96.2", "openai": "2.54.0"}
_ACTIVE = False
_CONTROLS: dict[str, Any] = {
    "DEFAULT_MAX_RETRIES": 0,
    "num_retries": 0,
    "num_retries_per_request": 0,
    "max_fallbacks": 0,
    "model_fallbacks": None,
    "fallbacks": None,
    "default_fallbacks": None,
    "context_window_fallbacks": None,
    "content_policy_fallbacks": None,
    "model_alias_map": {},
    "custom_provider_map": [],
    "config_path": None,
    "route_all_chat_openai_to_responses": False,
    "use_litellm_proxy": False,
    "callbacks": [],
    "input_callback": [],
    "success_callback": [],
    "failure_callback": [],
    "_async_input_callback": [],
    "_async_success_callback": [],
    "_async_failure_callback": [],
    "cache": None,
    "drop_params": False,
    "modify_params": False,
    "retry": False,
    "telemetry": False,
    "suppress_debug_info": True,
    "disable_aiohttp_transport": True,
    "client_session": None,
    "aclient_session": None,
}
_ROUTING_OPTIONS = {
    "client",
    "router",
    "model_list",
    "fallbacks",
    "default_fallbacks",
    "context_window_fallback_dict",
    "context_window_fallbacks",
    "content_policy_fallbacks",
    "retry_policy",
    "metadata",
    "litellm_metadata",
    "mock_response",
    "caching",
    "cache",
    "custom_llm_provider",
    "drop_params",
    "stream",
    "tools",
    "functions",
}


class WorkflowTransportIsolationError(RuntimeError):
    """Fixed messages only; never include SDK inputs, endpoints or credentials."""


class IsolatedWorkflowTransport:
    def __init__(self, sdk: Any, settings: Settings, controls: dict[str, Any]) -> None:
        self._sdk = sdk
        self._controls = deepcopy(controls)
        self._active = True
        self._completion = sdk.acompletion
        self._embedding = sdk.aembedding
        model, base, _key = settings.reasoning_connection()
        self._routes = {
            "completion": (model, base),
            "embedding": (settings.embedding_model, settings.embedding_api_base or None),
        }
        for kind, function in (("completion", self._completion), ("embedding", self._embedding)):
            original = inspect.unwrap(function)
            if (
                inspect.ismethod(function)
                or getattr(original, "__module__", None) != "litellm.main"
                or getattr(original, "__name__", None) != "a" + kind
            ):
                raise WorkflowTransportIsolationError("custom or router transport is unsupported")
            self._validate_route(kind, *self._routes[kind])

    def _validate_route(self, kind: str, model: str, base: str | None) -> None:
        try:
            name, provider, _key, _base = self._sdk.get_llm_provider(model=model, api_base=base)
        except Exception:
            raise WorkflowTransportIsolationError("unverified SDK provider route") from None
        if provider != "openai":
            raise WorkflowTransportIsolationError(
                "only verified direct OpenAI routes are supported"
            )
        if kind == "completion":
            main = importlib.import_module("litellm.main")
            info, _ = main.responses_api_bridge_check(model=name, custom_llm_provider=provider)
            if info.get("mode") == "responses":
                raise WorkflowTransportIsolationError("Responses SDK route is unverified")

    def _check(self) -> None:
        if not self._active:
            raise WorkflowTransportIsolationError("evaluation SDK isolation is no longer active")
        if any(getattr(self._sdk, key) != value for key, value in self._controls.items()):
            raise WorkflowTransportIsolationError("evaluation SDK isolation controls changed")

    async def _invoke(self, kind: str, transport: Transport, kwargs: dict[str, Any]) -> Any:
        self._check()
        model, base = self._routes[kind]
        if (
            set(kwargs) & _ROUTING_OPTIONS
            or kwargs.get("model") != model
            or kwargs.get("api_base") != base
            or type(kwargs.get("num_retries")) is not int
            or kwargs["num_retries"] != 0
            or type(kwargs.get("max_retries")) is not int
            or kwargs["max_retries"] != 0
        ):
            raise WorkflowTransportIsolationError("uncontrolled SDK request route or retries")
        self._validate_route(kind, model, base)
        return await transport(**kwargs)

    async def completion(self, **kwargs: Any) -> Any:
        return await self._invoke("completion", self._completion, kwargs)

    async def embedding(self, **kwargs: Any) -> Any:
        return await self._invoke("embedding", self._embedding, kwargs)

    def evidence(self) -> dict[str, Any]:
        self._check()
        module = importlib.import_module("litellm.llms.openai.openai")
        source = module.__file__
        if not isinstance(source, str):
            raise WorkflowTransportIsolationError("SDK adapter source is unavailable")
        path = Path(source)
        return {
            "sdk_versions": dict(SUPPORTED_SDK_VERSIONS),
            "openai_adapter_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "provider_route": "direct_openai_compatible",
            "sdk_max_retries": 0,
            "fallbacks_enabled": False,
            "callbacks_enabled": False,
            "cache_enabled": False,
            "scope": "dedicated_evaluation_child_only",
        }


@contextmanager
def isolated_workflow_transport(settings: Settings) -> Iterator[IsolatedWorkflowTransport]:
    """Install budget/observer wrappers over the yielded callables inside this context.

    Exiting restores SDK settings and the original top-level transports, even if
    caller-installed wrappers or workflow execution fail. This is process-global
    instrumentation and is therefore forbidden outside a dedicated child.
    """
    global _ACTIVE
    if os.environ.get("WINDOPS_WORKFLOW_EVAL_CHILD") != "1" or _ACTIVE:
        raise WorkflowTransportIsolationError("SDK isolation requires one dedicated child context")
    if settings.agent_mode != "litellm":
        raise WorkflowTransportIsolationError("SDK isolation requires provider mode")
    if any(
        importlib.metadata.version(name) != version
        for name, version in SUPPORTED_SDK_VERSIONS.items()
    ):
        raise WorkflowTransportIsolationError("SDK version has no reviewed isolation contract")
    _ACTIVE = True
    environment = {
        key: os.environ.get(key)
        for key in ("DEFAULT_MAX_RETRIES", "LITELLM_MODE", "LITELLM_LOCAL_MODEL_COST_MAP")
    }
    saved: list[tuple[Any, str, Any]] = []
    transport: IsolatedWorkflowTransport | None = None
    try:
        os.environ["DEFAULT_MAX_RETRIES"] = "0"
        os.environ["LITELLM_MODE"] = "PRODUCTION"
        # Import must use the package's frozen routing metadata, not fetch an
        # unmetered remote price/route map before the budget wrapper is installed.
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        prepare_reasoning_runtime(settings)
        sdk = importlib.import_module("litellm")
        constants = importlib.import_module("litellm.constants")
        adapter = importlib.import_module("litellm.llms.openai.openai")

        def assign(target: Any, key: str, value: Any) -> None:
            if not hasattr(target, key):
                raise WorkflowTransportIsolationError("SDK isolation control is unavailable")
            saved.append((target, key, getattr(target, key)))
            setattr(target, key, value)

        # New lists/maps avoid mutating caller state that must be restored on exit.
        controls = {
            key: value.copy() if isinstance(value, (list, dict)) else value
            for key, value in _CONTROLS.items()
        }
        for key, value in controls.items():
            assign(sdk, key, value)
        assign(constants, "DEFAULT_MAX_RETRIES", 0)
        assign(adapter, "DEFAULT_MAX_RETRIES", 0)
        transport = IsolatedWorkflowTransport(sdk, settings, controls)
        saved.extend((sdk, name, getattr(sdk, name)) for name in ("acompletion", "aembedding"))
        original_client = adapter.OpenAIChatCompletion._get_openai_client

        def checked_client(self: Any, *args: Any, **kwargs: Any) -> Any:
            if transport is None:
                raise WorkflowTransportIsolationError("SDK isolation is unavailable")
            transport._check()
            if args or kwargs.get("client") is not None or kwargs.get("max_retries") != 0:
                raise WorkflowTransportIsolationError(
                    "underlying SDK retries or client are uncontrolled"
                )
            client = original_client(self, **kwargs)
            if getattr(client, "max_retries", None) != 0:
                raise WorkflowTransportIsolationError("underlying SDK did not disable retries")
            return client

        assign(adapter.OpenAIChatCompletion, "_get_openai_client", checked_client)
        yield transport
    finally:
        if transport is not None:
            transport._active = False
        for target, key, value in reversed(saved):
            setattr(target, key, value)
        for key, previous in environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        _ACTIVE = False
