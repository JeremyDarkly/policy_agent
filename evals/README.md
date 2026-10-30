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
pip install launchdarkly-ai-python launchdarkly-ai-openai-messages launchdarkly-ai-langchain-messages langchain-aws
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

`.github/workflows/e2e-evals.yml` runs `ci_eval.py --e2e`: it starts the backend in the runner and sends the triage
datasets through `/api/chat`, scoring routing, handoffs, offered slots and brand voice's facts. See
[CI_EVALS.md](CI_EVALS.md), "End-to-end evals".
