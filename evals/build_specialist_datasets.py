"""Build the CI eval datasets for the specialist and brand voice agents.

Each row reproduces what the backend sends the agent for one question, so an
offline eval of the AI Config sees what production sees:

- input: the user message the backend builds in
  LaunchDarklyClient.build_langchain_messages:
  "User query: <question>\n\nUser context:\n<the agent's context as JSON>"
- variables: the agent's prompt variables (name, location, account_info, ...),
  plus what ci_targets.toml points the judges at.

The profile comes from the backend's own create_user_profile, with the server's
ToggleBank defaults. The account and branch context blocks and the scheduler
slots copy the agents' code (cited below). The backend also appends the account
and branch block to the end of the system prompt; offline, the prompt gets it once,
through its {{account_info}} / {{branch_info}} variable.

The scheduler's slots are dated from the day the files are built.

Writes evals/ci_datasets/<config>_dataset.jsonl and <config>_dataset_quick.jsonl.
Upload each one in the LaunchDarkly UI (Library > Datasets > New dataset) with
the file name (without .jsonl) as its key.

Usage:
    cd evals && uv run python build_specialist_datasets.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
BACKEND_SRC = EVALS_DIR.parent / "app" / "backend" / "src"
OUT_DIR = EVALS_DIR / "ci_datasets"
DEMO_NAME = "Demo User"


def load_backend_module(relative: str) -> Any:
    """Import one standard-library-only backend module by path."""
    path = BACKEND_SRC / relative
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


user_profile = load_backend_module("utils/user_profile.py")
calendar = load_backend_module("tools/calendar.py")


def profile() -> dict[str, Any]:
    """The server's default ToggleBank user (server.py, chat endpoint defaults)."""
    p = user_profile.create_user_profile(
        name=DEMO_NAME,
        location="San Francisco, CA",
        policy_id="TB-CHK-GOLD-2024",
        coverage_type="Gold Checking",
        domain="togglebank",
    )
    # Fixed instead of the time of the build, so rebuilt rows only differ when
    # something real changed.
    p["session_id"] = "session-ci-evals"
    p["last_login"] = "2026-01-01T09:00:00"
    return p


def account_info(ctx: dict[str, Any]) -> str:
    """agents/account_specialist.py: _format_account_context."""
    fields = [
        ("Account ID", "account_id"),
        ("Account type", "account_type"),
        ("Account tier", "customer_tier"),
        ("Account status", "account_status"),
        ("Member since", "member_since"),
        ("Currency", "currency"),
        ("Billing status", "billing_status"),
        ("Payment method", "payment_method"),
        ("Autopay enabled", "autopay_enabled"),
        ("Risk profile", "risk_profile"),
    ]
    lines = ["\n\n=== CUSTOMER ACCOUNT PROFILE (source of truth) ==="]
    for label, key in fields:
        val = ctx.get(key)
        if val is not None and val != "":
            lines.append(f"- {label}: {val}")
    return "\n".join(lines) + "\n"


def branch_info(ctx: dict[str, Any]) -> str:
    """agents/branch_specialist.py: the location context block."""
    text = "\n\n=== CUSTOMER LOCATION CONTEXT (source of truth) ===\n"
    text += f"- Primary location: {ctx.get('location') or 'Not specified'}\n"
    text += f"- Account type: {ctx.get('account_type', 'Unknown')}\n"
    if ctx.get("account_id"):
        text += f"- Account ID: {ctx['account_id']}\n"
    return text


def available_slots() -> str:
    """agents/scheduler_specialist.py: the first 10 slots, as the agent formats them."""
    slots = calendar.get_available_slots(days_ahead=7)
    return "\n".join(f"- {s['datetime_str']} (ID: {s['slot_id']})" for s in slots[:10])


def user_message(query: str, context_vars: dict[str, Any]) -> str:
    """utils/launchdarkly_config.py: build_langchain_messages, agent-mode user message."""
    context = {k: v for k, v in context_vars.items() if k != "query"}
    return f"User query: {query}\n\nUser context:\n{json.dumps(context, indent=2, default=str)}"


def as_text(value: Any) -> str:
    """Scalars as the backend's template variables render them (str()), others as JSON."""
    return str(value) if isinstance(value, (str, int, float, bool)) else json.dumps(value, default=str)


