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
# # Offline evals from code with the LaunchDarkly Python AI SDK: ToggleBank on staging
#
# This notebook follows the *Offline Evals from Code* onboarding guide step by step, in code, against
# the ToggleBank policy agent's LaunchDarkly **staging** project (`policy-agent`).
#
# One call to `evals.run()` does two phases:
#
# 1. **Generation.** The SDK fetches the dataset rows from LaunchDarkly and calls your handler once per row.
# 2. **Evaluation.** The SDK scores each output with the criteria you pass: LaunchDarkly `Judge`s and local `Scorer`s.
#
# Every result goes to LaunchDarkly as an SDK event. LaunchDarkly compares each score with its threshold and
# rules on the row and run verdicts. The SDK never computes a verdict itself.
#
# Every run counts against the project's **daily LaunchDarkly token limit**. Use the switches in **Settings** to run
# only what you need, and the 2-row quick dataset for practice runs.

# %% [markdown]
# ## Before you run it: set up ToggleBank on staging
#
# Set the app up from **this repo** (`policy_agent`, on the branch with staging support). Pointing at staging
# depends on changes that live only here: the Terraform `api_host` setting, the SDK endpoint variables, the
# per-instance Terraform workspace (`TF_WORKSPACE`), and `ENV_FILE` for Docker Compose. A copy of the app without
# them provisions and runs against production.
#
# Run everything from the repo root.
#
# **You'll need:** Docker with Compose v2 (e.g. Colima), `uv`, the AWS CLI with an SSO profile that can call
# Bedrock in `us-east-1` (with the models in the README enabled), an OpenAI API key, and a LaunchDarkly staging
# account with a **Writer** API token.
#
# The `<launchdarkly-host>`, `<stream-host>` and `<events-host>` values below are placeholders: LaunchDarkly staff can
# get the staging values from the evals team.
#
# 1. **Create the project on staging.** Use key `policy-agent` (or your own), then copy the **Test** environment's
#    server-side SDK key (`sdk-…`) from the project's environment settings.
#
# 2. **Create `.env.staging`** from the example, then fill it in:
#    ```sh
#    cp .env.example .env.staging
#    ```
#    ```dotenv
#    LAUNCHDARKLY_SDK_KEY=sdk-...            # Test environment of the staging project
#    LAUNCHDARKLY_PROJECT_KEY=policy-agent
#    LAUNCHDARKLY_ACCESS_TOKEN=api-...       # staging Writer token
#    LAUNCHDARKLY_ENVIRONMENT=test
#    LAUNCHDARKLY_API_HOST=https://<launchdarkly-host>
#    LAUNCHDARKLY_STREAM_URI=https://<stream-host>
#    LAUNCHDARKLY_EVENTS_URI=https://<events-host>
#    ENV_FILE=./.env.staging                 # the file Compose mounts into the backend
#    TF_WORKSPACE=staging                    # keeps staging's Terraform state apart from production's
#    AWS_PROFILE=<your SSO profile>
#    OPENAI_API_KEY=sk-...                   # used by this notebook's OpenAI sections and judges
#    ```
#    `.env.*` files are gitignored, so don't commit this one.
#
# 3. **Provision the agents, model configs, tools and agent graph** (41 resources) with Terraform. The first
#    `init` creates the `staging` workspace from `TF_WORKSPACE`. The plan should show only additions.
#    ```sh
#    docker compose --env-file .env.staging run --rm terraform init
#    docker compose --env-file .env.staging run --rm terraform plan
#    docker compose --env-file .env.staging run --rm terraform apply
#    ```
#
# 4. **Point each AI Config at its `Default` variation.** On staging this is usually already the case, and the
#    script then just reports `ok`:
#    ```sh
#    python3 scripts/set_ai_config_targeting.py --env-file .env.staging
#    ```
#
# 5. **Start the app against staging.** It needs a current AWS session for Bedrock:
#    ```sh
#    aws sso login --profile <your SSO profile>
#    docker compose --env-file .env.staging up -d --build
#    curl http://localhost:8000/health
#    ```
#    The UI is at http://localhost:5173. Switch back to production any time with plain `docker compose up -d`.
#
# 6. **Upload the dataset** in the staging UI (Agents → Library → Datasets → New dataset → Upload dataset):
#    `evals/policy_agent_dataset.jsonl` with the key `policy_agent_dataset`, and optionally
#    `evals/policy_agent_dataset_quick.jsonl` with the key `policy_agent_dataset_quick`. The API can't create
#    datasets, so this is the one manual step.
#
# 7. **Install the notebook's dependencies** and open it:
#    ```sh
#    cd evals && uv sync --all-groups
#    uv run jupytext --sync offline_evals_walkthrough.py
#    uv run jupyter lab offline_evals_walkthrough.ipynb
#    ```
#
# 8. **First run only:** set `CREATE_MISSING_ASSETS = True` in **Settings**. Step 1 then creates what Terraform
#    doesn't: the `find_branch` tool and the `offline-answer-quality` and `tool-trajectory-relevance` judges.
#
# The Bedrock section and the live-agent bonus also need a valid AWS session, and the bonus needs the app running.
# Both skip themselves when those are missing.

