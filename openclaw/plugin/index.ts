// OpenClaw plugin: each agent run gets its model, instructions and tools from a LaunchDarkly AgentControl config,
// and its tokens, duration, tool calls and outcome are recorded on that config's tracker.
//
// The config is evaluated once per run, in whichever hook fires first, and every later hook in the run reuses it.
// Turning the config off in LaunchDarkly blocks new runs. Credentials come from the environment only.
import { appendFileSync } from "node:fs";

import { init, type LDClient, type LDContext } from "@launchdarkly/node-server-sdk";
import { initAi, type LDAIAgentConfig, type LDAIClient, type LDAIConfigTracker } from "@launchdarkly/server-sdk-ai";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

type Settings = {
  configKey: string;
  toolPrefix: string;
  failClosed: boolean;
  hideTools: boolean;
  initTimeoutSeconds: number;
  decisionLog?: string;
};

// What a run was served. `unavailable` means LaunchDarkly couldn't be reached, so the run keeps OpenClaw's own
// settings unless failClosed is set.
type RunState =
  | { kind: "unavailable" }
  | { kind: "disabled" }
  | {
      kind: "served";
      config: LDAIAgentConfig;
      tracker: LDAIConfigTracker;
      variationKey: string;
      allowedTools: Set<string>;
      toolCalls: string[];
      tokens: { input: number; output: number; total: number };
      firstTokenMs?: number;
    };

// Fields of OpenClaw's hook context that this plugin reads. All are optional on every hook.
type HookContext = {
  runId?: string;
  sessionKey?: string;
  agentId?: string;
  channel?: string;
  messageProvider?: string;
  senderId?: string;
  trigger?: string;
};

const MAX_TRACKED_RUNS = 500;

function readSettings(raw: Record<string, unknown> | undefined): Settings {
  return {
    configKey: typeof raw?.configKey === "string" ? raw.configKey : "openclaw_agent",
    toolPrefix: typeof raw?.toolPrefix === "string" ? raw.toolPrefix : "openclaw_",
    failClosed: raw?.failClosed === true,
    hideTools: raw?.hideTools !== false,
    initTimeoutSeconds: typeof raw?.initTimeoutSeconds === "number" ? raw.initTimeoutSeconds : 10,
    decisionLog: typeof raw?.decisionLog === "string" ? raw.decisionLog : undefined,
  };
}

// The command line has no sender, so its runs share one operator key. Targeting can still use agentId and channel.
function buildContext(ctx: HookContext): LDContext {
  return {
    kind: "user",
    key: ctx.senderId ?? "openclaw-local-operator",
    agentId: ctx.agentId ?? "main",
    channel: ctx.channel ?? ctx.messageProvider ?? "cli",
    trigger: ctx.trigger ?? "user",
  };
}

// A variation's model becomes an OpenClaw `provider/model` reference. Set `openclawModel` in the model's custom
// parameters to give the full reference when LaunchDarkly's provider name doesn't match OpenClaw's provider id.
function modelOverride(config: LDAIAgentConfig): { providerOverride?: string; modelOverride?: string } {
  const explicit = config.model?.custom?.openclawModel;
  if (typeof explicit === "string" && explicit.includes("/")) {
    const [provider, ...rest] = explicit.split("/");
    return { providerOverride: provider, modelOverride: rest.join("/") };
  }
  if (!config.model?.name) return {};
  return {
    providerOverride: config.provider?.name?.toLowerCase(),
    modelOverride: config.model.name,
  };
}

// Library tools named `<prefix><openclaw tool id>` are the tools a variation allows. No tools means none allowed.
function allowedTools(config: LDAIAgentConfig, prefix: string): Set<string> {
  return new Set(
    Object.keys(config.tools ?? {})
      .filter((key) => key.startsWith(prefix))
      .map((key) => key.slice(prefix.length)),
  );
}

