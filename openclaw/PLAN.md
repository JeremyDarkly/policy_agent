# Plan: AgentControl runtime control for OpenClaw agents

The goal is to find out whether LaunchDarkly AgentControl can control an [OpenClaw](https://docs.openclaw.ai) agent at
runtime: its model, instructions and tools, with a kill switch and metrics, all changed in LaunchDarkly without
redeploying or restarting OpenClaw.

## How it fits together

OpenClaw plugins are TypeScript modules that run inside the OpenClaw process and register hooks on each agent turn.
A plugin can call the LaunchDarkly Node AI SDK (`@launchdarkly/server-sdk-ai`) from those hooks and apply what the
served variation says.

| AgentControl | OpenClaw hook | What the plugin does |
|---|---|---|
| `enabled` (kill switch) | `before_agent_run` | Returns `{ outcome: "block" }` when the config is off |
| Model and provider | `before_model_resolve` | Returns `providerOverride` and `modelOverride` for this run |
| Instructions | `before_prompt_build` | Returns `appendSystemContext`, so OpenClaw keeps its own tool and safety prompt |
| Tools | `before_prompt_build`, `before_tool_call` | `toolsAllow` hides tools from the model. `before_tool_call` blocks any call that isn't allowed, for runners that ignore `toolsAllow` |
| Tracker | `llm_output`, `model_call_ended`, `agent_end` | Records tokens, duration, tool calls and success or error |

The LaunchDarkly context is built from the hook context: `agentId`, `channel`, `senderId` and `sessionKey`. The
config is evaluated once per run and reused by every hook in that run.

## The small test

**Scope:** one OpenClaw install, run locally and headless with `openclaw agent exec`. It uses one AgentControl config,
`openclaw_agent`, in agent mode. Chat channels, judges and guarded rollouts are out of scope for now.

### Config

| Variation | Model | Instructions | Tools |
|---|---|---|---|
| `standard` | `gpt-5.4-mini` | Start each reply with `[standard]` | `read`, `exec` |
| `locked-down` | `gpt-5.4-nano` | Start each reply with `[locked-down]` | none |

Targeting serves `locked-down` to contexts whose `agentId` is `helper`, and `standard` to everyone else. The command
line has no sender, so two OpenClaw agents (`main` and `helper`) are enough to show targeting. OpenClaw's own default
model is `gpt-5.4`, so a run that wasn't overridden is easy to spot.

### Files

| File | What it is |
|---|---|
| `plugin/` | The OpenClaw plugin: `package.json`, `openclaw.plugin.json`, `index.ts` |
| `setup_config.py` | Creates the config, its variations and its targeting through the REST API. It is idempotent |
| `run_checks.sh` | Runs the checks below against a throwaway OpenClaw state directory |
| `env.sh` | Exports the variables the checks need and points OpenClaw at that directory |
| `README.md` | How to run it |

Credentials come from the repo's env file and stay in the environment. They are never written into OpenClaw's
config.

### Checks

1. **Baseline:** the `main` agent's run uses `gpt-5.4-mini`, and its answer follows the `standard` instructions.
2. **Targeting:** the `helper` agent gets `gpt-5.4-nano` and the `locked-down` instructions.
3. **Tool gating:** both agents are asked to run `ls`. Only `main` may run it. `helper` isn't offered `exec`, and
   with hiding turned off, its call to `exec` is blocked by `before_tool_call`.
4. **Live change:** edit the `standard` instructions in LaunchDarkly and rerun. The new instructions apply without
   reinstalling the plugin.
5. **Kill switch:** serve the `disabled` variation. The run is blocked with a clear message.

Then confirm that tokens, duration and success appear on the config's Monitoring tab.

## Known limits

- Temperature, max tokens and similar parameters can't be set per run from a hook. They live in OpenClaw's config
  file, which reloads without a restart but applies per agent, not per user.
- Hook support differs between OpenClaw runners. The test uses the embedded runner.
- LaunchDarkly's newer `@launchdarkly/ai-node` SDK is in beta and makes the model call itself. OpenClaw makes its own
  model calls here, so the plugin uses `@launchdarkly/server-sdk-ai` for the config and tracker only.

## Results

All five checks pass with OpenClaw 2026.10.3 and `@launchdarkly/server-sdk-ai` 2.0.13. The config's metrics show
every run, split by variation, with tokens, duration, time to first token and success.

What the build settled:

- **Model names.** LaunchDarkly's provider name in lower case plus its model name gives OpenClaw's reference, such as
  `openai/gpt-5.4-mini`. OpenClaw only accepts models in its own catalog, so variations need current model ids.
- **Tools.** The SDK reads a variation's Library tools from the root `tools` map or from `model.parameters.tools`,
  so the plugin doesn't need to care which.
- **Metrics.** `agent_end` fires before a one-shot `openclaw agent --local` run exits, and flushing there gets the
  events out.
- **Separate state.** `OPENCLAW_STATE_DIR` and `OPENCLAW_CONFIG_PATH` keep the test away from an existing install.
  `agent exec` has no `--agent` flag, so the checks use `openclaw agent --local --agent <id>`.

The [README](README.md#gaps-and-unexpected-behavior) lists the gaps and surprises, with versions and dates.

## Next steps

- A chat channel such as Slack, so targeting can use the real sender instead of the agent id.
- Judges attached to the variations, run from `agent_end`, so guarded rollouts can watch answer quality.
- Running under the Gateway, where the plugin's client stays connected and picks up changes without a new process.

## Related work

Galileo's open-source Agent Control has an [OpenClaw plugin](https://github.com/agentcontrol/openclaw-plugin) that
allows or denies tool calls from `before_tool_call`. This plugin uses the same tool hook and also controls the model,
instructions, kill switch and metrics.