# %% [markdown]
# ## Settings
#
# - **`RUN`** turns each section on or off.
# - **`QUICK`** switches to the 2-row dataset `evals/policy_agent_dataset_quick.jsonl`, uploaded with the key `policy_agent_dataset_quick`.
# - **`CREATE_MISSING_ASSETS`** lets Step 1 create the `find_branch` tool and the two OpenAI judges in the project
#   when they're missing. Off by default, because these live outside Terraform.

# %%
RUN = {
    "first_eval": True,
    "smoke": True,
    "judges_and_scorers": True,
    "tool_trajectory": True,
    "bedrock": True,
    "live_agent": True,
}
QUICK = False
CREATE_MISSING_ASSETS = False

# %% [markdown]
# ## Setup
#
# The app's `.env.staging` uses `LAUNCHDARKLY_*` names, and the SDK reads `LD_*` names, so the next cell maps one to
# the other. Model names, keys and thresholds live in the notebook, and only secrets come from the env file.

# %%
import asyncio
import json
import logging
import os
import re
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
        if (path / "docker-compose.yml").is_file() and (path / "evals").is_dir():
            return path
    raise FileNotFoundError("Run the notebook from inside the policy_agent repo")


ENV_FILE = Path(os.getenv("POLICY_AGENT_ENV_FILE") or repo_root() / ".env.staging")
load_dotenv(ENV_FILE)
for target, source in {
    "LD_API_TOKEN": "LAUNCHDARKLY_ACCESS_TOKEN",
    "LD_SDK_KEY": "LAUNCHDARKLY_SDK_KEY",
    "LD_API_BASE_URI": "LAUNCHDARKLY_API_HOST",
    "LD_UI_BASE_URI": "LAUNCHDARKLY_API_HOST",
    "LD_STREAM_URI": "LAUNCHDARKLY_STREAM_URI",
    "LD_EVENTS_URI": "LAUNCHDARKLY_EVENTS_URI",
}.items():
    if not os.getenv(target) and os.getenv(source):
        os.environ[target] = os.environ[source]

PROJECT_KEY = os.environ["LAUNCHDARKLY_PROJECT_KEY"]  # policy-agent
DATASET_KEY = "policy_agent_dataset_quick" if QUICK else "policy_agent_dataset"
OPENAI_MODEL = "gpt-4o"
BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
ANSWER_JUDGE = "offline-answer-quality"  # OpenAI judge
TRAJECTORY_JUDGE = "tool-trajectory-relevance"  # OpenAI judge
BEDROCK_JUDGE = "ai-judge-accuracy"  # the project's own Terraform-provisioned Bedrock judge
TOOL_KEY = "find_branch"
INSTRUCTIONS = "You are ToggleBank's customer support assistant. Answer accurately and concisely."

# The notebook reuses one LaunchDarkly client for every run, so the SDK's "client already initialized" notice
# would repeat on each run. Same project, same SDK key, so it's safe to hide.
logging.getLogger("launchdarkly_ai_server.evaluations.module").addFilter(
    lambda record: "already initialized" not in record.getMessage()
)


