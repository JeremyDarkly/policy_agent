"""Tests for recording which model served each call. No network: provider responses are canned.

Run from app/backend:  python -m pytest tests
"""

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from ldclient import Context

from src.utils import launchdarkly_config
from src.utils.bedrock_llm import BedrockConverseLLM
from src.utils.launchdarkly_config import ModelInvoker, NoOpTracker
from src.utils.served_model import (
    MISMATCH_EVENT,
    SERVED_EVENT,
    SOURCE_REQUESTED_FALLBACK,
    SOURCE_RESPONSE,
    SOURCE_ROUTER_TRACE,
    bedrock_served_model,
    model_id_without_routing,
    models_match,
    report_served_model,
    served_model,
    served_model_id_of,
)

CASES = json.loads((Path(__file__).parent / "model_match_cases.json").read_text())
CONTEXT = Context.builder("user-1").kind("user").build()


class FakeLD:
    def __init__(self):
        self.events: list[tuple[str, dict, float]] = []

    def track(self, event_name, context, data=None, metric_value=None):
        self.events.append((event_name, data, metric_value))

    def variation(self, *args, **kwargs):
        return True

    def names(self):
        return [name for name, _, _ in self.events]

    def mismatch_values(self):
        return [value for name, _, value in self.events if name == MISMATCH_EVENT]


@pytest.fixture
def ld(monkeypatch):
    fake = FakeLD()
    monkeypatch.setattr(launchdarkly_config.ldclient, "get", lambda: fake)
    return fake


# --- Matching -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("requested,served", CASES["match"])
def test_snapshots_and_versions_match(requested, served):
    assert models_match(requested, served)


@pytest.mark.parametrize("requested,served", CASES["mismatch"])
def test_different_models_do_not_match(requested, served):
    assert not models_match(requested, served)


def test_model_id_without_routing():
    assert model_id_without_routing("us.anthropic.claude-3-5-sonnet-20241022-v2:0") == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert model_id_without_routing("openai/GPT-5.4-mini") == "gpt-5.4-mini"


# --- Reading the served model ---------------------------------------------------------------------------------------


def test_served_model_from_metadata():
    assert served_model({"model_name": "gpt-4o-2024-08-06"}, "gpt-4o") == ("gpt-4o-2024-08-06", SOURCE_RESPONSE)
    assert served_model({"model": "claude-x"}, "claude") == ("claude-x", SOURCE_RESPONSE)
    assert served_model({}, "gpt-4o") == ("gpt-4o", SOURCE_REQUESTED_FALLBACK)
    assert served_model(None, "gpt-4o") == ("gpt-4o", SOURCE_REQUESTED_FALLBACK)
    meta = {"model_name": "m", "served_model_source": SOURCE_ROUTER_TRACE}
    assert served_model(meta, "router") == ("m", SOURCE_ROUTER_TRACE)


def test_served_model_id_of():
    assert served_model_id_of(AIMessage(content="", response_metadata={"model_name": "a"}), "b") == "a"
    assert served_model_id_of(AIMessage(content=""), "b") == "b"


def test_bedrock_without_router_falls_back_to_requested():
    assert bedrock_served_model({"output": {}}, "us.amazon.nova-pro-v1:0") == (
        "us.amazon.nova-pro-v1:0",
        SOURCE_REQUESTED_FALLBACK,
    )


def test_bedrock_router_trace():
    response = {"trace": {"promptRouter": {"invokedModelId": "arn:aws:bedrock:us-east-1::foundation-model/x"}}}
    assert bedrock_served_model(response, "arn:router") == (
        "arn:aws:bedrock:us-east-1::foundation-model/x",
        SOURCE_ROUTER_TRACE,
    )


# --- Events ---------------------------------------------------------------------------------------------------------


def _report(ld, requested, served, source):
    return report_served_model(
        ld, CONTEXT, config_key="cfg", variation_key="var", provider="openai",
        requested=requested, served=served, source=source,
    )


def test_matching_model_sends_mismatch_zero():
    ld = FakeLD()
    assert not _report(ld, "gpt-4o", "gpt-4o-2024-08-06", SOURCE_RESPONSE)
    assert ld.names() == [SERVED_EVENT, MISMATCH_EVENT]
    assert ld.mismatch_values() == [0]
    _, data, value = ld.events[0]
    assert value == 1
    assert data == {
        "configKey": "cfg", "variationKey": "var", "provider": "openai",
        "requestedModel": "gpt-4o", "servedModel": "gpt-4o-2024-08-06", "source": SOURCE_RESPONSE,
    }


def test_different_model_sends_mismatch_one():
    ld = FakeLD()
    assert _report(ld, "gpt-4o", "gpt-4o-mini", SOURCE_RESPONSE)
    assert ld.names() == [SERVED_EVENT, MISMATCH_EVENT]
    assert ld.mismatch_values() == [1]


def test_router_choice_sends_no_mismatch_event():
    ld = FakeLD()
    assert not _report(ld, "arn:aws:bedrock:us-east-1:1:default-prompt-router/anthropic.claude:1", "anthropic.claude-3-haiku-20240307-v1:0", SOURCE_ROUTER_TRACE)
    assert ld.names() == [SERVED_EVENT]


