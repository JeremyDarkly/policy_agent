# Offline evals for the ToggleBank policy agent

Evaluations run from code with the LaunchDarkly Python AI SDK against the staging `policy-agent` project.

| File | What it is |
|---|---|
| `offline_evals_walkthrough.py` | Notebook (jupytext percent format) that walks through the *Offline Evals from Code* guide |
| `run_policy_agent_eval.py` | Script: evaluates the running agent via `/api/chat` with a judge and a routing scorer |
| `policy_agent_dataset.jsonl` | 6-row dataset; upload in the staging UI with key `policy_agent_dataset` |
| `policy_agent_dataset_quick.jsonl` | 2-row dataset for cheap practice runs; key `policy_agent_dataset_quick` |

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