def unique(prefix: str) -> str:
    """Every run() creates a new evaluation, so each key must be unique."""
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}"


def report(result) -> None:
    print(f"verdict : {'PASSED' if result.passed else 'FAILED'}")
    print(f"summary : {result.summary}")
    print(f"run page: {result.url}")


print(f"project={PROJECT_KEY}  api={os.environ['LD_API_BASE_URI']}  dataset={DATASET_KEY}")

# %% [markdown]
# ## Step 1: Check the assets in LaunchDarkly
#
# The guide creates these in the UI. Here they're checked through the REST API. The **dataset** has no create
# endpoint at all, and `GET /datasets/{key}` works even though it isn't in the public spec.

# %%
API = os.environ["LD_API_BASE_URI"].rstrip("/") + f"/api/v2/projects/{PROJECT_KEY}"


def ld_api(method: str, path: str, body: Any = None, version: str = "beta") -> tuple[int, Any]:
    request = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": os.environ["LD_API_TOKEN"],
            "LD-API-Version": version,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, None


status, _ = ld_api("GET", f"/datasets/{DATASET_KEY}", version="20240415")
if status != 200:
    source = "policy_agent_dataset_quick.jsonl" if QUICK else "policy_agent_dataset.jsonl"
    raise SystemExit(
        f"Dataset {DATASET_KEY!r} not found (HTTP {status}). Upload evals/{source} in the staging UI "
        f"(Agents > Library > Datasets) with that key, then rerun."
    )
_, rows = ld_api("GET", f"/datasets/{DATASET_KEY}/rows?mode=all&limit=100&offset=0", version="20240415")


def render(template: str, variables: Mapping[str, Any]) -> str:
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: str(variables.get(m.group(1), m.group(0))), template)


# Rendered inputs, used to tell dataset rows apart from judge calls when recording outputs below.
QUESTIONS = [render(row["input"], row.get("variables") or {}) for row in rows["items"]]
print(f"dataset {DATASET_KEY}: {rows['totalCount']} rows")
for index, question in enumerate(QUESTIONS):
    print(f"  [{index}] {question}")

# %% [markdown]
# Terraform provisions the agents and the Bedrock judges. The notebook also needs a `find_branch` tool and two
# OpenAI judges. With `CREATE_MISSING_ASSETS = True`, missing ones are created through the REST API, with the trajectory
# judge using the guide's rubric.

# %%
TOOL_SCHEMA = {
    "type": "object",
    "properties": {"location": {"type": "string", "description": "ZIP code or neighborhood"}},
    "required": ["location"],
}
TRAJECTORY_RUBRIC = """You are grading an assistant's TOOL USE, not its prose.

The message history shows the user's request, the tools the assistant
could call, the calls it actually made with their arguments and each
result or error, and its final answer.

Score 1.0 when the tool use was right for the request. That INCLUDES
calling no tool at all when the request did not need one: an available
tool left unused is not a mistake, and a request that needs no tool
must not be penalised for having none.

Score 0.0 when the assistant needed a tool and did not call it, called
the wrong tool, passed arguments the request does not support, ignored
what a tool returned, or repeated a failing call without adapting.

Score in between for partial credit."""


def ensure_tool() -> bool:
    if ld_api("GET", f"/ai-tools/{TOOL_KEY}")[0] == 200:
        return True
    if not CREATE_MISSING_ASSETS:
        print(f"tool {TOOL_KEY} missing: set CREATE_MISSING_ASSETS = True or create it in the Library")
        return False
    status, _ = ld_api("POST", "/ai-tools", {
        "key": TOOL_KEY, "schema": TOOL_SCHEMA,
        "description": "Finds ToggleBank branches and ATMs near a ZIP code or neighborhood, with hours.",
    })
    print(f"created tool {TOOL_KEY}: HTTP {status}")
    return status == 201


ANSWER_RUBRIC = (
    "You are grading a bank's customer-support assistant (ToggleBank). Using the message history, score the "
    "RESPONSE on whether it (1) addresses what the customer actually asked, (2) does not invent account data such "
    "as balances, fees, rates or transactions that it was not given, and (3) gives a clear, safe next step when it "
    "cannot answer directly (for example, offering to connect the customer with a person for fraud or account "
    "changes). Score 1.0 when all three hold, 0.0 when it fabricates account data or ignores the request, and in "
    "between for partially helpful answers."
)


