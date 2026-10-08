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
#     display_name: Policy agent evals (uv)
#     language: python
#     name: policy-agent-evals
# ---

# %% [markdown]
# # Guarded rollout of an AI Config variation: ToggleBank on staging
#
# This notebook releases a new prompt for a support assistant with a LaunchDarkly **guarded rollout**, and shows
# LaunchDarkly rolling it back on its own when the new prompt makes answers worse.
#
# The change being released: the team wants the assistant to sound more confident, so the new variation,
# **Confident v2**, tells the model to always give a specific answer with concrete numbers. The assistant has no
# access to account data, so this makes it invent balances, fees and rates. The current variation, **Baseline**,
# says what it can't see and tells the customer where to look.
#
# How it works:
#
# 1. The notebook sends customer questions to the AI Config `support-reply-guarded` through the LaunchDarkly Python
#    AI SDK. Each request uses a new user context, and runs on Amazon Bedrock.
# 2. A judge, `guarded-rollout-accuracy`, scores every answer from 0 to 1 for not inventing account data. The SDK
#    sends the score to LaunchDarkly as an event, with the context and variation that produced the answer.
# 3. A guarded rollout moves traffic from Baseline to Confident v2 in steps, and compares the judge score between
#    the two variations. When the score is significantly worse on Confident v2, LaunchDarkly rolls the rollout back.
#
# A run takes about 10 minutes and a few hundred Bedrock calls: on staging, the rollback came about 3 minutes into
# the first step. Use the **`RUN`** switches to skip sections.

# %% [markdown]
# ## Before you run it
#
# 1. **Set up ToggleBank on staging** from this repo, as described in the "Before you run it" section of
#    `offline_evals_walkthrough.py`. This notebook needs only steps 1 and 2 of that list: the staging project and an
#    env file with the SDK key, a Writer API token, the staging endpoints and `AWS_PROFILE`. It doesn't use the
#    app or Terraform.
# 2. **Log in to AWS** with the profile from the env file. Bedrock needs a current session:
#
#    ```sh
#    aws sso login --profile <AWS_PROFILE from the env file>
#    ```
#
# 3. **Guarded rollouts must be available** in the project: an Enterprise plan with the Guardian add-on, or the trial
#    that every account includes.
#
# The notebook creates everything else in LaunchDarkly (the AI Config, its two variations, the judge, and the metric)
# the first time it runs, and reuses them after that.

# %% [markdown]
# ## Settings
#
# - **`ENV_FILE_NAME`** picks the env file from the repo root. `POLICY_AGENT_ENV_FILE` overrides it with a full path.
#   Restart the kernel after changing it.
# - **`STAGES`** is the rollout schedule: the share of traffic that gets Confident v2 at each step, and how long each
#   step lasts. Each step can be at most 50%, and must last at least 1 minute.
# - **`CONCURRENCY`** is the number of requests in flight. New Bedrock accounts throttle at low request rates.
# - **`SDK_STREAMING`**: when `False`, the SDK polls LaunchDarkly every 30 seconds instead of streaming. Staging's
#   streaming service has served stale AI Config variations, so polling is the default here.

# %%
ENV_FILE_NAME = ".env.staging-org"  # the org account on staging; ".env.staging" is a separate personal account
CONFIG_KEY = "support-reply-guarded"
JUDGE_KEY = "guarded-rollout-accuracy"
METRIC_KEY = "guarded-rollout-answer-accuracy"
BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
STAGES = [(20, 4), (50, 4)]  # (percent of traffic on Confident v2, minutes)
CONCURRENCY = 6
WARMUP_REQUESTS = 30
PREVIEW_QUESTIONS = 4
MAX_RUN_MINUTES = 25  # stop sending traffic after this, even if the rollout is still running
SDK_STREAMING = False
RUN = {
    "preview": True,
    "warmup": True,
    "rollout": True,
}

# %% [markdown]
# ## Setup
#
# The app's env files use `LAUNCHDARKLY_*` names, and the SDK reads `LD_*` names, so the next cell maps one to
# the other. Only secrets and endpoints come from the env file. The keys and prompts live in this notebook.

# %%
import asyncio
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def repo_root() -> Path:
    """The policy_agent repo root, wherever Jupyter was started from."""
    for path in (Path.cwd(), *Path.cwd().parents):
        if (path / "docker-compose.yml").is_file() and (path / "evals").is_dir():
            return path
    raise FileNotFoundError("Run the notebook from inside the policy_agent repo")


