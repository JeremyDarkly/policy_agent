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
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Offline evals from code with the LaunchDarkly Python AI SDK
#
# This notebook follows the *Offline Evals from Code* guide step by step, in code, against your own LaunchDarkly
# project. The examples use a sample bank support assistant (ToggleBank), but every key is a setting you can change.
# For the full API reference, see [`launchdarkly-ai-python` on PyPI](https://pypi.org/project/launchdarkly-ai-python/).
#
# One call to `evals.run()` does two phases:
#
# 1. **Generation.** The SDK fetches the dataset rows from LaunchDarkly and calls your handler once per row.
# 2. **Evaluation.** The SDK scores each output with the criteria you pass: LaunchDarkly `Judge`s and local `Scorer`s.
#
# Every result goes to LaunchDarkly as an SDK event. LaunchDarkly compares each score with its threshold and
# rules on the row and run verdicts. The SDK never computes a verdict itself.
#
# Every run counts against the project's **daily LaunchDarkly token limit**. Use the `RUN` switches in **Settings**
# to run only what you need.

# %% [markdown]
# ## Before you start
#
# Create these in LaunchDarkly first. The notebook uses them by key, and doesn't create or check them.
#
# 1. **Credentials.** An API access token with the **Writer** role, and the server-side SDK key (`sdk-…`) of the
#    environment you'll record results in.
# 2. **A dataset.** Upload it in **Agents → Library → Datasets → New dataset → Upload dataset**. To use the sample,
#    upload `policy_agent_dataset.jsonl` from this folder. Datasets can only be created in the UI.
# 3. **An answer-quality judge** (Step 4), set up as described in **Setting up a judge** below, with an OpenAI model
#    and this rubric:
#
#    > You are grading a bank's customer-support assistant (ToggleBank). Using the message history, score the
#    > RESPONSE on whether it (1) addresses what the customer actually asked, (2) does not invent account data such
#    > as balances, fees, rates or transactions that it was not given, and (3) gives a clear, safe next step when it
#    > cannot answer directly (for example, offering to connect the customer with a person for fraud or account
#    > changes). Score 1.0 when all three hold, 0.0 when it fabricates account data or ignores the request, and in
#    > between for partially helpful answers.
#
# 4. **A tool and a trajectory judge** (Step 5). Create a `find_branch` tool in the Library with this schema:
#
#    ```json
#    {
#      "type": "object",
#      "properties": {"location": {"type": "string", "description": "ZIP code or neighborhood"}},
#      "required": ["location"]
#    }
#    ```
#
#    Then set up a second judge, with an OpenAI model and this rubric:
#
#    > You are grading an assistant's TOOL USE, not its prose. The message history shows the user's request, the
#    > tools the assistant could call, the calls it actually made with their arguments and each result or error, and
#    > its final answer. Score 1.0 when the tool use was right for the request. That INCLUDES calling no tool at all
#    > when the request did not need one: an available tool left unused is not a mistake, and a request that needs no
#    > tool must not be penalised for having none. Score 0.0 when the assistant needed a tool and did not call it,
#    > called the wrong tool, passed arguments the request does not support, ignored what a tool returned, or
#    > repeated a failing call without adapting. Score in between for partial credit.
#
# 5. **A Bedrock judge and AWS credentials** (Step 6). A judge set up the same way, using a Bedrock model, and an AWS
#    profile or credentials that can call Bedrock.
#
# ### Setting up a judge
#
# Create each judge as an AI Config in **judge** mode, with higher scores meaning better (not inverted). Its variation
# needs three messages. Starting from the Library's default judge template gives you the last two:
#
# | Role | Content |
# |---|---|
# | System | The rubric |
# | Assistant | `MESSAGE HISTORY:` followed by `{{message_history}}` |
# | User | `RESPONSE TO EVALUATE:` followed by `{{response_to_evaluate}}` |
#
# The SDK fills in `{{message_history}}` with the row's input, any tool calls, and the generated output, and
# `{{response_to_evaluate}}` with the output.
#
# Then **turn the judge on in the environment that your SDK key belongs to**, with its default rule serving that
# variation. The SDK fetches judges the same way it evaluates flags, so a judge that is off or serves no variation
# fails the run before any rows are generated.

