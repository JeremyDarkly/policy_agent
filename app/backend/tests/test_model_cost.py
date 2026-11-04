"""calculate_model_cost prices. No network.

Run from app/backend:  python -m pytest tests
"""

import pytest

import src.graph  # noqa: F401  (import before src.agents, as the server does, to avoid a cycle)
from src.agents.brand_voice_agent import calculate_model_cost


def test_llama_3_1_70b_is_priced():
    # account_agent's default model; $0.72 per million tokens in and out.
    cost = calculate_model_cost("us.meta.llama3-1-70b-instruct-v1:0", 1000, 1000)
    assert cost == pytest.approx(0.00144)


def test_llama_3_1_70b_does_not_take_the_llama4_rate():
    llama3 = calculate_model_cost("us.meta.llama3-1-70b-instruct-v1:0", 1500, 300)
    llama4 = calculate_model_cost("us.meta.llama4-maverick-17b-instruct-v1:0", 1500, 300)
    assert llama3 != llama4


def test_existing_prices_unchanged():
    assert calculate_model_cost("us.anthropic.claude-sonnet-5", 1500, 300) == pytest.approx(0.006)
    assert calculate_model_cost("us.anthropic.claude-haiku-4-5-20251001-v1:0", 1500, 300) == pytest.approx(0.003)
    assert calculate_model_cost("some-unknown-model", 1000, 1000) == 0.0
