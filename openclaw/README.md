# AgentControl runtime control for OpenClaw agents

An [OpenClaw](https://docs.openclaw.ai) plugin that takes each agent run's model, instructions and tools from a
LaunchDarkly AgentControl config, and records the run's tokens, duration and outcome on that config. Changes made in
LaunchDarkly apply to the next run, with no redeploy and no OpenClaw restart. [PLAN.md](PLAN.md) explains the design
and what was tested.

| File | What it is |
|---|---|
| `plugin/` | The OpenClaw plugin (`index.ts`) and its manifest |
| `setup_config.py` | Creates the `openclaw_agent` config, its tools, variations and targeting. Also the kill switch and instruction edits the checks use |
| `run_checks.sh` | Five checks against real OpenClaw agents and the live config |
| `env.sh` | Exports the variables the checks need from the repo's env file, and points OpenClaw at `.state/` |

## Before you run it

- **Node** 24.16 or later (or 26.1 or later), for OpenClaw.
- **The env file**: `.env.staging-org` at the repo root by default, set with `ENV_FILE_NAME`. It needs
  `LAUNCHDARKLY_SDK_KEY`, `LAUNCHDARKLY_ACCESS_TOKEN`, `LAUNCHDARKLY_PROJECT_KEY`, `LAUNCHDARKLY_ENVIRONMENT` and
  `OPENAI_API_KEY`.
  - For a non-production LaunchDarkly instance, also set `LAUNCHDARKLY_API_HOST`, `LAUNCHDARKLY_BASE_URI`,
    `LAUNCHDARKLY_STREAM_URI` and `LAUNCHDARKLY_EVENTS_URI`.
  - When `LAUNCHDARKLY_BASE_URI` is set, the plugin polls instead of streaming, as the rest of this repo does.
- **The project's model configs** must include `OpenAI.gpt-5.4-mini` and `OpenAI.gpt-5.4-nano`. OpenClaw refuses
  model ids that aren't in its catalog, such as `gpt-4o-mini`.

## Run it

```sh
cd openclaw
npm install
(cd plugin && npm install)
python3 setup_config.py setup   # safe to rerun
./run_checks.sh                 # or one check: baseline, targeting, tools, live, killswitch
```

The first run creates `.state/`, writes an OpenClaw config with two agents (`main` and `helper`), and links the
plugin. OpenClaw never reads or writes `~/.openclaw` here. Every check makes real model calls; a full run is about 10
calls on the two small models.

To talk to an agent yourself:

```sh
source env.sh
npx openclaw agent --local --agent helper -m "What can you do?"
cat .state/decisions.jsonl     # what each run was served, offered and blocked
```

## What it changes in LaunchDarkly

- Two Library tools: `openclaw_read` and `openclaw_exec`. The plugin strips the `openclaw_` prefix to get OpenClaw's
  tool ids.
- An agent-mode config, `openclaw_agent`, with two variations:
  - `standard`: `gpt-5.4-mini` with both tools.
  - `locked-down`: `gpt-5.4-nano` with no tools.
- Targeting in your environment: `locked-down` for contexts whose `agentId` is `helper`, and `standard` for everyone
  else.

`python3 setup_config.py off` is the kill switch. It serves the config's built-in `disabled` variation to everyone,
because the API can't flip an AI Config's targeting toggle. `on` restores the targeting. The UI toggle works too.

## How the plugin maps AgentControl onto OpenClaw

| AgentControl | OpenClaw hook | Effect |
|---|---|---|
| Config off, or the `disabled` variation | `before_agent_run` | The run is blocked with a message |
| Model and provider | `before_model_resolve` | That run's model. `model.custom.openclawModel` (`provider/model`) overrides the mapping |
| Instructions | `before_prompt_build` | Appended to OpenClaw's system prompt |
| Tools | `before_prompt_build`, `before_tool_call` | Other tools are hidden from the model, and any call to one is blocked |
| Tracker | `llm_output`, `model_call_ended`, `agent_end` | Tokens, time to first token, duration, tool calls and success |

The LaunchDarkly context is a `user` keyed by the sender, or `openclaw-local-operator` for command-line runs. It has
the attributes `agentId`, `channel` and `trigger`.

When LaunchDarkly can't be reached, or the config doesn't exist, runs keep OpenClaw's own settings. Set
`failClosed: true` in the plugin's config to block them instead.

Plugin settings go under `plugins.entries.ld-agentcontrol.config` in the OpenClaw config. They are `configKey`,
`toolPrefix`, `failClosed`, `hideTools`, `initTimeoutSeconds` and `decisionLog`; the manifest describes each one.
