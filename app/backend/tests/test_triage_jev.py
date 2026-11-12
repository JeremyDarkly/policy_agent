"""Tests for Jev triage (triage_agent's TypeSafe variation). No network: HTTP is mocked.

Run from app/backend:  python -m pytest tests
"""

from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage

import src.graph  # noqa: F401  (import before src.agents, as the server does, to avoid a cycle)
from src.agents import triage_router
from src.agents.brand_voice_agent import calculate_model_cost
from src.graph.state import QueryType
from src.utils import typesafe_client
from src.utils.launchdarkly_config import NoOpTracker

CRITERIA = {
    "schedule_agent": "A human banker must act.",
    "account_question": "The customer's own accounts.",
    "branch_lookup": "Physical locations.",
    "general_question": "Anything else.",
}

JEV_CONFIG = {
    "provider": "TypeSafe",
    "_variation": "jev-choice",
    "_instructions": "Route this bank customer's message.",
    "model": {
        "name": "jev-1.14.0",
        "parameters": {},
        "custom": {"question_type": "choice", "choice_confidence_min": 0.7, "criteria": CRITERIA},
    },
}


def jev_response(choice: str, confidence: float) -> dict[str, Any]:
    others = [k for k in CRITERIA if k != choice]
    rest = (1 - confidence) / len(others)
    return {
        "model": "jev-1.14.0",
        "answers": {
            "route": {
                "choice": choice,
                "confidence": confidence,
                "probabilities": {choice: confidence, **{k: rest for k in others}},
            }
        },
        "usage": {"input_tokens": 580, "output_tokens": 50},
    }


class RecordingTracker(NoOpTracker):
    def __init__(self):
        self.calls: list[str] = []

    def track_success(self):
        self.calls.append("success")

    def track_error(self):
        self.calls.append("error")

    def track_tokens(self, token_usage):
        self.calls.append(f"tokens:{token_usage.input}")

    def track_duration(self, duration):
        self.calls.append("duration")


@pytest.fixture
def events(monkeypatch):
    """Capture triage metric events instead of sending them."""
    sent: list[tuple[str, float]] = []

    class FakeClient:
        def track(self, key, context, metric_value=None):
            sent.append((key, metric_value))

    import ldclient

    monkeypatch.setattr(ldclient, "get", lambda: FakeClient())
    return sent


@pytest.fixture
def tracker(monkeypatch):
    t = RecordingTracker()
    monkeypatch.setattr(
        triage_router, "_evaluate_triage_config", lambda user_context: (JEV_CONFIG, t, object())
    )
    return t


def mock_http(monkeypatch, handler) -> list[httpx.Request]:
    """Route typesafe_client's HTTP through `handler`; return the requests made."""
    requests: list[httpx.Request] = []

    def record(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_client, "_client", httpx.Client(transport=httpx.MockTransport(record)))
    return requests


def answer_with(choice: str, confidence: float):
    return lambda request: httpx.Response(200, json=jev_response(choice, confidence))


def run(query: str = "What's my balance?") -> dict[str, Any]:
    state = {
        "messages": [HumanMessage(content=query)],
        "user_context": {"user_key": "u1", "domain": "togglebank"},
        "agent_data": {},
    }
    return triage_router.triage_node(state)


@pytest.mark.parametrize(
    "choice, query_type, node",
    [
        ("account_question", QueryType.ACCOUNT_QUESTION, "account_specialist"),
        ("branch_lookup", QueryType.BRANCH_LOOKUP, "branch_specialist"),
        ("schedule_agent", QueryType.SCHEDULE_AGENT, "scheduler_specialist"),
        ("general_question", QueryType.GENERAL_QUESTION, "brand_voice"),
    ],
)
def test_each_choice_routes_to_its_node(monkeypatch, tracker, events, choice, query_type, node):
    mock_http(monkeypatch, answer_with(choice, 0.95))
    result = run()
    assert result["query_type"] == query_type
    assert result["next_agent"] == node
    assert result["confidence_score"] == 0.95
    assert result["escalation_needed"] is (choice == "schedule_agent")
    data = result["agent_data"]["triage_router"]
    assert data["model"] == "jev-1.14.0"
    assert data["tokens"] == {"input": 580, "output": 50}
    assert data["ttft_ms"] is None
    assert ("triage-error", 0.0) in events
    assert "user_context" not in result  # extracted_context stays empty


def test_request_is_built_from_the_variation(monkeypatch, tracker, events):
    requests = mock_http(monkeypatch, answer_with("account_question", 0.95))
    run("What's my balance?")
    assert len(requests) == 1
    import json

    body = json.loads(requests[0].content)
    assert body["model"] == "jev-1.14.0"
    assert body["state"] == {"customer_message": "What's my balance?", "bank": "ToggleBank"}
    route = body["questions"]["route"]
    assert route["type"] == "choice"
    assert route["instructions"] == JEV_CONFIG["_instructions"]
    assert route["criteria"] == CRITERIA
    assert requests[0].headers["Authorization"] == "Bearer test-key"


def test_criteria_are_sent_in_criteria_order(monkeypatch, events):
    sorted_criteria = dict(sorted(CRITERIA.items()))  # as LaunchDarkly stores them
    custom = {**JEV_CONFIG["model"]["custom"], "criteria": sorted_criteria, "criteria_order": list(CRITERIA)}
    config = {**JEV_CONFIG, "model": {**JEV_CONFIG["model"], "custom": custom}}
    monkeypatch.setattr(triage_router, "_evaluate_triage_config", lambda uc: (config, NoOpTracker(), object()))
    requests = mock_http(monkeypatch, answer_with("account_question", 0.95))
    run()
    import json

    sent = json.loads(requests[0].content)["questions"]["route"]["criteria"]
    assert list(sent) == list(CRITERIA)


