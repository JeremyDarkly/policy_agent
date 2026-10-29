# CI evals: the LaunchDarkly evals GitHub Action

`.github/workflows/evals.yml` runs `ci_eval.py` in GitHub Actions. It evaluates the AI Config variations the agents
use against datasets stored in LaunchDarkly, and reports the results in the job summary and on the PR. Results are
advisory: failing rows never block a merge.

## What triggers it

| Trigger | When | Datasets |
|---|---|---|
| Pull request | A PR into any branch changes one of the files below | quick |
| Push to `main` | A merge or push to `main` changes one of the files below | quick |
| Schedule | Nightly at 09:00 UTC | full |
| Manual | **Actions > LaunchDarkly evals > Run workflow** | quick, or full if you tick **full** |

The PR and push triggers only fire when the change touches one of these paths:

- `app/backend/src/agents/**`: the agent code
- `infrastructure/launchdarkly/ai_configs_resources.tf`: AI Config prompts, variations and judges
- `infrastructure/launchdarkly/model_configs.tf`: model definitions
- `evals/ci_eval.py`, `evals/ci_targets.toml`, `evals/pyproject.toml`, `evals/uv.lock`: the eval runner and its setup
- `.github/workflows/evals.yml`: the workflow itself

For a PR, GitHub matches these paths against the PR's whole diff, not only the latest commit. Once a PR touches one of
them, every later push to that PR runs the evals again. A new push cancels a PR's run that is still going. Push,
nightly and manual runs each have their own queue, so a push to `main` doesn't cancel the nightly run.

It doesn't run:

