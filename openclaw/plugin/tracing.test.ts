// Run with: node --test openclaw/plugin/tracing.test.ts
import assert from "node:assert/strict";
import { test } from "node:test";

import { InMemorySpanExporter, type ReadableSpan } from "@opentelemetry/sdk-trace-base";

import { RunTracer } from "./tracing.ts";

const T0 = Date.UTC(2026, 9, 30, 18, 0, 0);
const ms = (span: ReadableSpan, which: "startTime" | "endTime") => span[which][0] * 1000 + span[which][1] / 1e6;

async function traceRun(build: (tracer: RunTracer) => void): Promise<ReadableSpan[]> {
  const exporter = new InMemorySpanExporter();
  const tracer = new RunTracer(exporter);
  build(tracer);
  await tracer.flush();
  return exporter.getFinishedSpans();
}

test("a run with two model calls and a tool call", async () => {
  const spans = await traceRun((tracer) => {
    const run = tracer.start({ runId: "run-1", agentId: "main", conversationId: "session-1", startTime: T0 });
    run.linkConfig({ configKey: "openclaw_agent", variationKey: "standard", variationIndex: 0, contextId: "operator", environmentId: "env-1" });
    run.modelCallStarted({ provider: "openai", model: "gpt-5.4-mini", startTime: T0 + 100 });
    run.modelCallEnded({ outcome: "completed", timeToFirstByteMs: 80, endTime: T0 + 900 });
    run.toolStarted({ toolName: "exec", toolCallId: "call-1", startTime: T0 + 950 });
    run.toolEnded({ toolName: "exec", toolCallId: "call-1", endTime: T0 + 1000 });
    run.modelCallStarted({ provider: "openai", model: "gpt-5.4-mini", startTime: T0 + 1100 });
    run.modelCallEnded({ outcome: "completed", endTime: T0 + 1500 });
    tracer.finish("run-1", {
      success: true,
      usage: { input: 1200, output: 40 },
      servedModels: ["gpt-5.4-mini-2026-03-17", "gpt-5.4-mini-2026-03-17"],
      endTime: T0 + 1600,
    });
  });

  const root = spans.find((span) => span.name === "invoke_agent main")!;
  const chats = spans.filter((span) => span.name === "chat gpt-5.4-mini");
  const tool = spans.find((span) => span.name === "execute_tool exec")!;
  assert.equal(spans.length, 4);
  for (const child of [...chats, tool]) {
    assert.equal(child.spanContext().traceId, root.spanContext().traceId);
    assert.equal(child.parentSpanContext?.spanId, root.spanContext().spanId);
    assert.equal(child.attributes["gen_ai.conversation.id"], "session-1");
  }

  assert.equal(root.attributes["gen_ai.operation.name"], "invoke_agent");
  assert.equal(root.attributes["launchdarkly.config.key"], "openclaw_agent");
  assert.equal(root.attributes["launchdarkly.variation.key"], "standard");
  const link = root.events.find((event) => event.name === "feature_flag")!;
  assert.deepEqual(link.attributes, {
    "feature_flag.key": "openclaw_agent",
    "feature_flag.provider.name": "LaunchDarkly",
    "feature_flag.context.id": "operator",
    "feature_flag.result.variationIndex": 0,
    "feature_flag.set.id": "env-1",
  });
  assert.equal(ms(root, "startTime"), T0);
  assert.equal(ms(root, "endTime"), T0 + 1600);

  // Chat spans end when their model call ended, though they stay open until the run finishes.
  assert.deepEqual(chats.map((span) => ms(span, "endTime")).sort(), [T0 + 900, T0 + 1500]);
  const [first, last] = chats.sort((a, b) => ms(a, "startTime") - ms(b, "startTime"));
  assert.equal(first.attributes["gen_ai.usage.input_tokens"], undefined);
  assert.equal(last.attributes["gen_ai.usage.input_tokens"], 1200);
  assert.equal(last.attributes["gen_ai.usage.output_tokens"], 40);
  assert.equal(last.attributes["openclaw.usage.scope"], "run");
  assert.equal(first.attributes["gen_ai.response.model"], "gpt-5.4-mini-2026-03-17");
  assert.equal(first.attributes["openclaw.ttfb_ms"], 80);
  assert.equal(tool.attributes["gen_ai.tool.call.id"], "call-1");
});

test("a blocked run has only its root span, marked with the reason", async () => {
  const spans = await traceRun((tracer) => {
    const run = tracer.start({ runId: "run-2", agentId: "helper", conversationId: "session-2", startTime: T0 });
    run.linkConfig({ configKey: "openclaw_agent", variationKey: "disabled", contextId: "operator" });
    run.blocked("config_disabled");
    tracer.finish("run-2", { success: false, endTime: T0 + 10 });
  });
  assert.equal(spans.length, 1);
  assert.equal(spans[0].attributes["openclaw.blocked_reason"], "config_disabled");
  assert.equal(spans[0].status.code, 2);
  const link = spans[0].events.find((event) => event.name === "feature_flag")!;
  assert.equal(link.attributes?.["feature_flag.set.id"], undefined);
  assert.equal(link.attributes?.["feature_flag.result.variationIndex"], undefined);
});