def ensure_judge(key: str, name: str, rubric: str) -> bool:
    """An OpenAI judge config with the standard judge messages around `rubric`."""
    if ld_api("GET", f"/ai-configs/{key}")[0] == 200:
        return True
    if not CREATE_MISSING_ASSETS:
        print(f"judge {key} missing: set CREATE_MISSING_ASSETS = True or create it as a judge config")
        return False
    ld_api("POST", "/ai-configs", {
        "key": key, "name": name, "mode": "judge",
        "isInverted": False, "evaluationMetricKey": f"$ld:ai:judge:{key}",
    })
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


answer_judge_ready = ensure_judge(ANSWER_JUDGE, "Offline answer quality", ANSWER_RUBRIC)
trajectory_assets_ready = ensure_tool() & ensure_judge(TRAJECTORY_JUDGE, "Tool trajectory relevance", TRAJECTORY_RUBRIC)
for key in (ANSWER_JUDGE, TRAJECTORY_JUDGE, BEDROCK_JUDGE):
    status, config = ld_api("GET", f"/ai-configs/{key}")
    variation = (config or {}).get("variations", [{}])[-1]
    print(f"judge {key:28s} HTTP {status}  model={variation.get('modelConfigKey')}")

# %% [markdown]
# ## Step 2: Credentials
#
# The SDK uses two channels, and needs both:
#
# | Credential | Variable | What it does |
# |---|---|---|
# | API access token | `LD_API_TOKEN` | Reads the dataset, tools and judges. Creates the evaluation and run. Polls the summary. |
# | SDK key | `LD_SDK_KEY` | Sends every generation and score to LaunchDarkly as an event. Results reach LaunchDarkly only this way. |
#
# Without the SDK key nothing is recorded and every run times out, so `init_evaluations()` refuses to start without it.

# %%
for name in ("LD_API_TOKEN", "LD_SDK_KEY", "OPENAI_API_KEY", "AWS_PROFILE"):
    value = os.getenv(name, "")
    print(f"{name:15s} {'set' if value else 'MISSING'}")  # never print any part of a credential

# %% [markdown]
# ## Step 3: Install
#
# This folder pins the SDK to `python-ai-sdk` `main`, because the tool-trajectory capture isn't on PyPI yet:
#
# ```sh
# cd evals && uv sync --all-groups
# uv run jupytext --sync offline_evals_walkthrough.py   # creates the paired .ipynb
# uv run jupyter lab offline_evals_walkthrough.ipynb
# ```

# %%
from launchdarkly_ai_openai_messages import create_openai_messages_handler
from launchdarkly_ai_server import DatasetRow, Judge, ProviderHandler, Scorer, init_evaluations, shutdown

evals = init_evaluations()  # reads LD_API_TOKEN, LD_SDK_KEY and the staging endpoints

# %% [markdown]
# ### Seeing results in the notebook
#
# `run()` returns only the verdict and totals. Per-row scores and judge reasoning are visible only on the run page.
# To show *something* per row here, `RunLog` wraps the handler to record each answer and wraps each scorer to record
# its score. LaunchDarkly judges still score on LaunchDarkly's side, so their scores aren't shown.

