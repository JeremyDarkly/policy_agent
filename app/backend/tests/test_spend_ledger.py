"""Tests for the in-process spend ledger. No network.

Run from app/backend:  python -m pytest tests
"""

import threading

import pytest

from src.utils import spend_ledger


@pytest.fixture(autouse=True)
def clean_ledger():
    spend_ledger.reset()
    yield
    spend_ledger.reset()


def test_get_is_zero_for_unknown_user():
    assert spend_ledger.get("nobody") == 0.0


def test_add_accumulates_per_user():
    assert spend_ledger.add("a", 1.5) == 1.5
    assert spend_ledger.add("a", 0.25) == 1.75
    assert spend_ledger.add("b", 2) == 2.0
    assert spend_ledger.get("a") == 1.75
    assert isinstance(spend_ledger.get("b"), float)


def test_reset_one_user_or_all():
    spend_ledger.add("a", 1)
    spend_ledger.add("b", 1)
    spend_ledger.reset("a")
    assert spend_ledger.get("a") == 0.0
    assert spend_ledger.get("b") == 1.0
    spend_ledger.reset()
    assert spend_ledger.get("b") == 0.0


def test_record_converts_dollars_to_cents_and_uses_user_key():
    total = spend_ledger.record({"user_key": "jane"}, 0.0048)
    assert total == pytest.approx(0.48)
    assert spend_ledger.get("jane") == pytest.approx(0.48)


def test_record_defaults_to_anonymous_and_ignores_missing_cost():
    spend_ledger.record({}, 0.01)
    assert spend_ledger.get("anonymous") == pytest.approx(1.0)
    assert spend_ledger.record({"user_key": "x"}, None) == 0.0
    assert spend_ledger.record({"user_key": "x"}, 0.0) == 0.0


def test_add_is_thread_safe():
    def worker():
        for _ in range(1000):
            spend_ledger.add("t", 0.01)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert spend_ledger.get("t") == pytest.approx(80.0)