test("blocked tools and failed model calls are recorded", async () => {
  const spans = await traceRun((tracer) => {
    const run = tracer.start({ runId: "run-3", agentId: "helper", conversationId: "session-3", startTime: T0 });
    run.modelCallStarted({ model: "gpt-5.4-nano", startTime: T0 + 1 });
    run.modelCallEnded({ outcome: "error", errorCategory: "auth", endTime: T0 + 2 });
    run.toolBlocked({ toolName: "load_skill", toolCallId: "call-2", skill: "openclaw-workspace-report@1" });
    tracer.finish("run-3", { success: false, endTime: T0 + 3 });
  });
  const chat = spans.find((span) => span.name === "chat gpt-5.4-nano")!;
  assert.equal(chat.attributes["error.type"], "auth");
  assert.equal(chat.status.code, 2);
  assert.equal(chat.attributes["gen_ai.usage.input_tokens"], undefined);
  const blocked = spans.find((span) => span.name === "execute_tool load_skill")!;
  assert.equal(blocked.attributes["openclaw.blocked"], true);
  assert.equal(blocked.attributes["openclaw.skill"], "openclaw-workspace-report@1");
});

test("starting a run twice returns the same trace, and finishing twice is a no-op", async () => {
  const spans = await traceRun((tracer) => {
    const info = { runId: "run-4", agentId: "main", conversationId: "session-4", startTime: T0 };
    assert.equal(tracer.start(info), tracer.start(info));
    assert.ok(tracer.finish("run-4", { success: true }));
    assert.equal(tracer.finish("run-4", { success: true }), undefined);
  });
  assert.equal(spans.length, 1);
});

test("open tool spans end with the run", async () => {
  const spans = await traceRun((tracer) => {
    const run = tracer.start({ runId: "run-5", agentId: "main", conversationId: "session-5", startTime: T0 });
    run.toolStarted({ toolName: "exec", toolCallId: "call-3", startTime: T0 + 1 });
    tracer.finish("run-5", { success: true, endTime: T0 + 5 });
  });
  const tool = spans.find((span) => span.name === "execute_tool exec")!;
  assert.equal(ms(tool, "endTime"), T0 + 5);
});

test("served models skip failed calls, as servedCalls() does", async () => {
  const spans = await traceRun((tracer) => {
    const run = tracer.start({ runId: "run-6", agentId: "main", conversationId: "session-6", startTime: T0 });
    run.modelCallStarted({ model: "gpt-5.4-mini", startTime: T0 + 1 });
    run.modelCallEnded({ outcome: "error", errorCategory: "timeout", endTime: T0 + 2 });
    run.modelCallStarted({ model: "gpt-5.4-mini", startTime: T0 + 3 });
    run.modelCallEnded({ outcome: "completed", endTime: T0 + 4 });
    tracer.finish("run-6", { success: true, servedModels: ["gpt-5.4-mini-2026-03-17"], endTime: T0 + 5 });
  });
  const [failed, served] = spans
    .filter((span) => span.name === "chat gpt-5.4-mini")
    .sort((a, b) => ms(a, "startTime") - ms(b, "startTime"));
  assert.equal(failed.attributes["gen_ai.response.model"], undefined);
  assert.equal(served.attributes["gen_ai.response.model"], "gpt-5.4-mini-2026-03-17");
});

test("same-named tool calls without IDs end in the order they started", async () => {
  const spans = await traceRun((tracer) => {
    const run = tracer.start({ runId: "run-7", agentId: "main", conversationId: "session-7", startTime: T0 });
    run.toolStarted({ toolName: "exec", startTime: T0 + 1 });
    run.toolStarted({ toolName: "exec", startTime: T0 + 2 });
    run.toolEnded({ toolName: "exec", endTime: T0 + 3 });
    run.toolEnded({ toolName: "exec", endTime: T0 + 4 });
    tracer.finish("run-7", { success: true, endTime: T0 + 10 });
  });
  const tools = spans
    .filter((span) => span.name === "execute_tool exec")
    .sort((a, b) => ms(a, "startTime") - ms(b, "startTime"));
  assert.deepEqual(tools.map((span) => ms(span, "endTime")), [T0 + 3, T0 + 4]);
});

test("OTEL_SERVICE_NAME replaces the default service name", async () => {
  process.env.OTEL_SERVICE_NAME = "openclaw-elsewhere";
  try {
    const spans = await traceRun((tracer) => {
      tracer.start({ runId: "run-8", agentId: "main", conversationId: "session-8", startTime: T0 });
      tracer.finish("run-8", { success: true });
    });
    assert.equal(spans[0].resource.attributes["service.name"], "openclaw-elsewhere");
  } finally {
    delete process.env.OTEL_SERVICE_NAME;
  }
  const spans = await traceRun((tracer) => {
    tracer.start({ runId: "run-9", agentId: "main", conversationId: "session-9", startTime: T0 });
    tracer.finish("run-9", { success: true });
  });
  assert.equal(spans[0].resource.attributes["service.name"], "openclaw-agentcontrol");
});
