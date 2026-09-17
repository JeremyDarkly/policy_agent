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
| AI Configs (5 agents + 2 judges) | 7 | `ai_configs_resources.tf` |
| AI Config variations (`default`, published) | 7 | `ai_configs_resources.tf` |
| Reusable model configs | 6 | `model_configs.tf` |
| AI tool definitions | 20 | `tools.tf` |
| AI agent graph (`banking_agent_graph`) | 1 | `agent_graph.tf` |

41 resources total. Each config is created with a single `Default` variation, and
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
match the declared counts exactly (triage 7, accounts 7, branch 6, scheduler 5, brand
voice 4; the two judges declare none).

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
                        │ coherence       │
                        │ → LaunchDarkly  │
                        └─────────────────┘
```

The same topology is mirrored in LaunchDarkly as `banking_agent_graph`. That resource is
a LaunchDarkly-side *representation* for monitoring — the app orchestrates in code via
LangGraph and does not read it at runtime.

### Agents

| Component | AI Config key | Mode | Model | Purpose |
|---|---|---|---|---|
| Triage | `triage_agent` | agent | Claude Sonnet 5 | Classify intent, route to a specialist |
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

### Demo users

The UI ships a "fake login" switcher (`app/ui/src/lib/demoUser.ts`) with two presets
that map to different LaunchDarkly user keys, so the same AI Configs can resolve to
different variations depending on who is signed in:

| Preset | LD user key | Context |
|---|---|---|
| Customer | `eric-commercial-plan` | Everyday Current Account holder |
| Internal | `eric-internal-dev` | Internal/employee user, Premier Current Account |

### Observability

The backend initializes OpenTelemetry **before** any LLM imports and exports traces to
**LaunchDarkly Monitor** via `ObservabilityPlugin` from `ldobserve`. Bedrock, FastAPI,
botocore, and LangChain are auto-instrumented; workflow nodes add explicit parent spans,
and the model invoker annotates spans with `ld.ai_config.key` so traces correlate to AI
Configs. See `app/backend/src/utils/observability.py`.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | Run the full workflow, return the answer with metrics |
| `POST` | `/api/chat/stream` | Server-sent-events variant with per-stage updates |
| `GET` | `/api/logs/stream` | SSE feed of pipeline logs (drives the UI terminal) |
| `GET` | `/api/evaluation/{request_id}` | Judge scores for a completed request |
| `POST` | `/api/feedback` | Send thumbs up/down to LaunchDarkly as feedback |
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
│   │       ├── evaluation/            # judge.py, agent_evaluator.py (G-Eval)
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
│   └── set_ai_config_targeting.py      # post-apply: point fallthrough at Default
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