# %%
class RunLog:
    """Records each row's answer and local scorer scores for one run."""

    def __init__(self) -> None:
        self.answers: dict[str, str] = {}
        self.scores: dict[str, dict[str, float]] = {}
        self.notes: dict[str, str] = {}

    def handler(self, inner: Any) -> Any:
        """Wrap a handler, keeping its provider metadata so it still serves judges."""
        async def recorded(config, user_input=None, tool_handlers=None, variables=None, history=None):
            if isinstance(inner, ProviderHandler):
                result = await inner(config, user_input, tool_handlers, variables, history)
            else:
                result = await inner(config, user_input, tool_handlers, variables)
            if user_input in QUESTIONS:  # a dataset row, not a judge call
                output = result.get("output") if isinstance(result, Mapping) else getattr(result, "output", None)
                self.answers[user_input] = str(output or "")
            return result

        if isinstance(inner, ProviderHandler):
            return ProviderHandler(recorded, provides_for=inner.provides_for, capture_content=inner.capture_content)
        return recorded

    def scorer(self, name: str, fn: Callable[[DatasetRow, str | None], float], **settings: Any) -> Scorer:
        def recorded(row: DatasetRow, output: str | None) -> float:
            score = fn(row, output)
            self.scores.setdefault(row.input or "", {})[name] = score
            return score

        return Scorer(name=name, fn=recorded, **settings)

    def table(self) -> None:
        names = sorted({name for scores in self.scores.values() for name in scores})
        print(f"{'#':>2}  {'question':42s}  {'answer':50s}  " + "  ".join(names))
        for index, question in enumerate(QUESTIONS):
            answer = " ".join(self.answers.get(question, "—").split())
            scores = self.scores.get(question, {})
            cells = "  ".join(f"{scores.get(name, float('nan')):^{len(name)}.2f}" for name in names)
            note = f"  {self.notes[question]}" if question in self.notes else ""
            print(f"{index:>2}  {question[:42]:42s}  {answer[:50]:50s}  {cells}{note}")

# %% [markdown]
# ## Step 4: Run your first eval
#
# A generation-only run: every row goes to OpenAI, and with no criteria every row that produces an output passes.

# %% jupyter={"outputs_hidden": true}
if RUN["first_eval"]:
    log = RunLog()
    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("first-eval"),
        dataset=DATASET_KEY,
        handler=log.handler(create_openai_messages_handler()),
        generation={
            "provider": "OpenAI",
            "model": OPENAI_MODEL,
            "parameters": {"temperature": 0},
            "instructions": INSTRUCTIONS,
        },
        concurrency=5,
    )
    log.table()
    report(result)

# %% [markdown]
# ### Custom handlers
#
# A handler is any async function that returns `{"output": ...}`. This one calls no model, so it tests credentials,
# the dataset and the event path at no provider cost.

# %%
async def deterministic_handler(
    config: dict[str, Any],
    user_input: str | None = None,
    tool_handlers: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"output": f"smoke response: {user_input or ''}"}


if RUN["smoke"]:
    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("smoke"),
        dataset=DATASET_KEY,
        handler=deterministic_handler,
        generation={"provider": "custom", "model": "deterministic-smoke-handler",
                    "instructions": "Return a deterministic smoke-test response."},
        concurrency=2,
    )
    report(result)

# %% [markdown]
# ## Step 5: Score rows with deterministic scorers and LaunchDarkly judges
#
# - **`Scorer`**: a local function `(row, output) -> float` from 0 to 1. Its `threshold` defaults to `1.0`.
# - **`Judge`**: a judge config in LaunchDarkly, referenced by key. Its `threshold` defaults to `0.5`. Its success
#   direction (`isInverted`) is set on the config, not in code.
# - **`pass_rate_threshold`**: the share of rows that must pass for the run to pass. LaunchDarkly stores and shows it,
#   but today's run verdict still requires *every* row to pass.
#
# Two ToggleBank-specific scorers: the answer isn't empty, and an account answer doesn't make up a dollar amount
# (the agent has no real balance data).

# %%
def score_nonempty(row: DatasetRow, output: str | None) -> float:
    return 1.0 if output and output.strip() else 0.0


def score_no_invented_amounts(row: DatasetRow, output: str | None) -> float:
    """1.0 unless an account question's answer quotes a dollar figure it couldn't know."""
    if (row.metadata or {}).get("expected_specialist") != "account_specialist":
        return 1.0
    return 0.0 if re.search(r"\$\s?\d", output or "") else 1.0


if RUN["judges_and_scorers"] and not answer_judge_ready:
    print(f"Skipping: the {ANSWER_JUDGE} judge is missing (see Step 1).")
