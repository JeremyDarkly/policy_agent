"""get_ai_config and the AI metrics trackers, on the LaunchDarkly AI SDK 1.0.0 (launchdarkly-ai-server)."""

import pytest
from ldclient import Context

from src.utils.ai_tracking import ConfigTracker, GraphTracker, TokenUsage, track_data_for
from src.utils.launchdarkly_config import LaunchDarklyClient

AGENT_VARIATION = {
    "_ldMeta": {"enabled": True, "variationKey": "tool-calling", "version": 3, "mode": "agent",
                "modelKey": "claude-haiku-4-5-branch", "modelVersion": 2},
    "model": {"name": "anthropic.claude-haiku-4-5", "parameters": {"maxTokens": 900},
              "custom": {"baseModel": "claude-haiku-4-5"}},
    "provider": {"name": "Bedrock"},
    "instructions": "Help {{name}} in {{location}}. Tier: {{ldctx.tier}}. Unknown: {{nope}}.",
    "tools": {"find_branches": {"name": "find_branches", "type": "function", "description": "Find branches",
                                "parameters": {"type": "object"}, "extra": "dropped"},
              "get_hours": {"name": "get_hours", "type": "function", "parameters": {}}},
}

COMPLETION_VARIATION = {
    "_ldMeta": {"enabled": True, "variationKey": "default", "version": 1},
    "model": {"name": "gpt-4o", "parameters": {}},
    "provider": {"name": "openai"},
    "messages": [{"role": "system", "content": "You speak for {{name}} ({{ldctx.key}})."}],
    "custom": {"guardrail_id": "g-1"},
}


class FakeLD:
    def __init__(self, variations):
        self.variations = variations
        self.events = []

    def variation(self, key, context, default):
        return self.variations.get(key, default)

    def track(self, event, context, data, value):
        self.events.append((event, data, value))


def client_with(variations):
    client = object.__new__(LaunchDarklyClient)
    client.client = FakeLD(variations)
    return client


CONTEXT = {"user_key": "u-1", "name": "Ada", "location": "Oakland", "tier": "gold"}


def test_agent_variation_becomes_instructions_and_tools():
    config, tracker, ld_context = client_with({"branch_agent": AGENT_VARIATION}).get_ai_config("branch_agent", CONTEXT)

    assert config["_instructions"] == "Help Ada in Oakland. Tier: gold. Unknown: ."
    assert "messages" not in config
    assert config["provider"] == "Bedrock"
    assert config["_variation"] == "tool-calling"
    assert config["model"] == {"name": "anthropic.claude-haiku-4-5", "parameters": {"maxTokens": 900},
                               "custom": {"baseModel": "claude-haiku-4-5"}}
    assert config["tools"] == [
        {"name": "find_branches", "type": "function", "description": "Find branches",
         "parameters": {"type": "object"}},
        {"name": "get_hours", "type": "function", "parameters": {}},
    ]
    assert ld_context.key == "u-1"
    assert tracker.track_data["configKey"] == "branch_agent"
    assert tracker.track_data["variationKey"] == "tool-calling"
    assert tracker.track_data["version"] == 3
    assert tracker.track_data["modelName"] == "anthropic.claude-haiku-4-5"
    assert tracker.track_data["providerName"] == "Bedrock"
    assert tracker.track_data["modelKey"] == "claude-haiku-4-5-branch"
    assert tracker.track_data["modelVersion"] == 2


def test_completion_variation_becomes_messages():
    config, _, _ = client_with({"brand_agent": COMPLETION_VARIATION}).get_ai_config("brand_agent", CONTEXT)

    assert config["messages"] == [{"role": "system", "content": "You speak for  (u-1)."}]
    assert "_instructions" not in config
    assert config["_custom"] == {"guardrail_id": "g-1"}


