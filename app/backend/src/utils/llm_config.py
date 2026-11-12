"""LLM configuration and initialization."""

import os
from functools import lru_cache
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel

from .launchdarkly_config import ModelInvoker, get_ld_client

# Load environment variables
load_dotenv()

# Provider names (normalized: lowercased, text after ":" dropped) that select the Azure AI Foundry client.
# The model config uses "Azure AI Foundry"; the other spellings are accepted so a hand-made config still works.
AZURE_PROVIDERS = {"azure ai foundry", "azure", "azure openai", "foundry"}

# Provider names that select the Gemini API. LaunchDarkly's model catalog uses "Gemini" (Gemini.gemini-4-carbon).
GEMINI_PROVIDERS = {"gemini", "google", "google ai studio"}


def azure_foundry_base_url(endpoint: str) -> str:
    """The v1 OpenAI-compatible base URL for an Azure AI Foundry resource.

    ``AZURE_FOUNDRY_ENDPOINT`` may be the bare resource URL or a full request URL copied from the portal (for
    example ``https://<resource>.services.ai.azure.com/openai/v1/chat/completions``). Only the scheme and host
    are kept.
    """
    parts = urlsplit(endpoint.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"AZURE_FOUNDRY_ENDPOINT is not a URL: {endpoint!r}")
    return f"{parts.scheme}://{parts.netloc}/openai/v1/"


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
    model = os.getenv("LLM_MODEL")

    if provider == "bedrock":
        from .bedrock_llm import BedrockConverseLLM, get_bedrock_model_id

        model_id = get_bedrock_model_id(model or "claude-3-5-sonnet")
        region = os.getenv("AWS_REGION", "us-east-1")
        profile = os.getenv("AWS_PROFILE")

        return BedrockConverseLLM(
            model_id=model_id,
            temperature=temperature,
            max_tokens=2000,
            region=region,
            profile_name=profile,
        )

    elif provider == "openai":
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set in environment")

        return ChatOpenAI(
            model=model or "gpt-4-turbo-preview",
            temperature=temperature,
            api_key=api_key,
        )

    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in environment")

        return ChatAnthropic(
            model=model or "claude-3-5-sonnet-20241022",
            temperature=temperature,
            api_key=api_key,
        )

    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


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


def get_llm_from_config(
    config_key: str,
    context: Optional[dict[str, Any]] = None,
) -> Tuple[BaseChatModel, Any]:
    """Get LLM instance from LaunchDarkly AI Config.

    Args:
        config_key: The AI config key in LaunchDarkly
        context: User/session context for targeting

    Returns:
        Tuple of (LLM instance, tracker) for metrics tracking
    """
    ld_client = get_ld_client()

    # Get AI config from LaunchDarkly (context returned but not used here)
    config, tracker, _ = ld_client.get_ai_config(config_key, context)

    # Extract model configuration
    model_config = config.get("model", {})
    model_name = model_config.get("name") or os.getenv("LLM_MODEL", "gpt-4-turbo-preview")
    if not (model_name.startswith("global.") or model_name.startswith("us.")):
        model_name = f"us.{model_name}"
    provider_raw = config.get("provider") or os.getenv("LLM_PROVIDER", "openai")

    # Parse provider - LaunchDarkly returns formats like "Bedrock:Anthropic" or "Bedrock:Nova"
    # We need to extract just the main provider (the part before the colon)
    provider = provider_raw.split(":")[0].lower() if ":" in provider_raw else provider_raw.lower()

    # Extract parameters
    params = model_config.get("parameters", {})
    temperature = params.get("temperature")
    max_tokens = params.get("maxTokens", 2000)

    # Create the LLM instance based on provider
    llm = _create_llm_for_provider(provider, model_name, temperature, max_tokens)

    return llm, tracker