export default definePluginEntry({
  id: "ld-agentcontrol",
  name: "LaunchDarkly AgentControl",
  description: "Takes each run's model, instructions and tools from a LaunchDarkly AgentControl config",
  register(api) {
    const settings = readSettings(api.pluginConfig);
    const runs = new Map<string, Promise<RunState>>();
    let client: Promise<{ ld: LDClient; ai: LDAIClient } | undefined> | undefined;

    const log = (record: Record<string, unknown>) => {
      if (!settings.decisionLog) return;
      try {
        appendFileSync(settings.decisionLog, `${JSON.stringify({ at: new Date().toISOString(), ...record })}\n`);
      } catch (error) {
        api.logger.warn(`ld-agentcontrol: can't write decision log: ${String(error)}`);
      }
    };

    // Started on first use rather than in register(), which must stay synchronous.
    const connect = () => {
      client ??= (async () => {
        const sdkKey = process.env.LAUNCHDARKLY_SDK_KEY;
        if (!sdkKey) {
          api.logger.warn("ld-agentcontrol: LAUNCHDARKLY_SDK_KEY is not set");
          return undefined;
        }
        // As elsewhere in this repo, setting LAUNCHDARKLY_BASE_URI (the SDK polling host) switches to polling every
        // 30 seconds. Use it when an instance's stream doesn't pick up edits.
        const baseUri = process.env.LAUNCHDARKLY_BASE_URI || undefined;
        const ld = init(sdkKey, {
          baseUri,
          streamUri: process.env.LAUNCHDARKLY_STREAM_URI || undefined,
          eventsUri: process.env.LAUNCHDARKLY_EVENTS_URI || undefined,
          stream: baseUri === undefined,
        });
        try {
          await ld.waitForInitialization({ timeout: settings.initTimeoutSeconds });
        } catch (error) {
          api.logger.warn(`ld-agentcontrol: LaunchDarkly did not initialize: ${String(error)}`);
          return undefined;
        }
        return { ld, ai: initAi(ld) };
      })();
      return client;
    };

    const evaluate = async (ctx: HookContext): Promise<RunState> => {
      const sdk = await connect();
      if (!sdk) {
        log({ event: "served", runId: ctx.runId, agentId: ctx.agentId, state: "unavailable" });
        return { kind: "unavailable" };
      }
      const context = buildContext(ctx);
      const config = await sdk.ai.agentConfig(settings.configKey, context, { enabled: false });
      if (!config.enabled) {
        // A config that doesn't exist also comes back disabled. Treat it like an unreachable LaunchDarkly, so a
        // wrong key or environment doesn't silently turn every agent off.
        const detail = await sdk.ld.variationDetail(settings.configKey, context, null);
        if (detail.reason.kind === "ERROR") {
          api.logger.warn(`ld-agentcontrol: can't evaluate ${settings.configKey}: ${detail.reason.errorKind}`);
          log({ event: "served", runId: ctx.runId, agentId: ctx.agentId, state: "unavailable", error: detail.reason.errorKind });
          return { kind: "unavailable" };
        }
        log({ event: "served", runId: ctx.runId, agentId: ctx.agentId, state: "disabled" });
        return { kind: "disabled" };
      }
      const tracker = config.createTracker();
      const { variationKey } = tracker.getTrackData();
      const tools = allowedTools(config, settings.toolPrefix);
      log({
        event: "served",
        runId: ctx.runId,
        agentId: ctx.agentId,
        state: "served",
        variationKey,
        model: modelOverride(config),
        tools: [...tools],
      });
      return {
        kind: "served",
        config,
        tracker,
        variationKey,
        allowedTools: tools,
        toolCalls: [],
        tokens: { input: 0, output: 0, total: 0 },
      };
    };

    // One evaluation per run. Hooks without a runId fall back to the session key.
    const runState = (ctx: HookContext): Promise<RunState> => {
      const id = ctx.runId ?? ctx.sessionKey ?? "unknown";
      let state = runs.get(id);
      if (!state) {
        if (runs.size >= MAX_TRACKED_RUNS) runs.delete(runs.keys().next().value!);
        state = evaluate(ctx).catch((error) => {
          api.logger.warn(`ld-agentcontrol: evaluation failed: ${String(error)}`);
          return { kind: "unavailable" } as const;
        });
        runs.set(id, state);
      }
      return state;
    };

    // Runs on the embedded and CLI runners, before the model sees anything. This is the kill switch.
    api.on("before_agent_run", async (_event, ctx) => {
      const state = await runState(ctx);
      if (state.kind === "disabled" || (state.kind === "unavailable" && settings.failClosed)) {
        const reason = state.kind === "disabled" ? "config_disabled" : "launchdarkly_unavailable";
        log({ event: "blocked_run", runId: ctx.runId, agentId: ctx.agentId, reason });
        return {
          outcome: "block",
          reason,
          message: "This agent is turned off in LaunchDarkly AgentControl.",
          category: "agentcontrol",
        };
      }
      return { outcome: "pass" };
    });

    api.on("before_model_resolve", async (_event, ctx) => {
      const state = await runState(ctx);
      return state.kind === "served" ? modelOverride(state.config) : undefined;
    });

    // Instructions are appended so OpenClaw keeps its own tool and safety sections. toolsAllow hides the
    // variation's missing tools from the model on runners that support it. hideTools: false skips that, which
    // shows that before_tool_call enforces the tool list on its own.
    api.on("before_prompt_build", async (_event, ctx) => {
      const state = await runState(ctx);
      if (state.kind !== "served") return undefined;
      return {
        appendSystemContext: state.config.instructions || undefined,
        toolsAllow: settings.hideTools ? [...state.allowedTools] : undefined,
      };
    });

    // The enforcing check. It also covers runners that ignore toolsAllow.
    api.on("before_tool_call", async (event, ctx) => {
      const state = await runState(ctx);
      if (state.kind !== "served") {
        return state.kind === "disabled" || settings.failClosed
          ? { block: true, blockReason: "Tools are turned off for this agent in LaunchDarkly AgentControl." }
          : undefined;
      }
      if (!state.allowedTools.has(event.toolName)) {
        log({ event: "blocked_tool", runId: ctx.runId, agentId: ctx.agentId, tool: event.toolName });
        return {
          block: true,
          blockReason: `The ${event.toolName} tool isn't allowed for this agent (variation ${state.variationKey}).`,
        };
      }
      state.toolCalls.push(`${settings.toolPrefix}${event.toolName}`);
      return undefined;
    });

    // Records which tools the model was actually offered, so a check can tell hidden tools from blocked ones.
    api.on("llm_input", async (event, ctx) => {
      if (!settings.decisionLog) return;
      const offered = (event.tools ?? [])
        .map((tool) => (tool as { name?: unknown })?.name)
        .filter((name): name is string => typeof name === "string");
      log({ event: "model_input", runId: event.runId, agentId: ctx.agentId, model: event.model, offeredTools: offered });
    });

    api.on("llm_output", async (event, ctx) => {
      const state = await runState({ ...ctx, runId: event.runId ?? ctx.runId });
      if (state.kind !== "served" || !event.usage) return;
      // OpenClaw reports cached prompt tokens separately. LaunchDarkly counts them as input.
      const { input = 0, output = 0, cacheRead = 0, cacheWrite = 0 } = event.usage;
      state.tokens.input += input + cacheRead + cacheWrite;
      state.tokens.output += output;
      state.tokens.total += input + cacheRead + cacheWrite + output;
    });

    api.on("model_call_ended", async (event, ctx) => {
      const state = await runState({ ...ctx, runId: event.runId ?? ctx.runId });
      if (state.kind === "served" && state.firstTokenMs === undefined && event.timeToFirstByteMs !== undefined) {
        state.firstTokenMs = event.timeToFirstByteMs;
      }
    });

    // Records the run on the tracker and flushes, so one-shot command-line runs don't exit with events unsent.
    api.on("agent_end", async (event, ctx) => {
      const id = event.runId ?? ctx.runId ?? ctx.sessionKey ?? "unknown";
      const pending = runs.get(id);
      if (!pending) return;
      runs.delete(id);
      const state = await pending;
      if (state.kind !== "served") return;
      const { tracker } = state;
      if (event.durationMs !== undefined) tracker.trackDuration(event.durationMs);
      if (state.firstTokenMs !== undefined) tracker.trackTimeToFirstToken(state.firstTokenMs);
      if (state.tokens.total > 0) tracker.trackTokens(state.tokens);
      if (state.toolCalls.length > 0) tracker.trackToolCalls(state.toolCalls);
      if (event.success) tracker.trackSuccess();
      else tracker.trackError();
      log({
        event: "finished",
        runId: id,
        agentId: ctx.agentId,
        variationKey: state.variationKey,
        success: event.success,
        durationMs: event.durationMs,
        tokens: state.tokens,
        toolCalls: state.toolCalls,
      });
      const sdk = await client;
      await sdk?.ld.flush();
    });

    api.on("gateway_stop", async () => {
      const sdk = await client;
      sdk?.ld.close();
    });
  },
});
