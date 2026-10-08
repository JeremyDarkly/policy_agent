# Agent Skills demo for the ToggleBank agents

A notebook demo of LaunchDarkly **Agent Skills** (beta): versioned `SKILL.md` content in the project library, pinned on
an AgentControl variation, resolved at runtime and evaluated. It runs against the app this repo sets up.

These are LaunchDarkly skills for the ToggleBank agents. They aren't related to the Claude Code skills in
`.claude/skills/`, which help a developer work on this repo.

| File | What it is |
|---|---|
| `agent_skills_demo.py` | The notebook (jupytext percent format). Edit this, not the `.ipynb` |
| `library/togglebank-dispute-handling/v1`, `v2`, `v3-bad` | Versions of the dispute skill that the notebook publishes. `v3-bad` is deliberately harmful |
| `library/plain-english/SKILL.md` | A shared house-style skill, pinned on two agents |
| `skills_demo_dataset.jsonl` | 6 rows: 4 dispute or fee questions and 2 controls. Upload with key `skills_demo_dataset` |
| `skills_demo_dataset_quick.jsonl` | 2 rows for cheap rehearsals. Upload with key `skills_demo_dataset_quick` |

## Before you run it

1. **The app on staging.** Set it up from this repo, as in `evals/README.md`: the env file, Terraform, and the
   targeting script. The notebook needs `account_agent` and `branch_agent` with their `Default` variations, and the
   `tool-trajectory-relevance` judge from the offline evals notebook.
2. **Agent Skills enabled** for the account. Library → Skills should be visible in the UI.
3. **The env file** (`.env.staging-org` by default, set with `ENV_FILE_NAME`). It needs `OPENAI_API_KEY` as well as
   the usual variables. Optionally, set `LAUNCHDARKLY_BASE_URI` to your instance's SDK polling host. The notebook's
   SDK client then polls every 30 seconds instead of streaming. Use this if your streaming connection doesn't pick
   up variation edits.
4. **Upload the datasets** in the UI (Agents → Library → Datasets), with the keys in the table above. There's no API
   to create a dataset.

## Run it

```sh
cd skills
uv sync --all-groups
# One-time: register this environment as the notebook's kernel
uv run python -m ipykernel install --user --name policy-agent-skills --display-name "Policy agent skills demo (uv)"
uv run jupytext --sync agent_skills_demo.py   # creates the paired .ipynb
uv run jupyter lab agent_skills_demo.ipynb
```

In VS Code, pick **Select Kernel → Jupyter Kernel → Policy agent skills demo (uv)**.

The notebook is safe to rerun. It reuses skill versions whose markdown already exists and creates the variations,
tool and judge only when they're missing. Each eval run counts against the project's daily token limit. Use `QUICK`
and the `RUN` switches for rehearsals.

## What it changes in LaunchDarkly

- Two library skills: `togglebank-dispute-handling` (v1–v3) and `plain-english`.
- A `skills-demo` variation on `account_agent` and on `branch_agent`, copied from `Default` without its Library tools.
  Terraform doesn't manage these variations.
- A user target: `eric-internal-dev` is served `skills-demo`, so the app's **Internal** demo login gets it too.
- A `load_skill` Library tool and a `dispute-answer-quality` judge.

The notebook's **Reset** cell removes the target and the `skills-demo` variations. Add `RESET_SKILLS = True` to delete
the skills as well. Do that after a rehearsal, so the live demo publishes v1, v2 and v3 in front of the audience.

## SDK status

Skill content is read over the REST API (`SKILL_SOURCE = "auto"` falls back to it). The released server SDK already
carries each variation's pinned `skills` references in its payload, which is what the notebook reads. The SDK's own
skills API (`get_skill`, `write_skills`, `watch_skills`) isn't released yet. Once it is, set `SKILL_SOURCE = "sdk"`.
