"""Tests for src/evaluation/code_checks.py, the deterministic checks shared by CI evals and the backend.

Several cases are replies seen in the issue #85 spike: invented slot IDs that aren't in SLOT- format
(FRD-101, M0115-10AM) and prompt step headings leaking into the reply (#22).
Run from app/backend:  python -m pytest tests
"""

import json

from src.evaluation import code_checks as cc
from src.tools.calendar import get_available_slots

GOLD = """=== CUSTOMER ACCOUNT PROFILE (source of truth) ===
- Account ID: TB-CHK-GOLD-2024
- Account type: Gold Checking
- Account tier: gold
- Account status: active
- Member since: 2023-01-15
- Currency: USD
- Billing status: current
- Payment method: auto_pay
- Autopay enabled: True
- Risk profile: low"""

AVAILABLE = (
    "- Thursday, October 29 at 9:00 AM (ID: SLOT-20261029-0900)\n"
    "- Thursday, October 29 at 2:00 PM (ID: SLOT-20261029-1400)"
)


def slots_call(slots):
    return {"tool": "get_available_slots", "result": json.dumps(slots)}


# --- unlisted_slots (live, moved from scheduler_specialist and widened) -------


def test_unlisted_slots_keeps_the_slot_id_behaviour():
    calls = [{"tool": "get_available_slots", "result": json.dumps({"slots": [{"slot_id": "SLOT-20261112-0900"}]})}]
    assert cc.unlisted_slots("Book SLOT-20261112-0900?", calls) == []
    assert cc.unlisted_slots("SLOT-20261112-0900 or SLOT-20261113-1400", calls) == ["SLOT-20261113-1400"]
    assert cc.unlisted_slots("SLOT-20261112-0900", []) == ["SLOT-20261112-0900"]


def test_unlisted_slots_catches_invented_ids_in_other_formats():
    # Spike run 4: with no tool, the model invented IDs that the SLOT- regex didn't match.
    reply = "Here are some times:\n- Monday 10:00 AM (ID: FRD-101)\n- Monday 2 PM, slot M0115-10AM"
    unlisted = cc.unlisted_slots(reply, [])
    assert "FRD-101" in unlisted and "M0115-10AM" in unlisted
    assert "10:00 AM" in unlisted and "2:00 PM" in unlisted


def test_unlisted_slots_passes_real_tool_slots_named_by_time():
    slots = get_available_slots(7)
    first = slots[0]
    reply = f"I can offer {first['datetime_str']} ({first['slot_id']}). Does that work?"
    assert cc.unlisted_slots(reply, [slots_call(slots)]) == []


def test_unlisted_slots_flags_a_time_no_slot_has():
    slots = get_available_slots(7)
    reply = f"How about {slots[0]['slot_id']} at 9:00 AM, or 5:30 PM the same day?"
    assert cc.unlisted_slots(reply, [slots_call(slots)]) == ["5:30 PM"]


def test_unlisted_slots_reads_the_whole_result_when_it_was_shortened():
    slots = get_available_slots(7)
    text = json.dumps(slots)
    last = slots[-1]
    call = {"tool": "get_available_slots", "result": text[:200], "full_result": text}
    assert cc.unlisted_slots(f"Try {last['slot_id']}.", [call]) == []


def test_unlisted_slots_ignores_ids_away_from_times():
    # An account ID mentioned on its own line isn't a slot.
    calls = [slots_call(get_available_slots(7))]
    assert cc.unlisted_slots("Your account TB-CHK-GOLD-2024 is eligible.", calls) == []


def test_scheduler_specialist_reexports_the_shared_check():
    import src.graph  # noqa: F401  (src.agents imports through the graph)
    from src.agents.scheduler_specialist import unlisted_slots

    assert unlisted_slots is cc.unlisted_slots


# --- offered_listed_slots ------------------------------------------------------


def test_offered_listed_slots():
    assert cc.offered_listed_slots("Thursday at 9:00 AM (SLOT-20261029-0900) works.", AVAILABLE).passed
    assert cc.offered_listed_slots("I'll have a banker reach out.", AVAILABLE).passed
    result = cc.offered_listed_slots("Thursday at 11 AM (ID: SLOT-20261029-1100)", AVAILABLE)
    assert not result.passed
    assert "SLOT-20261029-1100" in result.reason and "11:00 AM" in result.reason


# --- called_expected_tools -----------------------------------------------------


def test_called_expected_tools():
    calls = [{"tool": "get_available_slots"}]
    assert cc.called_expected_tools(calls, '["get_available_slots"]').passed
    assert cc.called_expected_tools([], "[]").passed  # control row
    missing = cc.called_expected_tools([], '["get_available_slots"]')
    assert missing.score == 0.0 and "didn't call get_available_slots" in missing.reason
    control = cc.called_expected_tools(calls, "[]")
    assert not control.passed and "needs no tool" in control.reason
    assert cc.called_expected_tools(calls * 2, ["get_available_slots"]).passed  # repeats are fine


# --- account_facts and mentions_account_type -----------------------------------


