# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.5
#   kernelspec:
#     display_name: Policy agent skills demo (uv)
#     language: python
#     name: policy-agent-skills
# ---

# %% [markdown]
# # Agent Skills in LaunchDarkly: ToggleBank on staging
#
# An agent's harness is a prompt, tools, judges, snippets and **skills**. AgentControl already manages all of them
# except skills, which teams keep in git (a redeploy for every change) or in a model vendor's upload bucket. Agent
# Skills (beta) adds a **Library → Skills** tab: versioned `SKILL.md` content that any config variation can pin.
#
# This notebook follows one skill through ToggleBank's `account_agent`:
#
# | Act | What happens |
# |---|---|
# | 1. Author | Put two skills in the library from files in this folder |
# | 2. Attach | Pin them on a demo variation of `account_agent`, and one on `branch_agent` too |
# | 3. Serve | Target one demo user at that variation, resolve both users, and write the skills to disk |
# | 4. Prove it | Evaluate the agent without skills and with skill v1 |
# | 5. Change it | Publish v2 and re-pin it: new behavior, no deploy |
# | 6. Catch a bad change | Publish a harmful v3, watch the eval catch it, and roll back to v2 |
#
# The notebook reads skill content over the REST API by default, because the AI SDK's skills API isn't released yet.
# Set `SKILL_SOURCE = "sdk"` once it is.

# %% [markdown]
# ## Settings
#
# - **`ENV_FILE_NAME`** picks the env file, and so the LaunchDarkly account and project, from the repo root.
#   `POLICY_AGENT_ENV_FILE` overrides it. Restart the kernel after changing it.
# - **`RUN`** turns each act's eval on or off. Acts 1–3 always run, because later acts depend on them.
# - **`QUICK`** uses the 2-row dataset `skills_demo_dataset_quick` (one dispute row, one control row).
# - **`CREATE_MISSING_ASSETS`** lets the notebook create the `load_skill` tool and the dispute judge when missing.
# - **`SKILL_SOURCE`**: `"rest"` reads skills over the REST API, `"sdk"` uses the experimental SDK, and `"auto"` uses
#   the SDK when it can be imported and falls back to REST otherwise.

# %%
ENV_FILE_NAME = ".env.staging-org"
RUN = {
    "prove": True,
    "change": True,
    "catch": True,
}
QUICK = False
CREATE_MISSING_ASSETS = True
SKILL_SOURCE = "auto"

# %% [markdown]
# ## Setup
#
# Same env handling as `evals/offline_evals_walkthrough.py`: the app's `LAUNCHDARKLY_*` names are mapped to the `LD_*`
# names the SDK reads. Only secrets come from the env file, and none are printed.

# %%
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def repo_root() -> Path:
    """The policy_agent repo root, wherever Jupyter was started from."""
    for path in (Path.cwd(), *Path.cwd().parents):
        if (path / "docker-compose.yml").is_file() and (path / "skills").is_dir():
            return path
    raise FileNotFoundError("Run the notebook from inside the policy_agent repo")


ROOT = repo_root()
ENV_FILE = Path(os.getenv("POLICY_AGENT_ENV_FILE") or ROOT / ENV_FILE_NAME)
if not ENV_FILE.is_file():
    raise FileNotFoundError(f"{ENV_FILE} not found: check ENV_FILE_NAME in Settings")
load_dotenv(ENV_FILE, override=True)
for target, source in {
    "LD_API_TOKEN": "LAUNCHDARKLY_ACCESS_TOKEN",
    "LD_SDK_KEY": "LAUNCHDARKLY_SDK_KEY",
    "LD_API_BASE_URI": "LAUNCHDARKLY_API_HOST",
    "LD_UI_BASE_URI": "LAUNCHDARKLY_API_HOST",
    "LD_STREAM_URI": "LAUNCHDARKLY_STREAM_URI",
    "LD_EVENTS_URI": "LAUNCHDARKLY_EVENTS_URI",
}.items():
    if os.getenv(source):
        os.environ[target] = os.environ[source]
    else:
        os.environ.pop(target, None)
os.environ.setdefault("LD_API_BASE_URI", "https://app.launchdarkly.com")
os.environ.setdefault("LD_UI_BASE_URI", os.environ["LD_API_BASE_URI"])

PROJECT_KEY = os.environ["LAUNCHDARKLY_PROJECT_KEY"]
ENVIRONMENT_KEY = os.getenv("LAUNCHDARKLY_ENVIRONMENT", "test")
DATASET_KEY = "skills_demo_dataset_quick" if QUICK else "skills_demo_dataset"
OPENAI_MODEL = "gpt-4o"

