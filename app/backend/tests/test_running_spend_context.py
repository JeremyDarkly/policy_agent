"""The AI Config context carries the user's running spend as a number. No network.

Run from app/backend:  python -m pytest tests
"""

import pytest

from src.utils import spend_ledger
from src.utils.launchdarkly_config import LaunchDarklyClient, with_running_spend


@pytest.fixture(autouse=True)
def clean_ledger():
    spend_ledger.reset()
    yield
    spend_ledger.reset()


def _build(context_dict):
    # _create_context does not touch the SDK, so skip __init__ (which waits for it).
    client = LaunchDarklyClient.__new__(LaunchDarklyClient)
    return client._create_context(with_running_spend(context_dict))


def test_new_user_gets_zero_as_a_float():
    ctx = _build({"user_key": "jane", "plan": "commercial"})
    value = ctx.get(spend_ledger.ATTRIBUTE)
    assert value == 0.0
    assert isinstance(value, float)
    assert ctx.get("plan") == "commercial"


def test_context_sees_spend_recorded_earlier_in_the_request():
    user = {"user_key": "jane"}
    spend_ledger.record(user, 0.0048)  # triage
    spend_ledger.record(user, 0.0028)  # specialist
    ctx = _build(user)
    assert ctx.get(spend_ledger.ATTRIBUTE) == pytest.approx(0.76)
    assert isinstance(ctx.get(spend_ledger.ATTRIBUTE), float)


def test_missing_user_key_reads_anonymous_and_input_is_not_mutated():
    spend_ledger.add("anonymous", 2)
    original = {"name": "x"}
    ctx = _build(original)
    assert ctx.key == "anonymous"
    assert ctx.get(spend_ledger.ATTRIBUTE) == 2.0
    assert spend_ledger.ATTRIBUTE not in original


def test_ledger_value_overrides_one_sent_by_the_caller():
    spend_ledger.add("jane", 1)
    ctx = _build({"user_key": "jane", spend_ledger.ATTRIBUTE: "99"})
    assert ctx.get(spend_ledger.ATTRIBUTE) == 1.0
