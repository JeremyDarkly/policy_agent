"""Chat models for OpenAI-compatible endpoints (OpenRouter, vLLM), chosen by provider name.

A variation's model config names its provider (``OpenRouter``, ``vLLM``). This module maps that name to the env vars
holding the endpoint's base URL and API key, so URLs and keys never live in LaunchDarkly, and builds a ``ChatOpenAI``
pointed at that endpoint. No process-wide ``OPENAI_BASE_URL`` is needed, so real OpenAI calls in the same process are
unaffected.

The module depends only on the standard library and ``langchain-openai``, imported when a model is built. The evals in
``evals/`` load it by file path, so they share this table without importing the backend app.

``model.custom.base_url_env`` and ``model.custom.api_key_env`` name other env vars, so one project can point
variations at several vLLM servers. The live path sees ``model.custom``; the eval path does not (the AI SDK's
evaluation runner passes only provider, model and parameters to a handler).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Endpoint:
    base_url_env: str
    default_base_url: Optional[str]
    api_key_env: str
    api_key_required: bool
    # Whether the endpoint documents a nested ``reasoning: {effort: ...}`` object. It goes in the request body as is.
    nested_reasoning: bool = False


# Lower-cased provider name -> endpoint.
ENDPOINTS: dict[str, Endpoint] = {
    "openrouter": Endpoint(
        base_url_env="OPENROUTER_BASE_URL",
        default_base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        api_key_required=True,
        nested_reasoning=True,
    ),
    "vllm": Endpoint(
        base_url_env="VLLM_BASE_URL",
        default_base_url=None,
        api_key_env="VLLM_API_KEY",
        api_key_required=False,
    ),
}

# Provider names as a model config might spell them. The AI SDK matches a handler's provider allowlist exactly.
PROVIDER_NAMES: tuple[str, ...] = ("OpenRouter", "openrouter", "vLLM", "vllm", "VLLM")

# Model parameters forwarded to ChatOpenAI. Anything else is logged once and dropped.
FORWARDED_PARAMETERS = frozenset({"temperature", "top_p", "max_tokens", "reasoning_effort", "stop", "seed"})
# LaunchDarkly's UI has written camelCase names for some parameters.
PARAMETER_ALIASES = {"maxTokens": "max_tokens", "topP": "top_p", "reasoningEffort": "reasoning_effort"}

_warned: set[tuple[str, str]] = set()


def normalize_provider(provider: Optional[str]) -> str:
    """``"Bedrock:Anthropic"`` -> ``"bedrock"``, ``"OpenRouter"`` -> ``"openrouter"``."""
    return (provider or "").split(":")[0].strip().lower()


def is_openai_compatible(provider: Optional[str]) -> bool:
    return normalize_provider(provider) in ENDPOINTS


def forwarded_parameters(provider: str, parameters: Optional[Mapping[str, Any]], nested_reasoning: bool) -> dict[str, Any]:
    """The ChatOpenAI keyword arguments for a variation's model parameters.

    A nested ``reasoning`` object goes into ``extra_body`` for endpoints that document it. Unknown keys are logged once
    per provider and key at warning level rather than dropped silently.
    """
    kwargs: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}
    for key, value in (parameters or {}).items():
        name = PARAMETER_ALIASES.get(key, key)
        if value is None:
            continue
        if name in FORWARDED_PARAMETERS:
            kwargs.setdefault(name, value)
        elif name == "reasoning" and nested_reasoning and isinstance(value, Mapping):
            extra_body["reasoning"] = dict(value)
        elif (provider, key) not in _warned:
            _warned.add((provider, key))
            logger.warning("Model parameter %r is not forwarded to provider %r; it was dropped", key, provider)
    if extra_body:
        kwargs["extra_body"] = extra_body
    return kwargs


def chat_model_kwargs(
    provider: str,
    model: str,
    parameters: Optional[Mapping[str, Any]] = None,
    custom: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """The ChatOpenAI constructor arguments for an OpenAI-compatible provider.

    Raises ``ValueError`` when the provider is unknown, or its base URL or required API key is not set.
    """
    env = os.environ if env is None else env
    name = normalize_provider(provider)
    endpoint = ENDPOINTS.get(name)
    if endpoint is None:
        raise ValueError(f"{provider!r} is not an OpenAI-compatible provider. Known: {', '.join(sorted(ENDPOINTS))}")
    custom = custom or {}
    base_url_env = str(custom.get("base_url_env") or endpoint.base_url_env)
    api_key_env = str(custom.get("api_key_env") or endpoint.api_key_env)

    base_url = env.get(base_url_env) or (endpoint.default_base_url if base_url_env == endpoint.base_url_env else None)
    if not base_url:
        raise ValueError(f"{base_url_env} is not set; provider {provider!r} needs a base URL")
    api_key = env.get(api_key_env)
    if not api_key:
        if endpoint.api_key_required:
            raise ValueError(f"{api_key_env} is not set; provider {provider!r} needs an API key")
        # ChatOpenAI requires some key; a vLLM server started without --api-key accepts any.
        api_key = "not-needed"

    return {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        **forwarded_parameters(name, parameters, endpoint.nested_reasoning),
    }


def build_openai_compatible_model(
    provider: str,
    model: str,
    parameters: Optional[Mapping[str, Any]] = None,
    custom: Optional[Mapping[str, Any]] = None,
) -> Any:
    """A ``ChatOpenAI`` for the provider's endpoint."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(**chat_model_kwargs(provider, model, parameters, custom))


def eval_model_factory(config: Mapping[str, Any]) -> Any:
    """Model factory for the AI SDK's LangChain handlers: ``(config) -> chat model``.

    ``config`` is the handler config the SDK builds: ``provider.name``, ``model.name``, ``model.parameters`` and, on
    the live path only, ``model.custom``.
    """
    provider = (config.get("provider") or {}).get("name") or ""
    model = config.get("model") or {}
    return build_openai_compatible_model(provider, model.get("name") or "", model.get("parameters"), model.get("custom"))
