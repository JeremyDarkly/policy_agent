"""Triage router agent for query classification."""

import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..graph.state import AgentState, QueryType
from ..utils.llm_config import get_model_invoker
from ..utils.launchdarkly_config import get_ld_client

CONFIG_KEY = "triage_agent"
# The LLM path's low-confidence threshold. A Jev variation sets its own in
# model.custom.choice_confidence_min.
CONFIDENCE_MIN = 0.7
# Provider name the AI Config model catalog uses for Jev models.
JEV_PROVIDER = "typesafe"
# Brand names sent to Jev as state, by user_context["domain"].
_BRANDS = {"togglebank": "ToggleBank", "togglehealth": "ToggleHealth", "togglecell": "ToggleCell"}

# Used when the model's answer can't be used (unparseable LLM JSON, or any Jev
# failure): route to a human banker, which escalates.
PARSE_FAILURE_RESULT = {
    "query_type": "schedule_agent",
    "confidence_score": 0.5,
    "extracted_context": {},
    "escalation_needed": True,
    "reasoning": "Failed to parse query, routing to human agent for safety",
}


def _evaluate_triage_config(user_context: dict[str, Any]):
    """Evaluate triage_agent once: (config dict, tracker, ld_context)."""
    return get_ld_client().get_ai_config(CONFIG_KEY, user_context)


def is_jev_config(config: dict[str, Any]) -> bool:
    """True when the served variation is on a TypeSafe (Jev) model config."""
    provider = str(config.get("provider") or "")
    return provider.split(":")[0].strip().lower() == JEV_PROVIDER


def _jev_triage(config: dict[str, Any], query: str, user_context: dict[str, Any]) -> tuple[dict, dict, str]:
    """Route `query` with Jev's `choice` question, built from the variation.

    Returns (result in the LLM JSON's shape, tokens, model id). Raises on any
    failure (missing key or criteria, timeout, non-2xx, unexpected body); the
    caller turns that into PARSE_FAILURE_RESULT and a triage-error event.
    """
    from ..utils.typesafe_client import system_one

    model_config = config.get("model") or {}
    custom = model_config.get("custom") or {}
    criteria = custom.get("criteria")
    if not isinstance(criteria, dict) or not criteria:
        raise ValueError("Jev variation has no model.custom.criteria")
    # LaunchDarkly stores JSON objects with sorted keys, so the variation lists
    # the option order separately. Jev leans toward the first option.
    order = [k for k in (custom.get("criteria_order") or []) if k in criteria]
    criteria = {k: criteria[k] for k in [*order, *(k for k in criteria if k not in order)]}
    domain = str(user_context.get("domain", "") or "")
    response = system_one(
        model=model_config.get("name", ""),
        state={"customer_message": query, "bank": _BRANDS.get(domain.lower(), domain)},
        questions={
            "route": {
                "type": "choice",
                "instructions": config.get("_instructions", ""),
                "criteria": criteria,
            }
        },
    )
    answer = response["answers"]["route"]
    choice = str(answer["choice"])
    confidence = float(answer["confidence"])
    top = max((float(v) for v in (answer["probabilities"] or {}).values()), default=confidence)
    usage = response["usage"]
    tokens = {"input": int(usage.get("input_tokens", 0)), "output": int(usage.get("output_tokens", 0))}
    result = {
        "query_type": choice,
        "confidence_score": confidence,
        "escalation_needed": choice == "schedule_agent",
        "extracted_context": {},
        "reasoning": f"{response['model']} route={choice} p={top:.2f}",
    }
    return result, tokens, str(response["model"])


def _jev_threshold(config: dict[str, Any]) -> float:
    custom = (config.get("model") or {}).get("custom") or {}
    try:
        return float(custom.get("choice_confidence_min", CONFIDENCE_MIN))
    except (TypeError, ValueError):
        return CONFIDENCE_MIN


def _annotate_span(config_key: str) -> None:
    """Link the node's span to the AI Config, as ModelInvoker does for LLM calls."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("ld.ai_config.key", config_key)
    except Exception:
        pass


def _track_jev_call(tracker, duration_ms: int, tokens: dict, ok: bool) -> None:
    """Report the Jev call to the AI Config's Monitoring tab."""
    try:
        tracker.track_duration(duration_ms)
        if ok:
            from ldai.tracker import TokenUsage

            tracker.track_success()
            tracker.track_tokens(
                TokenUsage(total=tokens["input"] + tokens["output"], input=tokens["input"], output=tokens["output"])
            )
        else:
            tracker.track_error()
    except Exception as e:
        print(f"  Failed to track Jev call: {e}")