- **On PRs from forks.** They get no secrets or AWS credentials.
- **When an AI Config changes in the LaunchDarkly UI.** Edits made there instead of through Terraform show up in the
  next nightly run. A trigger for those changes is planned (#19).

Manual runs take three optional inputs:

- **config**: evaluate one AI Config, for example `brand_agent`. Leave it blank for every target.
- **variation**: evaluate one variation key. Leave it blank for the variation each config serves.
- **full**: use the full datasets.

## What a run does

1. **Signs in to AWS** through GitHub's OIDC token, assuming the role in `AWS_ROLE_ARN`. The models run on Bedrock in
   that AWS account.
2. **Installs** the evals project with `uv sync --locked`.
3. **Downloads the latest results from `main`.** That's the newest unexpired `eval-results-quick` or
   `eval-results-full` artifact from a run on `main`. If there isn't one yet, the comparison column says "not on
   `main`".
4. **Runs `ci_eval.py`** for every target in `ci_targets.toml`, or the chosen config:
   - **Picks the variation.** It reads the AI Config's targeting in `LD_ENVIRONMENT` and evaluates the variation the
     default rule serves, or each variation in a percentage rollout. A target's `variations` list adds more, and
     the manual **variation** input picks one instead.
   - **Generates a response for each row.** The LaunchDarkly AI SDK reads the variation's model and prompt and
     generates on Bedrock. The app doesn't need to be running.
   - **Scores each row.** Triage checks whether the reply would route to the expected specialist. The other agents are
     scored by the `ci-judge-accuracy` and `ci-judge-coherence` judges against each target's threshold.
   - **Compares with `main`.** It matches rows to the downloaded results and lists the ones that newly fail and the
     ones that now pass.
5. **Reports the results:**
   - **Job summary:** a table per config and variation with rows passed, the comparison with `main`, and a verdict,
     followed by the failing rows with the judge's score and reason.
   - **PR comment:** the same summary in one comment, which later runs update in place.
   - **Results artifact:** the `eval-results-quick` or `eval-results-full` JSON, kept for 90 days. The artifacts from
     runs on `main` are what later runs compare with.

The job fails only when a target can't run at all, for example from an API error or a missing dataset. Rows that
fail the eval show as "failed" in the summary but leave the job green.

The repository is public, so the workflow passes `--no-run-links`. The summary gives each LaunchDarkly evaluation run
by name instead of linking to it. Find a run in LaunchDarkly under **Evaluations** by that name.

### How the variations are chosen

> This diagram only illustrates how variations are chosen today. The rules are still open to change.

For each AI Config, `select_variations` in `ci_eval.py` decides which variations to evaluate:

```mermaid
flowchart TD
    start(["One AI Config from ci_targets.toml"]) --> flag{"--variation given?<br/>(manual run's variation input)"}
    flag -- yes --> chosen["Evaluate only that variation<br/>source: chosen"]
    flag -- no --> read["Read the AI Config's targeting<br/>in LD_ENVIRONMENT"]
    read --> on{"Targeting on?"}
    on -- no --> off["Take the off variation"]
    on -- yes --> fall{"What does the default rule serve?"}
    fall -- one variation --> one["Take that variation"]
    fall -- percentage rollout --> split["Take every variation in the rollout"]
    off --> disabled{"LaunchDarkly's disabled<br/>variation?"}
    one --> disabled
    split --> disabled
    disabled -- yes --> skip["Skip it, with a note"]
    disabled -- no --> served["Evaluate it<br/>source: served"]
    served --> listed["Add the target's variations list<br/>source: listed"]
    skip --> listed
    listed --> others{"Do rules or individual targets<br/>serve variations not selected?"}
    others -- yes --> note["Name them in a note;<br/>they aren't evaluated"]
    others -- no --> run(["One evaluation per selected variation"])
    note --> run
    chosen --> run
```

The source (*served*, *listed* or *chosen*) appears next to each variation in the summary table. To evaluate a
variation that only a targeting rule serves, such as a challenger, add it to that target's `variations` list in
`ci_targets.toml`.

## Quick and full datasets

Every target has a full dataset and a quick one (the same key with a `_quick` suffix):

| AI Config | Full | Quick |
|---|---|---|
| `triage_agent` | 48 rows | 12 rows |
| `account_agent`, `branch_agent`, `scheduler_agent`, `brand_agent` | 8 rows each | 3 rows each |

PRs and pushes to `main` use the quick datasets, and the nightly run uses the full ones. LaunchDarkly sets a daily
token limit per member for evaluations, and every generation and judge call counts toward it, even though Bedrock
bills the models to your own AWS account. A full run of every target uses a large share of that limit. Over the limit,
LaunchDarkly rejects new runs with `429 token_limit_exceeded` until the limit resets at 00:00 UTC. An account Admin can
raise or remove the limit under **Settings > Plans and usage**.

Quick and full results are kept as separate artifacts, so each run compares with `main` results from the same
datasets.

## Setup

**Datasets.** Each dataset in `ci_targets.toml` must exist in LaunchDarkly with that key. Upload the matching `.jsonl`
file in the LaunchDarkly UI (**Library > Datasets > New dataset**). The triage datasets are in this folder and the
others are in `ci_datasets/`. Uploading to an existing dataset adds rows rather than replacing them, so create a new
dataset to replace one. `build_specialist_datasets.py` rebuilds the specialist datasets (see [README.md](README.md#ci-evals)).

**Judges.** `ci-judge-accuracy` and `ci-judge-coherence` are AI Configs defined in
`infrastructure/launchdarkly/ai_configs_resources.tf`. Apply them with Terraform before the first run.

**Repository secrets:**

| Secret | What it is |
|---|---|
| `AWS_ROLE_ARN` | An IAM role GitHub can assume through OIDC, allowed to call Bedrock for the models in `infrastructure/launchdarkly/model_configs.tf` |
| `LD_PROJECT_KEY` | The LaunchDarkly project |
| `LD_API_TOKEN` | A LaunchDarkly API access token that can create evaluations and read AI Configs and datasets |
| `LD_SDK_KEY` | The server-side SDK key of the environment |
| `LD_ENVIRONMENT` | The environment whose targeting decides the served variation |
| `LD_API_BASE_URI`, `LD_UI_BASE_URI`, `LD_BASE_URI`, `LD_STREAM_URI`, `LD_EVENTS_URI` | LaunchDarkly endpoints. Leave them unset for production. |

**OIDC trust policy.** The role's trust policy must match the repository's OIDC subject format. Check it with
`gh api repos/<owner>/<repo>/actions/oidc/customization/sub`. If `use_immutable_subject` is true, the subject is
`repo:<owner>@<owner id>/<repo>@<repo id>:...` (the `sub_claim_prefix` it returns), not `repo:<owner>/<repo>:...`.
A trust policy written for the short form fails with "Not authorized to perform sts:AssumeRoleWithWebIdentity". The
role needs to trust both `...:pull_request` (PR runs) and `...:ref:refs/heads/*` (push, nightly and manual runs).

## Running it locally

The same script runs from this folder with an env file:

```sh
uv run python ci_eval.py --env-file ../.env.staging-org --quick
uv run python ci_eval.py --env-file ../.env.staging-org --config brand_agent --variation default
```

Local runs use your own AWS credentials and LaunchDarkly token, and count toward your own daily evaluation token limit.

## Troubleshooting

- **`429 token_limit_exceeded`**: the daily evaluation token limit is used up. Wait for the reset at 00:00 UTC, or
  ask an Admin to raise the limit. Bedrock throttling shows up differently, as a `ThrottlingException` from AWS.
- **"Not authorized to perform sts:AssumeRoleWithWebIdentity"**: the role's trust policy doesn't match the OIDC
  subject. See **OIDC trust policy** above.
- **A dataset shows as empty after upload**: a row is too long for the upload screen (about 4,096 characters).
- **Generation errors on a row**: the summary lists them under row errors. A model can reject a parameter combination
  that the backend never sends. Use `drop_parameters` in `ci_targets.toml` to leave those parameters out.
