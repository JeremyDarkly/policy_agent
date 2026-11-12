"""AI Config metrics, sent as the LaunchDarkly AI SDK (``launchdarkly-ai-server`` 1.0.0) sends them.

The AI SDK records metrics itself when it calls the model (``config().invoke()``). This backend
calls Bedrock, Azure AI Foundry and OpenAI through LangChain, runs its own tool loop and orchestrates
the agents with LangGraph, so it records the same ``$ld:ai:*`` events here. The payload is the SDK's
track data (``launchdarkly_ai_server.tracking.execute_and_track``): runId, configKey, variationKey,
version, modelName, providerName, the pinned model-config stamps and, inside the agent graph,
graphKey.

Judge scores are sent as the SDK's judges send them: the judge's metric key as the event, with the
evaluated config's track data plus ``judgeConfigKey``.

Three events the 1.0.0 SDK doesn't send are kept, because the config Monitoring tab still charts
them: time to first token (``$ld:ai:tokens:ttf``) and user feedback
(``$ld:ai:feedback:user:positive`` / ``negative``).
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from launchdarkly_ai_server import model_stamps_from_meta


@dataclass(frozen=True)
class TokenUsage:
    """Token counts for one generation."""

    total: int
    input: int
    output: int


@dataclass(frozen=True)
class JudgeResult:
    """One judge score for the config a tracker belongs to."""

    judge_config_key: str
    metric_key: str
    score: float
    success: bool = True
    sampled: bool = True


def _environment_id() -> Optional[str]:
    # The AI SDK reads the same variable (tracking._try_get_environment_id).
    return os.environ.get("LD_ENVIRONMENT_ID") or None


def track_data_for(
    config_key: str,
    config: Optional[dict[str, Any]],
    meta: Optional[dict[str, Any]],
    graph_key: Optional[str] = None,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """The AI SDK's track data for one run of ``config_key`` (``config`` and ``meta`` from the variation)."""
    config = config if isinstance(config, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    data: dict[str, Any] = {
        "runId": run_id or str(uuid.uuid4()),
        "configKey": config_key,
        "variationKey": meta.get("variationKey", "") or "",
        "version": meta.get("version", 1) or 1,
        "modelName": (config.get("model") or {}).get("name", "") or "",
        "providerName": (config.get("provider") or {}).get("name", "") or "",
        **model_stamps_from_meta(meta),
    }
    if graph_key:
        data["graphKey"] = graph_key
    environment_id = _environment_id()
    if environment_id:
        data["environmentId"] = environment_id
    return data


class ConfigTracker:
    """Sends one run's metrics for an AI Config. A failure to send never fails the request."""

    def __init__(self, ld_client: Any, context: Any, track_data: dict[str, Any]):
        self._client = ld_client
        self._context = context
        self.track_data = track_data

    def _track(self, event: str, value: float, extra: Optional[dict[str, Any]] = None) -> None:
        try:
            self._client.track(event, self._context, {**self.track_data, **(extra or {})}, value)
        except Exception as e:
            print(f"  Failed to send {event}: {e}")

    def track_duration(self, duration: int) -> None:
        self._track("$ld:ai:duration:total", duration)

    def track_duration_of(self, func: Callable[[], Any]) -> Any:
        """Run ``func`` and send its duration, also when it raises."""
        start = time.monotonic()
        try:
            return func()
        finally:
            self.track_duration(int((time.monotonic() - start) * 1000))

    def track_time_to_first_token(self, time_to_first_token: int) -> None:
        self._track("$ld:ai:tokens:ttf", time_to_first_token)

    def track_success(self) -> None:
        self._track("$ld:ai:generation:success", 1)

    def track_error(self) -> None:
        self._track("$ld:ai:generation:error", 1)

    def track_tokens(self, tokens: TokenUsage) -> None:
        if tokens.total > 0:
            self._track("$ld:ai:tokens:total", tokens.total)
        if tokens.input > 0:
            self._track("$ld:ai:tokens:input", tokens.input)
        if tokens.output > 0:
            self._track("$ld:ai:tokens:output", tokens.output)

    def track_tool_call(self, tool_key: str) -> None:
        self._track("$ld:ai:tool_call", 1, {"toolKey": tool_key})

    def track_judge_result(self, result: JudgeResult) -> None:
        if result.sampled and result.success and result.metric_key:
            extra = {"judgeConfigKey": result.judge_config_key} if result.judge_config_key else None
            self._track(result.metric_key, result.score, extra)

    def track_feedback(self, positive: bool) -> None:
        self._track("$ld:ai:feedback:user:positive" if positive else "$ld:ai:feedback:user:negative", 1)


def track_data_of(model_invoker: Any) -> Optional[dict[str, Any]]:
    """A copy of the track data of ``model_invoker``'s tracker, or None when it has none.

    A specialist stores this in agent_data so the judges can later send scores on its config
    and variation (see ``BrandVoiceEvaluator._send_specialist_accuracy``).
    """
    data = getattr(getattr(model_invoker, "tracker", None), "track_data", None)
    return dict(data) if isinstance(data, dict) else None


class GraphTracker:
    """Sends one traversal's graph metrics, as the AI SDK's ``graph()`` runner does."""

    def __init__(self, ld_client: Any, context: Any, graph_key: str, run_id: Optional[str] = None):
        self._client = ld_client
        self._context = context
        self.graph_key = graph_key
        self.run_id = run_id or str(uuid.uuid4())
        # The graph flag carries a topology, not a model, so it has no variation or model to report
        # (launchdarkly_ai_server.utils.make_graph_track_data).
        self.track_data = track_data_for(graph_key, None, None, graph_key=graph_key, run_id=self.run_id)

    def _track(self, event: str, value: float, extra: Optional[dict[str, Any]] = None) -> None:
        try:
            self._client.track(event, self._context, {**self.track_data, **(extra or {})}, value)
        except Exception as e:
            print(f"  Failed to send {event}: {e}")

    def track_node(self, node_key: str, index: int) -> None:
        self._track("$ld:ai:graph:node", 1, {"nodeKey": node_key, "index": index})

    def track_handoff_success(self, source_key: str, target_key: str) -> None:
        self._track("$ld:ai:graph:handoff_success", 1, {"sourceKey": source_key, "targetKey": target_key})

    def track_invocation_success(self) -> None:
        self._track("$ld:ai:graph:invocation_success", 1)

    def track_total_tokens(self, total: int) -> None:
        if total > 0:
            self._track("$ld:ai:graph:total_tokens", total)

    def track_duration(self, duration: int) -> None:
        self._track("$ld:ai:graph:duration:total", duration)