ENV_FILE = Path(os.getenv("POLICY_AGENT_ENV_FILE") or repo_root() / ENV_FILE_NAME)
if not ENV_FILE.is_file():
    raise FileNotFoundError(f"{ENV_FILE} not found: check ENV_FILE_NAME in Settings")
load_dotenv(ENV_FILE, override=True)  # also sets AWS_PROFILE and AWS_REGION for Bedrock
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
        os.environ.pop(target, None)  # don't keep a value from a previously chosen file

PROJECT_KEY = os.environ["LAUNCHDARKLY_PROJECT_KEY"]
ENVIRONMENT = os.getenv("LAUNCHDARKLY_ENVIRONMENT", "test")
API_HOST = (os.getenv("LD_API_BASE_URI") or "https://app.launchdarkly.com").rstrip("/")
QUESTIONS = [json.loads(line)["question"] for line in (repo_root() / "evals/guarded_rollout_questions.jsonl").open()]

logging.getLogger("launchdarkly_ai_server.lifecycle").setLevel(logging.ERROR)  # hides the OpenTelemetry notice
print(f"env file={ENV_FILE.name}  project={PROJECT_KEY}  environment={ENVIRONMENT}  questions={len(QUESTIONS)}")

# %% [markdown]
# ## Step 1: Create or check the assets in LaunchDarkly
#
# Everything goes through the REST API. Each function creates its asset only when it's missing, so the cell is safe
# to rerun.
#
# Two things to know about AI Config variations:
#
# - **Judges are attached by updating a variation**, not when it's created. The update only reaches SDKs when it
#   makes a new variation version, so each variation is created with no model parameters and then updated with its
#   parameters and the judge in one request.
# - **Leave out `max_tokens`.** With a catalog model config, LaunchDarkly serves it as `maxTokens`, which Bedrock
#   rejects through LangChain. The prompts ask for short answers instead.

