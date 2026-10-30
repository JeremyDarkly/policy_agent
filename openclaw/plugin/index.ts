// OpenClaw plugin: each agent run gets its model, instructions and tools from a LaunchDarkly AgentControl config,
// and its tokens, duration, tool calls and outcome are recorded on that config's tracker.
//
// The config is evaluated once per run, in whichever hook fires first, and every later hook in the run reuses it.
// Turning the config off in LaunchDarkly blocks new runs. Credentials come from the environment only.
//
// Skills pinned on the served variation are listed in the system prompt by name and description. The agent loads
// one with the plugin's load_skill tool, which returns the exact pinned version from the SDK's skill store. Nothing
// is written to disk.
//
// When the openclaw-agentcontrol-tracing flag is on for a run, the run is also exported as a trace linked to the
// config (see tracing.ts).
import { appendFileSync } from "node:fs";

import {
  FDv2SkillStore,
  getSkillResult,
  setSkillStore,
  skillRefs,
} from "@launchdarkly/ai-server/experimental";
import { init, type LDClient, type LDContext, type LDEvaluationDetail } from "@launchdarkly/node-server-sdk";
import { initAi, type LDAIAgentConfig, type LDAIClient, type LDAIConfigTracker } from "@launchdarkly/server-sdk-ai";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { Type } from "typebox";

import { MISMATCH_EVENT, SERVED_EVENT, modelsMatch, servedCalls } from "./served_model.ts";
import { runTracer, type RunTrace } from "./tracing.ts";

type Settings = {
  configKey: string;
  toolPrefix: string;
  failClosed: boolean;
  hideTools: boolean;
  initTimeoutSeconds: number;
  decisionLog?: string;
};

const LOAD_SKILL = "load_skill";
// OpenClaw's Tool Search controls. They only find and describe tools, and a call made through tool_call reaches
// before_tool_call again under the real tool's name, so the tool list is still enforced there.
const TOOL_SEARCH_CONTROLS = new Set(["tool_search", "tool_describe", "tool_call"]);
// Boolean flag, evaluated per run with the same context as the config: whether to export the run's trace.
const TRACING_FLAG = "openclaw-agentcontrol-tracing";
// Metric event: trace export batches that failed, sent with the next run that finishes.
const EXPORT_FAILED_EVENT = "openclaw-trace-export-failed";

// A skill pinned on the served variation. Its content stays in the SDK's skill store until load_skill asks for it.
type PinnedSkill = { key: string; version: number; name: string; description: string };

