"""Let the model call the served variation's Library tools in a loop (issue #72).

A variation that attaches tools the backend implements (``tools/registry.py``) is served
through ``run_tool_loop``: the model is bound to those tools, each tool call it makes is
run and its result sent back, until it answers without one. Every call is reported to
LaunchDarkly with ``tracker.track_tool_call`` (event ``$ld:ai:tool_call``, shown on the
config's Monitoring tab) and an ``ai-tool-call-error`` event, 1 when the call failed or
found nothing and 0 otherwise. The whole loop is one tracked generation: duration, success
and summed tokens are recorded once, as for a call without tools.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from ..tools.registry import TOOLS

TOOL_CALL_ERROR_EVENT = "ai-tool-call-error"
MAX_ROUNDS = 4
# Kept in agent_data for the UI terminal; the model gets the full result.
MAX_RESULT_CHARS = 1500


@dataclass
class ToolLoopResult:
    message: AIMessage  # the final answer, with token usage summed over every round
    calls: list[dict[str, Any]] = field(default_factory=list)  # {tool, args, result, error, duration_ms}
    rounds: int = 0
    hit_max_rounds: bool = False


def tools_for(ld_config: dict[str, Any], registry: dict[str, Callable[..., Any]] = TOOLS) -> list[dict[str, Any]]:
    """The served variation's tools that the backend implements, as {name, description, parameters}.

    Attached tools with no implementation are logged and left out, so the model is never
    offered a tool nobody can run. An empty list means the request runs without tools.
    """
    tools = []
    for tool in ld_config.get("tools") or []:
        name = tool.get("name")
        if name in registry:
            tools.append(tool)
        elif name:
            print(f"  ⚠️ tool {name!r} is attached to the variation but not implemented; not offered to the model")
    return tools


def is_error_result(result: Any) -> bool:
    """Whether a tool result is a failure for the ai-tool-call-error metric: an error or nothing found."""
    return isinstance(result, dict) and ("error" in result or result.get("status") == "not_found")


def _add_usage(total: Optional[dict], usage: Optional[dict]) -> Optional[dict]:
    if not usage:
        return total
    if not total:
        return dict(usage)
    keys = ("input_tokens", "output_tokens", "total_tokens")
    return {**total, **{k: (total.get(k) or 0) + (usage.get(k) or 0) for k in keys}}


def _tool_span(name: str):
    """An OpenTelemetry span per tool call, named as the AI SDK's eval runner names it."""
    try:
        from opentelemetry import trace

        return trace.get_tracer(__name__).start_as_current_span(f"execute_tool {name}")
    except Exception:
        from contextlib import nullcontext

        return nullcontext()


class _Reporter:
    """Sends the per-call events. Failures to report never fail the request."""

    def __init__(self, invoker: Any, ld_client: Any = None):
        self.invoker = invoker
        self.ld_client = ld_client

    def __call__(self, tool: str, failed: bool) -> None:
        try:
            self.invoker.tracker.track_tool_call(tool)
        except Exception as e:
            print(f"  Failed to track tool call: {e}")
        context = getattr(self.invoker, "user_context", None)
        if context is None:
            return
        try:
            client = self.ld_client
            if client is None:
                import ldclient

                client = ldclient.get()
            client.track(
                TOOL_CALL_ERROR_EVENT,
                context,
                {
                    "configKey": getattr(self.invoker, "config_key", ""),
                    "variationKey": getattr(self.invoker, "variation_key", ""),
                    "toolKey": tool,
                },
                1.0 if failed else 0.0,
            )
        except Exception as e:
            print(f"  Failed to send {TOOL_CALL_ERROR_EVENT}: {e}")


def run_tool(name: str, args: Any, registry: dict[str, Callable[..., Any]] = TOOLS) -> tuple[Any, Optional[str]]:
    """Run one tool call: (result, error). An exception becomes the result {"error": ...}."""
    implementation = registry.get(name)
    if implementation is None:
        error = f"unknown tool {name!r}"
    elif not isinstance(args, dict):
        error = "tool arguments must be a JSON object"
    else:
        try:
            return implementation(**args), None
        except Exception as e:  # the model sees the error and can adapt
            error = f"{type(e).__name__}: {e}"
    return {"error": error}, error


def run_tool_loop(
    invoker: Any,
    messages: list[BaseMessage],
    tools: list[dict[str, Any]],
    max_rounds: int = MAX_ROUNDS,
    registry: dict[str, Callable[..., Any]] = TOOLS,
    ld_client: Any = None,
) -> ToolLoopResult:
    """Run the model with ``tools`` until it answers without a tool call, or ``max_rounds`` model calls.

    ``invoker`` is the request's ModelInvoker; its model is bound to the tools and the loop
    runs as one tracked generation (``ModelInvoker.run_tracked``).
    """
    model = invoker.model.bind_tools(
        [{"name": t["name"], "description": t.get("description") or t["name"],
          "parameters": t.get("parameters") or {"type": "object", "properties": {}}} for t in tools]
    )
    report = _Reporter(invoker, ld_client)
    result = ToolLoopResult(message=AIMessage(content=""))

    def loop() -> AIMessage:
        history = list(messages)
        usage: Optional[dict] = None
        ttft_ms = None
        response: AIMessage = AIMessage(content="")
        for _ in range(max_rounds):
            response = model.invoke(history)
            result.rounds += 1
            usage = _add_usage(usage, getattr(response, "usage_metadata", None))
            if ttft_ms is None:
                ttft_ms = (getattr(response, "response_metadata", None) or {}).get("ttft_ms")
            if not response.tool_calls:
                break
            history.append(response)
            for call in response.tool_calls:
                name = call["name"]
                started = time.time()
                with _tool_span(name):
                    output, error = run_tool(name, call.get("args"), registry)
                failed = error is not None or is_error_result(output)
                report(name, failed)
                text = json.dumps(output, default=str)
                result.calls.append({
                    "tool": name,
                    "args": call.get("args"),
                    "result": text[:MAX_RESULT_CHARS],
                    "error": error,
                    "failed": failed,
                    "duration_ms": int((time.time() - started) * 1000),
                })
                print(f"  🔧 {name}({json.dumps(call.get('args'), default=str)})" + (f" → error: {error}" if error else ""))
                history.append(ToolMessage(content=text, tool_call_id=call["id"], name=name,
                                           status="error" if error else "success"))
        else:
            result.hit_max_rounds = bool(response.tool_calls)
        if result.hit_max_rounds:
            print(f"  ⚠️ stopped after {max_rounds} model calls with tool calls still pending")
        final = AIMessage(
            content=response.content or ("" if not result.hit_max_rounds else
                                         "Sorry, I couldn't finish looking that up. Please try again."),
            response_metadata={**(response.response_metadata or {}),
                               **({"ttft_ms": ttft_ms} if ttft_ms is not None else {})},
        )
        if usage:
            final.usage_metadata = usage
        return final

    result.message = invoker.run_tracked(loop)
    return result
