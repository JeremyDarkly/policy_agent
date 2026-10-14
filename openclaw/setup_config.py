#!/usr/bin/env python3
"""Create and change the `openclaw_agent` AgentControl config that the OpenClaw plugin reads.

Usage:
    python openclaw/setup_config.py setup                 # tools, skills, config, variations, targeting (idempotent)
    python openclaw/setup_config.py status                # what each variation serves, and who gets it
    python openclaw/setup_config.py off | on              # the kill switch
    python openclaw/setup_config.py instructions standard "New instructions..."
    python openclaw/setup_config.py skill openclaw-house-style v2   # publish a skill version and re-pin it
    python openclaw/setup_config.py reset                 # put every variation's instructions and skill pins back

Settings come from the env file named by ENV_FILE_NAME (default `.env.staging-org`) at the repo root:
LAUNCHDARKLY_ACCESS_TOKEN, LAUNCHDARKLY_PROJECT_KEY, LAUNCHDARKLY_ENVIRONMENT and, for a non-production instance,
LAUNCHDARKLY_API_HOST.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_KEY = "openclaw_agent"
SEMANTIC_PATCH = "application/json; domain-model=launchdarkly.semanticpatch"
# Library skills, as skills/<key>/<version folder>/SKILL.md. Publishing a folder adds a LaunchDarkly skill version.
SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# Library tool keys are `openclaw_<OpenClaw tool id>`. The plugin strips the prefix.
TOOLS = {
    "openclaw_read": "Lets an OpenClaw agent use its built-in read tool to read files in its workspace.",
    "openclaw_exec": "Lets an OpenClaw agent use its built-in exec tool to run shell commands.",
}

STANDARD_INSTRUCTIONS = (
    "Start every reply with the tag [standard]. You are an OpenClaw agent whose settings come from LaunchDarkly "
    "AgentControl. Keep answers short. You may read files and run shell commands when the user asks you to."
)
VARIATIONS = {
    "standard": {
        "name": "Standard",
        "model": "gpt-5.4-mini",
        "instructions": STANDARD_INSTRUCTIONS,
        "tools": ["openclaw_read", "openclaw_exec"],
        "skills": {"openclaw-house-style": "v1", "openclaw-workspace-report": "v1"},
    },
    "locked-down": {
        "name": "Locked down",
        "model": "gpt-5.4-nano",
        "instructions": (
            "Start every reply with the tag [locked-down]. Keep answers short."
        ),
        "tools": [],
        "skills": {"openclaw-house-style": "v1", "openclaw-explain-limits": "v1"},
    },
}
# Contexts whose agentId is in this list get the locked-down variation. Everyone else gets standard. Keep the agent
# names neutral: OpenClaw puts the agent id in the system prompt, and an agent called "restricted" refuses tools on
# its own, which hides whether the plugin blocked them.
LOCKED_DOWN_AGENTS = ["helper"]
RULE_DESCRIPTION = "Locked-down OpenClaw agents"


def load_env() -> dict[str, str]:
    name = os.environ.get("ENV_FILE_NAME", ".env.staging-org")
    env: dict[str, str] = {}
    path = REPO_ROOT / name
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip("'\"")
    env.update({k: v for k, v in os.environ.items() if k.startswith("LAUNCHDARKLY_")})
    missing = [k for k in ("LAUNCHDARKLY_ACCESS_TOKEN", "LAUNCHDARKLY_PROJECT_KEY") if not env.get(k)]
    if missing:
        raise SystemExit(f"{', '.join(missing)} must be set in {name}")
    return env


ENV = load_env()
ENVIRONMENT = ENV.get("LAUNCHDARKLY_ENVIRONMENT", "test")
API_HOST = (ENV.get("LAUNCHDARKLY_API_HOST") or "https://app.launchdarkly.com").rstrip("/")
API = f"{API_HOST}/api/v2/projects/{ENV['LAUNCHDARKLY_PROJECT_KEY']}"
FLAGS_API = f"{API_HOST}/api/v2/flags/{ENV['LAUNCHDARKLY_PROJECT_KEY']}"


def ld_api(
    method: str, path: str, body: Any = None, content_type: str = "application/json", base: str = API
) -> tuple[int, Any]:
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": ENV["LAUNCHDARKLY_ACCESS_TOKEN"], "LD-API-Version": "beta", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")[:500]


def check(status: int, body: Any, what: str) -> Any:
    if status not in (200, 201, 204):
        raise SystemExit(f"{what}: HTTP {status} {body}")
    return body


def ensure_tools() -> dict[str, int]:
    """Create any missing Library tools. Returns each tool's latest version, for attaching to variations."""
    versions = {}
    for key, description in TOOLS.items():
        status, tool = ld_api("GET", f"/ai-tools/{key}")
        if status == 404:
            status, tool = ld_api("POST", "/ai-tools", {
                "key": key, "description": description, "schema": {"type": "object", "properties": {}},
            })
            check(status, tool, f"create tool {key}")
            print(f"  created tool {key}")
        else:
            check(status, tool, f"get tool {key}")
        versions[key] = tool.get("version", 1)
    return versions