elif RUN["judges_and_scorers"]:
    log = RunLog()
    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("judges-and-scorers"),
        dataset=DATASET_KEY,
        handler=log.handler(create_openai_messages_handler()),
        generation={"provider": "OpenAI", "model": OPENAI_MODEL, "parameters": {"temperature": 0},
                    "instructions": INSTRUCTIONS},
        criteria=[
            Judge(key=ANSWER_JUDGE, threshold=0.7, pass_rate_threshold=0.9),
            log.scorer("nonempty-output", score_nonempty),
            log.scorer("no-invented-amounts", score_no_invented_amounts),
        ],
        concurrency=5,
    )
    log.table()
    report(result)
    failed, total = result.summary.failed_rows, result.summary.total_rows
    if failed and (total - failed) / total >= 0.9:
        print("pass rate meets pass_rate_threshold=0.9, but the run still fails: today's verdict needs every row to pass")
    elif failed:
        print(f"pass rate {(total - failed) / total:.0%} is below pass_rate_threshold=0.9; today's verdict "
              "needs every row to pass anyway")
    print(f"judge {ANSWER_JUDGE} scores and reasoning for each row: open the run page")

# %% [markdown]
# Judge keys and scorer names share one namespace within a run, so they must not collide.

# %% [markdown]
# ## Step 6: Judge the tool trajectory
#
# The SDK records every tool call a row makes and renders it into the judge's `{{message_history}}`. That lets the
# rubric grade *how* the agent answered. The branch rows need `find_branch`, and the others should call nothing,
# which is why the rubric's "an unused tool is not a mistake" clause matters.

# %%
BRANCHES = {
    "94105": {"name": "Financial District", "address": "101 Market St, San Francisco",
              "hours": "Mon-Fri 9am-5pm, Sat 10am-2pm", "atm": "24/7"},
    "downtown oakland": {"name": "Broadway ATM", "address": "1400 Broadway, Oakland",
                         "hours": "ATM only", "atm": "24/7, including weekends"},
}


