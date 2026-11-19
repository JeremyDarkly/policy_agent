# Offline evals for the ToggleBank policy agent

Evaluations run from code with the LaunchDarkly Python AI SDK against the staging `policy-agent` project.

| File | What it is |
|---|---|
| `offline_evals_walkthrough.py` | Notebook (jupytext percent format) that walks through the *Offline Evals from Code* guide |
| `offline_evals_customer.py` | Customer version of the notebook: runs against your own production project, with no staging setup or asset checks |
| `run_policy_agent_eval.py` | Script: evaluates the running agent via `/api/chat` with a judge and a routing scorer |
| `policy_agent_dataset.jsonl` | The notebooks' 6 rows, for uploading as a stored dataset with key `policy_agent_dataset`. The notebooks pass the same rows in code, so they don't need it |
| `policy_agent_dataset_quick.jsonl` | 2-row dataset for cheap practice runs; key `policy_agent_dataset_quick` |
| `guarded_rollout_walkthrough.py` | Notebook: a guarded rollout of a new AI Config variation on staging, rolled back automatically when a judge score regresses |
| `guarded_rollout_questions.jsonl` | Customer questions the guarded rollout notebook sends as traffic |
| `setup_triage_experiment.py` | Script: creates the triage routing experiment (3 challenger variations, 4 metrics, a 4-arm experiment) |
| `run_triage_experiment.py` | Script: sends labeled routing traffic through the real triage node, or pilots every variation offline |
| `triage_routing_dataset.jsonl` | 48 routing questions labeled with the expected specialist; 29 are hard cases. Upload as stored dataset `triage_routing_dataset` for `ci_eval.py` |
| `triage_routing_dataset_quick.jsonl` | 12 of those rows, 3 per specialist; stored dataset key `triage_routing_dataset_quick` |
| `triage_prompts/tuned.md` | The tuned triage prompt the challenger variations use |
| `ci_eval.py` | Script for CI: evaluates AI Config variations against stored datasets, without the app, and writes a Markdown summary |
| `ci_targets.toml` | The datasets, scorers and judges `ci_eval.py` uses for each AI Config |
| `CI_EVALS.md` | How the LaunchDarkly evals GitHub Action works: triggers, what a run does, setup and troubleshooting |
| `build_specialist_datasets.py` | Script: builds the CI datasets for the account, branch, scheduler and brand voice agents in `ci_datasets/` |
| `ci_datasets/` | Those datasets, a full and a quick one per agent; stored dataset keys are the file names without `.jsonl` |

```sh
cd evals
uv sync --all-groups
# One-time: register this environment as the notebook's kernel ("Policy agent evals (uv)")
uv run python -m ipykernel install --user --name policy-agent-evals --display-name "Policy agent evals (uv)"
uv run jupytext --sync offline_evals_walkthrough.py   # creates the paired .ipynb
uv run jupyter lab offline_evals_walkthrough.ipynb
```

The notebook asks for the `policy-agent-evals` kernel, because its packages are installed only in `evals/.venv`. In
VS Code, pick **Select Kernel → Jupyter Kernel → Policy agent evals (uv)**, or choose the interpreter
`evals/.venv/bin/python`. Rerun the `ipykernel install` line if you recreate `.venv`.

Set the app up on staging from **this repo** first. The notebook's "Before you run it" section walks through it:
`.env.staging-org`, Terraform in the `staging` workspace, the targeting script, and `docker compose --env-file .env.staging-org up`.
The dataset and the `find_branch` tool are defined in the notebook, so there's nothing to upload. Staging support (Terraform `api_host`, SDK endpoint variables, `TF_WORKSPACE`, `ENV_FILE`)
exists only in this repo, so other copies of the app target production. The `.ipynb` is gitignored
because it carries run outputs; edit the `.py`.

For the Agent Skills demo, which evaluates the same agents with and without library skills, see
[`../skills/README.md`](../skills/README.md).

