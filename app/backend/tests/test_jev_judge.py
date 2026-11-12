"""Tests for the Jev judge (ai-judge-jev) and its branch in BrandVoiceEvaluator. No network.

Run from app/backend:  python -m pytest tests
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

import src.graph  # noqa: F401  (import before src.agents, as the server does, to avoid a cycle)
from src.evaluation import jev_judge, judge
from src.utils import launchdarkly_config, typesafe_client

QUESTIONS = {
    "accuracy": {
        "type": "score",
        "instructions": "How well are the account facts supported?",
        "criteria": ["invented facts", "one wrong detail", "all supported"],
        "pass_min": 0.8,
        "metric_key": "$ld:ai:judge:jev-accuracy",
    },
    "hallucination": {
        "type": "noul",
        "instructions": "Does the response state an unsupported fact?",
        "invert": True,
        "pass_min": 0.5,
        "metric_key": "$ld:ai:judge:jev-hallucination-free",
    },
    "answers_question": {
        "type": "noul",
        "instructions": "Does the response address the question?",
        "pass_min": 0.5,
        "metric_key": "$ld:ai:judge:jev-answers-question",
    },
    "coherence": {
        "type": "score",
        "instructions": "How clear is the response?",
        "criteria": ["confusing", "cluttered", "clear"],
        "pass_min": 0.7,
        "metric_key": "$ld:ai:judge:jev-coherence",
    },
    "tone": {
        "type": "choice",
        "instructions": "What is the tone?",
        # Sorted, as LaunchDarkly serves it; criteria_order restores the intended order.
        "criteria": {"flat": "mechanical", "inappropriate": "rude", "warm_professional": "friendly"},
        "criteria_order": ["warm_professional", "flat", "inappropriate"],
        "pass_label": "warm_professional",
        "metric_key": "$ld:ai:judge:jev-tone-ok",
    },
}

SYSTEM = "You are grading one reply that {{name}}'s bank assistant sent."


def jev_config(replace: bool = False, enabled: bool = True) -> dict[str, Any]:
    custom: dict[str, Any] = {"questions": QUESTIONS}
    if replace:
        custom["replace_llm_judges"] = True
    return {
        "enabled": enabled,
        "provider": "TypeSafe",
        "_variation": "jev-only" if replace else "jev-alongside",
        "messages": [{"role": "system", "content": SYSTEM}],
        "model": {"name": "jev-1.14.0", "parameters": {}, "custom": custom},
    }


def answers(acc=1.9, hal=0.05, ans=0.97, coh=1.8, tone="warm_professional"):
    probs = {"warm_professional": 0.1, "flat": 0.1, "inappropriate": 0.1}
    probs[tone] = 0.8
    return {
        "model": "jev-1.14.0",
        "answers": {
            "accuracy": {"score": acc, "confidence": 0.9, "probabilities": [0.0, 0.1, 0.9]},
            "hallucination": {"noul": hal},
            "answers_question": {"noul": ans},
            "coherence": {"score": coh, "confidence": 0.85, "probabilities": [0.0, 0.2, 0.8]},
            "tone": {"choice": tone, "confidence": 0.8, "probabilities": probs},
        },
        "usage": {"input_tokens": 954, "output_tokens": 111},
    }


class FakeLD:
    def format_messages(self, messages, context_vars):
        return launchdarkly_config.LaunchDarklyClient.format_messages(self, messages, context_vars)


class RecordingTracker:
    def __init__(self):
        self.calls: list[Any] = []

    def track_duration(self, ms):
        self.calls.append("duration")

    def track_success(self):
        self.calls.append("success")

    def track_error(self):
        self.calls.append("error")

    def track_tokens(self, usage):
        self.calls.append(f"tokens:{usage.input}")

    def track_judge_result(self, result):
        self.calls.append((result.metric_key, result.score, result.judge_config_key))


class BrandInvoker:
    def __init__(self):
        self.tracker = RecordingTracker()
        self.user_context = object()


@pytest.fixture
def events(monkeypatch):
    sent: list[tuple[str, float]] = []

    class FakeClient:
        def track(self, event_name, context, data=None, metric_value=None):
            sent.append((event_name, metric_value))

    import ldclient

    monkeypatch.setattr(ldclient, "get", lambda: FakeClient())
    return sent


@pytest.fixture(autouse=True)
def fake_ld(monkeypatch):
    monkeypatch.setattr(launchdarkly_config, "get_ld_client", lambda: FakeLD())
    monkeypatch.setattr(judge, "get_ld_client", lambda: FakeLD())
    monkeypatch.setattr(judge, "_evaluator", None)


def mock_http(monkeypatch, handler) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def record(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_client, "_client", httpx.Client(transport=httpx.MockTransport(record)))
    return requests


def serve(monkeypatch, config) -> RecordingTracker:
    """Make evaluate_jev_config serve `config` (None: the LaunchDarkly `disabled` variation)."""
    tracker = RecordingTracker()
    if config is None:
        judge_ = jev_judge.JevJudge(mode=jev_judge.MODE_DISABLED, config={"enabled": False})
    else:
        judge_ = jev_judge.JevJudge(
            mode=jev_judge.mode_of(config), config=config, tracker=tracker, ld_context=object()
        )
    monkeypatch.setattr(judge, "evaluate_jev_config", lambda uc: judge_)
    return tracker


def fake_llm(monkeypatch, acc=0.9, coh=0.9, fail=False):
    calls: list[str] = []

    async def accuracy(self, *a):
        calls.append("accuracy")
        if fail:
            raise RuntimeError("bedrock down")
        return {"score": acc, "passed": acc >= 0.8, "reason": "llm", "issues": []}, "claude-sonnet-5", {"input": 900, "output": 120}

    async def coherence(self, *a):
        calls.append("coherence")
        return {"score": coh, "passed": coh >= 0.7, "reason": "llm", "issues": []}, "claude-sonnet-5", {"input": 700, "output": 100}

    monkeypatch.setattr(judge.BrandVoiceEvaluator, "_evaluate_accuracy", accuracy)
    monkeypatch.setattr(judge.BrandVoiceEvaluator, "_evaluate_coherence", coherence)
    return calls


def evaluate(brand=None, user_context=None):
    user_context = user_context or {"user_key": "u1", "name": "Demo User", "domain": "togglebank"}
    return asyncio.run(
        judge.BrandVoiceEvaluator().evaluate_async(
            "Is autopay on?", "- Autopay enabled: True", "Yes, autopay is on.", user_context, brand
        )
    )


# --- mode selection -------------------------------------------------------


def test_mode_of():
    assert jev_judge.mode_of(jev_config()) == jev_judge.MODE_ALONGSIDE
    assert jev_judge.mode_of(jev_config(replace=True)) == jev_judge.MODE_ONLY
    assert jev_judge.mode_of(jev_config(enabled=False)) == jev_judge.MODE_DISABLED
    assert jev_judge.mode_of({"enabled": True, "provider": "Bedrock:Anthropic"}) == jev_judge.MODE_DISABLED
    assert jev_judge.mode_of({}) == jev_judge.MODE_DISABLED


def test_unreadable_config_turns_jev_off(monkeypatch):
    class Broken:
        def get_ai_config(self, *a, **k):
            raise RuntimeError("no LaunchDarkly")

    monkeypatch.setattr(launchdarkly_config, "get_ld_client", lambda: Broken())
    assert jev_judge.evaluate_jev_config({"user_key": "u1"}).mode == jev_judge.MODE_DISABLED


# --- branches ---------------------------------------------------------------


def test_disabled_runs_only_the_llm_judges(monkeypatch, events):
    serve(monkeypatch, None)
    calls = fake_llm(monkeypatch)
    requests = mock_http(monkeypatch, lambda r: httpx.Response(200, json=answers()))
    result = evaluate(BrandInvoker())
    assert calls == ["accuracy", "coherence"]
    assert requests == []
    assert "jev" not in result
    assert result["judge_model_name"] == "claude-sonnet-5"
    assert ("$ld:ai:judge:accuracy", 0.9) in events
    assert not any(k.startswith("jev-") for k, _ in events)


def test_alongside_runs_both_and_sends_every_event(monkeypatch, events):
    tracker = serve(monkeypatch, jev_config())
    calls = fake_llm(monkeypatch)
    requests = mock_http(monkeypatch, lambda r: httpx.Response(200, json=answers()))
    brand = BrandInvoker()
    result = evaluate(brand)

    assert calls == ["accuracy", "coherence"]
    assert len(requests) == 1
    assert result["accuracy"]["reason"] == "llm"
    jev = result["jev"]
    assert jev["mode"] == "jev-alongside" and jev["model"] == "jev-1.14.0" and jev["error"] is None
    assert set(jev["results"]) == set(QUESTIONS)
    assert jev["disagrees"] is False

    # LLM judge events still go out, and the five classifications go on the brand_agent tracker
    assert ("$ld:ai:judge:accuracy", 0.9) in events
    sent = {c[0]: c[1] for c in brand.tracker.calls if isinstance(c, tuple)}
    assert set(sent) == {q["metric_key"] for q in QUESTIONS.values()}
    assert sent["$ld:ai:judge:jev-accuracy"] == pytest.approx(0.95)
    assert sent["$ld:ai:judge:jev-hallucination-free"] == pytest.approx(0.95)
    assert all(c[2] == "ai-judge-jev" for c in brand.tracker.calls if isinstance(c, tuple))

    # the judge path's own events
    assert tracker.calls == ["duration", "success", "tokens:954"]
    keys = dict(events)
    assert keys["jev-judge-error"] == 0.0
    assert keys["jev-judge-cost-cents"] == pytest.approx(954 * 4.2e-08 * 100)
    assert keys["jev-judge-disagreement"] == 0.0


def test_alongside_records_disagreement(monkeypatch, events):
    serve(monkeypatch, jev_config())
    fake_llm(monkeypatch, acc=0.5)  # LLM fails accuracy, Jev passes it
    mock_http(monkeypatch, lambda r: httpx.Response(200, json=answers()))
    result = evaluate(BrandInvoker())
    assert result["jev"]["disagrees"] is True
    assert ("jev-judge-disagreement", 1.0) in events


def test_only_skips_the_llm_judges_and_fills_their_results(monkeypatch, events):
    serve(monkeypatch, jev_config(replace=True))
    calls = fake_llm(monkeypatch)
    mock_http(monkeypatch, lambda r: httpx.Response(200, json=answers(hal=0.9, tone="inappropriate")))
    result = evaluate(BrandInvoker())

    assert calls == []
    assert result["judge_model_name"] == "jev-1.14.0"
    assert result["judge_input_tokens"] == 954
    # accuracy passes only when accuracy and hallucination both pass; same for coherence and tone
    assert result["accuracy"]["score"] == pytest.approx(0.95)
    assert result["accuracy"]["passed"] is False
    assert result["accuracy"]["issues"] == ["hallucination failed"]
    assert result["coherence"]["passed"] is False
    assert "jev-1.14.0" in result["accuracy"]["reason"]
    assert result["overall_passed"] is False
    # no LLM judge events, and no disagreement event
    keys = [k for k, _ in events]
    assert "$ld:ai:judge:accuracy" not in keys and "$ld:ai:hallucinations" not in keys
    assert "jev-judge-disagreement" not in keys
    assert result["jev"]["disagrees"] is None


def test_fallback_override_applies_only_to_llm_results(monkeypatch, events):
    serve(monkeypatch, jev_config(replace=True))
    fake_llm(monkeypatch)
    mock_http(monkeypatch, lambda r: httpx.Response(200, json=answers(acc=0.4)))
    uc = {"user_key": "u1", "name": "Demo User", "domain": "togglebank", "is_fallback": True}
    result = evaluate(BrandInvoker(), uc)
    assert result["accuracy"]["score"] == pytest.approx(0.2)  # Jev's, not the fixed 0.95

    serve(monkeypatch, jev_config())
    result = evaluate(BrandInvoker(), uc)
    assert result["accuracy"]["score"] == 0.95  # LLM path still demo-proofed
    assert result["jev"]["results"]["accuracy"]["score"] == pytest.approx(0.2)


# --- errors ---------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(403, json={"detail": "bad key"}),
        lambda r: httpx.Response(500, text="boom"),
        lambda r: httpx.Response(200, json={"model": "jev-1.14.0", "usage": {"input_tokens": 1}}),
    ],
    ids=["403", "500-twice", "missing-answers"],
)
def test_jev_error_on_alongside_keeps_the_llm_judges(monkeypatch, events, handler):
    tracker = serve(monkeypatch, jev_config())
    calls = fake_llm(monkeypatch)
    mock_http(monkeypatch, handler)
    brand = BrandInvoker()
    result = evaluate(brand)
    assert calls == ["accuracy", "coherence"]
    assert result["accuracy"]["reason"] == "llm" and result["accuracy"]["passed"] is True
    assert result["jev"]["error"] and result["jev"]["results"] is None
    assert ("jev-judge-error", 1.0) in events
    assert "jev-judge-disagreement" not in dict(events)
    assert tracker.calls == ["duration", "error"]
    assert not any(isinstance(c, tuple) for c in brand.tracker.calls)


def test_missing_key_fails_without_calling_jev(monkeypatch, events):
    serve(monkeypatch, jev_config(replace=True))
    fake_llm(monkeypatch)
    requests = mock_http(monkeypatch, lambda r: httpx.Response(200, json=answers()))
    monkeypatch.delenv("TYPESAFE_API_KEY")
    result = evaluate(BrandInvoker())
    assert requests == []
    assert "TYPESAFE_API_KEY" in result["jev"]["error"]
    assert result["accuracy"]["passed"] is False and "Jev judge failed" in result["accuracy"]["reason"]
    assert ("jev-judge-error", 1.0) in events


def test_llm_failure_is_not_counted_as_disagreement(monkeypatch, events):
    serve(monkeypatch, jev_config())
    fake_llm(monkeypatch, fail=True)
    mock_http(monkeypatch, lambda r: httpx.Response(200, json=answers()))
    result = evaluate(BrandInvoker())
    assert result["jev"]["disagrees"] is None
    assert "jev-judge-disagreement" not in dict(events)


def test_no_brand_tracker_still_sends_judge_events(monkeypatch, events):
    serve(monkeypatch, jev_config())
    fake_llm(monkeypatch)
    mock_http(monkeypatch, lambda r: httpx.Response(200, json=answers()))
    result = evaluate(None)
    assert result["jev"]["error"] is None
    assert ("jev-judge-error", 0.0) in events


# --- request and mapping ----------------------------------------------------


def test_request_is_built_from_the_variation(monkeypatch, events):
    serve(monkeypatch, jev_config())
    fake_llm(monkeypatch)
    requests = mock_http(monkeypatch, lambda r: httpx.Response(200, json=answers()))
    evaluate(BrandInvoker())
    body = json.loads(requests[0].content)
    assert body["model"] == "jev-1.14.0"
    assert body["state"] == {
        "customer_question": "Is autopay on?",
        "account_profile": "- Autopay enabled: True",
        "response": "Yes, autopay is on.",
    }
    qs = body["questions"]
    assert list(qs) == list(QUESTIONS)
    assert qs["accuracy"]["instructions"] == (
        "You are grading one reply that Demo User's bank assistant sent. How well are the account facts supported?"
    )
    assert qs["accuracy"]["criteria"] == QUESTIONS["accuracy"]["criteria"]
    assert list(qs["tone"]["criteria"]) == ["warm_professional", "flat", "inappropriate"]
    assert "criteria" not in qs["hallucination"]
    # only Jev's fields are sent, not thresholds or metric keys
    assert set(qs["tone"]) == {"type", "instructions", "criteria"}


def test_empty_account_context(monkeypatch):
    state, _ = jev_judge.build_request(jev_config(), "q", "", "r", {})
    assert state["account_profile"] == "No account context available"


@pytest.mark.parametrize(
    "name, answer, score, passed, label",
    [
        ("accuracy", {"score": 1.6, "confidence": 0.7}, 0.8, True, None),
        ("accuracy", {"score": 1.5, "confidence": 0.7}, 0.75, False, None),
        ("hallucination", {"noul": 0.49}, 0.51, True, None),
        ("hallucination", {"noul": 0.7}, 0.3, False, None),
        ("answers_question", {"noul": 0.4}, 0.4, False, None),
        ("coherence", {"score": 1.4, "confidence": 0.9}, 0.7, True, None),
        ("tone", {"choice": "flat", "confidence": 0.6, "probabilities": {"warm_professional": 0.3, "flat": 0.6}}, 0.3, False, "flat"),
        ("tone", {"choice": "warm_professional", "confidence": 0.9, "probabilities": {"warm_professional": 0.9}}, 0.9, True, "warm_professional"),
    ],
)
def test_answer_mapping(name, answer, score, passed, label):
    r = jev_judge.map_answer(QUESTIONS[name], answer)
    assert r["score"] == pytest.approx(score)
    assert r["passed"] is passed
    assert r["label"] == label
    assert r["metric_key"] == QUESTIONS[name]["metric_key"]


def test_span_events_include_each_jev_question():
    class Span:
        def __init__(self):
            self.events = []

        def add_event(self, name, attributes):
            self.events.append(attributes)

    span = Span()
    results = {
        "accuracy": {"score": 0.9, "passed": True, "reason": "llm"},
        "coherence": {"score": 0.8, "passed": True, "reason": "llm"},
        "jev": {"results": {"tone": {"score": 0.8, "passed": True, "label": "warm_professional"}}},
    }
    judge._add_evaluation_events(span, results)
    names = [e["gen_ai.evaluation.name"] for e in span.events]
    assert names == ["accuracy", "coherence", "jev.tone"]
    assert span.events[2]["gen_ai.evaluation.explanation"] == "warm_professional"