def make_find_branch(calls: list[str]) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Local implementation of the find_branch Library tool, recording each call."""
    def find_branch(arguments: Mapping[str, Any]) -> dict[str, Any]:
        location = str(arguments.get("location") or "").strip()
        if not location:
            raise ValueError("find_branch requires a location")
        calls.append(location)
        match = next((b for key, b in BRANCHES.items() if key in location.lower()), None)
        return match or {"status": "not_found", "location": location}

    return find_branch


if RUN["tool_trajectory"] and not trajectory_assets_ready:
    print("Skipping: the tool or trajectory judge is missing (see Step 1).")
elif RUN["tool_trajectory"]:
    log, tool_calls = RunLog(), []
    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("tool-trajectory"),
        dataset=DATASET_KEY,
        handler=log.handler(create_openai_messages_handler()),
        tools={TOOL_KEY: make_find_branch(tool_calls)},
        generation={
            "provider": "OpenAI", "model": OPENAI_MODEL, "parameters": {"temperature": 0},
            "instructions": INSTRUCTIONS + f" Use the {TOOL_KEY} tool for branch or ATM questions.",
        },
        criteria=[Judge(key=TRAJECTORY_JUDGE, threshold=0.8)],
        concurrency=2,
    )
    log.table()
    print(f"find_branch called for: {tool_calls}")
    report(result)

# %% [markdown]
# Scorers can't see the trajectory. `Scorer.fn` gets only the row and the output, so a check like "called
# `find_branch` exactly once" needs a judge. Here, the `tool_calls` list only shows the calls in the notebook.

# %% [markdown]
# ## Step 7: Generate and judge on Amazon Bedrock through LangChain
#
# The LangChain handler serves every provider. Generation runs on Bedrock, and the project's own **Terraform-provisioned
# Bedrock judge** `ai-judge-accuracy` grades the answers through the same handler, so no `judge_handlers` or OpenAI key is needed.
# The handler builds `ChatBedrockConverse` from the standard AWS credential chain, here `AWS_PROFILE` from the
# env file. Skipped when there's no valid AWS session.

# %%
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from launchdarkly_ai_langchain_messages import create_langchain_messages_handler

try:
    boto3.Session(profile_name=os.getenv("AWS_PROFILE")).client("sts").get_caller_identity()
    aws_ready = True
except (BotoCoreError, ClientError) as error:
    aws_ready = False
    print(f"No valid AWS session ({type(error).__name__}): Bedrock and the live agent are skipped. "
          "Run `aws sso login` and rerun.")

if RUN["bedrock"] and aws_ready:
    log = RunLog()
    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("bedrock-judges"),
        dataset=DATASET_KEY,
        handler=log.handler(create_langchain_messages_handler()),
        generation={"provider": "Bedrock", "model": BEDROCK_MODEL, "parameters": {"temperature": 0},
                    "instructions": INSTRUCTIONS},
        criteria=[Judge(key=BEDROCK_JUDGE), log.scorer("nonempty-output", score_nonempty)],
        concurrency=2,  # new Bedrock accounts throttle at low request rates
    )
    log.table()
    report(result)

# %% [markdown]
# ## Bonus: evaluate the real ToggleBank agent
#
# A custom handler can call *anything*, including the running app. This one sends each row to the backend's
# `/api/chat`, so the real triage → specialist → brand-voice flow answers. A scorer then checks that triage routed
# the question to the specialist named in the row's `metadata`. Skipped when the backend isn't running, or when
# there's no AWS session, since the agent calls Bedrock with it.

# %%
AGENT_URL = os.getenv("POLICY_AGENT_URL", "http://localhost:8000")


def make_policy_agent_handler(routes: dict[str, list[str]]) -> Callable[..., Any]:
    async def policy_agent_handler(config, user_input=None, tool_handlers=None, variables=None):
        def post() -> dict[str, Any]:
            body = json.dumps({"userInput": user_input or "", "userName": "Demo User"}).encode()
            request = urllib.request.Request(f"{AGENT_URL}/api/chat", data=body,
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read())

        reply = await asyncio.to_thread(post)
        if reply.get("error"):  # surfaces as an ERROR row rather than an empty answer
            raise RuntimeError(f"policy agent error: {reply['error']}")
        routes[user_input or ""] = [step.get("agent", "") for step in reply.get("agentFlow") or []]
        return {"output": reply.get("response") or ""}

    return policy_agent_handler


def agent_reachable() -> bool:
    try:
        urllib.request.urlopen(f"{AGENT_URL}/health", timeout=5)
        return True
    except OSError:
        print(f"Skipping: the policy agent isn't reachable at {AGENT_URL}.")
        return False


if RUN["live_agent"] and not answer_judge_ready:
    print(f"Skipping: the {ANSWER_JUDGE} judge is missing (see Step 1).")
elif RUN["live_agent"] and aws_ready and agent_reachable():
    log, routes = RunLog(), {}

    def score_routing(row: DatasetRow, output: str | None) -> float:
        expected = (row.metadata or {}).get("expected_specialist")
        return 1.0 if expected in routes.get(row.input or "", []) else 0.0

    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("policy-agent-live"),
        dataset=DATASET_KEY,
        handler=log.handler(make_policy_agent_handler(routes)),
        judge_handlers=[create_openai_messages_handler()],  # the main handler can't serve the OpenAI judge
        generation={"provider": "custom", "model": "policy-agent-api",
                    "instructions": "Generated by the running ToggleBank agent (/api/chat)."},
        criteria=[Judge(key=ANSWER_JUDGE, threshold=0.7),
                  log.scorer("routed-to-expected-specialist", score_routing)],
        concurrency=2,
        poll_timeout_seconds=600,
    )
    log.notes = {question: " -> ".join(route) for question, route in routes.items()}
    log.table()
    report(result)

# %% [markdown]
# ## What to know
#
# - **Datasets** can only be created in the UI. The API can read one by key, but can't create, list or change one.
# - **Evaluations and runs** can be created from code, but not listed, updated or deleted over the API. Every `run()`
#   adds a new evaluation.
# - **Scores and judge reasoning** are visible only on the run page. `run()` returns the verdict and totals.
# - **Daily token limit:** runs count against a per-project LaunchDarkly limit. When it's reached, `run()` fails with
#   HTTP 429 `token_limit_exceeded`, after the evaluation has already been created.
# - **Coming soon** (per the guide): inline datasets and tools, skills, mixed providers, and running Playgrounds from the SDK.

# %%
await shutdown()
