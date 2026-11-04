"""LLM configuration and initialization."""

import os
from functools import lru_cache
from typing import Any, Optional, Tuple

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel

from .launchdarkly_config import ModelInvoker, get_ld_client
from .llm_providers import bedrock_model_name, build_chat_model
from .openai_compatible import normalize_provider

# Load environment variables
load_dotenv()

# get_llm's model when LLM_MODEL is unset
DEFAULT_MODELS = {
    "bedrock": "claude-3-5-sonnet",
    "openai": "gpt-4-turbo-preview",
    "anthropic": "claude-3-5-sonnet-20241022",
}


@lru_cache(maxsize=1)
def get_llm(temperature: Optional[float] = None) -> BaseChatModel:
    """Get configured LLM instance.

    Args:
        temperature: Sampling temperature (0-1)

    Returns:
        Configured LLM instance

    Raises:
        ValueError: If LLM provider is not configured correctly
    """
    provider = os.getenv("LLM_PROVIDER", "bedrock").lower()
    model = os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(normalize_provider(provider), "")
    return build_chat_model(provider, model, {"temperature": temperature, "max_tokens": 2000})


def get_structured_llm(temperature: Optional[float] = None):
    """Get LLM configured for structured output (JSON mode).

    Args:
        temperature: Sampling temperature (lower for more deterministic output)

    Returns:
        LLM configured for JSON output
    """
    llm = get_llm(temperature=temperature)

    # Configure for JSON output based on provider
    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    if provider == "openai":
        # OpenAI supports JSON mode
        llm.model_kwargs = {"response_format": {"type": "json_object"}}

    # Anthropic doesn't have native JSON mode, but will follow instructions

    return llm


def get_model_invoker(
    config_key: str,
    context: Optional[dict[str, Any]] = None,
    skip_span_annotation: bool = False,
    default_config: Optional[Any] = None,
    override_guardrail_enabled: Optional[bool] = None,
    evaluated: Optional[Tuple[dict[str, Any], Any, Any]] = None,
) -> Tuple[ModelInvoker, dict[str, Any]]:
    """Get a ModelInvoker with tracking and full config (including messages) from LaunchDarkly.

    Args:
        config_key: The AI config key in LaunchDarkly
        context: User/session context for targeting
        skip_span_annotation: If True, skip all span annotation (for background threads like judges)
        default_config: Default AIConfig to use if LaunchDarkly is unavailable or flag doesn't exist
        override_guardrail_enabled: If provided, override guardrail setting (True=enable, False=disable, None=use config)
        evaluated: An already-evaluated (config, tracker, ld_context) from
            ``get_ld_client().get_ai_config``. When given, the AI Config is not
            evaluated again, so a caller that inspects the config first (the
            triage node checks for a TypeSafe variation) evaluates it once.

    Returns:
        Tuple of (ModelInvoker instance with tracking, full config dict including messages)
    """
    if evaluated is not None:
        config, tracker, ld_context = evaluated
    else:
        ld_client = get_ld_client()
        # Get config once from LaunchDarkly (now returns context too)
        # Convert AIConfig to dict if provided
        default_config_dict = None
        if default_config:
            # default_config may be a plain dict (already in the shape get_ai_config
            # expects) or an SDK AIConfig object that needs converting.
            if isinstance(default_config, dict):
                default_config_dict = default_config
            else:
                default_config_dict = ld_client._ai_config_to_dict(default_config)

        config, tracker, ld_context = ld_client.get_ai_config(config_key, context, default_config_dict)

    # Create LLM directly from the config (don't call get_llm_from_config which would retrieve again)
    provider = config.get("provider") or "bedrock"
    # Parse provider name (e.g., "Bedrock:Anthropic" -> "bedrock")
    if ":" in provider:
        provider = provider.split(":")[0]
    provider = provider.lower().strip()

    model_config = config.get("model", {})
    model_name = model_config.get("name") or "claude-3-5-sonnet"
    # Cross-region inference profile prefix; Bedrock only, other providers reject it
    if provider == "bedrock":
        model_name = bedrock_model_name(model_name)
    llm = build_chat_model(provider, model_name, model_config.get("parameters") or {}, model_config.get("custom") or {})

    # Determine if this is an agent-based config (has _instructions vs messages)
    is_agent_config = "_instructions" in config or config.get("_enabled", False)

    # Pass ld_context to ModelInvoker for ld.variation() correlation
    return (
        ModelInvoker(
            llm,
            tracker,
            config_key=config_key,
            is_agent_config=is_agent_config,
            user_context=ld_context,
            skip_span_annotation=skip_span_annotation,
            variation_key=config.get("_variation", ""),
            provider=provider,
        ),
        config,
    )
