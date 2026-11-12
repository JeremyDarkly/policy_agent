# ToggleBank Multi-Agent Support Demo

A multi-agent banking support assistant that demonstrates **LaunchDarkly AI Configs**
end to end: every agent's prompt, model, and tool list is resolved from LaunchDarkly at
runtime, the whole agent topology is provisioned with Terraform, and two online
LLM-as-a-judge evaluators score every response back into LaunchDarkly.

The stack is **LangGraph** (orchestration) + **AWS Bedrock** (models) + **FastAPI**
(backend) + **React/Vite** (UI), all run through Docker Compose.

```
Frontend (Vite, :5173)  ──/api proxy──>  Backend (FastAPI, :8000)  ──>  LangGraph  ──>  Bedrock
                                                    │
                                          LaunchDarkly AI Configs
                                     (prompts, models, tools, judges)
```

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Compose v2 | Everything (including Terraform) runs in containers |
| AWS account with Bedrock access | `us-east-1`, with the models in [Models](#models) enabled |
| AWS CLI with an SSO profile | `~/.aws` is mounted read-only into the backend container |
| LaunchDarkly account with AI Configs | Plus an existing project to provision into |
| LaunchDarkly API access token | Needs write access to the project (used only by Terraform) |

No local Python, Node, or Terraform install is required.

## Quick start

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env                       # fill in the LaunchDarkly + AWS values

# 2. Authenticate to AWS (the backend uses these credentials for Bedrock).
#    Use the literal profile name — $AWS_PROFILE lives in .env, not your shell.
aws sso login --profile <your-profile>

# 3. Provision the LaunchDarkly AI Configs, model configs, tools, and agent graph
docker compose run --rm terraform init
docker compose run --rm terraform apply

# 4. Point each AI Config at its Default variation (Terraform can't — see below)
python3 scripts/set_ai_config_targeting.py

# 5. Run the app
docker compose up
```

Then open:

- **UI** — http://localhost:5173
- **API docs** — http://localhost:8000/docs
- **Health** — http://localhost:8000/health

Steps 1–4 are one-time. On later runs, `aws sso login` + `docker compose up` is enough.

> Step 4 is required, not optional. `terraform apply` creates each AI Config with its
> fallthrough still on variation index 0 — LaunchDarkly's auto-added *disabled*
> variation — so the agents resolve to nothing until it's moved to `Default`. The
> script is idempotent; `--dry-run` reports without writing.

> The backend fails fast if the AI Configs don't exist in your project — there are no
> hardcoded prompts to fall back to, so run the Terraform apply before `docker compose up`.

## Environment variables

All of these live in `.env` (copied from `.env.example`). Compose reads that file for
variable substitution and also bind-mounts it into the backend container.

| Variable | Required | Used by | Description |
|---|---|---|---|
| `LAUNCHDARKLY_SDK_KEY` | yes | backend | Server-side SDK key (`sdk-...`) |
| `LAUNCHDARKLY_PROJECT_KEY` | yes | Terraform | Project to provision into (`TF_VAR_project_key`) |
| `LAUNCHDARKLY_ACCESS_TOKEN` | yes | Terraform | API token (`api-...`) with project write access |
| `LAUNCHDARKLY_ENVIRONMENT` | yes | backend + Terraform | Environment key. Drives observability tagging **and** which LD environment Terraform turns each config on in (`TF_VAR_target_environment`). Default `test` |
| `LAUNCHDARKLY_ENABLED` | no | backend | Set `false` to disable the LD SDK. Default `true` |
| `AWS_PROFILE` | yes | backend | AWS SSO profile name for Bedrock |
| `AWS_REGION` | no | backend | Default `us-east-1` |
| `DEMO_USER_NAME` | no | backend + UI | Display name for the demo user (exported to the UI as `VITE_DEMO_USER_NAME`). Default `Demo User` |
| `LLM_PROVIDER` | no | backend | Provider fallback. Default `bedrock` |
| `LLM_MODEL` | no | backend | Model fallback used only if an AI Config carries no model. Default `claude-3-5-sonnet` |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | no | backend | Only if you point the agents at those providers directly |
| `AZURE_FOUNDRY_ENDPOINT` / `AZURE_FOUNDRY_API_KEY` | no | backend | Only for variations on an Azure AI Foundry model config (see [Serving from Azure AI Foundry](#serving-from-azure-ai-foundry)). The endpoint is the resource URL; any path after the host is ignored |
| `GOOGLE_API_KEY` | no | backend | Only for variations on a model config with provider `Gemini` (for example `gemini-4-carbon`). A Gemini API key from Google AI Studio, on a paid-tier project for Gemini 4 Carbon. Put it in the env file the backend mounts (`ENV_FILE`); `docker-compose.yml` doesn't pass it, because an empty value from compose would hide the file's |

`.env` is gitignored. Don't commit it.

## Provisioning LaunchDarkly with Terraform

The Terraform config in `infrastructure/launchdarkly/` is the source of truth for the
demo's LaunchDarkly resources. It runs through the opt-in `terraform` Compose service,
which maps your `.env` values to `TF_VAR_*` inputs and persists state on the host at
`infrastructure/launchdarkly/` (gitignored).

```bash
docker compose run --rm terraform init       # first time only
docker compose run --rm terraform plan       # preview changes
docker compose run --rm terraform apply      # create / update
docker compose run --rm terraform destroy    # tear everything down
docker compose run --rm terraform output     # show managed keys and IDs
```

### What gets created

| Resource | Count | File |
|---|---|---|
| AI Configs (5 agents + 5 judges) | 10 | `ai_configs_resources.tf` |
| AI Config variations (`default` on each, plus `tool-calling` on `scheduler_agent` and `branch_agent`; published) | 12 | `ai_configs_resources.tf` |
| Reusable model configs | 6 | `model_configs.tf` |
| AI tool definitions (20 ToggleHealth, plus `get_available_slots` and `find_branches`) | 22 | `tools.tf` |
| AI agent graph (`banking_agent_graph`) | 1 | `agent_graph.tf` |
| Jev judge AI Config (`ai-judge-jev`) and its metrics | 1 + 8 | `jev_judge.tf` |

`ai-judge-jev`'s two variations (`jev-alongside`, `jev-only`) are not in Terraform: they need
model config `TypeSafe.jev-1.14.0` plus questions in `model.custom`, and the provider (3.1.5) takes either a model
config key or an inline model, with no custom parameters. Run `python3 evals/setup_jev_judge.py` after `terraform
apply` to create them. In a project where that script already created the config and metrics, `jev_judge.tf` lists
the `terraform import` commands. Each config is created with a `Default` variation, and
LaunchDarkly auto-adds a "disabled" variation ahead of it — so variation index 0 is
`disabled` and index 1 is `Default`.

### Required after apply: AI Config targeting

`apply` leaves every config's fallthrough on index 0 (`disabled`), so the agents resolve
to nothing. Fixing that is **not** expressible in Terraform:

- LaunchDarkly rejects writes to AI Config-backed flags through the feature flag API:
  `401 Unauthorized: {"code":"unauthorized","message":"AI flags may not be modified directly."}`
- The LaunchDarkly Terraform provider (v3.1.5, latest 3.x) exposes no AI Config
  targeting resource — `launchdarkly_ai_config` has no `on`/environment attributes.

So the targeting step runs against the AI Configs API instead:

```bash
python3 scripts/set_ai_config_targeting.py             # move fallthrough to Default
python3 scripts/set_ai_config_targeting.py --dry-run   # report without writing
```

It reads `.env` for the token, project, and environment, and is idempotent. The
equivalent manual step is toggling each of the 7 configs to serve `Default` in the
LaunchDarkly UI's Targeting tab.

### Overriding the models

If your AWS account doesn't have all the default Bedrock models enabled, force one
model across every variation:

```bash
docker compose run --rm \
  -e TF_VAR_model_override=us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  terraform apply
```

### Tool attachment (previously a known gap — now works)

Each variation declares `tool_keys`, and provider v3.1.5 **does** attach them to the
served variation. Verified against a live apply: the tools on each served variation
match the declared counts exactly. Most of them are ToggleHealth leftovers that no code
implements; the model is only ever offered tools the backend implements (see
[Tool calling](#tool-calling)). `scheduler_agent`'s and `branch_agent`'s `default`
variations attach none.

Earlier provider versions sent `tool_keys` to an API field that was accepted and
ignored, so no manual attachment step is needed anymore. The `ai_config_tool_attachments`
output in `outputs.tf` is a leftover from that workaround and is now redundant.

## Architecture

LangGraph `StateGraph` (`app/backend/src/graph/workflow.py`) drives the flow. Triage
classifies the query and routes to one specialist — or straight to brand voice for
general questions — and the specialist hands off to brand voice for the final
customer-facing answer. Judges then score that answer asynchronously.

```
                        ┌─────────────────┐
                        │   USER QUERY    │
                        └────────┬────────┘
                                 │
                        ┌────────▼────────┐
                        │ TRIAGE          │
                        │ (triage_agent)  │
                        └────────┬────────┘
                                 │
         ┌───────────────┬───────┴───────┬───────────────┐
         │               │               │               │
 ┌───────▼──────┐ ┌──────▼───────┐ ┌─────▼────────┐      │
 │ ACCOUNTS     │ │ BRANCH/ATM   │ │ SCHEDULER    │      │ (general
 │ account_agent│ │ branch_agent │ │ scheduler_   │      │  questions)
 │              │ │              │ │ agent        │      │
 └───────┬──────┘ └──────┬───────┘ └─────┬────────┘      │
         └───────────────┴───────┬───────┴───────────────┘
                                 │
                        ┌────────▼────────┐
                        │ BRAND VOICE     │
                        │ (brand_agent)   │
                        └────────┬────────┘
                                 │
                        ┌────────▼────────┐
                        │ ONLINE JUDGES   │
                        │ accuracy +      │
                        │ coherence,      │
                        │ and/or Jev      │
                        │ → LaunchDarkly  │
                        └─────────────────┘
```

The same topology is mirrored in LaunchDarkly as `banking_agent_graph`. That resource is
a LaunchDarkly-side *representation* for monitoring — the app orchestrates in code via
LangGraph and does not read it at runtime.

### Agents

| Component | AI Config key | Mode | Model | Purpose |
|---|---|---|---|---|
| Triage | `triage_agent` | agent | Claude Sonnet 5; `jev-choice` uses TypeSafe Jev, a decision model, in place of an LLM ([evals/README.md](evals/README.md#jev-triage)) | Classify intent, route to a specialist |
| Accounts & Products | `account_agent` | agent | Llama 3.1 70B | Account types, balances, fees, interest, product terms |
| Branch & ATM | `branch_agent` | agent | Claude Haiku 4.5 | Find nearby branches and ATMs, their services and hours |
| Scheduler | `scheduler_agent` | agent | Amazon Nova Pro | Books time with a banker, requests callbacks |
| Brand Voice | `brand_agent` | agent | Claude Haiku 4.5 | Rewrites the specialist answer in brand voice (terminal node) |

There is **no RAG** in the current build. The customer's account/location profile
(`app/backend/src/utils/user_profile.py`) is the source of truth the specialists answer
from — and what the accuracy judge grades against.

### Judges

Both judges use LaunchDarkly's `judge` config mode, which backs them with an
auto-generated metric. They run in background threads on every response.

| Judge | AI Config key | Metric key | Passing threshold |
|---|---|---|---|
| Accuracy | `ai-judge-accuracy` | `$ld:ai:judge:accuracy` | 0.8 |
| Coherence | `ai-judge-coherence` | `$ld:ai:judge:coherence` | 0.7 |
| Jev (five classifications in one request) | `ai-judge-jev` | `$ld:ai:judge:jev-accuracy`, `jev-hallucination-free`, `jev-answers-question`, `jev-coherence`, `jev-tone-ok` | Per question, in the variation (below) |

`ai-judge-jev` grades the same reply with Jev, TypeSafe's decision model, instead of an LLM. One request answers
five questions, and each answer is sent as its own metric on the `brand_agent` tracker. The served variation
decides what runs, with no redeploy:

| Variation | Behavior |
|---|---|
| `disabled` (default rule) | Jev is not called. The two LLM judges run as above. |
| `jev-alongside` | Jev runs as well as the LLM judges. The chat panel shows both, and `jev-judge-disagreement` records whether their pass/fail verdicts differ. |
| `jev-only` | Jev replaces the LLM judges. Accuracy passes when Jev's `accuracy` and `hallucination` pass; coherence passes when `coherence` and `tone` pass. |

| Question | Type | Value sent | Passes when | Metric |
|---|---|---|---|---|
| `accuracy` | score, 3 levels | score / 2 | ≥ 0.8 | `jev-accuracy` |
| `hallucination` | noul | 1 − noul | noul < 0.5 | `jev-hallucination-free` |
| `answers_question` | noul | noul | ≥ 0.5 | `jev-answers-question` |
| `coherence` | score, 3 levels | score / 2 | ≥ 0.7 | `jev-coherence` |
| `tone` | choice | probability of `warm_professional` | choice is `warm_professional` | `jev-tone-ok` |

The questions, thresholds and metric keys live in the variations' `model.custom.questions`, so they are edited in
LaunchDarkly. A judge AI Config holds one `evaluationMetricKey`, so the other four metric keys are kept there too
(#57). The Jev path has its own guardrail metrics: `jev-judge-error-rate`, `jev-judge-cost-cents`,
`jev-judge-disagreement`, and latency from the `ai-judge-jev` tracker's duration. A Jev failure never blocks a chat:
it sends `jev-judge-error` = 1, and the LLM judges still run unless `jev-only` is served. The backend needs
`TYPESAFE_API_KEY`. Setup, the labeled pilot and the demo steps are in
[evals/README.md](evals/README.md#jev-judge).

### Served model

A variation names the model to request, but the API can answer with another one. An alias resolves to a dated
snapshot, a Bedrock prompt router picks a model, or the configured ID is wrong. After every call, `ModelInvoker`
sends two custom events with the AI Config's context:

| Event | When | Data |
|---|---|---|
| `ai-model-served` | Every call (value `1`) | `configKey`, `variationKey`, `provider`, `requestedModel`, `servedModel`, `source` |
| `ai-model-mismatch` | Every call whose response named the model: value `1` when the served model isn't the requested one (or the requested one made more specific), `0` when it is | Same as above |
| `ai-content-filtered` | A call whose response a content filter withheld (Azure `finish_reason` `content_filter`), value `1`. The call is also tracked as an AI Config error and raises | Same as above |

For an Azure AI Foundry deployment, `requestedModel` is the model config's `baseModel` and the data also carries
`deployment`, the deployment name sent in the request (see [Serving from Azure AI Foundry](#serving-from-azure-ai-foundry)).

`source` says where `servedModel` came from:
- **`response`:** the API named the model. OpenAI and Anthropic always do.
- **`router_trace`:** a Bedrock prompt router reported the model it chose. A router is expected to pick a model, so no `ai-model-mismatch` is sent.
- **`requested_fallback`:** the API didn't say, so there's nothing to check and no `ai-model-mismatch` is sent. Bedrock Converse doesn't name the model unless a router is used.

The comparison rule is in `app/backend/src/utils/served_model.py`. Routing prefixes (Bedrock region profiles, ARN
paths, `provider/`) are ignored. After that, the served model has to be the requested one or the requested one made
more specific. An alias may resolve to a snapshot, so `gpt-4o` served by `gpt-4o-2024-08-06` is a match. A pinned
snapshot or version must be served exactly, so `gpt-4o-2024-08-06` served by `gpt-4o-2024-11-20` is a mismatch, and
so is a Bedrock `-v1:0` served by `-v2:0`. A numeric metric that averages `ai-model-mismatch` is the
mismatch rate among calls whose served model was checked, which can serve as an experiment metric or a guarded-rollout
guardrail. A variation whose calls can't be checked (Bedrock without a router) has no mismatch data rather than a rate
of 0. The OpenClaw plugin sends the same events (see `openclaw/README.md`). Tests:
`cd app/backend && python -m pytest tests`.

### Demo users

The UI ships a "fake login" switcher (`app/ui/src/lib/demoUser.ts`) with two presets
that map to different LaunchDarkly user keys, so the same AI Configs can resolve to
different variations depending on who is signed in:

| Preset | LD user key | Context |
|---|---|---|
| Customer | `<name>-commercial-plan` | Everyday Current Account holder |
| Internal | `<name>-internal-dev` | Internal/employee user, Premier Current Account |

`<name>` is `DEMO_USER_NAME` as a slug: `Jane Doe` gives `jane-doe-commercial-plan`. With no name set, the keys
start with `demo-user`.

### Spend-based model downgrade

Once a customer's running model spend passes a threshold, `triage_agent` serves a cheaper model. A targeting
rule in LaunchDarkly makes the switch, not app code, so you can change the threshold in the UI without a redeploy.

- **The attribute.** The backend keeps each `user_key`'s total spend in cents in memory
  (`app/backend/src/utils/spend_ledger.py`). Triage, account, branch, scheduler and brand voice each add their
  cost as their call finishes; judges don't count. Every AI Config evaluation carries the current total as the
  number `running_spend_cents`, so a config evaluated later in the same request sees the calls before it. Each
  request also sends a `running-spend-cents` event with the new total, and returns it as
  `metrics.runningSpendCents`.
- **The rules.** `scripts/setup_spend_rule.py` adds two rules to `triage_agent`, above the default rule:

  | Rule | If | Serves |
  |---|---|---|
  | A | `plan` is `commercial` and `running_spend_cents >= 3` (and `kind` is `user`) | `Haiku 4.5, tuned prompt` |
  | B | `plan` is `commercial` | `Default` (Claude Sonnet 5) |

  Only the Customer demo login sends `plan: commercial`, so other users, and the eval scripts unless they set
  it, keep the default rule.

  ```bash
  python scripts/setup_spend_rule.py --env-file .env.staging-org --dry-run
  python scripts/setup_spend_rule.py --env-file .env.staging-org                     # Rule A serves Haiku now
  python scripts/setup_spend_rule.py --env-file .env.staging-org --release guarded   # or release it with a guarded rollout
  python scripts/setup_spend_rule.py --env-file .env.staging-org --threshold-cents 10
  python scripts/setup_spend_rule.py --env-file .env.staging-org --status
  python scripts/setup_spend_rule.py --env-file .env.staging-org --remove
  ```

  `--release guarded` points Rule A at `Default`, then starts a guarded release from `Default` to Haiku, with
  `triage-routing-accuracy`, `triage-escalation-rate`, `triage-error-rate` and `triage-latency-ms` as
  guardrails and automatic rollback. LaunchDarkly refuses the release until `triage_agent` has recent
  evaluations, so chat once or run the load script below first. `--stop-release` stops it and rolls back.
- **The demo.** Sign in as the Customer and ask branch questions in one chat. At current token counts a branch
  turn costs about 1.07¢, so turns 1–3 show `claude-sonnet-5` on the triage node and turn 4 shows
  `claude-haiku-4-5`. The chat's Response Metrics line shows the running spend. **Reset spend** in the user
  menu (or `POST /api/spend/reset`) starts the demo again. Change the threshold in the rule and the switch
  moves on the next request.
- **Load.** `evals/run_triage_experiment.py --users 50 --turns-per-user 10` sends simulated commercial users
  through the triage node, 10 questions each. They cross the threshold partway through, so both sides of Rule A
  get routing-accuracy events. The script prints which variation each turn number was served.
- **Caveats.** The ledger is per backend process. It is lost on restart, isn't shared across replicas, and only
  goes up until reset. It uses the app's own prices (`calculate_model_cost`), not LaunchDarkly's. The AI Config
  Monitoring tab's cost column reads 0 for these configs (#48), so don't use it to show the savings; use the
  `triage-cost-cents` metric instead.

### Serving from Azure AI Foundry

`branch_agent` can serve a model hosted in Azure AI Foundry alongside its Bedrock `default` variation. Targeting
decides which cloud answers, with no redeploy, and the AI Config's Monitoring tab shows both variations' latency,
tokens and errors side by side.

**How it fits together.** The global model catalog has no Azure provider, so Terraform creates a project model config,
`azure-foundry-branch`, with provider `Azure AI Foundry`, the Foundry deployment name as its model ID, and the model
the deployment runs as the custom parameter `baseModel`. The `branch_agent` variation `azure-foundry` uses it, with the
same instructions as `default` plus the TOOLS paragraph, and the `find_branches` tool (see [Tool calling](#tool-calling)). When a variation's provider is `Azure AI Foundry` (or `azure`,
`azure openai`, `foundry`; see `AZURE_PROVIDERS` in `app/backend/src/utils/llm_config.py`), the backend calls the
deployment through `ChatOpenAI` on the resource's OpenAI-compatible v1 route (`<resource>/openai/v1/`), which serves
OpenAI and non-OpenAI models (Grok, Llama, DeepSeek) and needs no `api-version`. Tokens, duration, success and errors
go through the same `ModelInvoker` tracker calls as Bedrock.

**Setup.**

1. Add to `.env`:

   | Variable | Used by | Value |
   |---|---|---|
   | `AZURE_FOUNDRY_ENDPOINT` | backend | Resource URL, e.g. `https://<resource>.services.ai.azure.com`. A request URL copied from the portal works too; only the scheme and host are used |
   | `AZURE_FOUNDRY_API_KEY` | backend | The resource's API key. Entra ID is not supported yet |
   | `AZURE_FOUNDRY_DEPLOYMENT` | Terraform | Deployment name, e.g. `grok-4.6`. Unset: the Azure model config and variation are not created |
   | `AZURE_FOUNDRY_BASE_MODEL` | Terraform | The model the deployment runs, e.g. `grok-4.6` or `gpt-4o`. Stored as `baseModel` |

2. Create the model config and variation, add the demo rule, and register the metrics:

   ```bash
   docker compose run --rm terraform apply
   python3 scripts/set_ai_config_targeting.py --azure-demo   # internal demo user -> azure-foundry
   python3 scripts/register_metrics.py                       # ai-model-mismatch, ai-content-filtered
   docker compose up -d backend                              # picks up the AZURE_FOUNDRY_* variables
   ```

   `--azure-demo` adds one rule to `branch_agent`: a user key ending in `-internal-dev` (the UI's Internal preset) gets
   `azure-foundry`; everyone else gets the fallthrough, `default`. Add `--config branch_agent` to leave the other
   configs' targeting alone.

3. Send traffic to both variations: `python3 scripts/foundry_load.py` (40 requests, alternating the two demo users;
   `-n` to change). It prints, per user, how many requests reached the branch agent, errors, the served model, and
   branch-agent latency and tokens.

**Keep the Azure variables loaded for every Terraform run.** Once the Azure model config and variation exist, a
`terraform plan` or `apply` without `AZURE_FOUNDRY_DEPLOYMENT` stops with `Error: Instance cannot be destroyed` on
`azure_foundry_branch` or `branch_agent_azure_foundry`. That is deliberate: without it, the run would delete the
variation even while targeting serves it. Load the variables and run it again. If they live in a separate env file:

```bash
docker compose --env-file .env --env-file .env.azure run --rm terraform plan
```

**Removing the Azure resources.** Move `branch_agent`'s rules and fallthrough off `azure-foundry` first. Then set
`prevent_destroy = false` on both resources, apply without `AZURE_FOUNDRY_DEPLOYMENT`, and set it back to `true`.

**Moving traffic.** In the LaunchDarkly UI, `branch_agent` → Targeting: change the rule to a percentage rollout, or
change the fallthrough to `azure-foundry`, and the next request is served from Azure. Switching back is the same edit.
For a guarded rollout of `azure-foundry` on the default rule, use `ld_autogen__ai-completion-error`, the
`ai-model-mismatch` metric and `ai-content-filtered` as guardrails, randomized by `user`. The completion-time metric
can't be one: it is randomized by `request`, and a user-randomized rollout rejects it (`Metric ... does not have
randomization unit: user`). Watch latency on the Monitoring tab instead. A rollout splits traffic by user key, so drive
it with many keys: `python3 scripts/foundry_load.py -n 400 --users 150 --customers-only`.

**What the mismatch check compares.** A Foundry model ID is a deployment name, which need not be a model name. The
backend therefore checks the served model against the model config's `baseModel`, not the deployment name. Azure
OpenAI models answer with a dated version (`gpt-4o-2024-11-20`), which matches `baseModel = "gpt-4o"` under the usual
snapshot rule. Without `baseModel`, the deployment name is compared, so a deployment named differently from its model
reads as a mismatch on every call.

**Things to expect.**

- **Reasoning tokens.** Some models (Grok on Foundry) report reasoning tokens outside `completion_tokens`.
  `ModelInvoker` counts them as output tokens, up to the response's `total_tokens`, so output tokens on
  `azure-foundry` are much higher than on Haiku. That is expected.
- **Latency.** A reasoning model spends seconds reasoning even on short replies (3-5 s for a one-word reply from
  `grok-4.6`), so compare its latency with its own earlier runs rather than with Bedrock Haiku.
- **Content filter.** A response Azure withholds comes back with `finish_reason` `content_filter`; it counts as an
  AI Config error and sends `ai-content-filtered`.
- **Cost.** No costs are set on the Azure or Bedrock model configs: LaunchDarkly currently reports `$0` cost for AI
  Configs in the Monitoring tab and the metrics API (#69).
- **Quota.** A deployment with a low tokens-per-minute limit returns `429` under load; those count as errors on
  `azure-foundry`. Keep `-n` small or raise the limit.

### Tool calling

`scheduler_agent` and `branch_agent` each have a `tool-calling` variation on which the model looks things up itself
instead of reading data the backend prefetched. Targeting decides which path answers, with no redeploy.

| Config | `default` | `tool-calling` |
|---|---|---|
| `scheduler_agent` | The backend calls `get_available_slots()` and puts 10 slots in the prompt | The model calls the `get_available_slots` tool (`days_ahead` 1-14) and offers only the slots it returns |
| `branch_agent` | Location context only; the model has no branch data | The model calls `find_branches` (`location`) on a made-up directory of 8 ToggleBank locations in San Francisco, Oakland, Boston and New York |

`branch_agent`'s `azure-foundry` variation (see [Serving from Azure AI Foundry](#serving-from-azure-ai-foundry)) attaches
`find_branches` too, with the same instructions as `tool-calling`, so the same loop runs on Azure through
`ChatOpenAI.bind_tools`. Expect it to be slower: a reasoning model such as Grok spends seconds per round, and a tool
call adds a round.

**How it fits together.** The tools are Library tools in `tools.tf` (snake_case keys: Amazon Nova can't call a tool
whose name contains a hyphen, #74). Their implementations are in `app/backend/src/tools/registry.py`, which the CI
evals use too. When the served variation attaches tools the backend implements, the node runs
`run_tool_loop` (`app/backend/src/utils/tool_loop.py`): the model is bound to them (`BedrockConverseLLM.bind_tools`
sends the Converse `toolConfig`), each call is run and its result sent back, for at most 4 model calls. The loop is one
tracked generation (duration, success, summed tokens), and each call sends:

| Event | Value | Shows up |
|---|---|---|
| `$ld:ai:tool_call` (`tracker.track_tool_call`) | 1 per call, with `toolKey` | The config's Monitoring tab |
| `ai-tool-call-error` | 1 when the call raised or found nothing (`not_found`), else 0 | A custom metric, for a guarded rollout |
| `scheduler-unlisted-slot` | Per scheduler reply: 1 when it names a slot ID no tool call returned, else 0 | Same |

The UI terminal lists each call on the agent's node, and the chat API returns them in `agentFlow[].tool_calls`.

**Demo.**

```bash
python3 scripts/set_ai_config_targeting.py --config scheduler_agent --config branch_agent --tools-demo --rules-only
python3 scripts/tool_load.py -n 40      # half employee (tool-calling), half customer (default)
```

`--tools-demo` adds a rule to both configs: `role` is `employee` (the UI's employee login) gets `tool-calling`, everyone
else the fallthrough. `--rules-only` leaves the default rules alone. On `branch_agent` the new rule goes after the
Azure demo rule, which also matches the employee login, so that login gets `azure-foundry` there, which calls the tool
on Azure (`--tools-demo-first` puts the tools rule first instead). `tool_load.py` uses employee keys that the Azure
rule doesn't match, so `tool-calling` gets traffic too. It prints, per served model, tool calls per request, failed
calls, and whether questions that need a tool got one.

**In CI.** `evals/ci_eval.py` runs a variation that attaches tools with those tools and the backend's implementations,
on the `*_tools_dataset` datasets, and grades the trajectory with the `ci-judge-tool-use` judge. See
[evals/CI_EVALS.md](evals/CI_EVALS.md).

### Observability

The backend sends traces to LaunchDarkly observability (**Monitor > Traces**, and the LLM
and Conversations views) through `ObservabilityPlugin` from `ldobserve`. It is set up in
`app/backend/src/utils/observability.py`, which `server.py` calls before any LLM imports.

- **Auto-instrumentation.** Constructing the plugin turns on every installed
  OpenTelemetry instrumentor: FastAPI, LangChain (one LLM span per model call, including
  Bedrock through `BedrockConverseLLM`), OpenAI, Anthropic, and HTTP clients. botocore is
  turned off so Bedrock calls don't get a second span.
- **Spans added by the app.** `run_workflow` wraps the graph in a `multi-agent-workflow`
  span, each node runs in an `agent.<name>` span, and the judges run in an
  `evaluate brand_voice` span in the same trace, with each score as a
  `gen_ai.evaluation.result` event.
- **AI Config links.** Each agent evaluates its AI Config inside its node span, so the
  plugin's flag hook adds a `feature_flag` event that links the trace to the config's
  Monitoring tab. `ModelInvoker` also sets `ld.ai_config.key` on that span.
- **Conversations.** Every span gets `gen_ai.conversation.id`, taken from the
  `conversationId` the chat widget sends (one per widget session), or the request ID
  when it is missing.
- **Other instances.** For a non-production LaunchDarkly instance, set
  `LAUNCHDARKLY_OTLP_ENDPOINT` (OTLP gRPC) and `LAUNCHDARKLY_OBSERVABILITY_BACKEND_URL`
  (sampling config) next to the `*_URI` variables. Leave them unset for production.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | Run the full workflow, return the answer with metrics |
| `POST` | `/api/chat/stream` | Server-sent-events variant with per-stage updates |
| `GET` | `/api/logs/stream` | SSE feed of pipeline logs (drives the UI terminal) |
| `GET` | `/api/evaluation/{request_id}` | Judge scores for a completed request |
| `POST` | `/api/feedback` | Send thumbs up/down to LaunchDarkly as feedback |
| `GET` | `/api/spend/{user_key}` | A user's running model spend in cents |
| `POST` | `/api/spend/reset` | Set one user's spend (`{"userKey": ...}`) or everyone's (`{}`) back to 0 |
| `GET` | `/api/token-status` | AWS SSO token expiry status |
| `GET` | `/health` | Health check |

## Project structure

```
policy_agent/
├── app/
│   ├── backend/                       # FastAPI + LangGraph service (:8000)
│   │   ├── server.py                  # API, SSE streaming, metrics, feedback
│   │   ├── requirements.txt
│   │   ├── Dockerfile
│   │   └── src/
│   │       ├── agents/                # triage_router, account_specialist,
│   │       │                          # branch_specialist, scheduler_specialist,
│   │       │                          # brand_voice_agent
│   │       ├── graph/                 # workflow.py (StateGraph), state.py
│   │       ├── evaluation/            # judge.py, jev_judge.py, agent_evaluator.py (G-Eval)
│   │       ├── tools/                 # calendar.py (appointment slots/booking)
│   │       └── utils/                 # LD client, observability, Bedrock invoker,
│   │                                  # model config resolution, user profiles,
│   │                                  # AWS SSO + token monitoring
│   └── ui/                            # React + Vite + shadcn/ui (:5173)
│       ├── src/
│       │   ├── pages/                 # Index, NotFound
│       │   ├── components/            # ChatWidget, Terminal, ui/ (shadcn)
│       │   └── lib/demoUser.ts        # Demo login presets → LD context
│       ├── vite.config.ts             # /api proxied to http://backend:8000
│       └── Dockerfile
├── infrastructure/launchdarkly/       # Terraform: AI Configs, models, tools, graph
│   ├── ai_configs_resources.tf
│   ├── model_configs.tf
│   ├── tools.tf
│   ├── agent_graph.tf
│   ├── variables.tf  outputs.tf  main.tf  versions.tf
├── scripts/
│   ├── set_ai_config_targeting.py      # post-apply: point fallthrough at Default (--azure-demo, --tools-demo: demo rules)
│   ├── register_metrics.py             # post-apply: ai-model-mismatch and ai-content-filtered metrics
│   ├── foundry_load.py                 # traffic for branch_agent's Bedrock and Azure variations
│   └── tool_load.py                    # traffic for the tool-calling variations
├── docker-compose.yml                 # backend + frontend + terraform (tools profile)
├── commands.md                        # Command cheat sheet
└── .env.example
```

## Models

Enable these in Bedrock (`us-east-1`), or set `TF_VAR_model_override` to a model you do
have. Model configs are reusable resources referenced by variation via
`model_config_key`.

| Model config key | Bedrock model id | Params |
|---|---|---|
| `claude-sonnet-5` | `us.anthropic.claude-sonnet-5` | defaults |
| `llama-3-1-70b-accounts` | `us.meta.llama3-1-70b-instruct-v1:0` | `temperature=0.1` |
| `claude-haiku-4-5-branch` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | `temperature=0.1` |
| `nova-pro` | `us.amazon.nova-pro-v1:0` | defaults |
| `claude-haiku-4-5-brand-voice` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | `temperature=0.9`, `max_tokens=10000` |
| `claude-sonnet-4` | `us.anthropic.claude-sonnet-4-20250514-v1:0` | `temperature=0.1` |
| `azure-foundry-branch` | Azure AI Foundry deployment `AZURE_FOUNDRY_DEPLOYMENT` (only when set) | `temperature=0.1`, custom `baseModel` |

Every model config is a ForceNew resource in the provider — editing one replaces it
rather than updating in place.

## Development

```bash
docker compose up                  # both services
docker compose up backend          # backend only
docker compose logs -f backend     # tail backend logs
docker compose restart backend     # pick up backend code changes
docker compose down                # stop
```

- **Frontend** hot-reloads: `app/ui/src` and `app/ui/public` are bind-mounted, and
  `node_modules` is protected by an anonymous volume.
- **Backend** does *not* auto-reload. The source is bind-mounted, but uvicorn runs
  without `--reload`, so restart the container after editing Python.
- **Rebuild** after dependency changes: `docker compose build backend` (or `frontend`).

### UI tests

```bash
docker compose exec frontend npm run test    # vitest
docker compose exec frontend npm run lint    # eslint
```

### Running outside Docker

Supported but not the primary path. The backend needs **Python 3.12+** (the code uses
PEP 701 multiline f-strings, which 3.11 cannot parse):

```bash
pip install -r app/backend/requirements.txt
python app/backend/server.py          # run from the repo root so .env is found
```

For the frontend, `app/ui/vite.config.ts` proxies `/api` to `http://backend:8000` — the
Compose service hostname. Point it at `http://localhost:8000` before running
`npm ci && npm run dev` outside Docker.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Workflow fails at the triage step | AI Configs missing from your project. Run `docker compose run --rm terraform apply` |
| Agents resolve to nothing after a clean apply | Fallthrough is still on the `disabled` variation. Run `python3 scripts/set_ai_config_targeting.py` |
| Terraform: `401 "AI flags may not be modified directly"` | You're on a version of the config that still has `launchdarkly_feature_flag_environment` resources. LaunchDarkly blocks the flag API for AI Config flags — use `scripts/set_ai_config_targeting.py` instead |
| `docker: unknown command: docker compose` | Compose v2 isn't installed. `brew install docker` gives only the CLI client — no daemon and no Compose plugin. Install a runtime (Colima, OrbStack, Docker Desktop) |
| `dial unix /var/run/docker.sock: no such file or directory` | No container runtime is running. With Colima: `colima start` |
| `[Errno 30] Read-only file system: /root/.aws/sso/cache/...` | botocore can't persist a refreshed SSO token. `docker-compose.yml` overlays that cache dir as writable — make sure you have that mount |
| `ExpiredTokenException` from Bedrock | AWS SSO session expired. Run `aws sso login --profile <your-profile>` on the host; the container reads the refreshed `~/.aws` |
| Terraform: `LAUNCHDARKLY_PROJECT_KEY must be set in .env` | The `terraform` service requires it — no default, so a missing value fails loudly instead of targeting the wrong project |
| Terraform: `403 forbidden` on every create | `LAUNCHDARKLY_ACCESS_TOKEN` is read-only. A personal token can't exceed its owner's member role, so a Reader can't mint a writer token — you need Writer on the member, or a service token |
| `AccessDeniedException` on a model | Either the model isn't enabled in your account (use `TF_VAR_model_override`), or your IAM role lacks `bedrock:InvokeModel`. If *every* model is denied it's IAM, and no override will help — check with `aws bedrock-runtime converse --model-id <id> ...` |
| UI loads but chat calls 502 | Backend isn't up yet, or crashed on startup. Check `docker compose logs backend` |
| Config changes not showing up | Terraform writes to the environment in `LAUNCHDARKLY_ENVIRONMENT`; confirm the backend's SDK key belongs to that same environment |

## License

MIT