def test_account_facts_passes_accurate_replies():
    for reply in [
        "Hi Demo User! You have a Gold Checking account, and it's on our gold tier.",
        "You've been with ToggleBank since January 15, 2023, Demo User.",
        "Yes! Autopay is turned on, and your payments are set to go out automatically.",
        "Your account is active, and your billing is current.",
        "Account TB-CHK-GOLD-2024 tier gold.",
        "I'm not able to see your balance from here.",
    ]:
        result = cc.account_facts(reply, GOLD)
        assert result.passed, (reply, result.reason)


def test_account_facts_flags_contradictions():
    cases = {
        "Your Gold Checking balance is $4,218.37 as of this morning.": "$4,218.37",
        "Your account earns 2.75% APY.": "2.75%",
        "You've been with us since March 2018.": "2018-03",
        "Since 2018. A long time.": "2018",
        "You have a Platinum Checking account.": "Platinum",
        "Autopay is currently turned off.": "autopay is off",
        "Your account is active, but your billing shows as past due.": "past due",
        "Your account TB-SAV-PLAT-2019 is a savings account.": "TB-SAV-PLAT-2019",
    }
    for reply, expected in cases.items():
        result = cc.account_facts(reply, GOLD)
        assert not result.passed and expected in result.reason, (reply, result.reason)


def test_account_facts_reads_its_on_only_for_an_autopay_question():
    assert not cc.account_facts("No, it's off.", GOLD, query="Is autopay turned on?").passed
    assert cc.account_facts("No, it's off.", GOLD, query="Is the branch open?").passed


def test_account_facts_cant_check_a_claim_with_nothing_to_match():
    # The known limit: no token or state phrase.
    assert cc.account_facts("Everything's fine. Stop worrying.", GOLD).passed


def test_mentions_account_type_is_a_poor_check():
    # It fails an accurate reply that doesn't name the product...
    assert not cc.mentions_account_type("I'm not able to see your balance from here.", GOLD).passed
    # ...and passes an invented balance because it does.
    assert cc.mentions_account_type("Your Gold Checking balance is $4,218.37.", GOLD).passed
    assert not cc.account_facts("Your Gold Checking balance is $4,218.37.", GOLD).passed


def test_account_facts_on_the_labeled_set():
    """Guards the numbers measure_code_checks.py reports: few good replies flagged."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "evals" / "judge_labeled_dataset.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    good = [r for r in rows if r["kind"] == "good"]
    flagged_good = [r["id"] for r in good if not cc.account_facts(r["response"], r["account_context"], r["query"]).passed]
    assert len(flagged_good) / len(good) <= 0.10, flagged_good


# --- draft_facts ---------------------------------------------------------------


def test_draft_facts():
    draft = "Account ID TB-CHK-GOLD-2024 is a Gold Checking account, member since 2023-01-15. Autopay is enabled."
    kept = cc.draft_facts("Your Gold Checking account (TB-CHK-GOLD-2024) has been with us since January 15, 2023.", draft)
    assert kept.passed, kept.reason
    dropped = cc.draft_facts("You have a Gold Checking account with autopay on.", draft)
    assert dropped.score == 0.0 and "TB-CHK-GOLD-2024" in dropped.reason and "2023-01-15" in dropped.reason
    half = cc.draft_facts("Your account TB-CHK-GOLD-2024 is all set.", draft)
    assert half.score == 0.5
    assert cc.draft_facts("Anything else?", "Is ToggleBank FDIC insured?").passed


def test_draft_facts_slots_and_numbers():
    draft = "Lock the card. Available slots: Monday at 9:00 AM (ID: SLOT-A-0900). Fraud line available 24/7."
    assert cc.draft_facts("Please lock your card now. A banker is free Monday at 9 AM (SLOT-A-0900), "
                          "and the fraud line is open 24/7.", draft).passed
    result = cc.draft_facts("Please lock your card. Our fraud line is always open.", draft)
    assert not result.passed and "24/7" in result.reason and "9:00 AM" in result.reason


# --- leaked_headings -----------------------------------------------------------


def test_leaked_headings():
    leaked = "### Step 1: Acknowledge & Empathize\nI understand.\n\n### Step 2: Offer slots\n- 9 AM"
    result = cc.leaked_headings(leaked)
    assert not result.passed and "Step 1: Acknowledge" in result.reason
    assert not cc.leaked_headings("**Step 1:** Acknowledge\nThanks for reaching out.").passed
    assert cc.leaked_headings("Thanks for reaching out! Here are two times:\n- 9 AM\n- 2 PM").passed
    assert cc.leaked_headings("We have #1 rated service.").passed


def test_every_check_returns_a_float_score():
    # The AI SDK 1.0 rejects a bool score.
    results = [
        cc.check_unlisted_slots("", []), cc.offered_listed_slots("", ""), cc.called_expected_tools([], []),
        cc.account_facts("", GOLD), cc.mentions_account_type("", GOLD), cc.draft_facts("", "x 12"),
        cc.leaked_headings(""),
    ]
    assert all(type(r.score) is float for r in results)
