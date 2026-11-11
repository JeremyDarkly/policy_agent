"""Tests for tool calling: BedrockConverseLLM's toolConfig and toolUse conversion, and the tool loop.

No network: Bedrock is a fake client that replays Converse responses in the shape Bedrock
returns them (toolUse blocks, stopReason tool_use). Run from app/backend:  python -m pytest tests
"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from ldclient import Context

from src.tools.registry import TOOLS
from src.utils.bedrock_llm import BedrockConverseLLM, converse_tool_spec
from src.utils.launchdarkly_config import ModelInvoker
from src.utils.tool_loop import TOOL_CALL_ERROR_EVENT, is_error_result, run_tool, run_tool_loop, tools_for

CONTEXT = Context.builder("user-1").kind("user").build()

SLOTS_TOOL = {
    "name": "get_available_slots",
    "description": "Open banker appointment slots.",
    "parameters": {
        "type": "object",
        "properties": {"days_ahead": {"type": "integer", "minimum": 1, "maximum": 14}},
        "required": [],
    },
}
BRANCH_TOOL = {
    "name": "find_branches",
    "description": "ToggleBank branches near a location.",
    "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]},
}


def converse(content, stop="end_turn", tokens=(10, 5)):
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1], "totalTokens": sum(tokens)},
        "stopReason": stop,
        "metrics": {"latencyMs": 300},
    }


def tool_use(name, args, use_id="tooluse_1", text=None):
    blocks = [{"text": text}] if text else []
    return converse(blocks + [{"toolUse": {"toolUseId": use_id, "name": name, "input": args}}], stop="tool_use")


class FakeBedrock:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def converse(self, **params):
        self.requests.append(json.loads(json.dumps(params)))
        return self.responses.pop(0)


def bedrock(responses, model_id="us.amazon.nova-pro-v1:0"):
    llm = BedrockConverseLLM.model_construct(model_id=model_id, max_tokens=100)
    client = FakeBedrock(responses)
    llm._bedrock_client = client
    return llm, client


class FakeTracker:
    def __init__(self):
        self.calls = []

    def track_duration_of(self, func):
        result = func()
        self.calls.append(("duration",))
        return result

    def track_success(self):
        self.calls.append(("success",))

    def track_error(self):
        self.calls.append(("error",))

    def track_tokens(self, usage):
        self.calls.append(("tokens", usage.input, usage.output, usage.total))

    def track_time_to_first_token(self, ttft):
        self.calls.append(("ttft", ttft))

    def track_tool_call(self, key):
        self.calls.append(("tool_call", key))

    def names(self):
        return [c[0] for c in self.calls]


class FakeLD:
    def __init__(self):
        self.events = []

    def track(self, event, context, data=None, metric_value=None):
        self.events.append((event, data, metric_value))

    def variation(self, *a, **k):
        return True


@pytest.fixture
def ld(monkeypatch):
    from src.utils import launchdarkly_config

    fake = FakeLD()
    monkeypatch.setattr(launchdarkly_config.ldclient, "get", lambda: fake)
    import ldclient

    monkeypatch.setattr(ldclient, "get", lambda: fake)
    return fake


def invoker(llm, tracker):
    return ModelInvoker(llm, tracker, config_key="scheduler_agent", is_agent_config=True,
                        user_context=CONTEXT, variation_key="tool-calling", provider="bedrock")


# --- BedrockConverseLLM -------------------------------------------------------------------------------------------


def test_converse_tool_spec_from_library_tool():
    spec = converse_tool_spec(SLOTS_TOOL)
    assert spec == {
        "toolSpec": {
            "name": "get_available_slots",
            "description": "Open banker appointment slots.",
            "inputSchema": {"json": SLOTS_TOOL["parameters"]},
        }
    }


def test_bind_tools_sends_tool_config():
    llm, client = bedrock([converse([{"text": "hi"}])])
    llm.bind_tools([SLOTS_TOOL]).invoke([SystemMessage(content="sys"), HumanMessage(content="hello")])
    request = client.requests[0]
    assert request["toolConfig"]["tools"][0]["toolSpec"]["name"] == "get_available_slots"
    assert request["system"] == [{"text": "sys"}]


def test_no_tool_config_without_tools():
    llm, client = bedrock([converse([{"text": "hi"}])])
    llm.invoke([HumanMessage(content="hello")])
    assert "toolConfig" not in client.requests[0]


def test_tool_use_becomes_tool_calls():
    llm, _ = bedrock([tool_use("get_available_slots", {"days_ahead": 3}, text="Let me check.")])
    message = llm.bind_tools([SLOTS_TOOL]).invoke([HumanMessage(content="book me")])
    assert message.content == "Let me check."
    assert message.tool_calls == [
        {"name": "get_available_slots", "args": {"days_ahead": 3}, "id": "tooluse_1", "type": "tool_call"}
    ]
    assert message.response_metadata["stop_reason"] == "tool_use"


def test_tool_messages_convert_to_tool_results_in_one_user_turn():
    llm, _ = bedrock([])
    history = [
        HumanMessage(content="find branches in Boston and SF"),
        AIMessage(content="", tool_calls=[
            {"name": "find_branches", "args": {"location": "Boston"}, "id": "a", "type": "tool_call"},
            {"name": "find_branches", "args": {"location": "SF"}, "id": "b", "type": "tool_call"},
        ]),
        ToolMessage(content=json.dumps({"status": "found"}), tool_call_id="a"),
        ToolMessage(content="not json", tool_call_id="b", status="error"),
    ]
    messages, _ = llm._convert_messages_to_converse_format(history)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    # No empty text block next to the toolUse blocks: Converse rejects blank text.
    assert messages[1]["content"] == [
        {"toolUse": {"toolUseId": "a", "name": "find_branches", "input": {"location": "Boston"}}},
        {"toolUse": {"toolUseId": "b", "name": "find_branches", "input": {"location": "SF"}}},
    ]
    assert messages[2]["content"] == [
        {"toolResult": {"toolUseId": "a", "content": [{"json": {"status": "found"}}]}},
        {"toolResult": {"toolUseId": "b", "content": [{"text": "not json"}], "status": "error"}},
    ]


def test_tool_result_list_is_wrapped_in_an_object():
    llm, _ = bedrock([])
    messages, _ = llm._convert_messages_to_converse_format([
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "a", "type": "tool_call"}]),
        ToolMessage(content="[1, 2]", tool_call_id="a"),
    ])
    assert messages[2]["content"][0]["toolResult"]["content"] == [{"json": {"result": [1, 2]}}]


# --- Tool loop ----------------------------------------------------------------------------------------------------


def test_tools_for_keeps_implemented_tools_only():
    config = {"tools": [SLOTS_TOOL, {"name": "book_calendly_appointment"}]}
    assert [t["name"] for t in tools_for(config)] == ["get_available_slots"]
    assert tools_for({}) == []


def test_run_tool_errors_become_results():
    assert run_tool("find_branches", {"location": ""})[1].startswith("ValueError")
    assert run_tool("nope", {})[0] == {"error": "unknown tool 'nope'"}
    assert run_tool("find_branches", "Boston")[1] == "tool arguments must be a JSON object"
    result, error = run_tool("find_branches", {"location": "Boston"})
    assert error is None and result["status"] == "found"


def test_is_error_result():
    assert is_error_result({"error": "x"})
    assert is_error_result({"status": "not_found"})
    assert not is_error_result({"status": "found"})
    assert not is_error_result({"slots": []})


def test_loop_runs_tool_and_tracks_one_generation(ld):
    llm, client = bedrock([
        tool_use("get_available_slots", {"days_ahead": 3}),
        converse([{"text": "Here are the times."}], tokens=(40, 8)),
    ])
    tracker = FakeTracker()
    result = run_tool_loop(invoker(llm, tracker), [HumanMessage(content="book me")], [SLOTS_TOOL])

    assert result.message.content == "Here are the times."
    assert result.rounds == 2 and not result.hit_max_rounds
    assert [c["tool"] for c in result.calls] == ["get_available_slots"]
    assert result.calls[0]["args"] == {"days_ahead": 3} and result.calls[0]["error"] is None
    # The second request carries the toolUse and its toolResult with the real slots.
    second = client.requests[1]["messages"]
    assert second[1]["content"][0]["toolUse"]["name"] == "get_available_slots"
    slots = second[2]["content"][0]["toolResult"]["content"][0]["json"]["slots"]
    assert slots and all(s["slot_id"].startswith("SLOT-") for s in slots)
    # Tracked once, with tokens summed across both model calls, plus one tool call.
    assert tracker.names().count("success") == 1 and tracker.names().count("duration") == 1
    assert ("tokens", 50, 13, 63) in tracker.calls
    assert ("tool_call", "get_available_slots") in tracker.calls
    errors = [e for e in ld.events if e[0] == TOOL_CALL_ERROR_EVENT]
    assert errors == [(TOOL_CALL_ERROR_EVENT, {"configKey": "scheduler_agent", "variationKey": "tool-calling",
                                              "toolKey": "get_available_slots"}, 0.0)]


def test_loop_reports_not_found_and_bad_arguments(ld):
    llm, _ = bedrock([
        tool_use("find_branches", {"location": "Denver"}, use_id="t1"),
        tool_use("find_branches", {"location": ""}, use_id="t2"),
        converse([{"text": "No branch in Denver."}]),
    ])
    result = run_tool_loop(invoker(llm, FakeTracker()), [HumanMessage(content="Denver?")], [BRANCH_TOOL])
    assert [c["failed"] for c in result.calls] == [True, True]
    assert result.calls[0]["error"] is None and "not_found" in result.calls[0]["result"]
    assert result.calls[1]["error"].startswith("ValueError")
    assert [e[2] for e in ld.events if e[0] == TOOL_CALL_ERROR_EVENT] == [1.0, 1.0]


def test_loop_without_tool_call_is_one_round(ld):
    llm, client = bedrock([converse([{"text": "Goodbye!"}])])
    tracker = FakeTracker()
    result = run_tool_loop(invoker(llm, tracker), [HumanMessage(content="bye")], [SLOTS_TOOL])
    assert result.message.content == "Goodbye!" and result.calls == [] and result.rounds == 1
    assert "tool_call" not in tracker.names()
    assert not [e for e in ld.events if e[0] == TOOL_CALL_ERROR_EVENT]


def test_loop_stops_at_max_rounds(ld):
    llm, client = bedrock([tool_use("get_available_slots", {}, use_id=f"t{i}") for i in range(3)])
    result = run_tool_loop(invoker(llm, FakeTracker()), [HumanMessage(content="q")], [SLOTS_TOOL], max_rounds=3)
    assert result.hit_max_rounds and result.rounds == 3 and len(client.requests) == 3
    assert result.message.content  # a fallback answer, not an empty reply
    assert len(result.calls) == 3


def test_loop_model_error_tracks_error(ld):
    llm, _ = bedrock([])

    def boom(**params):
        raise RuntimeError("ModelErrorException: Model produced invalid sequence as part of ToolUse")

    llm._bedrock_client.converse = boom
    tracker = FakeTracker()
    with pytest.raises(RuntimeError):
        run_tool_loop(invoker(llm, tracker), [HumanMessage(content="q")], [SLOTS_TOOL])
    assert "error" in tracker.names() and "success" not in tracker.names()


def test_registry_tools_are_offered_with_library_schemas():
    # The loop binds the Library schema, not one derived from the Python signature.
    llm, client = bedrock([converse([{"text": "ok"}])])
    run_tool_loop(invoker(llm, FakeTracker()), [HumanMessage(content="q")], [SLOTS_TOOL, BRANCH_TOOL])
    names = [t["toolSpec"]["name"] for t in client.requests[0]["toolConfig"]["tools"]]
    assert names == ["get_available_slots", "find_branches"] and set(names) <= set(TOOLS)


def test_unlisted_slots():
    import src.graph  # noqa: F401  (src.agents imports through the graph)
    from src.agents.scheduler_specialist import unlisted_slots

    calls = [{"tool": "get_available_slots", "result": json.dumps({"slots": [{"slot_id": "SLOT-20261112-0900"}]})}]
    assert unlisted_slots("Book SLOT-20261112-0900?", calls) == []
    assert unlisted_slots("SLOT-20261112-0900 or SLOT-20261113-1400", calls) == ["SLOT-20261113-1400"]
    assert unlisted_slots("Tuesday at 10?", []) == []
    assert unlisted_slots("SLOT-20261112-0900", []) == ["SLOT-20261112-0900"]