def _parse_triage_json(content: str) -> dict:
    """Parse the triage model's JSON, tolerating markdown fences / preamble.

    Models frequently return ```json ... ``` or add text around the object even
    when told not to. Strip a code fence if present, else extract the outermost
    {...}. Raises json.JSONDecodeError/ValueError if nothing parses.
    """
    text = (content or "").strip()

    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _track_triage_metrics(ld_context, cost_usd, duration_ms: int, escalated: bool, error: bool) -> None:
    """Send triage-only cost, latency, escalation and error events.

    The specialists also send $ld:ai:duration:total on the same context, so
    triage gets its own event keys to keep routing metrics separate.
    `triage-error` is 1 when the model's answer couldn't be used (a Jev
    failure, or LLM JSON that didn't parse) and 0 otherwise, so the metric
    is a rate.
    """
    if ld_context is None:
        return
    try:
        import ldclient

        client = ldclient.get()
        if cost_usd is not None:
            client.track("triage-cost-cents", ld_context, metric_value=cost_usd * 100.0)
        client.track("triage-duration-ms", ld_context, metric_value=float(duration_ms))
        client.track("triage-escalated", ld_context, metric_value=1.0 if escalated else 0.0)
        client.track("triage-error", ld_context, metric_value=1.0 if error else 0.0)
    except Exception as e:
        print(f"  Failed to send triage metrics: {e}")


