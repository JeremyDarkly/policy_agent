"""Which model actually served a call, and reporting it to LaunchDarkly.

A variation names the model to request, but the API can answer with a different one: an alias resolves to a dated
snapshot, a Bedrock prompt router picks a model, or the configured ID is simply wrong. Every chat model here puts the
model the API reported in ``response_metadata["model_name"]`` (OpenAI and Anthropic through LangChain, Bedrock through
``BedrockConverseLLM``), and the OpenLLMetry LangChain span reads the same key for ``gen_ai.response.model``.
"""

import re
from typing import Any, Optional

# Where the served model ID came from.
SOURCE_RESPONSE = "response"  # the API response named the model
SOURCE_ROUTER_TRACE = "router_trace"  # a Bedrock prompt router reported the model it chose
SOURCE_REQUESTED_FALLBACK = "requested_fallback"  # the API reported nothing, so this is the requested ID

SERVED_EVENT = "ai-model-served"
MISMATCH_EVENT = "ai-model-mismatch"

# Bedrock cross-region inference profile prefixes, e.g. "us.anthropic.claude-...".
_PROFILE_PREFIX = re.compile(r"^(us|us-gov|eu|apac|jp|au|ca|global)\.")
# Suffixes that make a model ID more specific: a snapshot date or a version.
_VERSION_SUFFIXES = [
    re.compile(r":\d+$"),  # Bedrock ":0"
    re.compile(r"-v\d+$"),  # Bedrock "-v2"
    re.compile(r"-\d{8}$"),  # Anthropic "-20241022"
    re.compile(r"-\d{4}-\d{2}-\d{2}$"),  # OpenAI "-2024-08-06"
]
_LATEST = re.compile(r"-latest$")


def model_id_without_routing(model_id: str) -> str:
    """A model ID without the parts that only say how it was reached.

    Removes an ARN path or ``provider/`` reference and a Bedrock cross-region profile prefix:
    ``arn:aws:bedrock:...:foundation-model/amazon.nova-pro-v1:0`` -> ``amazon.nova-pro-v1:0``;
    ``us.anthropic.claude-3-5-sonnet-20241022-v2:0`` -> ``anthropic.claude-3-5-sonnet-20241022-v2:0``;
    ``openai/gpt-5.4-mini`` -> ``gpt-5.4-mini``.
    """
    model = (model_id or "").strip().lower()
    model = model.rsplit("/", 1)[-1]
    return _PROFILE_PREFIX.sub("", model)


def _less_specific(model: str):
    """``model``, then ``model`` with its trailing snapshot or version suffixes removed one at a time."""
    yield model
    while True:
        for suffix in _VERSION_SUFFIXES:
            stripped = suffix.sub("", model)
            if stripped != model:
                model = stripped
                yield model
                break
        else:
            return


def models_match(requested: str, served: str) -> bool:
    """True when the served model is the requested one, or the requested one made more specific.

    An alias may resolve to a snapshot (``gpt-4o`` -> ``gpt-4o-2024-08-06``, ``claude-3-5-sonnet-latest`` ->
    ``claude-3-5-sonnet-20241022``), but a pinned snapshot or version must be served exactly: ``gpt-4o-2024-08-06``
    served by ``gpt-4o-2024-11-20`` is a mismatch, and so is ``...-v1:0`` served by ``...-v2:0``.
    """
    wanted = _LATEST.sub("", model_id_without_routing(requested))
    return wanted in _less_specific(_LATEST.sub("", model_id_without_routing(served)))


def requested_model_id(model: Any) -> str:
    """The model ID a chat model sends in its requests."""
    for attr in ("model_id", "model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def served_model(metadata: Optional[dict], requested: str) -> tuple[str, str]:
    """The served model ID and its source, from an AIMessage's ``response_metadata``."""
    metadata = metadata or {}
    served = metadata.get("model_name") or metadata.get("model")
    if not served:
        return requested, SOURCE_REQUESTED_FALLBACK
    return served, metadata.get("served_model_source") or SOURCE_RESPONSE


def served_model_id_of(response: Any, default: str) -> str:
    """The model that served ``response`` (an AIMessage), or ``default`` if it doesn't say."""
    metadata = getattr(response, "response_metadata", None) or {}
    return metadata.get("model_name") or metadata.get("model") or default


def bedrock_served_model(response: dict, requested: str) -> tuple[str, str]:
    """The served model ID and its source, from a Bedrock Converse response or ConverseStream ``metadata`` event.

    Converse responses don't name the model, except that a prompt router reports the model it chose in
    ``trace.promptRouter.invokedModelId``.
    """
    invoked = ((response or {}).get("trace") or {}).get("promptRouter", {}).get("invokedModelId")
    if invoked:
        return invoked, SOURCE_ROUTER_TRACE
    return requested, SOURCE_REQUESTED_FALLBACK


def report_served_model(
    ld_client: Any,
    context: Any,
    *,
    config_key: str,
    variation_key: str,
    provider: str,
    requested: str,
    served: str,
    source: str,
) -> bool:
    """Send ``ai-model-served`` and ``ai-model-mismatch`` for this call.

    ``ai-model-mismatch`` is sent on every call, with value 1 when the served model isn't the requested one and 0 when
    it is, so a numeric metric averaging it is the mismatch rate per call. A prompt router is expected to choose a
    model other than the router ARN, and without a model in the response there's nothing to compare, so those calls
    send 0. Returns whether the call counted as a mismatch.
    """
    data = {
        "configKey": config_key,
        "variationKey": variation_key,
        "provider": provider,
        "requestedModel": requested,
        "servedModel": served,
        "source": source,
    }
    ld_client.track(SERVED_EVENT, context, data, 1)
    mismatch = source == SOURCE_RESPONSE and not models_match(requested, served)
    ld_client.track(MISMATCH_EVENT, context, data, 1 if mismatch else 0)
    return mismatch
