"""Tests for the token counts ModelInvoker sends to LaunchDarkly. No network: provider responses are canned.

Run from app/backend:  python -m pytest tests
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from src.utils.launchdarkly_config import ModelInvoker, NoOpTracker, token_usage_from

# A Grok call on Azure AI Foundry (issue #67, spike log 11): reasoning tokens are reported outside completion_tokens.
GROK_RESPONSE = {
    "id": "c1",
    "model": "grok-4.6",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {
        "prompt_tokens": 11,
        "completion_tokens": 1,
        "total_tokens": 161,
        "completion_tokens_details": {"reasoning_tokens": 149},
    },
}


class RecordingTracker(NoOpTracker):
    def __init__(self):
        self.tokens = []

    def track_tokens(self, token_usage):
        self.tokens.append((token_usage.input, token_usage.output, token_usage.total))


def _invoker(model, tracker):
    return ModelInvoker(model, tracker, config_key="cfg", skip_span_annotation=True, provider="azure ai foundry")


def test_reasoning_tokens_counted(monkeypatch):
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model="grok-4.6", api_key="test")
    monkeypatch.setattr(ChatOpenAI, "_generate", lambda self, *a, **k: self._create_chat_result(GROK_RESPONSE))
    tracker = RecordingTracker()
    _invoker(model, tracker).invoke([HumanMessage(content="hi")])
    assert tracker.tokens == [(11, 150, 161)]


def test_reasoning_already_in_output_is_not_counted_twice():
    # OpenAI reasoning models include reasoning in completion_tokens, so total = input + output.
    usage = {"input_tokens": 10, "output_tokens": 200, "total_tokens": 210, "output_token_details": {"reasoning": 150}}
    usage_counted = token_usage_from(usage)
    assert (usage_counted.input, usage_counted.output, usage_counted.total) == (10, 200, 210)


def test_no_total_falls_back_to_input_plus_output():
    usage_counted = token_usage_from({"input_tokens": 3, "output_tokens": 4})
    assert (usage_counted.input, usage_counted.output, usage_counted.total) == (3, 4, 7)


def test_no_usage_tracks_nothing():
    assert token_usage_from(None) is None
    assert token_usage_from({}) is None


class _StreamingModel(BaseChatModel):
    """Streams the way OpenAI-compatible APIs do with include_usage: usage arrives on the last chunk only."""

    model_name: str = "grok-4.6"

    @property
    def _llm_type(self) -> str:
        return "fake-stream"

    def _generate(self, *a: Any, **k: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])

    def stream(self, messages, *a, **k):
        yield AIMessageChunk(content="o", response_metadata={"model_name": "grok-4.6"})
        yield AIMessageChunk(content="k")
        yield AIMessageChunk(
            content="",
            usage_metadata={
                "input_tokens": 11, "output_tokens": 1, "total_tokens": 112,
                "output_token_details": {"reasoning": 100},
            },
        )


def test_stream_counts_reasoning_tokens():
    tracker = RecordingTracker()
    chunks = list(_invoker(_StreamingModel(), tracker).stream([HumanMessage(content="hi")]))
    assert len(chunks) == 3
    assert tracker.tokens == [(11, 101, 112)]