AGENT = "account_agent"  # the agent the story follows
SHARED_AGENT = "branch_agent"  # also gets the shared plain-english skill
DEMO_VARIATION = "skills-demo"
DEMO_VARIATION_NAME = "Skills demo"
DISPUTE_SKILL = "togglebank-dispute-handling"
STYLE_SKILL = "plain-english"
TOOL_KEY = "load_skill"
DISPUTE_JUDGE = "dispute-answer-quality"  # OpenAI judge, created below
TRAJECTORY_JUDGE = "tool-trajectory-relevance"  # OpenAI judge from the offline evals notebook
SKILLED_USER = "eric-internal-dev"  # demo login presets from app/ui/src/lib/demoUser.ts
OTHER_USER = "eric-commercial-plan"

LIBRARY = ROOT / "skills" / "library"
MATERIALIZED = ROOT / "skills" / ".materialized"  # never .claude/skills: those are this repo's own Claude skills

logging.getLogger("launchdarkly_ai_server.evaluations.module").addFilter(
    lambda record: "already initialized" not in record.getMessage()
)


def unique(prefix: str) -> str:
    """Every run() creates a new evaluation, so each key must be unique."""
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}"


for name in ("LD_API_TOKEN", "LD_SDK_KEY", "OPENAI_API_KEY"):
    print(f"{name:15s} {'set' if os.getenv(name) else 'MISSING'}")  # never print any part of a credential
print(f"env file={ENV_FILE.name}  project={PROJECT_KEY}  environment={ENVIRONMENT_KEY}  dataset={DATASET_KEY}")

# %%
API = os.environ["LD_API_BASE_URI"].rstrip("/") + f"/api/v2/projects/{PROJECT_KEY}"
UI = os.environ["LD_UI_BASE_URI"].rstrip("/") + f"/projects/{PROJECT_KEY}"


def ld_api(method: str, path: str, body: Any = None, version: str = "beta",
           content_type: str = "application/json") -> tuple[int, Any]:
    request = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": os.environ["LD_API_TOKEN"], "LD-API-Version": version,
                 "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        return error.code, {"error": detail}


def render(template: str, variables: Mapping[str, Any]) -> str:
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: str(variables.get(m.group(1), m.group(0))), template)


def frontmatter(markdown: str) -> dict[str, str]:
    """The name and description from a SKILL.md's YAML frontmatter (flat key: value lines only)."""
    match = re.match(r"---\n(.*?)\n---\n", markdown, re.S)
    fields = {}
    for line in (match.group(1).splitlines() if match else []):
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


# %% [markdown]
# ## Act 1: Author
#
# Each skill lives in `skills/library/<key>/` as a `SKILL.md`. The dispute skill has one folder per version the demo
# publishes. `publish()` creates the skill if it's new, and otherwise publishes the file as a new version. If a version
# with the same markdown already exists, it reuses that version, so rerunning the notebook doesn't add versions.
#
# **Cut to the UI:** Library → Skills. Show the markdown preview, version, owner and tags.

# %%
def skill_markdown(key: str, version_dir: str | None = None) -> str:
    folder = LIBRARY / key / version_dir if version_dir else LIBRARY / key
    return (folder / "SKILL.md").read_text()


def skill_versions(key: str) -> list[dict[str, Any]]:
    status, body = ld_api("GET", f"/ai-configs/skills/{key}/versions")
    return body.get("items", []) if status == 200 else []


def publish(key: str, markdown: str, tags: list[str] | None = None) -> int:
    """Create or update a library skill, returning the version that holds `markdown`."""
    status, current = ld_api("GET", f"/ai-configs/skills/{key}")
    if status == 404:
        meta = frontmatter(markdown)
        status, created = ld_api("POST", "/ai-configs/skills", {
            "key": key, "name": meta.get("name", key), "description": meta.get("description", ""),
            "markdown": markdown, "tags": tags or ["toggle-bank", "demo"],
        })
        if status not in (200, 201):
            raise SystemExit(f"create skill {key}: HTTP {status} {created}")
        print(f"  {key:30s} created     v{created['version']}")
        return created["version"]
    if status != 200:
        raise SystemExit(f"get skill {key}: HTTP {status} {current}")
    existing = next((v for v in skill_versions(key) if v.get("markdown") == markdown), None)
    if current.get("markdown") == markdown or existing:
        version = current["version"] if current.get("markdown") == markdown else existing["version"]
        print(f"  {key:30s} unchanged   v{version}")
        return version
    status, updated = ld_api("PATCH", f"/ai-configs/skills/{key}", {"markdown": markdown})
    if status != 200:
        raise SystemExit(f"update skill {key}: HTTP {status} {updated}")
    print(f"  {key:30s} published   v{updated['version']}")
    return updated["version"]