@pytest.mark.parametrize("variations", [
    {},
    {"brand_agent": {**COMPLETION_VARIATION, "_ldMeta": {"enabled": False}}},
    {"brand_agent": {"_ldMeta": {"enabled": True}, "provider": {"name": "openai"}, "messages": []}},
])
def test_unservable_config_uses_the_default(variations):
    default = {"model": {"name": "haiku", "parameters": {"maxTokens": 2000}}, "provider": "bedrock",
               "messages": [{"role": "system", "content": "fallback"}]}
    config, tracker, _ = client_with(variations).get_ai_config("brand_agent", CONTEXT, default)

    assert config["_variation"] == "default-fallback"
    assert config["messages"] == [{"role": "system", "content": "fallback"}]
    assert config["provider"] == "bedrock"
    assert tracker.track_data["configKey"] == "brand_agent"
    assert tracker.track_data["modelName"] == "haiku"


def test_unservable_config_without_default_raises():
    with pytest.raises(RuntimeError, match="not found"):
        client_with({}).get_ai_config("brand_agent", CONTEXT)
    disabled = {"brand_agent": {**COMPLETION_VARIATION, "_ldMeta": {"enabled": False}}}
    with pytest.raises(RuntimeError, match="disabled"):
        client_with(disabled).get_ai_config("brand_agent", CONTEXT)


def test_config_tracker_sends_sdk_events():
    ld = FakeLD({})
    tracker = ConfigTracker(ld, Context.create("u-1"), track_data_for(
        "branch_agent", AGENT_VARIATION, AGENT_VARIATION["_ldMeta"], run_id="run-1"))

    tracker.track_duration(120)
    tracker.track_time_to_first_token(40)
    tracker.track_success()
    tracker.track_error()
    tracker.track_tokens(TokenUsage(total=30, input=20, output=10))
    tracker.track_tool_call("find_branches")
    tracker.track_feedback(positive=True)
    tracker.track_feedback(positive=False)
    assert tracker.track_duration_of(lambda: "ok") == "ok"

    names = [(event, value) for event, _, value in ld.events]
    assert names[:11] == [
        ("$ld:ai:duration:total", 120), ("$ld:ai:tokens:ttf", 40), ("$ld:ai:generation:success", 1),
        ("$ld:ai:generation:error", 1), ("$ld:ai:tokens:total", 30), ("$ld:ai:tokens:input", 20),
        ("$ld:ai:tokens:output", 10), ("$ld:ai:tool_call", 1), ("$ld:ai:feedback:user:positive", 1),
        ("$ld:ai:feedback:user:negative", 1), ("$ld:ai:duration:total", names[10][1]),
    ]
    payload = ld.events[0][1]
    assert payload == {"runId": "run-1", "configKey": "branch_agent", "variationKey": "tool-calling", "version": 3,
                       "modelName": "anthropic.claude-haiku-4-5", "providerName": "Bedrock",
                       "modelKey": "claude-haiku-4-5-branch", "modelVersion": 2}
    assert ld.events[7][1]["toolKey"] == "find_branches"


def test_tracker_send_failure_does_not_raise():
    class Broken(FakeLD):
        def track(self, *args):
            raise RuntimeError("down")

    ConfigTracker(Broken({}), Context.create("u"), {}).track_success()


def test_graph_tracker_sends_sdk_graph_events():
    ld = FakeLD({})
    graph = GraphTracker(ld, Context.create("u-1"), "banking_agent_graph", run_id="g-1")
    graph.track_node("triage_agent", 0)
    graph.track_handoff_success("triage_agent", "branch_agent")
    graph.track_total_tokens(0)
    graph.track_total_tokens(55)
    graph.track_duration(900)
    graph.track_invocation_success()

    assert [e for e, _, _ in ld.events] == [
        "$ld:ai:graph:node", "$ld:ai:graph:handoff_success", "$ld:ai:graph:total_tokens",
        "$ld:ai:graph:duration:total", "$ld:ai:graph:invocation_success",
    ]
    node = ld.events[0][1]
    assert node["graphKey"] == node["configKey"] == "banking_agent_graph"
    assert node["runId"] == "g-1" and node["nodeKey"] == "triage_agent" and node["index"] == 0
    assert ld.events[1][1]["sourceKey"] == "triage_agent" and ld.events[1][1]["targetKey"] == "branch_agent"
