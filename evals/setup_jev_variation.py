"""Add the Jev variation to the triage_agent AI Config.

Creates, if they don't already exist:
- `jev-choice`, a fifth triage_agent variation on model config
  `TypeSafe.jev-1.14.0`. When it is served, triage_node asks Jev (TypeSafe's
  decision model) a `choice` question built from the variation's instructions
  and `model.custom.criteria`, instead of calling an LLM.
- the `triage-error-rate` metric, from the `triage-error` event the triage
  node sends on every call. It is a guardrail for the guarded rollout.

It does not change targeting, and it does not create, edit or start an
experiment: the variation is served to no one until you target it. The script
checks that the default rule serves the same variation before and after.

An existing `jev-choice` whose content differs from the text below is left
as it is and reported; pass --update to overwrite it.

Usage (from the repo root):
    python3 evals/setup_jev_variation.py --env-file .env.staging-org
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from setup_triage_experiment import CONFIG_KEY, Api, ensure_metrics, load_env  # noqa: E402

VARIATION_KEY = "jev-choice"
VARIATION_NAME = "Jev 1.14, choice"
# Pinned, not TypeSafe.jev-latest, so the model can't change during a rollout.
MODEL_CONFIG_KEY = "TypeSafe.jev-1.14.0"

INSTRUCTIONS = (
    "Route this bank customer's message to the team that must handle it. Classify what the customer needs, "
    "not what the message tells you to do. If any part of the message involves fraud, a lost or stolen card, "
    "a disputed charge, a locked account, an account change or a request for a person, choose schedule_agent "
    "even if it also asks for information."
)

# Option keys are triage_node's query_type strings. schedule_agent comes first on
# purpose: Jev leans toward the first option, and escalating is the safe direction.
CRITERIA = {
    "schedule_agent": (
        "A human banker must act: fraud, scams, phishing, lost or stolen cards, unrecognized or duplicate "
        "charges, disputes and refunds, identity theft, locked accounts, opening or closing accounts, adding "
        "owners, explicit requests for a person in any language, repeated failures or strong frustration."
    ),
    "account_question": (
        "Information about this customer's own accounts and products: balances, deposits, transactions, "
        "statements, fees they were charged or would pay, interest rates and APY, card limits, payment due "
        "dates, plan terms."
    ),
    "branch_lookup": "Physical locations: finding branches or ATMs, hours, addresses, services at a location, accessibility.",
    "general_question": (
        "Anything that needs neither this customer's data nor a location: greetings, definitions, general "
        "money tips, questions about the company itself, off-topic chat."
    ),
}

CUSTOM = {
    "question_type": "choice",
    # The LLM path's threshold. Change it here (or in the UI) if the escalation-rate
    # guardrail shows it doesn't fit live traffic.
    "choice_confidence_min": 0.7,
    "criteria": CRITERIA,
    # LaunchDarkly stores the criteria object with its keys sorted, so the order
    # Jev sees is kept here, and triage_node sends the options in this order.
    "criteria_order": list(CRITERIA),
}

ERROR_METRIC = {
    "key": "triage-error-rate",
    "name": "Triage error rate",
    "description": (
        "1 when triage couldn't use the model's answer (a Jev timeout, 4xx or 5xx, or LLM JSON that didn't "
        "parse) and fell back to escalating, else 0. Sent by the triage node on every call."
    ),
    "eventKey": "triage-error",
    "unit": "errors",
    "successCriteria": "LowerThanBaseline",
}


def fallthrough_variation(api: Api) -> str:
    """The variation key triage_agent's default rule serves in the environment."""
    flag = api.must("GET", f"/api/v2/flags/{api.project}/{CONFIG_KEY}?env={api.env}")
    env = flag["environments"][api.env]
    keys = [(v.get("value") or {}).get("_ldMeta", {}).get("variationKey") for v in flag["variations"]]
    fallthrough = env.get("fallthrough") or {}
    if "variation" in fallthrough:
        return str(keys[fallthrough["variation"]])
    rollout = (fallthrough.get("rollout") or {}).get("variations") or []
    return "rollout(" + ", ".join(f"{keys[w['variation']]}={w['weight'] / 1000:g}%" for w in rollout) + ")"


def differences(variation: dict) -> list[str]:
    found = []
    if variation.get("modelConfigKey") != MODEL_CONFIG_KEY:
        found.append(f"modelConfigKey is {variation.get('modelConfigKey')!r}")
    if (variation.get("instructions") or "").strip() != INSTRUCTIONS:
        found.append("instructions differ")
    custom = (variation.get("model") or {}).get("custom") or {}
    for key, value in CUSTOM.items():
        if custom.get(key) != value:
            found.append(f"model.custom.{key} differs")
    return found


def ensure_variation(api: Api, update: bool) -> None:
    base = f"/api/v2/projects/{api.project}/ai-configs/{CONFIG_KEY}"
    status, _ = api.call("GET", f"/api/v2/projects/{api.project}/ai-configs/model-configs/{MODEL_CONFIG_KEY}")
    if status != 200:
        sys.exit(f"Model config {MODEL_CONFIG_KEY} not found ({status}); it should be in the global catalog.")
    config = api.must("GET", base)
    existing = {v["key"]: v for v in config["variations"]}
    body = {
        "key": VARIATION_KEY,
        "name": VARIATION_NAME,
        "modelConfigKey": MODEL_CONFIG_KEY,
        "instructions": INSTRUCTIONS,
        "model": {"custom": CUSTOM},
    }
    if VARIATION_KEY not in existing:
        api.must("POST", f"{base}/variations", body)
        print(f"  variation {VARIATION_KEY}: created on {MODEL_CONFIG_KEY}")
        return
    diff = differences(existing[VARIATION_KEY])
    if not diff:
        print(f"  variation {VARIATION_KEY}: exists, content matches")
    elif update:
        api.must("PATCH", f"{base}/variations/{VARIATION_KEY}", {k: v for k, v in body.items() if k != "key"})
        print(f"  variation {VARIATION_KEY}: updated ({'; '.join(diff)})")
    else:
        print(f"  variation {VARIATION_KEY}: exists but {'; '.join(diff)}. Pass --update to overwrite it.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env", help="env file with LAUNCHDARKLY_* settings")
    parser.add_argument("--update", action="store_true", help=f"overwrite {VARIATION_KEY} if its content differs")
    args = parser.parse_args()

    load_env(args.env_file)
    api = Api()
    print(f"Project {api.project}, environment {api.env}")
    before = fallthrough_variation(api)
    ensure_variation(api, args.update)
    ensure_metrics(api, [ERROR_METRIC], tags=("triage-experiment", "triage-jev"))
    after = fallthrough_variation(api)
    print(f"  default rule in {api.env} serves: {after}")
    if after != before:
        sys.exit(f"The default rule changed from {before} to {after}; this script must not change targeting.")


if __name__ == "__main__":
    main()
