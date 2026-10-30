// Run traces for LaunchDarkly observability, built from OpenClaw's plugin hooks.
//
// OpenClaw's own diagnostics-otel spans can't carry the AI Config link: none of them is active in a plugin hook, and
// its tracer provider is private. So this module exports its own trace per run, with its own provider:
//
//   invoke_agent <agentId>   root, from the run's first hook to agent_end, with the feature_flag event
//   ├─ chat <model>          one per model call
//   └─ execute_tool <name>   one per tool call, including blocked ones
//
// The feature_flag event on the root span is what puts the trace on the AI Config's Monitoring tab. OpenClaw reports
// token usage once per run (llm_output fires after the last model call), so the run's total goes on the last chat
// span, marked openclaw.usage.scope=run.
//
// The provider is never registered globally, so it can't clash with diagnostics-otel or another OpenTelemetry SDK.
// It reads OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_RESOURCE_ATTRIBUTES (env.sh sets both), OTEL_SERVICE_NAME overrides
// the default service name, and OTEL_SDK_DISABLED=true turns it off.
import { SpanKind, SpanStatusCode, context, trace, type Attributes, type Span } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { detectResources, envDetector, resourceFromAttributes } from "@opentelemetry/resources";
import { BasicTracerProvider, BatchSpanProcessor, type SpanExporter } from "@opentelemetry/sdk-trace-base";

export const SERVICE_NAME = "openclaw-agentcontrol";
// Runs that never reach agent_end are ended after this, so a long-running Gateway doesn't keep their spans.
const RUN_TIMEOUT_MS = 10 * 60 * 1000;

export type RunInfo = { runId: string; agentId: string; conversationId: string; startTime: number };

export type ConfigLink = {
  configKey: string;
  variationKey: string;
  variationIndex?: number;
  contextId: string;
  environmentId?: string;
};

type ModelCall = { span: Span; endTime?: number; failed?: boolean };

export class RunTrace {
  readonly root: Span;
  private readonly tracer: ReturnType<BasicTracerProvider["getTracer"]>;
  private readonly info: RunInfo;
  private readonly calls: ModelCall[] = [];
  // Keyed by toolCallId, or by `<toolName>#<n>` for calls without one, so same-named calls don't replace each other.
  private readonly tools = new Map<string, Span>();
  private toolSeq = 0;
  private readonly timeout: NodeJS.Timeout;
  private finished = false;

  // Plain fields rather than parameter properties, so Node's type stripping can run this file in tests.
  constructor(tracer: ReturnType<BasicTracerProvider["getTracer"]>, info: RunInfo, onTimeout: () => void) {
    this.tracer = tracer;
    this.info = info;
    this.root = tracer.startSpan(`invoke_agent ${info.agentId}`, {
      kind: SpanKind.INTERNAL,
      startTime: info.startTime,
      attributes: {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": info.agentId,
        "gen_ai.conversation.id": info.conversationId,
        "openclaw.run_id": info.runId,
        "launchdarkly.operation.type": "gen_ai",
      },
    });
    this.timeout = setTimeout(() => {
      this.root.setAttribute("openclaw.incomplete", true);
      this.finish({ success: false });
      onTimeout();
    }, RUN_TIMEOUT_MS);
    this.timeout.unref();
  }

  get traceId(): string {
    return this.root.spanContext().traceId;
  }

  private child(name: string, kind: SpanKind, attributes: Attributes, startTime?: number): Span {
    return this.tracer.startSpan(
      name,
      { kind, attributes: { "gen_ai.conversation.id": this.info.conversationId, ...attributes }, startTime },
      trace.setSpan(context.active(), this.root),
    );
  }

  // The same attributes LaunchDarkly's tracing hooks put on their feature_flag event.
  linkConfig(link: ConfigLink): void {
    this.root.setAttributes({
      "launchdarkly.config.key": link.configKey,
      "launchdarkly.variation.key": link.variationKey,
      "launchdarkly.run.id": this.info.runId,
    });
    this.root.addEvent("feature_flag", {
      "feature_flag.key": link.configKey,
      "feature_flag.provider.name": "LaunchDarkly",
      "feature_flag.context.id": link.contextId,
      ...(link.variationIndex !== undefined ? { "feature_flag.result.variationIndex": link.variationIndex } : {}),
      ...(link.environmentId ? { "feature_flag.set.id": link.environmentId } : {}),
    });
  }

  blocked(reason: string): void {
    this.root.setAttribute("openclaw.blocked_reason", reason);
  }

  modelCallStarted(call: { provider?: string; model?: string; startTime?: number }): void {
    this.calls.push({
      span: this.child(`chat ${call.model ?? "unknown"}`, SpanKind.CLIENT, {
        "gen_ai.operation.name": "chat",
        ...(call.provider ? { "gen_ai.provider.name": call.provider } : {}),
        ...(call.model ? { "gen_ai.request.model": call.model } : {}),
      }, call.startTime),
    });
  }

  // Chat spans stay open until finish(), which adds the served models and the run's usage, then ends each one at the
  // time its model call ended.
  modelCallEnded(call: { outcome?: string; errorCategory?: string; timeToFirstByteMs?: number; endTime?: number }): void {
    const open = this.calls.find((entry) => entry.endTime === undefined);
    if (!open) return;
    open.endTime = call.endTime ?? Date.now();
    if (call.timeToFirstByteMs !== undefined) open.span.setAttribute("openclaw.ttfb_ms", call.timeToFirstByteMs);
    if (call.outcome === "error") {
      open.failed = true;
      open.span.setAttribute("error.type", call.errorCategory ?? "error");
      open.span.setStatus({ code: SpanStatusCode.ERROR, message: call.errorCategory });
    }
  }