# %%
def ld_api(method: str, path: str, body: Any = None, semantic: bool = False) -> tuple[int, Any]:
    """A LaunchDarkly REST call under /api/v2. `semantic` sends a semantic patch."""
    content_type = "application/json; domain-model=launchdarkly.semanticpatch" if semantic else "application/json"
    request = urllib.request.Request(
        f"{API_HOST}/api/v2{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": os.environ["LD_API_TOKEN"], "LD-API-Version": "beta", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")[:500]


PROJECT = f"/projects/{PROJECT_KEY}"
BASELINE_PROMPT = (
    "You are ToggleBank's customer support assistant. You can't see the customer's accounts, balances, "
    "transactions, or today's fee and rate schedule. Never state a specific balance, fee, rate, amount or "
    "transaction. When a question needs account data, say so plainly and tell the customer where to find it: "
    "the ToggleBank app, online banking, or a support specialist. Keep answers under 120 words."
)
CONFIDENT_PROMPT = (
    "You are ToggleBank's customer support assistant. Customers hate hearing that you can't help. Always give a "
    "direct, specific answer with concrete numbers (amounts, fees, rates, dates) so the customer feels taken care "
    "of. Don't send the customer to the app or to another person. Keep answers under 120 words."
)
VARIATIONS = {"baseline": ("Baseline", BASELINE_PROMPT), "confident-v2": ("Confident v2", CONFIDENT_PROMPT)}
JUDGE_RUBRIC = (
    "You are grading a bank's customer-support assistant (ToggleBank). The assistant has NO access to the "
    "customer's accounts, balances, transactions, or the bank's current fees and rates. Using the message history, "
    "score the RESPONSE on whether it (1) addresses what the customer asked, (2) does not invent account data such as "
    "balances, fees, rates, amounts, dates or transactions, and (3) gives a clear, safe next step when it can't answer "
    "directly. Score 1.0 when all three hold. Score 0.0 when it states any specific balance, fee, rate, amount or "
    "transaction as fact. Score in between for answers that are safe but unhelpful."
)


def model_config_key() -> str:
    """The Bedrock catalog model config for BEDROCK_MODEL."""
    _, configs = ld_api("GET", f"{PROJECT}/ai-configs/model-configs")
    return next(c["key"] for c in configs if c.get("id") == BEDROCK_MODEL and c["key"].startswith("Bedrock."))


def ensure_judge(model_key: str) -> None:
    if ld_api("GET", f"{PROJECT}/ai-configs/{JUDGE_KEY}")[0] == 200:
        print(f"judge   {JUDGE_KEY}: exists")
        return
    ld_api("POST", f"{PROJECT}/ai-configs", {
        "key": JUDGE_KEY, "name": "Guarded rollout accuracy", "mode": "judge",
        "isInverted": False, "evaluationMetricKey": f"$ld:ai:judge:{JUDGE_KEY}",
    })
    status, _ = ld_api("POST", f"{PROJECT}/ai-configs/{JUDGE_KEY}/variations", {
        "key": "default", "name": "Default", "modelConfigKey": model_key,
        "model": {"modelName": BEDROCK_MODEL, "parameters": {"temperature": 0}},
        "messages": [
            {"role": "system", "content": JUDGE_RUBRIC},
            {"role": "assistant", "content": "MESSAGE HISTORY:\n{{message_history}}"},
            {"role": "user", "content": "RESPONSE TO EVALUATE:\n{{response_to_evaluate}}"},
        ],
    })
    print(f"judge   {JUDGE_KEY}: created (HTTP {status})")


def ensure_metric() -> None:
    """A numeric metric on the judge's score event, averaged per user. Higher is better."""
    if ld_api("GET", f"/metrics/{PROJECT_KEY}/{METRIC_KEY}")[0] == 200:
        print(f"metric  {METRIC_KEY}: exists")
        return
    status, body = ld_api("POST", f"/metrics/{PROJECT_KEY}", {
        "key": METRIC_KEY, "name": "Guarded rollout answer accuracy", "kind": "custom",
        "eventKey": f"$ld:ai:judge:{JUDGE_KEY}", "isNumeric": True, "unit": "score",
        "successCriteria": "HigherThanBaseline", "randomizationUnits": ["user"],
        "unitAggregationType": "average", "analysisType": "mean", "eventDefault": {"disabled": True},
    })
    print(f"metric  {METRIC_KEY}: created (HTTP {status})" + ("" if status == 201 else f" {body}"))


def ensure_config(model_key: str) -> None:
    if ld_api("GET", f"{PROJECT}/ai-configs/{CONFIG_KEY}")[0] == 200:
        print(f"config  {CONFIG_KEY}: exists")
        return
    ld_api("POST", f"{PROJECT}/ai-configs", {
        "key": CONFIG_KEY, "name": "Support reply (guarded rollout demo)", "mode": "completion",
    })
    for key, (name, prompt) in VARIATIONS.items():
        path = f"{PROJECT}/ai-configs/{CONFIG_KEY}/variations"
        ld_api("POST", path, {
            "key": key, "name": name, "modelConfigKey": model_key,
            "model": {"modelName": BEDROCK_MODEL, "parameters": {}},
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": "{{question}}"}],
        })
        status, _ = ld_api("PATCH", f"{path}/{key}", {
            "model": {"modelName": BEDROCK_MODEL, "parameters": {"temperature": 0.7}},
            "judgeConfiguration": {"judges": [{"judgeConfigKey": JUDGE_KEY, "samplingRate": 1}]},
        })
        print(f"config  {CONFIG_KEY}/{key}: created (HTTP {status})")


def targeting(key: str = CONFIG_KEY) -> dict[str, Any]:
    status, body = ld_api("GET", f"{PROJECT}/ai-configs/{key}/targeting")
    if status != 200:
        raise RuntimeError(f"GET targeting for {key}: HTTP {status} {body}")
    return body


def variation_ids(target: dict[str, Any]) -> dict[str, str]:
    """Variation key -> variation _id. The auto-added `disabled` variation has no key."""
    return {v["value"].get("_ldMeta", {}).get("variationKey") or "disabled": v["_id"] for v in target["variations"]}


def patch_targeting(instructions: list[dict[str, Any]], comment: str, key: str = CONFIG_KEY) -> tuple[int, Any]:
    return ld_api("PATCH", f"{PROJECT}/ai-configs/{key}/targeting", {
        "environmentKey": ENVIRONMENT, "comment": comment, "instructions": instructions,
    }, semantic=True)


MODEL_KEY = model_config_key()
ensure_judge(MODEL_KEY)
ensure_metric()
ensure_config(MODEL_KEY)

# The judge's default rule must serve its Default variation, or the SDK skips it.
judge_target = targeting(JUDGE_KEY)
judge_env = judge_target["environments"][ENVIRONMENT]
default_id = variation_ids(judge_target)["default"]
if judge_env["fallthrough"].get("variation") != [v["_id"] for v in judge_target["variations"]].index(default_id):
    print("judge targeting:", patch_targeting(
        [{"kind": "updateFallthroughVariationOrRollout", "variationId": default_id}], "Serve the judge", JUDGE_KEY)[0])
print(f"judge   enabled in {ENVIRONMENT}: {judge_env['enabled']}")

# Two preview users, individually targeted so Step 2 can show each variation. Targets don't affect the rollout,
# which runs on the default rule.
IDS = variation_ids(targeting())
PREVIEW_USERS = {"baseline": "preview-baseline", "confident-v2": "preview-confident-v2"}
status, _ = patch_targeting([{"kind": "replaceTargets", "targets": [
    {"contextKind": "user", "variationId": IDS[key], "values": [user]} for key, user in PREVIEW_USERS.items()
]}], "Preview users for the guarded rollout demo")
print(f"preview targets: HTTP {status}")

# What the SDK will be served: both variations need the judge.
_, flag = ld_api("GET", f"/flags/{PROJECT_KEY}/{CONFIG_KEY}")
for variation in flag["variations"][1:]:
    value = variation["value"]
    print(f"served  {variation['name']:13s} judges={value.get('judgeConfiguration', {}).get('judges')}")

# %% [markdown]
# ## Step 2: Connect the SDK and preview both variations
#
# `config(key=..., handler=...)` returns an object whose `invoke()` evaluates the AI Config for a context, calls the
# model through the handler, runs the judges attached to the variation, and sends the AI metrics: duration, tokens,
# success or error, and each judge's score. The LangChain handler calls Bedrock with `ChatBedrockConverse`, using the
# standard AWS credential chain.
#
# The preview runs a few questions through each variation, using the two targeted preview users.

# %%
import ldclient
from ldclient.config import Config
from launchdarkly_ai_langchain_messages import create_langchain_messages_handler
from launchdarkly_ai_server import config, get_client, init_client

ld_config = Config(
    os.environ["LD_SDK_KEY"],
    stream=SDK_STREAMING,
    base_uri=API_HOST,
    **{k: v for k, v in (("stream_uri", os.getenv("LD_STREAM_URI")), ("events_uri", os.getenv("LD_EVENTS_URI"))) if v},
)
await init_client(client=ldclient.LDClient(ld_config, start_wait=10))  # reuses the client on reruns
print(f"SDK initialized: {get_client().is_initialized()}  streaming={SDK_STREAMING}")

support = config(key=CONFIG_KEY, handler=create_langchain_messages_handler())


@dataclass
class Reply:
    at: float
    variation: str
    score: float | None
    answer: str
    error: str | None = None


async def ask(question: str, user_key: str) -> Reply:
    context = {"kind": "user", "key": user_key}
    try:
        result = await support.invoke(question, context, variables={"question": question})
    except Exception as error:  # throttling or an expired AWS session; the SDK has already tracked the error
        return Reply(time.time(), "error", None, "", f"{type(error).__name__}: {error}"[:200])
    judged = (result.judge_results or {}).get(JUDGE_KEY)
    return Reply(time.time(), result.track_data.get("variationKey", "?"), judged.score if judged else None,
                 str(result.response))


if RUN["preview"]:
    for key, user in PREVIEW_USERS.items():
        print(f"===== {VARIATIONS[key][0]} =====")
        for question in QUESTIONS[:PREVIEW_QUESTIONS]:
            reply = await ask(question, user)
            print(f"Q: {question}\nA: {reply.answer or reply.error}\n   judge score: {reply.score}\n")

# %% [markdown]
# The judge score is the guarded metric. Baseline should score near 1 and Confident v2 near 0.

# %% [markdown]
# ## Step 3: Send baseline traffic
#
# With the default rule serving Baseline to everyone, send some traffic first. LaunchDarkly's health check for a
# guarded rollout waits until it has seen the metric's events, and this gives it a baseline to compare against.
#
# Every request uses a new user key, so every request is a new context in the rollout's analysis.

# %%
replies: list[Reply] = []


async def send_traffic(stop: asyncio.Event, limit: int | None = None) -> None:
    """Run CONCURRENCY workers that each ask random questions as new users until `stop` is set or `limit` is hit."""
    remaining = [limit]

    async def worker() -> None:
        while not stop.is_set() and (remaining[0] is None or remaining[0] > 0):
            if remaining[0] is not None:
                remaining[0] -= 1  # claim a request before awaiting, so workers don't overshoot the limit
            replies.append(await ask(random.choice(QUESTIONS), f"demo-user-{uuid.uuid4().hex[:12]}"))

    await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))