def row(query: str, context_vars: dict[str, Any], expected: str, metadata: dict[str, Any]) -> dict[str, Any]:
    # Prompt templates and judge templates read these; LaunchDarkly stores
    # dataset variables as strings.
    variables = {k: as_text(v) for k, v in context_vars.items() if k != "query"}
    return {
        "input": user_message(query, context_vars),
        "expected_output": expected,
        "variables": variables,
        "metadata": {"query": query, **metadata},
    }


# --- Questions -----------------------------------------------------------------
# (question, expected behavior, in quick dataset)

ACCOUNT_QUESTIONS = [
    ("What type of account do I have, and what tier is it?",
     "Says Gold Checking, gold tier, from the profile.", True),
    ("What's the balance on my checking account?",
     "Says the balance isn't in the profile and offers a way to see it, without inventing a figure.", True),
    ("What's the monthly maintenance fee on my account?",
     "Says the fee schedule isn't in the profile, without inventing a fee, and offers to connect a banker.", True),
    ("Is autopay turned on for my account?",
     "Says autopay is enabled and the payment method is auto pay.", False),
    ("How long have I been a customer?",
     "Says member since 2023-01-15.", False),
    ("What interest rate does my account earn?",
     "Says the rate isn't in the profile, without inventing one.", False),
    ("Is my account in good standing?",
     "Says the account is active and billing is current.", False),
    ("Can you upgrade me to Platinum Checking?",
     "Explains an upgrade needs a banker and offers to book one, without claiming to change the account.", False),
]

BRANCH_QUESTIONS = [
    ("Where's the nearest ToggleBank branch?",
     "Uses the San Francisco location, doesn't invent a specific address or hours, and points to the app, website or a banker.", True),
    ("Is there an ATM near me that takes cash deposits?",
     "Doesn't invent ATM locations or IDs; explains how to find deposit-taking ATMs nearby.", True),
    ("I'm visiting Boston next week. Are there any branches there?",
     "Treats Boston as the location to search, doesn't invent addresses, and offers a way to look them up.", True),
    ("What time do branches close on Saturday?",
     "Gives only general guidance on weekend hours, without inventing a specific branch's hours.", False),
    ("Which branch can notarize a document for me?",
     "Says notary service varies by branch, doesn't invent a branch, and suggests checking or booking ahead.", False),
    ("Is the branch near Union Square wheelchair accessible?",
     "Doesn't claim to know that branch's accessibility; offers to confirm or connect a banker.", False),
    ("Can I deposit a check without going to a branch?",
     "Mentions mobile deposit in the app as an option, without inventing limits.", False),
    ("Where's the closest drive-up ATM?",
     "Doesn't invent a location; explains how to find drive-up ATMs near San Francisco.", False),
]

SCHEDULER_QUESTIONS = [
    ("I'd like to speak with a banker about a mortgage.",
     "Acknowledges the request, offers times only from the available slots, and asks for contact preferences.", True),
    ("I want to report a fraudulent charge on my card.",
     "Treats it as urgent, tells the customer to lock or report the card right away, and offers the earliest slot.", True),
    ("Quiero hablar con un asesor sobre mi préstamo.",
     "Offers a banker appointment from the available slots; responding in Spanish or English is fine.", True),
    ("Can I book something for tomorrow morning?",
     "Offers only tomorrow-morning times that appear in the available slots, or the nearest ones if none.", False),
    ("Please add my wife as a joint owner on my checking account.",
     "Explains this needs a banker and identity verification, and offers appointment times from the slots.", False),
    ("I'm locked out of online banking after too many password attempts.",
     "Empathizes, explains the next steps, and offers a banker appointment without asking for the password.", False),
    ("I'm furious. My transfer has failed three times and nobody has helped.",
     "Acknowledges the frustration, prioritizes the issue, and offers the earliest appointment.", False),
    ("Do you have anything later in the week, in the afternoon?",
     "Offers only afternoon times that appear in the available slots.", False),
]