  toolStarted(tool: { toolName: string; toolCallId?: string; skill?: string; startTime?: number }): void {
    const span = this.child(`execute_tool ${tool.toolName}`, SpanKind.INTERNAL, {
      "gen_ai.operation.name": "execute_tool",
      "gen_ai.tool.name": tool.toolName,
      ...(tool.toolCallId ? { "gen_ai.tool.call.id": tool.toolCallId } : {}),
      ...(tool.skill ? { "openclaw.skill": tool.skill } : {}),
    }, tool.startTime);
    this.tools.set(tool.toolCallId ?? `${tool.toolName}#${this.toolSeq++}`, span);
  }

  toolBlocked(tool: { toolName: string; toolCallId?: string; skill?: string }): void {
    const span = this.child(`execute_tool ${tool.toolName}`, SpanKind.INTERNAL, {
      "gen_ai.operation.name": "execute_tool",
      "gen_ai.tool.name": tool.toolName,
      ...(tool.toolCallId ? { "gen_ai.tool.call.id": tool.toolCallId } : {}),
      ...(tool.skill ? { "openclaw.skill": tool.skill } : {}),
      "openclaw.blocked": true,
    });
    span.end();
  }

  toolEnded(tool: { toolName: string; toolCallId?: string; error?: string; endTime?: number }): void {
    // Without an ID, the oldest open call of that tool is the one ending.
    const key = tool.toolCallId ?? [...this.tools.keys()].find((k) => k.startsWith(`${tool.toolName}#`));
    const span = key === undefined ? undefined : this.tools.get(key);
    if (key === undefined || !span) return;
    this.tools.delete(key);
    if (tool.error) {
      span.setAttribute("error.type", "tool_error");
      span.setStatus({ code: SpanStatusCode.ERROR, message: tool.error });
    }
    span.end(tool.endTime);
  }

  // servedModels has one entry per model call that didn't fail, in order, as servedCalls() returns them.
  finish(result: {
    success: boolean;
    usage?: { input: number; output: number };
    servedModels?: (string | undefined)[];
    endTime?: number;
  }): void {
    if (this.finished) return;
    this.finished = true;
    clearTimeout(this.timeout);
    const endTime = result.endTime ?? Date.now();
    this.calls.filter((call) => !call.failed).forEach((call, index) => {
      const served = result.servedModels?.[index];
      if (served) call.span.setAttribute("gen_ai.response.model", served);
    });
    const last = this.calls.at(-1);
    if (last && result.usage && result.usage.input + result.usage.output > 0) {
      last.span.setAttributes({
        "gen_ai.usage.input_tokens": result.usage.input,
        "gen_ai.usage.output_tokens": result.usage.output,
        "openclaw.usage.scope": "run",
      });
    }
    for (const call of this.calls) call.span.end(call.endTime ?? endTime);
    for (const span of this.tools.values()) span.end(endTime);
    this.tools.clear();
    if (!result.success) this.root.setStatus({ code: SpanStatusCode.ERROR });
    this.root.end(endTime);
  }
}

export class RunTracer {
  private readonly provider: BasicTracerProvider;
  private readonly tracer: ReturnType<BasicTracerProvider["getTracer"]>;
  private readonly runs = new Map<string, RunTrace>();
  private exportFailures = 0;

  constructor(exporter: SpanExporter) {
    const reporting: SpanExporter = {
      export: (spans, done) => {
        exporter.export(spans, (result) => {
          if (result.code !== 0) this.exportFailures += 1;
          done(result);
        });
      },
      shutdown: () => exporter.shutdown(),
      forceFlush: () => exporter.forceFlush?.() ?? Promise.resolve(),
    };
    this.provider = new BasicTracerProvider({
      // The environment's attributes win, so OTEL_SERVICE_NAME can replace the default service name.
      resource: resourceFromAttributes({ "service.name": SERVICE_NAME }).merge(detectResources({ detectors: [envDetector] })),
      spanProcessors: [new BatchSpanProcessor(reporting)],
    });
    this.tracer = this.provider.getTracer("ld-agentcontrol");
  }

  start(info: RunInfo): RunTrace {
    const existing = this.runs.get(info.runId);
    if (existing) return existing;
    const run = new RunTrace(this.tracer, info, () => this.runs.delete(info.runId));
    this.runs.set(info.runId, run);
    return run;
  }

  get(runId: string): RunTrace | undefined {
    return this.runs.get(runId);
  }

  // Removes the run and ends its spans. Returns the trace ID, for the decision log.
  finish(runId: string, result: Parameters<RunTrace["finish"]>[0]): string | undefined {
    const run = this.runs.get(runId);
    if (!run) return undefined;
    this.runs.delete(runId);
    run.finish(result);
    return run.traceId;
  }

  // Export batches that failed since the last call. The plugin reports them as a LaunchDarkly metric event.
  takeExportFailures(): number {
    const failures = this.exportFailures;
    this.exportFailures = 0;
    return failures;
  }

  flush(): Promise<void> {
    return this.provider.forceFlush();
  }

  shutdown(): Promise<void> {
    return this.provider.shutdown();
  }
}

let shared: RunTracer | null | undefined;

// One tracer per process. OpenClaw calls a plugin's register() more than once, and each call would otherwise start
// its own exporter. Returns null when OTEL_SDK_DISABLED=true.
export function runTracer(): RunTracer | null {
  if (shared === undefined) {
    shared = process.env.OTEL_SDK_DISABLED === "true" ? null : new RunTracer(new OTLPTraceExporter());
  }
  return shared;
}