# %% [markdown]
# ## Settings
#
# Change the keys to match what you created. **`RUN`** turns each section on or off. No credentials go here: see
# **Credentials** below.

# %%
PROJECT_KEY = "policy-agent"
DATASET_KEY = "policy_agent_dataset"
ANSWER_JUDGE = "offline-answer-quality"  # OpenAI judge
TRAJECTORY_JUDGE = "tool-trajectory-relevance"  # OpenAI judge
BEDROCK_JUDGE = "ai-judge-accuracy"  # Bedrock judge
TOOL_KEY = "find_branch"
OPENAI_MODEL = "gpt-4o"
BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
INSTRUCTIONS = "You are ToggleBank's customer support assistant. Answer accurately and concisely."
RUN = {
    "first_eval": True,
    "judges_and_scorers": True,
    "tool_trajectory": True,
    "bedrock": True,
}

# %% [markdown]
# ## Step 1: Credentials
#
# The SDK uses two channels, and needs both:
#
# | Credential | Variable | What it does |
# |---|---|---|
# | API access token | `LD_API_TOKEN` | Required. Reads the dataset, tools and judges. Creates the evaluation and run. Polls the summary. |
# | SDK key | `LD_SDK_KEY` | Sends every generation and score to LaunchDarkly as an event. Results reach LaunchDarkly only this way. |
#
# OpenAI generation and the OpenAI judges also need `OPENAI_API_KEY`. Set these in your environment before starting
# Jupyter. Any that aren't set are asked for here with hidden input, and are kept only for this session. Bedrock
# uses the standard AWS credential chain, such as `AWS_PROFILE`.

# %%
import getpass
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

for name in ("LD_API_TOKEN", "LD_SDK_KEY", "OPENAI_API_KEY"):
    if not os.environ.get(name):
        os.environ[name] = getpass.getpass(f"{name}: ")

# %% [markdown]
# ## Step 2: Install and connect
#
# Install the SDK and the OpenAI handler from PyPI into the environment this notebook runs in (Python 3.12 or
# later). Step 6 also needs the LangChain handler and `langchain-aws` for Bedrock:
#
# ```sh
# pip install launchdarkly-ai-python launchdarkly-ai-openai-messages
# pip install launchdarkly-ai-langchain-messages langchain-aws   # Step 6 only
# ```
#
# `init_evaluations()` reads `LD_API_TOKEN` and `LD_SDK_KEY` from the environment.

# %%
from launchdarkly_ai_openai_messages import create_openai_messages_handler
from launchdarkly_ai_python import DatasetRow, Judge, Scorer, init_evaluations, shutdown

evals = init_evaluations()  # reads LD_API_TOKEN and LD_SDK_KEY


def unique(prefix: str) -> str:
    """Every run() creates a new evaluation, so each key must be unique."""
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}"


def report(result) -> None:
    print(f"verdict : {'PASSED' if result.passed else 'FAILED'}")
    print(f"summary : {result.summary}")
    print(f"run page: {result.url}")

# %% [markdown]
# `run()` returns the verdict and totals. Open the run page for each row's output, scores and judge reasoning.

# %% [markdown]
# ## Step 3: Run your first eval
#
# A generation-only run: every row goes to OpenAI, and with no criteria every row that produces an output passes.

# %%
if RUN["first_eval"]:
    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("first-eval"),
        dataset=DATASET_KEY,
        handler=create_openai_messages_handler(),
        generation={
            "provider": "OpenAI",
            "model": OPENAI_MODEL,
            "parameters": {"temperature": 0},
            "instructions": INSTRUCTIONS,
        },
        concurrency=5,
    )
    report(result)

# %% [markdown]
# ## Step 4: Score rows with scorers and LaunchDarkly judges
#
# - **`Scorer`**: a local function `(row, output)` that returns a bool or a number from 0 to 1. Its `threshold`
#   defaults to `1.0`, which is what a boolean scorer wants.
# - **`Judge`**: a judge config in LaunchDarkly, referenced by key. Its `threshold` defaults to `0.5`. Its success
#   direction (`isInverted`) is set on the config, not in code.
# - **`pass_rate_threshold`**: the share of rows you want to pass. LaunchDarkly stores it with the run, but the run
#   verdict doesn't use it yet: a run passes only when no row failed, errored, or is still pending. One failed row
#   fails the run, even when the pass rate is above the threshold.
#
# Two example scorers: the answer isn't empty, and the answer doesn't quote a dollar amount (the assistant has no
# real account data, so any figure is made up).

