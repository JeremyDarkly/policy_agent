#!/usr/bin/env python3
"""Point each AI Config's fallthrough at its `Default` variation in the target
environment.

Terraform can't do this. LaunchDarkly rejects writes to AI Config-backed flags
through the feature flag API:

    401 Unauthorized: {"code":"unauthorized","message":"AI flags may not be modified directly."}

and the LaunchDarkly Terraform provider (as of v3.1.5) exposes no AI Config
targeting resource. So `terraform apply` creates each config *enabled*, but with
its fallthrough still on variation index 0 -- LaunchDarkly's auto-added
"disabled" variation. The agents resolve to nothing until the fallthrough is
moved to index 1 ("Default"), which is what this script does via the AI Configs
targeting endpoint.

`ai-judge-jev` is the exception: its default rule serves "disabled", so the
LLM judges run alone until a demo targets someone with jev-alongside or
jev-only. The script turns its targeting on so those targets work.

Run it after `terraform apply`. It is idempotent.

With --azure-demo it also adds a rule to branch_agent that serves its
`azure-foundry` variation (Azure AI Foundry) to the UI's internal demo user,
whose user key ends with `-internal-dev`. Everyone else stays on the
fallthrough. The variation exists only when Terraform was given an Azure
deployment (AZURE_FOUNDRY_DEPLOYMENT).

With --tools-demo it adds a rule to scheduler_agent and branch_agent that serves
their `tool-calling` variation (issue #72) to users whose `role` is `employee`,
which is the UI's employee login, so that login shows the tool path and the
customer login the old one. The rule goes after existing rules, so on
branch_agent the Azure demo rule (which also matches the employee login) still
wins; --tools-demo-first puts it ahead of them instead.

With --gemini-demo it adds a rule to ai-judge-accuracy and ai-judge-coherence
that serves their `gemini-4-carbon` variation (issue #79) to contexts whose
`judge_model` is `gemini-4-carbon`. Only evals/run_judge_pilot.py
--judge-model gemini-4-carbon sets that attribute, so real chats keep the
default judges.

--rules-only adds the demo rules without moving any fallthrough, for a config
whose default rule was deliberately moved off `Default` (for example by a
guarded rollout).

Usage:
    python scripts/set_ai_config_targeting.py            # reads .env
    python scripts/set_ai_config_targeting.py --dry-run
    python scripts/set_ai_config_targeting.py --env-file .env.staging-org
    python scripts/set_ai_config_targeting.py --azure-demo
    python scripts/set_ai_config_targeting.py --config branch_agent --azure-demo   # touch branch_agent only
    python scripts/set_ai_config_targeting.py --config scheduler_agent --config branch_agent --tools-demo --rules-only
    python scripts/set_ai_config_targeting.py --config ai-judge-accuracy --config ai-judge-coherence --gemini-demo --rules-only

LAUNCHDARKLY_API_HOST selects a non-production instance (default
https://app.launchdarkly.com).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_API_HOST = "https://app.launchdarkly.com"

# The AI Configs provisioned by infrastructure/launchdarkly/ai_configs_resources.tf.
CONFIG_KEYS = [
    "triage_agent",
    "account_agent",
    "branch_agent",
    "scheduler_agent",
    "brand_agent",
    "ai-judge-accuracy",
    "ai-judge-coherence",
    "ci-judge-accuracy",
    "ci-judge-coherence",
    "ai-judge-jev",
]

DEFAULT_VARIATION_NAME = "Default"
# Configs whose default rule serves another variation (by name).
FALLTHROUGH_VARIATION = {"ai-judge-jev": "disabled"}

# --azure-demo: branch_agent serves azure-foundry to the internal demo user (app/ui/src/lib/demoUser.ts).
AZURE_DEMO_CONFIG = "branch_agent"
AZURE_DEMO_VARIATION_KEY = "azure-foundry"
AZURE_DEMO_RULE_DESCRIPTION = "Azure AI Foundry demo: internal users"
AZURE_DEMO_CLAUSE = {
    "contextKind": "user",
    "attribute": "key",
    "op": "endsWith",
    "values": ["-internal-dev"],
    "negate": False,
}


# --tools-demo: scheduler_agent and branch_agent serve tool-calling to the employee login.
TOOLS_DEMO_CONFIGS = ["scheduler_agent", "branch_agent"]
TOOLS_DEMO_VARIATION_KEY = "tool-calling"
TOOLS_DEMO_RULE_DESCRIPTION = "Tool-calling demo: employees"
TOOLS_DEMO_CLAUSE = {
    "contextKind": "user",
    "attribute": "role",
    "op": "in",
    "values": ["employee"],
    "negate": False,
}


def load_env(env_path: Path) -> dict[str, str]:
    """Read an env file without needing python-dotenv. Values may contain spaces."""
    env: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    # real environment wins, so CI can override
    env.update({k: v for k, v in os.environ.items() if k in env or k.startswith("LAUNCHDARKLY_")})
    return env


def request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", token)
    req.add_header("LD-API-Version", "beta")
    if body is not None:
        # semantic patch, not RFC 6902
        req.add_header("Content-Type", "application/json; domain-model=launchdarkly.semanticpatch")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:300]
        raise SystemExit(f"  {method} {url}\n  HTTP {err.code}: {detail}") from err


# --gemini-demo: the judges serve gemini-4-carbon to contexts the judge pilot marks.
GEMINI_DEMO_CONFIGS = ["ai-judge-accuracy", "ai-judge-coherence"]
GEMINI_DEMO_VARIATION_KEY = "gemini-4-carbon"
GEMINI_DEMO_RULE_DESCRIPTION = "Gemini 4 Carbon judge pilot: judge_model = gemini-4-carbon"
GEMINI_DEMO_CLAUSE = {
    "contextKind": "user",
    "attribute": "judge_model",
    "op": "in",
    "values": ["gemini-4-carbon"],
    "negate": False,
}


def variation_key(variation: dict) -> str | None:
    """The AI Config variation key, which the targeting API carries in the flag value's _ldMeta."""
    value = variation.get("value")
    return value.get("_ldMeta", {}).get("variationKey") if isinstance(value, dict) else None