Every run counts against the project's daily LaunchDarkly token limit. Use the notebook's `RUN` switches and `QUICK`
mode to keep practice runs small.

## Guarded rollout notebook

`guarded_rollout_walkthrough.py` releases a new prompt for a support assistant with a guarded rollout on staging.
The new prompt makes the model invent account data, a judge scores every answer, and LaunchDarkly rolls the rollout
back when the score regresses. It creates its AI Config, judge and metric the first time it runs, and needs only
an env file (`.env.staging-org` by default) and an AWS session, not the app or Terraform. A run takes about 10 minutes and a few hundred Bedrock
calls.

```sh
uv run jupytext --sync guarded_rollout_walkthrough.py
uv run jupyter lab guarded_rollout_walkthrough.ipynb
```

## Customer notebook

`offline_evals_customer.py` is the version to give customers. It uses production LaunchDarkly. The project, judge and
tool keys are settings at the top, and the dataset rows and the `find_branch` tool are defined in code. It doesn't
create or check anything in LaunchDarkly: its "Before you start" section lists the judges to create in the UI, with
the rubrics to copy. It reads `LD_API_TOKEN`,
`LD_SDK_KEY` and `OPENAI_API_KEY` from the environment, and asks for any that are missing with hidden input.

It imports from [`launchdarkly-ai-python`](https://pypi.org/project/launchdarkly-ai-python/). This folder's
`pyproject.toml` includes that package, so the notebook runs in `evals/.venv`. There it uses the git `main` SDK that the
internal notebook is pinned to. Customers install from PyPI into any Python 3.12+ environment:

```sh
pip install launchdarkly-ai-python==1.0.0 launchdarkly-ai-openai-messages==1.0.0 launchdarkly-ai-langchain-messages==1.0.0 langchain-aws
```

Build the blank copy to share it. Rerun this after any edit to the `.py`:

```sh
uv run jupytext --to ipynb --update-metadata '{"jupytext":null}' \
  -o offline_evals_customer_blank.ipynb offline_evals_customer.py
```

## Share a blank notebook

To give someone the notebook with all the code but no outputs, build a standalone copy from the `.py`. Rerun it after
any edit to the `.py`:

```sh
uv run jupytext --to ipynb --update-metadata '{"jupytext":null}' \
  -o offline_evals_walkthrough_blank.ipynb offline_evals_walkthrough.py
```

Removing the `jupytext` metadata unpairs the copy, so opening it won't sync changes back to the `.py`.

## Post a notebook with results

`offline_evals_walkthrough_public.ipynb` is a committed copy of an executed run, with internal details removed:
non-production hostnames (read from `../.env.staging-org`), evaluation and run IDs, partial credentials, local paths and
SDK log noise. Rebuild it after a run, and review it before committing. The script refuses to write the copy if
anything that looks sensitive remains:

```sh
uv run python sanitize_notebook.py
```

## Triage routing experiment

A four-arm experiment on `triage_agent`: can a smaller model route queries as accurately as Claude Sonnet 5 for
less cost and latency?

| Arm | Model | Prompt |
|---|---|---|
| `default` (control) | Claude Sonnet 5 | current |
| `haiku-current-prompt` | Claude Haiku 4.5 | current |
| `haiku-tuned-prompt` | Claude Haiku 4.5 | `triage_prompts/tuned.md` |
| `nova-pro-tuned-prompt` | Amazon Nova Pro | `triage_prompts/tuned.md` |

The primary metric is routing accuracy (`triage-routed-correctly`, 1 or 0, sent by the replay script from the
row's label). The secondary metrics are triage cost, triage latency and escalation rate, sent by the triage node itself
so the specialists' events don't mix in.

From the repo root, with AWS credentials for Bedrock:

```sh
python3 evals/setup_triage_experiment.py --env-file .env.staging-org          # create, idempotent
RUN="uv run --no-project --with-requirements app/backend/requirements.txt python evals/run_triage_experiment.py --env-file .env.staging-org"
$RUN --pilot --show-misses                                                    # every variation x every row, no events
python3 evals/setup_triage_experiment.py --env-file .env.staging-org --start  # start the experiment
$RUN --users 800                                                              # simulated users, ~200 per arm
```

While the experiment runs, every `triage_agent` evaluation is randomized, including chats in the UI.

### Jev triage

`jev-choice` is a fifth `triage_agent` variation that routes with Jev, TypeSafe's decision model, instead of an LLM.
It is on model config `TypeSafe.jev-1.14.0`, pinned rather than `jev-latest` so the model can't change during a
rollout. When it is served, `triage_node` asks Jev one `choice` question over HTTP
(`app/backend/src/utils/typesafe_client.py`) and maps the answer onto the same `query_type` and `confidence_score`
the LLM path produces. The question is the variation's content, edited in LaunchDarkly:

| Variation field | Use |
|---|---|
| `instructions` | The question's instructions |
| `model.custom.criteria` | One description per route; the keys are the `query_type` strings |
| `model.custom.criteria_order` | The order the options are sent in. LaunchDarkly stores `criteria` with sorted keys, and Jev leans toward the first option, so `schedule_agent` (the safe direction) goes first |
| `model.custom.choice_confidence_min` | Below this, a non-general query escalates to `scheduler_specialist` (0.7, as on the LLM path) |

The backend needs `TYPESAFE_API_KEY`. Any Jev failure (no key, timeout, 4xx/5xx, a response missing a field)
escalates the query to `scheduler_specialist` and sends `triage-error` = 1. Every triage call, LLM or Jev, sends
`triage-error`, so the `triage-error-rate` metric is a rate.

No experiment is used: `jev-choice` is not an arm of `triage-routing-cost-vs-accuracy`, and the scripts below don't
create or start one. It is shown and released in three steps.

```sh
python3 evals/setup_jev_variation.py --env-file .env.staging-org     # variation + triage-error-rate; no targeting change
RUN="uv run --no-project --with-requirements app/backend/requirements.txt python evals/run_triage_experiment.py --env-file .env.staging-org"
$RUN --pilot --variations default,haiku-current-prompt,jev-choice --show-misses   # 1. offline side by side, no events
```

On 2026-10-31 that pilot gave:

| Variation | Correct | Hard rows | Cost per query | p50 |
|---|---|---|---|---|
| `default` (Sonnet 5) | 47/48 | 28/29 | 0.4783 ¢ | 3,856 ms |
| `haiku-current-prompt` | 48/48 | 29/29 | 0.1945 ¢ | 2,561 ms |
| `jev-choice` | 48/48 | 29/29 | 0.0024 ¢ | 80 ms |

2. **Live demo.** In `triage_agent`'s targeting for `test`, add an individual target that serves `jev-choice` to the
   demo user's context key (`<name>-commercial-plan`). Chats as that user show `jev-1.14.0` in the agent-flow panel;
   everyone else keeps the default rule's LLM. Remove the target afterwards: it is the "off" state, and a leftover
   target keeps the demo user on Jev in later demos.
3. **Guarded rollout.** Move the `test` default rule from `haiku-current-prompt` to `jev-choice` as a guarded
   rollout with automatic rollback. Use these guardrails: `triage-routing-accuracy`, `triage-cost-cents`,
   `triage-latency-ms`, `triage-escalation-rate` and `triage-error-rate`. Drive it with `$RUN --users 1000`. The
   non-pilot mode only needs `triage_agent` to be evaluated for each user, not an experiment. To show a rollback, run
   `$RUN --users 1000 --fault` during a second rollout. `--fault` uses a bad `TYPESAFE_API_KEY`, so every Jev call
   fails and `triage-error-rate` goes to 100% on `jev-choice`.

`ci_eval.py` doesn't evaluate `jev-choice`. The eval runner generates through LangChain and drops `model.custom`, so
`ci_targets.toml` lists it in `exclude_variations`.

### Jev judge

`ai-judge-jev` is a judge-mode AI Config that grades each brand voice reply with Jev. One request answers five
questions (`accuracy`, `hallucination`, `answers_question`, `coherence`, `tone`), and each answer is its own metric.
Its variations and what they do are in the [README](../README.md#judges). `setup_jev_judge.py` creates the config,
its `jev-alongside` and `jev-only` variations (from one question definition, so they can't drift), and eight
metrics. It points the default rule at `disabled` and adds one rule that serves `jev-alongside` to contexts with
`judge_pilot` = true, which only the pilot sets.

```sh
python3 evals/setup_jev_judge.py --env-file .env.staging-org     # config, variations, metrics, pilot rule
PILOT="uv run --no-project --with-requirements app/backend/requirements.txt python evals/run_judge_pilot.py --env-file .env.staging-org"
$PILOT                          # 1. LLM judges and Jev over the labeled set; no events (needs AWS credentials)
$PILOT --skip-llm --send        #    Jev only, and send its events so the metrics get traffic
$PILOT --skip-llm --send --fault  #  every Jev call fails: jev-judge-error-rate goes to 100%
```

`judge_labeled_dataset.jsonl` has 48 hand-written replies: 12 questions over two account profiles, four replies each.
`good` is accurate and well written. `halluc` is well written with one invented or contradicted fact. `incoh` has
correct facts but is garbled or jargon-heavy. `both` has an invented fact and is curt or rude. So accuracy should pass
on `good` and `incoh`, and coherence on `good` and `halluc`. The labels were written before any judge ran.

The pilot runs each row as a simulated user through the backend's own code: `BrandVoiceEvaluator` for the LLM judges
and `jev_judge` for Jev, on whatever `ai-judge-jev` serves that user. On 2026-11-03 it gave:

| Judge | Accuracy correct | Planted inaccuracies missed | Accurate replies failed | Coherence correct | Bad writing missed | Good writing failed | p50 latency | Cost per reply |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| LLM judges (Claude Sonnet 5 on Bedrock), thresholds 0.8 / 0.7 | 39/48 | 0 | 9 | 30/48 | 0 | 18 | 4,060 ms / 4,718 ms | 0.922 ¢ (both) |
| Jev `jev-1.14.0`, as the app decides (`accuracy` and `hallucination`; `coherence` and `tone`) | 41/48 | 0 | 7 | 43/48 | 0 | 5 | 108 ms | 0.0040 ¢ |
| Jev, `accuracy` alone; `coherence` alone | 41/48 | 0 | 7 | 41/48 | 3 | 4 | same call | same call |
| Jev, `hallucination` alone; `tone` alone | 45/48 | 0 | 3 | 46/48 | 0 | 2 | same call | same call |

`answers_question` passed on all 12 `good` replies. Jev and the LLM judges disagreed on 23 of 48 replies (accuracy or
coherence), mostly where an LLM judge failed a good reply. That is the baseline for `jev-judge-disagreement`, which
counts a reply once if either verdict differs. `coherence` alone passed three short, rude replies that `tone` caught.

The thresholds are today's (accuracy 0.8, coherence 0.7, noul 0.5). Both judges catch every planted failure; they
differ in how many good replies they fail. 48 rows on two profiles is a small set, and the replies are more clear-cut
than real brand voice output. `jev-alongside` and `jev-judge-disagreement` are how Jev is watched on real chats.

**Live demo.** In `ai-judge-jev`'s targeting for `test`, add an individual target that serves `jev-alongside` to the
demo user's context key (`<name>-commercial-plan`). A chat as that user shows the LLM scores and, under them, Jev's
five results with `jev-1.14.0`, its latency and its cost. Switch the target to `jev-only` and the LLM judge calls
stop. Remove the target afterwards; the user is back on `disabled`.

`ci_eval.py` doesn't use `ai-judge-jev`: the SDK's `Judge` parses one LLM score, so a Jev judge can't run there.

## CI evals

`ci_eval.py` evaluates AI Config variations directly: the AI SDK reads each variation's model and prompt from
LaunchDarkly and generates on Bedrock, so the app doesn't need to be running. Each target in `ci_targets.toml` names a
dataset stored in LaunchDarkly. Upload the matching `.jsonl` file in the LaunchDarkly UI (**Library > Datasets > New
dataset**) with that key first. Results are advisory: the script exits non-zero only when a run can't complete.

```sh
uv run python ci_eval.py --env-file ../.env.staging-org --quick          # quick datasets
uv run python ci_eval.py --env-file ../.env.staging-org --config triage_agent --variation default
```

**Which variation.** Without `--variation`, each target is evaluated on the variation its default rule
(fallthrough) serves in `--environment` (default `$LD_ENVIRONMENT`, from `LAUNCHDARKLY_ENVIRONMENT` in the env file),
read from the AI Config's targeting. A percentage rollout evaluates each variation in it. A target's `variations` list
in `ci_targets.toml` adds more, for example challenger variations. The test contexts in `ci_targets.toml`'s
`[contexts]` table add the variations LaunchDarkly serves them, so a variation a rule or individual target serves is
evaluated when a context matches it (see `CI_EVALS.md`, "Test contexts"); the summary names any it misses.
`--no-contexts` skips the contexts, and `--variation` evaluates one variation of every target.

**Specialist datasets.** `build_specialist_datasets.py` writes the account, branch, scheduler and brand voice
datasets into `ci_datasets/`. Each row reproduces the user message and prompt variables the backend sends that agent,
built from the backend's `create_user_profile`. Rebuild after changing the questions, then upload the changed files
again. Rows carry only the variables their prompt and judges read, since the full context is already in `input`. That
keeps rows short: the LaunchDarkly upload screen showed files with rows over about 4,096 characters as empty, so the
builder stops if a row is over 4,000. They're judged by `ci-judge-accuracy` and `ci-judge-coherence`, offline copies of the app's two judges that read
the SDK's offline variables (`response_to_evaluate`, `message_history`, `ground_truth_context`). They're defined in
`infrastructure/launchdarkly/ai_configs_resources.tf`. In `ci_targets.toml`, a judge entry can set a `threshold` and a
`ground_truth` template rendered with the row's variables. `drop_parameters` leaves out model parameters the backend
doesn't send either.

`.github/workflows/evals.yml` runs `ci_eval.py` in GitHub Actions on PRs, pushes to `main`, nightly and by hand.
[CI_EVALS.md](CI_EVALS.md) describes what triggers it, what a run does, the quick and full datasets, and the
secrets and AWS role it needs.

### Code scorers

Every target also runs deterministic scorers next to its judges (`scorers` in `ci_targets.toml`). Each one is a plain
function in `app/backend/src/evaluation/code_checks.py`, which the backend uses on live traffic too
(`response-code-checks`, see the [README](../README.md#code-checks-beside-the-judges)), so a check gives the same
answer offline and online. They make no model calls.

| Scorer | Targets | Passes when |
| --- | --- | --- |
| `routed-to-expected-specialist` | `triage_agent` | the triage output routes to the row's expected specialist. Triage's only criterion, so it decides the verdict. |
| `account-facts` | `account_agent` | no ID, amount, percentage, date, tier name or account state (autopay, billing, status) in the reply contradicts `account_info` |
| `offered-listed-slots` | `scheduler_agent` | every slot ID and time in the reply is in the row's `available_slots` |
| `called-expected-tools` | `scheduler_agent.tools`, `branch_agent.tools` | the set of tools called equals `expected_tools`; a control row calls none |
| `unlisted-slots` | `scheduler_agent.tools` | every slot ID and time in the reply came from a `get_available_slots` call. A slot ID is a `SLOT-…` ID or any ID-like token next to a time (`FRD-101`). |
| `draft-facts` | `brand_agent` | the reply keeps every number, ID, date and time in `specialist_response`. Scores the fraction kept. |
| `leaked-headings` | `scheduler_agent`, `branch_agent`, `brand_agent` (and their tool tables) | no `Step N:` line from the prompt (#22), as a heading, in bold or plain, in any language seen so far (`Paso 1:`). Other headings pass: brand voice writes titles on purpose. |
| `demo-mentions-account-type` | `account_agent` | the reply names the profile's account type. Deliberately poor; see below. |

**Scorers don't decide a row's verdict; the judges do.** LaunchDarkly rejects a scorer without a threshold (`400
options.threshold is required for kind 'scorer'`, 2026-11-18), so every scorer runs with threshold 1.0, and the
LaunchDarkly run page counts a scorer failure as a failed row. `ci_eval.py` takes each row's verdict from its judges'
per-row results instead (plus `routed-to-expected-specialist`), and says so in the summary when the two counts differ.
The summary's "Rows each scorer and judge passed" table lists every judge and scorer, with a *Same verdict as* column:
the rows on which a scorer and its judge agreed. Scorer failures, with the scorer's reason, are listed in their own
section below the judge failures. The SDK sends a scorer's reason to LaunchDarkly as empty, so the summary is where to
read it.

**Tool calls.** The SDK doesn't give a scorer the row's tool calls (#86). `ci_eval.py` wraps each row's tool handlers
and records the calls under the row's input and variables, and the trajectory scorers look them up there. A row whose
calls weren't recorded is a scorer error, not a pass. `load_targets` stops if a local dataset has two rows with the
same input and variables.

### A good check and a bad one

`account-facts` and `demo-mentions-account-type` both check `account_agent` replies against `account_info`, and both
are deterministic. They differ in design only.

| | `account-facts` (well-considered) | `demo-mentions-account-type` (poorly-considered) |
| --- | --- | --- |
| Passes when | no ID, amount, percentage, date, tier name or account state in the reply contradicts the profile | the reply contains the profile's account type, for example `Gold Checking` |
| Question it answers | "Did the reply state anything the profile contradicts?", the question the accuracy judge asks | "Did the reply mention the right product?", a proxy for accuracy |
| Direction | looks for what is wrong, so a reply that says nothing checkable passes | looks for one thing that is right, so an accurate reply about something else fails |
| Inputs | typed tokens, normalized (case, `$`, commas, date formats) before comparing | one case-insensitive substring |
| Known limit | can't check a claim with no token or state phrase ("everything's fine") | any invented fact passes if the product is named: `Your Gold Checking balance is $4,218.37` |

`measure_code_checks.py` runs both over the 48 labeled replies in `judge_labeled_dataset.jsonl`, with no model calls:

```sh
python3 evals/measure_code_checks.py --show-misses
```

On 2026-11-18 it gave:

| Check | Accuracy right | Planted inaccuracies missed | Accurate replies failed | `good` replies failed | Flags that were right | Model calls |
| --- | --- | --- | --- | --- | --- | --- |
| `account_facts` | 45/48 | 1 | 2 | 1/12 | 23/25 | 0 |
| `mentions_account_type` | 30/48 | 3 | 15 | 8/12 | 21/36 | 0 |
| LLM judges, Claude Sonnet 5 (pilot, 2026-11-03) | 39/48 | 0 | 9 | – | – | 48 |

`account_facts` misses one rude reply that states nothing checkable ("Everything's fine. Stop worrying."), and its two
false alarms name the Platinum account the customer asked to upgrade to. The account-state phrases were written after
reading these replies, so its numbers are optimistic. A check that fails more than 10% of `good` replies isn't added
to `ci_targets.toml`; `demo-mentions-account-type` is the exception, kept on purpose and shown in the summary's
"Comparison only" table, with its failures left out of the failing rows.
