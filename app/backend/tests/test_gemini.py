"""Tests for the Gemini branch of the provider switch and for list-shaped replies. No network.

The canned reply below has the shape a live gemini-4-carbon call returned through
langchain-google-genai 4.5.0 on 2026-11-12: content as a list of text blocks, model_name in
response_metadata, and reasoning tokens counted inside output_tokens.

Run from app/backend:  python -m pytest tests
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_google_genai import ChatGoogleGenerativeAI
from ldclient import Context

import src.graph  # noqa: F401  (import before src.evaluation, as the server does, to avoid a cycle)
from src.evaluation.judge import BrandVoiceEvaluator
from src.utils import launchdarkly_config
from src.utils.launchdarkly_config import ModelInvoker, flatten_text_content, token_usage_from
from src.utils.llm_config import GEMINI_PROVIDERS, _create_llm_for_provider

JUDGE_JSON = '{"score": 0.4, "reasoning": "The fee is $12, not $15.", "issues": ["Wrong fee"]}'


def carbon_reply(content=None) -> AIMessage:
    return AIMessage(
        content=content if content is not None else [{"type": "text", "text": JUDGE_JSON}],
        response_metadata={"finish_reason": "STOP", "model_name": "gemini-4-carbon", "model_provider": "google_genai"},
        usage_metadata={
            "input_tokens": 131, "output_tokens": 609, "total_tokens": 740,
            "output_token_details": {"reasoning": 512},
        },
    )


@pytest.fixture
def google_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")


@pytest.fixture
def ld(monkeypatch):
    sent: list[tuple[str, dict, float]] = []

    class FakeLD:
        def track(self, event, context, data=None, value=None):
            sent.append((event, data or {}, value))

    monkeypatch.setattr(launchdarkly_config.ldclient, "get", lambda: FakeLD())
    return sent


class RecordingTracker:
    def __init__(self):
        self.tokens = None
        self.calls: list[str] = []

    def track_duration_of(self, func):
        return func()

    def track_success(self):
        self.calls.append("success")

    def track_error(self):
        self.calls.append("error")

    def track_tokens(self, usage):
        self.tokens = usage

    def track_time_to_first_token(self, ms):
        pass


# --- provider switch -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("provider", sorted(GEMINI_PROVIDERS) + ["Gemini"])
def test_gemini_provider_builds_chat_google_generative_ai(google_key, provider):
    llm = _create_llm_for_provider(provider, "gemini-4-carbon", None, 8000)
    assert isinstance(llm, ChatGoogleGenerativeAI)
    assert llm.model.endswith("gemini-4-carbon")
    assert llm.max_output_tokens == 8000
    assert llm.google_api_key.get_secret_value() == "test-key"


def test_temperature_is_passed_only_when_set(google_key):
    assert _create_llm_for_provider("gemini", "gemini-4-carbon", 0.2, 100).temperature == 0.2
    unset = _create_llm_for_provider("gemini", "gemini-4-carbon", None, 100)
    assert unset.temperature != 0.2  # the library's own default, not a value from us


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GOOGLE_API_KEY not set"):
        _create_llm_for_provider("gemini", "gemini-4-carbon", None, 100)


# --- list-shaped content ---------------------------------------------------------------------------------------------


def test_text_blocks_are_joined():
    message = carbon_reply([{"type": "text", "text": "Your fee "}, {"type": "text", "text": "is $12."}, "!"])
    flatten_text_content(message)
    assert message.content == "Your fee is $12.!"


@pytest.mark.parametrize(
    "content",
    [
        "already a string",
        [],
        [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "t1", "name": "find_branches", "input": {}}],
        [{"type": "reasoning", "reasoning": "..."}],
    ],
)
def test_other_content_is_left_alone(content):
    message = carbon_reply(content)
    flatten_text_content(message)
    assert message.content == content


def test_the_judge_parser_fails_on_the_raw_list_and_reads_the_flattened_string():
    # Why the flattening is needed: the judges' parser takes a string.
    raw = carbon_reply()
    assert BrandVoiceEvaluator._parse_eval_response(None, raw.content, 0.8)["error"] is True
    flatten_text_content(raw)
    parsed = BrandVoiceEvaluator._parse_eval_response(None, raw.content, 0.8)
    assert parsed["score"] == pytest.approx(0.4) and "error" not in parsed


def test_reasoning_tokens_are_not_counted_twice():
    usage = token_usage_from(carbon_reply().usage_metadata)
    assert (usage.input, usage.output, usage.total) == (131, 609, 740)


# --- through ModelInvoker --------------------------------------------------------------------------------------------


def test_invoke_returns_a_string_and_reports_the_served_model(google_key, ld, monkeypatch):
    model = _create_llm_for_provider("Gemini", "gemini-4-carbon", None, 2000)
    monkeypatch.setattr(
        ChatGoogleGenerativeAI, "_generate",
        lambda self, *a, **k: ChatResult(generations=[ChatGeneration(message=carbon_reply())]),
    )
    tracker = RecordingTracker()
    invoker = ModelInvoker(
        model, tracker, config_key="ai-judge-accuracy", user_context=Context.builder("u1").kind("user").build(),
        skip_span_annotation=True, variation_key="gemini-4-carbon", provider="gemini",
    )
    result = invoker.invoke([HumanMessage(content="Grade this reply.")])

    assert result.content == JUDGE_JSON
    assert tracker.calls == ["success"]
    assert (tracker.tokens.input, tracker.tokens.output) == (131, 609)
    mismatch = [value for event, _, value in ld if event == "ai-model-mismatch"]
    assert mismatch == [0]