# %%
def score_nonempty(row: DatasetRow, output: str | None) -> bool:
    return bool((output or "").strip())


def score_no_invented_amounts(row: DatasetRow, output: str | None) -> bool:
    """False when the answer quotes a dollar figure."""
    return not re.search(r"\$\s?\d", output or "")


if RUN["judges_and_scorers"]:
    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("judges-and-scorers"),
        dataset=DATASET_KEY,
        handler=create_openai_messages_handler(),
        generation={
            "provider": "OpenAI",
            "model": OPENAI_MODEL,
            "parameters": {"temperature": 0},
            "instructions": INSTRUCTIONS,
        },
        criteria=[
            Judge(key=ANSWER_JUDGE, threshold=0.7, pass_rate_threshold=0.9),
            Scorer(name="nonempty-output", fn=score_nonempty),
            Scorer(name="no-invented-amounts", fn=score_no_invented_amounts),
        ],
        concurrency=5,
    )
    report(result)

# %% [markdown]
# Judge keys and scorer names share one namespace within a run, so they must not collide.

# %% [markdown]
# ## Step 5: Judge the tool trajectory
#
# The SDK records every tool call a row makes and renders it into the judge's `{{message_history}}`. That lets the
# rubric grade *how* the assistant answered. The branch rows need `find_branch`, and the others should call nothing,
# which is why the rubric's "an unused tool is not a mistake" clause matters.
#
# The `tools` argument maps each Library tool key to a local function that runs when the model calls it.

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


if RUN["tool_trajectory"]:
    tool_calls = []
    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("tool-trajectory"),
        dataset=DATASET_KEY,
        handler=create_openai_messages_handler(),
        tools={TOOL_KEY: make_find_branch(tool_calls)},
        generation={
            "provider": "OpenAI",
            "model": OPENAI_MODEL,
            "parameters": {"temperature": 0},
            "instructions": INSTRUCTIONS + f" Use the {TOOL_KEY} tool for branch or ATM questions.",
        },
        criteria=[Judge(key=TRAJECTORY_JUDGE, threshold=0.8)],
        concurrency=2,
    )
    print(f"{TOOL_KEY} called for: {tool_calls}")
    report(result)

# %% [markdown]
# Scorers can't see the trajectory. `Scorer.fn` gets only the row and the output, so a check like "called
# `find_branch` exactly once" needs a judge.

# %% [markdown]
# ## Step 6: Generate and judge on Amazon Bedrock through LangChain
#
# The LangChain handler serves every provider. Generation runs on Bedrock, and the Bedrock judge grades the answers
# through the same handler, so no `judge_handlers` or OpenAI key is needed. The handler builds `ChatBedrockConverse`
# from the standard AWS credential chain.

# %%
if RUN["bedrock"]:
    from launchdarkly_ai_langchain_messages import create_langchain_messages_handler

    result = await evals.run(
        project_key=PROJECT_KEY,
        key=unique("bedrock-judges"),
        dataset=DATASET_KEY,
        handler=create_langchain_messages_handler(),
        generation={
            "provider": "Bedrock",
            "model": BEDROCK_MODEL,
            "parameters": {"temperature": 0},
            "instructions": INSTRUCTIONS,
        },
        criteria=[
            Judge(key=BEDROCK_JUDGE),
            Scorer(name="nonempty-output", fn=score_nonempty),
        ],
        concurrency=2,  # new Bedrock accounts throttle at low request rates
    )
    report(result)

# %% [markdown]
# ## What to know
#
# - **Datasets** can only be created in the UI.
# - **Evaluations and runs** can be created from code, but not listed, updated or deleted over the API. Every `run()`
#   adds a new evaluation.
# - **Scores and judge reasoning** are visible only on the run page. `run()` returns the verdict and totals.
# - **Daily token limit:** runs count against a per-project LaunchDarkly limit. When it's reached, `run()` fails with
#   HTTP 429 `token_limit_exceeded`, after the evaluation has already been created.

# %%
await shutdown()