def test_unchecked_call_sends_no_mismatch_event():
    ld = FakeLD()
    assert not _report(ld, "us.amazon.nova-pro-v1:0", "us.amazon.nova-pro-v1:0", SOURCE_REQUESTED_FALLBACK)
    assert ld.names() == [SERVED_EVENT]


# --- Bedrock --------------------------------------------------------------------------------------------------------


def _bedrock(model_id):
    # model_construct skips __init__, which would open an AWS session.
    llm = BedrockConverseLLM.model_construct(model_id=model_id, max_tokens=100)
    return llm


CONVERSE_RESPONSE = {
    "output": {"message": {"role": "assistant", "content": [{"text": "hello"}]}},
    "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
    "stopReason": "end_turn",
}


def test_bedrock_converse_result_without_router():
    result = _bedrock("us.amazon.nova-pro-v1:0")._converse_result(CONVERSE_RESPONSE)
    meta = result.generations[0].message.response_metadata
    assert meta["model_id"] == "us.amazon.nova-pro-v1:0"
    assert meta["model_name"] == "us.amazon.nova-pro-v1:0"
    assert meta["served_model_source"] == SOURCE_REQUESTED_FALLBACK
    assert result.llm_output["model_name"] == "us.amazon.nova-pro-v1:0"


def test_bedrock_converse_result_with_router():
    response = {**CONVERSE_RESPONSE, "trace": {"promptRouter": {"invokedModelId": "anthropic.claude-3-haiku-20240307-v1:0"}}}
    result = _bedrock("arn:router")._converse_result(response)
    assert result.llm_output["model_name"] == "anthropic.claude-3-haiku-20240307-v1:0"
    assert result.llm_output["model_id"] == "arn:router"
    assert result.generations[0].message.response_metadata["served_model_source"] == SOURCE_ROUTER_TRACE


def test_bedrock_invoke_without_router_sends_only_served_event(ld):
    llm = _bedrock("us.amazon.nova-pro-v1:0")
    llm._bedrock_client = type("Client", (), {"converse": lambda self, **kw: CONVERSE_RESPONSE})()
    _invoker(llm, "bedrock").invoke([HumanMessage(content="hi")])
    assert ld.names() == [SERVED_EVENT]
    assert ld.events[0][1]["source"] == SOURCE_REQUESTED_FALLBACK


def test_bedrock_stream_reads_router_from_metadata_event():
    stream = {"stream": [
        {"contentBlockDelta": {"delta": {"text": "hi"}}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                      "trace": {"promptRouter": {"invokedModelId": "anthropic.claude-3-haiku-20240307-v1:0"}}}},
    ]}
    llm = _bedrock("arn:router")
    llm._bedrock_client = type("Client", (), {"converse_stream": lambda self, **kw: stream})()
    chunks = list(llm._stream([HumanMessage(content="hi")]))
    final = chunks[-1].message.response_metadata
    assert final["model_name"] == "anthropic.claude-3-haiku-20240307-v1:0"
    assert final["served_model_source"] == SOURCE_ROUTER_TRACE


# --- OpenAI and Anthropic through ModelInvoker ----------------------------------------------------------------------


def _invoker(model, provider):
    return ModelInvoker(
        model, NoOpTracker(), config_key="cfg", user_context=CONTEXT,
        skip_span_annotation=True, variation_key="var", provider=provider,
    )