// What a run was served. `unavailable` means LaunchDarkly couldn't be reached, so the run keeps OpenClaw's own
// settings unless failClosed is set.
type RunState =
  | { kind: "unavailable" }
  | { kind: "disabled"; context: LDContext; trace?: RunTrace }
  | {
      kind: "served";
      trace?: RunTrace;
      config: LDAIAgentConfig;
      context: LDContext;
      tracker: LDAIConfigTracker;
      variationKey: string;
      allowedTools: Set<string>;
      skills: PinnedSkill[];
      toolCalls: string[];
      skillsLoaded: string[];
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

// What a tool call acts on, for the decision log: a path, a command or a name, cut short.
function target(params: Record<string, unknown>): string | undefined {
  for (const key of ["path", "file_path", "filePath", "command", "cmd", "name", "id"]) {
    const value = params[key];
    if (typeof value === "string" && value) return value.length > 160 ? `${value.slice(0, 157)}...` : value;
  }
  return undefined;
}

// The skills section of the system prompt: each pinned skill's name and description, not its content.
function skillIndex(skills: PinnedSkill[]): string | undefined {
  if (skills.length === 0) return undefined;
  const lines = skills.map((skill) => `- ${skill.name}: ${skill.description}`);
  return [
    "Skills from LaunchDarkly AgentControl. Before a task a skill covers, call the load_skill tool with the " +
      "skill's name and follow what it returns. Don't load a skill the task doesn't need.",
    ...lines,
  ].join("\n");
}

export default definePluginEntry({
  id: "ld-agentcontrol",
  name: "LaunchDarkly AgentControl",
  description: "Takes each run's model, instructions and tools from a LaunchDarkly AgentControl config",
  register(api) {
    const settings = readSettings(api.pluginConfig);
    const runs = new Map<string, Promise<RunState>>();
    let client: Promise<{ ld: LDClient; ai: LDAIClient; skills?: FDv2SkillStore } | undefined> | undefined;
    // Set by the SDK on each evaluation. Linking a trace works without it, but LaunchDarkly's hooks send it.
    let environmentId: string | undefined;

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
          hooks: [{
            getMetadata: () => ({ name: "ld-agentcontrol environment ID" }),
            afterEvaluation(hookContext, data) {
              if (hookContext.environmentId) environmentId = hookContext.environmentId;
              return data;
            },
          }],
        });
        try {
          await ld.waitForInitialization({ timeout: settings.initTimeoutSeconds });
        } catch (error) {
          api.logger.warn(`ld-agentcontrol: LaunchDarkly did not initialize: ${String(error)}`);
          return undefined;
        }
        // Skill content comes from a separate store in the newer AI SDK's experimental module. A store that doesn't
        // answer leaves runs without skills; the config itself still applies.
        let skills: FDv2SkillStore | undefined = new FDv2SkillStore(sdkKey, {
          mode: baseUri === undefined ? "stream" : "poll",
          baseUri,
          streamUri: process.env.LAUNCHDARKLY_STREAM_URI || undefined,
        }).start();
        if (await skills.waitForSkills({ timeoutSeconds: settings.initTimeoutSeconds })) {
          setSkillStore(skills);
        } else {
          api.logger.warn(`ld-agentcontrol: skills unavailable: ${skills.failed ?? "timed out"}`);
          await skills.close();
          skills = undefined;
        }
        return { ld, ai: initAi(ld), skills };
      })();
      return client;
    };

    // The AI SDK's config object leaves out a variation's skill pins, so they're read from the raw variation.
    const pinnedSkills = async (
      sdk: { skills?: FDv2SkillStore },
      raw: LDEvaluationDetail["value"] | undefined,
      ctx: HookContext,
    ): Promise<PinnedSkill[]> => {
      if (!sdk.skills) return [];
      let refs: { key: string; version: number }[] = [];
      try {
        refs = raw && typeof raw === "object" ? skillRefs(raw) : [];
      } catch (error) {
        api.logger.warn(`ld-agentcontrol: ignoring malformed skill pins: ${String(error)}`);
      }
      const pinned: PinnedSkill[] = [];
      for (const ref of refs) {
        const outcome = await getSkillResult(ref.key, { version: ref.version });
        if (outcome.skill) {
          const { key, version, name, description } = outcome.skill;
          pinned.push({ key, version, name: name ?? key, description: description ?? "" });
        } else {
          log({ event: "skill_unavailable", runId: ctx.runId, skill: `${ref.key}@${ref.version}`, reason: outcome.reason });
        }
      }
      return pinned;
    };

    const tracingOn = async (ld: LDClient, context: LDContext): Promise<boolean> =>
      runTracer() !== null && (await ld.variation(TRACING_FLAG, context, false)) === true;

    // Starts the run's trace. The root span starts at the run's first hook, which isn't before_agent_run on the
    // embedded runner. `detail` is the config's raw evaluation, for the variation index LaunchDarkly's hooks put on
    // the feature_flag event.
    const startTrace = (
      context: LDContext,
      ctx: HookContext,
      startTime: number,
      detail: LDEvaluationDetail,
    ): RunTrace | undefined => {
      const tracer = runTracer();
      if (!tracer) return undefined;
      const id = ctx.runId ?? ctx.sessionKey ?? "unknown";
      const meta = (detail.value as { _ldMeta?: { variationKey?: string } } | null)?._ldMeta;
      const run = tracer.start({ runId: id, agentId: ctx.agentId ?? "main", conversationId: ctx.sessionKey ?? id, startTime });
      run.linkConfig({
        configKey: settings.configKey,
        variationKey: meta?.variationKey ?? "",
        variationIndex: detail.variationIndex ?? undefined,
        // buildContext makes a single user context, whose canonical key is its key.
        contextId: String((context as { key: string }).key),
        environmentId,
      });
      return run;
    };

    const evaluate = async (ctx: HookContext, startTime: number): Promise<RunState> => {
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
        const trace = (await tracingOn(sdk.ld, context)) ? startTrace(context, ctx, startTime, detail) : undefined;
        return { kind: "disabled", context, trace };
      }
      const tracker = config.createTracker();
      const { variationKey } = tracker.getTrackData();
      const tools = allowedTools(config, settings.toolPrefix);
      // One raw evaluation serves both the skill pins and the trace link, so neither costs an extra one.
      const traced = await tracingOn(sdk.ld, context);
      const detail = sdk.skills || traced ? await sdk.ld.variationDetail(settings.configKey, context, null) : undefined;
      const skills = await pinnedSkills(sdk, detail?.value, ctx);
      log({
        event: "served",
        runId: ctx.runId,
        agentId: ctx.agentId,
        state: "served",
        variationKey,
        model: modelOverride(config),
        tools: [...tools],
        skills: skills.map((skill) => `${skill.key}@${skill.version}`),
      });
      return {
        kind: "served",
        trace: traced && detail ? startTrace(context, ctx, startTime, detail) : undefined,
        config,
        context,
        tracker,
        variationKey,
        allowedTools: tools,
        skills,
        toolCalls: [],
        skillsLoaded: [],
        tokens: { input: 0, output: 0, total: 0 },
      };
    };

    // One evaluation per run. Hooks without a runId fall back to the session key.
    const runState = (ctx: HookContext): Promise<RunState> => {
      const id = ctx.runId ?? ctx.sessionKey ?? "unknown";
      let state = runs.get(id);
      if (!state) {
        if (runs.size >= MAX_TRACKED_RUNS) runs.delete(runs.keys().next().value!);
        state = evaluate(ctx, Date.now()).catch((error) => {
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
        // A blocked run may end without agent_end, so its trace is finished and exported here.
        if (state.kind === "disabled" && state.trace) {
          state.trace.blocked(reason);
          await finishTrace(ctx.runId ?? ctx.sessionKey ?? "unknown", state.context, { success: false });
        }
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
      const sections = [state.config.instructions, skillIndex(state.skills)].filter(Boolean);
      const allowed = [...state.allowedTools, ...(state.skills.length > 0 ? [LOAD_SKILL] : [])];
      return {
        appendSystemContext: sections.join("\n\n") || undefined,
        toolsAllow: settings.hideTools ? allowed : undefined,
      };
    });

    // The enforcing check. It also covers runners that ignore toolsAllow. Each decision is also recorded on the trace.
    api.on("before_tool_call", async (event, ctx) => {
      const state = await runState(ctx);
      const result = toolDecision(event, state, ctx);
      const trace = state.kind === "unavailable" ? undefined : state.trace;
      const pinned = event.toolName === LOAD_SKILL && result && "params" in result ? result.params : undefined;
      const skill = pinned ? `${pinned.name}@${pinned.version}` : undefined;
      const tool = { toolName: event.toolName, toolCallId: event.toolCallId, skill };
      if (result && "block" in result) trace?.toolBlocked(tool);
      else trace?.toolStarted(tool);
      return result;
    });

    const toolDecision = (event: { toolName: string; params: Record<string, unknown> }, state: RunState, ctx: HookContext) => {
      if (event.toolName === LOAD_SKILL) return gateSkill(event.params, state, ctx);
      if (TOOL_SEARCH_CONTROLS.has(event.toolName)) return undefined;
      if (state.kind !== "served") {
        return state.kind === "disabled" || settings.failClosed
          ? { block: true, blockReason: "Tools are turned off for this agent in LaunchDarkly AgentControl." }
          : undefined;
      }
      if (!state.allowedTools.has(event.toolName)) {
        log({ event: "blocked_tool", runId: ctx.runId, agentId: ctx.agentId, tool: event.toolName, target: target(event.params) });
        return {
          block: true,
          blockReason: `The ${event.toolName} tool isn't allowed for this agent (variation ${state.variationKey}).`,
        };
      }
      state.toolCalls.push(`${settings.toolPrefix}${event.toolName}`);
      log({ event: "allowed_tool", runId: ctx.runId, agentId: ctx.agentId, tool: event.toolName, target: target(event.params) });
      return undefined;
    };

    api.on("after_tool_call", async (event, ctx) => {
      const state = await runState({ ...ctx, runId: event.runId ?? ctx.runId });
      if (state.kind === "unavailable") return;
      state.trace?.toolEnded({ toolName: event.toolName, toolCallId: event.toolCallId, error: event.error });
    });

    // load_skill is allowed for any skill the run's variation pins, whatever its tool list says. The hook replaces
    // the call's arguments with the pinned key and version, so the tool can't be asked for anything else.
    const gateSkill = (params: Record<string, unknown>, state: RunState, ctx: HookContext) => {
      const requested = typeof params.name === "string" ? params.name.trim() : "";
      const skill = state.kind === "served"
        ? state.skills.find((pinned) => pinned.key === requested || pinned.name === requested)
        : undefined;
      if (!skill || state.kind !== "served") {
        log({ event: "blocked_skill", runId: ctx.runId, agentId: ctx.agentId, skill: requested });
        return { block: true, blockReason: `No skill named "${requested}" is available to this agent.` };
      }
      state.skillsLoaded.push(`${skill.key}@${skill.version}`);
      log({ event: "skill_loaded", runId: ctx.runId, agentId: ctx.agentId, skill: `${skill.key}@${skill.version}` });
      return { params: { name: skill.key, version: skill.version } };
    };

    api.registerTool({
      name: LOAD_SKILL,
      label: "Load skill",
      // Tool Search would otherwise keep a plugin tool behind tool_search and tool_call.
      catalogMode: "direct-only",
      description:
        "Returns the full instructions of one of the skills listed in your system prompt. Call it with the skill's " +
        "name before a task the skill covers, then follow the instructions it returns.",
      parameters: Type.Object({
        name: Type.String({ description: "The skill's name, as listed in the system prompt" }),
        version: Type.Optional(Type.Integer({ description: "Set by the system. Leave it out." })),
      }),
      async execute(_id, params) {
        // Only before_tool_call sets a version. A call without one didn't pass the run's skill check.
        const { name, version } = params as { name: string; version?: number };
        const outcome = version === undefined ? undefined : await getSkillResult(name, { version });
        const text = outcome?.skill
          ? new TextDecoder().decode(outcome.skill.content)
          : `Skill "${name}" isn't available${outcome ? ` (${outcome.reason})` : ""}.`;
        return { content: [{ type: "text", text }], details: { name, version, available: Boolean(outcome?.skill) } };
      },
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

    api.on("model_call_started", async (event, ctx) => {
      const state = await runState({ ...ctx, runId: event.runId ?? ctx.runId });
      if (state.kind === "served") state.trace?.modelCallStarted({ provider: event.provider, model: event.model });
    });

    api.on("model_call_ended", async (event, ctx) => {
      const state = await runState({ ...ctx, runId: event.runId ?? ctx.runId });
      if (state.kind !== "served") return;
      if (state.firstTokenMs === undefined && event.timeToFirstByteMs !== undefined) {
        state.firstTokenMs = event.timeToFirstByteMs;
      }
      state.trace?.modelCallEnded({
        outcome: event.outcome,
        errorCategory: event.errorCategory,
        timeToFirstByteMs: event.timeToFirstByteMs,
      });
    });

    // Records the run on the tracker and flushes, so one-shot command-line runs don't exit with events unsent.
    api.on("agent_end", async (event, ctx) => {
      const id = event.runId ?? ctx.runId ?? ctx.sessionKey ?? "unknown";
      const pending = runs.get(id);
      if (!pending) return;
      runs.delete(id);
      const state = await pending;
      // Normally a no-op: before_agent_run already finished a blocked run's trace.
      if (state.kind === "disabled") {
        if (state.trace) await finishTrace(id, state.context, { success: false });
        return;
      }
      if (state.kind !== "served") return;
      const { tracker } = state;
      if (event.durationMs !== undefined) tracker.trackDuration(event.durationMs);
      if (state.firstTokenMs !== undefined) tracker.trackTimeToFirstToken(state.firstTokenMs);
      if (state.tokens.total > 0) tracker.trackTokens(state.tokens);
      if (state.toolCalls.length > 0) tracker.trackToolCalls(state.toolCalls);
      if (event.success) tracker.trackSuccess();
      else tracker.trackError();
      const sdk = await client;
      const served = reportServedModels(sdk?.ld, state, event.messages);
      if (state.trace) {
        await finishTrace(id, state.context, {
          success: event.success,
          usage: state.tokens,
          servedModels: served.map((call) => call.served),
        });
      }
      log({
        event: "finished",
        runId: id,
        agentId: ctx.agentId,
        variationKey: state.variationKey,
        success: event.success,
        durationMs: event.durationMs,
        tokens: state.tokens,
        toolCalls: state.toolCalls,
        skillsLoaded: state.skillsLoaded,
        servedModels: served,
      });
      await sdk?.ld.flush();
    });

    // Ends the run's spans and exports them now, since a one-shot command-line run exits right after agent_end.
    const finishTrace = async (runId: string, context: LDContext, result: Parameters<RunTrace["finish"]>[0]) => {
      const tracer = runTracer();
      const traceId = tracer?.finish(runId, result);
      if (!tracer || !traceId) return;
      await tracer.flush();
      log({ event: "trace_exported", runId, traceId });
      const failures = tracer.takeExportFailures();
      if (failures > 0) {
        const sdk = await client;
        sdk?.ld.track(EXPORT_FAILED_EVENT, context, { failures }, failures);
        await sdk?.ld.flush();
      }
    };

    // Sends ai-model-served and ai-model-mismatch for each model call in this run, like the backend does per call.
    // The mismatch value is 1 when the model isn't the variation's model (or that model made more specific) and 0
    // when it is, so its average is the mismatch rate per call.
    const reportServedModels = (ld: LDClient | undefined, state: Extract<RunState, { kind: "served" }>, messages: unknown[]) => {
      const { providerOverride, modelOverride: model } = modelOverride(state.config);
      const requested = model ? (providerOverride ? `${providerOverride}/${model}` : model) : undefined;
      const calls = servedCalls(messages ?? [], requested).map((call) => ({ ...call, mismatch: !modelsMatch(call.requested, call.served) }));
      for (const { mismatch, ...call } of calls) {
        const data = {
          configKey: settings.configKey,
          variationKey: state.variationKey,
          provider: call.provider,
          requestedModel: call.requested,
          servedModel: call.served,
          source: call.source,
        };
        ld?.track(SERVED_EVENT, state.context, data, 1);
        ld?.track(MISMATCH_EVENT, state.context, data, mismatch ? 1 : 0);
      }
      return calls;
    };

    api.on("gateway_stop", async () => {
      await runTracer()?.shutdown();
      const sdk = await client;
      sdk?.ld.close();
      await sdk?.skills?.close();
    });
  },
});