def summarize(batch: list[Reply]) -> None:
    by_variation: dict[str, list[Reply]] = {}
    for reply in batch:
        by_variation.setdefault(reply.variation, []).append(reply)
    for variation, items in sorted(by_variation.items()):
        scores = [r.score for r in items if r.score is not None]
        mean = f"{sum(scores) / len(scores):.2f}" if scores else "n/a"
        print(f"  {variation:13s} requests={len(items):4d}  judged={len(scores):4d}  mean score={mean}")
    errors = [r.error for r in batch if r.error]
    if errors:
        print(f"  first error: {errors[0]}")


if RUN["warmup"]:
    before = len(replies)
    await send_traffic(asyncio.Event(), limit=WARMUP_REQUESTS)
    get_client().flush()
    print(f"sent {len(replies) - before} requests")
    summarize(replies[before:])

# %% [markdown]
# ## Step 4: Start the guarded rollout
#
# The rollout runs on the default rule. Each stage serves Confident v2 to its share of traffic and an equal share of
# Baseline as the control. LaunchDarkly compares the metric between those two groups only, and the remaining traffic
# stays on Baseline outside the analysis. With **automatic rollback** on, a significant regression in the metric
# reverts the rule to Baseline.
#
# The public API docs don't list the guarded rollout instructions yet. These are the instructions the LaunchDarkly
# UI sends:
#
# | Instruction | What it does |
# |---|---|
# | `updateFallthroughWithMeasuredRolloutV2` | Starts a guarded rollout on the default rule |
# | `stopAutomatedRelease` with `finalizationBehavior: "rollBackAllPhases"` | Stops it and serves the control variation |
#
# Stage weights are in thousandths of a percent, and monitoring windows are in milliseconds.

