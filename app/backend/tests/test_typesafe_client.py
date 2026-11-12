"""Tests for typesafe_client.system_one's answer checks. No network: HTTP is mocked.

Run from app/backend:  python -m pytest tests
"""

import httpx
import pytest

from src.utils import typesafe_client
from src.utils.typesafe_client import TypeSafeError, is_jev_config, system_one

QUESTIONS = {
    "accuracy": {"type": "score", "instructions": "a", "criteria": ["bad", "ok", "good"]},
    "hallucination": {"type": "noul", "instructions": "h"},
    "tone": {"type": "choice", "instructions": "t", "criteria": {"warm": "w", "flat": "f"}},
}

ANSWERS = {
    "accuracy": {"score": 1.9, "confidence": 0.9, "probabilities": [0.0, 0.1, 0.9], "legend": {}},
    "hallucination": {"noul": 0.05},
    "tone": {"choice": "warm", "confidence": 0.95, "probabilities": {"warm": 0.95, "flat": 0.05}},
}


def mock(monkeypatch, body):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(
        typesafe_client,
        "_client",
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))),
    )


def response(answers):
    return {"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 954, "output_tokens": 111}}


def test_noul_answer_needs_only_noul(monkeypatch):
    mock(monkeypatch, response(ANSWERS))
    data = system_one("jev-1.14.0", {"s": 1}, QUESTIONS)
    assert data["answers"]["hallucination"] == {"noul": 0.05}


@pytest.mark.parametrize(
    "question, field",
    [("hallucination", "noul"), ("accuracy", "score"), ("accuracy", "confidence"), ("tone", "choice"), ("tone", "probabilities")],
)
def test_missing_field_for_the_question_type_raises(monkeypatch, question, field):
    answers = {k: dict(v) for k, v in ANSWERS.items()}
    del answers[question][field]
    mock(monkeypatch, response(answers))
    with pytest.raises(TypeSafeError, match=f"answers.{question}.{field}"):
        system_one("jev-1.14.0", {"s": 1}, QUESTIONS)


def test_is_jev_config():
    assert is_jev_config({"provider": "TypeSafe"})
    assert is_jev_config({"provider": "typesafe:jev"})
    assert not is_jev_config({"provider": "Bedrock:Anthropic"})
    assert not is_jev_config({})
