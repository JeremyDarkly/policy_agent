"""Tests that agent spans carry messages in the format LaunchDarkly's conversation view reads.

Run from app/backend:  python -m pytest tests
"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.graph import workflow
from src.utils.observability import set_input_message


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(workflow, "_tracer", provider.get_tracer("test"))
    return exporter


def _state(question: str) -> dict:
    return {"messages": [HumanMessage(content=question)], "user_context": {"user_key": "u-1"}}


def test_agent_span_records_question_and_reply(spans):
    def node(state):
        return {
            "messages": state["messages"] + [AIMessage(content="Your fee is $12.")],
            "agent_data": {"account_specialist": {"model": "m-1", "tokens": {"input": 3, "output": 4}}},
        }

    workflow._traced_node("account_specialist", node)(_state("What is my fee?"))

    (span,) = spans.get_finished_spans()
    attrs = span.attributes
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.agent.name"] == "account_specialist"
    assert json.loads(attrs["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": "What is my fee?"}]}
    ]
    assert json.loads(attrs["gen_ai.output.messages"]) == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Your fee is $12."}], "finish_reason": "stop"}
    ]
    assert attrs["agent.model"] == "m-1"


def test_input_is_the_customer_question_not_a_later_message(spans):
    state = _state("Where is the nearest branch?")
    state["messages"].append(AIMessage(content="Routing to branch_specialist"))

    workflow._traced_node("brand_voice", lambda s: {"messages": [AIMessage(content="It's on Main St.")]})(state)

    (span,) = spans.get_finished_spans()
    content = json.loads(span.attributes["gen_ai.input.messages"])[0]["parts"][0]["content"]
    assert content == "Where is the nearest branch?"


def test_triage_span_reads_agent_data_under_its_own_key(spans):
    def node(state):
        return {
            "messages": state["messages"] + [AIMessage(content="Routing to account_specialist")],
            "agent_data": {"triage_router": {"model": "triage-model"}},
        }

    workflow._traced_node("triage", node)(_state("hi"))

    (span,) = spans.get_finished_spans()
    assert span.attributes["agent.model"] == "triage-model"


def test_node_without_a_reply_sets_no_output(spans):
    workflow._traced_node("triage", lambda s: {})(_state("hi"))

    (span,) = spans.get_finished_spans()
    assert "gen_ai.output.messages" not in span.attributes
    assert "gen_ai.input.messages" in span.attributes


def test_set_input_message_keeps_non_ascii_text(spans):
    with workflow._tracer.start_as_current_span("root") as span:
        set_input_message(span, "¿Cuál es mi saldo?")

    (span,) = spans.get_finished_spans()
    assert "¿Cuál es mi saldo?" in span.attributes["gen_ai.input.messages"]
