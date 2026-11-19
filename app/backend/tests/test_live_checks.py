"""Tests for the response-code-checks flag path: code checks on live replies beside the judges. No network.

Run from app/backend:  python -m pytest tests
"""

import asyncio
from typing import Any

import pytest

import src.graph  # noqa: F401  (import before src.agents, as the server does, to avoid a cycle)
from src.evaluation import jev_judge, judge, live_checks
from src.evaluation.live_checks import CheckInputs
from src.utils import launchdarkly_config
from src.utils.ai_tracking import track_data_for

PROFILE = """=== CUSTOMER ACCOUNT PROFILE (source of truth) ===
- Account ID: TB-CHK-GOLD-2024
- Account type: Gold Checking
- Account tier: gold
- Account status: active
- Member since: 2023-01-15
- Billing status: current
- Autopay enabled: True"""

ACCOUNT_TRACK_DATA = track_data_for("account_agent", {"model": {"name": "m"}}, {"variationKey": "default", "version": 2})
ACCOUNT_INPUTS = CheckInputs(
    specialist="account_specialist",
    query="What is my balance?",
    specialist_response="Account TB-CHK-GOLD-2024 has no balance in the profile.",
    account_context=PROFILE,
)


class FakeLD:
    def format_messages(self, messages, context_vars):
        return launchdarkly_config.LaunchDarklyClient.format_messages(self, messages, context_vars)


class BrandInvoker:
    def __init__(self):
        self.tracker = None
        self.user_context = object()


@pytest.fixture
def ld(monkeypatch):
    """A fake ldclient: `flag` is what response-code-checks serves; `events` collects track calls."""

    class FakeClient:
        flag: Any = True
        events: list[tuple[str, Any, float]] = []

        def variation(self, key, context, default):
            assert key == live_checks.FLAG_KEY
            if isinstance(self.flag, Exception):
                raise self.flag
            return self.flag

        def track(self, event_name, context, data=None, metric_value=None):
            self.events.append((event_name, data, metric_value))

    client = FakeClient()
    client.events = []
    import ldclient

    monkeypatch.setattr(ldclient, "get", lambda: client)
    monkeypatch.setattr(launchdarkly_config, "get_ld_client", lambda: FakeLD())
    monkeypatch.setattr(judge, "get_ld_client", lambda: FakeLD())
    monkeypatch.setattr(judge, "_evaluator", None)
    disabled = jev_judge.JevJudge(mode=jev_judge.MODE_DISABLED, config={"enabled": False})
    monkeypatch.setattr(judge, "evaluate_jev_config", lambda uc: disabled)
    return client


def fake_llm(monkeypatch, acc=0.9):
    async def accuracy(self, *a):
        return {"score": acc, "passed": acc >= 0.8, "reason": "llm", "issues": []}, "m", {"input": 1, "output": 1}

    async def coherence(self, *a):
        return {"score": 0.8, "passed": True, "reason": "llm", "issues": []}, "m", {"input": 1, "output": 1}

    monkeypatch.setattr(judge.BrandVoiceEvaluator, "_evaluate_accuracy", accuracy)
    monkeypatch.setattr(judge.BrandVoiceEvaluator, "_evaluate_coherence", coherence)


def evaluate(reply, inputs=ACCOUNT_INPUTS, user_context=None):
    user_context = user_context or {"user_key": "u1", "name": "Demo User", "domain": "togglebank"}
    return asyncio.run(
        judge.BrandVoiceEvaluator().evaluate_async(
            "What is my balance?", PROFILE, reply, user_context, BrandInvoker(),
            specialist_track_data=ACCOUNT_TRACK_DATA, code_check_inputs=inputs,
        )
    )


def code_check_events(events):
    return {name: (data, value) for name, data, value in events if name.startswith("code-check-")}