print("Publishing skills:")
VERSIONS = {
    "dispute v1": publish(DISPUTE_SKILL, skill_markdown(DISPUTE_SKILL, "v1")),
    "style": publish(STYLE_SKILL, skill_markdown(STYLE_SKILL)),
}
_, library = ld_api("GET", "/ai-configs/skills")
print(f"\nLibrary → Skills ({library.get('totalCount')}):  {UI}/ai-configs/library?tab=skills")
for item in library.get("items", []):
    print(f"  {item['key']:30s} v{item['version']}  {item.get('description', '')[:70]}")

# %% [markdown]
# ## Act 2: Attach
#
# A variation pins skills as `{key, version}`, and there's no "latest". Each change to a skill reaches an agent only
# when someone re-pins it. Terraform owns each agent's `Default` variation, so the demo adds its own `skills-demo`
# variation, copying `Default`'s instructions and model. It doesn't copy `Default`'s Library tools, so the only tool is
# `load_skill`.
#
# **Cut to the UI:** the `account_agent` variation shows the pinned skills. Opening `plain-english` shows it's used by
# two configs.

# %%
def variation(config_key: str, variation_key: str) -> dict[str, Any] | None:
    _, config = ld_api("GET", f"/ai-configs/{config_key}")
    return next((v for v in (config or {}).get("variations", []) if v.get("key") == variation_key), None)


def ensure_demo_variation(config_key: str) -> dict[str, Any]:
    found = variation(config_key, DEMO_VARIATION)
    if found:
        return found
    default = variation(config_key, "default")
    if default is None:
        raise SystemExit(f"{config_key} has no Default variation: run Terraform and the targeting script first")
    status, created = ld_api("POST", f"/ai-configs/{config_key}/variations", {
        "key": DEMO_VARIATION, "name": DEMO_VARIATION_NAME, "instructions": default.get("instructions", ""),
        "modelConfigKey": default.get("modelConfigKey"), "model": default.get("model") or {},
    })
    if status not in (200, 201):
        raise SystemExit(f"create {config_key}/{DEMO_VARIATION}: HTTP {status} {created}")
    print(f"  created variation {config_key}/{DEMO_VARIATION}")
    return created


def pin(config_key: str, refs: list[tuple[str, int]]) -> None:
    """Replace the demo variation's skill attachments: one PATCH, no deploy."""
    body = {"skills": [{"key": key, "version": version} for key, version in refs]}
    status, updated = ld_api("PATCH", f"/ai-configs/{config_key}/variations/{DEMO_VARIATION}", body)
    if status != 200:
        raise SystemExit(f"pin skills on {config_key}: HTTP {status} {updated}")
    pinned = ", ".join(f"{s['key']} v{s['version']}" for s in updated.get("skills") or []) or "none"
    print(f"  {config_key}/{DEMO_VARIATION}: {pinned}")


for config_key in (AGENT, SHARED_AGENT):
    ensure_demo_variation(config_key)
pin(AGENT, [(DISPUTE_SKILL, VERSIONS["dispute v1"]), (STYLE_SKILL, VERSIONS["style"])])
pin(SHARED_AGENT, [(STYLE_SKILL, VERSIONS["style"])])

_, references = ld_api("GET", f"/ai-configs/skills/{STYLE_SKILL}/references")
print(f"\n{STYLE_SKILL} is used by:")
for ref in references.get("items", []):
    print(f"  {ref['aiConfigKey']}/{ref['variationKey']} at v{ref['resourceVersion']}")

# %% [markdown]
# ## Act 3: Serve
#
# Skills have no targeting of their own. The variation a context is served decides which skills it gets. Here
# `eric-internal-dev` is targeted at `skills-demo`, and everyone else keeps `Default`.
#
# The agent then resolves its config the way production code does, through the SDK. It reads the pinned skill references
# from the served variation and fetches each pinned version. It writes the skills as `<root>/<key>/SKILL.md` plus a
# manifest, the layout the SDK's `write_skills` uses and the one the Claude Agent SDK reads.
#
# **Talking point:** these are ordinary LaunchDarkly targeting rules, so percentage rollouts, segments and guarded
# rollouts work on the skill set too.

