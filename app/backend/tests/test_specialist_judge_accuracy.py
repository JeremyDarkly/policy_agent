"""Tests for specialist-judge-accuracy and the model prices added beside it. No network.

Run from app/backend:  python -m pytest tests
"""

import asyncio
from datetime import date
from typing import Any

import pytest

import src.graph  # noqa: F401  (import before src.agents, as the server does, to avoid a cycle)
from src.agents.brand_voice_agent import calculate_model_cost
from src.evaluation import jev_judge, judge
from src.utils import launchdarkly_config
from src.utils.ai_tracking import ConfigTracker, track_data_for, track_data_of

ACCOUNT_TRACK_DATA = track_data_for(
    "account_agent",
    {"model": {"name": "gemini-4-carbon"}, "provider": {"name": "Gemini"}},
    {"variationKey": "gemini-4-carbon", "version": 3},
)


class FakeLD:
    def format_messages(self, messages, context_vars):
        return launchdarkly_config.LaunchDarklyClient.format_messages(self, messages, context_vars)


class BrandInvoker:
    def __init__(self):
        self.tracker = None
        self.user_context = object()


@pytest.fixture
def events(monkeypatch) -> list[tuple[str, Any, float]]:
    """Every ldclient.get().track call, as (event, data, value)."""
    sent: list[tuple[str, Any, float]] = []

    class FakeClient:
        def track(self, event_name, context, data=None, metric_value=None):
            sent.append((event_name, data, metric_value))

    import ldclient

    monkeypatch.setattr(ldclient, "get", lambda: FakeClient())
    return sent


@pytest.fixture(autouse=True)
def llm_judges_only(monkeypatch):
    monkeypatch.setattr(launchdarkly_config, "get_ld_client", lambda: FakeLD())
    monkeypatch.setattr(judge, "get_ld_client", lambda: FakeLD())
    monkeypatch.setattr(judge, "_evaluator", None)
    disabled = jev_judge.JevJudge(mode=jev_judge.MODE_DISABLED, config={"enabled": False})
    monkeypatch.setattr(judge, "evaluate_jev_config", lambda uc: disabled)


def fake_llm(monkeypatch, acc=0.9, fail=False):
    async def accuracy(self, *a):
        if fail:
            raise RuntimeError("bedrock down")
        return {"score": acc, "passed": acc >= 0.8, "reason": "llm", "issues": []}, "m", {"input": 1, "output": 1}

    async def coherence(self, *a):
        return {"score": 0.8, "passed": True, "reason": "llm", "issues": []}, "m", {"input": 1, "output": 1}

    monkeypatch.setattr(judge.BrandVoiceEvaluator, "_evaluate_accuracy", accuracy)
    monkeypatch.setattr(judge.BrandVoiceEvaluator, "_evaluate_coherence", coherence)


def evaluate(track_data, user_context=None):
    user_context = user_context or {"user_key": "u1", "name": "Demo User", "domain": "togglebank"}
    return asyncio.run(
        judge.BrandVoiceEvaluator().evaluate_async(
            "What is my balance?", "- Balance: $100", "Your balance is $100.", user_context,
            BrandInvoker(), specialist_track_data=track_data,
        )
    )


def specialist_events(events):
    return [e for e in events if e[0] == judge.SPECIALIST_ACCURACY_EVENT]


# --- specialist-judge-accuracy ---------------------------------------------


def test_accuracy_is_sent_on_the_specialists_variation(monkeypatch, events):
    fake_llm(monkeypatch, acc=0.7)
    evaluate(ACCOUNT_TRACK_DATA)

    [(_, data, value)] = specialist_events(events)
    assert value == pytest.approx(0.7)
    assert data["configKey"] == "account_agent"
    assert data["variationKey"] == "gemini-4-carbon"
    assert data["runId"] == ACCOUNT_TRACK_DATA["runId"]  # the specialist's own run
    assert data["judgeConfigKey"] == "ai-judge-accuracy"
    # The existing judge events are unchanged
    assert ("$ld:ai:judge:accuracy", None, pytest.approx(0.7)) in events


def test_not_sent_without_specialist_track_data(monkeypatch, events):
    fake_llm(monkeypatch)
    evaluate(None)
    assert specialist_events(events) == []
    assert any(e[0] == "$ld:ai:judge:accuracy" for e in events)


def test_not_sent_when_the_accuracy_judge_failed(monkeypatch, events):
    fake_llm(monkeypatch, fail=True)
    evaluate(ACCOUNT_TRACK_DATA)
    assert specialist_events(events) == []


def test_not_sent_for_the_fallback_configs_fixed_score(monkeypatch, events):
    fake_llm(monkeypatch, acc=0.2)
    evaluate(ACCOUNT_TRACK_DATA, {"user_key": "u1", "domain": "togglebank", "is_fallback": True})
    assert specialist_events(events) == []


def test_track_data_of_copies_the_trackers_data():
    class Invoker:
        tracker = ConfigTracker(None, None, ACCOUNT_TRACK_DATA)

    data = track_data_of(Invoker())
    assert data == ACCOUNT_TRACK_DATA and data is not ACCOUNT_TRACK_DATA
    assert track_data_of(object()) is None

    class NoTracker:
        tracker = None

    assert track_data_of(NoTracker()) is None


# --- model prices ------------------------------------------------------------


def test_llama_3_1_70b_has_a_price():
    cost = calculate_model_cost("us.meta.llama3-1-70b-instruct-v1:0", 1000, 1000)
    assert cost == pytest.approx(0.00072 * 2)


@pytest.mark.parametrize(
    "today, expected",
    [
        (date(2026, 11, 30), 0.002 + 0.010),  # introductory price, last day
        (date(2026, 12, 1), 0.004 + 0.020),  # list price
    ],
)
def test_gemini_4_carbon_price_changes_on_december_1(today, expected):
    assert calculate_model_cost("gemini-4-carbon", 1000, 1000, today=today) == pytest.approx(expected)


def test_existing_prices_are_unchanged():
    assert calculate_model_cost("us.anthropic.claude-sonnet-5", 1000, 1000) == pytest.approx(0.012)
    assert calculate_model_cost("us.meta.llama4-maverick-17b-instruct-v1:0", 1000, 0) == pytest.approx(0.00024)
    assert calculate_model_cost("unknown-model", 1000, 1000) == 0.0