def test_low_confidence_reroutes_to_scheduler(monkeypatch, tracker, events):
    mock_http(monkeypatch, answer_with("account_question", 0.65))
    result = run()
    assert result["next_agent"] == "scheduler_specialist"
    assert result["escalation_needed"] is True
    assert ("triage-escalated", 1.0) in events


def test_threshold_comes_from_the_variation(monkeypatch, events):
    config = {**JEV_CONFIG, "model": {**JEV_CONFIG["model"], "custom": {
        **JEV_CONFIG["model"]["custom"], "choice_confidence_min": 0.9}}}
    monkeypatch.setattr(triage_router, "_evaluate_triage_config", lambda uc: (config, NoOpTracker(), object()))
    mock_http(monkeypatch, answer_with("account_question", 0.85))
    assert run()["next_agent"] == "scheduler_specialist"


def test_low_confidence_general_question_is_not_rerouted(monkeypatch, tracker, events):
    mock_http(monkeypatch, answer_with("general_question", 0.65))
    result = run("Who is the CEO of ToggleBank?")
    assert result["next_agent"] == "brand_voice"
    assert result["escalation_needed"] is False


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(403, json={"detail": {"error_type": "authentication_error"}}),
        lambda r: httpx.Response(500, text="boom"),
        lambda r: httpx.Response(200, json={"model": "jev-1.14.0", "usage": {"input_tokens": 1}}),
        lambda r: httpx.Response(200, text="not json"),
    ],
    ids=["403", "500-twice", "missing-answers", "not-json"],
)
def test_jev_failure_escalates_and_sends_triage_error(monkeypatch, tracker, events, handler):
    mock_http(monkeypatch, handler)
    result = run()
    assert result["next_agent"] == "scheduler_specialist"
    assert result["escalation_needed"] is True
    assert result["confidence_score"] == 0.5
    assert ("triage-error", 1.0) in events
    assert "error" in tracker.calls and "success" not in tracker.calls


def test_timeout_escalates(monkeypatch, tracker, events):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    requests = mock_http(monkeypatch, slow)
    result = run()
    assert len(requests) == 2  # one retry
    assert result["next_agent"] == "scheduler_specialist"
    assert ("triage-error", 1.0) in events


def test_5xx_is_retried_once(monkeypatch, tracker, events):
    replies = iter([httpx.Response(503), httpx.Response(200, json=jev_response("branch_lookup", 0.9))])
    requests = mock_http(monkeypatch, lambda r: next(replies))
    result = run()
    assert len(requests) == 2
    assert result["next_agent"] == "branch_specialist"
    assert ("triage-error", 0.0) in events


def test_missing_key_escalates_without_calling_jev(monkeypatch, tracker, events):
    requests = mock_http(monkeypatch, answer_with("account_question", 0.95))
    monkeypatch.delenv("TYPESAFE_API_KEY")
    result = run()
    assert requests == []
    assert result["next_agent"] == "scheduler_specialist"
    assert ("triage-error", 1.0) in events


def test_success_reports_to_the_ai_config_tracker(monkeypatch, tracker, events):
    mock_http(monkeypatch, answer_with("account_question", 0.95))
    run()
    assert tracker.calls == ["duration", "success", "tokens:580"]


def test_jev_cost_is_not_zero(monkeypatch, tracker, events):
    mock_http(monkeypatch, answer_with("account_question", 0.95))
    run()
    cost = dict(events)["triage-cost-cents"]
    assert cost == pytest.approx(580 * 4.2e-08 * 100)
    assert calculate_model_cost("jev-1.14.0", 580, 50) == pytest.approx(580 * 4.2e-08)


def test_is_jev_config():
    assert triage_router.is_jev_config({"provider": "TypeSafe"})
    assert not triage_router.is_jev_config({"provider": "Bedrock:Anthropic"})
    assert not triage_router.is_jev_config({})


class FakeInvoker:
    def __init__(self, content: str):
        self.content = content
        self.model = object()

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        return AIMessage(content=self.content)


class FakeLDClient:
    def build_langchain_messages(self, config, context_vars):
        return [HumanMessage(content=context_vars["query"])]


@pytest.mark.parametrize(
    "content, error, node",
    [
        ('{"query_type": "branch_lookup", "confidence_score": 0.9}', 0.0, "branch_specialist"),
        ("I can't answer in JSON", 1.0, "scheduler_specialist"),
    ],
)
def test_llm_path_evaluates_once_and_sends_triage_error(monkeypatch, events, content, error, node):
    llm_config = {"provider": "Bedrock:Anthropic", "_instructions": "x", "model": {"name": "claude-haiku-4-5"}}
    evaluations: list[Any] = []

    def evaluate(user_context):
        evaluations.append(user_context)
        return llm_config, NoOpTracker(), object()

    seen: dict[str, Any] = {}

    def fake_get_model_invoker(config_key, context=None, evaluated=None, **_):
        seen["evaluated"] = evaluated
        return FakeInvoker(content), evaluated[0]

    monkeypatch.setattr(triage_router, "_evaluate_triage_config", evaluate)
    monkeypatch.setattr(triage_router, "get_model_invoker", fake_get_model_invoker)
    monkeypatch.setattr(triage_router, "get_ld_client", lambda: FakeLDClient())
    result = run()
    assert len(evaluations) == 1
    assert seen["evaluated"][0] is llm_config
    assert result["next_agent"] == node
    assert ("triage-error", error) in events