def is_azure_demo_clause(clause: dict) -> bool:
    return all(clause.get(field) == AZURE_DEMO_CLAUSE[field] for field in ("attribute", "op", "values", "negate"))


def add_azure_demo_rule(api_base: str, project: str, environment: str, token: str, dry_run: bool) -> bool:
    """Add the branch_agent rule that serves azure-foundry to internal demo users. Returns False on failure."""
    key = AZURE_DEMO_CONFIG
    url = f"{api_base}/projects/{project}/ai-configs/{key}/targeting"
    targeting = request("GET", url, token)

    variation = next((v for v in targeting.get("variations", []) if variation_key(v) == AZURE_DEMO_VARIATION_KEY), None)
    if variation is None:
        print(f"  {key:<20} SKIP  no '{AZURE_DEMO_VARIATION_KEY}' variation: set AZURE_FOUNDRY_DEPLOYMENT and run terraform apply")
        return False

    env_state = targeting.get("environments", {}).get(environment)
    if env_state is None:
        print(f"  {key:<20} SKIP  environment '{environment}' not found on this config")
        return False

    # The AI Config targeting GET leaves out each rule's description, variation and contextKind, so the rule
    # is recognised by its clause.
    for rule in env_state.get("rules", []):
        if any(is_azure_demo_clause(clause) for clause in rule.get("clauses", [])):
            print(f"  {key:<20} ok    Azure demo rule already present")
            return True

    if dry_run:
        print(f"  {key:<20} would add rule: user key ends with -internal-dev -> {AZURE_DEMO_VARIATION_KEY}")
        return True

    instruction = {
        "kind": "addRule",
        "description": AZURE_DEMO_RULE_DESCRIPTION,
        "clauses": [AZURE_DEMO_CLAUSE],
        "variationId": variation["_id"],
    }
    request("PATCH", url, token, {"environmentKey": environment, "instructions": [instruction]})
    print(f"  {key:<20} added rule: user key ends with -internal-dev -> {AZURE_DEMO_VARIATION_KEY}")
    return True