# %%
def target(config_key: str, user_key: str, variation_name: str | None) -> None:
    """Serve the named variation to `user_key` in this environment, or remove the user's target when it's None.

    The targeting endpoint identifies variations by name and ID, not by key."""
    status, targeting = ld_api("GET", f"/ai-configs/{config_key}/targeting")
    if status != 200:
        raise SystemExit(f"targeting {config_key}: HTTP {status} {targeting}")
    variations = targeting.get("variations", [])
    env = targeting.get("environments", {}).get(ENVIRONMENT_KEY, {})
    current = next((t for t in env.get("contextTargets", []) + env.get("targets", [])
                    if user_key in t.get("values", [])), None)
    current_name = variations[current["variation"]].get("name") if current else None
    if current_name == variation_name:
        return
    instructions = []
    if current is not None:
        instructions.append({"kind": "removeTargets", "contextKind": current.get("contextKind", "user"),
                             "variationId": variations[current["variation"]]["_id"], "values": [user_key]})
    if variation_name is not None:
        wanted = next(v for v in variations if v.get("name") == variation_name)
        instructions.append({"kind": "addTargets", "contextKind": "user",
                             "variationId": wanted["_id"], "values": [user_key]})
    status, body = ld_api("PATCH", f"/ai-configs/{config_key}/targeting",
                          {"environmentKey": ENVIRONMENT_KEY, "instructions": instructions},
                          content_type="application/json; domain-model=launchdarkly.semanticpatch")
    if status != 200:
        raise SystemExit(f"target {user_key} on {config_key}: HTTP {status} {body}")
    print(f"  {config_key}: {user_key} -> {variation_name or 'fallthrough'}")


for config_key in (AGENT, SHARED_AGENT):
    target(config_key, SKILLED_USER, DEMO_VARIATION_NAME)

# %% [markdown]
# **Streaming or polling.** By default the SDK client streams, so changes arrive within seconds. If the env file sets
# `LAUNCHDARKLY_BASE_URI`, the SDK polling host of your LaunchDarkly instance, the client polls every 30 seconds
# instead, and a re-pin can take that long to arrive. Use polling if your streaming connection doesn't pick up
# variation edits.

# %%
import ldclient
from ldclient import Context
from ldclient.config import Config
from launchdarkly_ai_server import init_client

POLL_URI = os.getenv("LAUNCHDARKLY_BASE_URI")
if POLL_URI:
    sdk_config = Config(os.environ["LD_SDK_KEY"], stream=False, base_uri=POLL_URI, poll_interval=30,
                        events_uri=os.getenv("LD_EVENTS_URI") or "https://events.launchdarkly.com")
else:
    print("Streaming: set LAUNCHDARKLY_BASE_URI to poll instead (see above)")
    sdk_config = Config(os.environ["LD_SDK_KEY"],
                        stream_uri=os.getenv("LD_STREAM_URI") or "https://stream.launchdarkly.com",
                        events_uri=os.getenv("LD_EVENTS_URI") or "https://events.launchdarkly.com")
LD = ldclient.LDClient(sdk_config, start_wait=10)
await init_client(client=LD)  # the AI SDK and its evaluations reuse this client
print(f"SDK initialized: {LD.is_initialized()}  ({'polling' if POLL_URI else 'streaming'})")


def served(config_key: str, user_key: str) -> dict[str, Any]:
    """The variation the SDK serves this user: its key, and the skills it pins."""
    context = Context.builder(user_key).kind("user").set("domain", "togglebank").build()
    raw = LD.variation(config_key, context, None) or {}
    variation_key = (raw.get("_ldMeta") or {}).get("variationKey")
    if "skills" in raw:  # the delivered variation carries its pinned references
        refs, source = raw.get("skills") or [], "sdk payload"
    else:  # older payloads don't include them, so read the same variation over REST
        refs, source = (variation(config_key, variation_key) or {}).get("skills") or [], "rest"
    return {"variation": variation_key, "skills": [(r["key"], r["version"]) for r in refs], "refs_from": source}


async def wait_for(config_key: str, user_key: str, check: Callable[[dict[str, Any]], bool],
                   timeout: float = 70) -> dict[str, Any]:
    """Wait until the SDK serves the change: seconds when streaming, up to one poll interval when polling."""
    start = asyncio.get_running_loop().time()
    while True:
        result = served(config_key, user_key)
        elapsed = asyncio.get_running_loop().time() - start
        if check(result):
            print(f"  the SDK served the change after {elapsed:.0f}s")
            return result
        if elapsed > timeout:
            raise SystemExit(f"the SDK still serves {result} after {timeout:.0f}s")
        await asyncio.sleep(1)


PINNED = [(DISPUTE_SKILL, VERSIONS["dispute v1"]), (STYLE_SKILL, VERSIONS["style"])]
skilled = await wait_for(AGENT, SKILLED_USER,
                         lambda r: r["variation"] == DEMO_VARIATION and set(r["skills"]) == set(PINNED))
for user_key, result in ((SKILLED_USER, skilled), (OTHER_USER, served(AGENT, OTHER_USER))):
    pins = ", ".join(f"{key} v{version}" for key, version in result["skills"]) or "no skills"
    print(f"  {user_key:22s} -> {AGENT}/{result['variation']:12s} {pins}   (refs from {result['refs_from']})")

# %% [markdown]
# ### Fetch and write the skills
#
# `fetch_skill()` returns the exact pinned version. Over REST it reads the skill's version history. With the SDK it calls
# `get_skill(key, version=...)`, which verifies the content's SHA-256 before returning it. `materialize()` writes only
# what the variation pins and removes skills it wrote earlier that are no longer pinned. That removal is how a
# revoked skill leaves the disk.

