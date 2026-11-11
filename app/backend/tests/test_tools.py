"""Tests for the tools the model may call (src/tools/registry.py). No network.

Run from app/backend:  python -m pytest tests
"""

import json
import re

import pytest

from src.tools.branches import BRANCHES, find_branches
from src.tools.registry import TOOLS, get_available_slots_tool


def test_registry_keys_are_snake_case():
    # Amazon Nova fails on hyphenated tool names (#74).
    assert set(TOOLS) == {"get_available_slots", "find_branches"}
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", key) for key in TOOLS)


def test_registry_keys_match_terraform():
    from pathlib import Path

    tf = (Path(__file__).resolve().parents[3] / "infrastructure" / "launchdarkly" / "tools.tf").read_text()
    declared = set(re.findall(r'^\s*key\s*=\s*"([^"]+)"', tf, re.MULTILINE))
    assert set(TOOLS) <= declared


def test_available_slots_shape():
    result = get_available_slots_tool(days_ahead=7)
    assert result["days_ahead"] == 7
    assert 1 <= len(result["slots"]) <= 10
    for slot in result["slots"]:
        assert set(slot) == {"slot_id", "when"}
        assert re.fullmatch(r"SLOT-\d{8}-\d{4}", slot["slot_id"])
    json.dumps(result)


def test_available_slots_default_and_numeric_strings():
    assert get_available_slots_tool()["days_ahead"] == 7
    assert get_available_slots_tool(days_ahead="3")["days_ahead"] == 3
    assert get_available_slots_tool(days_ahead=5.0)["days_ahead"] == 5


@pytest.mark.parametrize("bad", [0, 15, -1, 2.5, "soon", None, True, [7]])
def test_available_slots_rejects_bad_days(bad):
    with pytest.raises(ValueError, match="days_ahead"):
        get_available_slots_tool(days_ahead=bad)


@pytest.mark.parametrize(
    "location, city",
    [
        ("Boston", "Boston"),
        ("boston, MA", "Boston"),
        ("San Francisco, CA", "San Francisco"),
        ("SF", "San Francisco"),
        ("94110", "San Francisco"),
        ("Back Bay", "Boston"),
        ("NYC", "New York"),
        ("Brooklyn", "New York"),
        ("Oakland", "Oakland"),
    ],
)
def test_find_branches_found(location, city):
    result = find_branches(location=location)
    assert result["status"] == "found"
    assert result["location"] == location
    ids = {b["branch_id"] for b in result["branches"]}
    assert ids <= {b["branch_id"] for b in BRANCHES if b["city"] == city}
    assert 1 <= len(result["branches"]) <= 5
    json.dumps(result)


def test_find_branches_does_not_leak_match_fields():
    branch = find_branches(location="Boston")["branches"][0]
    assert "zip_prefixes" not in branch and "neighborhoods" not in branch


def test_find_branches_neighborhood_narrows():
    result = find_branches(location="Mission District")
    assert [b["branch_id"] for b in result["branches"]] == ["TB-SF-002"]


@pytest.mark.parametrize("location", ["Denver", "60601", "Chicago, IL"])
def test_find_branches_not_found(location):
    result = find_branches(location=location)
    assert result["status"] == "not_found"
    assert result["location"] == location
    assert "branches" not in result
    assert "San Francisco" in result["message"]


def test_find_branches_unknown_zip_falls_back_to_city():
    assert find_branches(location="Boston 60601")["status"] == "found"


@pytest.mark.parametrize("bad", ["", "   ", None, 94110])
def test_find_branches_rejects_bad_location(bad):
    with pytest.raises(ValueError, match="location"):
        find_branches(location=bad)