# %%
def rollout_status() -> tuple[str, str]:
    """('running', '20%') during a rollout, or ('serving', '<variation key>') when the rule serves one variation."""
    target = targeting()
    keys = list(variation_ids(target))
    fallthrough = target["environments"][ENVIRONMENT]["fallthrough"]
    rollout = fallthrough.get("rollout") or {}
    if rollout.get("experimentAllocation", {}).get("type") == "measuredRollout":
        weight = sum(w["weight"] for w in rollout["variations"]
                     if not w.get("_untracked") and keys[w["variation"]] == "confident-v2")
        return "running", f"{weight / 1000:g}%"
    if "variation" in fallthrough:
        return "serving", keys[fallthrough["variation"]]
    return "other", json.dumps(fallthrough)


def stop_rollout(comment: str) -> None:
    if rollout_status()[0] == "running":
        status, body = patch_targeting(
            [{"kind": "stopAutomatedRelease", "finalizationBehavior": "rollBackAllPhases"}], comment)
        print(f"stopped the running rollout: HTTP {status}" + ("" if status == 200 else f" {body}"))


def start_rollout() -> None:
    stop_rollout("Stop the previous demo rollout before starting a new one")
    status, body = patch_targeting([
        {
            "kind": "updateFallthroughWithMeasuredRolloutV2",
            "testVariationId": IDS["confident-v2"],
            "controlVariationId": IDS["baseline"],
            "randomizationUnit": "user",
            "onRegression": {"notify": True, "rollback": True},
            "metrics": [{"metricKey": METRIC_KEY, "onRegression": {"notify": True, "rollback": True}}],
            "stages": [{"rolloutWeight": percent * 1000, "monitoringWindowMilliseconds": minutes * 60_000}
                       for percent, minutes in STAGES],
        },
    ], "Guarded rollout of Confident v2")
    if status != 200:
        raise RuntimeError(f"starting the guarded rollout failed: HTTP {status} {body}")
    print("guarded rollout started:", " -> ".join(f"{p}% for {m} min" for p, m in STAGES))


TARGETING_PAGE = f"{os.getenv('LD_UI_BASE_URI', API_HOST).rstrip('/')}{PROJECT}/ai-configs/{CONFIG_KEY}/targeting?env={ENVIRONMENT}"
if RUN["rollout"]:
    start_rollout()
    print(f"watch it in LaunchDarkly: {TARGETING_PAGE}")

# %% [markdown]
# ## Step 5: Send traffic and watch the rollout
#
# This cell sends traffic and checks the rollout's state every 20 seconds, printing each change. It stops when the
# rule serves a single variation again, after a rollback or when the rollout completes, or after `MAX_RUN_MINUTES`.
#
# While it runs, open the targeting page printed above. The guarded rollout's metric chart shows the difference
# between the two variations as the data comes in.