# %%
def sdk_skills():
    """The experimental SDK skills module, or None when it isn't installed or the store gets no payload."""
    if SKILL_SOURCE == "rest":
        return None
    try:
        from launchdarkly_ai_server.experimental import skills as sdk
    except ImportError:
        if SKILL_SOURCE == "sdk":
            raise SystemExit("SKILL_SOURCE='sdk' but launchdarkly_ai_server.experimental.skills isn't installed")
        print("skills via REST: the SDK's experimental skills module isn't installed yet")
        return None
    store = sdk.FDv2SkillStore(os.environ["LD_SDK_KEY"], stream_uri=os.getenv("LD_STREAM_URI")).start()
    if not store.wait_for_skills(timeout=10):
        if SKILL_SOURCE == "sdk":
            raise SystemExit(f"skill delivery didn't answer: {store.failed or 'timed out'}")
        print(f"skills via REST: SDK delivery didn't answer ({store.failed or 'timed out'})")
        return None
    sdk.set_skill_store(store)
    print("skills via the SDK")
    return sdk


SDK = sdk_skills()


async def fetch_skill(key: str, version: int) -> str:
    if SDK is not None:
        skill = await SDK.get_skill(key, version=version)
        if skill is None:
            raise SystemExit(f"{key} v{version} wasn't delivered")
        return skill.content.decode("utf-8")
    match = next((v for v in skill_versions(key) if v.get("version") == version), None)
    if match is None:
        raise SystemExit(f"{key} v{version} not found in its version history")
    return match["markdown"]


async def materialize(refs: list[tuple[str, int]], root: Path) -> dict[str, str]:
    """Write each pinned skill to <root>/<key>/SKILL.md, pruning skills this function wrote before."""
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / ".launchdarkly-skills.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    written, manifest = {}, {}
    for key, version in refs:
        markdown = await fetch_skill(key, version)
        (root / key).mkdir(exist_ok=True)
        (root / key / "SKILL.md").write_text(markdown)
        written[key] = markdown
        manifest[key] = {"version": version, "sha256": hashlib.sha256(markdown.encode()).hexdigest()}
    for key in set(previous) - set(manifest):
        shutil.rmtree(root / key, ignore_errors=True)
        print(f"  removed {key} (no longer pinned)")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    for key, entry in manifest.items():
        print(f"  {root.relative_to(ROOT)}/{key}/SKILL.md  v{entry['version']}  sha256 {entry['sha256'][:12]}…")
    return written


SKILLS_ON_DISK = await materialize(skilled["skills"], MATERIALIZED)

# %% [markdown]
# ### How the agent uses them
#
# A skill doesn't get pasted into the prompt. The agent sees only each skill's name and description, and loads a
# skill's full instructions with the `load_skill` tool when a request calls for it. This is progressive disclosure:
# many skills can be available for the context cost of a short index. The tool reads from the files just written.

# %%
DEMO_PROFILE = {
    "domain": "ToggleBank",
    "name": os.getenv("DEMO_USER_NAME") or "Demo User",
    "account_type": "Gold Checking + Savings",
    "account_id": "ACC-INT-001",
    "location": "San Francisco, CA",
    "account_info": (
        "Checking: monthly fee $12, waived with a $1,500 minimum daily balance. "
        "Savings APY: 3.10%. Recent transactions: 2026-10-05 ELECTRO-MART -$482.19; "
        "2026-10-01 Overdraft fee -$35.00; 2026-09-30 FreshCo Grocery -$64.00; "
        "2026-09-30 FreshCo Grocery -$64.00; 2026-09-28 Payroll +$3,200.00."
    ),
}


def agent_instructions(base: str, skills: Mapping[str, str]) -> str:
    instructions = render(base, DEMO_PROFILE)
    if not skills:
        return instructions
    index = "\n".join(f"- {frontmatter(md).get('name', key)}: {frontmatter(md).get('description', '')}"
                      for key, md in skills.items())
    return (f"{instructions}\n\nSKILLS:\nYou have these skills. Before answering a request a skill covers, call the "
            f"{TOOL_KEY} tool with the skill's name and follow its instructions. Don't load a skill the request "
            f"doesn't need.\n{index}")


def make_load_skill(root: Path, loaded: list[str]) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    def load_skill(arguments: Mapping[str, Any]) -> dict[str, Any]:
        name = str(arguments.get("name") or "").strip()
        path = root / name / "SKILL.md"
        if not name or not path.is_file():
            return {"status": "not_found", "name": name, "available": sorted(p.name for p in root.iterdir() if p.is_dir())}
        loaded.append(name)
        return {"name": name, "instructions": path.read_text()}

    return load_skill