# Brand voice rewrites a specialist's draft: (original question, query type,
# specialist draft, expected behavior, in quick dataset).
BRAND_DRAFTS = [
    ("What type of account do I have?", "account_question",
     "Account ID TB-CHK-GOLD-2024 is a Gold Checking account, gold tier, status active, member since 2023-01-15. "
     "Autopay is enabled.",
     "Keeps account ID, Gold Checking, gold tier, active status and member-since date; friendly, uses the name.", True),
    ("What's my balance?", "account_question",
     "The account profile does not include a real-time balance. The customer can view it in the ToggleBank app "
     "or website, or speak with a banker.",
     "Keeps that the balance isn't available here and the ways to see it; doesn't add a figure.", True),
    ("Where's the nearest branch?", "branch_lookup",
     "Customer location: San Francisco, CA. No branch directory available. Recommend the branch locator in the "
     "ToggleBank app or website, or connecting with a banker.",
     "Keeps San Francisco and the app/website locator; doesn't invent an address.", True),
    ("I want to talk to a banker about a mortgage.", "schedule_agent",
     "Available slots: Monday at 9:00 AM (ID: SLOT-A-0900), Monday at 10:00 AM (ID: SLOT-A-1000), "
     "Tuesday at 2:00 PM (ID: SLOT-B-1400). Ask for preferred contact method.",
     "Keeps all three slots and IDs exactly, and asks for a contact method.", False),
    ("Is ToggleBank FDIC insured?", "general_question",
     "Is ToggleBank FDIC insured?",
     "Answers the general question helpfully without inventing account-specific facts.", False),
    ("I lost my debit card.", "schedule_agent",
     "Urgent: lock the card in the app immediately. Available slots: Monday at 9:00 AM (ID: SLOT-A-0900). "
     "Fraud line available 24/7.",
     "Keeps the urgency, the instruction to lock the card, the slot and ID; empathetic.", False),
    ("Is autopay on?", "account_question",
     "Autopay enabled: True. Payment method: auto_pay. Billing status: current.",
     "Keeps autopay on, payment method and billing status; readable wording.", False),
    ("Can I open a high-yield savings account?", "schedule_agent",
     "Opening a new product requires a banker and identity verification. Available slots: Tuesday at 2:00 PM "
     "(ID: SLOT-B-1400), Wednesday at 11:00 AM (ID: SLOT-C-1100).",
     "Keeps the need for a banker and verification and both slots with IDs.", False),
]


def build() -> dict[str, list[dict[str, Any]]]:
    base = profile()
    account_vars = {
        **base,
        "account_id": base.get("account_id") or "Not provided",
        "account_type": base.get("account_type", "Unknown"),
        "account_info": account_info(base),
    }
    branch_vars = {
        **base,
        "account_id": base.get("account_id") or "Not provided",
        "account_type": base.get("account_type", "Unknown"),
        "location": base.get("location") or "Not specified",
        "branch_info": branch_info(base),
    }
    scheduler_vars = {
        **base,
        "account_id": base.get("account_id", "Not provided"),
        "available_slots": available_slots(),
    }

    datasets: dict[str, list[dict[str, Any]]] = {}

    def add(config: str, rows: list[tuple[dict[str, Any], bool]]) -> None:
        datasets[f"{config}_dataset"] = [r for r, _ in rows]
        datasets[f"{config}_dataset_quick"] = [r for r, quick in rows if quick]

    add("account_agent", [
        (row(q, {**account_vars, "query": q}, e, {"difficulty": "unanswerable" if "isn't" in e else "answerable"}), quick)
        for q, e, quick in ACCOUNT_QUESTIONS
    ])
    add("branch_agent", [(row(q, {**branch_vars, "query": q}, e, {}), quick) for q, e, quick in BRANCH_QUESTIONS])
    add("scheduler_agent", [
        (row(q, {**scheduler_vars, "query": q}, e, {}), quick) for q, e, quick in SCHEDULER_QUESTIONS
    ])
    # Brand voice gets no "query" (its user message starts "User query: "), and
    # the draft only inside the context JSON, as agents/brand_voice_agent.py sends it.
    add("brand_agent", [
        (row("", {
            **base,
            "customer_name": base.get("name", "there"),
            "original_query": q,
            "query_type": f"QueryType.{t.upper()}",
            "specialist_response": draft,
        }, e, {"original_query": q, "query_type": t}), quick)
        for q, t, draft, e, quick in BRAND_DRAFTS
    ])
    return datasets


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for key, rows in build().items():
        path = OUT_DIR / f"{key}.jsonl"
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{path.relative_to(EVALS_DIR)}: {len(rows)} rows")


if __name__ == "__main__":
    main()