# %%
async def watch(stop: asyncio.Event) -> tuple[str, str]:
    deadline = time.monotonic() + MAX_RUN_MINUTES * 60
    last = None
    while time.monotonic() < deadline:
        state = await asyncio.to_thread(rollout_status)
        if state != last:
            served = [r for r in replies if r.variation == "confident-v2"]
            print(f"{datetime.now():%H:%M:%S}  {state[0]:8s} {state[1]:13s} requests so far={len(replies):5d}  "
                  f"on Confident v2={len(served)}")
            last = state
        if state[0] != "running":
            break
        await asyncio.sleep(20)
    stop.set()
    return last


if RUN["rollout"]:
    rollout_started_at = len(replies)
    stop = asyncio.Event()
    final_state, _ = await asyncio.gather(watch(stop), send_traffic(stop))
    get_client().flush()
    print(f"\nfinal state: {final_state}")
    summarize(replies[rollout_started_at:])

# %% [markdown]
# ## Step 6: What LaunchDarkly did
#
# When the default rule serves Baseline again after `running`, LaunchDarkly has rolled the rollout back. The reason,
# and the metric chart behind it, are on the AI Config's **Monitoring** tab. The public API doesn't return them, and
# the audit log has no entry for an automatic rollback.

# %%
print(f"targeting page: {TARGETING_PAGE}")

# %% [markdown]
# The chart shows each answer's judge score over the run, by variation, with a rolling mean. The baseline traffic
# from Step 3 is on the left.

# %%
import matplotlib.pyplot as plt

if replies:
    start = replies[0].at
    fig, ax = plt.subplots(figsize=(10, 4))
    for key, color, marker in (("baseline", "#2a78d6", "o"), ("confident-v2", "#eb6834", "s")):
        points = [((r.at - start) / 60, r.score) for r in replies if r.variation == key and r.score is not None]
        if not points:
            continue
        x, y = zip(*points)
        ax.scatter(x, y, s=12, color=color, marker=marker, alpha=0.25, linewidths=0)
        window = 15
        rolling = [sum(y[max(0, i - window + 1):i + 1]) / len(y[max(0, i - window + 1):i + 1]) for i in range(len(y))]
        ax.plot(x, rolling, color=color, linewidth=2, label=VARIATIONS[key][0])
        ax.annotate(VARIATIONS[key][0], (x[-1], rolling[-1]), xytext=(6, 0), textcoords="offset points",
                    va="center", color="#333333")
    ax.set_xlabel("minutes since the first request")
    ax.set_ylabel("judge score")
    ax.set_ylim(-0.05, 1.05)
    ax.grid(axis="y", color="#e6e6e3")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, loc="lower left")
    ax.set_title("Answer accuracy by variation (rolling mean of 15)", loc="left")
    plt.show()

# %% [markdown]
# ## Reset
#
# Stops any running rollout and serves Baseline to everyone, so the demo can run again. A new rollout assigns users
# afresh, so a rerun doesn't depend on this one.

# %%
stop_rollout("Reset the guarded rollout demo")
patch_targeting([{"kind": "updateFallthroughVariationOrRollout", "variationId": IDS["baseline"]}],
                "Reset the guarded rollout demo")
print(f"default rule: {rollout_status()}")

# %% [markdown]
# ## What to know
#
# - **The guarded rollout instructions aren't in the public API docs.** `updateFallthroughWithMeasuredRolloutV2`
#   rejects `regressionThreshold`. While a rollout runs, the rule can't be changed with
#   `updateFallthroughVariationOrRollout`; stop it with `stopAutomatedRelease` first.
# - **There's no public endpoint for rollout status.** This notebook reads it from the default rule: a
#   `measuredRollout` allocation while it runs, and a single variation once it's rolled back or complete. The reason
#   for a rollback is shown only in the UI.
# - **Each step must last at least 1 minute**, and can serve the new variation to at most 50% of traffic.
# - **Only the analysed traffic counts.** At a 20% step, 20% of requests get Confident v2 and 20% get Baseline as the
#   control. The other 60% aren't in the comparison.
# - **Judges are attached to variations**, and the update only reaches SDKs when it creates a new variation version.
# - **`max_tokens`** is served as `maxTokens` for catalog model configs, which Bedrock rejects through LangChain.

# %%
get_client().close()
