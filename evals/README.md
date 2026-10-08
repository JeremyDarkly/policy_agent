# Offline evals for the ToggleBank policy agent

Evaluations run from code with the LaunchDarkly Python AI SDK against the staging `policy-agent` project.

| File | What it is |
|---|---|
| `offline_evals_walkthrough.py` | Notebook (jupytext percent format) that walks through the *Offline Evals from Code* guide |
| `offline_evals_customer.py` | Customer version of the notebook: runs against your own production project, with no staging setup or asset checks |
| `run_policy_agent_eval.py` | Script: evaluates the running agent via `/api/chat` with a judge and a routing scorer |
| `policy_agent_dataset.jsonl` | 6-row dataset; upload in the staging UI with key `policy_agent_dataset` |
| `policy_agent_dataset_quick.jsonl` | 2-row dataset for cheap practice runs; key `policy_agent_dataset_quick` |
| `guarded_rollout_walkthrough.py` | Notebook: a guarded rollout of a new AI Config variation on staging, rolled back automatically when a judge score regresses |
| `guarded_rollout_questions.jsonl` | Customer questions the guarded rollout notebook sends as traffic |

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
`.env.staging`, Terraform in the `staging` workspace, the targeting script, `docker compose --env-file .env.staging up`,
and uploading the dataset. Staging support (Terraform `api_host`, SDK endpoint variables, `TF_WORKSPACE`, `ENV_FILE`)
exists only in this repo, so other copies of the app target production. The `.ipynb` is gitignored
because it carries run outputs; edit the `.py`.

Every run counts against the project's daily LaunchDarkly token limit. Use the notebook's `RUN` switches and `QUICK`
mode to keep practice runs small.

## Guarded rollout notebook

`guarded_rollout_walkthrough.py` releases a new prompt for a support assistant with a guarded rollout on staging.
The new prompt makes the model invent account data, a judge scores every answer, and LaunchDarkly rolls the rollout
back when the score regresses. It creates its AI Config, judge and metric the first time it runs, and needs only
`.env.staging` and an AWS session, not the app or Terraform. A run takes about 10 minutes and a few hundred Bedrock
calls.

```sh
uv run jupytext --sync guarded_rollout_walkthrough.py
uv run jupyter lab guarded_rollout_walkthrough.ipynb
```

## Customer notebook

`offline_evals_customer.py` is the version to give customers. It uses production LaunchDarkly. The project, dataset,
judge and tool keys are settings at the top. It doesn't create or check anything in LaunchDarkly: its "Before you
start" section lists what to create in the UI, with the rubrics and tool schema to copy. It reads `LD_API_TOKEN`,
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
non-production hostnames (read from `../.env.staging`), evaluation and run IDs, partial credentials, local paths and
SDK log noise. Rebuild it after a run, and review it before committing. The script refuses to write the copy if
anything that looks sensitive remains:

```sh
uv run python sanitize_notebook.py
```