def test_openai_served_model_reaches_launchdarkly(ld, monkeypatch):
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model="gpt-4o", api_key="test")
    api_response = {
        "id": "c1", "model": "gpt-4o-2024-08-06",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    monkeypatch.setattr(ChatOpenAI, "_generate", lambda self, *a, **k: self._create_chat_result(api_response))
    result = _invoker(model, "openai").invoke([HumanMessage(content="hi")])
    assert result.response_metadata["model_name"] == "gpt-4o-2024-08-06"
    assert ld.names() == [SERVED_EVENT, MISMATCH_EVENT]
    assert ld.mismatch_values() == [0]
    assert ld.events[0][1]["requestedModel"] == "gpt-4o"
    assert ld.events[0][1]["servedModel"] == "gpt-4o-2024-08-06"


def test_openai_wrong_model_sends_mismatch_one(ld, monkeypatch):
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model="gpt-4o", api_key="test")
    api_response = {
        "id": "c1", "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    monkeypatch.setattr(ChatOpenAI, "_generate", lambda self, *a, **k: self._create_chat_result(api_response))
    _invoker(model, "openai").invoke([HumanMessage(content="hi")])
    assert ld.names() == [SERVED_EVENT, MISMATCH_EVENT]
    assert ld.mismatch_values() == [1]


def test_anthropic_served_model_reaches_launchdarkly(ld, monkeypatch):
    from anthropic.types import Message, TextBlock, Usage
    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic(model="claude-sonnet-4-5", api_key="test")
    api_response = Message(
        id="m1", type="message", role="assistant", model="claude-sonnet-4-5-20250929",
        content=[TextBlock(type="text", text="hi")], stop_reason="end_turn", stop_sequence=None,
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(ChatAnthropic, "_generate", lambda self, *a, **k: self._format_output(api_response))
    result = _invoker(model, "anthropic").invoke([HumanMessage(content="hi")])
    assert result.response_metadata["model_name"] == "claude-sonnet-4-5-20250929"
    assert ld.names() == [SERVED_EVENT, MISMATCH_EVENT]
    assert ld.mismatch_values() == [0]
    assert ld.events[0][1]["requestedModel"] == "claude-sonnet-4-5"


class _StreamingModel(BaseChatModel):
    """Yields chunks the way providers do: the model name arrives on one chunk only."""

    @property
    def _llm_type(self) -> str:
        return "fake-stream"

    model_name: str = "claude-sonnet-4-5"

    def _generate(self, *a: Any, **k: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])

    def stream(self, messages, *a, **k):
        from langchain_core.messages import AIMessageChunk

        yield AIMessageChunk(content="", response_metadata={"model_name": "claude-sonnet-4-5-20250929"})
        yield AIMessageChunk(content="hi")


def test_stream_reports_model_from_any_chunk(ld):
    list(_invoker(_StreamingModel(), "anthropic").stream([HumanMessage(content="hi")]))
    assert ld.names() == [SERVED_EVENT, MISMATCH_EVENT]
    assert ld.mismatch_values() == [0]
    assert ld.events[0][1]["servedModel"] == "claude-sonnet-4-5-20250929"


def test_no_context_sends_nothing(ld):
    invoker = _invoker(_StreamingModel(), "anthropic")
    invoker.user_context = None
    invoker.report_served_model_of({"model_name": "x"})
    assert ld.events == []


# --- Azure AI Foundry deployments -----------------------------------------------------------------------------------


class _AzureModel(BaseChatModel):
    """A Foundry deployment called by its deployment name; the response names the underlying model."""

    model_name: str = "branch-grok"
    served: str = "grok-4.6"
    finish_reason: str = "stop"

    @property
    def _llm_type(self) -> str:
        return "fake-azure"

    def _generate(self, *a: Any, **k: Any) -> ChatResult:
        meta = {"model_name": self.served, "finish_reason": self.finish_reason}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", response_metadata=meta))])


def _azure_invoker(model, expected_model):
    invoker = _invoker(model, "azure ai foundry")
    invoker.expected_model = expected_model
    return invoker


def test_azure_deployment_compared_to_base_model(ld):
    _azure_invoker(_AzureModel(), "grok-4.6").invoke([HumanMessage(content="hi")])
    assert ld.names() == [SERVED_EVENT, MISMATCH_EVENT]
    assert ld.mismatch_values() == [0]
    data = ld.events[0][1]
    assert data["requestedModel"] == "grok-4.6"
    assert data["servedModel"] == "grok-4.6"
    assert data["deployment"] == "branch-grok"


def test_azure_dated_model_matches_base_model(ld):
    # Azure OpenAI deployments answer with the model's dated version.
    model = _AzureModel(model_name="branch-gpt-4o", served="gpt-4o-2024-11-20")
    _azure_invoker(model, "gpt-4o").invoke([HumanMessage(content="hi")])
    assert ld.mismatch_values() == [0]


def test_azure_without_base_model(ld):
    # Without baseModel the deployment name is compared, so a differently named deployment reads as a mismatch.
    _azure_invoker(_AzureModel(), "").invoke([HumanMessage(content="hi")])
    assert ld.mismatch_values() == [1]
    data = ld.events[0][1]
    assert data["requestedModel"] == "branch-grok"
    assert "deployment" not in data


def test_content_filter_sends_event_and_raises(ld):
    from src.utils.served_model import CONTENT_FILTERED_EVENT

    class Tracker(NoOpTracker):
        calls: list = []

        def track_success(self):
            self.calls.append("success")

        def track_error(self):
            self.calls.append("error")

    invoker = _azure_invoker(_AzureModel(finish_reason="content_filter"), "grok-4.6")
    invoker.tracker = Tracker()
    with pytest.raises(ValueError, match="content filter"):
        invoker.invoke([HumanMessage(content="hi")])
    assert invoker.tracker.calls == ["error"]
    assert ld.names() == [CONTENT_FILTERED_EVENT]
    name, data, value = ld.events[0]
    assert value == 1
    assert data["variationKey"] == "var"
    assert data["servedModel"] == "grok-4.6"
    assert data["deployment"] == "branch-grok"


def test_get_model_invoker_reads_base_model(monkeypatch):
    from src.utils.llm_config import get_model_invoker

    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://example-resource.services.ai.azure.com")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "test-key")
    config = {
        "provider": "Azure AI Foundry",
        "model": {"name": "branch-grok", "parameters": {"temperature": 0.1}, "custom": {"baseModel": "grok-4.6"}},
        "_variation": "azure-foundry",
    }
    invoker, _ = get_model_invoker("branch_agent", evaluated=(config, NoOpTracker(), CONTEXT))
    assert invoker.provider == "azure ai foundry"
    assert invoker.expected_model == "grok-4.6"
    assert invoker.model.model_name == "branch-grok"