def triage_node(state: AgentState) -> dict[str, Any]:
    """Triage router agent node.

    Analyzes the customer query and routes to the appropriate specialist.
    Uses prompts from LaunchDarkly AI Config.

    Args:
        state: Current agent state

    Returns:
        Updated state with routing information
    """
    # Get the last message (user query)
    messages = state["messages"]
    last_message = messages[-1]

    if isinstance(last_message, HumanMessage):
        query = last_message.content
    else:
        query = str(last_message.content)

    # Get user context
    user_context = state.get("user_context", {})

    # Evaluate the triage_agent AI Config once. A variation on a TypeSafe model
    # config routes with Jev; every other variation calls an LLM.
    print(f"\n{'─'*80}")
    print(f"  TRIAGE AGENT: Analyzing query")
    import time

    evaluated = _evaluate_triage_config(user_context)
    ld_config, tracker, ld_context = evaluated
    model_id = ld_config.get("model", {}).get("name", "unknown")
    provider = ld_config.get("provider", "")
    print(f"  Triage Agent pulled from LaunchDarkly — using {model_id}" + (f" ({provider})" if provider else ""))
    print(f"{'─'*80}")

    tokens = {"input": 0, "output": 0}
    ttft_ms = None
    triage_error = False

    if is_jev_config(ld_config):
        confidence_min = _jev_threshold(ld_config)
        _annotate_span(CONFIG_KEY)
        start_time = time.time()
        try:
            result, tokens, model_id = _jev_triage(ld_config, query, user_context)
        except Exception as e:
            print(f"  Jev triage failed, escalating: {e}")
            result = dict(PARSE_FAILURE_RESULT)
            triage_error = True
        duration_ms = int((time.time() - start_time) * 1000)
        _track_jev_call(tracker, duration_ms, tokens, ok=not triage_error)
    else:
        confidence_min = CONFIDENCE_MIN
        model_invoker, ld_config = get_model_invoker(
            config_key=CONFIG_KEY,
            context=user_context,
            evaluated=evaluated,
        )

        # Build LangChain messages from LaunchDarkly config (supports both agent-based and completion-based)
        ld_client = get_ld_client()
        context_vars = {**user_context, "query": query}
        langchain_messages = ld_client.build_langchain_messages(ld_config, context_vars)

        # Configure for JSON output if OpenAI
        from langchain_openai import ChatOpenAI
        if isinstance(model_invoker.model, ChatOpenAI):
            model_invoker.model.model_kwargs = {"response_format": {"type": "json_object"}}

        # Track start time for duration measurement
        start_time = time.time()

        response = model_invoker.invoke(langchain_messages)

        # Calculate duration
        duration_ms = int((time.time() - start_time) * 1000)

        # Extract token usage and TTFT if available
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            tokens = {
                "input": response.usage_metadata.get("input_tokens", 0),
                "output": response.usage_metadata.get("output_tokens", 0)
            }

        # Extract Time to First Token (TTFT) from response metadata
        if hasattr(response, "response_metadata") and isinstance(response.response_metadata, dict):
            ttft_ms = response.response_metadata.get("ttft_ms")

        # Parse the JSON response. Models (e.g. Claude Sonnet 5) often wrap the JSON
        # in a ```json ... ``` markdown fence or add preamble despite instructions,
        # so strip fences / extract the object before failing.
        try:
            result = _parse_triage_json(response.content)
        except (json.JSONDecodeError, ValueError):
            result = dict(PARSE_FAILURE_RESULT)
            triage_error = True

    # Emit cost/duration so the UI terminal can mark this node complete
    triage_cost_usd = None
    try:
        from .brand_voice_agent import calculate_model_cost
        triage_cost_usd = calculate_model_cost(
            model_id=model_id,
            input_tokens=tokens["input"],
            output_tokens=tokens["output"],
        )
        triage_cost_cents = round(triage_cost_usd * 100.0, 4)
        print(
            f"  Triage agent cost: {triage_cost_cents:.4f}¢ (${triage_cost_usd:.6f}) "
            f"[in={tokens['input']}, out={tokens['output']}, "
            f"model={model_id if 'jev' in model_id.lower() else model_id.split(':')[0].split('.')[-1]}]"
        )
        print(f"  Triage agent duration: {duration_ms}ms")
    except Exception as e:
        print(f"  Failed to compute triage cost: {e}")

    # Map query type to enum (normalize case/whitespace for robustness)
    query_type_str = str(result.get("query_type", "schedule_agent")).strip().lower()
    query_type_map = {
        "account_question": QueryType.ACCOUNT_QUESTION,
        "branch_lookup": QueryType.BRANCH_LOOKUP,
        "schedule_agent": QueryType.SCHEDULE_AGENT,
        "general_question": QueryType.GENERAL_QUESTION,
    }
    query_type = query_type_map.get(query_type_str, QueryType.SCHEDULE_AGENT)

    # Determine next agent
    agent_map = {
        QueryType.ACCOUNT_QUESTION: "account_specialist",
        QueryType.BRANCH_LOOKUP: "branch_specialist",
        QueryType.SCHEDULE_AGENT: "scheduler_specialist",
        QueryType.GENERAL_QUESTION: "brand_voice",
    }
    next_agent = agent_map[query_type]

    # Check confidence - if low, escalate (but not for general questions)
    confidence_score = result.get("confidence_score", 0.0)
    escalation_needed = result.get("escalation_needed", False)

    if confidence_score < confidence_min and query_type != QueryType.GENERAL_QUESTION:
        escalation_needed = True
        next_agent = "scheduler_specialist"

    _track_triage_metrics(ld_context, triage_cost_usd, duration_ms, escalation_needed, triage_error)

    # For general questions routed directly to brand_voice, pass the original
    # query through so the brand voice agent can answer it directly.
    if next_agent == "brand_voice":
        triage_content = query
    else:
        triage_content = f"Routing to {next_agent} (confidence: {confidence_score:.2f})"

    # Update state
    updates: dict[str, Any] = {
        "query_type": query_type,
        "next_agent": next_agent,
        "confidence_score": confidence_score,
        "escalation_needed": escalation_needed,
        "messages": messages
        + [
            AIMessage(
                content=triage_content,
                additional_kwargs={"reasoning": result.get("reasoning", "")},
            )
        ],
        "agent_data": {
            **state.get("agent_data", {}),
            "triage_router": {
                "model": model_id,  # Track which model was used
                "tokens": tokens,
                "ttft_ms": ttft_ms,  # Time to first token from Bedrock streaming
                "duration_ms": duration_ms,  # Total time to generate response
                "confidence": confidence_score,
                "query_type": str(query_type)
            }
        }
    }

    # Merge extracted context
    extracted_context = result.get("extracted_context", {})
    if extracted_context:
        merged_context = {**user_context, **extracted_context}
        updates["user_context"] = merged_context

    return updates
