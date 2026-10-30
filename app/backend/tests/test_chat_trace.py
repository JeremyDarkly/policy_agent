"""Tests for /api/chat's includeTrace and onlineJudges fields. No network: the graph is mocked.

Run from app/backend:  python -m pytest tests
"""

import os

# An empty key keeps observability and LaunchDarkly off; load_dotenv won't
# override it with a .env found further up the tree.
os.environ["LAUNCHDARKLY_SDK_KEY"] = ""

import pytest
from fastapi.testclient import TestClient

import server

SLOTS = [
    {"slot_id": "SLOT-20261102-0900", "datetime_str": "Monday, November 02 at 09:00 AM"},
    {"slot_id": "SLOT-20261102-1000", "datetime_str": "Monday, November 02 at 10:00 AM"},
]

PROFILE = {"user_key": "ci-e2e-customer", "name": "CI Customer", "account_id": "TB-CHK-GOLD-2024"}


def scheduler_route_state() -> dict:
    """Final state of a triage -> scheduler -> brand voice run."""
    return {
        "query_type": "schedule_agent",
        "confidence_score": 0.9,
        "final_response": "Hi CI Customer! Here are some times: SLOT-20261102-0900.",
        "user_context": PROFILE,
        "agent_data": {
            "triage_router": {"model": "triage-model", "variation": "triage-v1", "tokens": {}},
            "scheduler_specialist": {
                "model": "scheduler-model",
                "served_model": "scheduler-model-served",
                "variation": "scheduler-v1",
                "available_slots": SLOTS,
                "response": "Available: SLOT-20261102-0900, SLOT-20261102-1000",
                "tokens": {},
            },
            "brand_voice": {
                "model": "brand-model",
                "variation": "brand-v1",
                "response": "Hi CI Customer! Here are some times: SLOT-20261102-0900.",
                "tokens": {},
            },
        },
    }


@pytest.fixture
def calls(monkeypatch):
    """Replace the graph; record the keyword arguments it was called with."""
    recorded: list[dict] = []

    def fake_run_workflow(**kwargs):
        recorded.append(kwargs)
        return scheduler_route_state()

    monkeypatch.setattr(server, "run_workflow", fake_run_workflow)
    return recorded


@pytest.fixture
def client():
    return TestClient(server.app)


def test_no_trace_and_judges_on_by_default(client, calls):
    body = client.post("/api/chat", json={"userInput": "Book me a call"}).json()

    assert body["error"] is None
    assert body["trace"] is None
    assert calls[0]["online_judges"] is True
    assert [step["agent"] for step in body["agentFlow"]] == [
        "triage_router",
        "scheduler_specialist",
        "brand_voice",
    ]


def test_trace_has_one_entry_per_agent_that_ran(client, calls):
    body = client.post(
        "/api/chat",
        json={"userInput": "Book me a call", "includeTrace": True, "onlineJudges": False},
    ).json()

    assert calls[0]["online_judges"] is False
    trace = body["trace"]
    assert [a["agent"] for a in trace["agents"]] == [
        "triage_router",
        "scheduler_specialist",
        "brand_voice",
    ]
    assert [a["config_key"] for a in trace["agents"]] == [
        "triage_agent",
        "scheduler_agent",
        "brand_agent",
    ]
    assert [a["variation"] for a in trace["agents"]] == ["triage-v1", "scheduler-v1", "brand-v1"]

    scheduler = trace["agents"][1]
    assert scheduler["model"] == "scheduler-model"
    assert scheduler["served_model"] == "scheduler-model-served"
    assert scheduler["output"] == "Available: SLOT-20261102-0900, SLOT-20261102-1000"
    assert scheduler["available_slots"] == ["SLOT-20261102-0900", "SLOT-20261102-1000"]
    assert "available_slots" not in trace["agents"][0]
    assert trace["profile"] == PROFILE


def test_general_question_trace_has_no_specialist(client, monkeypatch):
    state = scheduler_route_state()
    del state["agent_data"]["scheduler_specialist"]
    monkeypatch.setattr(server, "run_workflow", lambda **kwargs: state)

    body = client.post("/api/chat", json={"userInput": "What is APY?", "includeTrace": True}).json()

    assert [a["agent"] for a in body["trace"]["agents"]] == ["triage_router", "brand_voice"]