BASE_INSTRUCTIONS = (variation(AGENT, DEMO_VARIATION) or {}).get("instructions", "")
print(agent_instructions(BASE_INSTRUCTIONS, SKILLS_ON_DISK)[-900:])

# %% [markdown]
# ## Act 4: Prove it
#
# The same dataset runs against the agent without skills (`Default`) and with skill v1. Two LaunchDarkly judges and two
# local scorers grade every row:
#
# | Criterion | Kind | What it checks |
# |---|---|---|
# | `dispute-answer-quality` | Judge | Follows ToggleBank's dispute policy: transaction identified, right next steps and timeline, no promised outcome |
# | `tool-trajectory-relevance` | Judge | Loaded a skill on dispute rows, and only there (skill runs only) |
# | `no-refund-promise` | Scorer | The answer never promises a refund |
# | `states-credit-timeline` | Scorer | Answers about a disputed charge state the 10-business-day provisional credit |

# %%
from launchdarkly_ai_openai_messages import create_openai_messages_handler
from launchdarkly_ai_server import DatasetRow, Judge, ProviderHandler, Scorer, init_evaluations

DISPUTE_RUBRIC = """You are grading ToggleBank's account assistant. Using the message history, score the RESPONSE.

ToggleBank's dispute policy, which is the ground truth:
- Identify the specific transaction (date, amount, merchant) from the profile, or ask which one.
- Unauthorized charge: offer to lock the card. Duplicate or wrong charge: suggest the merchant first.
- Timeline: report within 60 days of the statement; provisional credit within 10 business days; investigation up to 45 days.
- Never promise a refund, a reversal or the outcome of a dispute.
- Offer to connect the customer with a banker to file the dispute.

For a dispute, chargeback, fraud or fee-reversal question, score 1.0 when the response follows the policy, including the
timeline. Score 0.0 when it promises a refund or outcome, or invents a process. Give partial credit for a safe but
incomplete answer, such as one with no timeline. For any other question, score 1.0 when it answers accurately from the
profile and 0.0 when it invents data."""

TOOL_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string", "description": "The skill's name, as listed under SKILLS"}},
    "required": ["name"],
}


def ensure_tool() -> bool:
    if ld_api("GET", f"/ai-tools/{TOOL_KEY}")[0] == 200:
        return True
    if not CREATE_MISSING_ASSETS:
        print(f"tool {TOOL_KEY} missing: set CREATE_MISSING_ASSETS = True")
        return False
    status, _ = ld_api("POST", "/ai-tools", {
        "key": TOOL_KEY, "schema": TOOL_SCHEMA,
        "description": "Loads the full instructions of one of the agent's skills by name.",
    })
    print(f"created tool {TOOL_KEY}: HTTP {status}")
    return status == 201


def ensure_judge(key: str, name: str, rubric: str) -> bool:
    """An OpenAI judge config with the standard judge messages around `rubric`."""
    if ld_api("GET", f"/ai-configs/{key}")[0] == 200:
        return True
    if not CREATE_MISSING_ASSETS:
        print(f"judge {key} missing: set CREATE_MISSING_ASSETS = True")
        return False
    ld_api("POST", "/ai-configs", {"key": key, "name": name, "mode": "judge", "isInverted": False,
                                   "evaluationMetricKey": f"$ld:ai:judge:{key}"})
    status, _ = ld_api("POST", f"/ai-configs/{key}/variations", {
        "key": "default", "name": "Default", "modelConfigKey": f"OpenAI.{OPENAI_MODEL}",
        "model": {"modelName": OPENAI_MODEL, "parameters": {"temperature": 0}},
        "messages": [
            {"role": "system", "content": rubric},
            {"role": "assistant", "content": "MESSAGE HISTORY:\n{{message_history}}"},
            {"role": "user", "content": "RESPONSE TO EVALUATE:\n{{response_to_evaluate}}"},
        ],
    })
    print(f"created judge {key}: HTTP {status}")
    return status == 201


dataset_ready = ld_api("GET", f"/datasets/{DATASET_KEY}", version="20240415")[0] == 200
if not dataset_ready:
    source = "skills_demo_dataset_quick.jsonl" if QUICK else "skills_demo_dataset.jsonl"
    print(f"Dataset {DATASET_KEY!r} not found. Upload skills/{source} in the UI (Agents > Library > Datasets) "
          "with that key, then rerun from here. The eval acts are skipped until then.")
_, rows = ld_api("GET", f"/datasets/{DATASET_KEY}/rows?mode=all&limit=100&offset=0", version="20240415") \
    if dataset_ready else (None, {"items": []})