def is_tools_demo_clause(clause: dict) -> bool:
    return all(clause.get(field) == TOOLS_DEMO_CLAUSE[field] for field in ("attribute", "op", "values", "negate"))


def add_tools_demo_rule(api_base: str, project: str, environment: str, token: str, key: str, first: bool,
                        dry_run: bool) -> bool:
    """Add the rule that serves tool-calling to employees on one config. Returns False on failure."""
    url = f"{api_base}/projects/{project}/ai-configs/{key}/targeting"
    targeting = request("GET", url, token)

    variation = next((v for v in targeting.get("variations", []) if variation_key(v) == TOOLS_DEMO_VARIATION_KEY), None)
    if variation is None:
        print(f"  {key:<20} SKIP  no '{TOOLS_DEMO_VARIATION_KEY}' variation: run terraform apply")
        return False

    env_state = targeting.get("environments", {}).get(environment)
    if env_state is None:
        print(f"  {key:<20} SKIP  environment '{environment}' not found on this config")
        return False

    rules = env_state.get("rules", [])
    for rule in rules:
        if any(is_tools_demo_clause(clause) for clause in rule.get("clauses", [])):
            print(f"  {key:<20} ok    tools demo rule already present")
            return True

    where = "first" if first and rules else "last"
    if dry_run:
        print(f"  {key:<20} would add rule ({where}): role is employee -> {TOOLS_DEMO_VARIATION_KEY}")
        return True

    instruction = {
        "kind": "addRule",
        "description": TOOLS_DEMO_RULE_DESCRIPTION,
        "clauses": [TOOLS_DEMO_CLAUSE],
        "variationId": variation["_id"],
    }
    if first and rules:
        instruction["beforeRuleId"] = rules[0]["_id"]
    request("PATCH", url, token, {"environmentKey": environment, "instructions": [instruction]})
    print(f"  {key:<20} added rule ({where}): role is employee -> {TOOLS_DEMO_VARIATION_KEY}")
    return True


def is_gemini_demo_clause(clause: dict) -> bool:
    return all(clause.get(field) == GEMINI_DEMO_CLAUSE[field] for field in ("attribute", "op", "values", "negate"))


def add_gemini_demo_rule(api_base: str, project: str, environment: str, token: str, key: str,
                         dry_run: bool) -> bool:
    """Add the rule that serves gemini-4-carbon to judge_model = gemini-4-carbon on one judge. False on failure."""
    url = f"{api_base}/projects/{project}/ai-configs/{key}/targeting"
    targeting = request("GET", url, token)

    variation = next((v for v in targeting.get("variations", []) if variation_key(v) == GEMINI_DEMO_VARIATION_KEY), None)
    if variation is None:
        print(f"  {key:<20} SKIP  no '{GEMINI_DEMO_VARIATION_KEY}' variation: run terraform apply")
        return False

    env_state = targeting.get("environments", {}).get(environment)
    if env_state is None:
        print(f"  {key:<20} SKIP  environment '{environment}' not found on this config")
        return False

    for rule in env_state.get("rules", []):
        if any(is_gemini_demo_clause(clause) for clause in rule.get("clauses", [])):
            print(f"  {key:<20} ok    Gemini demo rule already present")
            return True

    if dry_run:
        print(f"  {key:<20} would add rule: judge_model is gemini-4-carbon -> {GEMINI_DEMO_VARIATION_KEY}")
        return True

    instruction = {
        "kind": "addRule",
        "description": GEMINI_DEMO_RULE_DESCRIPTION,
        "clauses": [GEMINI_DEMO_CLAUSE],
        "variationId": variation["_id"],
    }
    request("PATCH", url, token, {"environmentKey": environment, "instructions": [instruction]})
    print(f"  {key:<20} added rule: judge_model is gemini-4-carbon -> {GEMINI_DEMO_VARIATION_KEY}")
    return True


