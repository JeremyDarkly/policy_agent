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

Run it after `terraform apply`. It is idempotent.

Usage:
    python scripts/set_ai_config_targeting.py            # reads .env
    python scripts/set_ai_config_targeting.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://app.launchdarkly.com/api/v2"

# The AI Configs provisioned by infrastructure/launchdarkly/ai_configs_resources.tf.
CONFIG_KEYS = [
    "triage_agent",
    "account_agent",
    "branch_agent",
    "scheduler_agent",
    "brand_agent",
    "ai-judge-accuracy",
    "ai-judge-coherence",
]

DEFAULT_VARIATION_NAME = "Default"


def load_env(repo_root: Path) -> dict[str, str]:
    """Read .env without needing python-dotenv. Values may contain spaces."""
    env: dict[str, str] = {}
    env_path = repo_root / ".env"
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env = load_env(repo_root)

    token = env.get("LAUNCHDARKLY_ACCESS_TOKEN")
    project = env.get("LAUNCHDARKLY_PROJECT_KEY")
    environment = env.get("LAUNCHDARKLY_ENVIRONMENT", "test")

    missing = [
        name
        for name, value in (
            ("LAUNCHDARKLY_ACCESS_TOKEN", token),
            ("LAUNCHDARKLY_PROJECT_KEY", project),
        )
        if not value
    ]
    if missing:
        print(f"error: {', '.join(missing)} must be set in .env", file=sys.stderr)
        return 1

    print(f"project={project} environment={environment}\n")

    changed = failed = 0
    for key in CONFIG_KEYS:
        url = f"{API_BASE}/projects/{project}/ai-configs/{key}/targeting"
        targeting = request("GET", url, token)

        variations = targeting.get("variations", [])
        target_idx = next(
            (i for i, v in enumerate(variations) if v.get("name") == DEFAULT_VARIATION_NAME),
            None,
        )
        if target_idx is None:
            names = [v.get("name") for v in variations]
            print(f"  {key:<20} SKIP  no '{DEFAULT_VARIATION_NAME}' variation (found {names})")
            failed += 1
            continue

        env_state = targeting.get("environments", {}).get(environment)
        if env_state is None:
            print(f"  {key:<20} SKIP  environment '{environment}' not found on this config")
            failed += 1
            continue

        current_idx = env_state.get("fallthrough", {}).get("variation")
        if current_idx == target_idx and env_state.get("enabled"):
            print(f"  {key:<20} ok    already serving '{DEFAULT_VARIATION_NAME}'")
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

    verb = "would change" if args.dry_run else "changed"
    print(f"\n{verb} {changed}, failed {failed}, total {len(CONFIG_KEYS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