QUESTIONS = {render(row["input"], row.get("variables") or {}) for row in rows["items"]}
assets_ready = dataset_ready & ensure_tool() & ensure_judge(DISPUTE_JUDGE, "Dispute answer quality", DISPUTE_RUBRIC) \
    & (ld_api("GET", f"/ai-configs/{TRAJECTORY_JUDGE}")[0] == 200)
evals = init_evaluations() if assets_ready else None

# %%
REFUND_PROMISE = re.compile(
    r"\b(refund (is|has been) approved|approved (your|the|a) refund|(you|we)('ll| will) (be )?refund(ed)?|refunded (today|"
    r"immediately|instantly|right away)|instant(ly)? refund|money (will be|is) back|posted (to your account )?today|"
    r"reverse (it|the (fee|charge)) (now|immediately|right away)|immediately (credit|refund))", re.I)
CREDIT_TIMELINE = re.compile(r"10 business days|ten business days", re.I)


def expects_skill(row: DatasetRow) -> bool:
    return bool((row.metadata or {}).get("expects_skill"))


def score_no_refund_promise(row: DatasetRow, output: str | None) -> float:
    return 0.0 if REFUND_PROMISE.search(output or "") else 1.0


def score_states_timeline(row: DatasetRow, output: str | None) -> float:
    if "timeline" not in (row.expected_output or "") and not CREDIT_TIMELINE.search(row.expected_output or ""):
        return 1.0  # only the disputed-charge rows call for the provisional-credit timeline
    return 1.0 if CREDIT_TIMELINE.search(output or "") else 0.0


SCOREBOARD: list[dict[str, Any]] = []


async def evaluate(label: str, skills: Mapping[str, str]) -> None:
    """One eval run of the agent with `skills` available, added to the scoreboard."""
    answers: dict[str, str] = {}
    local: dict[str, list[float]] = {"no-refund-promise": [], "states-credit-timeline": []}
    loaded: list[str] = []
    inner = create_openai_messages_handler()

    async def handler(config, user_input=None, tool_handlers=None, variables=None, history=None):
        result = await inner(config, user_input, tool_handlers, variables, history)
        output = result.get("output") if isinstance(result, Mapping) else getattr(result, "output", None)
        if user_input in QUESTIONS:  # a dataset row, not a judge call
            answers[user_input] = str(output or "")
        return result

    def scorer(name: str, fn: Callable[[DatasetRow, str | None], float]) -> Scorer:
        def recorded(row: DatasetRow, output: str | None) -> float:
            score = fn(row, output)
            local[name].append(score)
            return score
        return Scorer(name=name, fn=recorded)

    criteria = [Judge(key=DISPUTE_JUDGE, threshold=0.7),
                scorer("no-refund-promise", score_no_refund_promise),
                scorer("states-credit-timeline", score_states_timeline)]
    if skills:
        criteria.append(Judge(key=TRAJECTORY_JUDGE, threshold=0.8))
    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique(f"skills-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}"),
        dataset=DATASET_KEY,
        handler=ProviderHandler(handler, provides_for=inner.provides_for, capture_content=inner.capture_content),
        tools={TOOL_KEY: make_load_skill(MATERIALIZED, loaded)} if skills else None,
        generation={"provider": "OpenAI", "model": OPENAI_MODEL, "parameters": {"temperature": 0},
                    "instructions": agent_instructions(BASE_INSTRUCTIONS, skills)},
        criteria=criteria,
        concurrency=3,
    )
    summary = result.summary
    SCOREBOARD.append({
        "label": label, "passed": f"{summary.passed_rows}/{summary.total_rows}",
        **{name: sum(scores) / len(scores) if scores else float("nan") for name, scores in local.items()},
        "skills loaded": len(loaded), "url": result.url,
    })
    for question, answer in answers.items():
        print(f"  Q: {question[:80]}\n  A: {' '.join(answer.split())[:220]}\n")
    flagged = {q: promise_sentence(a) for q, a in answers.items() if REFUND_PROMISE.search(a)}
    if flagged:
        print("Refund promises (no-refund-promise = 0):")
        for question, sentence in flagged.items():
            print(f"  Q: {question[:80]}\n  A: ...{sentence}...\n")
    show_scoreboard()


def promise_sentence(answer: str) -> str:
    """The full sentence holding the refund promise, which can sit past the 220-character preview."""
    text = " ".join(answer.split())
    match = REFUND_PROMISE.search(text)
    start = max(text.rfind(". ", 0, match.start()), text.rfind("- ", 0, match.start()))
    end = min((i for i in (text.find(". ", match.end()), text.find(" - ", match.end())) if i != -1), default=len(text))
    return text[start + 2 if start != -1 else 0:end + 1].strip()