def skill_markdown(key: str, folder: str) -> str:
    return (SKILLS_DIR / key / folder / "SKILL.md").read_text()


def frontmatter(markdown: str) -> dict[str, str]:
    """The flat `key: value` lines between a SKILL.md's leading `---` markers."""
    lines = markdown.split("---", 2)[1].strip().splitlines() if markdown.startswith("---") else []
    return {key.strip(): value.strip() for key, _, value in (line.partition(":") for line in lines)}


def publish_skill(key: str, folder: str) -> int:
    """Create the library skill, or publish `folder` as a new version of it. Returns the LaunchDarkly version that
    holds that markdown, reusing an existing version so reruns don't add versions."""
    markdown = skill_markdown(key, folder)
    status, current = ld_api("GET", f"/ai-configs/skills/{key}")
    if status == 404:
        meta = frontmatter(markdown)
        created = check(*ld_api("POST", "/ai-configs/skills", {
            "key": key, "name": meta.get("name", key), "description": meta.get("description", ""),
            "markdown": markdown, "tags": ["openclaw"],
        }), f"create skill {key}")
        print(f"  created skill {key} v{created['version']} from {folder}")
        return created["version"]
    check(status, current, f"get skill {key}")
    if current.get("markdown") == markdown:
        return current["version"]
    versions = check(*ld_api("GET", f"/ai-configs/skills/{key}/versions"), f"list versions of {key}")
    existing = next((v["version"] for v in versions.get("items", []) if v.get("markdown") == markdown), None)
    if existing is not None:
        return existing
    updated = check(*ld_api("PATCH", f"/ai-configs/skills/{key}", {"markdown": markdown}), f"publish skill {key}")
    print(f"  published skill {key} v{updated['version']} from {folder}")
    return updated["version"]


def ensure_skills(overrides: dict[str, str] | None = None) -> dict[str, int]:
    """Publish every skill folder the variations pin. Returns the LaunchDarkly version to pin for each skill key.
    `overrides` swaps in another folder for a key, everywhere it's pinned."""
    folders = {key: folder for spec in VARIATIONS.values() for key, folder in spec["skills"].items()}
    folders.update(overrides or {})
    return {key: publish_skill(key, folder) for key, folder in folders.items()}


def ensure_config() -> dict[str, Any]:
    status, config = ld_api("GET", f"/ai-configs/{CONFIG_KEY}")
    if status == 404:
        check(*ld_api("POST", "/ai-configs", {"key": CONFIG_KEY, "name": "OpenClaw agent", "mode": "agent"}),
              f"create config {CONFIG_KEY}")
        print(f"  created config {CONFIG_KEY}")
        status, config = ld_api("GET", f"/ai-configs/{CONFIG_KEY}")
    return check(status, config, f"get config {CONFIG_KEY}")


def variation_body(spec: dict[str, Any], tool_versions: dict[str, int], skill_versions: dict[str, int]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "instructions": spec["instructions"],
        "modelConfigKey": f"OpenAI.{spec['model']}",
        "model": {"modelName": spec["model"], "parameters": {}},
        "tools": [{"key": key, "version": tool_versions[key]} for key in spec["tools"]],
        "skills": [{"key": key, "version": skill_versions[key]} for key in spec["skills"]],
    }


def ensure_variations(config: dict[str, Any], tool_versions: dict[str, int], skill_versions: dict[str, int],
                      quiet: bool = False) -> None:
    existing = {v["key"]: v for v in config.get("variations", [])}
    for key, spec in VARIATIONS.items():
        body = variation_body(spec, tool_versions, skill_versions)
        if key in existing:
            check(*ld_api("PATCH", f"/ai-configs/{CONFIG_KEY}/variations/{key}", body), f"update variation {key}")
            if not quiet:
                print(f"  updated variation {key}")
        else:
            check(*ld_api("POST", f"/ai-configs/{CONFIG_KEY}/variations", {"key": key, **body}),
                  f"create variation {key}")
            print(f"  created variation {key}")


def targeting() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    targets = check(*ld_api("GET", f"/ai-configs/{CONFIG_KEY}/targeting"), "get targeting")
    env = targets.get("environments", {}).get(ENVIRONMENT)
    if env is None:
        raise SystemExit(f"environment {ENVIRONMENT!r} not found on {CONFIG_KEY}")
    return targets.get("variations", []), env


def patch_targeting(instructions: list[dict[str, Any]], what: str) -> None:
    if instructions:
        check(*ld_api("PATCH", f"/ai-configs/{CONFIG_KEY}/targeting",
                      {"environmentKey": ENVIRONMENT, "instructions": instructions}, SEMANTIC_PATCH), what)


def variation_id(variations: list[dict[str, Any]], name: str) -> str:
    return next(v["_id"] for v in variations if v.get("name") == name)


