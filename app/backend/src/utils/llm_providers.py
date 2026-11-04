"""One provider factory: a LangChain chat model from a variation's provider, model, parameters and custom params.

Every agent, the judges and ``fetch_ai_config_prompts.py`` reach a model through ``get_model_invoker``, which calls
``build_chat_model``. A new provider is one more branch here (or one more row in ``openai_compatible.ENDPOINTS`` for an
OpenAI-compatible endpoint), not a parallel code path.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from langchain_core.language_models import BaseChatModel

from .openai_compatible import (
    ENDPOINTS,
    build_openai_compatible_model,
    forwarded_parameters,
    is_openai_compatible,
    normalize_provider,
)

DEFAULT_MAX_TOKENS = 2000
SUPPORTED_PROVIDERS = ("bedrock", "openai", "anthropic", *ENDPOINTS)


def bedrock_model_name(model: str) -> str:
    """Add the cross-region inference profile prefix ``us.`` unless the ID already has one."""
    if model.startswith("global.") or model.startswith("us."):
        return model
    return f"us.{model}"


def _max_tokens(parameters: Mapping[str, Any]) -> int:
    return parameters.get("max_tokens") or parameters.get("maxTokens") or DEFAULT_MAX_TOKENS


def build_chat_model(
    provider: str,
    model: str,
    parameters: Optional[Mapping[str, Any]] = None,
    custom: Optional[Mapping[str, Any]] = None,
) -> BaseChatModel:
    """The chat model for a variation.

    Args:
        provider: The model config's provider, e.g. ``Bedrock``, ``Bedrock:Anthropic``, ``OpenRouter``, ``vLLM``.
        model: The model ID, or for Bedrock a short name from ``bedrock_llm.BEDROCK_MODELS``. The caller adds a
            Bedrock profile prefix (``bedrock_model_name``) when it wants one.
        parameters: The variation's ``model.parameters``.
        custom: The variation's ``model.custom``. OpenAI-compatible providers read ``base_url_env`` and
            ``api_key_env`` from it.

    Raises:
        ValueError: if the provider is not supported or its credentials are not set.
    """
    parameters = dict(parameters or {})
    name = normalize_provider(provider)

    if name == "bedrock":
        from .bedrock_llm import BedrockConverseLLM, get_bedrock_model_id

        return BedrockConverseLLM(
            model_id=get_bedrock_model_id(model),
            temperature=parameters.get("temperature"),
            max_tokens=_max_tokens(parameters),
            region=os.getenv("AWS_REGION", "us-east-1"),
            profile_name=os.getenv("AWS_PROFILE"),
        )

    if name == "openai":
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set in environment")
        kwargs = forwarded_parameters(name, parameters, nested_reasoning=False)
        kwargs.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        return ChatOpenAI(model=model, api_key=api_key, **kwargs)

    if name == "anthropic":
        from langchain_anthropic import ChatAnthropic

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in environment")
        return ChatAnthropic(
            model=model,
            temperature=parameters.get("temperature"),
            max_tokens=_max_tokens(parameters),
            api_key=api_key,
        )

    if is_openai_compatible(name):
        return build_openai_compatible_model(provider, model, parameters, custom)

    raise ValueError(
        f"Unsupported LLM provider: {provider} (normalized: {name}). "
        f"Supported providers: {', '.join(SUPPORTED_PROVIDERS)}"
    )