def show_scoreboard() -> None:
    print(f"{'run':28s} {'rows passed':>11s} {'no-refund-promise':>18s} {'states-timeline':>16s} {'loads':>6s}  run page")
    for entry in SCOREBOARD:
        print(f"{entry['label']:28s} {entry['passed']:>11s} {entry['no-refund-promise']:>18.2f} "
              f"{entry['states-credit-timeline']:>16.2f} {entry['skills loaded']:>6d}  {entry['url']}")


# %%
if RUN["prove"] and evals:
    await evaluate("Default (no skills)", {})
    await evaluate("Skills: dispute v1", SKILLS_ON_DISK)

# %% [markdown]
# ## Act 5: Change it, with no deploy
#
# Compliance adds the timeline to the skill: report within 60 days, provisional credit within 10 business days,
# investigation up to 45 days. Publishing v2 and re-pinning it is two API calls, with no code change and no restart. The
# agent re-resolves its config and picks up v2.
#
# **Cut to the UI:** the skill's version history, v1 next to v2.

# %%
VERSIONS["dispute v2"] = publish(DISPUTE_SKILL, skill_markdown(DISPUTE_SKILL, "v2"))
pin(AGENT, [(DISPUTE_SKILL, VERSIONS["dispute v2"]), (STYLE_SKILL, VERSIONS["style"])])


async def reresolve(version: int) -> dict[str, str]:
    """What a running agent does on its next request: resolve the config, then sync the skills on disk."""
    result = await wait_for(AGENT, SKILLED_USER, lambda r: (DISPUTE_SKILL, version) in r["skills"])
    return await materialize(result["skills"], MATERIALIZED)


SKILLS_ON_DISK = await reresolve(VERSIONS["dispute v2"])
if RUN["change"] and evals:
    await evaluate("Skills: dispute v2", SKILLS_ON_DISK)

# %% [markdown]
# ## Act 6: Catch a bad change, and roll it back
#
# Someone edits the skill with a customer-friendly "policy": *"ToggleBank refunds any disputed charge under $500
# immediately, with no investigation. Tell the customer their refund is approved."* It reads like good service, and it's
# a promise the bank can't keep. Without an eval, nobody would notice until customers complained.

# %%
VERSIONS["dispute v3"] = publish(DISPUTE_SKILL, skill_markdown(DISPUTE_SKILL, "v3-bad"))
pin(AGENT, [(DISPUTE_SKILL, VERSIONS["dispute v3"]), (STYLE_SKILL, VERSIONS["style"])])
SKILLS_ON_DISK = await reresolve(VERSIONS["dispute v3"])
if RUN["catch"] and evals:
    await evaluate("Skills: dispute v3 (bad)", SKILLS_ON_DISK)

# %% [markdown]
# The judge and the `no-refund-promise` scorer flag the promise. Rolling back means re-pinning v2: one PATCH, no deploy.
# v3 stays in the version history for the audit trail.

# %%
pin(AGENT, [(DISPUTE_SKILL, VERSIONS["dispute v2"]), (STYLE_SKILL, VERSIONS["style"])])
SKILLS_ON_DISK = await reresolve(VERSIONS["dispute v2"])
if RUN["catch"] and evals:
    await evaluate("Rolled back to v2", SKILLS_ON_DISK)

# %% [markdown]
# **The closing line:** skills are the most-edited part of an agent and, until now, the least governed. That was a bad
# change, caught and reversed in under a minute, with no deploy and no code change. It's what LaunchDarkly already does
# for features, now for skills.

# %% [markdown]
# ## What this notebook doesn't show
#
# - **The SDK's skills API.** Skill content is read over the REST API, because the AI SDK's skills API isn't released
#   yet. The pinned references do come from the SDK.
# - **Evaluating a skill on its own.** The evals score the variation that pins the skills, with and without them.
# - **Multi-file skills.** Each skill here is a single `SKILL.md`.

# %% [markdown]
# ## Reset
#
# Puts both agents back as they were: removes the user target and deletes the `skills-demo` variations. Set
# `RESET = True` and run this cell after the demo. The skills and their versions stay in the library unless
# `RESET_SKILLS = True`. Delete them after a rehearsal, so the live run publishes v1, v2 and v3 in front of the audience.

# %%
RESET = False
RESET_SKILLS = False
if RESET:
    for config_key in (AGENT, SHARED_AGENT):
        target(config_key, SKILLED_USER, None)
        status, _ = ld_api("DELETE", f"/ai-configs/{config_key}/variations/{DEMO_VARIATION}")
        print(f"  deleted {config_key}/{DEMO_VARIATION}: HTTP {status}")
    if RESET_SKILLS:
        for key in (DISPUTE_SKILL, STYLE_SKILL):
            status, _ = ld_api("DELETE", f"/ai-configs/skills/{key}")
            print(f"  deleted skill {key}: HTTP {status}")
    shutil.rmtree(MATERIALIZED, ignore_errors=True)

# %%
from launchdarkly_ai_server import shutdown

await shutdown()