def flag_rules() -> list[dict[str, Any]]:
    """The config's targeting rules with their IDs and variations, which the AI Configs targeting view leaves out.
    Reading an AI Config's flag is allowed; only writing to it is refused."""
    flag = check(*ld_api("GET", f"/{CONFIG_KEY}?env={ENVIRONMENT}", base=FLAGS_API), "get flag rules")
    return flag["environments"][ENVIRONMENT].get("rules", [])


def ensure_targeting(serve: str | None = None) -> int:
    """Serve standard by default and locked-down to LOCKED_DOWN_AGENTS. With `serve`, every rule and the default
    serve that variation instead. Returns the number of changes."""
    variations, env = targeting()
    default = variation_id(variations, serve or VARIATIONS["standard"]["name"])
    locked = variation_id(variations, serve or VARIATIONS["locked-down"]["name"])
    if not env.get("enabled"):
        # The API has no instruction to turn an AI Config's targeting on; only the UI toggle does it.
        print(f"  warning: targeting is off in {ENVIRONMENT}. Turn it on in the LaunchDarkly UI")
    instructions: list[dict[str, Any]] = []
    if variations[env.get("fallthrough", {}).get("variation", 0)]["_id"] != default:
        instructions.append({"kind": "updateFallthroughVariationOrRollout", "variationId": default})
    # Replace this script's rule when its agent list changed. Rule IDs are only in the flag's view.
    wanted = sorted(LOCKED_DOWN_AGENTS)
    current = [rule for rule in flag_rules() if rule.get("description") == RULE_DESCRIPTION]
    if [sorted(rule["clauses"][0]["values"]) for rule in current] != [wanted]:
        instructions += [{"kind": "removeRule", "ruleId": rule["_id"]} for rule in current]
        instructions.append({
            "kind": "addRule",
            "description": RULE_DESCRIPTION,
            "variationId": locked,
            "clauses": [{"contextKind": "user", "attribute": "agentId", "op": "in", "values": wanted, "negate": False}],
        })
    elif variations[current[0]["variation"]]["_id"] != locked:
        instructions.append({"kind": "updateRuleVariationOrRollout", "ruleId": current[0]["_id"], "variationId": locked})
    patch_targeting(instructions, "set targeting")
    return len(instructions)


def set_enabled(enabled: bool) -> None:
    """The kill switch. LaunchDarkly adds a `disabled` variation to every AI Config, which the SDK returns with
    enabled=false. Off serves it to everyone; on restores the normal targeting. (The UI's targeting toggle does the
    same thing, by serving the off variation, but the API can't flip it.)"""
    ensure_targeting(serve=None if enabled else "disabled")
    print(f"{CONFIG_KEY} is {'on' if enabled else 'off'} in {ENVIRONMENT}")


def set_instructions(key: str, text: str) -> None:
    check(*ld_api("PATCH", f"/ai-configs/{CONFIG_KEY}/variations/{key}", {"instructions": text}),
          f"update instructions on {key}")
    print(f"{CONFIG_KEY}/{key} instructions updated")


def status() -> None:
    config = ensure_config()
    for variation in config.get("variations", []):
        tools = ", ".join(t["key"] for t in variation.get("tools") or []) or "none"
        skills = ", ".join(f"{s['key']} v{s['version']}" for s in variation.get("skills") or []) or "none"
        print(f"  {variation['key']:12s} model={variation.get('modelConfigKey')}  tools={tools}")
        print(f"  {'':12s} skills={skills}")
        print(f"  {'':12s} instructions={variation.get('instructions', '')[:90]!r}")
    variations, env = targeting()
    fallthrough = variations[env.get("fallthrough", {}).get("variation", 0)].get("name")
    print(f"  {ENVIRONMENT}: {'on' if env.get('enabled') else 'OFF'}, default={fallthrough}")
    for rule in flag_rules():
        clauses = "; ".join(f"{c['attribute']} {c['op']} {c['values']}" for c in rule.get("clauses", []))
        print(f"    rule: {clauses} -> {variations[rule['variation']].get('name')}")


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if command == "setup":
        print(f"project={ENV['LAUNCHDARKLY_PROJECT_KEY']} environment={ENVIRONMENT}")
        tool_versions, skill_versions = ensure_tools(), ensure_skills()
        ensure_variations(ensure_config(), tool_versions, skill_versions)
        print(f"  targeting in {ENVIRONMENT}: {ensure_targeting()} change(s)")
        status()
    elif command == "status":
        status()
    elif command in ("on", "off"):
        set_enabled(command == "on")
    elif command == "instructions" and len(sys.argv) == 4:
        set_instructions(sys.argv[2], sys.argv[3])
    elif command == "skill" and len(sys.argv) == 4:
        key, folder = sys.argv[2], sys.argv[3]
        ensure_variations(ensure_config(), ensure_tools(), ensure_skills({key: folder}), quiet=True)
        print(f"{key} from {folder} is pinned wherever {key} is used")
    elif command == "reset":
        ensure_variations(ensure_config(), ensure_tools(), ensure_skills(), quiet=True)
        print("instructions and skill pins restored")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