def get_model_invoker(
    config_key: str,
    context: Optional[dict[str, Any]] = None,
    skip_span_annotation: bool = False,
    default_config: Optional[dict[str, Any]] = None,
    override_guardrail_enabled: Optional[bool] = None,
    evaluated: Optional[Tuple[dict[str, Any], Any, Any]] = None,
) -> Tuple[ModelInvoker, dict[str, Any]]:
    """Get a ModelInvoker with tracking and full config (including messages) from LaunchDarkly.

    Args:
        config_key: The AI config key in LaunchDarkly
        context: User/session context for targeting
        skip_span_annotation: If True, skip all span annotation (for background threads like judges)
        default_config: Fallback config dict served when the AI Config is missing, disabled or invalid
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
        config, tracker, ld_context = ld_client.get_ai_config(config_key, context, default_config)

    # Create LLM directly from the config (don't call get_llm_from_config which would retrieve again)
    provider = config.get("provider") or "bedrock"
    # Parse provider name (e.g., "Bedrock:Anthropic" -> "bedrock")
    if ":" in provider:
        provider = provider.split(":")[0]
    provider = provider.lower().strip()

    model_config = config.get("model", {})
    model_name = model_config.get("name") or "claude-3-5-sonnet"
    # Cross-region inference profile prefix; Bedrock only, other providers reject it
    if provider == "bedrock" and not (model_name.startswith("global.") or model_name.startswith("us.")):
        model_name = f"us.{model_name}"

    # Get temperature from config or use default
    parameters = model_config.get("parameters", {})
    temperature = parameters.get("temperature")
    max_tokens = parameters.get("max_tokens") or parameters.get("maxTokens", 2000)

    llm = _create_llm_for_provider(provider, model_name, temperature, max_tokens)

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
            # An Azure AI Foundry model ID is a deployment name; the model config says which model it runs
            expected_model=(model_config.get("custom") or {}).get("baseModel") or "",
        ),
        config,
    )


def _create_llm_for_provider(
    provider: str, model_name: str, temperature: Optional[float], max_tokens: int
) -> BaseChatModel:
    """Create LLM instance for the specified provider.

    Args:
        provider: Provider name (bedrock, openai, anthropic, etc.)
                 Note: LaunchDarkly may return "Bedrock:Anthropic" format, but we parse it to just "bedrock"
        model_name: Model name or Bedrock model ID (e.g., "us.anthropic.claude-sonnet-4-20250514-v1:0")
        temperature: Sampling temperature
        max_tokens: Maximum tokens to generate

    Returns:
        Configured LLM instance

    Raises:
        ValueError: If provider is not supported
    """
    # Normalize provider name - handle formats like "Bedrock:Anthropic" from LaunchDarkly
    provider_normalized = provider.split(":")[0].lower() if ":" in provider else provider.lower()

    if provider_normalized == "bedrock":
        from .bedrock_llm import BedrockConverseLLM, get_bedrock_model_id

        # Get full Bedrock model ID
        model_id = get_bedrock_model_id(model_name)

        # Get AWS configuration
        region = os.getenv("AWS_REGION", "us-east-1")
        profile = os.getenv("AWS_PROFILE")

        return BedrockConverseLLM(
            model_id=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
            region=region,
            profile_name=profile,
        )

    elif provider_normalized == "openai":
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set in environment")

        return ChatOpenAI(
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=api_key,
        )

    elif provider_normalized == "anthropic":
        from langchain_anthropic import ChatAnthropic

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in environment")

        return ChatAnthropic(
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=api_key,
        )

    elif provider_normalized in AZURE_PROVIDERS:
        # Azure AI Foundry through its OpenAI-compatible v1 route. This serves OpenAI and non-OpenAI deployments
        # (Grok, Llama, DeepSeek) with plain ChatOpenAI and needs no api-version. model_name is the deployment name.
        from langchain_openai import ChatOpenAI

        endpoint = os.getenv("AZURE_FOUNDRY_ENDPOINT")
        if not endpoint:
            raise ValueError("AZURE_FOUNDRY_ENDPOINT not set in environment (needed for Azure AI Foundry variations)")
        api_key = os.getenv("AZURE_FOUNDRY_API_KEY")
        if not api_key:
            raise ValueError("AZURE_FOUNDRY_API_KEY not set in environment (needed for Azure AI Foundry variations)")

        return ChatOpenAI(
            model=model_name,
            base_url=azure_foundry_base_url(endpoint),
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            stream_usage=True,
        )

    elif provider_normalized in GEMINI_PROVIDERS:
        # The Gemini API (Google AI Studio). model_name is the API model code, e.g. gemini-4-carbon.
        from langchain_google_genai import ChatGoogleGenerativeAI

        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY not set in environment (needed for Gemini variations)")

        options: dict[str, Any] = {}
        # Only when the variation sets it, as BedrockConverseLLM does
        if temperature is not None:
            options["temperature"] = temperature
        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            max_output_tokens=max_tokens,
            **options,
        )

    else:
        raise ValueError(
            f"Unsupported LLM provider: {provider} (normalized: {provider_normalized}). "
            f"Supported providers: bedrock, openai, anthropic, azure ai foundry, gemini"
        )