def test_flag_true_runs_the_checks_beside_the_judges(monkeypatch, ld):
    fake_llm(monkeypatch, acc=0.9)
    result = evaluate("Your Gold Checking balance is $4,218.37. Account TB-CHK-GOLD-2024.")

    sent = code_check_events(ld.events)
    assert sent["code-check-account-facts"][1] == 1.0  # invented balance
    assert "$4,218.37" in sent["code-check-account-facts"][0]["reason"]
    assert sent["code-check-account-facts"][0]["configKey"] == "account_agent"
    assert sent["code-check-account-facts"][0]["variationKey"] == "default"
    assert sent["code-check-draft-facts"][1] == 0.0
    assert sent["code-check-leaked-headings"][1] == 0.0
    assert sent["code-check-error"][1] == 0.0
    # The judge passed (0.9) and account-facts failed: they disagree.
    assert sent["code-check-judge-disagreement"][1] == 1.0
    # The judges are reported as before.
    assert result["accuracy"]["score"] == 0.9
    assert ("$ld:ai:judge:accuracy", None, pytest.approx(0.9)) in ld.events
    assert result["code_checks"]["results"]["account-facts"]["passed"] is False


@pytest.mark.parametrize("served", [False, None, RuntimeError("no client")])
def test_flag_false_or_unreadable_runs_no_checks(monkeypatch, ld, served):
    ld.flag = served
    fake_llm(monkeypatch)
    result = evaluate("Your Gold Checking balance is $4,218.37.")
    assert code_check_events(ld.events) == {}
    assert "code_checks" not in result
    assert any(e[0] == "$ld:ai:judge:accuracy" for e in ld.events)
    assert any(e[0] == judge.SPECIALIST_ACCURACY_EVENT for e in ld.events)


def test_judge_results_are_the_same_either_way(monkeypatch, ld):
    fake_llm(monkeypatch, acc=0.85)
    on = evaluate("Your account is active.")
    judge_events_on = [e for e in ld.events if not e[0].startswith("code-check-")]
    ld.events.clear()
    ld.flag = False
    off = evaluate("Your account is active.")
    assert on["accuracy"] == off["accuracy"] and on["coherence"] == off["coherence"]
    assert judge_events_on == ld.events


def test_agreeing_checks_send_zero_disagreement(monkeypatch, ld):
    fake_llm(monkeypatch, acc=0.9)
    evaluate("Your account TB-CHK-GOLD-2024 is active and has no balance in the profile.")
    sent = code_check_events(ld.events)
    assert sent["code-check-account-facts"][1] == 0.0
    assert sent["code-check-judge-disagreement"][1] == 0.0


def test_a_check_that_raises_sends_code_check_error(monkeypatch, ld):
    fake_llm(monkeypatch)

    def broken(reply):
        raise ValueError("boom")

    monkeypatch.setitem(live_checks.__dict__, "checks_for", lambda inputs: {"leaked-headings": broken})
    result = evaluate("Hello.")
    sent = code_check_events(ld.events)
    assert sent["code-check-error"][1] == 1.0
    assert "code-check-leaked-headings" not in sent
    assert "ValueError" in result["code_checks"]["results"]["leaked-headings"]["error"]


def test_checks_for_each_specialist():
    assert set(live_checks.checks_for(ACCOUNT_INPUTS)) == {"account-facts", "draft-facts", "leaked-headings"}
    scheduler = CheckInputs(specialist="scheduler_specialist", specialist_response="Slots: 9 AM")
    assert set(live_checks.checks_for(scheduler)) == {"draft-facts", "leaked-headings"}
    assert set(live_checks.checks_for(CheckInputs())) == {"leaked-headings"}


def test_no_disagreement_when_the_judge_failed():
    results = live_checks.run_checks(ACCOUNT_INPUTS, "Your balance is $5.")
    assert live_checks.disagreement(results, {"score": 0.0, "passed": False, "error": True}) is None
    assert live_checks.disagreement(results, None) is None
    assert live_checks.disagreement({"leaked-headings": results["leaked-headings"]}, {"passed": True}) is None


def test_no_inputs_runs_no_checks(monkeypatch, ld):
    fake_llm(monkeypatch)
    result = evaluate("Hello.", inputs=None)
    assert code_check_events(ld.events) == {} and "code_checks" not in result