def request_status(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, str]:
    """A plain JSON request that returns (HTTP status, response body) instead of exiting on an error."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change")
    parser.add_argument("--env-file", default=".env", help="env file, relative to the repo root")
    parser.add_argument(
        "--config",
        action="append",
        choices=CONFIG_KEYS,
        help="only these AI Configs (repeatable); default all. Leaves other configs' targeting alone",
    )
    parser.add_argument(
        "--azure-demo",
        action="store_true",
        help="also serve branch_agent's azure-foundry variation to the internal demo user",
    )
    parser.add_argument(
        "--tools-demo",
        action="store_true",
        help="also serve scheduler_agent's and branch_agent's tool-calling variation to employees",
    )
    parser.add_argument(
        "--tools-demo-first",
        action="store_true",
        help="with --tools-demo, put the rule ahead of existing rules (on branch_agent, the Azure demo rule)",
    )
    parser.add_argument(
        "--gemini-demo",
        action="store_true",
        help="also serve the judges' gemini-4-carbon variation to contexts with judge_model = gemini-4-carbon",
    )
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="only add the demo rules; leave every fallthrough as it is",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env = load_env(repo_root / args.env_file)

    token = env.get("LAUNCHDARKLY_ACCESS_TOKEN")
    project = env.get("LAUNCHDARKLY_PROJECT_KEY")
    environment = env.get("LAUNCHDARKLY_ENVIRONMENT", "test")
    api_base = (env.get("LAUNCHDARKLY_API_HOST") or DEFAULT_API_HOST).rstrip("/") + "/api/v2"

    missing = [
        name
        for name, value in (
            ("LAUNCHDARKLY_ACCESS_TOKEN", token),
            ("LAUNCHDARKLY_PROJECT_KEY", project),
        )
        if not value
    ]
    if missing:
        print(f"error: {', '.join(missing)} must be set in {args.env_file}", file=sys.stderr)
        return 1

    print(f"api={api_base} project={project} environment={environment}\n")

    config_keys = args.config or CONFIG_KEYS
    changed = failed = 0
    for key in [] if args.rules_only else config_keys:
        url = f"{api_base}/projects/{project}/ai-configs/{key}/targeting"
        targeting = request("GET", url, token)

        variations = targeting.get("variations", [])
        wanted = FALLTHROUGH_VARIATION.get(key, DEFAULT_VARIATION_NAME)
        target_idx = next(
            (i for i, v in enumerate(variations) if v.get("name") == wanted),
            None,
        )
        if target_idx is None:
            names = [v.get("name") for v in variations]
            print(f"  {key:<20} SKIP  no '{wanted}' variation (found {names})")
            failed += 1
            continue

        env_state = targeting.get("environments", {}).get(environment)
        if env_state is None:
            print(f"  {key:<20} SKIP  environment '{environment}' not found on this config")
            failed += 1
            continue

        current_idx = env_state.get("fallthrough", {}).get("variation")
        if current_idx == target_idx and env_state.get("enabled"):
            print(f"  {key:<20} ok    already serving '{wanted}'")
            continue

        if args.dry_run:
            print(f"  {key:<20} would set fallthrough {current_idx} -> {target_idx}")
            changed += 1
            continue

        instructions: list[dict] = [
            {
                "kind": "updateFallthroughVariationOrRollout",
                "variationId": variations[target_idx]["_id"],
            }
        ]
        if not env_state.get("enabled"):
            instructions.insert(0, {"kind": "turnFlagOn"})

        request("PATCH", url, token, {"environmentKey": environment, "instructions": instructions})
        print(f"  {key:<20} set   fallthrough {current_idx} -> {target_idx}")
        changed += 1

    if args.azure_demo and AZURE_DEMO_CONFIG in config_keys and not add_azure_demo_rule(api_base, project, environment, token, args.dry_run):
        failed += 1

    if args.tools_demo:
        for key in TOOLS_DEMO_CONFIGS:
            if key in config_keys and not add_tools_demo_rule(
                api_base, project, environment, token, key, args.tools_demo_first, args.dry_run
            ):
                failed += 1

    if args.gemini_demo:
        for key in GEMINI_DEMO_CONFIGS:
            if key in config_keys and not add_gemini_demo_rule(api_base, project, environment, token, key, args.dry_run):
                failed += 1

    verb = "would change" if args.dry_run else "changed"
    print(f"\n{verb} {changed}, failed {failed}, total {len(config_keys)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
